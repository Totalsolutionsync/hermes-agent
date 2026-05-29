"""Wire Kynver operating substrate into Hermes agent lifecycle (minimal core surface)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from .agentos_bridge import KynverAgentOSClient, load_kynver_agentos_config
from .context_envelope import format_context_envelope_block, load_context_envelope
from .operating_context import load_operating_context
from .session_manager import KynverSessionManager
from .skills_bridge import format_agentos_skills_index, list_agentos_skill_manifest
from .substrate import allow_local_fallback, resolve_memory_provider_name, substrate_active
from .todo_store import KynverTodoStore

logger = logging.getLogger(__name__)


def configure_agent(
    agent: Any,
    agent_cfg: Mapping[str, Any],
    *,
    platform: Optional[str] = None,
    skip_memory: bool = False,
) -> None:
    """Attach Kynver session, todo store, and prompt substrate hooks to an AIAgent."""

    agent._kynver_active = False
    agent._kynver_degraded = False
    agent._kynver_context_block = ""
    agent._kynver_skills_block = ""
    agent._kynver_session_manager = None
    agent._kynver_client = None
    agent._kynver_operating_context = load_operating_context(config=agent_cfg)

    if not substrate_active(config=agent_cfg):
        return

    client = KynverAgentOSClient()
    agent._kynver_client = client
    agent._kynver_active = True

    session_mgr = KynverSessionManager(client)
    agent._kynver_session_manager = session_mgr

    channel = platform or getattr(agent, "platform", None) or "hermes"
    model = getattr(agent, "model", "") or ""
    session_mgr.open_session(
        channel=str(channel),
        model=str(model),
        hermes_session_id=getattr(agent, "session_id", "") or "",
        metadata={"source": "hermes-forge"},
    )

    envelope = load_context_envelope(client, agent._kynver_operating_context)
    agent._kynver_context_block = format_context_envelope_block(envelope)

    manifest = list_agentos_skill_manifest(client)
    agent._kynver_skills_block = format_agentos_skills_index(manifest)

    fallback_ok = allow_local_fallback(agent_cfg)
    agent._todo_store = KynverTodoStore(
        client,
        operating_context=agent._kynver_operating_context,
        allow_fallback=fallback_ok,
        hermes_session_id=getattr(agent, "session_id", "") or "",
    )
    agent._kynver_degraded = bool(getattr(agent._todo_store, "degraded", False))

    if not skip_memory:
        mem_cfg = agent_cfg.get("memory") if isinstance(agent_cfg.get("memory"), dict) else {}
        resolved = resolve_memory_provider_name(mem_cfg, full_config=agent_cfg)
        if resolved == "kynver":
            agent._kynver_resolved_memory_provider = "kynver"


def resolve_memory_provider_for_init(
    mem_config: Mapping[str, Any],
    agent_cfg: Mapping[str, Any],
) -> str:
    return resolve_memory_provider_name(mem_config, full_config=agent_cfg)


def on_conversation_start(agent: Any, user_message: str = "") -> None:
    if not getattr(agent, "_kynver_active", False):
        return
    mgr = getattr(agent, "_kynver_session_manager", None)
    if mgr and not mgr.agentos_session_id:
        mgr.open_session(
            channel=str(getattr(agent, "platform", None) or "hermes"),
            model=str(getattr(agent, "model", "") or ""),
            hermes_session_id=getattr(agent, "session_id", "") or "",
        )
    if user_message and mgr:
        preview = (user_message[:120] + "…") if len(user_message) > 120 else user_message
        mgr.log_event(f"Turn started: {preview}", event_type="topic")


def on_session_boundary(agent: Any, messages: Optional[List[Dict[str, Any]]] = None) -> None:
    mgr = getattr(agent, "_kynver_session_manager", None)
    if not mgr or not mgr.agentos_session_id:
        return
    summary = "Hermes session boundary"
    if messages:
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                content = msg["content"]
                if isinstance(content, str):
                    summary = content[:500]
                    break
    mgr.close_session(summary)


def get_prompt_blocks(agent: Any) -> List[str]:
    blocks: List[str] = []
    ctx_block = getattr(agent, "_kynver_context_block", "") or ""
    if ctx_block:
        blocks.append(ctx_block)
    skills_block = getattr(agent, "_kynver_skills_block", "") or ""
    if skills_block:
        blocks.append(skills_block)
    if getattr(agent, "_kynver_degraded", False):
        blocks.append(
            "[Kynver: degraded mode — some operating state is using local Hermes fallback]"
        )
    return blocks


def memory_provider_init_kwargs(agent: Any) -> Dict[str, Any]:
    mgr = getattr(agent, "_kynver_session_manager", None)
    if mgr and mgr.agentos_session_id:
        return {"agentos_session_id": mgr.agentos_session_id}
    return {}
