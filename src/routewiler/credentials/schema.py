"""Credential record — the persistent shape stored in the credentials table."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, TypedDict

from routewiler._base import RoutewilerModel
from routewiler.normalized import Rail


class L402CredentialPayload(TypedDict):
    """Typed shape for L402 credential payloads stored in CredentialRecord.payload."""

    macaroon: str
    preimage_hex: str
    invoice: str
    payment_hash_hex: str


class CredentialState(StrEnum):
    PERSISTED = "persisted"
    RECOVERING = "recovering"
    REDEEMED = "redeemed"
    MANUAL_HOLD = "manual_hold"


class ManualHoldReason(StrEnum):
    EXHAUSTED = "exhausted"  # recovery attempts exhausted
    EXPIRED = "expired"  # macaroon valid_until or invoice expiry past


class CredentialRecord(RoutewilerModel):
    """A persisted rail credential and its recovery lifecycle state.

    Rail-agnostic: `payload` carries the per-rail data (macaroon+preimage for
    L402; tx_hash+payment_payload for x402; charge_id for MPP).
    """

    credential_id: str
    request_id: str  # links to TraceEvent.request_id
    rail: Rail
    challenge_url: str
    payload: dict[str, Any]  # rail-specific opaque blob
    state: CredentialState
    manual_hold_reason: ManualHoldReason | None = None
    persisted_at: datetime
    redeemed_at: datetime | None = None
    last_transition_at: datetime
    expires_at: datetime | None = None  # from challenge.expires_at; drives "expired" reason
