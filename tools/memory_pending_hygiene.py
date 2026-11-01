"""Keep the pending-memory queue small: supersede, expire, cap, skip no-ops, nag rarely.

Background review stages destructive memory edits (patch/remove/replace) under
``<HERMES_HOME>/pending/memory/`` because they cannot apply unattended. Left alone the queue
grew without bound (103 records in 10 days, mostly rewordings of the same few entries). Rules:

* at most one open record per ``matched_entry``: a new proposal supersedes the old one
* a record whose ``matched_entry`` is gone/changed (it can no longer apply) or that is older
  than ``memory.pending_ttl_days`` is dropped
* background review stops proposing at ``memory.pending_max_open`` open records, and skips the
  consolidation pass when MEMORY.md/USER.md are unchanged since the last staged proposal
* pure rewordings (same facts, same terms) are not staged
* the Telegram/CLI notice carries the total and is sent at most once per ``memory.pending_nag_hours``
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 3
DEFAULT_MAX_OPEN = 5
DEFAULT_NAG_HOURS = 24
_DESTRUCTIVE = ("replace", "remove", "patch")


def settings() -> dict[str, float]:
    """``memory.pending_ttl_days`` / ``pending_max_open`` / ``pending_nag_hours`` with defaults."""
    out = {"ttl_days": DEFAULT_TTL_DAYS, "max_open": DEFAULT_MAX_OPEN, "nag_hours": DEFAULT_NAG_HOURS}
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        for key, cfg_key in (("ttl_days", "pending_ttl_days"), ("max_open", "pending_max_open"),
                             ("nag_hours", "pending_nag_hours")):
            value = cfg_get(cfg, "memory", cfg_key, default=None)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                out[key] = value
    except Exception:
        pass
    return out


# --- payload helpers ---

def _ops(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ops = payload.get("operations") if payload.get("action") == "batch" else [payload]
    return [op for op in (ops or []) if isinstance(op, dict)]


def matched_entries(payload: dict[str, Any]) -> set[str]:
    """The pinned entries a staged payload would edit."""
    return {op["matched_entry"] for op in _ops(payload)
            if op.get("action") in _DESTRUCTIVE and op.get("matched_entry")}


def _unpinned_destructive(payload: dict[str, Any]) -> bool:
    return any(op.get("action") in _DESTRUCTIVE and not op.get("matched_entry") for op in _ops(payload))


# --- cosmetic-reword heuristic ---

_FACT_RE = re.compile(
    r"https?://\S+|[~/\\][\w./\\~-]{2,}|\b\w+(?:[./:_-]\w+)+\b|\b\d[\d.,:%/-]*\w*\b|`[^`]+`|\b[A-Z][A-Z0-9_]{2,}\b|\b[A-Z][a-z]+[A-Z]\w*\b")
_TERM_RE = re.compile(r"[a-z][a-z'-]{4,}")


def _facts(text: str) -> set[str]:
    return {m.group(0).strip(".,;:()'\"`").lower() for m in _FACT_RE.finditer(text)} - {""}


def _terms(text: str) -> set[str]:
    return set(_TERM_RE.findall(text.lower()))


def is_cosmetic_rewrite(old: str, new: str, min_term_overlap: float = 0.8) -> bool:
    """True when ``new`` carries the same numbers/paths/URLs/identifiers as ``old`` and nearly the
    same content words, i.e. only wording/length changed. Cheap, no model call."""
    if not old or not new:
        return False
    if old.strip() == new.strip():
        return True
    if _facts(old) != _facts(new):
        return False
    a, b = _terms(old), _terms(new)
    union = a | b
    return not union or len(a & b) / len(union) >= min_term_overlap


def _new_text(op: dict[str, Any]) -> Optional[str]:
    old = op.get("matched_entry") or ""
    if op.get("action") == "replace":
        return op.get("content")
    if op.get("action") == "patch" and op.get("old_text") and old:
        return old.replace(op["old_text"], op.get("content") or "", 1)
    return None  # remove (or unknowable) is never cosmetic


def payload_is_cosmetic(payload: dict[str, Any]) -> bool:
    """Every destructive op is a pure rewording of its pinned entry (and there is at least one)."""
    ds = [op for op in _ops(payload) if op.get("action") in _DESTRUCTIVE]
    if not ds:
        return False
    for op in ds:
        new = _new_text(op)
        if new is None or not is_cosmetic_rewrite(op.get("matched_entry") or "", new):
            return False
    return True


# --- state file (kept OUTSIDE pending/memory so it is never counted as a record) ---

def _state_path() -> Path:
    return get_hermes_home() / "pending" / ".memory_review_state.json"


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        from utils import atomic_json_write
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(_state_path(), state)
    except Exception:
        logger.warning("Could not save memory review state", exc_info=True)


def memory_hash(store: Any) -> str:
    """Hash of MEMORY.md + USER.md as they are on disk now."""
    h = hashlib.sha256()
    for target in ("memory", "user"):
        try:
            h.update(store._path_for(target).read_bytes())
        except Exception:
            pass
        h.update(b"\0")
    return h.hexdigest()[:16]


# --- queue maintenance ---

def _current_entries(store: Any, target: str) -> set[str]:
    return set(store._read_file(store._path_for(target)))


def _stale_reason(record: dict[str, Any], entries_by_target: dict[str, set[str]], now: float,
                  ttl_days: float) -> Optional[str]:
    payload = record.get("payload") or {}
    if ttl_days and now - float(record.get("created_at") or 0) > ttl_days * 86400:
        return "expired"
    if _unpinned_destructive(payload):
        return "unpinned"
    current = entries_by_target.get(payload.get("target", "memory"), set())
    if any(e not in current for e in matched_entries(payload)):
        return "entry_changed"
    return None


def prune(store: Any, *, now: Optional[float] = None) -> dict[str, list[str]]:
    """Drop records that can no longer apply or are older than the TTL. Returns ``{reason: [ids]}``."""
    from tools import write_approval as wa
    now = time.time() if now is None else now
    ttl = settings()["ttl_days"]
    entries: dict[str, set[str]] = {}
    dropped: dict[str, list[str]] = {}
    for record in wa.list_pending(wa.MEMORY):
        payload = record.get("payload") or {}
        if payload.get("action") == "add":
            reason = "expired" if ttl and now - float(record.get("created_at") or 0) > ttl * 86400 else None
        else:
            target = payload.get("target", "memory")
            if target not in entries:
                entries[target] = _current_entries(store, target)
            reason = _stale_reason(record, entries, now, ttl)
        if reason and wa.discard_pending(wa.MEMORY, record["id"]):
            dropped.setdefault(reason, []).append(record["id"])
    return dropped


def supersede(payload: dict[str, Any]) -> list[str]:
    """Discard open records sharing a matched_entry (same target) with ``payload``; returns ids."""
    from tools import write_approval as wa
    mine = matched_entries(payload)
    if not mine:
        return []
    target = payload.get("target", "memory")
    gone = []
    for record in wa.list_pending(wa.MEMORY):
        other = record.get("payload") or {}
        if other.get("target", "memory") == target and matched_entries(other) & mine:
            if wa.discard_pending(wa.MEMORY, record["id"]):
                gone.append(record["id"])
    return gone


def open_count() -> int:
    from tools import write_approval as wa
    return wa.pending_count(wa.MEMORY)


def queue_full(store: Any) -> bool:
    prune(store)
    return open_count() >= int(settings()["max_open"])


def should_skip_review(store: Any) -> Optional[str]:
    """Reason to skip a memory-consolidation review pass, else None."""
    if queue_full(store):
        return "pending memory queue at cap"
    last = _load_state().get("last_staged_hash")
    if last and last == memory_hash(store):
        return "memory unchanged since last staged proposal"
    return None


def should_nag(*, now: Optional[float] = None) -> bool:
    """True (and records it) at most once per ``pending_nag_hours``."""
    now = time.time() if now is None else now
    state = _load_state()
    if now - float(state.get("last_nag_at") or 0) < settings()["nag_hours"] * 3600:
        return False
    state["last_nag_at"] = now
    _save_state(state)
    return True


def stage(store: Any, payload: dict[str, Any], *, summary: str, origin: str) -> dict[str, Any]:
    """Stage ``payload`` with queue hygiene. Returns ``{"record": rec|None, "skipped": reason|None,
    "superseded": [ids], "pruned": {...}, "total": n}``. Background proposals are refused when
    the queue is at cap or the change is cosmetic; every stage supersedes overlapping records."""
    from tools import write_approval as wa
    background = origin == "background_review"
    pruned = prune(store)
    if background and payload_is_cosmetic(payload):
        return {"record": None, "skipped": "cosmetic", "superseded": [], "pruned": pruned, "total": open_count()}
    superseded = supersede(payload)
    if background and open_count() >= int(settings()["max_open"]):
        return {"record": None, "skipped": "cap", "superseded": superseded, "pruned": pruned,
                "total": open_count()}
    record = wa.stage_write(wa.MEMORY, payload, summary=summary, origin=origin)
    if background:
        state = _load_state()
        state["last_staged_hash"] = memory_hash(store)
        _save_state(state)
    return {"record": record, "skipped": None, "superseded": superseded, "pruned": pruned,
            "total": open_count()}


# --- diagnostics ---

def queue_health(*, now: Optional[float] = None) -> dict[str, Any]:
    """Read-only: ``{count, cap, ttl_days, expired: [ids], ok}`` for ``hermes doctor``."""
    from tools import write_approval as wa
    now = time.time() if now is None else now
    s = settings()
    records = wa.list_pending(wa.MEMORY)
    expired = [r.get("id") for r in records
               if s["ttl_days"] and now - float(r.get("created_at") or 0) > s["ttl_days"] * 86400]
    return {"count": len(records), "cap": int(s["max_open"]), "ttl_days": s["ttl_days"],
            "expired": expired, "ok": len(records) <= int(s["max_open"]) and not expired}
