"""Profile-safe storage for oversized terminal/process tool output.

Full logs live under ``<HERMES_HOME>/artifacts/tool-output/``. The model
receives a compact summary with first/last-line preview and a resolvable
handle (``hermes-artifact:tool-output/<id>``).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

HANDLE_PREFIX = "hermes-artifact:tool-output/"
DEFAULT_THRESHOLD_CHARS = 50_000
DEFAULT_PREVIEW_LINES = 8


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    handle: str
    path: Path
    byte_count: int
    line_count: int
    kind: str
    command: str
    exit_code: Optional[int]


def _artifacts_root() -> Path:
    root = get_hermes_home() / "artifacts" / "tool-output"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _split_preview_lines(text: str, *, head_lines: int, tail_lines: int) -> tuple[str, str]:
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines:
        return text, ""
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    return head, tail


def store_tool_output_artifact(
    content: str,
    *,
    kind: str = "terminal",
    command: str = "",
    exit_code: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ArtifactRecord:
    """Persist *content* under the active Hermes profile and return metadata."""
    artifact_id = uuid.uuid4().hex
    root = _artifacts_root()
    data_path = root / f"{artifact_id}.txt"
    meta_path = root / f"{artifact_id}.json"

    data_path.write_text(content, encoding="utf-8")
    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    record_meta = {
        "artifact_id": artifact_id,
        "handle": f"{HANDLE_PREFIX}{artifact_id}",
        "path": str(data_path),
        "byte_count": len(content.encode("utf-8")),
        "line_count": line_count,
        "kind": kind,
        "command": command,
        "exit_code": exit_code,
        "metadata": metadata or {},
    }
    meta_path.write_text(json.dumps(record_meta, indent=2), encoding="utf-8")

    return ArtifactRecord(
        artifact_id=artifact_id,
        handle=record_meta["handle"],
        path=data_path,
        byte_count=record_meta["byte_count"],
        line_count=line_count,
        kind=kind,
        command=command,
        exit_code=exit_code,
    )


def resolve_tool_output_artifact(handle: str) -> Optional[str]:
    """Load full artifact text for *handle*, or None if missing."""
    if not handle or not str(handle).startswith(HANDLE_PREFIX):
        return None
    artifact_id = str(handle)[len(HANDLE_PREFIX) :].strip()
    if not artifact_id:
        return None
    data_path = _artifacts_root() / f"{artifact_id}.txt"
    if not data_path.is_file():
        return None
    try:
        return data_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read artifact %s: %s", handle, exc)
        return None


def artifact_path_for_handle(handle: str) -> Optional[Path]:
    """Return the profile-local filesystem path for a handle, if it exists."""
    if not handle or not str(handle).startswith(HANDLE_PREFIX):
        return None
    artifact_id = str(handle)[len(HANDLE_PREFIX) :].strip()
    if not artifact_id:
        return None
    data_path = _artifacts_root() / f"{artifact_id}.txt"
    return data_path if data_path.is_file() else None


def compact_tool_output(
    output: str,
    *,
    threshold: int = DEFAULT_THRESHOLD_CHARS,
    kind: str = "terminal",
    command: str = "",
    exit_code: Optional[int] = None,
    preview_lines: int = DEFAULT_PREVIEW_LINES,
) -> str:
    """Return *output* unchanged when small; otherwise store artifact + preview."""
    if output is None:
        output = ""
    if len(output) <= threshold:
        return output

    try:
        record = store_tool_output_artifact(
            output,
            kind=kind,
            command=command,
            exit_code=exit_code,
        )
    except OSError as exc:
        logger.warning("Artifact store failed, falling back to inline truncation: %s", exc)
        head, tail = _split_preview_lines(output, head_lines=preview_lines, tail_lines=preview_lines)
        omitted = len(output) - len(head) - len(tail)
        notice = (
            f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted; "
            f"artifact store failed] ...\n\n"
        )
        return head + notice + tail if tail else head + notice

    head, tail = _split_preview_lines(output, head_lines=preview_lines, tail_lines=preview_lines)
    omitted = record.byte_count - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    if omitted < 0:
        omitted = record.byte_count

    parts = [
        f"[TOOL OUTPUT ARTIFACT — {record.kind}]",
        f"Full output: {record.byte_count:,} bytes ({record.line_count:,} lines).",
    ]
    if command:
        parts.append(f"Command: {command}")
    if exit_code is not None:
        parts.append(f"Exit code: {exit_code}")
    parts.extend(
        [
            f"Handle: {record.handle}",
            f"Profile path: {record.path}",
            "Retrieve with read_file on the profile path or resolve_tool_output_artifact(handle).",
            "",
            "--- first lines ---",
            head,
        ]
    )
    if tail:
        parts.extend(["", f"... [{omitted:,} bytes omitted] ...", "", "--- last lines ---", tail])
    return "\n".join(parts)
