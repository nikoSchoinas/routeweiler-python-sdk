"""Tests for L402Adapter.can_handle() — detector truth table."""

from __future__ import annotations

import httpx
import pytest

from routeweiler.rails.l402 import L402Adapter


def _make_response(status: int, www_auth: str = "") -> httpx.Response:
    headers = {}
    if www_auth:
        headers["WWW-Authenticate"] = www_auth
    return httpx.Response(status_code=status, headers=headers)


@pytest.fixture
def adapter() -> L402Adapter:
    return L402Adapter([])


class TestCanHandle:
    def test_l402_scheme(self, adapter: L402Adapter) -> None:
        r = _make_response(402, 'L402 macaroon="abc", invoice="lnbc1"')
        assert adapter.can_handle(r) is True

    def test_lsat_scheme(self, adapter: L402Adapter) -> None:
        r = _make_response(402, 'LSAT macaroon="abc", invoice="lnbc1"')
        assert adapter.can_handle(r) is True

    def test_lowercase_scheme(self, adapter: L402Adapter) -> None:
        r = _make_response(402, 'l402 macaroon="abc", invoice="lnbc1"')
        assert adapter.can_handle(r) is True

    def test_200_with_l402_header_is_false(self, adapter: L402Adapter) -> None:
        r = _make_response(200, 'L402 macaroon="abc", invoice="lnbc1"')
        assert adapter.can_handle(r) is False

    def test_402_without_header_is_false(self, adapter: L402Adapter) -> None:
        r = _make_response(402)
        assert adapter.can_handle(r) is False

    def test_402_bearer_scheme_is_false(self, adapter: L402Adapter) -> None:
        r = _make_response(402, "Bearer token123")
        assert adapter.can_handle(r) is False

    def test_402_x402_scheme_is_false(self, adapter: L402Adapter) -> None:
        r = _make_response(402, "PAYMENT-REQUIRED somedata")
        assert adapter.can_handle(r) is False

    def test_404_with_l402_header_is_false(self, adapter: L402Adapter) -> None:
        r = _make_response(404, 'L402 macaroon="abc", invoice="lnbc1"')
        assert adapter.can_handle(r) is False

    def test_empty_www_authenticate_is_false(self, adapter: L402Adapter) -> None:
        r = _make_response(402, "")
        assert adapter.can_handle(r) is False
