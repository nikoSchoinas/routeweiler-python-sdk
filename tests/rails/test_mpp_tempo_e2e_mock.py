"""End-to-end MPP-Tempo integration test using the in-process mock ASGI server.

Covers the full round-trip:
    402 challenge → parse → match_funding → pay → retry with Authorization
    → 200 response with Payment-Receipt → confirm → trace emission.

No real Tempo chain required — FakeTempoSigner returns a deterministic
synthetic signed tx that the mock server validates structurally.

Mirrors test_l402_e2e_mock.py.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
from eth_account import Account

from routeweiler import Funding, Routeweiler
from routeweiler.errors import RailNotSupportedError
from routeweiler.funding.tempo import TempoFundingSource
from routeweiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_tempo import FakeTempoSigner
from tests.fixtures.mpp_tempo_mock_server import (
    MOCK_CHAIN_ID,
    MOCK_TOKEN,
    MOCK_WWW_AUTHENTICATE,
    mock_mpp_tempo_app,
)


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_mpp_client(
    transport: httpx.ASGITransport,
    db_path: Path,
    *,
    address: str = "0xTestAddress" + "00" * 14,
    chain_id: int = MOCK_CHAIN_ID,
) -> Routeweiler:
    signer = FakeTempoSigner(address=address, chain_id=chain_id)
    source = TempoFundingSource(signer=signer, network="tempo-moderato", asset=MOCK_TOKEN)
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    client = Routeweiler(funding=[source], trace_sink=sink)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )
    return client


@pytest.fixture
def mpp_transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=mock_mpp_tempo_app)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy path: 402 → pay → 200 with Payment-Receipt + trace
# ---------------------------------------------------------------------------


async def test_happy_path_returns_200_and_writes_trace(
    mpp_transport: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    """Full MPP-Tempo flow: 402 → sign tx → retry → 200 + Payment-Receipt."""
    client = _make_mpp_client(mpp_transport, tmp_trace_db_path)

    resp = await client.get("http://mock/protected")
    await client.aclose()

    assert resp.status_code == 200
    assert resp.json() == {"result": "ok", "rail": "mpp-tempo"}

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]

    assert row["selected_rail"] == "mpp-tempo"
    assert row["http_status"] == 200
    assert row["service_delivered"] == 1

    payload = json.loads(row["payload"])
    assert payload["payment"]["proofType"] == "txid"
    # proof_value is keccak256(FAKE_SIGNED_TX), not MOCK_TX_HASH (that's the receipt reference)
    assert payload["payment"]["proofValue"].startswith("0x")


# ---------------------------------------------------------------------------
# Passthrough: /free does not trigger payment
# ---------------------------------------------------------------------------


async def test_passthrough_does_not_pay(
    mpp_transport: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    client = _make_mpp_client(mpp_transport, tmp_trace_db_path)

    resp = await client.get("http://mock/free")
    await client.aclose()

    assert resp.status_code == 200
    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    assert rows[0]["selected_rail"] is None


# ---------------------------------------------------------------------------
# No MPP adapter when no Tempo funding
# ---------------------------------------------------------------------------


async def test_mpp_challenge_without_tempo_funding_raises(
    tmp_trace_db_path: Path,
) -> None:
    """With EVM-only funding, an MPP-Tempo challenge raises RailNotSupportedError."""
    key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    wallet = Account.from_key(key)
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    client = Routeweiler(
        funding=[Funding.base_sepolia_usdc(wallet=wallet)],
        trace_sink=sink,
    )

    with respx.mock:
        respx.get("https://api.example.com/mpp-only").mock(
            return_value=httpx.Response(
                402,
                headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
            )
        )
        with pytest.raises(RailNotSupportedError):
            await client.get("https://api.example.com/mpp-only")

    await client.aclose()
