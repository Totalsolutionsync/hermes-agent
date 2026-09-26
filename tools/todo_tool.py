"""Todo tool: in-memory, revisioned task list for multi-step work. State lives on the
AIAgent (one per session), is re-injected after context compression, and every write bumps
a monotonic revision so UI clients can reject stale updates. One ``todo_list`` tool: pass
``todos`` to write, omit to read; every call returns the full list (past
MAX_TODO_OUTPUT_CHARS the oldest finished items are omitted) plus a ✓/✗ checklist. No
system-prompt mutation."""

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
# The list is re-read after every compression (format_for_injection), so unbounded
# content/count would defeat the compression it rides through. Caps apply equally to
# model-authored items and caller-replayed API history.
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
# Max single todo tool-result payload accepted during history hydration, so a forged
# oversized result is dropped before parsing (AIAgent._hydrate_todo_store).
MAX_TODO_RESULT_CHARS = 512_000
_TRUNCATION_MARKER = "… [truncated]"
# Persisted as ordinary message content; ContextCompressor keys on this stable header to
# tell the synthetic post-compaction row from a real user message.
TODO_INJECTION_HEADER = "[Your active task list was preserved across context compression]"
_STATUS_MARKERS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "cancelled": "[~]"}
_ACTIVE_STATUSES = {"pending", "in_progress"}
# Human-readable checklist in every tool result: done and dropped must never look alike.
CHECKLIST_MARKS = {"completed": "✓", "cancelled": "✗", "in_progress": "▶", "pending": "○"}
_CHECKLIST_CONTENT_CHARS = 100
# One todo tool result stays this small however long the list is: finished items are
# omitted oldest-first (still counted in the summary), then item text is shortened.
MAX_TODO_OUTPUT_CHARS = 16_000
_CAPPED_CONTENT_CHARS = 200
_MIN_CAPPED_CONTENT_CHARS = 40
# An in_progress item untouched this long gets a nudge in the todo result (workers run ≤3h).
STALE_IN_PROGRESS_SECONDS = 2 * 3600


class BaseTodoStore(ABC):
    """Contract for a session todo store (``agent._todo_store``). ``todo_tool``, history
    hydration (``AIAgent._hydrate_todo_store``) and the TUI ``todo_state`` all call these, and
    plugins may swap the store in (``agent/todo_store_provider.py``) — subclassing turns a
    missing method into a TypeError at construction instead of an AttributeError mid-turn."""

    @abstractmethod
    def read(self) -> List[Dict[str, str]]: ...

    @abstractmethod
    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]: ...

    @abstractmethod
    def has_items(self) -> bool: ...

    @abstractmethod
    def snapshot(self) -> Dict[str, Any]:
        """``{"todos": [...], "revision": int}``; revision is monotonic across writes."""

    @abstractmethod
    def restore(self, todos: List[Dict[str, Any]], *, revision: Any = 0) -> List[Dict[str, str]]:
        """Adopt a trusted snapshot without manufacturing a new revision."""

    @abstractmethod
    def format_for_injection(self) -> Optional[str]: ...

    def adopt(self, ref: str) -> List[Dict[str, str]]:
        """Make a shared plan handed over by id/title/link this session's list (Kynver store)."""
        raise NotImplementedError("Picking up a shared plan needs the Kynver todo store")

    def sync_info(self) -> Dict[str, Any]:
        """Where the last list came from (shared plan, or a local fallback and why)."""
        return {}

    def in_progress_since(self) -> Dict[str, float]:
        """``{id: epoch seconds}`` each in_progress item went in progress, when known."""
        return {}


class TodoStore(BaseTodoStore):
    """In-memory todo list, one per AIAgent. List position is priority; items are
    ``{id, content, status, parent?}`` — ``parent`` nests a subtask."""

    def __init__(self):
        self._items: List[Dict[str, str]] = []
        self._revision = 0
        self._since: Dict[str, float] = {}

    def _track_since(self) -> None:
        """Remember when each item went in_progress (stale-work hints)."""
        now = time.time()
        active = {i["id"] for i in self._items if i["status"] == "in_progress"}
        self._since = {i: self._since.get(i, now) for i in active}

    def in_progress_since(self) -> Dict[str, float]:
        return dict(self._since)

    def _fresh_items(self, todos: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Validate, dedupe and order a whole new list (replace / restore)."""
        return self._normalize_order([self._validate(t) for t in self._dedupe_by_id(todos)])

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        """Replace the list (default) or merge by id; returns the full list after writing."""
        before = self.read()
        if merge:
            self._merge(todos)
        else:
            self._items = self._fresh_items(todos)
        del self._items[MAX_TODO_ITEMS:]  # keep the priority head; replays can't grow unbounded
        self._sanitize_parents(self._items)
        self._track_since()
        if self._items != before:
            self._revision += 1
        return self.read()

    def _merge(self, todos: List[Dict[str, Any]]) -> None:
        """Update existing items only in the fields provided; append new ones (validated)."""
        existing = {item["id"]: item for item in self._items}
        for t in self._dedupe_by_id(todos):
            item_id = str(t.get("id", "")).strip()
            if not item_id:
                continue  # can't merge without an id
            cur = existing.get(item_id)
            if cur is None:
                validated = self._validate(t)
                existing[validated["id"]] = validated
                self._items.append(validated)
                continue
            if t.get("content"):
                cur["content"] = self._cap_content(str(t["content"]).strip())
            if t.get("status") and str(t["status"]).strip().lower() in VALID_STATUSES:
                cur["status"] = str(t["status"]).strip().lower()
            if "parent" in t:
                parent = str(t["parent"] or "").strip()
                if parent:
                    cur["parent"] = parent
                else:
                    cur.pop("parent", None)
        # Rebuild preserving original order for existing items (first occurrence wins).
        rebuilt = {item["id"]: existing.get(item["id"], item) for item in self._items}
        self._items = self._normalize_order(list(rebuilt.values()))

    def read(self) -> List[Dict[str, str]]:
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        return bool(self._items)

    def snapshot(self) -> Dict[str, Any]:
        """Full state clients can reconcile atomically."""
        return {"todos": self.read(), "revision": self._revision}

    def restore(self, todos: List[Dict[str, Any]], *, revision: Any = 0) -> List[Dict[str, str]]:
        """Restore a trusted snapshot without manufacturing a new revision."""
        self._items = self._fresh_items(todos)[:MAX_TODO_ITEMS]
        self._track_since()
        try:
            self._revision = max(0, int(revision or 0))
        except (TypeError, ValueError):
            self._revision = 0
        return self.read()

    def format_for_injection(self) -> Optional[str]:
        """Render the list for post-compression injection, or None if nothing active. Only
        pending/in_progress items are injected — finished ones make the model re-do work after
        compression. A parent is kept (with its real status marker) when any descendant is
        active so subtasks keep context."""
        if not self._items:
            return None
        children: Dict[str, List[Dict[str, str]]] = {}
        for item in self._items:
            if item.get("parent"):
                children.setdefault(item["parent"], []).append(item)

        def render(item: Dict[str, str], depth: int, out: List[str]) -> bool:
            kid_lines: List[str] = []
            has_active_kid = False
            for kid in children.get(item["id"], []):
                has_active_kid |= render(kid, depth + 1, kid_lines)
            keep = item["status"] in _ACTIVE_STATUSES or has_active_kid
            if keep:
                marker = _STATUS_MARKERS.get(item["status"], "[?]")
                out.append(f"{'  ' * depth}- {marker} {item['id']}. "
                           f"{item['content']} ({item['status']})")
                out.extend(kid_lines)
            return keep

        lines = [TODO_INJECTION_HEADER]
        for item in self._items:
            if not item.get("parent"):
                render(item, 0, lines)
        return "\n".join(lines) if len(lines) > 1 else None

    @staticmethod
    def _cap_text(content: str, limit: int) -> str:
        """Truncate to ``limit`` chars keeping the head (the actionable part) + marker."""
        if len(content) > limit:
            return content[:limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _cap_content(content: str) -> str:
        return TodoStore._cap_text(content, MAX_TODO_CONTENT_CHARS)

    @staticmethod
    def _validate(item: Dict[str, Any]) -> Dict[str, str]:
        """Normalize one item to ``{id, content, status, parent?}`` (placeholders when missing)."""
        if not isinstance(item, dict):
            return {"id": "?", "content": "(invalid item)", "status": "pending"}
        item_id = str(item.get("id", "")).strip() or "?"
        content = str(item.get("content", "")).strip()
        status = str(item.get("status", "pending")).strip().lower()
        result = {"id": item_id,
                  "content": TodoStore._cap_content(content) if content else "(no description)",
                  "status": status if status in VALID_STATUSES else "pending"}
        parent = str(item.get("parent") or "").strip()
        if parent and parent != item_id:
            result["parent"] = parent
        return result

    @staticmethod
    def _sanitize_parents(items: List[Dict[str, str]]) -> None:
        """Drop dangling parent refs and break cycles in place (such items become roots)."""
        by_id = {item["id"]: item for item in items}
        for item in items:
            if item.get("parent") and item["parent"] not in by_id:
                item.pop("parent", None)
        for item in items:
            seen, node = {item["id"]}, item
            while node.get("parent"):
                if node["parent"] in seen:
                    item.pop("parent", None)
                    break
                seen.add(node["parent"])
                node = by_id[node["parent"]]

    @staticmethod
    def _dedupe_by_id(todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Collapse duplicate ids, keeping the last occurrence in its position."""
        last_index: Dict[str, int] = {}
        for i, item in enumerate(todos):  # non-dicts get a synthetic key; _validate handles them
            key = str(item.get("id", "")).strip() if isinstance(item, dict) else f"__invalid_{i}"
            last_index[key or "?"] = i
        return [todos[i] for i in sorted(last_index.values())]

    @staticmethod
    def _normalize_order(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Lift the in_progress step ahead of any earlier pending placeholder. Nested lists
        keep authored order — reordering would tear a subtask from its siblings."""
        statuses = [item["status"] for item in items]
        if any(item.get("parent") for item in items) or "in_progress" not in statuses:
            return items
        active_index = statuses.index("in_progress")
        if "pending" not in statuses[:active_index]:
            return items
        normalized = items.copy()
        normalized.insert(statuses.index("pending"), normalized.pop(active_index))
        return normalized


def format_checklist(items: List[Dict[str, str]]) -> str:
    """``✓ done`` / ``✗ dropped`` / ``▶ doing`` / ``○ to do`` lines, subtasks indented; an item
    someone else changed (another chat, a cron, Kynver) ends with ``— by <who>``."""
    by_id = {item.get("id"): item for item in items}
    lines = []
    for item in items:
        depth, node, seen = 0, item, set()
        while node.get("parent") in by_id and node["parent"] not in seen and depth < 4:
            seen.add(node["parent"])
            node = by_id[node["parent"]]
            depth += 1
        text = " ".join(str(item.get("content") or item.get("id") or "").split())
        if len(text) > _CHECKLIST_CONTENT_CHARS:
            text = text[:_CHECKLIST_CONTENT_CHARS - 1] + "…"
        status = item.get("status", "pending")
        suffix = " (dropped)" if status == "cancelled" else ""
        if item.get("by"):
            suffix += f" — by {item['by']}"
        lines.append(f"{'  ' * depth}{CHECKLIST_MARKS.get(status, '○')} {text}{suffix}")
    return "\n".join(lines)


def stale_in_progress_hint(items: List[Dict[str, str]], since: Dict[str, float],
                           now: Optional[float] = None) -> Optional[str]:
    """Nudge for in_progress items untouched for STALE_IN_PROGRESS_SECONDS, else None."""
    now = time.time() if now is None else now
    stale = [(i, now - since[i["id"]]) for i in items
             if i.get("status") == "in_progress" and i.get("id") in since
             and now - since[i["id"]] >= STALE_IN_PROGRESS_SECONDS]
    if not stale:
        return None
    names = ", ".join(f"'{' '.join(i['content'].split())[:60]}' ({int(age // 3600)}h)" for i, age in stale[:3])
    more = f" and {len(stale) - 3} more" if len(stale) > 3 else ""
    return (f"In progress with no update: {names}{more}. Still running? Mark each ✓/✗ "
            "or note what it is waiting on.")


def _todo_payload(items: List[Dict[str, str]], revision: Any, *, sync: Optional[Dict[str, Any]] = None,
                  hint: Optional[str] = None) -> str:
    summary: Dict[str, int] = {"total": len(items)}
    for status in ("pending", "in_progress", "completed", "cancelled"):
        summary[status] = sum(1 for i in items if i["status"] == status)

    def dump(shown: List[Dict[str, str]]) -> str:
        body: Dict[str, Any] = {"todos": shown, "revision": revision, "summary": summary,
                                "checklist": format_checklist(shown)}
        if len(shown) < len(items):
            body["omitted_finished"] = len(items) - len(shown)
        if sync:
            body["sync"] = sync
        if hint:
            body["hint"] = hint
        return json.dumps(body, ensure_ascii=False)

    out = dump(items)
    if len(out) <= MAX_TODO_OUTPUT_CHARS:
        return out
    shown = list(items)
    for item in [i for i in items if i["status"] not in _ACTIVE_STATUSES]:
        shown.remove(item)
        out = dump(shown)
        if len(out) <= MAX_TODO_OUTPUT_CHARS:
            return out
    # Only unfinished items are left and they are never dropped: shorten their text instead.
    limit, full = _CAPPED_CONTENT_CHARS, shown
    while True:
        shown = [dict(i, content=TodoStore._cap_text(i["content"], limit)) for i in full]
        out = dump(shown)
        if len(out) <= MAX_TODO_OUTPUT_CHARS or limit <= _MIN_CAPPED_CONTENT_CHARS:
            return out
        limit //= 2


def todo_tool(todos: Optional[List[Dict[str, Any]]] = None, merge: bool = False,
              store: Optional[BaseTodoStore] = None, plan: Optional[str] = None) -> str:
    """Write ``todos`` (replace, or ``merge`` by id) or read when None -> list + summary JSON.
    ``plan`` first adopts a shared plan handed over (Kynver store)."""
    if store is None:
        return tool_error("TodoStore not initialized")
    if todos is not None:
        if isinstance(todos, str):  # LLMs sometimes send a JSON string instead of a list
            try:
                todos = json.loads(todos)
            except (json.JSONDecodeError, TypeError):
                return tool_error("todos must be a list of objects, got unparseable string")
        if not isinstance(todos, list):
            return tool_error(f"todos must be a list, got {type(todos).__name__}")
    try:
        items = store.adopt(plan.strip()) if isinstance(plan, str) and plan.strip() else None
        if todos is not None:
            items = store.write(todos, merge)
        elif items is None:
            items = store.read()
    except (NotImplementedError, ValueError) as exc:  # ValueError: a transition the store refused
        return tool_error(str(exc))
    return _todo_payload(items, store.snapshot()["revision"], sync=store.sync_info(),
                         hint=stale_in_progress_hint(items, store.in_progress_since()))


def check_todo_requirements() -> bool:
    """Todo tool has no external requirements -- always available."""
    return True


# Behavioral guidance is baked into the (static, cached) description; item shape and merge
# semantics live ONLY in the parameter schema.
TODO_SCHEMA = {
    "name": "todo_list",
    "description": (
        # See #95681. Kept short: this schema rides on every call.
        "Your working memory for everything you are tracking in this chat: multi-step work "
        "(3+ steps; one item per instance for 'all N' tasks), background workers (one item "
        "each, in_progress while it runs; any number can be in progress) and things waiting "
        "on the user ('Needs <user>: …'). Read it (no args) when resuming work. Update it in "
        "the same turn something starts, finishes or fails. Order is priority; nest subtasks "
        "with parent. Mark completed only after verifying; if dropped or failed, cancel it "
        "(✗, distinct from ✓) and add the replacement. Returns the current list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string"
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"]
                        },
                        "parent": {
                            "type": "string",
                            "description": "Optional id of another item, making this a nested subtask. Omit for top-level."
                        }
                    },
                    "required": ["id", "content", "status"]
                }
            },
            "plan": {
                "type": "string",
                "description": "Pick up a shared plan handed to you (Kynver plan id, title or link; "
                               "session:<id> for another chat's list). Its items become this list."
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id, add new ones. "
                    "false (default): replace the entire list with a fresh plan."
                ),
                "default": False
            }
        },
        "required": []
    }
}


from tools.registry import registry, tool_error

registry.register(
    name="todo_list", toolset="todo", schema=TODO_SCHEMA, check_fn=check_todo_requirements,
    handler=lambda args, **kw: todo_tool(
        todos=args.get("todos"), merge=args.get("merge", False), store=kw.get("store"),
        plan=args.get("plan")),
    emoji="📋")
