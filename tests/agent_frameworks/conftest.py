"""Shared fixtures for agent-framework integration smoke tests.

Each test file imports the ``fw_rw_context`` fixture which yields a fully wired
Routeweiler client (ASGI-mounted x402 merchant, mocked x402Client signer, SQLite
trace sink, declarative BudgetEnvelope) and the path to the SQLite DB for direct
assertion queries.  The Routeweiler context is pre-entered so tests can call
``await rw.get(url)`` without an inner ``async with``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from eth_account.signers.local import LocalAccount

from routeweiler import BudgetEnvelope, Funding, Routeweiler
from routeweiler.trace.sink_sqlite import TraceSink

MERCHANT_URL = "http://mock/protected"


@pytest.fixture
def merchant_url() -> str:
    return MERCHANT_URL


@pytest.fixture
async def fw_rw_context(
    tmp_path: Path,
    test_account: LocalAccount,
    mock_x402_app: httpx.ASGITransport,
) -> AsyncGenerator[tuple[Routeweiler, Path], None]:
    """Yield (Routeweiler, db_path) with the client already entered.

    The x402Client signer is patched to return a structurally valid signed payload
    so no on-chain activity occurs.  The ASGI transport bypasses the network.
    """
    db_path = tmp_path / "fw-traces.db"
    sink = TraceSink.sqlite(db_path, url_mode="raw")

    signed_payload: dict[str, Any] = {
        "x402Version": 2,
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

    with patch("routeweiler.rails.x402.x402Client") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.create_payment_payload = AsyncMock(return_value=signed_payload)
        mock_cls.return_value = mock_instance

        client = Routeweiler(
            funding=[Funding.base_sepolia_usdc(wallet=test_account)],
            trace_sink=sink,
            budget_envelope=BudgetEnvelope(
                id="fw-test",
                cap_minor_units=100_000,
                cap_currency="usd",
                allowed_rails=["x402"],
                ttl_seconds=3_600,
            ),
            keystore_root=tmp_path / "keys",
        )
        # Swap in the ASGI transport so all HTTP traffic hits the mock server.
        client._http = httpx.AsyncClient(
            auth=client._http.auth,
            event_hooks=client._http.event_hooks,
            transport=mock_x402_app,
        )

    async with client:
        yield client, db_path


# ---------------------------------------------------------------------------
# SQLite query helpers (shared across all three framework test files)
# ---------------------------------------------------------------------------


def trace_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def draw_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM draws ORDER BY issued_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]
