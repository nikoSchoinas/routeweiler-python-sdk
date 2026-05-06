"""Unit tests for MppTempoAdapter.parse() — challenge parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from routeweiler.errors import ChallengeExpiredError, ChallengeParseError
from routeweiler.funding.tempo import TempoFundingSource
from routeweiler.normalized import MppTempoRailRaw
from routeweiler.rails._mpp_http import b64url_encode, jcs_encode
from routeweiler.rails.mpp_tempo import MppTempoAdapter
from tests.fixtures.fake_tempo import FakeTempoSigner
from tests.fixtures.mpp_tempo_mock_server import (
    MOCK_AMOUNT,
    MOCK_CHAIN_ID,
    MOCK_CHARGE_ID,
    MOCK_EXPIRES,
    MOCK_RECIPIENT,
    MOCK_REQUEST_B64,
    MOCK_TOKEN,
    MOCK_WWW_AUTHENTICATE,
)

_adapter = MppTempoAdapter([])


def _make_402(www_auth: str) -> tuple[httpx.Request, httpx.Response]:
    request = httpx.Request("GET", "http://example.com/protected")
    response = httpx.Response(
        status_code=402,
        headers={"WWW-Authenticate": www_auth},
        request=request,
    )
    return request, response


def _make_request_b64(overrides: dict[str, object] | None = None) -> str:
    req: dict[str, object] = {
        "amount": MOCK_AMOUNT,
        "currency": MOCK_TOKEN,
        "recipient": MOCK_RECIPIENT,
        "methodDetails": {
            "chainId": MOCK_CHAIN_ID,
            "feePayer": False,
            "supportedModes": ["pull"],
        },
    }
    if overrides:
        req.update(overrides)
    return b64url_encode(jcs_encode(req))


def _make_www_auth(
    charge_id: str = MOCK_CHARGE_ID,
    request_b64: str | None = None,
    expires: str = MOCK_EXPIRES,
    extra: str = "",
) -> str:
    rb64 = request_b64 or MOCK_REQUEST_B64
    header = (
        f'Payment id="{charge_id}", '
        f'realm="test.example.com", '
        f'method="tempo", '
        f'request="{rb64}", '
        f'expires="{expires}"'
    )
    if extra:
        header += f", {extra}"
    return header


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parse_happy_path() -> None:
    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)

    assert challenge.rail == "mpp-tempo"
    assert challenge.scheme == "exact"
    assert challenge.nonce == MOCK_CHARGE_ID
    assert challenge.price.amount == int(MOCK_AMOUNT)
    assert MOCK_TOKEN.lower() in challenge.price.currency
    assert challenge.payee.identifier == MOCK_RECIPIENT
    assert isinstance(challenge.raw, MppTempoRailRaw)
    assert challenge.raw.charge_id == MOCK_CHARGE_ID


def test_parse_resource_fields() -> None:
    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)
    assert challenge.resource.method == "GET"
    assert challenge.resource.url == "http://example.com/protected"
    assert challenge.resource.original_status == 402


def test_parse_raw_extra_has_chain_id() -> None:
    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)
    assert isinstance(challenge.raw, MppTempoRailRaw)
    assert challenge.raw.extra["chain_id"] == MOCK_CHAIN_ID


def test_parse_raw_extra_has_request_decoded() -> None:
    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)
    assert isinstance(challenge.raw, MppTempoRailRaw)
    decoded = challenge.raw.extra["request_decoded"]
    assert decoded["amount"] == MOCK_AMOUNT
    assert decoded["currency"] == MOCK_TOKEN


def test_parse_currency_string_ends_with_tip20() -> None:
    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)
    assert challenge.price.currency.endswith("-tip20")


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_parse_expired_raises() -> None:
    past = "2000-01-01T00:00:00Z"
    www_auth = _make_www_auth(expires=past)
    request, response = _make_402(www_auth)
    with pytest.raises(ChallengeExpiredError):
        _adapter.parse(request, response)


def test_parse_future_expires_ok() -> None:
    future = "2099-12-31T23:59:59Z"
    www_auth = _make_www_auth(expires=future)
    request, response = _make_402(www_auth)
    challenge = _adapter.parse(request, response)
    assert challenge.expires_at.year == 2099


def test_parse_no_expires_defaults_to_5_min() -> None:
    rb64 = _make_request_b64()
    header = f'Payment id="cid", method="tempo", request="{rb64}"'
    request, response = _make_402(header)
    challenge = _adapter.parse(request, response)
    assert challenge.expires_at > datetime.now(tz=UTC)
    assert challenge.expires_at < datetime.now(tz=UTC) + timedelta(minutes=6)


# ---------------------------------------------------------------------------
# Missing required fields → ChallengeParseError
# ---------------------------------------------------------------------------


def test_parse_missing_id_raises() -> None:
    rb64 = _make_request_b64()
    header = f'Payment method="tempo", request="{rb64}"'
    request, response = _make_402(header)
    with pytest.raises(ChallengeParseError, match="missing 'id'"):
        _adapter.parse(request, response)


def test_parse_missing_request_raises() -> None:
    header = 'Payment id="cid", method="tempo"'
    request, response = _make_402(header)
    with pytest.raises(ChallengeParseError, match="missing 'request'"):
        _adapter.parse(request, response)


def test_parse_missing_amount_raises() -> None:
    rb64 = _make_request_b64({"amount": None})
    www_auth = _make_www_auth(request_b64=rb64)
    request, response = _make_402(www_auth)
    with pytest.raises(ChallengeParseError):
        _adapter.parse(request, response)


def test_parse_missing_currency_raises() -> None:
    req: dict[str, object] = {
        "amount": MOCK_AMOUNT,
        "recipient": MOCK_RECIPIENT,
        "methodDetails": {"chainId": MOCK_CHAIN_ID, "supportedModes": ["pull"]},
    }
    rb64 = b64url_encode(jcs_encode(req))
    www_auth = _make_www_auth(request_b64=rb64)
    request, response = _make_402(www_auth)
    with pytest.raises(ChallengeParseError, match="currency"):
        _adapter.parse(request, response)


def test_parse_missing_recipient_raises() -> None:
    req: dict[str, object] = {
        "amount": MOCK_AMOUNT,
        "currency": MOCK_TOKEN,
        "methodDetails": {"chainId": MOCK_CHAIN_ID, "supportedModes": ["pull"]},
    }
    rb64 = b64url_encode(jcs_encode(req))
    www_auth = _make_www_auth(request_b64=rb64)
    request, response = _make_402(www_auth)
    with pytest.raises(ChallengeParseError, match="recipient"):
        _adapter.parse(request, response)


def test_parse_bad_b64_raises() -> None:
    header = 'Payment id="cid", method="tempo", request="!not-valid-b64!"'
    request, response = _make_402(header)
    with pytest.raises(ChallengeParseError, match="decode"):
        _adapter.parse(request, response)


def test_parse_bad_amount_type_raises() -> None:
    rb64 = _make_request_b64({"amount": "not_an_int"})
    www_auth = _make_www_auth(request_b64=rb64)
    request, response = _make_402(www_auth)
    with pytest.raises(ChallengeParseError, match="amount"):
        _adapter.parse(request, response)


def test_parse_push_only_mode_raises() -> None:
    req: dict[str, object] = {
        "amount": MOCK_AMOUNT,
        "currency": MOCK_TOKEN,
        "recipient": MOCK_RECIPIENT,
        "methodDetails": {
            "chainId": MOCK_CHAIN_ID,
            "supportedModes": ["push"],  # push-only, pull not supported
        },
    }
    rb64 = b64url_encode(jcs_encode(req))
    www_auth = _make_www_auth(request_b64=rb64)
    request, response = _make_402(www_auth)
    with pytest.raises(ChallengeParseError, match="pull"):
        _adapter.parse(request, response)


# ---------------------------------------------------------------------------
# match_funding
# ---------------------------------------------------------------------------


def test_match_funding_by_chain_id_and_canonical_asset() -> None:
    signer = FakeTempoSigner(chain_id=MOCK_CHAIN_ID)
    fs = TempoFundingSource(signer=signer, network="tempo-moderato", asset="pathusd")

    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)

    adapter_with_funding = MppTempoAdapter([fs])
    match = adapter_with_funding.match_funding(challenge, [fs])
    assert match is fs


def test_match_funding_by_hex_address() -> None:
    signer = FakeTempoSigner(chain_id=MOCK_CHAIN_ID)
    # Pass the contract address directly as asset
    fs = TempoFundingSource(signer=signer, network="tempo-moderato", asset=MOCK_TOKEN)

    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)

    adapter_with_funding = MppTempoAdapter([fs])
    match = adapter_with_funding.match_funding(challenge, [fs])
    assert match is fs


def test_match_funding_chain_id_mismatch_returns_none() -> None:
    signer = FakeTempoSigner(chain_id=1)  # wrong chain
    fs = TempoFundingSource(signer=signer, network="tempo", asset="usdc")

    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)

    match = MppTempoAdapter([fs]).match_funding(challenge, [fs])
    assert match is None


def test_match_funding_no_sources_returns_none() -> None:
    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)

    match = MppTempoAdapter([]).match_funding(challenge, [])
    assert match is None


def test_match_funding_picks_first_match_when_multiple() -> None:
    s1 = FakeTempoSigner(chain_id=MOCK_CHAIN_ID, address="0xFirst" + "00" * 17)
    s2 = FakeTempoSigner(chain_id=MOCK_CHAIN_ID, address="0xSecond" + "00" * 17)
    fs1 = TempoFundingSource(signer=s1, network="tempo-moderato", asset=MOCK_TOKEN)
    fs2 = TempoFundingSource(signer=s2, network="tempo-moderato", asset=MOCK_TOKEN)

    request, response = _make_402(MOCK_WWW_AUTHENTICATE)
    challenge = _adapter.parse(request, response)

    match = MppTempoAdapter([fs1, fs2]).match_funding(challenge, [fs1, fs2])
    assert match is fs1
