"""RoutewilerAuth — httpx.Auth subclass that retries on 402."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from routewiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routewiler.errors import RailNotSupportedError
from routewiler.rails.base import RailAdapter

if TYPE_CHECKING:
    from routewiler.trace.emitter import TraceEmitter

_PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"


class RoutewilerAuth(httpx.Auth):
    """Intercepts HTTP 402 responses and retries with a signed payment.

    Uses ``httpx.Auth.async_auth_flow()`` — the correct primitive for
    retry-with-modified-request (event_hooks are read-only and cannot retry).
    ``requires_request_body = True`` ensures the body is buffered so POST
    retries work correctly.
    """

    requires_request_body = True

    def __init__(
        self,
        adapters: list[RailAdapter],
        *,
        emitter: TraceEmitter | None = None,
    ) -> None:
        self._adapters = adapters
        self._emitter = emitter

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        # Sync path not supported — callers must use AsyncClient.
        raise NotImplementedError(
            "Routewiler is async-only. Use httpx.AsyncClient (or await client.get(...))."
        )
        # httpx uses inspect.isgeneratorfunction to detect auth_flow; the yield
        # keyword must exist even though it is unreachable.
        yield request  # pragma: no cover

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        ts_start = datetime.now(UTC)
        response = yield request

        if response.status_code != HTTP_STATUS_PAYMENT_REQUIRED:
            # Non-402: the passthrough trace is emitted by the client's
            # response event hook (which also fires for free calls).  We tag
            # the extensions dict so the hook knows it is not a paid call.
            response.extensions["routewiler_emitted"] = False
            return

        # Drain the 402 body so the connection returns to the pool.
        await response.aread()

        adapter = next((a for a in self._adapters if a.can_handle(response)), None)
        if adapter is None:
            ts_end = datetime.now(UTC)
            # No adapter recognised this rail — emit an error trace and
            # surface the error to the caller.
            err = RailNotSupportedError(
                f"No rail adapter can handle the 402 from {request.url}. "
                "Check that the server uses a supported rail (x402, L402, MPP) "
                "and that you have configured the matching funding source."
            )
            if self._emitter:
                await self._emitter.emit_error(
                    request=request,
                    response=response,
                    error=err,
                    challenge=None,
                    ts_start=ts_start,
                    ts_end=ts_end,
                )
            raise err

        challenge = None
        try:
            challenge = adapter.parse(request, response)
            payment_header = await adapter.sign(challenge)
        except Exception as exc:
            ts_end = datetime.now(UTC)
            if self._emitter:
                await self._emitter.emit_error(
                    request=request,
                    response=response,
                    error=exc,
                    challenge=challenge,
                    ts_start=ts_start,
                    ts_end=ts_end,
                )
            raise

        retry = httpx.Request(
            method=request.method,
            url=request.url,
            headers={**dict(request.headers), _PAYMENT_SIGNATURE_HEADER: payment_header},
            content=request.content,
            extensions=request.extensions,
        )
        ts_retry = datetime.now(UTC)
        final_response = yield retry
        ts_end = datetime.now(UTC)

        # Tag the final response so the passthrough hook skips it.
        final_response.extensions["routewiler_emitted"] = True

        if self._emitter:
            settlement = None
            # Only X402Adapter exposes parse_settlement today.
            if hasattr(adapter, "parse_settlement"):
                settlement = adapter.parse_settlement(final_response)
            await self._emitter.emit_paid(
                request=request,
                challenge=challenge,
                settlement=settlement,
                final_response=final_response,
                ts_start=ts_start,
                ts_retry=ts_retry,
                ts_end=ts_end,
            )
