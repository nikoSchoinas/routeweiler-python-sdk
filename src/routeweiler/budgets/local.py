"""Local SQLite budget counter — single-process, row-locked draws."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast

from routeweiler._constants import (
    FMV_REFRESH_INTERVAL_BTC_SECONDS,
    FMV_REFRESH_INTERVAL_ECB_SECONDS,
    REAPER_INTERVAL_SECONDS,
)
from routeweiler._storage import ensure_schema as _ensure_schema
from routeweiler._storage import open_connection as _open_connection
from routeweiler.budgets._draw import confirm_sync as _confirm_sync_fn
from routeweiler.budgets._draw import draw_sync as _draw_sync_fn
from routeweiler.budgets._draw import rollback_sync as _rollback_sync_fn
from routeweiler.budgets._reaper import reap_sync as _reap_sync_fn
from routeweiler.budgets._snapshot import load_fmv_snapshot_sync as _load_fmv_snapshot_sync_fn
from routeweiler.budgets._snapshot import upsert_snapshot_sync as _upsert_snapshot_sync_fn
from routeweiler.budgets._sync_reads import envelope_exists_sync as _envelope_exists_sync_fn
from routeweiler.budgets._sync_reads import (
    get_envelope_allowed_rails_sync as _get_envelope_allowed_rails_sync_fn,
)
from routeweiler.budgets._sync_reads import (
    get_envelope_currency_sync as _get_envelope_currency_sync_fn,
)
from routeweiler.budgets.ecb_provider import EcbRateProvider
from routeweiler.budgets.fmv import capture_fmv_snapshot as _capture_fmv_snapshot
from routeweiler.budgets.fmv_provider import FmvProvider
from routeweiler.budgets.keystore import EnvelopeKeystore
from routeweiler.budgets.schema import (
    BudgetEnvelope,
    DrawReceipt,
    EnvelopeCurrency,
    EnvelopeStatus,
)
from routeweiler.errors import (
    FmvUnavailableError,
)
from routeweiler.normalized import Rail

_log = logging.getLogger(__name__)

_STATUS_ACTIVE: EnvelopeStatus = "active"

# ---------------------------------------------------------------------------
# _EnvelopeRow — typed dict for SQLite envelope INSERT parameters.
# ---------------------------------------------------------------------------


class _EnvelopeRow(TypedDict):
    id: str
    cap_minor_units: int
    cap_currency: EnvelopeCurrency
    allowed_rails: str  # json.dumps(list[Rail])
    allowed_origins_glob: str  # json.dumps(list[str])
    status: EnvelopeStatus
    created_at: str  # ISO-8601 UTC
    expires_at: str  # ISO-8601 UTC
    counter_public_key: str


# ---------------------------------------------------------------------------
# Default draw TTL — 2x p99 settlement latency.
# The 30-second clock-skew buffer (CLOCK_SKEW_BUFFER_SECONDS) is added on top
# at insert time so the durable record includes the full intended window.
# ---------------------------------------------------------------------------

_DEFAULT_DRAW_TTL_SECONDS = 120


# ---------------------------------------------------------------------------
# BudgetStore
# ---------------------------------------------------------------------------


class BudgetStore:
    """Single-process SQLite budget counter.

    Uses BEGIN IMMEDIATE transactions for atomic cap-check + insert.
    Budget enforcement is always local (no hosted counter at MVP).
    Initialises the envelopes and draws tables on construction (idempotent).

    The reaper task is started by calling ``await store.start()`` once
    the event loop is running (e.g. from ``Routeweiler.__aenter__``).
    """

    def __init__(
        self,
        db_path: Path,
        keystore: EnvelopeKeystore,
        *,
        reaper_interval_seconds: float = REAPER_INTERVAL_SECONDS,
        btc_refresh_interval_seconds: float = FMV_REFRESH_INTERVAL_BTC_SECONDS,
        ecb_refresh_interval_seconds: float = FMV_REFRESH_INTERVAL_ECB_SECONDS,
        fmv_provider: FmvProvider | None = None,
        ecb_provider: EcbRateProvider | None = None,
    ) -> None:
        self._db_path = db_path
        self._keystore = keystore
        self._reaper_interval_seconds = reaper_interval_seconds
        self._btc_refresh_interval_seconds = btc_refresh_interval_seconds
        self._ecb_refresh_interval_seconds = ecb_refresh_interval_seconds
        self._fmv_provider = fmv_provider
        self._ecb_provider = ecb_provider
        self._conn = _open_connection(db_path)
        _ensure_schema(self._conn)
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._btc_refresh_task: asyncio.Task[None] | None = None
        self._ecb_refresh_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background tasks. Idempotent."""
        if self._closed:
            raise RuntimeError("BudgetStore is closed; cannot restart.")
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_loop(), name="routeweiler-reaper")
        if self._fmv_provider is not None and (
            self._btc_refresh_task is None or self._btc_refresh_task.done()
        ):
            self._btc_refresh_task = asyncio.create_task(
                self._btc_refresh_loop(), name="routeweiler-btc-fmv-refresh"
            )
        if self._ecb_provider is not None and (
            self._ecb_refresh_task is None or self._ecb_refresh_task.done()
        ):
            self._ecb_refresh_task = asyncio.create_task(
                self._ecb_refresh_loop(), name="routeweiler-ecb-fmv-refresh"
            )

    async def _reap_loop(self) -> None:
        """Roll back stale reserved draws periodically."""
        while True:
            await asyncio.sleep(self._reaper_interval_seconds)
            try:
                async with self._lock:
                    rolled = await asyncio.to_thread(self._reap_sync)
                    if rolled:
                        _log.debug("Reaper rolled back %d stale draw(s).", rolled)
            except Exception:
                _log.exception("Reaper iteration failed; will retry.")

    def _reap_sync(self) -> int:
        return _reap_sync_fn(self._conn)

    async def _btc_refresh_loop(self) -> None:
        """Re-fetch BTC/sats rates every btc_refresh_interval_seconds for L402 envelopes."""
        while True:
            await asyncio.sleep(self._btc_refresh_interval_seconds)
            try:
                await self._refresh_all_btc_snapshots()
            except Exception:
                _log.exception("BTC FMV refresh pass failed; will retry next interval.")

    async def _ecb_refresh_loop(self) -> None:
        """Re-fetch ECB cross-fiat rates every ecb_refresh_interval_seconds."""
        while True:
            await asyncio.sleep(self._ecb_refresh_interval_seconds)
            try:
                await self._refresh_all_ecb_snapshots()
            except Exception:
                _log.exception("ECB FMV refresh pass failed; will retry next interval.")

    async def _refresh_all_btc_snapshots(self) -> None:
        """Re-fetch BTC rates for all active L402 envelopes."""
        now_iso = datetime.now(UTC).isoformat()
        async with self._lock:
            rows = await asyncio.to_thread(
                lambda: self._conn.execute(
                    "SELECT id, cap_currency FROM envelopes "
                    "WHERE status=? AND expires_at > ? AND allowed_rails LIKE '%l402%'",
                    (_STATUS_ACTIVE, now_iso),
                ).fetchall()
            )
        for row in rows:
            envelope_id, cap_currency = str(row[0]), cast(EnvelopeCurrency, str(row[1]))
            try:
                await self._refresh_sats_leg(envelope_id, cap_currency)
            except Exception:
                _log.exception(
                    "BTC FMV snapshot refresh failed for envelope '%s'; skipping.", envelope_id
                )

    async def _refresh_all_ecb_snapshots(self) -> None:
        """Re-fetch ECB cross-fiat rates for all active envelopes."""
        now_iso = datetime.now(UTC).isoformat()
        async with self._lock:
            rows = await asyncio.to_thread(
                lambda: self._conn.execute(
                    "SELECT id, cap_currency FROM envelopes WHERE status=? AND expires_at > ?",
                    (_STATUS_ACTIVE, now_iso),
                ).fetchall()
            )
        for row in rows:
            envelope_id, cap_currency = str(row[0]), cast(EnvelopeCurrency, str(row[1]))
            try:
                await self._refresh_ecb_legs(envelope_id, cap_currency)
            except Exception:
                _log.exception(
                    "ECB FMV snapshot refresh failed for envelope '%s'; skipping.", envelope_id
                )

    async def _refresh_sats_leg(self, envelope_id: str, cap_currency: EnvelopeCurrency) -> None:
        """Re-fetch BTC rate and upsert only the sats leg of the snapshot.

        On provider failure the previously-persisted sats rate is carried forward
        so L402 draws are not disrupted by a transient CoinGecko outage.
        """
        assert self._fmv_provider is not None
        prior = self.load_fmv_snapshot_sync(envelope_id) or {}
        sats_key = f"sats->{cap_currency.lower()}"
        try:
            rate = await self._fmv_provider.fetch_btc_to(cap_currency)
            sats_rates = {sats_key: rate}
        except Exception:
            if sats_key in prior:
                _log.warning(
                    "Envelope '%s': BTC FMV refresh failed; carrying forward previous %s rate.",
                    envelope_id,
                    sats_key,
                )
                sats_rates = {sats_key: prior[sats_key]}
            else:
                _log.warning(
                    "Envelope '%s': BTC FMV refresh failed and no prior rate exists; "
                    "L402 draws will raise FmvUnavailableError until rates are available.",
                    envelope_id,
                )
                return

        snapshot_rates, snapshot_quality = _capture_fmv_snapshot(
            cap_currency, sats_rates=sats_rates, cross_rates=None
        )
        now_iso = datetime.now(UTC).isoformat()
        async with self._lock:
            await asyncio.to_thread(
                self._upsert_snapshot_sync,
                envelope_id,
                now_iso,
                snapshot_rates,
                snapshot_quality,
            )
        _log.debug("Envelope '%s': BTC FMV snapshot refreshed at %s.", envelope_id, now_iso)

    async def _refresh_ecb_legs(self, envelope_id: str, cap_currency: EnvelopeCurrency) -> None:
        """Re-fetch ECB cross-fiat rates and upsert only those legs of the snapshot.

        On per-pair failure the previously-persisted rate for that pair is carried forward
        (rather than falling back to the offline hardcoded constant).
        """
        assert self._ecb_provider is not None
        prior = self.load_fmv_snapshot_sync(envelope_id) or {}
        env_cur = cap_currency.lower()
        cross_rates: dict[str, Decimal] = {}
        for src in ("usd", "eur", "gbp", "jpy"):
            if src == env_cur:
                continue
            key = f"{src}->{env_cur}"
            try:
                cross_rates[key] = await self._ecb_provider.fetch_rate(src, env_cur)
            except Exception:
                if key in prior:
                    _log.warning(
                        "Envelope '%s': ECB %s→%s refresh failed; carrying forward previous rate.",
                        envelope_id,
                        src.upper(),
                        env_cur.upper(),
                    )
                    cross_rates[key] = prior[key]
                else:
                    _log.warning(
                        "Envelope '%s': ECB %s→%s refresh failed and no prior rate exists; "
                        "offline fallback will apply.",
                        envelope_id,
                        src.upper(),
                        env_cur.upper(),
                    )

        snapshot_rates, snapshot_quality = _capture_fmv_snapshot(
            cap_currency, sats_rates=None, cross_rates=cross_rates
        )
        now_iso = datetime.now(UTC).isoformat()
        async with self._lock:
            await asyncio.to_thread(
                self._upsert_snapshot_sync,
                envelope_id,
                now_iso,
                snapshot_rates,
                snapshot_quality,
            )
        _log.debug("Envelope '%s': ECB FMV snapshot refreshed at %s.", envelope_id, now_iso)

    def _upsert_snapshot_sync(
        self,
        envelope_id: str,
        captured_at: str,
        snapshot_rates: dict[str, Decimal],
        snapshot_quality: dict[str, str],
    ) -> None:
        _upsert_snapshot_sync_fn(
            self._conn, envelope_id, captured_at, snapshot_rates, snapshot_quality
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task
        if self._btc_refresh_task is not None:
            self._btc_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._btc_refresh_task
        if self._ecb_refresh_task is not None:
            self._ecb_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._ecb_refresh_task
        if self._fmv_provider is not None and hasattr(self._fmv_provider, "aclose"):
            await self._fmv_provider.aclose()
        if self._ecb_provider is not None and hasattr(self._ecb_provider, "aclose"):
            await self._ecb_provider.aclose()
        async with self._lock:
            await asyncio.to_thread(self._conn.close)

    # ------------------------------------------------------------------
    # Synchronous helpers (safe to call from a synchronous constructor)
    # ------------------------------------------------------------------

    def envelope_exists_sync(self, envelope_id: str) -> bool:
        return _envelope_exists_sync_fn(self._conn, envelope_id)

    def get_envelope_currency_sync(self, envelope_id: str) -> EnvelopeCurrency | None:
        """Return the cap_currency for an envelope, or None if not found.

        Synchronous — safe to call from the Routeweiler constructor and start().
        """
        return _get_envelope_currency_sync_fn(self._conn, envelope_id)

    def get_envelope_allowed_rails_sync(self, envelope_id: str) -> list[Rail]:
        """Return the allowed_rails list for an envelope (empty list if not found).

        Synchronous — safe to call from the Routeweiler constructor and start().
        """
        return _get_envelope_allowed_rails_sync_fn(self._conn, envelope_id)

    def load_fmv_snapshot_sync(self, envelope_id: str) -> dict[str, Decimal] | None:
        """Return the stored FMV snapshot rates for an envelope, or None if absent."""
        return _load_fmv_snapshot_sync_fn(self._conn, envelope_id)

    # ------------------------------------------------------------------
    # Async public API
    # ------------------------------------------------------------------

    async def load_fmv_snapshot(self, envelope_id: str) -> dict[str, Decimal] | None:
        """Async wrapper for load_fmv_snapshot_sync."""
        async with self._lock:
            return await asyncio.to_thread(self.load_fmv_snapshot_sync, envelope_id)

    async def create_envelope(
        self,
        envelope_id: str,
        *,
        cap_minor_units: int,
        cap_currency: EnvelopeCurrency,
        allowed_rails: list[Rail],
        allowed_origins_glob: list[str] | None = None,
        ttl_seconds: int,
        owner_agent_id: str | None = None,
    ) -> None:
        """Insert a new envelope row and create the Ed25519 keypair.

        Raises sqlite3.IntegrityError on duplicate id.
        """
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)

        # Create keypair before the DB insert so that the public key is available.
        private_key = self._keystore.create(envelope_id)
        pub_key_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()

        # Fetch per-satoshi rate for L402 budget enforcement.
        # A provider outage must not block envelope creation — log a warning and proceed
        # without sats rates; draws on l402 will raise FmvUnavailableError at runtime until
        # a new envelope is created with fresh rates (daily refresh is post-MVP).
        sats_rates: dict[str, Decimal] | None = None
        if "l402" in allowed_rails:
            if self._fmv_provider is not None:
                try:
                    rate = await self._fmv_provider.fetch_btc_to(cap_currency)
                    sats_rates = {f"sats->{cap_currency.lower()}": rate}
                except FmvUnavailableError:
                    _log.warning(
                        "Envelope '%s': BTC FMV fetch failed at creation (provider unreachable); "
                        "L402 draws will raise FmvUnavailableError until rates are available.",
                        envelope_id,
                    )
            else:
                _log.warning(
                    "Envelope '%s' includes the l402 rail but no fmv_provider was supplied; "
                    "draws on l402 will raise FmvUnavailableError at runtime.",
                    envelope_id,
                )

        # Pre-fetch live ECB cross-fiat rates for the snapshot.
        # Partial failures are logged and the offline fallback is used for that pair.
        cross_rates: dict[str, Decimal] | None = None
        if self._ecb_provider is not None:
            env_cur = cap_currency.lower()
            known_fiats = {"usd", "eur", "gbp", "jpy"}
            cross_rates = {}
            for src in known_fiats:
                if src != env_cur:
                    try:
                        ecb_rate = await self._ecb_provider.fetch_rate(src, env_cur)
                        cross_rates[f"{src}->{env_cur}"] = ecb_rate
                    except FmvUnavailableError:
                        _log.warning(
                            "ECB: %s→%s rate unavailable; offline fallback will apply.",
                            src.upper(),
                            env_cur.upper(),
                        )

        snapshot_rates, snapshot_quality = _capture_fmv_snapshot(
            cap_currency, sats_rates=sats_rates, cross_rates=cross_rates
        )
        row: _EnvelopeRow = {
            "id": envelope_id,
            "cap_minor_units": cap_minor_units,
            "cap_currency": cap_currency,
            "allowed_rails": json.dumps(allowed_rails),
            "allowed_origins_glob": json.dumps(allowed_origins_glob or ["*"]),
            "status": _STATUS_ACTIVE,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "counter_public_key": pub_key_b64,
        }
        try:
            async with self._lock:
                await asyncio.to_thread(
                    self._create_envelope_sync,
                    row,
                    now.isoformat(),
                    snapshot_rates,
                    snapshot_quality,
                )
        except sqlite3.IntegrityError:
            # Roll back the key file so the envelope can be retried or diagnosed clearly.
            self._keystore.delete(envelope_id)
            raise

    async def create_envelope_if_absent(self, spec: BudgetEnvelope) -> bool:
        """Create an envelope from *spec* only when no row with that id exists.

        Returns ``True`` when a new envelope was inserted, ``False`` when an
        existing row was found (which is left untouched).  Delegates to
        :meth:`create_envelope` so keystore creation, FMV snapshot fetch, and
        the DB insert all follow the same code path.
        """
        async with self._lock:
            if self.envelope_exists_sync(spec.id):
                return False
        await self.create_envelope(
            spec.id,
            cap_minor_units=spec.cap_minor_units,
            cap_currency=spec.cap_currency,
            allowed_rails=list(spec.allowed_rails),
            allowed_origins_glob=spec.allowed_origins_glob,
            ttl_seconds=spec.ttl_seconds,
            owner_agent_id=spec.owner_agent_id,
        )
        return True

    def _create_envelope_sync(
        self,
        row: _EnvelopeRow,
        captured_at: str,
        snapshot_rates: dict[str, Decimal],
        snapshot_quality: dict[str, str],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO envelopes (
                id, cap_minor_units, cap_currency,
                allowed_rails, allowed_origins_glob,
                status, created_at, expires_at, counter_public_key
            ) VALUES (
                :id, :cap_minor_units, :cap_currency,
                :allowed_rails, :allowed_origins_glob,
                :status, :created_at, :expires_at, :counter_public_key
            )
            """,
            row,
        )
        _upsert_snapshot_sync_fn(
            self._conn, row["id"], captured_at, snapshot_rates, snapshot_quality
        )

    async def draw(
        self,
        *,
        envelope_id: str,
        request_id: str,
        idempotency_key: str,
        amount_reserved_minor_units: int,
        rail_quoted: Rail,
        ttl_seconds: int = _DEFAULT_DRAW_TTL_SECONDS,
    ) -> DrawReceipt:
        """Atomically reserve capacity from the envelope and return a signed receipt.

        BEGIN IMMEDIATE → cap check → idempotency → INSERT → COMMIT.
        Raises BudgetExceededError, EnvelopeNotFoundError, EnvelopeFrozenError,
        or EnvelopeExpiredError on rejection.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._draw_sync,
                envelope_id=envelope_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                amount_reserved_minor_units=amount_reserved_minor_units,
                rail_quoted=rail_quoted,
                ttl_seconds=ttl_seconds,
            )

    def _draw_sync(
        self,
        *,
        envelope_id: str,
        request_id: str,
        idempotency_key: str,
        amount_reserved_minor_units: int,
        rail_quoted: Rail,
        ttl_seconds: int,
    ) -> DrawReceipt:
        return _draw_sync_fn(
            self._conn,
            self._keystore,
            envelope_id=envelope_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            amount_reserved_minor_units=amount_reserved_minor_units,
            rail_quoted=rail_quoted,
            ttl_seconds=ttl_seconds,
        )

    async def confirm(self, draw_id: str, amount_settled_minor_units: int) -> None:
        """Transition a reserved draw to settled with the actual settled amount."""
        async with self._lock:
            await asyncio.to_thread(self._confirm_sync, draw_id, amount_settled_minor_units)

    def _confirm_sync(self, draw_id: str, amount_settled_minor_units: int) -> None:
        _confirm_sync_fn(self._conn, draw_id, amount_settled_minor_units)

    async def rollback(self, draw_id: str) -> None:
        """Transition a reserved draw to rolled_back, freeing its reserved capacity."""
        async with self._lock:
            await asyncio.to_thread(self._rollback_sync, draw_id)

    def _rollback_sync(self, draw_id: str) -> None:
        _rollback_sync_fn(self._conn, draw_id)
