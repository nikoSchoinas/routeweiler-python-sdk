"""Tests for L402Adapter.pay(), confirm(), and match_funding()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from routeweiler.errors import InvoicePaymentError, NoFundingForRailError, PreimageMismatchError
from routeweiler.funding.evm import EvmFundingSource
from routeweiler.funding.lightning import LightningFundingSource
from routeweiler.normalized import L402RailRaw, NormalizedChallenge, Payee, Price, Resource
from routeweiler.rails.l402 import L402Adapter
from tests.fixtures.fake_lnd import FakeLndClient
from tests.fixtures.l402_mock_server import (
    MOCK_BOLT11,
    MOCK_MACAROON_B64,
    MOCK_PAYMENT_HASH,
    MOCK_PREIMAGE,
    _build_mock_invoice,
)

# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lightning_source() -> LightningFundingSource:
    return LightningFundingSource(
        client=FakeLndClient(),
        network="bitcoin-regtest",
        node_pubkey="03" + "ab" * 32,
    )


@pytest.fixture
def adapter(lightning_source: LightningFundingSource) -> L402Adapter:
    return L402Adapter([lightning_source])


def _make_challenge(
    *,
    bolt11: str = MOCK_BOLT11,
    macaroon: str = MOCK_MACAROON_B64,
    payment_hash: str = MOCK_PAYMENT_HASH,
) -> NormalizedChallenge:
    return NormalizedChallenge(
        rail="l402",
        resource=Resource(
            method="GET",
            url="http://example.com/protected",
            url_encoding="raw",
            original_status=402,
        ),
        price=Price(amount=5000, currency="btc-lightning", human_amount="5000 sats"),
        payee=Payee(identifier=""),
        scheme="exact",
        nonce=payment_hash,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        raw=L402RailRaw(kind="l402", macaroon=macaroon, invoice=bolt11),
    )


# ---------------------------------------------------------------------------
# Tests: match_funding
# ---------------------------------------------------------------------------


class TestMatchFunding:
    def test_regtest_source_matches_regtest_invoice(
        self, adapter: L402Adapter, lightning_source: LightningFundingSource
    ) -> None:
        challenge = _make_challenge()  # MOCK_BOLT11 is lnbcrt → regtest
        result = adapter.match_funding(challenge, [lightning_source])
        assert result is lightning_source

    def test_testnet_source_does_not_match_regtest_invoice(self, adapter: L402Adapter) -> None:
        testnet_source = LightningFundingSource(
            client=FakeLndClient(),
            network="bitcoin-testnet",
            node_pubkey="03" + "cd" * 32,
        )
        challenge = _make_challenge()
        assert adapter.match_funding(challenge, [testnet_source]) is None

    def test_empty_funding_returns_none(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        assert adapter.match_funding(challenge, []) is None

    def test_evm_source_is_skipped(self, adapter: L402Adapter) -> None:
        evm = EvmFundingSource(wallet=MagicMock(), network="base", asset="usdc")
        challenge = _make_challenge()
        assert adapter.match_funding(challenge, [evm]) is None  # type: ignore[arg-type]

    def test_mainnet_invoice_matches_mainnet_source(self) -> None:
        mainnet_invoice = _build_mock_invoice(MOCK_PAYMENT_HASH).replace("lnbcrt", "lnbc", 1)
        mainnet_source = LightningFundingSource(
            client=FakeLndClient(),
            network="bitcoin",
            node_pubkey="03" + "ef" * 32,
        )
        challenge = _make_challenge(bolt11=mainnet_invoice)
        a = L402Adapter([mainnet_source])
        assert a.match_funding(challenge, [mainnet_source]) is mainnet_source


# ---------------------------------------------------------------------------
# Tests: pay
# ---------------------------------------------------------------------------


class TestPay:
    async def test_happy_path_returns_authorization_header(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.header_name == "authorization"
        assert result.header_value is not None
        assert result.header_value.startswith("L402 ")

    async def test_header_value_format(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.header_value is not None
        # L402 <macaroon>:<preimage_hex>
        _, credential_str = result.header_value.split(" ", 1)
        mac, preimage_hex = credential_str.rsplit(":", 1)
        assert mac == MOCK_MACAROON_B64
        assert preimage_hex == MOCK_PREIMAGE.hex()

    async def test_proof_type_is_preimage(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.proof_type == "preimage"

    async def test_proof_value_is_preimage_hex(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.proof_value == MOCK_PREIMAGE.hex()

    async def test_credential_contains_macaroon(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.credential is not None
        assert result.credential["macaroon"] == MOCK_MACAROON_B64

    async def test_credential_contains_preimage_hex(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.credential is not None
        assert result.credential["preimage_hex"] == MOCK_PREIMAGE.hex()

    async def test_credential_contains_payment_hash(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.credential is not None
        assert result.credential["payment_hash_hex"] == MOCK_PAYMENT_HASH

    async def test_credential_contains_invoice(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.credential is not None
        assert result.credential["invoice"] == MOCK_BOLT11

    async def test_preimage_mismatch_raises(self) -> None:
        wrong_preimage = bytes(32)  # all zeros — sha256 != MOCK_PAYMENT_HASH
        bad_source = LightningFundingSource(
            client=FakeLndClient(preimage=wrong_preimage),
            network="bitcoin-regtest",
            node_pubkey="03" + "ab" * 32,
        )
        adapter = L402Adapter([bad_source])
        challenge = _make_challenge()
        with pytest.raises(PreimageMismatchError):
            await adapter.pay(challenge)

    async def test_invoice_payment_failure_propagates(self) -> None:
        failing_source = LightningFundingSource(
            client=FakeLndClient(should_fail=True),
            network="bitcoin-regtest",
            node_pubkey="03" + "ab" * 32,
        )
        adapter = L402Adapter([failing_source])
        challenge = _make_challenge()
        with pytest.raises(InvoicePaymentError):
            await adapter.pay(challenge)

    async def test_no_funding_source_raises(self) -> None:
        empty_adapter = L402Adapter([])
        challenge = _make_challenge()
        with pytest.raises(NoFundingForRailError):
            await empty_adapter.pay(challenge)

    async def test_pay_returns_preimage_proof(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        result = await adapter.pay(challenge)
        assert result.proof_type == "preimage"


# ---------------------------------------------------------------------------
# Tests: confirm
# ---------------------------------------------------------------------------


class TestConfirm:
    async def test_confirm_success_on_200(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        payment_result = await adapter.pay(challenge)
        response = httpx.Response(200)
        settlement = await adapter.confirm(payment_result, response)

        assert settlement.success is True

    async def test_confirm_failure_on_401(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        payment_result = await adapter.pay(challenge)
        response = httpx.Response(401)
        settlement = await adapter.confirm(payment_result, response)

        assert settlement.success is False

    async def test_confirm_amount_paid_in_sats(self, adapter: L402Adapter) -> None:
        challenge = _make_challenge()
        payment_result = await adapter.pay(challenge)
        response = httpx.Response(200)
        settlement = await adapter.confirm(payment_result, response)

        # MOCK_BOLT11 = 50000n msat = 5000 sats
        assert settlement.amount_paid == 5000
