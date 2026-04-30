"""Shared pytest fixtures for the Routewiler test suite."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount

from routewiler.funding.evm import EvmFundingSource

# ---------------------------------------------------------------------------
# Test private key — DETERMINISTIC TEST KEY — DO NOT FUND
# This key is public knowledge; never use it with real funds.
# ---------------------------------------------------------------------------
_TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


@pytest.fixture(scope="session")
def test_account() -> LocalAccount:
    """A deterministic LocalAccount for signing tests. DO NOT FUND."""
    return Account.from_key(_TEST_PRIVATE_KEY)


@pytest.fixture(scope="session")
def base_usdc_funding(test_account: LocalAccount) -> EvmFundingSource:
    return EvmFundingSource(wallet=test_account, network="base", asset="usdc")


@pytest.fixture(scope="session")
def base_sepolia_usdc_funding(test_account: LocalAccount) -> EvmFundingSource:
    return EvmFundingSource(wallet=test_account, network="base-sepolia", asset="usdc")


# ---------------------------------------------------------------------------
# Fixture loader helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "x402"


def load_challenge_fixture(name: str) -> dict:  # type: ignore[type-arg]
    """Load a JSON challenge fixture and return the decoded dict."""
    return json.loads((_FIXTURE_DIR / name).read_text())


def encode_challenge(data: dict) -> str:  # type: ignore[type-arg]
    """Base64-encode a challenge dict as the PAYMENT-REQUIRED header value."""
    return base64.b64encode(json.dumps(data).encode()).decode()


@pytest.fixture(scope="session")
def challenge_base_usdc_dict() -> dict:  # type: ignore[type-arg]
    return load_challenge_fixture("challenge_base_usdc.json")


@pytest.fixture(scope="session")
def challenge_multi_accept_dict() -> dict:  # type: ignore[type-arg]
    return load_challenge_fixture("challenge_multi_accept.json")


@pytest.fixture(scope="session")
def challenge_base_usdc_header(challenge_base_usdc_dict: dict) -> str:  # type: ignore[type-arg]
    return encode_challenge(challenge_base_usdc_dict)


@pytest.fixture(scope="session")
def challenge_multi_accept_header(challenge_multi_accept_dict: dict) -> str:  # type: ignore[type-arg]
    return encode_challenge(challenge_multi_accept_dict)
