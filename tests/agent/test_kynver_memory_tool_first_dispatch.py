import json
from types import SimpleNamespace


def test_invoke_tool_uses_memory_manager_first_for_memory_writes(monkeypatch):
    """The built-in memory tool dispatch must give Kynver-first providers first refusal."""
    from agent.agent_runtime_helpers import invoke_tool

    class Manager:
        def __init__(self):
            self.first_calls = []
            self.observed_calls = []

        def try_handle_memory_tool_first(self, args, metadata=None):
            self.first_calls.append((dict(args), dict(metadata or {})))
            return json.dumps(
                {
                    "success": True,
                    "provider": "kynver",
                    "memory_mirror": "primary",
                    "kynverMemoryPrimary": True,
                    "observer_metadata": [
                        {"provider": "kynver", "memory_mirror": "primary", "durable": True}
                    ],
                }
            )

        def on_tool_observed(self, tool_name, args, result, metadata=None):
            self.observed_calls.append((tool_name, dict(args), result, dict(metadata or {})))
            return []

    def fail_local_memory_tool(*args, **kwargs):  # pragma: no cover - only called on regression
        raise AssertionError("local memory_tool should not run after Kynver-first prehandle")

    import tools.memory_tool as memory_tool_module

    monkeypatch.setattr(memory_tool_module, "memory_tool", fail_local_memory_tool)

    manager = Manager()
    agent = SimpleNamespace(
        _memory_manager=manager,
        _memory_store=object(),
        session_id="session-1",
        _parent_session_id="parent-1",
        platform="cli",
    )
    from agent.background_review import build_memory_write_metadata

    agent._build_memory_write_metadata = lambda **kwargs: build_memory_write_metadata(agent, **kwargs)

    result = invoke_tool(
        agent,
        "memory",
        {
            "action": "add",
            "target": "memory",
            "content": "Kynver AgentOS memory writes are first-class.",
        },
        effective_task_id="task-1",
        tool_call_id="call-1",
        pre_tool_block_checked=True,
    )

    payload = json.loads(result)
    assert payload["kynverMemoryPrimary"] is True
    assert payload["observer_metadata"] == [
        {"provider": "kynver", "memory_mirror": "primary", "durable": True}
    ]
    assert len(manager.first_calls) == 1
    first_args, first_metadata = manager.first_calls[0]
    assert first_args == {
        "action": "add",
        "target": "memory",
        "content": "Kynver AgentOS memory writes are first-class.",
    }
    assert first_metadata == {
        "write_origin": "assistant_tool",
        "execution_context": "foreground",
        "task_id": "task-1",
        "session_id": "session-1",
        "parent_session_id": "parent-1",
        "platform": "cli",
        "tool_name": "memory",
        "tool_call_id": "call-1",
    }
    assert manager.observed_calls[0][0] == "memory"
