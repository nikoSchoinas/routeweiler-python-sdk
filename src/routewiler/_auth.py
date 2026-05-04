"""RoutewilerAuth — httpx.Auth subclass that retries on 402."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from routewiler._constants import HTTP_CLIENT_ERROR_THRESHOLD, HTTP_STATUS_PAYMENT_REQUIRED
from routewiler.budgets.local import BudgetStore
from routewiler.budgets.schema import DrawReceipt
from routewiler.credentials.schema import CredentialState
from routewiler.errors import (
    NoFeasibleRailError,
    PolicyDeniedError,
    PolicyMaxPerCallExceededError,
    RailNotSupportedError,
)
from routewiler.funding import FundingSource
from routewiler.policy.engine import PolicyEngine
from routewiler.routing.router import Router
from routewiler.routing.sticky import StickyCache, StickyKey

if TYPE_CHECKING:
    from routewiler.credentials.recovery import CredentialRecoverer
    from routewiler.credentials.schema import CredentialRecord
    from routewiler.credentials.store import CredentialStore
    from routewiler.normalized import Rail
    from routewiler.trace.emitter import TraceEmitter

_log = logging.getLogger(__name__)


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
        *,
        router: Router,
        sticky_cache: StickyCache,
        funding: list[FundingSource],
        agent_id: str | None = None,
        session_id: str | None = None,
        emitter: TraceEmitter | None = None,
        budget_store: BudgetStore | None = None,
        envelope_id: str | None = None,
        envelope_currency: str | None = None,
        policy_engine: PolicyEngine | None = None,
        credential_store: CredentialStore | None = None,
        recoverer: CredentialRecoverer | None = None,
    ) -> None:
        self._router = router
        self._sticky_cache = sticky_cache
        self._funding = funding
        self._agent_id = agent_id
        self._session_id = session_id
        self._emitter = emitter
        self._budget_store = budget_store
        self._envelope_id = envelope_id
        self._envelope_currency = envelope_currency
        self._policy_engine = policy_engine
        self._credential_store = credential_store
        self._recoverer = recoverer

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

        request_id = uuid4().hex
        sticky_key = StickyKey(
            origin=_origin(request.url),
            agent_id=self._agent_id,
            session_id=self._session_id,
        )

        # Load FMV snapshot once per 402 (needed by router for cost scoring).
        fmv_snapshot = None
        if self._budget_store is not None and self._envelope_id is not None:
            fmv_snapshot = await self._budget_store.load_fmv_snapshot(self._envelope_id)

        excluded_rails: frozenset[Rail] = frozenset()
        prior_rail: Rail | None = None
        attempt = 0

        while True:
            # -----------------------------------------------------------------
            # Routing phase: select the best feasible rail.
            # -----------------------------------------------------------------
            sticky_rail = self._sticky_cache.lookup(sticky_key)
            try:
                choice = await self._router.decide(
                    request=request,
                    response=response,
                    policy_engine=self._policy_engine,
                    funding=self._funding,
                    envelope_currency=self._envelope_currency,
                    fmv_snapshot=fmv_snapshot,
                    excluded_rails=excluded_rails,
                    sticky_rail=sticky_rail,
                    prior_rail=prior_rail,
                    attempt=attempt,
                )
            except (RailNotSupportedError, PolicyDeniedError, NoFeasibleRailError) as err:
                ts_end = datetime.now(UTC)
                if self._emitter:
                    await self._emitter.emit_error(
                        request=request,
                        response=response,
                        error=err,
                        challenge=None,
                        ts_start=ts_start,
                        ts_end=ts_end,
                        fallback_from=prior_rail,
                    )
                raise

            challenge = choice.candidate.challenge
            adapter = choice.candidate.adapter
            decision = choice.candidate.policy_decision

            # -----------------------------------------------------------------
            # Policy max_per_call gate (post-routing, §7.1 step 2 applies the
            # prefer filter; max_per_call is amount-based and rail-agnostic).
            # -----------------------------------------------------------------
            if (
                decision.max_per_call_minor_units is not None
                and self._envelope_currency is not None
                and choice.candidate.quote_envelope_minor_units is not None
                and choice.candidate.quote_envelope_minor_units > decision.max_per_call_minor_units
            ):
                ts_end = datetime.now(UTC)
                exc = PolicyMaxPerCallExceededError(
                    rule_name=decision.rule_name,
                    requested=choice.candidate.quote_envelope_minor_units,
                    limit=decision.max_per_call_minor_units,
                )
                if self._emitter:
                    await self._emitter.emit_error(
                        request=request,
                        response=response,
                        error=exc,
                        challenge=challenge,
                        ts_start=ts_start,
                        ts_end=ts_end,
                        fallback_from=choice.fallback_from,
                    )
                raise exc

            # -----------------------------------------------------------------
            # Budget draw phase — reserve capacity before committing to payment.
            # -----------------------------------------------------------------
            receipt: DrawReceipt | None = None
            quote = choice.candidate.quote_envelope_minor_units
            if (
                self._budget_store is not None
                and self._envelope_id is not None
                and self._envelope_currency is not None
                and quote is not None  # None means FMV conversion failed; skip draw
            ):
                idempotency_key = _make_idempotency_key(request_id, attempt)
                try:
                    receipt = await self._budget_store.draw(
                        envelope_id=self._envelope_id,
                        request_id=request_id,
                        idempotency_key=idempotency_key,
                        amount_reserved_minor_units=quote,
                        rail_quoted=adapter.rail,
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
                            fallback_from=choice.fallback_from,
                        )
                    raise  # budget errors are not failover-able

            # -----------------------------------------------------------------
            # Pay phase — produce the PaymentResult (signs + header for x402;
            # pays invoice for L402).
            # On failure: rollback, exclude this rail, attempt failover.
            # -----------------------------------------------------------------
            try:
                payment_result = await adapter.pay(challenge, receipt)
            except Exception:
                _log.warning(
                    "Pay failed for rail %r on attempt %d; rolling back and trying next rail.",
                    adapter.rail,
                    attempt,
                )
                if receipt is not None and self._budget_store is not None:
                    try:
                        await self._budget_store.rollback(receipt.receipt_id)
                    except Exception:
                        _log.exception(
                            "Rollback failed after pay error; draw will expire via reaper."
                        )
                self._sticky_cache.forget(sticky_key)
                excluded_rails = excluded_rails | {adapter.rail}
                prior_rail = adapter.rail
                attempt += 1
                continue

            # -----------------------------------------------------------------
            # Persist credential before the retry so a crash mid-retry leaves a
            # recoverable PERSISTED row (§9.2 — "before the retry is attempted").
            # -----------------------------------------------------------------
            credential_record: CredentialRecord | None = None
            if payment_result.credential is not None and self._credential_store is not None:
                try:
                    credential_record = await self._credential_store.persist(
                        request_id=request_id,
                        rail=adapter.rail,
                        challenge_url=str(request.url),
                        payload=payment_result.credential,
                        expires_at=challenge.expires_at,
                    )
                except Exception:
                    _log.exception(
                        "Credential persistence failed; retry proceeds without recovery tracking."
                    )

            # Build the retry request, adding the payment header if present.
            retry_headers = dict(request.headers)
            if payment_result.header_name is not None and payment_result.header_value is not None:
                retry_headers[payment_result.header_name] = payment_result.header_value
            retry = httpx.Request(
                method=request.method,
                url=request.url,
                headers=retry_headers,
                content=request.content,
                extensions=request.extensions,
            )
            ts_retry = datetime.now(UTC)

            # -----------------------------------------------------------------
            # Yield retry — may raise on transport error.
            # On transport error: rollback, emit error trace, attempt failover.
            # -----------------------------------------------------------------
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
                if credential_record is not None and self._recoverer is not None:
                    try:
                        await self._recoverer.attempt_recovery(
                            credential_record.credential_id, last_response=None
                        )
                    except Exception:
                        _log.exception("Credential recovery attempt failed after transport error.")
                ts_end = datetime.now(UTC)
                if self._emitter:
                    await self._emitter.emit_error(
                        request=request,
                        response=response,
                        error=exc,
                        challenge=challenge,
                        ts_start=ts_start,
                        ts_end=ts_end,
                        fallback_from=choice.fallback_from,
                    )
                self._sticky_cache.forget(sticky_key)
                excluded_rails = excluded_rails | {adapter.rail}
                prior_rail = adapter.rail
                attempt += 1
                continue

            ts_end = datetime.now(UTC)

            # Tag the final response so the passthrough hook skips it.
            final_response.extensions["routewiler_emitted"] = True

            # -----------------------------------------------------------------
            # Phase 4a: Credential lifecycle transitions.
            # Split-URL recovery runs here — BEFORE the budget confirm/rollback —
            # so that a successful recovery substitutes the canonical response that
            # drives the budget decision (confirm on 2xx, rollback on 4xx/5xx).
            # CredentialRecoverer handles all state transitions internally:
            #   PERSISTED → RECOVERING → REDEEMED | MANUAL_HOLD
            # -----------------------------------------------------------------
            # ``budget_response`` is the response we use for budget and trace.
            # A successful split-URL recovery replaces it with the recovered 2xx.
            budget_response = final_response

            if credential_record is not None:
                if final_response.status_code < HTTP_CLIENT_ERROR_THRESHOLD:
                    # Direct 2xx from the original retry — credential is consumed.
                    if self._credential_store is not None:
                        try:
                            await self._credential_store.transition(
                                credential_record.credential_id,
                                to_state=CredentialState.REDEEMED,
                            )
                        except Exception:
                            _log.exception(
                                "Credential REDEEMED transition failed; credential stays PERSISTED."
                            )
                elif self._recoverer is not None:
                    # 4xx/5xx: attempt split-URL recovery.
                    # On success the recoverer transitions REDEEMED internally.
                    # On exhaustion it transitions MANUAL_HOLD and emits a trace event.
                    try:
                        outcome = await self._recoverer.attempt_recovery(
                            credential_record.credential_id,
                            last_response=final_response,
                        )
                        if outcome.succeeded and outcome.response is not None:
                            # Tag the recovered response so _traced() does not double-emit.
                            outcome.response.extensions["routewiler_emitted"] = True
                            # Signal client._traced() to return this response to the caller.
                            final_response.extensions["routewiler_recovered_response"] = (
                                outcome.response
                            )
                            # Use the recovered 2xx for budget confirm and trace emission.
                            budget_response = outcome.response
                    except Exception:
                        _log.exception("Credential recovery attempt failed.")

            # -----------------------------------------------------------------
            # Phase 4b: confirm or rollback the draw based on post-recovery status.
            # -----------------------------------------------------------------
            if receipt is not None and self._budget_store is not None:
                if budget_response.status_code < HTTP_CLIENT_ERROR_THRESHOLD:
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

            # Update sticky cache on successful payment.
            self._sticky_cache.remember(sticky_key, adapter.rail, challenge.expires_at)

            if self._emitter:
                settlement = await adapter.confirm(payment_result, budget_response)
                await self._emitter.emit_paid(
                    request=request,
                    challenge=challenge,
                    payment_result=payment_result,
                    settlement=settlement,
                    final_response=budget_response,
                    ts_start=ts_start,
                    ts_retry=ts_retry,
                    ts_end=ts_end,
                    fallback_from=choice.fallback_from,
                    snapshot_rates=fmv_snapshot,
                )

            return  # success — exit the failover loop


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _origin(url: httpx.URL) -> str:
    """Return the origin of a URL as "{scheme}://{host}:{port}"."""
    default_port = 443 if url.scheme == "https" else 80
    port = url.port if url.port is not None else default_port
    return f"{url.scheme}://{url.host}:{port}"


def _make_idempotency_key(request_id: str, attempt: int) -> str:
    """Deterministic idempotency key derived from (request_id, attempt).

    Using SHA-256 means a retried failover with the same (request_id, attempt)
    naturally collapses to the same draw (§7.3: "naturally idempotent").
    """
    return hashlib.sha256(f"{request_id}:{attempt}".encode()).hexdigest()
