"""NormalizedChallenge — the universal shape every rail parses a 402 response into."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from routewiler._base import RoutewilerModel

# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

Rail = Literal["x402", "l402", "mpp-tempo", "mpp-spt"]
Scheme = Literal["exact", "upto", "stream"]
UrlEncoding = Literal["raw", "hashed", "dropped"]

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
# RailRaw — discriminated union per rail
# Each variant carries the verbatim rail payload for the adapter.
# Adapter-side fields are typed in the rail-adapter weeks; for now the
# payload is an open dict so the models can be constructed from raw 402 data.
# ---------------------------------------------------------------------------


class X402RailRaw(RoutewilerModel):
    kind: Literal["x402"]
    payment_requirements: dict[str, Any]
    facilitator_hint: str | None = None


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
