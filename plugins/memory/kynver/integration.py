"""Wire Kynver plan-progress todo store and operating prompt blocks into Hermes."""

from __future__ import annotations

import json
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


def scope_for_session_id(agent: Any, session_id: str) -> Optional[str]:
    """Todo scope of any session id (its compression-lineage root), e.g. the chat that launched
    a worker, so a cron can pick up that chat's list with ``plan="session:<id>"``."""
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    lineage_of = getattr(getattr(agent, "_session_db", None), "get_compression_lineage", None)
    root = session_id
    if callable(lineage_of):
        try:
            lineage = list(lineage_of(session_id) or [])
        except Exception as exc:
            logger.debug("Kynver todo scope: lineage lookup failed for %s: %s", session_id, exc)
            lineage = []
        if lineage:
            root = str(lineage[0])
    return normalize_todo_scope(root)


_PLATFORM_LABELS = {"telegram": "Telegram", "discord": "Discord", "slack": "Slack", "cli": "CLI",
                    "cron": "Hermes cron", "whatsapp": "WhatsApp", "signal": "Signal"}
# Surfaces without a person or chat to name: the bare platform is the whole label.
_UNNAMED_PLATFORMS = frozenset({"", "cli", "cron", "hermes", "local"})


def _clean_name(value: Any) -> str:
    return " ".join(str(value).split())[:80] if value else ""


def _stored_chat_name(agent: Any, session_id: str) -> str:
    """The chat/user name the gateway stored on the session row (``display_name``, else the
    origin's ``chat_name`` / ``user_name``), for agents built without the live source's name."""
    get_session = getattr(getattr(agent, "_session_db", None), "get_session", None)
    if not callable(get_session):
        return ""
    try:
        row = get_session(session_id) or {}
    except Exception as exc:
        logger.debug("Kynver todo label: session lookup failed for %s: %s", session_id, exc)
        return ""
    name = _clean_name(row.get("display_name"))
    if name:
        return name
    try:
        origin = json.loads(row.get("origin_json") or "null") or {}
    except (TypeError, ValueError):
        origin = {}
    if isinstance(origin, dict):
        return _clean_name(origin.get("chat_name") or origin.get("user_name"))
    return ""


def _session_title(agent: Any, session_id: str) -> str:
    title_of = getattr(getattr(agent, "_session_db", None), "get_session_title", None)
    if not callable(title_of):
        return ""
    try:
        return _clean_name(title_of(session_id))
    except Exception:
        return ""


def todo_session_label(agent: Any) -> Optional[str]:
    """How this chat's rows are labelled in Kynver: "Telegram · Will H", "Hermes cron · <title>".

    The name comes from the live source, else the session row the gateway stored, else the
    session title. A chat platform whose name is still unknown sends no label (Kynver keeps the
    list's existing name) — never a bare "Telegram" that would rename "Telegram · Will H"."""
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    where = _PLATFORM_LABELS.get(platform, platform.title() if platform else "Hermes")
    who = _clean_name(getattr(agent, "_chat_name", None) or getattr(agent, "_user_name", None))
    session_id = str(getattr(agent, "session_id", "") or "").strip()
    if not who and session_id:
        cached = getattr(agent, "_kynver_todo_label_name", None)
        if cached and cached[0] == session_id:
            who = cached[1]
        else:
            who = _stored_chat_name(agent, session_id) or _session_title(agent, session_id)
            if who:  # only a hit is cached: the row may gain its name later in the turn
                agent._kynver_todo_label_name = (session_id, who)
    if who:
        return f"{where} · {who}"
    return where if platform in _UNNAMED_PLATFORMS else None


def todo_actor_source(agent: Any) -> str:
    """``hermes-cron`` for scheduled runs (worker reviews tick items they did not create)."""
    return "hermes-cron" if str(getattr(agent, "platform", "") or "").lower() == "cron" else "hermes-chat"


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
        label=lambda: todo_session_label(agent),
        actor_source=todo_actor_source(agent),
        scope_for_session=lambda session_id: scope_for_session_id(agent, session_id),
    )
    agent._todo_store_provider = "kynver"
    agent._kynver_degraded = bool(getattr(agent._todo_store, "degraded", False))

    logger.info(
        "Kynver todo store active (plan: %s; one /todos/session call per read or write)",
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
            "[Kynver: your todo list is a live view of shared Kynver plan rows — Will, other "
            "chats, crons and Kynver agents may tick items too (shown as 'by …'). Several items "
            "may be in progress at once. 1–2 item lists stay in the Inbox; 3+ get their own plan "
            "automatically. Mark dropped items cancelled (✗), not completed (✓)]"
        )
    return blocks
