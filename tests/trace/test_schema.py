"""Tests for TraceEvent and its nested types."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from routewiler.normalized import NormalizedChallenge, X402RailRaw
from routewiler.trace.schema import (
    FmvQuality,
    Outcome,
    OutcomeError,
    PaymentDetails,
    Reconciliation,
    TraceEvent,
)

NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 4, 27, 12, 5, 0, tzinfo=UTC)


def _challenge() -> NormalizedChallenge:
    return NormalizedChallenge(
        rail="x402",
        resource={"method": "GET", "url": "https://x.com", "url_encoding": "raw"},
        price={
            "amount": 1_000_000,
            "currency": "eip155:8453/erc20:0x...",
            "human_amount": "0.01 USDC",
        },
        payee={"identifier": "0xAbCd"},
        scheme="exact",
        nonce="n1",
        expires_at=EXPIRES,
        raw=X402RailRaw(kind="x402", accepts=[]),
    )


def _payment() -> PaymentDetails:
    return PaymentDetails(
        proof_type="txid",
        proof_value="0xdeadbeef",
        amount_native=1_000_000,
        amount_native_currency="eip155:8453/erc20:0x...",
        amount_envelope=0.01,
        amount_envelope_currency="usd",
        fmv_quality="stablecoin_peg",
        settlement_latency_ms=320,
    )


def _outcome(error: OutcomeError | None = None) -> Outcome:
    return Outcome(
        http_status=200,
        service_delivered=True,
        service_latency_ms=450,
        error=error,
    )


def _reconciliation() -> Reconciliation:
    return Reconciliation(vat_applicable=False)


def _event(**overrides) -> TraceEvent:
    defaults = dict(
        request_id="req_abc",
        envelope_id="env_01HW",
        policy_hash="sha256:abcd1234",
        challenge=_challenge(),
        selected_rail="x402",
        funding_source="evm:local",
        payment=_payment(),
        outcome=_outcome(),
        reconciliation=_reconciliation(),
        timestamp_start=NOW,
        timestamp_end=NOW,
    )
    defaults.update(overrides)
    return TraceEvent(**defaults)


# ---------------------------------------------------------------------------
# Construction and schema_version default
# ---------------------------------------------------------------------------


def test_schema_version_default():
    ev = _event()
    assert ev.schema_version == "1.0"


def test_optional_fields_default_none():
    ev = _event()
    assert ev.parent_request_id is None
    assert ev.agent_id is None
    assert ev.facilitator is None


def test_payment_none_accepted():
    ev = _event(payment=None)
    assert ev.payment is None


def test_outcome_error_none():
    outcome = _outcome(error=None)
    assert outcome.error is None


def test_outcome_error_populated():
    err = OutcomeError(code="RAIL_TIMEOUT", message="x402 settlement timed out")
    outcome = _outcome(error=err)
    assert outcome.error is not None
    assert outcome.error.code == "RAIL_TIMEOUT"


# ---------------------------------------------------------------------------
# camelCase round-trip
# ---------------------------------------------------------------------------


def test_camel_roundtrip():
    ev = _event()
    dumped = ev.model_dump(by_alias=True)
    assert dumped["requestId"] == "req_abc"
    assert dumped["envelopeId"] == "env_01HW"
    assert dumped["selectedRail"] == "x402"
    assert dumped["schemaVersion"] == "1.0"
    assert dumped["payment"]["proofType"] == "txid"
    assert dumped["outcome"]["httpStatus"] == 200
    assert dumped["reconciliation"]["vatApplicable"] is False


def test_nested_challenge_camel():
    ev = _event()
    dumped = ev.model_dump(by_alias=True)
    assert dumped["challenge"]["rail"] == "x402"
    assert dumped["challenge"]["price"]["humanAmount"] == "0.01 USDC"


# ---------------------------------------------------------------------------
# FMV outage — amount_envelope may be None
# ---------------------------------------------------------------------------


def test_payment_amount_envelope_null():
    payment = PaymentDetails(
        proof_type="preimage",
        proof_value="abc123",
        amount_native=50_000,
        amount_native_currency="btc-lightning",
        amount_envelope=None,
        amount_envelope_currency="usd",
        fmv_quality="unavailable",
        settlement_latency_ms=1200,
    )
    ev = _event(payment=payment)
    dumped = ev.model_dump(by_alias=True)
    assert dumped["payment"]["amountEnvelope"] is None
    assert dumped["payment"]["fmvQuality"] == "unavailable"


# ---------------------------------------------------------------------------
# Reconciliation optional fields
# ---------------------------------------------------------------------------


def test_reconciliation_with_tax_category():
    rec = Reconciliation(tax_category="inference", vat_applicable=True)
    ev = _event(reconciliation=rec)
    assert ev.reconciliation.tax_category == "inference"


def test_reconciliation_invalid_tax_category():
    with pytest.raises(ValidationError):
        Reconciliation(tax_category="groceries", vat_applicable=False)


# ---------------------------------------------------------------------------
# Extra fields forbidden
# ---------------------------------------------------------------------------


def test_trace_event_extra_forbidden():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TraceEvent(
            request_id="req_abc",
            envelope_id="env_01HW",
            policy_hash="sha256:abcd1234",
            challenge=_challenge(),
            selected_rail="x402",
            funding_source="evm:local",
            payment=None,
            outcome=_outcome(),
            reconciliation=_reconciliation(),
            timestamp_start=NOW,
            timestamp_end=NOW,
            unknown_field="oops",
        )


def test_payment_details_extra_forbidden():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PaymentDetails(
            proof_type="txid",
            proof_value="0x",
            amount_native=100,
            amount_native_currency="usd-fiat",
            amount_envelope=1.0,
            amount_envelope_currency="usd",
            fmv_quality="stablecoin_peg",
            settlement_latency_ms=10,
            bad_extra="x",
        )


# ---------------------------------------------------------------------------
# FmvQuality type alias sanity
# ---------------------------------------------------------------------------


def test_all_fmv_quality_values():
    valid: list[FmvQuality] = ["stablecoin_peg", "coingecko_simple", "fx_leg", "unavailable"]
    for q in valid:
        p = PaymentDetails(
            proof_type="txid",
            proof_value="0x",
            amount_native=1,
            amount_native_currency="usd-fiat",
            amount_envelope=0.01,
            amount_envelope_currency="usd",
            fmv_quality=q,
            settlement_latency_ms=1,
        )
        assert p.fmv_quality == q
