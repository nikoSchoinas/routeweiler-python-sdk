"""NormalizedChallenge — the universal shape every rail parses a 402 response into."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from routeweiler._base import RouteweilerLooseModel, RouteweilerModel
from routeweiler._constants import HTTP_STATUS_PAYMENT_REQUIRED

Rail = Literal["x402", "l402", "mpp-tempo", "mpp-spt"]
# Only "exact" is production-ready; "upto"/"stream" are deferred.
Scheme = Literal["exact"]
# "hash" requires the hosted uploader (Phase 2); narrowed away until then.
UrlEncoding = Literal["raw", "drop"]
ProofType = Literal["txid", "preimage", "spt_id"]


class Resource(RouteweilerModel):
    method: str
    url: str
    url_encoding: UrlEncoding
    # Reserved — only 402 today; non-402 challenges may exist post-MVP.
    original_status: int = HTTP_STATUS_PAYMENT_REQUIRED


class Price(RouteweilerModel):
    """Payment amount.

    `amount` is in base units (wei, sats, cents). Python int is arbitrary-precision,
    matching TypeScript's bigint.
    """

    amount: int
    currency: str  # CAIP-19, "btc-lightning", or "<iso4217>-fiat"
    human_amount: str  # e.g. "0.01 USDC", "50000 sats"


class Payee(RouteweilerModel):
    identifier: str  # address, Lightning pubkey, Stripe account
    metadata: dict[str, Any] = Field(default_factory=dict)


# x402-specific payment requirements — mirrors the wire format exactly.
# Uses RouteweilerLooseModel so future x402 spec additions don't break parsing.
class X402PaymentRequirements(RouteweilerLooseModel):
    """One payment option from the x402 server's `accepts` array."""

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


class X402RailRaw(RouteweilerModel):
    kind: Literal["x402"]
    # Full list of payment options from the wire's `accepts` array.
    # The chosen alternative is captured in NormalizedChallenge.price at parse time.
    accepts: list[X402PaymentRequirements]
    x402_version: int = 1  # round-tripped from the wire's x402Version field


class L402RailRaw(RouteweilerModel):
    kind: Literal["l402"]
    macaroon: str
    invoice: str


class MppTempoRailRaw(RouteweilerModel):
    kind: Literal["mpp-tempo"]
    charge_id: str
    auth_params: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


class MppSptRailRaw(RouteweilerModel):
    kind: Literal["mpp-spt"]
    seller_details: dict[str, Any]
    payment_method_hint: str | None = None
    auth_params: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


RailRaw = Annotated[
    X402RailRaw | L402RailRaw | MppTempoRailRaw | MppSptRailRaw,
    Field(discriminator="kind"),
]


class NormalizedChallenge(RouteweilerModel):
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
