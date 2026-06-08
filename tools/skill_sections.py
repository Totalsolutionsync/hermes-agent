"""Section-aware parsing and truncation for SKILL.md bodies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


_SECTION_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Sections that must never be truncated when max_bytes applies.
_DEFAULT_MANDATORY_SECTIONS = frozenset(
    {
        "overview",
        "when to use",
        "safety",
        "pitfalls",
        "verification",
    }
)


@dataclass(frozen=True)
class SkillSection:
    title: str
    level: int
    body: str
    start: int
    end: int


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip()).lower()


def get_mandatory_sections(frontmatter: Dict[str, Any]) -> Set[str]:
    """Return normalized mandatory section titles from skill frontmatter."""
    mandatory = set(_DEFAULT_MANDATORY_SECTIONS)
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return mandatory

    hermes_meta = metadata.get("hermes")
    if not isinstance(hermes_meta, dict):
        return mandatory

    raw = hermes_meta.get("mandatory_sections")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                mandatory.add(_normalize_title(item))
    return mandatory


def parse_markdown_sections(content: str) -> List[SkillSection]:
    """Split markdown *content* into sections by ATX headings."""
    matches = list(_SECTION_RE.finditer(content))
    if not matches:
        return []

    sections: List[SkillSection] = []
    preamble = content[: matches[0].start()].strip()
    if preamble:
        sections.append(
            SkillSection(
                title="Preamble",
                level=0,
                body=preamble,
                start=0,
                end=matches[0].start(),
            )
        )

    for idx, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        sections.append(SkillSection(title=title, level=level, body=body, start=start, end=end))
    return sections


def build_section_index(content: str) -> List[Dict[str, Any]]:
    """Return lightweight section manifest entries for summary responses."""
    return [
        {
            "title": section.title,
            "level": section.level,
            "chars": len(section.body),
        }
        for section in parse_markdown_sections(content)
    ]


def find_section(content: str, section_name: str) -> Optional[SkillSection]:
    """Return the section whose title matches *section_name* (case-insensitive)."""
    target = _normalize_title(section_name)
    for section in parse_markdown_sections(content):
        if _normalize_title(section.title) == target:
            return section
    return None


def extract_section_content(content: str, section_name: str) -> Optional[str]:
    section = find_section(content, section_name)
    return section.body if section else None


def truncate_sections(
    content: str,
    *,
    max_bytes: int,
    mandatory_sections: Optional[Set[str]] = None,
) -> tuple[str, bool]:
    """Truncate non-mandatory sections to fit *max_bytes* UTF-8 budget."""
    if max_bytes <= 0:
        return content, False

    mandatory = mandatory_sections or _DEFAULT_MANDATORY_SECTIONS
    sections = parse_markdown_sections(content)
    if not sections:
        encoded = content.encode("utf-8")
        if len(encoded) <= max_bytes:
            return content, False
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return truncated.rstrip() + "\n...[truncated]", True

    parts: List[str] = []
    used = 0
    truncated_any = False

    for section in sections:
        is_mandatory = _normalize_title(section.title) in mandatory
        body = section.body
        body_bytes = len(body.encode("utf-8"))

        if is_mandatory or used + body_bytes <= max_bytes:
            parts.append(body)
            used += body_bytes
            continue

        remaining = max_bytes - used
        if remaining <= 0:
            truncated_any = True
            parts.append(f"## {section.title}\n\n...[section omitted — use section= to load]")
            continue

        slice_bytes = body.encode("utf-8")[:remaining]
        clipped = slice_bytes.decode("utf-8", errors="ignore").rstrip()
        parts.append(clipped + "\n...[section truncated]")
        truncated_any = True
        used = max_bytes

    return "\n\n".join(parts), truncated_any
