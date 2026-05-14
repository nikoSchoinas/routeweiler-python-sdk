"""Non-negotiable test — "The Lightning Enable Store split-URL bug is a passing test."

If this test ever breaks, the PR does not merge.

Scenario (from https://refinedelement.com/blog/l402-broke-at-the-worst-possible-moment):
  1. The agent calls POST /api/store/checkout on the L402-gated store.
  2. The server returns 402 with a BOLT-11 invoice and a macaroon that embeds
     order_id=<id> as a first-party caveat.
  3. Routeweiler pays the invoice via Lightning and retries with Authorization: L402 ...
  4. The server returns 404 — fulfilment is at a DIFFERENT URL (/api/store/claim).
  5. Routeweiler reads the order_id caveat from the macaroon, consults the
     lightning-enable-store manifest, and replays the credential at /api/store/claim.
  6. The fulfilment endpoint returns 200.
  7. The caller receives the 200 response.
  8. The credential ends in REDEEMED state.

A naive client would stop at step 4 and return 404 to the caller,
losing the paid-but-undelivered Lightning payment.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
from pymacaroons import Macaroon  # type: ignore[import-untyped]

from routeweiler import Routeweiler
from routeweiler.credentials.manifest_strategy import ManifestRecoveryStrategy
from routeweiler.credentials.manifests.loader import ManifestRegistry
from routeweiler.credentials.manifests.schema import ServiceShape, ServiceShapeStep
from routeweiler.funding.lightning import LightningFundingSource
from routeweiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import MOCK_PREIMAGE
from tests.fixtures.l402_split_url_mock_server import MOCK_ORDER_ID, mock_split_url_app

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

_TEST_MANIFEST_REGISTRY = ManifestRegistry(
    shapes=(
        ServiceShape(
            name="test-lightning-enable-store",
            domain_matches="mock",
            flow=[
                ServiceShapeStep(
                    challenge_path="/api/store/checkout",
                    fulfil_path_template="/api/store/claim",
                    id_extractor="macaroon:order_id",
                    method="POST",
                )
            ],
        ),
    )
)


def _make_split_url_client(
    tmp_trace_db_path: Path,
) -> tuple[Routeweiler, httpx.AsyncClient]:
    """Build a Routeweiler client wired to the split-URL mock app."""
    transport = httpx.ASGITransport(app=mock_split_url_app)  # type: ignore[arg-type]

    source = LightningFundingSource(
        client=FakeLndClient(preimage=MOCK_PREIMAGE),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")

    recovery_http = httpx.AsyncClient(transport=transport)
    strategy = ManifestRecoveryStrategy(registry=_TEST_MANIFEST_REGISTRY, client=recovery_http)

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
# The non-negotiable tests
# ---------------------------------------------------------------------------


async def test_split_url_recovery_returns_200_to_caller(
    tmp_trace_db_path: Path,
) -> None:
    """The caller receives the recovered 200 from the fulfilment URL."""
    client, recovery_http = _make_split_url_client(tmp_trace_db_path)

    resp = await client.post("http://mock/api/store/checkout")
    await client.aclose()
    await recovery_http.aclose()

    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == MOCK_ORDER_ID
    assert body["status"] == "fulfilled"


async def test_split_url_recovery_credential_ends_in_redeemed(
    tmp_trace_db_path: Path,
) -> None:
    """The credential transitions to REDEEMED after split-URL recovery."""
    client, recovery_http = _make_split_url_client(tmp_trace_db_path)

    await client.post("http://mock/api/store/checkout")
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
    tmp_trace_db_path: Path,
) -> None:
    """The trace event records the final 200 (not the intermediate 404)."""
    client, recovery_http = _make_split_url_client(tmp_trace_db_path)

    await client.post("http://mock/api/store/checkout")
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
    tmp_trace_db_path: Path,
) -> None:
    """No MANUAL_HOLD trace event is emitted when recovery succeeds."""
    client, recovery_http = _make_split_url_client(tmp_trace_db_path)

    await client.post("http://mock/api/store/checkout")
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

    no_match_registry = ManifestRegistry(
        shapes=(ServiceShape(name="other", domain_matches="*.other.com", flow=[]),)
    )
    recovery_http = httpx.AsyncClient(transport=transport)
    strategy = ManifestRecoveryStrategy(registry=no_match_registry, client=recovery_http)

    client = Routeweiler(funding=[source], trace_sink=sink, recovery_strategy=strategy)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )

    resp = await client.post("http://mock/api/store/checkout")
    await client.aclose()
    await recovery_http.aclose()

    assert resp.status_code == 404

    rows = _credential_rows(tmp_trace_db_path)
    assert rows[0]["state"] == "manual_hold"
    assert rows[0]["manual_hold_reason"] == "exhausted"


async def test_macaroon_caveat_extraction(tmp_path: Path) -> None:
    """macaroon:order_id extractor correctly reads the caveat from a serialised macaroon."""
    mac = Macaroon(location="http://example.com", identifier="test-id", key=b"test-key")
    mac.add_first_party_caveat("order_id=ORD-999")
    b64 = mac.serialize()

    step = ServiceShapeStep(
        challenge_path="/api/store/checkout",
        fulfil_path_template="/api/store/claim",
        id_extractor="macaroon:order_id",
        method="POST",
    )

    result = step.extract_id("/api/store/checkout", {"macaroon": b64})
    assert result == "ORD-999"


async def test_macaroon_caveat_extraction_missing_key(tmp_path: Path) -> None:
    """Returns None when the requested caveat key is not present in the macaroon."""
    mac = Macaroon(location="http://example.com", identifier="test-id", key=b"test-key")
    mac.add_first_party_caveat("other_key=value")
    b64 = mac.serialize()

    step = ServiceShapeStep(
        challenge_path="/api/store/checkout",
        fulfil_path_template="/api/store/claim",
        id_extractor="macaroon:order_id",
        method="POST",
    )

    result = step.extract_id("/api/store/checkout", {"macaroon": b64})
    assert result is None


async def test_macaroon_caveat_extraction_no_payload(tmp_path: Path) -> None:
    """Returns None when credential_payload is None."""
    step = ServiceShapeStep(
        challenge_path="/api/store/checkout",
        fulfil_path_template="/api/store/claim",
        id_extractor="macaroon:order_id",
        method="POST",
    )
    assert step.extract_id("/api/store/checkout", None) is None
