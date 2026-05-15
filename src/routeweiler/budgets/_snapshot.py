"""FMV-snapshot sync helpers — load and upsert the per-envelope rate snapshot."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal


def load_fmv_snapshot_sync(conn: sqlite3.Connection, envelope_id: str) -> dict[str, Decimal] | None:
    """Return the stored FMV snapshot rates for an envelope, or None if absent."""
    row = conn.execute(
        "SELECT rates_json FROM envelope_fmv_snapshots WHERE envelope_id = ?",
        (envelope_id,),
    ).fetchone()
    if row is None:
        return None
    raw: dict[str, str] = json.loads(str(row[0]))
    return {k: Decimal(v) for k, v in raw.items()}


def upsert_snapshot_sync(
    conn: sqlite3.Connection,
    envelope_id: str,
    captured_at: str,
    snapshot_rates: dict[str, Decimal],
    snapshot_quality: dict[str, str],
) -> None:
    """Insert or replace the FMV snapshot row for an envelope."""
    conn.execute(
        "INSERT OR REPLACE INTO envelope_fmv_snapshots "
        "(envelope_id, captured_at, rates_json, quality_json) VALUES (?, ?, ?, ?)",
        (
            envelope_id,
            captured_at,
            json.dumps({k: str(v) for k, v in snapshot_rates.items()}),
            json.dumps(snapshot_quality),
        ),
    )
