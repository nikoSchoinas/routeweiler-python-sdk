"""Tests for credentials/recovery.py — state machine via NoOpRecoveryStrategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from routewiler.credentials.recovery import (
    CredentialRecoverer,
    NoOpRecoveryStrategy,
    RecoveryOutcome,
    RecoveryStrategy,
)
from routewiler.credentials.schema import CredentialRecord, CredentialState, ManualHoldReason
from routewiler.credentials.store import CredentialStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> CredentialStore:
    s = CredentialStore(tmp_path / "recovery_test.db")
    yield s  # type: ignore[misc]
    await s.aclose()


@pytest.fixture
def mock_emitter() -> MagicMock:
    emitter = MagicMock()
    emitter.emit_credential_manual_hold = AsyncMock()
    return emitter


@pytest.fixture
def recoverer(store: CredentialStore, mock_emitter: MagicMock) -> CredentialRecoverer:
    return CredentialRecoverer(
        store=store,
        strategy=NoOpRecoveryStrategy(),
        emitter=mock_emitter,
    )


# ---------------------------------------------------------------------------
# NoOpRecoveryStrategy
# ---------------------------------------------------------------------------


async def test_noop_strategy_returns_exhausted() -> None:
    strategy = NoOpRecoveryStrategy()
    now = datetime.now(UTC)
    dummy = CredentialRecord(
        credential_id="x",
        request_id="r",
        rail="l402",
        challenge_url="http://x.com",
        payload={},
        state=CredentialState.PERSISTED,
        persisted_at=now,
        last_transition_at=now,
    )
    outcome = await strategy.recover(dummy, None)
    assert outcome.succeeded is False
    assert outcome.reason == ManualHoldReason.EXHAUSTED
    assert outcome.response is None


async def test_noop_strategy_matches_protocol() -> None:
    assert isinstance(NoOpRecoveryStrategy(), RecoveryStrategy)


# ---------------------------------------------------------------------------
# CredentialRecoverer — happy path (NoOp → MANUAL_HOLD)
# ---------------------------------------------------------------------------


async def test_recoverer_persisted_to_manual_hold_exhausted(
    store: CredentialStore,
    recoverer: CredentialRecoverer,
    mock_emitter: MagicMock,
) -> None:
    record = await store.persist(
        request_id="r1", rail="l402", challenge_url="http://x.com", payload={}
    )
    outcome = await recoverer.attempt_recovery(record.credential_id, last_response=None)

    assert outcome.succeeded is False
    assert outcome.reason == ManualHoldReason.EXHAUSTED

    final = await store.get(record.credential_id)
    assert final is not None
    assert final.state == CredentialState.MANUAL_HOLD
    assert final.manual_hold_reason == ManualHoldReason.EXHAUSTED

    mock_emitter.emit_credential_manual_hold.assert_awaited_once()
    call_kwargs = mock_emitter.emit_credential_manual_hold.call_args.kwargs
    assert call_kwargs["reason"] == ManualHoldReason.EXHAUSTED


async def test_recoverer_with_4xx_response(
    store: CredentialStore, recoverer: CredentialRecoverer
) -> None:
    record = await store.persist(
        request_id="r2", rail="l402", challenge_url="http://x.com", payload={}
    )
    fake_response = httpx.Response(404)
    outcome = await recoverer.attempt_recovery(record.credential_id, last_response=fake_response)
    assert outcome.succeeded is False
    final = await store.get(record.credential_id)
    assert final is not None
    assert final.state == CredentialState.MANUAL_HOLD


# ---------------------------------------------------------------------------
# Expiry pre-check
# ---------------------------------------------------------------------------


async def test_recoverer_expired_credential_goes_to_manual_hold_expired(
    store: CredentialStore, recoverer: CredentialRecoverer
) -> None:
    past = datetime.now(UTC) - timedelta(seconds=10)
    record = await store.persist(
        request_id="r3",
        rail="l402",
        challenge_url="http://x.com",
        payload={},
        expires_at=past,
    )
    outcome = await recoverer.attempt_recovery(record.credential_id, last_response=None)

    assert outcome.succeeded is False
    assert outcome.reason == ManualHoldReason.EXPIRED

    final = await store.get(record.credential_id)
    assert final is not None
    assert final.state == CredentialState.MANUAL_HOLD
    assert final.manual_hold_reason == ManualHoldReason.EXPIRED


async def test_recoverer_non_expired_credential_processes_normally(
    store: CredentialStore, recoverer: CredentialRecoverer
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    record = await store.persist(
        request_id="r4",
        rail="l402",
        challenge_url="http://x.com",
        payload={},
        expires_at=future,
    )
    outcome = await recoverer.attempt_recovery(record.credential_id, last_response=None)
    assert outcome.succeeded is False
    # Went through RECOVERING (NoOp), not the expiry pre-check
    assert outcome.reason == ManualHoldReason.EXHAUSTED


# ---------------------------------------------------------------------------
# Terminal state idempotency
# ---------------------------------------------------------------------------


async def test_recoverer_already_redeemed_is_noop(
    store: CredentialStore, recoverer: CredentialRecoverer, mock_emitter: MagicMock
) -> None:
    record = await store.persist(
        request_id="r5", rail="l402", challenge_url="http://x.com", payload={}
    )
    await store.transition(record.credential_id, to_state=CredentialState.REDEEMED)

    outcome = await recoverer.attempt_recovery(record.credential_id, last_response=None)

    assert outcome.succeeded is True
    mock_emitter.emit_credential_manual_hold.assert_not_awaited()


async def test_recoverer_already_manual_hold_is_noop(
    store: CredentialStore, recoverer: CredentialRecoverer, mock_emitter: MagicMock
) -> None:
    record = await store.persist(
        request_id="r6", rail="l402", challenge_url="http://x.com", payload={}
    )
    await store.transition(
        record.credential_id,
        to_state=CredentialState.MANUAL_HOLD,
        manual_hold_reason=ManualHoldReason.EXHAUSTED,
    )

    # Reset mock to verify no second emission
    mock_emitter.emit_credential_manual_hold.reset_mock()
    await recoverer.attempt_recovery(record.credential_id, last_response=None)
    mock_emitter.emit_credential_manual_hold.assert_not_awaited()


# ---------------------------------------------------------------------------
# Unknown credential
# ---------------------------------------------------------------------------


async def test_recoverer_unknown_credential_returns_exhausted(
    recoverer: CredentialRecoverer,
) -> None:
    outcome = await recoverer.attempt_recovery("ghost_id", last_response=None)
    assert outcome.succeeded is False
    assert outcome.reason == ManualHoldReason.EXHAUSTED


# ---------------------------------------------------------------------------
# Emitter is optional
# ---------------------------------------------------------------------------


async def test_recoverer_without_emitter_does_not_raise(
    store: CredentialStore,
) -> None:
    recoverer = CredentialRecoverer(
        store=store,
        strategy=NoOpRecoveryStrategy(),
        emitter=None,
    )
    record = await store.persist(
        request_id="r7", rail="l402", challenge_url="http://x.com", payload={}
    )
    outcome = await recoverer.attempt_recovery(record.credential_id, last_response=None)
    assert outcome.succeeded is False


# ---------------------------------------------------------------------------
# Recovering → REDEEMED via a custom strategy
# ---------------------------------------------------------------------------


async def test_recoverer_successful_strategy_transitions_to_redeemed(
    store: CredentialStore,
) -> None:
    class AlwaysSucceedsStrategy:
        async def recover(
            self, credential: CredentialRecord, last_response: httpx.Response | None
        ) -> RecoveryOutcome:
            return RecoveryOutcome(succeeded=True, response=None, reason=None)

    recoverer = CredentialRecoverer(
        store=store,
        strategy=AlwaysSucceedsStrategy(),
        emitter=None,
    )
    record = await store.persist(
        request_id="r8", rail="l402", challenge_url="http://x.com", payload={}
    )
    outcome = await recoverer.attempt_recovery(record.credential_id, last_response=None)

    assert outcome.succeeded is True
    final = await store.get(record.credential_id)
    assert final is not None
    assert final.state == CredentialState.REDEEMED
