"""Unit tests for MppSptAdapter.pay() and .confirm().

Uses FakeSptCreator to avoid hitting the Stripe API.
Covers: credential structure, usage_limits derivation, proof_type/proof_value,
receipt parsing, receipt cross-checks, and SptCreationError propagation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from routewiler.errors import MppReceiptVerificationError, NoFundingForRailError, SptCreationError
from routewiler.funding.stripe import StripeFundingSource
from routewiler.normalized import MppSptRailRaw, NormalizedChallenge, Payee, Price, Resource
from routewiler.rails._mpp_http import (
    b64url_decode,
    build_payment_receipt,
)
from routewiler.rails.mpp_spt import MppSptAdapter
from tests.fixtures.fake_stripe import FAKE_SPT_ID, FakeSptCreator
from tests.fixtures.mpp_spt_mock_server import (
    MOCK_AMOUNT,
    MOCK_CHARGE_ID,
    MOCK_CURRENCY,
    MOCK_CUSTOMER,
    MOCK_EXPIRES,
    MOCK_PAYMENT_INTENT,
    MOCK_PAYMENT_METHOD,
    MOCK_RECEIPT_HEADER,
    MOCK_RECIPIENT,
    MOCK_REQUEST_B64,
)


def _make_source(*, currency: str = "usd", pm: str = MOCK_PAYMENT_METHOD) -> StripeFundingSource:
    return StripeFundingSource(
        api_key="sk_test_fake",
        customer=MOCK_CUSTOMER,
        payment_method=pm,
        currency=currency,
        spt_creator=FakeSptCreator(),
    )


def _make_challenge(
    *,
    amount: int = int(MOCK_AMOUNT),
    currency: str = MOCK_CURRENCY,
    expires_offset_s: int = 300,
    charge_id: str = MOCK_CHARGE_ID,
    recipient: str = MOCK_RECIPIENT,
) -> NormalizedChallenge:
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=expires_offset_s)
    raw = MppSptRailRaw(
        kind="mpp-spt",
        seller_details={"account": recipient},
        payment_method_hint=None,
        extra={
            "iso_currency": currency,
            "amount": amount,
            "recipient": recipient,
            "auth_params": {
                "id": charge_id,
                "realm": "mock.test",
                "method": "stripe",
                "intent": "charge",
                "request": MOCK_REQUEST_B64,
                "expires": MOCK_EXPIRES,
            },
        },
    )
    return NormalizedChallenge(
        rail="mpp-spt",
        resource=Resource(method="GET", url="https://api.example.com/report", url_encoding="raw"),
        price=Price(
            amount=amount, currency=f"{currency}-fiat", human_amount=f"${amount / 100:.2f}"
        ),
        payee=Payee(identifier=recipient, metadata={"iso_currency": currency}),
        scheme="exact",
        nonce=charge_id,
        expires_at=expires_at,
        raw=raw,
    )


def _make_adapter(source: StripeFundingSource) -> MppSptAdapter:
    return MppSptAdapter([source])


# ---------------------------------------------------------------------------
# pay() — happy path
# ---------------------------------------------------------------------------


async def test_pay_returns_spt_id_as_proof_value() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()

    result = await adapter.pay(challenge)

    assert result.proof_type == "spt_id"
    assert result.proof_value == FAKE_SPT_ID


async def test_pay_header_name_is_authorization() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()

    result = await adapter.pay(challenge)

    assert result.header_name == "Authorization"
    assert result.header_value is not None
    assert result.header_value.startswith("Payment ")


async def test_pay_credential_blob_structure() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()

    result = await adapter.pay(challenge)

    assert result.header_value is not None
    _, token = result.header_value.split(" ", 1)
    raw = b64url_decode(token.strip())
    cred = json.loads(raw)

    assert cred["challenge"]["id"] == MOCK_CHARGE_ID
    assert cred["payload"]["type"] == "shared_payment_granted_token"
    assert cred["payload"]["id"] == FAKE_SPT_ID
    assert cred["source"] == f"stripe:customer:{MOCK_CUSTOMER}"


async def test_pay_usage_limits_derived_from_challenge() -> None:
    fake_creator = FakeSptCreator()
    source_with_fake = StripeFundingSource(
        api_key="sk_test_fake",
        customer=MOCK_CUSTOMER,
        payment_method=MOCK_PAYMENT_METHOD,
        currency="usd",
        spt_creator=fake_creator,
    )
    adapter = MppSptAdapter([source_with_fake])
    challenge = _make_challenge(amount=750)

    await adapter.pay(challenge)

    ul = fake_creator.last_kwargs["usage_limits"]
    assert ul["currency"] == "usd"
    assert ul["max_amount"] == 750
    assert isinstance(ul["expires_at"], int)
    assert ul["expires_at"] > int(datetime.now(tz=UTC).timestamp())


async def test_pay_passes_customer_and_payment_method_to_creator() -> None:
    fake_creator = FakeSptCreator()
    source = StripeFundingSource(
        api_key="sk_test_fake",
        customer="cus_TESTCUSTOMER",
        payment_method="pm_TESTPM",
        currency="usd",
        spt_creator=fake_creator,
    )
    adapter = MppSptAdapter([source])
    challenge = _make_challenge()

    await adapter.pay(challenge)

    assert fake_creator.last_kwargs["customer"] == "cus_TESTCUSTOMER"
    assert fake_creator.last_kwargs["payment_method"] == "pm_TESTPM"


async def test_pay_persisted_credential_contains_spt_id() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()

    result = await adapter.pay(challenge)

    assert result.credential is not None
    assert result.credential["spt_id"] == FAKE_SPT_ID
    assert result.credential["charge_id"] == MOCK_CHARGE_ID
    assert result.credential["currency"] == "usd"
    assert result.credential["amount"] == int(MOCK_AMOUNT)


# ---------------------------------------------------------------------------
# pay() — error paths
# ---------------------------------------------------------------------------


async def test_pay_raises_no_funding_when_currency_mismatch() -> None:
    source = _make_source(currency="eur")  # source is EUR, challenge is USD
    adapter = _make_adapter(source)
    challenge = _make_challenge(currency="usd")

    with pytest.raises(NoFundingForRailError, match="currency='usd'"):
        await adapter.pay(challenge)


async def test_pay_raises_spt_creation_error_on_stripe_failure() -> None:
    stripe_error = RuntimeError("Stripe test error: card declined")
    fake_creator = FakeSptCreator(fail_with=stripe_error)
    source = StripeFundingSource(
        api_key="sk_test_fake",
        customer=MOCK_CUSTOMER,
        payment_method=MOCK_PAYMENT_METHOD,
        currency="usd",
        spt_creator=fake_creator,
    )
    adapter = MppSptAdapter([source])
    challenge = _make_challenge()

    with pytest.raises(SptCreationError, match="card declined"):
        await adapter.pay(challenge)


# ---------------------------------------------------------------------------
# match_funding
# ---------------------------------------------------------------------------


def test_match_funding_picks_currency_matching_source() -> None:
    usd_source = _make_source(currency="usd")
    eur_source = _make_source(currency="eur")
    adapter = MppSptAdapter([eur_source, usd_source])
    challenge = _make_challenge(currency="usd")

    match = adapter.match_funding(challenge, [eur_source, usd_source])
    assert match is usd_source


def test_match_funding_prefers_pm_hint_match() -> None:
    pm_generic = _make_source(currency="usd", pm="pm_generic")
    pm_specific = _make_source(currency="usd", pm="pm_hinted")
    raw = MppSptRailRaw(
        kind="mpp-spt",
        seller_details={},
        payment_method_hint="pm_hinted",
        extra={"iso_currency": "usd"},
    )
    challenge = _make_challenge()
    # Reconstruct with a hint
    challenge_with_hint = NormalizedChallenge(
        rail="mpp-spt",
        resource=challenge.resource,
        price=challenge.price,
        payee=challenge.payee,
        scheme=challenge.scheme,
        nonce=challenge.nonce,
        expires_at=challenge.expires_at,
        raw=raw,
    )
    adapter = MppSptAdapter([pm_generic, pm_specific])
    match = adapter.match_funding(challenge_with_hint, [pm_generic, pm_specific])
    assert match is pm_specific


def test_match_funding_returns_none_when_no_sources() -> None:
    adapter = MppSptAdapter([])
    challenge = _make_challenge()
    assert adapter.match_funding(challenge, []) is None


# ---------------------------------------------------------------------------
# confirm() — happy path
# ---------------------------------------------------------------------------


async def test_confirm_success_parses_receipt() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()
    result = await adapter.pay(challenge)

    resp = httpx.Response(200, headers={"Payment-Receipt": MOCK_RECEIPT_HEADER})
    settlement = await adapter.confirm(result, resp)

    assert settlement is not None
    assert settlement.success is True
    assert settlement.tx_hash == MOCK_PAYMENT_INTENT
    assert settlement.network_id == "stripe"
    assert settlement.amount_paid == int(MOCK_AMOUNT)


async def test_confirm_no_receipt_header_returns_minimal_settlement() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()
    result = await adapter.pay(challenge)

    resp = httpx.Response(200)
    settlement = await adapter.confirm(result, resp)

    assert settlement is not None
    assert settlement.success is True
    assert settlement.tx_hash == FAKE_SPT_ID  # falls back to proof_value
    assert settlement.network_id == "stripe"
    assert settlement.amount_paid is None


async def test_confirm_method_card_accepted() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()
    result = await adapter.pay(challenge)

    card_receipt = build_payment_receipt(
        challenge_id=MOCK_CHARGE_ID,
        method="card",
        reference=MOCK_PAYMENT_INTENT,
        amount=MOCK_AMOUNT,
        currency=MOCK_CURRENCY,
        status="success",
    )
    resp = httpx.Response(200, headers={"Payment-Receipt": card_receipt})
    settlement = await adapter.confirm(result, resp)

    assert settlement is not None
    assert settlement.success is True


# ---------------------------------------------------------------------------
# confirm() — error paths
# ---------------------------------------------------------------------------


async def test_confirm_rejects_wrong_challenge_id() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()
    result = await adapter.pay(challenge)

    wrong_receipt = build_payment_receipt(
        challenge_id="WRONG_ID",
        method="stripe",
        reference=MOCK_PAYMENT_INTENT,
        amount=MOCK_AMOUNT,
        currency=MOCK_CURRENCY,
        status="success",
    )
    resp = httpx.Response(200, headers={"Payment-Receipt": wrong_receipt})
    with pytest.raises(MppReceiptVerificationError, match="challengeId"):
        await adapter.confirm(result, resp)


async def test_confirm_rejects_method_tempo() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()
    result = await adapter.pay(challenge)

    tempo_receipt = build_payment_receipt(
        challenge_id=MOCK_CHARGE_ID,
        method="tempo",
        reference="tx_fake",
        amount=MOCK_AMOUNT,
        currency=MOCK_CURRENCY,
        status="success",
    )
    resp = httpx.Response(200, headers={"Payment-Receipt": tempo_receipt})
    with pytest.raises(MppReceiptVerificationError, match="method"):
        await adapter.confirm(result, resp)


async def test_confirm_rejects_malformed_receipt() -> None:
    source = _make_source()
    adapter = _make_adapter(source)
    challenge = _make_challenge()
    result = await adapter.pay(challenge)

    resp = httpx.Response(200, headers={"Payment-Receipt": "not-valid-base64!!!"})
    with pytest.raises(MppReceiptVerificationError, match="decode"):
        await adapter.confirm(result, resp)
