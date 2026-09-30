"""Whole-item context budgeting for the Kynver memory provider.

Budgets are expressed as approximate tokens (UTF-8 bytes / 4).  A memory is
either rendered in full or left out; its body is never shortened to make it fit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable


DEFAULT_PREFETCH_TOKEN_BUDGET = 2_000
DEFAULT_SEARCH_TOKEN_BUDGET = 4_000
DEFAULT_RELEVANCE_SCORE_FLOOR = 0.01
DEFAULT_MAX_INDEX_ITEMS = 8


@dataclass(frozen=True)
class KynverContextSettings:
    prefetch_token_budget: int = DEFAULT_PREFETCH_TOKEN_BUDGET
    search_token_budget: int = DEFAULT_SEARCH_TOKEN_BUDGET
    relevance_score_floor: float = DEFAULT_RELEVANCE_SCORE_FLOOR
    max_index_items: int = DEFAULT_MAX_INDEX_ITEMS


@dataclass(frozen=True)
class WholeItemFit:
    included: list[dict[str, Any]]
    omitted: list[dict[str, str]]
    markdown: str
    estimated_tokens: int


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def load_context_settings() -> KynverContextSettings:
    """Read behavioral knobs from top-level ``kynver`` in config.yaml."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        config = {}
    kynver = config.get("kynver") if isinstance(config, dict) else {}
    if not isinstance(kynver, dict):
        kynver = {}
    return KynverContextSettings(
        prefetch_token_budget=_bounded_int(
            kynver.get("prefetch_token_budget"),
            DEFAULT_PREFETCH_TOKEN_BUDGET,
            minimum=128,
            maximum=32_000,
        ),
        search_token_budget=_bounded_int(
            kynver.get("search_token_budget"),
            DEFAULT_SEARCH_TOKEN_BUDGET,
            minimum=128,
            maximum=64_000,
        ),
        relevance_score_floor=_bounded_float(
            kynver.get("relevance_score_floor"),
            DEFAULT_RELEVANCE_SCORE_FLOOR,
            minimum=0.0,
            maximum=1.0,
        ),
        max_index_items=_bounded_int(
            kynver.get("max_index_items"),
            DEFAULT_MAX_INDEX_ITEMS,
            minimum=0,
            maximum=50,
        ),
    )


def estimate_tokens(text: str) -> int:
    """Cheap deterministic estimate used only for context admission."""
    return (len(text.encode("utf-8")) + 3) // 4


def item_key(item: dict[str, Any]) -> str:
    for key in ("key", "slug", "id", "sourceId"):
        value = item.get(key)
        if value:
            return str(value).strip()
    return ""


def item_text(item: dict[str, Any]) -> str:
    # contentPreview is intentionally absent: a server preview is an index, not
    # authoritative recalled content.
    for key in ("content", "text", "memory", "summary", "description", "title"):
        value = item.get(key)
        if value:
            return str(value).strip()
    return ""


def item_fingerprint(item: dict[str, Any]) -> str:
    return hashlib.sha256(item_text(item).encode("utf-8")).hexdigest()


def relevance_score(item: dict[str, Any]) -> float | None:
    for key in ("rrfScore", "score", "similarity"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def relevant_items(items: Iterable[dict[str, Any]], score_floor: float) -> list[dict[str, Any]]:
    selected = []
    for item in items:
        score = relevance_score(item)
        if score is not None and score < score_floor and not bool(item.get("queryMatched")):
            continue
        selected.append(item)
    return selected


def _title(item: dict[str, Any]) -> str:
    for key in ("title", "summary"):
        value = item.get(key)
        if value:
            return " ".join(str(value).split())
    return ""


def _index_entry(item: dict[str, Any]) -> dict[str, str]:
    key = item_key(item) or "unknown"
    return {
        "key": key,
        "title": _title(item),
        "expand": f'kynver_memory_search(key="{key}")',
    }


def fit_whole_items(
    items: Iterable[dict[str, Any]],
    *,
    token_budget: int,
    max_index_items: int,
    header: str = "## Kynver AgentOS Context\nAuthoritative runtime memory for Hermes Forge.",
) -> WholeItemFit:
    """Render ranked items whole; skip an item when its complete line will not fit."""
    included: list[dict[str, Any]] = []
    omitted_items: list[dict[str, Any]] = []
    lines = [header]
    used = estimate_tokens(header)

    for item in items:
        text = item_text(item)
        if not text:
            omitted_items.append(item)
            continue
        key = item_key(item)
        suffix = f" [{key}]" if key else ""
        line = f"- {text}{suffix}"
        cost = estimate_tokens("\n" + line)
        if used + cost <= token_budget:
            lines.append(line)
            used += cost
            included.append(item)
        else:
            omitted_items.append(item)

    candidate_index = [_index_entry(item) for item in omitted_items[:max_index_items]]
    omitted: list[dict[str, str]] = []
    if candidate_index:
        index_lines = ["", "## INDEX — full memories available on demand"]
        # Index entries are small metadata handles. Respect the same budget by
        # adding only complete lines; never evict or cut an included memory.
        for line in index_lines:
            cost = estimate_tokens("\n" + line)
            if used + cost > token_budget:
                return WholeItemFit(
                    included=included,
                    omitted=omitted,
                    markdown="\n".join(lines) if included else "",
                    estimated_tokens=used,
                )
            lines.append(line)
            used += cost
        for row in candidate_index:
            title = f" — {row['title']}" if row["title"] else ""
            line = f"- `{row['key']}`{title}; expand with `{row['expand']}`"
            cost = estimate_tokens("\n" + line)
            if used + cost > token_budget:
                break
            lines.append(line)
            used += cost
            omitted.append(row)

    markdown = "\n".join(lines) if included or omitted else ""
    return WholeItemFit(included=included, omitted=omitted, markdown=markdown, estimated_tokens=used)
