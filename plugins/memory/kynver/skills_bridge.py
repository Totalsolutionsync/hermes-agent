"""AgentOS skills projection for Hermes skill surfaces."""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

from .agentos_bridge import KynverAgentOSClient, KynverAgentOSError

logger = logging.getLogger(__name__)


def _coerce_skills(payload: Any) -> List[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [s for s in payload if isinstance(s, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("skills", "items", "manifest", "results"):
        val = payload.get(key)
        if isinstance(val, list):
            return [s for s in val if isinstance(s, dict)]
    return []


def list_agentos_skill_manifest(client: KynverAgentOSClient) -> List[dict[str, Any]]:
    try:
        payload = client.get("/skills?view=manifest")
        return _coerce_skills(payload)
    except KynverAgentOSError:
        logger.debug("AgentOS skills manifest fetch failed", exc_info=True)
        return []


def format_agentos_skills_index(skills: List[dict[str, Any]]) -> str:
    if not skills:
        return ""
    lines = [
        "# Kynver AgentOS skills (manifest)",
        "Runtime-eligible AgentOS skills. Use local ``skill_view`` for bundled Hermes skills; "
        "fetch AgentOS skill instructions on demand when a slug matches this list.",
    ]
    for skill in skills[:40]:
        slug = skill.get("slug") or skill.get("skillSlug") or skill.get("name") or "?"
        desc = (skill.get("description") or "").strip()
        if len(desc) > 80:
            desc = desc[:77] + "..."
        lines.append(f"- `{slug}`: {desc or '(no description)'}")
    if len(skills) > 40:
        lines.append(f"- … and {len(skills) - 40} more")
    return "\n".join(lines)


def get_agentos_skill(
    client: KynverAgentOSClient,
    skill_slug: str,
    *,
    source: str = "",
) -> Optional[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"source": source} if source else {}
    )
    suffix = f"?{params}" if params else ""
    try:
        payload = client.get(f"/skills/{urllib.parse.quote(skill_slug, safe='')}{suffix}")
        return payload if isinstance(payload, dict) else None
    except KynverAgentOSError:
        logger.debug("AgentOS skill fetch failed for %s", skill_slug, exc_info=True)
        return None


def create_or_update_agentos_skill(
    client: KynverAgentOSClient,
    *,
    skill_slug: str,
    name: str,
    description: str,
    instructions: str,
    create: bool = True,
) -> dict[str, Any]:
    body = {
        "name": name,
        "description": description,
        "instructions": instructions,
    }
    if create:
        return client.post("/skills", {**body, "slug": skill_slug}) or {}
    return client.patch(f"/skills/{urllib.parse.quote(skill_slug, safe='')}", body) or {}


def mirror_skill_manage_to_agentos(
    client: KynverAgentOSClient,
    action: str,
    args: Dict[str, Any],
    *,
    result_json: str,
) -> None:
    """Phase 2: mirror skill_manage create/patch to AgentOS user skills when practical."""

    if action not in {"create", "patch", "edit"}:
        return
    try:
        parsed = json.loads(result_json) if result_json else {}
    except Exception:
        parsed = {}
    if not parsed.get("success"):
        return
    name = str(args.get("name") or parsed.get("name") or "").strip()
    if not name:
        return
    slug = name.replace("/", "-").replace("_", "-").lower()
    instructions = str(args.get("instructions") or args.get("content") or parsed.get("content") or "")
    description = str(args.get("description") or parsed.get("description") or "")[:200]
    if not instructions:
        return
    try:
        create_or_update_agentos_skill(
            client,
            skill_slug=slug,
            name=name,
            description=description,
            instructions=instructions,
            create=action == "create",
        )
    except KynverAgentOSError:
        logger.debug("AgentOS skill mirror failed for %s", slug, exc_info=True)
