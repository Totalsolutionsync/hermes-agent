"""Safety-net sync + ``status`` for the ``hermes kynver skills`` CLI (H4).

``sync_now`` is the manual / cron entry point that replaces the old
``kynver-skill-sync`` script: find locally edited skills the live hooks missed
(edits made while Hermes was down, or by hand), queue them, push the outbox,
then pull. It is refused until the H7 migration has run, so it can never be the
first pull. Flags in ``kynver.skills.*`` do not gate it: running it is the
explicit request.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from . import migration, outbox, usage
from .bundle import slug_for
from .drain import Drainer
from .migration import _live_dirs, _local_hash
from .refresher import Refresher
from .store import get_base, load_state, read_sidecar


def scan_dirty() -> list[str]:
    """Queue synced skills whose files changed behind the hooks' back. Returns the slugs queued.

    Skills with a pending marker (proposal, conflict, diverged) are the server's
    to resolve and are left alone; so are skills never synced from here.
    """
    queued: list[str] = []
    for d in _live_dirs():
        slug = slug_for(d.name)
        side = read_sidecar(d)
        base = side.get("baseHash") or get_base(slug)
        if not base or side.get("pending") or outbox.has(slug):
            continue
        local = _local_hash(d)
        if local is not None and local != base:
            outbox.mark_dirty(slug, "upsert", base)
            queued.append(slug)
    return sorted(queued)


def sync_now(client: Any, source_id: str) -> dict[str, Any]:
    if not migration.is_migrated():
        return {"status": "awaiting_initial_migration",
                "hint": "run `hermes kynver skills migrate` (dry run) then `--apply`"}
    queued = scan_dirty()
    return {"status": "ok", "queued": queued,
            "push": Drainer(client, source_id).drain_once(),
            "pull": Refresher(client, source_id).refresh_once()}


def _pending_markers() -> list[dict[str, Any]]:
    out = []
    for d in _live_dirs():
        pend = read_sidecar(d).get("pending")
        if pend:
            out.append({"slug": slug_for(d.name), "state": pend.get("state")})
    return sorted(out, key=lambda e: e["slug"])


def status_report(now: Optional[float] = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    st = load_state()
    ob = outbox.stats(now)
    m = migration.load_migration()
    parked = [{"slug": e["slug"], "reason": e.get("parked"), "lastError": e.get("lastError")}
              for e in sorted(outbox.entries(), key=lambda e: e["slug"]) if e.get("parked")]
    last_pull = st.get("lastPullAt")
    return {
        "migration": m.get("phase") or "not_started",
        "outboxDepth": ob["depth"],
        "outboxPending": ob["pending"],
        "oldestAgeSeconds": ob["oldestAgeSeconds"],
        "parked": parked,
        "lastPullAt": last_pull,
        "lastPullAgeSeconds": int(now - last_pull) if isinstance(last_pull, (int, float)) else None,
        "lastCheckAt": st.get("lastCheckAt"),
        "lastResult": st.get("lastResult"),
        "pullRejections": list(st.get("rejections") or []),
        "pendingLocal": _pending_markers(),
        "usageBuffered": usage.buffered(),
    }


def format_status(r: dict[str, Any]) -> str:
    def age(sec: Optional[int]) -> str:
        return "never" if sec is None else f"{sec}s ago"
    lines = [
        f"  migration:      {r['migration']}",
        f"  outbox:         {r['outboxDepth']} queued ({r['outboxPending']} due/retrying, "
        f"{len(r['parked'])} parked), oldest {r['oldestAgeSeconds']}s",
        f"  last pull:      {age(r['lastPullAgeSeconds'])} (last check result: {r['lastResult'] or 'n/a'})",
        f"  pull rejected:  {len(r['pullRejections'])}",
        f"  local pending:  {len(r['pendingLocal'])} (awaiting review / diverged from server)",
    ]
    for p in r["parked"]:
        lines.append(f"  parked: {p['slug']} [{p['reason']}] {p['lastError'] or ''}".rstrip())
    for p in r["pullRejections"]:
        lines.append(f"  rejected on pull: {p.get('slug')}: {p.get('reason')}")
    return "\n".join(lines)
