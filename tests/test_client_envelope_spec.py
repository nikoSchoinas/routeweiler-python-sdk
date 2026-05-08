"""Tests for the BudgetEnvelopeSpec declarative construction path.

Verifies that a caller can pass a BudgetEnvelopeSpec as budget_envelope and
the client creates (or reuses) the envelope inside __aenter__ — without a
separate client.envelopes.create() call or two-step construction.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from eth_account.signers.local import LocalAccount

from routeweiler import BudgetEnvelopeSpec, EnvelopeNotFoundError, Funding, Routeweiler
from routeweiler.trace.sink_sqlite import TraceSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope_row(db_path: Path, envelope_id: str) -> dict | None:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM envelopes WHERE id=?", (envelope_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _draw_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM draws ORDER BY issued_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_client(
    test_account: LocalAccount,
    transport: httpx.ASGITransport,
    db_path: Path,
    budget_envelope: BudgetEnvelopeSpec | str | None,
    keystore_root: Path | None = None,
) -> Routeweiler:
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

        client = Routeweiler(
            funding=[Funding.base_sepolia_usdc(wallet=test_account)],
            trace_sink=sink,
            budget_envelope=budget_envelope,
            keystore_root=keystore_root,
        )
        client._http = httpx.AsyncClient(
            auth=client._http.auth,
            event_hooks=client._http.event_hooks,
            transport=transport,
        )
    return client


_SPEC = BudgetEnvelopeSpec(
    id="session-spec",
    cap_minor_units=10_000,
    cap_currency="usd",
    allowed_rails=["x402"],
    ttl_seconds=3_600,
)


# ---------------------------------------------------------------------------
# Test 1 — spec creates the envelope on __aenter__
# ---------------------------------------------------------------------------


async def test_spec_creates_envelope_on_aenter(
    test_account: LocalAccount,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """Entering the client context with a BudgetEnvelopeSpec must create the envelope row."""
    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path, _SPEC, tmp_path / "keys")
    assert _envelope_row(tmp_trace_db_path, "session-spec") is None  # not yet

    async with client:
        row = _envelope_row(tmp_trace_db_path, "session-spec")
        assert row is not None
        assert row["cap_minor_units"] == 10_000
        assert row["cap_currency"] == "usd"
        assert json.loads(row["allowed_rails"]) == ["x402"]
        assert row["status"] == "active"


# ---------------------------------------------------------------------------
# Test 2 — spec is a no-op when the envelope already exists
# ---------------------------------------------------------------------------


async def test_spec_idempotent_when_envelope_exists(
    test_account: LocalAccount,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """Entering the context twice with the same spec must not raise or overwrite the row."""
    ks_root = tmp_path / "keys"
    client1 = _make_client(test_account, mock_x402_app, tmp_trace_db_path, _SPEC, ks_root)
    async with client1:
        pass  # creates the envelope

    client2 = _make_client(test_account, mock_x402_app, tmp_trace_db_path, _SPEC, ks_root)
    # Second enter must not raise even though the envelope already exists.
    async with client2:
        row = _envelope_row(tmp_trace_db_path, "session-spec")
        assert row is not None  # still exists, same data
        assert row["cap_minor_units"] == 10_000


# ---------------------------------------------------------------------------
# Test 3 — spec path pays successfully and settles budget
# ---------------------------------------------------------------------------


async def test_spec_path_pays_and_settles(
    test_account: LocalAccount,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """A full payment via the spec path must settle the budget draw and emit a trace."""
    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path, _SPEC, tmp_path / "keys")
    async with client:
        resp = await client.get("http://mock/protected")

    assert resp.status_code == 200

    draws = _draw_rows(tmp_trace_db_path)
    assert len(draws) == 1
    assert draws[0]["state"] == "settled"
    assert draws[0]["envelope_id"] == "session-spec"

    traces = _trace_rows(tmp_trace_db_path)
    assert len(traces) == 1
    assert traces[0]["selected_rail"] == "x402"
    assert traces[0]["envelope_id"] == "session-spec"


# ---------------------------------------------------------------------------
# Test 4 — legacy str form still raises EnvelopeNotFoundError at construction
# ---------------------------------------------------------------------------


def test_str_form_raises_envelope_not_found(
    test_account: LocalAccount,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """Passing a string id for a non-existent envelope must raise at construction time."""
    sink = TraceSink.sqlite(tmp_trace_db_path, url_mode="raw")
    with pytest.raises(EnvelopeNotFoundError):
        Routeweiler(
            funding=[Funding.base_sepolia_usdc(wallet=test_account)],
            trace_sink=sink,
            budget_envelope="does-not-exist",
            keystore_root=tmp_path / "keys",
        )
