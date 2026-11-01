"""One-time, resumable first-run migration of the already-synced skills (H7).

``hermes kynver skills migrate`` (dry run, writes nothing) then
``... migrate --apply``. Apply, in order:

1. tarball backup of the whole skills dir (``state/kynver-skill-backups/``,
   outside ``skills/`` so Hermes never indexes the copies);
2. seed each skill's sidecar ``baseHash`` from the legacy sync's
   ``state/kynver-skill-sync.json`` so "locally edited since last sync" can be
   told apart from "never synced";
3. pull every page from the server and apply each skill with the normal
   ``refresher.apply_pulled``, which never overwrites a skill whose local hash
   differs from both its base and the server (the server copy is stored beside
   it in ``.kynver.server.json`` and the skill is marked ``pending``).

Progress is journaled in ``state/kynver-skill-migration.json`` (backup path,
last fully applied page cursor, slugs already handled, cumulative report), so a
killed run resumes where it stopped and never takes a second backup. Only when
every page is applied does the global pull cursor get set; until then the
background refresher stays at ``awaiting_initial_migration``.

Legacy state shapes accepted (flat ``{slug: hash}``, or under ``skills``, each
value a hash string or an object with ``contentHash``/``baseHash``/``hash``).
"""

from __future__ import annotations

import logging
import os
import tarfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterator, Optional

from .bundle import BundleError, bundle_from_pull, read_bundle, slug_for
from .refresher import apply_pulled, _stamp
from .scope import out_of_scope_reason
from .store import (
    _EXCLUDED,
    atomic_write_json,
    backup_root,
    file_lock,
    get_base,
    load_state,
    locate_archived_dir,
    locate_skill_dir,
    read_json,
    read_sidecar,
    skills_root,
    state_dir,
    update_state,
    write_sidecar,
)

logger = logging.getLogger(__name__)

BUCKETS = ("added", "changed", "archived", "rejected", "conflict", "skipped", "unchanged")
DEFAULT_TIMEOUT = 15.0
MAX_PAGES = 500  # safety valve against a server that never says hasMore=false
LEGACY_STATE_NAME = "kynver-skill-sync.json"


def _mpath() -> Path:
    return state_dir() / "kynver-skill-migration.json"


def load_migration() -> dict[str, Any]:
    m = read_json(_mpath(), {})
    return m if isinstance(m, dict) else {}


def is_migrated() -> bool:
    return load_migration().get("phase") == "done"


# -- legacy state ---------------------------------------------------------------

def legacy_bases(path: Optional[Path] = None) -> dict[str, str]:
    data = read_json(path or state_dir() / LEGACY_STATE_NAME, {})
    if not isinstance(data, dict):
        return {}
    table = data.get("skills") if isinstance(data.get("skills"), dict) else data
    out: dict[str, str] = {}
    for key, val in table.items():
        if isinstance(val, dict):
            val = val.get("contentHash") or val.get("baseHash") or val.get("hash")
        if isinstance(val, str) and len(val) == 64:
            out[slug_for(str(key))] = val
    return out


def _live_dirs() -> Iterator[Path]:
    root = skills_root()
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED]
        if "SKILL.md" in filenames:
            yield Path(dirpath)


def seed_bases(legacy: dict[str, str]) -> int:
    """Write the legacy hash as ``baseHash`` where a skill has none yet. Idempotent."""
    seeded = 0
    for d in _live_dirs():
        slug = slug_for(d.name)
        side = read_sidecar(d)
        if side.get("baseHash") or slug not in legacy:
            continue
        write_sidecar(d, {**side, "baseHash": legacy[slug], "source": "migration"})
        seeded += 1
    return seeded


def make_backup() -> Path:
    """tar.gz of the skills dir, written under a temp name then renamed (never half-visible)."""
    dest_dir = backup_root()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"skills-pre-migration-{_stamp()}.tar.gz"
    part = final.with_name(final.name + ".part")
    with tarfile.open(part, "w:gz") as tar:
        tar.add(skills_root(), arcname="skills")
    os.chmod(part, 0o600)
    os.replace(part, final)
    return final


# -- classification ---------------------------------------------------------------

def _local_hash(d: Path) -> Optional[str]:
    try:
        return read_bundle(d).content_hash
    except (BundleError, OSError, UnicodeDecodeError):
        return None


def classify(item: dict[str, Any], source_id: str, legacy: dict[str, str]) -> tuple[str, str]:
    """-> (bucket, reason) for one pull item, without touching anything."""
    slug = str(item.get("slug"))
    why = out_of_scope_reason(item, source_id)
    if why:
        return "skipped", why
    try:
        server_hash = bundle_from_pull(item).content_hash
    except BundleError as exc:
        return "rejected", str(exc)
    archived = str(item.get("status") or "active") == "archived"
    live = locate_skill_dir(slug)
    if live is None:
        if archived:
            return "archived", "server-only archived skill"
        return "added", "restored from local archive" if locate_archived_dir(slug) else "not on this box"
    local = _local_hash(live)
    if local == server_hash and not archived:
        return "unchanged", ""
    base = read_sidecar(live).get("baseHash") or get_base(slug) or legacy.get(slug)
    if base is None:
        return "conflict", "never synced from here and differs from the server"
    if local != base:
        return "conflict", "edited locally since last sync and differs from the server"
    return ("archived", "archived on the server") if archived else ("changed", "server has a newer version")


# -- server paging ---------------------------------------------------------------

def _pages(client: Any, source_id: str, cursor: Optional[str], timeout: float) -> Iterator[dict[str, Any]]:
    for _ in range(MAX_PAGES):
        q = urllib.parse.urlencode({k: v for k, v in
                                    {"sourceId": source_id, "cursor": cursor, "limit": 50}.items() if v})
        _, _, page = client.request_raw("GET", "/skills/sync/pull?" + q, timeout=timeout)
        if not isinstance(page, dict):
            raise RuntimeError("unexpected pull response")
        yield page
        cursor = page.get("nextCursor") or cursor
        if not page.get("hasMore"):
            return
    raise RuntimeError("pull did not finish")


def _empty_buckets() -> dict[str, list[Any]]:
    return {b: [] for b in BUCKETS}


def _entry(slug: str, reason: str) -> Any:
    return {"slug": slug, "reason": reason} if reason else slug


def dry_run(client: Any, source_id: str, *, legacy_path: Optional[Path] = None,
            timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """What ``--apply`` would do. Reads the server and local files only; writes nothing."""
    legacy = legacy_bases(legacy_path)
    buckets = _empty_buckets()
    for page in _pages(client, source_id, None, timeout):
        for item in page.get("skills") or []:
            bucket, reason = classify(item, source_id, legacy)
            buckets[bucket].append(_entry(str(item.get("slug")), reason))
    return {"mode": "dry-run", "legacyEntries": len(legacy), "buckets": buckets}


# -- apply -----------------------------------------------------------------------

def apply(client: Any, source_id: str, *, legacy_path: Optional[Path] = None,
          timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run (or resume) the migration. Raises on server/IO errors; progress is already journaled."""
    with file_lock("kynver-skill-migration"):
        m = load_migration()
        if m.get("phase") == "done":
            return {"mode": "apply", "alreadyDone": True, "buckets": m.get("buckets") or _empty_buckets(),
                    "backup": m.get("backup")}
        resumed = bool(m.get("phase"))
        backup = m.get("backup")
        if not (backup and Path(backup).is_file()):
            backup = str(make_backup())
        m = {**m, "phase": "applying", "backup": backup, "startedAt": m.get("startedAt") or int(time.time())}
        m.setdefault("buckets", _empty_buckets())
        m.setdefault("done", [])
        atomic_write_json(_mpath(), m)

        legacy = legacy_bases(legacy_path)
        seeded = seed_bases(legacy)
        done = set(m["done"])
        cursor = m.get("cursor")
        final_cursor = cursor
        for page in _pages(client, source_id, cursor, timeout):
            for item in page.get("skills") or []:
                slug = str(item.get("slug"))
                if slug in done:
                    continue
                bucket, reason = classify(item, source_id, legacy)
                apply_pulled(item, source_id)
                m["buckets"][bucket].append(_entry(slug, reason))
                m["done"].append(slug)
                done.add(slug)
                atomic_write_json(_mpath(), m)
            final_cursor = page.get("nextCursor") or final_cursor
            m["cursor"] = final_cursor
            atomic_write_json(_mpath(), m)
        update_state(cursor=final_cursor, initialized=True, etag=None)
        m.update(phase="done", completedAt=int(time.time()), done=[])
        atomic_write_json(_mpath(), m)
        _clear_prompt_cache()
        return {"mode": "apply", "resumed": resumed, "seeded": seeded, "backup": backup,
                "buckets": m["buckets"]}


def _clear_prompt_cache() -> None:
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:  # health: allow BLE001 -- best effort; agent package may be absent in a plugin-only context
        logger.debug("kynver skill migration: prompt cache not cleared", exc_info=True)


def format_report(report: dict[str, Any]) -> str:
    lines = []
    b = report["buckets"]
    if report["mode"] == "dry-run":
        lines.append("Dry run: nothing was changed. Re-run with --apply to migrate.")
    elif report.get("alreadyDone"):
        lines.append("Migration already completed; nothing to do.")
    else:
        lines.append(("Resumed and completed" if report.get("resumed") else "Completed") + " the migration.")
        lines.append(f"  backup: {report.get('backup')}")
    for name in BUCKETS:
        items = b[name]
        lines.append(f"  {name}: {len(items)}")
        if name in ("unchanged", "skipped") and report["mode"] == "apply":
            continue
        for e in items:
            if isinstance(e, dict):
                lines.append(f"    - {e['slug']}: {e['reason']}")
            elif name != "unchanged":
                lines.append(f"    - {e}")
    return "\n".join(lines)


__all__ = ["apply", "dry_run", "format_report", "is_migrated", "legacy_bases"]
