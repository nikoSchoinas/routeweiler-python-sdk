"""SQLite-backed trace sink — the always-on local durability layer."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from routewiler.normalized import UrlEncoding
    from routewiler.trace.schema import TraceEvent

_DDL = """
CREATE TABLE IF NOT EXISTS trace_events (
    request_id              TEXT    PRIMARY KEY,
    envelope_id             TEXT    NOT NULL,
    selected_rail           TEXT,               -- NULL for passthrough / pre-rail-selection errors
    facilitator             TEXT,
    http_status             INTEGER NOT NULL,
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


class SqliteTraceSink:
    """Append-only SQLite writer for TraceEvent records.

    One sqlite3 connection per sink (WAL mode, check_same_thread=False).
    All blocking I/O is offloaded to a thread via asyncio.to_thread so the
    event loop is never blocked.
    """

    def __init__(self, db_path: Path, url_mode: UrlEncoding) -> None:
        self._db_path = db_path
        self._url_mode = url_mode
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)
        self._conn.commit()
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def url_mode(self) -> UrlEncoding:
        return self._url_mode

    async def emit(self, event: TraceEvent) -> None:
        """Persist one TraceEvent row. Silently ignores duplicate request_ids."""
        # Build the row outside the thread to keep imports on the main thread.
        row = _build_row(event)
        payload = event.model_dump_json(by_alias=True)

        async with self._lock:
            await asyncio.to_thread(self._insert, row, payload)

    def _insert(self, row: dict[str, object], payload: str) -> None:
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO trace_events (
                    request_id, envelope_id, selected_rail, facilitator,
                    http_status, service_delivered,
                    amount_native, amount_native_currency,
                    amount_envelope, amount_envelope_currency, fmv_quality,
                    ts_start, ts_end, shipped_at, payload
                ) VALUES (
                    :request_id, :envelope_id, :selected_rail, :facilitator,
                    :http_status, :service_delivered,
                    :amount_native, :amount_native_currency,
                    :amount_envelope, :amount_envelope_currency, :fmv_quality,
                    :ts_start, :ts_end, NULL, :payload
                )
                """,
                {**row, "payload": payload},
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await asyncio.to_thread(self._conn.close)


def _build_row(event: TraceEvent) -> dict[str, object]:
    p = event.payment
    return {
        "request_id": event.request_id,
        "envelope_id": event.envelope_id,
        "selected_rail": event.selected_rail,
        "facilitator": event.facilitator,
        "http_status": event.outcome.http_status,
        "service_delivered": int(event.outcome.service_delivered),
        "amount_native": str(p.amount_native) if p else None,
        "amount_native_currency": p.amount_native_currency if p else None,
        "amount_envelope": p.amount_envelope if p else None,
        "amount_envelope_currency": p.amount_envelope_currency if p else None,
        "fmv_quality": p.fmv_quality if p else None,
        "ts_start": event.timestamp_start.isoformat(),
        "ts_end": event.timestamp_end.isoformat(),
    }


class TraceSink:
    """Factory for trace sink backends."""

    @classmethod
    def sqlite(
        cls,
        path: str | Path = "./routewiler-traces.db",
        *,
        url_mode: UrlEncoding = "raw",
    ) -> SqliteTraceSink:
        """Create a local SQLite-backed sink.

        Args:
            path:     File path for the SQLite database (created if absent).
            url_mode: Controls URL storage. ``"raw"`` stores full URLs (local
                      default). ``"drop"`` strips query strings. ``"hash"``
                      (hosted-mode default) is not yet implemented — it ships
                      with the hosted uploader in Week 18.
        """
        if url_mode == "hash":
            raise NotImplementedError(
                "url_mode='hash' requires the hosted uploader (Week 18). "
                "Use 'raw' or 'drop' for local sinks."
            )
        return SqliteTraceSink(Path(path), url_mode)
