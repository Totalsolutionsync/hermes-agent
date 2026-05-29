import pytest

from plugins.memory.kynver.operating_context import OperatingContext
from plugins.memory.kynver.todo_store import KynverTodoStore


class TodoFakeClient:
    def __init__(self):
        self.tasks = {}
        self.calls = []
        self.config = type("C", (), {"enabled": True})()

    def get(self, path, *, slug=None):
        self.calls.append(("GET", path))
        if path.startswith("/tasks"):
            items = list(self.tasks.values())
            return {"tasks": items}
        return {}

    def post(self, path, body, *, slug=None):
        self.calls.append(("POST", path, body))
        if path == "/tasks":
            tid = f"task-{len(self.tasks)+1}"
            task = dict(body)
            task["id"] = tid
            task["status"] = body.get("status", "ready")
            idem = body.get("idempotencyKey", "")
            self.tasks[tid] = task
            return task
        if "/close" in path:
            tid = path.split("/")[2]
            if tid in self.tasks:
                self.tasks[tid]["status"] = body.get("status", "done")
            return {"ok": True}
        return {}

    def patch(self, path, body, *, slug=None):
        self.calls.append(("PATCH", path, body))
        tid = path.split("/")[2]
        if tid in self.tasks:
            self.tasks[tid].update(body)
        return self.tasks.get(tid, {})


def test_todo_write_creates_agentos_task_with_idempotency():
    client = TodoFakeClient()
    ctx = OperatingContext(plan_id="plan-1", task_id="parent-task")
    store = KynverTodoStore(client, operating_context=ctx, hermes_session_id="sess-1")

    items = store.write(
        [{"id": "1", "content": "First step", "status": "in_progress"}],
        merge=False,
    )

    assert len(items) == 1
    assert items[0]["status"] == "in_progress"
    assert any(c[0] == "POST" and c[1] == "/tasks" for c in client.calls)
    body = next(c[2] for c in client.calls if c[0] == "POST" and c[1] == "/tasks")
    assert body["idempotencyKey"] == "hermes-forge:todo:1"
    assert body["parentTaskId"] == "parent-task"


def test_todo_close_maps_completed_to_done():
    client = TodoFakeClient()
    ctx = OperatingContext(plan_id="plan-1", task_id="parent-task")
    store = KynverTodoStore(client, operating_context=ctx)

    store.write([{"id": "a", "content": "Do thing", "status": "in_progress"}], merge=False)
    store.write([{"id": "a", "content": "Do thing", "status": "completed"}], merge=True)

    close_calls = [c for c in client.calls if c[0] == "POST" and "/close" in c[1]]
    assert close_calls
    assert close_calls[-1][2]["status"] == "done"


def test_single_in_progress_projection():
    client = TodoFakeClient()
    client.tasks = {
        "t1": {
            "id": "t1",
            "title": "A",
            "status": "running",
            "idempotencyKey": "hermes-forge:todo:a",
        },
        "t2": {
            "id": "t2",
            "title": "B",
            "status": "running",
            "idempotencyKey": "hermes-forge:todo:b",
        },
    }
    store = KynverTodoStore(client, operating_context=OperatingContext(plan_id="p", task_id="t"))
    items = store.read()
    in_progress = [i for i in items if i["status"] == "in_progress"]
    assert len(in_progress) == 1


def test_degraded_fallback_on_agentos_failure():
    client = TodoFakeClient()

    def fail_get(path, *, slug=None):
        raise RuntimeError("network down")

    client.get = fail_get
    store = KynverTodoStore(
        client,
        operating_context=OperatingContext(plan_id="p", task_id="t"),
        allow_fallback=True,
    )
    written = store.write([{"id": "x", "content": "local", "status": "pending"}], merge=False)
    assert store.degraded
    assert written[0]["content"] == "local"
