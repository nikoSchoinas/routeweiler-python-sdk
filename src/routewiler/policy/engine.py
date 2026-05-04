"""Policy engine — first-match rule evaluation."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from routewiler.normalized import NormalizedChallenge, Rail, X402RailRaw
from routewiler.policy.dsl import PolicyDocument, RuleMatch


@dataclass(frozen=True)
class PolicyDecision:
    """Result of evaluating a challenge against the policy.

    `prefer` is captured but not yet used to select rails — W7's routing engine
    will consume it. `deny` and `max_per_call_minor_units` are enforced in
    `_auth.py` between the parse and draw phases.
    """

    rule_name: str | None  # None when no rule matched and the default block applies
    deny: bool
    prefer: tuple[Rail, ...]  # () when not specified by the matching rule
    max_per_call_minor_units: int | None
    reason: str | None


class PolicyEngine:
    """Evaluates a NormalizedChallenge against a PolicyDocument (first-match wins)."""

    def __init__(self, document: PolicyDocument) -> None:
        self._doc = document

    def evaluate(self, challenge: NormalizedChallenge) -> PolicyDecision:
        for rule in self._doc.rules:
            if _matches(rule.when, challenge):
                return PolicyDecision(
                    rule_name=rule.name,
                    deny=rule.deny,
                    prefer=tuple(rule.prefer) if rule.prefer else (),
                    max_per_call_minor_units=rule.max_per_call_minor_units,
                    reason=rule.reason,
                )
        # No rule matched → use the default block.
        # `prefer` is left empty so all capable adapters are considered; the
        # router uses `default.rail` as a scoring tiebreaker (§7.1), not as a
        # hard filter.  Hard filtering is only applied when a rule explicitly
        # sets `prefer`.
        return PolicyDecision(
            rule_name=None,
            deny=False,
            prefer=(),
            max_per_call_minor_units=None,
            reason=None,
        )


# ---------------------------------------------------------------------------
# Internal matching helpers
# ---------------------------------------------------------------------------


def _matches(when: RuleMatch, challenge: NormalizedChallenge) -> bool:
    """Return True if all non-None conditions in `when` match `challenge`."""
    # `any` condition — short-circuit OR of nested RuleMatches.
    if when.any is not None:
        if not any(_matches(sub, challenge) for sub in when.any):
            return False

    # `url_matches` — fnmatch glob against the challenge URL.
    if when.url_matches is not None:
        if not fnmatch.fnmatch(challenge.resource.url, when.url_matches):
            return False

    # `scheme` — exact equality.
    if when.scheme is not None:
        if challenge.scheme != when.scheme:
            return False

    # `network` — x402-only: match any entry in the accepts array.
    if when.network is not None:
        if not _network_matches(when.network, challenge):
            return False

    return True


def _network_matches(network: str, challenge: NormalizedChallenge) -> bool:
    """Return True if the challenge carries an x402 accept for the given network."""
    if not isinstance(challenge.raw, X402RailRaw):
        return False
    return any(req.network == network for req in challenge.raw.accepts)
