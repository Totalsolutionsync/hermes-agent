"""Automatic Kynver memory-quality feedback: failed/empty recall and chat corrections are
recorded without the agent having to remember, and an outage is recorded once Kynver is back.

The provider is loaded through the real memory-provider discovery path and initialized with a
fake HTTP client (the plugin's own transport seam).
"""

import json
from types import SimpleNamespace

import pytest

from plugins.memory import load_memory_provider
from plugins.memory.kynver.agentos_bridge import KynverAgentOSError

FEEDBACK = "/memory/quality-feedback"
RATE_LIMITED = KynverAgentOSError("Kynver AgentOS HTTP 429: rate-limited", status=429, retry_after=30)
DOWN = KynverAgentOSError("Kynver AgentOS request failed: connection refused")


class FakeKynver:
    """Memory reads answer ``read_result``; feedback posts raise ``feedback_error`` when set."""

    def __init__(self, *, observe_only=False):
        self.read_result = {"memories": []}
        self.feedback_error = None
        self.feedback = []
        self.feedback_attempts = 0
        self.config = SimpleNamespace(
            enabled=True, observe_only=observe_only, memory_disabled=False, tasks_disabled=False,
            skills_disabled=False, session_sync_disabled=True, todo_mirror_disabled=False,
            side_effect_timeout=3.0, timeout=3.0,
        )

    def get(self, path, *, slug=None, timeout=None):
        if isinstance(self.read_result, Exception):
            raise self.read_result
        return self.read_result

    def post(self, path, body, *, slug=None, timeout=None):
        if path != FEEDBACK:
            return {}
        self.feedback_attempts += 1
        if self.feedback_error:
            raise self.feedback_error
        self.feedback.append(body)
        return {"id": f"fb-{len(self.feedback)}"}

    def patch(self, path, body, *, slug=None, timeout=None):
        return {}


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("KYNVER_API_KEY", raising=False)
    return home


def _provider(client, session="s1"):
    provider = load_memory_provider("kynver")
    assert provider is not None and provider.name == "kynver"
    provider._client = client
    provider.initialize(session, platform="cli")
    return provider


def _settle(provider):
    provider.shutdown()


def test_rate_limited_prefetch_records_one_retrieval_miss(hermes_home):
    client = FakeKynver()
    client.read_result = RATE_LIMITED
    provider = _provider(client)
    query = "what did we decide about the Appleseed launch " + "x" * 300

    provider.on_turn_start(1, query)
    notice = provider.prefetch(query)
    _settle(provider)

    assert "unavailable this turn" in notice
    assert len(client.feedback) == 1
    event = client.feedback[0]
    assert (event["outcomeKind"], event["signal"], event["note"]) == ("retrieval_miss", "negative", "rate_limited")
    assert query.startswith(event["queryText"]) and len(event["queryText"]) < len(query)
    assert event["idempotencyKey"]


def test_empty_recall_is_a_miss_and_injected_recall_is_used_once_per_query(hermes_home):
    client = FakeKynver()
    provider = _provider(client)

    provider.on_turn_start(1, "who is Annie")
    assert provider.prefetch("who is Annie") == ""
    _settle(provider)
    assert [(e["outcomeKind"], e["note"]) for e in client.feedback] == [("retrieval_miss", "empty")]

    client.feedback.clear()
    client.read_result = {"memories": [{"id": "mem-7", "key": "annie", "content": "Annie runs ops."}]}
    for turn in (2, 3):
        provider.on_turn_start(turn, "who runs ops")
        provider.prefetch("who runs ops")
    _settle(provider)
    assert [(e["outcomeKind"], e["signal"], e.get("memoryIds")) for e in client.feedback] == [
        ("retrieval_used", "positive", ["mem-7"])
    ]


@pytest.mark.parametrize(
    "message",
    [
        "Appleseed was already approved, don't go backwards",
        "I already told you the deploy window is Friday",
        "you're Forge, you do the PR reviews",
        "You forgot that we moved the standup",
    ],
)
def test_user_correction_records_unclassified_human_correction(hermes_home, message):
    client = FakeKynver()
    provider = _provider(client)

    provider.on_turn_start(4, message)
    _settle(provider)

    assert len(client.feedback) == 1
    event = client.feedback[0]
    assert (event["outcomeKind"], event["signal"]) == ("human_correction", "negative")
    assert "correctionReasonClass" not in event
    assert message[:40] in event["note"]


@pytest.mark.parametrize("message", ["Can you draft the Appleseed launch email?", "Forge, open a PR for the parser"])
def test_ordinary_message_records_nothing(hermes_home, message):
    client = FakeKynver()
    provider = _provider(client)

    provider.on_turn_start(1, message)
    _settle(provider)

    assert client.feedback == []


def test_outage_is_buffered_on_disk_and_flushed_after_recovery(hermes_home):
    client = FakeKynver()
    client.read_result = RATE_LIMITED
    client.feedback_error = RATE_LIMITED
    provider = _provider(client)

    provider.on_turn_start(1, "who am I")
    provider.prefetch("who am I")
    _settle(provider)  # the 429 reply has set the cooldown before the next event
    provider.on_turn_start(2, "I already told you, you're Forge")
    _settle(provider)

    # The rate-limited post was tried once, not retried in a loop, and kept for later.
    assert client.feedback_attempts == 1
    assert client.feedback == []
    buffer_file = hermes_home / "kynver" / "quality_feedback_queue.jsonl"
    kinds = sorted(json.loads(line)["outcomeKind"] for line in buffer_file.read_text().splitlines())
    assert kinds == ["human_correction", "retrieval_miss"]

    # A new process for the same profile after Kynver recovers: the next successful call flushes.
    healthy = FakeKynver()
    healthy.read_result = {"memories": [{"id": "mem-1", "content": "You are Forge."}]}
    restarted = _provider(healthy, session="s2")
    restarted.on_turn_start(1, "identity")
    restarted.prefetch("identity")
    _settle(restarted)

    assert sorted(e["outcomeKind"] for e in healthy.feedback) == [
        "human_correction", "retrieval_miss", "retrieval_used"
    ]
    assert not buffer_file.exists()


def test_observe_only_and_config_opt_out_record_nothing(hermes_home):
    observer = FakeKynver(observe_only=True)
    provider = _provider(observer)
    provider.on_turn_start(1, "I already told you")
    provider.prefetch("anything")
    _settle(provider)
    assert observer.feedback == []

    (hermes_home / "config.yaml").write_text("kynver:\n  quality_feedback:\n    enabled: false\n")
    client = FakeKynver()
    provider = _provider(client)
    provider.on_turn_start(1, "I already told you")
    provider.prefetch("anything")
    _settle(provider)
    assert client.feedback == []
