"""Unit tests for BudgetStore."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.budgets.receipts import canonical_payload
from routeweiler.budgets.receipts import verify as verify_receipt
from routeweiler.budgets.schema import DrawReceipt
from routeweiler.errors import (
    BudgetExceededError,
    EnvelopeExpiredError,
    EnvelopeFrozenError,
    EnvelopeNotFoundError,
    KeystoreAlreadyExistsError,
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
) -> DrawReceipt:
    return await store.draw(
        envelope_id=envelope_id,
        request_id="req_" + (ikey or "a"),
        idempotency_key=ikey or "ikey_a",
        amount_reserved_minor_units=amount,
        rail_quoted="x402",
    )


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


async def test_create_envelope_writes_fmv_snapshot(
    tmp_budget_store: BudgetStore, tmp_trace_db_path: Path
) -> None:
    await _make_envelope(tmp_budget_store)
    conn = sqlite3.connect(str(tmp_trace_db_path))
    snap = conn.execute(
        "SELECT rates_json FROM envelope_fmv_snapshots WHERE envelope_id='env_test'"
    ).fetchone()
    conn.close()
    assert snap is not None
    rates = json.loads(snap[0])
    assert "usd->usd" in rates


async def test_create_envelope_duplicate_raises(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store)
    # Normal duplicate: keystore already has the key file → KeystoreAlreadyExistsError
    # fires before the DB insert is even attempted.
    with pytest.raises(KeystoreAlreadyExistsError):
        await _make_envelope(tmp_budget_store)


async def test_create_envelope_no_orphan_key_on_db_failure(
    tmp_budget_store: BudgetStore,
    tmp_trace_db_path: Path,
    tmp_keystore: EnvelopeKeystore,
) -> None:
    """If the DB row already exists but no key file does (e.g. key was manually deleted),
    create_envelope must not leave an orphan key file behind on the IntegrityError path.
    """
    # Seed a DB row directly without going through create_envelope (no key file created).
    conn = sqlite3.connect(str(tmp_trace_db_path))
    now = datetime.now(UTC)
    conn.execute(
        """INSERT INTO envelopes
           (id, cap_minor_units, cap_currency, allowed_rails, allowed_origins_glob,
            status, created_at, expires_at, counter_public_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "env_orphan",
            1000,
            "usd",
            json.dumps(["x402"]),
            json.dumps(["*"]),
            "active",
            now.isoformat(),
            (now + timedelta(days=30)).isoformat(),
            "",
        ),
    )
    conn.commit()
    conn.close()

    # Now create_envelope will: create key file → fail DB insert (IntegrityError) → delete key.
    with pytest.raises(sqlite3.IntegrityError):
        await tmp_budget_store.create_envelope(
            "env_orphan",
            cap_minor_units=1000,
            cap_currency="usd",
            allowed_rails=["x402"],
            ttl_seconds=3600,
        )

    # No orphan key file must remain.
    assert not tmp_keystore.exists("env_orphan"), "orphan key file must be cleaned up"


# ---------------------------------------------------------------------------
# BudgetStore — draw
# ---------------------------------------------------------------------------


async def test_draw_under_cap_succeeds(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store, cap=1000)
    receipt = await _draw(tmp_budget_store, amount=100)
    assert isinstance(receipt, DrawReceipt)
    assert receipt.amount_reserved_minor_units == 100
    assert receipt.rail_quoted == "x402"
    verify_receipt(receipt)  # signature must be valid


async def test_draw_over_cap_raises_budget_exceeded(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store, cap=1000)
    await _draw(tmp_budget_store, amount=600, ikey="ikey_1")
    with pytest.raises(BudgetExceededError) as exc_info:
        await _draw(tmp_budget_store, amount=600, ikey="ikey_2")
    assert exc_info.value.requested_minor_units == 600
    assert exc_info.value.available_minor_units == 400


async def test_draw_idempotent_returns_existing_draw(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store)
    receipt_a = await _draw(tmp_budget_store, amount=100, ikey="same_key")
    receipt_b = await _draw(tmp_budget_store, amount=100, ikey="same_key")
    assert receipt_a.receipt_id == receipt_b.receipt_id


async def test_draw_idempotent_receipt_is_byte_identical(tmp_budget_store: BudgetStore) -> None:
    """Two draws with the same idempotency_key but different request_ids must return
    byte-identical receipts (§8.2: "return the existing receipt unchanged").
    The original request_id is preserved so canonical_payload and signature match.
    """
    await _make_envelope(tmp_budget_store)
    receipt_a = await tmp_budget_store.draw(
        envelope_id="env_test",
        request_id="req_first",
        idempotency_key="idm_byte",
        amount_reserved_minor_units=100,
        rail_quoted="x402",
    )
    receipt_b = await tmp_budget_store.draw(
        envelope_id="env_test",
        request_id="req_second",  # different request_id — must be ignored on replay
        idempotency_key="idm_byte",
        amount_reserved_minor_units=100,
        rail_quoted="x402",
    )
    assert receipt_a.receipt_id == receipt_b.receipt_id
    assert receipt_a.request_id == receipt_b.request_id  # original stored value used
    assert canonical_payload(receipt_a) == canonical_payload(receipt_b)
    assert receipt_a.signature == receipt_b.signature


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
    receipt = await _draw(tmp_budget_store, amount=100)
    await tmp_budget_store.confirm(receipt.receipt_id, 100)
    rows = _draw_rows(tmp_trace_db_path)
    assert rows[0]["state"] == "settled"
    assert rows[0]["amount_settled_minor_units"] == 100
    assert rows[0]["settled_at"] is not None


async def test_confirm_uses_settled_amount_against_cap(
    tmp_budget_store: BudgetStore,
) -> None:
    # Reserved + settled both count against the cap (§8.3).
    await _make_envelope(tmp_budget_store, cap=200)
    receipt_a = await _draw(tmp_budget_store, amount=100, ikey="a")
    await tmp_budget_store.confirm(receipt_a.receipt_id, 100)
    # 100 settled; 100 remaining
    receipt_b = await _draw(tmp_budget_store, amount=100, ikey="b")
    assert receipt_b.amount_reserved_minor_units == 100
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
    receipt = await _draw(tmp_budget_store, amount=100)
    await tmp_budget_store.rollback(receipt.receipt_id)
    rows = _draw_rows(tmp_trace_db_path)
    assert rows[0]["state"] == "rolled_back"


async def test_rollback_frees_capacity(tmp_budget_store: BudgetStore) -> None:
    await _make_envelope(tmp_budget_store, cap=100)
    receipt = await _draw(tmp_budget_store, amount=100, ikey="a")
    # Cap exhausted — next draw would fail
    with pytest.raises(BudgetExceededError):
        await _draw(tmp_budget_store, amount=1, ikey="b")
    # After rollback, capacity is freed
    await tmp_budget_store.rollback(receipt.receipt_id)
    receipt2 = await _draw(tmp_budget_store, amount=100, ikey="c")
    assert receipt2.amount_reserved_minor_units == 100
