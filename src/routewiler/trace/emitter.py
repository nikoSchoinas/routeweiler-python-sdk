"""TraceEmitter — builds and persists TraceEvent records."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import httpx

from routewiler.trace.schema import (
    FmvQuality,
    Outcome,
    OutcomeError,
    PaymentDetails,
    Reconciliation,
    TraceEvent,
)

if TYPE_CHECKING:
    from routewiler.normalized import NormalizedChallenge, UrlEncoding
    from routewiler.rails.x402 import SettlementInfo
    from routewiler.trace.sink_sqlite import SqliteTraceSink

# ---------------------------------------------------------------------------
# Known stablecoin peg assets (CAIP-19 address suffixes)
# Maps lowercase ERC-20 address → ISO-4217 peg currency.
# Extend as more stablecoins gain facilitator support.
# ---------------------------------------------------------------------------
_STABLECOIN_PEG: dict[str, str] = {
    # USDC
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "usd",  # base mainnet
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e": "usd",  # base-sepolia
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "usd",  # polygon
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": "usd",  # arbitrum
    # EURC
    "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42": "eur",  # base mainnet
}

_STABLECOIN_DECIMALS = 6  # USDC and EURC both use 6

_POLICY_HASH_PLACEHOLDER = "none"  # replaced by real SHA-256 in Week 10

_HTTP_CLIENT_ERROR_THRESHOLD = 400  # HTTP status codes below this are considered successful


class TraceEmitter:
    """Assembles TraceEvent instances and delegates persistence to a sink."""

    def __init__(
        self,
        sink: SqliteTraceSink,
        envelope_id: str,
        envelope_currency: str,
        funding_label: str,
        url_mode: UrlEncoding,
    ) -> None:
        self._sink = sink
        self._envelope_id = envelope_id
        self._envelope_currency = envelope_currency
        self._funding_label = funding_label
        self._url_mode = url_mode

    # ------------------------------------------------------------------
    # Public emit helpers
    # ------------------------------------------------------------------

    async def emit_paid(
        self,
        *,
        request: httpx.Request,
        challenge: NormalizedChallenge,
        settlement: SettlementInfo | None,
        final_response: httpx.Response,
        ts_start: datetime,
        ts_retry: datetime,
        ts_end: datetime,
    ) -> None:
        request_id = _request_id()
        settlement_ms = _ms(ts_retry, ts_end)
        total_ms = _ms(ts_start, ts_end)

        challenge = _apply_url_mode(challenge, self._url_mode)
        payment = _build_payment(challenge, settlement, settlement_ms, self._envelope_currency)
        outcome = Outcome(
            http_status=final_response.status_code,
            service_delivered=(final_response.status_code < _HTTP_CLIENT_ERROR_THRESHOLD),
            service_latency_ms=settlement_ms,
        )
        event = TraceEvent(
            request_id=request_id,
            envelope_id=self._envelope_id,
            policy_hash=_POLICY_HASH_PLACEHOLDER,
            challenge=challenge,
            selected_rail=challenge.rail,
            funding_source=self._funding_label,
            payment=payment,
            outcome=outcome,
            reconciliation=Reconciliation(vat_applicable=False),
            timestamp_start=ts_start,
            timestamp_end=ts_end,
        )
        _ = total_ms  # available for future latency breakdown fields
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
            request_id=_request_id(),
            envelope_id=self._envelope_id,
            policy_hash=_POLICY_HASH_PLACEHOLDER,
            challenge=None,
            selected_rail="none",
            funding_source=self._funding_label,
            payment=None,
            outcome=outcome,
            reconciliation=Reconciliation(vat_applicable=False),
            timestamp_start=ts_start,
            timestamp_end=ts_end,
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
    ) -> None:
        http_status = response.status_code if response is not None else 0
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
        rail = challenge.rail if challenge is not None else "none"
        event = TraceEvent(
            request_id=_request_id(),
            envelope_id=self._envelope_id,
            policy_hash=_POLICY_HASH_PLACEHOLDER,
            challenge=challenge,
            selected_rail=rail,
            funding_source=self._funding_label,
            payment=None,
            outcome=outcome,
            reconciliation=Reconciliation(vat_applicable=False),
            timestamp_start=ts_start,
            timestamp_end=ts_end,
        )
        await self._sink.emit(event)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _request_id() -> str:
    return uuid4().hex


def _apply_url_mode(challenge: NormalizedChallenge, url_mode: UrlEncoding) -> NormalizedChallenge:
    """Return a copy of the challenge with resource.url and url_encoding adjusted.

    ``"raw"``  — unchanged (fast path, no copy needed).
    ``"drop"`` — query string stripped; ``url_encoding`` set to ``"drop"``.
    ``"hash"`` — not yet implemented; gated at sink construction.
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
    # "hash" is gated at TraceSink.sqlite construction; should never reach here.
    return challenge  # pragma: no cover


def _ms(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


def _build_payment(
    challenge: NormalizedChallenge,
    settlement: SettlementInfo | None,
    settlement_latency_ms: int,
    envelope_currency: str,
) -> PaymentDetails:
    currency = challenge.price.currency
    amount_native = challenge.price.amount

    amount_envelope, fmv_quality = _fmv(currency, amount_native, envelope_currency)

    # Proof of payment from the settlement response.
    proof_type: str = "txid"
    proof_value: str | None = settlement.tx_hash if settlement is not None else None

    return PaymentDetails(
        proof_type=proof_type,
        proof_value=proof_value,
        amount_native=amount_native,
        amount_native_currency=currency,
        amount_envelope=amount_envelope,
        amount_envelope_currency=envelope_currency,
        fmv_quality=fmv_quality,
        settlement_latency_ms=settlement_latency_ms,
    )


def _fmv(
    caip19_currency: str, amount_native: int, envelope_currency: str
) -> tuple[float | None, FmvQuality]:
    """Compute FMV for trace emission (never on the call path).

    Week 3 implements the stablecoin-peg rule only.  All other assets emit
    ``(None, "unavailable")`` — CoinGecko + ECB integration ships when the
    L402 adapter (sats) lands in Week 8.
    """
    # Extract the trailing ERC-20 address from a CAIP-19 like
    # "eip155:8453/erc20:0x833589..."
    address: str | None = None
    if "/erc20:" in caip19_currency:
        address = caip19_currency.rsplit("/erc20:", maxsplit=1)[-1].lower()

    if address and address in _STABLECOIN_PEG:
        peg_currency = _STABLECOIN_PEG[address]
        if peg_currency == envelope_currency:
            # 1:1 peg — USDC has 6 decimals, EURC has 6 decimals.
            return amount_native / 10**_STABLECOIN_DECIMALS, "stablecoin_peg"
        # Stablecoin in a different-currency envelope → fx_leg needed; defer.

    return None, "unavailable"
