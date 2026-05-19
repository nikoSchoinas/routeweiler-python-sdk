"""EVM funding source — wraps an eth_account signer for x402 payments."""

from __future__ import annotations

from dataclasses import dataclass

from eth_account.signers.local import LocalAccount


@dataclass(frozen=True)
class EvmFundingSource:
    """An EVM wallet plus the (network, asset) pair it can pay on.

    Use the ``Funding`` factory methods rather than constructing directly::

        from routeweiler import Funding
        source = Funding.base_usdc(wallet=signer)          # Base mainnet USDC
        source = Funding.base_sepolia_usdc(wallet=signer)  # Base Sepolia testnet

    Attributes:
        wallet:  An ``eth_account.LocalAccount`` (from ``Account.from_key(...)``).
                 Signs EIP-3009 ``transferWithAuthorization`` messages in-process.
        network: x402 network identifier (e.g. ``"base"``, ``"base-sepolia"``).
                 Must match one of the ``network`` values in the server's ``accepts`` array.
        asset:   Canonical token name (``"usdc"``, ``"eurc"``) or lowercase ERC-20
                 address.  The x402 adapter resolves canonical names to on-chain addresses.
    """

    wallet: LocalAccount
    network: str
    asset: str
