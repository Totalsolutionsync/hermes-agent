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


# --- Belt and braces: never load a superseded / tombstone rule (Kynver incident 22 Oct 2026) ---


def test_stale_rule_text_detection():
    from plugins.memory.kynver.standing_rules import is_stale_rule_text

    for text in (
        "Superseded historical preference: Cursor was previously the default worker engine.",
        "Previously Forge used Cursor Composer for every worker.",
        "[REPLACED by sonnet-version-policy — do not use as current.] Keep Cursor credits.",
        "Cursor default — no longer active since 17 Oct.",
    ):
        assert is_stale_rule_text(text), text
    for text in (
        "Never write to the D: drive.",
        "Claude Code workers dispatch on the current Sonnet version.",
        "Always confirm before sending email. Drafts that are no longer active must be deleted at the end.",
        "",
        None,
    ):
        assert not is_stale_rule_text(text), text


def test_legacy_packing_skips_superseded_and_tombstone_rules():
    from plugins.memory.kynver.standing_rules import fit_standing_rules

    live = {"slug": "d-drive", "content": "Never write to the D: drive.", "tier": "custom"}
    tomb = {"slug": "cursor-note", "content": "Superseded historical preference: Cursor was the default.", "tier": "custom"}
    sup = {"slug": "codex-until-cursor", "content": "Use Codex until Cursor is restored.", "tier": "custom", "status": "superseded"}
    retired = {
        "slug": "sonnet-5",
        "content": "Dispatch Claude Code workers on Sonnet 5.",
        "tier": "custom",
        "metadata": {"operatingRuleRetiredAt": "2026-10-17T00:00:00Z"},
    }
    fit = fit_standing_rules(_payload(live, tomb, sup, retired), budget=2000)
    assert fit.included == ["d-drive"]
    assert "Cursor" not in fit.markdown and "Codex" not in fit.markdown and "Sonnet 5" not in fit.markdown


def test_topic_mode_strips_stale_bullets_and_empty_headings():
    from plugins.memory.kynver.standing_rules import parse_topic_policy, topic_policy_always_block, topic_policy_turn_block

    payload = {
        "topicPolicy": {
            "topicSetKey": "kynver-dev",
            "fingerprint": "f",
            "statusLine": "Standing rules: always 2 · kynver-dev 1",
            "always": {"overCap": False},
            "topics": [{"slug": "kynver-dev"}],
            "markdown": {
                "always": "# Standing rules (every turn)\n- Never write to the D: drive.\n- Previously Forge used Cursor Composer\n  for every worker.",
                "topics": "# Standing rules for this conversation's topics\n\n## Kynver dev\n- Superseded: use Codex until Cursor is restored.\n\n## Email\n- CC Will on outbound email.",
            },
        }
    }
    policy = parse_topic_policy(payload)
    always = topic_policy_always_block(policy)
    turn = topic_policy_turn_block(policy)
    assert "D: drive" in always and "Cursor" not in always and "for every worker" not in always
    assert "Kynver dev" not in turn and "Codex" not in turn
    assert "## Email\n- CC Will on outbound email." in turn


def test_topic_mode_all_stale_topic_block_is_empty():
    from plugins.memory.kynver.standing_rules import parse_topic_policy, topic_policy_turn_block

    payload = {
        "topicPolicy": {
            "topicSetKey": "kynver-dev",
            "markdown": {"always": "", "topics": "# Standing rules for this conversation's topics\n\n## Kynver dev\n- [REPLACED by x] Use Cursor workers."},
        }
    }
    assert topic_policy_turn_block(parse_topic_policy(payload)) == ""
