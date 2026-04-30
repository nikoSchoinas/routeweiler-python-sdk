"""Funding sources — builders for rail-specific payment credentials."""

from __future__ import annotations

from eth_account.signers.local import LocalAccount

from routewiler.funding.evm import EvmFundingSource

__all__ = ["EvmFundingSource", "Funding"]


class Funding:
    """Factory for funding sources passed to ``Routewiler(funding=[...])``.

    Each static method returns a concrete funding source.  Additional rail
    funding types (Lightning, Stripe) are added in later months.
    """

    @staticmethod
    def base_usdc(*, wallet: LocalAccount) -> EvmFundingSource:
        """USDC on Base mainnet (chain ID 8453)."""
        return EvmFundingSource(wallet=wallet, network="base", asset="usdc")

    @staticmethod
    def base_sepolia_usdc(*, wallet: LocalAccount) -> EvmFundingSource:
        """USDC on Base Sepolia testnet (chain ID 84532)."""
        return EvmFundingSource(wallet=wallet, network="base-sepolia", asset="usdc")
