"""Kynver-owned Hermes todo projection over AgentOS tasks."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tools.todo_tool import VALID_STATUSES, TodoStore

from .agentos_bridge import KynverAgentOSClient, KynverAgentOSError
from .operating_context import (
    OperatingContext,
    ensure_intake_task,
    load_operating_context,
    todo_idempotency_key,
)

logger = logging.getLogger(__name__)

_HERMES_TODO_PREFIX = "hermes-forge:todo:"

# AgentOS → Hermes status projection
_AGENTOS_TO_HERMES = {
    "ready": "pending",
    "waiting": "pending",
    "scheduled": "pending",
    "running": "in_progress",
    "blocked": "in_progress",
    "needs_input": "in_progress",
    "awaiting_review": "in_progress",
    "done": "completed",
    "failed": "cancelled",
    "cancelled": "cancelled",
}

_HERMES_TO_AGENTOS_CREATE = {
    "pending": "ready",
    "in_progress": "running",
}

_TERMINAL_HERMES = {"completed", "cancelled"}
_TERMINAL_CLOSE = {
    "completed": "done",
    "cancelled": "cancelled",
}


def _coerce_tasks(payload: Any) -> List[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("tasks", "items", "results", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return [t for t in val if isinstance(t, dict)]
    return []


def _task_idempotency(task: dict[str, Any]) -> str:
    return str(
        task.get("idempotencyKey")
        or task.get("idempotency_key")
        or (task.get("metadata") or {}).get("idempotencyKey")
        or ""
    )


def _task_to_todo(task: dict[str, Any]) -> Dict[str, str]:
    idem = _task_idempotency(task)
    todo_id = idem[len(_HERMES_TODO_PREFIX) :] if idem.startswith(_HERMES_TODO_PREFIX) else str(
        task.get("id") or "?"
    )
    status_raw = str(task.get("status") or "ready").lower()
    hermes_status = _AGENTOS_TO_HERMES.get(status_raw, "pending")
    content = str(task.get("title") or task.get("description") or "(no description)").strip()
    return {"id": todo_id, "content": content, "status": hermes_status}


def _enforce_single_in_progress(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = False
    out: List[Dict[str, str]] = []
    for item in items:
        if item.get("status") == "in_progress":
            if seen:
                copy = dict(item)
                copy["status"] = "pending"
                out.append(copy)
                continue
            seen = True
        out.append(item)
    return out


class KynverTodoStore:
    """TodoStore that reads/writes AgentOS tasks (Kynver-first, local fallback)."""

    def __init__(
        self,
        client: KynverAgentOSClient,
        *,
        operating_context: Optional[OperatingContext] = None,
        allow_fallback: bool = True,
        degraded: bool = False,
        hermes_session_id: str = "",
    ):
        self._client = client
        self._ctx = operating_context or load_operating_context()
        self._allow_fallback = allow_fallback
        self._degraded = degraded
        self._hermes_session_id = hermes_session_id
        self._local = TodoStore()
        self._task_ids: Dict[str, str] = {}  # todo_id → agentos task id

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _mark_degraded(self, reason: str) -> None:
        self._degraded = True
        logger.warning("Kynver todo operating in degraded local fallback mode: %s", reason)

    def _list_forge_tasks(self) -> List[dict[str, Any]]:
        try:
            payload = self._client.get("/tasks?limit=200")
            tasks = _coerce_tasks(payload)
            return [t for t in tasks if _task_idempotency(t).startswith(_HERMES_TODO_PREFIX)]
        except Exception as exc:
            if self._allow_fallback:
                self._mark_degraded(str(exc))
                return []
            if isinstance(exc, KynverAgentOSError):
                raise
            raise KynverAgentOSError(str(exc)) from exc

    def read(self) -> List[Dict[str, str]]:
        if self._degraded:
            return self._local.read()
        tasks = self._list_forge_tasks()
        items = [_task_to_todo(t) for t in tasks]
        for t in tasks:
            idem = _task_idempotency(t)
            if idem.startswith(_HERMES_TODO_PREFIX):
                todo_id = idem[len(_HERMES_TODO_PREFIX) :]
                tid = str(t.get("id") or "")
                if todo_id and tid:
                    self._task_ids[todo_id] = tid
        return _enforce_single_in_progress(items)

    def _upsert_task(self, item: Dict[str, Any], ctx: OperatingContext) -> None:
        todo_id = str(item.get("id", "")).strip()
        if not todo_id:
            return
        content = str(item.get("content", "")).strip() or "(no description)"
        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"
        idem = todo_idempotency_key(todo_id)
        agentos_id = self._task_ids.get(todo_id)

        if status in _TERMINAL_HERMES:
            if not agentos_id:
                existing = self._list_forge_tasks()
                for t in existing:
                    if _task_idempotency(t) == idem:
                        agentos_id = str(t.get("id") or "")
                        break
            if agentos_id:
                self._client.post(
                    f"/tasks/{agentos_id}/close",
                    {
                        "status": _TERMINAL_CLOSE[status],
                        "summary": content[:500],
                    },
                )
            return

        body: dict[str, Any] = {
            "title": content[:500],
            "description": content,
            "status": _HERMES_TO_AGENTOS_CREATE.get(status, "ready"),
            "idempotencyKey": idem,
            "executor": "inline",
            "metadata": {"hermesTodoId": todo_id, "hermesSessionId": self._hermes_session_id},
        }
        if ctx.plan_id:
            body.setdefault("metadata", {})["planId"] = ctx.plan_id
        if ctx.task_id:
            body["parentTaskId"] = ctx.task_id
        if ctx.goal_id:
            body["goalId"] = ctx.goal_id
        if ctx.project_id:
            body["projectId"] = ctx.project_id

        if agentos_id:
            patch_body = {
                "title": body["title"],
                "description": body["description"],
                "status": body["status"],
                "lastSummary": f"hermes todo {status}",
            }
            result = self._client.patch(f"/tasks/{agentos_id}", patch_body)
        else:
            result = self._client.post("/tasks", body)
            if isinstance(result, dict):
                agentos_id = str(result.get("id") or result.get("taskId") or "")
                if agentos_id:
                    self._task_ids[todo_id] = agentos_id

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        if self._degraded:
            items = self._local.write(todos, merge=merge)
            return items

        ctx = ensure_intake_task(
            self._client,
            self._ctx,
            hermes_session_id=self._hermes_session_id,
        )

        try:
            if not merge:
                try:
                    current = self.read()
                except KynverAgentOSError as exc:
                    if not self._allow_fallback:
                        raise
                    self._mark_degraded(str(exc))
                    return self._local.write(todos, merge=False)
                for existing in current:
                    if existing.get("status") not in _TERMINAL_HERMES:
                        self._upsert_task(
                            {**existing, "status": "cancelled"},
                            ctx,
                        )
            for item in todos:
                self._upsert_task(item, ctx)
            if self._degraded:
                return self._local.write(todos, merge=merge)
            return self.read()
        except KynverAgentOSError as exc:
            if not self._allow_fallback:
                raise
            self._mark_degraded(str(exc))
            return self._local.write(todos, merge=merge)
        except Exception as exc:
            if not self._allow_fallback:
                raise KynverAgentOSError(str(exc)) from exc
            self._mark_degraded(str(exc))
            return self._local.write(todos, merge=merge)

    def has_items(self) -> bool:
        return bool(self.read())

    def format_for_injection(self) -> Optional[str]:
        base = TodoStore()
        base._items = self.read()  # noqa: SLF001 — reuse formatter
        text = base.format_for_injection()
        if self._degraded and text:
            return text + "\n[Kynver todo: degraded — using local fallback cache]"
        if self._degraded:
            return "[Kynver todo: degraded — local fallback; AgentOS task board unavailable]"
        if self._ctx.intake_required:
            return (
                (text + "\n") if text else ""
            ) + "[Kynver: operating context missing plan/task anchor — intake classification task required]"
        return text
