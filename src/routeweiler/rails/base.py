"""RailAdapter — protocol every rail adapter must satisfy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from routeweiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routeweiler.normalized import NormalizedChallenge, ProofType, Rail, Resource

if TYPE_CHECKING:
    from routeweiler.funding import FundingSource


def resource_from_request(request: httpx.Request) -> Resource:
    """Build the ``Resource`` block common to every adapter's ``parse()``."""
    return Resource(
        method=request.method,
        url=str(request.url),
        url_encoding="raw",
        original_status=HTTP_STATUS_PAYMENT_REQUIRED,
    )


@dataclass(frozen=True)
class SettlementInfo:
    """Rail-agnostic payment proof from the server's response headers.

    All fields except `success` are optional because the spec allows a
    facilitator to omit them (e.g. in testnet / mock scenarios).
    """

    success: bool
    tx_hash: str | None = None
    network_id: str | None = None
    payer_address: str | None = None
    amount_paid: int | None = None  # base units; None if facilitator omits it
    facilitator: str | None = None  # e.g. "stripe", "tempo", "lightning", "cdp"


@dataclass(frozen=True)
class PaymentResult:
    """Output of ``RailAdapter.pay()``.

    Attributes:
        header_name:   HTTP header to set on the retry request (e.g.
                       ``"PAYMENT-SIGNATURE"`` for x402, ``"Authorization"``
                       for L402/MPP).
        header_value:  The header string.
        credential:    Rail-specific persisted credential (e.g.
                       ``{"macaroon": ..., "preimage": ...}`` for L402);
                       ``None`` for x402.
        proof_type:    Category of payment proof produced by this rail.
        proof_value:   Proof string (preimage hex for L402, SPT id for MPP-SPT,
                       tx hash for MPP-Tempo). For x402, ``pay()`` sets this to
                       ``None``; the emitter falls back to ``settlement.tx_hash``
                       from the PAYMENT-RESPONSE header.
    """

    header_name: str | None
    header_value: str | None
    credential: dict[str, Any] | None
    proof_type: ProofType
    proof_value: str | None


@runtime_checkable
class RailAdapter(Protocol):
    """Protocol every rail adapter implements.

    The adapter lifecycle per payment:
        1. ``can_handle``   — detect a 402 as belonging to this rail.
        2. ``parse``        — decode the challenge into ``NormalizedChallenge``.
        3. ``match_funding``— confirm a funding source is available.
        4. ``pay``          — produce a ``PaymentResult`` (builds the signed
                              credential and authorization header).
        5. ``confirm``      — read the server's settlement proof from the response.

    The canonical implementation path: override ``pay`` and ``confirm``.
    Adapters may use private helpers (e.g. ``_sign``) but must not expose them
    through this Protocol.
    """

    rail: Rail
    """Rail identity (e.g. ``"x402"``) — maps policy prefer lists to adapters."""

    proof_type: ProofType
    """Proof category produced by this rail (``"txid"``, ``"preimage"``, ``"spt_id"``)."""

    def can_handle(self, response: httpx.Response) -> bool:
        """Return True if this adapter recognizes the 402 challenge."""
        ...

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        """Decode the 402 response into a NormalizedChallenge.

        Raises ChallengeParseError on malformed or unsupported payloads.
        """
        ...

    def match_funding(
        self,
        challenge: NormalizedChallenge,
        funding: Sequence[FundingSource],
    ) -> FundingSource | None:
        """Return the first funding source that can satisfy this challenge, or None.

        Called by the router after parsing to check funding availability before
        committing to a payment attempt.
        """
        ...

    async def pay(
        self,
        challenge: NormalizedChallenge,
    ) -> PaymentResult:
        """Execute the payment and return a PaymentResult.

        Raises SigningError (or a rail-specific payment error) on failure.
        """
        ...

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo:
        """Read settlement proof from the server's successful reply.

        When no settlement header is present (mock/testnet), returns a
        SettlementInfo with ``tx_hash=None`` and ``success`` derived from the
        HTTP status code.
        """
        ...
