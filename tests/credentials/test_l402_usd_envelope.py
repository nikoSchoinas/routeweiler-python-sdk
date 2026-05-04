"""W12.2 — L402 payment drawing against a USD-denominated budget envelope.

Verifies:
  1. A 50-sat invoice is paid and the draw is settled.
  2. The FMV conversion (5000 sats x 0.00065 USD/sat x 1.05 buffer = 342 cents) is
     applied correctly during cap enforcement.
  3. The trace event records ``amount_envelope`` and ``fmv_quality="coingecko_simple"``.

No real Lightning node or CoinGecko call is needed — ``FakeLndClient`` provides
the preimage and ``_StubFmvProvider`` returns a fixed rate.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx

from routewiler import Routewiler
from routewiler.budgets.keystore import EnvelopeKeystore
from routewiler.budgets.local import BudgetStore, ensure_default_envelope
from routewiler.errors import FmvUnavailableError
from routewiler.funding.lightning import LightningFundingSource
from routewiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import MOCK_PREIMAGE, mock_l402_app

# ---------------------------------------------------------------------------
# Stub FMV provider — fixed sats→USD rate for tests.
# ---------------------------------------------------------------------------

_SATS_USD_RATE = Decimal("0.00065")  # ~$65 000/BTC for test purposes


class _StubFmvProvider:
    """Returns a fixed sats→fiat rate; never hits CoinGecko."""

    def __init__(self, rate: Decimal = _SATS_USD_RATE) -> None:
        self._rate = rate

    async def fetch_btc_to(self, currency: str) -> Decimal:
        if currency.lower() == "usd":
            return self._rate
        raise FmvUnavailableError(f"StubFmvProvider only knows USD, got {currency!r}")


# ---------------------------------------------------------------------------
# DB query helpers
# ---------------------------------------------------------------------------

_ENVELOPE_ID = "research-usd"


def _draw_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM draws WHERE envelope_id = ? ORDER BY issued_at", (_ENVELOPE_ID,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Test client factory
# ---------------------------------------------------------------------------


async def _make_usd_envelope_client(
    transport: httpx.ASGITransport,
    db_path: Path,
    keystore_root: Path,
) -> Routewiler:
    """Build a Routewiler client wired to a USD envelope seeded with sats rates.

    Creates the ``research-usd`` envelope via a BudgetStore with a stub FMV
    provider so the sats→USD snapshot is persisted before the client reads it.
    """
    keystore = EnvelopeKeystore(root=keystore_root)
    ensure_default_envelope(db_path, keystore)

    # Create the USD envelope with a stub FMV provider so the sats→USD rate
    # is stored in the ``envelope_fmv_snapshots`` table.
    setup_store = BudgetStore(db_path, keystore, fmv_provider=_StubFmvProvider())
    await setup_store.create_envelope(
        _ENVELOPE_ID,
        cap_minor_units=100_000,  # $1 000.00
        cap_currency="usd",
        allowed_rails=["l402"],
        ttl_seconds=3600,
    )
    await setup_store.aclose()

    source = LightningFundingSource(
        client=FakeLndClient(preimage=MOCK_PREIMAGE),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )
    sink = TraceSink.sqlite(db_path, url_mode="raw")

    client = Routewiler(
        funding=[source],
        trace_sink=sink,
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


async def test_l402_usd_envelope_returns_200(tmp_path: Path, tmp_trace_db_path: Path) -> None:
    """Full L402 flow against a USD envelope returns 200 to the caller."""
    transport = httpx.ASGITransport(app=mock_l402_app)  # type: ignore[arg-type]
    client = await _make_usd_envelope_client(transport, tmp_trace_db_path, tmp_path / "keys")

    resp = await client.get("http://mock/protected")
    await client.aclose()

    assert resp.status_code == 200
    assert resp.json() == {"result": "ok", "rail": "l402"}


async def test_l402_usd_envelope_draw_settled_with_correct_amount(
    tmp_path: Path, tmp_trace_db_path: Path
) -> None:
    """5000 sats x 0.00065 USD/sat x 1.05 buffer = 3.4125 -> ceil to 342 cents reserved."""
    transport = httpx.ASGITransport(app=mock_l402_app)  # type: ignore[arg-type]
    client = await _make_usd_envelope_client(transport, tmp_trace_db_path, tmp_path / "keys")

    await client.get("http://mock/protected")
    await client.aclose()

    rows = _draw_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "settled"
    assert row["amount_reserved_minor_units"] == 342  # ceil(5000 x 0.00065 x 1.05 x 100)
    assert row["envelope_id"] == _ENVELOPE_ID
    assert row["rail_quoted"] == "l402"


async def test_l402_usd_envelope_trace_has_fmv_quality(
    tmp_path: Path, tmp_trace_db_path: Path
) -> None:
    """Trace event records ``fmv_quality='coingecko_simple'`` and a non-null ``amount_envelope``."""
    transport = httpx.ASGITransport(app=mock_l402_app)  # type: ignore[arg-type]
    client = await _make_usd_envelope_client(transport, tmp_trace_db_path, tmp_path / "keys")

    await client.get("http://mock/protected")
    await client.aclose()

    rows = _trace_rows(tmp_trace_db_path)
    paid_events = [r for r in rows if r.get("selected_rail") == "l402"]
    assert len(paid_events) == 1

    payload = json.loads(paid_events[0]["payload"])
    payment = payload["payment"]

    assert payment["amountEnvelopeCurrency"] == "usd"
    assert payment["fmvQuality"] == "coingecko_simple"
    assert payment["amountEnvelope"] is not None
    # 5000 sats x 0.00065 USD/sat = 3.25 USD (no buffer on trace, informational only)
    assert abs(payment["amountEnvelope"] - 3.25) < 1e-9


async def test_l402_usd_envelope_trace_proof_matches_preimage(
    tmp_path: Path, tmp_trace_db_path: Path
) -> None:
    """Trace records the correct Lightning preimage proof."""
    transport = httpx.ASGITransport(app=mock_l402_app)  # type: ignore[arg-type]
    client = await _make_usd_envelope_client(transport, tmp_trace_db_path, tmp_path / "keys")

    await client.get("http://mock/protected")
    await client.aclose()

    rows = _trace_rows(tmp_trace_db_path)
    paid_events = [r for r in rows if r.get("selected_rail") == "l402"]
    payload = json.loads(paid_events[0]["payload"])

    assert payload["payment"]["proofType"] == "preimage"
    assert payload["payment"]["proofValue"] == MOCK_PREIMAGE.hex()
