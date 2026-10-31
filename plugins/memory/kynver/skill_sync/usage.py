"""Skill usage buffer and batched reporter (H3).

``record_load`` is the only call on the turn path: it appends one small JSON
line to a local buffer (no network). The background worker flushes up to 500
events per POST to ``/skills/usage`` with client event ids, so a retried batch
never double counts on the server. The buffer is capped at 10k events (oldest
dropped, drop count reported) so an outage cannot grow it without bound.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from .store import atomic_write_json, file_lock, load_state, state_dir, update_state

logger = logging.getLogger(__name__)

BATCH = 500
BUFFER_CAP = 10_000
DEFAULT_TIMEOUT = 10.0


def _path():
    return state_dir() / "kynver-skill-usage.jsonl"


def _read_lines() -> list[str]:
    try:
        return [ln for ln in _path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def _write_lines(lines: list[str]) -> None:
    p = _path()
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def record_load(slug: str, actor: Optional[str] = None, now: Optional[float] = None) -> None:
    """Append a load event. Local file only; trims the oldest past the cap."""
    ev = {"clientEventId": uuid.uuid4().hex, "slug": slug, "kind": "load",
          "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))}
    if actor:
        ev["actorAgentId"] = actor
    with file_lock("kynver-skill-usage"):
        p = _path()
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        # Cheap size gate first; only re-read the file when it could be over the cap.
        if p.stat().st_size > BUFFER_CAP * 80:
            lines = _read_lines()
            if len(lines) > BUFFER_CAP:
                dropped = len(lines) - BUFFER_CAP
                _write_lines(lines[-BUFFER_CAP:])
                update_state(usageDropped=int(load_state().get("usageDropped") or 0) + dropped)


def buffered() -> int:
    return len(_read_lines())


class UsageReporter:
    def __init__(self, client: Any, *, timeout: float = DEFAULT_TIMEOUT):
        self.client = client
        self.timeout = timeout

    def flush_once(self) -> dict[str, Any]:
        """Send buffered events (oldest first, ≤500 per call, until empty or a failure)."""
        out = {"sent": 0, "accepted": 0, "duplicates": 0, "unknownSlug": 0, "invalid": 0, "offline": False}
        try:
            while True:
                with file_lock("kynver-skill-usage"):
                    lines = _read_lines()
                batch_lines = lines[:BATCH]
                if not batch_lines:
                    break
                events = []
                for ln in batch_lines:
                    try:
                        events.append(json.loads(ln))
                    except ValueError:
                        pass  # torn line: dropped below with the batch
                try:
                    _, _, res = self.client.request_raw("POST", "/skills/usage", {"events": events},
                                                        timeout=self.timeout) if events else (200, {}, {})
                except Exception as exc:
                    logger.info("kynver skill usage: flush deferred (%s)", exc)
                    out["offline"] = True
                    break
                with file_lock("kynver-skill-usage"):
                    # New events may have been appended meanwhile; drop only what we sent.
                    current = _read_lines()
                    _write_lines(current[len(batch_lines):] if current[:len(batch_lines)] == batch_lines
                                 else [ln for ln in current if ln not in set(batch_lines)])
                out["sent"] += len(events)
                if isinstance(res, dict):
                    for k in ("accepted", "duplicates", "unknownSlug", "invalid"):
                        out[k] += int(res.get(k) or 0)
                if len(batch_lines) < BATCH:
                    break
            if out["sent"]:
                st = load_state()
                update_state(usageSent=int(st.get("usageSent") or 0) + out["sent"],
                             usageUnknownSlug=int(st.get("usageUnknownSlug") or 0) + out["unknownSlug"],
                             lastUsageFlushAt=int(time.time()))
        except Exception as exc:
            logger.info("kynver skill usage: flush skipped (%s)", exc)
            out["offline"] = True
        return out
