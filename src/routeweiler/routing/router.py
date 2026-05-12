"""Routing engine - routeDecision(challenge, policy, funding) -> Rail.

Implements cost-based selection, sticky routing, and failover.

Selection order (after policy/funding filters):
1. Sticky: if the cached rail is among survivors, pick it directly.
2. Lowest FMV-converted cost wins.
   - FMV-failed candidates (quote=None) rank worst.
   - When all quotes are equal (budget off, quote=0), cost is a no-op.
3. Tie: prefer rails (policy.prefer) beat non-prefer.
4. Tie: policy.default_rail wins.
5. Tie: adapter registration order.

Latency and reliability signals are not used until rolling trace data
accumulates post-MVP (TECHNICAL_PLAN.md §7.4).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

from routeweiler.budgets.fmv import amount_to_envelope_minor_units
from routeweiler.budgets.schema import EnvelopeCurrency
from routeweiler.errors import NoFeasibleRailError, PolicyDeniedError, RailNotSupportedError
from routeweiler.normalized import NormalizedChallenge, Rail
from routeweiler.policy.engine import PolicyDecision, _default_decision

if TYPE_CHECKING:
    from routeweiler.funding import FundingSource
    from routeweiler.policy.engine import PolicyEngine
    from routeweiler.rails.base import RailAdapter

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


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
    """

    candidate: Candidate
    fallback_from: Rail | None
    attempt: int


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """Selects the best feasible rail for each 402 response using cost-based
    selection, sticky routing, and failover.

    The router is constructed once per ``Routeweiler`` instance and called on
    every 402 response.  It is stateless w.r.t. routing decisions — the sticky
    cache lives in ``StickyCache`` and is managed by the caller (``RouteweilerAuth``).
    """

    def __init__(self, adapters: Sequence[RailAdapter]) -> None:
        self._adapters = list(adapters)

    async def decide(
        self,
        *,
        request: httpx.Request,
        response: httpx.Response,
        policy_engine: PolicyEngine | None,
        funding: Sequence[FundingSource],
        envelope_currency: EnvelopeCurrency | None,
        fmv_snapshot: dict[str, Decimal] | None,
        excluded_rails: frozenset[Rail] = frozenset(),
        sticky_rail: Rail | None = None,
        prior_rail: Rail | None = None,
        attempt: int = 0,
    ) -> RoutingChoice:
        """Select the best feasible rail for this 402 response.

        Steps:
        1. Enumerate adapters whose ``can_handle`` returns True and rail is not
           in ``excluded_rails``.
        2. Parse each adapter into a NormalizedChallenge (swallows per-adapter
           parse failures so a malformed header for one rail doesn't block others).
        3. Evaluate policy per challenge; drop candidates where ``deny`` is True.
           ``prefer`` is a tiebreaker boost — non-prefer rails remain eligible.
        4. Filter by funding availability via ``match_funding``.
        5. FMV-convert quote to envelope minor units (0 when budget not active).
        6. Apply sticky: if the cached rail is among survivors, pick it directly.
        7. Otherwise rank by cost (lower wins), then prefer, then default_rail,
           then adapter registration order.

        Raises:
            RailNotSupportedError:  No adapter's ``can_handle`` matched (before any filtering).
            PolicyDeniedError:      All matching candidates were denied by policy.
            NoFeasibleRailError:    Candidates exist but all filtered out by deny/funding.
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
        # ``deny`` is a hard exclusion; ``prefer`` is a tiebreaker boost — non-prefer
        # rails are NOT dropped here, they just rank lower.
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

        # Steps 7-8: sticky shortcut then cost-based selection
        default_rail: Rail | None = (
            policy_engine.default_rail if policy_engine is not None else None
        )
        winner = _select_winner(
            candidates=candidates,
            sticky_rail=sticky_rail,
            default_rail=default_rail,
        )

        return RoutingChoice(
            candidate=winner,
            fallback_from=prior_rail,
            attempt=attempt,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_INF = float("inf")


def _select_winner(
    candidates: list[tuple[Candidate, PolicyDecision]],
    sticky_rail: Rail | None,
    *,
    default_rail: Rail | None = None,
) -> Candidate:
    """Return the winning candidate.

    Sticky rail wins immediately if it is among the survivors.
    Otherwise sort by: cost (asc) → prefer (yes first) → default_rail (yes first) → list order.
    """
    if sticky_rail is not None:
        for candidate, _decision in candidates:
            if candidate.adapter.rail == sticky_rail:
                return candidate

    def _key(item: tuple[Candidate, PolicyDecision]) -> tuple[float, int, int, int]:
        candidate, decision = item
        rail = candidate.adapter.rail
        q = candidate.quote_envelope_minor_units
        cost_rank = float(q) if q is not None else _INF
        prefer_rank = 0 if rail in decision.prefer else 1
        default_rank = 0 if rail == default_rail else 1
        order_rank = candidates.index(item)
        return (cost_rank, prefer_rank, default_rank, order_rank)

    return min(candidates, key=_key)[0]


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
    envelope_currency: EnvelopeCurrency | None,
    fmv_snapshot: dict[str, Decimal] | None,
) -> int | None:
    """Convert the challenge price to envelope minor units.

    Returns:
        0       — budget enforcement is not active (no envelope_currency); all
                  candidates are cost-equal and fall through to other tiebreakers.
        int > 0 — successful FMV conversion.
        None    — FMV conversion failed; caller ranks this candidate worst on cost.
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
            "FMV conversion failed for %s→%s; candidate will be ranked worst on cost.",
            challenge.price.currency,
            envelope_currency,
        )
        return None
