"""Kynver write source ids follow the agent's name, never a hardcoded Forge."""

import pytest

from plugins.memory.kynver.compaction_flush import is_security_lane_content
from plugins.memory.kynver.source_id import (
    agent_display_name,
    context_tag,
    resolve_flush_source_id,
    resolve_source_id,
    skill_sync_source_id,
)


def _home(tmp_path, monkeypatch, *, profile=None, soul=None, config=None):
    home = (
        tmp_path / ".hermes" / "profiles" / profile if profile else tmp_path / ".hermes"
    )
    home.mkdir(parents=True)
    if soul is not None:
        (home / "SOUL.md").write_text(soul, encoding="utf-8")
    if config is not None:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_forge_named_default_keeps_forge_ids(tmp_path, monkeypatch):
    _home(
        tmp_path,
        monkeypatch,
        soul='Identity: your name is Forge (also "Hermes Forge"), Will\'s agent.',
    )
    assert resolve_source_id() == "hermes:forge"
    assert resolve_flush_source_id() == "hermes:forge:flush"


def test_shade_in_fortress_profile_is_named_shade(tmp_path, monkeypatch):
    _home(
        tmp_path,
        monkeypatch,
        profile="fortress",
        soul="You are Shade. Your name is Shade, never Forge.",
    )
    assert resolve_source_id() == "hermes:shade"
    assert resolve_flush_source_id() == "hermes:shade:flush"


def test_another_users_default_profile_named_nova(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, soul="Your name is Nova.")
    assert resolve_source_id() == "hermes:nova"


def test_config_name_beats_soul(tmp_path, monkeypatch):
    _home(
        tmp_path,
        monkeypatch,
        soul="Your name is Forge.",
        config="kynver:\n  agent_name: Ada Lovelace\n",
    )
    assert resolve_source_id() == "hermes:ada-lovelace"


def test_unnamed_default_sets_no_source_so_kynver_infers_it(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, soul="You are Hermes Agent, built by Nous Research.")
    assert resolve_source_id() is None
    assert resolve_flush_source_id() is None
    assert agent_display_name() is None
    assert context_tag() == "hermes"
    assert skill_sync_source_id() == "hermes:agent"


def test_explicit_source_id_in_profile_config_wins(tmp_path, monkeypatch):
    _home(
        tmp_path,
        monkeypatch,
        profile="fortress",
        soul="Your name is Forge.",
        config="kynver:\n  source_id: hermes:shade:flush\n",
    )
    assert resolve_source_id() == "hermes:shade"
    assert resolve_flush_source_id() == "hermes:shade:flush"


def test_shade_provenance_names_shade(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, profile="fortress", soul="Your name is Shade.")
    assert agent_display_name() == "Shade"
    assert context_tag() == "hermes-shade"


def test_unnamed_flush_body_omits_source(tmp_path, monkeypatch):
    from plugins.memory.kynver.compaction_flush import build_memory_body

    _home(tmp_path, monkeypatch)
    body = build_memory_body(
        {"text": "Deploys run on Tuesdays.", "kind": "fact", "confidence": 0.9},
        session_id="s1",
        provenance={},
    )
    assert "sourceId" not in body


def test_shade_flush_body_uses_shade_flush(tmp_path, monkeypatch):
    from plugins.memory.kynver.compaction_flush import build_memory_body

    _home(tmp_path, monkeypatch, profile="fortress", soul="Your name is Shade.")
    body = build_memory_body(
        {"text": "Deploys run on Tuesdays.", "kind": "fact", "confidence": 0.9},
        session_id="s1",
        provenance={},
    )
    assert body["sourceId"] == "hermes:shade:flush"


def test_unnamed_named_profile_uses_profile(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, profile="research")
    assert resolve_source_id() == "hermes:research"


def test_two_profiles_in_one_process_do_not_share_a_cached_id(tmp_path, monkeypatch):
    _home(tmp_path / "a", monkeypatch, soul="Your name is Forge.")
    assert resolve_source_id() == "hermes:forge"
    _home(tmp_path / "b", monkeypatch, profile="fortress", soul="Your name is Shade.")
    assert resolve_source_id() == "hermes:shade"


@pytest.mark.parametrize(
    "sid", ["hermes:forge:flush", "hermes:shade:flush", "hermes:nova:flush"]
)
def test_flush_ids_of_ordinary_agents_are_not_security_lane(sid):
    # Every chat's flush carries its agent's flush id: the id alone must not hide it from recall.
    assert not is_security_lane_content("benign", source_id=sid)


@pytest.mark.parametrize("sid", ["hermes:fortress:flush", "hermes:red-team:flush"])
def test_flush_ids_of_security_profiles_hit_security_exclusion(sid):
    assert is_security_lane_content("benign", source_id=sid)


def test_plain_agent_source_is_not_security_lane():
    assert not is_security_lane_content("benign", source_id="hermes:shade")
