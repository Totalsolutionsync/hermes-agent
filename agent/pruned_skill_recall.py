"""Topic-triggered recall of skills pruned by context compression.

Compression demotes a loaded skill's body to a ``[SKILL_PRUNED: ...]`` marker. The marker and
the summary's generic "reload before acting" note cross the boundary, but nothing ties them to
the moment the skill matters again: a later user turn on the skill's topic gets answered from
priors (the agent told Will printing was impossible while the pruned legal-case skill said he
HAS a Wi-Fi printer and how to print to it).

This module closes that gap deterministically, with no LLM call and no standing context: on
each user turn it collects the skills still pruned in this conversation, matches the human's
own words against a small per-skill keyword index (name, description, headings, procedure
steps), and for at most ``MAX_RECALLED_SKILLS`` matches returns a short note — plus the
matching step's key sentences — that rides the turn's ephemeral user-message context (never
the system prompt, never a new message). A turn that matches nothing costs nothing.

Matching is IDF-weighted across every installed skill so words most skills share ("need",
"send", "file") never trigger; only terms rare in the skill corpus count. Index entries are
cached per ``(path, mtime_ns, size)``.
"""

from __future__ import annotations

import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

MAX_RECALLED_SKILLS = 2
EXCERPT_MAX_CHARS = 400
# A step is "about" a message term when the term recurs in it (tf >= _DENSE_TF) and makes up
# at least _STEP_DENSITY of the step's content words — step 15b of the legal skill is 12% "print".
_DENSE_TF = 4
_STEP_DENSITY = 0.08
# A long step that repeats a term this often (at a lower density) is also about it.
_HEAVY_TF = 6
_HEAVY_DENSITY = 0.04
# Density means nothing in a heading or one-line bullet ("## Kynver prod" is 50% "kynver").
_DENSE_MIN_WORDS = 30
# ...and the term is not everyday vocabulary across installed skills ("work", "test", "build").
_DENSE_MAX_DF_RATIO = 0.3
# Two distinct message terms in ONE sentence of a step fire when each is used by at most this
# share of installed skills.
_PAIR_MAX_DF_RATIO = 0.04
# The frontmatter description is the author's own trigger text; an uncommon word from it fires.
_HEADER_MAX_DF_RATIO = 0.15
# A term used by at most this many skills is jargon ("mediator", "counterclaim") and fires alone
# when the skill uses it at least twice.
_JARGON_MAX_DF = 2
_JARGON_MIN_CHARS = 5
# A term in at least this share of the conversation's earlier human turns (and >= _BACKGROUND_MIN
# of them) is the conversation's background ("Kynver" in Will's chats), not a topic shift, so it
# can't trigger on its own.
_BACKGROUND_SHARE = 0.08
_BACKGROUND_MIN = 4
_BACKGROUND_WINDOW = 400  # messages
_MIN_TERM_CHARS = 3
_HUMAN_TEXT_MAX_CHARS = 4000

_WORD_RE = re.compile(r"[a-z][a-z0-9]+")
# URLs, paths and long hex ids in a message are addresses, not topic.
_ADDRESS_RE = re.compile(r"\S*(?:://|/|\\)\S*|\b[0-9a-f]{7,}\b", re.IGNORECASE)
_REPLY_QUOTE_RE = re.compile(r'^\[Replying to[^\n]*?: ".*?"\]\n\n', re.DOTALL)
_MEMORY_CONTEXT_RE = re.compile(r"<memory-context>.*?(?:</memory-context>|$)", re.DOTALL | re.IGNORECASE)
_STEP_START_RE = re.compile(r"^(?:#{1,6}\s|\d+[a-z]?[.)]\s|[-*+]\s)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;:])\s+(?=[A-Z`*(\"'~])")
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
# Code blocks are recipes, not topic: `print(` in a dozen skills must not make "print" common.
_FENCED_CODE_RE = re.compile(r"^\s*```.*?^\s*```[^\n]*$", re.DOTALL | re.MULTILINE)

# Function words and conversational filler; IDF handles generic domain words.
_STOPWORDS = frozenset(
    """
    about above after again against all also and any are aren because been before being below
    between both but can cannot could did didn does doesn doing don down during each else ever
    every few for from further had hadn has hasn have haven having her here hers herself him
    himself his how i'd i'll i'm into isn it's its itself just let me more most much must mustn
    myself need needs nor not now off once only other our ours ourselves out over own same shan
    she should shouldn some such than that that's the their theirs them themselves then there
    these they this those through too under until upon very was wasn were weren what when where
    which while who whom why will with won would wouldn you your yours yourself yourselves
    please thanks thank okay yeah yes hey hello really still just want wants like know think
    going get got make made let lets one two way thing things something anything everything
    today tomorrow yesterday tonight now right sure good great fine also again maybe
    give take say ask call real line use used using help helping making doing
    """.split()
)

_SUFFIXES = ("ings", "ing", "ers", "er", "ed", "es", "s")


def _stem(word: str) -> str:
    """Crude suffix strip so print/prints/printing/printer share a key; no dictionary."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    return word


def _term_counts(text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for word in _WORD_RE.findall((text or "").lower()):
        if len(word) < _MIN_TERM_CHARS or word in _STOPWORDS:
            continue
        stem = _stem(word)
        out[stem] = out.get(stem, 0) + 1
    return out


def _terms(text: str) -> set[str]:
    return set(_term_counts(text))


@dataclass(frozen=True)
class _Step:
    text: str
    counts: Tuple[Tuple[str, int], ...]
    sentences: Tuple[frozenset, ...]

    @property
    def terms(self) -> frozenset:
        return frozenset(term for term, _ in self.counts)


@dataclass(frozen=True)
class _SkillIndex:
    name: str
    steps: Tuple[_Step, ...]
    terms: frozenset


def _split_steps(body: str) -> List[str]:
    """Headings, numbered steps and bullets each start a step; continuation lines join it."""
    steps: List[str] = []
    for raw in _FENCED_CODE_RE.sub("\n", body).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _STEP_START_RE.match(line.lstrip()) and not raw[:1].isspace():
            steps.append(line.strip())
        elif steps:
            steps[-1] += " " + line.strip()
        else:
            steps.append(line.strip())
    return steps


def _build_index(path: Path) -> Optional[_SkillIndex]:
    from agent.skill_utils import parse_frontmatter

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        frontmatter, body = parse_frontmatter(content)
    except Exception:
        frontmatter, body = {}, _FRONTMATTER_RE.sub("", content, count=1)
    name = str((frontmatter or {}).get("name") or path.parent.name)
    header = f"{name.replace('-', ' ')} {(frontmatter or {}).get('description') or ''}".strip()
    steps = [
        _Step(text, tuple(_term_counts(text).items()), tuple(frozenset(_terms(x)) for x in _sentences(text)))
        for text in [header, *_split_steps(body)]
    ]
    union: set = set()
    for step in steps:
        union |= step.terms
    return _SkillIndex(name=name, steps=tuple(steps), terms=frozenset(union))


_cache_lock = threading.Lock()
_index_cache: Dict[str, Tuple[Tuple[int, int], Optional[_SkillIndex]]] = {}
_corpus_cache: Dict[Tuple, Tuple[Dict[str, _SkillIndex], Dict[str, int], int]] = {}


def _skill_files() -> List[Path]:
    from agent.skill_utils import get_all_skills_dirs, iter_skill_index_files

    files: List[Path] = []
    seen: set = set()
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.is_dir():
            continue
        for path in iter_skill_index_files(skills_dir, "SKILL.md"):
            key = str(path)
            if key not in seen:
                seen.add(key)
                files.append(path)
    return files


def _cached_index(path: Path, stamp: Tuple[int, int]) -> Optional[_SkillIndex]:
    key = str(path)
    with _cache_lock:
        hit = _index_cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    index = _build_index(path)
    with _cache_lock:
        _index_cache[key] = (stamp, index)
    return index


def _corpus() -> Tuple[Dict[str, _SkillIndex], Dict[str, int], int]:
    """(name -> index, term -> document frequency, skill count); first name wins like skill_view."""
    stamped: List[Tuple[Path, Tuple[int, int]]] = []
    for path in _skill_files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        stamped.append((path, (st.st_mtime_ns, st.st_size)))
    signature = tuple((str(p), s) for p, s in stamped)
    with _cache_lock:
        hit = _corpus_cache.get(signature)
    if hit is not None:
        return hit
    by_name: Dict[str, _SkillIndex] = {}
    df: Dict[str, int] = {}
    for path, stamp in stamped:
        index = _cached_index(path, stamp)
        if index is None or index.name in by_name:
            continue
        by_name[index.name] = index
        for term in index.terms:
            df[term] = df.get(term, 0) + 1
    result = (by_name, df, len(by_name))
    with _cache_lock:
        _corpus_cache.clear()  # one live signature per process is enough
        _corpus_cache[signature] = result
    return result


# --- which skills are still pruned ------------------------------------------------------------


def _tool_call_skill_name(tool_call: Any) -> Optional[str]:
    import json

    fn = tool_call.get("function") if isinstance(tool_call, dict) else getattr(tool_call, "function", None)
    fn_name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
    if fn_name != "skill_view":
        return None
    raw = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return None
    if not isinstance(args, dict) or args.get("file_path"):
        return None  # a linked-file read does not restore the SKILL.md body
    name = args.get("name")
    return str(name) if name else None


def still_pruned_skill_names(messages: Sequence[Any]) -> List[str]:
    """Skills whose newest event in *messages* is a prune marker (not a later ``skill_view``
    reload), newest marker first."""
    from agent.context_compressor import (
        SKILL_PRUNED_MARKER_PREFIX,
        _content_text_for_contains,
        _extract_pruned_skill_names,
    )

    state: Dict[str, int] = {}
    for position, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        text = _content_text_for_contains(message.get("content"))
        if text and SKILL_PRUNED_MARKER_PREFIX in text:
            for name in _extract_pruned_skill_names(text):
                state[name] = position
        for tool_call in message.get("tool_calls") or ():
            name = _tool_call_skill_name(tool_call)
            if name:
                for key in [k for k in state if k == name or name.endswith("/" + k) or name.split(":")[-1] == k]:
                    state.pop(key)
    return [name for name, _ in sorted(state.items(), key=lambda item: -item[1])]


# --- matching -----------------------------------------------------------------------------------


def human_words(message: Any) -> str:
    """The text the human typed this turn: drops a quoted bot reply and injected memory context
    so the agent's own prior words can't trigger (or dominate) a match."""
    from agent.message_content import flatten_message_text

    text = flatten_message_text(message) if isinstance(message, (str, list)) else ""
    try:
        from agent.skill_commands import extract_user_instruction_from_skill_message

        text = extract_user_instruction_from_skill_message(text) or ""
    except Exception:
        pass
    text = _MEMORY_CONTEXT_RE.sub("", text)
    text = _REPLY_QUOTE_RE.sub("", text, count=1)
    from tools.todo_tool import TODO_INJECTION_HEADER

    text = text.split(TODO_INJECTION_HEADER, 1)[0]  # compression's todo snapshot rides the user row
    return text.strip()[:_HUMAN_TEXT_MAX_CHARS]


def background_terms(history: Sequence[Any]) -> frozenset:
    """Terms the human uses in many earlier turns of this conversation."""
    seen: Dict[str, int] = {}
    turns = 0
    for message in history[-_BACKGROUND_WINDOW:]:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = human_words(message.get("content"))
        if not text or text.startswith("[CONTEXT COMPACTION"):
            continue
        turns += 1
        for term in _terms(_ADDRESS_RE.sub(" ", text)):
            seen[term] = seen.get(term, 0) + 1
    floor = max(_BACKGROUND_MIN, turns * _BACKGROUND_SHARE)
    return frozenset(term for term, n in seen.items() if n >= floor)


@dataclass(frozen=True)
class SkillRecall:
    name: str
    matched: Tuple[str, ...]
    excerpt: str
    score: float


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(re.sub(r"\s+", " ", text)) if s.strip()]


def _excerpt(step_text: str, matched_stems: Iterable[str]) -> str:
    """The step's sentences that carry matched terms, densest first, kept in source order and
    capped at ``EXCERPT_MAX_CHARS``."""
    stems = set(matched_stems)
    sentences = _sentences(step_text)
    scored = []
    for idx, sentence in enumerate(sentences):
        hits = len(_terms(sentence) & stems)
        if hits:
            scored.append((hits / max(len(sentence), 1), idx, sentence))
    chosen: List[Tuple[int, str]] = []
    used = 0
    for _density, idx, sentence in sorted(scored, key=lambda row: (-row[0], row[1])):
        cost = len(sentence) + (1 if chosen else 0)
        if used + cost > EXCERPT_MAX_CHARS:
            continue
        chosen.append((idx, sentence))
        used += cost
    if not chosen:
        head = sentences[0] if sentences else step_text
        return head[: EXCERPT_MAX_CHARS - 1].rstrip() + "…" if len(head) > EXCERPT_MAX_CHARS else head
    return " ".join(sentence for _idx, sentence in sorted(chosen))


def _saturate(tf: int, k: float = 1.2) -> float:
    """BM25-style term-frequency saturation: repeats help, but sub-linearly."""
    return tf * (k + 1) / (tf + k)


def _step_hits(
    step: _Step, query: set, df: Dict[str, int], total: int, skill_tf: Dict[str, int], background: frozenset,
    *, is_header: bool = False,
) -> set:
    counts = dict(step.counts)
    present = {t for t in query if t in counts and df.get(t, 0) > 0}
    if not present:
        return set()
    length = max(sum(counts.values()), 1)
    topical = present - background
    hits = {
        t for t in topical
        if df[t] <= total * _DENSE_MAX_DF_RATIO
        and length >= _DENSE_MIN_WORDS
        and (
            (counts[t] >= _DENSE_TF and counts[t] / length >= _STEP_DENSITY)
            or (counts[t] >= _HEAVY_TF and counts[t] / length >= _HEAVY_DENSITY)
        )
    }
    if is_header:
        hits |= {t for t in topical if df[t] <= total * _HEADER_MAX_DF_RATIO}
    hits |= {
        t for t in topical
        if df[t] <= _JARGON_MAX_DF and len(t) >= _JARGON_MIN_CHARS and skill_tf.get(t, 0) >= 2
    }
    rare = {t for t in present if df[t] <= total * _PAIR_MAX_DF_RATIO}
    if len(rare) >= 2:
        for sentence in step.sentences:
            together = rare & sentence
            if len(together) >= 2:
                hits |= together
    return hits


def match_pruned_skills(
    user_text: str, pruned_names: Sequence[str], *, limit: int = MAX_RECALLED_SKILLS,
    background: frozenset = frozenset(),
) -> List[SkillRecall]:
    """Pruned skills whose content the message touches, best first, at most *limit*. A skill
    matches through ONE step that is about the message (see ``_step_hits``); a single shared
    common word never fires."""
    if not pruned_names or not user_text:
        return []
    query = _terms(_ADDRESS_RE.sub(" ", user_text))
    if not query:
        return []
    by_name, df, total = _corpus()
    results: List[SkillRecall] = []
    for name in pruned_names:
        index = by_name.get(name) or by_name.get(name.split("/")[-1].split(":")[-1])
        if index is None or not (index.terms & query):
            continue
        skill_tf: Dict[str, int] = {}
        for step in index.steps:
            for term, n in step.counts:
                if term in query:
                    skill_tf[term] = skill_tf.get(term, 0) + n
        best: Optional[Tuple[float, _Step, frozenset]] = None
        for position, step in enumerate(index.steps):
            hits = _step_hits(step, query, df, total, skill_tf, background, is_header=position == 0)
            if not hits:
                continue
            counts = dict(step.counts)
            score = sum(math.log((total + 1) / (df[t] + 1)) * _saturate(counts[t]) for t in hits)
            if best is None or score > best[0]:
                best = (score, step, frozenset(hits))
        if best is None:
            continue
        score, step, hits = best
        matched = tuple(sorted(hits, key=lambda t: (df[t], t)))
        results.append(SkillRecall(name=name, matched=matched, excerpt=_excerpt(step.text, hits), score=score))
    results.sort(key=lambda r: -r.score)
    return results[:limit]


def _matched_words(user_text: str, stems: Iterable[str]) -> List[str]:
    """Render matched stems as the words the user actually typed."""
    wanted = set(stems)
    words: List[str] = []
    for word in _WORD_RE.findall(user_text.lower()):
        stem = _stem(word)
        if stem in wanted and word not in words:
            words.append(word)
            wanted.discard(stem)
    return words


def _note_key(name: str) -> str:
    return f"Pruned skill '{name}' covers this"


def format_recall_note(user_text: str, recalls: Sequence[SkillRecall]) -> str:
    lines = []
    for recall in recalls:
        matched = ", ".join(_matched_words(user_text, recall.matched)) or ", ".join(recall.matched)
        lines.append(
            f"[{_note_key(recall.name)} (matched: {matched}). "
            f"Reload with skill_view(name='{recall.name}') before acting. Key step: {recall.excerpt}]"
        )
    return "\n".join(lines)


def _already_surfaced(history: Sequence[Any], recall: SkillRecall) -> bool:
    """Whether this exact step of this skill was already surfaced to the model in *history*
    (the note persists in the user row's ``api_content``): once is the reminder, every turn is noise."""
    needle = _note_key(recall.name)
    for message in history:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        for field in ("api_content", "content"):
            value = message.get(field)
            text = value if isinstance(value, str) else flatten_text(value)
            if needle in text and recall.excerpt in text:
                return True
    return False


def flatten_text(value: Any) -> str:
    from agent.message_content import flatten_message_text

    return flatten_message_text(value) if isinstance(value, list) else ""


def pruned_skill_recall_note(messages: Sequence[Any], current_turn_user_idx: int, user_message: Any) -> str:
    """The per-turn recall note for *user_message*, or ``""``. Only history BEFORE the current
    turn is scanned; nothing is read from disk unless a pruned skill exists and the turn has
    words of its own."""
    history = messages[:current_turn_user_idx] if 0 <= current_turn_user_idx <= len(messages) else messages
    pruned = still_pruned_skill_names(history)
    if not pruned:
        return ""
    text = human_words(user_message)
    if not text:
        return ""
    recalls = match_pruned_skills(text, pruned, limit=len(pruned))
    if recalls:  # background only matters once something matched; skip the scan otherwise
        recalls = match_pruned_skills(text, pruned, limit=len(pruned), background=background_terms(history))
    fresh = [r for r in recalls if not _already_surfaced(history, r)][:MAX_RECALLED_SKILLS]
    return format_recall_note(text, fresh)
