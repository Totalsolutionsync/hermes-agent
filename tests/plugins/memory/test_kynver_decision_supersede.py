"""Kynver flags todos a newer decision overrides; Hermes shows the flag and can ask first.

The 2026-10-18 Rufford Ford case: a 17 Oct todo said "schedule Rufford Ford v1 Sun"; the owner
later decided "hold Rufford, re-cut, post Mon". Kynver now returns that todo with
``superseded: "⚠ superseded by decision <key>: <line>"``. These tests pin that the flag survives
the session view, reaches the todo tool's checklist and summary, and the post-compression
injection, and that ``kynver_memory_decision_check`` asks Kynver's decision check route.
"""

from __future__ import annotations

import json
from typing import Any, List, Tuple

from plugins.memory.kynver.todo_session import parse_view
from tools.todo_tool import TodoStore, _todo_payload, format_checklist

FLAG = "⚠ superseded by decision rufford-hold: Rufford: hold, re-cut, post Mon."


def _view():
    return parse_view({
        "plan": {"id": "p1", "title": "Inbox", "inbox": True},
        "todos": [
            {"id": "rufford", "content": "schedule Rufford Ford v1 Sun", "status": "pending", "superseded": FLAG},
            {"id": "frame", "content": "render Frame v5", "status": "pending"},
            {"id": "old", "content": "old Rufford post", "status": "completed", "superseded": FLAG},
        ],
        "summary": {"total": 3, "superseded": 1},
    })


def test_session_view_keeps_the_flag_only_where_kynver_sent_it():
    items = _view().items
    assert items[0]["superseded"] == FLAG
    assert "superseded" not in items[1]


def test_checklist_and_summary_mark_superseded_items_not_actionable():
    items = _view().items
    lines = format_checklist(items).splitlines()
    assert lines[0] == f"○ schedule Rufford Ford v1 Sun — {FLAG} (do not act on it)"
    assert lines[1] == "○ render Frame v5"
    assert "superseded" not in lines[2]  # finished items carry no flag
    payload = json.loads(_todo_payload(items, 1))
    assert payload["summary"]["superseded"] == 1


def test_nothing_extra_when_no_item_is_flagged():
    items = [{"id": "frame", "content": "render Frame v5", "status": "pending"}]
    payload = json.loads(_todo_payload(items, 1))
    assert "superseded" not in payload["summary"]
    assert "superseded" not in payload["checklist"]


def test_injection_after_compression_carries_the_flag():
    store = TodoStore()
    store._items = _view().items  # noqa: SLF001 — same path KynverTodoStore.format_for_injection uses
    text = store.format_for_injection() or ""
    assert f"schedule Rufford Ford v1 Sun (pending) — {FLAG} (do not act on it)" in text


class _StubClient:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any]] = []

    def get(self, path: str, *, slug: Any = None, timeout: Any = None) -> Any:
        self.calls.append((path, timeout))
        return {"entity": "Rufford Ford", "decisions": [{"key": "rufford-hold", "line": "Rufford: hold, re-cut, post Mon."}]}


def test_decision_check_tool_asks_kynver():
    from plugins.memory import load_memory_provider

    discovered = load_memory_provider("kynver", register_skills=False)
    assert discovered is not None
    client = _StubClient()
    provider = type(discovered)(client=client)
    assert "kynver_memory_decision_check" in {s["name"] for s in provider.get_tool_schemas()}
    out = json.loads(provider.handle_tool_call("kynver_memory_decision_check", {"entity": "Rufford Ford", "limit": 2}))
    assert out["success"] is True and out["decisions"][0]["key"] == "rufford-hold"
    assert client.calls[0][0] == "/memory/decisions/check?entity=Rufford+Ford&limit=2"
    missing = json.loads(provider.handle_tool_call("kynver_memory_decision_check", {}))
    assert "entity is required" in json.dumps(missing)
