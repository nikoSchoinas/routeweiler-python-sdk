"""Tests for StickyCache — §7.2 sticky routing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from routewiler.routing.sticky import StickyCache, StickyKey


def _key(
    origin: str = "http://mock:80", agent_id: str | None = None, session_id: str | None = None
) -> StickyKey:
    return StickyKey(origin=origin, agent_id=agent_id, session_id=session_id)


def _future(seconds: int = 120) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _past(seconds: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


class TestStickyCache:
    def test_remember_then_lookup_returns_rail(self) -> None:
        cache = StickyCache()
        key = _key()
        cache.remember(key, "x402", _future())
        assert cache.lookup(key) == "x402"

    def test_lookup_returns_none_when_empty(self) -> None:
        cache = StickyCache()
        assert cache.lookup(_key()) is None

    def test_forget_clears_entry(self) -> None:
        cache = StickyCache()
        key = _key()
        cache.remember(key, "x402", _future())
        cache.forget(key)
        assert cache.lookup(key) is None

    def test_forget_on_absent_key_is_noop(self) -> None:
        cache = StickyCache()
        cache.forget(_key("http://nonexistent:80"))  # must not raise

    def test_expired_challenge_returns_none(self) -> None:
        cache = StickyCache(ttl=timedelta(hours=1))
        key = _key()
        # challenge expires in the past — entry should be evicted on lookup
        cache.remember(key, "x402", _past())
        assert cache.lookup(key) is None

    def test_ttl_wins_when_smaller_than_challenge_expiry(self) -> None:
        cache = StickyCache(ttl=timedelta(seconds=1))
        key = _key()
        cache.remember(key, "x402", _future(3600))
        # Still valid right after remembering.
        assert cache.lookup(key) == "x402"

    def test_different_origins_produce_distinct_keys(self) -> None:
        cache = StickyCache()
        cache.remember(_key("http://a:80"), "x402", _future())
        cache.remember(_key("http://b:80"), "l402", _future())
        assert cache.lookup(_key("http://a:80")) == "x402"
        assert cache.lookup(_key("http://b:80")) == "l402"

    def test_different_agent_ids_produce_distinct_keys(self) -> None:
        cache = StickyCache()
        cache.remember(_key(agent_id="agent-1"), "x402", _future())
        cache.remember(_key(agent_id="agent-2"), "l402", _future())
        assert cache.lookup(_key(agent_id="agent-1")) == "x402"
        assert cache.lookup(_key(agent_id="agent-2")) == "l402"

    def test_different_session_ids_produce_distinct_keys(self) -> None:
        cache = StickyCache()
        cache.remember(_key(session_id="s1"), "x402", _future())
        cache.remember(_key(session_id="s2"), "l402", _future())
        assert cache.lookup(_key(session_id="s1")) == "x402"
        assert cache.lookup(_key(session_id="s2")) == "l402"

    def test_overwrite_existing_entry(self) -> None:
        cache = StickyCache()
        key = _key()
        cache.remember(key, "x402", _future())
        cache.remember(key, "l402", _future())
        assert cache.lookup(key) == "l402"
