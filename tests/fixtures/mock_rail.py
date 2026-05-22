"""MockRailAdapter — a configurable test double for multi-rail routing tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx

from routeweiler.funding.evm import EvmFundingSource
from routeweiler.normalized import (
    NormalizedChallenge,
    Payee,
    Price,
    ProofType,
    Rail,
    Resource,
    X402PaymentRequirements,
    X402RailRaw,
)
from routeweiler.rails.base import PaymentResult, RailAdapter, SettlementInfo

# Sentinel object returned by match_funding when the test does not supply a real
# EvmFundingSource.  The router only checks truthiness (None vs. non-None), so
# any non-None value satisfies the contract.
_MOCK_FUNDING_SENTINEL: Any = MagicMock(name="MockFundingSource")


def make_mock_challenge(
    rail: Rail = "x402",
    url: str = "http://mock/resource",
    amount: int = 1000,
    currency: str = "eip155:84532/erc20:0x036cbd53842c5426634e7929541ec2318f3dcf7e",
) -> NormalizedChallenge:
    """Build a minimal NormalizedChallenge for testing."""
    expires_at = datetime.now(UTC) + timedelta(seconds=60)
    raw: X402RailRaw = X402RailRaw(
        kind="x402",
        accepts=[
            X402PaymentRequirements(
                scheme="exact",
                network="base-sepolia",
                amount=str(amount),
                pay_to="0xdeadbeef",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                extra={},
            )
        ],
    )
    return NormalizedChallenge(
        rail=rail,
        resource=Resource(method="GET", url=url, url_encoding="raw"),
        price=Price(amount=amount, currency=currency, human_amount=f"{amount} USDC"),
        payee=Payee(identifier="0xdeadbeef"),
        scheme="exact",
        nonce="abc123",
        expires_at=expires_at,
        raw=raw,
    )


class MockRailAdapter:
    """Configurable test double satisfying the RailAdapter protocol.

    Parameters
    ----------
    rail:
        Rail identifier (e.g. ``"x402"`` or ``"l402"``).
    handles:
        Whether ``can_handle`` returns True.
    parse_challenge:
        The challenge returned by ``parse`` (defaults to a synthetic one).
    sign_result:
        The header value returned by ``pay``.  If None, ``pay`` raises.
    sign_error:
        Exception raised by ``pay`` when ``sign_result`` is None.
    has_funding:
        When True (default), ``match_funding`` returns a real funding source if
        one is present in the list, or a sentinel mock if the list is empty.
        When False, ``match_funding`` always returns None (simulating no match).
    """

    proof_type: ProofType = "txid"

    def __init__(
        self,
        rail: Rail = "x402",
        *,
        handles: bool = True,
        parse_challenge: NormalizedChallenge | None = None,
        sign_result: str | None = "mock-payment-header",
        sign_error: Exception | None = None,
        has_funding: bool = True,
    ) -> None:
        self.rail = rail
        self._handles = handles
        self._parse_challenge = parse_challenge or make_mock_challenge(rail=rail)
        self._sign_result = sign_result
        self._sign_error = sign_error or RuntimeError(f"MockRailAdapter({rail}) sign error")
        self._has_funding = has_funding

        # Counters for assertions in tests.
        self.sign_call_count = 0
        self.parse_call_count = 0

    def can_handle(self, response: httpx.Response) -> bool:
        return self._handles

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        self.parse_call_count += 1
        return self._parse_challenge

    def match_funding(
        self,
        challenge: NormalizedChallenge,
        funding: Sequence[EvmFundingSource],
    ) -> Any:
        if not self._has_funding:
            return None
        # Return the first real EvmFundingSource from the list.
        for f in funding:
            if isinstance(f, EvmFundingSource):
                return f
        # No real funding in the list — return a sentinel so tests that do not
        # supply a real funding source still pass the router's funding filter.
        return _MOCK_FUNDING_SENTINEL

    async def sign(self, challenge: NormalizedChallenge) -> str:
        self.sign_call_count += 1
        if self._sign_result is None:
            raise self._sign_error
        return self._sign_result

    async def pay(self, challenge: NormalizedChallenge, receipt: Any = None) -> PaymentResult:
        self.sign_call_count += 1
        if self._sign_result is None:
            raise self._sign_error
        return PaymentResult(
            header_name="PAYMENT-SIGNATURE",
            header_value=self._sign_result,
            credential=None,
            proof_type=self.proof_type,
            proof_value=None,
        )

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo:
        return SettlementInfo(success=response.is_success)


# Ensure MockRailAdapter satisfies the runtime-checkable protocol.
assert isinstance(MockRailAdapter(), RailAdapter), (
    "MockRailAdapter does not satisfy RailAdapter protocol"
)
