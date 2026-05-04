"""Tests for credentials/store.py — CRUD, idempotency, state machine, concurrency."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from routewiler.credentials.schema import CredentialState, ManualHoldReason
from routewiler.credentials.store import CredentialStore
from routewiler.errors import CredentialNotFoundError, InvalidCredentialTransitionError


def _raw_rows(db_path: Path) -> list[dict]:  # type: ignore[type-arg]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM credentials ORDER BY persisted_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[CredentialStore, None]:
    s = CredentialStore(tmp_path / "test.db")
    yield s
    await s.aclose()


# ---------------------------------------------------------------------------
# persist
# ---------------------------------------------------------------------------


async def test_persist_creates_persisted_row(store: CredentialStore, tmp_path: Path) -> None:
    record = await store.persist(
        request_id="req1",
        rail="l402",
        challenge_url="http://vendor.com/resource",
        payload={"macaroon": "m", "preimage_hex": "ab"},
    )
    assert record.state == CredentialState.PERSISTED
    assert record.request_id == "req1"
    assert record.rail == "l402"
    assert record.redeemed_at is None

    rows = _raw_rows(tmp_path / "test.db")
    assert len(rows) == 1
    assert rows[0]["state"] == "persisted"


async def test_persist_stores_expires_at(store: CredentialStore) -> None:
    exp = datetime.now(UTC) + timedelta(hours=1)
    record = await store.persist(
        request_id="req2",
        rail="l402",
        challenge_url="http://x.com",
        payload={},
        expires_at=exp,
    )
    assert record.expires_at is not None
    assert abs((record.expires_at - exp).total_seconds()) < 1


async def test_persist_idempotent_on_request_id_and_rail(
    store: CredentialStore, tmp_path: Path
) -> None:
    r1 = await store.persist(
        request_id="req3", rail="l402", challenge_url="http://x.com", payload={"k": "v"}
    )
    r2 = await store.persist(
        request_id="req3", rail="l402", challenge_url="http://x.com", payload={"k": "v2"}
    )
    assert r1.credential_id == r2.credential_id
    rows = _raw_rows(tmp_path / "test.db")
    assert len(rows) == 1


async def test_persist_different_rails_are_distinct(store: CredentialStore, tmp_path: Path) -> None:
    await store.persist(request_id="req4", rail="l402", challenge_url="http://x.com", payload={})
    await store.persist(request_id="req4", rail="x402", challenge_url="http://x.com", payload={})
    rows = _raw_rows(tmp_path / "test.db")
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_returns_record(store: CredentialStore) -> None:
    r = await store.persist(
        request_id="req5", rail="l402", challenge_url="http://x.com", payload={}
    )
    fetched = await store.get(r.credential_id)
    assert fetched is not None
    assert fetched.credential_id == r.credential_id


async def test_get_returns_none_for_unknown(store: CredentialStore) -> None:
    result = await store.get("nonexistent_id")
    assert result is None


# ---------------------------------------------------------------------------
# list_by_state
# ---------------------------------------------------------------------------


async def test_list_by_state(store: CredentialStore) -> None:
    r1 = await store.persist(
        request_id="req6", rail="l402", challenge_url="http://x.com", payload={}
    )
    r2 = await store.persist(
        request_id="req7", rail="l402", challenge_url="http://x.com", payload={}
    )
    await store.transition(r2.credential_id, to_state=CredentialState.REDEEMED)

    persisted = await store.list_by_state(CredentialState.PERSISTED)
    redeemed = await store.list_by_state(CredentialState.REDEEMED)

    assert len(persisted) == 1
    assert persisted[0].credential_id == r1.credential_id
    assert len(redeemed) == 1
    assert redeemed[0].credential_id == r2.credential_id


# ---------------------------------------------------------------------------
# transition — legal edges
# ---------------------------------------------------------------------------


async def test_persisted_to_redeemed(store: CredentialStore) -> None:
    r = await store.persist(request_id="t1", rail="l402", challenge_url="http://x.com", payload={})
    updated = await store.transition(r.credential_id, to_state=CredentialState.REDEEMED)
    assert updated.state == CredentialState.REDEEMED
    assert updated.redeemed_at is not None


async def test_persisted_to_recovering(store: CredentialStore) -> None:
    r = await store.persist(request_id="t2", rail="l402", challenge_url="http://x.com", payload={})
    updated = await store.transition(r.credential_id, to_state=CredentialState.RECOVERING)
    assert updated.state == CredentialState.RECOVERING


async def test_persisted_to_manual_hold(store: CredentialStore) -> None:
    r = await store.persist(request_id="t3", rail="l402", challenge_url="http://x.com", payload={})
    updated = await store.transition(
        r.credential_id,
        to_state=CredentialState.MANUAL_HOLD,
        manual_hold_reason=ManualHoldReason.EXPIRED,
    )
    assert updated.state == CredentialState.MANUAL_HOLD
    assert updated.manual_hold_reason == ManualHoldReason.EXPIRED


async def test_recovering_to_redeemed(store: CredentialStore) -> None:
    r = await store.persist(request_id="t4", rail="l402", challenge_url="http://x.com", payload={})
    await store.transition(r.credential_id, to_state=CredentialState.RECOVERING)
    updated = await store.transition(r.credential_id, to_state=CredentialState.REDEEMED)
    assert updated.state == CredentialState.REDEEMED


async def test_recovering_to_manual_hold(store: CredentialStore) -> None:
    r = await store.persist(request_id="t5", rail="l402", challenge_url="http://x.com", payload={})
    await store.transition(r.credential_id, to_state=CredentialState.RECOVERING)
    updated = await store.transition(
        r.credential_id,
        to_state=CredentialState.MANUAL_HOLD,
        manual_hold_reason=ManualHoldReason.EXHAUSTED,
    )
    assert updated.state == CredentialState.MANUAL_HOLD
    assert updated.manual_hold_reason == ManualHoldReason.EXHAUSTED


# ---------------------------------------------------------------------------
# transition — illegal edges
# ---------------------------------------------------------------------------


async def test_redeemed_to_any_raises(store: CredentialStore) -> None:
    r = await store.persist(request_id="t6", rail="l402", challenge_url="http://x.com", payload={})
    await store.transition(r.credential_id, to_state=CredentialState.REDEEMED)
    with pytest.raises(InvalidCredentialTransitionError):
        await store.transition(r.credential_id, to_state=CredentialState.PERSISTED)


async def test_manual_hold_to_any_raises(store: CredentialStore) -> None:
    r = await store.persist(request_id="t7", rail="l402", challenge_url="http://x.com", payload={})
    await store.transition(
        r.credential_id,
        to_state=CredentialState.MANUAL_HOLD,
        manual_hold_reason=ManualHoldReason.EXHAUSTED,
    )
    with pytest.raises(InvalidCredentialTransitionError):
        await store.transition(r.credential_id, to_state=CredentialState.REDEEMED)


async def test_persisted_to_persisted_raises(store: CredentialStore) -> None:
    r = await store.persist(request_id="t8", rail="l402", challenge_url="http://x.com", payload={})
    with pytest.raises(InvalidCredentialTransitionError):
        await store.transition(r.credential_id, to_state=CredentialState.PERSISTED)


async def test_manual_hold_requires_reason(store: CredentialStore) -> None:
    r = await store.persist(request_id="t9", rail="l402", challenge_url="http://x.com", payload={})
    with pytest.raises(InvalidCredentialTransitionError, match="manual_hold_reason"):
        await store.transition(r.credential_id, to_state=CredentialState.MANUAL_HOLD)


async def test_transition_unknown_credential_raises(store: CredentialStore) -> None:
    with pytest.raises(CredentialNotFoundError):
        await store.transition("ghost_id", to_state=CredentialState.REDEEMED)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_transitions_serialise_correctly(store: CredentialStore) -> None:
    """10 concurrent REDEEMED transitions on the same credential: exactly one succeeds."""
    r = await store.persist(
        request_id="conc1", rail="l402", challenge_url="http://x.com", payload={}
    )

    successes = 0
    failures = 0

    async def try_redeem() -> None:
        nonlocal successes, failures
        try:
            await store.transition(r.credential_id, to_state=CredentialState.REDEEMED)
            successes += 1
        except InvalidCredentialTransitionError:
            failures += 1

    await asyncio.gather(*[try_redeem() for _ in range(10)])

    assert successes == 1
    assert failures == 9

    final = await store.get(r.credential_id)
    assert final is not None
    assert final.state == CredentialState.REDEEMED


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


async def test_aclose_is_idempotent(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "close_test.db")
    await store.aclose()
    await store.aclose()  # second call must not raise
