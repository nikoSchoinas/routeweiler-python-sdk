"""Tests for X402Adapter.sign()."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from routewiler.errors import SigningError
from routewiler.funding.evm import EvmFundingSource
from routewiler.normalized import NormalizedChallenge, X402PaymentRequirements, X402RailRaw
from routewiler.rails.x402 import X402Adapter

_PR_CAMEL = {
    "scheme": "exact",
    "network": "base",
    "maxAmountRequired": "1000",
    "payTo": "0x1234567890123456789012345678901234567890",
    "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "resource": "https://api.example.com/data",
}


def _make_challenge(accepts_override: list | None = None) -> NormalizedChallenge:
    accepts = accepts_override or [X402PaymentRequirements.model_validate(_PR_CAMEL)]
    raw = X402RailRaw(kind="x402", accepts=accepts)
    return NormalizedChallenge(
        rail="x402",
        resource={
            "method": "GET",
            "url": "https://api.example.com/data",
            "url_encoding": "raw",
            "original_status": 402,
        },
        price={
            "amount": 1000,
            "currency": "eip155:8453/erc20:0x833589...",
            "human_amount": "0.001 USDC",
        },
        payee={"identifier": "0x1234567890123456789012345678901234567890"},
        scheme="exact",
        nonce="0xabc",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        raw=raw,
    )


@pytest.fixture
def mock_x402_client() -> MagicMock:
    client = MagicMock()
    client.create_payment_payload = AsyncMock(
        return_value={
            "x-payment": "signed-payload",
            "signature": "0x" + "ab" * 65,
        }
    )
    return client


@pytest.fixture
def adapter(base_usdc_funding: EvmFundingSource, mock_x402_client: MagicMock) -> X402Adapter:
    return X402Adapter([base_usdc_funding], _x402_client=mock_x402_client)


async def test_sign_returns_string(adapter: X402Adapter) -> None:
    challenge = _make_challenge()
    result = await adapter.sign(challenge)
    assert isinstance(result, str)
    assert len(result) > 0


async def test_sign_passes_accepts_to_sdk(
    adapter: X402Adapter, mock_x402_client: MagicMock
) -> None:
    challenge = _make_challenge()
    await adapter.sign(challenge)

    call_args = mock_x402_client.create_payment_payload.call_args
    payment_required_arg = call_args[0][0] if call_args[0] else next(iter(call_args[1].values()))
    assert hasattr(payment_required_arg, "accepts")
    assert len(payment_required_arg.accepts) == 1


async def test_sign_base64_encodes_dict_payload(
    adapter: X402Adapter, mock_x402_client: MagicMock
) -> None:
    mock_x402_client.create_payment_payload = AsyncMock(return_value={"foo": "bar"})
    challenge = _make_challenge()
    result = await adapter.sign(challenge)

    decoded = json.loads(base64.b64decode(result))
    assert decoded == {"foo": "bar"}


async def test_sign_returns_string_payload_as_is(
    adapter: X402Adapter, mock_x402_client: MagicMock
) -> None:
    mock_x402_client.create_payment_payload = AsyncMock(return_value="already-encoded-string")
    challenge = _make_challenge()
    result = await adapter.sign(challenge)
    assert result == "already-encoded-string"


async def test_sign_raises_signing_error_on_sdk_failure(
    adapter: X402Adapter, mock_x402_client: MagicMock
) -> None:
    mock_x402_client.create_payment_payload = AsyncMock(side_effect=RuntimeError("network error"))
    challenge = _make_challenge()
    with pytest.raises(SigningError, match="x402 SDK signing failed"):
        await adapter.sign(challenge)
