"""Local SQLite budget state — Week 3 bootstrap; draw algorithm ships Week 4."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_ENVELOPE_ID = "default"
_DEFAULT_CAP_USD = 100
_DEFAULT_TTL_DAYS = 30


def ensure_default_envelope(db_path: Path) -> tuple[str, str]:
    """Idempotently insert the 'default' envelope row; return (id, cap_currency).

    Cap:            ROUTEWILER_DEFAULT_CAP_USD env var (default 100 USD).
    Currency:       "usd".
    Allowed rails:  all four rails.
    Allowed origins: ["*"].
    TTL:            30 days from now (on first creation; existing rows unchanged).

    Uses INSERT OR IGNORE so the function is safe to call on every client
    construction without hitting the DB unnecessarily. The draws table and
    enforcement logic ship in Week 4; this call just guarantees the FK target
    row exists in the trace sink's envelopes table.

    Returns the (envelope_id, cap_currency) of the row that exists after the call —
    which may have been inserted just now or have been there already.
    """
    cap_usd = int(os.environ.get("ROUTEWILER_DEFAULT_CAP_USD", _DEFAULT_CAP_USD))
    cap_minor = cap_usd * 100  # USD cents

    now = datetime.now(UTC)
    expires_at = now + timedelta(days=_DEFAULT_TTL_DAYS)

    row = {
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

    return result[0], result[1]
