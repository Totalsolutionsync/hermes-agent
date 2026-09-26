"""Kynver-owned Hermes todo store — plan progress rows + in_progress focus (not running)."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Union

from tools.todo_tool import BaseTodoStore, TodoStore

from .agentos_bridge import KynverAgentOSClient, KynverAgentOSError
from .operating_config import OperatingLinkage, load_operating_linkage
from .plan_binding import PROMOTE_MIN_ITEMS, TodoPlanResolver
from .plan_progress import (
    inspect_todo_write,
    project_todo_write,
    reconcile_todos_from_kynver,
)
from .pre_transition import PreTransitionError, normalize_todo_scope

logger = logging.getLogger(__name__)

ScopeSource = Union[str, Callable[[], Optional[str]], None]


class KynverTodoStore(BaseTodoStore):
    """TodoStore that projects to Kynver plan progress; falls back to local on failure.

    The revision belongs to the store, not to whichever backend answered: it bumps whenever
    the list handed back by read()/write() changes, so it stays monotonic across degraded
    fallback, recovery replay and read-back of rows edited elsewhere.

    Rows are keyed ``hermes-todo:<scope>:<id>`` and only the current scope is read back.
    ``scope`` is a string or a resolver called per operation (the agent's compression-lineage
    root, see ``integration.session_todo_scope``); without one it falls back to the linkage
    session id, then to a per-store id.

    With a ``plan_resolver`` each scope's rows go to the plan Kynver binds that session to
    (``plan_binding.TodoPlanResolver``); without one, to the fixed ``linkage.plan_id``
    (legacy ``KYNVER_PLAN_ID``). A 1–2 item list stays on the Inbox; the write that takes an
    Inbox-bound list to ``PROMOTE_MIN_ITEMS`` promotes it to its own plan first.

    Reads return the session's working set, never its history: see
    ``plan_progress.reconcile_todos_from_kynver``."""

    def __init__(
        self,
        client: KynverAgentOSClient,
        *,
        linkage: Optional[OperatingLinkage] = None,
        allow_fallback: bool = True,
        degraded: bool = False,
        retry_interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        scope: ScopeSource = None,
        plan_resolver: Optional[TodoPlanResolver] = None,
    ):
        self._client = client
        self._linkage = linkage or load_operating_linkage()
        self._allow_fallback = allow_fallback
        self._degraded = degraded
        self._retry_interval_seconds = max(0.0, retry_interval_seconds)
        self._clock = clock
        self._degraded_at = self._clock() if degraded else None
        self._pending_writes: List[tuple[str, List[Dict[str, Any]], bool]] = []
        self._local = TodoStore()
        self._last_items: List[Dict[str, str]] = []
        self._revision = 0
        self._scope_source = scope
        self._fallback_scope = f"local-{uuid.uuid4().hex[:12]}"
        self._active_scope: Optional[str] = None
        self._plan_resolver = plan_resolver

    def _can_project(self) -> bool:
        return self._plan_resolver is not None or bool(self._linkage.plan_id)

    def _linkage_for(self, scope: str) -> OperatingLinkage:
        """Linkage whose ``plan_id`` is ``scope``'s todo plan. May raise KynverAgentOSError."""
        if self._plan_resolver is None:
            return self._linkage
        return self._plan_resolver.linkage_for(scope)

    def _promoted_linkage(
        self, scope: str, items: List[Dict[str, str]], linkage: OperatingLinkage
    ) -> OperatingLinkage:
        """``linkage`` moved to the session's own plan when this list has outgrown the Inbox.
        A failed promotion leaves the list on the Inbox; the next write asks again."""
        if self._plan_resolver is None or len(items) < PROMOTE_MIN_ITEMS:
            return linkage
        title = next(
            (i["content"] for i in items if i.get("status") != "cancelled" and not i.get("parent")),
            items[0]["content"],
        )
        try:
            plan = self._plan_resolver.promote(scope, title=title)
        except KynverAgentOSError as exc:
            logger.warning("Kynver todo: could not move a %d-item list off the Inbox: %s", len(items), exc)
            return linkage
        if plan is None:
            return linkage
        logger.info("Kynver todo: %d-item list moved from the Inbox to plan %s", len(items), plan.plan_id)
        return replace(linkage, plan_id=plan.plan_id)

    def plan_for_current_scope(self) -> Optional[str]:
        """Plan id the current session's todos land on (None when local-only)."""
        return self._linkage_for(self._current_scope()).plan_id

    def _current_scope(self) -> str:
        """Resolve the session scope; a changed scope starts from an empty local cache so one
        session's items never leak into another's list."""
        source = self._scope_source
        raw = source() if callable(source) else source
        scope = normalize_todo_scope(raw or self._linkage.session_id or self._fallback_scope)
        if self._active_scope is not None and scope != self._active_scope:
            self._local = TodoStore()
        self._active_scope = scope
        return scope

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _mark_degraded(self, reason: str) -> None:
        self._degraded = True
        self._degraded_at = self._clock()
        logger.warning("Kynver todo store degraded to local fallback: %s", reason)

    def _recovery_due(self) -> bool:
        if not self._degraded:
            return False
        if self._degraded_at is None:
            return True
        return self._clock() - self._degraded_at >= self._retry_interval_seconds

    def _try_recover(self) -> Optional[List[Dict[str, str]]]:
        """Retry a read after the cooldown and clear transient degradation."""

        if not self._recovery_due() or not self._can_project():
            return None
        try:
            while self._pending_writes:
                pending_scope, pending_todos, pending_merge = self._pending_writes[0]
                # Replayed against the plan of the scope it was written in.
                pending_linkage = self._linkage_for(pending_scope)
                if not pending_linkage.plan_id:
                    self._pending_writes.pop(0)
                    continue
                try:
                    blocked = inspect_todo_write(
                        self._client,
                        pending_linkage,
                        pending_todos,
                        merge=pending_merge,
                        scope=pending_scope,
                    )
                    if blocked:
                        raise PreTransitionError(blocked)
                    project_todo_write(
                        self._client,
                        pending_linkage,
                        pending_todos,
                        merge=pending_merge,
                        scope=pending_scope,
                    )
                except PreTransitionError as exc:
                    self._pending_writes.pop(0)
                    logger.warning(
                        "Kynver todo recovery skipped a write blocked by transition policy: %s",
                        exc,
                    )
                    continue
                self._pending_writes.pop(0)
            local_items = self._local.read()
            scope = self._current_scope()
            linkage = self._linkage_for(scope)
            items = (
                reconcile_todos_from_kynver(self._client, linkage, local_items, scope=scope)
                if linkage.plan_id
                else local_items
            )
        except Exception as exc:
            self._mark_degraded(str(exc))
            return None
        self._degraded = False
        self._degraded_at = None
        logger.info("Kynver todo store recovered; AgentOS plan progress is available")
        return items

    def _publish(self, items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        if items != self._last_items:
            self._revision += 1
            self._last_items = [dict(item) for item in items]
        return items

    def read(self) -> List[Dict[str, str]]:
        return self._publish(self._read_items())

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        return self._publish(self._write_items(todos, merge))

    def snapshot(self) -> Dict[str, Any]:
        """State as of the last read/write/restore — no AgentOS round trip, so todo_tool and
        the TUI can pair it with the list they just got."""
        return {"todos": [dict(item) for item in self._last_items], "revision": self._revision}

    def restore(self, todos: List[Dict[str, Any]], *, revision: Any = 0) -> List[Dict[str, str]]:
        """Seed the local cache from a trusted snapshot (history hydration). Nothing is
        projected to AgentOS: replaying history must not rewrite plan rows."""
        self._current_scope()
        items = self._local.restore(todos, revision=revision)
        self._revision = self._local.snapshot()["revision"]
        self._last_items = [dict(item) for item in items]
        return items

    def _read_items(self) -> List[Dict[str, str]]:
        scope = self._current_scope()
        if self._degraded:
            recovered = self._try_recover()
            if recovered is not None:
                return recovered
            return self._local.read()
        if not self._can_project():
            return self._local.read()
        try:
            linkage = self._linkage_for(scope)
            if not linkage.plan_id:
                return self._local.read()
            return reconcile_todos_from_kynver(
                self._client,
                linkage,
                self._local.read(),
                scope=scope,
            )
        except KynverAgentOSError as exc:
            if not self._allow_fallback:
                raise
            self._mark_degraded(str(exc))
            return self._local.read()
        except Exception as exc:
            if not self._allow_fallback:
                raise KynverAgentOSError(str(exc)) from exc
            self._mark_degraded(str(exc))
            return self._local.read()

    def _write_items(self, todos: List[Dict[str, Any]], merge: bool) -> List[Dict[str, str]]:
        scope = self._current_scope()
        local_items = self._local.write(todos, merge=merge)
        write_batch = (scope, [dict(item) for item in todos], merge)

        if self._degraded:
            self._pending_writes.append(write_batch)
            recovered = self._try_recover()
            if self._degraded:
                return local_items
            if recovered is not None:
                return recovered
        if not self._can_project():
            return local_items

        try:
            linkage = self._linkage_for(scope)
            if not linkage.plan_id:
                return local_items
            linkage = self._promoted_linkage(scope, local_items, linkage)
            blocked = inspect_todo_write(
                self._client,
                linkage,
                list(todos),
                merge=merge,
                scope=scope,
            )
            if blocked:
                raise PreTransitionError(blocked)

            project_todo_write(
                self._client,
                linkage,
                list(todos),
                merge=merge,
                scope=scope,
            )
            return self._read_items()
        except PreTransitionError:
            raise
        except KynverAgentOSError as exc:
            if not self._allow_fallback:
                raise
            self._pending_writes.append(write_batch)
            self._mark_degraded(str(exc))
            return self._local.read()
        except Exception as exc:
            if not self._allow_fallback:
                raise KynverAgentOSError(str(exc)) from exc
            self._pending_writes.append(write_batch)
            self._mark_degraded(str(exc))
            return self._local.read()

    def has_items(self) -> bool:
        return bool(self.read())

    def format_for_injection(self) -> Optional[str]:
        base = TodoStore()
        base._items = self.read()  # noqa: SLF001 — reuse formatter
        text = base.format_for_injection()
        if self._degraded and text:
            return text + "\n[Kynver todo: degraded — using local fallback cache]"
        if self._degraded:
            return "[Kynver todo: degraded — local fallback; AgentOS plan progress unavailable]"
        return text
