"""kynver_memory_write can edit an existing memory.

A stable key is an idempotency key on Kynver's ``/memory`` route, so changed content
under an existing key is an HTTP 409 by design. The tool must offer the audited
``/memory/correct`` path instead of an opaque failure (which led the agent to write
"-v2" duplicates while the stale memory stayed active).

Exercises the discovered provider and the real ``KynverAgentOSClient`` (urllib)
against a local stand-in for the Kynver API.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SLUG = "test-agent"


class _FakeKynver:
    """Minimal AgentOS memory API: keyed writes conflict when the content changes."""

    def __init__(self):
        self.requests = []
        self.memories = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, body))
                status, payload = fake.handle(self.path, body)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def handle(self, path, body):
        if path == f"/api/agent-os/{SLUG}/memory":
            key = body.get("slug")
            if key in self.memories and self.memories[key] != body["content"]:
                return 409, {"error": "Memory operation conflicts with existing state"}
            self.memories[key] = body["content"]
            return 201, {"ok": True, "slug": key}
        if path == f"/api/agent-os/{SLUG}/memory/correct":
            if body["targetSlug"] not in self.memories:
                return 400, {"error": f'Memory "{body["targetSlug"]}" not found.'}
            new_key = body.get("key") or f"{body['targetSlug']}-correction"
            self.memories[new_key] = body["content"]
            return 201, {"ok": True, "correction": {"slug": new_key}, "superseded": [body["targetSlug"]]}
        return 404, {"error": "not found"}


@pytest.fixture
def kynver(monkeypatch):
    from plugins.memory import load_memory_provider
    from plugins.memory.kynver.agentos_bridge import KynverAgentOSClient, KynverAgentOSConfig

    fake = _FakeKynver()
    thread = threading.Thread(target=fake.server.serve_forever, daemon=True)
    thread.start()
    discovered = load_memory_provider("kynver", register_skills=False)
    assert discovered is not None
    client = KynverAgentOSClient(KynverAgentOSConfig(api_url=fake.url, api_key="k", slug=SLUG))
    provider = type(discovered)(client=client)
    try:
        yield fake, provider
    finally:
        fake.server.shutdown()
        fake.server.server_close()


def test_keyed_rewrite_conflict_is_actionable_and_correction_supersedes(kynver):
    fake, provider = kynver
    first = json.loads(provider.handle_tool_call(
        "kynver_memory_write", {"content": "Deploys run on Fridays.", "key": "deploy-day"}
    ))
    assert first["success"] is True
    path, body = fake.requests[0]
    assert path == f"/api/agent-os/{SLUG}/memory"
    assert (body["slug"], body["memoryType"], body["sourceId"]) == ("deploy-day", "fact", "hermes:forge")

    conflict = json.loads(provider.handle_tool_call(
        "kynver_memory_write", {"content": "Deploys run on Tuesdays.", "key": "deploy-day"}
    ))
    assert "supersedes='deploy-day'" in conflict["error"]
    assert fake.memories["deploy-day"] == "Deploys run on Fridays."

    fixed = json.loads(provider.handle_tool_call(
        "kynver_memory_write",
        {"content": "Deploys run on Tuesdays.", "supersedes": "deploy-day", "reason": "Schedule moved."},
    ))
    assert fixed["success"] is True and fixed["corrected"] == "deploy-day"
    path, body = fake.requests[-1]
    assert path == f"/api/agent-os/{SLUG}/memory/correct"
    assert body == {
        "targetSlug": "deploy-day",
        "content": "Deploys run on Tuesdays.",
        "reason": "Schedule moved.",
        "sourceId": "hermes:forge",
    }


def test_correction_requires_reason_and_a_distinct_key(kynver):
    fake, provider = kynver
    no_reason = json.loads(provider.handle_tool_call(
        "kynver_memory_write", {"content": "x", "supersedes": "deploy-day"}
    ))
    same_key = json.loads(provider.handle_tool_call(
        "kynver_memory_write",
        {"content": "x", "supersedes": "deploy-day", "key": "deploy-day", "reason": "r"},
    ))
    assert "reason is required" in no_reason["error"]
    assert "must differ" in same_key["error"]
    assert fake.requests == []
