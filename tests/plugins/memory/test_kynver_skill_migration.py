"""Kynver skills H7 migration, H4 safety-net/status CLI, H6 runtime scope.

Reuses the FakeKynver server from the H1-H3 tests. No network, no real HERMES_HOME.
"""

import json
import tarfile
from argparse import Namespace

import pytest

from plugins.memory.kynver import cli
from plugins.memory.kynver.skill_sync import migration, outbox, refresher, safety_net
from plugins.memory.kynver.skill_sync.bundle import read_bundle
from plugins.memory.kynver.skill_sync.scope import out_of_scope_reason
from plugins.memory.kynver.skill_sync.store import load_state, locate_skill_dir, read_sidecar
from tests.plugins.memory.test_kynver_skill_sync import FakeKynver, md, write_local

SRC = "hermes:forge"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    (h / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def local_hash(home, name):
    return read_bundle(home / "skills" / name).content_hash


def write_legacy(home, **hashes):
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "kynver-skill-sync.json").write_text(json.dumps({"skills": hashes}), encoding="utf-8")


def server_with(**bodies):
    k = FakeKynver()
    for slug, body in bodies.items():
        k.server_write(slug, body)
    return k


def server_hash(k, slug):
    return k._row(slug)["contentHash"]


def names(bucket):
    return sorted(e["slug"] if isinstance(e, dict) else e for e in bucket)


@pytest.fixture
def box(home):
    """Local: clean (== legacy base, server moved on), edited (!= base and != server), plus
    one identical, one never-synced differing. Server also has an added, an archived and a bad skill."""
    k = server_with(clean="v1", edited="v1", same="v1", fresh="v1")
    write_local(home, "clean", "v1")
    write_local(home, "edited", "v1")
    write_local(home, "same", "v1")
    write_local(home, "fresh", "local-only")
    legacy = {s: local_hash(home, s) for s in ("clean", "edited", "same")}
    write_legacy(home, **legacy)
    write_local(home, "edited", "local-edit")
    k.server_write("clean", "v2")
    k.server_write("edited", "v2")
    k.server_write("added", "new")
    k.server_write("old", "bye", status="archived")
    k.server_write("badsecret", "token = " + "A" * 40)
    return k


def test_dry_run_reports_every_bucket_with_reasons_and_writes_nothing(home, box):
    before = {p: p.read_bytes() for p in (home / "skills").rglob("*") if p.is_file()}
    rep = migration.dry_run(box, SRC)
    b = rep["buckets"]
    assert names(b["added"]) == ["added"]
    assert names(b["changed"]) == ["clean"]
    assert names(b["archived"]) == ["old"]
    assert names(b["unchanged"]) == ["same"]
    assert names(b["conflict"]) == ["edited", "fresh"]
    assert [e["slug"] for e in b["rejected"]] == ["badsecret"]
    assert "secret" in b["rejected"][0]["reason"]
    assert all(e["reason"] for e in b["conflict"])
    after = {p: p.read_bytes() for p in (home / "skills").rglob("*") if p.is_file()}
    assert before == after
    assert not (home / "state" / "kynver-skill-backups").exists()
    assert not migration.load_migration() and load_state().get("cursor") is None
    text = migration.format_report(rep)
    assert "badsecret: " in text and "Dry run" in text


def test_apply_backs_up_seeds_applies_and_never_overwrites_diverged(home, box):
    edited_before = (home / "skills" / "edited" / "SKILL.md").read_text()
    fresh_before = (home / "skills" / "fresh" / "SKILL.md").read_text()
    rep = migration.apply(box, SRC)
    assert (home / "skills" / "clean" / "SKILL.md").read_text() == md("clean", "v2")
    assert (home / "skills" / "added" / "SKILL.md").read_text() == md("added", "new")
    assert locate_skill_dir("added") is not None and locate_skill_dir("old") is None
    assert (home / "skills" / ".archive" / "old" / "SKILL.md").is_file()
    # diverged skills keep local text, with the server copy beside them and a pending marker
    assert (home / "skills" / "edited" / "SKILL.md").read_text() == edited_before
    assert (home / "skills" / "fresh" / "SKILL.md").read_text() == fresh_before
    for slug in ("edited", "fresh"):
        assert read_sidecar(home / "skills" / slug)["pending"]["state"] == "diverged"
        assert (home / "skills" / slug / ".kynver.server.json").is_file()
    assert read_sidecar(home / "skills" / "same")["baseHash"] == server_hash(box, "same")
    assert names(rep["buckets"]["rejected"]) == ["badsecret"]
    assert load_state()["rejections"][0]["slug"] == "badsecret"
    # tarball holds the pre-migration text
    with tarfile.open(rep["backup"]) as tar:
        member = tar.extractfile("skills/clean/SKILL.md")
        assert member.read().decode() == md("clean", "v1")
    assert migration.is_migrated() and load_state()["initialized"] is True
    assert migration.apply(box, SRC)["alreadyDone"] is True


def test_seeding_uses_legacy_hash_as_base(home, box):
    migration.seed_bases(migration.legacy_bases())
    assert read_sidecar(home / "skills" / "clean")["baseHash"] == migration.legacy_bases()["clean"]
    assert "baseHash" not in read_sidecar(home / "skills" / "fresh")


def test_legacy_state_shapes(home):
    h = "a" * 64
    for payload in ({"x_y": h}, {"skills": {"x-y": {"contentHash": h}}}, {"skills": {"x-y": h}}):
        (home / "state").mkdir(exist_ok=True)
        (home / "state" / "kynver-skill-sync.json").write_text(json.dumps(payload))
        assert migration.legacy_bases() == {"x-y": h}
    (home / "state" / "kynver-skill-sync.json").write_text("not json")
    assert migration.legacy_bases() == {}


def test_killed_mid_apply_resumes_without_second_backup_or_overwrite(home, box, monkeypatch):
    for i in range(5):
        box.server_write(f"extra{i}", "x")
    real = migration.apply_pulled
    calls = []

    def die_on_third(item, source_id=None):
        if len(calls) == 3:
            raise KeyboardInterrupt  # SIGKILL stand-in: BaseException, no cleanup runs
        calls.append(item["slug"])
        return real(item, source_id)

    monkeypatch.setattr(migration, "apply_pulled", die_on_third)
    with pytest.raises(KeyboardInterrupt):
        migration.apply(box, SRC)
    m = migration.load_migration()
    assert m["phase"] == "applying" and len(m["done"]) == 3 and m["backup"]
    assert load_state().get("initialized") is None  # refresher still gated
    assert refresher.Refresher(box, SRC).refresh_once()["status"] == "awaiting_initial_migration"
    backups = sorted((home / "state" / "kynver-skill-backups").glob("skills-pre-migration-*.tar.gz"))
    assert len(backups) == 1

    monkeypatch.setattr(migration, "apply_pulled", real)
    rep = migration.apply(box, SRC)
    assert rep["resumed"] is True and rep["backup"] == m["backup"]
    assert sorted((home / "state" / "kynver-skill-backups").glob("skills-pre-migration-*.tar.gz")) == backups
    handled = names([e for v in rep["buckets"].values() for e in v])
    assert handled == sorted(set(handled))  # nobody handled twice
    assert {"added", "clean", "edited", "extra4", "old"} <= set(handled)
    assert (home / "skills" / "edited" / "SKILL.md").read_text() == md("edited", "local-edit")
    assert (home / "skills" / "extra4" / "SKILL.md").is_file()
    assert migration.is_migrated()
    # after migration the normal refresher takes over from the final cursor
    assert refresher.Refresher(box, SRC).refresh_once()["status"] in ("ok", "not_modified")


def test_server_outage_mid_migration_is_resumable(home, box):
    box.down = True
    with pytest.raises(RuntimeError):
        migration.apply(box, SRC)
    assert migration.load_migration()["phase"] == "applying" and not migration.is_migrated()
    box.down = False
    assert migration.apply(box, SRC)["buckets"]["added"]


# ---------------------------------------------------------------- H6 scope

def item(runtime=None, bindings=None, via="payload"):
    meta = {"kynver": {k: v for k, v in (("runtime", runtime), ("bindings", bindings)) if v is not None}}
    if via == "payload":
        return {"slug": "s", "skillMd": md("s"), "metadata": meta}
    fm = "---\nname: s\ndescription: d\nmetadata:\n  kynver:\n"
    for k, v in meta["kynver"].items():
        fm += f"    {k}: {json.dumps(v)}\n"
    return {"slug": "s", "skillMd": fm + "---\n# s\n"}


@pytest.mark.parametrize("via", ["payload", "frontmatter"])
@pytest.mark.parametrize("runtime,bindings,skipped", [
    ("hermes", None, False), ("any", None, False), (None, None, False),
    ("somethingnew", None, False),            # unknown = any
    ("claude-code", None, True),
    ("any", ["forge"], False), ("hermes", ["hermes:forge"], False),
    ("any", ["someone-else"], True),
    ("any", [], False),
])
def test_runtime_and_bindings_scope(runtime, bindings, skipped, via):
    assert bool(out_of_scope_reason(item(runtime, bindings, via), SRC)) is skipped


def test_pull_skips_out_of_scope_without_touching_disk_or_recording_rejection(home):
    k = server_with(mine="x")
    k.server_write("cc-only", "x")
    k.skills["cc-only"]["skillMd"] = (
        "---\nname: cc-only\ndescription: d\nmetadata:\n  kynver:\n    runtime: claude-code\n---\n# x\n")
    res = refresher.Refresher(k, SRC, allow_initial_pull=True).refresh_once()
    assert res["applied"] == 1 and res["skipped"] == 1
    assert locate_skill_dir("cc-only") is None and not load_state().get("rejections")


def test_migration_skips_out_of_scope(home):
    k = server_with(mine="x")
    k.server_write("other", "x")
    k.skills["other"]["skillMd"] = (
        "---\nname: other\ndescription: d\nmetadata:\n  kynver:\n    bindings: [sage]\n---\n# x\n")
    rep = migration.dry_run(k, SRC)
    assert names(rep["buckets"]["skipped"]) == ["other"] and names(rep["buckets"]["added"]) == ["mine"]


# ------------------------------------------------- H4 safety net + status CLI

def migrated(home, k):
    migration.apply(k, SRC)


def test_sync_refuses_before_migration(home):
    k = server_with(a="x")
    assert safety_net.sync_now(k, SRC)["status"] == "awaiting_initial_migration"
    assert k.calls == []


def test_sync_pushes_missed_edit_then_pulls(home):
    k = server_with(a="v1", b="v1")
    write_local(home, "a", "v1")
    write_local(home, "b", "v1")
    write_legacy(home, a=local_hash(home, "a"), b=local_hash(home, "b"))
    migrated(home, k)
    write_local(home, "a", "edited while hermes was down")  # no hook fired
    k.server_write("b", "v2", writer="kynver-chat")
    res = safety_net.sync_now(k, SRC)
    assert res["queued"] == ["a"] and res["push"]["written"] == 1
    assert "edited while" in k.skills["a"]["skillMd"]
    assert (home / "skills" / "b" / "SKILL.md").read_text() == md("b", "v2")
    assert outbox.entries() == []
    assert safety_net.sync_now(k, SRC)["queued"] == []


def test_scan_leaves_pending_and_unsynced_skills_alone(home, box):
    migrated(home, box)
    assert safety_net.scan_dirty() == []  # edited/fresh are diverged-pending; others clean


def test_status_reports_outbox_parked_and_last_pull(home):
    k = server_with(a="x")
    migrated(home, k)
    refresher.Refresher(k, SRC).refresh_once()
    outbox.mark_dirty("a", "upsert", "h", now=1000.0)
    outbox.mark_dirty("b", "upsert", None, now=2000.0)
    outbox.park("b", "conflict", "server hash differs")
    r = safety_net.status_report(now=1600.0)
    assert r["outboxDepth"] == 2 and r["oldestAgeSeconds"] == 600
    assert r["parked"] == [{"slug": "b", "reason": "conflict", "lastError": "server hash differs"}]
    assert r["lastPullAt"] is not None and r["migration"] == "done"
    assert "parked: b [conflict]" in safety_net.format_status(r)


def test_cli_status_migrate_and_sync(home, box, capsys):
    assert cli.cmd_skills_status(Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["migration"] == "not_started"
    assert cli.cmd_skills_sync(Namespace(), box) == cli.EXIT_UNAVAILABLE
    capsys.readouterr()
    assert cli.cmd_skills_migrate(Namespace(apply=False, json=False), box) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "badsecret" in out and not migration.is_migrated()
    assert cli.cmd_skills_migrate(Namespace(apply=True, json=True), box) == 0
    assert json.loads(capsys.readouterr().out)["backup"] and migration.is_migrated()
    assert cli.cmd_skills_sync(Namespace(), box) == 0
    box.down = True
    assert cli.cmd_skills_migrate(Namespace(apply=False, json=False), box) == cli.EXIT_UNAVAILABLE


def test_register_cli_builds_skills_tree():
    import argparse
    p = argparse.ArgumentParser()
    cli.register_cli(p)
    ns = p.parse_args(["skills", "migrate", "--apply"])
    assert (ns.kynver_command, ns.skills_command, ns.apply) == ("skills", "migrate", True)
