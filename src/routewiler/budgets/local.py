"""Local SQLite budget counter — single-process, row-locked draws."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast

from routewiler._constants import CLOCK_SKEW_BUFFER_SECONDS, REAPER_INTERVAL_SECONDS
from routewiler._storage import ensure_schema as _ensure_schema
from routewiler._storage import open_connection as _open_connection
from routewiler.budgets.ecb_provider import EcbRateProvider
from routewiler.budgets.fmv import capture_fmv_snapshot as _capture_fmv_snapshot
from routewiler.budgets.fmv_provider import FmvProvider
from routewiler.budgets.keystore import EnvelopeKeystore
from routewiler.budgets.receipts import issue as _issue_receipt
from routewiler.budgets.receipts import uuid7
from routewiler.budgets.schema import DrawReceipt
from routewiler.errors import (
    BudgetExceededError,
    EnvelopeExpiredError,
    EnvelopeFrozenError,
    EnvelopeNotFoundError,
    FmvUnavailableError,
)
from routewiler.normalized import Rail

_log = logging.getLogger(__name__)

DEFAULT_ENVELOPE_ID = "default"
_DEFAULT_CAP_USD = 100
_DEFAULT_TTL_DAYS = 30

# ---------------------------------------------------------------------------
# _EnvelopeRow — typed dict for SQLite envelope INSERT parameters.
# ---------------------------------------------------------------------------


class _EnvelopeRow(TypedDict):
    id: str
    cap_minor_units: int
    cap_currency: str
    allowed_rails: str  # json.dumps(list[Rail])
    allowed_origins_glob: str  # json.dumps(list[str])
    status: str
    created_at: str  # ISO-8601 UTC
    expires_at: str  # ISO-8601 UTC
    counter_public_key: str


# ---------------------------------------------------------------------------
# Default draw TTL — §8.4: "default 2x p99 settlement latency".
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
    Budget enforcement is always local (§8 — no hosted counter at MVP).
    Initialises the envelopes and draws tables on construction (idempotent).

    The reaper task (§8.3) is started by calling ``await store.start()`` once
    the event loop is running (e.g. from ``Routewiler.__aenter__``).
    """

    def __init__(
        self,
        db_path: Path,
        keystore: EnvelopeKeystore,
        *,
        reaper_interval_seconds: float = REAPER_INTERVAL_SECONDS,
        fmv_provider: FmvProvider | None = None,
        ecb_provider: EcbRateProvider | None = None,
    ) -> None:
        self._db_path = db_path
        self._keystore = keystore
        self._reaper_interval_seconds = reaper_interval_seconds
        self._fmv_provider = fmv_provider
        self._ecb_provider = ecb_provider
        self._conn = _open_connection(db_path)
        _ensure_schema(self._conn)
        self._seed_default_envelope_sync()
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the reaper background task. Idempotent."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_loop(), name="routewiler-reaper")

    async def _reap_loop(self) -> None:
        """Roll back stale reserved draws periodically (§8.3)."""
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
        """Transition all expired reserved draws to rolled_back. Returns rowcount."""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "UPDATE draws SET state='rolled_back' WHERE state='reserved' AND expires_at < ?",
            (now,),
        )
        return cursor.rowcount

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task
        async with self._lock:
            await asyncio.to_thread(self._conn.close)

    # ------------------------------------------------------------------
    # Synchronous helpers (safe to call from a synchronous constructor)
    # ------------------------------------------------------------------

    def _seed_default_envelope_sync(self) -> None:
        """Idempotently insert the 'default' envelope row using this store's connection.

        Cap is read from the ROUTEWILER_DEFAULT_CAP_USD env var (default 100 USD).
        Uses INSERT OR IGNORE so repeated calls are safe.
        Called once from __init__ after ensure_schema.
        """
        cap_usd = int(os.environ.get("ROUTEWILER_DEFAULT_CAP_USD", _DEFAULT_CAP_USD))
        cap_minor = cap_usd * 100  # USD cents
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=_DEFAULT_TTL_DAYS)

        if not self._keystore.exists(DEFAULT_ENVELOPE_ID):
            private_key = self._keystore.create(DEFAULT_ENVELOPE_ID)
            pub_key_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
        else:
            pub_key_b64 = self._keystore.public_key_b64(DEFAULT_ENVELOPE_ID)

        row: _EnvelopeRow = {
            "id": DEFAULT_ENVELOPE_ID,
            "cap_minor_units": cap_minor,
            "cap_currency": "usd",
            "allowed_rails": json.dumps(["x402"]),
            "allowed_origins_glob": json.dumps(["*"]),
            "status": "active",
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "counter_public_key": pub_key_b64,
        }
        self._conn.execute(
            """
            INSERT OR IGNORE INTO envelopes (
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
        # Write FMV snapshot only when the envelope row was actually inserted.
        changes_row = self._conn.execute("SELECT changes()").fetchone()
        if changes_row is not None and changes_row[0]:
            snapshot_rates, snapshot_quality = _capture_fmv_snapshot("usd")
            self._conn.execute(
                """
                INSERT OR IGNORE INTO envelope_fmv_snapshots
                    (envelope_id, captured_at, rates_json, quality_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    DEFAULT_ENVELOPE_ID,
                    now.isoformat(),
                    json.dumps({k: str(v) for k, v in snapshot_rates.items()}),
                    json.dumps(snapshot_quality),
                ),
            )

    def envelope_exists_sync(self, envelope_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM envelopes WHERE id = ?", (envelope_id,)).fetchone()
        return row is not None

    def get_envelope_currency_sync(self, envelope_id: str) -> str | None:
        """Return the cap_currency for an envelope, or None if not found.

        Synchronous — runs on the already-open connection so it is safe to call
        from the Routewiler constructor before the event loop is available.
        This is the one permitted sync DB read in the constructor path.
        """
        row = self._conn.execute(
            "SELECT cap_currency FROM envelopes WHERE id = ?", (envelope_id,)
        ).fetchone()
        return str(row[0]) if row else None

    def load_fmv_snapshot_sync(self, envelope_id: str) -> dict[str, Decimal] | None:
        """Return the stored FMV snapshot rates for an envelope, or None if absent."""
        row = self._conn.execute(
            "SELECT rates_json FROM envelope_fmv_snapshots WHERE envelope_id = ?",
            (envelope_id,),
        ).fetchone()
        if row is None:
            return None
        raw: dict[str, str] = json.loads(str(row[0]))
        return {k: Decimal(v) for k, v in raw.items()}

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
        cap_currency: str,
        allowed_rails: list[str],
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

        # Fetch per-satoshi rate — required for L402 budget enforcement.
        # Propagate FmvUnavailableError so the caller knows the envelope is
        # not safe for L402 draws rather than silently creating a broken envelope.
        sats_rates: dict[str, Decimal] | None = None
        if "l402" in allowed_rails:
            if self._fmv_provider is not None:
                rate = await self._fmv_provider.fetch_btc_to(cap_currency)
                sats_rates = {f"sats->{cap_currency.lower()}": rate}
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
            "status": "active",
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
        self._conn.execute(
            """
            INSERT OR REPLACE INTO envelope_fmv_snapshots
                (envelope_id, captured_at, rates_json, quality_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                row["id"],
                captured_at,
                json.dumps({k: str(v) for k, v in snapshot_rates.items()}),
                json.dumps(snapshot_quality),
            ),
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

        Implements §8.2: BEGIN IMMEDIATE → cap check → idempotency → INSERT → COMMIT.
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
        conn = self._conn
        now = datetime.now(UTC)
        # Include clock-skew buffer so the reaper doesn't fire before the
        # active path can confirm/rollback (§8.4).
        expires = now + timedelta(seconds=ttl_seconds + CLOCK_SKEW_BUFFER_SECONDS)

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Load envelope (cap, status, expiry, public key).
            env_row = conn.execute(
                "SELECT cap_minor_units, status, expires_at, cap_currency, counter_public_key "
                "FROM envelopes WHERE id = ?",
                (envelope_id,),
            ).fetchone()
            if env_row is None:
                raise EnvelopeNotFoundError(f"Envelope '{envelope_id}' not found.")
            cap, status, env_expires_raw, cap_currency, pub_key_b64 = env_row

            if status != "active":
                raise EnvelopeFrozenError(
                    f"Envelope '{envelope_id}' has status '{status}' (expected 'active')."
                )

            env_expires = datetime.fromisoformat(env_expires_raw)
            if now >= env_expires:
                raise EnvelopeExpiredError(
                    f"Envelope '{envelope_id}' expired at {env_expires_raw}."
                )

            # Idempotency short-circuit — return a re-signed receipt for the same draw.
            # request_id is re-read from the stored row so the receipt is byte-identical
            # to the one returned on the original call (§8.2: "return the existing receipt
            # unchanged").
            existing = conn.execute(
                "SELECT id, request_id, amount_reserved_minor_units, rail_quoted, "
                "issued_at, expires_at "
                "FROM draws WHERE envelope_id = ? AND idempotency_key = ?",
                (envelope_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                ex_id, ex_req_id, ex_amt, ex_rail, ex_issued, ex_exp = existing
                private_key = self._keystore.load(envelope_id)
                return _issue_receipt(
                    private_key=private_key,
                    public_key_b64=str(pub_key_b64),
                    receipt_id=str(ex_id),
                    envelope_id=envelope_id,
                    request_id=str(ex_req_id),
                    idempotency_key=idempotency_key,
                    amount_reserved_minor_units=int(ex_amt),
                    amount_reserved_currency=cap_currency,
                    rail_quoted=cast(Rail, str(ex_rail)),
                    issued_at=datetime.fromisoformat(str(ex_issued)),
                    expires_at=datetime.fromisoformat(str(ex_exp)),
                )

            # Cap check: reserved + settled must not exceed cap after this draw.
            reserved: int = conn.execute(
                "SELECT COALESCE(SUM(amount_reserved_minor_units), 0) FROM draws "
                "WHERE envelope_id = ? AND state = 'reserved'",
                (envelope_id,),
            ).fetchone()[0]
            settled: int = conn.execute(
                "SELECT COALESCE(SUM(amount_settled_minor_units), 0) FROM draws "
                "WHERE envelope_id = ? AND state = 'settled'",
                (envelope_id,),
            ).fetchone()[0]

            available = int(cap) - int(reserved) - int(settled)
            if amount_reserved_minor_units > available:
                raise BudgetExceededError(
                    envelope_id=envelope_id,
                    requested_minor_units=amount_reserved_minor_units,
                    available_minor_units=available,
                )

            draw_id = uuid7()
            conn.execute(
                """
                INSERT INTO draws (
                    id, envelope_id, request_id, idempotency_key,
                    amount_reserved_minor_units, rail_quoted, state,
                    issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    draw_id,
                    envelope_id,
                    request_id,
                    idempotency_key,
                    amount_reserved_minor_units,
                    rail_quoted,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            conn.execute("COMMIT")

            private_key = self._keystore.load(envelope_id)
            return _issue_receipt(
                private_key=private_key,
                public_key_b64=str(pub_key_b64),
                receipt_id=draw_id,
                envelope_id=envelope_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                amount_reserved_minor_units=amount_reserved_minor_units,
                amount_reserved_currency=cap_currency,
                rail_quoted=rail_quoted,
                issued_at=now,
                expires_at=expires,
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    async def confirm(self, draw_id: str, amount_settled_minor_units: int) -> None:
        """Transition a reserved draw to settled with the actual settled amount."""
        async with self._lock:
            await asyncio.to_thread(self._confirm_sync, draw_id, amount_settled_minor_units)

    def _confirm_sync(self, draw_id: str, amount_settled_minor_units: int) -> None:
        now = datetime.now(UTC)
        self._conn.execute(
            "UPDATE draws SET state='settled', amount_settled_minor_units=?, "
            "settled_at=? WHERE id=? AND state='reserved'",
            (amount_settled_minor_units, now.isoformat(), draw_id),
        )

    async def rollback(self, draw_id: str) -> None:
        """Transition a reserved draw to rolled_back, freeing its reserved capacity."""
        async with self._lock:
            await asyncio.to_thread(self._rollback_sync, draw_id)

    def _rollback_sync(self, draw_id: str) -> None:
        self._conn.execute(
            "UPDATE draws SET state='rolled_back' WHERE id=? AND state='reserved'",
            (draw_id,),
        )
