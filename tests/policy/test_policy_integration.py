"""Integration tests: policy enforcement in the auth flow.

Verifies that `deny: true` and `max_per_call_minor_units` are enforced by
RouteweilerAuth before any budget draw is attempted.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routeweiler import Routeweiler
from routeweiler.budgets.schema import BudgetEnvelope
from routeweiler.errors import FmvUnavailableError, PolicyDeniedError, PolicyMaxPerCallExceededError
from routeweiler.funding.evm import EvmFundingSource
from routeweiler.policy.dsl import Policy, PolicyRule, RuleMatch
from routeweiler.trace.sink_sqlite import TraceSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _draw_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    conn.close()
    return int(count)


def _trace_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trace_events ORDER BY ts_start").fetchall()
    conn.close()
    return [dict(r) for r in rows]


_POLICY_TEST_ENVELOPE = BudgetEnvelope(
    id="policy_test_env",
    cap_minor_units=100_000,
    cap_currency="usd",
    allowed_rails=["x402"],
    ttl_seconds=86_400,
)


def _make_client(
    test_account,  # type: ignore[no-untyped-def]
    transport: httpx.ASGITransport,
    db_path: Path,
    *,
    policy: Policy | None = None,
    budget_envelope: BudgetEnvelope | str | None = None,
    keystore_root: Path | None = None,
) -> Routeweiler:
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    with patch("routeweiler.rails.x402.x402Client") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.create_payment_payload = AsyncMock(
            return_value={
                "x402Version": 2,
                "payload": {
                    "authorization": {
                        "from": test_account.address,
                        "to": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                        "value": "1000",
                        "validAfter": "0",
                        "validBefore": "9999999999",
                        "nonce": "0xdeadbeef",
                    },
                    "signature": "0x" + "ab" * 65,
                },
            }
        )
        mock_cls.return_value = mock_instance

        kwargs: dict = {}
        if budget_envelope is not None:
            kwargs["budget_envelope"] = budget_envelope
        if keystore_root is not None:
            kwargs["keystore_root"] = keystore_root

        client = Routeweiler(
            funding=[EvmFundingSource(wallet=test_account, network="base-sepolia", asset="usdc")],
            trace_sink=sink,
            policy=policy,
            **kwargs,
        )
        client._http = httpx.AsyncClient(
            auth=client._http.auth,
            event_hooks=client._http.event_hooks,
            transport=transport,
        )
    return client


# ---------------------------------------------------------------------------
# deny: true enforcement
# ---------------------------------------------------------------------------


async def test_deny_rule_blocks_payment_and_emits_error_trace(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """A deny rule raises PolicyDeniedError; no draw is inserted."""
    policy = Policy(
        rules=[
            PolicyRule(
                name="deny-mock",
                when=RuleMatch(url_matches="http://mock/*"),
                deny=True,
                reason="test deny",
            )
        ]
    )
    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path, policy=policy)

    with pytest.raises(PolicyDeniedError) as exc_info:
        await client.get("http://mock/protected")
    await client.aclose()

    assert "test deny" in str(exc_info.value)

    assert _draw_count(tmp_trace_db_path) == 0

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    assert rows[0]["http_status"] == 402
    assert rows[0]["service_delivered"] == 0


# ---------------------------------------------------------------------------
# max_per_call_minor_units enforcement
# ---------------------------------------------------------------------------


async def test_max_per_call_blocks_oversized_payment(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """max_per_call_minor_units=0 rejects the 1000-unit USDC challenge."""
    policy = Policy(
        rules=[
            PolicyRule(
                name="tiny-cap",
                when=RuleMatch(url_matches="http://mock/*"),
                max_per_call_minor_units=0,
            )
        ]
    )
    client = _make_client(
        test_account,
        mock_x402_app,
        tmp_trace_db_path,
        policy=policy,
        budget_envelope=_POLICY_TEST_ENVELOPE,
        keystore_root=tmp_trace_db_path.parent / "keys",
    )

    async with client:
        with pytest.raises(PolicyMaxPerCallExceededError) as exc_info:
            await client.get("http://mock/protected")

    assert exc_info.value.limit == 0
    assert exc_info.value.requested > 0

    assert _draw_count(tmp_trace_db_path) == 0

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    assert rows[0]["service_delivered"] == 0


# ---------------------------------------------------------------------------
# max_per_call_minor_units without budget_envelope (policy.currency path)
# ---------------------------------------------------------------------------


async def test_max_per_call_blocks_without_envelope(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """max_per_call_minor_units=0 rejects payment even without a budget_envelope."""
    policy = Policy(
        currency="usd",
        rules=[
            PolicyRule(
                name="tiny-cap-no-envelope",
                when=RuleMatch(url_matches="http://mock/*"),
                max_per_call_minor_units=0,
            )
        ],
    )
    # No budget_envelope — policy.currency="usd" is the reference currency.
    client = _make_client(
        test_account,
        mock_x402_app,
        tmp_trace_db_path,
        policy=policy,
        budget_envelope=None,
    )

    async with client:
        with pytest.raises(PolicyMaxPerCallExceededError) as exc_info:
            await client.get("http://mock/protected")

    assert exc_info.value.limit == 0
    assert exc_info.value.requested > 0

    # No draws possible without an envelope.
    assert _draw_count(tmp_trace_db_path) == 0

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    assert rows[0]["service_delivered"] == 0


async def test_max_per_call_fmv_unavailable_fails_closed(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """FMV unavailable for a capped rail raises FmvUnavailableError (fail closed)."""
    policy = Policy(
        currency="usd",
        rules=[
            PolicyRule(
                name="cap-with-fmv-fail",
                when=RuleMatch(url_matches="http://mock/*"),
                max_per_call_minor_units=9999,
            )
        ],
    )
    client = _make_client(
        test_account,
        mock_x402_app,
        tmp_trace_db_path,
        policy=policy,
        budget_envelope=None,
    )

    # Patch _fmv_quote to return None (simulates FMV conversion failure).
    with patch(
        "routeweiler.routing.router._fmv_quote",
        return_value=None,
    ):
        async with client:
            with pytest.raises(FmvUnavailableError):
                await client.get("http://mock/protected")


# ---------------------------------------------------------------------------
# policy_hash in trace
# ---------------------------------------------------------------------------


async def test_policy_hash_in_trace_matches_class(
    test_account,
    mock_x402_app: httpx.ASGITransport,
    tmp_trace_db_path: Path,
    tmp_path: Path,
) -> None:
    """The policy_hash in the emitted trace matches the Policy instance's hash."""
    policy = Policy(default_rail="x402")
    expected_hash = policy.policy_hash
    assert expected_hash.startswith("sha256:")

    client = _make_client(test_account, mock_x402_app, tmp_trace_db_path, policy=policy)
    await client.get("http://mock/protected")
    await client.aclose()

    rows = _trace_rows(tmp_trace_db_path)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["policyHash"] == expected_hash
