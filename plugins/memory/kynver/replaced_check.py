"""Second look at REPLACED matches: does the brief line restate the old rule or the newest one?

The 18 Oct failure: check-brief refused three briefs that restated *current* rules ("Cold open"
in a chapter-rename line, "QA gate", "the whole script in one continuous generation"). Kynver
matched each line to a note that had been replaced, but the replacing note said the same thing
(or the old note had only moved, or only another point of it had changed). Restating the newest
rule is correct, so those lines must not block a launch.

For every replaced match Kynver returns, this module reads the old note and follows its
``replacedBy`` chain to the newest note, then keeps the flag only when the line carries wording
from the part of the old note that was replaced and the newest note dropped. Everything else is
cleared, with the reason kept for the report. When a note can't be read the flag stays
(conservative: Kynver's verdict stands).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from .contract import memory_search_path
from .manual_steps import GuardFinding, subject_stems

MAX_CHAIN = 5
MAX_FETCHES = 16
FETCH_TIMEOUT = 8.0
# Old wording the newest note dropped: fewer shared stems than this is chance overlap.
MIN_OLD_ONLY_HITS = 2

_BANNER = re.compile(r"^\s*\[(?:REPLACED|UNVERIFIED|SUPERSEDED)[^\]]*\]\s*", re.IGNORECASE)
# Numbered points / lines first; a one-paragraph note falls back to sentences.
_POINT_SPLIT = re.compile(r"\n+|\s(?=\(\d{1,2}\)\s)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(\d])")
# A replacement that relocated or regrouped the rule rather than reversing it.
_MOVED = re.compile(
    r"\b(?:moved|moves|split|merged|consolidated|relocated|renamed|lives? in|now in|copied|restated|reworded|"
    r"clarif(?:ied|ies)|same rule|unchanged|deduplicated|duplicate)\b",
    re.IGNORECASE,
)
_CHAT_SUMMARY_SLUG = re.compile(r"^(?:gateway-conv:|session-|conv-)", re.IGNORECASE)
_CHAT_SUMMARY_TEXT = re.compile(r"^\s*(?:Conversation \"|Session summary)", re.IGNORECASE)


@dataclass
class Note:
    slug: str
    content: str
    replaced_by: str = ""
    reason: str = ""


class _Reader:
    """Exact-key memory reads, cached and capped so the launcher guard stays fast."""

    def __init__(self, client: Any, *, timeout: float = FETCH_TIMEOUT):
        self.client = client
        self.timeout = timeout
        self.cache: Dict[str, Optional[Note]] = {}
        self.fetches = 0

    def get(self, slug: str) -> Optional[Note]:
        slug = str(slug or "").strip()
        if not slug:
            return None
        if slug in self.cache:
            return self.cache[slug]
        note = None
        if self.fetches < MAX_FETCHES:
            self.fetches += 1
            try:
                payload = self.client.get(
                    memory_search_path(q=slug, key=slug, k=1, purpose="explicit_recall", view="full", surface="operator"),
                    timeout=self.timeout,
                )
                note = _note_from(payload, slug)
            except Exception:
                note = None
        self.cache[slug] = note
        return note


def _note_from(payload: Any, slug: str) -> Optional[Note]:
    items = payload.get("items") if isinstance(payload, dict) else payload
    for item in items or []:
        if not isinstance(item, dict) or str(item.get("slug") or item.get("id") or "") != slug:
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        contra = meta.get("contradiction") if isinstance(meta.get("contradiction"), dict) else {}
        replaced_by = ""
        if contra.get("status") == "replaced":
            replaced_by = str(contra.get("replacedBy") or "")
        replaced_by = replaced_by or str(meta.get("supersededBy") or "")
        reason = str(contra.get("replacedReason") or meta.get("supersededReason") or "")
        content = str(item.get("content") or item.get("text") or "")
        return Note(slug=slug, content=_BANNER.sub("", content), replaced_by=replaced_by, reason=reason)
    return None


def _points(text: str) -> List[str]:
    out = [p.strip(" -*•\t") for p in _POINT_SPLIT.split(text or "")]
    out = [p for p in out if len(subject_stems(p)) >= 2]
    if len(out) <= 1:
        out = [p.strip() for p in _SENTENCE_SPLIT.split(text or "")]
        out = [p for p in out if len(subject_stems(p)) >= 2]
    return out


def _overlap(a: Set[str], b: Set[str]) -> float:
    """How much of b the other side covers (b = a note point, so long briefs don't dilute it)."""
    return len(a & b) / max(1, len(b))


def judge(line: str, old: Optional[Note], chain: Sequence[Note], record: Dict[str, Any]) -> Dict[str, Any]:
    """{"keep": bool, "why": str, "newest": slug} for one replaced match of one brief line."""
    old_slug = str(record.get("slug") or "")
    preview = str(record.get("preview") or "")
    if _CHAT_SUMMARY_SLUG.match(old_slug) or _CHAT_SUMMARY_TEXT.match(old.content if old else preview):
        return {"keep": False, "why": f"matched a chat summary ({old_slug}), not a rule", "newest": ""}
    newest = chain[-1] if chain else None
    if newest is None:
        return {"keep": True, "why": f"could not read the newer note {record.get('replacedBy') or '?'}; Kynver's match stands", "newest": ""}
    low = line.lower()
    for note in chain:
        if note.slug and note.slug.lower() in low:
            return {"keep": False, "why": f"the line cites the newer note {note.slug}", "newest": newest.slug}
    reason = old.reason if old else ""
    if reason and _MOVED.search(reason) and not re.search(r"\breplac|\bforbid|\bno longer|\binstead\b", reason, re.IGNORECASE):
        return {"keep": False, "why": f"{old_slug} was moved/regrouped, not reversed ({reason[:120]})", "newest": newest.slug}

    line_stems = set(subject_stems(line))
    new_stems = set(subject_stems(newest.content))
    points = _points(old.content if old else preview) or [old.content if old else preview]
    point_stems = [set(subject_stems(p)) for p in points]
    # Which points did the replacement target? Those closest to the stated reason (else the newest note).
    target = set(subject_stems(reason)) or new_stems
    scores = [_overlap(target, ps) for ps in point_stems]
    top = max(scores) if scores else 0.0
    replaced_idx = [i for i, s in enumerate(scores) if top > 0 and s >= 0.5 * top] or list(range(len(points)))

    line_fit = [_overlap(line_stems, ps) for ps in point_stems]
    best = max(range(len(points)), key=lambda i: line_fit[i]) if points else 0
    best_replaced = max((line_fit[i] for i in replaced_idx), default=0.0)
    if points and best not in replaced_idx and line_fit[best] > best_replaced:
        return {
            "keep": False,
            "why": f"the line restates a part of {old_slug} that is still current (\"{points[best][:80]}\"); "
                   f"only \"{points[replaced_idx[0]][:80]}\" was replaced",
            "newest": newest.slug,
        }

    old_only = set().union(*(point_stems[i] for i in replaced_idx)) - new_stems
    old_hits = sorted(line_stems & old_only)
    new_hits = line_stems & new_stems
    if len(old_hits) < MIN_OLD_ONLY_HITS or len(new_hits) > len(old_hits):
        return {
            "keep": False,
            "why": f"the line agrees with the newest note {newest.slug} "
                   f"(shares {len(new_hits)} term(s) with it, {len(old_hits)} only with the old wording)",
            "newest": newest.slug,
        }
    return {
        "keep": True,
        "why": f"uses old wording the newest note dropped ({', '.join(old_hits[:6])}); "
               f"newest {newest.slug} says: \"{newest.content[:160].strip()}\"",
        "newest": newest.slug,
    }


def _chain(reader: _Reader, first: str) -> List[Note]:
    chain: List[Note] = []
    seen: Set[str] = set()
    slug = first
    while slug and slug not in seen and len(chain) < MAX_CHAIN:
        seen.add(slug)
        note = reader.get(slug)
        if note is None:
            break
        chain.append(note)
        slug = note.replaced_by
    return chain


def refine_replaced(findings: Sequence[GuardFinding], client: Any) -> List[GuardFinding]:
    """Clear REPLACED matches whose line agrees with the newest note. Mutates and returns findings."""
    reader = _Reader(client)
    for finding in findings:
        if not finding.replaced:
            continue
        kept, cleared = [], []
        for record in finding.replaced:
            old_slug = str(record.get("slug") or "")
            old = None if _CHAT_SUMMARY_SLUG.match(old_slug) else reader.get(old_slug)
            chain = _chain(reader, str(record.get("replacedBy") or (old.replaced_by if old else "")))
            verdict = judge(finding.statement.text, old, chain, record)
            entry = {**record, "why": verdict["why"], "newest": verdict["newest"]}
            (kept if verdict["keep"] else cleared).append(entry)
        finding.replaced = kept
        finding.cleared = cleared
    return list(findings)
