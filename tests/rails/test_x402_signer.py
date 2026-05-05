"""Tests for X402Adapter._sign()."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from routewiler.errors import SigningError
from routewiler.funding.evm import EvmFundingSource
from routewiler.normalized import NormalizedChallenge, X402PaymentRequirements, X402RailRaw
from routewiler.rails.x402 import X402Adapter
from tests.fixtures.fake_x402_client import FakeX402Client

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


def _make_adapter(base_usdc_funding: EvmFundingSource, fake_client: FakeX402Client) -> X402Adapter:
    adapter = X402Adapter([base_usdc_funding])
    adapter._x402 = fake_client  # type: ignore[assignment]
    return adapter


@pytest.fixture
def fake_client() -> FakeX402Client:
    return FakeX402Client(
        return_value={
            "x-payment": "signed-payload",
            "signature": "0x" + "ab" * 65,
        }
    )


@pytest.fixture
def adapter(base_usdc_funding: EvmFundingSource, fake_client: FakeX402Client) -> X402Adapter:
    return _make_adapter(base_usdc_funding, fake_client)


async def test__sign_returns_string(adapter: X402Adapter) -> None:
    challenge = _make_challenge()
    result = await adapter._sign(challenge)
    assert isinstance(result, str)
    assert len(result) > 0


async def test__sign_passes_accepts_to_sdk(
    adapter: X402Adapter, fake_client: FakeX402Client
) -> None:
    challenge = _make_challenge()
    await adapter._sign(challenge)

    assert len(fake_client.calls) == 1
    payment_required_arg = fake_client.calls[0]
    assert hasattr(payment_required_arg, "accepts")
    assert len(payment_required_arg.accepts) == 1


async def test__sign_base64_encodes_dict_payload(
    base_usdc_funding: EvmFundingSource,
) -> None:
    fake = FakeX402Client(return_value={"foo": "bar"})
    adapter = _make_adapter(base_usdc_funding, fake)
    challenge = _make_challenge()
    result = await adapter._sign(challenge)

    decoded = json.loads(base64.b64decode(result))
    assert decoded == {"foo": "bar"}


async def test__sign_returns_string_payload_as_is(
    base_usdc_funding: EvmFundingSource,
) -> None:
    fake = FakeX402Client(return_value="already-encoded-string")
    adapter = _make_adapter(base_usdc_funding, fake)
    challenge = _make_challenge()
    result = await adapter._sign(challenge)
    assert result == "already-encoded-string"


async def test__sign_raises_signing_error_on_sdk_failure(
    base_usdc_funding: EvmFundingSource,
) -> None:
    fake = FakeX402Client(fail_with=RuntimeError("network error"))
    adapter = _make_adapter(base_usdc_funding, fake)
    challenge = _make_challenge()
    with pytest.raises(SigningError, match="x402 SDK signing failed"):
        await adapter._sign(challenge)
