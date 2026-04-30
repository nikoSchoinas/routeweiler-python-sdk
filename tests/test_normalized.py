"""Tests for NormalizedChallenge and its nested types."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from routewiler.normalized import (
    L402RailRaw,
    MppSptRailRaw,
    MppTempoRailRaw,
    NormalizedChallenge,
    X402PaymentRequirements,
    X402RailRaw,
)

NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 4, 27, 12, 5, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Minimal fixtures for X402PaymentRequirements
# ---------------------------------------------------------------------------

_PR_SNAKE = dict(
    scheme="exact",
    network="base",
    max_amount_required="1000",
    pay_to="0x1234567890123456789012345678901234567890",
    asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    resource="https://api.example.com/data",
)

_PR_CAMEL = {
    "scheme": "exact",
    "network": "base",
    "maxAmountRequired": "1000",
    "payTo": "0x1234567890123456789012345678901234567890",
    "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "resource": "https://api.example.com/data",
}

_X402_RAW_CAMEL = {"kind": "x402", "accepts": [_PR_CAMEL]}


def _base_challenge(raw: dict) -> dict:  # type: ignore[type-arg]
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


def test_snake_case_construction() -> None:
    c = NormalizedChallenge(
        rail="x402",
        resource={"method": "GET", "url": "https://x.com", "url_encoding": "raw"},
        price={"amount": 100, "currency": "usd-fiat", "human_amount": "1.00 USD"},
        payee={"identifier": "acc_123"},
        scheme="exact",
        nonce="n1",
        expires_at=EXPIRES,
        raw=X402RailRaw(kind="x402", accepts=[X402PaymentRequirements(**_PR_SNAKE)]),
    )
    assert c.rail == "x402"
    assert c.price.amount == 100


def test_camel_case_json_roundtrip() -> None:
    data = _base_challenge(_X402_RAW_CAMEL)
    c = NormalizedChallenge.model_validate(data)
    dumped = c.model_dump(by_alias=True)
    assert dumped["rail"] == "x402"
    assert dumped["resource"]["urlEncoding"] == "raw"
    assert dumped["price"]["humanAmount"] == "0.01 USDC"
    assert dumped["payee"]["identifier"] == "0xAbCd"


def test_payee_metadata_optional() -> None:
    data = _base_challenge(_X402_RAW_CAMEL)
    c = NormalizedChallenge.model_validate(data)
    assert c.payee.metadata is None


def test_extra_field_forbidden() -> None:
    data = _base_challenge(_X402_RAW_CAMEL)
    data["unknownField"] = "oops"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NormalizedChallenge.model_validate(data)


# ---------------------------------------------------------------------------
# X402PaymentRequirements
# ---------------------------------------------------------------------------


def test_x402_pr_snake_construction() -> None:
    pr = X402PaymentRequirements(**_PR_SNAKE)
    assert pr.scheme == "exact"
    assert pr.network == "base"
    assert pr.max_amount_required == "1000"
    assert pr.pay_to == "0x1234567890123456789012345678901234567890"


def test_x402_pr_camel_roundtrip() -> None:
    pr = X402PaymentRequirements.model_validate(_PR_CAMEL)
    dumped = pr.model_dump(by_alias=True)
    assert dumped["maxAmountRequired"] == "1000"
    assert dumped["payTo"] == "0x1234567890123456789012345678901234567890"


def test_x402_pr_defaults() -> None:
    pr = X402PaymentRequirements(**_PR_SNAKE)
    assert pr.description == ""
    assert pr.mime_type == "application/json"
    assert pr.max_timeout_seconds == 60
    assert pr.extra == {}
    assert pr.output_schema is None


def test_x402_pr_unknown_fields_ignored() -> None:
    pr = X402PaymentRequirements.model_validate({**_PR_CAMEL, "futureField": "value"})
    assert pr.scheme == "exact"


def test_x402_pr_extra_dict_captured() -> None:
    pr = X402PaymentRequirements.model_validate(
        {**_PR_CAMEL, "extra": {"nonce": "0xabc", "validBefore": 9999999999}}
    )
    assert pr.extra["nonce"] == "0xabc"
    assert pr.extra["validBefore"] == 9999999999


def test_x402_pr_invalid_scheme() -> None:
    with pytest.raises(ValidationError):
        X402PaymentRequirements.model_validate({**_PR_CAMEL, "scheme": "batch"})


# ---------------------------------------------------------------------------
# RailRaw discriminated union
# ---------------------------------------------------------------------------


def test_discriminator_x402() -> None:
    raw = X402RailRaw(kind="x402", accepts=[X402PaymentRequirements(**_PR_SNAKE)])
    assert isinstance(raw, X402RailRaw)
    assert len(raw.accepts) == 1
    assert raw.accepts[0].network == "base"


def test_discriminator_x402_camel() -> None:
    data = _base_challenge(_X402_RAW_CAMEL)
    c = NormalizedChallenge.model_validate(data)
    assert isinstance(c.raw, X402RailRaw)
    assert c.raw.accepts[0].max_amount_required == "1000"


def test_discriminator_l402() -> None:
    data = _base_challenge({"kind": "l402", "macaroon": "mac_abc", "invoice": "lnbc..."})
    data["rail"] = "l402"
    c = NormalizedChallenge.model_validate(data)
    assert isinstance(c.raw, L402RailRaw)
    assert c.raw.macaroon == "mac_abc"


def test_discriminator_mpp_tempo() -> None:
    data = _base_challenge(
        {"kind": "mpp-tempo", "chargeId": "ch_123", "settlementNetwork": "tempo"}
    )
    data["rail"] = "mpp-tempo"
    c = NormalizedChallenge.model_validate(data)
    assert isinstance(c.raw, MppTempoRailRaw)
    assert c.raw.charge_id == "ch_123"


def test_discriminator_mpp_spt() -> None:
    data = _base_challenge({"kind": "mpp-spt", "sellerDetails": {"account": "acct_xyz"}})
    data["rail"] = "mpp-spt"
    c = NormalizedChallenge.model_validate(data)
    assert isinstance(c.raw, MppSptRailRaw)


def test_invalid_rail_value() -> None:
    data = _base_challenge(_X402_RAW_CAMEL)
    data["rail"] = "unknown-rail"
    with pytest.raises(ValidationError):
        NormalizedChallenge.model_validate(data)


def test_invalid_scheme_value() -> None:
    data = _base_challenge(_X402_RAW_CAMEL)
    data["scheme"] = "batch"
    with pytest.raises(ValidationError):
        NormalizedChallenge.model_validate(data)
