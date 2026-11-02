"""Kynver write source ids follow the running profile; the default profile stays hermes:forge."""

from plugins.memory.kynver.compaction_flush import is_security_lane_content
from plugins.memory.kynver.source_id import resolve_flush_source_id, resolve_source_id


def test_default_profile_keeps_forge_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert resolve_source_id() == "hermes:forge"
    assert resolve_flush_source_id() == "hermes:forge:flush"


def test_unset_profile_env_is_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_source_id() == "hermes:forge"


def test_named_profile_gets_own_ids(tmp_path, monkeypatch):
    home = tmp_path / ".hermes" / "profiles" / "fortress"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert resolve_source_id() == "hermes:fortress"
    assert resolve_flush_source_id() == "hermes:fortress:flush"


def test_flush_ids_of_any_profile_hit_security_exclusion():
    for sid in ("hermes:forge:flush", "hermes:fortress:flush"):
        assert is_security_lane_content("benign", source_id=sid)
    assert not is_security_lane_content("benign", source_id="hermes:fortress")
