"""Unit tests for DrawReceipt issuance, verification, and canonical payload."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from routewiler.budgets.keystore import EnvelopeKeystore
from routewiler.budgets.receipts import canonical_payload, issue, uuid7, verify
from routewiler.budgets.schema import DrawReceipt
from routewiler.errors import ReceiptVerificationError

NOW = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
EXPIRES = NOW + timedelta(seconds=150)


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    return key, pub_b64


def _make_receipt(keypair: tuple[Ed25519PrivateKey, str], **overrides) -> DrawReceipt:
    key, pub_b64 = keypair
    kwargs: dict = dict(
        private_key=key,
        public_key_b64=pub_b64,
        receipt_id=uuid7(),
        envelope_id="env_test",
        request_id="req_001",
        idempotency_key="ikey_001",
        amount_reserved_minor_units=100,
        amount_reserved_currency="usd",
        rail_quoted="x402",
        issued_at=NOW,
        expires_at=EXPIRES,
    )
    kwargs.update(overrides)
    return issue(**kwargs)


# ---------------------------------------------------------------------------
# UUIDv7
# ---------------------------------------------------------------------------


def test_uuid7_format() -> None:
    u = uuid7()
    parts = u.split("-")
    assert len(parts) == 5
    assert len(parts[0]) == 8
    assert parts[2][0] == "7"  # version nibble


def test_uuid7_timestamp_prefix_non_decreasing() -> None:
    # Extract the 48-bit timestamp prefix (first 12 hex chars) and verify
    # it is non-decreasing across 50 IDs generated in sequence.
    ids = [uuid7() for _ in range(50)]
    timestamps = [u.replace("-", "")[:12] for u in ids]
    assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Issue + verify
# ---------------------------------------------------------------------------


def test_issue_and_verify_succeeds(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    verify(receipt)  # must not raise


def test_receipt_has_non_empty_signature(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    assert len(receipt.signature) > 0
    base64.b64decode(receipt.signature)  # must be valid base64


def test_tampered_amount_fails_verification(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    tampered = receipt.model_copy(update={"amount_reserved_minor_units": 99999})
    with pytest.raises(ReceiptVerificationError):
        verify(tampered)


def test_tampered_signature_fails_verification(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    bad_sig = base64.b64encode(b"\xff" * 64).decode()
    tampered = receipt.model_copy(update={"signature": bad_sig})
    with pytest.raises(ReceiptVerificationError):
        verify(tampered)


def test_tampered_envelope_id_fails_verification(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    tampered = receipt.model_copy(update={"envelope_id": "evil_env"})
    with pytest.raises(ReceiptVerificationError):
        verify(tampered)


# ---------------------------------------------------------------------------
# Canonical payload stability
# ---------------------------------------------------------------------------


def test_canonical_payload_is_byte_stable(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    p1 = canonical_payload(receipt)
    p2 = canonical_payload(receipt)
    assert p1 == p2


def test_canonical_payload_excludes_signature_and_pubkey(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    payload = json.loads(canonical_payload(receipt))
    assert "signature" not in payload
    assert "counter_public_key" not in payload


def test_canonical_payload_keys_are_sorted(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    raw = canonical_payload(receipt).decode()
    parsed = json.loads(raw)
    assert list(parsed.keys()) == sorted(parsed.keys())


def test_canonical_payload_datetime_format(keypair: tuple) -> None:
    receipt = _make_receipt(keypair)
    payload = json.loads(canonical_payload(receipt))
    assert payload["issued_at"].endswith("Z")
    assert "+" not in payload["issued_at"]


# ---------------------------------------------------------------------------
# Keystore-integrated issuance
# ---------------------------------------------------------------------------


def test_keystore_issue_and_verify(tmp_path: Path) -> None:
    ks = EnvelopeKeystore(root=tmp_path / "keys")
    private_key = ks.create("env_k1")
    pub_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    receipt = issue(
        private_key=private_key,
        public_key_b64=pub_b64,
        receipt_id=uuid7(),
        envelope_id="env_k1",
        request_id="req_k1",
        idempotency_key="ikey_k1",
        amount_reserved_minor_units=500,
        amount_reserved_currency="usd",
        rail_quoted="x402",
        issued_at=NOW,
        expires_at=EXPIRES,
    )
    verify(receipt)
