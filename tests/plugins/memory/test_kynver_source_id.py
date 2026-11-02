"""Kynver write source ids follow the agent's name, never a hardcoded Forge."""

import pytest

from plugins.memory.kynver.compaction_flush import is_security_lane_content
from plugins.memory.kynver.source_id import resolve_flush_source_id, resolve_source_id


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


def test_unnamed_default_is_never_forge(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, soul="You are Hermes Agent, built by Nous Research.")
    assert resolve_source_id() == "hermes:agent"


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
def test_flush_ids_of_any_agent_hit_security_exclusion(sid):
    assert is_security_lane_content("benign", source_id=sid)


def test_plain_agent_source_is_not_security_lane():
    assert not is_security_lane_content("benign", source_id="hermes:shade")
