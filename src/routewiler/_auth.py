"""RoutewilerAuth — httpx.Auth subclass that retries on 402."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import httpx

from routewiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routewiler.errors import RailNotSupportedError
from routewiler.rails.base import RailAdapter

_PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"


class RoutewilerAuth(httpx.Auth):
    """Intercepts HTTP 402 responses and retries with a signed payment.

    Uses ``httpx.Auth.async_auth_flow()`` — the correct primitive for
    retry-with-modified-request (event_hooks are read-only and cannot retry).
    ``requires_request_body = True`` ensures the body is buffered so POST
    retries work correctly.
    """

    requires_request_body = True

    def __init__(self, adapters: list[RailAdapter]) -> None:
        self._adapters = adapters

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        # Sync path not supported — callers must use AsyncClient.
        raise NotImplementedError(
            "Routewiler is async-only. Use httpx.AsyncClient (or await client.get(...))."
        )
        yield request  # pragma: no cover — makes this a generator function

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        response = yield request

        if response.status_code != HTTP_STATUS_PAYMENT_REQUIRED:
            return

        # Drain the 402 body so the connection returns to the pool.
        await response.aread()

        adapter = next((a for a in self._adapters if a.can_handle(response)), None)
        if adapter is None:
            # No adapter recognised this rail — surface the 402 to the caller.
            raise RailNotSupportedError(
                f"No rail adapter can handle the 402 from {request.url}. "
                "Check that the server uses a supported rail (x402, L402, MPP) "
                "and that you have configured the matching funding source."
            )

        challenge = adapter.parse(request, response)
        payment_header = await adapter.sign(challenge)

        retry = httpx.Request(
            method=request.method,
            url=request.url,
            headers={**dict(request.headers), _PAYMENT_SIGNATURE_HEADER: payment_header},
            content=request.content,
            extensions=request.extensions,
        )
        yield retry
