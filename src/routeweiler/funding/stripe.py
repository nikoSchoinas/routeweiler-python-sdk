"""Stripe funding source — wraps Stripe API for MPP-SPT payments.

MPP-SPT (Shared Payment Token) is the fiat/card fallback path for Stripe's
Machine Payments Protocol.  The Routeweiler adapter mints an SPT from the
buyer's saved Stripe payment method and passes it to the merchant; the
merchant redeems it server-side via a PaymentIntent.

The ``SptCreator`` Protocol abstracts Stripe API calls so the adapter is
testable without a live Stripe HTTP round-trip.

Public surface:
    - ``SptCreator``        — Protocol; implement to inject fakes in tests.
    - ``StripeSptCreator``  — Concrete impl; defers ``import stripe`` to call time.
    - ``StripeFundingSource`` — Dataclass; holds API key, customer, payment method.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SptCreator(Protocol):
    """Minimum interface for creating a Stripe Shared Payment Token.

    Concrete implementations:
        - ``StripeSptCreator`` — backed by the Stripe Python SDK.
        - ``FakeSptCreator``   — deterministic synthetic creator for tests.
    """

    async def create_spt(
        self,
        *,
        usage_limits: dict[str, Any],
        seller_details: dict[str, Any],
        payment_method: str,
        customer: str,
    ) -> str:
        """Create a Shared Payment Token and return its id (``spt_<...>``).

        Args:
            usage_limits:    Stripe ``usage_limits`` dict:
                             ``{"currency": "usd", "max_amount": 500, "expires_at": <unix>}``.
            seller_details:  Stripe ``seller_details`` dict (seller account id, etc.).
            payment_method:  Buyer's saved Stripe payment method id (``pm_<id>``).
            customer:        Buyer's Stripe customer id (``cus_<id>``).

        Returns:
            The SPT id string, e.g. ``"spt_01ABC..."``.

        Raises:
            Any exception from the underlying Stripe API call.  The adapter
            wraps these in ``SptCreationError``.
        """
        ...


class StripeSptCreator:
    """SptCreator backed by the Stripe Python SDK.

    Defers ``import stripe`` to ``create_spt()`` call time so users who never
    use the SPT path do not pay the import cost.

    The Stripe SPT resource (``shared_payment.issued_token``) was introduced
    alongside MPP in March 2026.  SDK >=12 exposes it via ``raw_request_async``
    since the typed resource surface may lag behind the API.  This creator calls
    ``POST /v1/shared_payment/issued_tokens`` via ``StripeClient.raw_request_async``;
    the response ``id`` field is the ``spt_<id>`` string.

    If a future SDK release adds a typed ``client.shared_payment.issued_tokens``
    resource, replace the raw_request call with the typed one — the Protocol
    interface stays the same.
    """

    _SPT_ENDPOINT = "/v1/shared_payment/issued_tokens"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def create_spt(
        self,
        *,
        usage_limits: dict[str, Any],
        seller_details: dict[str, Any],
        payment_method: str,
        customer: str,
    ) -> str:
        import stripe as _stripe  # lazy — defers cost to call time  # noqa: PLC0415

        client = _stripe.StripeClient(self._api_key)
        params: dict[str, Any] = {
            "customer": customer,
            "payment_method": payment_method,
            "usage_limits": usage_limits,
        }
        params["seller_details"] = seller_details

        resp = await client.raw_request_async("post", self._SPT_ENDPOINT, **params)
        spt_id: str = resp.data["id"]
        return spt_id


def _make_default_spt_creator(api_key: str) -> SptCreator:
    return StripeSptCreator(api_key)


@dataclass(frozen=True)
class StripeFundingSource:
    """A Stripe buyer profile used for MPP-SPT fiat payments.

    ``spt_creator`` defaults to a ``StripeSptCreator`` built from ``api_key``.
    Pass a ``FakeSptCreator`` (or any ``SptCreator``-conforming object) to
    override in tests without monkey-patching.

    Args:
        api_key:        Buyer's Stripe secret key (``sk_live_...`` or ``sk_test_...``).
        customer:       Buyer's Stripe customer id (``cus_<id>``).
        payment_method: Buyer's saved Stripe payment method id (``pm_<id>``).
        currency:       ISO-4217 lowercase currency this source covers (``"usd"``, etc.).
        spt_creator:    Injected SPT creator; defaults to ``StripeSptCreator(api_key)``.
    """

    api_key: str = field(repr=False)  # sk_live_... — excluded from repr to avoid secret leakage
    customer: str
    payment_method: str
    currency: str  # ISO-4217 lowercase: "usd", "eur", "gbp", "jpy"
    spt_creator: SptCreator = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.spt_creator is None:
            object.__setattr__(self, "spt_creator", _make_default_spt_creator(self.api_key))
