"""Unit tests for _mpp_http.py — pure wire-format helpers."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from routewiler.rails._mpp_http import (
    b64url_decode,
    b64url_encode,
    build_authorization_header,
    build_payment_receipt,
    decode_request_param,
    encode_credential,
    jcs_encode,
    parse_payment_challenge,
    parse_payment_receipt,
)

# ---------------------------------------------------------------------------
# Base64url helpers
# ---------------------------------------------------------------------------


def test_b64url_encode_no_padding() -> None:
    result = b64url_encode(b"hello world")
    assert "=" not in result
    assert result == "aGVsbG8gd29ybGQ"


def test_b64url_roundtrip() -> None:
    data = b"\x00\xff\xab\xcd\xef"
    assert b64url_decode(b64url_encode(data)) == data


def test_b64url_decode_with_padding() -> None:
    assert b64url_decode("aGVsbG8") == b"hello"


# ---------------------------------------------------------------------------
# JCS canonicalisation
# ---------------------------------------------------------------------------


def test_jcs_encode_sorts_keys() -> None:
    obj = {"z": 1, "a": 2, "m": 3}
    canonical = jcs_encode(obj)
    assert canonical == b'{"a":2,"m":3,"z":1}'


def test_jcs_encode_deterministic_regardless_of_insertion_order() -> None:
    a = jcs_encode({"b": "two", "a": "one"})
    b = jcs_encode({"a": "one", "b": "two"})
    assert a == b


def test_jcs_encode_nested() -> None:
    obj = {"outer": {"inner": [1, 2, 3]}}
    result = jcs_encode(obj)
    assert result == b'{"outer":{"inner":[1,2,3]}}'


def test_jcs_encode_special_chars_escaped() -> None:
    obj = {"k": 'quote"tab\tnewline\n'}
    raw = jcs_encode(obj)
    decoded = json.loads(raw)
    assert decoded["k"] == 'quote"tab\tnewline\n'


def test_jcs_encode_bool_and_null() -> None:
    assert jcs_encode({"a": True, "b": False, "c": None}) == b'{"a":true,"b":false,"c":null}'


# ---------------------------------------------------------------------------
# parse_payment_challenge
# ---------------------------------------------------------------------------


def test_parse_challenge_basic() -> None:
    header = 'Payment id="abc123", method="tempo", realm="example.com"'
    params = parse_payment_challenge(header)
    assert params["id"] == "abc123"
    assert params["method"] == "tempo"
    assert params["realm"] == "example.com"


def test_parse_challenge_unquoted_token() -> None:
    header = "Payment id=abc123 method=tempo"
    params = parse_payment_challenge(header)
    assert params["id"] == "abc123"
    assert params["method"] == "tempo"


def test_parse_challenge_case_insensitive_keys() -> None:
    header = 'Payment Id="abc", Method="tempo"'
    params = parse_payment_challenge(header)
    assert params["id"] == "abc"
    assert params["method"] == "tempo"


def test_parse_challenge_quoted_with_escapes() -> None:
    header = r'Payment realm="example \"quoted\""'
    params = parse_payment_challenge(header)
    assert params["realm"] == 'example "quoted"'


def test_parse_challenge_strips_payment_prefix() -> None:
    header = 'Payment id="x"'
    params = parse_payment_challenge(header)
    assert "id" in params


def test_parse_challenge_with_b64url_request() -> None:
    req = {"amount": "100", "currency": "0xabc", "recipient": "0xdef"}
    b64 = b64url_encode(jcs_encode(req))
    header = f'Payment id="cid", method="tempo", request="{b64}"'
    params = parse_payment_challenge(header)
    assert params["request"] == b64


# ---------------------------------------------------------------------------
# decode_request_param
# ---------------------------------------------------------------------------


def test_decode_request_param_roundtrip() -> None:
    req = {"amount": "500", "currency": "0xtoken", "recipient": "0xaddr"}
    b64 = b64url_encode(jcs_encode(req))
    decoded = decode_request_param(b64)
    assert decoded["amount"] == "500"
    assert decoded["currency"] == "0xtoken"


def test_decode_request_param_bad_b64() -> None:
    with pytest.raises(ValueError, match="decode failed"):
        decode_request_param("!not valid base64!")


# ---------------------------------------------------------------------------
# encode_credential / build_authorization_header
# ---------------------------------------------------------------------------


def test_encode_credential_is_deterministic() -> None:
    cred = {"z": "last", "a": "first", "m": "middle"}
    assert encode_credential(cred) == encode_credential(cred)


def test_encode_credential_same_content_different_order() -> None:
    a = encode_credential({"b": 2, "a": 1})
    b = encode_credential({"a": 1, "b": 2})
    assert a == b


def test_build_authorization_header_prefix() -> None:
    cred = {"type": "test"}
    header = build_authorization_header(cred)
    assert header.startswith("Payment ")


def test_authorization_header_roundtrip() -> None:
    cred = {"challenge": {"id": "abc"}, "payload": {"type": "transaction", "signature": "0x76ab"}}
    header = build_authorization_header(cred)
    _, token = header.split(" ", 1)
    decoded = json.loads(b64url_decode(token))
    assert decoded["challenge"]["id"] == "abc"
    assert decoded["payload"]["type"] == "transaction"


# ---------------------------------------------------------------------------
# parse_payment_receipt / build_payment_receipt
# ---------------------------------------------------------------------------


def test_parse_payment_receipt_roundtrip() -> None:
    header = build_payment_receipt(
        challenge_id="cid123",
        method="tempo",
        reference="0x" + "aa" * 32,
        amount="10000",
        currency="0xtoken",
        status="success",
    )
    receipt = parse_payment_receipt(header)
    assert receipt.challenge_id == "cid123"
    assert receipt.method == "tempo"
    assert receipt.reference == "0x" + "aa" * 32
    assert receipt.settlement["amount"] == "10000"
    assert receipt.settlement["currency"] == "0xtoken"
    assert receipt.status == "success"


def test_parse_payment_receipt_failure_status() -> None:
    header = build_payment_receipt(
        challenge_id="cid",
        method="tempo",
        reference="0x" + "ff" * 32,
        amount="0",
        currency="0xtoken",
        status="failure",
    )
    receipt = parse_payment_receipt(header)
    assert receipt.status == "failure"


def test_parse_payment_receipt_bad_b64() -> None:
    with pytest.raises(ValueError, match="decode failed"):
        parse_payment_receipt("!not_valid!")


def test_parse_payment_receipt_unknown_method_rejected() -> None:
    bad_receipt = {
        "challengeId": "x",
        "method": "unknown_method",
        "reference": "0x00",
        "settlement": {},
        "status": "success",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    b64 = b64url_encode(jcs_encode(bad_receipt))
    with pytest.raises(ValidationError):
        parse_payment_receipt(b64)
