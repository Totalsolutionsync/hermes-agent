"""Tests for platform-aware compression settings resolution."""

from agent.compression_settings import resolve_compression_settings


def test_defaults_without_platform():
    resolved = resolve_compression_settings({"target_ratio": 0.20, "protect_last_n": 20})
    assert resolved["target_ratio"] == 0.20
    assert resolved["protect_last_n"] == 20


def test_telegram_platform_override():
    cfg = {
        "target_ratio": 0.20,
        "protect_last_n": 20,
        "platform_overrides": {
            "telegram": {"protect_last_n": 8, "target_ratio": 0.16},
        },
    }
    cli = resolve_compression_settings(cfg, platform="cli")
    assert cli["protect_last_n"] == 20
    assert cli["target_ratio"] == 0.20

    telegram = resolve_compression_settings(cfg, platform="telegram")
    assert telegram["protect_last_n"] == 8
    assert telegram["target_ratio"] == 0.16


def test_unknown_platform_keeps_defaults():
    cfg = {"platform_overrides": {"telegram": {"protect_last_n": 8}}}
    resolved = resolve_compression_settings(cfg, platform="discord")
    assert resolved["protect_last_n"] == 20
