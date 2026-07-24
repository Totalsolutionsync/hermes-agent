"""KynverTodoStore — plan progress projection, degraded fallback, no running lease."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from plugins.memory.kynver.operating_config import OperatingLinkage
from plugins.memory.kynver.plan_progress import project_todo_write
from plugins.memory.kynver.pre_transition import PreTransitionError
from plugins.memory.kynver.todo_store import KynverTodoStore


class PlanProgressFakeClient:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.focus_key: str | None = None
        self.calls: list[tuple] = []
        self.config = type("C", (), {"enabled": True})()

    def get(self, path, **kwargs):
        self.calls.append(("GET", path))
        if path.endswith("/progress-rows"):
            return {"items": list(self.rows.values())}
        if "/plans/" in path and not path.endswith("progress-rows"):
            return {"plan": {"id": "plan-1", "inProgressRowKey": self.focus_key}}
        return {}

    def post(self, path, body, **kwargs):
        self.calls.append(("POST", path, body))
        if path.endswith("progress-rows"):
            for row in body.get("rows", []):
                self.rows[row["rowKey"]] = dict(row)
        if path.endswith("progress-focus"):
            self.focus_key = body.get("rowKey")
        return {"ok": True}


def test_todo_write_projects_in_progress_focus_not_running():
    client = PlanProgressFakeClient()
    linkage = OperatingLinkage(
        plan_id="plan-1",
        task_id="task-1",
        session_id="sess-1",
        executor_ref="hermes:forge",
    )
    store = KynverTodoStore(client, linkage=linkage)

    items = store.write(
        [{"id": "a", "content": "Step A", "status": "in_progress"}],
        merge=False,
    )

    assert items[0]["status"] == "in_progress"
    focus_calls = [c for c in client.calls if c[0] == "POST" and str(c[1]).endswith("progress-focus")]
    assert focus_calls
    assert focus_calls[0][2]["rowKey"] == "hermes-todo:a"
    row_posts = [c for c in client.calls if c[0] == "POST" and str(c[1]).endswith("progress-rows")]
    assert row_posts
    assert row_posts[0][2]["rows"][0]["status"] == "in_progress"
    assert "running" not in str(row_posts)


def test_running_row_read_back_stays_pending_without_focus():
    client = PlanProgressFakeClient()
    client.rows["hermes-todo:a"] = {
        "rowKey": "hermes-todo:a",
        "title": "Lease row",
        "status": "running",
    }
    client.focus_key = None
    linkage = OperatingLinkage(
        plan_id="plan-1",
        task_id=None,
        session_id=None,
        executor_ref="hermes:forge",
    )
    store = KynverTodoStore(client, linkage=linkage)

    items = store.read()
    assert len(items) == 1
    assert items[0]["status"] == "pending"


def test_degraded_fallback_on_agentos_failure():
    client = PlanProgressFakeClient()

    def fail_get(path, **kwargs):
        raise RuntimeError("network down")

    client.get = fail_get
    linkage = OperatingLinkage(plan_id="plan-1", task_id=None, session_id=None, executor_ref="hermes:forge")
    store = KynverTodoStore(client, linkage=linkage, allow_fallback=True)

    written = store.write([{"id": "x", "content": "local", "status": "pending"}], merge=False)
    assert store.degraded
    assert written[0]["content"] == "local"


def test_degraded_store_recovers_after_retry_interval():
    client = PlanProgressFakeClient()
    original_get = client.get
    failing = True

    def flaky_get(path, **kwargs):
        if failing:
            raise RuntimeError("network down")
        return original_get(path, **kwargs)

    client.get = flaky_get
    now = [100.0]
    linkage = OperatingLinkage(
        plan_id="plan-1",
        task_id=None,
        session_id=None,
        executor_ref="hermes:forge",
    )
    store = KynverTodoStore(
        client,
        linkage=linkage,
        allow_fallback=True,
        retry_interval_seconds=30,
        clock=lambda: now[0],
    )

    local = store.write([{"id": "x", "content": "local update", "status": "completed"}])
    assert store.degraded
    assert local[0]["content"] == "local update"

    failing = False
    client.rows["hermes-todo:x"] = {
        "rowKey": "hermes-todo:x",
        "title": "stale remote",
        "status": "todo",
    }
    now[0] = 129.9
    assert store.read()[0]["content"] == "local update"
    assert store.degraded

    now[0] = 130.0
    recovered = store.read()
    assert recovered == [{"id": "x", "content": "local update", "status": "completed"}]
    assert client.rows["hermes-todo:x"]["title"] == "local update"
    assert client.rows["hermes-todo:x"]["status"] == "partial"
    assert not store.degraded


def test_write_retries_remote_projection_after_degraded_cooldown():
    client = PlanProgressFakeClient()
    original_get = client.get
    failing = True

    def flaky_get(path, **kwargs):
        if failing:
            raise RuntimeError("network down")
        return original_get(path, **kwargs)

    client.get = flaky_get
    now = [10.0]
    linkage = OperatingLinkage(
        plan_id="plan-1",
        task_id=None,
        session_id=None,
        executor_ref="hermes:forge",
    )
    store = KynverTodoStore(
        client,
        linkage=linkage,
        retry_interval_seconds=5,
        clock=lambda: now[0],
    )
    store.write([{"id": "x", "content": "local", "status": "pending"}])
    assert store.degraded

    failing = False
    now[0] = 15.0
    result = store.write([{"id": "y", "content": "synced", "status": "in_progress"}])

    assert not store.degraded
    assert client.rows["hermes-todo:y"]["title"] == "synced"
    assert client.focus_key == "hermes-todo:y"
    assert result[0]["id"] == "y"


def test_merge_recovery_preserves_unrelated_remote_row_and_focus():
    client = PlanProgressFakeClient()
    original_get = client.get
    failing = False

    def flaky_get(path, **kwargs):
        if failing:
            raise RuntimeError("network down")
        return original_get(path, **kwargs)

    client.get = flaky_get
    now = [0.0]
    linkage = OperatingLinkage(
        plan_id="plan-1",
        task_id=None,
        session_id=None,
        executor_ref="hermes:forge",
    )
    store = KynverTodoStore(
        client,
        linkage=linkage,
        retry_interval_seconds=30,
        clock=lambda: now[0],
    )
    store.write(
        [
            {"id": "x", "content": "x0", "status": "pending"},
            {"id": "y", "content": "y0", "status": "pending"},
        ],
        merge=False,
    )

    failing = True
    store.write([{"id": "x", "content": "x-local", "status": "completed"}], merge=True)
    assert store.degraded

    failing = False
    client.rows["hermes-todo:y"]["title"] = "y-remote"
    client.rows["hermes-todo:y"]["status"] = "partial"
    client.focus_key = "hermes-todo:y"
    now[0] = 30.0

    recovered = store.read()
    by_id = {item["id"]: item for item in recovered}
    assert by_id["x"] == {"id": "x", "content": "x-local", "status": "completed"}
    assert by_id["y"] == {"id": "y", "content": "y-remote", "status": "in_progress"}
    assert client.rows["hermes-todo:y"]["title"] == "y-remote"
    assert client.focus_key == "hermes-todo:y"
    assert not store.degraded


def test_recovery_replays_multiple_degraded_writes_in_order():
    client = PlanProgressFakeClient()
    original_get = client.get
    failing = True

    def flaky_get(path, **kwargs):
        if failing:
            raise RuntimeError("network down")
        return original_get(path, **kwargs)

    client.get = flaky_get
    now = [0.0]
    linkage = OperatingLinkage(
        plan_id="plan-1",
        task_id=None,
        session_id=None,
        executor_ref="hermes:forge",
    )
    store = KynverTodoStore(
        client,
        linkage=linkage,
        retry_interval_seconds=10,
        clock=lambda: now[0],
    )

    store.write([{"id": "x", "content": "first", "status": "pending"}], merge=False)
    store.write([{"id": "y", "content": "second", "status": "in_progress"}], merge=True)
    assert store.degraded

    failing = False
    now[0] = 10.0
    recovered = store.read()
    by_id = {item["id"]: item for item in recovered}

    assert by_id["x"]["content"] == "first"
    assert by_id["y"] == {"id": "y", "content": "second", "status": "in_progress"}
    assert client.focus_key == "hermes-todo:y"
    assert not store.degraded


def test_conflicting_recovery_write_does_not_starve_later_batches():
    client = PlanProgressFakeClient()
    original_get = client.get
    failing = True

    def flaky_get(path, **kwargs):
        if failing:
            raise RuntimeError("network down")
        return original_get(path, **kwargs)

    client.get = flaky_get
    now = [0.0]
    linkage = OperatingLinkage(
        plan_id="plan-1",
        task_id=None,
        session_id=None,
        executor_ref="hermes:forge",
    )
    store = KynverTodoStore(
        client,
        linkage=linkage,
        retry_interval_seconds=10,
        clock=lambda: now[0],
    )

    store.write([{"id": "x", "content": "blocked focus", "status": "in_progress"}], merge=True)
    store.write([{"id": "y", "content": "later write", "status": "completed"}], merge=True)
    assert store.degraded

    failing = False
    client.rows["hermes-todo:x"] = {
        "rowKey": "hermes-todo:x",
        "title": "leased remotely",
        "status": "running",
    }
    now[0] = 10.0
    recovered = store.read()
    by_id = {item["id"]: item for item in recovered}

    assert by_id["x"] == {"id": "x", "content": "leased remotely", "status": "pending"}
    assert by_id["y"] == {"id": "y", "content": "later write", "status": "completed"}
    assert client.rows["hermes-todo:x"]["status"] == "running"
    assert client.rows["hermes-todo:y"]["status"] == "partial"
    assert not store.degraded


def test_idempotent_row_keys_on_repeat_write():
    client = PlanProgressFakeClient()
    linkage = OperatingLinkage(plan_id="plan-1", task_id=None, session_id=None, executor_ref="hermes:forge")
    store = KynverTodoStore(client, linkage=linkage)

    store.write([{"id": "same", "content": "v1", "status": "pending"}], merge=False)
    store.write([{"id": "same", "content": "v2", "status": "completed"}], merge=True)

    assert client.rows["hermes-todo:same"]["title"] == "v2"
    assert client.rows["hermes-todo:same"]["status"] == "partial"


def test_project_todo_write_never_sets_running_status():
    client = MagicMock()
    client.get.return_value = {"items": []}
    linkage = OperatingLinkage(plan_id="p", task_id="t", session_id=None, executor_ref="hermes:forge")
    project_todo_write(
        client,
        linkage,
        [{"id": "1", "content": "x", "status": "in_progress"}],
        merge=False,
    )
    row_body = next(c.args[1] for c in client.post.call_args_list if "progress-rows" in c.args[0])
    assert row_body["rows"][0]["status"] == "in_progress"
    assert all(r["status"] != "running" for r in row_body["rows"])
