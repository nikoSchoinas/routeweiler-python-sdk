"""TraceEmitter — builds and persists TraceEvent records."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, assert_never
from urllib.parse import urlparse, urlunparse

import httpx

from routewiler._constants import HTTP_CLIENT_ERROR_THRESHOLD as _HTTP_CLIENT_ERROR_THRESHOLD
from routewiler.budgets.fmv import fmv_for_trace as _fmv_for_trace
from routewiler.budgets.receipts import uuid7 as _uuid7
from routewiler.trace.schema import (
    Outcome,
    OutcomeError,
    PaymentDetails,
    Reconciliation,
    TraceEvent,
)

if TYPE_CHECKING:
    from routewiler.credentials.schema import CredentialRecord, ManualHoldReason
    from routewiler.normalized import NormalizedChallenge, Rail, UrlEncoding
    from routewiler.rails.base import PaymentResult, SettlementInfo
    from routewiler.trace.sink_sqlite import SqliteTraceSink


class TraceEmitter:
    """Assembles TraceEvent instances and delegates persistence to a sink."""

    def __init__(
        self,
        sink: SqliteTraceSink,
        envelope_id: str,
        envelope_currency: str,
        funding_label: str | None,
        url_mode: UrlEncoding,
        policy_hash: str,
        agent_id: str | None = None,
    ) -> None:
        self._sink = sink
        self._envelope_id = envelope_id
        self._envelope_currency = envelope_currency
        self._funding_label = funding_label
        self._url_mode = url_mode
        self._policy_hash = policy_hash
        self._agent_id = agent_id

    # ------------------------------------------------------------------
    # Public emit helpers
    # ------------------------------------------------------------------

    async def emit_paid(
        self,
        *,
        request: httpx.Request,
        challenge: NormalizedChallenge,
        payment_result: PaymentResult,
        settlement: SettlementInfo,
        final_response: httpx.Response,
        ts_start: datetime,
        ts_retry: datetime,
        ts_end: datetime,
        fallback_from: Rail | None = None,
        snapshot_rates: dict[str, Decimal] | None = None,
    ) -> None:
        settlement_ms = _ms(ts_retry, ts_end)
        challenge = _apply_url_mode(challenge, self._url_mode)
        payment = _build_payment(
            challenge,
            payment_result,
            settlement,
            settlement_ms,
            self._envelope_currency,
            snapshot_rates,
        )
        outcome = Outcome(
            http_status=final_response.status_code,
            service_delivered=(final_response.status_code < _HTTP_CLIENT_ERROR_THRESHOLD),
            service_latency_ms=settlement_ms,
        )
        event = TraceEvent(
            **self._base_event_kwargs(),
            challenge=challenge,
            selected_rail=challenge.rail,
            fallback_from=fallback_from,
            facilitator=settlement.facilitator,
            payment=payment,
            outcome=outcome,
            timestamp_start=ts_start,
            timestamp_end=ts_end,
        )
        await self._sink.emit(event)

    async def emit_passthrough(
        self,
        *,
        request: httpx.Request,
        response: httpx.Response,
        ts_start: datetime,
        ts_end: datetime,
    ) -> None:
        service_ms = _ms(ts_start, ts_end)
        outcome = Outcome(
            http_status=response.status_code,
            service_delivered=(response.status_code < _HTTP_CLIENT_ERROR_THRESHOLD),
            service_latency_ms=service_ms,
        )
        event = TraceEvent(
            **self._base_event_kwargs(),
            challenge=None,
            selected_rail=None,
            payment=None,
            outcome=outcome,
            timestamp_start=ts_start,
            timestamp_end=ts_end,
        )
        await self._sink.emit(event)

    async def emit_credential_manual_hold(
        self,
        *,
        credential: CredentialRecord,
        reason: ManualHoldReason,
        ts: datetime,
    ) -> None:
        """Emit a trace event when a credential enters MANUAL_HOLD terminal state.

        Sets ``credential_state = 'manual_hold'`` on the event so queries on the
        trace store can identify credentials that require manual inspection.
        """
        event = TraceEvent(
            **self._base_event_kwargs(),
            challenge=None,
            selected_rail=credential.rail,
            payment=None,
            outcome=Outcome(
                http_status=None,
                service_delivered=False,
                service_latency_ms=0,
            ),
            timestamp_start=ts,
            timestamp_end=ts,
            credential_id=credential.credential_id,
            credential_state="manual_hold",
        )
        await self._sink.emit(event)

    async def emit_error(
        self,
        *,
        request: httpx.Request,
        response: httpx.Response | None,
        error: Exception,
        challenge: NormalizedChallenge | None,
        ts_start: datetime,
        ts_end: datetime,
        fallback_from: Rail | None = None,
    ) -> None:
        http_status = response.status_code if response is not None else None
        service_ms = _ms(ts_start, ts_end)
        outcome = Outcome(
            http_status=http_status,
            service_delivered=False,
            service_latency_ms=service_ms,
            error=OutcomeError(
                code=type(error).__name__,
                message=str(error),
            ),
        )
        if challenge is not None:
            challenge = _apply_url_mode(challenge, self._url_mode)
        rail = challenge.rail if challenge is not None else None
        event = TraceEvent(
            **self._base_event_kwargs(),
            challenge=challenge,
            selected_rail=rail,
            fallback_from=fallback_from,
            payment=None,
            outcome=outcome,
            timestamp_start=ts_start,
            timestamp_end=ts_end,
        )
        await self._sink.emit(event)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _base_event_kwargs(self) -> dict[str, Any]:
        """Return the TraceEvent fields shared by all emit_* methods."""
        return {
            "request_id": _request_id(),
            "agent_id": self._agent_id,
            "envelope_id": self._envelope_id,
            "policy_hash": self._policy_hash,
            "funding_source": self._funding_label,
            "reconciliation": Reconciliation(vat_applicable=False),
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _request_id() -> str:
    return _uuid7()


def _apply_url_mode(challenge: NormalizedChallenge, url_mode: UrlEncoding) -> NormalizedChallenge:
    """Return a copy of the challenge with resource.url and url_encoding adjusted.

    ``"raw"``  — unchanged (fast path, no copy needed).
    ``"drop"`` — query string stripped; ``url_encoding`` set to ``"drop"``.
    """
    if url_mode == "raw":
        return challenge
    if url_mode == "drop":
        parsed = urlparse(challenge.resource.url)
        clean_url = urlunparse(parsed._replace(query="", fragment=""))
        new_resource = challenge.resource.model_copy(
            update={"url": clean_url, "url_encoding": "drop"}
        )
        return challenge.model_copy(update={"resource": new_resource})
    assert_never(url_mode)


def _ms(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


def _build_payment(
    challenge: NormalizedChallenge,
    payment_result: PaymentResult,
    settlement: SettlementInfo,
    settlement_latency_ms: int,
    envelope_currency: str,
    snapshot_rates: dict[str, Decimal] | None = None,
) -> PaymentDetails:
    currency = challenge.price.currency
    amount_native = challenge.price.amount

    amount_envelope, fmv_quality = _fmv_for_trace(
        currency, amount_native, envelope_currency, snapshot_rates
    )

    # proof_value: prefer the value set by the rail adapter in pay() (e.g. L402 preimage);
    # fall back to the tx_hash from the server's settlement response (x402).
    proof_value = payment_result.proof_value or settlement.tx_hash

    return PaymentDetails(
        proof_type=payment_result.proof_type,
        proof_value=proof_value,
        amount_native=amount_native,
        amount_native_currency=currency,
        amount_envelope=amount_envelope,
        amount_envelope_currency=envelope_currency,
        fmv_quality=fmv_quality,
        settlement_latency_ms=settlement_latency_ms,
    )
