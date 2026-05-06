"""EVM funding source — wraps an eth_account signer for x402 payments."""

from __future__ import annotations

from dataclasses import dataclass

from eth_account.signers.local import LocalAccount


@dataclass(frozen=True)
class EvmFundingSource:
    """An EVM wallet plus the (network, asset) pair it can pay on.

    `wallet` is an `eth_account.LocalAccount` (from `Account.from_key(...)`).
    Future weeks will widen this to a Protocol covering Turnkey/Privy/Fireblocks.

    `network` and `asset` identify which x402 PaymentRequirements entry this
    source can satisfy. Examples:
        network="base",         asset="usdc"
        network="base-sepolia", asset="usdc"

    `asset` may be a canonical name ("usdc", "eurc") or a lowercase ERC-20
    address. The x402 adapter resolves canonical names to on-chain addresses.
    """

    wallet: LocalAccount
    network: str
    asset: str
