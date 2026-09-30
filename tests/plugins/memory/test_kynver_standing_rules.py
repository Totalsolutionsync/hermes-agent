"""Kynver standing rules: loaded once per session, whole rules only, loud when full."""

from types import SimpleNamespace

import pytest

POLICY_PATH = "/memory/instruction-policy?includeMarkdown=1"


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
