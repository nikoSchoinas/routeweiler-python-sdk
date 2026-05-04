"""Injectable FMV (Fair Market Value) providers for sats/BTC → fiat conversion.

Protocol:
    FmvProvider — async interface; production code uses CoinGeckoProvider.

Test code uses StubFmvProvider (lives in tests/), passing it via BudgetStore's
``fmv_provider`` kwarg so no monkey-patching is needed.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Protocol, runtime_checkable

import httpx

from routewiler.errors import FmvUnavailableError

_SATS_PER_BTC = 100_000_000
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


@runtime_checkable
class FmvProvider(Protocol):
    """Async interface for fetching live BTC/sats → fiat conversion rates."""

    async def fetch_btc_to(self, currency: str) -> Decimal:
        """Return the per-satoshi rate for ``currency``.

        Raises ``FmvUnavailableError`` on any fetch failure so callers can
        record ``quality="unavailable"`` without crashing.
        """
        ...


class CoinGeckoProvider:
    """Fetches BTC price from the CoinGecko ``simple/price`` endpoint.

    Caches results in-process for ``cache_ttl_seconds`` (default 60 s) to
    avoid hammering the API on every envelope creation.  Uses a 5 s HTTP
    timeout with no automatic retry — transient failures raise
    ``FmvUnavailableError`` so the caller can record ``quality="unavailable"``
    gracefully.
    """

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._timeout = timeout
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[Decimal, float]] = {}

    async def fetch_btc_to(self, currency: str) -> Decimal:
        """Return the per-satoshi rate, e.g. ``Decimal("0.000001")`` for 1 sat = $0.000001."""
        cur = currency.lower()

        cached = self._cache.get(cur)
        if cached is not None:
            rate, expires_at = cached
            if time.monotonic() < expires_at:
                return rate

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _COINGECKO_URL,
                    params={"ids": "bitcoin", "vs_currencies": cur},
                    headers={"User-Agent": "routewiler/fmv-provider"},
                )
                response.raise_for_status()
                data = response.json()
                btc_price = Decimal(str(data["bitcoin"][cur]))
        except Exception as exc:
            raise FmvUnavailableError(f"CoinGecko BTC/{cur.upper()} fetch failed: {exc}") from exc

        rate = btc_price / _SATS_PER_BTC
        self._cache[cur] = (rate, time.monotonic() + self._cache_ttl)
        return rate
