"""MPP-SPT rail adapter — Stripe Shared Payment Token for fiat payments.

Implements ``RailAdapter`` for the MPP ``stripe`` (and ``card``) charge method.
This is the fiat fallback path: when a vendor's 402 advertises ``method=stripe``
or ``method=card``, Routewiler mints a scoped ``spt_<id>`` from the buyer's
saved Stripe payment method and delivers it in the ``Authorization: Payment``
retry header.  The merchant redeems the SPT server-side via a Stripe
``PaymentIntent``; Routewiler does not participate in redemption.

Flow:
    1. ``can_handle``   — 402 with ``WWW-Authenticate: Payment method=stripe|card``.
    2. ``parse``        — decode auth-params + JCS-JSON request into
                          ``NormalizedChallenge`` (``price.currency = "<iso>-fiat"``).
    3. ``match_funding``— find a ``StripeFundingSource`` whose currency matches
                          the challenge's ISO currency (+ optional payment-method hint).
    4. ``pay``          — call ``SptCreator.create_spt``; build MPP credential;
                          return ``Authorization: Payment <b64url(JCS)>``.
    5. ``confirm``      — decode ``Payment-Receipt`` (method=stripe|card); return
                          ``SettlementInfo`` with the PaymentIntent reference.

Week 14 scope:
    - ``exact`` scheme only (single SPT per challenge).
    - ``method=stripe`` and ``method=card`` accepted (both route here).
    - ``method=tempo`` is left for ``MppTempoAdapter``.

References:
    - MPP draft: https://paymentauth.org
    - Stripe SPT: https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from routewiler._constants import HTTP_STATUS_PAYMENT_REQUIRED
from routewiler.errors import (
    ChallengeExpiredError,
    ChallengeParseError,
    MppReceiptVerificationError,
    NoFundingForRailError,
    SptCreationError,
)
from routewiler.funding.stripe import StripeFundingSource
from routewiler.normalized import (
    MppSptRailRaw,
    NormalizedChallenge,
    Payee,
    Price,
    ProofType,
    Rail,
    Resource,
)
from routewiler.rails._mpp_http import (
    PAYMENT_RECEIPT,
    WWW_AUTHENTICATE,
    build_authorization_header,
    decode_request_param,
    parse_payment_challenge,
    parse_payment_receipt,
)
from routewiler.rails.base import PaymentResult, RailAdapter, SettlementInfo

if TYPE_CHECKING:
    from routewiler.budgets.schema import DrawReceipt
    from routewiler.funding import FundingSource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WWW_AUTH_HEADER = WWW_AUTHENTICATE  # "www-authenticate" (httpx lower-cases)
_PAYMENT_RECEIPT_HEADER = PAYMENT_RECEIPT  # "payment-receipt"
_AUTHORIZATION_HEADER = "Authorization"

_HANDLED_METHODS = {"stripe", "card"}

# When the 402 doesn't include an `expires` param, default to 5 minutes.
_DEFAULT_VALIDITY_SECONDS = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _human_fiat(amount_minor: int, iso_currency: str) -> str:
    """Human-readable fiat amount, e.g. ``"$5.00"`` or ``"¥500"``."""
    symbols = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥"}
    sym = symbols.get(iso_currency.lower(), iso_currency.upper() + " ")
    if iso_currency.lower() == "jpy":
        return f"{sym}{amount_minor}"
    return f"{sym}{amount_minor / 100:.2f}"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MppSptAdapter:
    """MPP-SPT rail adapter.

    Pass a list of ``StripeFundingSource`` objects (one per supported currency /
    payment-method combination).  The adapter selects the best match per
    challenge in ``match_funding``.
    """

    rail: Rail = "mpp-spt"
    proof_type: ProofType = "spt_id"

    def __init__(self, funding_sources: list[StripeFundingSource]) -> None:
        self._funding = funding_sources

    # ------------------------------------------------------------------
    # RailAdapter protocol
    # ------------------------------------------------------------------

    def can_handle(self, response: httpx.Response) -> bool:
        """Return True for a 402 with ``WWW-Authenticate: Payment method=stripe|card``."""
        if response.status_code != HTTP_STATUS_PAYMENT_REQUIRED:
            return False
        header = response.headers.get(_WWW_AUTH_HEADER, "")
        if not header:
            return False
        if not header.strip().lower().startswith("payment"):
            return False
        try:
            params = parse_payment_challenge(header)
        except Exception:
            return False
        return params.get("method", "").lower() in _HANDLED_METHODS

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        """Decode the MPP-SPT 402 challenge into a ``NormalizedChallenge``.

        Raises:
            ChallengeParseError:   Malformed header, missing required fields.
            ChallengeExpiredError: Challenge ``expires`` is in the past.
        """
        header = response.headers.get(_WWW_AUTH_HEADER, "")
        try:
            params = parse_payment_challenge(header)
        except Exception as exc:
            raise ChallengeParseError(f"MPP-SPT: malformed WWW-Authenticate: {exc}") from exc

        challenge_id = params.get("id", "")
        if not challenge_id:
            raise ChallengeParseError("MPP-SPT: missing 'id' auth-param")

        request_b64 = params.get("request", "")
        if not request_b64:
            raise ChallengeParseError("MPP-SPT: missing 'request' auth-param")
        try:
            req = decode_request_param(request_b64)
        except Exception as exc:
            raise ChallengeParseError(f"MPP-SPT: failed to decode 'request': {exc}") from exc

        for required_field in ("amount", "currency", "recipient"):
            if required_field not in req:
                raise ChallengeParseError(
                    f"MPP-SPT: 'request' missing required field '{required_field}'"
                )

        try:
            amount = int(req["amount"])
        except (ValueError, TypeError) as exc:
            raise ChallengeParseError(
                f"MPP-SPT: 'amount' must be a base-10 integer string: {exc}"
            ) from exc
        if amount < 0:
            raise ChallengeParseError(f"MPP-SPT: 'amount' must be non-negative, got {amount}")

        raw_currency: str = req["currency"]
        if not (2 <= len(raw_currency) <= 4 and raw_currency.replace("-", "").isalpha()):
            raise ChallengeParseError(
                f"MPP-SPT: 'currency' must be a 2-4 char ISO-4217 code, got {raw_currency!r}"
            )
        iso_currency = raw_currency.lower()
        recipient: str = req["recipient"]

        method_details: dict[str, Any] = req.get("methodDetails", {})
        payment_method_hint: str | None = (
            method_details.get("paymentMethodHint") or params.get("payment_method_hint") or None
        )
        seller_details: dict[str, Any] = method_details.get("sellerDetails", {})
        if not seller_details and recipient:
            seller_details = {"account": recipient}

        # Expiry
        expires_str = params.get("expires", "")
        if expires_str:
            try:
                expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ChallengeParseError(
                    f"MPP-SPT: could not parse 'expires' value {expires_str!r}: {exc}"
                ) from exc
        else:
            expires_at = datetime.fromtimestamp(time.time() + _DEFAULT_VALIDITY_SECONDS, tz=UTC)

        if datetime.now(tz=UTC) >= expires_at:
            raise ChallengeExpiredError(
                f"MPP-SPT challenge {challenge_id!r} expired at {expires_at.isoformat()}"
            )

        raw = MppSptRailRaw(
            kind="mpp-spt",
            seller_details=seller_details,
            payment_method_hint=payment_method_hint,
            extra={
                "iso_currency": iso_currency,
                "amount": amount,
                "recipient": recipient,
                "auth_params": dict(params),
            },
        )

        return NormalizedChallenge(
            rail="mpp-spt",
            resource=Resource(
                method=request.method,
                url=str(request.url),
                url_encoding="raw",
                original_status=402,
            ),
            price=Price(
                amount=amount,
                currency=f"{iso_currency}-fiat",
                human_amount=_human_fiat(amount, iso_currency),
            ),
            payee=Payee(
                identifier=recipient,
                metadata={
                    "iso_currency": iso_currency,
                    "seller_details": seller_details,
                    "payment_method_hint": payment_method_hint,
                },
            ),
            scheme="exact",
            nonce=challenge_id,
            expires_at=expires_at,
            raw=raw,
        )

    def match_funding(
        self,
        challenge: NormalizedChallenge,
        funding: Sequence[FundingSource],
    ) -> StripeFundingSource | None:
        """Return the best-matching ``StripeFundingSource`` for this challenge.

        Matching rules (in order):
        1. Source must be a ``StripeFundingSource``.
        2. Source ``currency`` must match the challenge ISO currency.
        3. If a ``paymentMethodHint`` is present, prefer the source whose
           ``payment_method`` equals the hint (first such match returned).
        4. Otherwise return the first currency-matching source.
        """
        if not isinstance(challenge.raw, MppSptRailRaw):
            return None

        iso_currency: str = challenge.raw.extra.get("iso_currency", "")
        pm_hint: str | None = challenge.raw.payment_method_hint

        if pm_hint:
            for fs in funding:
                if isinstance(fs, StripeFundingSource) and (
                    fs.currency.lower() == iso_currency.lower() and fs.payment_method == pm_hint
                ):
                    return fs

        for fs in funding:
            if isinstance(fs, StripeFundingSource) and (
                fs.currency.lower() == iso_currency.lower()
            ):
                return fs

        return None

    async def pay(
        self,
        challenge: NormalizedChallenge,
        receipt: DrawReceipt | None = None,
    ) -> PaymentResult:
        """Mint a Stripe SPT and build the MPP credential.

        Steps:
            1. Locate the matching ``StripeFundingSource``.
            2. Build ``usage_limits`` from challenge amount/currency/expires_at.
            3. Call ``SptCreator.create_spt``; wrap failures in ``SptCreationError``.
            4. Build the MPP credential dict with ``payload.type =
               "shared_payment_granted_token"``.
            5. Return ``PaymentResult(proof_type="spt_id", proof_value=spt_id)``.

        Raises:
            NoFundingForRailError: No matching ``StripeFundingSource``.
            SptCreationError:      Stripe API call failed.
        """
        assert isinstance(challenge.raw, MppSptRailRaw), "pay() called with non-MPP-SPT challenge"

        source = self.match_funding(challenge, self._funding)
        if source is None:
            iso = challenge.raw.extra.get("iso_currency", "?")
            available = [f.currency for f in self._funding if isinstance(f, StripeFundingSource)]
            raise NoFundingForRailError(
                f"No StripeFundingSource matches currency={iso!r}. "
                f"Available currencies: {available}"
            )

        iso_currency: str = challenge.raw.extra.get("iso_currency", source.currency)
        seller_details: dict[str, Any] = challenge.raw.seller_details
        auth_params: dict[str, Any] = challenge.raw.extra.get("auth_params", {})

        usage_limits: dict[str, Any] = {
            "currency": iso_currency,
            "max_amount": challenge.price.amount,
            "expires_at": int(challenge.expires_at.timestamp()),
        }

        try:
            spt_id = await source.spt_creator.create_spt(
                usage_limits=usage_limits,
                seller_details=seller_details,
                payment_method=source.payment_method,
                customer=source.customer,
            )
        except Exception as exc:
            raise SptCreationError(
                f"MPP-SPT: Stripe SPT creation failed for challenge {challenge.nonce!r}: {exc}"
            ) from exc

        # Build the MPP credential per draft-httpauth-payment-00.
        challenge_echo: dict[str, Any] = {
            "id": challenge.nonce,
            "realm": auth_params.get("realm", ""),
            "method": auth_params.get("method", "stripe"),
            "intent": auth_params.get("intent", "charge"),
            "request": auth_params.get("request", ""),
            "expires": auth_params.get("expires", ""),
            "opaque": auth_params.get("opaque", ""),
        }
        # Strip empty-string keys to keep the credential blob compact.
        challenge_echo = {k: v for k, v in challenge_echo.items() if v != ""}

        credential: dict[str, Any] = {
            "challenge": challenge_echo,
            "payload": {
                "type": "shared_payment_granted_token",
                "id": spt_id,
            },
            "source": f"stripe:customer:{source.customer}",
        }

        header_value = build_authorization_header(credential)

        persisted: dict[str, Any] = {
            "charge_id": challenge.nonce,
            "spt_id": spt_id,
            "expires_at_unix": int(challenge.expires_at.timestamp()),
            "currency": iso_currency,
            "amount": challenge.price.amount,
        }

        return PaymentResult(
            header_name=_AUTHORIZATION_HEADER,
            header_value=header_value,
            credential=persisted,
            proof_type=self.proof_type,  # "spt_id"
            proof_value=spt_id,
        )

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo | None:
        """Parse the ``Payment-Receipt`` header and return settlement info.

        Returns:
            ``SettlementInfo`` where ``tx_hash`` is the merchant's
            ``PaymentIntent`` reference.

        Raises:
            MppReceiptVerificationError: Receipt is malformed or mismatches
                                         the credential we sent.
        """
        receipt_header = response.headers.get(_PAYMENT_RECEIPT_HEADER, "")
        if not receipt_header:
            return SettlementInfo(
                success=response.is_success,
                tx_hash=result.proof_value,
                network_id="stripe",
                payer_address=None,
                amount_paid=None,
            )

        try:
            receipt = parse_payment_receipt(receipt_header)
        except Exception as exc:
            raise MppReceiptVerificationError(
                f"MPP-SPT: failed to decode Payment-Receipt: {exc}"
            ) from exc

        expected_id = (result.credential or {}).get("charge_id", "")
        if expected_id and receipt.challenge_id != expected_id:
            raise MppReceiptVerificationError(
                f"MPP-SPT: receipt challengeId {receipt.challenge_id!r} != expected {expected_id!r}"
            )
        if receipt.method not in ("stripe", "card"):
            raise MppReceiptVerificationError(
                f"MPP-SPT: receipt method {receipt.method!r} is not 'stripe' or 'card'"
            )

        try:
            amount_paid = int(receipt.settlement.get("amount", "0"))
        except (ValueError, TypeError):
            amount_paid = None

        return SettlementInfo(
            success=receipt.status == "success" and response.is_success,
            tx_hash=receipt.reference,
            network_id="stripe",
            payer_address=None,
            amount_paid=amount_paid,
        )

    async def sign(self, challenge: NormalizedChallenge) -> str:
        raise NotImplementedError(
            "MppSptAdapter uses pay() not sign(). "
            "sign() is a legacy method for x402-style header-signing adapters."
        )

    def parse_settlement(self, response: httpx.Response) -> SettlementInfo | None:
        return None


# ---------------------------------------------------------------------------
# Verify protocol conformance at import time
# ---------------------------------------------------------------------------

assert isinstance(MppSptAdapter([]), RailAdapter), (
    "MppSptAdapter does not satisfy the RailAdapter protocol"
)
