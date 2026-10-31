"""Background cache refresher (H1). Never runs on the turn path.

One ``refresh_once`` call: conditional manifest (``If-None-Match`` → 304 =
nothing to do), then ``pull`` pages by the opaque ``cursor``, applying each
skill to ``~/.hermes/skills`` after verifying it. Every network call has a
short timeout; any failure logs and leaves the cache exactly as it was.

Apply rules (plan §2.2 items 7-9):

* the payload is untrusted: contentHash, every file sha256, paths, sizes and
  the secret scan are checked before anything touches disk; a bad skill is
  discarded and reported, the rest of the pull continues;
* per-skill atomic replace (temp dir + rename) with the previous copy backed
  up outside ``skills/``;
* a skill with a local edit not yet on the server (pending proposal, outbox
  entry, or local hash != base) is never overwritten — the server version is
  stored beside it in ``.kynver.server.json``;
* ``archived`` moves the folder into ``.archive/``;
* the hash just applied becomes the sidecar ``baseHash``, so a pulled change is
  never pushed back.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import outbox
from .bundle import (
    SERVER_COPY_NAME,
    SIDECAR_NAME,
    Bundle,
    BundleError,
    bundle_from_pull,
    read_bundle,
    write_bundle_dir,
)
from .store import (
    archive_root,
    backup_root,
    load_state,
    locate_archived_dir,
    locate_skill_dir,
    read_sidecar,
    skills_root,
    tmp_root,
    update_sidecar,
    update_state,
    write_server_copy,
    write_sidecar,
    clear_server_copy,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0
MAX_PAGES_PER_REFRESH = 20
KEEP_BACKUPS = 3
STALE_TMP_SECONDS = 600.0
MAX_REJECTIONS_KEPT = 50
_SUPPORT_PREFIXES = ("references/", "templates/", "scripts/", "assets/")


def _stamp() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime()) + f"{int((time.time() % 1) * 1000):03d}"


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


def _local_hash(skill_dir: Path) -> Optional[str]:
    try:
        return read_bundle(skill_dir).content_hash
    except (BundleError, OSError, UnicodeDecodeError):
        return None


def _prune_backups(slug: str) -> None:
    root = backup_root() / slug
    try:
        olds = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for p in olds[:-KEEP_BACKUPS]:
        shutil.rmtree(p, ignore_errors=True)


def _carry_over_unsynced(old: Path, tmp: Path, bundle: Bundle) -> None:
    """Keep files the sync does not own (binaries, other layout) when replacing a folder."""
    for dirpath, dirnames, filenames in os.walk(old, followlinks=False):
        for fn in filenames:
            fp = Path(dirpath) / fn
            rel = fp.relative_to(old).as_posix()
            if rel == "SKILL.md" or rel in bundle.files or fn in (SIDECAR_NAME, SERVER_COPY_NAME):
                continue
            if fp.is_symlink():
                continue
            if rel.startswith(_SUPPORT_PREFIXES):
                try:
                    fp.read_bytes().decode("utf-8")
                    continue  # a text file the server no longer has: removed on purpose
                except UnicodeDecodeError:
                    pass
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, target)


def install_bundle(slug: str, dest: Path, bundle: Bundle) -> None:
    """Atomically make ``dest`` hold ``bundle`` (backing up whatever was there)."""
    tmp = tmp_root() / f"{slug}-{os.getpid()}-{_stamp()}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_bundle_dir(tmp, bundle)
        backup: Optional[Path] = None
        if dest.exists():
            _carry_over_unsynced(dest, tmp, bundle)
            backup = backup_root() / slug / _stamp()
            update_state(inflight={"slug": slug, "dest": str(dest), "backup": str(backup)})
            _move(dest, backup)
        _move(tmp, dest)
        update_state(inflight=None)
        _prune_backups(slug)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def recover_interrupted() -> Optional[str]:
    """If a previous apply died between 'move old aside' and 'move new in', put the old copy back."""
    st = load_state()
    inflight = st.get("inflight")
    if not isinstance(inflight, dict):
        _prune_stale_tmp()
        return None
    dest, backup = Path(inflight.get("dest", "")), Path(inflight.get("backup", ""))
    msg = None
    if backup.is_dir() and not dest.exists():
        _move(backup, dest)
        msg = f"restored {dest.name} from interrupted apply"
        logger.warning("kynver skill sync: %s", msg)
    update_state(inflight=None)
    shutil.rmtree(tmp_root(), ignore_errors=True)
    return msg


def _prune_stale_tmp(max_age_seconds: float = STALE_TMP_SECONDS) -> None:
    """Drop temp dirs a killed apply left behind. Age-gated: another process may be mid-apply."""
    root = tmp_root()
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    cutoff = time.time() - max_age_seconds
    for p in entries:
        try:
            if p.stat().st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _archive_live(slug: str, live: Path) -> None:
    dest = archive_root() / live.name
    if dest.exists():
        dest = archive_root() / f"{live.name}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}"
    backup = backup_root() / slug / _stamp()
    shutil.copytree(live, backup, symlinks=True)
    _move(live, dest)
    try:  # keep Hermes' own curator telemetry consistent; best effort, without echoing it back
        from tools.skill_usage import STATE_ARCHIVED, set_state
        from .service import suppress_events
        with suppress_events():
            set_state(live.name, STATE_ARCHIVED)
    except Exception:
        pass


def _record_rejection(slug: str, reason: str) -> None:
    logger.warning("kynver skill sync: rejected pulled skill %s: %s", slug, reason)
    st = load_state()
    rej: list[dict[str, str]] = [r for r in st.get("rejections", []) if r.get("slug") != slug]
    rej.append({"slug": str(slug)[:64], "reason": reason[:200]})
    update_state(rejections=rej[-MAX_REJECTIONS_KEPT:])


def apply_pulled(item: dict[str, Any]) -> str:
    """Apply one pull item. Returns applied|unchanged|pending|archived|rejected."""
    slug = item.get("slug") if isinstance(item, dict) else None
    try:
        bundle = bundle_from_pull(item)
    except BundleError as exc:
        _record_rejection(str(slug), str(exc))
        return "rejected"
    status = str(item.get("status") or "active")
    server_hash = bundle.content_hash
    live = locate_skill_dir(slug)
    arch = None if live else locate_archived_dir(slug)

    if live is not None:
        side = read_sidecar(live)
        local_hash = _local_hash(live)
        if local_hash == server_hash and status != "archived":
            update_sidecar(live, baseHash=server_hash, status=status, pending=None)
            clear_server_copy(live)
            return "unchanged"
        base = side.get("baseHash")
        protected = (
            bool(side.get("pending"))
            or outbox.has(slug)
            or base is None  # never synced here: H7 decides, not a background pull
            or local_hash != base
        )
        if protected:
            write_server_copy(live, {
                "contentHash": server_hash, "status": status, "skillMd": bundle.skill_md,
                "files": bundle.files, "receivedAt": int(time.time()),
            })
            pending = side.get("pending") or {"state": "diverged"}
            update_sidecar(live, pending=pending, serverHash=server_hash)
            return "pending"
        if status == "archived":
            _archive_live(slug, live)
            return "archived"
        install_bundle(slug, live, bundle)
        write_sidecar(live, {"baseHash": server_hash, "status": status, "source": "pull",
                             "appliedAt": int(time.time())})
        return "applied"

    # Not live locally.
    if status == "archived":
        if arch is None:
            dest = archive_root() / slug
            if not dest.exists():
                install_bundle(slug, dest, bundle)
                write_sidecar(dest, {"baseHash": server_hash, "status": status, "source": "pull",
                                     "appliedAt": int(time.time())})
        elif _local_hash(arch) != server_hash:
            install_bundle(slug, arch, bundle)
            write_sidecar(arch, {"baseHash": server_hash, "status": status, "source": "pull",
                                 "appliedAt": int(time.time())})
        return "archived"
    if arch is not None:
        dest = skills_root() / slug
        if dest.exists():
            return "pending"
        _move(arch, dest)
        install_bundle(slug, dest, bundle)
    else:
        dest = skills_root() / slug
        install_bundle(slug, dest, bundle)
    write_sidecar(dest, {"baseHash": server_hash, "status": status, "source": "pull",
                         "appliedAt": int(time.time())})
    return "applied"


class Refresher:
    def __init__(self, client: Any, source_id: str, *, timeout: float = DEFAULT_TIMEOUT,
                 allow_initial_pull: bool = False):
        self.client = client
        self.source_id = source_id
        self.timeout = timeout
        self.allow_initial_pull = allow_initial_pull

    def _q(self, **params: Any) -> str:
        return urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})

    def refresh_once(self) -> dict[str, Any]:
        """One refresh cycle. Never raises."""
        result: dict[str, Any] = {"status": "ok", "applied": 0, "pending": 0, "archived": 0,
                                  "rejected": 0, "unchanged": 0}
        try:
            recover_interrupted()
            st = load_state()
            if st.get("cursor") is None and not st.get("initialized") and not self.allow_initial_pull:
                update_state(lastCheckAt=int(time.time()), lastResult="awaiting_initial_migration")
                result["status"] = "awaiting_initial_migration"
                return result
            self._resolve_pending_proposals()
            ob = outbox.stats()
            headers = {"If-None-Match": st["etag"]} if st.get("etag") else {}
            code, resp_headers, _ = self.client.request_raw(
                "GET",
                "/skills/sync/manifest?" + self._q(
                    sourceId=self.source_id, outboxDepth=ob["depth"],
                    outboxOldestAgeSeconds=ob["oldestAgeSeconds"], outboxParked=ob["parked"]),
                headers=headers, timeout=self.timeout)
            now = int(time.time())
            if code == 304:
                update_state(lastCheckAt=now, lastResult="not_modified")
                result["status"] = "not_modified"
                return result
            new_etag = resp_headers.get("etag")
            cursor = st.get("cursor")
            applied_any = False
            for _ in range(MAX_PAGES_PER_REFRESH):
                _, _, page = self.client.request_raw(
                    "GET", "/skills/sync/pull?" + self._q(sourceId=self.source_id, cursor=cursor, limit=50),
                    timeout=self.timeout)
                if not isinstance(page, dict):
                    raise RuntimeError("unexpected pull response")
                for item in page.get("skills") or []:
                    try:
                        outcome = apply_pulled(item)
                    except Exception as exc:  # IO failure: stop here, retry the page next time
                        logger.warning("kynver skill sync: apply failed for %s: %s",
                                       (item or {}).get("slug"), exc)
                        result["status"] = "apply_error"
                        update_state(lastCheckAt=now, lastResult="apply_error")
                        return result
                    result[outcome] = result.get(outcome, 0) + 1
                    applied_any = applied_any or outcome in ("applied", "archived")
                cursor = page.get("nextCursor") or cursor
                update_state(cursor=cursor, initialized=True)
                if not page.get("hasMore"):
                    break
            else:
                result["status"] = "more_pending"
                update_state(lastCheckAt=now, lastResult="more_pending")
                return result
            if applied_any:
                try:
                    from agent.prompt_builder import clear_skills_system_prompt_cache
                    clear_skills_system_prompt_cache(clear_snapshot=True)
                except Exception:
                    pass
            update_state(etag=new_etag, lastPullAt=now, lastCheckAt=now, lastResult="pulled")
        except Exception as exc:
            logger.info("kynver skill sync: refresh skipped (%s)", exc)
            update_state(lastCheckAt=int(time.time()), lastResult=f"error: {str(exc)[:120]}")
            result["status"] = "error"
        return result

    def _resolve_pending_proposals(self) -> None:
        """Clear 'pending review' markers once the owner decided the proposal."""
        st = load_state()
        pend = st.get("pendingProposals") or {}
        for slug, info in list(pend.items()):
            try:
                _, _, body = self.client.request_raw(
                    "GET", "/skills/proposals?" + self._q(skill=slug, status="applied,rejected,reverted", limit=20),
                    timeout=self.timeout)
            except Exception:
                return
            proposals = (body or {}).get("proposals") if isinstance(body, dict) else None
            decided = next((p for p in proposals or [] if p.get("id") == info.get("proposalId")), None)
            if not decided:
                continue
            live = locate_skill_dir(slug)
            if live is not None:
                if decided.get("status") == "applied":
                    # Accepted: the server now holds what we pushed, so it is the new base.
                    update_sidecar(live, baseHash=info.get("pushedHash"), pending=None, serverHash=None)
                    clear_server_copy(live)
                else:
                    # Declined: keep the local edit, flagged. (Replacing it with the server
                    # copy needs a single-skill read endpoint; see PR notes.)
                    update_sidecar(live, pending={"state": "declined", "proposalId": info.get("proposalId")})
            pend.pop(slug, None)
            update_state(pendingProposals=pend)
