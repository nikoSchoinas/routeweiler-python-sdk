"""Tests for L402Adapter.parse() — challenge parsing from WWW-Authenticate header."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from routewiler.errors import ChallengeExpiredError, ChallengeParseError
from routewiler.normalized import L402RailRaw
from routewiler.rails.l402 import L402Adapter
from tests.fixtures.l402_mock_server import (
    MOCK_BOLT11,
    MOCK_MACAROON_B64,
    MOCK_PAYMENT_HASH,
    MOCK_WWW_AUTHENTICATE,
    _bytes_to_5bit,
    _int_to_5bit_be,
)

# MOCK_BOLT11 is lnbcrt50000n -> 50000n = 50000 nano-BTC x 100 msat = 5_000_000 msat = 5000 sats
MOCK_PRICE_SATS = 5000


def _make_402(www_auth: str) -> tuple[httpx.Request, httpx.Response]:
    request = httpx.Request("GET", "http://example.com/protected")
    response = httpx.Response(
        status_code=402,
        headers={"WWW-Authenticate": www_auth},
    )
    return request, response


def _build_expired_invoice() -> str:
    """Build a minimal BOLT-11 that is already expired (timestamp 2001, expiry 1s)."""
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    ts_groups = _int_to_5bit_be(1_000_000_000, 7)  # 2001-09-09 — way in the past
    hash_5bit = _bytes_to_5bit(bytes.fromhex(MOCK_PAYMENT_HASH))[:52]
    tag_p = [1, *_int_to_5bit_be(52, 2), *hash_5bit]
    expiry_groups = _int_to_5bit_be(1, 6)  # 1-second expiry; already expired (6 groups)
    tag_x = [6, *_int_to_5bit_be(6, 2), *expiry_groups]
    payload = ts_groups + tag_p + tag_x
    all_groups = payload + [0] * 104 + [0] * 6
    return "lnbcrt50000n1" + "".join(charset[g] for g in all_groups)


@pytest.fixture
def adapter() -> L402Adapter:
    return L402Adapter([])


class TestParseHappyPath:
    def test_rail_is_l402(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        assert adapter.parse(req, resp).rail == "l402"

    def test_price_currency_is_btc_lightning(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        assert adapter.parse(req, resp).price.currency == "btc-lightning"

    def test_price_amount_in_sats(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        assert adapter.parse(req, resp).price.amount == MOCK_PRICE_SATS

    def test_price_human_amount_has_sats_suffix(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        assert "sats" in adapter.parse(req, resp).price.human_amount

    def test_scheme_is_exact(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        assert adapter.parse(req, resp).scheme == "exact"

    def test_nonce_equals_payment_hash(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        assert adapter.parse(req, resp).nonce == MOCK_PAYMENT_HASH

    def test_raw_type_and_macaroon(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        raw = adapter.parse(req, resp).raw
        assert isinstance(raw, L402RailRaw)
        assert raw.macaroon == MOCK_MACAROON_B64

    def test_raw_invoice(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        raw = adapter.parse(req, resp).raw
        assert isinstance(raw, L402RailRaw)
        assert raw.invoice == MOCK_BOLT11

    def test_expires_at_is_in_future(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        assert adapter.parse(req, resp).expires_at > datetime.now(UTC)

    def test_resource_url_and_method(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        challenge = adapter.parse(req, resp)
        assert challenge.resource.url == "http://example.com/protected"
        assert challenge.resource.method == "GET"
        assert challenge.resource.original_status == 402

    def test_lsat_scheme_is_accepted(self, adapter: L402Adapter) -> None:
        www_auth = MOCK_WWW_AUTHENTICATE.replace("L402", "LSAT", 1)
        req, resp = _make_402(www_auth)
        assert adapter.parse(req, resp).rail == "l402"

    def test_unquoted_params_accepted(self, adapter: L402Adapter) -> None:
        www_auth = f"L402 macaroon={MOCK_MACAROON_B64}, invoice={MOCK_BOLT11}"
        req, resp = _make_402(www_auth)
        raw = adapter.parse(req, resp).raw
        assert isinstance(raw, L402RailRaw)
        assert raw.macaroon == MOCK_MACAROON_B64


class TestParseErrors:
    def test_wrong_scheme_raises_challenge_parse_error(self, adapter: L402Adapter) -> None:
        req, resp = _make_402("Bearer token123")
        with pytest.raises(ChallengeParseError, match="Cannot parse WWW-Authenticate"):
            adapter.parse(req, resp)

    def test_missing_invoice_raises(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(f'L402 macaroon="{MOCK_MACAROON_B64}"')
        with pytest.raises(ChallengeParseError):
            adapter.parse(req, resp)

    def test_missing_macaroon_raises(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(f'L402 invoice="{MOCK_BOLT11}"')
        with pytest.raises(ChallengeParseError):
            adapter.parse(req, resp)

    def test_bad_bolt11_raises(self, adapter: L402Adapter) -> None:
        req, resp = _make_402(f'L402 macaroon="{MOCK_MACAROON_B64}", invoice="not-a-bolt11"')
        with pytest.raises(ChallengeParseError, match="BOLT-11"):
            adapter.parse(req, resp)

    def test_expired_invoice_raises_challenge_expired_error(self, adapter: L402Adapter) -> None:
        expired = _build_expired_invoice()
        www_auth = f'L402 macaroon="{MOCK_MACAROON_B64}", invoice="{expired}"'
        req, resp = _make_402(www_auth)
        with pytest.raises(ChallengeExpiredError):
            adapter.parse(req, resp)


class TestPayeeIdentifierFallback:
    """Regression tests for B.3: payee.identifier must never be empty string."""

    def test_invoice_without_pubkey_uses_payment_hash(self, adapter: L402Adapter) -> None:
        # MOCK_BOLT11 has no 'n' tag (no pubkey) — only 'p' (payment_hash) and 'x' (expiry).
        # Before B.3 the identifier would be "" (empty string from `or ""`).
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        challenge = adapter.parse(req, resp)
        assert challenge.payee.identifier != ""
        assert challenge.payee.identifier == MOCK_PAYMENT_HASH

    def test_payee_identifier_equals_nonce_when_no_pubkey(self, adapter: L402Adapter) -> None:
        # The nonce is always payment_hash_hex; payee.identifier should match it
        # when there is no pubkey, so both fields refer to the same invoice anchor.
        req, resp = _make_402(MOCK_WWW_AUTHENTICATE)
        challenge = adapter.parse(req, resp)
        assert challenge.payee.identifier == challenge.nonce
