"""Hermes plugin hooks — pre-transition guards for Kynver todo writes.

Projection and read-back live on :class:`KynverTodoStore`; hooks only block
illegal focus transitions before the built-in ``todo_list`` tool runs.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .agentos_bridge import (
    KynverAgentOSClient,
    KynverAgentOSError,
    load_kynver_agentos_config,
)
from .operating_config import kynver_operating_tools_enabled, load_operating_linkage
from .plan_progress import inspect_todo_write
from .integration import known_session_scope
from .plan_binding import shared_plan_resolver
from .pre_transition import TODO_TOOL_NAMES

logger = logging.getLogger(__name__)


def _client() -> KynverAgentOSClient | None:
    if not kynver_operating_tools_enabled():
        return None
    cfg = load_kynver_agentos_config()
    if not cfg.enabled:
        return None
    return KynverAgentOSClient(cfg)


def on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    session_id: str = "",
    **_: Any,
) -> Optional[dict[str, Any]]:
    if tool_name not in TODO_TOOL_NAMES:
        return None
    if not isinstance(args, dict):
        return None
    todos = args.get("todos")
    if todos is None:
        return None

    client = _client()
    if not client:
        return None

    scope = known_session_scope(session_id)
    linkage = load_operating_linkage()
    if scope:
        try:
            # Inspect the plan this session's todos actually land on.
            linkage = shared_plan_resolver(client, linkage).linkage_for(scope)
        except KynverAgentOSError as exc:
            # Fail open: the todo store re-checks transitions before projecting.
            logger.debug("Kynver todo guard: plan resolution failed for %s: %s", scope, exc)
            return None
    blocked = inspect_todo_write(
        client,
        linkage,
        list(todos),
        merge=bool(args.get("merge")),
        scope=scope,
    )
    if blocked:
        return {
            "action": "block",
            "message": blocked,
        }
    return None


def on_transform_tool_result(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> Optional[str]:
    """No-op — KynverTodoStore reconciles on read/write."""

    return None


def register_operating_hooks(ctx) -> None:
    """No hooks: Kynver enforces todo transitions inside the one ``/todos/session`` call, so a
    pre-call guard would only add Kynver round trips (it used to cost 1–2 per write).
    ``on_pre_tool_call`` stays importable for an older Kynver's legacy projection path."""
    return None
