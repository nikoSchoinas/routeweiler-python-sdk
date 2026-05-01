"""TraceEvent and its nested models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from routewiler._base import RoutewilerModel
from routewiler.normalized import NormalizedChallenge, Rail

# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

ProofType = Literal["txid", "preimage", "spt_id"]
FmvQuality = Literal["stablecoin_peg", "coingecko_simple", "fx_leg", "unavailable"]
TaxCategory = Literal["data_api", "inference", "compute", "other"]

# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class PaymentDetails(RoutewilerModel):
    """Payment proof and amounts, emitted after settlement."""

    proof_type: ProofType
    proof_value: str | None  # None when facilitator omits the on-chain proof (mock/testnet)
    amount_native: int  # rail-native base units (wei, sats, cents)
    amount_native_currency: str  # CAIP-19 / "btc-lightning" / "<iso4217>-fiat"
    amount_envelope: float | None  # None when FMV is unavailable
    amount_envelope_currency: str  # mirrors the envelope's cap_currency
    fmv_quality: FmvQuality
    settlement_latency_ms: int


class OutcomeError(RoutewilerModel):
    code: str
    message: str


class Outcome(RoutewilerModel):
    http_status: int
    service_delivered: bool
    service_latency_ms: int
    error: OutcomeError | None = None


class Reconciliation(RoutewilerModel):
    tax_category: TaxCategory | None = None
    vat_applicable: bool
    invoice_reference: str | None = None


# ---------------------------------------------------------------------------
# TraceEvent
# ---------------------------------------------------------------------------


class TraceEvent(RoutewilerModel):
    """One structured event per `routewiler.get/post/...` call.

    `payment` is None when the call did not reach the payment step (e.g. the
    request succeeded without a 402, or the budget draw was rejected before
    payment was attempted).
    """

    request_id: str
    parent_request_id: str | None = None  # sub-agent tree linkage
    agent_id: str | None = None
    envelope_id: str
    policy_hash: str
    challenge: NormalizedChallenge | None  # None for passthrough and pre-parse error traces
    selected_rail: Rail
    facilitator: str | None = None
    funding_source: str
    payment: PaymentDetails | None
    outcome: Outcome
    reconciliation: Reconciliation
    timestamp_start: datetime
    timestamp_end: datetime
    schema_version: Literal["1.0"] = "1.0"
