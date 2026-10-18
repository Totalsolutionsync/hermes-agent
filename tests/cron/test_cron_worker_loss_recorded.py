"""A restart-safe cron worker that dies before writing its result must still leave a
failed run on the job (18 Oct: a one-shot's scope was OOM-killed and the job kept
last_run_at/last_status null, no output, no delivery — a silent death)."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

import cron.jobs as jobs_mod


@pytest.fixture
def cron_store(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    return hermes_home


def _claimed_oneshot(owner: str) -> dict:
    job = jobs_mod.create_job(prompt="land the tool", schedule="in 30m")
    jobs = jobs_mod.load_jobs()
    for j in jobs:
        if j["id"] == job["id"]:
            j["fire_claim"] = {"at": "2026-10-18T05:00:26-07:00", "by": owner}
            j["repeat"]["completed"] = 1
    jobs_mod.save_jobs(jobs)
    return job


def test_oom_killed_worker_is_recorded_as_failed_run(cron_store, monkeypatch):
    import cron.scheduler_worker_loss as loss

    monkeypatch.setattr(loss, "_scope_journal", lambda unit: (
        f"{unit}: A process of this unit has been killed by the OOM killer.\n"
        f"{unit}: Failed with result 'oom-kill'.\n"))
    job = _claimed_oneshot("host:209:abc")

    assert loss.record_worker_loss(job["id"], "host:209:abc", "exec-1", -15) is True

    stored = jobs_mod.get_job(job["id"])
    assert stored["last_status"] == "error"
    assert "OOM-killed" in stored["last_error"]
    assert "exec-1" in stored["last_error"]
    assert stored["last_run_at"] is not None
    outputs = list((cron_store / "cron" / "output" / job["id"]).glob("*.md"))
    assert outputs and "OOM-killed" in outputs[0].read_text()


def test_loss_is_not_recorded_over_another_owners_fire(cron_store, monkeypatch):
    import cron.scheduler_worker_loss as loss

    monkeypatch.setattr(loss, "_scope_journal", lambda unit: "")
    job = _claimed_oneshot("host:209:new-owner")

    assert loss.record_worker_loss(job["id"], "host:209:old-owner", "exec-1", 9) is False
    assert jobs_mod.get_job(job["id"])["last_status"] is None


@pytest.mark.parametrize("journal,rc,expected", [
    ("Failed with result 'oom-kill'", -15, "OOM-killed"),
    ("", -9, "signal 9"),
    ("", 137, "signal 9"),
    ("", 3, "code 3"),
    ("", 0, "without writing a result"),
])
def test_loss_reason(monkeypatch, journal, rc, expected):
    import cron.scheduler_worker_loss as loss

    monkeypatch.setattr(loss, "_scope_journal", lambda unit: journal)
    assert expected in loss.worker_loss_reason("job", "exec", rc)


def test_scope_unit_name_matches_dispatch_suffix():
    import cron.scheduler_worker_loss as loss

    assert loss.scope_unit_name("90fd5932a2ac", "e6c5") == "hermes-worker-cron-90fd5932a2ac-exec-e6c5.scope"


def test_wait_records_loss_when_worker_dies_unterminalized(monkeypatch):
    import cron.scheduler as scheduler
    import cron.scheduler_worker_loss as loss

    statuses = iter([{"id": "exec-1", "status": "running"}, {"id": "exec-1", "status": "unknown"}])
    monkeypatch.setattr(scheduler, "get_execution", lambda _e: next(statuses))
    monkeypatch.setattr(scheduler, "recover_interrupted_executions", Mock(return_value=1))
    record = Mock(return_value=True)
    monkeypatch.setattr(loss, "record_worker_loss", record)
    process = Mock()
    process.wait.return_value = -15

    assert scheduler._wait_for_external_cron_worker(
        process, execution_id="exec-1", job_id="job-1", fire_owner="host:1:x") is True
    record.assert_called_once_with("job-1", "host:1:x", "exec-1", -15)


def test_wait_does_not_record_loss_when_worker_finished_itself(monkeypatch):
    import cron.scheduler as scheduler
    import cron.scheduler_worker_loss as loss

    statuses = iter([{"id": "exec-1", "status": "running"}, {"id": "exec-1", "status": "completed"}])
    monkeypatch.setattr(scheduler, "get_execution", lambda _e: next(statuses))
    record = Mock()
    monkeypatch.setattr(loss, "record_worker_loss", record)
    process = Mock()
    process.wait.return_value = 0

    assert scheduler._wait_for_external_cron_worker(
        process, execution_id="exec-1", job_id="job-1", fire_owner="host:1:x") is True
    record.assert_not_called()


@pytest.mark.parametrize("supported", [True, False])
def test_scoped_dispatch_continues_past_a_child_oom_when_systemd_allows(monkeypatch, supported):
    import tools.process_registry as process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setenv("INVOCATION_ID", "managed-service")
    monkeypatch.setattr(process_registry, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr(process_registry, "_systemd_scope_oom_policy_supported", lambda: supported)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

    dispatch = process_registry.restart_safe_gateway_child_argv(
        ["python", "worker.py"], unit_suffix="cron-job-1", require_restart_safe_scope=False)

    assert dispatch.mode == "scoped"
    assert ("OOMPolicy=continue" in dispatch.argv) is supported
    assert dispatch.argv[-3:] == ["--", "python", "worker.py"]


def test_oom_policy_support_reads_systemd_version(monkeypatch):
    import tools.process_registry as process_registry

    for version, expected in (("systemd 255 (255.4-1ubuntu8)", True), ("systemd 249 (249.11)", False)):
        monkeypatch.setattr(process_registry, "_scope_oom_policy_supported", None)
        monkeypatch.setattr(process_registry.subprocess, "run",
                            lambda *a, **k: Mock(stdout=f"{version}\n+PAM +AUDIT\n"))
        assert process_registry._systemd_scope_oom_policy_supported() is expected
