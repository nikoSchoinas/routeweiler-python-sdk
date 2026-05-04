"""Tests for credentials/schema.py — enum values and Pydantic round-trip."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from routewiler.credentials.schema import CredentialRecord, CredentialState, ManualHoldReason


def _make_record(**overrides: object) -> CredentialRecord:
    now = datetime.now(UTC)
    defaults: dict[str, object] = {
        "credential_id": "abc123",
        "request_id": "req456",
        "rail": "l402",
        "challenge_url": "http://vendor.com/resource",
        "payload": {"macaroon": "base64==", "preimage_hex": "deadbeef"},
        "state": CredentialState.PERSISTED,
        "persisted_at": now,
        "last_transition_at": now,
    }
    defaults.update(overrides)
    return CredentialRecord(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


def test_credential_state_values() -> None:
    assert CredentialState.PERSISTED.value == "persisted"
    assert CredentialState.RECOVERING.value == "recovering"
    assert CredentialState.REDEEMED.value == "redeemed"
    assert CredentialState.MANUAL_HOLD.value == "manual_hold"


def test_manual_hold_reason_values() -> None:
    assert ManualHoldReason.EXHAUSTED.value == "exhausted"
    assert ManualHoldReason.EXPIRED.value == "expired"


def test_credential_state_is_str() -> None:
    assert isinstance(CredentialState.PERSISTED, str)
    assert CredentialState.PERSISTED == "persisted"


# ---------------------------------------------------------------------------
# CredentialRecord Pydantic round-trip
# ---------------------------------------------------------------------------


def test_record_camel_case_json_round_trip() -> None:
    record = _make_record()
    json_str = record.model_dump_json(by_alias=True)
    data = json.loads(json_str)
    assert "credentialId" in data
    assert "requestId" in data
    assert "challengeUrl" in data
    assert "persistedAt" in data
    assert "lastTransitionAt" in data
    assert data["state"] == "persisted"


def test_record_optional_fields_default_to_none() -> None:
    record = _make_record()
    assert record.manual_hold_reason is None
    assert record.redeemed_at is None
    assert record.expires_at is None


def test_record_with_manual_hold_reason() -> None:
    record = _make_record(
        state=CredentialState.MANUAL_HOLD,
        manual_hold_reason=ManualHoldReason.EXHAUSTED,
    )
    assert record.manual_hold_reason == ManualHoldReason.EXHAUSTED
    assert record.manual_hold_reason == "exhausted"


def test_record_extra_fields_forbidden() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        CredentialRecord(
            credential_id="x",
            request_id="y",
            rail="l402",
            challenge_url="http://x.com",
            payload={},
            state=CredentialState.PERSISTED,
            persisted_at=now,
            last_transition_at=now,
            unknown_field="oops",  # type: ignore[call-arg]
        )


def test_record_payload_survives_round_trip() -> None:
    payload = {"macaroon": "mac_b64==", "preimage_hex": "abcd1234", "invoice": "lnbcrt..."}
    record = _make_record(payload=payload)
    dumped = json.loads(record.model_dump_json(by_alias=True))
    assert dumped["payload"] == payload
