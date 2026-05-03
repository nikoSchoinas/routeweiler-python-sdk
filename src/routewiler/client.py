"""Routewiler — the public async HTTP client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from routewiler._auth import RoutewilerAuth
from routewiler.budgets.local import DEFAULT_ENVELOPE_ID, BudgetStore, ensure_default_envelope
from routewiler.errors import EnvelopeNotFoundError
from routewiler.funding.evm import EvmFundingSource
from routewiler.rails.x402 import X402Adapter
from routewiler.trace.emitter import TraceEmitter
from routewiler.trace.sink_sqlite import SqliteTraceSink


def _build_adapters(funding: list[EvmFundingSource]) -> list[Any]:
    adapters: list[Any] = []
    evm = [f for f in funding if isinstance(f, EvmFundingSource)]
    if evm:
        adapters.append(X402Adapter(evm))
    return adapters


def _funding_label(funding: list[EvmFundingSource]) -> str:
    if not funding:
        return "none"
    f = funding[0]
    return f"evm:{f.network}:{f.asset}"


class Routewiler:
    """Async HTTP client that transparently handles 402 Payment Required.

    Mirrors the ``httpx.AsyncClient`` method surface (get/post/put/delete/
    patch/head/options/request).  Use as an async context manager to ensure
    the underlying connection pool is closed cleanly:

        async with Routewiler(funding=[Funding.base_usdc(wallet=signer)]) as c:
            resp = await c.get("https://api.vendor.com/data")

    Args:
        funding:         One or more funding sources (e.g. ``Funding.base_usdc(wallet=...)``).
        policy:          Reserved — policy DSL added in Week 10.
        budget_envelope: ID of the envelope to draw from. Defaults to ``"default"``.
                         The envelope must exist in the database (use BudgetStore.create_envelope
                         to create custom envelopes before constructing the client).
                         Budget enforcement requires a trace_sink; if trace_sink is None,
                         no enforcement runs.
        trace_sink:      SQLite trace sink. Pass ``TraceSink.sqlite(path)`` to
                         enable local tracing. Defaults to ``None`` (no tracing).
    """

    def __init__(
        self,
        *,
        funding: list[EvmFundingSource],
        policy: None = None,
        budget_envelope: str | None = None,
        trace_sink: SqliteTraceSink | None = None,
    ) -> None:
        self._funding = funding
        self._trace_sink = trace_sink
        envelope_id = budget_envelope or DEFAULT_ENVELOPE_ID

        emitter: TraceEmitter | None = None
        budget_store: BudgetStore | None = None
        envelope_currency: str | None = None

        if trace_sink is not None:
            # Seed the default envelope row (idempotent INSERT OR IGNORE).
            ensure_default_envelope(trace_sink.db_path)

            budget_store = BudgetStore(trace_sink.db_path)

            # Resolve the envelope's declared currency from the DB.
            envelope_currency = budget_store.get_currency_sync(envelope_id)
            if envelope_currency is None:
                raise EnvelopeNotFoundError(
                    f"Envelope '{envelope_id}' not found. "
                    "Create it with BudgetStore.create_envelope() before constructing Routewiler."
                )

            emitter = TraceEmitter(
                sink=trace_sink,
                envelope_id=envelope_id,
                envelope_currency=envelope_currency,
                funding_label=_funding_label(funding),
                url_mode=trace_sink.url_mode,
            )

        self._budget_store = budget_store
        self._emitter = emitter
        adapters = _build_adapters(funding)
        auth = RoutewilerAuth(
            adapters,
            emitter=emitter,
            budget_store=budget_store,
            envelope_id=envelope_id if budget_store is not None else None,
            envelope_currency=envelope_currency,
        )
        self._http = httpx.AsyncClient(auth=auth)

    # ------------------------------------------------------------------
    # Internal trace helper
    # ------------------------------------------------------------------

    async def _traced(self, coro: Any, ts_start: datetime) -> httpx.Response:
        """Execute an httpx coroutine and emit a passthrough trace if needed.

        The auth_flow marks paid responses with ``extensions["routewiler_emitted"]``.
        Any response that does not carry that flag gets a passthrough trace here.
        Errors raised by auth_flow (RailNotSupportedError, SigningError, etc.) have
        already been traced by auth_flow, so we let them propagate without re-tracing.
        """
        resp: httpx.Response = await coro
        ts_end = datetime.now(UTC)
        if self._emitter and not resp.extensions.get("routewiler_emitted"):
            await self._emitter.emit_passthrough(
                request=resp.request,
                response=resp,
                ts_start=ts_start,
                ts_end=ts_end,
            )
        return resp

    # ------------------------------------------------------------------
    # HTTP methods — delegate to the underlying AsyncClient
    # ------------------------------------------------------------------

    async def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.get(url, **kwargs), datetime.now(UTC))

    async def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.post(url, **kwargs), datetime.now(UTC))

    async def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.put(url, **kwargs), datetime.now(UTC))

    async def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.delete(url, **kwargs), datetime.now(UTC))

    async def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.patch(url, **kwargs), datetime.now(UTC))

    async def head(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.head(url, **kwargs), datetime.now(UTC))

    async def options(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.options(url, **kwargs), datetime.now(UTC))

    async def request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self._traced(self._http.request(method, url, **kwargs), datetime.now(UTC))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._budget_store is not None:
            await self._budget_store.aclose()
        if self._trace_sink is not None:
            await self._trace_sink.aclose()

    async def __aenter__(self) -> Routewiler:
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
