"""Source ids for Kynver writes, taken from the running profile (``hermes:<slug>``).

The id comes from, in order: ``kynver.source_id`` in the profile's ``config.yaml`` (a full id), then
``kynver.agent_name`` there, the name the profile's ``SOUL.md`` gives the agent ("Your name is
Shade"), then the profile name. An unnamed default profile sets NO source id: the write goes without
one and Kynver tags it with the agent the API key is bound to (the key's credential binding). Nothing
here assumes Forge. Flush writes use ``<source>:flush``.

Resolved from ``get_hermes_home()`` per call (one process serves many profiles) and cached per home
keyed on the two files' mtimes, so an edit to the name takes effect without a restart.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home, profile_name_for_home

FLUSH_SUFFIX = ":flush"
# Skill sync must name an actor (an unbound key claims the first one it sends).
SKILL_SYNC_FALLBACK_SOURCE_ID = "hermes:agent"

_SOUL_NAME_RE = re.compile(
    r"\byour\s+name\s+is\s+[\"“']?([A-Za-z0-9][\w-]*)", re.IGNORECASE
)
_SOURCE_ID_RE = re.compile(r"^hermes:[a-z0-9][a-z0-9_.-]*$")
_cache: dict[tuple, tuple[Optional[str], str]] = {}


def slugify(name: object) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:32].strip("-")
    )


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _kynver_config() -> dict:
    from hermes_cli.config import load_config_readonly

    kynver = load_config_readonly().get("kynver")
    return kynver if isinstance(kynver, dict) else {}


def _soul_name(home: Path) -> str:
    try:
        match = _SOUL_NAME_RE.search(
            (home / "SOUL.md").read_text(encoding="utf-8")[:4000]
        )
    except OSError:
        return ""
    return match.group(1) if match else ""


def _resolve(home: Path) -> tuple[Optional[str], str]:
    """``(source_id or None, display name)`` for ``home``."""
    cfg = _kynver_config()
    configured_name = str(cfg.get("agent_name") or "").strip()
    explicit = str(cfg.get("source_id") or "").strip().lower()
    explicit = explicit.removesuffix(FLUSH_SUFFIX)
    if _SOURCE_ID_RE.match(explicit):
        return explicit, configured_name or explicit.split(":", 1)[1]
    profile = profile_name_for_home(home)
    for name in (
        configured_name,
        _soul_name(home),
        profile if profile and profile != "default" else "",
    ):
        slug = slugify(name)
        if slug:
            return f"hermes:{slug}", name
    return None, ""


def _resolved() -> tuple[Optional[str], str]:
    home = get_hermes_home()
    key = (str(home), _mtime(home / "config.yaml"), _mtime(home / "SOUL.md"))
    cached = _cache.get(key)
    if cached is None:
        cached = _cache[key] = _resolve(home)
    return cached


def resolve_source_id() -> Optional[str]:
    """``hermes:<slug>`` for this profile, or None to let Kynver infer it from the key."""
    return _resolved()[0]


def resolve_flush_source_id() -> Optional[str]:
    source = resolve_source_id()
    return source + FLUSH_SUFFIX if source else None


def agent_slug() -> Optional[str]:
    source = resolve_source_id()
    return source.split(":", 1)[1] if source else None


def agent_display_name() -> Optional[str]:
    """The agent's name for provenance (``callSign``), or None when unnamed."""
    source, name = _resolved()
    if not source or not name:
        return None
    return name[:1].upper() + name[1:]


def context_tag() -> str:
    slug = agent_slug()
    return f"hermes-{slug}" if slug else "hermes"


def skill_sync_source_id() -> str:
    """Skill sync always sends an actor id; an unnamed profile uses a neutral one."""
    return resolve_source_id() or SKILL_SYNC_FALLBACK_SOURCE_ID
