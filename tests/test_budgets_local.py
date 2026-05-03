"""Unit tests for BudgetStore and amount_to_envelope_minor_units."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from routewiler.budgets.local import (
    BudgetStore,
    Draw,
    amount_to_envelope_minor_units,
)
from routewiler.errors import (
    BudgetExceededError,
    EnvelopeExpiredError,
    EnvelopeFrozenError,
    EnvelopeNotFoundError,
    PaymentError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _draw_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM draws ORDER BY issued_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _make_envelope(
    store: BudgetStore,
    envelope_id: str = "env_test",
    cap: int = 1000,
) -> None:
    await store.create_envelope(
        envelope_id,
        cap_minor_units=cap,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )


async def _draw(
    store: BudgetStore,
    envelope_id: str = "env_test",
    amount: int = 100,
    ikey: str | None = None,
) -> Draw:
    return await store.draw(
        envelope_id=envelope_id,
        request_id="req_" + (ikey or "a"),
        idempotency_key=ikey or "ikey_a",
        amount_reserved_minor_units=amount,
        rail_quoted="x402",
    )


# ---------------------------------------------------------------------------
# amount_to_envelope_minor_units
# ---------------------------------------------------------------------------

_USDC_BASE = "eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
_USDC_SEPOLIA = "eip155:84532/erc20:0x036cbd53842c5426634e7929541ec2318f3dcf7e"
_EURC_BASE = "eip155:8453/erc20:0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42"


def test_fmv_usdc_to_usd_exact_cent() -> None:
    # 10000 base units = 0.01 USDC = 1 cent
    assert amount_to_envelope_minor_units(_USDC_BASE, 10000, "usd") == 1


def test_fmv_usdc_to_usd_sub_cent_rounds_up() -> None:
    # 1000 base units = 0.001 USDC = 0.1 cents → ceiling → 1 cent
    assert amount_to_envelope_minor_units(_USDC_SEPOLIA, 1000, "usd") == 1


def test_fmv_usdc_to_usd_one_dollar() -> None:
    # 1_000_000 base units = 1 USDC = 100 cents
    assert amount_to_envelope_minor_units(_USDC_BASE, 1_000_000, "usd") == 100


def test_fmv_eurc_to_eur() -> None:
    assert amount_to_envelope_minor_units(_EURC_BASE, 10000, "eur") == 1


def test_fmv_unsupported_asset_raises() -> None:
    with pytest.raises(PaymentError, match="FMV conversion"):
        amount_to_envelope_minor_units("eip155:1/erc20:0xdeadbeef", 1000, "usd")


def test_fmv_usdc_in_eur_envelope_raises() -> None:
    # USDC pegs to USD, not EUR — requires FX leg, not yet supported
    with pytest.raises(PaymentError, match="FMV conversion"):
        amount_to_envelope_minor_units(_USDC_BASE, 1000, "eur")


def test_fmv_non_erc20_raises() -> None:
    with pytest.raises(PaymentError, match="FMV conversion"):
        amount_to_envelope_minor_units("btc-lightning", 50000, "usd")


# ---------------------------------------------------------------------------
# BudgetStore — envelope creation
# ---------------------------------------------------------------------------


async def test_create_envelope_inserts_row(
    tmp_budget_store: BudgetStore, tmp_trace_db_path: Path
) -> None:
    await _make_envelope(tmp_budget_store)
    conn = sqlite3.connect(str(tmp_trace_db_path))
    row = conn.execute("SELECT id, cap_minor_units FROM envelopes WHERE id='env_test'").fetchone()
    conn.close()
    assert row is not None
    assert row[1] == 1000


async def test_create_envelope_duplicate_raises(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store)
    with pytest.raises(sqlite3.IntegrityError):
        await _make_envelope(tmp_budget_store)


# ---------------------------------------------------------------------------
# BudgetStore — draw
# ---------------------------------------------------------------------------


async def test_draw_under_cap_succeeds(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store, cap=1000)
    draw = await _draw(tmp_budget_store, amount=100)
    assert isinstance(draw, Draw)
    assert draw.amount_reserved_minor_units == 100
    assert draw.rail_quoted == "x402"


async def test_draw_over_cap_raises_budget_exceeded(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store, cap=1000)
    await _draw(tmp_budget_store, amount=600, ikey="ikey_1")
    with pytest.raises(BudgetExceededError) as exc_info:
        await _draw(tmp_budget_store, amount=600, ikey="ikey_2")
    assert exc_info.value.requested_minor_units == 600
    assert exc_info.value.available_minor_units == 400


async def test_draw_idempotent_returns_existing_draw(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store)
    draw_a = await _draw(tmp_budget_store, amount=100, ikey="same_key")
    draw_b = await _draw(tmp_budget_store, amount=100, ikey="same_key")
    assert draw_a.id == draw_b.id


async def test_draw_idempotent_does_not_double_count(
    tmp_budget_store: BudgetStore, tmp_trace_db_path: Path
) -> None:
    await _make_envelope(tmp_budget_store, cap=100)
    await _draw(tmp_budget_store, amount=100, ikey="same_key")
    # Second call with same key must NOT insert a new row
    await _draw(tmp_budget_store, amount=100, ikey="same_key")
    rows = _draw_rows(tmp_trace_db_path)
    assert len(rows) == 1


async def test_envelope_not_found_raises(tmp_budget_store: BudgetStore) -> None:
    with pytest.raises(EnvelopeNotFoundError):
        await _draw(tmp_budget_store, envelope_id="does_not_exist")


async def test_envelope_frozen_raises(
    tmp_budget_store: BudgetStore, tmp_trace_db_path: Path
) -> None:
    await _make_envelope(tmp_budget_store)
    # Directly update status to frozen
    conn = sqlite3.connect(str(tmp_trace_db_path))
    conn.execute("UPDATE envelopes SET status='frozen' WHERE id='env_test'")
    conn.commit()
    conn.close()
    with pytest.raises(EnvelopeFrozenError):
        await _draw(tmp_budget_store)


async def test_envelope_revoked_raises(
    tmp_budget_store: BudgetStore, tmp_trace_db_path: Path
) -> None:
    await _make_envelope(tmp_budget_store)
    conn = sqlite3.connect(str(tmp_trace_db_path))
    conn.execute("UPDATE envelopes SET status='revoked' WHERE id='env_test'")
    conn.commit()
    conn.close()
    with pytest.raises(EnvelopeFrozenError):
        await _draw(tmp_budget_store)


async def test_envelope_expired_raises(
    tmp_budget_store: BudgetStore, tmp_trace_db_path: Path
) -> None:
    await _make_envelope(tmp_budget_store)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(str(tmp_trace_db_path))
    conn.execute("UPDATE envelopes SET expires_at=? WHERE id='env_test'", (past,))
    conn.commit()
    conn.close()
    with pytest.raises(EnvelopeExpiredError):
        await _draw(tmp_budget_store)


# ---------------------------------------------------------------------------
# BudgetStore — confirm
# ---------------------------------------------------------------------------


async def test_confirm_marks_settled(
    tmp_budget_store: BudgetStore, tmp_trace_db_path: Path
) -> None:
    await _make_envelope(tmp_budget_store)
    draw = await _draw(tmp_budget_store, amount=100)
    await tmp_budget_store.confirm(draw.id, 100)
    rows = _draw_rows(tmp_trace_db_path)
    assert rows[0]["state"] == "settled"
    assert rows[0]["amount_settled_minor_units"] == 100
    assert rows[0]["settled_at"] is not None


async def test_confirm_uses_settled_amount_against_cap(
    tmp_budget_store: BudgetStore,
) -> None:
    # Reserved + settled both count against the cap (§8.3).
    await _make_envelope(tmp_budget_store, cap=200)
    draw_a = await _draw(tmp_budget_store, amount=100, ikey="a")
    await tmp_budget_store.confirm(draw_a.id, 100)
    # 100 settled; 100 remaining
    draw_b = await _draw(tmp_budget_store, amount=100, ikey="b")
    assert draw_b.amount_reserved_minor_units == 100
    # 100 settled + 100 reserved = 200; no room for more
    with pytest.raises(BudgetExceededError):
        await _draw(tmp_budget_store, amount=1, ikey="c")


# ---------------------------------------------------------------------------
# BudgetStore — rollback
# ---------------------------------------------------------------------------


async def test_rollback_marks_rolled_back(
    tmp_budget_store: BudgetStore, tmp_trace_db_path: Path
) -> None:
    await _make_envelope(tmp_budget_store)
    draw = await _draw(tmp_budget_store, amount=100)
    await tmp_budget_store.rollback(draw.id)
    rows = _draw_rows(tmp_trace_db_path)
    assert rows[0]["state"] == "rolled_back"


async def test_rollback_frees_capacity(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store, cap=100)
    draw = await _draw(tmp_budget_store, amount=100, ikey="a")
    # Cap exhausted — next draw would fail
    with pytest.raises(BudgetExceededError):
        await _draw(tmp_budget_store, amount=1, ikey="b")
    # After rollback, capacity is freed
    await tmp_budget_store.rollback(draw.id)
    draw2 = await _draw(tmp_budget_store, amount=100, ikey="c")
    assert draw2.amount_reserved_minor_units == 100
