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
    """The HTTP resource that triggered the 402 challenge."""

    method: str  # HTTP method of the original request (e.g. "GET", "POST")
    url: str  # full URL of the protected resource
    url_encoding: UrlEncoding  # how the URL is stored in traces: "raw" or "drop"
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
    """The payment recipient extracted from the 402 challenge."""

    identifier: str  # EVM address, Lightning pubkey, Stripe connected account id, etc.
    metadata: dict[str, Any] = Field(default_factory=dict)  # rail-specific extra data


# x402-specific payment requirements — mirrors the wire format exactly.
# Uses RouteweilerLooseModel so future x402 spec additions don't break parsing.
class X402PaymentRequirements(RouteweilerLooseModel):
    """One payment option from the x402 v2 server's `accepts` array."""

    scheme: Literal["exact", "upto", "stream"]
    network: str  # "base" | "base-sepolia" | "polygon" | "arbitrum" | "world" | "solana"
    amount: str  # payment amount in base units (x402 v2 wire field)
    description: str = ""
    mime_type: str = "application/json"
    pay_to: str  # recipient address
    max_timeout_seconds: int = 60
    asset: str  # ERC-20 address or canonical name ("usdc", "eurc")
    output_schema: dict[str, Any] | None = None
    # EVM extension data: nonce, validBefore, validAfter, token name/version
    extra: dict[str, Any] = Field(default_factory=dict)


class X402RailRaw(RouteweilerModel):
    """Parsed x402 wire data retained on ``NormalizedChallenge.raw``."""

    kind: Literal["x402"]
    # Full list of payment options from the wire's `accepts` array.
    # The chosen alternative is captured in NormalizedChallenge.price at parse time.
    accepts: list[X402PaymentRequirements]
    x402_version: Literal[2] = 2  # round-tripped from the wire's x402Version field


class L402RailRaw(RouteweilerModel):
    """Parsed L402 wire data retained on ``NormalizedChallenge.raw``."""

    kind: Literal["l402"]
    macaroon: str  # base64-encoded macaroon from the WWW-Authenticate header
    invoice: str  # BOLT-11 payment request string


class MppTempoRailRaw(RouteweilerModel):
    """Parsed MPP-Tempo wire data retained on ``NormalizedChallenge.raw``."""

    kind: Literal["mpp-tempo"]
    charge_id: str  # unique charge identifier from the Tempo server
    auth_params: dict[str, str] = Field(default_factory=dict)  # WWW-Authenticate auth-params
    extra: dict[str, Any] = Field(default_factory=dict)  # forward-compat extra fields


class MppSptRailRaw(RouteweilerModel):
    """Parsed MPP-SPT wire data retained on ``NormalizedChallenge.raw``."""

    kind: Literal["mpp-spt"]
    seller_details: dict[str, Any]  # Stripe seller_details dict from the 402 challenge
    payment_method_hint: str | None = None  # optional payment method type hint
    auth_params: dict[str, str] = Field(default_factory=dict)  # WWW-Authenticate auth-params
    extra: dict[str, Any] = Field(default_factory=dict)  # forward-compat extra fields


RailRaw = Annotated[
    X402RailRaw | L402RailRaw | MppTempoRailRaw | MppSptRailRaw,
    Field(discriminator="kind"),
]


class NormalizedChallenge(RouteweilerModel):
    """Rail-agnostic representation of a 402 Payment Required challenge.

    Every rail adapter parses its wire format into this shape before the
    routing engine and budget counter see it.  The ``rail`` field acts as a
    discriminator: ``raw`` is the corresponding ``*RailRaw`` subtype
    (``X402RailRaw``, ``L402RailRaw``, ``MppTempoRailRaw``, or ``MppSptRailRaw``).
    """

    rail: Rail
    resource: Resource
    price: Price
    payee: Payee
    scheme: Scheme
    nonce: str
    expires_at: datetime
    raw: RailRaw
