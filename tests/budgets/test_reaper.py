"""Unit tests for the BudgetStore reaper background task."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.errors import BudgetExceededError


@pytest.fixture
def tmp_keystore(tmp_path: Path) -> EnvelopeKeystore:
    return EnvelopeKeystore(root=tmp_path / "keys")


def _set_draw_expired(db_path: Path, draw_id: str) -> None:
    """Back-date a draw's expires_at to the past so the reaper fires on it."""
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


# ---------------------------------------------------------------------------
# _reap_sync — direct unit test (no event loop involvement)
# ---------------------------------------------------------------------------


async def test_reap_sync_rolls_back_expired_draw(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    db = tmp_path / "reap.db"
    store = BudgetStore(db, tmp_keystore)

    await store.create_envelope(
        "env_reap",
        cap_minor_units=1000,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    receipt = await store.draw(
        envelope_id="env_reap",
        request_id="req_1",
        idempotency_key="ikey_1",
        amount_reserved_minor_units=100,
        rail_quoted="x402",
    )
    assert _draw_state(db, receipt.receipt_id) == "reserved"

    # Back-date the draw so the reaper sees it as expired.
    _set_draw_expired(db, receipt.receipt_id)

    rolled = store._reap_sync()
    assert rolled == 1
    assert _draw_state(db, receipt.receipt_id) == "rolled_back"

    await store.aclose()


async def test_reap_sync_ignores_settled_and_rolled_back(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    db = tmp_path / "reap2.db"
    store = BudgetStore(db, tmp_keystore)

    await store.create_envelope(
        "env_r2",
        cap_minor_units=1000,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    r1 = await store.draw(
        envelope_id="env_r2",
        request_id="r1",
        idempotency_key="k1",
        amount_reserved_minor_units=100,
        rail_quoted="x402",
    )
    r2 = await store.draw(
        envelope_id="env_r2",
        request_id="r2",
        idempotency_key="k2",
        amount_reserved_minor_units=100,
        rail_quoted="x402",
    )
    await store.confirm(r1.receipt_id, 100)
    await store.rollback(r2.receipt_id)

    # Back-date both — reaper must not touch them.
    _set_draw_expired(db, r1.receipt_id)
    _set_draw_expired(db, r2.receipt_id)

    rolled = store._reap_sync()
    assert rolled == 0

    await store.aclose()


async def test_reap_sync_frees_capacity(tmp_path: Path, tmp_keystore: EnvelopeKeystore) -> None:
    db = tmp_path / "reap3.db"
    store = BudgetStore(db, tmp_keystore)

    await store.create_envelope(
        "env_cap",
        cap_minor_units=100,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    r = await store.draw(
        envelope_id="env_cap",
        request_id="req_cap",
        idempotency_key="k_cap",
        amount_reserved_minor_units=100,
        rail_quoted="x402",
    )
    # Cap is now fully reserved — another draw would fail.
    with pytest.raises(BudgetExceededError):
        await store.draw(
            envelope_id="env_cap",
            request_id="req_cap2",
            idempotency_key="k_cap2",
            amount_reserved_minor_units=1,
            rail_quoted="x402",
        )

    _set_draw_expired(db, r.receipt_id)
    store._reap_sync()

    # Capacity freed — next draw succeeds.
    r2 = await store.draw(
        envelope_id="env_cap",
        request_id="req_cap3",
        idempotency_key="k_cap3",
        amount_reserved_minor_units=100,
        rail_quoted="x402",
    )
    assert r2.amount_reserved_minor_units == 100

    await store.aclose()


# ---------------------------------------------------------------------------
# Lifecycle — start() and aclose() are well-behaved
# ---------------------------------------------------------------------------


async def test_start_spawns_reaper_task(tmp_path: Path, tmp_keystore: EnvelopeKeystore) -> None:
    db = tmp_path / "life.db"
    store = BudgetStore(db, tmp_keystore)

    assert store._reaper_task is None
    await store.start()
    assert store._reaper_task is not None
    assert not store._reaper_task.done()

    await store.aclose()
    assert store._reaper_task.done()


async def test_start_is_idempotent(tmp_path: Path, tmp_keystore: EnvelopeKeystore) -> None:
    db = tmp_path / "idem.db"
    store = BudgetStore(db, tmp_keystore)

    await store.start()
    task_first = store._reaper_task

    await store.start()  # second call must be a no-op
    assert store._reaper_task is task_first

    await store.aclose()


async def test_aclose_is_idempotent(tmp_path: Path, tmp_keystore: EnvelopeKeystore) -> None:
    db = tmp_path / "close.db"
    store = BudgetStore(db, tmp_keystore)
    await store.start()
    await store.aclose()
    await store.aclose()  # second call must not raise
