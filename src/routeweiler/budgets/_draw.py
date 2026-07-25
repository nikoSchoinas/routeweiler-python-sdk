"""Draw-lifecycle sync helpers — BEGIN IMMEDIATE cap-check, confirm, rollback."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import cast

from routeweiler._constants import CLOCK_SKEW_BUFFER_SECONDS
from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.receipts import issue as _issue_receipt
from routeweiler.budgets.receipts import uuid7
from routeweiler.budgets.receipts import verify_against_envelope as _verify_against_envelope
from routeweiler.budgets.schema import DrawReceipt
from routeweiler.errors import (
    BudgetError,
    BudgetExceededError,
    EnvelopeExpiredError,
    EnvelopeFrozenError,
    EnvelopeNotFoundError,
    ReceiptVerificationError,
)
from routeweiler.normalized import Rail

_log = logging.getLogger(__name__)


def draw_sync(
    conn: sqlite3.Connection,
    keystore: EnvelopeKeystore,
    *,
    envelope_id: str,
    request_id: str,
    idempotency_key: str,
    amount_reserved_minor_units: int,
    rail_quoted: Rail,
    ttl_seconds: int,
) -> DrawReceipt:
    """Atomically reserve capacity and return a signed receipt.

    BEGIN IMMEDIATE → cap check → idempotency short-circuit → INSERT → COMMIT.
    Raises BudgetExceededError, EnvelopeNotFoundError, EnvelopeFrozenError,
    or EnvelopeExpiredError on rejection.
    """
    now = datetime.now(UTC)
    # Include clock-skew buffer so the reaper doesn't fire before the
    # active path can confirm/rollback.
    expires = now + timedelta(seconds=ttl_seconds + CLOCK_SKEW_BUFFER_SECONDS)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Load envelope (cap, status, expiry, public key).
        env_row = conn.execute(
            "SELECT cap_minor_units, status, expires_at, cap_currency, counter_public_key "
            "FROM envelopes WHERE id = ?",
            (envelope_id,),
        ).fetchone()
        if env_row is None:
            raise EnvelopeNotFoundError(f"Envelope '{envelope_id}' not found.")
        cap, status, env_expires_raw, cap_currency, pub_key_b64 = env_row

        if status != "active":
            raise EnvelopeFrozenError(
                f"Envelope '{envelope_id}' has status '{status}' (expected 'active')."
            )

        env_expires = datetime.fromisoformat(env_expires_raw)
        if now >= env_expires:
            raise EnvelopeExpiredError(f"Envelope '{envelope_id}' expired at {env_expires_raw}.")

        # Idempotency short-circuit — return a re-signed receipt for the same draw.
        # request_id is re-read from the stored row so the receipt is byte-identical
        # to the one returned on the original call.
        existing = conn.execute(
            "SELECT id, request_id, amount_reserved_minor_units, rail_quoted, "
            "issued_at, expires_at "
            "FROM draws WHERE envelope_id = ? AND idempotency_key = ?",
            (envelope_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            ex_id, ex_req_id, ex_amt, ex_rail, ex_issued, ex_exp = existing
            private_key = keystore.load(envelope_id)
            return _issue_receipt(
                private_key=private_key,
                public_key_b64=str(pub_key_b64),
                receipt_id=str(ex_id),
                envelope_id=envelope_id,
                request_id=str(ex_req_id),
                idempotency_key=idempotency_key,
                amount_reserved_minor_units=int(ex_amt),
                amount_reserved_currency=cap_currency,
                rail_quoted=cast(Rail, str(ex_rail)),
                issued_at=datetime.fromisoformat(str(ex_issued)),
                expires_at=datetime.fromisoformat(str(ex_exp)),
            )

        # Cap check: reserved + settled must not exceed cap after this draw.
        reserved: int = conn.execute(
            "SELECT COALESCE(SUM(amount_reserved_minor_units), 0) FROM draws "
            "WHERE envelope_id = ? AND state = 'reserved'",
            (envelope_id,),
        ).fetchone()[0]
        settled: int = conn.execute(
            "SELECT COALESCE(SUM(amount_settled_minor_units), 0) FROM draws "
            "WHERE envelope_id = ? AND state = 'settled'",
            (envelope_id,),
        ).fetchone()[0]

        available = int(cap) - int(reserved) - int(settled)
        if amount_reserved_minor_units > available:
            raise BudgetExceededError(
                envelope_id=envelope_id,
                requested_minor_units=amount_reserved_minor_units,
                available_minor_units=available,
            )

        draw_id = uuid7()
        conn.execute(
            """
            INSERT INTO draws (
                id, envelope_id, request_id, idempotency_key,
                amount_reserved_minor_units, rail_quoted, state,
                issued_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draw_id,
                envelope_id,
                request_id,
                idempotency_key,
                amount_reserved_minor_units,
                rail_quoted,
                "reserved",
                now.isoformat(),
                expires.isoformat(),
            ),
        )
        conn.execute("COMMIT")

        private_key = keystore.load(envelope_id)
        receipt = _issue_receipt(
            private_key=private_key,
            public_key_b64=str(pub_key_b64),
            receipt_id=draw_id,
            envelope_id=envelope_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            amount_reserved_minor_units=amount_reserved_minor_units,
            amount_reserved_currency=cap_currency,
            rail_quoted=rail_quoted,
            issued_at=now,
            expires_at=expires,
        )
        # Defense-in-depth: verify the receipt against the trusted key in the DB
        # to catch key-swap attacks before returning the receipt to the caller.
        try:
            _verify_against_envelope(receipt, conn)
        except ReceiptVerificationError:
            _log.error("Receipt '%s' failed key verification; rolling back draw.", draw_id)
            rollback_sync(conn, draw_id)
            raise
        return receipt
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def confirm_sync(conn: sqlite3.Connection, draw_id: str, amount_settled_minor_units: int) -> None:
    """Transition a reserved (or reaper-rolled-back) draw to settled.

    Confirm is only ever called after the payment committed on the wire, so it
    also wins over a ``rolled_back`` state: the reaper may have expired the draw
    while a slow payment was still in flight, and letting the rollback stand
    would erase real spend from the ledger and free cap that was actually used.
    Explicit ``rollback()`` (pay-phase failure) and ``confirm()`` are mutually
    exclusive in the auth flow, so this cannot resurrect a cancelled draw.
    """
    now = datetime.now(UTC)
    prior = conn.execute("SELECT state FROM draws WHERE id=?", (draw_id,)).fetchone()
    if prior is None:
        raise BudgetError(f"confirm: draw '{draw_id}' not found.")
    cursor = conn.execute(
        "UPDATE draws SET state='settled', amount_settled_minor_units=?, "
        "settled_at=? WHERE id=? AND state IN ('reserved', 'rolled_back')",
        (amount_settled_minor_units, now.isoformat(), draw_id),
    )
    if cursor.rowcount > 0 and prior[0] == "rolled_back":
        _log.warning(
            "Draw '%s' was rolled back (reaper expired it mid-payment) but the "
            "payment committed; settling it late to keep the ledger truthful.",
            draw_id,
        )
    # rowcount == 0 means the draw is already settled — an idempotent re-confirm.


def rollback_sync(conn: sqlite3.Connection, draw_id: str) -> None:
    """Transition a reserved draw to rolled_back, freeing its reserved capacity."""
    cursor = conn.execute(
        "UPDATE draws SET state='rolled_back' WHERE id=? AND state='reserved'",
        (draw_id,),
    )
    if cursor.rowcount == 0:
        row = conn.execute("SELECT state FROM draws WHERE id=?", (draw_id,)).fetchone()
        if row is None:
            raise BudgetError(f"rollback: draw '{draw_id}' not found.")
        # Draw already in a terminal state — either an idempotent re-rollback or the
        # losing side of a concurrent rollback+confirm race. Both are safe.
