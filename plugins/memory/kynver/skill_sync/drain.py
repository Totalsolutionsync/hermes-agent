"""Outbox drain (H2): push the dirty set to Kynver in the background.

Content is read from the cache at send time. ``onConflict`` is always
``"propose"`` and ``force`` is never sent: a stale ``baseHash`` becomes a
proposal on the server and the local edit stays, marked pending review in the
skill's sidecar. A network error backs the entry off (never parks it); a
server rejection is retried and parked after 3 tries so one bad skill cannot
block the queue.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from . import outbox
from .bundle import BundleError, read_bundle, slug_for, validate_bundle
from .store import (
    load_state,
    locate_archived_dir,
    locate_skill_dir,
    read_sidecar,
    update_sidecar,
    update_state,
)

logger = logging.getLogger(__name__)

PUSH_BATCH = 50  # Kynver SKILL_SYNC_PUSH_MAX_SKILLS
DEFAULT_TIMEOUT = 10.0


def _build_item(entry: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str]]:
    """-> (push item, hash being pushed, drop reason). Item None = nothing to send."""
    slug, kind = entry["slug"], entry["kind"]
    base = entry.get("baseHash")
    if kind in ("archive", "restore"):
        d = locate_skill_dir(slug) if kind == "restore" else None
        if kind == "archive" and base is None:
            return None, None, "never synced; nothing to archive on the server"
        if kind == "restore" and d is None:
            return None, None, "restored folder is gone"
        return {"slug": slug, "action": kind, "baseHash": base}, None, None
    d = locate_skill_dir(slug)
    if d is None:
        if locate_archived_dir(slug) is not None and base is not None:
            return {"slug": slug, "action": "archive", "baseHash": base}, None, None
        # Deleted locally: never a hard delete over sync — archive on the server.
        if base is None:
            return None, None, "created and deleted before it ever synced"
        return {"slug": slug, "action": "archive", "baseHash": base}, None, None
    bundle = read_bundle(d)
    validate_bundle(bundle)
    h = bundle.content_hash
    if h == base:
        return None, h, "unchanged since last sync"
    item: dict[str, Any] = {
        "slug": slug,
        "skillMd": bundle.skill_md,
        "files": [{"path": p, "content": c} for p, c in sorted(bundle.files.items())],
        "baseHash": base,
    }
    return item, h, None


class Drainer:
    def __init__(self, client: Any, source_id: str, *, timeout: float = DEFAULT_TIMEOUT):
        self.client = client
        self.source_id = source_id
        self.timeout = timeout

    def drain_once(self, now: Optional[float] = None) -> dict[str, Any]:
        """Send everything due. Never raises."""
        now = time.time() if now is None else now
        summary = {"sent": 0, "written": 0, "proposed": 0, "conflict": 0, "rejected": 0,
                   "parked": 0, "dropped": 0, "offline": False}
        try:
            batch: list[tuple[dict[str, Any], dict[str, Any], Optional[str]]] = []
            for entry in outbox.due(now):
                slug = entry["slug"]
                try:
                    item, pushed_hash, _drop = _build_item(entry)
                except (BundleError, OSError, UnicodeDecodeError) as exc:
                    parked = outbox.record_reject(slug, f"local bundle invalid: {exc}", now)
                    summary["parked"] += int(parked)
                    summary["rejected"] += 1
                    continue
                if item is None:
                    outbox.remove(slug)
                    summary["dropped"] += 1
                    continue
                batch.append((entry, item, pushed_hash))
                if len(batch) >= PUSH_BATCH:
                    break
            if not batch:
                return summary
            self._push(batch, summary, now)
        except Exception as exc:
            logger.info("kynver skill sync: drain skipped (%s)", exc)
            summary["offline"] = True
        return summary

    def _push(self, batch, summary: dict[str, Any], now: float) -> None:
        st = load_state()
        rejections = list(st.get("rejections") or [])
        body = {
            "sourceId": self.source_id,
            "skills": [it for _, it, _ in batch],
            "onConflict": "propose",
            "outbox": outbox.stats(now),  # numbers for the skill-sync-drift diagnostic
        }
        if rejections:
            body["pullRejections"] = rejections
        try:
            _, _, res = self.client.request_raw("POST", "/skills/sync/push", body, timeout=self.timeout)
        except Exception as exc:
            for entry, _, _ in batch:
                outbox.record_outage(entry["slug"], str(exc), now)
            summary["offline"] = True
            return
        if not isinstance(res, dict) or not isinstance(res.get("results"), list):
            for entry, _, _ in batch:
                outbox.record_outage(entry["slug"], "unexpected push response", now)
            summary["offline"] = True
            return
        if rejections:
            update_state(rejections=[])
        by_slug = {r.get("slug"): r for r in res["results"] if isinstance(r, dict)}
        summary["sent"] = len(batch)
        for entry, item, pushed_hash in batch:
            slug = entry["slug"]
            r = by_slug.get(slug)
            if r is None:
                outbox.record_outage(slug, "no result for slug", now)
                continue
            self._settle(entry, item, pushed_hash, r, summary, now)

    def _settle(self, entry, item, pushed_hash, r, summary, now) -> None:
        slug = entry["slug"]
        outcome = r.get("outcome")
        d = locate_skill_dir(slug) or locate_archived_dir(slug)
        if outcome in ("created", "updated", "unchanged"):
            new_hash = r.get("contentHash") or pushed_hash
            if d is not None:
                update_sidecar(d, baseHash=new_hash, pending=None, serverHash=None)
            self._finish(slug, new_hash, pushed_hash)
            summary["written"] += 1
        elif outcome == "proposed":
            if d is not None:
                update_sidecar(d, pending={"state": "proposal", "proposalId": r.get("proposalId"),
                                           "pushedHash": pushed_hash},
                               serverHash=r.get("serverHash"))
            pend = dict(load_state().get("pendingProposals") or {})
            pend[slug] = {"proposalId": r.get("proposalId"), "pushedHash": pushed_hash}
            update_state(pendingProposals=pend)
            outbox.remove(slug)
            summary["proposed"] += 1
        elif outcome == "conflict":
            # Server would not store a proposal (conflictMode off). Keep the local edit,
            # mark it, and park so the diagnostic shows it.
            if d is not None:
                update_sidecar(d, pending={"state": "conflict", "serverHash": r.get("serverHash")})
            outbox.park(slug, "conflict", "server hash differs and proposals are off")
            summary["conflict"] += 1
            summary["parked"] += 1
        else:  # rejected / anything unknown
            errors = "; ".join(r.get("errors") or []) or str(r.get("note") or outcome)
            parked = outbox.record_reject(slug, errors, now)
            summary["rejected"] += 1
            summary["parked"] += int(parked)

    @staticmethod
    def _finish(slug: str, new_base: Optional[str], pushed_hash: Optional[str]) -> None:
        """Entry done — unless the skill changed again while the push was in flight."""
        d = locate_skill_dir(slug)
        if d is not None and pushed_hash is not None:
            try:
                if read_bundle(d).content_hash != pushed_hash:
                    outbox.update(slug, baseHash=new_base, nextAt=0, fails=0, rejects=0)
                    return
            except (BundleError, OSError, UnicodeDecodeError):
                pass
        outbox.remove(slug)
