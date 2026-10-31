"""Kynver skill sync: H1 refresher, H2 outbox/drain/seam, H3 usage reporter.

The server is a FakeKynver that follows the S1 contracts (manifest ETag/304,
cursor pull, push with baseHash CAS + onConflict propose + status actions,
usage ingest, proposals list). No test touches the network.
"""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.memory.kynver.skill_sync import drain as drain_mod
from plugins.memory.kynver.skill_sync import outbox, refresher, service, usage
from plugins.memory.kynver.skill_sync.bundle import (
    Bundle,
    compute_content_hash,
    read_bundle,
    sha256_hex,
)
from plugins.memory.kynver.skill_sync.store import (
    get_base,
    load_state,
    locate_skill_dir,
    read_sidecar,
)

MD = "---\nname: {n}\ndescription: Fixture {n}.\n---\n# {n}\n\n{body}\n"


def md(name, body="body"):
    return MD.format(n=name, body=body)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    (h / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def write_local(home, name, body="body", files=None):
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(md(name, body), encoding="utf-8")
    for rel, content in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(content, encoding="utf-8")
    return d


class FakeKynver:
    def __init__(self):
        self.config = SimpleNamespace(enabled=True)
        self.skills = {}  # slug -> dict(skillMd, files{}, status, writer, seq)
        self.seq = 0
        self.calls = []
        self.down = False
        self.propose = True
        self.proposals = []
        self.usage_ids = set()
        self.reject = {}  # slug -> error text

    def _row(self, slug):
        s = self.skills[slug]
        return {"slug": slug, "name": slug, "contentHash": compute_content_hash(s["skillMd"], s["files"]),
                "skillMd": s["skillMd"], "status": s["status"], "visibility": "private", "category": "general",
                "files": [{"path": p, "content": c, "sha256": sha256_hex(c), "byteSize": len(c)}
                          for p, c in sorted(s["files"].items())],
                "updatedAt": "t", "updatedBy": s["writer"]}

    def server_write(self, slug, body, files=None, status="active", writer="kynver-chat"):
        self.seq += 1
        self.skills[slug] = {"skillMd": md(slug, body), "files": dict(files or {}), "status": status,
                             "writer": writer, "seq": self.seq}

    def _etag(self):
        return '"ws:{}"'.format(hash(tuple(sorted((k, v["seq"], v["status"]) for k, v in self.skills.items()))))

    def request_raw(self, method, path, body=None, *, headers=None, timeout=None, slug=None):
        self.calls.append((method, path, body, dict(headers or {}), timeout))
        if self.down:
            raise RuntimeError("connection refused")
        base, _, qs = path.partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if base == "/skills/sync/manifest":
            tag = self._etag()
            if (headers or {}).get("If-None-Match") == tag:
                return 304, {"etag": tag}, None
            return 200, {"etag": tag}, {"skills": []}
        if base == "/skills/sync/pull":
            src = q.get("sourceId")
            after = int(q["cursor"].split("-")[1]) if q.get("cursor") else 0
            rows = sorted(((v["seq"], k) for k, v in self.skills.items()
                           if v["seq"] > after and v["writer"] != src))
            page, more = rows[:2], len(rows) > 2
            last = page[-1][0] if page else after
            return 200, {}, {"skills": [self._row(k) for _, k in page], "hasMore": more,
                             "nextCursor": f"c-{last}"}
        if base == "/skills/sync/push":
            return 200, {}, {"results": [self._push_one(body["sourceId"], it, body.get("onConflict"))
                                         for it in body["skills"]]}
        if base == "/skills/usage":
            acc = dup = unk = 0
            for ev in body["events"]:
                if ev["slug"] not in self.skills:
                    unk += 1
                elif ev["clientEventId"] in self.usage_ids:
                    dup += 1
                else:
                    self.usage_ids.add(ev["clientEventId"])
                    acc += 1
            return 200, {}, {"accepted": acc, "duplicates": dup, "unknownSlug": unk, "invalid": 0}
        if base == "/skills/proposals":
            return 200, {}, {"proposals": self.proposals}
        raise AssertionError(path)

    def _push_one(self, src, it, on_conflict):
        slug = it["slug"]
        if slug in self.reject:
            return {"slug": slug, "outcome": "rejected", "errors": [self.reject[slug]]}
        cur = self.skills.get(slug)
        cur_hash = compute_content_hash(cur["skillMd"], cur["files"]) if cur else None
        if it.get("action") in ("archive", "delete", "restore"):
            if not cur:
                return {"slug": slug, "outcome": "rejected", "errors": ["status changes apply to existing skills only"]}
            if it.get("baseHash") != cur_hash:
                return {"slug": slug, "outcome": "conflict", "serverHash": cur_hash}
            cur["status"] = "active" if it["action"] == "restore" else "archived"
            self.seq += 1
            cur.update(seq=self.seq, writer=src)
            return {"slug": slug, "outcome": "updated", "contentHash": cur_hash, "status": cur["status"]}
        files = {f["path"]: f["content"] for f in it.get("files", [])}
        new_hash = compute_content_hash(it["skillMd"], files)
        if cur is None:
            self.server_write(slug, "x", files, writer=src)
            self.skills[slug]["skillMd"] = it["skillMd"]
            return {"slug": slug, "outcome": "created", "contentHash": new_hash}
        if cur_hash == new_hash:
            return {"slug": slug, "outcome": "unchanged", "contentHash": new_hash}
        if it.get("baseHash") != cur_hash:
            if on_conflict == "propose" and self.propose:
                pid = f"p{len(self.proposals) + 1}"
                self.proposals.append({"id": pid, "status": "proposed"})
                return {"slug": slug, "outcome": "proposed", "proposalId": pid, "serverHash": cur_hash}
            return {"slug": slug, "outcome": "conflict", "serverHash": cur_hash}
        self.seq += 1
        cur.update(skillMd=it["skillMd"], files=files, seq=self.seq, writer=src)
        return {"slug": slug, "outcome": "updated", "contentHash": new_hash}


def pull_item(slug, body="srv", files=None, status="active"):
    k = FakeKynver()
    k.server_write(slug, body, files, status)
    return k._row(slug)


# ---------------------------------------------------------------- bundle / hash

def test_hash_matches_kynver_algorithm():
    # Vector produced by Kynver's computeSkillContentHash (skill-bundle.hash.ts).
    got = compute_content_hash("---\nname: a\n---\nbody é\r\n", {"references/b.md": "B", "assets/a.txt": "A"})
    assert got == "2b66474a88e742eb815627a6a798b600378f361fe2cee677af4afc741ab6c4f2"


def test_read_bundle_skips_binary_symlink_and_dotfiles(home):
    d = write_local(home, "alpha", files={"references/n.md": "N", "references/.hid": "x"})
    (d / "assets").mkdir()
    (d / "assets" / "pic.bin").write_bytes(b"\xff\xfe\x00")
    (d / "references" / "link").symlink_to(d / "SKILL.md")
    (d / ".kynver.json").write_text("{}")
    assert sorted(read_bundle(d).files) == ["references/n.md"]


# ---------------------------------------------------------------------- outbox

def test_outbox_coalesces_and_is_bounded_by_skill_count(home):
    for _ in range(10):
        for i in range(50):
            outbox.mark_dirty(f"skill-{i}", "upsert", f"base-{i}")
    es = outbox.entries()
    assert len(es) == 50
    assert all(e["baseHash"].startswith("base-") for e in es)  # first base kept
    assert outbox.stats()["depth"] == 50
    assert oct((home / "state" / "kynver-skill-outbox.jsonl").stat().st_mode & 0o777) == "0o600"


def test_outbox_survives_torn_line_and_parks_after_three_rejects(home):
    outbox.mark_dirty("a-skill", "upsert", None)
    with open(home / "state" / "kynver-skill-outbox.jsonl", "a") as fh:
        fh.write('{"slug": "torn\n')
    assert [e["slug"] for e in outbox.entries()] == ["a-skill"]
    assert not outbox.record_reject("a-skill", "too big", now=0)
    assert not outbox.record_reject("a-skill", "too big", now=0)
    assert outbox.record_reject("a-skill", "too big", now=0)
    assert outbox.due(now=10**9) == []
    assert outbox.stats()["parked"] == 1
    outbox.mark_dirty("a-skill", "upsert", None)  # a fresh edit revives it
    assert outbox.stats()["parked"] == 0


def test_backoff_is_capped_and_jittered():
    vals = [outbox.backoff_seconds(n) for n in (1, 5, 30, 100)]
    assert vals[0] < vals[1] <= outbox.BACKOFF_CAP
    assert all(v <= outbox.BACKOFF_CAP for v in vals)


# --------------------------------------------------------------- seam + drain

@pytest.fixture
def svc(home):
    k = FakeKynver()
    cfg = {"cache_first": True, "write_through": True, "report_usage": True, "initial_pull": True,
           "refresh_interval_seconds": 600, "source_id": "hermes:forge"}
    return service.SkillSyncService(k, cfg), k


@pytest.fixture
def hooks_live(svc, monkeypatch):
    """Route Hermes' plugin hooks to the skill-sync hook functions (what register() wires up)."""
    from hermes_cli import lifecycle
    s, _ = svc
    table = {"on_skill_lifecycle": service.on_skill_lifecycle, "post_tool_call": service.on_post_tool_call}
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name in table)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kw: [table[name](**kw)] if name in table else [])
    monkeypatch.setattr(service, "_service", s)
    return svc


def test_skill_manage_marks_dirty_without_network(home, hooks_live, monkeypatch):
    _, k = hooks_live
    from tools.skill_manager_tool import skill_manage

    def boom(*a, **kw):
        raise AssertionError("network on the turn path")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    res = json.loads(skill_manage("create", "seam-skill", content=md("seam-skill")))
    assert res["success"], res
    res = json.loads(skill_manage("patch", "seam-skill", old_string="body", new_string="body2"))
    assert res["success"], res
    assert k.calls == []
    es = outbox.entries()
    assert [(e["slug"], e["kind"], e["baseHash"]) for e in es] == [("seam-skill", "upsert", None)]


def test_lifecycle_hook_mapping(home, hooks_live):
    service.on_skill_lifecycle(action="archived", skill_name="gone_skill")
    service.on_skill_lifecycle(action="installed", skill_name="hub-skill")  # hub installs are not ours
    service.on_skill_lifecycle(action="stale", skill_name="old-skill")
    kinds = {e["slug"]: e["kind"] for e in outbox.entries()}
    assert kinds == {"gone-skill": "archive"}
    service.on_skill_lifecycle(action="restored", skill_name="gone_skill")
    assert {e["slug"]: e["kind"] for e in outbox.entries()}["gone-skill"] == "restore"


def test_hard_delete_via_post_tool_call(home, hooks_live):
    service.on_post_tool_call(tool_name="skill_manage", args={"action": "delete", "name": "doomed"},
                              result=json.dumps({"success": True, "message": "deleted"}))
    service.on_post_tool_call(tool_name="skill_manage", args={"action": "delete", "name": "kept"},
                              result=json.dumps({"success": False}))
    service.on_post_tool_call(tool_name="skill_manage", args={"action": "delete", "name": "curated"},
                              result=json.dumps({"success": True, "_archived": True}))
    service.on_post_tool_call(tool_name="terminal", args={"action": "delete", "name": "x"}, result="{}")
    assert [(e["slug"], e["kind"]) for e in outbox.entries()] == [("doomed", "archive")]


def test_suppressed_events_do_not_echo(home, hooks_live):
    with service.suppress_events():
        service.on_skill_lifecycle(action="archived", skill_name="pulled-away")
    assert outbox.entries() == []


def test_hooks_are_noops_before_start(home, monkeypatch):
    monkeypatch.setattr(service, "_service", None)
    service.on_skill_lifecycle(action="created", skill_name="early")
    assert outbox.entries() == []


def test_register_skill_sync_hooks_uses_standard_hooks():
    seen = []

    class Ctx:
        def register_hook(self, name, cb):
            seen.append(name)
    service.register_skill_sync_hooks(Ctx())
    assert seen == ["on_skill_lifecycle", "post_tool_call"]


def test_drain_creates_then_updates_with_base_hash(home, svc):
    s, k = svc
    d = write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    out = s.drainer.drain_once()
    assert out["written"] == 1 and outbox.entries() == []
    h = read_bundle(d).content_hash
    assert read_sidecar(d)["baseHash"] == h == get_base("alpha")
    # edit, push again: baseHash is the previous server hash, never force
    (d / "SKILL.md").write_text(md("alpha", "changed"), encoding="utf-8")
    outbox.mark_dirty("alpha", "upsert", get_base("alpha"))
    s.drainer.drain_once()
    push = [c for c in k.calls if c[1] == "/skills/sync/push"][-1][2]
    assert push["onConflict"] == "propose" and "force" not in push["skills"][0]
    assert push["skills"][0]["baseHash"] == h
    assert push["outbox"]["depth"] == 1
    assert k.skills["alpha"]["skillMd"].endswith("changed\n")


def test_unchanged_since_base_is_dropped_without_push(home, svc):
    s, k = svc
    d = write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", read_bundle(d).content_hash)
    out = s.drainer.drain_once()
    assert out["dropped"] == 1 and k.calls == []


def test_outage_keeps_every_edit_and_drains_after(home, svc):
    s, k = svc
    k.down = True
    for i in range(30):
        write_local(home, f"s{i:02d}")
        for _ in range(7):  # a week of edits
            outbox.mark_dirty(f"s{i:02d}", "upsert", None, now=1000.0)
    out = s.drainer.drain_once(now=1000.0)
    assert out["offline"] and len(outbox.entries()) == 30
    assert all(e["fails"] == 1 and not e["parked"] for e in outbox.entries())
    k.down = False
    out = s.drainer.drain_once(now=1000.0 + 20 * 60)
    assert out["written"] == 30 and outbox.entries() == []
    assert len(k.skills) == 30


def test_rejected_entry_parks_and_queue_moves_on(home, svc):
    s, k = svc
    write_local(home, "bad-one")
    write_local(home, "good-one")
    k.reject["bad-one"] = "core over 12 KB"
    outbox.mark_dirty("bad-one", "upsert", None, now=0)
    outbox.mark_dirty("good-one", "upsert", None, now=0)
    for t in (0, 10**4, 2 * 10**4):
        s.drainer.drain_once(now=t)
    assert [e["slug"] for e in outbox.entries()] == ["bad-one"]
    assert outbox.entries()[0]["parked"] == "rejected"
    assert "good-one" in k.skills


def test_conflict_becomes_proposal_and_local_edit_is_kept(home, svc):
    s, k = svc
    d = write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    s.drainer.drain_once()
    base = get_base("alpha")
    k.server_write("alpha", "kynver edit")  # someone edits in Kynver
    (d / "SKILL.md").write_text(md("alpha", "forge edit"), encoding="utf-8")
    outbox.mark_dirty("alpha", "upsert", base)
    out = s.drainer.drain_once()
    assert out["proposed"] == 1 and outbox.entries() == []
    assert "forge edit" in (d / "SKILL.md").read_text()
    assert read_sidecar(d)["pending"]["state"] == "proposal"
    assert "kynver edit" in k.skills["alpha"]["skillMd"]  # server untouched


def test_conflict_with_proposals_off_parks_and_marks_pending(home, svc):
    s, k = svc
    k.propose = False
    d = write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    s.drainer.drain_once()
    base = get_base("alpha")
    k.server_write("alpha", "srv")
    (d / "SKILL.md").write_text(md("alpha", "mine"), encoding="utf-8")
    outbox.mark_dirty("alpha", "upsert", base)
    out = s.drainer.drain_once()
    assert out["conflict"] == 1 and outbox.stats()["parked"] == 1
    assert read_sidecar(d)["pending"]["state"] == "conflict"


def test_delete_is_an_archive_push_never_a_hard_delete(home, svc):
    s, k = svc
    d = write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    s.drainer.drain_once()
    import shutil
    shutil.rmtree(d)
    s.on_skill_event("write", "alpha", {"action": "delete"})
    s.drainer.drain_once()
    assert k.skills["alpha"]["status"] == "archived"
    assert [c[2]["skills"][0].get("action") for c in k.calls if c[1] == "/skills/sync/push"][-1] == "archive"


def test_edit_during_push_is_not_lost(home, svc, monkeypatch):
    s, k = svc
    d = write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    real = k.request_raw

    def racing(method, path, body=None, **kw):
        res = real(method, path, body, **kw)
        if path == "/skills/sync/push":
            (d / "SKILL.md").write_text(md("alpha", "edited mid-push"), encoding="utf-8")
        return res

    monkeypatch.setattr(k, "request_raw", racing)
    s.drainer.drain_once()
    (e,) = outbox.entries()
    assert e["slug"] == "alpha" and e["baseHash"] == get_base("alpha")
    monkeypatch.setattr(k, "request_raw", real)
    s.drainer.drain_once()
    assert outbox.entries() == [] and "edited mid-push" in k.skills["alpha"]["skillMd"]


def test_seam_inactive_when_write_through_off(home):
    cfg = {"cache_first": False, "write_through": False, "report_usage": False, "initial_pull": False,
           "refresh_interval_seconds": 600, "source_id": "hermes:forge"}
    s = service.SkillSyncService(FakeKynver(), cfg)
    s.on_skill_event("write", "alpha", {"action": "edit"})
    s.on_skill_event("view", "alpha", {})
    assert outbox.entries() == [] and usage.buffered() == 0


# ------------------------------------------------------------------- refresher

def make_refresher(k, **kw):
    return refresher.Refresher(k, "hermes:forge", **kw)


def test_first_pull_waits_for_migration_unless_allowed(home):
    k = FakeKynver()
    k.server_write("srv-only", "x")
    r = make_refresher(k)
    assert r.refresh_once()["status"] == "awaiting_initial_migration"
    assert k.calls == [] and locate_skill_dir("srv-only") is None
    r = make_refresher(k, allow_initial_pull=True)
    assert r.refresh_once()["applied"] == 1
    assert locate_skill_dir("srv-only") is not None


def test_refresh_applies_pages_then_304s_with_etag_and_timeouts(home):
    k = FakeKynver()
    for i in range(5):
        k.server_write(f"srv-{i}", f"b{i}", {"references/r.md": f"ref{i}"})
    r = make_refresher(k, allow_initial_pull=True)
    out = r.refresh_once()
    assert out["applied"] == 5
    d = locate_skill_dir("srv-3")
    assert (d / "references" / "r.md").read_text() == "ref3"
    assert read_sidecar(d)["baseHash"] == read_bundle(d).content_hash == get_base("srv-3")
    assert load_state()["cursor"] == "c-5"
    n = len(k.calls)
    assert r.refresh_once()["status"] == "not_modified"
    assert len(k.calls) == n + 1  # idle refresh = one conditional manifest call
    assert k.calls[-1][3]["If-None-Match"] and all(c[4] == 5.0 for c in k.calls)
    assert outbox.entries() == []  # nothing pulled is marked dirty (no echo)


def test_unreachable_kynver_keeps_cache_and_does_not_raise(home):
    k = FakeKynver()
    k.server_write("srv", "x")
    r = make_refresher(k, allow_initial_pull=True)
    r.refresh_once()
    before = (locate_skill_dir("srv") / "SKILL.md").read_text()
    k.down = True
    assert r.refresh_once()["status"] == "error"
    assert (locate_skill_dir("srv") / "SKILL.md").read_text() == before


def test_resume_after_kynver_edit_updates_cache(home):
    k = FakeKynver()
    k.server_write("srv", "v1")
    r = make_refresher(k, allow_initial_pull=True)
    r.refresh_once()
    k.server_write("srv", "v2")
    assert r.refresh_once()["applied"] == 1
    assert "v2" in (locate_skill_dir("srv") / "SKILL.md").read_text()
    assert len(list((home / "state" / "kynver-skill-backups" / "srv").iterdir())) == 1


@pytest.mark.parametrize("mutate,why", [
    (lambda i: i.update(contentHash="0" * 64), "contentHash"),
    (lambda i: i["files"].append({"path": "../evil.md", "content": "x", "sha256": sha256_hex("x")}), "traversal"),
    (lambda i: i["files"].append({"path": "/etc/x", "content": "x", "sha256": sha256_hex("x")}), "absolute"),
    (lambda i: i["files"][0].update(sha256="1" * 64), "sha256"),
    (lambda i: i.update(slug="Bad Slug"), "slug"),
])
def test_poisoned_pull_payload_is_rejected_old_copy_intact(home, mutate, why):
    write_local(home, "srv", body="keep me")
    refresher.write_sidecar(locate_skill_dir("srv"), {"baseHash": read_bundle(locate_skill_dir("srv")).content_hash})
    item = pull_item("srv", "evil", {"references/r.md": "r"})
    mutate(item)
    assert refresher.apply_pulled(item) == "rejected"
    assert "keep me" in (locate_skill_dir("srv") / "SKILL.md").read_text()
    assert load_state()["rejections"]


def test_secret_in_pulled_skill_is_rejected(home):
    k = FakeKynver()
    k.server_write("srv", "key sk-" + "a" * 40)
    assert refresher.apply_pulled(k._row("srv")) == "rejected"
    assert locate_skill_dir("srv") is None


def test_rejections_ride_on_the_next_push(home, svc):
    s, k = svc
    k.server_write("srv", "key sk-" + "b" * 40)
    refresher.apply_pulled(k._row("srv"))
    write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    s.drainer.drain_once()
    body = [c for c in k.calls if c[1] == "/skills/sync/push"][-1][2]
    assert body["pullRejections"][0]["slug"] == "srv"
    assert load_state()["rejections"] == []


def test_local_pending_edit_is_never_overwritten(home, svc):
    s, k = svc
    d = write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    s.drainer.drain_once()
    base = get_base("alpha")
    (d / "SKILL.md").write_text(md("alpha", "local edit"), encoding="utf-8")
    outbox.mark_dirty("alpha", "upsert", base)  # not pushed yet
    k.server_write("alpha", "server edit")
    assert refresher.apply_pulled(k._row("alpha")) == "pending"
    assert "local edit" in (d / "SKILL.md").read_text()
    assert json.loads((d / ".kynver.server.json").read_text())["skillMd"].endswith("server edit\n")
    assert read_sidecar(d)["pending"]


def test_unsynced_local_copy_that_differs_is_protected_not_overwritten(home):
    d = write_local(home, "legacy", body="old local")  # no sidecar: pre-H7 skill
    assert refresher.apply_pulled(pull_item("legacy", "server")) == "pending"
    assert "old local" in (d / "SKILL.md").read_text()


def test_identical_unsynced_copy_just_records_base(home):
    d = write_local(home, "same", body="same")
    item = {"slug": "same", "contentHash": read_bundle(d).content_hash, "skillMd": (d / "SKILL.md").read_text(),
            "files": [], "status": "active"}
    assert refresher.apply_pulled(item) == "unchanged"
    assert get_base("same") == item["contentHash"]


def test_archived_pull_moves_folder_to_archive_and_restore_brings_it_back(home):
    k = FakeKynver()
    k.server_write("srv", "v1")
    refresher.apply_pulled(k._row("srv"))
    k.server_write("srv", "v1", status="archived")
    # same content but archived: sidecar base == local == server
    assert refresher.apply_pulled(k._row("srv")) == "archived"
    assert locate_skill_dir("srv") is None and (home / "skills" / ".archive" / "srv" / "SKILL.md").exists()
    k.server_write("srv", "v1", status="active")
    assert refresher.apply_pulled(k._row("srv")) == "applied"
    assert locate_skill_dir("srv") is not None


def test_server_only_archived_skill_is_parked_in_archive_not_indexed(home):
    assert refresher.apply_pulled(pull_item("gone", status="archived")) == "archived"
    assert locate_skill_dir("gone") is None
    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
    clear_skills_system_prompt_cache(clear_snapshot=True)
    assert "gone" not in build_skills_system_prompt()


def test_apply_keeps_binary_assets_and_drops_removed_text_files(home):
    d = write_local(home, "alpha", files={"references/old.md": "old"})
    (d / "assets").mkdir()
    (d / "assets" / "pic.bin").write_bytes(b"\xff\x00\xfe")
    h = read_bundle(d).content_hash
    refresher.write_sidecar(d, {"baseHash": h})
    refresher.apply_pulled(pull_item("alpha", "new", {"references/new.md": "n"}))
    d = locate_skill_dir("alpha")
    assert (d / "references" / "new.md").exists() and not (d / "references" / "old.md").exists()
    assert (d / "assets" / "pic.bin").read_bytes() == b"\xff\x00\xfe"


def test_crash_between_move_and_install_is_recovered(home, monkeypatch):
    d = write_local(home, "alpha", body="precious")
    refresher.write_sidecar(d, {"baseHash": read_bundle(d).content_hash})
    real_move = refresher._move
    calls = {"n": 0}

    def crash_second(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("SIGKILL")
        real_move(src, dst)

    monkeypatch.setattr(refresher, "_move", crash_second)
    with pytest.raises(KeyboardInterrupt):
        refresher.apply_pulled(pull_item("alpha", "new"))
    assert locate_skill_dir("alpha") is None  # dir moved aside, new one not in
    monkeypatch.setattr(refresher, "_move", real_move)
    assert "restored alpha" in refresher.recover_interrupted()
    assert "precious" in (locate_skill_dir("alpha") / "SKILL.md").read_text()


def test_start_recovers_interrupted_apply_before_any_skill_load(home, monkeypatch):
    d = write_local(home, "alpha", body="precious")
    refresher.write_sidecar(d, {"baseHash": read_bundle(d).content_hash})
    real_move = refresher._move
    calls = {"n": 0}

    def crash_second(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("SIGKILL")
        real_move(src, dst)

    monkeypatch.setattr(refresher, "_move", crash_second)
    with pytest.raises(KeyboardInterrupt):
        refresher.apply_pulled(pull_item("alpha", "new"))
    monkeypatch.setattr(refresher, "_move", real_move)
    assert locate_skill_dir("alpha") is None
    # all flags off, non-primary: no worker thread, still restored synchronously
    assert service.start(None, primary=False, cfg={"cache_first": False, "write_through": False,
                                                    "report_usage": False}) is None
    assert "precious" in (locate_skill_dir("alpha") / "SKILL.md").read_text()


def test_stale_tmp_from_killed_apply_is_pruned_but_fresh_one_kept(home):
    from plugins.memory.kynver.skill_sync.store import tmp_root
    old, fresh = tmp_root() / "dead-1-0", tmp_root() / "live-2-0"
    for p in (old, fresh):
        p.mkdir(parents=True)
        (p / "SKILL.md").write_text("x")
    import os, time
    t = time.time() - 3600
    os.utime(old, (t, t))
    assert refresher.recover_interrupted() is None
    assert not old.exists() and fresh.exists()


def test_pending_proposal_accepted_clears_marker_declined_flags(home, svc):
    s, k = svc
    d = write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    s.drainer.drain_once()
    base = get_base("alpha")
    k.server_write("alpha", "other")
    (d / "SKILL.md").write_text(md("alpha", "mine"), encoding="utf-8")
    outbox.mark_dirty("alpha", "upsert", base)
    s.drainer.drain_once()
    pushed = read_sidecar(d)["pending"]["pushedHash"]
    k.proposals[0]["status"] = "applied"
    s.refresher.allow_initial_pull = True
    s.refresher._resolve_pending_proposals()
    assert read_sidecar(d).get("pending") is None and get_base("alpha") == pushed


def test_second_refresh_never_pushes_back_a_pulled_change(home, svc):
    s, k = svc
    k.server_write("srv", "from kynver")
    s.refresher.refresh_once()
    s.drainer.drain_once()
    assert [c for c in k.calls if c[1] == "/skills/sync/push"] == []


# ----------------------------------------------------------------------- usage

def test_usage_buffer_flushes_in_batches_idempotently(home, svc):
    s, k = svc
    k.server_write("alpha", "x")
    for _ in range(1100):
        usage.record_load("alpha")
    usage.record_load("not-in-kynver")
    out = s.reporter.flush_once()
    assert out["sent"] == 1101 and out["accepted"] == 1100 and out["unknownSlug"] == 1
    sizes = [len(c[2]["events"]) for c in k.calls if c[1] == "/skills/usage"]
    assert sizes == [500, 500, 101] and usage.buffered() == 0
    assert load_state()["usageSent"] == 1101


def test_usage_flush_offline_keeps_events(home, svc):
    s, k = svc
    k.server_write("alpha", "x")
    usage.record_load("alpha")
    k.down = True
    assert s.reporter.flush_once()["offline"] and usage.buffered() == 1
    k.down = False
    assert s.reporter.flush_once()["accepted"] == 1 and usage.buffered() == 0


def test_usage_buffer_is_capped_and_reports_drops(home, monkeypatch):
    monkeypatch.setattr(usage, "BUFFER_CAP", 100)
    monkeypatch.setattr(usage, "_path", usage._path)  # real path
    for _ in range(300):
        usage.record_load("alpha")
    assert usage.buffered() <= 300
    # trim happens once the file passes cap*80 bytes; force one more write past it
    for _ in range(60):
        usage.record_load("alpha")
    assert usage.buffered() <= 160
    assert load_state().get("usageDropped", 0) > 0


def test_view_event_records_usage_only_when_enabled(home, svc):
    s, _ = svc
    s.on_skill_event("view", "my_skill", {})
    line = json.loads((home / "state" / "kynver-skill-usage.jsonl").read_text().splitlines()[0])
    assert line["slug"] == "my-skill" and line["kind"] == "load" and len(line["clientEventId"]) == 32


# --------------------------------------------------------------------- service

def test_start_builds_service_only_when_a_flag_is_on(home):
    off = {"cache_first": False, "write_through": False, "report_usage": False, "initial_pull": False,
           "refresh_interval_seconds": 600, "source_id": "hermes:forge"}
    assert service.start(FakeKynver(), primary=True, cfg=off) is None
    on = dict(off, write_through=True)
    s = service.start(FakeKynver(), primary=False, cfg=on)
    try:
        assert s is not None and s._thread is None  # non-primary: hooks only, no thread
        assert service._service is s
    finally:
        service.shutdown()
    assert service._service is None


def test_tick_runs_enabled_parts_and_status_reports_numbers(home, svc):
    s, k = svc
    k.server_write("srv", "x")
    write_local(home, "alpha")
    outbox.mark_dirty("alpha", "upsert", None)
    out = s.tick()
    assert out["refresh"]["applied"] == 1 and out["drain"]["written"] == 1
    st = service.status()
    assert st["outbox"]["depth"] == 0 and st["lastResult"] == "pulled"


def test_provider_initialize_starts_sync_never_raises(home, monkeypatch):
    from plugins.memory.kynver import KynverMemoryProvider
    calls = []
    monkeypatch.setattr(service, "start", lambda client, primary, cfg=None: calls.append(primary))
    p = KynverMemoryProvider(client=FakeKynver())
    p.initialize("s1", agent_context="primary")
    p.initialize("s2", agent_context="cron")
    assert calls == [True, False]
    monkeypatch.setattr(service, "start", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    p.initialize("s3")  # swallowed
