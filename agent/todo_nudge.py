"""Tool-result nudge that keeps the todo list tracking background work.

When a tool call starts work that outlives the call — ``delegate_task(background=true)``, a
``terminal`` background job with ``notify_on_complete``, or any command that prints
``[background-work-started: <name>]`` (``~/.hermes/scripts/launch-worker.sh`` does) — and the
session's todo list has no active item for it, a one-line hint is attached to THAT tool
result. Cache-safe by construction: nothing touches the system prompt or injects a message,
and the list is read from the store's last snapshot (no Kynver round trip). Each piece of
work is nudged at most once per agent.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

BACKGROUND_WORK_MARKER = "[background-work-started:"
_MARKER_RE = re.compile(r"\[background-work-started:\s*([^\]\n]{1,120})\]")
_DELEGATE_CONTROL_ACTIONS = frozenset({"list", "steer", "stop", "status", "cancel"})
_ACTIVE = frozenset({"pending", "in_progress"})
_LABEL_CHARS = 80
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{4,}")
_STOP_WORDS = frozenset({
    "about", "after", "again", "being", "check", "every", "first", "from", "into", "make",
    "other", "should", "their", "there", "these", "thing", "those", "until", "using", "which",
    "while", "with", "would", "worker", "background", "hermes", "please",
})


def _preview(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= _LABEL_CHARS else text[: _LABEL_CHARS - 1] + "…"


def background_work_labels(function_name: str, args: Dict[str, Any], result_text: str) -> List[str]:
    """Names of background work this call started (explicit markers first)."""
    labels: List[str] = [m.strip() for m in _MARKER_RE.findall(result_text or "")]
    args = args if isinstance(args, dict) else {}
    if function_name == "delegate_task" and args.get("background"):
        action = str(args.get("action") or "").strip().lower()
        if action not in _DELEGATE_CONTROL_ACTIONS:
            tasks = args.get("tasks") if isinstance(args.get("tasks"), list) else [{"goal": args.get("goal")}]
            labels.extend(_preview(t.get("goal")) for t in tasks if isinstance(t, dict) and t.get("goal"))
    elif function_name == "terminal" and args.get("background") and args.get("notify_on_complete"):
        if args.get("command"):
            labels.append(_preview(args["command"]))
    seen, out = set(), []
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _is_tracked(label: str, active_text: str) -> bool:
    low = label.lower()
    if low in active_text:
        return True
    bare = low[len("worker-"):] if low.startswith("worker-") else low
    if bare and bare in active_text:
        return True
    words = [w for w in _WORD_RE.findall(low) if w not in _STOP_WORDS][:6]
    if not words:
        return False
    return sum(1 for w in words if w in active_text) * 2 >= len(words)


def untracked_work(labels: Iterable[str], todos: Iterable[Dict[str, Any]]) -> List[str]:
    active_text = " ".join(
        str(t.get("content") or "").lower() for t in todos if isinstance(t, dict) and t.get("status") in _ACTIVE
    )
    return [label for label in labels if not _is_tracked(label, active_text)]


def todo_tracking_hint(agent: Any, function_name: str, args: Dict[str, Any], result: Any) -> Optional[str]:
    """The hint for this tool result, or None."""
    text = result if isinstance(result, str) else ""
    if function_name not in {"delegate_task", "terminal"} and BACKGROUND_WORK_MARKER not in text:
        return None
    store = getattr(agent, "_todo_store", None)
    valid = getattr(agent, "valid_tool_names", None)
    if store is None or (valid is not None and "todo_list" not in valid):
        return None
    labels = background_work_labels(function_name, args, text)
    if not labels:
        return None
    try:
        todos = (store.snapshot() or {}).get("todos") or []
    except Exception:
        return None
    nudged = getattr(agent, "_todo_nudged_work", None)
    if not isinstance(nudged, set):
        nudged = set()
        agent._todo_nudged_work = nudged
    missing = [label for label in untracked_work(labels, todos) if label not in nudged]
    if not missing:
        return None
    nudged.update(missing)
    names = "; ".join(missing[:3]) + (f"; +{len(missing) - 3} more" if len(missing) > 3 else "")
    return (f"[todo] Background work started ({names}) and your todo list does not track it. "
            "Add one in_progress item per worker now and mark it ✓/✗ when it reports back.")


def attach_hint(result: Any, hint: Optional[str]) -> Any:
    """``todo_hint`` key on a JSON-object result, else the hint appended as a trailing line."""
    if not hint or not isinstance(result, str):
        return result
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        payload["todo_hint"] = hint
        return json.dumps(payload, ensure_ascii=False)
    return f"{result}\n\n{hint}"
