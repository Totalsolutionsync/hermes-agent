"""Shared todo lists: Hermes ↔ Kynver through one ``POST /todos/session`` call per read/write.

Kynver plans are the single source of truth. These tests pin what the chat sees:

a. several items may be in progress at once (parallel workers);
b. one Kynver call per read or write (it used to be 6–8 and hit HTTP 429);
c. ✓ items finished earlier in the same list stay visible after a merge — including after
   history hydration under a provisional scope (the 2026-09-26 "vanishing ✓" bug);
d. anyone can tick: a cron adopts the chat's list by session id, ticks a worker item, the chat
   sees ✓ with who did it;
e. Kynver down/429 → the local cache answers and the result says so; writes replay later;
   a refusal on the merits (bad adopt ref) is a tool error, not an outage.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from plugins.memory.kynver.agentos_bridge import KynverAgentOSError
from plugins.memory.kynver.operating_config import OperatingLinkage
from plugins.memory.kynver.todo_store import KynverTodoStore
from tools.todo_tool import todo_tool

_UNFINISHED = {"todo", "in_progress", "running"}
_TO_ROW = {"pending": "todo", "in_progress": "in_progress", "completed": "partial", "cancelled": "blocked"}
_TO_HERMES = {"todo": "pending", "in_progress": "in_progress", "running": "in_progress",
              "partial": "completed", "done": "completed", "blocked": "cancelled"}


class Kynver:
    """In-memory port of Kynver's todo-session.service.ts (binding → Inbox → own plan at 3
    items, adopt, list window, actor stamps) behind the one ``/todos/session`` endpoint."""

    def __init__(self) -> None:
        self.plans: Dict[str, Dict[str, Any]] = {"inbox": {"title": "Inbox", "inbox": True}}
        self.rows: Dict[str, List[Dict[str, Any]]] = {"inbox": []}
        self.bindings: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []
        self.clock = 0
        self.down: Optional[KynverAgentOSError] = None
        self.config = type("C", (), {"enabled": True, "api_url": "https://k", "slug": "ghost"})()

    # -- helpers -------------------------------------------------------------------------
    def tick(self) -> int:
        self.clock += 1
        return self.clock

    @staticmethod
    def prefix(session_key: str) -> str:
        return f"hermes-todo:{session_key.split(':', 1)[1]}:"

    def row(self, plan: str, key: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self.rows[plan] if r["rowKey"] == key), None)

    def stamp(self, row: Dict[str, Any], status: str, actor: Dict[str, Any], at: int) -> None:
        row.update(status=status, changedAt=at, source=actor.get("source"), actor=actor.get("name"), ref=actor.get("ref"))

    # -- endpoint ------------------------------------------------------------------------
    def get(self, path, **kwargs):
        raise AssertionError(f"unexpected GET {path}: every sync is one POST /todos/session")

    def post(self, path, body, **kwargs):
        self.calls.append({"path": path, "body": json.loads(json.dumps(body))})
        if self.down:
            raise self.down
        assert path == "/todos/session", path
        key = body["sessionKey"]
        actor = body["actor"]
        adopted = False
        if body.get("adopt"):
            ref = body["adopt"]
            if ref.startswith("session:"):
                target = self.bindings.get(ref[len("session:"):])
                if not target:
                    raise KynverAgentOSError('Kynver AgentOS HTTP 400: {"error":"No todo list found for session"}', status=400)
                plan = target["plan"]
            elif ref in self.plans:
                plan = ref
            else:
                raise KynverAgentOSError(f'Kynver AgentOS HTTP 400: {{"error":"No open plan matches \\"{ref}\\""}}', status=400)
            self.bindings[key] = {"plan": plan, "source": "explicit", "start": self.tick(), "keys": set()}
            adopted = True
        todos = body.get("todos")
        binding = self.bindings.get(key)
        if todos is None and binding is None:
            return {"plan": None, "todos": [], "summary": {}}
        if binding is None:
            binding = self.bindings[key] = {"plan": "inbox", "source": "inbox", "start": None, "keys": set()}
        prefix = self.prefix(key)
        if todos is not None:
            merge = body.get("merge", True)
            plan = binding["plan"]
            if self.plans[plan].get("inbox"):
                own = {r["rowKey"] for r in self.rows[plan] if r["rowKey"].startswith(prefix)
                       and (r["status"] in _UNFINISHED or self.in_view(r, binding))}
                if not merge:
                    own = set()
                own |= {t["id"] if t["id"].startswith("hermes-todo:") else prefix + t["id"] for t in todos}
                if len(own) >= 3:
                    new_plan = f"plan-{key}"
                    self.plans[new_plan] = {"title": todos[0].get("content") or "Todo plan", "inbox": False}
                    self.rows[new_plan] = [r for r in self.rows[plan] if r["rowKey"].startswith(prefix)]
                    self.rows[plan] = [r for r in self.rows[plan] if not r["rowKey"].startswith(prefix)]
                    binding.update(plan=new_plan, source="promoted")
                    plan = new_plan
            now = self.tick()
            given = []
            visible = {r["rowKey"] for r in self.view_rows(plan, prefix)}
            for t in todos:
                row_key = t["id"] if t["id"] in visible or t["id"].startswith(prefix) else prefix + t["id"]
                given.append(row_key)
                row = self.row(plan, row_key)
                status = _TO_ROW[t["status"]]
                if row is None:
                    row = {"rowKey": row_key, "title": t.get("content") or t["id"], "ordinal": len(self.rows[plan])}
                    self.rows[plan].append(row)
                    self.stamp(row, status, actor, now)
                    continue
                if t.get("content"):
                    row["title"] = t["content"]
                if _TO_HERMES[row["status"]] != t["status"]:
                    self.stamp(row, status, actor, now)
            if not merge:
                for row in self.rows[plan]:
                    if row["rowKey"].startswith(prefix) and row["rowKey"] not in given and row["status"] in _UNFINISHED:
                        self.stamp(row, "blocked", actor, now)
                binding.update(start=now, keys=set(given))
            else:
                if binding["start"] is None:
                    binding["start"] = now
                binding["keys"] |= set(given)
        return self.view(key, adopted)

    def view_rows(self, plan: str, prefix: str) -> List[Dict[str, Any]]:
        rows = self.rows[plan]
        return [r for r in rows if r["rowKey"].startswith(prefix)] if self.plans[plan].get("inbox") else rows

    @staticmethod
    def in_view(row: Dict[str, Any], binding: Dict[str, Any]) -> bool:
        if row["status"] in _UNFINISHED or row["rowKey"] in binding["keys"]:
            return True
        return binding["start"] is not None and row["changedAt"] > binding["start"]

    def view(self, key: str, adopted: bool) -> Dict[str, Any]:
        binding = self.bindings[key]
        plan = binding["plan"]
        prefix = self.prefix(key)
        todos = []
        for r in sorted(self.view_rows(plan, prefix), key=lambda r: r["ordinal"]):
            if not self.in_view(r, binding):
                continue
            item = {"id": r["rowKey"][len(prefix):] if r["rowKey"].startswith(prefix) else r["rowKey"],
                    "content": r["title"], "status": _TO_HERMES[r["status"]], "rowKey": r["rowKey"]}
            if r["ref"] and r["ref"] != key:
                item["by"] = {"name": r["actor"] or r["source"], "source": r["source"], "at": None}
            if r["status"] == "in_progress":
                item["since"] = "2026-09-26T10:00:00Z"
            todos.append(item)
        return {"plan": {"id": plan, "title": self.plans[plan]["title"], "inbox": self.plans[plan].get("inbox", False)},
                "todos": todos, "summary": {}, **({"adopted": True} if adopted else {})}


def linkage() -> OperatingLinkage:
    return OperatingLinkage(plan_id=None, task_id=None, session_id=None, executor_ref="hermes:forge")


class _Resolver:  # plan_resolver presence marks the store as Kynver-projecting
    def linkage_for(self, scope):
        return linkage()


def store(kynver: Kynver, scope, **kw) -> KynverTodoStore:
    return KynverTodoStore(kynver, linkage=linkage(), scope=scope, plan_resolver=_Resolver(), **kw)


def call(s: KynverTodoStore, **kw) -> Dict[str, Any]:
    return json.loads(todo_tool(store=s, **kw))


WORKERS = [
    {"id": "w1", "content": "Worker hermes-todo-firstclass", "status": "in_progress"},
    {"id": "w2", "content": "Worker kynver-memory-health", "status": "in_progress"},
    {"id": "w3", "content": "Worker kariad-content-ready", "status": "in_progress"},
    {"id": "n1", "content": "Needs Will: approve deploy", "status": "pending"},
]


def test_three_workers_in_progress_at_once_in_one_call_each():
    kynver = Kynver()
    chat = store(kynver, "chatA", label="Telegram · Will H")

    out = call(chat, todos=WORKERS)
    assert out["summary"]["in_progress"] == 3
    assert out["checklist"].count("▶") == 3
    assert out["sync"] == {"via": "kynver", "plan": "Worker hermes-todo-firstclass"}  # own plan at 3+ items
    assert len(kynver.calls) == 1
    body = kynver.calls[0]["body"]
    assert body["merge"] is False and body["sessionKey"] == "hermes:chatA"
    assert body["actor"] == {"source": "hermes-chat", "name": "Telegram · Will H", "ref": "hermes:chatA"}
    assert body["label"] == "Telegram · Will H"

    call(chat)
    call(chat, todos=[{"id": "w1", "status": "completed"}], merge=True)
    assert len(kynver.calls) == 3  # one call per read, one per write


def test_merge_keeps_same_session_finished_items_visible():
    kynver = Kynver()
    chat = store(kynver, "chatA")
    items = [{"id": str(i), "content": f"Step {i}", "status": "pending"} for i in range(1, 12)]
    items[0]["status"] = items[1]["status"] = "completed"
    call(chat, todos=items)
    out = call(chat, todos=[{"id": "3", "status": "in_progress"}, {"id": "4", "status": "completed"},
                            {"id": "12", "content": "New step", "status": "pending"}], merge=True)
    assert out["summary"]["total"] == 12
    assert out["summary"]["completed"] == 3
    assert [i["id"] for i in out["todos"] if i["status"] == "completed"] == ["1", "2", "4"]


def test_hydrated_list_survives_scope_resolution_and_outage():
    """Gateway builds a fresh agent per message: history restores the list before the
    session's lineage row exists (provisional scope), then the real scope resolves. The ✓
    items must not vanish — not from Kynver's view and not from the local fallback."""
    kynver = Kynver()
    root = store(kynver, "root")
    history = [{"id": str(i), "content": f"Step {i}", "status": "pending"} for i in range(1, 14)]
    history[0]["status"] = history[1]["status"] = "completed"
    written = call(root, todos=history)

    scope = {"value": "child-provisional"}
    fresh = store(kynver, lambda: scope["value"])
    fresh.restore(written["todos"], revision=written["revision"])
    scope["value"] = "root"  # lineage row written: the compression root is the scope

    online = call(fresh, todos=[{"id": "3", "status": "in_progress"}, {"id": "4", "status": "in_progress"},
                                {"id": "5", "status": "in_progress"}], merge=True)
    assert online["summary"]["completed"] == 2 and online["summary"]["total"] == 13
    assert online["revision"] > written["revision"]

    kynver.down = KynverAgentOSError("Kynver AgentOS HTTP 429: Too many requests", status=429)
    scope2 = {"value": "child-provisional"}
    offline = store(kynver, lambda: scope2["value"])
    offline.restore(written["todos"], revision=written["revision"])
    scope2["value"] = "root"
    out = call(offline, todos=[{"id": "6", "status": "in_progress"}], merge=True)
    assert out["summary"]["completed"] == 2 and out["summary"]["total"] == 13
    assert out["sync"]["via"] == "local cache"
    assert "HTTP 429" in out["sync"]["note"]


def test_cron_ticks_the_chats_worker_item_and_the_chat_sees_who():
    kynver = Kynver()
    chat = store(kynver, "chatA", label="Telegram · Will H")
    call(chat, todos=WORKERS)

    cron = store(kynver, "cron-run-1", label="Hermes cron · Worker wake", actor_source="hermes-cron",
                 scope_for_session=lambda session_id: {"20260926_101500_abcd": "chatA"}.get(session_id))
    picked = call(cron, plan="session:20260926_101500_abcd")
    assert kynver.calls[-1]["body"]["adopt"] == "session:hermes:chatA"  # raw session id → its todo scope
    assert picked["sync"]["adopted"] is True
    item = next(i for i in picked["todos"] if "firstclass" in i["content"])
    assert item["id"] == "hermes-todo:chatA:w1"

    call(cron, todos=[{"id": item["id"], "status": "completed"}], merge=True)
    assert kynver.calls[-1]["body"]["actor"]["source"] == "hermes-cron"

    seen = call(chat)
    w1 = next(i for i in seen["todos"] if i["id"] == "w1")
    assert w1["status"] == "completed"
    assert w1["by"] == "Hermes cron · Worker wake (Hermes cron)"
    assert "✓ Worker hermes-todo-firstclass — by Hermes cron · Worker wake (Hermes cron)" in seen["checklist"]
    assert "by" not in next(i for i in seen["todos"] if i["id"] == "w2")


def test_chat_b_adopts_chat_a_plan_and_finishes_an_item():
    kynver = Kynver()
    chat_a = store(kynver, "chatA", label="Telegram · Will H")
    plan_id = call(chat_a, todos=WORKERS) and kynver.bindings["hermes:chatA"]["plan"]
    chat_b = store(kynver, "chatB", label="Telegram · Will H (2)")
    got = call(chat_b, plan=plan_id)
    w2 = next(i for i in got["todos"] if "memory-health" in i["content"])
    call(chat_b, todos=[{"id": w2["id"], "status": "cancelled"}], merge=True)
    seen = call(chat_a)
    assert next(i for i in seen["todos"] if i["id"] == "w2")["status"] == "cancelled"
    assert "✗ Worker kynver-memory-health (dropped) — by Telegram · Will H (2) (Hermes chat)" in seen["checklist"]


def test_outage_writes_replay_on_recovery_and_kynver_wins_after():
    now = [0.0]
    kynver = Kynver()
    chat = store(kynver, "chatA", clock=lambda: now[0], retry_interval_seconds=30)
    call(chat, todos=[{"id": "a", "content": "A", "status": "pending"}])

    kynver.down = KynverAgentOSError("Kynver AgentOS HTTP 503: unavailable", status=503)
    offline = call(chat, todos=[{"id": "a", "status": "in_progress"}], merge=True)
    assert offline["todos"][0]["status"] == "in_progress" and offline["sync"]["via"] == "local cache"
    calls_while_down = len(kynver.calls)
    assert call(chat)["sync"]["via"] == "local cache"
    assert len(kynver.calls) == calls_while_down  # no hammering inside the retry window

    kynver.down = None
    now[0] = 31.0
    back = call(chat)
    assert back["sync"]["via"] == "kynver"
    assert kynver.bindings["hermes:chatA"] and kynver.rows["inbox"][0]["status"] == "in_progress"
    assert back["todos"][0]["status"] == "in_progress"


def test_refusals_on_the_merits_are_tool_errors_not_outages():
    kynver = Kynver()
    chat = store(kynver, "chatA")
    out = call(chat, plan="Some plan nobody has")
    assert "No open plan matches" in out["error"]
    assert not chat.degraded


def test_local_store_explains_that_adoption_needs_kynver():
    from tools.todo_tool import TodoStore
    out = json.loads(todo_tool(store=TodoStore(), plan="plan-123"))
    assert "Kynver" in out["error"]


def test_no_pre_call_guard_adds_kynver_round_trips(monkeypatch):
    """The pre_tool_call guard used to read plan rows (1–2 calls) before every write; Kynver
    now enforces transitions inside the one session call, so no hook is registered."""
    from plugins.memory.kynver import operating_hooks

    registered = []
    ctx = type("Ctx", (), {"register_hook": lambda self, name, fn: registered.append(name)})()
    monkeypatch.setenv("KYNVER_OPERATING_TOOLS", "true")
    operating_hooks.register_operating_hooks(ctx)
    assert registered == []
