"""Tests for Router — §7.1 static scoring and §7.3 filtering."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from routeweiler.errors import NoFeasibleRailError, PolicyDeniedError, RailNotSupportedError
from routeweiler.policy.dsl import DefaultBlock, PolicyDocument
from routeweiler.policy.engine import PolicyDecision, PolicyEngine
from routeweiler.routing.router import (
    DEFAULT_WEIGHTS,
    Router,
    ScoringWeights,
)
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
    async def test_single_candidate_cost_score_is_one(self) -> None:
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
        # When there is only one candidate, max_quote == quote → cost_score = 1.0.
        assert choice.score_breakdown["cost"] == pytest.approx(DEFAULT_WEIGHTS.cost * 1.0)

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
        """Two x402 adapters; the one with a cheaper quote should win."""
        cheap = MockRailAdapter(
            rail="x402",
            parse_challenge=make_mock_challenge(rail="x402", amount=100),
        )
        expensive = MockRailAdapter(
            rail="l402",
            parse_challenge=make_mock_challenge(rail="l402", amount=1000),
        )
        # Use cost-only weighting to isolate.
        weights = ScoringWeights(cost=1.0, latency=0.0, reliability=0.0, privacy=0.0)
        router = Router(
            [cheap, expensive],
            weights=weights,
            latency_p50_ms={"x402": 1000, "l402": 1000},
            reliability={"x402": 1.0, "l402": 1.0},
        )
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency="usd",
            fmv_snapshot={
                "usd->usd": Decimal("1"),
                # USDC stablecoin peg amounts handled by fmv module
            },
        )
        assert choice.candidate.adapter is cheap

    @pytest.mark.anyio
    async def test_more_reliable_candidate_wins_on_reliability(self) -> None:
        """Reliability-only weighting picks the higher-reliability rail."""
        reliable = MockRailAdapter(
            rail="x402",
            parse_challenge=make_mock_challenge(rail="x402", amount=100),
        )
        unreliable = MockRailAdapter(
            rail="l402",
            parse_challenge=make_mock_challenge(rail="l402", amount=100),
        )
        weights = ScoringWeights(cost=0.0, latency=0.0, reliability=1.0, privacy=0.0)
        router = Router(
            [reliable, unreliable],
            weights=weights,
            latency_p50_ms={"x402": 1000, "l402": 1000},
            reliability={"x402": 0.99, "l402": 0.80},
        )
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        assert choice.candidate.adapter is reliable

    @pytest.mark.anyio
    async def test_score_breakdown_matches_formula(self) -> None:
        """Verify the 0.3/0.1/0.4/0.2 formula numerically for a single candidate."""
        adapter = MockRailAdapter(rail="x402", parse_challenge=make_mock_challenge(amount=0))
        router = Router(
            [adapter],
            weights=DEFAULT_WEIGHTS,
            latency_p50_ms={"x402": 1500},
            reliability={"x402": 0.97},
        )
        choice = await router.decide(
            request=_request(),
            response=_402_response(),
            policy_engine=None,
            funding=[],
            envelope_currency=None,
            fmv_snapshot=None,
        )
        bd = choice.score_breakdown
        # Single candidate → cost=1.0, latency=1.0 (only one latency). Privacy no prefer → inherent.
        assert bd["cost"] == pytest.approx(0.3 * 1.0)
        assert bd["latency"] == pytest.approx(0.1 * 1.0)
        assert bd["reliability"] == pytest.approx(0.4 * 0.97)
        # Privacy: no prefer → inherent for x402 = 0.3
        assert bd["privacy"] == pytest.approx(0.2 * 0.3)


class TestRouterPolicyFiltering:
    @pytest.mark.anyio
    async def test_prefer_boosts_preferred_rail_to_winner(self) -> None:
        # prefer is a scoring boost: the preferred rail wins over non-preferred
        # when both are available, even though non-preferred is not dropped.
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
        # prefer is a scoring boost (§7.1), not a hard filter.
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
    async def test_sticky_rail_wins_over_higher_scorer(self) -> None:
        """Sticky rail is picked even if a different rail scores higher."""
        # l402 is cheaper (cost_score=1.0 when it has lower quote).
        # But we make x402 sticky → x402 should win.
        x402 = MockRailAdapter(
            rail="x402",
            parse_challenge=make_mock_challenge(rail="x402", amount=1000),
        )
        l402 = MockRailAdapter(
            rail="l402",
            parse_challenge=make_mock_challenge(rail="l402", amount=100),
        )
        # Use cost-only weighting → l402 would win without sticky.
        weights = ScoringWeights(cost=1.0, latency=0.0, reliability=0.0, privacy=0.0)
        router = Router(
            [x402, l402],
            weights=weights,
            latency_p50_ms={"x402": 1000, "l402": 1000},
            reliability={"x402": 1.0, "l402": 1.0},
        )
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
        """If sticky rail was excluded, fall through to normal scoring."""
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
