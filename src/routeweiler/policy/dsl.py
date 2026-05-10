"""Policy YAML DSL — document model, file loader, and canonical hash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, model_validator

from routeweiler._base import RouteweilerModel
from routeweiler.normalized import Rail, Scheme

# ---------------------------------------------------------------------------
# DSL models
# ---------------------------------------------------------------------------


class DefaultBlock(RouteweilerModel):
    """The fallback rail when no rule matches."""

    rail: Rail


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
    prefer: list[Rail] | None = None  # captured; enforced by W7 routing engine
    deny: bool = False
    max_per_call_minor_units: int | None = None
    reason: str | None = None


class PolicyDocument(RouteweilerModel):
    """Top-level policy document. Validated on load; unknown fields are rejected."""

    version: Literal[1]
    default: DefaultBlock
    rules: list[PolicyRule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Built-in default policy
# ---------------------------------------------------------------------------


def default_policy() -> PolicyDocument:
    """Return the built-in policy: prefer x402, no rules."""
    return PolicyDocument(
        version=1,
        default=DefaultBlock(rail="x402"),
        rules=[],
    )


# ---------------------------------------------------------------------------
# Canonical hash
# ---------------------------------------------------------------------------


def compute_policy_hash(doc: PolicyDocument) -> str:
    """Return 'sha256:<64hex>' over the canonical JSON of the policy.

    Hashes the parsed model dump (not the raw YAML bytes) so the hash is
    invariant to comments, whitespace, and YAML serializer differences.
    """
    canonical = json.dumps(
        doc.model_dump(mode="json", by_alias=False),
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# PolicyFile — loads a YAML file and exposes document + hash
# ---------------------------------------------------------------------------


class PolicyFile:
    """Loads and validates a policy YAML file.

    Usage::

        policy = PolicyFile("policy.yaml")
        client = Routeweiler(policy=policy, ...)

    The document is parsed once at construction; the hash is stable for the
    lifetime of the object.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        raw: Any = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        self._document = PolicyDocument.model_validate(raw)
        self._hash = compute_policy_hash(self._document)

    @property
    def document(self) -> PolicyDocument:
        return self._document

    @property
    def policy_hash(self) -> str:
        """SHA-256 of the canonicalized policy. Format: 'sha256:<64hex>'."""
        return self._hash
