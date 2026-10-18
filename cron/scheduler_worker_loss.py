"""Cron: record a restart-safe worker that died before writing its own result.

The external worker owns the job's terminal bookkeeping (``mark_job_run`` + output file).
When it is killed first (OOM in its scope, SIGKILL, host restart) the gateway's waiter only
recovers the execution row to ``unknown``; the job itself kept ``last_run_at``/``last_status``
null, wrote no output and delivered nothing, so a one-shot looked like it never ran.
``record_worker_loss`` closes that gap: it names the cause (from the scope's journal) and
writes it as a failed run, through the same fenced ``mark_job_run`` the worker would use.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def scope_unit_name(job_id: str, execution_id: str) -> str:
    """Matches ``_launch_external_cron_worker``'s ``unit_suffix`` under ``hermes-worker-``."""
    return f"hermes-worker-cron-{job_id}-exec-{execution_id}.scope"


def _scope_journal(unit: str) -> str:
    try:
        return subprocess.run(
            ["journalctl", "--user", "-u", unit, "--no-pager", "-o", "cat", "-n", "40"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001 — diagnostics only
        return ""


def worker_loss_reason(job_id: str, execution_id: str, returncode: Optional[int]) -> str:
    text = _scope_journal(scope_unit_name(job_id, execution_id))
    if "oom-kill" in text or "killed by the OOM killer" in text:
        return ("OOM-killed: the job's memory cap was exceeded (usually a type check, build or "
                "parallel test run inside the job), so the cron worker was stopped with it")
    if returncode is not None and returncode < 0:
        return f"killed by signal {-returncode}"
    if returncode is not None and returncode > 128:
        return f"killed by signal {returncode - 128}"
    if returncode:
        return f"exited with code {returncode}"
    return "stopped without writing a result"


def record_worker_loss(
    job_id: str, fire_owner: Optional[str], execution_id: str, returncode: Optional[int],
) -> bool:
    """Write the dead worker's run as failed on the job; True if the job record was updated."""
    from cron.jobs import get_job, mark_job_run, save_job_output

    reason = worker_loss_reason(job_id, execution_id, returncode)
    error = (f"Cron worker died before finishing ({reason}); execution {execution_id}. "
             "Whether its side effects ran is unknown.")
    logger.error("Job '%s': %s", job_id, error)
    job = None
    try:
        job = get_job(job_id)
        save_job_output(job_id, f"# Cron run died before finishing\n\n- job: {job_id} "
                                f"({(job or {}).get('name', '?')})\n- execution: {execution_id}\n"
                                f"- cause: {reason}\n- exit: {returncode}\n")
    except Exception:  # noqa: BLE001 — the status write below matters more
        logger.debug("Job '%s': could not write worker-loss output", job_id, exc_info=True)
    try:
        kwargs = {"expected_fire_owner": fire_owner} if fire_owner else {}
        return bool(mark_job_run(job_id, False, error, **kwargs))
    except Exception:  # noqa: BLE001
        logger.exception("Job '%s': could not record worker loss", job_id)
        return False
