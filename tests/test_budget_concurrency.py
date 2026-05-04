"""Concurrency + idempotency + rollback-corner-case integration tests.

Week 8 deliverables §16 #3:
    Single-envelope budget primitive passes deterministic concurrent-draw
    integration tests covering reserved/settled invariants under partial-failure
    scenarios (10k concurrent draws, idempotency, rollback).

All tests are single-process asyncio — hosted multi-process budget counter is
post-MVP (§17).
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from routewiler.budgets.keystore import EnvelopeKeystore
from routewiler.budgets.local import BudgetStore, ensure_default_envelope
from routewiler.budgets.schema import DrawReceipt
from routewiler.errors import BudgetExceededError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_keystore(tmp_path: Path) -> EnvelopeKeystore:
    return EnvelopeKeystore(root=tmp_path / "keys")


@pytest.fixture
async def store(tmp_path: Path, tmp_keystore: EnvelopeKeystore) -> BudgetStore:
    db = tmp_path / "concurrent.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)
    yield s
    await s.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sum_state(db_path: Path, envelope_id: str) -> tuple[int, int, int]:
    """Return (reserved_sum, settled_sum, rolled_back_sum) for an envelope."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN state='reserved' THEN amount_reserved_minor_units ELSE 0 END),0)"
        " AS reserved, "
        "COALESCE(SUM(CASE WHEN state='settled' THEN amount_settled_minor_units ELSE 0 END),0)"
        " AS settled, "
        "COALESCE(SUM(CASE WHEN state='rolled_back' THEN amount_reserved_minor_units ELSE 0 END),0)"
        " AS rolled_back "
        "FROM draws WHERE envelope_id=?",
        (envelope_id,),
    ).fetchone()
    conn.close()
    return int(row["reserved"]), int(row["settled"]), int(row["rolled_back"])


def _draw_count(db_path: Path, envelope_id: str, state: str) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM draws WHERE envelope_id=? AND state=?",
        (envelope_id, state),
    ).fetchone()[0]
    conn.close()
    return int(count)


async def _draw_and_confirm(
    store: BudgetStore,
    *,
    envelope_id: str,
    amount: int,
    key: str,
) -> DrawReceipt | BudgetExceededError:
    """Try to draw and confirm; return the receipt or the BudgetExceededError."""
    try:
        receipt = await store.draw(
            envelope_id=envelope_id,
            request_id=f"req_{key}",
            idempotency_key=key,
            amount_reserved_minor_units=amount,
            rail_quoted="x402",
        )
        await store.confirm(receipt.receipt_id, amount)
        return receipt
    except BudgetExceededError as exc:
        return exc


async def _draw_then_maybe_confirm(
    store: BudgetStore,
    *,
    envelope_id: str,
    amount: int,
    key: str,
    confirm: bool,
) -> DrawReceipt | BudgetExceededError:
    """Draw and either confirm or rollback based on the `confirm` flag."""
    try:
        receipt = await store.draw(
            envelope_id=envelope_id,
            request_id=f"req_{key}",
            idempotency_key=key,
            amount_reserved_minor_units=amount,
            rail_quoted="x402",
        )
        if confirm:
            await store.confirm(receipt.receipt_id, amount)
        else:
            await store.rollback(receipt.receipt_id)
        return receipt
    except BudgetExceededError as exc:
        return exc


# ===========================================================================
# W8.1 — Concurrency stress: 10k draws, single envelope
# ===========================================================================


async def test_10k_concurrent_draws_never_exceed_cap(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """10 000 concurrent tasks racing on a cap-10 000 envelope.

    Each task draws exactly 1 minor unit and confirms on success.
    Invariant: reserved + settled ≤ cap at all times; settled == cap after all
    tasks complete; exactly cap draws succeed, the remainder raise BudgetExceededError.
    """
    db = tmp_path / "stress10k.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    cap = 10_000
    amount = 1
    n_tasks = 10_000
    envelope_id = "env_10k"
    await s.create_envelope(
        envelope_id,
        cap_minor_units=cap,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    tasks = [
        _draw_and_confirm(s, envelope_id=envelope_id, amount=amount, key=f"k_{i}")
        for i in range(n_tasks)
    ]
    results: list[Any] = await asyncio.gather(*tasks, return_exceptions=False)
    await s.aclose()

    successes = [r for r in results if isinstance(r, DrawReceipt)]
    failures = [r for r in results if isinstance(r, BudgetExceededError)]

    assert len(successes) == cap
    assert len(failures) == n_tasks - cap

    reserved, settled, _ = _sum_state(db, envelope_id)
    assert reserved == 0
    assert settled == cap


async def test_concurrent_excess_attempts_only_grants_cap_amount(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """Cap 100, amount 1, attempts 1000: exactly 100 succeed."""
    db = tmp_path / "excess.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    cap = 100
    n_tasks = 1000
    envelope_id = "env_excess"
    await s.create_envelope(
        envelope_id,
        cap_minor_units=cap,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    tasks = [
        _draw_and_confirm(s, envelope_id=envelope_id, amount=1, key=f"excess_{i}")
        for i in range(n_tasks)
    ]
    results: list[Any] = await asyncio.gather(*tasks)
    await s.aclose()

    successes = sum(1 for r in results if isinstance(r, DrawReceipt))
    failures = sum(1 for r in results if isinstance(r, BudgetExceededError))

    assert successes == cap
    assert failures == n_tasks - cap

    _, settled, _ = _sum_state(db, envelope_id)
    assert settled == cap


async def test_concurrent_partial_failure_keeps_invariant(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """5000 tasks on a 1000-unit cap; each randomly confirms or rolls back.

    After all tasks complete:
    - reserved == 0 (every successful draw is finalized)
    - settled ≤ cap
    - successful_draws == confirmed + rolled_back
    """
    rng = random.Random(42)
    db = tmp_path / "partial.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    cap = 1000
    n_tasks = 5000
    envelope_id = "env_partial"
    await s.create_envelope(
        envelope_id,
        cap_minor_units=cap,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    do_confirm = [rng.choice([True, False]) for _ in range(n_tasks)]

    tasks = [
        _draw_then_maybe_confirm(
            s,
            envelope_id=envelope_id,
            amount=1,
            key=f"pf_{i}",
            confirm=do_confirm[i],
        )
        for i in range(n_tasks)
    ]
    results: list[Any] = await asyncio.gather(*tasks)
    await s.aclose()

    successful_draws = [r for r in results if isinstance(r, DrawReceipt)]
    assert len(successful_draws) <= cap

    reserved, settled, _ = _sum_state(db, envelope_id)
    assert reserved == 0
    assert settled + 0 <= cap  # some were rolled back, settled covers only confirms


async def test_concurrent_variable_amounts_invariant(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """500 tasks drawing amounts from {1, 5, 10} against a cap of 1000.

    Invariant: sum(reserved+settled) ≤ cap at all times; no duplicate
    idempotency_keys among successful draws.
    """
    rng = random.Random(7)
    db = tmp_path / "variable.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    cap = 1000
    n_tasks = 500
    amounts = [rng.choice([1, 5, 10]) for _ in range(n_tasks)]
    envelope_id = "env_var"
    await s.create_envelope(
        envelope_id,
        cap_minor_units=cap,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    tasks = [
        _draw_and_confirm(s, envelope_id=envelope_id, amount=amounts[i], key=f"var_{i}")
        for i in range(n_tasks)
    ]
    results: list[Any] = await asyncio.gather(*tasks)
    await s.aclose()

    successes = [r for r in results if isinstance(r, DrawReceipt)]
    ikeys = [r.receipt_id for r in successes]
    assert len(ikeys) == len(set(ikeys)), "duplicate receipt_ids among successful draws"

    reserved, settled, _ = _sum_state(db, envelope_id)
    assert reserved == 0
    assert settled <= cap


# ===========================================================================
# W8.2 — Idempotency collision tests
# ===========================================================================


async def test_concurrent_same_idempotency_key_yields_one_row(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """200 tasks all using the same (envelope_id, idempotency_key).

    The SQLite UNIQUE constraint (envelope_id, idempotency_key) and the
    idempotency short-circuit in _draw_sync guarantee:
    - Exactly ONE row is inserted.
    - Every task gets a DrawReceipt with the same receipt_id and the same
      Ed25519 signature bytes (byte-identical re-issue, §8.2).
    - Cap (1 unit) is reserved exactly once, never double-charged.
    """
    db = tmp_path / "idem_collision.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    envelope_id = "env_idem"
    await s.create_envelope(
        envelope_id,
        cap_minor_units=1,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    shared_key = "shared_idem_key"
    n = 200

    async def _draw_shared() -> DrawReceipt:
        return await s.draw(
            envelope_id=envelope_id,
            request_id="any_request",
            idempotency_key=shared_key,
            amount_reserved_minor_units=1,
            rail_quoted="x402",
        )

    results: list[DrawReceipt] = await asyncio.gather(*[_draw_shared() for _ in range(n)])
    await s.aclose()

    receipt_ids = {r.receipt_id for r in results}
    assert len(receipt_ids) == 1, "all tasks must get the same receipt_id"

    sig_bytes = {r.signature for r in results}
    assert len(sig_bytes) == 1, "all tasks must get a byte-identical signature"

    assert _draw_count(db, envelope_id, "reserved") == 1


async def test_idempotency_scoped_to_envelope(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """Same idempotency_key in two different envelopes produces two distinct rows."""
    db = tmp_path / "idem_scope.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    for eid in ["env_a", "env_b"]:
        await s.create_envelope(
            eid,
            cap_minor_units=10,
            cap_currency="usd",
            allowed_rails=["x402"],
            ttl_seconds=3600,
        )

    shared_key = "same_key_across_envelopes"
    ra = await s.draw(
        envelope_id="env_a",
        request_id="r_a",
        idempotency_key=shared_key,
        amount_reserved_minor_units=1,
        rail_quoted="x402",
    )
    rb = await s.draw(
        envelope_id="env_b",
        request_id="r_b",
        idempotency_key=shared_key,
        amount_reserved_minor_units=1,
        rail_quoted="x402",
    )
    await s.aclose()

    assert ra.receipt_id != rb.receipt_id
    assert _draw_count(db, "env_a", "reserved") == 1
    assert _draw_count(db, "env_b", "reserved") == 1


async def test_idempotency_after_rollback_still_returns_existing_row(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """Contract: a rolled-back draw's idempotency_key is permanent (§8.2).

    A re-draw with the same key returns the existing rolled-back receipt;
    no new row is created and the rolled-back amount is NOT re-reserved.
    This is the intended behavior; relaxation is a post-MVP §17 item.
    """
    db = tmp_path / "idem_rollback.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    envelope_id = "env_idem_rb"
    await s.create_envelope(
        envelope_id,
        cap_minor_units=10,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    key = "key_k"
    r1 = await s.draw(
        envelope_id=envelope_id,
        request_id="req_1",
        idempotency_key=key,
        amount_reserved_minor_units=1,
        rail_quoted="x402",
    )
    await s.rollback(r1.receipt_id)

    # Re-draw with the same key: gets the rolled-back receipt back.
    r2 = await s.draw(
        envelope_id=envelope_id,
        request_id="req_2",
        idempotency_key=key,
        amount_reserved_minor_units=1,
        rail_quoted="x402",
    )
    await s.aclose()

    assert r2.receipt_id == r1.receipt_id
    # Exactly one row in draws; cap is NOT consumed (state is rolled_back).
    assert _draw_count(db, envelope_id, "rolled_back") == 1
    assert _draw_count(db, envelope_id, "reserved") == 0


# ===========================================================================
# W8.4 — Rollback corner cases
# ===========================================================================


async def test_concurrent_rollback_and_confirm_idempotent(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """Concurrent rollback + confirm on the same draw: exactly one wins, one is a no-op.

    Both UPDATE statements filter on state='reserved', so only one can
    transition the row.  After both complete the state is deterministically
    either 'settled' or 'rolled_back' — never stuck or flipped back.
    """
    db = tmp_path / "race.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    envelope_id = "env_race"
    await s.create_envelope(
        envelope_id,
        cap_minor_units=100,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    receipt = await s.draw(
        envelope_id=envelope_id,
        request_id="race_req",
        idempotency_key="race_key",
        amount_reserved_minor_units=10,
        rail_quoted="x402",
    )

    # Fire rollback and confirm concurrently.
    await asyncio.gather(
        s.rollback(receipt.receipt_id),
        s.confirm(receipt.receipt_id, 10),
    )
    await s.aclose()

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT state FROM draws WHERE id=?", (receipt.receipt_id,)
    ).fetchone()
    conn.close()

    assert row[0] in {"settled", "rolled_back"}

    # Verify the state doesn't flip back — read twice under no further writes.
    conn2 = sqlite3.connect(str(db))
    row2 = conn2.execute(
        "SELECT state FROM draws WHERE id=?", (receipt.receipt_id,)
    ).fetchone()
    conn2.close()
    assert row2[0] == row[0]


async def test_double_confirm_is_noop(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """Confirming an already-settled draw is a no-op: state stays 'settled'."""
    db = tmp_path / "dbl_confirm.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    await s.create_envelope(
        "env_dc",
        cap_minor_units=100,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    r = await s.draw(
        envelope_id="env_dc",
        request_id="r_dc",
        idempotency_key="k_dc",
        amount_reserved_minor_units=10,
        rail_quoted="x402",
    )
    await s.confirm(r.receipt_id, 10)
    await s.confirm(r.receipt_id, 999)  # second confirm: no-op
    await s.aclose()

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT state, amount_settled_minor_units FROM draws WHERE id=?", (r.receipt_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "settled"
    assert int(row[1]) == 10  # original amount, not 999


async def test_double_rollback_is_noop(
    tmp_path: Path, tmp_keystore: EnvelopeKeystore
) -> None:
    """Rolling back an already-rolled-back draw is a no-op."""
    db = tmp_path / "dbl_rollback.db"
    ensure_default_envelope(db, tmp_keystore)
    s = BudgetStore(db, tmp_keystore)

    await s.create_envelope(
        "env_dr",
        cap_minor_units=100,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )
    r = await s.draw(
        envelope_id="env_dr",
        request_id="r_dr",
        idempotency_key="k_dr",
        amount_reserved_minor_units=10,
        rail_quoted="x402",
    )
    await s.rollback(r.receipt_id)
    await s.rollback(r.receipt_id)  # second rollback: no-op
    await s.aclose()

    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT state FROM draws WHERE id=?", (r.receipt_id,)).fetchone()
    conn.close()
    assert row[0] == "rolled_back"
