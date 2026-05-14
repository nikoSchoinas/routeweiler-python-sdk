"""Policy DSL — class-based policy authoring."""

from __future__ import annotations

import hashlib
import json
from functools import cached_property
from typing import Annotated

from pydantic import Field, model_validator

from routeweiler._base import RouteweilerModel
from routeweiler.normalized import Rail, Scheme

# ---------------------------------------------------------------------------
# DSL models
# ---------------------------------------------------------------------------


class RuleMatch(RouteweilerModel):
    """Condition for a policy rule. All non-None fields combine with AND.
    Use `any` for boolean OR.
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
    """A single first-match policy rule."""

    name: str
    when: RuleMatch
    prefer: list[Rail] | None = None
    deny: bool = False
    max_per_call_minor_units: int | None = None
    reason: str | None = None


class Policy(RouteweilerModel):
    """Routing policy for a Routeweiler client.

    Pass an instance as ``policy`` to ``Routeweiler(...)``::

        from routeweiler import Policy, PolicyRule, RuleMatch, Routeweiler

        policy = Policy(
            default_rail="x402",
            rules=[
                PolicyRule(
                    name="deny-testnet",
                    when=RuleMatch(network="base-sepolia"),
                    deny=True,
                ),
            ],
        )

        async with Routeweiler(funding=[...], policy=policy) as client:
            ...

    Rules are evaluated first-match-wins, top to bottom.
    ``policy_hash`` is a stable SHA-256 fingerprint used in trace events.
    """

    default_rail: Rail = "x402"
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
