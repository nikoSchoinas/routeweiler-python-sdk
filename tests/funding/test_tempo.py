"""Tests for TempoFundingSource, EthAccountTempoSigner, and Funding factories."""

from __future__ import annotations

import typing

import pytest
from eth_account import Account

from routeweiler.funding import Funding, FundingSource
from routeweiler.funding.tempo import (
    EthAccountTempoSigner,
    TempoFundingSource,
    TempoSigner,
)
from tests.fixtures.fake_tempo import FAKE_SIGNED_TX, FakeTempoSigner

# Deterministic test wallet — DO NOT USE WITH REAL FUNDS.
_TEST_PRIVATE_KEY = "0x" + "aa" * 32
_TEST_WALLET = Account.from_key(_TEST_PRIVATE_KEY)
_TEST_TOKEN = "0x20c0000000000000000000000000000000000000"
_TEST_RECIPIENT = "0x" + "bb" * 20


# ---------------------------------------------------------------------------
# FakeTempoSigner
# ---------------------------------------------------------------------------


class TestFakeTempoSigner:
    def test_satisfies_protocol(self) -> None:
        signer = FakeTempoSigner()
        assert isinstance(signer, TempoSigner)

    def test_chain_id_property(self) -> None:
        signer = FakeTempoSigner(chain_id=42431)
        assert signer.chain_id == 42431

    def test_address_property(self) -> None:
        signer = FakeTempoSigner(address="0xDeaDBeef" + "00" * 16)
        assert signer.address == "0xDeaDBeef" + "00" * 16

    async def test_sign_transaction_returns_fake_tx(self) -> None:
        signer = FakeTempoSigner()
        result = await signer.sign_transaction(
            tip20_token=_TEST_TOKEN,
            recipient=_TEST_RECIPIENT,
            amount=10_000,
            nonce=0,
            valid_before=9_999_999_999,
        )
        assert result == FAKE_SIGNED_TX

    async def test_sign_transaction_failure_propagates(self) -> None:
        signer = FakeTempoSigner(should_fail=True)
        with pytest.raises(RuntimeError, match="forced failure"):
            await signer.sign_transaction(
                tip20_token=_TEST_TOKEN,
                recipient=_TEST_RECIPIENT,
                amount=10_000,
                nonce=0,
                valid_before=9_999_999_999,
            )


# ---------------------------------------------------------------------------
# EthAccountTempoSigner
# ---------------------------------------------------------------------------


class TestEthAccountTempoSigner:
    def test_satisfies_protocol(self) -> None:
        signer = EthAccountTempoSigner(wallet=_TEST_WALLET, chain_id=42431)
        assert isinstance(signer, TempoSigner)

    def test_chain_id(self) -> None:
        signer = EthAccountTempoSigner(wallet=_TEST_WALLET, chain_id=42431)
        assert signer.chain_id == 42431

    def test_address_is_checksummed(self) -> None:
        signer = EthAccountTempoSigner(wallet=_TEST_WALLET, chain_id=42431)
        assert signer.address == _TEST_WALLET.address

    async def test_sign_transaction_returns_0x76_prefix(self) -> None:
        signer = EthAccountTempoSigner(wallet=_TEST_WALLET, chain_id=42431)
        tx = await signer.sign_transaction(
            tip20_token=_TEST_TOKEN,
            recipient=_TEST_RECIPIENT,
            amount=10_000,
            nonce=0,
            valid_before=9_999_999_999,
        )
        assert tx.startswith("0x76")


# ---------------------------------------------------------------------------
# TempoFundingSource
# ---------------------------------------------------------------------------


class TestTempoFundingSource:
    def test_frozen_dataclass(self) -> None:
        signer = FakeTempoSigner()
        fs = TempoFundingSource(signer=signer, network="tempo-moderato", asset="pathusd")
        with pytest.raises((AttributeError, TypeError)):
            fs.asset = "usdc"  # type: ignore[misc]

    def test_fields(self) -> None:
        signer = FakeTempoSigner(chain_id=42431)
        fs = TempoFundingSource(signer=signer, network="tempo-moderato", asset="pathusd")
        assert fs.network == "tempo-moderato"
        assert fs.asset == "pathusd"
        assert fs.signer is signer


# ---------------------------------------------------------------------------
# Funding factories
# ---------------------------------------------------------------------------


class TestFundingFactories:
    def test_tempo_pathusd_moderato_returns_source(self) -> None:
        fs = Funding.tempo_pathusd_moderato(wallet=_TEST_WALLET)
        assert isinstance(fs, TempoFundingSource)
        assert fs.network == "tempo-moderato"
        assert fs.asset == "pathusd"
        assert fs.signer.chain_id == 42431

    def test_tempo_pathusd_moderato_address_matches_wallet(self) -> None:
        fs = Funding.tempo_pathusd_moderato(wallet=_TEST_WALLET)
        assert fs.signer.address == _TEST_WALLET.address

    def test_tempo_usdc_returns_source(self) -> None:
        fs = Funding.tempo_usdc(wallet=_TEST_WALLET)
        assert isinstance(fs, TempoFundingSource)
        assert fs.network == "tempo"
        assert fs.asset == "usdc"
        assert fs.signer.chain_id == 42430  # mainnet


# ---------------------------------------------------------------------------
# FundingSource union includes TempoFundingSource
# ---------------------------------------------------------------------------


def test_tempo_in_funding_source_union() -> None:
    signer = FakeTempoSigner()
    fs = TempoFundingSource(signer=signer, network="tempo-moderato", asset="pathusd")
    assert isinstance(fs, TempoFundingSource)
    # FundingSource is a type alias; verify the isinstance check works for the union
    # (Python doesn't support isinstance on Union directly, but we can verify the type)
    args = typing.get_args(FundingSource)
    assert TempoFundingSource in args
