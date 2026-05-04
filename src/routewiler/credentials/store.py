"""Credential store — SQLite-backed persistence for rail credentials.

Mirrors the shape and conventions of budgets/local.py:
- One persistent sqlite3.Connection per store (WAL, check_same_thread=False).
- All blocking I/O offloaded via asyncio.to_thread.
- One asyncio.Lock guards writes.
- DDL is idempotent (CREATE TABLE IF NOT EXISTS).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from routewiler.credentials.schema import CredentialRecord, CredentialState, ManualHoldReason
from routewiler.errors import CredentialNotFoundError, InvalidCredentialTransitionError
from routewiler.normalized import Rail

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS credentials (
    credential_id        TEXT    PRIMARY KEY,
    request_id           TEXT    NOT NULL,
    rail                 TEXT    NOT NULL,
    challenge_url        TEXT    NOT NULL,
    payload_json         TEXT    NOT NULL,   -- JSON; opaque per-rail
    state                TEXT    NOT NULL,   -- CredentialState value
    manual_hold_reason   TEXT,               -- ManualHoldReason value or NULL
    persisted_at         TEXT    NOT NULL,   -- ISO-8601 UTC
    redeemed_at          TEXT,               -- ISO-8601 UTC; set on REDEEMED
    last_transition_at   TEXT    NOT NULL,   -- ISO-8601 UTC
    expires_at           TEXT                -- ISO-8601 UTC; from challenge.expires_at
);

CREATE INDEX IF NOT EXISTS credentials_state ON credentials (state);
CREATE INDEX IF NOT EXISTS credentials_request ON credentials (request_id);
CREATE UNIQUE INDEX IF NOT EXISTS credentials_request_rail
    ON credentials (request_id, rail);
"""

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[CredentialState, set[CredentialState]] = {
    CredentialState.PERSISTED: {
        CredentialState.RECOVERING,
        CredentialState.REDEEMED,
        CredentialState.MANUAL_HOLD,
    },
    CredentialState.RECOVERING: {
        CredentialState.REDEEMED,
        CredentialState.MANUAL_HOLD,
    },
    CredentialState.REDEEMED: set(),
    CredentialState.MANUAL_HOLD: set(),
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ensure_credentials_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the credentials table. Safe to call multiple times."""
    conn.executescript(_CREDENTIALS_DDL)
    conn.commit()


def _row_to_record(row: sqlite3.Row) -> CredentialRecord:
    return CredentialRecord(
        credential_id=str(row["credential_id"]),
        request_id=str(row["request_id"]),
        rail=str(row["rail"]),
        challenge_url=str(row["challenge_url"]),
        payload=json.loads(str(row["payload_json"])),
        state=CredentialState(str(row["state"])),
        manual_hold_reason=(
            ManualHoldReason(str(row["manual_hold_reason"]))
            if row["manual_hold_reason"] is not None
            else None
        ),
        persisted_at=datetime.fromisoformat(str(row["persisted_at"])),
        redeemed_at=(
            datetime.fromisoformat(str(row["redeemed_at"]))
            if row["redeemed_at"] is not None
            else None
        ),
        last_transition_at=datetime.fromisoformat(str(row["last_transition_at"])),
        expires_at=(
            datetime.fromisoformat(str(row["expires_at"]))
            if row["expires_at"] is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# CredentialStore
# ---------------------------------------------------------------------------


class CredentialStore:
    """Single-process SQLite credential store.

    Persists payment credentials (macaroon+preimage for L402, etc.) before
    the retry is attempted, enabling post-failure recovery.

    State machine (§9.1):
        PERSISTED → RECOVERING → REDEEMED | MANUAL_HOLD(exhausted|expired)

    Shares the same DB file as BudgetStore and SqliteTraceSink (multi-table,
    single WAL file — the established project pattern).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_credentials_schema(self._conn)
        self._lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def persist(
        self,
        *,
        request_id: str,
        rail: Rail,
        challenge_url: str,
        payload: dict[str, Any],
        expires_at: datetime | None = None,
    ) -> CredentialRecord:
        """Insert a new credential in PERSISTED state.

        Idempotent on (request_id, rail): replaying after a crash returns the
        existing row instead of duplicating.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._persist_sync,
                request_id=request_id,
                rail=rail,
                challenge_url=challenge_url,
                payload=payload,
                expires_at=expires_at,
            )

    async def get(self, credential_id: str) -> CredentialRecord | None:
        """Fetch a credential by its id; returns None if not found."""
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, credential_id)

    async def list_by_state(self, state: CredentialState) -> list[CredentialRecord]:
        """Return all credentials in a given state."""
        async with self._lock:
            return await asyncio.to_thread(self._list_by_state_sync, state)

    async def transition(
        self,
        credential_id: str,
        *,
        to_state: CredentialState,
        manual_hold_reason: ManualHoldReason | None = None,
    ) -> CredentialRecord:
        """Atomically transition a credential to a new state.

        Validates the transition against the §9.1 state machine graph.
        Raises InvalidCredentialTransitionError for illegal edges.
        Raises CredentialNotFoundError if the credential does not exist.
        Transitioning to MANUAL_HOLD requires manual_hold_reason.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._transition_sync,
                credential_id=credential_id,
                to_state=to_state,
                manual_hold_reason=manual_hold_reason,
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            await asyncio.to_thread(self._conn.close)

    # ------------------------------------------------------------------
    # Synchronous helpers (run inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _persist_sync(
        self,
        *,
        request_id: str,
        rail: Rail,
        challenge_url: str,
        payload: dict[str, Any],
        expires_at: datetime | None,
    ) -> CredentialRecord:
        conn = self._conn
        now = datetime.now(UTC)
        credential_id = uuid4().hex

        # INSERT OR IGNORE provides idempotency; the UNIQUE index on
        # (request_id, rail) prevents duplicates across retries.
        conn.execute(
            """
            INSERT OR IGNORE INTO credentials (
                credential_id, request_id, rail, challenge_url,
                payload_json, state, manual_hold_reason,
                persisted_at, redeemed_at, last_transition_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, 'persisted', NULL, ?, NULL, ?, ?)
            """,
            (
                credential_id,
                request_id,
                rail,
                challenge_url,
                json.dumps(payload),
                now.isoformat(),
                now.isoformat(),
                expires_at.isoformat() if expires_at is not None else None,
            ),
        )

        row = conn.execute(
            "SELECT * FROM credentials WHERE request_id = ? AND rail = ?",
            (request_id, rail),
        ).fetchone()
        return _row_to_record(row)

    def _get_sync(self, credential_id: str) -> CredentialRecord | None:
        row = self._conn.execute(
            "SELECT * FROM credentials WHERE credential_id = ?", (credential_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def _list_by_state_sync(self, state: CredentialState) -> list[CredentialRecord]:
        rows = self._conn.execute(
            "SELECT * FROM credentials WHERE state = ?", (state.value,)
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def _transition_sync(
        self,
        *,
        credential_id: str,
        to_state: CredentialState,
        manual_hold_reason: ManualHoldReason | None,
    ) -> CredentialRecord:
        conn = self._conn

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM credentials WHERE credential_id = ?", (credential_id,)
            ).fetchone()
            if row is None:
                raise CredentialNotFoundError(f"Credential '{credential_id}' not found.")

            current_state = CredentialState(str(row["state"]))
            allowed = _VALID_TRANSITIONS[current_state]
            if to_state not in allowed:
                raise InvalidCredentialTransitionError(
                    f"Cannot transition credential '{credential_id}' "
                    f"from {current_state.value!r} to {to_state.value!r}."
                )

            if to_state == CredentialState.MANUAL_HOLD and manual_hold_reason is None:
                raise InvalidCredentialTransitionError(
                    "Transitioning to MANUAL_HOLD requires a manual_hold_reason."
                )

            now = datetime.now(UTC)
            redeemed_at_val = now.isoformat() if to_state == CredentialState.REDEEMED else None

            conn.execute(
                """
                UPDATE credentials
                SET state              = ?,
                    manual_hold_reason = ?,
                    last_transition_at = ?,
                    redeemed_at        = COALESCE(redeemed_at, ?)
                WHERE credential_id = ?
                """,
                (
                    to_state.value,
                    manual_hold_reason.value if manual_hold_reason is not None else None,
                    now.isoformat(),
                    redeemed_at_val,
                    credential_id,
                ),
            )
            conn.execute("COMMIT")

            updated = conn.execute(
                "SELECT * FROM credentials WHERE credential_id = ?", (credential_id,)
            ).fetchone()
            return _row_to_record(updated)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
