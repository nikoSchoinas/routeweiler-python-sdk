"""DrawReceipt issuance and Ed25519 signature verification.

Signing key management lives in ``budgets/keystore.py``.
The Pydantic wire/storage model lives in ``budgets/schema.py``.

Canonical payload
-----------------
The signed payload is a compact JSON object (``sort_keys=True``,
``separators=(",",":")``).  It includes every ``DrawReceipt`` field **except**
``signature`` and ``counter_public_key`` — the public key is the verifying
material and is therefore excluded from the signed bytes.  Datetimes are
serialised as RFC 3339 UTC strings (``2026-01-02T15:04:05Z``).
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from routewiler.budgets.schema import DrawReceipt, EnvelopeCurrency
from routewiler.errors import ReceiptVerificationError
from routewiler.normalized import Rail

# ---------------------------------------------------------------------------
# UUIDv7 — inline implementation per RFC 9562 §5.7
# ---------------------------------------------------------------------------
# Bit layout (128 bits):
#   [0:48]   unix_ts_ms  (48 bits)
#   [48:52]  version     (0b0111 = 7)
#   [52:64]  rand_a      (12 bits random)
#   [64:66]  variant     (0b10)
#   [66:128] rand_b      (62 bits random)


def uuid7() -> str:
    """Return a new UUIDv7 string (time-ordered, RFC 9562)."""
    ts_ms = int(time.time() * 1000) & 0xFFFF_FFFF_FFFF  # 48-bit ms timestamp

    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    rand_a = rand >> 68  # top 12 bits
    rand_b = rand & 0x3FFF_FFFF_FFFF_FFFF  # bottom 62 bits

    hi = (ts_ms << 16) | (0x7 << 12) | rand_a
    lo = (0b10 << 62) | rand_b

    raw = (hi << 64) | lo
    hex_str = f"{raw:032x}"
    return f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:]}"


# ---------------------------------------------------------------------------
# Canonical payload serialisation
# ---------------------------------------------------------------------------


def _dt_to_rfc3339(dt: datetime) -> str:
    """Convert a UTC datetime to an RFC 3339 string ending in 'Z'."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_payload(receipt: DrawReceipt) -> bytes:
    """Return the deterministic bytes that are signed / verified.

    Excludes ``signature`` and ``counter_public_key`` (the verifying material).
    """
    payload: dict[str, Any] = {
        "amount_reserved_currency": receipt.amount_reserved_currency,
        "amount_reserved_minor_units": receipt.amount_reserved_minor_units,
        "envelope_id": receipt.envelope_id,
        "expires_at": _dt_to_rfc3339(receipt.expires_at),
        "idempotency_key": receipt.idempotency_key,
        "issued_at": _dt_to_rfc3339(receipt.issued_at),
        "rail_quoted": receipt.rail_quoted,
        "receipt_id": receipt.receipt_id,
        "request_id": receipt.request_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# Issue and verify
# ---------------------------------------------------------------------------


def issue(
    *,
    private_key: Ed25519PrivateKey,
    public_key_b64: str,
    receipt_id: str,
    envelope_id: str,
    request_id: str,
    idempotency_key: str,
    amount_reserved_minor_units: int,
    amount_reserved_currency: EnvelopeCurrency,
    rail_quoted: Rail,
    issued_at: datetime,
    expires_at: datetime,
) -> DrawReceipt:
    """Build and sign a new DrawReceipt."""
    # Build an unsigned receipt first so canonical_payload can serialise it.
    unsigned = DrawReceipt(
        receipt_id=receipt_id,
        envelope_id=envelope_id,
        request_id=request_id,
        idempotency_key=idempotency_key,
        amount_reserved_minor_units=amount_reserved_minor_units,
        amount_reserved_currency=amount_reserved_currency,
        rail_quoted=rail_quoted,
        issued_at=issued_at,
        expires_at=expires_at,
        counter_public_key=public_key_b64,
        signature="",  # placeholder; not included in signed payload
    )
    sig_bytes = private_key.sign(canonical_payload(unsigned))
    sig_b64 = base64.b64encode(sig_bytes).decode()
    return unsigned.model_copy(update={"signature": sig_b64})


def verify(receipt: DrawReceipt) -> None:
    """Verify the Ed25519 signature on a DrawReceipt.

    Raises:
        ReceiptVerificationError: Signature is invalid or payload was tampered with.
    """
    try:
        pub_bytes = base64.b64decode(receipt.counter_public_key)
        public_key: Ed25519PublicKey = Ed25519PublicKey.from_public_bytes(pub_bytes)
        sig_bytes = base64.b64decode(receipt.signature)
        public_key.verify(sig_bytes, canonical_payload(receipt))
    except InvalidSignature as exc:
        raise ReceiptVerificationError(
            f"Receipt '{receipt.receipt_id}' has an invalid Ed25519 signature."
        ) from exc
    except Exception as exc:
        raise ReceiptVerificationError(
            f"Receipt '{receipt.receipt_id}' verification failed: {exc}"
        ) from exc
