"""Per-session todo plan binding — each Hermes session's todos land on its own AgentOS plan."""

from __future__ import annotations

import pytest

from plugins.memory.kynver import operating_hooks, plan_binding
from plugins.memory.kynver.agentos_bridge import KynverAgentOSError
from plugins.memory.kynver.operating_config import OperatingLinkage
from plugins.memory.kynver.plan_binding import (
    TodoPlanResolver,
    plan_binding_enabled,
    shared_plan_resolver,
)
from plugins.memory.kynver.todo_store import KynverTodoStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _prefix_filter(query: str, rows):
    """Server-side ``?rowKeyPrefix=`` narrowing of GET progress-rows."""
    from urllib.parse import parse_qs

    prefix = (parse_qs(query).get("rowKeyPrefix") or [""])[0]
    return [dict(r) for r in rows if str(r.get("rowKey", "")).startswith(prefix)]


class BindingFakeClient:
    """AgentOS fake: plan-binding endpoint + per-plan progress rows/focus."""

    def __init__(self, bindings: dict[str, str] | None = None):
        self.bindings = dict(bindings or {})
        self.rows: dict[str, dict[str, dict]] = {}
        self.focus: dict[str, str | None] = {}
        self.calls: list[tuple] = []
        self.binding_error: Exception | None = None
        self.config = type("C", (), {"enabled": True, "api_url": "https://k", "slug": "ghost"})()

    @staticmethod
    def _plan(path: str) -> str:
        return path.split("/plans/")[1].split("/")[0]

    def get(self, path, **kwargs):
        self.calls.append(("GET", path))
        path, _, query = path.partition("?")
        plan = self._plan(path)
        if path.endswith("/progress-rows"):
            return {"items": _prefix_filter(query, self.rows.get(plan, {}).values())}
        return {"plan": {"id": plan, "inProgressRowKey": self.focus.get(plan)}}

    def post(self, path, body, **kwargs):
        if path == "/todos/session":  # an older Kynver: legacy projection path
            raise KynverAgentOSError("Kynver AgentOS HTTP 404: Not found", status=404)
        self.calls.append(("POST", path, body))
        if path == plan_binding.BINDING_PATH:
            if self.binding_error:
                raise self.binding_error
            key = body["sessionKey"]
            if body.get("bind"):
                self.bindings[key] = body.get("planId") or f"plan-of-{body.get('taskId')}"
            plan_id = self.bindings.setdefault(key, "inbox")
            return {
                "planId": plan_id,
                "planTitle": f"Title {plan_id}",
                "source": "explicit" if body.get("bind") else "inbox",
                "bound": True,
            }
        plan = self._plan(path)
        if path.endswith("progress-rows"):
            for row in body.get("rows", []):
                self.rows.setdefault(plan, {})[row["rowKey"]] = dict(row)
        if path.endswith("progress-focus"):
            self.focus[plan] = body.get("rowKey")
        return {"ok": True}

    def binding_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == plan_binding.BINDING_PATH]


def linkage(plan_id: str | None = "legacy-plan", task_id: str | None = "task-1") -> OperatingLinkage:
    return OperatingLinkage(plan_id=plan_id, task_id=task_id, session_id=None, executor_ref="hermes:forge")


# --- resolver ---------------------------------------------------------------------------


def test_resolver_posts_session_key_and_task_hint_then_caches_until_ttl():
    client = BindingFakeClient({"hermes:s1": "plan-a"})
    clock = Clock()
    resolver = TodoPlanResolver(client, linkage(), clock=clock, ttl_seconds=300)

    plan = resolver.plan_for("s1")
    assert (plan.plan_id, plan.title, plan.bound) == ("plan-a", "Title plan-a", True)
    assert client.binding_posts()[0][2] == {"sessionKey": "hermes:s1", "hints": {"taskId": "task-1"}}

    assert resolver.plan_for("s1").plan_id == "plan-a"
    assert len(client.binding_posts()) == 1

    clock.now += 301
    client.bindings["hermes:s1"] = "plan-b"  # re-bound elsewhere (chat / MCP bind)
    assert resolver.plan_for("s1").plan_id == "plan-b"
    assert len(client.binding_posts()) == 2


def test_resolver_omits_hints_without_a_linked_task():
    client = BindingFakeClient()
    TodoPlanResolver(client, linkage(task_id=None)).plan_for("s1")
    assert client.binding_posts()[0][2] == {"sessionKey": "hermes:s1"}


def test_missing_endpoint_falls_back_to_legacy_plan_and_stays_there_for_the_ttl():
    client = BindingFakeClient()
    client.binding_error = KynverAgentOSError("Kynver AgentOS HTTP 404: Not Found")
    clock = Clock()
    resolver = TodoPlanResolver(client, linkage(), clock=clock, ttl_seconds=300)

    plan = resolver.plan_for("s1")
    assert (plan.plan_id, plan.source) == ("legacy-plan", "legacy")
    resolver.plan_for("s2")
    assert len(client.binding_posts()) == 1  # no hammering an old deploy

    clock.now += 301
    client.binding_error = None
    assert resolver.plan_for("s1").plan_id == "inbox"


def test_missing_endpoint_without_legacy_plan_is_local_only():
    client = BindingFakeClient()
    client.binding_error = KynverAgentOSError("Kynver AgentOS HTTP 405: Method Not Allowed")
    resolver = TodoPlanResolver(client, linkage(plan_id=None))
    assert resolver.plan_for("s1") is None
    assert resolver.linkage_for("s1").plan_id is None


@pytest.mark.parametrize(
    "error",
    [
        KynverAgentOSError("Kynver AgentOS HTTP 500: boom"),
        KynverAgentOSError("Kynver AgentOS request failed: timed out"),
    ],
)
def test_transient_failures_raise_instead_of_redirecting_to_legacy(error):
    client = BindingFakeClient()
    client.binding_error = error
    with pytest.raises(KynverAgentOSError):
        TodoPlanResolver(client, linkage()).plan_for("s1")


def test_response_without_plan_id_raises():
    client = BindingFakeClient()
    client.post = lambda path, body, **kw: {"ok": True}
    with pytest.raises(KynverAgentOSError, match="no planId"):
        TodoPlanResolver(client, linkage()).plan_for("s1")


def test_disabled_binding_uses_the_legacy_plan_without_calling_kynver(monkeypatch):
    monkeypatch.setenv("KYNVER_TODO_PLAN_BINDING", "off")
    assert plan_binding_enabled() is False
    client = BindingFakeClient()
    resolver = TodoPlanResolver(client, linkage(), enabled=plan_binding_enabled())
    assert resolver.plan_for("s1").plan_id == "legacy-plan"
    assert client.binding_posts() == []


def test_shared_resolver_is_one_instance_per_target(monkeypatch):
    monkeypatch.setattr(plan_binding, "_SHARED", {})
    client = BindingFakeClient()
    a = shared_plan_resolver(client, linkage())
    assert shared_plan_resolver(client, linkage()) is a
    assert shared_plan_resolver(client, linkage(plan_id="other")) is not a


# --- store ------------------------------------------------------------------------------


def test_sessions_project_into_their_own_bound_plans():
    client = BindingFakeClient({"hermes:s1": "plan-a", "hermes:s2": "plan-b"})
    resolver = TodoPlanResolver(client, linkage())
    scope = {"value": "s1"}
    store = KynverTodoStore(client, linkage=linkage(), scope=lambda: scope["value"], plan_resolver=resolver)

    store.write([{"id": "x", "content": "Session one work", "status": "in_progress"}], merge=True)
    scope["value"] = "s2"
    store.write([{"id": "y", "content": "Session two work", "status": "pending"}], merge=True)

    assert set(client.rows) == {"plan-a", "plan-b"}
    assert list(client.rows["plan-a"]) == ["hermes-todo:s1:x"]
    assert list(client.rows["plan-b"]) == ["hermes-todo:s2:y"]
    assert client.focus["plan-a"] == "hermes-todo:s1:x"
    assert "legacy-plan" not in client.rows
    assert [i["id"] for i in store.read()] == ["y"]
    assert store.plan_for_current_scope() == "plan-b"


def test_transient_resolution_failure_degrades_then_replays_into_the_scopes_plan():
    client = BindingFakeClient({"hermes:s1": "plan-a"})
    clock = Clock()
    resolver = TodoPlanResolver(client, linkage(), clock=clock)
    store = KynverTodoStore(
        client,
        linkage=linkage(),
        scope="s1",
        plan_resolver=resolver,
        clock=clock,
        retry_interval_seconds=30,
    )

    client.binding_error = KynverAgentOSError("Kynver AgentOS request failed: timed out")
    items = store.write([{"id": "x", "content": "Offline", "status": "pending"}], merge=True)
    assert store.degraded is True
    assert [i["id"] for i in items] == ["x"]
    assert "legacy-plan" not in client.rows  # never silently redirected

    client.binding_error = None
    clock.now += 31
    assert [i["id"] for i in store.read()] == ["x"]
    assert store.degraded is False
    assert list(client.rows["plan-a"]) == ["hermes-todo:s1:x"]


def test_without_a_resolver_the_fixed_legacy_plan_is_used():
    client = BindingFakeClient()
    store = KynverTodoStore(client, linkage=linkage(), scope="s1")
    store.write([{"id": "x", "content": "Legacy", "status": "pending"}], merge=True)
    assert list(client.rows) == ["legacy-plan"]
    assert client.binding_posts() == []


# --- pre_tool_call guard ----------------------------------------------------------------


def test_guard_inspects_the_sessions_bound_plan(monkeypatch):
    client = BindingFakeClient({"hermes:s1": "plan-a"})
    monkeypatch.setattr(plan_binding, "_SHARED", {})
    monkeypatch.setattr(operating_hooks, "_client", lambda: client)
    monkeypatch.setattr(operating_hooks, "load_operating_linkage", lambda: linkage())
    monkeypatch.setattr(operating_hooks, "known_session_scope", lambda sid: "s1")

    result = operating_hooks.on_pre_tool_call(
        tool_name="todo",
        args={"todos": [{"id": "x", "content": "X", "status": "pending"}], "merge": True},
        session_id="s1",
    )
    assert result is None
    gets = [c[1] for c in client.calls if c[0] == "GET"]
    assert gets and all("/plans/plan-a/" in path for path in gets)


def test_guard_fails_open_when_the_plan_cannot_be_resolved(monkeypatch):
    client = BindingFakeClient()
    client.binding_error = KynverAgentOSError("Kynver AgentOS HTTP 503: busy")
    monkeypatch.setattr(plan_binding, "_SHARED", {})
    monkeypatch.setattr(operating_hooks, "_client", lambda: client)
    monkeypatch.setattr(operating_hooks, "load_operating_linkage", lambda: linkage())
    monkeypatch.setattr(operating_hooks, "known_session_scope", lambda sid: "s1")

    result = operating_hooks.on_pre_tool_call(
        tool_name="todo",
        args={"todos": [{"id": "x", "status": "pending"}]},
        session_id="s1",
    )
    assert result is None
    assert [c for c in client.calls if c[0] == "GET"] == []
