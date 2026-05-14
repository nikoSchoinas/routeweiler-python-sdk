"""Tests for BudgetEnvelopeRecord and DrawReceipt."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from routeweiler.budgets.schema import BudgetEnvelopeRecord, DrawReceipt

NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 4, 27, 23, 59, 59, tzinfo=UTC)


def _envelope_data(**overrides) -> dict:
    base: dict = {
        "id": "env_01HW",
        "capMinorUnits": 500_00,  # $500.00 in cents
        "capCurrency": "usd",
        "allowedRails": ["x402", "l402"],
        "allowedOriginsGlob": ["*.vendor.com"],
        "createdAt": NOW.isoformat(),
        "expiresAt": EXPIRES.isoformat(),
    }
    base.update(overrides)
    return base


def _receipt_data(**overrides) -> dict:
    base: dict = {
        "receiptId": "019184b6-7f00-7000-8000-000000000001",
        "envelopeId": "env_01HW",
        "requestId": "req_abc",
        "idempotencyKey": "idem_xyz",
        "amountReservedMinorUnits": 100,
        "amountReservedCurrency": "usd",
        "railQuoted": "x402",
        "issuedAt": NOW.isoformat(),
        "expiresAt": EXPIRES.isoformat(),
        "counterPublicKey": "base64pubkey==",
        "signature": "base64sig==",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# BudgetEnvelopeRecord
# ---------------------------------------------------------------------------


def test_envelope_defaults():
    env = BudgetEnvelopeRecord.model_validate(_envelope_data())
    assert env.status == "active"
    assert env.owner_agent_id is None


def test_envelope_snake_case_construction():
    env = BudgetEnvelopeRecord(
        id="env_01HW",
        cap_minor_units=10_000,
        cap_currency="eur",
        allowed_rails=["l402"],
        allowed_origins_glob=["api.example.org"],
        created_at=NOW,
        expires_at=EXPIRES,
    )
    assert env.cap_currency == "eur"
    assert env.allowed_rails == ["l402"]


def test_envelope_camel_roundtrip():
    env = BudgetEnvelopeRecord.model_validate(_envelope_data())
    dumped = env.model_dump(by_alias=True)
    assert dumped["capMinorUnits"] == 500_00
    assert dumped["capCurrency"] == "usd"
    assert dumped["allowedRails"] == ["x402", "l402"]
    assert dumped["status"] == "active"


def test_envelope_with_owner_agent():
    env = BudgetEnvelopeRecord.model_validate(_envelope_data(ownerAgentId="erc8004:1:42"))
    assert env.owner_agent_id == "erc8004:1:42"


def test_envelope_invalid_currency():
    with pytest.raises(ValidationError):
        BudgetEnvelopeRecord.model_validate(_envelope_data(capCurrency="chf"))


def test_envelope_invalid_status():
    with pytest.raises(ValidationError):
        BudgetEnvelopeRecord.model_validate(_envelope_data(status="paused"))


def test_envelope_extra_field_forbidden():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BudgetEnvelopeRecord.model_validate(_envelope_data(hackField="bad"))


def test_envelope_frozen_status():
    env = BudgetEnvelopeRecord.model_validate(_envelope_data(status="frozen"))
    assert env.status == "frozen"


# ---------------------------------------------------------------------------
# DrawReceipt
# ---------------------------------------------------------------------------


def test_receipt_construction():
    r = DrawReceipt.model_validate(_receipt_data())
    assert r.receipt_id == "019184b6-7f00-7000-8000-000000000001"
    assert r.amount_reserved_minor_units == 100
    assert r.rail_quoted == "x402"


def test_receipt_camel_roundtrip():
    r = DrawReceipt.model_validate(_receipt_data())
    dumped = r.model_dump(by_alias=True)
    assert dumped["receiptId"] == "019184b6-7f00-7000-8000-000000000001"
    assert dumped["railQuoted"] == "x402"
    assert dumped["counterPublicKey"] == "base64pubkey=="
    assert dumped["signature"] == "base64sig=="


def test_receipt_missing_signature():
    data = _receipt_data()
    del data["signature"]
    with pytest.raises(ValidationError):
        DrawReceipt.model_validate(data)


def test_receipt_missing_counter_public_key():
    data = _receipt_data()
    del data["counterPublicKey"]
    with pytest.raises(ValidationError):
        DrawReceipt.model_validate(data)


def test_receipt_invalid_currency():
    with pytest.raises(ValidationError):
        DrawReceipt.model_validate(_receipt_data(amountReservedCurrency="btc"))


def test_receipt_invalid_rail():
    with pytest.raises(ValidationError):
        DrawReceipt.model_validate(_receipt_data(railQuoted="eth"))


def test_receipt_extra_field_forbidden():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DrawReceipt.model_validate(_receipt_data(surprise="value"))
