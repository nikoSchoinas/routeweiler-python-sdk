"""Policy engine — first-match rule evaluation."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from routeweiler.normalized import NormalizedChallenge, Rail, X402RailRaw
from routeweiler.policy.dsl import Policy, RuleMatch


@dataclass(frozen=True)
class PolicyDecision:
    """Result of evaluating a challenge against the policy.

    `prefer` gives preferred rails a score boost in the routing engine.
    `deny` and `max_per_call_minor_units` are enforced in `_auth.py` between
    the parse and draw phases.
    """

    rule_name: str | None  # None when no rule matched and the default block applies
    deny: bool
    prefer: tuple[Rail, ...]  # () when not specified by the matching rule
    max_per_call_minor_units: int | None
    reason: str | None


def _default_decision() -> PolicyDecision:
    return PolicyDecision(
        rule_name=None,
        deny=False,
        prefer=(),
        max_per_call_minor_units=None,
        reason=None,
    )


class PolicyEngine:
    """Evaluates a NormalizedChallenge against a Policy (first-match wins)."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy

    @property
    def default_rail(self) -> Rail:
        """The ``default_rail`` from the policy — used as a routing tie-breaker."""
        return self._policy.default_rail

    def evaluate(self, challenge: NormalizedChallenge) -> PolicyDecision:
        for rule in self._policy.rules:
            if _matches(rule.when, challenge):
                return PolicyDecision(
                    rule_name=rule.name,
                    deny=rule.deny,
                    prefer=tuple(rule.prefer) if rule.prefer else (),
                    max_per_call_minor_units=rule.max_per_call_minor_units,
                    reason=rule.reason,
                )
        return _default_decision()


# ---------------------------------------------------------------------------
# Internal matching helpers
# ---------------------------------------------------------------------------


def _matches(when: RuleMatch, challenge: NormalizedChallenge) -> bool:
    """Return True if all non-None conditions in `when` match `challenge`."""
    if when.any is not None:
        if not any(_matches(sub, challenge) for sub in when.any):
            return False

    if when.url_matches is not None:
        if not fnmatch.fnmatch(challenge.resource.url, when.url_matches):
            return False

    if when.scheme is not None:
        if challenge.scheme != when.scheme:
            return False

    if when.network is not None:
        if not _network_matches(when.network, challenge):
            return False

    return True


def _network_matches(network: str, challenge: NormalizedChallenge) -> bool:
    """Return True if the challenge carries an x402 accept for the given network."""
    if not isinstance(challenge.raw, X402RailRaw):
        return False
    return any(req.network == network for req in challenge.raw.accepts)
