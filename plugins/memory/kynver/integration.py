"""Wire Kynver plan-progress todo store and operating prompt blocks into Hermes."""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, List, Mapping, Optional

from agent.operating_prompt import register_operating_prompt_hook

from .agentos_bridge import KynverAgentOSClient
from .operating_config import load_operating_linkage
from .plan_binding import plan_binding_enabled, shared_plan_resolver
from .pre_transition import normalize_todo_scope
from .substrate import allow_local_fallback, substrate_active
from .todo_store import KynverTodoStore

logger = logging.getLogger(__name__)

_PROMPT_HOOK_REGISTERED = False

# session id -> resolved todo scope, for the pre_tool_call guard, which only receives the
# session id. Session ids are globally unique, so this is not profile state.
_SESSION_SCOPES: "OrderedDict[str, str]" = OrderedDict()
_SESSION_SCOPES_MAX = 512


def session_todo_scope(agent: Any) -> Optional[str]:
    """Todo scope for the agent's current session: the root of its compression lineage.

    Compression mints a child session id and rotates ``agent.session_id`` in place (and the
    gateway may build a fresh agent on the child), but it is the same conversation, so the
    todo list must continue. Branches, delegates and /new are separate lineages
    (``get_compression_lineage`` excludes forks) and get their own list."""

    session_id = str(getattr(agent, "session_id", "") or "").strip()
    if not session_id:
        return None
    cached = getattr(agent, "_kynver_todo_scope", None)
    if cached and cached[0] == session_id:
        return cached[1]
    root = session_id
    lineage_of = getattr(getattr(agent, "_session_db", None), "get_compression_lineage", None)
    lineage: list = []
    if callable(lineage_of):
        try:
            lineage = list(lineage_of(session_id) or [])
        except Exception as exc:
            logger.debug("Kynver todo scope: lineage lookup failed for %s: %s", session_id, exc)
    if lineage:
        root = str(lineage[0])
    scope = normalize_todo_scope(root)
    # An empty lineage means the session row is not written yet; resolve again next time.
    if lineage:
        agent._kynver_todo_scope = (session_id, scope)
    _SESSION_SCOPES[session_id] = scope
    _SESSION_SCOPES.move_to_end(session_id)
    while len(_SESSION_SCOPES) > _SESSION_SCOPES_MAX:
        _SESSION_SCOPES.popitem(last=False)
    return scope


def known_session_scope(session_id: str) -> Optional[str]:
    """Scope last resolved for ``session_id``; the id itself before any resolution."""
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    return _SESSION_SCOPES.get(session_id) or normalize_todo_scope(session_id)


def _ensure_prompt_hook() -> None:
    global _PROMPT_HOOK_REGISTERED
    if _PROMPT_HOOK_REGISTERED:
        return
    register_operating_prompt_hook(get_prompt_blocks)
    _PROMPT_HOOK_REGISTERED = True


def configure_agent(
    agent: Any,
    agent_cfg: Mapping[str, Any],
    *,
    platform: Optional[str] = None,
) -> None:
    """Replace the default local todo store when Kynver substrate is active."""

    _ensure_prompt_hook()

    agent._kynver_active = False
    agent._kynver_degraded = False

    if not substrate_active(config=agent_cfg):
        return

    client = KynverAgentOSClient()
    linkage = load_operating_linkage()
    fallback_ok = allow_local_fallback(agent_cfg)

    agent._kynver_client = client
    agent._kynver_active = True
    agent._todo_store = KynverTodoStore(
        client,
        linkage=linkage,
        allow_fallback=fallback_ok,
        scope=lambda: session_todo_scope(agent),
        plan_resolver=shared_plan_resolver(client, linkage),
    )
    agent._todo_store_provider = "kynver"
    agent._kynver_degraded = bool(getattr(agent._todo_store, "degraded", False))

    logger.info(
        "Kynver todo store active (plan: %s; in_progress uses progress-focus, not running)",
        "per-session binding" + (f", legacy fallback {linkage.plan_id}" if linkage.plan_id else "")
        if plan_binding_enabled()
        else f"fixed {linkage.plan_id or '(none)'}",
    )


def get_prompt_blocks(agent: Any) -> List[str]:
    blocks: List[str] = []
    if getattr(agent, "_kynver_degraded", False):
        blocks.append(
            "[Kynver: degraded mode — todo/current-focus may use local Hermes fallback]"
        )
    provider = getattr(agent, "_todo_store_provider", "local")
    if provider == "kynver" and getattr(agent, "_kynver_active", False):
        blocks.append(
            "[Kynver: session todos sync to AgentOS plan progress; "
            "in_progress is current focus, not harness running lease. "
            "1–2 step lists stay in the Inbox; a list of 3+ gets its own plan automatically — "
            "never link plans by hand. Mark dropped items cancelled (✗), not completed (✓)]"
        )
    return blocks
