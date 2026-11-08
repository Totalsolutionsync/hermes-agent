"""Kynver Updates / Failures tabs as cron delivery targets.

Background job output belongs in the agent's Kynver tabs, not the user's main chat. When the Kynver
plugin is configured for the profile:

* ``kynver:updates`` / ``kynver:failures`` are delivery targets. Hermes posts nothing for them itself:
  the Kynver runtime's cron forwarder (``kynver hermes cron-forward``, started by the Kynver daemon)
  already mirrors every finished run into the Kynver tabs by kind (a run that failed → Failures),
  so a second post here would duplicate it. The targets keep a run out of the main chat.
* New jobs default to ``deliver=kynver:updates`` and ``failure_deliver=kynver:failures``.
* ``origin`` resolves to the matching tab (updates for results, failures for failure notices).
* A run whose output carries ``[ACTION_NEEDED]`` on its first or last line is posted as
  ``action_needed`` AND sent to the job's origin chat — the one case that reaches the main chat.
* Stored jobs still on the old defaults are migrated once on load (backup kept, see
  ``migrate_jobs``); jobs pinned to a ``platform:chat_id`` are left alone.

The tabs only apply while the forwarder is enabled (a ``~/.kynver/hermes-cron-routes.json`` file or
``KYNVER_HERMES_CRON_FORWARD=1``, the same test the Kynver daemon uses): without it nothing would
post, so every path here is a no-op and today's defaults hold. The same applies without Kynver; a
``kynver:*`` target on a profile that loses Kynver or the forwarder falls back to ``origin``.
Opt out with ``kynver.cron_tabs: false``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

KYNVER_PLATFORM = "kynver"
UPDATES_TARGET = "kynver:updates"
FAILURES_TARGET = "kynver:failures"
_TAB_KINDS = {"updates": "update", "failures": "failure"}
ACTION_NEEDED_MARKER = "[ACTION_NEEDED]"
MIGRATION_MARKER = ".kynver_tabs_migrated"
_DEFAULT_LANE_TOKENS = frozenset({"origin", "local"})


def cron_forward_enabled() -> bool:
    """True when the Kynver runtime's cron forwarder runs on this computer (mirrors its own check:
    ``KYNVER_HERMES_CRON_FORWARD`` on/off, else the presence of ``~/.kynver/hermes-cron-routes.json``)."""
    flag = os.environ.get("KYNVER_HERMES_CRON_FORWARD", "").strip().lower()
    if flag in ("0", "false", "off"):
        return False
    return flag in ("1", "true") or (Path.home() / ".kynver" / "hermes-cron-routes.json").exists()


def kynver_tabs_active() -> bool:
    """True when this profile has Kynver configured, enabled, the cron tabs not opted out, and the
    forwarder that fills the tabs is running."""
    if not cron_forward_enabled():
        return False
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        from plugins.memory.kynver.agentos_bridge import agentos_enabled
        from plugins.memory.kynver.substrate import kynver_explicitly_disabled

        config = load_config_readonly() or {}
        if cfg_get(config, "kynver", "cron_tabs", default=True) is False:
            return False
        return agentos_enabled() and not kynver_explicitly_disabled(config=config)
    except Exception:
        logger.debug("Kynver tab check unavailable", exc_info=True)
        return False


def parse_tab_token(part: Any) -> Optional[str]:
    """``kynver:<tab>`` → ``<tab>`` (lowercased), else None."""
    raw = str(part or "").strip().lower()
    prefix = KYNVER_PLATFORM + ":"
    return raw[len(prefix):].strip() if raw.startswith(prefix) else None


def tab_target(job: dict, tab: str) -> Optional[dict]:
    if tab not in _TAB_KINDS:
        logger.warning("Job '%s': unknown Kynver tab 'kynver:%s' (use kynver:updates or "
                       "kynver:failures) — skipping target", job.get("id", "?"), tab)
        return None
    return {"platform": KYNVER_PLATFORM, "chat_id": tab, "thread_id": None}


def origin_as_tab(part: str, for_failure: bool) -> str:
    """Background jobs: ``origin`` means the Kynver tab for this lane when Kynver is on."""
    if part.strip().lower() != "origin" or not kynver_tabs_active():
        return part
    return FAILURES_TARGET if for_failure else UPDATES_TARGET


def creation_defaults(deliver: Optional[str], failure_deliver: Optional[str],
                      origin: Optional[dict]) -> tuple[str, Optional[str]]:
    """``create_job`` defaults: Kynver tabs when configured, else origin-or-local (unchanged)."""
    if deliver is None and kynver_tabs_active():
        return UPDATES_TARGET, FAILURES_TARGET if failure_deliver is None else failure_deliver
    if deliver is None:
        deliver = "origin" if origin else "local"
    return deliver, failure_deliver


def _strip_action_needed(content: str) -> Optional[str]:
    """Content without the marker when the first or last non-empty line carries it, else None."""
    lines = (content or "").splitlines()
    filled = [i for i, line in enumerate(lines) if line.strip()]
    for i in dict.fromkeys(filled[:1] + filled[-1:]):
        stripped = lines[i].strip()
        if stripped.upper().startswith(ACTION_NEEDED_MARKER):
            rest = stripped[len(ACTION_NEEDED_MARKER):].strip()
            return "\n".join(lines[:i] + ([rest] if rest else []) + lines[i + 1:]).strip()
    return None


def apply_action_needed(job: dict, content: str, targets: list[dict],
                        resolve_origin: Callable[[dict, str], Optional[dict]]
                        ) -> tuple[str, list[dict]]:
    """An ``[ACTION_NEEDED]`` run is flagged on its Kynver targets and also sent to the origin
    chat. Returns ``(content, targets)``, unchanged when there is no marker or no Kynver target."""
    tab_targets = [t for t in targets if t.get("platform") == KYNVER_PLATFORM]
    cleaned = _strip_action_needed(content) if tab_targets else None
    if cleaned is None:
        return content, targets
    for target in tab_targets:
        target["_kynver_kind"] = "action_needed"
    main = resolve_origin(job, "origin")
    if main and not any(
        (t["platform"].lower(), str(t["chat_id"]), t.get("thread_id"))
        == (main["platform"].lower(), str(main["chat_id"]), main.get("thread_id"))
        for t in targets
    ):
        targets = targets + [main]
    return cleaned, targets


def deliver_to_tab(job: dict, text: str, target: dict, *, for_failure: bool) -> Optional[str]:
    """A Kynver tab target: nothing to send. The cron forwarder posts the run file by kind, so a
    post here would double it. Always succeeds (None); the log line says who delivers."""
    kind = target.get("_kynver_kind") or ("failure" if for_failure else _TAB_KINDS[target["chat_id"]])
    logger.info("Job '%s': %s output left to kynver cron-forward (%s tab); Hermes posts nothing itself",
                job.get("id", "?"), kind, target.get("chat_id"))
    return None


# ---------------------------------------------------------------- one-time store migration

def _lane_parts(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        value = ",".join(str(p) for p in value)
    return [p.strip().lower() for p in str(value or "").split(",") if p.strip()]


def _is_default_lane(value: Any) -> bool:
    return set(_lane_parts(value)) <= _DEFAULT_LANE_TOKENS


def _pins_a_chat(value: Any) -> bool:
    """``platform:chat_id`` (an explicit conversation); ``kynver:*`` and ``bot-chat:*`` are not."""
    return any(":" in p and not p.startswith(("kynver:", "bot-chat:")) for p in _lane_parts(value))


def _migrate_job(job: dict) -> bool:
    deliver, has_failure = job.get("deliver"), "failure_deliver" in job
    failure = job.get("failure_deliver")
    if _pins_a_chat(deliver) or _pins_a_chat(failure):
        return False
    changed = False
    if _is_default_lane(deliver):
        job["deliver"] = UPDATES_TARGET
        changed = True
        if not has_failure or _is_default_lane(failure):
            job["failure_deliver"] = FAILURES_TARGET
    elif has_failure and failure is not None and _is_default_lane(failure):
        # deliver is a deliberate non-chat choice (home channel, all, bot-chat): only an explicit
        # origin/local failure lane moves; an absent one keeps following deliver.
        job["failure_deliver"] = FAILURES_TARGET
        changed = True
    return changed


def migrate_jobs(jobs: list[dict], jobs_file: Path, *, persist: bool) -> bool:
    """Move jobs still on the old defaults (``origin`` / ``local`` / unset) to the Kynver tabs,
    once per store. ``persist`` (the locked load pass) writes a backup of ``jobs.json`` and the
    marker that stops later loads from touching jobs created after the move. True when ``jobs``
    changed; the caller saves them."""
    marker = jobs_file.parent / MIGRATION_MARKER
    if marker.exists() or not kynver_tabs_active():
        return False
    candidates = [dict(j) for j in jobs]
    changed_ids = [j.get("id") for j in candidates if _migrate_job(j)]
    if not persist:
        return bool(changed_ids)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = None
    if changed_ids and jobs_file.exists():
        backup = jobs_file.with_name(f"{jobs_file.name}.pre-kynver-tabs-{stamp}.bak")
        shutil.copy2(jobs_file, backup)
    jobs[:] = candidates
    marker.write_text(json.dumps({"migrated_at": stamp, "jobs": changed_ids,
                                  "backup": backup.name if backup else None}) + "\n",
                      encoding="utf-8")
    if changed_ids:
        logger.warning("Moved %d cron job(s) to Kynver Updates/Failures tabs (backup: %s)",
                       len(changed_ids), backup)
    return bool(changed_ids)
