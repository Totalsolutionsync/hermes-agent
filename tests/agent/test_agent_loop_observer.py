"""Agent-loop tool observation: tools that bypass registry dispatch still reach providers."""

import json
from types import SimpleNamespace


def test_agent_loop_observer_can_annotate_todo_result():
    from agent.agent_loop_observer import append_observer_metadata, notify_agent_loop_tool

    class Manager:
        def on_tool_observed(self, tool_name, args, result, metadata=None):
            assert tool_name == "todo_list"
            assert args == {"merge": True}
            assert metadata["session_id"] == "sess-1"
            return [{"provider": "test", "todo_mirror": "synced"}]

    agent = SimpleNamespace(
        _memory_manager=Manager(),
        session_id="sess-1",
        _parent_session_id="",
        platform="cli",
    )
    result = json.dumps({"todos": []})

    annotations = notify_agent_loop_tool(agent, "todo_list", {"merge": True}, result, task_id="task-1")
    annotated = json.loads(append_observer_metadata(result, annotations))

    assert annotated["observer_metadata"] == [{"provider": "test", "todo_mirror": "synced"}]


def test_agent_loop_observer_marks_kynver_plan_progress_todo_store():
    from agent.agent_loop_observer import notify_agent_loop_tool

    seen = []

    class Manager:
        def on_tool_observed(self, tool_name, args, result, metadata=None):
            seen.append(metadata)
            return []

    class KynverTodoStore:
        pass

    agent = SimpleNamespace(
        _memory_manager=Manager(),
        _todo_store=KynverTodoStore(),
        session_id="sess-1",
        _parent_session_id="",
        platform="cli",
    )

    notify_agent_loop_tool(agent, "todo_list", {"merge": True}, json.dumps({"todos": []}))

    assert seen[0]["todo_store_provider"] == "kynver_plan_progress"


def test_agent_loop_observer_covers_memory_delegate_and_session_search():
    from agent.agent_loop_observer import notify_agent_loop_tool

    seen = []

    class Manager:
        def on_tool_observed(self, tool_name, args, result, metadata=None):
            seen.append((tool_name, args, result, metadata["session_id"], metadata["tool_name"]))
            return []

    agent = SimpleNamespace(
        _memory_manager=Manager(),
        session_id="sess-1",
        _parent_session_id="parent-1",
        platform="cli",
        _build_memory_write_metadata=lambda **kw: {"tool_name": "memory", "write_origin": "assistant_tool"},
    )

    for tool_name in ("memory", "delegate_task", "session_search"):
        notify_agent_loop_tool(agent, tool_name, {"q": tool_name}, "ok", task_id="task-1")

    assert [item[0] for item in seen] == ["memory", "delegate_task", "session_search"]
    assert all(item[3] == "sess-1" for item in seen)
    # Provenance metadata must not relabel non-memory tools as "memory".
    assert [item[4] for item in seen] == ["memory", "delegate_task", "session_search"]


def test_inline_todo_executor_reaches_memory_manager_observer():
    """The real inline executor table (shared by both tool paths) runs the observer."""
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext, resolve_invoke_tool_executor
    from tools.todo_tool import TodoStore

    class Manager:
        def __init__(self):
            self.seen = []

        def on_tool_observed(self, tool_name, args, result, metadata=None):
            self.seen.append((tool_name, metadata["tool_call_id"]))
            return [{"provider": "test", "todo_mirror": "synced"}]

        def has_tool(self, name):
            return False

    manager = Manager()
    agent = SimpleNamespace(
        _memory_manager=manager, _todo_store=TodoStore(),
        session_id="sess-1", _parent_session_id="", platform="cli",
    )
    ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")
    args = {"todos": [{"id": "a", "content": "ship it", "status": "pending"}]}

    for executor in (INLINE_TOOL_EXECUTORS["todo_list"], resolve_invoke_tool_executor(agent, "todo_list")):
        payload = json.loads(executor(agent, args, ctx))
        assert payload["todos"][0]["id"] == "a"
        assert payload["observer_metadata"] == [{"provider": "test", "todo_mirror": "synced"}]

    assert manager.seen == [("todo_list", "call-1"), ("todo_list", "call-1")]
