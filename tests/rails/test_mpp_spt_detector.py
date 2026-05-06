"""can_handle matrix for MppSptAdapter.

Mirrors test_mpp_tempo_detector.py — the polarity flips: method=stripe/card
is now accepted here (and explicitly rejected by MppTempoAdapter).
"""

from __future__ import annotations

import httpx
import pytest

from routeweiler.rails.mpp_spt import MppSptAdapter
from tests.fixtures.mpp_spt_mock_server import MOCK_WWW_AUTHENTICATE


def _make_402(www_auth: str) -> httpx.Response:
    return httpx.Response(402, headers={"WWW-Authenticate": www_auth})


def _make_adapter() -> MppSptAdapter:
    return MppSptAdapter([])


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_handles_method_stripe() -> None:
    adapter = _make_adapter()
    resp = _make_402(MOCK_WWW_AUTHENTICATE)  # method="stripe"
    assert adapter.can_handle(resp) is True


def test_handles_method_card() -> None:
    adapter = _make_adapter()
    resp = _make_402(
        'Payment id="x", method="card", request="eyJ...", expires="2099-01-01T00:00:00Z"'
    )
    assert adapter.can_handle(resp) is True


def test_handles_method_stripe_case_insensitive() -> None:
    adapter = _make_adapter()
    resp = _make_402('Payment id="x", method="STRIPE", request="eyJ..."')
    assert adapter.can_handle(resp) is True


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def test_ignores_method_tempo() -> None:
    adapter = _make_adapter()
    resp = _make_402('Payment id="x", method="tempo", request="eyJ..."')
    assert adapter.can_handle(resp) is False


def test_ignores_200() -> None:
    adapter = _make_adapter()
    resp = httpx.Response(200)
    assert adapter.can_handle(resp) is False


def test_ignores_no_www_authenticate() -> None:
    adapter = _make_adapter()
    resp = httpx.Response(402)
    assert adapter.can_handle(resp) is False


def test_ignores_non_payment_scheme() -> None:
    adapter = _make_adapter()
    resp = _make_402('Bearer realm="api"')
    assert adapter.can_handle(resp) is False


def test_ignores_missing_method_param() -> None:
    adapter = _make_adapter()
    resp = _make_402('Payment id="x", request="eyJ..."')
    assert adapter.can_handle(resp) is False


def test_ignores_unknown_method() -> None:
    adapter = _make_adapter()
    resp = _make_402('Payment id="x", method="lightning", request="eyJ..."')
    assert adapter.can_handle(resp) is False


@pytest.mark.parametrize("method", ["stripe", "card"])
def test_tempo_adapter_rejects_spt_methods(method: str) -> None:
    """Ensure MppTempoAdapter does NOT accept stripe/card (regression guard)."""
    from routeweiler.rails.mpp_tempo import MppTempoAdapter  # noqa: PLC0415

    adapter = MppTempoAdapter([])
    resp = _make_402(f'Payment id="x", method="{method}", request="eyJ..."')
    assert adapter.can_handle(resp) is False
