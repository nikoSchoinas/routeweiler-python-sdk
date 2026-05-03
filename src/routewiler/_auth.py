"""RoutewilerAuth — httpx.Auth subclass that retries on 402."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from routewiler._constants import HTTP_CLIENT_ERROR_THRESHOLD, HTTP_STATUS_PAYMENT_REQUIRED
from routewiler.budgets.fmv import amount_to_envelope_minor_units
from routewiler.budgets.local import BudgetStore
from routewiler.budgets.schema import DrawReceipt
from routewiler.errors import RailNotSupportedError
from routewiler.rails.base import RailAdapter

if TYPE_CHECKING:
    from routewiler.trace.emitter import TraceEmitter

_log = logging.getLogger(__name__)
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
        budget_store: BudgetStore | None = None,
        envelope_id: str | None = None,
        envelope_currency: str | None = None,
    ) -> None:
        self._adapters = adapters
        self._emitter = emitter
        self._budget_store = budget_store
        self._envelope_id = envelope_id
        self._envelope_currency = envelope_currency

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

        # Phase 1: parse the 402 challenge.
        challenge = None
        try:
            challenge = adapter.parse(request, response)
        except Exception as exc:
            ts_end = datetime.now(UTC)
            if self._emitter:
                await self._emitter.emit_error(
                    request=request,
                    response=response,
                    error=exc,
                    challenge=None,
                    ts_start=ts_start,
                    ts_end=ts_end,
                )
            raise

        # Phase 2: budget draw — reserve capacity before committing to payment.
        receipt: DrawReceipt | None = None
        if (
            self._budget_store is not None
            and self._envelope_id is not None
            and self._envelope_currency is not None
        ):
            idempotency_key = uuid4().hex
            request_id = uuid4().hex
            try:
                snapshot = await self._budget_store.load_fmv_snapshot(self._envelope_id)
                amount_envelope, _fmv_quality = amount_to_envelope_minor_units(
                    challenge.price.currency,
                    challenge.price.amount,
                    self._envelope_currency,
                    snapshot_rates=snapshot,
                )
                receipt = await self._budget_store.draw(
                    envelope_id=self._envelope_id,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    amount_reserved_minor_units=amount_envelope,
                    rail_quoted=challenge.rail,
                )
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

        # Phase 3: sign the challenge.
        payment_header: str
        try:
            payment_header = await adapter.sign(challenge)
        except Exception as exc:
            if receipt is not None and self._budget_store is not None:
                try:
                    await self._budget_store.rollback(receipt.receipt_id)
                except Exception:
                    _log.exception("Rollback failed after sign error; draw will expire via reaper.")
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
        try:
            final_response = yield retry
        except Exception as exc:
            if receipt is not None and self._budget_store is not None:
                try:
                    await self._budget_store.rollback(receipt.receipt_id)
                except Exception:
                    _log.exception(
                        "Rollback failed after transport error; draw will expire via reaper."
                    )
            raise exc
        ts_end = datetime.now(UTC)

        # Tag the final response so the passthrough hook skips it.
        final_response.extensions["routewiler_emitted"] = True

        # Phase 4: confirm or rollback the draw based on the final HTTP status.
        if receipt is not None and self._budget_store is not None:
            if final_response.status_code < HTTP_CLIENT_ERROR_THRESHOLD:
                # Use the reserved amount as the settled amount.  For x402 'exact' scheme
                # reserved == settled by definition.  For 'upto' scheme this is
                # conservative (may count more than actually settled); a proper FMV
                # re-conversion for upto ships with the upto rail adapter.
                try:
                    await self._budget_store.confirm(
                        receipt.receipt_id, receipt.amount_reserved_minor_units
                    )
                except Exception:
                    _log.exception("Confirm failed; draw will stay reserved until reaper.")
            else:
                try:
                    await self._budget_store.rollback(receipt.receipt_id)
                except Exception:
                    _log.exception(
                        "Rollback failed after error response; draw will expire via reaper."
                    )

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
