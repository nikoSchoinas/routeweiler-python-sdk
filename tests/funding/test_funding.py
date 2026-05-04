"""Tests for Funding builders and EvmFundingSource."""

import pytest
from eth_account.signers.local import LocalAccount

from routewiler.funding import EvmFundingSource, Funding


def test_base_usdc_returns_evm_source(test_account: LocalAccount) -> None:
    fs = Funding.base_usdc(wallet=test_account)
    assert isinstance(fs, EvmFundingSource)
    assert fs.network == "base"
    assert fs.asset == "usdc"
    assert fs.wallet is test_account


def test_base_sepolia_usdc(test_account: LocalAccount) -> None:
    fs = Funding.base_sepolia_usdc(wallet=test_account)
    assert fs.network == "base-sepolia"
    assert fs.asset == "usdc"


def test_evm_funding_source_is_frozen(test_account: LocalAccount) -> None:
    fs = EvmFundingSource(wallet=test_account, network="base", asset="usdc")
    with pytest.raises((AttributeError, TypeError)):
        fs.network = "polygon"  # type: ignore[misc]


def test_balance_not_implemented(test_account: LocalAccount) -> None:
    fs = Funding.base_usdc(wallet=test_account)
    with pytest.raises(NotImplementedError):
        fs.balance()
