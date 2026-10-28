"""Per-turn memory prefetch guard: honor Retry-After and keep recent results.

Prefetch runs once per turn. When Kynver answers 429, the next turn used to
call again straight into the same limit, and every refused turn got no memory
at all. The guard keeps a cooldown from the server's Retry-After and a short
cache of recent results, so a refused turn can still be served from a recent
answer to the same query, and a turn with nothing to serve is told so.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 600.0
FRESH_TTL_SECONDS = 60.0
STALE_TTL_SECONDS = 600.0
MAX_ENTRIES = 32

_RETRY_AFTER_TEXT = re.compile(r"retry after (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Cooldown the server asked for, or None when the failure was not a 429."""
    if getattr(exc, "status", None) != 429:
        return None
    value = getattr(exc, "retry_after", None)
    if value is None:
        match = _RETRY_AFTER_TEXT.search(str(exc))
        value = float(match.group(1)) if match else None
    if value is None or value <= 0:
        return DEFAULT_COOLDOWN_SECONDS
    return min(float(value), MAX_COOLDOWN_SECONDS)


class PrefetchGuard:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._entries: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    @staticmethod
    def _key(query: str) -> str:
        return " ".join(query.lower().split())

    def cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - self._clock())

    def fresh(self, query: str) -> Optional[Any]:
        """A result recent enough to reuse instead of calling Kynver again."""
        return self._lookup(query, FRESH_TTL_SECONDS)

    def stale(self, query: str) -> Optional[Any]:
        """An older result, served only when Kynver cannot be reached."""
        return self._lookup(query, STALE_TTL_SECONDS)

    def _lookup(self, query: str, ttl: float) -> Optional[Any]:
        key = self._key(query)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or self._clock() - entry[0] > ttl:
                return None
            return entry[1]

    def store(self, query: str, payload: Any) -> None:
        key = self._key(query)
        with self._lock:
            self._entries[key] = (self._clock(), payload)
            self._entries.move_to_end(key)
            while len(self._entries) > MAX_ENTRIES:
                self._entries.popitem(last=False)

    def note_failure(self, exc: BaseException) -> None:
        seconds = retry_after_seconds(exc)
        if seconds is None:
            return
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)


def degraded_notice(reason: str) -> str:
    """Short context block for a turn that got no Kynver memory."""
    return (
        "## Kynver AgentOS Context\n"
        f"Kynver memory is unavailable this turn ({reason}). Nothing was recalled; "
        "that does not mean nothing is known. Check with kynver_memory_search "
        "before stating facts about people, past decisions or prior work."
    )
