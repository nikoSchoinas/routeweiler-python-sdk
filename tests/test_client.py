"""End-to-end tests for the Routeweiler async client using respx mocks."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from routeweiler import Funding, Routeweiler
from routeweiler.errors import RailNotSupportedError
from routeweiler.policy.dsl import Policy, PolicyRule, RuleMatch
from routeweiler.trace.sink_sqlite import TraceSink


def _encode_challenge(data: dict) -> str:  # type: ignore[type-arg]
    return base64.b64encode(json.dumps(data).encode()).decode()


_CHALLENGE = {
    "accepts": [
        {
            "scheme": "exact",
            "network": "base",
            "maxAmountRequired": "1000",
            "resource": "https://api.example.com/data",
            "description": "Test endpoint",
            "mimeType": "application/json",
            "payTo": "0x1234567890123456789012345678901234567890",
            "maxTimeoutSeconds": 60,
            "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "extra": {"nonce": "0xabc", "validBefore": 9999999999, "validAfter": 0},
        }
    ]
}
_PAYMENT_REQUIRED_HEADER = _encode_challenge(_CHALLENGE)
_SIGNED_PAYLOAD = base64.b64encode(b'{"signature":"0xtest"}').decode()


@pytest.fixture
def routeweiler_client(test_account) -> Routeweiler:  # type: ignore[no-untyped-def]
    return Routeweiler(funding=[Funding.base_usdc(wallet=test_account)])


@respx.mock
async def test_happy_path_402_then_200(routeweiler_client: Routeweiler) -> None:
    """A 402 response triggers a signed retry that returns 200."""
    url = "https://api.example.com/data"

    with patch(
        "routeweiler.rails.x402.x402Client",
    ) as mock_cls:
        mock_instance = MagicMock()
        mock_instance.create_payment_payload = AsyncMock(return_value={"signature": "0xtest"})
        mock_cls.return_value = mock_instance

        # Re-create client so it picks up the patched x402Client

        client = Routeweiler(
            funding=[Funding.base_usdc(wallet=routeweiler_client._funding[0].wallet)]
        )

        # First call → 402; second call → 200
        route = respx.get(url)
        route.side_effect = [
            httpx.Response(
                status_code=402,
                headers={"PAYMENT-REQUIRED": _PAYMENT_REQUIRED_HEADER},
                content=b"payment required",
            ),
            httpx.Response(status_code=200, json={"result": "ok"}),
        ]

        resp = await client.get(url)

    assert resp.status_code == 200
    assert resp.json() == {"result": "ok"}
    # Second request must carry PAYMENT-SIGNATURE
    assert route.call_count == 2
    last_request = route.calls[-1].request
    assert "PAYMENT-SIGNATURE" in last_request.headers


@respx.mock
async def test_200_passthrough(routeweiler_client: Routeweiler) -> None:
    """Non-402 responses pass through without payment."""
    respx.get("https://api.example.com/free").mock(
        return_value=httpx.Response(200, json={"free": True})
    )
    resp = await routeweiler_client.get("https://api.example.com/free")
    assert resp.status_code == 200
    assert resp.json()["free"] is True


@respx.mock
async def test_unsupported_rail_raises(routeweiler_client: Routeweiler) -> None:
    """A 402 with no PAYMENT-REQUIRED header raises RailNotSupportedError."""
    respx.get("https://api.example.com/l402").mock(
        return_value=httpx.Response(
            402, headers={"WWW-Authenticate": 'L402 macaroon="abc", invoice="lnbc..."'}
        )
    )
    with pytest.raises(RailNotSupportedError):
        await routeweiler_client.get("https://api.example.com/l402")


async def test_context_manager(test_account) -> None:  # type: ignore[no-untyped-def]
    async with Routeweiler(funding=[Funding.base_usdc(wallet=test_account)]) as client:
        assert client._http is not None


# ---------------------------------------------------------------------------
# No-budget-envelope mode
# ---------------------------------------------------------------------------


@respx.mock
async def test_no_budget_envelope_succeeds_and_writes_trace(
    test_account,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """Routeweiler with trace_sink but no budget_envelope pays without enforcement.

    Asserts:
    - 402 → 200 succeeds.
    - Exactly one trace row written with envelope_id IS NULL.
    - No draw row created.
    - amount_envelope is NULL; fmv_quality is "unavailable".
    """
    db_path = tmp_path / "traces.db"
    sink = TraceSink.sqlite(db_path, url_mode="raw")

    with patch("routeweiler.rails.x402.x402Client") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.create_payment_payload = AsyncMock(
            return_value={
                "x402Version": 1,
                "payload": {
                    "authorization": {
                        "from": test_account.address,
                        "to": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
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

        client = Routeweiler(
            funding=[Funding.base_usdc(wallet=test_account)],
            trace_sink=sink,
            # budget_envelope deliberately omitted → no enforcement
        )

        url = "https://api.example.com/data"
        respx.get(url).side_effect = [
            httpx.Response(
                status_code=402,
                headers={"PAYMENT-REQUIRED": _PAYMENT_REQUIRED_HEADER},
                content=b"payment required",
            ),
            httpx.Response(status_code=200, json={"result": "ok"}),
        ]

        async with client:
            resp = await client.get(url)

    assert resp.status_code == 200

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    trace_rows = conn.execute("SELECT * FROM trace_events").fetchall()
    assert len(trace_rows) == 1
    row = dict(trace_rows[0])
    assert row["envelope_id"] is None
    assert row["amount_envelope"] is None
    assert row["amount_envelope_currency"] is None
    assert row["fmv_quality"] == "unavailable"

    draw_rows = conn.execute("SELECT * FROM draws").fetchall()
    assert len(draw_rows) == 0

    env_rows = conn.execute("SELECT * FROM envelopes").fetchall()
    assert len(env_rows) == 0

    conn.close()


async def test_no_budget_envelope_no_envelopes_table_row(
    test_account,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """Constructing Routeweiler without budget_envelope creates no envelope row."""
    db_path = tmp_path / "traces.db"
    sink = TraceSink.sqlite(db_path, url_mode="raw")
    async with Routeweiler(
        funding=[Funding.base_usdc(wallet=test_account)],
        trace_sink=sink,
    ):
        pass

    conn = sqlite3.connect(str(db_path))
    env_count = conn.execute("SELECT COUNT(*) FROM envelopes").fetchone()[0]
    conn.close()
    assert env_count == 0


def test_policy_max_per_call_without_currency_or_envelope_raises(
    test_account,  # type: ignore[no-untyped-def]
) -> None:
    """max_per_call_minor_units with no currency source raises ValueError at construction."""
    policy = Policy(
        rules=[
            PolicyRule(
                name="cap-calls",
                when=RuleMatch(url_matches="*"),
                max_per_call_minor_units=100,
            )
        ]
    )
    with pytest.raises(ValueError, match="max_per_call_minor_units"):
        Routeweiler(
            funding=[Funding.base_usdc(wallet=test_account)],
            policy=policy,
            # no budget_envelope and no policy.currency
        )


async def test_policy_max_per_call_with_policy_currency_constructs(
    test_account,  # type: ignore[no-untyped-def]
) -> None:
    """max_per_call_minor_units with policy.currency='usd' constructs without error."""
    policy = Policy(
        currency="usd",
        rules=[
            PolicyRule(
                name="cap-calls",
                when=RuleMatch(url_matches="*"),
                max_per_call_minor_units=100,
            )
        ],
    )
    client = Routeweiler(
        funding=[Funding.base_usdc(wallet=test_account)],
        policy=policy,
        # no budget_envelope — policy.currency provides the reference
    )
    await client.aclose()
