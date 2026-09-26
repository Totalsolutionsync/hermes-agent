"""Todo rules for Hermes ↔ Kynver AgentOS plans.

a. done and dropped are distinct end to end (cancelled ↔ Kynver ``blocked``, ✓ vs ✗);
b. 1–2 item lists stay in the Inbox, a list reaching 3 items moves to its own plan;
c. binding is automatic — there is no manual link call;
d. a read is the session's small working set, never other sessions' or superseded rows.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from agent.display import get_cute_tool_message
from plugins.memory.kynver import plan_binding
from plugins.memory.kynver.agentos_bridge import KynverAgentOSError
from plugins.memory.kynver.operating_config import OperatingLinkage
from plugins.memory.kynver.plan_binding import TodoPlanResolver
from plugins.memory.kynver.todo_store import KynverTodoStore
from tools.todo_tool import MAX_TODO_OUTPUT_CHARS, TodoStore, todo_tool


class Kynver:
    """AgentOS fake with Kynver's binding + promote semantics (todo-plan-binding.ts,
    todo-session-plan.ts) and ``?rowKeyPrefix=`` narrowing of GET progress-rows."""

    def __init__(self, *, supports_promote: bool = True):
        self.supports_promote = supports_promote
        self.bindings: dict[str, tuple[str, str]] = {}  # session key -> (plan, source)
        self.rows: dict[str, dict[str, dict]] = {}  # plan -> rowKey -> row
        self.row_gets: list[str] = []
        self.promote_posts: list[dict] = []
        self.promote_error: Exception | None = None
        self.config = type("C", (), {"enabled": True, "api_url": "https://k", "slug": "ghost"})()

    def bind_task(self, session_key: str, plan: str) -> None:
        self.bindings[session_key] = (plan, "task")

    def plan_rows(self, plan: str) -> dict[str, dict]:
        return self.rows.get(plan, {})

    def get(self, path, **kwargs):
        path, _, query = path.partition("?")
        plan = path.split("/plans/")[1].split("/")[0]
        if path.endswith("/progress-rows"):
            self.row_gets.append(query)
            prefix = (parse_qs(query).get("rowKeyPrefix") or [""])[0]
            return {"items": [dict(r) for k, r in self.plan_rows(plan).items() if k.startswith(prefix)]}
        return {"plan": {"id": plan}}

    def post(self, path, body, **kwargs):
        if path == plan_binding.BINDING_PATH:
            key = body["sessionKey"]
            plan, source = self.bindings.setdefault(key, ("inbox", "inbox"))
            if body.get("promote") is not None:
                self.promote_posts.append(body)
                if self.promote_error:
                    raise self.promote_error
                if self.supports_promote and source == "inbox":
                    new_plan = f"plan-{key}"
                    prefix = f"hermes-todo:{key.split(':', 1)[1]}:"
                    inbox = self.rows.setdefault("inbox", {})
                    for row_key in [k for k in inbox if k.startswith(prefix)]:
                        self.rows.setdefault(new_plan, {})[row_key] = inbox.pop(row_key)
                    plan, source = self.bindings[key] = (new_plan, "promoted")
            return {"planId": plan, "planTitle": plan, "source": source, "bound": True}
        plan = path.split("/plans/")[1].split("/")[0]
        if path.endswith("/progress-rows"):
            for row in body["rows"]:
                self.rows.setdefault(plan, {})[row["rowKey"]] = dict(row)
        return {"ok": True}


def linkage() -> OperatingLinkage:
    return OperatingLinkage(plan_id="legacy-plan", task_id=None, session_id=None, executor_ref="hermes:forge")


def store(kynver: Kynver, scope: str) -> KynverTodoStore:
    return KynverTodoStore(kynver, linkage=linkage(), scope=scope, plan_resolver=TodoPlanResolver(kynver, linkage()))


def items(*specs: tuple[str, str]) -> list[dict]:
    return [{"id": i, "content": f"step {i}", "status": status} for i, status in specs]


def statuses(todos) -> dict[str, str]:
    return {t["id"]: t["status"] for t in todos}


# --- b/c: Inbox for one-offs, own plan for 3+, never a manual link -------------------------


def test_small_list_stays_in_inbox_and_bigger_session_gets_its_own_plan():
    kynver = Kynver()
    a, b = store(kynver, "sess-a"), store(kynver, "sess-b")

    a.write(items(("1", "pending"), ("2", "pending")))
    b.write(items(("1", "in_progress"), ("2", "pending"), ("3", "pending"), ("4", "pending")))

    assert a.plan_for_current_scope() == "inbox"
    assert set(kynver.plan_rows("inbox")) == {"hermes-todo:sess-a:1", "hermes-todo:sess-a:2"}
    assert b.plan_for_current_scope() == "plan-hermes:sess-b"
    assert len(kynver.plan_rows("plan-hermes:sess-b")) == 4
    assert kynver.promote_posts[0]["promote"] == {"title": "step 1"}


def test_inbox_list_growing_to_three_moves_with_its_rows():
    kynver = Kynver()
    a = store(kynver, "sess-a")
    a.write(items(("1", "completed"), ("2", "pending")))
    assert kynver.promote_posts == []

    a.write(items(("3", "pending")), merge=True)

    assert a.plan_for_current_scope() == "plan-hermes:sess-a"
    assert kynver.plan_rows("inbox") == {}
    assert set(kynver.plan_rows("plan-hermes:sess-a")) == {f"hermes-todo:sess-a:{i}" for i in "123"}
    assert statuses(a.read()) == {"1": "completed", "2": "pending", "3": "pending"}
    # Promoted once; later writes stay on the session plan.
    a.write(items(("4", "pending")), merge=True)
    assert len(kynver.promote_posts) == 1


def test_a_task_bound_session_never_leaves_its_plan():
    kynver = Kynver()
    kynver.bind_task("hermes:sess-t", "task-plan")
    t = store(kynver, "sess-t")
    t.write(items(("1", "pending"), ("2", "pending"), ("3", "pending")))
    assert t.plan_for_current_scope() == "task-plan"
    assert kynver.promote_posts == []


def test_kynver_without_promote_is_asked_once_and_failures_keep_the_list_in_the_inbox():
    old = Kynver(supports_promote=False)
    s = store(old, "sess-a")
    s.write(items(("1", "pending"), ("2", "pending"), ("3", "pending")))
    s.write(items(("4", "pending")), merge=True)
    assert len(old.promote_posts) == 1
    assert len(old.plan_rows("inbox")) == 4

    flaky = Kynver()
    flaky.promote_error = KynverAgentOSError("HTTP 503")
    f = store(flaky, "sess-f")
    assert len(f.write(items(("1", "pending"), ("2", "pending"), ("3", "pending")))) == 3
    assert not f.degraded
    assert len(flaky.plan_rows("inbox")) == 3
    flaky.promote_error = None
    f.write(items(("4", "pending")), merge=True)
    assert f.plan_for_current_scope() == "plan-hermes:sess-f"


def test_there_is_no_manual_plan_link():
    assert not hasattr(TodoPlanResolver, "bind")
    assert not hasattr(KynverTodoStore, "bind")


# --- a: done vs dropped ------------------------------------------------------------------


def test_cancelled_round_trips_as_dropped_not_completed():
    kynver = Kynver()
    a = store(kynver, "sess-a")
    a.write(items(("1", "completed"), ("2", "cancelled")))
    rows = kynver.plan_rows("inbox")
    assert rows["hermes-todo:sess-a:1"]["status"] == "partial"
    assert rows["hermes-todo:sess-a:2"]["status"] == "blocked"

    # A fresh process holding the old statuses reads Kynver's outcome back.
    fresh = store(kynver, "sess-a")
    fresh.restore(items(("1", "pending"), ("2", "pending")))
    assert statuses(fresh.read()) == {"1": "completed", "2": "cancelled"}


def test_replace_cancels_dropped_work_and_keeps_finished_outcomes():
    kynver = Kynver()
    a = store(kynver, "sess-a")
    a.write(items(("done", "completed"), ("dropped", "cancelled"), ("open", "pending")))
    a.write(items(("next", "pending")))

    rows = kynver.plan_rows(a.plan_for_current_scope())
    assert rows["hermes-todo:sess-a:done"]["status"] == "partial"
    assert rows["hermes-todo:sess-a:dropped"]["status"] == "blocked"
    assert rows["hermes-todo:sess-a:open"]["status"] == "blocked"


def test_tool_output_marks_done_and_dropped_differently():
    s = TodoStore()
    out = json.loads(todo_tool(todos=[
        {"id": "1", "content": "Write tests", "status": "completed"},
        {"id": "2", "content": "Old approach", "status": "cancelled"},
        {"id": "3", "content": "Ship it", "status": "in_progress"},
        {"id": "4", "content": "Tell Will", "status": "pending"},
    ], store=s))
    assert out["checklist"].splitlines() == [
        "✓ Write tests", "✗ Old approach (dropped)", "▶ Ship it", "○ Tell Will",
    ]
    assert out["summary"]["completed"] == 1 and out["summary"]["cancelled"] == 1

    line = get_cute_tool_message("todo_list", {"todos": [{"id": "2"}], "merge": True}, 0.1,
                                 result=json.dumps(out))
    assert "1/4" in line and "1 ✗ dropped" in line


# --- d: small, session-only reads --------------------------------------------------------


def test_new_session_sees_only_its_own_rows_on_a_crowded_plan():
    kynver = Kynver()
    inbox = kynver.rows.setdefault("inbox", {})
    for n in range(864):
        inbox[f"hermes-todo:{n}"] = {"rowKey": f"hermes-todo:{n}", "title": "old", "status": "partial"}
        inbox[f"hermes-todo:other:{n}"] = {"rowKey": f"hermes-todo:other:{n}", "title": "x", "status": "partial"}

    new = store(kynver, "sess-new")
    assert new.read() == []
    new.write(items(("1", "pending")))
    assert statuses(new.read()) == {"1": "pending"}
    # Read-back asks Kynver for this session's rows only.
    assert kynver.row_gets and all(q == "rowKeyPrefix=hermes-todo%3Asess-new%3A" for q in kynver.row_gets)


def test_superseded_finished_rows_are_not_merged_back():
    kynver = Kynver()
    a = store(kynver, "sess-a")
    a.write(items(("old1", "completed"), ("old2", "completed")))
    a.write(items(("new", "pending")))
    assert statuses(a.read()) == {"new": "pending"}


@pytest.mark.parametrize("finished", ["completed", "cancelled"])
def test_tool_output_is_capped_by_omitting_old_finished_items(finished):
    s = TodoStore()
    todos = [{"id": str(n), "content": "x" * 400, "status": finished} for n in range(200)]
    todos.append({"id": "live", "content": "still doing this", "status": "in_progress"})
    out = todo_tool(todos=todos, store=s)
    assert len(out) <= MAX_TODO_OUTPUT_CHARS
    data = json.loads(out)
    assert data["summary"]["total"] == 201 and data["summary"][finished] == 200
    assert data["todos"][-1]["id"] == "live"
    assert data["omitted_finished"] == 201 - len(data["todos"])
    # Newest finished items are the ones kept.
    assert data["todos"][-2]["id"] == "199"


def test_tool_output_cap_holds_for_a_long_active_list():
    s = TodoStore()
    todos = [{"id": str(n), "content": "y" * 3000, "status": "pending"} for n in range(60)]
    out = todo_tool(todos=todos, store=s)
    assert len(out) <= MAX_TODO_OUTPUT_CHARS
    assert len(json.loads(out)["todos"]) == 60  # unfinished items are shortened, never dropped
