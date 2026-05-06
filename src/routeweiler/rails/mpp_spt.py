"""MPP-SPT rail adapter — Stripe Shared Payment Token for fiat payments.

Implements ``RailAdapter`` for the MPP ``stripe`` and ``card`` charge methods
(IETF ``draft-httpauth-payment-00``, https://paymentauth.org).  Mints a scoped
``spt_<id>`` from the buyer's saved Stripe payment method; the merchant redeems
it via a Stripe ``PaymentIntent``.

References:
    - Stripe SPT: https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import httpx

from routeweiler.errors import (
    ChallengeParseError,
    NoFundingForRailError,
    SptCreationError,
)
from routeweiler.funding.stripe import StripeFundingSource
from routeweiler.normalized import (
    MppSptRailRaw,
    NormalizedChallenge,
    Payee,
    Price,
    ProofType,
    Rail,
)
from routeweiler.rails._mpp_http import (
    AUTHORIZATION,
    WWW_AUTHENTICATE,
    build_mpp_credential,
    compute_mpp_expiry,
    confirm_mpp_receipt,
    is_mpp_payment_for,
    parse_mpp_envelope,
    parse_required_request_fields,
)
from routeweiler.rails.base import PaymentResult, SettlementInfo, resource_from_request

if TYPE_CHECKING:
    from routeweiler.funding import FundingSource

_HANDLED_METHODS = {"stripe", "card"}

# Stripe's documented minimum per-call amount for fiat currencies (minor units).
# Applies to known fiat ISO codes; leave non-fiat/exotic currencies unchecked.
_STRIPE_FIAT_MIN_MINOR_UNITS = 50
_STRIPE_KNOWN_FIATS = {"usd", "eur", "gbp", "cad", "aud", "nzd", "chf", "sek", "dkk", "nok"}

_log = logging.getLogger(__name__)


def _human_fiat(amount_minor: int, iso_currency: str) -> str:
    """Human-readable fiat amount, e.g. ``"$5.00"`` or ``"¥500"``."""
    symbols = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥"}
    sym = symbols.get(iso_currency.lower(), iso_currency.upper() + " ")
    if iso_currency.lower() == "jpy":
        return f"{sym}{amount_minor}"
    return f"{sym}{amount_minor / 100:.2f}"


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

    def can_handle(self, response: httpx.Response) -> bool:
        """Return True for a 402 with ``WWW-Authenticate: Payment method=stripe|card``."""
        return is_mpp_payment_for(response, _HANDLED_METHODS)

    def parse(self, request: httpx.Request, response: httpx.Response) -> NormalizedChallenge:
        """Decode the MPP-SPT 402 challenge into a ``NormalizedChallenge``.

        Raises:
            ChallengeParseError:   Malformed header, missing required fields.
            ChallengeExpiredError: Challenge ``expires`` is in the past.
        """
        header = response.headers.get(WWW_AUTHENTICATE, "")
        challenge_id, req, params = parse_mpp_envelope(header, rail_prefix="MPP-SPT")

        parse_required_request_fields(
            req, fields=("amount", "currency", "recipient"), rail_label="MPP-SPT"
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
        if iso_currency in _STRIPE_KNOWN_FIATS and amount < _STRIPE_FIAT_MIN_MINOR_UNITS:
            raise ChallengeParseError(
                f"MPP-SPT: amount {amount} {raw_currency.upper()} is below the minimum "
                f"{_STRIPE_FIAT_MIN_MINOR_UNITS} minor units required for fiat charges"
            )
        recipient: str = req["recipient"]

        method_details: dict[str, Any] = req.get("methodDetails", {})
        payment_method_hint: str | None = (
            method_details.get("paymentMethodHint") or params.get("payment_method_hint") or None
        )
        seller_details: dict[str, Any] = method_details.get("sellerDetails", {})
        if not seller_details and recipient:
            seller_details = {"account": recipient}

        expires_at = compute_mpp_expiry(params, challenge_id, rail_prefix="MPP-SPT")

        raw = MppSptRailRaw(
            kind="mpp-spt",
            seller_details=seller_details,
            payment_method_hint=payment_method_hint,
            auth_params=dict(params),
            extra={
                "iso_currency": iso_currency,
                "amount": amount,
                "recipient": recipient,
            },
        )

        return NormalizedChallenge(
            rail="mpp-spt",
            resource=resource_from_request(request),
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
        _log.debug(
            "pay: rail=%s nonce=%s amount=%s", self.rail, challenge.nonce, challenge.price.amount
        )

        spt_raw = cast(MppSptRailRaw, challenge.raw)
        source = self.match_funding(challenge, self._funding)
        if source is None:
            iso = spt_raw.extra.get("iso_currency", "?")
            available = [f.currency for f in self._funding if isinstance(f, StripeFundingSource)]
            raise NoFundingForRailError(
                f"No StripeFundingSource matches currency={iso!r}. "
                f"Available currencies: {available}"
            )

        iso_currency: str = spt_raw.extra.get("iso_currency", source.currency)
        seller_details: dict[str, Any] = spt_raw.seller_details

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
        _, header_value = build_mpp_credential(
            challenge_id=challenge.nonce,
            auth_params=spt_raw.auth_params,
            default_method="stripe",
            payload={"type": "shared_payment_granted_token", "id": spt_id},
            source=f"stripe:customer:{source.customer}",
        )

        persisted: dict[str, Any] = {
            "charge_id": challenge.nonce,
            "spt_id": spt_id,
            "expires_at_unix": int(challenge.expires_at.timestamp()),
            "currency": iso_currency,
            "amount": challenge.price.amount,
        }

        return PaymentResult(
            header_name=AUTHORIZATION,
            header_value=header_value,
            credential=persisted,
            proof_type=self.proof_type,  # "spt_id"
            proof_value=spt_id,
        )

    async def confirm(
        self,
        result: PaymentResult,
        response: httpx.Response,
    ) -> SettlementInfo:
        """Parse the ``Payment-Receipt`` header and return settlement info.

        Returns:
            ``SettlementInfo`` where ``tx_hash`` is the merchant's
            ``PaymentIntent`` reference.

        Raises:
            MppReceiptVerificationError: Receipt is malformed or mismatches
                                         the credential we sent.
        """
        _log.debug("confirm: status=%d", response.status_code)
        return confirm_mpp_receipt(
            result,
            response,
            expected_methods={"stripe", "card"},
            network_id="stripe",
            rail_prefix="MPP-SPT",
            facilitator="stripe",
        )
