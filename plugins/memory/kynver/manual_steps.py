"""Lines in briefs, reports and skills that tell Will to do something by hand, or repeat a
retired note.

The 17 Oct failure: compaction had been automated since 30 Sep, but a June note, a skill file
("hand Will compact_wsl_vhdx.ps1") and the 14 Oct cleanup REPORT.md ("Will has to run it
himself") kept saying otherwise, and Forge repeated them. Kynver now retires a note when a newer
one replaces it; this module finds the copies outside Kynver memory and asks Kynver's
``/memory/manual-step/check`` (batch) whether each line is automated already or repeats a
replaced note. Local matching is only a prefilter; Kynver decides.

It also lints a skill directory for files that contradict each other (one file says a step is
automated, another tells Will to do it by hand). That part is deterministic and report-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .blockers import FIX_NOTE_PREFIX, Statement, _clean, iter_files
from .contract import MANUAL_STEP_CHECK_PATH

BATCH_SIZE = 25
MAX_LINES_PER_FILE = 60

# Mirrors Kynver's memory-contradictions MANUAL_STEP_PATTERN: only steps aimed at Will (or
# "the user") count; "run the script manually" in a runbook is the agent's own step.
_MANUAL = re.compile(
    "|".join(
        [
            r"\b(?:will|the user)\s+(?:must|has to|have to|needs? to|should|will have to|is to)\s+(?:run|do|execute|click|open|launch|start|install|type|paste|approve|compact|delete|restart|reboot|log ?in|sign ?in)\b",
            r"\b(?:ask|tell|get|have|remind)\s+(?:will|the user)\s+to\b",
            r"\bhand\s+(?:will|the user)\b",
            r"\bsuggest (?:it|this|that) to (?:will|the user)\b",
            r"\bwill (?:runs?|does) (?:it|this|that) (?:himself|themselves|by hand|manually)\b",
            r"\badmin(?:istrator)? powershell\b",
        ]
    ),
    re.IGNORECASE,
)
# "never hand Will…", "do not ask Will to…": a negated manual step is the cure, not the disease.
_MANUAL_NEGATION = re.compile(
    r"\b(?:never|don't|do not|doesn't|does not|not|no longer|without|no need to|instead of)\b[^.;:!?]{0,40}$",
    re.IGNORECASE,
)
_AUTOMATED = re.compile(
    r"\b(?:automat(?:ed|ic|ically|ion)|auto-?(?:compact|run|retire|clean|merge|deploy|land)\w*|runs? (?:on its own|by itself|unattended|nightly|daily|hourly|weekly|every)|scheduled task|(?:by|via|from|on) (?:a |the |its own )?(?:cron|scheduler)|cron(?:job)? (?:runs|handles|does)|forge (?:does|runs|handles|will run|now runs)|no longer (?:needs?|requires?|asks?)|(?:never|don't|do not) (?:tell|ask) will to|without will|no manual)\b",
    re.IGNORECASE,
)
_NOT_AUTOMATED = re.compile(
    r"\b(?:not|isn't|is not|aren't|never been|no longer)\s+(?:yet\s+)?(?:automat\w*|auto-?\w+|scheduled)|\bno automation\b|\bnot (?:a )?cron\b",
    re.IGNORECASE,
)
# Standing advice a newer note may have replaced ("prefer…", "avoid…", "never…").
_ADVICE = re.compile(r"\b(?:prefers?|avoid|always|never|must|should|don't|do not|only)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")

_STOP = set(
    (
        "a an the and or but if then so to of on in at for by with from into onto as is are was were be been being "
        "it its this that these those there here we our us you your they their them i me my he she his her him himself "
        "can cant cannot not no yet still currently actually just only also very really because until due since "
        "do does doesn't don't did won't will would should could may might must has have had hold holding held "
        "keep send sends sending support supports supported via using use rows row any all some more most "
        "run runs running script scripts manual manually hand ask tell automatic automated automatically automation auto "
        "forge note memory now today need needs make sure time every day get set new old after before when what which "
        "who how about than over like one two always never user operator worker agent "
        "references reference md skill see"
    ).split()
)


def is_automation_text(text: str) -> bool:
    return bool(_AUTOMATED.search(text)) and not _NOT_AUTOMATED.search(text)


def is_manual_step_text(text: str) -> bool:
    """A by-hand instruction to Will; a negated one or a line saying the step is automated is the cure."""
    for match in _MANUAL.finditer(text):
        if not _MANUAL_NEGATION.search(text[max(0, match.start() - 60):match.start()]):
            return not is_automation_text(text)
    return False


def _stem(token: str) -> str:
    if len(token) <= 4:
        return token
    out = re.sub(r"(?:ions?|ing|ed|ically|ic|es|s)$", "", token)
    return re.sub(r"(.)\1$", r"\1", out)


def subject_stems(text: str) -> List[str]:
    out: List[str] = []
    for raw in re.split(r"[^A-Za-z0-9]+", text.lower()):
        if len(raw) < 2 or raw.isdigit() or raw in _STOP:
            continue
        stem = _stem(raw)
        if stem not in _STOP and stem not in out:
            out.append(stem)
    return out


def find_guard_statements(path: str, text: str) -> List[Statement]:
    """Manual-step and standing-advice sentences in one file (code fences, quotes and our notes skipped)."""
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
            bare = _QUOTED.sub(" ", sentence)
            if len(sentence) < 16 or not (is_manual_step_text(bare) or _ADVICE.search(bare)):
                continue
            key = sentence.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(Statement(path=path, line_no=line_no, text=sentence[:2000]))
            if len(found) >= MAX_LINES_PER_FILE:
                return found
    return found


@dataclass
class GuardFinding:
    statement: Statement
    manual_step: bool
    automated: bool
    automation: List[dict] = field(default_factory=list)
    replaced: List[dict] = field(default_factory=list)
    retired_manual_steps: List[dict] = field(default_factory=list)
    advice: str = ""

    @property
    def stale(self) -> bool:
        """The line asks Will to do something memory says is automated, or repeats a replaced note."""
        return (self.manual_step and (self.automated or bool(self.retired_manual_steps))) or bool(self.replaced)


class RouteMissing(RuntimeError):
    """Kynver is reachable but predates the manual-step route."""


def check_guard_statements(client: Any, statements: Sequence[Statement], *, timeout: float | None = None) -> List[GuardFinding]:
    """Ask Kynver about every statement (batched). Raises RouteMissing on an old server, else on failure."""
    findings: List[GuardFinding] = []
    for start in range(0, len(statements), BATCH_SIZE):
        batch = statements[start:start + BATCH_SIZE]
        try:
            payload = client.post(MANUAL_STEP_CHECK_PATH, {"items": [{"text": s.text} for s in batch]}, timeout=timeout)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise RouteMissing(str(exc)) from exc
            raise
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(batch):
            raise RuntimeError("Kynver manual-step check returned an unexpected payload")
        for statement, rec in zip(batch, results):
            rec = rec if isinstance(rec, dict) else {}
            findings.append(
                GuardFinding(
                    statement=statement,
                    manual_step=bool(rec.get("isManualStep")) or is_manual_step_text(statement.text),
                    automated=bool(rec.get("automated")),
                    automation=[m for m in rec.get("automation") or [] if isinstance(m, dict)],
                    replaced=[m for m in rec.get("replaced") or [] if isinstance(m, dict)],
                    retired_manual_steps=[m for m in rec.get("retiredManualSteps") or [] if isinstance(m, dict)],
                    advice=str(rec.get("advice") or ""),
                )
            )
    return findings


def describe_guard(finding: GuardFinding) -> str:
    s = finding.statement
    where = f"{s.path}:{s.line_no}"
    if finding.replaced:
        top = finding.replaced[0]
        return (
            f"REPLACED {where}  repeats memory {top.get('slug')}, replaced by {top.get('replacedBy') or 'a newer note'}\n"
            f"       \"{s.text}\""
        )
    top = (finding.automation or finding.retired_manual_steps or [{}])[0]
    when = str(top.get("updatedAt") or "")[:10]
    return (
        f"AUTOMATED {where}  asks Will to do this by hand, but memory {top.get('slug') or '?'}"
        f"{f' ({when})' if when else ''} says it is automated / done by Forge\n       \"{s.text}\""
    )


# ── lint-skill: a skill whose own files contradict each other ─────────────────────────────

@dataclass
class SkillConflict:
    manual: Statement
    automated: Statement
    shared: List[str]


def _automation_statements(path: str, text: str) -> List[Statement]:
    out: List[Statement] = []
    in_fence = False
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        for sentence in _SENTENCE_SPLIT.split(_clean(raw)):
            if len(sentence) >= 16 and is_automation_text(_QUOTED.sub(" ", sentence)):
                out.append(Statement(path=path, line_no=line_no, text=sentence.strip()[:2000]))
    return out


def skill_internal_conflicts(files: Iterable[Path]) -> List[SkillConflict]:
    """Pairs where one line tells Will to do X by hand and another (any file) says X is automated."""
    manual: List[Statement] = []
    automated: List[Statement] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        manual.extend(s for s in find_guard_statements(str(f), text) if is_manual_step_text(s.text))
        automated.extend(_automation_statements(str(f), text))
    conflicts: List[SkillConflict] = []
    for m in manual:
        m_stems = set(subject_stems(m.text))
        best: Optional[SkillConflict] = None
        for a in automated:
            if a.path == m.path and a.line_no == m.line_no:
                continue
            shared = [t for t in subject_stems(a.text) if t in m_stems]
            if len(shared) >= 2 and (best is None or len(shared) > len(best.shared)):
                best = SkillConflict(manual=m, automated=a, shared=shared)
        if best:
            conflicts.append(best)
    return conflicts


def lint_skill(directory: str, client: Any = None, *, timeout: float | None = None) -> Dict[str, Any]:
    """Report-only lint of one skill directory. ``client=None`` skips the memory comparison."""
    files = iter_files([directory])
    report: Dict[str, Any] = {
        "skill": directory,
        "files": [str(f) for f in files],
        "internal": skill_internal_conflicts(files),
        "memory": [],
        "memoryChecked": False,
        "memoryError": None,
    }
    if client is None:
        return report
    statements: List[Statement] = []
    for f in files:
        try:
            statements.extend(find_guard_statements(str(f), f.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    try:
        report["memory"] = [g for g in check_guard_statements(client, statements, timeout=timeout) if g.stale] if statements else []
        report["memoryChecked"] = True
    except Exception as exc:  # Kynver down / old server: the internal lint still stands
        report["memoryError"] = str(exc)
    return report
