"""Unit tests for StripeFundingSource, SptCreator, and Funding.stripe() factory.

No live Stripe API calls — FakeSptCreator is used throughout.
"""

from __future__ import annotations

import pytest

from routewiler.funding import Funding
from routewiler.funding.stripe import SptCreator, StripeFundingSource, StripeSptCreator
from tests.fixtures.fake_stripe import FAKE_SPT_ID, FakeSptCreator

# ---------------------------------------------------------------------------
# StripeFundingSource construction
# ---------------------------------------------------------------------------


def test_stripe_funding_source_fields() -> None:
    fake = FakeSptCreator()
    source = StripeFundingSource(
        api_key="sk_test_abc",
        customer="cus_123",
        payment_method="pm_456",
        currency="usd",
        spt_creator=fake,
    )
    assert source.api_key == "sk_test_abc"
    assert source.customer == "cus_123"
    assert source.payment_method == "pm_456"
    assert source.currency == "usd"
    assert source.spt_creator is fake


def test_stripe_funding_source_default_creator_is_stripe_spt_creator() -> None:
    source = StripeFundingSource(
        api_key="sk_test_abc",
        customer="cus_123",
        payment_method="pm_456",
        currency="usd",
    )
    assert isinstance(source.spt_creator, StripeSptCreator)


def test_stripe_funding_source_is_immutable() -> None:
    source = StripeFundingSource(
        api_key="sk_test_abc",
        customer="cus_123",
        payment_method="pm_456",
        currency="usd",
        spt_creator=FakeSptCreator(),
    )
    with pytest.raises((AttributeError, TypeError)):
        source.customer = "cus_CHANGED"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SptCreator Protocol conformance
# ---------------------------------------------------------------------------


def test_fake_spt_creator_satisfies_protocol() -> None:
    fake = FakeSptCreator()
    assert isinstance(fake, SptCreator)


def test_stripe_spt_creator_satisfies_protocol() -> None:
    creator = StripeSptCreator(api_key="sk_test_abc")
    assert isinstance(creator, SptCreator)


# ---------------------------------------------------------------------------
# FakeSptCreator behaviour
# ---------------------------------------------------------------------------


async def test_fake_spt_creator_returns_fake_id() -> None:
    fake = FakeSptCreator()
    spt_id = await fake.create_spt(
        usage_limits={"currency": "usd", "max_amount": 100, "expires_at": 9999999999},
        seller_details={},
        payment_method="pm_test",
        customer="cus_test",
    )
    assert spt_id == FAKE_SPT_ID


async def test_fake_spt_creator_tracks_call_count() -> None:
    fake = FakeSptCreator()
    assert fake.call_count == 0
    await fake.create_spt(
        usage_limits={"currency": "usd", "max_amount": 100, "expires_at": 9999999999},
        seller_details={},
        payment_method="pm_test",
        customer="cus_test",
    )
    assert fake.call_count == 1


async def test_fake_spt_creator_stores_last_kwargs() -> None:
    fake = FakeSptCreator()
    await fake.create_spt(
        usage_limits={"currency": "eur", "max_amount": 200, "expires_at": 9999999999},
        seller_details={"account": "acct_x"},
        payment_method="pm_eu",
        customer="cus_eu",
    )
    assert fake.last_kwargs["payment_method"] == "pm_eu"
    assert fake.last_kwargs["customer"] == "cus_eu"
    assert fake.last_kwargs["usage_limits"]["currency"] == "eur"


async def test_fake_spt_creator_raises_on_demand() -> None:
    err = RuntimeError("test error")
    fake = FakeSptCreator(fail_with=err)
    with pytest.raises(RuntimeError, match="test error"):
        await fake.create_spt(
            usage_limits={},
            seller_details={},
            payment_method="pm_x",
            customer="cus_x",
        )


# ---------------------------------------------------------------------------
# Funding.stripe() factory
# ---------------------------------------------------------------------------


def test_funding_stripe_factory_creates_source() -> None:
    fake = FakeSptCreator()
    source = Funding.stripe(
        api_key="sk_test_factory",
        customer="cus_factory",
        payment_method="pm_factory",
        spt_creator=fake,
    )
    assert isinstance(source, StripeFundingSource)
    assert source.currency == "usd"  # default
    assert source.spt_creator is fake


def test_funding_stripe_factory_custom_currency() -> None:
    source = Funding.stripe(
        api_key="sk_test_factory",
        customer="cus_factory",
        payment_method="pm_factory",
        currency="eur",
        spt_creator=FakeSptCreator(),
    )
    assert source.currency == "eur"


def test_funding_stripe_factory_no_creator_builds_stripe_spt_creator() -> None:
    source = Funding.stripe(
        api_key="sk_test_factory",
        customer="cus_factory",
        payment_method="pm_factory",
    )
    assert isinstance(source.spt_creator, StripeSptCreator)


# ---------------------------------------------------------------------------
# Secret field repr exclusion
# ---------------------------------------------------------------------------


def test_stripe_api_key_excluded_from_repr() -> None:
    """api_key must not appear in repr() to prevent accidental secret leakage in logs."""
    source = StripeFundingSource(
        api_key="sk_live_supersecret",
        customer="cus_123",
        payment_method="pm_456",
        currency="usd",
        spt_creator=FakeSptCreator(),
    )
    r = repr(source)
    assert "sk_live_supersecret" not in r
    # Other fields should still be visible.
    assert "cus_123" in r
