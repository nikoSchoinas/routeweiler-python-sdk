"""Regression test: emit_passthrough must carry agent_id (bug B.1)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx

from routewiler.trace.emitter import TraceEmitter
from routewiler.trace.sink_sqlite import TraceSink


def _rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events").fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def test_emit_passthrough_preserves_agent_id(tmp_path: Path) -> None:
    """agent_id supplied to TraceEmitter appears on passthrough trace events."""
    sink = TraceSink.sqlite(tmp_path / "trace.db")
    emitter = TraceEmitter(
        sink=sink,
        envelope_id="env_001",
        envelope_currency="usd",
        funding_label="evm:base-sepolia:usdc",
        url_mode="raw",
        policy_hash="sha256:" + "0" * 64,
        agent_id="agent-xyz",
    )

    request = httpx.Request("GET", "http://example.com/free")
    response = httpx.Response(200)
    ts = datetime.now(UTC)

    await emitter.emit_passthrough(
        request=request,
        response=response,
        ts_start=ts,
        ts_end=ts,
    )
    await sink.aclose()

    rows = _rows(tmp_path / "trace.db")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["agentId"] == "agent-xyz"


async def test_emit_passthrough_agent_id_none_when_not_set(tmp_path: Path) -> None:
    """agent_id is None in the payload when no agent_id is given to the emitter."""
    sink = TraceSink.sqlite(tmp_path / "trace.db")
    emitter = TraceEmitter(
        sink=sink,
        envelope_id="env_002",
        envelope_currency="usd",
        funding_label=None,
        url_mode="raw",
        policy_hash="sha256:" + "0" * 64,
        # agent_id omitted
    )

    request = httpx.Request("GET", "http://example.com/free")
    response = httpx.Response(200)
    ts = datetime.now(UTC)

    await emitter.emit_passthrough(
        request=request,
        response=response,
        ts_start=ts,
        ts_end=ts,
    )
    await sink.aclose()

    rows = _rows(tmp_path / "trace.db")
    payload = json.loads(rows[0]["payload"])
    assert payload.get("agentId") is None
