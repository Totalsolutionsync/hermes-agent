"""Source ids for Kynver writes, derived from the agent's name (``hermes:<slug>``).

The slug comes from, in order: ``kynver.agent_name`` in the profile's ``config.yaml``, the name the
profile's ``SOUL.md`` gives the agent ("Your name is Shade"), then the profile name; an unnamed default
profile is ``agent``. Nothing here assumes Forge: every install's agent tags its own rows. Resolved from
``get_hermes_home()`` per call (one process serves many profiles) and cached per home keyed on the two
files' mtimes, so an edit to the name takes effect without a restart.
"""

from __future__ import annotations

import re
from pathlib import Path

from hermes_constants import get_hermes_home, profile_name_for_home

FALLBACK_SLUG = "agent"
FLUSH_SUFFIX = ":flush"

_SOUL_NAME_RE = re.compile(
    r"\byour\s+name\s+is\s+[\"“']?([A-Za-z0-9][\w-]*)", re.IGNORECASE
)
_cache: dict[tuple, str] = {}


def slugify(name: object) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:32].strip("-")
    )


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _configured_name(home: Path) -> str:
    from hermes_cli.config import load_config_readonly

    kynver = load_config_readonly().get("kynver")
    return str(kynver.get("agent_name") or "") if isinstance(kynver, dict) else ""


def _soul_name(home: Path) -> str:
    try:
        match = _SOUL_NAME_RE.search(
            (home / "SOUL.md").read_text(encoding="utf-8")[:4000]
        )
    except OSError:
        return ""
    return match.group(1) if match else ""


def agent_slug() -> str:
    home = get_hermes_home()
    key = (str(home), _mtime(home / "config.yaml"), _mtime(home / "SOUL.md"))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    profile = profile_name_for_home(home)
    slug = (
        slugify(_configured_name(home))
        or slugify(_soul_name(home))
        or slugify(profile if profile != "default" else "")
        or FALLBACK_SLUG
    )
    _cache[key] = slug
    return slug


def resolve_source_id() -> str:
    return f"hermes:{agent_slug()}"


def resolve_flush_source_id() -> str:
    return resolve_source_id() + FLUSH_SUFFIX
