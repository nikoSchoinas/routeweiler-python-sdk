"""Parser unit tests for MppSptAdapter.parse().

Covers: happy path decoding, currency normalisation to "<iso>-fiat",
recipient / seller-details, payment-method hint, expiry handling,
and all ChallengeParseError / ChallengeExpiredError paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from routewiler.errors import ChallengeExpiredError, ChallengeParseError
from routewiler.normalized import MppSptRailRaw, NormalizedChallenge
from routewiler.rails._mpp_http import b64url_encode, jcs_encode
from routewiler.rails.mpp_spt import MppSptAdapter
from tests.fixtures.mpp_spt_mock_server import (
    MOCK_AMOUNT,
    MOCK_CHARGE_ID,
    MOCK_CURRENCY,
    MOCK_EXPIRES,
    MOCK_RECIPIENT,
    MOCK_REQUEST_B64,
    MOCK_WWW_AUTHENTICATE,
)


def _make_adapter() -> MppSptAdapter:
    return MppSptAdapter([])


def _make_pair(www_auth: str) -> tuple[httpx.Request, httpx.Response]:
    req = httpx.Request("GET", "https://api.example.com/report")
    resp = httpx.Response(402, headers={"WWW-Authenticate": www_auth})
    return req, resp


def _encode_request(data: dict) -> str:  # type: ignore[type-arg]
    return b64url_encode(jcs_encode(data))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parse_happy_path() -> None:
    adapter = _make_adapter()
    req, resp = _make_pair(MOCK_WWW_AUTHENTICATE)
    challenge = adapter.parse(req, resp)

    assert isinstance(challenge, NormalizedChallenge)
    assert challenge.rail == "mpp-spt"
    assert challenge.scheme == "exact"
    assert challenge.nonce == MOCK_CHARGE_ID
    assert challenge.price.amount == int(MOCK_AMOUNT)
    assert challenge.price.currency == f"{MOCK_CURRENCY}-fiat"
    assert challenge.price.human_amount == "$5.00"
    assert challenge.payee.identifier == MOCK_RECIPIENT


def test_parse_raw_is_mpp_spt_rail_raw() -> None:
    adapter = _make_adapter()
    req, resp = _make_pair(MOCK_WWW_AUTHENTICATE)
    challenge = adapter.parse(req, resp)
    assert isinstance(challenge.raw, MppSptRailRaw)
    assert challenge.raw.kind == "mpp-spt"


def test_parse_extra_contains_auth_params_echo() -> None:
    adapter = _make_adapter()
    req, resp = _make_pair(MOCK_WWW_AUTHENTICATE)
    challenge = adapter.parse(req, resp)
    raw = challenge.raw
    assert isinstance(raw, MppSptRailRaw)
    assert raw.extra["iso_currency"] == MOCK_CURRENCY
    assert raw.extra["amount"] == int(MOCK_AMOUNT)
    assert raw.extra["recipient"] == MOCK_RECIPIENT
    assert "auth_params" in raw.extra


def test_parse_seller_details_from_method_details() -> None:
    req_json = {
        "amount": "100",
        "currency": "usd",
        "recipient": "acct_seller",
        "methodDetails": {"sellerDetails": {"account": "acct_seller", "name": "ACME Corp"}},
    }
    www_auth = (
        f'Payment id="cid1", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    challenge = adapter.parse(req, resp)
    raw = challenge.raw
    assert isinstance(raw, MppSptRailRaw)
    assert raw.seller_details == {"account": "acct_seller", "name": "ACME Corp"}


def test_parse_seller_details_fallback_to_recipient() -> None:
    """When methodDetails.sellerDetails is absent, recipient becomes seller account."""
    req_json = {"amount": "100", "currency": "usd", "recipient": "acct_fallback"}
    www_auth = (
        f'Payment id="cid2", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    challenge = adapter.parse(req, resp)
    raw = challenge.raw
    assert isinstance(raw, MppSptRailRaw)
    assert raw.seller_details == {"account": "acct_fallback"}


def test_parse_payment_method_hint() -> None:
    req_json = {
        "amount": "200",
        "currency": "eur",
        "recipient": "acct_eu",
        "methodDetails": {"paymentMethodHint": "pm_card_eu_test"},
    }
    www_auth = (
        f'Payment id="cid3", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    challenge = adapter.parse(req, resp)
    raw = challenge.raw
    assert isinstance(raw, MppSptRailRaw)
    assert raw.payment_method_hint == "pm_card_eu_test"


def test_parse_currency_normalised_to_fiat_suffix() -> None:
    """Currency is normalised to '<iso>-fiat' regardless of case in the wire."""
    req_json = {"amount": "100", "currency": "EUR", "recipient": "acct_x"}
    www_auth = (
        f'Payment id="cid4", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    challenge = adapter.parse(req, resp)
    assert challenge.price.currency == "eur-fiat"


def test_parse_method_card() -> None:
    """method=card is parsed identically to method=stripe."""
    req_json = {"amount": "50", "currency": "gbp", "recipient": "acct_uk"}
    www_auth = (
        f'Payment id="cid5", method="card", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    challenge = adapter.parse(req, resp)
    assert challenge.rail == "mpp-spt"
    assert challenge.price.currency == "gbp-fiat"


def test_parse_default_expiry_when_absent() -> None:
    req_json = {"amount": "100", "currency": "usd", "recipient": "acct_x"}
    www_auth = f'Payment id="cid6", method="stripe", request="{_encode_request(req_json)}"'
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    challenge = adapter.parse(req, resp)
    now = datetime.now(tz=UTC)
    # Expiry should be ~ now + 5 minutes.
    assert challenge.expires_at > now
    assert challenge.expires_at < now + timedelta(minutes=6)


def test_parse_jpy_human_amount() -> None:
    """JPY uses no decimal places."""
    req_json = {"amount": "500", "currency": "jpy", "recipient": "acct_jp"}
    www_auth = (
        f'Payment id="cid7", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    challenge = adapter.parse(req, resp)
    assert challenge.price.human_amount == "¥500"


# ---------------------------------------------------------------------------
# ChallengeParseError paths
# ---------------------------------------------------------------------------


def test_parse_missing_id() -> None:
    www_auth = f'Payment method="stripe", request="{MOCK_REQUEST_B64}", expires="{MOCK_EXPIRES}"'
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeParseError, match="missing 'id'"):
        adapter.parse(req, resp)


def test_parse_missing_request_param() -> None:
    www_auth = f'Payment id="cid8", method="stripe", expires="{MOCK_EXPIRES}"'
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeParseError, match="missing 'request'"):
        adapter.parse(req, resp)


def test_parse_invalid_base64_request() -> None:
    www_auth = 'Payment id="cid9", method="stripe", request="!!!!notbase64!!!!"'
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeParseError, match="failed to decode"):
        adapter.parse(req, resp)


@pytest.mark.parametrize("missing", ["amount", "currency", "recipient"])
def test_parse_missing_required_request_field(missing: str) -> None:
    req_json: dict[str, str] = {"amount": "100", "currency": "usd", "recipient": "acct_x"}
    del req_json[missing]
    www_auth = (
        f'Payment id="cid10", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeParseError, match=f"missing required field '{missing}'"):
        adapter.parse(req, resp)


def test_parse_non_integer_amount() -> None:
    req_json = {"amount": "five", "currency": "usd", "recipient": "acct_x"}
    www_auth = (
        f'Payment id="cid11", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeParseError, match="must be a base-10 integer"):
        adapter.parse(req, resp)


def test_parse_negative_amount() -> None:
    req_json = {"amount": "-1", "currency": "usd", "recipient": "acct_x"}
    www_auth = (
        f'Payment id="cid12", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeParseError, match="non-negative"):
        adapter.parse(req, resp)


def test_parse_invalid_currency_code() -> None:
    req_json = {"amount": "100", "currency": "TOOLONG", "recipient": "acct_x"}
    www_auth = (
        f'Payment id="cid13", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{MOCK_EXPIRES}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeParseError, match="ISO-4217"):
        adapter.parse(req, resp)


def test_parse_invalid_expires_format() -> None:
    req_json = {"amount": "100", "currency": "usd", "recipient": "acct_x"}
    www_auth = (
        f'Payment id="cid14", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="not-a-date"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeParseError, match="could not parse 'expires'"):
        adapter.parse(req, resp)


# ---------------------------------------------------------------------------
# ChallengeExpiredError
# ---------------------------------------------------------------------------


def test_parse_expired_challenge() -> None:
    past = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    req_json = {"amount": "100", "currency": "usd", "recipient": "acct_x"}
    www_auth = (
        f'Payment id="cid15", method="stripe", '
        f'request="{_encode_request(req_json)}", expires="{past}"'
    )
    adapter = _make_adapter()
    req, resp = _make_pair(www_auth)
    with pytest.raises(ChallengeExpiredError):
        adapter.parse(req, resp)
