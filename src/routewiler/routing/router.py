"""Routing engine - routeDecision(challenge, policy, funding) -> Rail.

Implements §7.1 (static scoring), §7.2 (sticky routing), and §7.3 (failover)
of the Routewiler technical plan.

Static scoring weights (§7.1):
    cost 0.3 / latency 0.1 / reliability 0.4 / privacy 0.2

Live signals (§7.4) stay on hardcoded tables until sufficient trace data
accumulates (post-MVP).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

from routewiler.budgets.fmv import amount_to_envelope_minor_units
from routewiler.errors import NoFeasibleRailError, PolicyDeniedError, RailNotSupportedError
from routewiler.normalized import NormalizedChallenge, Rail
from routewiler.policy.engine import PolicyDecision

if TYPE_CHECKING:
    from routewiler.funding import FundingSource
    from routewiler.policy.engine import PolicyEngine
    from routewiler.rails.base import RailAdapter

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static hardcoded tables (§7.4 — replaced by rolling computation post-MVP)
# ---------------------------------------------------------------------------

# Median observed p50 latencies in milliseconds per rail type.
DEFAULT_LATENCY_P50_MS: dict[str, int] = {
    "x402": 1500,
    "l402": 4000,
    "mpp-tempo": 2500,
    "mpp-spt": 6000,
}

# Historical success rates per rail (replaced by rolling 24h trace data post-MVP).
DEFAULT_RELIABILITY: dict[str, float] = {
    "x402": 0.97,
    "l402": 0.92,
    "mpp-tempo": 0.95,
    "mpp-spt": 0.90,
}

_FALLBACK_LATENCY_MS = 5000
_FALLBACK_RELIABILITY = 0.5

# Inherent privacy scores per rail — used when no policy prefer is set.
# Higher = more private (lightning > on-chain EVM for obvious reasons).
_INHERENT_PRIVACY: dict[str, float] = {
    "x402": 0.3,
    "l402": 0.8,
    "mpp-tempo": 0.4,
    "mpp-spt": 0.7,
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringWeights:
    """Weights for the §7.1 scoring formula (must sum to 1.0)."""

    cost: float = 0.3
    latency: float = 0.1
    reliability: float = 0.4
    privacy: float = 0.2


DEFAULT_WEIGHTS = ScoringWeights()


@dataclass(frozen=True)
class Candidate:
    """A parsed, feasible rail candidate produced by the router.

    Attributes:
        adapter:                      The rail adapter to use for signing.
        challenge:                    Parsed NormalizedChallenge for this rail.
        quote_envelope_minor_units:   FMV-converted quote in envelope currency
                                      minor units.  0 when budget enforcement is
                                      not active (no trace_sink).
        policy_decision:              The PolicyDecision for this candidate's
                                      challenge — carries max_per_call, deny,
                                      prefer, and rule_name for error reporting.
    """

    adapter: RailAdapter
    challenge: NormalizedChallenge
    quote_envelope_minor_units: int | None  # None when FMV conversion failed; 0 when budget off
    policy_decision: PolicyDecision


@dataclass(frozen=True)
class RoutingChoice:
    """The router's output — the winning candidate and diagnostic metadata.

    Attributes:
        candidate:      The chosen rail candidate.
        fallback_from:  The rail that failed in the previous attempt (None on
                        the primary attempt).  Written into TraceEvent.fallback_from.
        attempt:        Attempt counter (0 = primary, 1+ = failover).
        score:          Total weighted score for the winner.
        score_breakdown: Per-component scores keyed by name.
    """

    candidate: Candidate
    fallback_from: Rail | None
    attempt: int
    score: float
    score_breakdown: dict[str, float]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Implements §7.1-§7.3 of the technical plan.

    The router is constructed once per ``Routewiler`` instance and called on
    every 402 response.  It is stateless w.r.t. routing decisions — the sticky
    cache lives in ``StickyCache`` and is managed by the caller (``RoutewilerAuth``).
    """

    def __init__(
        self,
        adapters: Sequence[RailAdapter],
        *,
        weights: ScoringWeights = DEFAULT_WEIGHTS,
        latency_p50_ms: Mapping[str, int] = DEFAULT_LATENCY_P50_MS,
        reliability: Mapping[str, float] = DEFAULT_RELIABILITY,
    ) -> None:
        self._adapters = list(adapters)
        self._weights = weights
        self._latency_p50_ms = latency_p50_ms
        self._reliability = reliability

    @property
    def adapters(self) -> list[RailAdapter]:
        return self._adapters

    async def decide(
        self,
        *,
        request: httpx.Request,
        response: httpx.Response,
        policy_engine: PolicyEngine | None,
        funding: Sequence[FundingSource],
        envelope_currency: str | None,
        fmv_snapshot: dict[str, Decimal] | None,
        excluded_rails: frozenset[Rail] = frozenset(),
        sticky_rail: Rail | None = None,
        prior_rail: Rail | None = None,
        attempt: int = 0,
    ) -> RoutingChoice:
        """Select the best feasible rail for this 402 response.

        Steps (§7.1):
        1. Enumerate adapters whose ``can_handle`` returns True and rail is not
           in ``excluded_rails``.
        2. Parse each adapter into a NormalizedChallenge (swallows per-adapter
           parse failures so a malformed header for one rail doesn't block others).
        3. Evaluate policy per challenge; drop candidates where ``deny`` is True.
        4. Filter by ``policy_decision.prefer`` (if non-empty).
        5. Filter by funding availability via ``match_funding``.
        6. FMV-convert quote to envelope minor units (0 when budget not active).
        7. Score remaining candidates.
        8. Apply sticky: if the cached rail is among survivors, pick it directly.
        9. Return winner; tie-break by prefer order then adapter order.

        Raises:
            RailNotSupportedError:  No adapter's ``can_handle`` matched (before any filtering).
            PolicyDeniedError:      All matching candidates were denied by policy.
            NoFeasibleRailError:    Candidates exist but all filtered out by prefer/funding.
        """
        # Step 1: detect
        can_handle = [
            a for a in self._adapters if a.can_handle(response) and a.rail not in excluded_rails
        ]
        if not can_handle:
            if not excluded_rails:
                raise RailNotSupportedError(
                    f"No rail adapter can handle the 402 from {request.url}. "
                    "Check that the server uses a supported rail (x402, L402, MPP) "
                    "and that you have configured the matching funding source."
                )
            raise NoFeasibleRailError(f"All available rails have been exhausted for {request.url}.")

        # Step 2: parse each candidate (swallow per-adapter failures)
        parsed = _parse_candidates(can_handle, request, response)
        if not parsed:
            raise NoFeasibleRailError(
                f"All candidate adapters failed to parse the 402 from {request.url}."
            )

        # Steps 3-4: policy filter
        policy_filtered: list[tuple[RailAdapter, NormalizedChallenge, PolicyDecision]] = []
        last_deny: PolicyDecision | None = None
        for adapter, challenge in parsed:
            decision = (
                policy_engine.evaluate(challenge)
                if policy_engine is not None
                else _default_decision()
            )
            if decision.deny:
                last_deny = decision
                _log.debug("Adapter %r denied by policy rule %r.", adapter.rail, decision.rule_name)
                continue
            if decision.prefer and adapter.rail not in decision.prefer:
                _log.debug(
                    "Adapter %r not in policy prefer list %r; dropping.",
                    adapter.rail,
                    decision.prefer,
                )
                continue
            policy_filtered.append((adapter, challenge, decision))

        if not policy_filtered:
            if last_deny is not None:
                raise PolicyDeniedError(reason=last_deny.reason, rule_name=last_deny.rule_name)
            raise NoFeasibleRailError(
                f"No candidate rail survived policy filtering for {request.url}."
            )

        # Step 5: funding filter
        funded: list[tuple[RailAdapter, NormalizedChallenge, PolicyDecision]] = []
        for adapter, challenge, decision in policy_filtered:
            matched = adapter.match_funding(challenge, funding)
            if matched is None:
                _log.debug("Adapter %r has no matching funding source; dropping.", adapter.rail)
                continue
            funded.append((adapter, challenge, decision))

        if not funded:
            raise NoFeasibleRailError(
                f"No candidate rail has a matching funding source for {request.url}."
            )

        # Step 6: FMV-convert quotes
        candidates: list[tuple[Candidate, PolicyDecision]] = []
        for adapter, challenge, decision in funded:
            quote = _fmv_quote(
                challenge=challenge,
                envelope_currency=envelope_currency,
                fmv_snapshot=fmv_snapshot,
            )
            candidates.append(
                (
                    Candidate(
                        adapter=adapter,
                        challenge=challenge,
                        quote_envelope_minor_units=quote,
                        policy_decision=decision,
                    ),
                    decision,
                )
            )

        # Step 7 & 8: score and apply sticky shortcut
        winner = _select_winner(
            candidates=candidates,
            sticky_rail=sticky_rail,
            weights=self._weights,
            latency_p50_ms=self._latency_p50_ms,
            reliability=self._reliability,
        )

        return RoutingChoice(
            candidate=winner.candidate,
            fallback_from=prior_rail,
            attempt=attempt,
            score=winner.score,
            score_breakdown=winner.score_breakdown,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScoredCandidate:
    candidate: Candidate
    score: float
    score_breakdown: dict[str, float]


def _select_winner(
    candidates: list[tuple[Candidate, PolicyDecision]],
    sticky_rail: Rail | None,
    weights: ScoringWeights,
    latency_p50_ms: Mapping[str, int],
    reliability: Mapping[str, float],
) -> _ScoredCandidate:
    """Score candidates and return the winner.

    Sticky rail wins immediately (§7.2) if it is among the survivors.
    Ties broken by policy prefer order then candidate list order.
    """
    # Check sticky shortcut first.
    if sticky_rail is not None:
        for candidate, _decision in candidates:
            if candidate.adapter.rail == sticky_rail:
                # Compute score for diagnostics even though we don't use it for selection.
                breakdown = _score_breakdown(
                    candidate, candidates, weights, latency_p50_ms, reliability
                )
                return _ScoredCandidate(
                    candidate=candidate,
                    score=sum(breakdown.values()),
                    score_breakdown=breakdown,
                )

    # Full scoring pass.
    scored = []
    for c, _ in candidates:
        breakdown = _score_breakdown(c, candidates, weights, latency_p50_ms, reliability)
        scored.append(
            _ScoredCandidate(
                candidate=c,
                score=sum(breakdown.values()),
                score_breakdown=breakdown,
            )
        )
    # Stable sort: highest score first; list order (= adapter + prefer order) breaks ties.
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[0]


def _score_breakdown(
    candidate: Candidate,
    all_candidates: list[tuple[Candidate, PolicyDecision]],
    weights: ScoringWeights,
    latency_p50_ms: Mapping[str, int],
    reliability: Mapping[str, float],
) -> dict[str, float]:
    """Compute the §7.1 score breakdown for a single candidate.

    FMV-failed candidates (quote_envelope_minor_units is None) receive
    cost_score=0.0 — they rank worst on cost versus any candidate with a known
    quote.  When *all* candidates have None quotes (total outage), everyone gets
    cost_score=1.0 and cost falls out of the decision.  The "budget off" case
    (quote=0) gives cost_score=1.0 for all, preserving the existing behaviour.
    """
    rail = candidate.adapter.rail
    decision = candidate.policy_decision

    valid_quotes = [
        c.quote_envelope_minor_units
        for c, _ in all_candidates
        if c.quote_envelope_minor_units is not None
    ]
    max_quote = max(valid_quotes) if valid_quotes else 0
    if candidate.quote_envelope_minor_units is None:
        cost_score = 0.0  # FMV failed — penalise on cost
    else:
        q = candidate.quote_envelope_minor_units
        cost_score = 1.0 - q / max_quote if max_quote > 0 else 1.0

    p50s = [latency_p50_ms.get(c.adapter.rail, _FALLBACK_LATENCY_MS) for c, _ in all_candidates]
    max_p50 = max(p50s) if p50s else 1
    my_p50 = latency_p50_ms.get(rail, _FALLBACK_LATENCY_MS)
    # When all candidates share the same latency (or there is only one), relative
    # score is 1.0 — mirroring the cost branch's treatment of max_quote == 0.
    latency_score = 1.0 - my_p50 / max_p50 if max_p50 > 0 and len(set(p50s)) > 1 else 1.0

    reliability_score = reliability.get(rail, _FALLBACK_RELIABILITY)

    privacy_fit_score = 1.0 if decision.prefer else _INHERENT_PRIVACY.get(rail, 0.5)

    return {
        "cost": weights.cost * cost_score,
        "latency": weights.latency * latency_score,
        "reliability": weights.reliability * reliability_score,
        "privacy": weights.privacy * privacy_fit_score,
    }


def _parse_candidates(
    adapters: list[RailAdapter],
    request: httpx.Request,
    response: httpx.Response,
) -> list[tuple[RailAdapter, NormalizedChallenge]]:
    parsed = []
    for adapter in adapters:
        try:
            challenge = adapter.parse(request, response)
            parsed.append((adapter, challenge))
        except Exception:
            _log.debug(
                "Adapter %r failed to parse 402 from %s; skipping.", adapter.rail, request.url
            )
    return parsed


def _fmv_quote(
    challenge: NormalizedChallenge,
    envelope_currency: str | None,
    fmv_snapshot: dict[str, Decimal] | None,
) -> int | None:
    """Convert the challenge price to envelope minor units.

    Returns:
        0       — budget enforcement is not active (no envelope_currency); all
                  candidates are cost-equal and 0/max_quote=0 gives cost_score=1.0.
        int > 0 — successful FMV conversion.
        None    — FMV conversion failed; caller scores this candidate worst on cost.
    """
    if envelope_currency is None:
        return 0
    try:
        quote, _quality = amount_to_envelope_minor_units(
            challenge.price.currency,
            challenge.price.amount,
            envelope_currency,
            snapshot_rates=fmv_snapshot,
        )
        return quote
    except Exception:
        _log.debug(
            "FMV conversion failed for %s→%s; candidate will be scored worst on cost.",
            challenge.price.currency,
            envelope_currency,
        )
        return None


def _default_decision() -> PolicyDecision:
    return PolicyDecision(
        rule_name=None,
        deny=False,
        prefer=(),
        max_per_call_minor_units=None,
        reason=None,
    )
