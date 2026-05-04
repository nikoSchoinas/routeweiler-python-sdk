"""Single source of truth for EVM asset metadata and chain IDs.

Both the x402 rail adapter (address resolution, CAIP-19 formatting) and the FMV
module (stablecoin peg table, decimals) consume these tables.  Adding a new
stablecoin or chain requires only one edit here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AssetMetadata:
    """Metadata for a token available on one or more EVM networks.

    Attributes:
        canonical_name:     Lowercase short name (``"usdc"``, ``"eurc"``).
        symbol:             Display symbol (``"USDC"``, ``"EURC"``).
        decimals:           ERC-20 token decimals (6 for USDC/EURC).
        peg_currency:       ISO-4217 peg currency for stablecoins (``"usd"``,
                            ``"eur"``); ``None`` for non-stablecoins.
        addresses:          ``{network: lowercase_address}`` mapping — one
                            entry per deployed chain.
    """

    canonical_name: str
    symbol: str
    decimals: int
    peg_currency: str | None
    addresses: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Asset definitions — keyed by canonical name.
# ---------------------------------------------------------------------------

_USDC = AssetMetadata(
    canonical_name="usdc",
    symbol="USDC",
    decimals=6,
    peg_currency="usd",
    addresses={
        "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "base-sepolia": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
        "polygon": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    },
)

_EURC = AssetMetadata(
    canonical_name="eurc",
    symbol="EURC",
    decimals=6,
    peg_currency="eur",
    addresses={
        "base": "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42",
    },
)

# All known assets, keyed by canonical name.
ASSETS: dict[str, AssetMetadata] = {
    "usdc": _USDC,
    "eurc": _EURC,
}

# ---------------------------------------------------------------------------
# Derived lookup tables — computed once at import time.
# ---------------------------------------------------------------------------

# (network, canonical_name) → lowercase address.
CANONICAL_ADDRESSES: dict[tuple[str, str], str] = {}
for _name, _meta in ASSETS.items():
    for _net, _addr in _meta.addresses.items():
        CANONICAL_ADDRESSES[(_net, _name)] = _addr.lower()

# Lowercase ERC-20 address → AssetMetadata (reverse lookup).
ASSETS_BY_ADDRESS: dict[str, AssetMetadata] = {}
for _meta in ASSETS.values():
    for _addr in _meta.addresses.values():
        ASSETS_BY_ADDRESS[_addr.lower()] = _meta

# EIP-155 chain IDs for EVM networks.
CHAIN_IDS: dict[str, int] = {
    "base": 8453,
    "base-sepolia": 84532,
    "polygon": 137,
    "arbitrum": 42161,
    "world": 480,
    "ethereum": 1,
}
