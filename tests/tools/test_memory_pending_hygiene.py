"""Pending memory queue hygiene (tools/memory_pending_hygiene.py), incl. a replay of the 103-record
backlog that background review staged between 21 and 31 Oct."""

import json
import time
from pathlib import Path

import pytest

from tools import memory_pending_hygiene as hy
from tools import write_approval as wa
from tools.memory_tool_store import ENTRY_DELIMITER

FIXTURE = Path(__file__).parent / "fixtures" / "pending_memory_1031.json"
DAY = 86400


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path / "memories", raising=False)
    (tmp_path / "memories").mkdir()
    return tmp_path


class FakeStore:
    def __init__(self, home, memory=(), user=()):
        self.home = home
        self.set(memory, user)

    def set(self, memory, user=()):
        (self.home / "memories" / "MEMORY.md").write_text(ENTRY_DELIMITER.join(memory), encoding="utf-8")
        (self.home / "memories" / "USER.md").write_text(ENTRY_DELIMITER.join(user), encoding="utf-8")

    def _path_for(self, target):
        return self.home / "memories" / ("USER.md" if target == "user" else "MEMORY.md")

    @staticmethod
    def _read_file(path):
        text = path.read_text(encoding="utf-8")
        return [e.strip() for e in text.split(ENTRY_DELIMITER) if e.strip()]


def replace_payload(entry, content, target="memory"):
    return {"action": "replace", "target": target, "content": content, "old_text": entry[:12],
            "matched_entry": entry}


def stage(store, payload, origin="background_review"):
    return hy.stage(store, payload, summary="s", origin=origin)


E1 = "Kynver Desktop (2026-10-01): Will chose MSIX packaging, build in apps/desktop with pnpm build:msix."
E2 = "state.db lives at ~/.hermes/state.db and holds 3 tables."


def test_new_proposal_supersedes_open_one_for_same_entry(home):
    store = FakeStore(home, [E1, E2])
    a = stage(store, replace_payload(E1, E1 + " Signed with cert X, valid until 2027-01-01."))
    b = stage(store, replace_payload(E1, E1 + " Signed with cert Y, expires 2028-02-02."))
    assert b["superseded"] == [a["record"]["id"]]
    assert [r["id"] for r in wa.list_pending("memory")] == [b["record"]["id"]]


def test_batch_overlapping_two_records_supersedes_both(home):
    store = FakeStore(home, [E1, E2])
    stage(store, replace_payload(E1, E1 + " Extra fact 42."))
    stage(store, replace_payload(E2, E2 + " Extra fact 43."))
    batch = {"action": "batch", "target": "memory", "operations": [
        replace_payload(E1, E1 + " Other fact 44."), replace_payload(E2, E2 + " Other fact 45.")]}
    stage(store, batch)
    assert len(wa.list_pending("memory")) == 1


def test_record_dropped_when_matched_entry_gone_or_changed(home):
    store = FakeStore(home, [E1, E2])
    stage(store, replace_payload(E1, E1 + " Fact 1."))
    stage(store, replace_payload(E2, E2 + " Fact 2."))
    stale_id = next(r["id"] for r in wa.list_pending("memory") if r["payload"]["matched_entry"] == E1)
    store.set([E1 + " (edited by hand)", E2])
    assert hy.prune(store) == {"entry_changed": [stale_id]}
    assert len(wa.list_pending("memory")) == 1


def test_records_expire_after_ttl(home, monkeypatch):
    store = FakeStore(home, [E1])
    stage(store, replace_payload(E1, E1 + " Fact 1."))
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 2 * DAY)
    assert hy.prune(store) == {}
    monkeypatch.setattr(time, "time", lambda: real + 4 * DAY)
    assert list(hy.prune(store)) == ["expired"] and wa.pending_count("memory") == 0


def test_cap_stops_proposing(home):
    entries = [f"Entry number {i} about thing{i} at /srv/x{i}" for i in range(8)]
    store = FakeStore(home, entries)
    results = [stage(store, replace_payload(e, e + f" New fact {i * 7 + 101}.")) for i, e in enumerate(entries)]
    assert wa.pending_count("memory") == 5
    assert [r["skipped"] for r in results[5:]] == ["cap"] * 3


def test_cap_does_not_block_superseding_existing_entry(home):
    entries = [f"Entry number {i} about thing{i} at /srv/x{i}" for i in range(6)]
    store = FakeStore(home, entries)
    for i, e in enumerate(entries[:5]):
        stage(store, replace_payload(e, e + f" New fact {i * 7 + 101}."))
    out = stage(store, replace_payload(entries[0], entries[0] + " Latest fact 99."))
    assert out["record"] and wa.pending_count("memory") == 5


def test_cosmetic_rewording_not_staged(home):
    store = FakeStore(home, [E1])
    reworded = "Will chose MSIX packaging for Kynver Desktop (2026-10-01); build in apps/desktop with pnpm build:msix."
    out = stage(store, replace_payload(E1, reworded))
    assert out["skipped"] == "cosmetic" and wa.pending_count("memory") == 0


def test_real_fact_change_is_staged(home):
    store = FakeStore(home, [E1])
    out = stage(store, replace_payload(E1, E1.replace("2026-10-01", "2026-10-15")))
    assert out["record"]


def test_remove_is_never_cosmetic(home):
    store = FakeStore(home, [E1])
    out = stage(store, {"action": "remove", "target": "memory", "old_text": "Kynver", "matched_entry": E1})
    assert out["record"]


def test_foreground_ignores_cap_and_cosmetic_but_still_supersedes(home):
    store = FakeStore(home, [E1])
    a = stage(store, replace_payload(E1, E1 + " Fact 5."))
    b = stage(store, replace_payload(E1, E1.upper()), origin="foreground")
    assert b["record"] and b["superseded"] == [a["record"]["id"]]


def test_skip_review_when_memory_unchanged_since_last_staged(home):
    store = FakeStore(home, [E1, E2])
    assert hy.should_skip_review(store) is None
    stage(store, replace_payload(E1, E1 + " Fact 3."))
    assert hy.should_skip_review(store) == "memory unchanged since last staged proposal"
    store.set([E1, E2, "A brand new entry"])
    assert hy.should_skip_review(store) is None


def test_skip_review_at_cap(home):
    entries = [f"Entry number {i} about thing{i} at /srv/x{i}" for i in range(6)]
    store = FakeStore(home, entries)
    for i, e in enumerate(entries[:5]):
        stage(store, replace_payload(e, e + f" New fact {i * 7 + 101}."))
    store.set(entries + ["new"])
    assert hy.should_skip_review(store) == "pending memory queue at cap"


def test_nag_at_most_once_a_day(home):
    t0 = 1_000_000.0
    assert hy.should_nag(now=t0) is True
    assert hy.should_nag(now=t0 + 3600) is False
    assert hy.should_nag(now=t0 + DAY + 1) is True


def test_state_file_not_counted_as_record(home):
    hy.should_nag(now=1.0)
    assert wa.pending_count("memory") == 0


def test_queue_health(home, monkeypatch):
    store = FakeStore(home, [E1])
    stage(store, replace_payload(E1, E1 + " Fact 9."))
    assert hy.queue_health()["ok"]
    h = hy.queue_health(now=time.time() + 5 * DAY)
    assert not h["ok"] and len(h["expired"]) == 1


def test_background_gate_message_has_total_and_nags_once(home, monkeypatch):
    from tools import memory_tool
    from tools.memory_tool import _background_delete_gate
    monkeypatch.setattr("tools.skill_provenance.is_unattended_review", lambda: True)
    monkeypatch.setattr(wa, "current_origin", lambda: "background_review")
    entries = [f"Entry number {i} about thing{i} at /srv/x{i}" for i in range(3)]
    store = FakeStore(home, entries)
    store.resolve_entry = lambda target, old, verb: {"success": True, "matched_entry": next(
        e for e in entries if old in e)}
    out = [json.loads(_background_delete_gate(store, "patch", None, content=f"x fact {i * 11}", old_text=f"number {i}"))
           for i in range(3)]
    assert [o["pending_total"] for o in out] == [1, 2, 3]
    assert [o["proposal_staged"] for o in out] == [True, False, False]
    assert "3 memory change(s)" in out[2]["message"]


def test_replay_103_record_backlog_collapses(home, monkeypatch):
    """Replay the real 21-31 Oct history in order; the queue must stay at the cap, not reach 103."""
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(records) == 103
    entries = sorted({e for r in records for e in hy.matched_entries(r["payload"])})
    store = FakeStore(home, entries)
    clock = [records[0]["created_at"]]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    peak, skipped = 0, {"cosmetic": 0, "cap": 0}
    for r in records:
        clock[0] = r["created_at"]
        out = hy.stage(store, r["payload"], summary=r["summary"], origin="background_review")
        if out["skipped"]:
            skipped[out["skipped"]] += 1
        peak = max(peak, wa.pending_count("memory"))
    assert peak <= 5
    assert wa.pending_count("memory") <= 5
    # Every surviving record targets a distinct entry set.
    seen = [e for r in wa.list_pending("memory") for e in hy.matched_entries(r["payload"])]
    assert len(seen) == len(set(seen))


def test_replay_without_cap_still_one_record_per_entry(home, monkeypatch):
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))
    entries = sorted({e for r in records for e in hy.matched_entries(r["payload"])})
    store = FakeStore(home, entries)
    monkeypatch.setattr(hy, "settings", lambda: {"ttl_days": 0, "max_open": 10_000, "nag_hours": 24})
    for r in records:
        hy.stage(store, r["payload"], summary=r["summary"], origin="background_review")
    seen = [e for rec in wa.list_pending("memory") for e in hy.matched_entries(rec["payload"])]
    assert len(seen) == len(set(seen))
    assert wa.pending_count("memory") <= len(entries) < 103
