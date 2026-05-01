"""RailAdapter — protocol every rail adapter must satisfy."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from routewiler.normalized import NormalizedChallenge


@runtime_checkable
class RailAdapter(Protocol):
    """Protocol every rail adapter implements.

    Week 2 surface: detect → parse → sign.
    Week 3+: pay(challenge, funding, receipt) + confirm(result).
    """

    def can_handle(self, response: httpx.Response) -> bool:
        """Return True if this adapter recognizes the 402 challenge."""
        ...

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        """Decode the 402 response into a NormalizedChallenge.

        Raises ChallengeParseError on malformed or unsupported payloads.
        """
        ...

    async def sign(self, challenge: NormalizedChallenge) -> str:
        """Produce the payment header value for the retry request.

        Returns the raw string to set as the PAYMENT-SIGNATURE (x402),
        Authorization (L402), or X-MPP-AUTHORIZATION (MPP) header.
        Raises SigningError on failure.
        """
        ...
