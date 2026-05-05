"""Unit tests for MppTempoAdapter.can_handle — 402 challenge detection."""

from __future__ import annotations

import httpx

from routewiler.rails.mpp_tempo import MppTempoAdapter
from tests.fixtures.mpp_tempo_mock_server import MOCK_WWW_AUTHENTICATE

_adapter = MppTempoAdapter([])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {})


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_handles_402_with_payment_tempo() -> None:
    r = _response(402, {"WWW-Authenticate": MOCK_WWW_AUTHENTICATE})
    assert _adapter.can_handle(r) is True


def test_handles_402_bare_method_param() -> None:
    r = _response(402, {"WWW-Authenticate": 'Payment id="x", method="tempo"'})
    assert _adapter.can_handle(r) is True


def test_handles_method_case_insensitive() -> None:
    r = _response(402, {"WWW-Authenticate": 'Payment id="x", method="Tempo"'})
    assert _adapter.can_handle(r) is True


def test_handles_scheme_name_case_insensitive() -> None:
    r = _response(402, {"WWW-Authenticate": 'payment id="x", method="tempo"'})
    assert _adapter.can_handle(r) is True


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def test_ignores_200() -> None:
    r = _response(200, {"WWW-Authenticate": 'Payment id="x", method="tempo"'})
    assert _adapter.can_handle(r) is False


def test_ignores_401() -> None:
    r = _response(401, {"WWW-Authenticate": 'Payment id="x", method="tempo"'})
    assert _adapter.can_handle(r) is False


def test_ignores_402_without_www_authenticate() -> None:
    r = _response(402)
    assert _adapter.can_handle(r) is False


def test_ignores_method_stripe() -> None:
    r = _response(402, {"WWW-Authenticate": 'Payment id="x", method="stripe"'})
    assert _adapter.can_handle(r) is False


def test_ignores_method_lightning() -> None:
    r = _response(402, {"WWW-Authenticate": 'Payment id="x", method="lightning"'})
    assert _adapter.can_handle(r) is False


def test_ignores_402_no_method_param() -> None:
    r = _response(402, {"WWW-Authenticate": 'Payment id="x"'})
    assert _adapter.can_handle(r) is False


def test_ignores_l402_scheme() -> None:
    r = _response(402, {"WWW-Authenticate": 'L402 macaroon="abc", invoice="lnbc..."'})
    assert _adapter.can_handle(r) is False


def test_ignores_bearer_scheme() -> None:
    r = _response(402, {"WWW-Authenticate": "Bearer realm=example.com"})
    assert _adapter.can_handle(r) is False
