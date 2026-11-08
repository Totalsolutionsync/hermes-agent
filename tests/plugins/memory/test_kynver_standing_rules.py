"""Kynver standing rules: loaded once per session, whole rules only, loud when full."""

from types import SimpleNamespace

import pytest

POLICY_PATH = "/memory/instruction-policy?includeMarkdown=1&topics="


class FakeClient:
    def __init__(self, payload=None, exc=None):
        self.calls = []
        self.payload = payload or {}
        self.exc = exc
        self.config = SimpleNamespace(
            enabled=True, observe_only=False, memory_disabled=False, tasks_disabled=False,
            skills_disabled=False, session_sync_disabled=True, todo_mirror_disabled=False,
            side_effect_timeout=3.0, timeout=3.0,
        )

    def get(self, path, *, slug=None, timeout=None):
        self.calls.append(path)
        if self.exc:
            raise self.exc
        return self.payload if path == POLICY_PATH else {}

    def post(self, path, body, *, slug=None, timeout=None):
        return {}

    def patch(self, path, body, *, slug=None, timeout=None):
        return {}


def _rule(slug, words, tier="custom"):
    return {"slug": slug, "memoryId": slug, "content": " ".join([f"{slug}-word"] * words), "tier": tier}


def _payload(*rules, total=None):
    policy = {
        "internalRules": [r for r in rules if r["tier"] == "internal"],
        "customRules": [r for r in rules if r["tier"] == "custom"],
        "personaRules": [],
    }
    if total is not None:
        policy["totalRuleCount"] = total
    return {"status": {}, "policy": policy}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _provider(client):
    from plugins.memory.kynver import KynverMemoryProvider

    provider = KynverMemoryProvider(client=client)
    provider.initialize(session_id="s1", platform="cli")
    return provider


def test_rules_reach_system_prompt_once_per_session_and_stay_byte_stable():
    client = FakeClient(_payload(_rule("media-on-d-drive", 5), _rule("floor", 3, tier="internal")))
    provider = _provider(client)

    first = provider.system_prompt_block()
    second = provider.system_prompt_block()

    assert first == second
    assert client.calls.count(POLICY_PATH) == 1
    assert "media-on-d-drive" in first and "floor" in first
    assert first.index("(floor)") < first.index("(media-on-d-drive)")
    assert "STANDING RULES FULL" not in first


def test_full_budget_drops_whole_rules_and_says_so(tmp_path):
    from plugins.memory.kynver.standing_rules import fit_standing_rules

    payload = _payload(_rule("a", 60), _rule("b", 60), _rule("c", 60))
    fit = fit_standing_rules(payload, budget=250)

    assert fit.full
    assert fit.included and fit.omitted
    assert set(fit.included) | set(fit.omitted) == {"a", "b", "c"}
    for slug in fit.omitted:
        assert f"({slug})" not in fit.markdown.split("STANDING RULES FULL")[0]
    assert "STANDING RULES FULL" in fit.markdown
    assert all(slug in fit.markdown.split("STANDING RULES FULL")[1] for slug in fit.omitted)
    assert fit.used_tokens <= fit.budget


def test_server_side_truncation_counts_as_full():
    from plugins.memory.kynver.standing_rules import fit_standing_rules

    fit = fit_standing_rules(_payload(_rule("a", 3), total=9), budget=1500)

    assert fit.full and not fit.omitted
    assert "8 rule(s)" in fit.markdown


def test_nearly_full_warns_before_it_overflows():
    from plugins.memory.kynver.standing_rules import fit_standing_rules

    fit = fit_standing_rules(_payload(_rule("a", 100)), budget=240)

    assert not fit.full and fit.nearly_full
    assert "nearly full" in fit.markdown


def test_unreachable_kynver_tells_the_agent_rules_are_missing():
    provider = _provider(FakeClient(exc=RuntimeError("503")))

    block = provider.system_prompt_block()

    assert "Standing rules (Kynver)" in block and "UNAVAILABLE" in block


def test_budget_zero_disables_and_config_bounds_apply(_home):
    (_home / "config.yaml").write_text("kynver:\n  standing_rules_token_budget: 0\n")
    client = FakeClient(_payload(_rule("a", 3)))
    provider = _provider(client)

    assert "Standing rules" not in provider.system_prompt_block()
    assert POLICY_PATH not in client.calls


def _topic_payload(topic_set_key="", topics_md=""):
    return {
        "topicPolicy": {
            "topicSetKey": topic_set_key,
            "fingerprint": "fp",
            "statusLine": "Standing rules: always 1 (~20 tokens)",
            "always": {"overCap": False},
            "topics": [],
            "markdown": {
                "always": "# Standing rules — always\n- (floor-rule) Always do the floor thing.",
                "topics": topics_md,
            },
        }
    }


class TopicClient(FakeClient):
    def get(self, path, *, slug=None, timeout=None):
        self.calls.append(path)
        if "text=" in path and "email" in path:
            return _topic_payload("email", "# Standing rules for this conversation's topics\n\n## Email\n- Email rule body.")
        return _topic_payload()


def test_topic_mode_puts_only_always_tier_in_system_prompt_without_full_warning():
    provider = _provider(TopicClient())
    block = provider.system_prompt_block()
    assert "Always do the floor thing" in block
    assert "Email rule body" not in block
    assert "STANDING RULES FULL" not in block
    assert provider._standing_rules_status["mode"] == "topics"


def test_topic_rules_arrive_once_per_session_on_matching_turn():
    provider = _provider(TopicClient())
    provider.system_prompt_block()
    assert provider._standing_rules_turn_block("what's the weather", "s1") == ""
    first = provider._standing_rules_turn_block("draft an email to Andy", "s1")
    assert "Email rule body" in first
    assert provider._standing_rules_turn_block("another email please", "s1") == ""
    assert "Email rule body" in provider._standing_rules_turn_block("email", "s2")


def test_topic_fetch_failure_fails_open():
    provider = _provider(TopicClient())
    provider.system_prompt_block()
    provider._client = FakeClient(exc=RuntimeError("down"))
    assert provider._standing_rules_turn_block("draft an email", "s1") == ""


class KynverDevClient(FakeClient):
    """Stands in for Kynver's matcher: any 'kynver' in the sent text attaches the kynver-dev pack."""

    def get(self, path, *, slug=None, timeout=None):
        from urllib.parse import parse_qs, urlsplit

        self.calls.append(path)
        sent = parse_qs(urlsplit(path).query).get("text", [""])[0].lower()
        if "kynver" in sent:
            return _topic_payload("kynver-dev", "# Standing rules for this conversation's topics\n\n## Kynver development\n- Kynver dev rule body.")
        return _topic_payload()


def test_cooking_question_with_relay_hint_gets_no_kynver_dev_pack():
    from agent.relay_text import KYNVER_RELAY_APPENDED_TEXT

    provider = _provider(KynverDevClient())
    provider.system_prompt_block()
    turn = (
        "Which sausage and biscuits should I buy? Make me a list.\n\n"
        f"{KYNVER_RELAY_APPENDED_TEXT[0]}\n\n[Image attached at: /tmp/api_1.jpg]\n[screenshot]"
    )
    assert provider._standing_rules_turn_block(turn, "s1") == ""
    sent = provider._client.calls[-1]
    assert "Kynver+files" not in sent and "Image+attached" not in sent and "sausage" in sent


def test_real_kynver_code_question_still_gets_the_pack():
    provider = _provider(KynverDevClient())
    provider.system_prompt_block()
    block = provider._standing_rules_turn_block("Why does the Kynver harness typecheck fail?", "s1")
    assert "Kynver dev rule body" in block


def test_attachment_only_turn_makes_no_topic_call():
    provider = _provider(KynverDevClient())
    provider.system_prompt_block()
    before = len(provider._client.calls)
    assert provider._standing_rules_turn_block("[Image attached at: /tmp/a.jpg]\n[screenshot]", "s1") == ""
    assert len(provider._client.calls) == before
