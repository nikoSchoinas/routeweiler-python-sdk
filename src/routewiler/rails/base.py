"""RailAdapter — protocol every rail adapter must satisfy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

import httpx

from routewiler.normalized import NormalizedChallenge, Rail

if TYPE_CHECKING:
    from routewiler.funding.evm import EvmFundingSource


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


@runtime_checkable
class RailAdapter(Protocol):
    """Protocol every rail adapter implements.

    Week 2 surface: detect → parse → sign → parse_settlement.
    Week 3+: pay(challenge, funding, receipt) + confirm(result).
    """

    rail: Rail
    """Rail identity (e.g. ``"x402"``) — used by the router to map policy prefer lists to adapters."""

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
        funding: Sequence[EvmFundingSource],
    ) -> EvmFundingSource | None:
        """Return the first funding source that can satisfy this challenge, or None.

        Called by the router after parsing to check funding availability before
        committing to a payment attempt.
        """
        ...

    async def sign(self, challenge: NormalizedChallenge) -> str:
        """Produce the payment header value for the retry request.

        Returns the raw string to set as the PAYMENT-SIGNATURE (x402),
        Authorization (L402), or X-MPP-AUTHORIZATION (MPP) header.
        Raises SigningError on failure.
        """
        ...

    def parse_settlement(self, response: httpx.Response) -> SettlementInfo | None:
        """Read payment proof from the server's successful reply headers.

        Returns None if the header is absent or cannot be decoded.
        """
        ...
