"""Tests for LightningFundingSource and LndClient (mocked LND)."""

from __future__ import annotations

import pytest

from routewiler.errors import InvoicePaymentError
from routewiler.funding.lightning import LightningFundingSource, LightningNodeClient, LndClient

FAKE_PUBKEY = "03" + "ab" * 32
FAKE_PREIMAGE = bytes.fromhex("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20")
FAKE_BOLT11_REGTEST = "lnbcrt50000n1fake..."


# ---------------------------------------------------------------------------
# FakeLndClient — imported from the payer test module for consistency
# ---------------------------------------------------------------------------


class FakeLndClient:
    def __init__(self, preimage: bytes = FAKE_PREIMAGE, *, should_fail: bool = False) -> None:
        self._preimage = preimage
        self._fail = should_fail
        self.calls: list[dict] = []

    async def pay_invoice(self, bolt11: str, *, max_fee_msat: int) -> bytes:
        self.calls.append({"bolt11": bolt11, "max_fee_msat": max_fee_msat})
        if self._fail:
            raise InvoicePaymentError("no_route")
        return self._preimage

    async def get_node_pubkey(self) -> str:
        return FAKE_PUBKEY


# ---------------------------------------------------------------------------
# Tests: LightningFundingSource construction
# ---------------------------------------------------------------------------


class TestLightningFundingSourceCreate:
    async def test_async_factory_populates_pubkey(self) -> None:
        client = FakeLndClient()
        source = await LightningFundingSource.create(client, "bitcoin-regtest")
        assert source.node_pubkey == FAKE_PUBKEY

    async def test_async_factory_sets_network(self) -> None:
        client = FakeLndClient()
        source = await LightningFundingSource.create(client, "bitcoin-testnet")
        assert source.network == "bitcoin-testnet"

    async def test_custom_max_fee_msat(self) -> None:
        client = FakeLndClient()
        source = await LightningFundingSource.create(client, "bitcoin-regtest", max_fee_msat=500)
        assert source.max_fee_msat == 500

    def test_direct_construction(self) -> None:
        client = FakeLndClient()
        source = LightningFundingSource(
            client=client,
            network="bitcoin",
            node_pubkey=FAKE_PUBKEY,
        )
        assert source.network == "bitcoin"
        assert source.node_pubkey == FAKE_PUBKEY

    def test_default_max_fee_msat(self) -> None:
        client = FakeLndClient()
        source = LightningFundingSource(client=client, network="bitcoin", node_pubkey=FAKE_PUBKEY)
        assert source.max_fee_msat == 1000


# ---------------------------------------------------------------------------
# Tests: LightningFundingSource.pay_invoice
# ---------------------------------------------------------------------------


class TestLightningFundingSourcePayInvoice:
    async def test_pay_invoice_returns_preimage(self) -> None:
        client = FakeLndClient()
        source = LightningFundingSource(
            client=client, network="bitcoin-regtest", node_pubkey=FAKE_PUBKEY
        )
        result = await source.pay_invoice(FAKE_BOLT11_REGTEST)
        assert result == FAKE_PREIMAGE

    async def test_pay_invoice_uses_source_max_fee(self) -> None:
        client = FakeLndClient()
        source = LightningFundingSource(
            client=client,
            network="bitcoin-regtest",
            node_pubkey=FAKE_PUBKEY,
            max_fee_msat=2000,
        )
        await source.pay_invoice(FAKE_BOLT11_REGTEST)
        assert client.calls[-1]["max_fee_msat"] == 2000

    async def test_pay_invoice_override_max_fee(self) -> None:
        client = FakeLndClient()
        source = LightningFundingSource(
            client=client,
            network="bitcoin-regtest",
            node_pubkey=FAKE_PUBKEY,
            max_fee_msat=1000,
        )
        await source.pay_invoice(FAKE_BOLT11_REGTEST, max_fee_msat=500)
        assert client.calls[-1]["max_fee_msat"] == 500

    async def test_payment_failure_propagates(self) -> None:
        client = FakeLndClient(should_fail=True)
        source = LightningFundingSource(
            client=client, network="bitcoin-regtest", node_pubkey=FAKE_PUBKEY
        )
        with pytest.raises(InvoicePaymentError):
            await source.pay_invoice(FAKE_BOLT11_REGTEST)


# ---------------------------------------------------------------------------
# Tests: LightningNodeClient Protocol conformance
# ---------------------------------------------------------------------------


class TestLightningNodeClientProtocol:
    def test_fake_client_satisfies_protocol(self) -> None:
        client = FakeLndClient()
        assert isinstance(client, LightningNodeClient)


# ---------------------------------------------------------------------------
# Secret field repr exclusion
# ---------------------------------------------------------------------------


def test_lnd_client_credentials_excluded_from_repr() -> None:
    """macaroon_hex and tls_cert_pem must not appear in repr() to prevent secret leakage."""
    client = LndClient(
        grpc_host="localhost",
        macaroon_hex="deadbeef01234567",
        tls_cert_pem="-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----",
    )
    r = repr(client)
    assert "deadbeef01234567" not in r
    assert "FAKE" not in r
    # Non-sensitive field should still be visible.
    assert "localhost" in r
