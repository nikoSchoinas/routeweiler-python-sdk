"""Unit tests for SqliteTraceSink."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from routewiler.normalized import NormalizedChallenge, Payee, Price, Resource, X402RailRaw
from routewiler.trace.emitter import TraceEmitter
from routewiler.trace.schema import Outcome, Reconciliation, TraceEvent
from routewiler.trace.sink_sqlite import SqliteTraceSink, TraceSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(request_id: str = "req_001") -> TraceEvent:
    raw = X402RailRaw(
        kind="x402",
        accepts=[],
    )
    challenge = NormalizedChallenge(
        rail="x402",
        resource=Resource(method="GET", url="http://example.com/data", url_encoding="raw"),
        price=Price(
            amount=1000,
            currency="eip155:84532/erc20:0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            human_amount="0.001 USDC",
        ),
        payee=Payee(identifier="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"),
        scheme="exact",
        nonce="0xabc",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        raw=raw,
    )
    return TraceEvent(
        request_id=request_id,
        envelope_id="default",
        policy_hash="none",
        challenge=challenge,
        selected_rail="x402",
        funding_source="evm:base-sepolia:usdc",
        payment=None,
        outcome=Outcome(http_status=200, service_delivered=True, service_latency_ms=42),
        reconciliation=Reconciliation(vat_applicable=False),
        timestamp_start=datetime.now(UTC),
        timestamp_end=datetime.now(UTC),
    )


def _rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_schema_migration_creates_tables(tmp_trace_db_path: Path) -> None:
    """TraceSink.sqlite creates trace_events and envelopes tables on first open."""
    sink = TraceSink.sqlite(tmp_trace_db_path)
    await sink.aclose()

    conn = sqlite3.connect(str(tmp_trace_db_path))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    assert "trace_events" in tables
    assert "envelopes" in tables


async def test_emit_writes_row(tmp_trace_db_path: Path) -> None:
    """emit() persists a TraceEvent row with correct indexed columns."""
    sink = TraceSink.sqlite(tmp_trace_db_path)
    event = _make_event("req_write_001")
    await sink.emit(event)
    await sink.aclose()

    rows = _rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == "req_write_001"
    assert row["envelope_id"] == "default"
    assert row["selected_rail"] == "x402"
    assert row["http_status"] == 200
    assert row["service_delivered"] == 1
    assert row["shipped_at"] is None


async def test_emit_payload_round_trips(tmp_trace_db_path: Path) -> None:
    """The full payload JSON stored in the row survives a round-trip."""
    sink = TraceSink.sqlite(tmp_trace_db_path)
    event = _make_event("req_roundtrip")
    await sink.emit(event)
    await sink.aclose()

    rows = _rows(tmp_trace_db_path)
    payload = json.loads(rows[0]["payload"])
    assert payload["requestId"] == "req_roundtrip"
    assert payload["selectedRail"] == "x402"
    assert payload["outcome"]["httpStatus"] == 200


async def test_emit_idempotent_on_duplicate_request_id(tmp_trace_db_path: Path) -> None:
    """A second emit with the same request_id is silently ignored (INSERT OR IGNORE)."""
    sink = TraceSink.sqlite(tmp_trace_db_path)
    event = _make_event("req_dup")
    await sink.emit(event)
    await sink.emit(event)
    await sink.aclose()

    rows = _rows(tmp_trace_db_path)
    assert len(rows) == 1


async def test_passthrough_event_no_challenge(tmp_trace_db_path: Path) -> None:
    """Passthrough TraceEvent (challenge=None, selected_rail='none') is stored correctly."""
    sink = TraceSink.sqlite(tmp_trace_db_path)
    event = TraceEvent(
        request_id="req_passthrough",
        envelope_id="default",
        policy_hash="none",
        challenge=None,
        selected_rail="none",
        funding_source="evm:base:usdc",
        payment=None,
        outcome=Outcome(http_status=200, service_delivered=True, service_latency_ms=5),
        reconciliation=Reconciliation(vat_applicable=False),
        timestamp_start=datetime.now(UTC),
        timestamp_end=datetime.now(UTC),
    )
    await sink.emit(event)
    await sink.aclose()

    rows = _rows(tmp_trace_db_path)
    assert rows[0]["selected_rail"] == "none"
    payload = json.loads(rows[0]["payload"])
    assert payload["challenge"] is None


async def test_url_mode_drop_stored_on_sink(tmp_trace_sink: SqliteTraceSink) -> None:
    """url_mode='drop' is accepted and stored on the sink object."""
    sink = TraceSink.sqlite(tmp_trace_sink.db_path.parent / "drop.db", url_mode="drop")
    assert sink.url_mode == "drop"
    await sink.aclose()


def test_hashed_url_mode_raises() -> None:
    """url_mode='hash' raises NotImplementedError until Week 18."""
    with pytest.raises(NotImplementedError, match="Week 18"):
        TraceSink.sqlite("/tmp/irrelevant.db", url_mode="hash")


async def test_url_mode_drop_strips_query_in_emitted_event(tmp_path: Path) -> None:
    """url_mode='drop' strips the query string from resource.url in stored events."""
    sink = TraceSink.sqlite(tmp_path / "drop.db", url_mode="drop")
    raw = X402RailRaw(kind="x402", accepts=[])
    challenge = NormalizedChallenge(
        rail="x402",
        resource=Resource(
            method="GET",
            url="http://api.example.com/data?token=secret&page=1",
            url_encoding="raw",
        ),
        price=Price(
            amount=1000,
            currency="eip155:84532/erc20:0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            human_amount="0.001 USDC",
        ),
        payee=Payee(identifier="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"),
        scheme="exact",
        nonce="0xabc",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        raw=raw,
    )
    emitter = TraceEmitter(
        sink=sink,
        envelope_id="default",
        envelope_currency="usd",
        funding_label="evm:base-sepolia:usdc",
        url_mode="drop",
    )
    await emitter.emit_error(
        request=None,  # type: ignore[arg-type]
        response=None,
        error=ValueError("test"),
        challenge=challenge,
        ts_start=datetime.now(UTC),
        ts_end=datetime.now(UTC),
    )
    await sink.aclose()

    rows = _rows(tmp_path / "drop.db")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    stored_url = payload["challenge"]["resource"]["url"]
    assert "token" not in stored_url
    assert "page" not in stored_url
    assert stored_url == "http://api.example.com/data"
    assert payload["challenge"]["resource"]["urlEncoding"] == "drop"
