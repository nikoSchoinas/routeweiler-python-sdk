"""Sticky routing cache — remembers the last successful rail per (origin, agent, session)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from routeweiler.normalized import Rail

_DEFAULT_STICKY_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class StickyKey:
    """Composite key for sticky routing decisions.

    One Routeweiler client instance represents one (agent, session). The `origin`
    is derived from the request URL (scheme + host + port). Optional `agent_id`
    and `session_id` allow multiple logical agents/sessions to share a single
    client without cross-contaminating their sticky state.
    """

    origin: str  # "{scheme}://{host}:{port}"
    agent_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class _StickyEntry:
    rail: Rail
    expires_at: datetime


class StickyCache:
    """In-memory sticky routing cache (per Routeweiler instance).

    Once a rail is selected for a (origin, agent, session) tuple, the cache
    returns the same rail for subsequent calls until the entry expires.  This
    avoids double-payment and inconsistent receipts.

    The effective TTL is the minimum of `ttl` (default 10 minutes) and the
    challenge's `expires_at` timestamp — both are passed to `remember`.
    """

    def __init__(self, ttl: timedelta = _DEFAULT_STICKY_TTL) -> None:
        self._ttl = ttl
        self._entries: dict[StickyKey, _StickyEntry] = {}

    def remember(self, key: StickyKey, rail: Rail, challenge_expires_at: datetime) -> None:
        """Record a successful rail for this key.

        The entry expires at the earlier of ``now + ttl`` and the challenge's
        own expiry.
        """
        effective = min(challenge_expires_at, datetime.now(UTC) + self._ttl)
        self._entries[key] = _StickyEntry(rail=rail, expires_at=effective)

    def lookup(self, key: StickyKey) -> Rail | None:
        """Return the cached rail for this key, or None if absent/expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if datetime.now(UTC) >= entry.expires_at:
            del self._entries[key]
            return None
        return entry.rail

    def forget(self, key: StickyKey) -> None:
        """Evict the cached rail for this key (called when the sticky rail fails)."""
        self._entries.pop(key, None)
