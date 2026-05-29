"""General Hermes plugin: Kynver AgentOS lifecycle hooks."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def _on_post_tool_call(tool_name: str, result: str, **kwargs) -> None:
    if tool_name != "skill_manage":
        return
    args = kwargs.get("args") or {}
    action = str(args.get("action") or "")
    try:
        from plugins.memory.kynver.agentos_bridge import KynverAgentOSClient
        from plugins.memory.kynver.skills_bridge import mirror_skill_manage_to_agentos
        from plugins.memory.kynver.substrate import substrate_active

        if not substrate_active():
            return
        mirror_skill_manage_to_agentos(
            KynverAgentOSClient(),
            action,
            args if isinstance(args, dict) else {},
            result_json=result if isinstance(result, str) else json.dumps(result),
        )
    except Exception:
        logger.debug("Kynver skill_manage mirror hook failed", exc_info=True)


def _on_session_end(messages, **kwargs) -> None:
    agent = kwargs.get("agent")
    if agent is None:
        return
    try:
        from plugins.memory.kynver.integration import on_session_boundary

        on_session_boundary(agent, messages)
    except Exception:
        logger.debug("Kynver on_session_end hook failed", exc_info=True)


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
