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

# ---------------------------------------------------------------------------
# Tempo TIP-20 tokens
# TIP-20 uses 6 decimal places (same as ERC-20 USDC).
# Chain ID 42431 = Tempo Moderato testnet (public, no native gas token).
# Mainnet addresses are forward-compat entries; not exercised in W13 tests.
# ---------------------------------------------------------------------------

_PATHUSD = AssetMetadata(
    canonical_name="pathusd",
    symbol="PathUSD",
    decimals=6,
    peg_currency="usd",
    addresses={
        # Moderato testnet — from Stripe docs (crypto_display_details example).
        "tempo-moderato": "0x20c0000000000000000000000000000000000000",
    },
)

_TEMPO_USDC = AssetMetadata(
    canonical_name="usdc",
    symbol="USDC",
    decimals=6,
    peg_currency="usd",
    addresses={
        # Tempo mainnet — from Stripe docs (crypto_display_details example).
        # NOT exercised in W13 tests; Moderato testnet uses PathUSD.
        "tempo": "0x20c000000000000000000000b9537d11c60e8b50",
    },
)

# All known assets, keyed by (network, canonical_name) so Tempo USDC and EVM
# USDC can coexist. The top-level ASSETS dict by canonical_name is kept for
# backward compatibility with code that doesn't care about the network.
ASSETS: dict[str, AssetMetadata] = {
    "usdc": _USDC,
    "eurc": _EURC,
    "pathusd": _PATHUSD,
}

# Tempo-specific assets keyed by (network, canonical_name).
# Allows address lookup for Tempo TIP-20 tokens without colliding with EVM.
TEMPO_ASSETS: dict[tuple[str, str], AssetMetadata] = {
    ("tempo-moderato", "pathusd"): _PATHUSD,
    ("tempo", "usdc"): _TEMPO_USDC,
}

# ---------------------------------------------------------------------------
# Derived lookup tables — computed once at import time.
# ---------------------------------------------------------------------------

# (network, canonical_name) → lowercase address.
CANONICAL_ADDRESSES: dict[tuple[str, str], str] = {}
for _name, _meta in ASSETS.items():
    for _net, _addr in _meta.addresses.items():
        CANONICAL_ADDRESSES[(_net, _name)] = _addr.lower()
# Also include Tempo-specific assets not already in ASSETS.
for (_tnet, _tname), _tmeta in TEMPO_ASSETS.items():
    for _taddr in _tmeta.addresses.values():
        CANONICAL_ADDRESSES[(_tnet, _tname)] = _taddr.lower()

# Lowercase address → AssetMetadata (reverse lookup — covers EVM + Tempo).
ASSETS_BY_ADDRESS: dict[str, AssetMetadata] = {}
for _meta in ASSETS.values():
    for _addr in _meta.addresses.values():
        ASSETS_BY_ADDRESS[_addr.lower()] = _meta
for _tmeta in TEMPO_ASSETS.values():
    for _taddr in _tmeta.addresses.values():
        ASSETS_BY_ADDRESS[_taddr.lower()] = _tmeta

# EIP-155 / network chain IDs.
CHAIN_IDS: dict[str, int] = {
    "base": 8453,
    "base-sepolia": 84532,
    "polygon": 137,
    "arbitrum": 42161,
    "world": 480,
    "ethereum": 1,
    # Tempo networks
    "tempo": 42430,  # mainnet (reserved; not yet exercised in tests)
    "tempo-moderato": 42431,  # Moderato testnet
}
