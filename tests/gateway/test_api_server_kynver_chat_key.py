"""X-Kynver-Chat-Key: remembered per session by the api_server, sent as ``chatKey`` by the todo sync."""

from types import SimpleNamespace

import pytest

from gateway.platforms import api_server_chat_key as ck
from plugins.memory.kynver.operating_config import OperatingLinkage
from plugins.memory.kynver.todo_session import session_call
from plugins.memory.kynver.todo_store import KynverTodoStore


@pytest.fixture(autouse=True)
def _clean():
    ck._keys.clear()
    yield
    ck._keys.clear()


class _Client:
    def __init__(self):
        self.bodies = []
        self.config = SimpleNamespace(enabled=True, api_url="https://k", slug="s")

    def post(self, path, body, **kwargs):
        self.bodies.append(body)
        return {"todos": [], "plan": None}


def _req(value):
    headers = {} if value is None else {ck.HEADER: value}
    return SimpleNamespace(headers=headers)


def test_valid_header_is_remembered_per_session():
    ck.remember_chat_key(_req("kynver-tab:abc123"), "sess-1")
    assert ck.chat_key_for_session("sess-1") == "kynver-tab:abc123"
    assert ck.chat_key_for_session("sess-2") is None


@pytest.mark.parametrize("bad", ["abc", "kynver-tab:", "other:abc", "kynver-tab:a\x00b",
                                 "kynver-tab:a\x7fb", "kynver-tab:" + "x" * 300, ""])
def test_bad_or_missing_header_is_ignored(bad):
    ck.remember_chat_key(_req(bad), "sess-1")
    ck.remember_chat_key(_req(None), "sess-1")
    assert ck.chat_key_for_session("sess-1") is None


def test_missing_header_keeps_an_earlier_key():
    ck.remember_chat_key(_req("kynver-tab:abc"), "s")
    ck.remember_chat_key(_req(None), "s")
    assert ck.chat_key_for_session("s") == "kynver-tab:abc"


def test_session_call_sends_chat_key_only_when_given():
    client = _Client()
    session_call(client, scope="abc", todos=[{"id": "1", "content": "x", "status": "pending"}],
                 chat_key="kynver-tab:abc")
    session_call(client, scope="abc", todos=[{"id": "1", "content": "x", "status": "pending"}])
    assert client.bodies[0]["chatKey"] == "kynver-tab:abc"
    assert "chatKey" not in client.bodies[1]


def test_store_write_carries_the_agent_chat_key():
    agent = SimpleNamespace(_kynver_chat_key=None)
    client = _Client()
    store = KynverTodoStore(client, linkage=OperatingLinkage("plan", None, None, "hermes"), scope=lambda: "abc", chat_key=lambda: getattr(agent, "_kynver_chat_key", None))
    item = [{"id": "1", "content": "x", "status": "pending"}]
    store.write(item)
    agent._kynver_chat_key = "kynver-tab:tab9"
    store.write(item)
    assert [("chatKey" in b) for b in client.bodies] == [False, True]
    assert client.bodies[1]["chatKey"] == "kynver-tab:tab9"


def _store_for(agent):
    from plugins.memory.kynver.integration import todo_chat_key

    client = _Client()
    store = KynverTodoStore(client, linkage=OperatingLinkage("plan", None, None, "hermes"), scope=lambda: "abc",
                            chat_key=lambda: todo_chat_key(agent))
    store.write([{"id": "1", "content": "x", "status": "pending"}])
    return client.bodies[0]


def test_gateway_runner_agent_without_attribute_uses_remembered_key_by_session_id():
    ck.remember_chat_key(_req("kynver-tab:tab7"), "sess-gw")
    agent = SimpleNamespace(session_id="sess-gw", _gateway_session_key="agent:main:api_server:dm:sess-gw")
    assert _store_for(agent)["chatKey"] == "kynver-tab:tab7"


def test_gateway_runner_agent_falls_back_to_id_in_session_key():
    ck.remember_chat_key(_req("kynver-tab:tab8"), "hdr-sess")
    agent = SimpleNamespace(session_id="internal-id", _gateway_session_key="agent:main:api_server:dm:hdr-sess")
    assert _store_for(agent)["chatKey"] == "kynver-tab:tab8"


def test_no_header_or_other_platform_sends_no_chat_key():
    ck.remember_chat_key(_req(None), "sess-gw")
    api = SimpleNamespace(session_id="sess-gw", _gateway_session_key="agent:main:api_server:dm:sess-gw")
    tg = SimpleNamespace(session_id="tg-1", _gateway_session_key="agent:main:telegram:dm:123")
    assert "chatKey" not in _store_for(api)
    assert "chatKey" not in _store_for(tg)


def test_attribute_still_wins():
    ck.remember_chat_key(_req("kynver-tab:other"), "s")
    agent = SimpleNamespace(session_id="s", _kynver_chat_key="kynver-tab:own")
    assert _store_for(agent)["chatKey"] == "kynver-tab:own"
