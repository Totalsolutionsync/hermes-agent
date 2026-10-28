"""Kynver standing rules (operating rules) loaded once per session into the system prompt.

Memory search (``prefetch``) only surfaces a memory when the current message is about it, so
rules that must hold on every turn ("media goes on D:", "Will approves every email") lose to
topic chatter. Kynver keeps those as operating rules (``metadata.kind = operating-rule``) and
serves them by exact lookup from ``/memory/instruction-policy``. This module fetches them once,
fits them into a fixed token budget, and says so loudly when the budget is full: a rule that
silently falls off the end is the exact failure this exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .context import DEFAULT_STANDING_RULES_TOKEN_BUDGET, estimate_tokens

INSTRUCTION_POLICY_PATH = "/memory/instruction-policy"
FULL_WARN_RATIO = 0.9


@dataclass(frozen=True)
class StandingRulesFit:
    markdown: str
    included: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    used_tokens: int = 0
    budget: int = DEFAULT_STANDING_RULES_TOKEN_BUDGET
    server_total: int | None = None
    server_returned: int = 0

    @property
    def full(self) -> bool:
        return bool(self.omitted) or self.server_truncated

    @property
    def server_truncated(self) -> bool:
        return self.server_total is not None and self.server_total > self.server_returned

    @property
    def nearly_full(self) -> bool:
        return not self.full and self.used_tokens >= int(self.budget * FULL_WARN_RATIO)


def _rules_from_payload(payload: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    if not isinstance(payload, Mapping):
        return [], None
    policy = payload.get("policy")
    if not isinstance(policy, Mapping):
        return [], None
    seen: set[str] = set()
    rules: list[Mapping[str, Any]] = []
    # internal (system floor) first, then owner rules; global before persona — server order.
    for bucket in ("internalRules", "customRules", "personaRules"):
        for rule in policy.get(bucket) or []:
            if not isinstance(rule, Mapping):
                continue
            key = str(rule.get("slug") or rule.get("memoryId") or "")
            content = str(rule.get("content") or "").strip()
            if not content or key in seen:
                continue
            seen.add(key)
            rules.append(rule)
    raw_status = payload.get("status")
    status: Mapping[str, Any] = raw_status if isinstance(raw_status, Mapping) else {}
    total = policy.get("totalRuleCount", status.get("totalRuleCount"))
    try:
        total_int = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_int = None
    return rules, total_int


def fit_standing_rules(payload: Any, *, budget: int = DEFAULT_STANDING_RULES_TOKEN_BUDGET) -> StandingRulesFit:
    rules, server_total = _rules_from_payload(payload)
    if not rules:
        return StandingRulesFit(markdown="", budget=budget, server_total=server_total)
    header = (
        "# Standing rules (Kynver)\n"
        "Will's standing rules. They apply on EVERY turn and to every worker you brief, "
        "whatever the topic. They override older memories that disagree."
    )
    lines: list[str] = []
    included: list[str] = []
    omitted: list[str] = []
    used = estimate_tokens(header)
    for rule in rules:
        slug = str(rule.get("slug") or rule.get("memoryId") or "rule")
        line = f"- ({slug}) {' '.join(str(rule.get('content')).split())}"
        cost = estimate_tokens(line) + 1
        if omitted or used + cost > budget:
            omitted.append(slug)
            continue
        lines.append(line)
        included.append(slug)
        used += cost
    fit = StandingRulesFit(
        markdown="",
        included=included,
        omitted=omitted,
        used_tokens=used,
        budget=budget,
        server_total=server_total,
        server_returned=len(rules),
    )
    notice = standing_rules_notice(fit)
    body = "\n".join([header, *lines])
    if notice:
        body = f"{body}\n{notice}"
    return StandingRulesFit(**{**fit.__dict__, "markdown": body})


def standing_rules_notice(fit: StandingRulesFit) -> str:
    """The loud line the agent must relay to Will when rules no longer all fit."""
    if fit.full:
        missing = len(fit.omitted)
        if fit.server_truncated:
            missing += (fit.server_total or 0) - fit.server_returned
        names = ", ".join(fit.omitted[:6]) or "rules Kynver did not send"
        return (
            f"STANDING RULES FULL: {missing} rule(s) did not fit the {fit.budget}-token budget "
            f"and are NOT loaded ({names}). Tell Will now, and offer to merge, shorten or retire "
            "rules, or raise kynver.standing_rules_token_budget."
        )
    if fit.nearly_full:
        return (
            f"Standing rules are nearly full ({fit.used_tokens}/{fit.budget} tokens). "
            "Mention it to Will before adding another rule."
        )
    return ""


ALWAYS_HEADER = (
    "# Standing rules (Kynver)\n"
    "Will's standing rules. They apply on EVERY turn and to every worker you brief, "
    "whatever the topic. They override older memories that disagree. Topic rules arrive "
    "with the message that mentions the topic."
)


@dataclass(frozen=True)
class TopicPolicy:
    """One ``topicPolicy`` response (docs/runbooks/standing-rules-topic-loading.md)."""

    topic_set_key: str
    fingerprint: str
    always_markdown: str
    topics_markdown: str
    status_line: str
    always_over_cap: bool
    needs_merge: list[str] = field(default_factory=list)

    @property
    def cache_key(self) -> str:
        import hashlib

        body = hashlib.sha1(self.topics_markdown.encode("utf-8")).hexdigest()[:12]
        return f"{self.topic_set_key}|{body}"


def parse_topic_policy(payload: Any) -> TopicPolicy | None:
    """None when the server predates topic loading (caller falls back to budget packing)."""
    if not isinstance(payload, Mapping):
        return None
    tp = payload.get("topicPolicy")
    if not isinstance(tp, Mapping):
        return None
    markdown = tp.get("markdown") if isinstance(tp.get("markdown"), Mapping) else {}
    always = tp.get("always") if isinstance(tp.get("always"), Mapping) else {}
    topics = tp.get("topics") if isinstance(tp.get("topics"), list) else []
    return TopicPolicy(
        topic_set_key=str(tp.get("topicSetKey") or ""),
        fingerprint=str(tp.get("fingerprint") or ""),
        always_markdown=str(markdown.get("always") or "").strip(),
        topics_markdown=str(markdown.get("topics") or "").strip(),
        status_line=str(tp.get("statusLine") or "").strip(),
        always_over_cap=bool(always.get("overCap")),
        needs_merge=[str(t.get("slug")) for t in topics if isinstance(t, Mapping) and t.get("needsMerge")],
    )


def topic_policy_always_block(policy: TopicPolicy) -> str:
    """System-prompt block: the every-turn floor only (byte-stable for the session)."""
    body = policy.always_markdown
    if body.startswith("# "):
        body = body.split("\n", 1)[1].strip() if "\n" in body else ""
    parts = [ALWAYS_HEADER]
    if body:
        parts.append(body)
    if policy.always_over_cap:
        parts.append(
            f"WARNING: the every-turn rules are over their cap ({policy.status_line}). "
            "Tell Will and offer to move some under a topic. No rule was dropped."
        )
    return "\n".join(parts)


def topic_policy_turn_block(policy: TopicPolicy) -> str:
    """Turn-context block: the matched topics' rules (never placed in the system prompt)."""
    if not policy.topic_set_key or not policy.topics_markdown:
        return ""
    block = policy.topics_markdown
    if policy.needs_merge:
        block += (
            f"\n_(Topic(s) {', '.join(policy.needs_merge)} are large and should be merged; "
            "all rules are still shown.)_"
        )
    return block


def summarize_for_status(fit: StandingRulesFit) -> dict[str, Any]:
    return {
        "loaded": len(fit.included),
        "omitted": list(fit.omitted),
        "usedTokens": fit.used_tokens,
        "budget": fit.budget,
        "full": fit.full,
        "nearlyFull": fit.nearly_full,
        "serverTotal": fit.server_total,
    }


__all__: Sequence[str] = (
    "DEFAULT_STANDING_RULES_TOKEN_BUDGET",
    "INSTRUCTION_POLICY_PATH",
    "StandingRulesFit",
    "TopicPolicy",
    "fit_standing_rules",
    "parse_topic_policy",
    "topic_policy_always_block",
    "topic_policy_turn_block",
    "standing_rules_notice",
    "summarize_for_status",
)
