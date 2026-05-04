"""End-to-end credential lifecycle tests using the in-process L402 mock server.

Verifies:
1. Happy path: 402 → pay → 200 → credential in REDEEMED state.
2. Retry failure: 402 → pay → 5xx → credential in MANUAL_HOLD(exhausted) + trace event.
3. Persist-before-retry ordering: persist is called before the retry yields.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from routewiler import Routewiler
from routewiler.funding.lightning import LightningFundingSource
from routewiler.trace.sink_sqlite import TraceSink
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import (
    MOCK_MACAROON_B64,
    MOCK_PAYMENT_HASH,
    MOCK_PREIMAGE,
    MOCK_WWW_AUTHENTICATE,
    mock_l402_app,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _credential_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM credentials ORDER BY persisted_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _make_client(transport: httpx.ASGITransport, db_path: Path) -> Routewiler:
    source = LightningFundingSource(
        client=FakeLndClient(preimage=MOCK_PREIMAGE),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    client = Routewiler(funding=[source], trace_sink=sink)
    client._http = httpx.AsyncClient(
        auth=client._http.auth,
        event_hooks=client._http.event_hooks,
        transport=transport,
    )
    return client


# ---------------------------------------------------------------------------
# Happy path: 402 → pay → 200 → REDEEMED
# ---------------------------------------------------------------------------


async def test_happy_path_credential_state_is_redeemed(tmp_trace_db_path: Path) -> None:
    transport = httpx.ASGITransport(app=mock_l402_app)  # type: ignore[arg-type]
    client = _make_client(transport, tmp_trace_db_path)

    resp = await client.get("http://mock/protected")
    await client.aclose()

    assert resp.status_code == 200

    rows = _credential_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "redeemed"
    assert row["rail"] == "l402"
    assert row["redeemed_at"] is not None

    payload = json.loads(row["payload_json"])
    assert payload["macaroon"] == MOCK_MACAROON_B64
    assert payload["preimage_hex"] == MOCK_PREIMAGE.hex()
    assert payload["payment_hash_hex"] == MOCK_PAYMENT_HASH


async def test_happy_path_no_manual_hold_trace_event(tmp_trace_db_path: Path) -> None:
    transport = httpx.ASGITransport(app=mock_l402_app)  # type: ignore[arg-type]
    client = _make_client(transport, tmp_trace_db_path)
    await client.get("http://mock/protected")
    await client.aclose()

    trace_rows = _trace_rows(tmp_trace_db_path)
    for row in trace_rows:
        payload = json.loads(row["payload"])
        assert payload.get("credentialState") != "manual_hold"


# ---------------------------------------------------------------------------
# Retry failure: 402 → pay → 5xx → MANUAL_HOLD
# ---------------------------------------------------------------------------


def _make_always_500_after_402_app() -> Starlette:
    """First request → 402. Retry → 500."""
    call_count = {"n": 0}

    async def handler(request: Request) -> Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return Response(
                content=b"payment required",
                status_code=402,
                headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
            )
        return Response(content=b"server error", status_code=500)

    return Starlette(routes=[Route("/resource", handler)])


async def test_retry_failure_credential_state_is_manual_hold(tmp_trace_db_path: Path) -> None:
    app = _make_always_500_after_402_app()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    client = _make_client(transport, tmp_trace_db_path)

    resp = await client.get("http://mock/resource")
    await client.aclose()

    assert resp.status_code == 500

    rows = _credential_rows(tmp_trace_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "manual_hold"
    assert row["manual_hold_reason"] == "exhausted"


async def test_retry_failure_emits_manual_hold_trace_event(tmp_trace_db_path: Path) -> None:
    app = _make_always_500_after_402_app()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    client = _make_client(transport, tmp_trace_db_path)

    await client.get("http://mock/resource")
    await client.aclose()

    trace_rows = _trace_rows(tmp_trace_db_path)
    manual_hold_events = [
        r for r in trace_rows if json.loads(r["payload"]).get("credentialState") == "manual_hold"
    ]
    assert len(manual_hold_events) == 1


# ---------------------------------------------------------------------------
# Persist-before-retry ordering
# ---------------------------------------------------------------------------


async def test_credential_persisted_before_retry_is_sent(tmp_trace_db_path: Path) -> None:
    """Verify that the credential row exists in PERSISTED state at the moment the
    retry request is sent.  We track the moment the mock server receives the retry
    and check the DB at that instant via a side-effect closure.
    """
    db_state_at_retry: list[str] = []

    async def handler(request: Request) -> Response:
        if "Authorization" not in request.headers:
            return Response(
                content=b"payment required",
                status_code=402,
                headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
            )
        # This is the retry — check the credential table right now.
        cred_rows = _credential_rows(tmp_trace_db_path)
        db_state_at_retry.extend(r["state"] for r in cred_rows)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/r", handler)])
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    client = _make_client(transport, tmp_trace_db_path)

    resp = await client.get("http://mock/r")
    await client.aclose()

    assert resp.status_code == 200
    # The credential was in 'persisted' state when the retry was sent, which later
    # transitions to 'redeemed' after the 200 response.
    assert len(db_state_at_retry) == 1
    assert db_state_at_retry[0] == "persisted"

    final_rows = _credential_rows(tmp_trace_db_path)
    assert final_rows[0]["state"] == "redeemed"
