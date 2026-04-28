"""Tests for NormalizedChallenge and its nested types."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from routewiler.normalized import (
    L402RailRaw,
    MppSptRailRaw,
    MppTempoRailRaw,
    NormalizedChallenge,
    X402RailRaw,
)

NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 4, 27, 12, 5, 0, tzinfo=UTC)


def _base_challenge(raw: dict) -> dict:
    return {
        "rail": "x402",
        "resource": {
            "method": "GET",
            "url": "https://api.example.com/data",
            "urlEncoding": "raw",
            "originalStatus": 402,
        },
        "price": {
            "amount": 1000000,
            "currency": "eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "humanAmount": "0.01 USDC",
        },
        "payee": {"identifier": "0xAbCd"},
        "scheme": "exact",
        "nonce": "abc123",
        "expiresAt": EXPIRES.isoformat(),
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Construction + camelCase aliases
# ---------------------------------------------------------------------------


def test_snake_case_construction():
    c = NormalizedChallenge(
        rail="x402",
        resource={"method": "GET", "url": "https://x.com", "url_encoding": "raw"},
        price={"amount": 100, "currency": "usd-fiat", "human_amount": "1.00 USD"},
        payee={"identifier": "acc_123"},
        scheme="exact",
        nonce="n1",
        expires_at=EXPIRES,
        raw=X402RailRaw(kind="x402", payment_requirements={"facilitator": "coinbase"}),
    )
    assert c.rail == "x402"
    assert c.price.amount == 100


def test_camel_case_json_roundtrip():
    raw_dict = {"kind": "x402", "paymentRequirements": {"facilitator": "coinbase"}}
    data = _base_challenge(raw_dict)
    c = NormalizedChallenge.model_validate(data)
    dumped = c.model_dump(by_alias=True)
    assert dumped["rail"] == "x402"
    assert dumped["resource"]["urlEncoding"] == "raw"
    assert dumped["price"]["humanAmount"] == "0.01 USDC"
    assert dumped["payee"]["identifier"] == "0xAbCd"


def test_payee_metadata_optional():
    data = _base_challenge({"kind": "x402", "paymentRequirements": {}})
    c = NormalizedChallenge.model_validate(data)
    assert c.payee.metadata is None


def test_extra_field_forbidden():
    data = _base_challenge({"kind": "x402", "paymentRequirements": {}})
    data["unknownField"] = "oops"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NormalizedChallenge.model_validate(data)


# ---------------------------------------------------------------------------
# RailRaw discriminated union
# ---------------------------------------------------------------------------


def test_discriminator_x402():
    raw = X402RailRaw(kind="x402", payment_requirements={"foo": "bar"})
    assert isinstance(raw, X402RailRaw)


def test_discriminator_l402():
    data = _base_challenge({"kind": "l402", "macaroon": "mac_abc", "invoice": "lnbc..."})
    data["rail"] = "l402"
    c = NormalizedChallenge.model_validate(data)
    assert isinstance(c.raw, L402RailRaw)
    assert c.raw.macaroon == "mac_abc"


def test_discriminator_mpp_tempo():
    data = _base_challenge(
        {"kind": "mpp-tempo", "chargeId": "ch_123", "settlementNetwork": "tempo"}
    )
    data["rail"] = "mpp-tempo"
    c = NormalizedChallenge.model_validate(data)
    assert isinstance(c.raw, MppTempoRailRaw)
    assert c.raw.charge_id == "ch_123"


def test_discriminator_mpp_spt():
    data = _base_challenge({"kind": "mpp-spt", "sellerDetails": {"account": "acct_xyz"}})
    data["rail"] = "mpp-spt"
    c = NormalizedChallenge.model_validate(data)
    assert isinstance(c.raw, MppSptRailRaw)


def test_invalid_rail_value():
    data = _base_challenge({"kind": "x402", "paymentRequirements": {}})
    data["rail"] = "unknown-rail"
    with pytest.raises(ValidationError):
        NormalizedChallenge.model_validate(data)


def test_invalid_scheme_value():
    data = _base_challenge({"kind": "x402", "paymentRequirements": {}})
    data["scheme"] = "batch"
    with pytest.raises(ValidationError):
        NormalizedChallenge.model_validate(data)
