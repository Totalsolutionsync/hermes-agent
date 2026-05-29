import pytest

from plugins.memory.kynver.substrate import (
    kynver_explicitly_disabled,
    resolve_memory_provider_name,
)


def test_explicit_disabled_env(monkeypatch):
    monkeypatch.setenv("KYNVER_DISABLED", "1")
    assert kynver_explicitly_disabled() is True


def test_auto_select_kynver_when_healthy(monkeypatch):
    monkeypatch.setenv("KYNVER_API_URL", "https://example.test")
    monkeypatch.setenv("KYNVER_API_KEY", "key")
    monkeypatch.setenv("KYNVER_AGENT_OS_SLUG", "forge")
    monkeypatch.setattr(
        "plugins.memory.kynver.substrate.probe_agentos_health",
        lambda client=None: True,
    )
    name = resolve_memory_provider_name({"provider": ""}, full_config={})
    assert name == "kynver"


def test_explicit_other_provider_not_overridden(monkeypatch):
    monkeypatch.setattr(
        "plugins.memory.kynver.substrate.probe_agentos_health",
        lambda client=None: True,
    )
    name = resolve_memory_provider_name({"provider": "honcho"}, full_config={})
    assert name == "honcho"
