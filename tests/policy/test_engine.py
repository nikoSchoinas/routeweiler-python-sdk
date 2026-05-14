"""Tests for policy/engine.py — first-match evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from routeweiler.normalized import (
    L402RailRaw,
    MppTempoRailRaw,
    NormalizedChallenge,
    Payee,
    Price,
    Resource,
    X402PaymentRequirements,
    X402RailRaw,
)
from routeweiler.policy.dsl import Policy, PolicyRule, RuleMatch
from routeweiler.policy.engine import PolicyEngine

# ---------------------------------------------------------------------------
# Challenge builders
# ---------------------------------------------------------------------------

_EXPIRES = datetime.now(UTC) + timedelta(hours=1)


def _x402_challenge(
    url: str = "https://api.example.com/data",
    network: str = "base",
) -> NormalizedChallenge:
    req = X402PaymentRequirements(
        scheme="exact",
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
        scheme="exact",
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


def _mpp_exact_challenge(url: str = "https://api.inference.com/chat") -> NormalizedChallenge:
    return NormalizedChallenge(
        rail="mpp-tempo",
        resource=Resource(method="POST", url=url, url_encoding="raw"),
        price=Price(amount=100, currency="usd-fiat", human_amount="$1.00"),
        payee=Payee(identifier="merchant_123"),
        scheme="exact",
        nonce="abc123",
        expires_at=_EXPIRES,
        raw=MppTempoRailRaw(kind="mpp-tempo", charge_id="ch_123"),
    )


# ---------------------------------------------------------------------------
# Default fallback
# ---------------------------------------------------------------------------


def test_default_when_no_rules_match():
    engine = PolicyEngine(Policy())
    decision = engine.evaluate(_x402_challenge())
    assert decision.rule_name is None
    assert decision.deny is False
    assert decision.prefer == ()
    assert decision.max_per_call_minor_units is None


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------


def test_url_matches_first_match_wins():
    policy = Policy(
        rules=[
            PolicyRule(
                name="first",
                when=RuleMatch(url_matches="https://api.example.com/*"),
                prefer=["l402"],
            ),
            PolicyRule(
                name="second",
                when=RuleMatch(url_matches="https://api.example.com/*"),
                prefer=["mpp-tempo"],
            ),
        ]
    )
    engine = PolicyEngine(policy)
    decision = engine.evaluate(_x402_challenge("https://api.example.com/data"))
    assert decision.rule_name == "first"
    assert decision.prefer == ("l402",)


def test_url_matches_no_match_falls_through():
    policy = Policy(
        rules=[
            PolicyRule(
                name="other",
                when=RuleMatch(url_matches="https://other.com/*"),
                deny=True,
            ),
        ]
    )
    engine = PolicyEngine(policy)
    decision = engine.evaluate(_x402_challenge("https://api.example.com/data"))
    assert decision.rule_name is None
    assert decision.deny is False


def test_any_or_short_circuit():
    policy = Policy(
        rules=[
            PolicyRule(
                name="privacy",
                when=RuleMatch(
                    any=[
                        RuleMatch(url_matches="*.competitorapi.com/*"),
                        RuleMatch(url_matches="*/sensitive/*"),
                    ]
                ),
                prefer=["l402"],
            )
        ]
    )
    engine = PolicyEngine(policy)
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


def test_scheme_match_exact():
    policy = Policy(
        rules=[
            PolicyRule(name="mpp-exact", when=RuleMatch(scheme="exact"), prefer=["mpp-tempo"]),
        ]
    )
    engine = PolicyEngine(policy)
    assert engine.evaluate(_mpp_exact_challenge()).rule_name == "mpp-exact"
    assert engine.evaluate(_x402_challenge()).rule_name == "mpp-exact"


def test_network_match_x402_only():
    policy = Policy(
        rules=[
            PolicyRule(name="testnet", when=RuleMatch(network="base-sepolia"), deny=True),
        ]
    )
    engine = PolicyEngine(policy)
    assert engine.evaluate(_x402_challenge(network="base-sepolia")).rule_name == "testnet"
    assert engine.evaluate(_x402_challenge(network="base")).rule_name is None
    assert engine.evaluate(_l402_challenge()).rule_name is None


def test_and_conditions_all_must_match():
    """Multiple conditions at the same level combine with AND."""
    policy = Policy(
        rules=[
            PolicyRule(
                name="testnet-exact",
                when=RuleMatch(network="base-sepolia", scheme="exact"),
                deny=True,
            )
        ]
    )
    engine = PolicyEngine(policy)
    assert engine.evaluate(_x402_challenge(network="base-sepolia")).rule_name == "testnet-exact"
    assert engine.evaluate(_x402_challenge(network="base")).rule_name is None


# ---------------------------------------------------------------------------
# Decision fields
# ---------------------------------------------------------------------------


def test_deny_rule():
    policy = Policy(
        rules=[
            PolicyRule(name="no-testnet", when=RuleMatch(network="base-sepolia"), deny=True),
        ]
    )
    engine = PolicyEngine(policy)
    decision = engine.evaluate(_x402_challenge(network="base-sepolia"))
    assert decision.deny is True
    assert decision.rule_name == "no-testnet"


def test_max_per_call_captured():
    policy = Policy(
        rules=[
            PolicyRule(
                name="capped",
                when=RuleMatch(scheme="exact"),
                max_per_call_minor_units=500,
            ),
        ]
    )
    engine = PolicyEngine(policy)
    decision = engine.evaluate(_mpp_exact_challenge())
    assert decision.max_per_call_minor_units == 500
    assert decision.deny is False


def test_prefer_captured():
    policy = Policy(
        rules=[
            PolicyRule(
                name="priv",
                when=RuleMatch(url_matches="*/sensitive/*"),
                prefer=["l402", "mpp-spt"],
            ),
        ]
    )
    engine = PolicyEngine(policy)
    decision = engine.evaluate(_x402_challenge("https://api.example.com/sensitive/q"))
    assert decision.prefer == ("l402", "mpp-spt")


def test_reason_captured():
    policy = Policy(
        rules=[
            PolicyRule(
                name="priv",
                when=RuleMatch(url_matches="*/sensitive/*"),
                deny=True,
                reason="privacy",
            ),
        ]
    )
    engine = PolicyEngine(policy)
    decision = engine.evaluate(_x402_challenge("https://api.example.com/sensitive/q"))
    assert decision.reason == "privacy"
