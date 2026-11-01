"""Pluggable session todo store initialization.

Hermes core always starts with the in-memory :class:`tools.todo_tool.TodoStore`.
Memory plugins (e.g. Kynver AgentOS) may replace ``agent._todo_store`` during
init when operating substrate is active and healthy.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


def init_todo_store(
    agent: Any,
    agent_cfg: Mapping[str, Any],
    *,
    platform: Optional[str] = None,
) -> None:
    """Attach the session todo store. Plugins may replace the default local store."""

    from agent.safe_worker_policy import safe_worker_enabled
    from tools.todo_tool import TodoStore

    agent._todo_store = TodoStore()
    agent._todo_store_provider = "local"
    agent._kynver_active = False
    agent._kynver_degraded = False
    # A safe worker loads no plugin code, including this memory plugin's todo hook.
    if safe_worker_enabled():
        return

    try:
        from plugins.memory.kynver.integration import configure_agent

        configure_agent(agent, agent_cfg, platform=platform)
    except Exception as exc:
        logger.debug("Kynver todo store configuration skipped: %s", exc)
