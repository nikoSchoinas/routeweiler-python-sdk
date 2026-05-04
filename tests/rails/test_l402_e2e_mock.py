"""End-to-end L402 integration test using the in-process mock ASGI server.

Covers the full round-trip:
    402 challenge → parse → match_funding → pay → retry with Authorization
    → 200 response → confirm → trace emission.

No real Lightning node required — a FakeLndClient returns a deterministic
preimage that the mock server validates.

Follows the same pattern as test_x402_e2e_mock.py: Routewiler is constructed
first, then the httpx transport is swapped to the in-process ASGI server.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
from eth_account import Account

from routewiler import Funding, Routewiler
from routewiler.errors import NoFeasibleRailError, RailNotSupportedError
from routewiler.funding.lightning import LightningFundingSource
from routewiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import MOCK_PREIMAGE, MOCK_WWW_AUTHENTICATE, mock_l402_app


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_l402_client(
    transport: httpx.ASGITransport,
    db_path: Path,
) -> Routewiler:
    source = LightningFundingSource(
        client=FakeLndClient(preimage=MOCK_PREIMAGE),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    client = Routewiler(funding=[source], trace_sink=sink)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )
    return client


@pytest.fixture
def l402_transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=mock_l402_app)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy path: 402 → pay → 200 with trace
# ---------------------------------------------------------------------------


async def test_happy_path_returns_200_and_writes_trace(
    l402_transport: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    """Full L402 flow: 402 → pay invoice → retry with Authorization → 200."""
    client = _make_l402_client(l402_transport, tmp_trace_db_path)

    resp = await client.get("http://mock/protected")
    await client.aclose()

    assert resp.status_code == 200
    assert resp.json() == {"result": "ok", "rail": "l402"}

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]

    assert row["selected_rail"] == "l402"
    assert row["http_status"] == 200
    assert row["service_delivered"] == 1

    payload = json.loads(row["payload"])
    assert payload["payment"]["proofType"] == "preimage"
    assert payload["payment"]["proofValue"] == MOCK_PREIMAGE.hex()


# ---------------------------------------------------------------------------
# Passthrough: /free does not trigger payment
# ---------------------------------------------------------------------------


async def test_passthrough_does_not_pay(
    l402_transport: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    client = _make_l402_client(l402_transport, tmp_trace_db_path)

    resp = await client.get("http://mock/free")
    await client.aclose()

    assert resp.status_code == 200
    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    assert rows[0]["selected_rail"] is None


# ---------------------------------------------------------------------------
# Wrong credential: mock server returns 401
# ---------------------------------------------------------------------------


async def test_wrong_preimage_exhausts_rails(
    l402_transport: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    # The adapter's defence-in-depth check raises PreimageMismatchError inside pay().
    # The auth flow catches all pay() exceptions and attempts failover; with only one
    # rail available, it exhausts options and raises NoFeasibleRailError.
    wrong_preimage = bytes(32)  # all-zero preimage; sha256 != MOCK_PAYMENT_HASH
    source = LightningFundingSource(
        client=FakeLndClient(preimage=wrong_preimage),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    client = Routewiler(funding=[source], trace_sink=sink)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=l402_transport,
    )

    with pytest.raises(NoFeasibleRailError):
        await client.get("http://mock/protected")

    await client.aclose()


# ---------------------------------------------------------------------------
# No L402 adapter when no Lightning funding
# ---------------------------------------------------------------------------


async def test_l402_challenge_without_lightning_funding_raises(
    tmp_trace_db_path: Path,
) -> None:
    """With EVM-only funding, an L402 challenge raises RailNotSupportedError."""
    key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    wallet = Account.from_key(key)
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    client = Routewiler(
        funding=[Funding.base_sepolia_usdc(wallet=wallet)],
        trace_sink=sink,
    )

    with respx.mock:
        respx.get("https://api.example.com/l402-only").mock(
            return_value=httpx.Response(
                402,
                headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
            )
        )
        with pytest.raises(RailNotSupportedError):
            await client.get("https://api.example.com/l402-only")

    await client.aclose()
