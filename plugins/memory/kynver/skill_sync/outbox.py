"""Durable dirty set of locally edited skills (H2).

One entry per slug, ``{slug, baseHash, kind, fails, rejects, nextAt, firstAt,
parked, lastError}``. Content is never stored: the drain reads the cache at
send time, so the file is bounded by the number of skills and ten offline
edits of one skill are one push. Stored as one JSON object per line
(``kynver-skill-outbox.jsonl``), rewritten atomically under a file lock, mode
0600.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Dict, List, Optional

from .store import atomic_write_json, file_lock, state_dir

KINDS = ("upsert", "archive", "restore")
MAX_REJECTS = 3
BACKOFF_BASE = 5.0
BACKOFF_CAP = 15 * 60.0


def _path():
    return state_dir() / "kynver-skill-outbox.jsonl"


def _load() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        for line in _path().read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue  # a torn line never blocks the rest
            if isinstance(e, dict) and isinstance(e.get("slug"), str):
                out[e["slug"]] = e
    except OSError:
        pass
    return out


def _save(entries: dict[str, dict[str, Any]]) -> None:
    import os
    import tempfile

    p = _path()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".outbox.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for slug in sorted(entries):
                fh.write(json.dumps(entries[slug], sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mark_dirty(slug: str, kind: str, base_hash: Optional[str], now: Optional[float] = None) -> None:
    """Record that ``slug`` changed locally. Coalesces with an existing entry:
    the *first* baseHash is kept (it is what the server last saw), the latest
    kind wins, and a parked entry is revived by a fresh edit."""
    if kind not in KINDS:
        raise ValueError(f"unknown outbox kind {kind!r}")
    now = time.time() if now is None else now
    with file_lock("kynver-skill-outbox"):
        entries = _load()
        cur = entries.get(slug)
        if cur is None:
            entries[slug] = {"slug": slug, "baseHash": base_hash, "kind": kind, "fails": 0,
                             "rejects": 0, "nextAt": now, "firstAt": now, "parked": None,
                             "lastError": None}
        else:
            cur["kind"] = kind
            cur["nextAt"] = now
            cur["fails"] = 0
            cur["rejects"] = 0
            cur["parked"] = None
        _save(entries)


def entries() -> list[dict[str, Any]]:
    with file_lock("kynver-skill-outbox"):
        return list(_load().values())


def get(slug: str) -> Optional[dict[str, Any]]:
    return _load().get(slug)


def has(slug: str) -> bool:
    return slug in _load()


def due(now: Optional[float] = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    return sorted(
        (e for e in entries() if not e.get("parked") and float(e.get("nextAt") or 0) <= now),
        key=lambda e: float(e.get("firstAt") or 0),
    )


def update(slug: str, **changes: Any) -> None:
    with file_lock("kynver-skill-outbox"):
        all_ = _load()
        if slug in all_:
            all_[slug].update(changes)
            _save(all_)


def remove(slug: str) -> None:
    with file_lock("kynver-skill-outbox"):
        all_ = _load()
        if all_.pop(slug, None) is not None:
            _save(all_)


def backoff_seconds(fails: int, rng: random.Random = random) -> float:
    """Jittered exponential backoff, capped at 15 minutes."""
    base = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, fails - 1)))
    return base * (0.5 + rng.random() * 0.5)


def record_outage(slug: str, error: str, now: Optional[float] = None) -> None:
    """Network/server failure: retry later, never parks (an outage is not the skill's fault)."""
    now = time.time() if now is None else now
    e = get(slug)
    if not e:
        return
    fails = int(e.get("fails") or 0) + 1
    update(slug, fails=fails, nextAt=now + backoff_seconds(fails), lastError=error[:300])


def record_reject(slug: str, error: str, now: Optional[float] = None) -> bool:
    """Server refused this skill. Parks after MAX_REJECTS. Returns True if parked."""
    now = time.time() if now is None else now
    e = get(slug)
    if not e:
        return False
    rejects = int(e.get("rejects") or 0) + 1
    parked = rejects >= MAX_REJECTS
    update(slug, rejects=rejects, lastError=error[:300],
           nextAt=now + backoff_seconds(rejects),
           parked=("rejected" if parked else None))
    return parked


def park(slug: str, reason: str, error: str = "") -> None:
    update(slug, parked=reason, lastError=error[:300])


def stats(now: Optional[float] = None) -> dict[str, Any]:
    """Numbers for the ``skill-sync-drift`` diagnostic and the status CLI."""
    now = time.time() if now is None else now
    es = entries()
    live = [e for e in es if not e.get("parked")]
    oldest = min((float(e.get("firstAt") or now) for e in es), default=None)
    return {
        "depth": len(es),
        "pending": len(live),
        "parked": len(es) - len(live),
        "parkedSlugs": sorted(e["slug"] for e in es if e.get("parked"))[:20],
        "oldestAgeSeconds": int(now - oldest) if oldest is not None else 0,
    }
