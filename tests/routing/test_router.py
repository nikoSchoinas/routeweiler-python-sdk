"""Tests for Router — cost-based selection and filtering."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from routeweiler.errors import NoFeasibleRailError, PolicyDeniedError, RailNotSupportedError
from routeweiler.policy.dsl import DefaultBlock, PolicyDocument
from routeweiler.policy.engine import PolicyDecision, PolicyEngine
from routeweiler.routing.router import Router
from tests.fixtures.mock_rail import MockRailAdapter, make_mock_challenge


def _402_response() -> httpx.Response:
    return httpx.Response(402, headers={"PAYMENT-REQUIRED": "dW5pdA=="})


def _200_response() -> httpx.Response:
    return httpx.Response(200)


def _request(url: str = "http://mock/resource") -> httpx.Request:
    return httpx.Request("GET", url)


def _policy_engine_prefer(*rails: str) -> PolicyEngine:
    """Build a PolicyEngine that returns prefer=rails for any challenge."""
    doc = PolicyDocument(
        version=1, default=DefaultBlock(rail=rails[0] if rails else "x402"), rules=[]
    )

    class _Fixed(PolicyEngine):
        def evaluate(self, challenge):  # type: ignore[override]
            return PolicyDecision(
                rule_name=None,
                deny=False,
                prefer=tuple(rails),
                max_per_call_minor_units=None,
                reason=None,
            )

    return _Fixed(doc)


def _deny_engine() -> PolicyEngine:
    doc = PolicyDocument(version=1, default=DefaultBlock(rail="x402"), rules=[])

    class _Deny(PolicyEngine):
        def evaluate(self, challenge):  # type: ignore[override]
            return PolicyDecision(
                rule_name="deny-all",
                deny=True,
                prefer=(),
                max_per_call_minor_units=None,
                reason="test deny",
            )

    return _Deny(doc)


class TestRouterSingleRail:
    @pytest.mark.anyio
    async def test_single_candidate_returns_it(self) -> None:
        adapter = MockRailAdapter(rail="x402")
        router = Router([adapter])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        assert choice.candidate.adapter is adapter
        assert choice.attempt == 0
        assert choice.fallback_from is None

    @pytest.mark.anyio
    async def test_no_adapters_raises_rail_not_supported(self) -> None:
        router = Router([])
        with pytest.raises(RailNotSupportedError):
            await router.decide(
                request=_request(),
                response=_402_response(),
                policy_engine=None,
                funding=[],
                envelope_currency=None,
                fmv_snapshot=None,
            )

    @pytest.mark.anyio
    async def test_adapter_cannot_handle_raises_rail_not_supported(self) -> None:
        adapter = MockRailAdapter(rail="x402", handles=False)
        router = Router([adapter])
        with pytest.raises(RailNotSupportedError):
            await router.decide(
                request=_request(),
                response=_402_response(),
                policy_engine=None,
                funding=[],
                envelope_currency=None,
                fmv_snapshot=None,
            )


class TestRouterScoring:
    @pytest.mark.anyio
    async def test_cheaper_candidate_wins_on_cost(self) -> None:
        """Two adapters; the one with a cheaper FMV-converted quote wins."""
        cheap = MockRailAdapter(
            rail="x402",
            parse_challenge=make_mock_challenge(rail="x402", amount=100),
        )
        expensive = MockRailAdapter(
            rail="l402",
            parse_challenge=make_mock_challenge(rail="l402", amount=1000),
        )
        router = Router([cheap, expensive])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency="usd",
            fmv_snapshot={
                "usd->usd": Decimal("1"),
            },
        )
        assert choice.candidate.adapter is cheap


class TestRouterPolicyFiltering:
    @pytest.mark.anyio
    async def test_prefer_boosts_preferred_rail_to_winner(self) -> None:
        # prefer is a tiebreaker: the preferred rail wins when quotes are equal.
        x402 = MockRailAdapter(rail="x402")
        l402 = MockRailAdapter(rail="l402")
        router = Router([x402, l402])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=_policy_engine_prefer("l402"),
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        assert choice.candidate.adapter is l402

    @pytest.mark.anyio
    async def test_deny_all_raises_policy_denied(self) -> None:
        adapter = MockRailAdapter(rail="x402")
        router = Router([adapter])
        with pytest.raises(PolicyDeniedError, match="test deny"):
            await router.decide(
                request=_request(),
                response=_402_response(),
                policy_engine=_deny_engine(),
                funding=[],
                envelope_currency=None,
                fmv_snapshot=None,
            )

    @pytest.mark.anyio
    async def test_prefer_one_of_two_picks_preferred(self) -> None:
        x402 = MockRailAdapter(rail="x402")
        l402 = MockRailAdapter(rail="l402")
        router = Router([x402, l402])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=_policy_engine_prefer("x402"),
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        assert choice.candidate.adapter is x402

    @pytest.mark.anyio
    async def test_prefer_falls_back_to_available_rail(self) -> None:
        # prefer is a tiebreaker, not a filter.
        # When policy prefers l402 but only x402 is available, x402 is still selected.
        x402 = MockRailAdapter(rail="x402")
        router = Router([x402])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=_policy_engine_prefer("l402"),  # prefer l402, but only x402 exists
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        assert choice.candidate.adapter is x402  # falls back gracefully


class TestRouterFundingFilter:
    @pytest.mark.anyio
    async def test_no_matching_funding_raises_no_feasible(self) -> None:
        # has_funding=False makes match_funding always return None.
        router = Router([MockRailAdapter(rail="x402", has_funding=False)])
        with pytest.raises(NoFeasibleRailError):
            await router.decide(
                request=_request(),
                response=_402_response(),
                policy_engine=None,
                funding=[],
                envelope_currency=None,
                fmv_snapshot=None,
            )


class TestRouterExcludedRails:
    @pytest.mark.anyio
    async def test_excluded_rail_is_skipped(self) -> None:
        x402 = MockRailAdapter(rail="x402")
        l402 = MockRailAdapter(rail="l402")
        router = Router([x402, l402])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
            excluded_rails=frozenset(["x402"]),
        )
        assert choice.candidate.adapter is l402

    @pytest.mark.anyio
    async def test_all_excluded_raises_no_feasible(self) -> None:
        x402 = MockRailAdapter(rail="x402")
        router = Router([x402])
        with pytest.raises(NoFeasibleRailError):
            await router.decide(
                request=_request(),
                response=_402_response(),
                policy_engine=None,
                funding=[],
                envelope_currency=None,
                fmv_snapshot=None,
                excluded_rails=frozenset(["x402"]),
            )

    @pytest.mark.anyio
    async def test_prior_rail_is_propagated_to_choice(self) -> None:
        l402 = MockRailAdapter(rail="l402")
        router = Router([l402])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
            excluded_rails=frozenset(["x402"]),
            prior_rail="x402",
            attempt=1,
        )
        assert choice.fallback_from == "x402"
        assert choice.attempt == 1


class TestRouterSticky:
    @pytest.mark.anyio
    async def test_sticky_rail_wins_over_cheaper(self) -> None:
        """Sticky rail is picked even if a different rail has a lower cost."""
        x402 = MockRailAdapter(
            rail="x402",
            parse_challenge=make_mock_challenge(rail="x402", amount=1000),
        )
        l402 = MockRailAdapter(
            rail="l402",
            parse_challenge=make_mock_challenge(rail="l402", amount=100),
        )
        router = Router([x402, l402])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
            sticky_rail="x402",
        )
        assert choice.candidate.adapter is x402

    @pytest.mark.anyio
    async def test_sticky_rail_not_in_candidates_falls_through(self) -> None:
        """If sticky rail was excluded, fall through to normal selection."""
        x402 = MockRailAdapter(rail="x402")
        router = Router([x402])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
            sticky_rail="l402",  # stale sticky; l402 not available
        )
        assert choice.candidate.adapter is x402


class TestDefaultRailTieBreak:
    """The policy's `default.rail` breaks cost ties."""

    def _build_engine(self, default_rail: str) -> PolicyEngine:
        doc = PolicyDocument(
            version=1,
            default=DefaultBlock(rail=default_rail),
            rules=[],  # type: ignore[arg-type]
        )
        return PolicyEngine(doc)

    @pytest.mark.anyio
    async def test_default_rail_wins_on_cost_tie_second_adapter(self) -> None:
        """When costs are equal, `default.rail` matching the second adapter wins."""
        first = MockRailAdapter(rail="x402", parse_challenge=make_mock_challenge(rail="x402"))
        second = MockRailAdapter(rail="l402", parse_challenge=make_mock_challenge(rail="l402"))
        router = Router([first, second])
        engine = self._build_engine("l402")
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=engine,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        assert choice.candidate.adapter is second

    @pytest.mark.anyio
    async def test_default_rail_wins_on_cost_tie_first_adapter(self) -> None:
        """When costs are equal, `default.rail` matching the first adapter wins."""
        first = MockRailAdapter(rail="x402", parse_challenge=make_mock_challenge(rail="x402"))
        second = MockRailAdapter(rail="l402", parse_challenge=make_mock_challenge(rail="l402"))
        router = Router([first, second])
        engine = self._build_engine("x402")
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=engine,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        assert choice.candidate.adapter is first

    @pytest.mark.anyio
    async def test_default_rail_does_not_override_clear_cost_winner(self) -> None:
        """A clearly cheaper rail wins regardless of `default.rail`."""
        cheap = MockRailAdapter(
            rail="x402",
            parse_challenge=make_mock_challenge(rail="x402", amount=100_000),
        )
        expensive = MockRailAdapter(
            rail="l402",
            parse_challenge=make_mock_challenge(rail="l402", amount=9_000_000),
        )
        router = Router([cheap, expensive])
        # default.rail is l402, but x402 should win on cost.
        engine = self._build_engine("l402")
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=engine,
            funding=[],
            envelope_currency="usd",
            fmv_snapshot={"usd->usd": __import__("decimal").Decimal("1")},
        )
        assert choice.candidate.adapter is cheap

    @pytest.mark.anyio
    async def test_no_policy_engine_falls_back_to_adapter_order(self) -> None:
        """Without a policy engine, adapter registration order is the final tie-breaker."""
        first = MockRailAdapter(rail="x402")
        second = MockRailAdapter(rail="l402")
        router = Router([first, second])
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        assert choice.candidate.adapter is first
