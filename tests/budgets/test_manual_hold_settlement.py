"""Budget accounting under MANUAL_HOLD and failed-retry conditions.

Confirms that once adapter.pay() returns successfully, the budget draw is
settled regardless of the HTTP outcome.  A MANUAL_HOLD credential records the
undelivered service; the envelope cap reflects the real spend.

The mock BOLT-11 invoice charges 5000 sats.
Stub FMV rate 0.00065 USD/sat: 5000 x 0.00065 x 1.05 buffer -> ceil(341.25) = 342 cents.
So cap_minor_units=342 exhausts after exactly one L402 payment.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from routeweiler import BudgetExceededError, Routeweiler
from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.credentials.schema import CredentialState
from routeweiler.errors import FmvUnavailableError, NoFeasibleRailError
from routeweiler.funding.lightning import LightningFundingSource
from routeweiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import MOCK_PREIMAGE, MOCK_WWW_AUTHENTICATE

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------
# Stub FMV provider — fixed sats→USD rate, no CoinGecko call.
# ---------------------------------------------------------------------------

_SATS_USD_RATE = Decimal("0.00065")  # ~$65 000/BTC

# Amount reserved per mock payment: ceil(5000 sats x 0.00065 USD/sat x 1.05 x 100 cents)
_CENTS_PER_PAYMENT = 342


class _StubFmvProvider:
    async def fetch_btc_to(self, currency: str) -> Decimal:
        if currency.lower() == "usd":
            return _SATS_USD_RATE
        raise FmvUnavailableError(f"StubFmvProvider only knows USD, got {currency!r}")


# ---------------------------------------------------------------------------
# Mock ASGI servers
# ---------------------------------------------------------------------------


def _l402_always_500_app() -> Starlette:
    """L402 server: issues a valid challenge, then returns 500 on the preimage retry."""

    async def endpoint(request: Request) -> Response:
        if request.headers.get("Authorization", "").lower().startswith("l402 "):
            return Response(b"internal error", status_code=500)
        return Response(
            b"payment required",
            status_code=402,
            headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
        )

    return Starlette(routes=[Route("/protected", endpoint)])


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _draw_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM draws ORDER BY issued_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _credential_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM credentials ORDER BY persisted_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

_ENVELOPE_ID = "l402-test-env"


async def _make_l402_budget_client(
    transport: httpx.ASGITransport,
    db_path: Path,
    cap_minor_units: int = 10_000,
    lnd_client: FakeLndClient | None = None,
) -> Routeweiler:
    keystore_root = db_path.parent / "keys"
    keystore = EnvelopeKeystore(root=keystore_root)

    # Seed envelope with FMV snapshot so sats→USD conversion works.
    store = BudgetStore(db_path, keystore, fmv_provider=_StubFmvProvider())
    await store.create_envelope(
        _ENVELOPE_ID,
        cap_minor_units=cap_minor_units,
        cap_currency="usd",
        allowed_rails=["l402"],
        ttl_seconds=3600,
    )
    await store.aclose()

    source = LightningFundingSource(
        client=lnd_client or FakeLndClient(preimage=MOCK_PREIMAGE),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )
    client = Routeweiler(
        funding=[source],
        trace_sink=TraceSink.sqlite(db_path, url_mode="raw"),
        budget_envelope=_ENVELOPE_ID,
        keystore_root=keystore_root,
    )
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_l402_manual_hold_consumes_cap(tmp_path: Path, tmp_trace_db_path: Path) -> None:
    """When the server accepts the L402 preimage but returns 500, the draw is
    settled (not rolled back) and the credential ends up in MANUAL_HOLD.

    Cap set to exactly one payment's worth (342 cents).  After one 500 response
    the envelope is exhausted and the next call raises BudgetExceededError.
    """
    transport = httpx.ASGITransport(app=_l402_always_500_app())  # type: ignore[arg-type]
    client = await _make_l402_budget_client(
        transport, tmp_trace_db_path, cap_minor_units=_CENTS_PER_PAYMENT
    )

    # Payment sent on wire; server returns 500.
    resp = await client.get("http://mock/protected")
    assert resp.status_code == 500

    draws = _draw_rows(tmp_trace_db_path)
    assert len(draws) == 1
    assert draws[0]["state"] == "settled", (
        "Wire payment committed — draw must be settled, not rolled_back"
    )
    assert draws[0]["amount_reserved_minor_units"] == _CENTS_PER_PAYMENT

    creds = _credential_rows(tmp_trace_db_path)
    assert len(creds) == 1
    assert creds[0]["state"] == CredentialState.MANUAL_HOLD

    # Cap is now exhausted — next call must be blocked.
    with pytest.raises(BudgetExceededError) as exc_info:
        await client.get("http://mock/protected")
    await client.aclose()

    assert exc_info.value.envelope_id == _ENVELOPE_ID
    assert exc_info.value.available_minor_units == 0


async def test_pay_phase_failure_does_not_consume_cap(
    tmp_path: Path, tmp_trace_db_path: Path
) -> None:
    """When pay_invoice raises InvoicePaymentError (no preimage returned), the draw
    is rolled back — no funds left the wallet so the cap is untouched.
    """
    transport = httpx.ASGITransport(app=_l402_always_500_app())  # type: ignore[arg-type]
    failing_lnd = FakeLndClient(should_fail=True)
    client = await _make_l402_budget_client(
        transport, tmp_trace_db_path, cap_minor_units=10_000, lnd_client=failing_lnd
    )

    # pay_invoice raises → no wire commit → auth flow exhausts rails.
    with pytest.raises(NoFeasibleRailError):
        await client.get("http://mock/protected")
    await client.aclose()

    draws = _draw_rows(tmp_trace_db_path)
    assert len(draws) == 1
    assert draws[0]["state"] == "rolled_back", "No funds left the wallet — draw must be rolled_back"

    # No credential row: pay() raised before persist.
    creds = _credential_rows(tmp_trace_db_path)
    assert len(creds) == 0
