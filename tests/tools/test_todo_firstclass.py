"""todo_list as the agent's working memory: parallel in-progress work, stale-work hint,
changes by others, and a schema that stays short (it rides on every call)."""

import json

from tools.todo_tool import (
    STALE_IN_PROGRESS_SECONDS,
    TODO_SCHEMA,
    TodoStore,
    format_checklist,
    stale_in_progress_hint,
    todo_tool,
)


def test_schema_describes_parallel_tracking_and_stays_short():
    desc = TODO_SCHEMA["description"]
    assert "Only ONE" not in desc
    for phrase in ("background workers", "any number can be in progress", "Needs <user>",
                   "same turn", "verifying", "replacement"):
        assert phrase in desc
    assert len(desc) < 700
    assert "plan" in TODO_SCHEMA["parameters"]["properties"]


def test_several_items_can_be_in_progress():
    store = TodoStore()
    out = json.loads(todo_tool(store=store, todos=[
        {"id": "w1", "content": "Worker one", "status": "in_progress"},
        {"id": "w2", "content": "Worker two", "status": "in_progress"},
        {"id": "w3", "content": "Worker three", "status": "in_progress"},
    ]))
    assert out["summary"]["in_progress"] == 3
    assert out["checklist"].splitlines() == ["▶ Worker one", "▶ Worker two", "▶ Worker three"]


def test_stale_in_progress_items_get_a_hint(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr("tools.todo_tool.time.time", lambda: clock[0])
    store = TodoStore()
    todo_tool(store=store, todos=[{"id": "w", "content": "Worker review", "status": "in_progress"},
                                  {"id": "p", "content": "Later", "status": "pending"}])
    assert "hint" not in json.loads(todo_tool(store=store))

    clock[0] += STALE_IN_PROGRESS_SECONDS + 3600
    out = json.loads(todo_tool(store=store))
    assert out["hint"].startswith("In progress with no update: 'Worker review' (3h)")

    # Re-marking it in progress does not reset the clock; finishing it clears the hint.
    out = json.loads(todo_tool(store=store, todos=[{"id": "w", "status": "completed"}], merge=True))
    assert "hint" not in out


def test_stale_hint_lists_at_most_three():
    items = [{"id": str(i), "content": f"W{i}", "status": "in_progress"} for i in range(5)]
    hint = stale_in_progress_hint(items, {str(i): 0.0 for i in range(5)}, now=STALE_IN_PROGRESS_SECONDS * 2)
    assert "and 2 more" in hint


def test_checklist_names_who_changed_an_item():
    lines = format_checklist([
        {"id": "a", "content": "Ship", "status": "completed", "by": "Kynver UI"},
        {"id": "b", "content": "Old", "status": "cancelled", "by": "Hermes cron"},
    ]).splitlines()
    assert lines == ["✓ Ship — by Kynver UI", "✗ Old (dropped) — by Hermes cron"]


def test_plan_needs_a_shared_store():
    out = json.loads(todo_tool(store=TodoStore(), plan="Kynver todo parity"))
    assert "Kynver" in out["error"]
