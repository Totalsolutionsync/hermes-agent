"""Offline stateful regressions: plan history must not become session todos."""
import json
from copy import deepcopy

import pytest

from plugins.memory.kynver.operating_config import OperatingLinkage
from plugins.memory.kynver.plan_progress import reconcile_todos_from_kynver
from plugins.memory.kynver.pre_transition import PreTransitionError
from plugins.memory.kynver.todo_store import KynverTodoStore
from tools.todo_tool import todo_tool
from tests.plugins.memory.test_kynver_todo_store import PlanProgressFakeClient


@pytest.fixture
def historical_store():
    client = PlanProgressFakeClient()
    for i in range(1000):
        key = f"hermes-todo:history-{i}"
        client.rows[key] = {"rowKey": key, "title": "Historical unrelated task " + "x" * 200,
                            "status": "partial", "taskId": "old-task"}
    linkage = OperatingLinkage(plan_id="plan-1", task_id="current-task",
                               session_id="current-session", executor_ref="hermes:forge")
    return client, linkage, KynverTodoStore(client, linkage=linkage)


def test_replace_merge_read_preserve_history_and_bound_result(historical_store):
    client, linkage, store = historical_store
    history = deepcopy(client.rows)
    assert len(json.dumps(history).encode()) > 111_000
    assert store.read() == []
    assert not store.has_items()
    todos = [{"id": str(i), "content": f"Current step {i}", "status": "pending"}
             for i in range(3)]
    result = todo_tool(todos=todos, store=store)
    assert json.loads(result)["todos"] == todos
    assert len(result.encode()) < 1000
    assert store.read() == todos
    assert "Historical" not in store.format_for_injection()
    assert store._linkage is linkage
    assert store._linkage.session_id == "current-session"

    # Another session's authoritative update must survive a partial merge.
    client.rows["hermes-todo:0"].update(title="Remote corrected title", status="done")
    result = json.loads(todo_tool(todos=[{"id": "0", "status": "cancelled"}],
                                  merge=True, store=store))
    assert result["summary"]["total"] == 3
    assert result["todos"][0] == {"id": "0", "content": "Remote corrected title",
                                    "status": "cancelled"}
    assert client.rows["hermes-todo:0"]["title"] == "Remote corrected title"
    todo_tool(todos=[{"id": "new", "content": "New step", "status": "pending"}],
              merge=True, store=store)
    assert [item["id"] for item in store.read()] == ["0", "1", "2", "new"]

    assert store.write([todos[2]]) == [todos[2]]
    assert store.read() == [todos[2]]
    assert store.write([]) == []
    assert store.read() == []
    assert store.format_for_injection() is None
    assert all(client.rows[key] == row for key, row in history.items())
    assert "hermes-todo:0" in client.rows  # replace is not remote deletion
    assert {call[0] for call in client.calls} == {"GET", "POST"}
    # Explicit plan-wide reconciliation remains available for cross-session context.
    assert len(reconcile_todos_from_kynver(client, linkage, [])) == 1004


def test_external_focus_and_status_are_authoritative_without_import(historical_store):
    client, _, store = historical_store
    store.write([{"id": "current", "content": "Current", "status": "in_progress"}])
    client.focus_key = "hermes-todo:history-0"
    assert store.read() == [{"id": "current", "content": "Current", "status": "pending"}]
    client.rows["hermes-todo:current"].update(title="Verified elsewhere", status="done")
    assert store.read() == [{"id": "current", "content": "Verified elsewhere", "status": "completed"}]
    assert store._local.read() == store.read()


def test_blocked_replace_keeps_session_membership(historical_store):
    client, _, store = historical_store
    original = [{"id": "current", "content": "Current", "status": "pending"}]
    store.write(original)
    client.rows["hermes-todo:leased"] = {"rowKey": "hermes-todo:leased", "status": "running"}
    before = deepcopy(client.rows)
    with pytest.raises(PreTransitionError):
        store.write([{"id": "leased", "content": "No", "status": "in_progress"}])
    assert store.read() == original
    assert client.rows == before


def test_fallback_keeps_only_session_items(historical_store):
    client, _, store = historical_store
    todos = [{"id": "current", "content": "Current", "status": "pending"}]
    store.write(todos)

    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    client.get = fail
    assert store.read() == todos
    assert store.degraded
    assert store.write([{"id": "current", "status": "completed"}], merge=True)[0]["content"] == "Current"
