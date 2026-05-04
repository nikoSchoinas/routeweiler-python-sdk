"""Reaper background-task integration tests.

These tests drive the real asyncio.create_task reaper loop end-to-end,
in contrast to test_reaper.py which calls _reap_sync() directly.

The BudgetStore is constructed with reaper_interval_seconds=0.05 (50 ms)
so the tests complete quickly.  Production code is unaffected — the kwarg
defaults to REAPER_INTERVAL_SECONDS (5 s).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from routewiler.budgets.keystore import EnvelopeKeystore
from routewiler.budgets.local import BudgetStore
from routewiler.errors import BudgetExceededError

# Reaper fires every 50 ms in tests — short enough to keep suite fast.
_FAST_REAPER = 0.05
# How long to sleep to guarantee at least one reaper tick with margin.
_REAPER_WAIT = _FAST_REAPER * 4


# ---------------------------------------------------------------------------
# Helpers (mirror the pattern in test_reaper.py)
# ---------------------------------------------------------------------------


def _set_draw_expired(db_path: Path, draw_id: str) -> None:
    """Back-date a draw's expires_at so the reaper sees it as expired."""
    past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE draws SET expires_at=? WHERE id=?", (past, draw_id))
    conn.commit()
    conn.close()


def _draw_state(db_path: Path, draw_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT state FROM draws WHERE id=?", (draw_id,)).fetchone()
    conn.close()
    return str(row[0]) if row else ""


def _reserved_count(db_path: Path, envelope_id: str) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM draws WHERE envelope_id=? AND state='reserved'",
        (envelope_id,),
    ).fetchone()[0]
    conn.close()
    return int(count)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_keystore(tmp_path: Path) -> EnvelopeKeystore:
    return EnvelopeKeystore(root=tmp_path / "keys")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_reaper_task_rolls_back_expired_draw(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """The background reaper transitions an expired reserved draw to rolled_back
    and frees its capacity for a follow-up draw.
    """
    db = tmp_path / "reap_integ.db"
    store = BudgetStore(db, tmp_keystore, reaper_interval_seconds=_FAST_REAPER)

    await store.create_envelope(
        "env_ri",
        cap_minor_units=10,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    receipt = await store.draw(
        envelope_id="env_ri",
        request_id="req_ri",
        idempotency_key="k_ri",
        amount_reserved_minor_units=10,
        rail_quoted="x402",
    )
    assert _draw_state(db, receipt.receipt_id) == "reserved"

    # Cap is now fully reserved — a new draw would fail.
    with pytest.raises(BudgetExceededError):
        await store.draw(
            envelope_id="env_ri",
            request_id="req_ri_2",
            idempotency_key="k_ri_2",
            amount_reserved_minor_units=1,
            rail_quoted="x402",
        )

    _set_draw_expired(db, receipt.receipt_id)
    await store.start()
    await asyncio.sleep(_REAPER_WAIT)

    assert _draw_state(db, receipt.receipt_id) == "rolled_back"

    # Capacity freed — follow-up draw succeeds.
    r2 = await store.draw(
        envelope_id="env_ri",
        request_id="req_ri_3",
        idempotency_key="k_ri_3",
        amount_reserved_minor_units=10,
        rail_quoted="x402",
    )
    assert r2.amount_reserved_minor_units == 10

    await store.aclose()


async def test_reaper_freed_capacity_visible_to_concurrent_drawers(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """Reserve the full cap, expire all draws, then concurrent tasks succeed.

    After one reaper iteration frees the capacity, M concurrent drawers
    (M ≤ cap) must all complete successfully.
    """
    db = tmp_path / "reap_freed.db"
    store = BudgetStore(db, tmp_keystore, reaper_interval_seconds=_FAST_REAPER)

    cap = 10
    envelope_id = "env_freed"
    await store.create_envelope(
        envelope_id,
        cap_minor_units=cap,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    # Fill the envelope entirely with reserved draws.
    receipts = []
    for i in range(cap):
        r = await store.draw(
            envelope_id=envelope_id,
            request_id=f"fill_{i}",
            idempotency_key=f"fill_k_{i}",
            amount_reserved_minor_units=1,
            rail_quoted="x402",
        )
        receipts.append(r)

    assert _reserved_count(db, envelope_id) == cap

    # Back-date all reserved draws.
    for r in receipts:
        _set_draw_expired(db, r.receipt_id)

    await store.start()
    await asyncio.sleep(_REAPER_WAIT)

    assert _reserved_count(db, envelope_id) == 0

    # Now cap concurrent drawers should all succeed.
    async def _draw_one(i: int) -> None:
        await store.draw(
            envelope_id=envelope_id,
            request_id=f"new_{i}",
            idempotency_key=f"new_k_{i}",
            amount_reserved_minor_units=1,
            rail_quoted="x402",
        )

    await asyncio.gather(*[_draw_one(i) for i in range(cap)])
    assert _reserved_count(db, envelope_id) == cap

    await store.aclose()


async def test_reaper_idempotent_under_concurrent_active_rollback(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """Racing reaper tick and explicit rollback: exactly one transitions the row.

    Both _reap_sync and _rollback_sync UPDATE draws WHERE state='reserved', so
    only one can win.  The final state must be rolled_back and must never flip.
    """
    db = tmp_path / "reap_race.db"
    store = BudgetStore(db, tmp_keystore, reaper_interval_seconds=_FAST_REAPER)

    await store.create_envelope(
        "env_race",
        cap_minor_units=10,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    receipt = await store.draw(
        envelope_id="env_race",
        request_id="r_race",
        idempotency_key="k_race",
        amount_reserved_minor_units=5,
        rail_quoted="x402",
    )

    _set_draw_expired(db, receipt.receipt_id)
    await store.start()

    # Concurrently: explicit rollback races the reaper.
    await asyncio.gather(
        store.rollback(receipt.receipt_id),
        asyncio.sleep(_REAPER_WAIT),
    )

    state = _draw_state(db, receipt.receipt_id)
    assert state == "rolled_back"

    # Read again — must be stable.
    assert _draw_state(db, receipt.receipt_id) == "rolled_back"

    await store.aclose()


async def test_reaper_survives_iteration_failure(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """A failing reaper iteration logs the error and the loop continues.

    Patch _reap_sync to raise on the first call, then succeed on subsequent
    calls.  Assert the task is still alive after the failure.
    """
    db = tmp_path / "reap_err.db"
    store = BudgetStore(db, tmp_keystore, reaper_interval_seconds=_FAST_REAPER)

    call_count = 0
    original_reap = store._reap_sync

    def _raise_once() -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("synthetic reaper failure")
        return original_reap()

    store._reap_sync = _raise_once  # type: ignore[method-assign]

    await store.start()
    # Wait long enough for at least two iterations.
    await asyncio.sleep(_REAPER_WAIT * 3)

    # Task must still be running despite the first-iteration failure.
    assert store._reaper_task is not None
    assert not store._reaper_task.done()
    assert call_count >= 2  # at least one failure + one success

    await store.aclose()
