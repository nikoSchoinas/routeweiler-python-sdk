"""TraceEvent and its nested models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from routeweiler._base import RouteweilerModel
from routeweiler.budgets.schema import EnvelopeCurrency
from routeweiler.credentials.schema import CredentialState
from routeweiler.normalized import NormalizedChallenge, ProofType, Rail

# Re-export ProofType so existing importers of this module are unaffected.
__all__ = ["ProofType"]

# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

FmvQuality = Literal["stablecoin_peg", "coingecko_simple", "fx_leg", "unavailable"]
TaxCategory = Literal["data_api", "inference", "compute", "other"]

# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class PaymentDetails(RouteweilerModel):
    """Payment proof and amounts, emitted after settlement."""

    proof_type: ProofType
    proof_value: str | None  # None when facilitator omits the on-chain proof (mock/testnet)
    amount_native: int  # rail-native base units (wei, sats, cents)
    amount_native_currency: str  # CAIP-19 / "btc-lightning" / "<iso4217>-fiat"
    amount_envelope: float | None  # None when FMV is unavailable or no envelope
    amount_envelope_currency: EnvelopeCurrency | None  # None when no envelope is configured
    fmv_quality: FmvQuality
    settlement_latency_ms: int


class OutcomeError(RouteweilerModel):
    code: str
    message: str


class Outcome(RouteweilerModel):
    http_status: int | None
    service_delivered: bool
    service_latency_ms: int
    error: OutcomeError | None = None


class Reconciliation(RouteweilerModel):
    tax_category: TaxCategory | None = None
    vat_applicable: bool
    invoice_reference: str | None = None


# ---------------------------------------------------------------------------
# TraceEvent
# ---------------------------------------------------------------------------


class TraceEvent(RouteweilerModel):
    """One structured event per `routeweiler.get/post/...` call.

    `payment` is None when the call did not reach the payment step (e.g. the
    request succeeded without a 402, or the budget draw was rejected before
    payment was attempted).
    """

    request_id: str
    parent_request_id: str | None = None  # sub-agent tree linkage
    agent_id: str | None = None
    envelope_id: str | None  # None when no budget envelope is configured
    policy_hash: str
    challenge: NormalizedChallenge | None  # None for passthrough and pre-parse error traces
    selected_rail: Rail | None  # None for passthrough and pre-rail-selection error traces
    fallback_from: Rail | None = None  # set when this attempt followed a rail failure
    facilitator: str | None = None
    funding_source: str | None
    payment: PaymentDetails | None
    outcome: Outcome
    reconciliation: Reconciliation
    timestamp_start: datetime
    timestamp_end: datetime
    schema_version: Literal["1.0"] = "1.0"
    # Credential lifecycle fields — populated only by emit_credential_manual_hold.
    credential_id: str | None = None
    credential_state: CredentialState | None = None
