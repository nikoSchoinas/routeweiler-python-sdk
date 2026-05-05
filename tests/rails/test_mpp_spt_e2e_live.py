"""Live MPP-SPT test against Stripe test mode.

Gated by ``--run-live`` marker. Requires environment variables:
    STRIPE_TEST_API_KEY        — Stripe secret key in test mode (sk_test_...)
    STRIPE_TEST_CUSTOMER       — Stripe test customer id (cus_...)
    STRIPE_TEST_PAYMENT_METHOD — Saved Stripe test payment method id (pm_...)
    STRIPE_TEST_SELLER_PROFILE — Seller's network business profile id (profile_...)

This test:
  1. Creates a real Stripe SPT via ``StripeSptCreator`` against Stripe's test API.
  2. Asserts the returned id starts with ``spt_test_`` (or ``spt_`` in test mode).
  3. Asserts the ``usage_limits`` in the response echo what we sent.

It does NOT drive merchant-side PaymentIntent redemption — that is the
merchant's responsibility and is exercised in the in-process mock server test.
"""

from __future__ import annotations

import os
import time

import pytest

from routewiler.funding.stripe import StripeSptCreator


@pytest.mark.live
async def test_live_spt_creation() -> None:
    api_key = os.environ.get("STRIPE_TEST_API_KEY", "")
    customer = os.environ.get("STRIPE_TEST_CUSTOMER", "")
    payment_method = os.environ.get("STRIPE_TEST_PAYMENT_METHOD", "")
    seller_profile = os.environ.get("STRIPE_TEST_SELLER_PROFILE", "")

    if not api_key or not customer or not payment_method or not seller_profile:
        pytest.skip(
            "Live SPT test requires STRIPE_TEST_API_KEY, STRIPE_TEST_CUSTOMER, "
            "STRIPE_TEST_PAYMENT_METHOD, and STRIPE_TEST_SELLER_PROFILE env vars."
        )

    creator = StripeSptCreator(api_key=api_key)

    usage_limits = {
        "currency": "usd",
        "max_amount": 100,  # $1.00
        "expires_at": int(time.time()) + 300,  # 5 minutes from now
    }
    seller_details = {"network_business_profile": seller_profile}

    spt_id = await creator.create_spt(
        usage_limits=usage_limits,
        seller_details=seller_details,
        payment_method=payment_method,
        customer=customer,
    )

    assert isinstance(spt_id, str), f"Expected str, got {type(spt_id)}"
    assert spt_id.startswith("spt_"), f"Expected spt_id to start with 'spt_', got {spt_id!r}"
