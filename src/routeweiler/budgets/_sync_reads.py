"""Synchronous DB read helpers — safe to call from constructors before the event loop starts."""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from routeweiler.budgets.schema import EnvelopeCurrency
from routeweiler.normalized import Rail


def envelope_exists_sync(conn: sqlite3.Connection, envelope_id: str) -> bool:
    """Return True if an envelope row with *envelope_id* exists."""
    row = conn.execute("SELECT 1 FROM envelopes WHERE id = ?", (envelope_id,)).fetchone()
    return row is not None


def get_envelope_currency_sync(
    conn: sqlite3.Connection, envelope_id: str
) -> EnvelopeCurrency | None:
    """Return the cap_currency for an envelope, or None if not found."""
    row = conn.execute("SELECT cap_currency FROM envelopes WHERE id = ?", (envelope_id,)).fetchone()
    return cast(EnvelopeCurrency, str(row[0])) if row else None


def get_envelope_allowed_rails_sync(conn: sqlite3.Connection, envelope_id: str) -> list[Rail]:
    """Return the allowed_rails list for an envelope (empty list if not found)."""
    row = conn.execute(
        "SELECT allowed_rails FROM envelopes WHERE id = ?", (envelope_id,)
    ).fetchone()
    if row is None:
        return []
    return cast(list[Rail], json.loads(str(row[0])))
