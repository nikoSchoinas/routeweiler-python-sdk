"""Shared SQLite connection factory and schema initializer.

All three SQLite-backed stores (BudgetStore, CredentialStore, SqliteTraceSink)
open the same trace.db file.  This module is the single authority for:
  - How connections are opened (pragmas, WAL mode, foreign keys, busy_timeout).
  - Which tables exist (DDL) and how they are migrated over time.

Stores call ``open_connection(path)`` and ``ensure_schema(conn)`` in their
``__init__``, replacing per-module ``_ensure_*_schema`` helpers and the
inconsistent pragma sets they carried.

Migration history
-----------------
trace_events v1 (W7): added ``fallback_from`` column.

The ``schema_versions`` table exists in existing databases but is not currently
written to or read.  Version tracking will be reintroduced alongside the v2
migration system (§17).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# DDL — one string per component; all idempotent (CREATE TABLE IF NOT EXISTS).
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
    expires_at                  TEXT    NOT NULL,   -- ISO-8601 UTC (+30s clock-skew buffer, §8.4)
    settled_at                  TEXT,               -- ISO-8601 UTC; set on confirm
    UNIQUE (envelope_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS draws_envelope_state ON draws (envelope_id, state);

CREATE TABLE IF NOT EXISTS envelope_fmv_snapshots (
    envelope_id  TEXT    PRIMARY KEY REFERENCES envelopes(id),
    captured_at  TEXT    NOT NULL,   -- ISO-8601 UTC
    rates_json   TEXT    NOT NULL,   -- JSON object {"sats->usd": "0.00065", ...}
    quality_json TEXT    NOT NULL    -- JSON object {"sats->usd": "coingecko_simple", ...}
);
"""

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

_TRACE_DDL = """
CREATE TABLE IF NOT EXISTS trace_events (
    request_id              TEXT    PRIMARY KEY,
    envelope_id             TEXT    NOT NULL,
    selected_rail           TEXT,               -- NULL for passthrough / pre-rail-selection errors
    fallback_from           TEXT,               -- NULL when no failover occurred
    facilitator             TEXT,
    http_status             INTEGER,            -- NULL when no HTTP response (MANUAL_HOLD events)
    service_delivered       INTEGER NOT NULL,   -- 0 | 1
    amount_native           TEXT,               -- base-units as string (bigint-safe)
    amount_native_currency  TEXT,
    amount_envelope         REAL,
    amount_envelope_currency TEXT,
    fmv_quality             TEXT,
    ts_start                TEXT    NOT NULL,   -- ISO-8601 UTC
    ts_end                  TEXT    NOT NULL,   -- ISO-8601 UTC
    shipped_at              TEXT,               -- set by hosted uploader (Week 18)
    payload                 TEXT    NOT NULL    -- full TraceEvent JSON
);

CREATE INDEX IF NOT EXISTS trace_events_envelope_ts
    ON trace_events (envelope_id, ts_start DESC);
"""

# Migration SQL — add fallback_from column to trace_events rows created before W7.
_MIGRATION_TRACE_V1_ADD_FALLBACK_FROM = "ALTER TABLE trace_events ADD COLUMN fallback_from TEXT"

# Migration: rebuild trace_events to allow NULL in http_status (MANUAL_HOLD events have no
# HTTP response). SQLite doesn't support ALTER COLUMN, so we rename + recreate + copy + drop.
_MIGRATION_TRACE_V2_HTTP_STATUS_NULLABLE = """
    ALTER TABLE trace_events RENAME TO trace_events_v2_migrate;
    CREATE TABLE trace_events (
        request_id              TEXT    PRIMARY KEY,
        envelope_id             TEXT    NOT NULL,
        selected_rail           TEXT,
        fallback_from           TEXT,
        facilitator             TEXT,
        http_status             INTEGER,
        service_delivered       INTEGER NOT NULL,
        amount_native           TEXT,
        amount_native_currency  TEXT,
        amount_envelope         REAL,
        amount_envelope_currency TEXT,
        fmv_quality             TEXT,
        ts_start                TEXT    NOT NULL,
        ts_end                  TEXT    NOT NULL,
        shipped_at              TEXT,
        payload                 TEXT    NOT NULL
    );
    INSERT INTO trace_events SELECT
        request_id, envelope_id, selected_rail, fallback_from, facilitator,
        http_status, service_delivered,
        amount_native, amount_native_currency,
        amount_envelope, amount_envelope_currency, fmv_quality,
        ts_start, ts_end, shipped_at, payload
    FROM trace_events_v2_migrate;
    DROP TABLE trace_events_v2_migrate;
    CREATE INDEX IF NOT EXISTS trace_events_envelope_ts
        ON trace_events (envelope_id, ts_start DESC);
"""


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------


def open_connection(path: Path) -> sqlite3.Connection:
    """Open a sqlite3 connection with the project-standard settings.

    Sets ``isolation_level=None`` (autocommit; callers manage explicit
    transactions with BEGIN/COMMIT), ``timeout=10.0`` (Python-level busy wait),
    ``PRAGMA busy_timeout=10000`` (SQLite-level busy wait in ms), WAL mode,
    ``PRAGMA foreign_keys=ON`` (enforces FK constraints declared in DDL), and
    ``PRAGMA synchronous=NORMAL`` (safe under WAL, faster than FULL).
    """
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create all tables and apply pending migrations.

    Safe to call on every store construction — all DDL uses
    ``CREATE TABLE IF NOT EXISTS`` and migrations are version-gated.
    """
    conn.executescript(_BUDGET_DDL)
    conn.executescript(_CREDENTIALS_DDL)
    conn.executescript(_TRACE_DDL)
    _migrate_trace_schema(conn)


def _migrate_trace_schema(conn: sqlite3.Connection) -> None:
    """Apply pending trace_events schema migrations."""
    col_info = {
        row[1]: {"notnull": row[3]} for row in conn.execute("PRAGMA table_info(trace_events)")
    }
    if "fallback_from" not in col_info:
        conn.execute(_MIGRATION_TRACE_V1_ADD_FALLBACK_FROM)
    if col_info.get("http_status", {}).get("notnull", 0) == 1:
        conn.executescript(_MIGRATION_TRACE_V2_HTTP_STATUS_NULLABLE)
