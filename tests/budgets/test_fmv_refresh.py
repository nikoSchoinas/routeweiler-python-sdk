"""Tests for the BudgetStore FMV snapshot refresh background tasks."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.local import BudgetStore
from routeweiler.errors import FmvUnavailableError

# ---------------------------------------------------------------------------
# Stub providers — controllable rates for tests
# ---------------------------------------------------------------------------


class _StubFmvProvider:
    def __init__(self, rate: Decimal) -> None:
        self._rate = rate
        self.call_count = 0

    async def fetch_btc_to(self, currency: str) -> Decimal:
        self.call_count += 1
        return self._rate


class _FailingFmvProvider:
    async def fetch_btc_to(self, currency: str) -> Decimal:
        raise FmvUnavailableError("StubFmvProvider deliberately failing")


class _StubEcbProvider:
    def __init__(self, rate: Decimal) -> None:
        self._rate = rate
        self.call_count = 0

    async def fetch_rate(self, src: str, dst: str) -> Decimal:
        self.call_count += 1
        return self._rate


class _FailingEcbProvider:
    async def fetch_rate(self, src: str, dst: str) -> Decimal:
        raise FmvUnavailableError("EcbProvider deliberately failing")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_snapshot_rates(db_path: Path, envelope_id: str) -> dict[str, str]:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT rates_json FROM envelope_fmv_snapshots WHERE envelope_id=?", (envelope_id,)
    ).fetchone()
    conn.close()
    assert row is not None, f"No FMV snapshot for envelope {envelope_id!r}"
    return json.loads(row[0])  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Existing tests — updated for split-task API
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fmv_refresh_updates_sats_rate(tmp_path: Path) -> None:
    """BTC refresh loop replaces the sats rate with a new provider value."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    initial_rate = Decimal("0.00000050")  # $0.50 per sat at $50k BTC
    updated_rate = Decimal("0.00000065")  # $0.65 per sat at $65k BTC

    fmv_provider = _StubFmvProvider(initial_rate)
    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=fmv_provider,
        btc_refresh_interval_seconds=0.05,
    )
    await store.create_envelope(
        "env_l402",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["l402"],
        ttl_seconds=3600,
    )

    initial_rates = _load_snapshot_rates(db_path, "env_l402")
    assert "sats->usd" in initial_rates
    initial_sats_rate = Decimal(initial_rates["sats->usd"])

    # Switch provider to the updated rate and start the refresh task.
    fmv_provider._rate = updated_rate
    await store.start()

    try:
        await asyncio.sleep(0.15)
        refreshed_rates = _load_snapshot_rates(db_path, "env_l402")
        assert "sats->usd" in refreshed_rates
        refreshed_sats_rate = Decimal(refreshed_rates["sats->usd"])
        assert refreshed_sats_rate != initial_sats_rate
        assert refreshed_sats_rate == updated_rate
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_fmv_refresh_updates_ecb_rate(tmp_path: Path) -> None:
    """ECB refresh loop replaces cross rates when ecb_provider is configured."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    updated_ecb_rate = Decimal("0.999")  # nearly 1:1 for testing purposes
    ecb_provider = _StubEcbProvider(updated_ecb_rate)

    store = BudgetStore(
        db_path,
        keystore,
        ecb_provider=ecb_provider,
        ecb_refresh_interval_seconds=0.05,
    )
    await store.create_envelope(
        "env_usd",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    await store.start()
    try:
        await asyncio.sleep(0.15)
        refreshed_rates = _load_snapshot_rates(db_path, "env_usd")
        # eur->usd should have been refreshed to the stub rate.
        assert "eur->usd" in refreshed_rates
        assert Decimal(refreshed_rates["eur->usd"]) == updated_ecb_rate
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_btc_refresh_loop_survives_provider_failure(tmp_path: Path) -> None:
    """BTC provider failure during refresh is logged and the loop continues."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=_FailingFmvProvider(),
        btc_refresh_interval_seconds=0.05,
    )
    await store.create_envelope(
        "env_l402_fail",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["l402"],
        ttl_seconds=3600,
    )

    await store.start()
    try:
        await asyncio.sleep(0.2)
        assert store._btc_refresh_task is not None
        assert not store._btc_refresh_task.done(), (
            "BTC refresh task should still be running after errors"
        )
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_ecb_refresh_loop_survives_provider_failure(tmp_path: Path) -> None:
    """ECB provider failure during refresh is logged and the loop continues."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    store = BudgetStore(
        db_path,
        keystore,
        ecb_provider=_FailingEcbProvider(),
        ecb_refresh_interval_seconds=0.05,
    )
    await store.create_envelope(
        "env_ecb_fail",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    await store.start()
    try:
        await asyncio.sleep(0.2)
        assert store._ecb_refresh_task is not None
        assert not store._ecb_refresh_task.done(), (
            "ECB refresh task should still be running after errors"
        )
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_fmv_tasks_not_started_without_providers(tmp_path: Path) -> None:
    """Neither refresh task is created when no providers are configured."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    store = BudgetStore(db_path, keystore)
    await store.start()
    try:
        assert store._btc_refresh_task is None, "BTC task must not start without fmv_provider"
        assert store._ecb_refresh_task is None, "ECB task must not start without ecb_provider"
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_fmv_refresh_skips_expired_envelopes(tmp_path: Path) -> None:
    """The BTC refresh query excludes envelopes that have already expired."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    fmv_provider = _StubFmvProvider(Decimal("0.00000065"))
    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=fmv_provider,
        btc_refresh_interval_seconds=0.05,
    )
    # Create envelope with a 1-second TTL so it expires quickly.
    await store.create_envelope(
        "env_expiring",
        cap_minor_units=5_000,
        cap_currency="usd",
        allowed_rails=["l402"],
        ttl_seconds=1,
    )

    initial_call_count = fmv_provider.call_count
    # Sleep past expiry, then let the refresh loop run.
    await asyncio.sleep(1.1)
    await store.start()
    await asyncio.sleep(0.15)
    # Provider should not be called for the expired envelope.
    assert fmv_provider.call_count == initial_call_count, (
        "BTC FMV provider should not refresh expired envelopes"
    )
    await store.aclose()


# ---------------------------------------------------------------------------
# New tests — independent cadence, carry-forward on failure, L402 filter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_btc_and_ecb_refresh_on_independent_cadence(tmp_path: Path) -> None:
    """BTC and ECB loops run at their own intervals independently."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    fmv_provider = _StubFmvProvider(Decimal("0.00000050"))
    ecb_provider = _StubEcbProvider(Decimal("0.92"))

    # BTC fires very fast; ECB fires much slower (won't fire during this test).
    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=fmv_provider,
        ecb_provider=ecb_provider,
        btc_refresh_interval_seconds=0.05,
        ecb_refresh_interval_seconds=10.0,
    )
    await store.create_envelope(
        "env_l402",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["l402"],
        ttl_seconds=3600,
    )

    # Baseline after creation (create_envelope also hits the providers for the initial snapshot).
    btc_count_after_create = fmv_provider.call_count
    ecb_count_after_create = ecb_provider.call_count

    await store.start()
    try:
        await asyncio.sleep(0.2)  # ~4 BTC cycles, 0 ECB cycles
        btc_new_calls = fmv_provider.call_count - btc_count_after_create
        ecb_new_calls = ecb_provider.call_count - ecb_count_after_create
        assert btc_new_calls >= 2, (
            f"BTC provider should have fired multiple times via the loop, got {btc_new_calls}"
        )
        assert ecb_new_calls == 0, (
            f"ECB provider should not have fired via the loop yet, got {ecb_new_calls}"
        )
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_btc_refresh_preserves_previous_sats_rate_on_failure(tmp_path: Path) -> None:
    """When CoinGecko fails mid-refresh, the prior sats rate is retained in the snapshot."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    good_rate = Decimal("0.00000050")
    fmv_provider = _StubFmvProvider(good_rate)

    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=fmv_provider,
        btc_refresh_interval_seconds=0.05,
    )
    await store.create_envelope(
        "env_l402",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["l402"],
        ttl_seconds=3600,
    )

    # Let one successful refresh run so the snapshot has a known good rate.
    await store.start()
    await asyncio.sleep(0.15)
    rates_after_success = _load_snapshot_rates(db_path, "env_l402")
    assert "sats->usd" in rates_after_success
    assert Decimal(rates_after_success["sats->usd"]) == good_rate

    # Now swap in a failing provider and let more refresh cycles run.
    store._fmv_provider = _FailingFmvProvider()
    await asyncio.sleep(0.2)

    # The sats rate must still be present and equal to the last successful value.
    rates_after_failure = _load_snapshot_rates(db_path, "env_l402")
    assert "sats->usd" in rates_after_failure, (
        "sats->usd must be retained when the BTC provider fails"
    )
    assert Decimal(rates_after_failure["sats->usd"]) == good_rate, (
        "sats->usd must carry forward the last good rate, not be wiped"
    )
    await store.aclose()


@pytest.mark.anyio
async def test_ecb_refresh_preserves_previous_cross_rate_on_failure(tmp_path: Path) -> None:
    """When the ECB provider fails mid-refresh, the prior cross rates are retained."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    good_rate = Decimal("0.92")
    ecb_provider = _StubEcbProvider(good_rate)

    store = BudgetStore(
        db_path,
        keystore,
        ecb_provider=ecb_provider,
        ecb_refresh_interval_seconds=0.05,
    )
    await store.create_envelope(
        "env_usd",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    # Let one successful refresh write the live rate.
    await store.start()
    await asyncio.sleep(0.15)
    rates_after_success = _load_snapshot_rates(db_path, "env_usd")
    assert "eur->usd" in rates_after_success
    assert Decimal(rates_after_success["eur->usd"]) == good_rate

    # Swap in a failing provider and run more cycles.
    store._ecb_provider = _FailingEcbProvider()
    await asyncio.sleep(0.2)

    rates_after_failure = _load_snapshot_rates(db_path, "env_usd")
    assert "eur->usd" in rates_after_failure, (
        "eur->usd must be retained when the ECB provider fails"
    )
    assert Decimal(rates_after_failure["eur->usd"]) == good_rate, (
        "eur->usd must carry forward the last good rate, not fall back to offline constant"
    )
    await store.aclose()


@pytest.mark.anyio
async def test_btc_refresh_skipped_for_envelopes_without_l402(tmp_path: Path) -> None:
    """The BTC refresh loop only processes envelopes that include the l402 rail."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    fmv_provider = _StubFmvProvider(Decimal("0.00000050"))
    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=fmv_provider,
        btc_refresh_interval_seconds=0.05,
    )
    # x402-only envelope — BTC provider should never be called for it.
    await store.create_envelope(
        "env_x402",
        cap_minor_units=10_000,
        cap_currency="usd",
        allowed_rails=["x402"],
        ttl_seconds=3600,
    )

    call_count_after_create = fmv_provider.call_count
    await store.start()
    await asyncio.sleep(0.2)
    assert fmv_provider.call_count == call_count_after_create, (
        "BTC provider should not be called for non-l402 envelopes"
    )
    await store.aclose()


@pytest.mark.anyio
async def test_btc_task_only_starts_when_fmv_provider_configured(tmp_path: Path) -> None:
    """Only the ECB task starts when only ecb_provider is supplied."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    store = BudgetStore(
        db_path,
        keystore,
        ecb_provider=_StubEcbProvider(Decimal("0.92")),
    )
    await store.start()
    try:
        assert store._btc_refresh_task is None, "BTC task must not start without fmv_provider"
        assert store._ecb_refresh_task is not None, "ECB task must start when ecb_provider is set"
        assert not store._ecb_refresh_task.done()
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_ecb_task_only_starts_when_ecb_provider_configured(tmp_path: Path) -> None:
    """Only the BTC task starts when only fmv_provider is supplied."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=_StubFmvProvider(Decimal("0.00000050")),
    )
    await store.start()
    try:
        assert store._ecb_refresh_task is None, "ECB task must not start without ecb_provider"
        assert store._btc_refresh_task is not None, "BTC task must start when fmv_provider is set"
        assert not store._btc_refresh_task.done()
    finally:
        await store.aclose()
