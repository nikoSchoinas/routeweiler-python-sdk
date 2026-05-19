"""Policy DSL — class-based policy authoring."""

from __future__ import annotations

import hashlib
import json
from functools import cached_property
from typing import Annotated

from pydantic import Field, model_validator

from routeweiler._base import RouteweilerModel
from routeweiler.budgets.schema import EnvelopeCurrency
from routeweiler.normalized import Rail, Scheme

# ---------------------------------------------------------------------------
# DSL models
# ---------------------------------------------------------------------------


class RuleMatch(RouteweilerModel):
    """Condition predicate for a ``PolicyRule``.

    All non-``None`` fields are combined with AND.  Use ``any`` for an OR of
    sub-conditions::

        # Match any x402 request on the "base" network:
        RuleMatch(network="base")

        # Match requests to *.example.com that use the "exact" scheme:
        RuleMatch(url_matches="*.example.com", scheme="exact")

        # OR: either of the above:
        RuleMatch(any=[RuleMatch(network="base"), RuleMatch(url_matches="*.example.com")])

    At least one condition must be set; ``ValueError`` is raised otherwise.
    """

    url_matches: str | None = None  # fnmatch glob against challenge.resource.url
    scheme: Scheme | None = None  # exact match on NormalizedChallenge.scheme
    network: str | None = None  # x402-only: match any of accepts[i].network
    any: Annotated[list[RuleMatch] | None, Field(default=None)]  # short-circuit OR

    @model_validator(mode="after")
    def _at_least_one_condition(self) -> RuleMatch:
        if (
            self.url_matches is None
            and self.scheme is None
            and self.network is None
            and (self.any is None or len(self.any) == 0)
        ):
            raise ValueError(
                "A 'when' block must have at least one condition "
                "(url_matches, scheme, network, or any)."
            )
        return self


# Resolve the forward reference for the recursive `any` field.
RuleMatch.model_rebuild()


class PolicyRule(RouteweilerModel):
    """A single entry in a ``Policy.rules`` list (first-match wins).

    When the ``when`` predicate matches a challenge, the rule's action fields
    (``deny``, ``prefer``, ``max_per_call_minor_units``) are applied.  Only the
    first matching rule takes effect — later rules are not evaluated::

        PolicyRule(
            name="cap-per-call",
            when=RuleMatch(url_matches="*"),
            max_per_call_minor_units=500,  # 5.00 USD in cents
        )

    Attributes:
        name:                    Human-readable label; appears in trace events and error messages.
        when:                    Condition that activates this rule.
        prefer:                  Rails to prefer (score-boosted) when this rule fires.
        deny:                    If ``True``, raise ``PolicyDeniedError`` without paying.
        max_per_call_minor_units: Reject challenges whose amount exceeds this limit
                                  (in the reference currency's minor units).
        reason:                  Optional human-readable reason included in ``PolicyDeniedError``.
    """

    name: str
    when: RuleMatch
    prefer: list[Rail] | None = None  # rails to score-boost
    deny: bool = False  # raise PolicyDeniedError when True
    max_per_call_minor_units: int | None = None  # per-call spend cap
    reason: str | None = None  # included in PolicyDeniedError.reason


class Policy(RouteweilerModel):
    """Routing policy for a Routeweiler client.

    Pass an instance as ``policy`` to ``Routeweiler(...)``::

        from routeweiler import Policy, PolicyRule, RuleMatch, Routeweiler

        policy = Policy(
            default_rail="x402",
            currency="usd",
            rules=[
                PolicyRule(
                    name="cap-per-call",
                    when=RuleMatch(url_matches="*"),
                    max_per_call_minor_units=500,
                ),
            ],
        )

        async with Routeweiler(funding=[...], policy=policy) as client:
            ...

    Rules are evaluated first-match-wins, top to bottom.
    ``policy_hash`` is a stable SHA-256 fingerprint used in trace events.

    ``currency`` declares the reference currency for ``max_per_call_minor_units``
    rules when no ``budget_envelope`` is configured on the client.  When a
    ``budget_envelope`` is set, its ``cap_currency`` takes precedence.  If any
    rule declares ``max_per_call_minor_units`` and neither ``currency`` nor a
    ``budget_envelope`` is provided, ``Routeweiler`` raises ``ValueError`` at
    construction time.
    """

    default_rail: Rail = "x402"
    currency: EnvelopeCurrency | None = None
    rules: list[PolicyRule] = Field(default_factory=list)

    @cached_property
    def policy_hash(self) -> str:
        """SHA-256 fingerprint of this policy. Format: ``'sha256:<64hex>'``."""
        canonical = json.dumps(
            self.model_dump(mode="json", by_alias=False),
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
