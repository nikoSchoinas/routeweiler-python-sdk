"""Tests for policy/engine.py — first-match evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from routewiler.normalized import (
    L402RailRaw,
    MppTempoRailRaw,
    NormalizedChallenge,
    Payee,
    Price,
    Resource,
    X402PaymentRequirements,
    X402RailRaw,
)
from routewiler.policy.dsl import PolicyDocument, default_policy
from routewiler.policy.engine import PolicyEngine

# ---------------------------------------------------------------------------
# Challenge builders
# ---------------------------------------------------------------------------

_EXPIRES = datetime.now(UTC) + timedelta(hours=1)


def _x402_challenge(
    url: str = "https://api.example.com/data",
    scheme: str = "exact",
    network: str = "base",
) -> NormalizedChallenge:
    req = X402PaymentRequirements(
        scheme=scheme,  # type: ignore[arg-type]
        network=network,
        max_amount_required="1000",
        resource=url,
        pay_to="0xabc",
        asset="usdc",
    )
    return NormalizedChallenge(
        rail="x402",
        resource=Resource(method="GET", url=url, url_encoding="raw"),
        price=Price(
            amount=1000,
            currency="eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            human_amount="0.001 USDC",
        ),
        payee=Payee(identifier="0xabc"),
        scheme=scheme,  # type: ignore[arg-type]
        nonce="abc123",
        expires_at=_EXPIRES,
        raw=X402RailRaw(kind="x402", accepts=[req]),
    )


def _l402_challenge(url: str = "https://api.example.com/data") -> NormalizedChallenge:
    return NormalizedChallenge(
        rail="l402",
        resource=Resource(method="GET", url=url, url_encoding="raw"),
        price=Price(amount=50_000, currency="btc-lightning", human_amount="50000 sats"),
        payee=Payee(identifier="pubkey_abc"),
        scheme="exact",
        nonce="abc123",
        expires_at=_EXPIRES,
        raw=L402RailRaw(kind="l402", macaroon="mac", invoice="lnbc..."),
    )


def _mpp_stream_challenge(url: str = "https://api.inference.com/chat") -> NormalizedChallenge:
    return NormalizedChallenge(
        rail="mpp-tempo",
        resource=Resource(method="POST", url=url, url_encoding="raw"),
        price=Price(amount=100, currency="usd-fiat", human_amount="$1.00"),
        payee=Payee(identifier="merchant_123"),
        scheme="stream",
        nonce="abc123",
        expires_at=_EXPIRES,
        raw=MppTempoRailRaw(kind="mpp-tempo", charge_id="ch_123", settlement_network="tempo"),
    )


# ---------------------------------------------------------------------------
# Default fallback
# ---------------------------------------------------------------------------


def test_default_when_no_rules_match():
    engine = PolicyEngine(default_policy())
    decision = engine.evaluate(_x402_challenge())
    assert decision.rule_name is None
    assert decision.deny is False
    # Default block sets no hard prefer filter; all capable adapters are eligible.
    # Hard filtering only applies when a rule explicitly sets prefer.
    assert decision.prefer == ()
    assert decision.max_per_call_minor_units is None


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------


def test_url_matches_first_match_wins():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {
                    "name": "first",
                    "when": {"url_matches": "https://api.example.com/*"},
                    "prefer": ["l402"],
                },
                {
                    "name": "second",
                    "when": {"url_matches": "https://api.example.com/*"},
                    "prefer": ["mpp-tempo"],
                },
            ],
        }
    )
    engine = PolicyEngine(doc)
    decision = engine.evaluate(_x402_challenge("https://api.example.com/data"))
    assert decision.rule_name == "first"
    assert decision.prefer == ("l402",)


def test_url_matches_no_match_falls_through():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {"name": "other", "when": {"url_matches": "https://other.com/*"}, "deny": True},
            ],
        }
    )
    engine = PolicyEngine(doc)
    decision = engine.evaluate(_x402_challenge("https://api.example.com/data"))
    assert decision.rule_name is None
    assert decision.deny is False


def test_any_or_short_circuit():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {
                    "name": "privacy",
                    "when": {
                        "any": [
                            {"url_matches": "*.competitorapi.com/*"},
                            {"url_matches": "*/sensitive/*"},
                        ]
                    },
                    "prefer": ["l402"],
                }
            ],
        }
    )
    engine = PolicyEngine(doc)
    # Second branch matches
    assert (
        engine.evaluate(_x402_challenge("https://api.example.com/sensitive/data")).rule_name
        == "privacy"
    )
    # First branch matches
    assert (
        engine.evaluate(_x402_challenge("https://api.competitorapi.com/v1")).rule_name == "privacy"
    )
    # Neither branch matches
    assert engine.evaluate(_x402_challenge("https://api.example.com/data")).rule_name is None


def test_scheme_match_streaming():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {"name": "streaming", "when": {"scheme": "stream"}, "prefer": ["mpp-tempo"]},
            ],
        }
    )
    engine = PolicyEngine(doc)
    assert engine.evaluate(_mpp_stream_challenge()).rule_name == "streaming"
    assert engine.evaluate(_x402_challenge(scheme="exact")).rule_name is None


def test_network_match_x402_only():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {"name": "testnet", "when": {"network": "base-sepolia"}, "deny": True},
            ],
        }
    )
    engine = PolicyEngine(doc)
    # x402 challenge on base-sepolia → matches
    assert engine.evaluate(_x402_challenge(network="base-sepolia")).rule_name == "testnet"
    # x402 challenge on mainnet → no match
    assert engine.evaluate(_x402_challenge(network="base")).rule_name is None
    # L402 challenge → network condition returns False (not x402 raw)
    assert engine.evaluate(_l402_challenge()).rule_name is None


def test_and_conditions_all_must_match():
    """Multiple conditions at the same level combine with AND."""
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {
                    "name": "testnet-exact",
                    "when": {
                        "network": "base-sepolia",
                        "scheme": "exact",
                    },
                    "deny": True,
                }
            ],
        }
    )
    engine = PolicyEngine(doc)
    # Both conditions match
    assert (
        engine.evaluate(_x402_challenge(network="base-sepolia", scheme="exact")).rule_name
        == "testnet-exact"
    )
    # Only network matches
    assert engine.evaluate(_x402_challenge(network="base-sepolia", scheme="upto")).rule_name is None
    # Only scheme matches
    assert engine.evaluate(_x402_challenge(network="base", scheme="exact")).rule_name is None


# ---------------------------------------------------------------------------
# Decision fields
# ---------------------------------------------------------------------------


def test_deny_rule():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {"name": "no-testnet", "when": {"network": "base-sepolia"}, "deny": True},
            ],
        }
    )
    engine = PolicyEngine(doc)
    decision = engine.evaluate(_x402_challenge(network="base-sepolia"))
    assert decision.deny is True
    assert decision.rule_name == "no-testnet"


def test_max_per_call_captured():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {"name": "capped", "when": {"scheme": "stream"}, "max_per_call_minor_units": 500},
            ],
        }
    )
    engine = PolicyEngine(doc)
    decision = engine.evaluate(_mpp_stream_challenge())
    assert decision.max_per_call_minor_units == 500
    assert decision.deny is False


def test_prefer_captured():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {
                    "name": "priv",
                    "when": {"url_matches": "*/sensitive/*"},
                    "prefer": ["l402", "mpp-spt"],
                },
            ],
        }
    )
    engine = PolicyEngine(doc)
    decision = engine.evaluate(_x402_challenge("https://api.example.com/sensitive/q"))
    assert decision.prefer == ("l402", "mpp-spt")


def test_reason_captured():
    doc = PolicyDocument.model_validate(
        {
            "version": 1,
            "default": {"rail": "x402"},
            "rules": [
                {
                    "name": "priv",
                    "when": {"url_matches": "*/sensitive/*"},
                    "deny": True,
                    "reason": "privacy",
                },
            ],
        }
    )
    engine = PolicyEngine(doc)
    decision = engine.evaluate(_x402_challenge("https://api.example.com/sensitive/q"))
    assert decision.reason == "privacy"
