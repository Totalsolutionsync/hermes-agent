"""Regression gate: standing-rule packs attach only for what the owner said this turn.

Each case leaked the full kynver-dev pack (53 rules, ~34k chars) into Will's Kynver chat
631607b0 on 7 Nov 2026. The fake server below matches the way the leak happened: any
"kynver" in the ``text`` Hermes sends as the owner's words attaches the pack. So these fail
if Hermes forwards a reply-quote header, a report-answer header or Kynver's own wording as
the owner's words. The quote travels in ``quote``; whether a quote is about a topic in
substance is Kynver's call, pinned by Kynver's standing-rules-relevance.regression.test.ts.
Runs in CI: ci.yaml -> tests.yml -> scripts/run_tests.sh collects the whole tests/ tree.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

# _home is the autouse HERMES_HOME fixture; importing it applies it here.
from tests.plugins.memory.test_kynver_standing_rules import FakeClient, _home, _provider, _topic_payload

PACK_RULES = [f"- kynver-dev rule {i}: " + "x" * 600 for i in range(1, 54)]
PACK_MD = "# Standing rules for this conversation's topics\n\n## Kynver development\n" + "\n".join(PACK_RULES)

# state.db message 650168, verbatim shape: a 6-word follow-up under a reply-quote header.
LANDED_QUOTE = (
    "Agreed, no cap on how many rules can attach. The fix is to attach rules only when they're relevant. "
    "- **Add tests** so a cooking question gets no developer rules and a real Kynver code question still "
    "gets all of them. - **Fix the related skill hints** that get triggered by everyday words like \"answer\". "
    "Changes go into PRs that won't be merged without my check."
)
LANDED_TURN = (
    f'[Replying to assistant message from 2026-11-07 21:53 UTC: "{LANDED_QUOTE}"]\n\n'
    "Please make sure this gets landed"
)
REPORT_ANSWER_TURN = "\n".join(
    [
        "[Kynver report answers] The owner sent 1 item on your report in Kynver.",
        "- [comment] Breakfast shopping list v1 — Will: get the Jimmy Dean sausage and skip the canned "
        "biscuits (commentId cmt_1) — reportId rpt_1",
        "",
        "Act on each one now: an approval or chosen option is the go-ahead for exactly what that ask proposed "
        "(still follow the usual rules for anything public, paid or irreversible). If an answer needs a "
        "follow-up question, ask it. When done, say what you did per item; publish a new version with the same "
        "seriesKey if the report itself should change.",
    ]
)
DEV_TURN = "fix the typecheck error in src/modules/agent-os"


class BrandLeakServer(FakeClient):
    """Kynver as it behaved when the pack leaked: 'kynver' or 'typecheck' in the owner text attaches the pack."""

    def get(self, path, *, slug=None, timeout=None):
        self.calls.append(path)
        q = parse_qs(urlsplit(path).query)
        sent = q.get("text", [""])[0].lower()
        if "kynver" in sent or "typecheck" in sent:
            return _topic_payload("kynver-dev", PACK_MD)
        return _topic_payload()


def _sent(provider) -> dict[str, str]:
    q = parse_qs(urlsplit(provider._client.calls[-1]).query)
    return {k: v[0] for k, v in q.items()}


def _turn(text: str) -> tuple[str, dict[str, str]]:
    provider = _provider(BrandLeakServer())
    provider.system_prompt_block()
    block = provider._standing_rules_turn_block(text, "s1")
    return block, _sent(provider) if provider._client.calls[1:] else {}


def test_a_landed_follow_up_with_kynver_reply_quote_injects_no_kynver_dev():
    block, sent = _turn(LANDED_TURN)
    assert len(block) == 0
    assert sent["text"] == "Please make sure this gets landed"
    assert "Replying to" not in sent["text"]


def test_a_quote_goes_separately_so_kynver_can_judge_it_on_substance():
    _block, sent = _turn(LANDED_TURN)
    assert sent.get("quote", "").startswith("Agreed, no cap")


def test_b_report_answer_header_plus_cooking_reply_injects_nothing():
    block, sent = _turn(REPORT_ANSWER_TURN)
    assert len(block) == 0
    assert "Jimmy Dean sausage" in sent["text"]
    for kynver_words in ("report answers", "on your report in Kynver", "Act on each one now", "commentId", "reportId"):
        assert kynver_words not in sent["text"]


def test_hermes_gateway_reply_header_is_split_off_too():
    turn = '[Replying to your previous message: "Kynver status:\nall PRs merged."]\n\nthanks, what\'s for dinner?'
    block, sent = _turn(turn)
    assert block == "" and sent["text"] == "thanks, what's for dinner?"


def test_c_real_dev_question_gets_the_full_pack():
    block, sent = _turn(DEV_TURN)
    assert sent["text"] == DEV_TURN
    for rule in PACK_RULES:
        assert rule in block
    assert block.count("kynver-dev rule ") == 53
