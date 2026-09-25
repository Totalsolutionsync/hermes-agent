"""todo_tool against KynverTodoStore over real HTTP to a fake AgentOS.

Regression: ``todo_list`` failed with ``'KynverTodoStore' object has no attribute 'snapshot'``
because the tool (and AIAgent history hydration) call the generic store contract.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plugins.memory.kynver.agentos_bridge import KynverAgentOSClient, KynverAgentOSConfig
from plugins.memory.kynver.operating_config import OperatingLinkage
from plugins.memory.kynver.todo_store import KynverTodoStore
from tools.todo_tool import todo_tool

_PLAN = "/api/agent-os/forge/plans/plan-1"


class _FakeAgentOS(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.rows: dict[str, dict] = {}
        self.focus_key: str | None = None
        self.down = False
        self.writes = 0


class _Handler(BaseHTTPRequestHandler):
    server: _FakeAgentOS

    def log_message(self, *_args):
        pass

    def _reply(self, code: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.server.down:
            return self._reply(503, {"error": "unavailable"})
        if self.path == f"{_PLAN}/progress-rows":
            return self._reply(200, {"items": list(self.server.rows.values())})
        if self.path == _PLAN:
            return self._reply(200, {"plan": {"id": "plan-1", "inProgressRowKey": self.server.focus_key}})
        self._reply(404, {"error": self.path})

    def do_POST(self):
        if self.server.down:
            return self._reply(503, {"error": "unavailable"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.writes += 1
        if self.path == f"{_PLAN}/progress-rows":
            for row in body["rows"]:
                self.server.rows[row["rowKey"]] = dict(row)
        elif self.path == f"{_PLAN}/progress-focus":
            self.server.focus_key = body.get("rowKey")
        self._reply(200, {"ok": True})


@pytest.fixture
def agentos():
    server = _FakeAgentOS()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _store(server: _FakeAgentOS, clock=lambda: 0.0) -> KynverTodoStore:
    client = KynverAgentOSClient(KynverAgentOSConfig(
        api_url=f"http://127.0.0.1:{server.server_address[1]}", api_key="test-key", slug="forge"))
    linkage = OperatingLinkage(plan_id="plan-1", task_id="task-1", session_id=None, executor_ref="hermes:forge")
    return KynverTodoStore(client, linkage=linkage, retry_interval_seconds=10, clock=clock)


def _call(store, **kwargs) -> dict:
    result = json.loads(todo_tool(store=store, **kwargs))
    assert "error" not in result, result
    assert result["revision"] == store.snapshot()["revision"]
    assert result["todos"] == store.snapshot()["todos"]
    return result


def test_todo_tool_read_write_merge_and_restore_through_kynver(agentos):
    store = _store(agentos)
    assert _call(store)["todos"] == []

    written = _call(store, todos=[{"id": "a", "content": "Step A", "status": "in_progress"},
                                  {"id": "b", "content": "Step B", "status": "pending"}])
    assert agentos.rows["hermes-todo:a"]["status"] == "in_progress"
    merged = _call(store, todos=[{"id": "a", "status": "completed"}], merge=True)
    assert {i["id"]: (i["content"], i["status"]) for i in merged["todos"]} == {
        "a": ("Step A", "completed"), "b": ("Step B", "pending")}
    assert agentos.rows["hermes-todo:a"]["status"] == "partial"
    assert merged["revision"] > written["revision"] > 0
    assert _call(store)["revision"] == merged["revision"]  # a read of unchanged state is not a write

    # History hydration into a fresh session store adopts the revision and writes nothing remote.
    fresh = _store(agentos)
    writes_before = agentos.writes
    fresh.restore(merged["todos"], revision=merged["revision"])
    assert fresh.snapshot()["revision"] == merged["revision"]
    assert agentos.writes == writes_before
    assert _call(fresh, todos=[{"id": "b", "status": "in_progress"}], merge=True)["revision"] > merged["revision"]


def test_revision_stays_monotonic_through_degraded_and_recovery(agentos):
    now = [0.0]
    store = _store(agentos, clock=lambda: now[0])
    revisions = [_call(store, todos=[{"id": "a", "content": "online", "status": "pending"}])["revision"]]

    agentos.down = True
    degraded = _call(store, todos=[{"id": "a", "content": "offline edit", "status": "in_progress"}], merge=True)
    assert store.degraded
    assert degraded["todos"][0]["content"] == "offline edit"
    revisions.append(degraded["revision"])
    revisions.append(_call(store)["revision"])

    agentos.down = False
    now[0] = 10.0
    recovered = _call(store)
    assert not store.degraded
    assert agentos.rows["hermes-todo:a"]["title"] == "offline edit"
    revisions.append(recovered["revision"])
    revisions.append(_call(store, todos=[{"id": "a", "status": "completed"}], merge=True)["revision"])

    assert revisions == sorted(revisions)
    assert revisions[1] > revisions[0] and revisions[-1] > revisions[-2]
