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
    """Machine-readable error detail when a payment flow fails before settlement."""

    code: str  # e.g. "policy_denied", "budget_exceeded", "no_feasible_rail"
    message: str  # human-readable description


class Outcome(RouteweilerModel):
    """HTTP outcome of a single ``Routeweiler`` call."""

    http_status: int | None  # None for pre-response errors (e.g. network timeout)
    service_delivered: bool  # True when the caller received a 2xx response
    service_latency_ms: int  # wall-clock ms from first send to final response
    error: OutcomeError | None = None  # populated when service_delivered is False


class Reconciliation(RouteweilerModel):
    """Optional metadata useful for tax reporting and invoice matching."""

    tax_category: TaxCategory | None = None  # classify the spend for VAT/GST
    vat_applicable: bool  # True when the merchant is a VAT-registered EU seller
    invoice_reference: str | None = None  # merchant invoice or receipt id


# ---------------------------------------------------------------------------
# TraceEvent
# ---------------------------------------------------------------------------


class TraceEvent(RouteweilerModel):
    """One structured audit record per ``Routeweiler`` HTTP call.

    Emitted by ``TraceEmitter`` and persisted by ``SqliteTraceSink`` to the
    ``trace_events`` table.  Every call — paid, free, or failed — produces
    exactly one ``TraceEvent``.

    ``payment`` is ``None`` when the call did not reach the payment step (e.g.
    the request succeeded without a 402, or the budget draw was rejected before
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
    # Credential lifecycle fields — populated only by emit_credential_manual_hold.
    credential_id: str | None = None
    credential_state: CredentialState | None = None
