from types import SimpleNamespace

import pytest

from plugins.memory.kynver.agentos_bridge import KynverAgentOSError
from plugins.memory.kynver.prefetch_guard import (
    DEFAULT_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS,
    PrefetchGuard,
    retry_after_seconds,
)

PATH = "/memory?q={q}&k=5&view=full&purpose=explicit_recall&surface=operator"
LIMITED = KynverAgentOSError(
    'Kynver AgentOS HTTP 429: {"error":"Capability is temporarily rate-limited — retry after 60s."}',
    status=429,
    retry_after=30,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _isolate_kynver_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("KYNVER_API_KEY", raising=False)


def test_retry_after_prefers_header_then_body_then_default():
    assert retry_after_seconds(LIMITED) == 30
    body_only = KynverAgentOSError("HTTP 429: retry after 45s.", status=429)
    assert retry_after_seconds(body_only) == 45
    assert retry_after_seconds(KynverAgentOSError("HTTP 429", status=429)) == DEFAULT_COOLDOWN_SECONDS
    assert retry_after_seconds(KynverAgentOSError("x", status=429, retry_after=99999)) == MAX_COOLDOWN_SECONDS


def test_non_429_failures_do_not_start_a_cooldown():
    guard = PrefetchGuard(clock=Clock())
    guard.note_failure(KynverAgentOSError("HTTP 500", status=500))
    guard.note_failure(RuntimeError("timeout"))
    assert guard.cooldown_remaining() == 0


def test_cache_is_fresh_for_a_minute_and_stale_for_ten():
    clock = Clock()
    guard = PrefetchGuard(clock=clock)
    guard.store("Annie  role", {"memories": []})
    assert guard.fresh("annie role") is not None
    clock.now += 61
    assert guard.fresh("annie role") is None
    assert guard.stale("annie role") is not None
    clock.now += 600
    assert guard.stale("annie role") is None


class LimitedClient:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.config = SimpleNamespace(
            enabled=True, observe_only=False, memory_disabled=False, tasks_disabled=False,
            skills_disabled=False, session_sync_disabled=False, todo_mirror_disabled=False,
            side_effect_timeout=3.0, timeout=3.0,
        )

    def get(self, path, *, slug=None, timeout=None):
        self.calls.append(path)
        if self.fail:
            raise LIMITED
        return {"memories": [{"content": f"Fact for {path}", "key": path}]}

    def post(self, path, body, *, slug=None, timeout=None):
        return {}


def _provider(client, clock):
    from plugins.memory.kynver import KynverMemoryProvider

    provider = KynverMemoryProvider(client=client)
    provider.initialize("session-1")
    provider._prefetch_guard = PrefetchGuard(clock=clock)
    return provider


def test_rate_limited_turn_says_so_and_the_next_turn_does_not_re_hit_the_limit():
    clock = Clock()
    client = LimitedClient()
    client.fail = True
    provider = _provider(client, clock)

    first = provider.prefetch("who is Annie")
    assert "unavailable this turn" in first
    assert "rate-limited" in first
    assert len(client.calls) == 1
    assert provider.is_authoritative_context() is False

    clock.now += 10
    second = provider.prefetch("who is Annie")
    assert "unavailable this turn" in second
    assert len(client.calls) == 1  # cooldown: no request

    clock.now += 21
    client.fail = False
    third = provider.prefetch("who is Annie")
    assert "Fact for" in third
    assert len(client.calls) == 2
    assert provider.is_authoritative_context() is True


def test_recent_result_is_served_during_a_cooldown_and_reuse_skips_the_request():
    clock = Clock()
    client = LimitedClient()
    provider = _provider(client, clock)

    assert "Fact for" in provider.prefetch("deploy status", session_id="a")
    assert "Fact for" in provider.prefetch("deploy status", session_id="b")
    assert len(client.calls) == 1  # fresh cache hit

    clock.now += 120
    client.fail = True
    served = provider.prefetch("deploy status", session_id="c")
    assert "Fact for" in served
    assert "recent result" in served
    assert len(client.calls) == 2

    clock.now += 5
    again = provider.prefetch("deploy status", session_id="d")
    assert "Fact for" in again
    assert len(client.calls) == 2  # still cooling down
