"""Words in a relayed user turn that the owner did not type.

Kynver's relay appends its own hints to an owner turn (the deliverable hint
"(Kynver files lists and plans …)") and the gateway adds attachment markers
("[Image attached at: …]", "[screenshot]"). Anything that decides relevance
from "what the owner asked this turn" — standing-rule topics, pruned-skill
recall — must read the turn without them: the deliverable hint alone used to
attach the whole kynver-dev rules pack to a grocery question.

The same goes for headers wrapped around the owner's words: the reply-quote
header (``[Replying to … "<earlier message>"]`` from the Kynver relay or the
gateway) and a report-answer push (``[Kynver report answers] The owner sent
1 item on your report in Kynver.`` + ids + an "Act on each one now" tail). A
6-word "please land this" under a quote that said "Kynver code question"
attached the full kynver-dev pack. ``split_reply_quote`` hands the quote back
separately so the matcher can judge it on substance.
"""

from __future__ import annotations

import re

# Pinned to Kynver's DELIVERABLE_RELAY_HINT (src/modules/workspace-reports/reports.deliverable-policy.ts).
KYNVER_RELAY_APPENDED_TEXT: tuple[str, ...] = (
    "(Kynver files lists and plans you write out as a Report under Reports — put the whole list "
    "in your reply; don't only save it to todos or a file.)",
)

# Pinned to Kynver's FORGE_ANSWER_TURN_TAIL (src/modules/workspace-reports/reports.forge-push.ts).
KYNVER_REPORT_ANSWER_TAIL = (
    "Act on each one now: an approval or chosen option is the go-ahead for exactly what that ask proposed "
    "(still follow the usual rules for anything public, paid or irreversible). If an answer needs a "
    "follow-up question, ask it. When done, say what you did per item; publish a new version with the same "
    "seriesKey if the report itself should change."
)

# Kynver's framing of a report-answer turn: header line, per-item ids, comment-kind tags.
_REPORT_FRAMING_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*\[Kynver report answers\][^\n]*$", re.MULTILINE),
    re.compile(r"\s*\((?:commentId|askId) [^)\n]*\)(?:\s*—\s*reportId \S+)?"),
)
_REPORT_ITEM_TAG_RE = re.compile(r"^(\s*-\s*)\[[a-z][a-z_-]*\]\s*", re.MULTILINE | re.IGNORECASE)

# ``[Replying to assistant message from …: "…"]`` (Kynver relay) or ``[Replying to your previous
# message: "…"]`` / ``[Replying to: "…"]`` (gateway). The quote may span lines and hold quotes.
_REPLY_QUOTE_RE = re.compile(r'^\s*\[Replying to(?: [^\n"]*?)?: "(.*?)"\](?=\s*(?:\n|$))\s*', re.DOTALL)

_ATTACHMENT_MARKER_RE = re.compile(
    r"^\s*\[(?:(?:Image|File|Document|Video|Audio|Attachment)\b[^\]\n]*|screenshot)\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_reply_quote(text: str) -> tuple[str, str]:
    """``(own, quote)``: the leading reply-quote header split off *text* (``quote`` is ``""`` if none)."""
    m = _REPLY_QUOTE_RE.match(text or "")
    if not m:
        return (text or ""), ""
    return text[m.end():], m.group(1).strip()


def owner_words(text: str) -> str:
    """*text* without a reply-quote header, Kynver's hints/report framing and attachment markers."""
    out, _quote = split_reply_quote(text or "")
    for appended in (*KYNVER_RELAY_APPENDED_TEXT, KYNVER_REPORT_ANSWER_TAIL):
        if appended in out:
            out = out.replace(appended, " ")
    for framing in _REPORT_FRAMING_RES:
        out = framing.sub(" ", out)
    out = _REPORT_ITEM_TAG_RE.sub(lambda m: m.group(1), out)
    out = _ATTACHMENT_MARKER_RE.sub("", out)
    return out.strip()
