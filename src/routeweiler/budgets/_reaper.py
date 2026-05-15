"""Reaper sync helper — rolls back stale reserved draws."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def reap_sync(conn: sqlite3.Connection) -> int:
    """Transition all expired reserved draws to rolled_back. Returns rowcount."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        "UPDATE draws SET state='rolled_back' WHERE state='reserved' AND expires_at < ?",
        (now,),
    )
    return cursor.rowcount
