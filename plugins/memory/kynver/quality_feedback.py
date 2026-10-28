"""Automatic memory-quality feedback: retrieval misses, retrieval use, chat corrections.

Kynver's L1 memory-health scan reads ``/memory/quality-feedback`` events. Before this
module, Hermes only produced them when the agent superseded a memory by hand, so a day
of 429s (every recall refused, the agent forgetting its own identity) left no trace.
This reporter records those events without the agent having to remember:

* ``retrieval_miss`` (negative) when recall fails (``rate_limited`` / ``unreachable`` /
  ``error``) or succeeds with nothing above the relevance floor (``empty``);
* ``retrieval_used`` (positive) when memories are injected, once per query per session,
  so health has a denominator;
* ``human_correction`` (negative) when the user's message reads like "you forgot" /
  "I already told you". It is left unclassified for the daily scan to label.

Posting never runs on the turn thread. A post that fails because Kynver is down or
rate-limited is kept in a bounded per-profile buffer (memory + JSONL under the profile
home) and flushed after the next successful Kynver call, so an outage is recorded once
Kynver is back. A rate-limited post is never retried in a loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home, hermes_home_key

from .agentos_bridge import redact
from .contract import QUALITY_FEEDBACK_PATH, make_idempotency_key
from .prefetch_guard import retry_after_seconds

logger = logging.getLogger(__name__)

MAX_BUFFERED = 200
MAX_TEXT_CHARS = 120
POST_TIMEOUT_SECONDS = 2.0
BUFFER_FILENAME = "quality_feedback_queue.jsonl"

MISS_RATE_LIMITED = "rate_limited"
MISS_UNREACHABLE = "unreachable"
MISS_ERROR = "error"
MISS_EMPTY = "empty"

_UNREACHABLE_STATUSES = frozenset({408, 502, 503, 504})
# Kept for a later flush; anything else in 4xx is a permanent refusal and is dropped.
_RETRYABLE_STATUSES = _UNREACHABLE_STATUSES | {429}

# Cheap, deliberately loose: false positives are triaged by the daily scan.
_CORRECTION_RE = re.compile(
    r"\balready (?:been )?(?:approved|decided|agreed|merged|done|shipped|covered|discussed)\b"
    r"|\bi(?:'ve| have)? (?:already |just )?(?:told|said|mentioned|explained)(?: (?:you|this|that))?\b"
    r"|\b(?:don'?t|do not|stop|quit) go(?:ing)? backwards?\b"
    r"|\byou(?:'ve| have)? forg(?:ot|otten)\b"
    r"|\bdid you forget\b"
    r"|\bwe (?:already |just )?(?:decided|agreed|settled)\b"
    r"|\bthat'?s (?:wrong|not right|incorrect|not what i said)\b"
    r"|\b(?:as|like) i said\b"
    r"|^\s*i said\b"
    r"|\bremember that\b"
    r"|\byou(?:'re| are) (?-i:[A-Z])[\w-]*[,.;:]\s*you\b",
    re.IGNORECASE,
)


_CRON_PREFIX = "[IMPORTANT: You are running as a scheduled cron job."
# Gateway reply context: '[Replying to: "<quoted bot text>"]\n\n<user text>' (gateway/run_inbound.py).
_REPLY_QUOTE_RE = re.compile(r'^\[Replying to[^\n]*?: ".*?"\]\n\n', re.DOTALL)
# Recalled-memory block the gateway may append to the user turn.
_MEMORY_CONTEXT_RE = re.compile(r"<memory-context>.*?(?:</memory-context>|$)", re.DOTALL)


def human_text(message: str) -> str:
    """Only the words the human typed this turn: drops cron prompts, slash-skill bodies,
    the quoted bot message in a reply, and injected memory context. Those are agent- or
    system-authored, so a correction phrase inside them is not a correction."""
    text = message or ""
    if text.startswith(_CRON_PREFIX):
        return ""
    try:
        from agent.skill_commands import extract_user_instruction_from_skill_message

        text = extract_user_instruction_from_skill_message(text) or ""
    except Exception:
        if text.startswith("[IMPORTANT: The user has invoked the "):
            return ""
    text = _MEMORY_CONTEXT_RE.sub("", text)
    return _REPLY_QUOTE_RE.sub("", text, count=1).strip()


def detect_correction(message: str) -> str:
    """The phrase that makes the human part of *message* look like a correction of the agent, or ""."""
    match = _CORRECTION_RE.search(human_text(message))
    return match.group(0).strip() if match else ""


def miss_reason(exc: BaseException) -> str:
    """Reason class for a failed recall: rate_limited | unreachable | error."""
    status = getattr(exc, "status", None)
    if status == 429:
        return MISS_RATE_LIMITED
    if status is None or status in _UNREACHABLE_STATUSES:
        return MISS_UNREACHABLE
    return MISS_ERROR


def short_text(text: str) -> str:
    return " ".join(redact(text or "").split())[:MAX_TEXT_CHARS]


def memory_ids(items: Iterable[Dict[str, Any]]) -> List[str]:
    return [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]


@dataclass(frozen=True)
class FeedbackSettings:
    retrieval_miss: bool = True
    retrieval_used: bool = True
    human_correction: bool = True

    def allows(self, kind: str) -> bool:
        return bool(getattr(self, kind, False))


def load_feedback_settings() -> FeedbackSettings:
    """``kynver.quality_feedback`` in config.yaml: a bool, or a mapping with ``enabled`` and
    one bool per outcome kind. Everything defaults on."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        config = {}
    kynver = config.get("kynver") if isinstance(config, dict) else None
    raw = kynver.get("quality_feedback", True) if isinstance(kynver, dict) else True
    if isinstance(raw, bool):
        return FeedbackSettings(raw, raw, raw)
    if not isinstance(raw, dict):
        return FeedbackSettings()
    if raw.get("enabled", True) is False:
        return FeedbackSettings(False, False, False)
    return FeedbackSettings(
        retrieval_miss=raw.get("retrieval_miss", True) is not False,
        retrieval_used=raw.get("retrieval_used", True) is not False,
        human_correction=raw.get("human_correction", True) is not False,
    )


def _buffer_path() -> Path:
    return get_hermes_home() / "kynver" / BUFFER_FILENAME


class QualityFeedbackReporter:
    """Fire-and-forget poster for ``/memory/quality-feedback`` with an outage buffer.

    ``client_getter`` returns the provider's client (``_require_client``) so the reporter
    uses the same transport and credential. ``spawn`` starts background work under the
    caller's profile scope (``spawn_context_thread``)."""

    def __init__(
        self,
        client_getter: Callable[[], Any],
        *,
        spawn: Callable[..., threading.Thread],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client_getter = client_getter
        self._spawn = spawn
        self._clock = clock
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        # home key -> idempotencyKey -> payload; loaded lazily from the JSONL per profile.
        self._buffers: Dict[str, "OrderedDict[str, Dict[str, Any]]"] = {}
        self._cooldown_until = 0.0
        self._used_queries: "OrderedDict[str, None]" = OrderedDict()
        self._threads: List[threading.Thread] = []
        self._warned_forbidden = False
        self.stats: Dict[str, int] = {
            "posted": 0,
            "buffered": 0,
            "rate_limited": 0,
            "rejected": 0,
            "overflow_dropped": 0,
        }

    # -- public entry points (turn thread; never block) ---------------------------------

    def record(
        self,
        outcome_kind: str,
        *,
        signal: str,
        session_id: str,
        turn: int,
        dedupe_text: str,
        query_text: str = "",
        note: str = "",
        ids: Optional[List[str]] = None,
    ) -> None:
        if not load_feedback_settings().allows(outcome_kind):
            return
        if outcome_kind == "retrieval_used" and not self._first_use(session_id, dedupe_text):
            return
        query_hash = hashlib.sha256(" ".join(dedupe_text.lower().split()).encode("utf-8")).hexdigest()[:16]
        idem_key = make_idempotency_key("hermes:quality-feedback", session_id, turn, outcome_kind, query_hash)
        payload = {
            name: value
            for name, value in {
                "signal": signal,
                "outcomeKind": outcome_kind,
                "queryText": short_text(query_text),
                "memoryIds": ids or [],
                "note": note,
                "idempotencyKey": idem_key,
            }.items()
            if value
        }
        self._start(self._deliver, payload)

    def flush_if_pending(self) -> None:
        """Called after any successful Kynver call: drain what an outage left behind."""
        home = hermes_home_key()
        with self._lock:
            known = home in self._buffers
            pending = bool(self._buffers.get(home))
        if (pending or not known) and not self._flush_lock.locked():
            self._start(self._flush)

    def wait_idle(self, timeout: float) -> None:
        """Bounded join of in-flight posts (shutdown); a flush may start more while we wait."""
        deadline = self._clock() + timeout
        while True:
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
            remaining = deadline - self._clock()
            if not alive or remaining <= 0:
                return
            alive[0].join(remaining)

    def pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buffer_locked(hermes_home_key()).values())

    # -- background ---------------------------------------------------------------------

    def _start(self, target: Callable[..., None], *args: Any) -> None:
        thread = self._spawn(target, name="kynver-quality-feedback", args=args)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _first_use(self, session_id: str, text: str) -> bool:
        marker = f"{session_id}\0{' '.join(text.lower().split())}"
        with self._lock:
            if marker in self._used_queries:
                return False
            self._used_queries[marker] = None
            while len(self._used_queries) > 1024:
                self._used_queries.popitem(last=False)
        return True

    def _deliver(self, payload: Dict[str, Any]) -> None:
        try:
            if self._post(payload):
                self._flush()
        except Exception as exc:  # the reporter must never surface into a turn
            logger.debug("Kynver quality feedback failed: %s", redact(str(exc)))

    def _flush(self) -> None:
        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                queued = list(self._buffer_locked(hermes_home_key()).values())
            for payload in queued:
                if not self._post(payload, buffered=True):
                    break
        except Exception as exc:
            logger.debug("Kynver quality feedback flush failed: %s", redact(str(exc)))
        finally:
            self._flush_lock.release()

    def _post(self, payload: Dict[str, Any], *, buffered: bool = False) -> bool:
        """Post once. True when Kynver answered (accepted or permanently refused)."""
        key = payload["idempotencyKey"]
        if self._clock() < self._cooldown_until:
            if not buffered:
                self._buffer(payload)
            return False
        try:
            self._client_getter().post(QUALITY_FEEDBACK_PATH, payload, timeout=POST_TIMEOUT_SECONDS)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 409:  # idempotent replay of an event Kynver already has
                self._unbuffer(key)
                return True
            if status is not None and 400 <= status < 500 and status not in _RETRYABLE_STATUSES:
                self._count("rejected")
                self._unbuffer(key)
                log = logger.debug
                if status in (401, 403) and not self._warned_forbidden:
                    # The feedback route needs operator (owner/admin) access on the AgentOS.
                    self._warned_forbidden = True
                    log = logger.warning
                log("Kynver refused quality feedback (HTTP %s): %s", status, redact(str(exc)))
                return True
            if status == 429:
                self._count("rate_limited")
                with self._lock:
                    self._cooldown_until = max(self._cooldown_until, self._clock() + (retry_after_seconds(exc) or 0))
            if not buffered:
                self._buffer(payload)
            logger.debug("Kynver quality feedback deferred: %s", redact(str(exc)))
            return False
        self._count("posted")
        self._unbuffer(key)
        return True

    def _count(self, name: str) -> None:
        with self._lock:
            self.stats[name] += 1

    # -- per-profile buffer -------------------------------------------------------------

    def _buffer_locked(self, home: str) -> "OrderedDict[str, Dict[str, Any]]":
        buffer = self._buffers.get(home)
        if buffer is None:
            buffer = OrderedDict()
            try:
                lines = _buffer_path().read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines[-MAX_BUFFERED:]:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("idempotencyKey"):
                    buffer[str(row["idempotencyKey"])] = row
            self._buffers[home] = buffer
        return buffer

    def _buffer(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            buffer = self._buffer_locked(hermes_home_key())
            buffer[payload["idempotencyKey"]] = payload
            self.stats["buffered"] += 1
            while len(buffer) > MAX_BUFFERED:
                buffer.popitem(last=False)
                self.stats["overflow_dropped"] += 1
            self._save_locked(buffer)

    def _unbuffer(self, key: str) -> None:
        with self._lock:
            buffer = self._buffer_locked(hermes_home_key())
            if buffer.pop(key, None) is not None:
                self._save_locked(buffer)

    @staticmethod
    def _save_locked(buffer: "OrderedDict[str, Dict[str, Any]]") -> None:
        path = _buffer_path()
        try:
            if not buffer:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in buffer.values()),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            logger.debug("Kynver quality feedback buffer not persisted: %s", exc)
