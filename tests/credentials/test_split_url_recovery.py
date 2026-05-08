"""Non-negotiable test — "The Refined Element bug is a passing test."

If this test ever breaks, the PR does not merge.

Scenario (from https://refinedelement.com/blog/l402-broke-at-the-worst-possible-moment):
  1. The agent calls /checkout/{order_id} on an L402-gated shop.
  2. The server returns 402 with a BOLT-11 invoice.
  3. Routeweiler pays the invoice via Lightning and retries with Authorization: L402 ...
  4. The server returns 404 — fulfilment is at a DIFFERENT URL (/orders/{id}/fulfil).
  5. Routeweiler consults the lightning-shop manifest, rewrites the URL, retries there.
  6. The fulfilment endpoint returns 200.
  7. The caller receives the 200 response.
  8. The credential ends in REDEEMED state.

The naive flow (no recovery) would stop at step 4 and return 404 to the caller,
losing the paid-but-undelivered Lightning payment.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import httpx
import pytest

from routeweiler import Routeweiler
from routeweiler.credentials.manifest_strategy import ManifestRecoveryStrategy
from routeweiler.credentials.manifests.loader import ManifestRegistry
from routeweiler.funding.lightning import LightningFundingSource
from routeweiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import MOCK_PREIMAGE
from tests.fixtures.l402_split_url_mock_server import mock_split_url_app

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _credential_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM credentials ORDER BY persisted_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Test-local manifest — domain_matches "mock" to match the ASGI transport host
# ---------------------------------------------------------------------------

_TEST_MANIFEST_YAML = textwrap.dedent(
    """\
    name: test-shop
    domain_matches: "mock"
    flow:
      - challenge_path: "/checkout/*"
        fulfil_path_template: "/orders/{order_id}/fulfil"
        id_extractor: "path:checkout/([^/]+)"
    """
)


def _make_split_url_client(
    tmp_path: Path,
    tmp_trace_db_path: Path,
) -> tuple[Routeweiler, httpx.AsyncClient]:
    """Build a Routeweiler client wired to the split-URL mock app.

    Returns ``(client, recovery_http)`` — the caller is responsible for closing
    ``recovery_http`` after the client is closed.
    """
    transport = httpx.ASGITransport(app=mock_split_url_app)  # type: ignore[arg-type]

    source = LightningFundingSource(
        client=FakeLndClient(preimage=MOCK_PREIMAGE),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")

    # Write the test manifest to a temp file so ManifestRegistry can load it.
    manifest_path = tmp_path / "test-shop.yaml"
    manifest_path.write_text(_TEST_MANIFEST_YAML, encoding="utf-8")

    # Use a separate recovery client so recovery HTTP calls also go through the ASGI app.
    recovery_http = httpx.AsyncClient(transport=transport)
    registry = ManifestRegistry.from_paths([manifest_path])
    strategy = ManifestRecoveryStrategy(registry=registry, client=recovery_http)

    client = Routeweiler(
        funding=[source],
        trace_sink=sink,
        recovery_strategy=strategy,
    )
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )
    return client, recovery_http


# ---------------------------------------------------------------------------
# The non-negotiable test
# ---------------------------------------------------------------------------


async def test_split_url_recovery_returns_200_to_caller(
    tmp_path: Path,
    tmp_trace_db_path: Path,
) -> None:
    """The caller receives the recovered 200 from the fulfilment URL."""
    client, recovery_http = _make_split_url_client(tmp_path, tmp_trace_db_path)

    resp = await client.get("http://mock/checkout/order_123")
    await client.aclose()
    await recovery_http.aclose()

    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == "order_123"
    assert body["status"] == "fulfilled"


async def test_split_url_recovery_credential_ends_in_redeemed(
    tmp_path: Path,
    tmp_trace_db_path: Path,
) -> None:
    """The credential transitions to REDEEMED after split-URL recovery."""
    client, recovery_http = _make_split_url_client(tmp_path, tmp_trace_db_path)

    await client.get("http://mock/checkout/order_123")
    await client.aclose()
    await recovery_http.aclose()

    rows = _credential_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "redeemed", (
        f"Expected REDEEMED after split-URL recovery, got {row['state']!r}"
    )
    assert row["rail"] == "l402"
    assert row["redeemed_at"] is not None


async def test_split_url_recovery_trace_records_200(
    tmp_path: Path,
    tmp_trace_db_path: Path,
) -> None:
    """The trace event records the final 200 (not the intermediate 404)."""
    client, recovery_http = _make_split_url_client(tmp_path, tmp_trace_db_path)

    await client.get("http://mock/checkout/order_123")
    await client.aclose()
    await recovery_http.aclose()

    rows = _trace_rows(tmp_trace_db_path)
    paid_events = [r for r in rows if r.get("selected_rail") == "l402"]
    assert len(paid_events) == 1, (
        f"Expected exactly 1 paid L402 trace event, got {len(paid_events)}"
    )

    row = paid_events[0]
    assert row["http_status"] == 200
    assert row["service_delivered"] == 1

    payload = json.loads(row["payload"])
    assert payload["payment"]["proofType"] == "preimage"
    assert payload["payment"]["proofValue"] == MOCK_PREIMAGE.hex()


async def test_split_url_recovery_no_manual_hold_trace_event(
    tmp_path: Path,
    tmp_trace_db_path: Path,
) -> None:
    """No MANUAL_HOLD trace event is emitted when recovery succeeds."""
    client, recovery_http = _make_split_url_client(tmp_path, tmp_trace_db_path)

    await client.get("http://mock/checkout/order_123")
    await client.aclose()
    await recovery_http.aclose()

    rows = _trace_rows(tmp_trace_db_path)
    manual_hold_events = [
        r for r in rows if json.loads(r["payload"]).get("credentialState") == "manual_hold"
    ]
    assert len(manual_hold_events) == 0, (
        f"Expected no MANUAL_HOLD events after successful recovery, got {len(manual_hold_events)}"
    )


async def test_split_url_recovery_no_manifest_match_gives_manual_hold(
    tmp_path: Path,
    tmp_trace_db_path: Path,
) -> None:
    """When no manifest matches, the credential transitions to MANUAL_HOLD(exhausted).

    Uses an empty manifest (different domain), so recovery is impossible and
    the caller receives the original 4xx response.
    """
    transport = httpx.ASGITransport(app=mock_split_url_app)  # type: ignore[arg-type]

    source = LightningFundingSource(
        client=FakeLndClient(preimage=MOCK_PREIMAGE),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")

    # Manifest with a non-matching domain — recovery will exhaust.
    manifest_path = tmp_path / "no-match.yaml"
    manifest_path.write_text(
        "name: other\ndomain_matches: '*.other.com'\nflow: []\n", encoding="utf-8"
    )
    recovery_http = httpx.AsyncClient(transport=transport)
    registry = ManifestRegistry.from_paths([manifest_path])
    strategy = ManifestRecoveryStrategy(registry=registry, client=recovery_http)

    client = Routeweiler(funding=[source], trace_sink=sink, recovery_strategy=strategy)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )

    resp = await client.get("http://mock/checkout/order_123")
    await client.aclose()
    await recovery_http.aclose()

    # No matching manifest → recovery exhausted → original 404 returned to caller.
    assert resp.status_code == 404

    rows = _credential_rows(tmp_trace_db_path)
    assert rows[0]["state"] == "manual_hold"
    assert rows[0]["manual_hold_reason"] == "exhausted"


@pytest.mark.parametrize("order_id", ["order_abc", "order-xyz-99", "12345"])
async def test_split_url_recovery_extracts_various_order_ids(
    order_id: str,
    tmp_path: Path,
    tmp_trace_db_path: Path,
) -> None:
    """Parametrised: recovery succeeds for various order ID formats."""
    client, recovery_http = _make_split_url_client(tmp_path, tmp_trace_db_path)

    resp = await client.get(f"http://mock/checkout/{order_id}")
    await client.aclose()
    await recovery_http.aclose()

    assert resp.status_code == 200
    assert resp.json()["order_id"] == order_id
    assert resp.json()["status"] == "fulfilled"
