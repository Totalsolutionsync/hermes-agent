"""Compaction fact flush (memory freshness F15/F16): when Hermes compresses context, the
departing turns' durable facts reach Kynver as ``hermes:forge:flush`` writes deduped at 0.92.

The provider is loaded through the real memory-provider discovery path with a fake HTTP
client; the auxiliary-model extraction is replaced by a stub so no network is touched.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from plugins.memory import load_memory_provider
from plugins.memory.kynver import compaction_flush as cf
from plugins.memory.kynver.agentos_bridge import KynverAgentOSError

RATE_LIMITED = KynverAgentOSError("Kynver AgentOS HTTP 429: over the daily flush cap", status=429, retry_after=600)
DOWN = KynverAgentOSError("Kynver AgentOS request failed: connection refused")

ITEMS = [
    {"kind": "decision", "text": "Will chose Railway over Vercel for Kynver production hosting.",
     "entities": ["Will", "Railway"], "confidence": 0.9, "fromToolOutput": False},
    {"kind": "state", "text": "PR #4866 on Totalsolutionsync/Kynver was squash-merged on 31 Oct 2026.",
     "entities": ["Kynver"], "confidence": 0.95, "fromToolOutput": True},
]


class FakeKynver:
    def __init__(self, *, observe_only=False):
        self.memory_posts = []
        self.memory_error = None
        self.memory_attempts = 0
        self.config = SimpleNamespace(
            enabled=True, observe_only=observe_only, memory_disabled=False, tasks_disabled=False,
            skills_disabled=True, session_sync_disabled=True, todo_mirror_disabled=False,
            side_effect_timeout=3.0, timeout=3.0,
        )

    def get(self, path, *, slug=None, timeout=None):
        return {"memories": []}

    def post(self, path, body, *, slug=None, timeout=None):
        if path != "/memory":
            return {}
        self.memory_attempts += 1
        if self.memory_error:
            raise self.memory_error
        self.memory_posts.append(body)
        return {"id": f"mem-{len(self.memory_posts)}"}

    def patch(self, path, body, *, slug=None, timeout=None):
        return {}


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("KYNVER_API_KEY", raising=False)
    return home


@pytest.fixture
def extracted(monkeypatch):
    """Stub the aux-model call; records each transcript it was given."""
    calls = []

    def fake_extract(transcript):
        calls.append(transcript)
        return [dict(item) for item in ITEMS]

    monkeypatch.setattr(cf, "extract_with_aux_llm", fake_extract)
    return calls


def _provider(client, session="s1", **kwargs):
    provider = load_memory_provider("kynver")
    assert provider is not None and provider.name == "kynver"
    provider._client = client
    provider._compaction_flush._extract = cf.extract_with_aux_llm  # bound at construction
    provider.initialize(session, platform="cli", **kwargs)
    return provider


def _settle(provider):
    provider._compaction_flush.wait_idle(5)


def _transcript(n=12):
    rows = [{"role": "system", "content": "You are Hermes."}]
    for i in range(n):
        rows.append({"role": "user", "content": f"Turn {i}: we are moving Kynver production to Railway, not Vercel. " * 2})
        rows.append({"role": "assistant", "content": f"Noted for turn {i}; Railway it is for production hosting."})
    return rows


def test_compression_posts_departing_facts_as_hermes_flush(hermes_home, extracted, monkeypatch):
    client = FakeKynver()
    provider = _provider(client)

    assert provider.on_pre_compress(_transcript()) == ""  # adds nothing to the summary prompt
    _settle(provider)

    assert len(extracted) == 1
    assert "You are Hermes." not in extracted[0]  # system prompt is not a departing turn
    assert "Turn 11" not in extracted[0]  # the protected tail survives compression
    assert len(client.memory_posts) == 2
    decision, state = client.memory_posts
    for body in (decision, state):
        assert body["sourceId"] == "hermes:forge:flush"
        assert body["dedupeAgainstSimilar"] == 0.92
        assert body["slug"].startswith("hermes-flush-")
        assert body["metadata"]["compactionFlush"] is True
        assert body["metadata"]["compaction"]["origin"] == "hermes"
    assert (decision["memoryType"], decision["confidence"]) == ("decision", "high")
    assert "reviewStatus" not in decision
    assert state["memoryType"] == "project_update" and state["metadata"]["volatile"] is True
    # Rests only on tool output: held for review, high confidence capped at medium.
    assert (state["reviewStatus"], state["confidence"]) == ("needs_review", "medium")
    assert state["metadata"]["hiddenFromRecall"] is True


def test_hook_returns_immediately_and_never_raises(hermes_home, monkeypatch):
    release = threading.Event()

    def slow_extract(_transcript):
        release.wait(5)
        raise RuntimeError("aux model exploded")

    client = FakeKynver()
    provider = _provider(client)
    provider._compaction_flush._extract = slow_extract

    started = time.monotonic()
    assert provider.on_pre_compress(_transcript()) == ""
    assert time.monotonic() - started < 0.5  # compression is not held by the flush
    release.set()
    _settle(provider)
    assert client.memory_posts == []
    assert provider._compaction_flush.stats["failed"] == 1

    # A broken flusher still cannot surface into compression.
    def boom(*_a, **_k):
        raise RuntimeError("scheduler broken")

    monkeypatch.setattr(provider._compaction_flush, "schedule", boom)
    assert provider.on_pre_compress(_transcript()) == ""


def test_rate_limit_spools_and_retries_after_cooldown(hermes_home):
    clock = [1000.0]
    client = FakeKynver()
    client.memory_error = RATE_LIMITED
    provider = _provider(client)
    flusher = provider._compaction_flush
    flusher._extract = lambda _t: [dict(i) for i in ITEMS]
    flusher._clock = lambda: clock[0]

    provider.on_pre_compress(_transcript())
    _settle(provider)

    assert client.memory_attempts == 1  # the 429 stops the batch; no retry loop
    assert len(flusher.pending()) == 2
    spool = hermes_home / "kynver" / "compaction_flush_queue.jsonl"
    assert len(spool.read_text().splitlines()) == 2

    client.memory_error = None
    flusher.drain_if_pending()  # still inside Retry-After: nothing sent
    _settle(provider)
    assert client.memory_posts == []

    clock[0] += 601
    flusher.drain_if_pending()
    _settle(provider)
    assert [b["sourceId"] for b in client.memory_posts] == ["hermes:forge:flush"] * 2
    assert flusher.pending() == [] and not spool.exists()


def test_spool_survives_restart_and_drains_at_session_start(hermes_home):
    client = FakeKynver()
    client.memory_error = DOWN
    provider = _provider(client)
    provider._compaction_flush._extract = lambda _t: [dict(ITEMS[0])]
    provider.on_pre_compress(_transcript())
    _settle(provider)
    assert len(provider._compaction_flush.pending()) == 1

    fresh = FakeKynver()
    restarted = _provider(fresh, session="s2")
    _settle(restarted)
    assert [b["content"] for b in fresh.memory_posts] == [ITEMS[0]["text"]]
    assert restarted._compaction_flush.pending() == []


def test_permanent_refusal_is_dropped_not_spooled(hermes_home):
    client = FakeKynver()
    client.memory_error = KynverAgentOSError("Kynver AgentOS HTTP 400: bad body", status=400)
    provider = _provider(client)
    provider._compaction_flush._extract = lambda _t: [dict(i) for i in ITEMS]
    provider.on_pre_compress(_transcript())
    _settle(provider)
    assert client.memory_attempts == 2
    assert provider._compaction_flush.pending() == []


def test_turns_already_flushed_are_not_sent_twice(hermes_home):
    seen = []
    client = FakeKynver()
    provider = _provider(client)
    provider._compaction_flush._extract = lambda t: seen.append(t) or []
    transcript = _transcript()
    provider.on_pre_compress(transcript)
    provider.on_pre_compress(transcript)  # same turns again (e.g. a retried compression)
    _settle(provider)
    assert len(seen) == 1


@pytest.mark.parametrize("agent_context", ["cron", "subagent"])
def test_cron_and_subagent_contexts_never_flush(hermes_home, agent_context):
    calls = []
    client = FakeKynver()
    provider = _provider(client, agent_context=agent_context)
    provider._compaction_flush._extract = lambda t: calls.append(t) or [dict(i) for i in ITEMS]
    provider.on_pre_compress(_transcript())
    _settle(provider)
    assert calls == [] and client.memory_posts == []


def test_observe_only_and_config_off_never_flush(hermes_home, monkeypatch):
    calls = []
    observe = _provider(FakeKynver(observe_only=True))
    observe._compaction_flush._extract = lambda t: calls.append(t) or []
    observe.on_pre_compress(_transcript())

    monkeypatch.setattr("plugins.memory.kynver.flush_enabled", lambda: False)
    off = _provider(FakeKynver())
    off._compaction_flush._extract = lambda t: calls.append(t) or []
    off.on_pre_compress(_transcript())
    _settle(observe)
    _settle(off)
    assert calls == []


def test_departing_lines_strip_recalled_memory_summaries_and_secrets():
    rows = [
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] earlier summary"},
        {"role": "user", "content": "Use Bearer abc.def.ghi for the call\n<memory-context>Annie runs ops.</memory-context>"},
        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        {"role": "tool", "content": "x" * 5_000},
        {"role": "user", "content": "tail message"},
    ]
    lines = [line for _fp, line in cf.departing_lines(rows, keep_tail=1)]
    joined = "\n".join(lines)
    assert "earlier summary" not in joined
    assert "Annie runs ops" not in joined
    assert "abc.def.ghi" not in joined and "[REDACTED]" in joined
    assert "Assistant: Done." in lines
    assert "tail message" not in joined
    tool_line = next(line for line in lines if line.startswith("Tool:"))
    assert len(tool_line) <= cf.TOOL_MAX_CHARS + 10


def test_parse_items_is_lenient_and_bounded():
    raw = """```json
    {"items": [
      {"kind": "FACT", "text": "Annie Lopez runs operations at Kynver.", "confidence": "0.8"},
      {"kind": "weird", "text": "Kynver production runs on Railway.", "confidence": 0.7},
      {"kind": "fact", "text": "low", "confidence": 0.9},
      {"kind": "fact", "text": "A guess the model is unsure about.", "confidence": 0.2},
      "not a row"
    ]}
    ```"""
    items = cf.parse_items(raw)
    assert [(i["kind"], i["text"]) for i in items] == [
        ("fact", "Annie Lopez runs operations at Kynver."),
        ("fact", "Kynver production runs on Railway."),
    ]
    many = '{"items": [' + ",".join(
        f'{{"kind":"fact","text":"Durable fact number {n} about Kynver.","confidence":0.9}}' for n in range(20)
    ) + "]}"
    assert len(cf.parse_items(many)) == cf.MAX_ITEMS
    assert cf.parse_items("not json") == []


def test_memory_manager_compression_hook_reaches_kynver(hermes_home):
    """The real core hook: MemoryManager.on_pre_compress (called by compress_dialog) → flush."""
    from agent.memory_manager import MemoryManager

    client = FakeKynver()
    provider = _provider(client)
    provider._compaction_flush._extract = lambda _t: [dict(ITEMS[0])]
    manager = MemoryManager()
    manager.add_provider(provider)

    assert manager.on_pre_compress(_transcript()) == ""
    _settle(provider)
    assert [b["sourceId"] for b in client.memory_posts] == ["hermes:forge:flush"]
