"""Stale blocker detection for files Kynver memory does not govern.

Limitation statements ("Facebook is held because KariAd can't send the AI
label") also live in Hermes skills, worker briefs and worker reports. When the
fix ships, Kynver retires its own memory of the limitation, but a copied brief
or a skill keeps repeating it. This module finds those statements and asks
Kynver's ``/memory/limitations/check`` (batch) whether each is still open,
already fixed, or unverified.

Local matching is only a cheap prefilter; Kynver decides. Worker reports are
history and are never rewritten: only files under a skills directory can be
annotated, and only with an appended note (the original line is kept).
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from .contract import LIMITATION_CHECK_PATH

BATCH_SIZE = 50
MAX_FILE_BYTES = 2_000_000
SCAN_SUFFIXES = {".md", ".markdown", ".txt"}
FIX_NOTE_PREFIX = "> **Kynver: FIXED**"

# Broad prefilter mirroring Kynver's limitation phrase table (can't/cannot,
# not supported, held/hold ... because/until/for, blocked by, workaround,
# until X lands). False positives cost one batch slot; Kynver drops them.
_CANDIDATE = re.compile(
    r"\b(?:can(?:'|’)?t|cannot|can not|unable to|(?:does|do|is|are|has|have)(?:n't| not)\s+(?:yet\s+)?"
    r"(?:support|send|carry|have|able)|won(?:'|’)?t|not (?:yet )?(?:supported|available|implemented|possible|wired)|"
    r"unsupported|no (?:support|way|api) (?:for|to)|held|hold(?:ing)?\b|on hold|blocked (?:on|by)|blocker|"
    r"work-?arounds?|until\b[^.\n]{0,60}\b(?:lands?|ships?|merges?|is (?:fixed|merged|deployed)))",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")
# Reported speech ("Forge said 'X can't do Y'") is not the file asserting the limitation.
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")
_MARKDOWN = re.compile(r"(\*\*|__|`|^\s*(?:[-*+]|\d+[.)])\s+|^\s*>+\s*|^#+\s*)")


@dataclass(frozen=True)
class Statement:
    path: str
    line_no: int
    text: str


@dataclass
class Finding:
    statement: Statement
    status: str
    fixed_by: Optional[str] = None
    fixed_at: Optional[str] = None
    matches: List[dict] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    # Kynver's own reading of the line; False for "Facebook is no longer held".
    is_limitation: Optional[bool] = None

    @property
    def stale(self) -> bool:
        """A line that still asserts a limitation whose fix has shipped."""
        return self.status == "fixed" and self.is_limitation is not False


def _clean(text: str) -> str:
    out = text.strip()
    if out.startswith("|"):
        out = " ".join(cell.strip() for cell in out.strip("|").split("|") if cell.strip())
    out = _MARKDOWN.sub("", out)
    return " ".join(out.split())


def find_statements(path: str, text: str) -> List[Statement]:
    """Candidate limitation sentences in one file (code fences and our own fix notes skipped)."""
    found: List[Statement] = []
    seen: set[str] = set()
    in_fence = False
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(FIX_NOTE_PREFIX):
            continue
        for sentence in _SENTENCE_SPLIT.split(_clean(raw)):
            sentence = sentence.strip()
            if len(sentence) < 12 or not _CANDIDATE.search(_QUOTED.sub(" ", sentence)):
                continue
            key = sentence.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(Statement(path=path, line_no=line_no, text=sentence[:2000]))
    return found


def iter_files(paths: Iterable[str]) -> List[Path]:
    """Expand files and directories (recursively) into scannable text files."""
    out: List[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            out.extend(
                sorted(
                    f for f in p.rglob("*")
                    if f.is_file() and f.suffix.lower() in SCAN_SUFFIXES and ".git" not in f.parts
                )
            )
        elif p.is_file():
            out.append(p)
    return [f for f in out if f.stat().st_size <= MAX_FILE_BYTES]


def collect_statements(paths: Iterable[str]) -> List[Statement]:
    statements: List[Statement] = []
    for f in iter_files(paths):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        statements.extend(find_statements(str(f), text))
    return statements


def _finding(statement: Statement, result: Any) -> Finding:
    rec = result if isinstance(result, dict) else {}
    return Finding(
        statement=statement,
        status=str(rec.get("status") or "unverified"),
        fixed_by=rec.get("fixedBy") or None,
        fixed_at=rec.get("fixedAt") or None,
        matches=[m for m in rec.get("matches") or [] if isinstance(m, dict)],
        evidence=[str(e) for e in rec.get("evidence") or []],
        is_limitation=rec.get("textIsLimitation") if isinstance(rec.get("textIsLimitation"), bool) else None,
    )


def check_statements(client: Any, statements: Sequence[Statement], *, timeout: float | None = None) -> List[Finding]:
    """Ask Kynver about every statement (batched). Raises when Kynver can't be reached."""
    findings: List[Finding] = []
    for start in range(0, len(statements), BATCH_SIZE):
        batch = statements[start:start + BATCH_SIZE]
        payload = client.post(
            LIMITATION_CHECK_PATH,
            {"items": [{"text": s.text} for s in batch]},
            timeout=timeout,
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(batch):
            raise RuntimeError("Kynver limitation check returned an unexpected payload")
        findings.extend(_finding(s, r) for s, r in zip(batch, results))
    return findings


def _day(value: Optional[str]) -> str:
    return (value or "")[:10]


def describe(finding: Finding) -> str:
    s = finding.statement
    where = f"{s.path}:{s.line_no}"
    if finding.stale:
        when = f" on {_day(finding.fixed_at)}" if finding.fixed_at else ""
        return f"STALE  {where}  fixed by {finding.fixed_by or 'a linked fix'}{when}\n       \"{s.text}\""
    if finding.status == "fixed":
        return f"CURRENT {where}  already reflects the fix ({finding.fixed_by or 'linked fix'})\n       \"{s.text}\""
    if finding.status == "open":
        ref = next((m.get("fixRef") for m in finding.matches if m.get("fixRef")), None)
        tracked = f" (fix tracked: {ref})" if ref else ""
        return f"OPEN   {where}{tracked}\n       \"{s.text}\""
    return f"UNVERIFIED {where}  no live record; recheck before relying on it\n       \"{s.text}\""


def is_under(path: str, roots: Sequence[Path]) -> bool:
    resolved = Path(path).expanduser().resolve()
    return any(resolved == r or r in resolved.parents for r in roots)


def default_skill_roots() -> List[Path]:
    from hermes_constants import get_hermes_home  # lazy: keeps this module importable in tests

    return [(get_hermes_home() / "skills").resolve()]


def fix_note(finding: Finding, *, today: Optional[str] = None) -> str:
    stamp = today or _dt.date.today().isoformat()
    when = f" on {_day(finding.fixed_at)}" if finding.fixed_at else ""
    return (
        f"{FIX_NOTE_PREFIX} (checked {stamp}): the limitation in the line above was fixed by "
        f"{finding.fixed_by or 'a linked fix'}{when}. It no longer applies."
    )


def annotate_skill_file(path: str, findings: Sequence[Finding], *, today: Optional[str] = None) -> int:
    """Append a FIXED note under each stale line in one skill file. Idempotent; returns notes added."""
    stale = sorted({f.statement.line_no: f for f in findings if f.stale and f.statement.path == path}.items())
    if not stale:
        return 0
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    added = 0
    for line_no, finding in reversed(stale):
        idx = line_no - 1
        if idx >= len(lines):
            continue
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        if nxt.strip().startswith(FIX_NOTE_PREFIX):
            continue
        indent = re.match(r"\s*", lines[idx]).group(0)
        newline = "\n" if lines[idx].endswith("\n") else ""
        if not newline:
            lines[idx] += "\n"
        lines.insert(idx + 1, f"{indent}{fix_note(finding, today=today)}\n")
        added += 1
    if added:
        p.write_text("".join(lines), encoding="utf-8")
    return added
