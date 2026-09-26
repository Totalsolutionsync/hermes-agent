"""Background work nudges the todo list — on the tool result only (cache-safe)."""

import json
from types import SimpleNamespace

from agent.todo_nudge import attach_hint, background_work_labels, todo_tracking_hint
from tools.todo_tool import TodoStore


def agent_with(todos=None, tools=("todo_list", "terminal", "delegate_task")):
    store = TodoStore()
    if todos:
        store.write(todos)
    return SimpleNamespace(_todo_store=store, valid_tool_names=set(tools), system_prompt="SYSTEM")


LAUNCH = "started worker-hermes-todo-firstclass pid=42 (guard attached); running workers now: 2\n" \
         "[background-work-started: worker-hermes-todo-firstclass]"


def test_worker_launch_marker_without_a_todo_item_gets_one_hint():
    agent = agent_with([{"id": "1", "content": "Review PR", "status": "in_progress"}])
    hint = todo_tracking_hint(agent, "terminal", {"command": "launch-worker.sh …"}, LAUNCH)
    assert hint and "worker-hermes-todo-firstclass" in hint and "in_progress" in hint
    # Once per piece of work.
    assert todo_tracking_hint(agent, "terminal", {"command": "launch-worker.sh …"}, LAUNCH) is None
    assert agent.system_prompt == "SYSTEM"


def test_tracked_worker_needs_no_hint():
    agent = agent_with([{"id": "w", "content": "Worker hermes-todo-firstclass: todo parity", "status": "in_progress"}])
    assert todo_tracking_hint(agent, "terminal", {}, LAUNCH) is None
    # A finished item does not count as tracking a newly launched worker.
    agent = agent_with([{"id": "w", "content": "Worker hermes-todo-firstclass", "status": "completed"}])
    assert todo_tracking_hint(agent, "terminal", {}, LAUNCH)


def test_background_delegation_and_notify_jobs_are_detected():
    delegate = {"background": True, "tasks": [{"goal": "Audit the Kynver plan UI for parallel rows"},
                                              {"goal": "Port the todo tests"}]}
    assert background_work_labels("delegate_task", delegate, "{}") == [
        "Audit the Kynver plan UI for parallel rows", "Port the todo tests"]
    assert background_work_labels("delegate_task", {"background": True, "action": "list"}, "") == []
    assert background_work_labels("delegate_task", {"goal": "sync work"}, "") == []  # foreground: finishes in-call
    assert background_work_labels("terminal", {"command": "pytest -q", "background": True, "notify_on_complete": True}, "") == ["pytest -q"]
    assert background_work_labels("terminal", {"command": "npm run dev", "background": True}, "") == []  # server


def test_hint_rides_on_json_results_as_a_key_and_on_text_as_a_line():
    hint = "[todo] track it"
    assert json.loads(attach_hint(json.dumps({"ok": True}), hint)) == {"ok": True, "todo_hint": hint}
    assert attach_hint("done", hint) == "done\n\n[todo] track it"
    assert attach_hint("done", None) == "done"


def test_no_hint_without_the_todo_tool():
    agent = agent_with(tools=("terminal",))
    assert todo_tracking_hint(agent, "terminal", {}, LAUNCH) is None


def test_hint_lands_on_the_tool_result_through_the_executor(tmp_path):
    """End to end through execute_tool_calls_sequential: the worker launch result carries
    the hint; the system prompt and message list shape are untouched (prompt cache)."""
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from agent.tool_executor import execute_tool_calls_sequential
    from run_agent import AIAgent

    defs = [{"type": "function", "function": {"name": name, "description": "t",
                                               "parameters": {"type": "object", "properties": {}}}}
            for name in ("terminal", "todo_list")]
    with (
        patch("model_tools.get_tool_definitions", return_value=defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("run_agent._hermes_home", Path(tmp_path)),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(api_key="k", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    agent._append_guardrail_observation = MagicMock(side_effect=lambda _n, _a, result, **_k: result)
    agent._record_file_mutation_result = MagicMock()
    agent._tool_result_content_for_active_model = MagicMock(side_effect=lambda _n, result: result)
    prompt_before = getattr(agent, "_cached_system_prompt", None)

    call = SimpleNamespace(id="c1", type="function", function=SimpleNamespace(
        name="terminal", arguments=json.dumps({"command": "~/.hermes/scripts/launch-worker.sh x /w b l"})))
    messages: list = []
    with patch("model_tools.handle_function_call", return_value=LAUNCH):
        execute_tool_calls_sequential(agent, SimpleNamespace(tool_calls=[call]), messages, "task")

    assert [m["role"] for m in messages] == ["tool"]
    assert messages[0]["content"].startswith(LAUNCH)
    assert "[todo] Background work started (worker-hermes-todo-firstclass)" in messages[0]["content"]
    assert getattr(agent, "_cached_system_prompt", None) == prompt_before
