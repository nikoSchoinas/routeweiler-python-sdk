"""Routewiler — the public async HTTP client."""

from __future__ import annotations

from typing import Any

import httpx

from routewiler._auth import RoutewilerAuth
from routewiler.funding.evm import EvmFundingSource
from routewiler.rails.x402 import X402Adapter


def _build_adapters(funding: list[EvmFundingSource]) -> list[Any]:
    adapters: list[Any] = []
    evm = [f for f in funding if isinstance(f, EvmFundingSource)]
    if evm:
        adapters.append(X402Adapter(evm))
    return adapters


class Routewiler:
    """Async HTTP client that transparently handles 402 Payment Required.

    Mirrors the ``httpx.AsyncClient`` method surface (get/post/put/delete/
    patch/head/options/request).  Use as an async context manager to ensure
    the underlying connection pool is closed cleanly:

        async with Routewiler(funding=[Funding.base_usdc(wallet=signer)]) as c:
            resp = await c.get("https://api.vendor.com/data")

    Args:
        funding: One or more funding sources (e.g. ``Funding.base_usdc(wallet=...)``).
        policy:  Reserved — policy DSL added in Week 10.
        budget_envelope: Reserved — budget enforcement added in Week 4/9.
        trace_sink: Reserved — trace emission added in Week 3.
    """

    def __init__(
        self,
        *,
        funding: list[EvmFundingSource],
        policy: None = None,
        budget_envelope: str | None = None,
        trace_sink: None = None,
    ) -> None:
        self._funding = funding
        adapters = _build_adapters(funding)
        auth = RoutewilerAuth(adapters)
        self._http = httpx.AsyncClient(auth=auth)

    # ------------------------------------------------------------------
    # HTTP methods — delegate to the underlying AsyncClient
    # ------------------------------------------------------------------

    async def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._http.get(url, **kwargs)

    async def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._http.post(url, **kwargs)

    async def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._http.put(url, **kwargs)

    async def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._http.delete(url, **kwargs)

    async def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._http.patch(url, **kwargs)

    async def head(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._http.head(url, **kwargs)

    async def options(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._http.options(url, **kwargs)

    async def request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._http.request(method, url, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Routewiler:
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._http.__aexit__(*args)
