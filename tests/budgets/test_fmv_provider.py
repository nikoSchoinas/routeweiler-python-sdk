"""Tests for FmvProvider protocol and CoinGeckoProvider."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from routeweiler.budgets.fmv import capture_fmv_snapshot
from routeweiler.budgets.fmv_provider import _COINGECKO_URL, CoinGeckoProvider, FmvProvider
from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.errors import FmvUnavailableError

# ---------------------------------------------------------------------------
# StubFmvProvider — test double used in other test modules too
# ---------------------------------------------------------------------------


class StubFmvProvider:
    """Returns a hardcoded per-satoshi rate; injectable via BudgetStore(fmv_provider=...)."""

    def __init__(self, btc_price_by_currency: dict[str, Decimal] | None = None) -> None:
        self._prices: dict[str, Decimal] = btc_price_by_currency or {"usd": Decimal("60000")}

    async def fetch_btc_to(self, currency: str) -> Decimal:
        cur = currency.lower()
        if cur not in self._prices:
            raise FmvUnavailableError(f"StubFmvProvider: no rate for {currency!r}")
        return self._prices[cur] / Decimal("100000000")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_coingecko_satisfies_protocol() -> None:
    assert isinstance(CoinGeckoProvider(), FmvProvider)


def test_stub_satisfies_protocol() -> None:
    assert isinstance(StubFmvProvider(), FmvProvider)


# ---------------------------------------------------------------------------
# CoinGeckoProvider — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_coingecko_returns_per_sat_rate() -> None:
    respx.get(_COINGECKO_URL).mock(
        return_value=httpx.Response(200, json={"bitcoin": {"usd": 60000}})
    )
    provider = CoinGeckoProvider()
    rate = await provider.fetch_btc_to("usd")
    expected = Decimal("60000") / Decimal("100000000")
    assert rate == expected


@pytest.mark.asyncio
@respx.mock
async def test_coingecko_cache_avoids_second_request() -> None:
    route = respx.get(_COINGECKO_URL).mock(
        return_value=httpx.Response(200, json={"bitcoin": {"usd": 60000}})
    )
    provider = CoinGeckoProvider(cache_ttl_seconds=3600)
    await provider.fetch_btc_to("usd")
    await provider.fetch_btc_to("usd")
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_coingecko_non_usd_currency() -> None:
    respx.get(_COINGECKO_URL).mock(
        return_value=httpx.Response(200, json={"bitcoin": {"eur": 55000}})
    )
    provider = CoinGeckoProvider()
    rate = await provider.fetch_btc_to("EUR")
    expected = Decimal("55000") / Decimal("100000000")
    assert rate == expected


# ---------------------------------------------------------------------------
# CoinGeckoProvider — failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_coingecko_http_error_raises_fmv_unavailable() -> None:
    respx.get(_COINGECKO_URL).mock(return_value=httpx.Response(500))
    provider = CoinGeckoProvider()
    with pytest.raises(FmvUnavailableError, match="CoinGecko"):
        await provider.fetch_btc_to("usd")


@pytest.mark.asyncio
@respx.mock
async def test_coingecko_network_error_raises_fmv_unavailable() -> None:
    respx.get(_COINGECKO_URL).mock(side_effect=httpx.ConnectError("timeout"))
    provider = CoinGeckoProvider()
    with pytest.raises(FmvUnavailableError, match="CoinGecko"):
        await provider.fetch_btc_to("usd")


# ---------------------------------------------------------------------------
# StubFmvProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_returns_correct_per_sat_rate() -> None:
    provider = StubFmvProvider({"usd": Decimal("60000")})
    rate = await provider.fetch_btc_to("usd")
    assert rate == Decimal("60000") / Decimal("100000000")


@pytest.mark.asyncio
async def test_stub_raises_for_unknown_currency() -> None:
    provider = StubFmvProvider({"usd": Decimal("60000")})
    with pytest.raises(FmvUnavailableError):
        await provider.fetch_btc_to("jpy")


# ---------------------------------------------------------------------------
# Integration: capture_fmv_snapshot with sats rates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_includes_sats_rate_when_provided() -> None:
    btc_price = Decimal("60000")
    sats_rate = btc_price / Decimal("100000000")
    snapshot_rates, snapshot_quality = capture_fmv_snapshot(
        "usd",
        sats_rates={"sats->usd": sats_rate},
    )
    assert "sats->usd" in snapshot_rates
    assert snapshot_rates["sats->usd"] == sats_rate
    assert snapshot_quality["sats->usd"] == "coingecko_simple"


@pytest.mark.asyncio
async def test_snapshot_omits_sats_rate_when_no_provider() -> None:
    snapshot_rates, _ = capture_fmv_snapshot("usd")
    assert "sats->usd" not in snapshot_rates


@pytest.mark.asyncio
async def test_budget_store_create_envelope_seeds_sats_rate(tmp_path: pytest.fixture) -> None:
    """BudgetStore.create_envelope writes sats rate into FMV snapshot for l402 envelopes."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")
    provider = StubFmvProvider({"usd": Decimal("60000")})

    store = BudgetStore(db_path, keystore, fmv_provider=provider)
    await store.create_envelope(
        "test-env",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["l402", "x402"],  # l402 triggers the BTC rate fetch
        ttl_seconds=3600,
    )

    snapshot = store.load_fmv_snapshot_sync("test-env")
    assert snapshot is not None
    assert "sats->usd" in snapshot
    expected = Decimal("60000") / Decimal("100000000")
    assert snapshot["sats->usd"] == expected
    await store.aclose()


@pytest.mark.asyncio
async def test_budget_store_create_envelope_no_sats_for_x402_only(
    tmp_path: pytest.fixture,
) -> None:
    """Sats rate is not fetched when the envelope does not include l402."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")
    provider = StubFmvProvider({"usd": Decimal("60000")})

    store = BudgetStore(db_path, keystore, fmv_provider=provider)
    await store.create_envelope(
        "test-env",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["x402"],  # no l402 — BTC rate is irrelevant
        ttl_seconds=3600,
    )

    snapshot = store.load_fmv_snapshot_sync("test-env")
    assert snapshot is not None
    assert "sats->usd" not in snapshot
    await store.aclose()


@pytest.mark.asyncio
async def test_budget_store_create_envelope_degrades_gracefully_on_fmv_outage(
    tmp_path: pytest.fixture,
) -> None:
    """FMV provider outage at envelope creation is a warning, not a fatal error.

    The envelope is created without sats rates; L402 draws will raise
    FmvUnavailableError at draw time rather than blocking envelope creation.
    This matches the rule that only call-time cap enforcement fails closed.
    """

    class FailingProvider:
        async def fetch_btc_to(self, currency: str) -> Decimal:
            raise FmvUnavailableError("CoinGecko down")

    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")
    store = BudgetStore(db_path, keystore, fmv_provider=FailingProvider())  # type: ignore[arg-type]

    # No exception expected — envelope creation must succeed even when FMV provider is down.
    await store.create_envelope(
        "test-env",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["l402", "x402"],
        ttl_seconds=3600,
    )
    await store.aclose()

    # Envelope must exist and snapshot must lack sats rates (degraded mode).
    store2 = BudgetStore(db_path, keystore)
    snapshot = store2.load_fmv_snapshot_sync("test-env")
    await store2.aclose()
    assert snapshot is not None, "Envelope FMV snapshot must be written even on provider outage"
    assert "sats->usd" not in snapshot, "No sats rates expected when provider failed"
