"""Tests for policy/dsl.py — Policy model and hash."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from routeweiler.policy.dsl import Policy, PolicyRule, RuleMatch

# ---------------------------------------------------------------------------
# Construction and defaults
# ---------------------------------------------------------------------------


def test_default_policy():
    policy = Policy()
    assert policy.default_rail == "x402"
    assert policy.rules == []


def test_explicit_default_rail():
    policy = Policy(default_rail="l402")
    assert policy.default_rail == "l402"


def test_policy_with_rules():
    policy = Policy(
        default_rail="x402",
        rules=[
            PolicyRule(
                name="privacy-sensitive",
                when=RuleMatch(
                    any=[
                        RuleMatch(url_matches="*.competitorapi.com/*"),
                        RuleMatch(url_matches="*.rival.io/*"),
                    ]
                ),
                prefer=["l402", "mpp-spt"],
                reason="on-chain payment leak unacceptable",
            ),
            PolicyRule(
                name="llm-inference-exact",
                when=RuleMatch(scheme="exact"),
                max_per_call_minor_units=500,
            ),
            PolicyRule(
                name="deny-testnet",
                when=RuleMatch(network="base-sepolia"),
                deny=True,
            ),
        ],
    )
    assert len(policy.rules) == 3
    assert policy.rules[0].name == "privacy-sensitive"
    assert policy.rules[0].prefer == ["l402", "mpp-spt"]
    assert policy.rules[1].max_per_call_minor_units == 500
    assert policy.rules[2].deny is True


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_unknown_field_in_rule_rejected():
    with pytest.raises(ValidationError):
        PolicyRule.model_validate(
            {
                "name": "tagged",
                "when": {"url_matches": "*.example.com"},
                "extra_field": True,
            }
        )


def test_invalid_rail_in_prefer_rejected():
    with pytest.raises(ValidationError):
        PolicyRule(
            name="aliased",
            when=RuleMatch(scheme="exact"),
            prefer=["x402-base-usdc"],  # type: ignore[list-item]
        )


def test_empty_when_block_rejected():
    with pytest.raises(ValidationError):
        PolicyRule(name="empty-when", when=RuleMatch.model_validate({}))


def test_unknown_top_level_field_rejected():
    with pytest.raises(ValidationError):
        Policy.model_validate({"extra_field": True})


# ---------------------------------------------------------------------------
# Hash properties
# ---------------------------------------------------------------------------


def test_hash_format():
    h = Policy().policy_hash
    assert h.startswith("sha256:")
    suffix = h[len("sha256:") :]
    assert len(suffix) == 64
    assert all(c in "0123456789abcdef" for c in suffix)


def test_hash_stable_same_policy():
    policy = Policy()
    assert policy.policy_hash == policy.policy_hash


def test_hash_same_for_equivalent_policies():
    p1 = Policy(default_rail="x402", rules=[])
    p2 = Policy(default_rail="x402")
    assert p1.policy_hash == p2.policy_hash


def test_hash_changes_when_rule_added():
    base = Policy()
    with_rule = Policy(rules=[PolicyRule(name="extra", when=RuleMatch(scheme="exact"), deny=False)])
    assert base.policy_hash != with_rule.policy_hash


def test_hash_changes_when_default_rail_changes():
    p_x402 = Policy(default_rail="x402")
    p_l402 = Policy(default_rail="l402")
    assert p_x402.policy_hash != p_l402.policy_hash
