"""End-to-end tests for the x402 happy path against the in-process ASGI mock.

These tests exercise the full SDK stack:
    ASGI mock server → 402 → X402Adapter.parse/sign → retry → 200
    → X402Adapter.parse_settlement → TraceEmitter → SqliteTraceSink

No network, no on-chain settlement, no real x402 SDK wallet signing needed
because the mock server only validates the structural shape of PAYMENT-SIGNATURE.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from routewiler import Funding, Routewiler
from routewiler.errors import RailNotSupportedError
from routewiler.trace.sink_sqlite import TraceSink
from tests.fixtures.x402_mock_server import MOCK_TX_HASH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_client(
    test_account,  # type: ignore[no-untyped-def]
    transport: httpx.ASGITransport,
    db_path: Path,
) -> Routewiler:
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    with patch("routewiler.rails.x402.x402Client") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.create_payment_payload = AsyncMock(
            return_value={
                "x402Version": 1,
                "payload": {
                    "authorization": {
                        "from": test_account.address,
                        "to": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                        "value": "1000",
                        "validAfter": "0",
                        "validBefore": "9999999999",
                        "nonce": "0xdeadbeef",
                    },
                    "signature": "0x" + "ab" * 65,
                },
            }
        )
        mock_cls.return_value = mock_instance

        client = Routewiler(
            funding=[Funding.base_sepolia_usdc(wallet=test_account)],
            trace_sink=sink,
        )
        # Swap the httpx transport to the mock server.
        # We close and reopen the underlying client with the custom transport.
        client._http = httpx.AsyncClient(
            auth=client._http.auth,
            event_hooks=client._http.event_hooks,
            transport=transport,
        )
    return client


# ---------------------------------------------------------------------------
# Happy path: 402 → sign → 200 with trace
# ---------------------------------------------------------------------------


async def test_happy_path_returns_200_and_writes_trace(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    """Full x402 flow: 402 → sign → retry → 200; one trace row written."""
    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path)

    resp = await client.get("http://mock/protected")
    await client.aclose()

    assert resp.status_code == 200
    assert resp.json() == {"result": "ok"}

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]

    # Core trace fields
    assert row["selected_rail"] == "x402"
    assert row["http_status"] == 200
    assert row["service_delivered"] == 1
    assert row["envelope_id"] == "default"
    assert row["shipped_at"] is None

    # FMV: base-sepolia USDC (1000 base units) → 0.001 USD stablecoin peg
    assert row["amount_native"] == "1000"
    assert (
        "usdc" in row["amount_native_currency"].lower() or "erc20" in row["amount_native_currency"]
    )
    assert abs(float(row["amount_envelope"]) - 0.001) < 1e-9
    assert row["amount_envelope_currency"] == "usd"
    assert row["fmv_quality"] == "stablecoin_peg"

    # Settlement proof
    payload = json.loads(row["payload"])
    assert payload["payment"]["proofValue"] == MOCK_TX_HASH
    assert payload["payment"]["proofType"] == "txid"
    assert payload["policyHash"] == "none"


# ---------------------------------------------------------------------------
# Passthrough: 200 direct → trace with selected_rail="none"
# ---------------------------------------------------------------------------


async def test_passthrough_writes_trace(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    """Non-402 response produces a passthrough trace with selected_rail='none'."""
    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path)

    resp = await client.get("http://mock/free")
    await client.aclose()

    assert resp.status_code == 200
    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]

    assert row["selected_rail"] == "none"
    assert row["http_status"] == 200
    assert row["service_delivered"] == 1
    payload = json.loads(row["payload"])
    assert payload["payment"] is None
    assert payload["challenge"] is None


# ---------------------------------------------------------------------------
# Unsupported rail error: 402 without PAYMENT-REQUIRED → error trace
# ---------------------------------------------------------------------------


async def test_unsupported_rail_writes_error_trace(
    test_account,
    tmp_trace_db_path: Path,
) -> None:
    """A 402 with an unknown rail writes one error trace before raising."""
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    client = Routewiler(
        funding=[Funding.base_sepolia_usdc(wallet=test_account)],
        trace_sink=sink,
    )

    # Patch transport to return an L402-style 402 (no PAYMENT-REQUIRED header).
    with respx.mock:
        respx.get("https://api.example.com/l402-only").mock(
            return_value=httpx.Response(
                402,
                headers={"WWW-Authenticate": 'L402 macaroon="abc", invoice="lnbc..."'},
            )
        )
        with pytest.raises(RailNotSupportedError):
            await client.get("https://api.example.com/l402-only")

    await client.aclose()

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["selected_rail"] == "none"
    assert row["http_status"] == 402
    assert row["service_delivered"] == 0
    payload = json.loads(row["payload"])
    assert payload["outcome"]["error"]["code"] == "RailNotSupportedError"
