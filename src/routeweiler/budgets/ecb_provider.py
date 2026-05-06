"""ECB (European Central Bank) daily exchange rate provider.

Protocol:
    EcbRateProvider — async interface; production code uses LiveEcbProvider.

Test code uses StubEcbProvider (lives in tests/), passed via BudgetStore's
``ecb_provider`` kwarg so no monkey-patching is needed.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable
from xml.etree import ElementTree

import httpx

from routeweiler.errors import FmvUnavailableError

_log = logging.getLogger(__name__)

_ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ECB_NS = "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"
_MIN_RATES_COUNT = 2  # EUR + at least one other currency


@runtime_checkable
class EcbRateProvider(Protocol):
    """Async interface for fetching live ECB cross-currency exchange rates."""

    async def fetch_rate(self, src: str, dst: str) -> Decimal:
        """Return 1 ``src`` = X ``dst`` using ECB daily reference rates.

        Raises ``FmvUnavailableError`` on fetch or parse failure.
        """
        ...


class LiveEcbProvider:
    """Fetches ECB daily reference rates from the official eurofxref XML feed.

    EUR is the pivot currency; all cross rates are computed as
    ``dst_per_eur / src_per_eur`` where rates are expressed as 1 EUR = N units.
    Results are cached in-process keyed by calendar date (ECB publishes around
    16:00 CET; a process started before publication uses the previous day's
    rates until midnight UTC resets the cache key).

    Uses a 5 s HTTP timeout with no automatic retry — transient failures raise
    ``FmvUnavailableError`` so the caller can record ``quality="unavailable"``
    gracefully.
    """

    def __init__(self, *, timeout: float = 5.0) -> None:
        self._timeout = timeout
        self._cache: tuple[date, dict[str, Decimal]] | None = None
        self._client: httpx.AsyncClient | None = None

    async def fetch_rate(self, src: str, dst: str) -> Decimal:
        """Return cross rate ``src``→``dst`` (e.g. 1 USD = X EUR)."""
        rates = await self._fetch_rates_by_eur()
        return _cross_rate(src.lower(), dst.lower(), rates)

    async def _fetch_rates_by_eur(self) -> dict[str, Decimal]:
        today = datetime.now(UTC).date()
        if self._cache is not None:
            cached_date, rates = self._cache
            if cached_date == today:
                return rates

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

        try:
            response = await self._client.get(
                _ECB_URL,
                headers={"User-Agent": "routeweiler/ecb-provider"},
            )
            response.raise_for_status()
            xml_text = response.text
        except Exception as exc:
            raise FmvUnavailableError(f"ECB XML fetch failed: {exc}") from exc

        rates = _parse_ecb_xml(xml_text)
        self._cache = (today, rates)
        _log.debug("ECB rates refreshed; %d currencies loaded.", len(rates) - 1)
        return rates

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _parse_ecb_xml(xml_text: str) -> dict[str, Decimal]:
    """Parse ECB eurofxref XML; returns rates keyed by lowercase currency code.

    All rates are expressed as 1 EUR = N <currency>; EUR itself maps to 1.
    """
    root = ElementTree.fromstring(xml_text)
    rates: dict[str, Decimal] = {"eur": Decimal("1")}

    for cube in root.findall(f".//{{{_ECB_NS}}}Cube[@currency][@rate]"):
        currency = cube.get("currency", "").lower()
        rate_str = cube.get("rate", "")
        if currency and rate_str:
            try:
                rates[currency] = Decimal(rate_str)
            except Exception:
                _log.warning("ECB: skipping malformed rate for %s=%r", currency, rate_str)

    if len(rates) < _MIN_RATES_COUNT:
        raise FmvUnavailableError("ECB XML contained no valid exchange rates")

    return rates


def _cross_rate(src: str, dst: str, rates_by_eur: dict[str, Decimal]) -> Decimal:
    """Compute src→dst cross rate via EUR as pivot.

    ECB convention: ``rates_by_eur[c]`` = units of ``c`` per 1 EUR.
    Therefore: 1 src = (rates_by_eur[dst] / rates_by_eur[src]) dst.
    """
    if src == dst:
        return Decimal("1")

    src_rate = rates_by_eur.get(src)
    dst_rate = rates_by_eur.get(dst)

    if src_rate is None:
        raise FmvUnavailableError(f"ECB: no rate for {src.upper()}")
    if dst_rate is None:
        raise FmvUnavailableError(f"ECB: no rate for {dst.upper()}")

    return dst_rate / src_rate
