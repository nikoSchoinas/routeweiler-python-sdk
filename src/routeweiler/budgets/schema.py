"""BudgetEnvelope and DrawReceipt Pydantic models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from routeweiler._base import RouteweilerModel
from routeweiler.normalized import Rail

# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

EnvelopeCurrency = Literal["usd", "eur", "jpy", "gbp"]
EnvelopeStatus = Literal["active", "frozen", "expired", "revoked"]
DrawState = Literal["reserved", "settled", "rolled_back"]

# ---------------------------------------------------------------------------
# BudgetEnvelope
# ---------------------------------------------------------------------------


class BudgetEnvelope(RouteweilerModel):
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
    created_at: datetime
    expires_at: datetime
    status: EnvelopeStatus = "active"
    # Ed25519 public key (base64-encoded); populated by budgets/keystore.py in Phase 1 W1.
    counter_public_key: str = ""


class BudgetEnvelopeSpec(RouteweilerModel):
    """Declarative spec for creating a spending envelope inside ``Routeweiler.__aenter__``.

    Pass an instance as ``budget_envelope`` to ``Routeweiler(...)`` to have the
    envelope created idempotently when the client enters its context — no separate
    ``client.envelopes.create(...)`` call or two-step construction required::

        async with Routeweiler(
            funding=[Funding.base_usdc(wallet=signer)],
            trace_sink=TraceSink.sqlite("rw.db"),
            budget_envelope=BudgetEnvelopeSpec(
                id="session-abc",
                cap_minor_units=500,
                cap_currency="usd",
                allowed_rails=["x402", "l402"],
                ttl_seconds=3_600,
            ),
        ) as client:
            ...

    If an envelope with the same ``id`` already exists in the database the spec is
    silently ignored and the existing envelope is used.
    """

    id: str
    cap_minor_units: int
    cap_currency: EnvelopeCurrency
    allowed_rails: list[Rail]
    ttl_seconds: int
    allowed_origins_glob: list[str] | None = None
    owner_agent_id: str | None = None


# ---------------------------------------------------------------------------
# DrawReceipt
# ---------------------------------------------------------------------------


class DrawReceipt(RouteweilerModel):
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
