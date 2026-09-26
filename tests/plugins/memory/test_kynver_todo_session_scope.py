"""Kynver todo rows are scoped per Hermes session.

Every session shares one AgentOS plan. Unscoped ``hermes-todo:<id>`` keys made a brand-new
chat read back every past session's rows (859 items live) and let one session's replace write
supersede another session's rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from plugins.memory.kynver.agentos_bridge import KynverAgentOSError
from plugins.memory.kynver.integration import session_todo_scope
from plugins.memory.kynver.operating_config import OperatingLinkage
from plugins.memory.kynver.pre_transition import parse_hermes_row_key
from plugins.memory.kynver.todo_store import KynverTodoStore

LINKAGE = OperatingLinkage(plan_id="plan-1", task_id=None, session_id=None, executor_ref="hermes:forge")


def _prefix_filter(query: str, rows):
    """Server-side ``?rowKeyPrefix=`` narrowing of GET progress-rows."""
    from urllib.parse import parse_qs

    prefix = (parse_qs(query).get("rowKeyPrefix") or [""])[0]
    return [dict(r) for r in rows if str(r.get("rowKey", "")).startswith(prefix)]


class SharedPlan:
    """One AgentOS plan with the server's focus semantics (kynver agent-os.plan-progress-*):
    focusing a row demotes every other in_progress row plan-wide; clearing resets the focused
    row to ``todo`` whatever its status; GET /plans/:id does not expose the focus key."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.focus_key: str | None = None

    def get(self, path, **kwargs):
        path, _, query = path.partition("?")
        if path.endswith("/progress-rows"):
            return {"items": _prefix_filter(query, self.rows.values())}
        return {"plan": {"id": "plan-1"}}

    def post(self, path, body, **kwargs):
        if path == "/todos/session":  # an older Kynver: legacy projection path
            raise KynverAgentOSError("Kynver AgentOS HTTP 404: Not found", status=404)
        if path.endswith("/progress-rows"):
            for row in body["rows"]:
                self.rows[row["rowKey"]] = dict(row)
        elif path.endswith("/progress-focus"):
            key = body.get("rowKey")
            if key:
                for other in self.rows.values():
                    if other["status"] == "in_progress":
                        other["status"] = "todo"
                self.rows[key]["status"] = "in_progress"
            elif self.focus_key:
                self.rows[self.focus_key]["status"] = "todo"
            self.focus_key = key
        return {"ok": True}


def _ids(items):
    return {item["id"]: item["status"] for item in items}


def test_sessions_on_one_plan_only_see_and_supersede_their_own_rows():
    plan = SharedPlan()
    plan.rows["hermes-todo:build"] = {"rowKey": "hermes-todo:build", "title": "legacy", "status": "todo"}
    a = KynverTodoStore(plan, linkage=LINKAGE, scope="sess-a")
    b = KynverTodoStore(plan, linkage=LINKAGE, scope="sess-b")

    a.write([{"id": "build", "content": "A build", "status": "pending"}])
    b.write([{"id": "build", "content": "B build", "status": "pending"}])
    # A replace write supersedes only A's omitted rows: the unfinished one was dropped, so it
    # is cancelled in AgentOS (not claimed completed) and leaves A's working set.
    a.write([{"id": "ship", "content": "A ship", "status": "pending"}])

    assert _ids(b.read()) == {"build": "pending"}
    assert _ids(a.read()) == {"ship": "pending"}
    assert plan.rows["hermes-todo:sess-a:build"]["status"] == "blocked"
    assert plan.rows["hermes-todo:sess-b:build"]["status"] == "todo"
    assert _ids(KynverTodoStore(plan, linkage=LINKAGE, scope="sess-new").read()) == {}
    # The legacy unscoped row is never pulled in, and nothing rewrote it.
    assert plan.rows["hermes-todo:build"]["status"] == "todo"
    assert plan.rows["hermes-todo:sess-a:build"]["title"] == "A build"
    assert plan.rows["hermes-todo:sess-b:build"]["title"] == "B build"


@pytest.mark.parametrize(
    ("row_key", "parsed"),
    [
        ("hermes-todo:build", (None, "build")),
        ("hermes-todo:20260924_101500_ab12cd:build", ("20260924_101500_ab12cd", "build")),
        ("hermes-todo:s1:step:2", ("s1", "step:2")),
        ("hermes-improvement:lesson:x", None),
        ("M3", None),
    ],
)
def test_row_key_parsing_distinguishes_legacy_and_scoped_keys(row_key, parsed):
    assert parse_hermes_row_key(row_key) == parsed


def test_plan_focus_is_released_only_by_its_holder_without_undoing_completion():
    plan = SharedPlan()
    a = KynverTodoStore(plan, linkage=LINKAGE, scope="sess-a")
    b = KynverTodoStore(plan, linkage=LINKAGE, scope="sess-b")

    a.write([
        {"id": "x", "content": "A x", "status": "in_progress"},
        {"id": "y", "content": "A y", "status": "pending"},
    ])
    # B does not hold the focus slot, so its replace must not clear (and reset) A's row.
    b.write([{"id": "x", "content": "B x", "status": "pending"}])
    assert plan.focus_key == "hermes-todo:sess-a:x"
    assert _ids(a.read()) == {"x": "in_progress", "y": "pending"}
    assert _ids(b.read()) == {"x": "pending"}

    a.write([{"id": "y", "status": "in_progress"}, {"id": "x", "status": "completed"}], merge=True)
    assert _ids(a.read()) == {"x": "completed", "y": "in_progress"}

    # Completing the focused item releases focus and the item stays completed.
    a.write([{"id": "y", "status": "completed"}], merge=True)
    assert plan.focus_key is None
    assert _ids(a.read()) == {"x": "completed", "y": "completed"}


def test_compaction_child_session_continues_the_same_list(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", "cli")
        plan = SharedPlan()
        agent = SimpleNamespace(session_id="root", _session_db=db)
        store = KynverTodoStore(plan, linkage=LINKAGE, scope=lambda: session_todo_scope(agent))
        store.write([
            {"id": "a", "content": "step a", "status": "in_progress"},
            {"id": "b", "content": "step b", "status": "pending"},
        ])

        # Compression ends the parent and rotates agent.session_id onto the child in place.
        db.end_session("root", "compression")
        db.create_session("child", "cli", parent_session_id="root")
        agent.session_id = "child"
        assert _ids(store.read()) == {"a": "in_progress", "b": "pending"}
        store.write([{"id": "a", "status": "completed"}, {"id": "b", "status": "in_progress"}], merge=True)

        # A fresh agent built on the child (gateway) continues the same list: its unfinished
        # items come back; finished ones stay in AgentOS, like the post-compression injection.
        fresh = SimpleNamespace(session_id="child", _session_db=db)
        fresh_store = KynverTodoStore(plan, linkage=LINKAGE, scope=lambda: session_todo_scope(fresh))
        assert _ids(fresh_store.read()) == {"b": "in_progress"}
        assert plan.rows["hermes-todo:root:a"]["status"] == "partial"
        assert {key for key in plan.rows} == {"hermes-todo:root:a", "hermes-todo:root:b"}

        # A branch off the conversation is its own lineage and starts empty.
        db.create_session("branch", "cli", parent_session_id="child", model_config={"_branched_from": "child"})
        branch = SimpleNamespace(session_id="branch", _session_db=db)
        assert KynverTodoStore(plan, linkage=LINKAGE, scope=lambda: session_todo_scope(branch)).read() == []
    finally:
        db.close()
