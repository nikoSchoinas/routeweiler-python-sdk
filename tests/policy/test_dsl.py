"""Tests for policy/dsl.py — document model, loader, and hash."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from routeweiler.policy.dsl import (
    PolicyDocument,
    PolicyFile,
    compute_policy_hash,
    default_policy,
)

_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def test_load_minimal_policy():
    pf = PolicyFile(_FIXTURES / "policy_minimal.yaml")
    assert pf.document.version == 1
    assert pf.document.default.rail == "x402"
    assert pf.document.rules == []


def test_load_three_rules_policy():
    pf = PolicyFile(_FIXTURES / "policy_three_rules.yaml")
    doc = pf.document
    assert doc.version == 1
    assert doc.default.rail == "x402"
    assert len(doc.rules) == 3
    names = [r.name for r in doc.rules]
    assert names == ["privacy-sensitive", "llm-inference-streaming", "deny-testnet"]

    privacy_rule = doc.rules[0]
    assert privacy_rule.prefer == ["l402", "mpp-spt"]
    assert privacy_rule.reason == "on-chain payment leak unacceptable"
    assert privacy_rule.when.any is not None

    stream_rule = doc.rules[1]
    assert stream_rule.when.scheme == "stream"
    assert stream_rule.max_per_call_minor_units == 500

    deny_rule = doc.rules[2]
    assert deny_rule.when.network == "base-sepolia"
    assert deny_rule.deny is True


def test_unknown_field_in_rule_rejected():
    """tag: is not supported at W6 — should fail validation."""
    raw = {
        "version": 1,
        "default": {"rail": "x402"},
        "rules": [
            {
                "name": "tagged",
                "when": {"tag": "sensitive"},
            }
        ],
    }
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(raw)


def test_unknown_top_level_field_rejected():
    raw = {"version": 1, "default": {"rail": "x402"}, "extra_field": True}
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(raw)


def test_invalid_rail_in_prefer_rejected():
    """x402-base-usdc-style aliases are W7 work; not valid at W6."""
    raw = {
        "version": 1,
        "default": {"rail": "x402"},
        "rules": [
            {
                "name": "aliased",
                "when": {"scheme": "exact"},
                "prefer": ["x402-base-usdc"],
            }
        ],
    }
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(raw)


def test_empty_when_block_rejected():
    raw = {
        "version": 1,
        "default": {"rail": "x402"},
        "rules": [{"name": "empty-when", "when": {}}],
    }
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(raw)


def test_default_policy_structure():
    doc = default_policy()
    assert doc.version == 1
    assert doc.default.rail == "x402"
    assert doc.rules == []


# ---------------------------------------------------------------------------
# Hash properties
# ---------------------------------------------------------------------------


def test_hash_format():
    h = compute_policy_hash(default_policy())
    assert h.startswith("sha256:")
    suffix = h[len("sha256:") :]
    assert len(suffix) == 64
    assert all(c in "0123456789abcdef" for c in suffix)


def test_hash_stable_same_policy():
    doc = default_policy()
    assert compute_policy_hash(doc) == compute_policy_hash(doc)


def test_hash_stable_across_whitespace():
    """Same semantic content, different YAML whitespace → same hash."""
    h1 = PolicyFile(_FIXTURES / "policy_three_rules.yaml").policy_hash
    h2 = PolicyFile(_FIXTURES / "policy_whitespace_variant.yaml").policy_hash
    assert h1 == h2


def test_hash_changes_when_rule_added():
    base = default_policy()
    raw = base.model_dump(mode="python")
    raw["rules"].append(
        {
            "name": "extra",
            "when": {"scheme": "exact"},
            "deny": False,
        }
    )
    modified = PolicyDocument.model_validate(raw)
    assert compute_policy_hash(base) != compute_policy_hash(modified)


def test_hash_changes_when_default_rail_changes():
    doc_x402 = PolicyDocument.model_validate({"version": 1, "default": {"rail": "x402"}})
    doc_l402 = PolicyDocument.model_validate({"version": 1, "default": {"rail": "l402"}})
    assert compute_policy_hash(doc_x402) != compute_policy_hash(doc_l402)


def test_policy_file_hash_equals_compute_policy_hash():
    pf = PolicyFile(_FIXTURES / "policy_three_rules.yaml")
    assert pf.policy_hash == compute_policy_hash(pf.document)
