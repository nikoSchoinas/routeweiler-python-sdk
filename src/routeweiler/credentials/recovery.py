"""Credential recovery — state machine orchestration and recovery strategy protocol.

The CredentialRecoverer drives the credential state machine on the failed-retry path:
    PERSISTED → RECOVERING → strategy.recover() → REDEEMED | MANUAL_HOLD

Week 10 ships NoOpRecoveryStrategy (straight to MANUAL_HOLD(exhausted)).
Week 11 plugs ManifestRecoveryStrategy into the same RecoveryStrategy Protocol.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx

from routeweiler.credentials.schema import CredentialRecord, CredentialState, ManualHoldReason

if TYPE_CHECKING:
    from routeweiler.credentials.store import CredentialStore
    from routeweiler.trace.emitter import TraceEmitter

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RecoveryOutcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryOutcome:
    succeeded: bool
    response: httpx.Response | None  # 2xx response if succeeded; None on failure
    reason: ManualHoldReason | None  # populated when succeeded=False


# ---------------------------------------------------------------------------
# RecoveryStrategy protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RecoveryStrategy(Protocol):
    """Strategy for recovering a credential after a failed retry.

    Implementations receive the persisted credential and the last HTTP response
    (or None on transport error) and return a RecoveryOutcome.
    """

    async def recover(
        self, credential: CredentialRecord, last_response: httpx.Response | None
    ) -> RecoveryOutcome: ...


# ---------------------------------------------------------------------------
# NoOpRecoveryStrategy
# ---------------------------------------------------------------------------


class NoOpRecoveryStrategy:
    """Default: no recovery attempt — go straight to MANUAL_HOLD(exhausted).

    Replaced in Week 11 by a manifest-driven strategy that retries alternate
    fulfilment URLs (split-URL recovery).
    """

    async def recover(
        self, credential: CredentialRecord, last_response: httpx.Response | None
    ) -> RecoveryOutcome:
        return RecoveryOutcome(
            succeeded=False,
            response=None,
            reason=ManualHoldReason.EXHAUSTED,
        )


# ---------------------------------------------------------------------------
# CredentialRecoverer
# ---------------------------------------------------------------------------


class CredentialRecoverer:
    """Owns all PERSISTED → {REDEEMED, MANUAL_HOLD} transitions.

    The happy path (2xx on first retry) calls ``mark_redeemed``; failed paths
    call ``attempt_recovery``.  Both are in-line and do not spawn background
    workers.

    Call ``attempt_recovery()`` from _auth.py after a 4xx/5xx retry response
    or a transport error.
    """

    def __init__(
        self,
        store: CredentialStore,
        strategy: RecoveryStrategy,
        emitter: TraceEmitter | None,
    ) -> None:
        self._store = store
        self._strategy = strategy
        self._emitter = emitter

    async def mark_redeemed(self, credential_id: str) -> None:
        """Transition a credential to REDEEMED (happy path: 2xx on first retry).

        Logs and swallows errors so that a failed DB write never blocks the caller
        — the credential stays PERSISTED and remains visible for manual inspection.
        """
        try:
            await self._store.transition(credential_id, to_state=CredentialState.REDEEMED)
        except Exception:
            _log.exception(
                "Credential REDEEMED transition failed for %r; credential stays PERSISTED.",
                credential_id,
            )

    async def attempt_recovery(
        self,
        credential_id: str,
        *,
        last_response: httpx.Response | None,
    ) -> RecoveryOutcome:
        """Run the recovery state machine for one credential.

        Safe to call even if the credential is already in a terminal state —
        returns immediately without re-transitioning.
        """
        credential = await self._store.get(credential_id)
        if credential is None:
            _log.warning("attempt_recovery: credential %r not found; skipping.", credential_id)
            return RecoveryOutcome(
                succeeded=False, response=None, reason=ManualHoldReason.EXHAUSTED
            )

        # Already terminal — idempotent no-op.
        if credential.state in (CredentialState.REDEEMED, CredentialState.MANUAL_HOLD):
            return RecoveryOutcome(
                succeeded=(credential.state == CredentialState.REDEEMED),
                response=None,
                reason=credential.manual_hold_reason,
            )

        # Expiry pre-check: if the credential is past its TTL, skip strategy and
        # go directly to MANUAL_HOLD(expired).
        if credential.expires_at is not None and datetime.now(UTC) >= credential.expires_at:
            await self._terminal(credential, ManualHoldReason.EXPIRED)
            return RecoveryOutcome(succeeded=False, response=None, reason=ManualHoldReason.EXPIRED)

        # Enter RECOVERING state (idempotent — state machine allows RECOVERING→RECOVERING
        # so process-crash resume works without a separate guard here).
        try:
            credential = await self._store.transition(
                credential_id, to_state=CredentialState.RECOVERING
            )
        except sqlite3.OperationalError:
            # Infrastructure failure (e.g. DB busy) — surface it; don't silently eat it.
            _log.exception("DB error transitioning credential %r to RECOVERING.", credential_id)
            raise
        except Exception:
            # Transition rejected or other error — credential stays in current state for inspection.
            _log.exception("Failed to transition credential %r to RECOVERING.", credential_id)
            return RecoveryOutcome(
                succeeded=False, response=None, reason=ManualHoldReason.EXHAUSTED
            )

        # Delegate to the strategy (NoOp in Week 10; manifest-driven in Week 11).
        try:
            outcome = await self._strategy.recover(credential, last_response)
        except Exception:
            _log.exception("RecoveryStrategy.recover raised; treating as exhausted.")
            outcome = RecoveryOutcome(
                succeeded=False, response=None, reason=ManualHoldReason.EXHAUSTED
            )

        if outcome.succeeded:
            try:
                await self._store.transition(credential_id, to_state=CredentialState.REDEEMED)
            except Exception:
                _log.exception(
                    "Failed to transition credential %r to REDEEMED after recovery.",
                    credential_id,
                )
            return outcome

        reason = outcome.reason or ManualHoldReason.EXHAUSTED
        await self._terminal(credential, reason)
        return RecoveryOutcome(succeeded=False, response=None, reason=reason)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _terminal(self, credential: CredentialRecord, reason: ManualHoldReason) -> None:
        """Transition to MANUAL_HOLD and emit a trace event."""
        try:
            await self._store.transition(
                credential.credential_id,
                to_state=CredentialState.MANUAL_HOLD,
                manual_hold_reason=reason,
            )
        except Exception:
            _log.exception(
                "Failed to transition credential %r to MANUAL_HOLD.", credential.credential_id
            )

        if self._emitter is not None:
            try:
                await self._emitter.emit_credential_manual_hold(
                    credential=credential,
                    reason=reason,
                    ts=datetime.now(UTC),
                )
            except Exception:
                _log.exception(
                    "Failed to emit MANUAL_HOLD trace for credential %r.",
                    credential.credential_id,
                )
