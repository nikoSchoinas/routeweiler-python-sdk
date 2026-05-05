"""Unit tests for MppTempoAdapter.pay() and confirm()."""

from __future__ import annotations

import json

import httpx
import pytest

from routewiler.errors import (
    MppChargeFailedError,
    MppReceiptVerificationError,
    NoFundingForRailError,
)
from routewiler.funding.tempo import TempoFundingSource
from routewiler.rails._mpp_http import (
    b64url_decode,
    build_payment_receipt,
)
from routewiler.rails._tempo_tx import tx_hash as tempo_tx_hash
from routewiler.rails.mpp_tempo import MppTempoAdapter
from tests.fixtures.fake_tempo import FAKE_SIGNED_TX, FakeTempoSigner
from tests.fixtures.mpp_tempo_mock_server import (
    MOCK_AMOUNT,
    MOCK_CHAIN_ID,
    MOCK_CHARGE_ID,
    MOCK_RECEIPT_HEADER,
    MOCK_TOKEN,
    MOCK_TX_HASH,
    MOCK_WWW_AUTHENTICATE,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_signer(chain_id: int = MOCK_CHAIN_ID) -> FakeTempoSigner:
    return FakeTempoSigner(
        address="0xTestAddress" + "00" * 14,
        chain_id=chain_id,
    )


def _make_fs(chain_id: int = MOCK_CHAIN_ID) -> TempoFundingSource:
    return TempoFundingSource(
        signer=_make_signer(chain_id),
        network="tempo-moderato",
        asset=MOCK_TOKEN,
    )


def _make_challenge(adapter: MppTempoAdapter) -> object:
    request = httpx.Request("GET", "http://example.com/protected")
    response = httpx.Response(
        status_code=402,
        headers={"WWW-Authenticate": MOCK_WWW_AUTHENTICATE},
        request=request,
    )
    return adapter.parse(request, response)


# ---------------------------------------------------------------------------
# pay() — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pay_returns_authorization_header() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)

    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    assert result.header_name == "Authorization"
    assert result.header_value.startswith("Payment ")


@pytest.mark.asyncio
async def test_pay_credential_has_correct_structure() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)

    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    _, token = result.header_value.split(" ", 1)
    cred = json.loads(b64url_decode(token))

    assert cred["challenge"]["id"] == MOCK_CHARGE_ID
    assert cred["payload"]["type"] == "transaction"
    assert cred["payload"]["signature"] == FAKE_SIGNED_TX
    assert cred["source"].startswith(f"did:pkh:eip155:{MOCK_CHAIN_ID}:")


@pytest.mark.asyncio
async def test_pay_source_did_encodes_correct_address() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)

    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    _, token = result.header_value.split(" ", 1)
    cred = json.loads(b64url_decode(token))
    source = cred["source"]
    assert source == f"did:pkh:eip155:{MOCK_CHAIN_ID}:{fs.signer.address}"


@pytest.mark.asyncio
async def test_pay_proof_type_is_txid() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]
    assert result.proof_type == "txid"


@pytest.mark.asyncio
async def test_pay_proof_value_is_tx_hash() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]
    # proof_value is keccak256 of the fake signed tx
    expected = tempo_tx_hash(FAKE_SIGNED_TX)
    assert result.proof_value == expected


@pytest.mark.asyncio
async def test_pay_credential_persists_charge_id() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]
    assert result.credential is not None
    assert result.credential["charge_id"] == MOCK_CHARGE_ID


# ---------------------------------------------------------------------------
# pay() — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pay_no_funding_raises() -> None:
    adapter = MppTempoAdapter([])
    challenge = _make_challenge(adapter)
    with pytest.raises(NoFundingForRailError):
        await adapter.pay(challenge)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_pay_signer_failure_raises_mpp_charge_failed() -> None:
    signer = FakeTempoSigner(chain_id=MOCK_CHAIN_ID, should_fail=True)
    fs = TempoFundingSource(signer=signer, network="tempo-moderato", asset=MOCK_TOKEN)
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    with pytest.raises(MppChargeFailedError):
        await adapter.pay(challenge)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# confirm() — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_parses_receipt_header() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    ok_response = httpx.Response(
        200,
        headers={"Payment-Receipt": MOCK_RECEIPT_HEADER},
        request=httpx.Request("GET", "http://example.com/protected"),
    )
    settlement = await adapter.confirm(result, ok_response)

    assert settlement is not None
    assert settlement.success is True
    assert settlement.network_id == "tempo"


@pytest.mark.asyncio
async def test_confirm_tx_hash_from_receipt() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    ok_response = httpx.Response(
        200,
        headers={"Payment-Receipt": MOCK_RECEIPT_HEADER},
        request=httpx.Request("GET", "http://example.com/protected"),
    )
    settlement = await adapter.confirm(result, ok_response)
    assert settlement is not None
    # The receipt carries MOCK_TX_HASH as reference
    assert settlement.tx_hash == MOCK_TX_HASH


@pytest.mark.asyncio
async def test_confirm_no_receipt_header_returns_info() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    ok_response = httpx.Response(
        200,
        request=httpx.Request("GET", "http://example.com/protected"),
    )
    settlement = await adapter.confirm(result, ok_response)
    assert settlement is not None
    assert settlement.success is True  # 200 response, no receipt → assume success


# ---------------------------------------------------------------------------
# confirm() — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_challenge_id_mismatch_raises() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    wrong_receipt = build_payment_receipt(
        challenge_id="completely_wrong_id",
        method="tempo",
        reference="0x" + "aa" * 32,
        amount=MOCK_AMOUNT,
        currency=MOCK_TOKEN,
        status="success",
    )
    bad_response = httpx.Response(
        200,
        headers={"Payment-Receipt": wrong_receipt},
        request=httpx.Request("GET", "http://example.com/protected"),
    )
    with pytest.raises(MppReceiptVerificationError, match="challengeId"):
        await adapter.confirm(result, bad_response)


@pytest.mark.asyncio
async def test_confirm_wrong_method_raises() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    wrong_receipt = build_payment_receipt(
        challenge_id=MOCK_CHARGE_ID,
        method="stripe",  # wrong method
        reference="0x" + "aa" * 32,
        amount=MOCK_AMOUNT,
        currency=MOCK_TOKEN,
        status="success",
    )
    bad_response = httpx.Response(
        200,
        headers={"Payment-Receipt": wrong_receipt},
        request=httpx.Request("GET", "http://example.com/protected"),
    )
    with pytest.raises(MppReceiptVerificationError, match="method"):
        await adapter.confirm(result, bad_response)


@pytest.mark.asyncio
async def test_confirm_malformed_receipt_raises() -> None:
    fs = _make_fs()
    adapter = MppTempoAdapter([fs])
    challenge = _make_challenge(adapter)
    result = await adapter.pay(challenge)  # type: ignore[arg-type]

    bad_response = httpx.Response(
        200,
        headers={"Payment-Receipt": "!!not_valid_b64!!"},
        request=httpx.Request("GET", "http://example.com/protected"),
    )
    with pytest.raises(MppReceiptVerificationError, match="decode"):
        await adapter.confirm(result, bad_response)


# ---------------------------------------------------------------------------
# Legacy protocol methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_raises_not_implemented() -> None:
    adapter = MppTempoAdapter([])
    challenge = _make_challenge(adapter)
    with pytest.raises(NotImplementedError):
        await adapter.sign(challenge)  # type: ignore[arg-type]


def test_parse_settlement_returns_none() -> None:
    adapter = MppTempoAdapter([])
    response = httpx.Response(200, request=httpx.Request("GET", "http://example.com"))
    assert adapter.parse_settlement(response) is None
