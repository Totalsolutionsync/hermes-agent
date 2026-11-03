"""Which Kynver skills belong in this Hermes install (H6).

A skill declares ``metadata.kynver.runtime`` (``hermes`` | ``any`` | another
runtime such as ``claude-code``) and, optionally, ``metadata.kynver.bindings``
(the agents / sync sources it is bound to). Hermes' skill index must only show
skills it can use, so the pull skips everything else instead of caching it:

* runtime ``hermes`` or ``any`` -> in scope; a missing or unknown value is
  treated as ``any`` (never hide a skill because of a value we do not know);
* a non-empty ``bindings`` list narrows the skill to the agents it names; an
  empty or absent list means every agent ("any").

The metadata comes from the pull item's ``metadata`` object when the server
sends one, else from the SKILL.md frontmatter.
"""

from __future__ import annotations

from typing import Any, Optional

SHOWN_RUNTIMES = frozenset({"hermes", "any"})
KNOWN_OTHER_RUNTIMES = frozenset({"claude-code", "claude", "codex", "cursor", "openclaw"})


def frontmatter(skill_md: str) -> dict[str, Any]:
    text = (skill_md or "").lstrip("﻿")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    import hermes_yaml

    try:
        data = hermes_yaml.safe_load(text[3:end])
    except hermes_yaml.YAMLError:  # malformed frontmatter: treat as no metadata
        return {}
    return data if isinstance(data, dict) else {}


def kynver_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """``metadata.kynver`` from the payload, falling back to the SKILL.md frontmatter."""
    for meta in (item.get("metadata"), frontmatter(str(item.get("skillMd") or "")).get("metadata")):
        k = meta.get("kynver") if isinstance(meta, dict) else None
        if isinstance(k, dict):
            return k
    return {}


def _agent_names(source_id: str) -> set[str]:
    sid = (source_id or "").strip().lower()
    return {n for n in (sid, sid.split(":", 1)[-1]) if n}


def out_of_scope_reason(item: dict[str, Any], source_id: str) -> Optional[str]:
    """Why ``item`` must not be pulled into this install, else None."""
    k = kynver_metadata(item)
    runtime = str(k.get("runtime") or "any").strip().lower()
    if runtime not in SHOWN_RUNTIMES and runtime in KNOWN_OTHER_RUNTIMES:
        return f"runtime is {runtime}"
    raw = k.get("bindings")
    bindings = {str(b).strip().lower() for b in raw if str(b).strip()} if isinstance(raw, list) else set()
    if bindings and not (bindings & (_agent_names(source_id) | {"hermes", "any"})):
        return "not bound to this agent"
    return None
