"""BudgetEnvelope and DrawReceipt Pydantic models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from routewiler._base import RoutewilerModel
from routewiler.normalized import Rail

# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

EnvelopeCurrency = Literal["usd", "eur", "jpy", "gbp"]
EnvelopeStatus = Literal["active", "frozen", "expired", "revoked"]

# ---------------------------------------------------------------------------
# BudgetEnvelope
# ---------------------------------------------------------------------------


class BudgetEnvelope(RoutewilerModel):
    """Per-agent spending envelope.

    Caps are in minor units of `cap_currency` (USD cents, EUR cents, JPY whole yen,
    GBP pence). Budget enforcement always runs locally via SQLite at MVP.
    """

    id: str
    owner_agent_id: str | None = None
    cap_minor_units: int
    cap_currency: EnvelopeCurrency
    allowed_rails: list[Rail]
    allowed_origins_glob: list[str]
    ttl_seconds: int
    created_at: datetime
    expires_at: datetime
    # Runtime counters — managed by budgets/local.py; zero at creation.
    reserved_minor_units: int = 0
    settled_minor_units: int = 0
    status: EnvelopeStatus = "active"
    # Ed25519 public key (base64-encoded); populated by budgets/keystore.py in Phase 1 W1.
    counter_public_key: str = ""


# ---------------------------------------------------------------------------
# DrawReceipt
# ---------------------------------------------------------------------------


class DrawReceipt(RoutewilerModel):
    """Ed25519-signed token authorizing a single budget draw.

    The signature covers all other fields. Issuance and verification logic
    live in budgets/receipts.py; this model is the wire/storage shape.
    """

    receipt_id: str  # UUIDv7
    envelope_id: str
    request_id: str  # caller's trace ID
    idempotency_key: str  # prevents double-counting on retry
    amount_reserved_minor_units: int
    amount_reserved_currency: EnvelopeCurrency
    rail_quoted: Rail
    issued_at: datetime
    expires_at: datetime
    counter_public_key: str  # base64-encoded Ed25519 public key
    signature: str  # base64-encoded Ed25519 signature over the above fields
