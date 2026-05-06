"""End-to-end MPP-SPT integration test using the in-process mock ASGI server.

Covers the full round-trip:
    402 challenge → parse → match_funding → pay → retry with Authorization
    → 200 response with Payment-Receipt → confirm → trace emission.

No real Stripe API required — FakeSptCreator returns a deterministic
``spt_test_FAKE...`` id that the mock server validates structurally.

Mirrors test_mpp_tempo_e2e_mock.py.
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
from routeweiler.funding.stripe import StripeFundingSource
from routeweiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_stripe import FakeSptCreator
from tests.fixtures.mpp_spt_mock_server import (
    MOCK_CURRENCY,
    MOCK_CUSTOMER,
    MOCK_PAYMENT_METHOD,
    MOCK_WWW_AUTHENTICATE,
    mock_mpp_spt_app,
)


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_spt_client(
    transport: httpx.ASGITransport,
    db_path: Path,
    *,
    currency: str = MOCK_CURRENCY,
) -> Routeweiler:
    fake_creator = FakeSptCreator()
    source = StripeFundingSource(
        api_key="sk_test_fake",
        customer=MOCK_CUSTOMER,
        payment_method=MOCK_PAYMENT_METHOD,
        currency=currency,
        spt_creator=fake_creator,
    )
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    client = Routeweiler(funding=[source], trace_sink=sink)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )
    return client


@pytest.fixture
def spt_transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=mock_mpp_spt_app)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy path: 402 → pay → 200 with Payment-Receipt + trace
# ---------------------------------------------------------------------------


async def test_happy_path_returns_200_and_writes_trace(
    spt_transport: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    """Full MPP-SPT flow: 402 → mint SPT → retry → 200 + Payment-Receipt."""
    client = _make_spt_client(spt_transport, tmp_trace_db_path)

    resp = await client.get("http://mock/protected")
    await client.aclose()

    assert resp.status_code == 200
    assert resp.json() == {"result": "ok", "rail": "mpp-spt"}

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]

    assert row["selected_rail"] == "mpp-spt"
    assert row["http_status"] == 200
    assert row["service_delivered"] == 1

    payload = json.loads(row["payload"])
    assert payload["payment"]["proofType"] == "spt_id"
    assert payload["payment"]["proofValue"].startswith("spt_test_")
    assert payload["payment"]["amountNativeCurrency"] == "usd-fiat"


# ---------------------------------------------------------------------------
# Passthrough: /free does not trigger payment
# ---------------------------------------------------------------------------


async def test_passthrough_does_not_pay(
    spt_transport: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    client = _make_spt_client(spt_transport, tmp_trace_db_path)

    resp = await client.get("http://mock/free")
    await client.aclose()

    assert resp.status_code == 200
    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    assert rows[0]["selected_rail"] is None


# ---------------------------------------------------------------------------
# No SPT adapter when no Stripe funding
# ---------------------------------------------------------------------------


async def test_mpp_spt_challenge_without_stripe_funding_raises(
    tmp_trace_db_path: Path,
) -> None:
    """With EVM-only funding, an MPP-SPT challenge raises RailNotSupportedError."""
    key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    wallet = Account.from_key(key)
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    client = Routeweiler(
        funding=[Funding.base_sepolia_usdc(wallet=wallet)],
        trace_sink=sink,
    )

    with respx.mock:
        respx.get("https://api.example.com/spt-only").mock(
            return_value=httpx.Response(
                402,
                headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
            )
        )
        with pytest.raises(RailNotSupportedError):
            await client.get("https://api.example.com/spt-only")

    await client.aclose()
