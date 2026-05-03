"""Local SQLite budget counter — single-process, row-locked draws."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from routewiler.errors import (
    BudgetExceededError,
    EnvelopeExpiredError,
    EnvelopeFrozenError,
    EnvelopeNotFoundError,
    PaymentError,
)

DEFAULT_ENVELOPE_ID = "default"
_DEFAULT_CAP_USD = 100
_DEFAULT_TTL_DAYS = 30

# ---------------------------------------------------------------------------
# Budget DDL — owned here so schema changes are colocated with the logic.
# SqliteTraceSink owns only trace_events; BudgetStore and ensure_default_envelope
# both call _ensure_budget_schema() to initialize these tables.
# ---------------------------------------------------------------------------

_BUDGET_DDL = """
CREATE TABLE IF NOT EXISTS envelopes (
    id                   TEXT    PRIMARY KEY,
    cap_minor_units      INTEGER NOT NULL,
    cap_currency         TEXT    NOT NULL,
    allowed_rails        TEXT    NOT NULL,   -- JSON array
    allowed_origins_glob TEXT    NOT NULL,   -- JSON array
    status               TEXT    NOT NULL,
    created_at           TEXT    NOT NULL,   -- ISO-8601 UTC
    expires_at           TEXT    NOT NULL,   -- ISO-8601 UTC
    counter_public_key   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS draws (
    id                          TEXT    PRIMARY KEY,
    envelope_id                 TEXT    NOT NULL REFERENCES envelopes(id),
    request_id                  TEXT    NOT NULL,
    idempotency_key             TEXT    NOT NULL,
    amount_reserved_minor_units INTEGER NOT NULL,
    amount_settled_minor_units  INTEGER,
    rail_quoted                 TEXT    NOT NULL,
    state                       TEXT    NOT NULL,   -- 'reserved' | 'settled' | 'rolled_back'
    issued_at                   TEXT    NOT NULL,   -- ISO-8601 UTC
    expires_at                  TEXT    NOT NULL,   -- ISO-8601 UTC
    settled_at                  TEXT,               -- ISO-8601 UTC; set on confirm
    UNIQUE (envelope_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS draws_envelope_state ON draws (envelope_id, state);
"""


def _ensure_budget_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the budget tables. Safe to call multiple times."""
    conn.executescript(_BUDGET_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# _EnvelopeRow — typed dict for SQLite envelope INSERT parameters.
# Values are the serialized (SQL-ready) forms: datetimes as ISO-8601 strings,
# Rail lists as JSON strings. counter_public_key is omitted and filled by the
# column DEFAULT until Phase 1 W1 (Ed25519 key generation).
# ---------------------------------------------------------------------------


class _EnvelopeRow(TypedDict):
    id: str
    cap_minor_units: int
    cap_currency: str
    allowed_rails: str  # json.dumps(list[Rail])
    allowed_origins_glob: str  # json.dumps(list[str])
    status: str
    created_at: str  # ISO-8601 UTC
    expires_at: str  # ISO-8601 UTC


# ---------------------------------------------------------------------------
# FMV: stablecoin-peg conversion (same asset set as trace/emitter.py)
# Week 4 supports only this path; CoinGecko + ECB integration ships in Week 8.
# ---------------------------------------------------------------------------

_STABLECOIN_PEG: dict[str, str] = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "usd",  # USDC base mainnet
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e": "usd",  # USDC base-sepolia
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "usd",  # USDC polygon
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": "usd",  # USDC arbitrum
    "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42": "eur",  # EURC base mainnet
}
_STABLECOIN_DECIMALS = 6
# Minor units per major unit for each envelope currency.
_MINOR_PER_MAJOR: dict[str, int] = {"usd": 100, "eur": 100, "gbp": 100, "jpy": 1}


def amount_to_envelope_minor_units(
    rail_currency: str, amount_native: int, envelope_currency: str
) -> int:
    """Convert rail-native base units to envelope minor units (ceiling rounding).

    Only the stablecoin-peg case is supported at Week 4 (USDC→USD, EURC→EUR).
    Ceiling rounding ensures the cap is never silently breached by sub-cent fractions.
    Raises PaymentError for assets requiring CoinGecko / ECB conversion (Week 8+).
    """
    address: str | None = None
    if "/erc20:" in rail_currency:
        address = rail_currency.rsplit("/erc20:", maxsplit=1)[-1].lower()

    if address and address in _STABLECOIN_PEG:
        peg = _STABLECOIN_PEG[address]
        if peg == envelope_currency.lower():
            minor_per_major: int = _MINOR_PER_MAJOR.get(peg, 100)
            divisor: int = 10**_STABLECOIN_DECIMALS
            # Exact ceiling: (a * b + d - 1) // d
            return (amount_native * minor_per_major + divisor - 1) // divisor

    raise PaymentError(
        f"Budget enforcement requires FMV conversion for asset '{rail_currency}' "
        f"in envelope currency '{envelope_currency}'. "
        "Only USDC/EURC stablecoin peg is supported at Week 4; "
        "CoinGecko + ECB integration ships in Week 8."
    )


# ---------------------------------------------------------------------------
# Draw — lightweight in-memory record returned by BudgetStore.draw()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Draw:
    """In-memory record of a reserved draw. Replaces a full DrawReceipt at Week 4.

    DrawReceipt (with Ed25519 signature) ships in Week 9.
    """

    id: str
    envelope_id: str
    idempotency_key: str
    amount_reserved_minor_units: int
    rail_quoted: str
    issued_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------------------
# BudgetStore
# ---------------------------------------------------------------------------

_DEFAULT_DRAW_TTL_SECONDS = 120


class BudgetStore:
    """Single-process SQLite budget counter.

    Uses BEGIN IMMEDIATE transactions for atomic cap-check + insert.
    Budget enforcement is always local (§8 — no hosted counter at MVP).
    Initialises the envelopes and draws tables on construction (idempotent).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_budget_schema(self._conn)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Synchronous helpers (safe to call from a synchronous constructor)
    # ------------------------------------------------------------------

    def envelope_exists_sync(self, envelope_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM envelopes WHERE id = ?", (envelope_id,)).fetchone()
        return row is not None

    def get_currency_sync(self, envelope_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT cap_currency FROM envelopes WHERE id = ?", (envelope_id,)
        ).fetchone()
        return str(row[0]) if row else None

    # ------------------------------------------------------------------
    # Async public API
    # ------------------------------------------------------------------

    async def create_envelope(
        self,
        envelope_id: str,
        *,
        cap_minor_units: int,
        cap_currency: str,
        allowed_rails: list[str],
        allowed_origins_glob: list[str] | None = None,
        ttl_seconds: int,
        owner_agent_id: str | None = None,
    ) -> None:
        """Insert a new envelope row. Raises sqlite3.IntegrityError on duplicate id."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        row: _EnvelopeRow = {
            "id": envelope_id,
            "cap_minor_units": cap_minor_units,
            "cap_currency": cap_currency,
            "allowed_rails": json.dumps(allowed_rails),
            "allowed_origins_glob": json.dumps(allowed_origins_glob or ["*"]),
            "status": "active",
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        async with self._lock:
            await asyncio.to_thread(self._create_envelope_sync, row)

    def _create_envelope_sync(self, row: _EnvelopeRow) -> None:
        self._conn.execute(
            """
            INSERT INTO envelopes (
                id, cap_minor_units, cap_currency,
                allowed_rails, allowed_origins_glob,
                status, created_at, expires_at
            ) VALUES (
                :id, :cap_minor_units, :cap_currency,
                :allowed_rails, :allowed_origins_glob,
                :status, :created_at, :expires_at
            )
            """,
            row,
        )

    async def draw(
        self,
        *,
        envelope_id: str,
        request_id: str,
        idempotency_key: str,
        amount_reserved_minor_units: int,
        rail_quoted: str,
        ttl_seconds: int = _DEFAULT_DRAW_TTL_SECONDS,
    ) -> Draw:
        """Atomically reserve capacity from the envelope.

        Implements §8.2:  BEGIN IMMEDIATE → cap check → idempotency → INSERT → COMMIT.
        Raises BudgetExceededError, EnvelopeNotFoundError, EnvelopeFrozenError,
        or EnvelopeExpiredError on rejection.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._draw_sync,
                envelope_id=envelope_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                amount_reserved_minor_units=amount_reserved_minor_units,
                rail_quoted=rail_quoted,
                ttl_seconds=ttl_seconds,
            )

    def _draw_sync(
        self,
        *,
        envelope_id: str,
        request_id: str,
        idempotency_key: str,
        amount_reserved_minor_units: int,
        rail_quoted: str,
        ttl_seconds: int,
    ) -> Draw:
        conn = self._conn
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Load envelope
            env_row = conn.execute(
                "SELECT cap_minor_units, status, expires_at FROM envelopes WHERE id = ?",
                (envelope_id,),
            ).fetchone()
            if env_row is None:
                raise EnvelopeNotFoundError(f"Envelope '{envelope_id}' not found.")
            cap, status, env_expires_raw = env_row

            if status != "active":
                raise EnvelopeFrozenError(
                    f"Envelope '{envelope_id}' has status '{status}' (expected 'active')."
                )

            env_expires = datetime.fromisoformat(env_expires_raw)
            if now >= env_expires:
                raise EnvelopeExpiredError(
                    f"Envelope '{envelope_id}' expired at {env_expires_raw}."
                )

            # Idempotency short-circuit
            existing = conn.execute(
                "SELECT id, amount_reserved_minor_units, rail_quoted, issued_at, expires_at "
                "FROM draws WHERE envelope_id = ? AND idempotency_key = ?",
                (envelope_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                ex_id, ex_amt, ex_rail, ex_issued, ex_exp = existing
                return Draw(
                    id=str(ex_id),
                    envelope_id=envelope_id,
                    idempotency_key=idempotency_key,
                    amount_reserved_minor_units=int(ex_amt),
                    rail_quoted=str(ex_rail),
                    issued_at=datetime.fromisoformat(str(ex_issued)),
                    expires_at=datetime.fromisoformat(str(ex_exp)),
                )

            # Cap check: reserved + settled must not exceed cap after this draw.
            # Both 'reserved' and 'settled' draws count against the cap (§8.3).
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

            draw_id = uuid4().hex
            conn.execute(
                """
                INSERT INTO draws (
                    id, envelope_id, request_id, idempotency_key,
                    amount_reserved_minor_units, rail_quoted, state,
                    issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    draw_id,
                    envelope_id,
                    request_id,
                    idempotency_key,
                    amount_reserved_minor_units,
                    rail_quoted,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            conn.execute("COMMIT")
            return Draw(
                id=draw_id,
                envelope_id=envelope_id,
                idempotency_key=idempotency_key,
                amount_reserved_minor_units=amount_reserved_minor_units,
                rail_quoted=rail_quoted,
                issued_at=now,
                expires_at=expires,
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    async def confirm(self, draw_id: str, amount_settled_minor_units: int) -> None:
        """Transition a reserved draw to settled with the actual settled amount."""
        async with self._lock:
            await asyncio.to_thread(self._confirm_sync, draw_id, amount_settled_minor_units)

    def _confirm_sync(self, draw_id: str, amount_settled_minor_units: int) -> None:
        now = datetime.now(UTC)
        self._conn.execute(
            "UPDATE draws SET state='settled', amount_settled_minor_units=?, "
            "settled_at=? WHERE id=? AND state='reserved'",
            (amount_settled_minor_units, now.isoformat(), draw_id),
        )

    async def rollback(self, draw_id: str) -> None:
        """Transition a reserved draw to rolled_back, freeing its reserved capacity."""
        async with self._lock:
            await asyncio.to_thread(self._rollback_sync, draw_id)

    def _rollback_sync(self, draw_id: str) -> None:
        self._conn.execute(
            "UPDATE draws SET state='rolled_back' WHERE id=? AND state='reserved'",
            (draw_id,),
        )

    async def aclose(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._conn.close)


# ---------------------------------------------------------------------------
# ensure_default_envelope — seeds the 'default' row on first client construction
# ---------------------------------------------------------------------------


def ensure_default_envelope(db_path: Path) -> tuple[str, str]:
    """Idempotently insert the 'default' envelope row; return (id, cap_currency).

    Cap:            ROUTEWILER_DEFAULT_CAP_USD env var (default 100 USD → 10000 cents).
    Currency:       "usd".
    Allowed rails:  all four rails.
    Allowed origins: ["*"].
    TTL:            30 days from now (on first creation; existing rows unchanged).

    Uses INSERT OR IGNORE so the function is safe to call on every client
    construction without hitting the DB unnecessarily.
    """
    cap_usd = int(os.environ.get("ROUTEWILER_DEFAULT_CAP_USD", _DEFAULT_CAP_USD))
    cap_minor = cap_usd * 100  # USD cents

    now = datetime.now(UTC)
    expires_at = now + timedelta(days=_DEFAULT_TTL_DAYS)

    row: _EnvelopeRow = {
        "id": DEFAULT_ENVELOPE_ID,
        "cap_minor_units": cap_minor,
        "cap_currency": "usd",
        "allowed_rails": json.dumps(["x402", "l402", "mpp-tempo", "mpp-spt"]),
        "allowed_origins_glob": json.dumps(["*"]),
        "status": "active",
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    try:
        _ensure_budget_schema(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO envelopes (
                id, cap_minor_units, cap_currency,
                allowed_rails, allowed_origins_glob,
                status, created_at, expires_at
            ) VALUES (
                :id, :cap_minor_units, :cap_currency,
                :allowed_rails, :allowed_origins_glob,
                :status, :created_at, :expires_at
            )
            """,
            row,
        )
        conn.commit()
        result = conn.execute(
            "SELECT id, cap_currency FROM envelopes WHERE id = ?", (DEFAULT_ENVELOPE_ID,)
        ).fetchone()
    finally:
        conn.close()

    return str(result[0]), str(result[1])
