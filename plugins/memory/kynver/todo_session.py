"""One call per todo read/write: ``POST /api/agent-os/{slug}/todos/session``.

Kynver plans are the single source of truth. Kynver resolves the session's plan (binding →
task's plan → Inbox, promoted to its own plan at 3 items), adopts a handed-over plan, applies
the write and returns the session's view — the rows any chat, cron, Kynver agent or Will in
the Kynver UI may have ticked since. Before this endpoint one write cost 6–8 calls (binding,
row reads for guards, upserts, focus) and hit HTTP 429 on the shared key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .agentos_bridge import KynverAgentOSClient, KynverAgentOSError
from .plan_binding import session_key_for_scope

SESSION_PATH = "/todos/session"
# `session:<id>` adopts another session's list; Hermes session ids become `session:hermes:<scope>`.
SESSION_REF_PREFIX = "session:"

# How a change made elsewhere is labelled in the todo result.
_SOURCE_LABELS = {
    "hermes-chat": "Hermes chat",
    "hermes-cron": "Hermes cron",
    "kynver-agent": "Kynver agent",
    "kynver-ui": "Kynver UI",
    "mcp": "MCP",
    "api": "API",
}
_HERMES_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


class SessionEndpointMissing(Exception):
    """This Kynver predates ``/todos/session`` (HTTP 404/405): use the legacy projection."""


@dataclass
class SessionView:
    items: List[Dict[str, str]]
    plan: Optional[Dict[str, Any]] = None
    errors: List[Dict[str, str]] = field(default_factory=list)
    omitted: int = 0
    adopted: bool = False
    # id -> epoch seconds the item went in progress (stale-work hints).
    since: Dict[str, float] = field(default_factory=dict)


def describe_actor(by: Any) -> Optional[str]:
    """``"Telegram · Will H (Hermes chat)"`` / ``"Kynver UI"`` for an item's ``by``."""
    if not isinstance(by, dict):
        return None
    source = str(by.get("source") or "")
    label = _SOURCE_LABELS.get(source, source or "someone else")
    name = " ".join(str(by.get("name") or "").split())
    if not name or name == source or name == label:
        return label
    return f"{name} ({label})"


def _item(raw: Dict[str, Any]) -> Optional[Dict[str, str]]:
    todo_id = str(raw.get("id") or "").strip()
    if not todo_id:
        return None
    status = str(raw.get("status") or "pending")
    item = {
        "id": todo_id,
        "content": str(raw.get("content") or todo_id),
        "status": status if status in _HERMES_STATUSES else "pending",
    }
    by = describe_actor(raw.get("by"))
    if by:
        item["by"] = by
    return item


def parse_view(payload: Any) -> SessionView:
    if not isinstance(payload, dict):
        raise KynverAgentOSError("Kynver todo session returned no list")
    items = [i for i in (_item(r) for r in payload.get("todos") or [] if isinstance(r, dict)) if i]
    errors = [
        {"id": str(e.get("id") or ""), "error": str(e.get("error") or "")}
        for e in payload.get("errors") or []
        if isinstance(e, dict)
    ]
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
    since: Dict[str, float] = {}
    for raw in payload.get("todos") or []:
        if isinstance(raw, dict) and raw.get("since") and raw.get("id"):
            try:
                since[str(raw["id"])] = datetime.fromisoformat(str(raw["since"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return SessionView(
        since=since,
        items=items,
        plan=plan,
        errors=errors,
        omitted=int(payload.get("omitted") or 0),
        adopted=bool(payload.get("adopted")),
    )


def session_call(
    client: KynverAgentOSClient,
    *,
    scope: str,
    todos: Optional[List[Dict[str, Any]]] = None,
    merge: bool = True,
    adopt: Optional[str] = None,
    label: Optional[str] = None,
    actor_source: str = "hermes-chat",
    task_id: Optional[str] = None,
) -> SessionView:
    """Read (``todos is None``) or write the scope's list; raises SessionEndpointMissing on an
    older Kynver and KynverAgentOSError (with ``status``) on any other failure."""
    session_key = session_key_for_scope(scope)
    body: Dict[str, Any] = {
        "sessionKey": session_key,
        "actor": {"source": actor_source, "name": label or None, "ref": session_key},
    }
    if label:
        body["label"] = label
    if task_id:
        body["hints"] = {"taskId": task_id}
    if adopt:
        body["adopt"] = adopt
    if todos is not None:
        body["todos"] = [
            {k: v for k, v in (("id", t.get("id")), ("content", t.get("content")), ("status", t.get("status"))) if v}
            for t in todos
            if isinstance(t, dict)
        ]
        body["merge"] = merge
    try:
        payload = client.post(SESSION_PATH, body)
    except KynverAgentOSError as exc:
        if exc.status in (404, 405):
            raise SessionEndpointMissing(str(exc)) from exc
        raise
    return parse_view(payload)


def is_client_error(exc: KynverAgentOSError) -> bool:
    """A request Kynver refused on its merits (bad id, ambiguous plan): report it, don't retry."""
    return exc.status is not None and 400 <= exc.status < 500 and exc.status not in (404, 405, 408, 429)
