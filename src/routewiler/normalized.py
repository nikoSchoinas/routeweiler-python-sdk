"""NormalizedChallenge — the universal shape every rail parses a 402 response into."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from routewiler._base import RoutewilerModel

# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

Rail = Literal["x402", "l402", "mpp-tempo", "mpp-spt"]
Scheme = Literal["exact", "upto", "stream"]
UrlEncoding = Literal["raw", "hash", "drop"]
ProofType = Literal["txid", "preimage", "spt_id"]

# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class Resource(RoutewilerModel):
    method: str
    url: str
    url_encoding: UrlEncoding
    original_status: int = 402


class Price(RoutewilerModel):
    """Payment amount.

    `amount` is in base units (wei, sats, cents). Python int is arbitrary-precision,
    matching TypeScript's bigint.
    """

    amount: int
    currency: str  # CAIP-19, "btc-lightning", or "<iso4217>-fiat"
    human_amount: str  # e.g. "0.01 USDC", "50000 sats"


class Payee(RoutewilerModel):
    identifier: str  # address, Lightning pubkey, Stripe account
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# x402-specific payment requirements — mirrors the wire format exactly.
# One entry per element of the server's `accepts` array.
# extra="ignore" so future x402 spec additions don't break parsing.
# ---------------------------------------------------------------------------


class X402PaymentRequirements(RoutewilerModel):
    """One payment option from the x402 server's `accepts` array."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",  # x402 spec evolves; silently drop unknown fields
        frozen=False,
    )

    scheme: Literal["exact", "upto", "stream"]
    network: str  # "base" | "base-sepolia" | "polygon" | "arbitrum" | "world" | "solana"
    max_amount_required: str  # decimal string in base units (matches wire)
    resource: str  # the URL being protected
    description: str = ""
    mime_type: str = "application/json"
    pay_to: str  # recipient address
    max_timeout_seconds: int = 60
    asset: str  # ERC-20 address or canonical name ("usdc", "eurc")
    output_schema: dict[str, Any] | None = None
    # EVM extension data: nonce, validBefore, validAfter, token name/version
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# RailRaw — discriminated union per rail
# Each variant carries the verbatim rail payload for the adapter.
# ---------------------------------------------------------------------------


class X402RailRaw(RoutewilerModel):
    kind: Literal["x402"]
    # Full list of payment options from the wire's `accepts` array.
    # The chosen alternative is captured in NormalizedChallenge.price at parse time.
    accepts: list[X402PaymentRequirements]
    facilitator_hint: str | None = None
    x402_version: int = 1  # round-tripped from the wire's x402Version field


class L402RailRaw(RoutewilerModel):
    kind: Literal["l402"]
    macaroon: str
    invoice: str


class MppTempoRailRaw(RoutewilerModel):
    kind: Literal["mpp-tempo"]
    charge_id: str
    settlement_network: Literal["tempo"]
    extra: dict[str, Any] = Field(default_factory=dict)


class MppSptRailRaw(RoutewilerModel):
    kind: Literal["mpp-spt"]
    seller_details: dict[str, Any]
    payment_method_hint: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


RailRaw = Annotated[
    X402RailRaw | L402RailRaw | MppTempoRailRaw | MppSptRailRaw,
    Field(discriminator="kind"),
]

# ---------------------------------------------------------------------------
# NormalizedChallenge
# ---------------------------------------------------------------------------


class NormalizedChallenge(RoutewilerModel):
    """Rail-agnostic representation of a 402 Payment Required challenge.

    Every rail adapter parses its wire format into this shape before the
    routing engine and budget counter see it.
    """

    rail: Rail
    resource: Resource
    price: Price
    payee: Payee
    scheme: Scheme
    nonce: str
    expires_at: datetime
    raw: RailRaw
