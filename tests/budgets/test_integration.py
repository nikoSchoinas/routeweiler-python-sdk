"""Budget enforcement integration tests — routeweiler.get() + x402 mock + SQLite counter.

Tests the full stack end-to-end:
    ASGI mock server → 402 → BudgetStore.draw() → X402Adapter.sign() → retry → 200 / 5xx
    → BudgetStore.confirm() / rollback() → TraceEmitter → SqliteTraceSink

These are the milestone tests for Week 4:
    routeweiler.get(...) pays via x402, logs a trace event, and enforces a flat cap.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from eth_account.signers.local import LocalAccount
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from routeweiler import BudgetExceededError, Funding, Routeweiler
from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.budgets.schema import BudgetEnvelopeSpec
from routeweiler.errors import FmvUnavailableError
from routeweiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import (
    MOCK_PREIMAGE,
)
from tests.fixtures.l402_mock_server import (
    mock_l402_app as _mock_l402_app,
)
from tests.fixtures.x402_mock_server import MOCK_CHALLENGE_B64, MOCK_TX_HASH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _draw_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM draws ORDER BY issued_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_client(
    test_account: LocalAccount,
    transport: httpx.ASGITransport,
    db_path: Path,
    budget_envelope: str | BudgetEnvelopeSpec | None = None,
    keystore_root: Path | None = None,
) -> Routeweiler:
    """Build a Routeweiler client backed by the given ASGI transport and DB.

    Patches the x402 SDK so no on-chain interaction happens.
    """
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    with patch("routeweiler.rails.x402.x402Client") as mock_cls:
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

        kwargs: dict = {}
        if budget_envelope is not None:
            kwargs["budget_envelope"] = budget_envelope
        if keystore_root is not None:
            kwargs["keystore_root"] = keystore_root

        client = Routeweiler(
            funding=[Funding.base_sepolia_usdc(wallet=test_account)],
            trace_sink=sink,
            **kwargs,
        )
        client._http = httpx.AsyncClient(
            auth=client._http.auth,
            event_hooks=client._http.event_hooks,
            transport=transport,
        )
    return client


_TEST_ENVELOPE = BudgetEnvelopeSpec(
    id="test_env",
    cap_minor_units=10_000,
    cap_currency="usd",
    allowed_rails=["x402"],
    ttl_seconds=86_400,
)


def _make_failing_server() -> httpx.ASGITransport:
    """An x402 server that always returns 500 on the signed retry."""

    async def endpoint(request: Request) -> Response:
        if request.headers.get("PAYMENT-SIGNATURE"):
            return JSONResponse({"error": "internal server error"}, status_code=500)
        return Response(
            content=b"payment required",
            status_code=402,
            headers={"PAYMENT-REQUIRED": MOCK_CHALLENGE_B64},
        )

    app = Starlette(routes=[Route("/protected", endpoint)])
    return httpx.ASGITransport(app=app)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test 1 — milestone test: pays, settles, writes trace, decrements cap
# ---------------------------------------------------------------------------


async def test_routeweiler_get_pays_x402_settles_under_cap(
    test_account: LocalAccount,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    """
    MILESTONE: routeweiler.get(...) pays via x402, logs a trace event, enforces a flat cap.

    One call against the default 10000-cent cap; the mock challenge is 1000 base units
    of Base-Sepolia USDC = 1 cent. Asserts:
    - HTTP 200 returned to the caller.
    - Exactly one trace row with selected_rail='x402'.
    - Exactly one draws row in state='settled'.
    - Cap arithmetic: reserved == settled == 1 (cent).
    """
    client = _make_client(
        test_account,
        mock_x402_app,
        tmp_trace_db_path,
        _TEST_ENVELOPE,
        keystore_root=tmp_trace_db_path.parent / "keys",
    )
    async with client:
        resp = await client.get("http://mock/protected")

    assert resp.status_code == 200
    assert resp.json() == {"result": "ok"}

    traces = _trace_rows(tmp_trace_db_path)
    assert len(traces) == 1
    assert traces[0]["selected_rail"] == "x402"
    assert traces[0]["http_status"] == 200
    assert traces[0]["service_delivered"] == 1
    assert traces[0]["envelope_id"] == "test_env"

    # Settlement proof in trace payload
    payload = json.loads(traces[0]["payload"])
    assert payload["payment"]["proofValue"] == MOCK_TX_HASH

    draws = _draw_rows(tmp_trace_db_path)
    assert len(draws) == 1
    assert draws[0]["state"] == "settled"
    assert draws[0]["amount_reserved_minor_units"] == 1  # 1 cent (1000 USDC base units)
    assert draws[0]["amount_settled_minor_units"] == 1
    assert draws[0]["envelope_id"] == "test_env"
    assert draws[0]["rail_quoted"] == "x402"


# ---------------------------------------------------------------------------
# Test 2 — cap exhausted blocks payment before retry hits the server
# ---------------------------------------------------------------------------


async def test_second_call_blocked_when_cap_exhausted(
    test_account: LocalAccount,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
) -> None:
    """After the cap is fully consumed, the next call raises BudgetExceededError
    before the retry reaches the mock server (enforcement is pre-pay).
    """
    # Create a 1-cent envelope so the first call exhausts it entirely.
    keystore_root = tmp_trace_db_path.parent / "keys"
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    await sink.aclose()  # close immediately; we only needed the DDL
    keystore = EnvelopeKeystore(root=keystore_root)
    store = BudgetStore(tmp_trace_db_path, keystore)
    await store.create_envelope(
        "tiny_env",
        cap_minor_units=1,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    await store.aclose()

    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path, "tiny_env", keystore_root)

    # First call succeeds and settles 1 cent (= the entire cap).
    resp = await client.get("http://mock/protected")
    assert resp.status_code == 200

    # Second call: cap is now fully settled; draw of 1 cent exceeds what remains (0).
    with pytest.raises(BudgetExceededError) as exc_info:
        await client.get("http://mock/protected")
    await client.aclose()

    assert exc_info.value.envelope_id == "tiny_env"
    assert exc_info.value.available_minor_units == 0

    traces = _trace_rows(tmp_trace_db_path)
    assert len(traces) == 2
    # First trace is a successful x402 payment.
    assert traces[0]["selected_rail"] == "x402"
    assert traces[0]["http_status"] == 200
    # Second trace is the budget-exceeded error (payment was never attempted).
    second_payload = json.loads(traces[1]["payload"])
    assert second_payload["outcome"]["error"]["code"] == "BudgetExceededError"
    assert second_payload["payment"] is None  # payment block is None when not attempted

    draws = _draw_rows(tmp_trace_db_path)
    # Only the first call produced a draw row.
    assert len(draws) == 1
    assert draws[0]["state"] == "settled"


# ---------------------------------------------------------------------------
# Test 3 — failed retry rolls back the reservation
# ---------------------------------------------------------------------------


async def test_failed_retry_rolls_back_reservation(
    test_account: LocalAccount,
    tmp_trace_db_path: Path,
) -> None:
    """When the merchant returns 500 on the signed retry, the reserved draw is
    rolled back so the capacity is available for subsequent calls.
    """
    failing_transport = _make_failing_server()
    client = _make_client(
        test_account,
        failing_transport,
        tmp_trace_db_path,
        _TEST_ENVELOPE,
        keystore_root=tmp_trace_db_path.parent / "keys",
    )

    async with client:
        # The call settles the payment signature but the server rejects with 500.
        resp = await client.get("http://mock/protected")
        # Routeweiler still returns the final response to the caller (it's not an exception).
        assert resp.status_code == 500

        draws = _draw_rows(tmp_trace_db_path)
        assert len(draws) == 1
        assert draws[0]["state"] == "rolled_back"

        traces = _trace_rows(tmp_trace_db_path)
        assert len(traces) == 1
        assert traces[0]["http_status"] == 500
        assert traces[0]["service_delivered"] == 0

        # Capacity has been freed — a second call can draw again.
        resp2 = await client.get("http://mock/protected")
        assert resp2.status_code == 500  # server still always fails, but no budget block
        draws2 = _draw_rows(tmp_trace_db_path)
        assert len(draws2) == 2
        assert draws2[1]["state"] == "rolled_back"


# ---------------------------------------------------------------------------
# Test 4 — draw idempotency: same idempotency key does not double-count
# ---------------------------------------------------------------------------


async def test_draw_idempotency_no_double_count(tmp_trace_db_path: Path) -> None:
    """Direct BudgetStore: two draws with the same idempotency key return the same
    Draw object and only one row is inserted. Cap is not double-charged.
    """
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    await sink.aclose()

    keystore = EnvelopeKeystore(root=tmp_trace_db_path.parent / "keys2")
    store = BudgetStore(tmp_trace_db_path, keystore)
    await store.create_envelope(
        "idem_env",
        cap_minor_units=1,  # only 1 cent available
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    draw_a = await store.draw(
        envelope_id="idem_env",
        request_id="req_1",
        idempotency_key="fixed_key",
        amount_reserved_minor_units=1,
        rail_quoted="x402",
    )
    draw_b = await store.draw(
        envelope_id="idem_env",
        request_id="req_2",  # different request_id — same idempotency_key
        idempotency_key="fixed_key",
        amount_reserved_minor_units=1,
        rail_quoted="x402",
    )
    await store.aclose()

    # Same receipt_id returned; cap not double-charged.
    assert draw_a.receipt_id == draw_b.receipt_id
    draws = _draw_rows(tmp_trace_db_path)
    assert len(draws) == 1


# ---------------------------------------------------------------------------
# Test 5 — FMV outage fails closed (Gap 2)
# ---------------------------------------------------------------------------


async def test_fmv_outage_raises_for_covered_rail(
    tmp_trace_db_path: Path,
) -> None:
    """When FMV conversion fails for a rail the envelope explicitly covers,
    `FmvUnavailableError` is raised rather than letting payment proceed uncapped.

    Scenario: L402 envelope (allowed_rails=["l402"]) with no fmv_provider configured
    so the FMV snapshot has no sats->usd rate.  When the L402 402 arrives,
    _fmv_quote returns None for the sats-denominated challenge, and the auth flow
    raises FmvUnavailableError instead of proceeding uncapped.
    """
    transport = httpx.ASGITransport(app=_mock_l402_app)  # type: ignore[arg-type]

    # Build a BudgetStore with an l402 envelope but NO fmv_provider — sats->usd absent.
    keystore = EnvelopeKeystore(root=tmp_trace_db_path.parent / "keys_fmv")
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    await sink.start()
    store = BudgetStore(tmp_trace_db_path, keystore)  # no fmv_provider
    await store.create_envelope(
        "l402-env",
        cap_minor_units=50_000,
        cap_currency="usd",
        allowed_rails=["l402"],
        ttl_seconds=3600,
    )

    lnd = FakeLndClient(preimage=MOCK_PREIMAGE)
    client = Routeweiler(
        funding=[
            Funding.lightning_lnd(
                client=lnd, network="bitcoin-regtest", node_pubkey="03" + "ab" * 32
            )
        ],
        trace_sink=sink,
        budget_envelope="l402-env",
        keystore_root=tmp_trace_db_path.parent / "keys_fmv",
    )
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )

    # The l402-env explicitly covers the l402 rail; no sats rate → must raise.
    with pytest.raises(FmvUnavailableError):
        await client.get("http://mock/protected")

    await client.aclose()

    # An error trace must have been emitted.
    traces = _trace_rows(tmp_trace_db_path)
    assert len(traces) == 1
    payload = json.loads(traces[0]["payload"])
    assert payload["outcome"]["error"]["code"] == "FmvUnavailableError"
