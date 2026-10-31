"""Background worker + skill-event listener for Kynver skill sync.

``register_skill_sync_hooks`` subscribes to Hermes' standard plugin hooks
(``on_skill_lifecycle`` for create/patch/edit/archive/restore/load and
``post_tool_call`` for a hard ``skill_manage`` delete) -- no Hermes core change.
``start`` is called once per process by the Kynver provider; it builds the
service the hooks forward to (cheap, local-file work only) and, for primary
agent contexts, starts one daemon thread that refreshes the cache
(H1), drains the outbox (H2) and flushes usage (H3). Each part is gated by its
own ``kynver.skills.*`` flag, all default off.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Dict, Optional

from . import outbox, usage
from .bundle import slug_for
from .drain import Drainer
from .refresher import Refresher
from .store import get_base, load_state

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_ID = "hermes:forge"
DEFAULT_REFRESH_SECONDS = 600
TICK_SECONDS = 30
_WRITE_ACTIONS = {"create", "edit", "patch", "write_file", "remove_file"}


def load_skill_sync_config() -> dict[str, Any]:
    """``kynver.skills`` from config.yaml (defaults: everything off)."""
    cfg: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config
        cfg = ((load_config() or {}).get("kynver") or {}).get("skills") or {}
    except Exception:
        logger.debug("kynver skill sync: config unreadable", exc_info=True)
    return {
        "cache_first": bool(cfg.get("cache_first", False)),
        "write_through": bool(cfg.get("write_through", False)),
        "report_usage": bool(cfg.get("report_usage", False)),
        "initial_pull": bool(cfg.get("initial_pull", False)),
        "refresh_interval_seconds": max(60, int(cfg.get("refresh_interval_seconds") or DEFAULT_REFRESH_SECONDS)),
        "source_id": str(cfg.get("source_id") or DEFAULT_SOURCE_ID),
    }


class SkillSyncService:
    def __init__(self, client: Any, cfg: dict[str, Any]):
        self.client = client
        self.cfg = cfg
        self.source_id = cfg["source_id"]
        self.refresher = Refresher(client, self.source_id, allow_initial_pull=cfg["initial_pull"])
        self.drainer = Drainer(client, self.source_id)
        self.reporter = usage.UsageReporter(client)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_refresh = 0.0

    # -- listener (turn path: local files only) -------------------------------
    def on_skill_event(self, kind: str, name: str, info: dict) -> None:
        slug = slug_for(name)
        if kind == "view":
            if self.cfg["report_usage"]:
                usage.record_load(slug, None)
            return
        if not self.cfg["write_through"]:
            return
        if kind == "write":
            action = info.get("action")
            if action == "delete":
                outbox.mark_dirty(slug, "archive", get_base(slug))
            elif action in _WRITE_ACTIONS:
                outbox.mark_dirty(slug, "upsert", get_base(slug))
            else:
                return
        elif kind == "archive":
            outbox.mark_dirty(slug, "archive", get_base(slug))
        elif kind == "restore":
            outbox.mark_dirty(slug, "restore", get_base(slug))
        else:
            return
        self._wake.set()

    # -- background loop -------------------------------------------------------
    def start_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kynver-skill-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def notify(self) -> None:
        """Ask for an immediate refresh (change notification)."""
        self._last_refresh = 0.0
        self._wake.set()

    def tick(self) -> dict[str, Any]:
        """One loop pass; separate from the thread so tests can drive it."""
        out: dict[str, Any] = {}
        now = time.time()
        if self.cfg["cache_first"] and now - self._last_refresh >= self.cfg["refresh_interval_seconds"]:
            self._last_refresh = now
            out["refresh"] = self.refresher.refresh_once()
        if self.cfg["write_through"]:
            out["drain"] = self.drainer.drain_once()
        if self.cfg["report_usage"]:
            out["usage"] = self.reporter.flush_once()
        return out

    def _run(self) -> None:
        # Small startup jitter so a fleet of gateways doesn't hit Kynver together.
        self._stop.wait(random.uniform(0.5, 3.0))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.debug("kynver skill sync tick failed", exc_info=True)
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()


_service: Optional[SkillSyncService] = None
_lock = threading.Lock()
_suppress = threading.local()

# on_skill_lifecycle action -> (kind, skill_manage-style action) for on_skill_event.
_LIFECYCLE_EVENTS = {
    "created": ("write", "create"),
    "patched": ("write", "patch"),
    "edited": ("write", "edit"),
    "archived": ("archive", None),
    "restored": ("restore", None),
    "loaded": ("view", None),
}


class suppress_events:
    """Ignore skill hooks fired by this thread (the refresher applying a pull must not echo it back)."""

    def __enter__(self) -> suppress_events:
        _suppress.depth = getattr(_suppress, "depth", 0) + 1
        return self

    def __exit__(self, *exc: object) -> None:
        _suppress.depth = max(0, getattr(_suppress, "depth", 1) - 1)


def _dispatch(kind: str, name: str, info: dict[str, Any]) -> None:
    svc = _service
    if svc is None or not name or getattr(_suppress, "depth", 0):
        return
    try:
        svc.on_skill_event(kind, name, info)
    except Exception:
        logger.debug("kynver skill sync: %s event for %s failed", kind, name, exc_info=True)


def on_skill_lifecycle(action: str = "", skill_name: str = "", **_: Any) -> None:
    """Plugin hook: Hermes' authoritative skill lifecycle facts. Never raises."""
    mapped = _LIFECYCLE_EVENTS.get(str(action or ""))
    if mapped:
        kind, write_action = mapped
        _dispatch(kind, str(skill_name or ""), {"action": write_action} if write_action else {})


def on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, **_: Any) -> None:
    """Plugin hook: a hard ``skill_manage`` delete has no lifecycle fact (curator deletes
    archive instead and arrive as ``archived``). Never raises."""
    if tool_name != "skill_manage" or not isinstance(args, dict) or args.get("action") != "delete":
        return
    try:
        import json
        res = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return
    if isinstance(res, dict) and res.get("success") and not res.get("_archived"):
        _dispatch("write", str(args.get("name") or ""), {"action": "delete"})


def register_skill_sync_hooks(ctx: Any) -> None:
    """Subscribe to the standard plugin hooks. Cheap no-ops until ``start`` builds the service."""
    ctx.register_hook("on_skill_lifecycle", on_skill_lifecycle)
    ctx.register_hook("post_tool_call", on_post_tool_call)


def _recover_interrupted_apply() -> None:
    """Put back a skill whose pull-apply was SIGKILLed after the old copy moved aside.

    Local file moves only, run before any skill is loaded in this process so the skill
    is never missing (the worker's own recovery waits for its startup jitter, and
    non-primary contexts have no worker).
    """
    try:
        from .refresher import recover_interrupted
        recover_interrupted()
    except Exception:
        logger.debug("kynver skill sync: recovery check failed", exc_info=True)


def start(client: Any, *, primary: bool, cfg: Optional[dict[str, Any]] = None) -> Optional[SkillSyncService]:
    """Idempotent. Builds the service the hooks forward to; ``primary`` also starts the worker thread."""
    global _service
    _recover_interrupted_apply()
    cfg = cfg or load_skill_sync_config()
    if not (cfg["cache_first"] or cfg["write_through"] or cfg["report_usage"]):
        return None
    with _lock:
        if _service is None:
            _service = SkillSyncService(client, cfg)
        if primary:
            _service.start_thread()
        return _service


def shutdown() -> None:
    global _service
    with _lock:
        if _service is not None:
            _service.stop()
            _service = None


def status() -> dict[str, Any]:
    """Outbox + cache numbers (diagnostics / future ``status`` CLI)."""
    st = load_state()
    return {
        "outbox": outbox.stats(),
        "usageBuffered": usage.buffered(),
        "usageDropped": int(st.get("usageDropped") or 0),
        "lastPullAt": st.get("lastPullAt"),
        "lastCheckAt": st.get("lastCheckAt"),
        "lastResult": st.get("lastResult"),
        "cursor": st.get("cursor"),
    }
