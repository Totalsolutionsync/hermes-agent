"""Kynver Updates / Failures tabs as the default cron delivery (cron/delivery_kynver.py).

With Kynver configured and the Kynver cron forwarder enabled, background job output goes to the
agent's Kynver tabs instead of the main chat (the forwarder posts it; Hermes posts nothing, so
there are no duplicates); ``[ACTION_NEEDED]`` output also reaches the origin chat; stored jobs on
the old defaults are migrated once with a backup. Without Kynver or the forwarder nothing changes.
"""

import json

import pytest

from cron import delivery_kynver as kt
from cron.scheduler import _resolve_delivery_targets
from cron.scheduler_delivery import _deliver_result, _resolve_single_delivery_target

TELEGRAM_ORIGIN = {"platform": "telegram", "chat_id": "555"}


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KYNVER_HERMES_CRON_FORWARD", raising=False)
    for name in ("KYNVER_API_KEY", "KYNVER_AGENT_OS_SLUG", "KYNVER_DISABLED",
                 "HERMES_KYNVER_DISABLED", "KYNVER_AGENTOS_MODE"):
        monkeypatch.delenv(name, raising=False)

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    return hermes_home


@pytest.fixture
def kynver_on(cron_env, monkeypatch, tmp_path):
    """Kynver configured for the profile and the cron forwarder enabled (its routes file exists).
    Returns the recorded AgentOS API posts: Hermes must make none for tab targets."""
    monkeypatch.setenv("KYNVER_API_KEY", "test-key")
    monkeypatch.setenv("KYNVER_AGENT_OS_SLUG", "will")
    (tmp_path / ".kynver").mkdir()
    (tmp_path / ".kynver" / "hermes-cron-routes.json").write_text("{}", encoding="utf-8")
    posts = []
    from plugins.memory.kynver import agentos_bridge

    def fake_post(self, path, body, **_kw):
        posts.append((path, body))
        return {"ok": True}

    monkeypatch.setattr(agentos_bridge.KynverAgentOSClient, "post", fake_post)
    return posts


def _write_jobs(home, jobs):
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _job(job_id, **fields):
    return {"id": job_id, "name": job_id, "prompt": "p", "enabled": True,
            "schedule": {"kind": "interval", "minutes": 60}, **fields}


def test_new_jobs_default_to_kynver_tabs(kynver_on):
    from cron.jobs import create_job
    job = create_job("check things", "every 1h", origin=TELEGRAM_ORIGIN)
    assert job["deliver"] == kt.UPDATES_TARGET
    assert job["failure_deliver"] == kt.FAILURES_TARGET


def test_without_kynver_defaults_are_unchanged(cron_env):
    from cron.jobs import create_job
    with_origin = create_job("a", "every 1h", origin=TELEGRAM_ORIGIN)
    without = create_job("b", "every 1h")
    assert with_origin["deliver"] == "origin" and "failure_deliver" not in with_origin
    assert without["deliver"] == "local"


def test_origin_resolves_to_the_tab_for_its_lane(kynver_on):
    job = _job("j", deliver="origin", origin=TELEGRAM_ORIGIN)
    assert _resolve_delivery_targets(job) == [
        {"platform": "kynver", "chat_id": "updates", "thread_id": None}]
    assert _resolve_delivery_targets(job, for_failure=True)[0]["chat_id"] == "failures"


def test_kynver_target_falls_back_to_origin_when_kynver_is_gone(cron_env):
    job = _job("j", deliver=kt.UPDATES_TARGET, origin=TELEGRAM_ORIGIN)
    [target] = _resolve_delivery_targets(job)
    assert (target["platform"], target["chat_id"]) == ("telegram", "555")


def test_cron_tabs_opt_out(kynver_on, cron_env):
    (cron_env / "config.yaml").write_text("kynver:\n  cron_tabs: false\n", encoding="utf-8")
    job = _job("j", deliver="origin", origin=TELEGRAM_ORIGIN)
    assert _resolve_delivery_targets(job)[0]["platform"] == "telegram"


def test_tab_delivery_posts_nothing_itself(kynver_on):
    """The forwarder mirrors the run file by kind; a Hermes-side post would duplicate it."""
    job = _job("j", deliver=kt.UPDATES_TARGET, failure_deliver=kt.FAILURES_TARGET,
               execution_id="exec-1")
    assert _deliver_result(job, "all green") is None
    assert _deliver_result(job, "it broke", for_failure=True) is None
    assert kynver_on == []


@pytest.mark.parametrize("kynver", [True, False])
def test_preflight_lets_kynver_tab_jobs_run(kynver, cron_env, request):
    """kynver:* has no gateway adapter to check: the forwarder posts it, or it falls back to origin.
    Treating ``kynver`` as an unknown platform blocked every migrated job as blocked_config."""
    from cron.scheduler_preflight import _preflight_check_delivery

    if kynver:
        request.getfixturevalue("kynver_on")
    job = _job("j", deliver=kt.UPDATES_TARGET, failure_deliver=kt.FAILURES_TARGET,
               origin=TELEGRAM_ORIGIN)
    assert _preflight_check_delivery(job) is None
    assert _preflight_check_delivery(dict(job, deliver="kynver:failures,local")) is None


def test_preflight_still_blocks_a_bogus_platform_beside_kynver_tabs(kynver_on):
    from cron.scheduler_preflight import _preflight_check_delivery

    job = _job("j", deliver=kt.UPDATES_TARGET, failure_deliver="nosuchplatform:123")
    err = _preflight_check_delivery(job)
    assert err is not None and "'nosuchplatform' is not a known" in err
    err = _preflight_check_delivery(_job("j", deliver="kynver:updates,bogus"))
    assert err is not None and "'bogus' is not a known" in err


def test_tabs_need_the_forwarder(kynver_on, tmp_path, monkeypatch):
    (tmp_path / ".kynver" / "hermes-cron-routes.json").unlink()
    assert not kt.kynver_tabs_active()
    from cron.jobs import create_job
    assert create_job("a", "every 1h", origin=TELEGRAM_ORIGIN)["deliver"] == "origin"
    monkeypatch.setenv("KYNVER_HERMES_CRON_FORWARD", "1")
    assert kt.kynver_tabs_active()
    monkeypatch.setenv("KYNVER_HERMES_CRON_FORWARD", "off")
    (tmp_path / ".kynver" / "hermes-cron-routes.json").write_text("{}", encoding="utf-8")
    assert not kt.kynver_tabs_active()


def test_action_needed_flags_the_tab_and_adds_the_origin_chat(kynver_on):
    job = _job("j", deliver=kt.UPDATES_TARGET, origin=TELEGRAM_ORIGIN)
    targets = _resolve_delivery_targets(job)
    content, routed = kt.apply_action_needed(
        job, "[ACTION_NEEDED] approve the invoice\ndetails", targets,
        _resolve_single_delivery_target)
    assert content == "approve the invoice\ndetails"
    assert [(t["platform"], t["chat_id"]) for t in routed] == [
        ("kynver", "updates"), ("telegram", "555")]
    assert routed[0]["_kynver_kind"] == "action_needed"


def test_plain_output_never_reaches_the_main_chat(kynver_on):
    job = _job("j", deliver=kt.UPDATES_TARGET, origin=TELEGRAM_ORIGIN)
    targets = _resolve_delivery_targets(job)
    content, routed = kt.apply_action_needed(
        job, "nightly sync done; nothing for you", targets, _resolve_single_delivery_target)
    assert routed == targets and content == "nightly sync done; nothing for you"


def test_load_migrates_old_defaults_once_with_backup(kynver_on, cron_env):
    _write_jobs(cron_env, [
        _job("origin-job", deliver="origin", origin=TELEGRAM_ORIGIN),
        _job("local-job", deliver="local"),
        _job("pinned", deliver="telegram:-100123"),
        _job("home", deliver="telegram", failure_deliver="local"),
        _job("home-plain", deliver="telegram"),
    ])
    from cron.jobs import load_jobs, save_jobs
    jobs = {j["id"]: j for j in load_jobs()}
    for moved in ("origin-job", "local-job"):
        assert jobs[moved]["deliver"] == kt.UPDATES_TARGET
        assert jobs[moved]["failure_deliver"] == kt.FAILURES_TARGET
    assert jobs["pinned"]["deliver"] == "telegram:-100123"
    assert "failure_deliver" not in jobs["pinned"]
    assert (jobs["home"]["deliver"], jobs["home"]["failure_deliver"]) == ("telegram", kt.FAILURES_TARGET)
    assert "failure_deliver" not in jobs["home-plain"]

    cron_dir = cron_env / "cron"
    [backup] = list(cron_dir.glob("jobs.json.pre-kynver-tabs-*.bak"))
    assert {j["id"]: j["deliver"] for j in json.loads(backup.read_text())["jobs"]}["origin-job"] == "origin"
    on_disk = {j["id"]: j for j in json.loads((cron_dir / "jobs.json").read_text())["jobs"]}
    assert on_disk["local-job"]["deliver"] == kt.UPDATES_TARGET

    # A job deliberately set to local after the move stays local.
    save_jobs(list(jobs.values()) + [_job("later", deliver="local")])
    assert {j["id"]: j for j in load_jobs()}["later"]["deliver"] == "local"


def test_load_without_kynver_leaves_store_alone(cron_env):
    _write_jobs(cron_env, [_job("origin-job", deliver="origin", origin=TELEGRAM_ORIGIN)])
    from cron.jobs import load_jobs
    assert load_jobs()[0]["deliver"] == "origin"
    assert not (cron_env / "cron" / kt.MIGRATION_MARKER).exists()
