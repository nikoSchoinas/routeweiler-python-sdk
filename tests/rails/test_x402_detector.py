"""Tests for X402Adapter.can_handle()."""

import httpx
import pytest

from routeweiler.funding.evm import EvmFundingSource
from routeweiler.rails.x402 import X402Adapter


def _make_response(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code=status, headers=headers or {})


@pytest.fixture
def adapter(base_usdc_funding: EvmFundingSource) -> X402Adapter:
    return X402Adapter([base_usdc_funding])


def test_detects_402_with_header(adapter: X402Adapter, challenge_base_usdc_header: str) -> None:
    resp = _make_response(402, {"PAYMENT-REQUIRED": challenge_base_usdc_header})
    assert adapter.can_handle(resp) is True


def test_ignores_200(adapter: X402Adapter, challenge_base_usdc_header: str) -> None:
    resp = _make_response(200, {"PAYMENT-REQUIRED": challenge_base_usdc_header})
    assert adapter.can_handle(resp) is False


def test_ignores_402_without_header(adapter: X402Adapter) -> None:
    resp = _make_response(402)
    assert adapter.can_handle(resp) is False


def test_ignores_404(adapter: X402Adapter) -> None:
    resp = _make_response(404, {"PAYMENT-REQUIRED": "something"})
    assert adapter.can_handle(resp) is False


def test_ignores_402_with_wrong_header(adapter: X402Adapter) -> None:
    resp = _make_response(402, {"WWW-Authenticate": "L402 macaroon=..., invoice=..."})
    assert adapter.can_handle(resp) is False
