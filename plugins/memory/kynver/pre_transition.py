"""Client-side guards before projecting Hermes todo state to Kynver plan progress."""

from __future__ import annotations

import hashlib
import re
from typing import Any


class PreTransitionError(ValueError):
    """Todo or focus transition rejected before calling Kynver."""


# Hermes renamed the todo tool to ``todo_list``; ``todo`` stays for pre-rename replays.
TODO_TOOL_NAMES = frozenset({"todo_list", "todo"})

HERMES_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
KYNVER_ROW_STATUSES = frozenset({"todo", "in_progress", "running", "partial", "blocked", "done"})


def normalize_hermes_status(status: str) -> str:
    clean = (status or "pending").strip().lower()
    return clean if clean in HERMES_STATUSES else "pending"


HERMES_TODO_PREFIX = "hermes-todo:"
_SAFE_SCOPE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def normalize_todo_scope(raw: str) -> str:
    """Row-key segment for a session scope: the id itself when it is short and colon-free,
    else a stable hash (a colon would make the scope/id split ambiguous)."""
    clean = (raw or "").strip()
    if _SAFE_SCOPE.match(clean):
        return clean
    return "h" + hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]


def hermes_row_key(todo_id: str, scope: str) -> str:
    """``hermes-todo:<scope>:<id>`` — every session owns its own rows in the shared plan."""
    prefix = f"{HERMES_TODO_PREFIX}{scope}:"
    tid = (todo_id or "").strip() or "?"
    return tid if tid.startswith(prefix) else f"{prefix}{tid}"


def parse_hermes_row_key(row_key: str) -> tuple[str | None, str] | None:
    """``(scope, id)`` for a Hermes todo row, ``(None, id)`` for a legacy unscoped
    ``hermes-todo:<id>`` row, None for non-Hermes rows. Legacy ids never contained a colon, so
    one marks a scoped key; the id itself may contain colons."""
    if not row_key.startswith(HERMES_TODO_PREFIX):
        return None
    rest = row_key[len(HERMES_TODO_PREFIX) :]
    scope, sep, todo_id = rest.partition(":")
    if sep and scope and todo_id:
        return scope, todo_id
    return (None, rest) if rest else None


def todo_id_in_scope(row_key: str, scope: str | None) -> str | None:
    """The todo id when ``row_key`` belongs to ``scope``, else None (other sessions' rows and
    legacy unscoped rows)."""
    parsed = parse_hermes_row_key(row_key)
    if not scope or parsed is None or parsed[0] != scope:
        return None
    return parsed[1]


def assert_focus_allowed(*, row_status: str | None, next_hermes_status: str) -> None:
    if next_hermes_status != "in_progress":
        return
    if row_status == "running":
        raise PreTransitionError(
            "cannot set Hermes todo in_progress while Kynver row has executor lease (running)"
        )
    if row_status == "done":
        raise PreTransitionError("cannot set focus on a done plan row")
