"""SQLite-backed trace sink — the always-on local durability layer."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from routewiler._storage import ensure_schema as _ensure_schema
from routewiler._storage import open_connection as _open_connection

if TYPE_CHECKING:
    from routewiler.normalized import UrlEncoding
    from routewiler.trace.schema import TraceEvent


class SqliteTraceSink:
    """Append-only SQLite writer for TraceEvent records.

    One sqlite3 connection per sink (WAL mode, check_same_thread=False).
    All blocking I/O is offloaded to a thread via asyncio.to_thread so the
    event loop is never blocked.
    """

    def __init__(self, db_path: Path, url_mode: UrlEncoding) -> None:
        self._db_path = db_path
        self._url_mode = url_mode
        self._conn = _open_connection(db_path)
        _ensure_schema(self._conn)
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def url_mode(self) -> UrlEncoding:
        return self._url_mode

    async def start(self) -> None:
        """No-op lifecycle hook — reserved for future background tasks."""

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
                    request_id, envelope_id, selected_rail, fallback_from, facilitator,
                    http_status, service_delivered,
                    amount_native, amount_native_currency,
                    amount_envelope, amount_envelope_currency, fmv_quality,
                    ts_start, ts_end, shipped_at, payload
                ) VALUES (
                    :request_id, :envelope_id, :selected_rail, :fallback_from, :facilitator,
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
        "fallback_from": event.fallback_from,
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
                      default). ``"drop"`` strips query strings (recommended
                      when URLs carry PII or secrets in query params).
        """
        return SqliteTraceSink(Path(path), url_mode)
