"""Tests for EcbRateProvider protocol and LiveEcbProvider."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from routeweiler.budgets.ecb_provider import (
    EcbRateProvider,
    LiveEcbProvider,
    _cross_rate,
    _parse_ecb_xml,
)
from routeweiler.errors import FmvUnavailableError

# Minimal ECB XML response with three currencies.
_ECB_NS = "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"
_SAMPLE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                 xmlns="{_ECB_NS}">
  <Cube>
    <Cube time="2026-05-05">
      <Cube currency="USD" rate="1.0824"/>
      <Cube currency="GBP" rate="0.8613"/>
      <Cube currency="JPY" rate="161.82"/>
    </Cube>
  </Cube>
</gesmes:Envelope>"""

_ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_live_ecb_provider_satisfies_protocol() -> None:
    assert isinstance(LiveEcbProvider(), EcbRateProvider)


# ---------------------------------------------------------------------------
# _parse_ecb_xml
# ---------------------------------------------------------------------------


def test_parse_ecb_xml_returns_eur_and_currencies() -> None:
    rates = _parse_ecb_xml(_SAMPLE_XML)
    assert rates["eur"] == Decimal("1")
    assert rates["usd"] == Decimal("1.0824")
    assert rates["gbp"] == Decimal("0.8613")
    assert rates["jpy"] == Decimal("161.82")


def test_parse_ecb_xml_empty_raises() -> None:
    empty_xml = f"""<?xml version="1.0"?>
    <gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                     xmlns="{_ECB_NS}">
      <Cube><Cube time="2026-05-05"/></Cube>
    </gesmes:Envelope>"""
    with pytest.raises(FmvUnavailableError, match="no valid exchange rates"):
        _parse_ecb_xml(empty_xml)


# ---------------------------------------------------------------------------
# _cross_rate
# ---------------------------------------------------------------------------


def test_cross_rate_same_currency() -> None:
    rates = {"eur": Decimal("1"), "usd": Decimal("1.08")}
    assert _cross_rate("usd", "usd", rates) == Decimal("1")


def test_cross_rate_eur_to_usd() -> None:
    rates = {"eur": Decimal("1"), "usd": Decimal("1.08")}
    # 1 EUR = 1.08 USD → 1/1 * 1.08 = 1.08
    assert _cross_rate("eur", "usd", rates) == Decimal("1.08")


def test_cross_rate_usd_to_eur() -> None:
    rates = {"eur": Decimal("1"), "usd": Decimal("1.08")}
    # 1 USD → 1/1.08 EUR
    expected = Decimal("1") / Decimal("1.08")
    assert abs(_cross_rate("usd", "eur", rates) - expected) < Decimal("0.0001")


def test_cross_rate_usd_to_gbp() -> None:
    rates = {"eur": Decimal("1"), "usd": Decimal("1.0824"), "gbp": Decimal("0.8613")}
    # 1 USD → (gbp_per_eur / usd_per_eur) GBP = 0.8613 / 1.0824
    expected = Decimal("0.8613") / Decimal("1.0824")
    result = _cross_rate("usd", "gbp", rates)
    assert abs(result - expected) < Decimal("0.0001")


def test_cross_rate_unknown_src_raises() -> None:
    rates = {"eur": Decimal("1"), "usd": Decimal("1.08")}
    with pytest.raises(FmvUnavailableError, match="CHF"):
        _cross_rate("chf", "usd", rates)


def test_cross_rate_unknown_dst_raises() -> None:
    rates = {"eur": Decimal("1"), "usd": Decimal("1.08")}
    with pytest.raises(FmvUnavailableError, match="CHF"):
        _cross_rate("usd", "chf", rates)


# ---------------------------------------------------------------------------
# LiveEcbProvider — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_live_ecb_returns_correct_cross_rate() -> None:
    respx.get(_ECB_URL).mock(return_value=httpx.Response(200, text=_SAMPLE_XML))
    provider = LiveEcbProvider()
    rate = await provider.fetch_rate("usd", "gbp")
    expected = Decimal("0.8613") / Decimal("1.0824")
    assert abs(rate - expected) < Decimal("0.0001")


@pytest.mark.asyncio
@respx.mock
async def test_live_ecb_cache_avoids_second_request() -> None:
    route = respx.get(_ECB_URL).mock(return_value=httpx.Response(200, text=_SAMPLE_XML))
    provider = LiveEcbProvider()
    await provider.fetch_rate("usd", "eur")
    await provider.fetch_rate("gbp", "eur")
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_live_ecb_eur_to_usd() -> None:
    respx.get(_ECB_URL).mock(return_value=httpx.Response(200, text=_SAMPLE_XML))
    provider = LiveEcbProvider()
    rate = await provider.fetch_rate("eur", "usd")
    assert rate == Decimal("1.0824")


@pytest.mark.asyncio
@respx.mock
async def test_live_ecb_same_currency_returns_one() -> None:
    respx.get(_ECB_URL).mock(return_value=httpx.Response(200, text=_SAMPLE_XML))
    provider = LiveEcbProvider()
    rate = await provider.fetch_rate("usd", "usd")
    assert rate == Decimal("1")


# ---------------------------------------------------------------------------
# LiveEcbProvider — failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_live_ecb_http_error_raises_fmv_unavailable() -> None:
    respx.get(_ECB_URL).mock(return_value=httpx.Response(503))
    provider = LiveEcbProvider()
    with pytest.raises(FmvUnavailableError, match="ECB XML fetch failed"):
        await provider.fetch_rate("usd", "eur")


@pytest.mark.asyncio
@respx.mock
async def test_live_ecb_network_error_raises_fmv_unavailable() -> None:
    respx.get(_ECB_URL).mock(side_effect=httpx.ConnectError("timeout"))
    provider = LiveEcbProvider()
    with pytest.raises(FmvUnavailableError, match="ECB XML fetch failed"):
        await provider.fetch_rate("usd", "eur")


@pytest.mark.asyncio
@respx.mock
async def test_live_ecb_unknown_currency_raises_fmv_unavailable() -> None:
    respx.get(_ECB_URL).mock(return_value=httpx.Response(200, text=_SAMPLE_XML))
    provider = LiveEcbProvider()
    with pytest.raises(FmvUnavailableError, match="CHF"):
        await provider.fetch_rate("usd", "chf")
