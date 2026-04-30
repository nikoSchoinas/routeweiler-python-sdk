"""Tests for X402Adapter.parse()."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest

from routewiler.errors import ChallengeParseError, NoFundingForRailError
from routewiler.funding.evm import EvmFundingSource
from routewiler.normalized import NormalizedChallenge, X402RailRaw
from routewiler.rails.x402 import X402Adapter


def _make_request(
    method: str = "GET", url: str = "https://api.example.com/data"
) -> httpx.Request:
    return httpx.Request(method, url)


def _make_402(header_value: str) -> httpx.Response:
    return httpx.Response(status_code=402, headers={"PAYMENT-REQUIRED": header_value})


def _encode(data: dict) -> str:  # type: ignore[type-arg]
    return base64.b64encode(json.dumps(data).encode()).decode()


@pytest.fixture
def adapter(base_usdc_funding: EvmFundingSource) -> X402Adapter:
    return X402Adapter([base_usdc_funding], _x402_client=MagicMock())


# ---------------------------------------------------------------------------
# Happy-path parsing
# ---------------------------------------------------------------------------


def test_parse_single_accept(
    adapter: X402Adapter,
    challenge_base_usdc_dict: dict,  # type: ignore[type-arg]
    challenge_base_usdc_header: str,
) -> None:
    challenge = adapter.parse(_make_request(), _make_402(challenge_base_usdc_header))

    assert isinstance(challenge, NormalizedChallenge)
    assert challenge.rail == "x402"
    assert challenge.scheme == "exact"
    assert challenge.price.amount == 1000
    assert (
        challenge.price.currency
        == "eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    assert "USDC" in challenge.price.human_amount
    assert challenge.payee.identifier == "0x1234567890123456789012345678901234567890"
    assert isinstance(challenge.raw, X402RailRaw)
    assert len(challenge.raw.accepts) == 1


def test_parse_multi_accept_picks_matching(
    adapter: X402Adapter,
    challenge_multi_accept_dict: dict,  # type: ignore[type-arg]
    challenge_multi_accept_header: str,
) -> None:
    # Adapter has base mainnet funding — should pick first entry
    challenge = adapter.parse(_make_request(), _make_402(challenge_multi_accept_header))
    assert challenge.price.currency.startswith("eip155:8453/")
    assert len(challenge.raw.accepts) == 2  # full array preserved


def test_parse_multi_accept_picks_testnet(
    base_sepolia_usdc_funding: EvmFundingSource,
    challenge_multi_accept_header: str,
) -> None:
    adapter = X402Adapter([base_sepolia_usdc_funding], _x402_client=MagicMock())
    challenge = adapter.parse(_make_request(), _make_402(challenge_multi_accept_header))
    # Should pick the second entry (base-sepolia)
    assert challenge.price.currency.startswith("eip155:84532/")


def test_parse_nonce_from_extra(
    adapter: X402Adapter,
    challenge_base_usdc_header: str,
) -> None:
    challenge = adapter.parse(_make_request(), _make_402(challenge_base_usdc_header))
    assert (
        challenge.nonce
        == "0x0000000000000000000000000000000000000000000000000000000000000001"
    )


def test_parse_expires_at_from_valid_before(
    adapter: X402Adapter,
    challenge_base_usdc_header: str,
) -> None:
    challenge = adapter.parse(_make_request(), _make_402(challenge_base_usdc_header))
    # validBefore=9999999999 → far-future datetime
    assert challenge.expires_at > datetime.now(UTC)


def test_parse_resource_from_request(
    adapter: X402Adapter,
    challenge_base_usdc_header: str,
) -> None:
    req = _make_request("POST", "https://api.example.com/submit")
    challenge = adapter.parse(req, _make_402(challenge_base_usdc_header))
    assert challenge.resource.method == "POST"
    assert "api.example.com" in challenge.resource.url


def test_parse_human_amount(
    adapter: X402Adapter,
    challenge_base_usdc_header: str,
) -> None:
    challenge = adapter.parse(_make_request(), _make_402(challenge_base_usdc_header))
    # 1000 base units with 6 decimals = 0.001 USDC
    assert "USDC" in challenge.price.human_amount


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_parse_invalid_base64(adapter: X402Adapter) -> None:
    resp = _make_402("not-valid-base64!!!")
    with pytest.raises(ChallengeParseError, match="Cannot decode"):
        adapter.parse(_make_request(), resp)


def test_parse_invalid_json(adapter: X402Adapter) -> None:
    bad = base64.b64encode(b"not json {{{").decode()
    with pytest.raises(ChallengeParseError, match="Cannot decode"):
        adapter.parse(_make_request(), _make_402(bad))


def test_parse_missing_accepts_field(adapter: X402Adapter) -> None:
    payload = _encode({"error": "Payment Required"})
    with pytest.raises(ChallengeParseError, match="no 'accepts'"):
        adapter.parse(_make_request(), _make_402(payload))


def test_parse_empty_accepts_list(adapter: X402Adapter) -> None:
    payload = _encode({"accepts": []})
    with pytest.raises(ChallengeParseError, match="empty"):
        adapter.parse(_make_request(), _make_402(payload))


def test_parse_no_matching_funding(
    base_usdc_funding: EvmFundingSource,
    challenge_base_usdc_dict: dict,  # type: ignore[type-arg]
) -> None:
    # Only testnet funding, mainnet challenge → NoFundingForRailError
    testnet_adapter = X402Adapter(
        [
            EvmFundingSource(
                wallet=base_usdc_funding.wallet, network="base-sepolia", asset="usdc"
            )
        ],
        _x402_client=MagicMock(),
    )
    header = base64.b64encode(json.dumps(challenge_base_usdc_dict).encode()).decode()
    with pytest.raises(NoFundingForRailError):
        testnet_adapter.parse(_make_request(), _make_402(header))
