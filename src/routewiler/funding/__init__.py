"""Funding sources — builders for rail-specific payment credentials."""

from __future__ import annotations

from typing import Literal

from eth_account.signers.local import LocalAccount

from routewiler.funding.evm import EvmFundingSource
from routewiler.funding.lightning import LightningFundingSource, LightningNodeClient, LndClient
from routewiler.funding.tempo import EthAccountTempoSigner, TempoFundingSource, TempoSigner

FundingSource = EvmFundingSource | LightningFundingSource | TempoFundingSource

__all__ = [
    "EthAccountTempoSigner",
    "EvmFundingSource",
    "Funding",
    "FundingSource",
    "LightningFundingSource",
    "LightningNodeClient",
    "LndClient",
    "TempoFundingSource",
    "TempoSigner",
]


class Funding:
    """Factory for funding sources passed to ``Routewiler(funding=[...])``.

    Each static method returns a concrete funding source.
    """

    @staticmethod
    def base_usdc(*, wallet: LocalAccount) -> EvmFundingSource:
        """USDC on Base mainnet (chain ID 8453)."""
        return EvmFundingSource(wallet=wallet, network="base", asset="usdc")

    @staticmethod
    def base_sepolia_usdc(*, wallet: LocalAccount) -> EvmFundingSource:
        """USDC on Base Sepolia testnet (chain ID 84532)."""
        return EvmFundingSource(wallet=wallet, network="base-sepolia", asset="usdc")

    @staticmethod
    def lightning(
        client: LightningNodeClient,
        network: Literal["bitcoin", "bitcoin-testnet", "bitcoin-regtest", "bitcoin-signet"],
        *,
        node_pubkey: str,
        max_fee_msat: int = 1000,
    ) -> LightningFundingSource:
        """Lightning on the specified network.

        Use ``LightningFundingSource.create(client, network)`` when you want
        the node pubkey populated automatically via an async ``getinfo`` call.
        This synchronous factory requires it to be passed explicitly.

        Args:
            client:       A ``LightningNodeClient``-conforming object (e.g. ``LndClient``).
            network:      The Bitcoin network the node operates on.
            node_pubkey:  Hex-encoded 33-byte compressed pubkey of the node.
            max_fee_msat: Per-payment fee cap (default 1000 msat).
        """
        return LightningFundingSource(
            client=client,
            network=network,
            node_pubkey=node_pubkey,
            max_fee_msat=max_fee_msat,
        )

    @staticmethod
    def tempo_pathusd_moderato(*, wallet: LocalAccount) -> TempoFundingSource:
        """PathUSD on Tempo Moderato testnet (chain ID 42431).

        PathUSD is the primary faucet-funded stablecoin on Moderato.
        Use ``Funding.tempo_usdc()`` for Tempo mainnet USDC.
        """
        return TempoFundingSource(
            signer=EthAccountTempoSigner(wallet=wallet, chain_id=42431),
            network="tempo-moderato",
            asset="pathusd",
        )

    @staticmethod
    def tempo_usdc(*, wallet: LocalAccount) -> TempoFundingSource:
        """USDC on Tempo mainnet (chain ID 42430).

        Not exercised in Week 13 tests; use ``tempo_pathusd_moderato`` for
        testnet development.
        """
        return TempoFundingSource(
            signer=EthAccountTempoSigner(wallet=wallet, chain_id=42430),
            network="tempo",
            asset="usdc",
        )
