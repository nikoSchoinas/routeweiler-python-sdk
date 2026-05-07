"""Tests for the BudgetStore FMV snapshot refresh background task (Gap 4)."""

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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fmv_refresh_updates_sats_rate(tmp_path: Path) -> None:
    """Refresh loop replaces the sats rate with a new provider value."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    initial_rate = Decimal("0.00000050")  # $0.50 per sat at $50k BTC
    updated_rate = Decimal("0.00000065")  # $0.65 per sat at $65k BTC

    fmv_provider = _StubFmvProvider(initial_rate)
    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=fmv_provider,
        fmv_refresh_interval_seconds=0.05,
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
    """Refresh loop replaces cross rates when ecb_provider is configured."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    updated_ecb_rate = Decimal("0.999")  # nearly 1:1 for testing purposes
    ecb_provider = _StubEcbProvider(updated_ecb_rate)

    store = BudgetStore(
        db_path,
        keystore,
        ecb_provider=ecb_provider,
        fmv_refresh_interval_seconds=0.05,
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
async def test_fmv_refresh_loop_survives_provider_failure(tmp_path: Path) -> None:
    """Provider failure during refresh is logged and the loop continues for other envelopes."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    # Failing FMV provider — should not crash the refresh loop.
    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=_FailingFmvProvider(),
        fmv_refresh_interval_seconds=0.05,
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
        # Loop must still be alive after sleeping through multiple refresh cycles.
        await asyncio.sleep(0.2)
        assert store._fmv_task is not None
        assert not store._fmv_task.done(), "FMV refresh task should still be running after errors"
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_fmv_task_not_started_without_providers(tmp_path: Path) -> None:
    """The FMV refresh task is not created when no providers are configured."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    store = BudgetStore(db_path, keystore)
    await store.start()
    try:
        assert store._fmv_task is None, "FMV task should not start without a provider"
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_fmv_refresh_skips_expired_envelopes(tmp_path: Path) -> None:
    """The refresh query excludes envelopes that have already expired."""
    db_path = tmp_path / "test.db"
    keystore = EnvelopeKeystore(root=tmp_path / "keys")

    fmv_provider = _StubFmvProvider(Decimal("0.00000065"))
    store = BudgetStore(
        db_path,
        keystore,
        fmv_provider=fmv_provider,
        fmv_refresh_interval_seconds=0.05,
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
        "FMV provider should not refresh expired envelopes"
    )
    await store.aclose()
