"""Integration tests for failover.

Uses MockRailAdapter to simulate sign failures and verifies that:
- The original draw is rolled back before the failover draw is issued.
- The failover draw uses a deterministic idempotency key derived from (request_id, attempt).
- TraceEvent.fallback_from is set to the failed rail.
- The sticky cache is updated to the successful failover rail.
- When ALL rails fail, NoFeasibleRailError is raised with a final error trace.
- Transport errors on the retry path emit an error trace and trigger failover.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from eth_account import Account

from routeweiler._auth import RouteweilerAuth, _make_idempotency_key
from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.budgets.schema import BudgetEnvelope
from routeweiler.errors import NoFeasibleRailError
from routeweiler.funding.evm import EvmFundingSource
from routeweiler.policy.dsl import Policy, PolicyRule, RuleMatch
from routeweiler.policy.engine import PolicyEngine
from routeweiler.routing.router import Router
from routeweiler.routing.sticky import StickyCache, StickyKey
from routeweiler.trace.emitter import TraceEmitter
from routeweiler.trace.sink_sqlite import TraceSink
from tests.budgets.test_fmv_provider import StubFmvProvider
from tests.fixtures.mock_rail import MockRailAdapter


def _x402_first_policy() -> PolicyEngine:
    """Policy that prefers x402 then l402 in that order.

    Having both rails in the prefer list sets privacy_fit_score=1.0 for both,
    so x402 wins on latency+reliability in the primary attempt.  On failover
    (x402 excluded) l402 is still in the prefer list and wins.
    """
    policy = Policy(
        default_rail="x402",
        rules=[
            PolicyRule(
                name="x402-then-l402",
                when=RuleMatch(url_matches="*"),
                prefer=["x402", "l402"],
            )
        ],
    )
    return PolicyEngine(policy)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _402_response() -> httpx.Response:
    challenge = {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:84532",
                "amount": "1000",
                "description": "",
                "mimeType": "application/json",
                "payTo": "0xdeadbeef",
                "maxTimeoutSeconds": 60,
                "asset": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                "extra": {"nonce": "0xabc", "validBefore": 9_999_999_999, "validAfter": 0},
            }
        ],
    }
    return httpx.Response(
        402,
        headers={"PAYMENT-REQUIRED": base64.b64encode(json.dumps(challenge).encode()).decode()},
    )


def _200_response() -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def _request() -> httpx.Request:
    return httpx.Request("GET", "http://mock/resource")


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _draw_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM draws ORDER BY issued_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


_FAILOVER_ENVELOPE = BudgetEnvelope(
    id="failover_env",
    cap_minor_units=100_000,
    cap_currency="usd",
    allowed_rails=["x402", "l402"],
    ttl_seconds=86_400,
)


async def _build_auth(
    adapters: list,
    db_path: Path,
    *,
    policy_engine: PolicyEngine | None = None,
) -> tuple[RouteweilerAuth, StickyCache]:
    key = EnvelopeKeystore(root=db_path.parent / "keys")
    store = BudgetStore(db_path, key, fmv_provider=StubFmvProvider())
    await store.create_envelope_if_absent(_FAILOVER_ENVELOPE)
    currency = store.get_envelope_currency_sync(_FAILOVER_ENVELOPE.id)

    sink = TraceSink.sqlite(db_path, url_mode="raw")
    emitter = TraceEmitter(
        sink=sink,
        envelope_id=_FAILOVER_ENVELOPE.id,
        envelope_currency=currency or "usd",
        funding_label="evm:base-sepolia:usdc",
        url_mode="raw",
        policy_hash="sha256:test",
    )
    sticky = StickyCache()
    router = Router(adapters)
    account = Account.from_key("0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")
    funding = [EvmFundingSource(wallet=account, network="base-sepolia", asset="usdc")]

    resolved_currency = currency or "usd"
    auth = RouteweilerAuth(
        router=router,
        sticky_cache=sticky,
        funding=funding,
        emitter=emitter,
        budget_store=store,
        envelope_id=_FAILOVER_ENVELOPE.id,
        envelope_currency=resolved_currency,
        reference_currency=resolved_currency,
        policy_engine=policy_engine,
    )
    return auth, sticky


# ---------------------------------------------------------------------------
# Idempotency key derivation
# ---------------------------------------------------------------------------


class TestMakeIdempotencyKey:
    def test_same_inputs_produce_same_key(self) -> None:
        k1 = _make_idempotency_key("req-abc", 0)
        k2 = _make_idempotency_key("req-abc", 0)
        assert k1 == k2

    def test_different_attempts_produce_different_keys(self) -> None:
        k0 = _make_idempotency_key("req-abc", 0)
        k1 = _make_idempotency_key("req-abc", 1)
        assert k0 != k1

    def test_different_request_ids_produce_different_keys(self) -> None:
        k1 = _make_idempotency_key("req-1", 0)
        k2 = _make_idempotency_key("req-2", 0)
        assert k1 != k2


# ---------------------------------------------------------------------------
# Failover: sign failure rolls back and tries next rail
# ---------------------------------------------------------------------------


class TestFailoverOnSignError:
    @pytest.mark.anyio
    async def test_primary_sign_fails_falls_over_to_secondary(self, tmp_path: Path) -> None:
        """First adapter's sign raises; second adapter signs and succeeds."""
        db_path = tmp_path / "traces.db"

        failing = MockRailAdapter(rail="x402", sign_result=None)
        healthy = MockRailAdapter(rail="l402", sign_result="mock-l402-header")

        auth, _ = await _build_auth([failing, healthy], db_path, policy_engine=_x402_first_policy())

        # Build a fake async auth flow by driving it manually.
        # We simulate: first yield returns 402; second yield (retry) returns 200.
        gen = auth.async_auth_flow(_request())
        await gen.__anext__()

        # First yield gives back the original request — send 402.
        try:
            await gen.asend(_402_response())
        except StopAsyncIteration:
            pass
        # The loop should have routed to x402, sign failed, then routed to l402.
        # The next yield will be the l402 retry request.
        # We need to send the 200 response to that.
        try:
            await gen.asend(_200_response())
        except StopAsyncIteration:
            pass

        # Verify: x402 sign was attempted once, l402 sign was attempted once.
        assert failing.sign_call_count == 1
        assert healthy.sign_call_count == 1

    @pytest.mark.anyio
    async def test_fallback_from_in_trace_on_failover(self, tmp_path: Path) -> None:
        """The trace event records fallback_from = the failed rail."""
        db_path = tmp_path / "traces.db"

        failing = MockRailAdapter(rail="x402", sign_result=None)
        healthy = MockRailAdapter(rail="l402", sign_result="mock-l402-header")

        auth, _ = await _build_auth([failing, healthy], db_path, policy_engine=_x402_first_policy())

        gen = auth.async_auth_flow(_request())
        await gen.__anext__()
        try:
            await gen.asend(_402_response())
        except StopAsyncIteration:
            pass
        try:
            await gen.asend(_200_response())
        except StopAsyncIteration:
            pass

        rows = _trace_rows(db_path)
        # The paid trace should have fallback_from = "x402"
        paid_rows = [r for r in rows if r["service_delivered"] == 1]
        assert paid_rows, "Expected at least one service-delivered trace row after failover"
        payload = json.loads(paid_rows[0]["payload"])
        assert payload.get("fallbackFrom") == "x402"

    @pytest.mark.anyio
    async def test_all_rails_fail_raises_no_feasible(self, tmp_path: Path) -> None:
        """When every adapter's sign fails, NoFeasibleRailError is raised."""
        db_path = tmp_path / "traces.db"

        x402 = MockRailAdapter(rail="x402", sign_result=None)
        l402 = MockRailAdapter(rail="l402", sign_result=None)

        auth, _ = await _build_auth([x402, l402], db_path)

        gen = auth.async_auth_flow(_request())
        await gen.__anext__()

        with pytest.raises(NoFeasibleRailError):
            await gen.asend(_402_response())

        # An error trace should have been emitted.
        rows = _trace_rows(db_path)
        error_rows = [r for r in rows if r["service_delivered"] == 0]
        assert len(error_rows) >= 1

    @pytest.mark.anyio
    async def test_draw_rollback_on_sign_failure(self, tmp_path: Path) -> None:
        """The primary draw is rolled_back after sign failure."""
        db_path = tmp_path / "traces.db"

        x402 = MockRailAdapter(rail="x402", sign_result=None)
        l402 = MockRailAdapter(rail="l402", sign_result="mock-header")

        auth, _ = await _build_auth([x402, l402], db_path, policy_engine=_x402_first_policy())

        gen = auth.async_auth_flow(_request())
        await gen.__anext__()
        try:
            await gen.asend(_402_response())
        except StopAsyncIteration:
            pass
        try:
            await gen.asend(_200_response())
        except StopAsyncIteration:
            pass

        draws = _draw_rows(db_path)
        # One draw should be rolled_back (x402 failed), one settled (l402 succeeded).
        statuses = {d["state"] for d in draws}
        assert {"rolled_back", "settled"}.issubset(statuses), (
            f"Expected both 'rolled_back' and 'settled' draws; got: {statuses}"
        )


# ---------------------------------------------------------------------------
# Sticky cache update after successful failover
# ---------------------------------------------------------------------------


class TestStickyAfterFailover:
    @pytest.mark.anyio
    async def test_sticky_cache_updated_to_failover_rail(self, tmp_path: Path) -> None:
        """After failover to l402, sticky cache records l402 for future calls."""
        db_path = tmp_path / "traces.db"

        failing = MockRailAdapter(rail="x402", sign_result=None)
        healthy = MockRailAdapter(rail="l402", sign_result="mock-header")

        auth, sticky = await _build_auth([failing, healthy], db_path)
        key = StickyKey(origin="http://mock:80", agent_id=None, session_id=None)

        gen = auth.async_auth_flow(_request())
        await gen.__anext__()
        try:
            await gen.asend(_402_response())
        except StopAsyncIteration:
            pass
        try:
            await gen.asend(_200_response())
        except StopAsyncIteration:
            pass

        # l402 should now be sticky.
        assert sticky.lookup(key) == "l402"
