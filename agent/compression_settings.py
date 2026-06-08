"""Resolve compression settings with optional per-platform overrides.

Gateway sessions (Telegram ops, etc.) can tune protect_last_n and
target_ratio without changing global defaults for CLI sessions.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        iv = int(value)
        return iv if iv >= 0 else default
    except (TypeError, ValueError):
        return default


def resolve_compression_settings(
    compression_cfg: Optional[Dict[str, Any]],
    *,
    platform: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge ``compression`` config with ``platform_overrides`` for *platform*.

    Returns keys: ``target_ratio``, ``protect_last_n``, ``protect_first_n``,
    ``threshold``, ``enabled``, ``abort_on_summary_failure`` — only keys
    present in the merged result are overrides callers should apply; base
    defaults are applied by :func:`agent.agent_init` when keys are absent.
    """
    cfg = compression_cfg if isinstance(compression_cfg, dict) else {}

    resolved: Dict[str, Any] = {
        "target_ratio": _coerce_float(cfg.get("target_ratio"), 0.20),
        "protect_last_n": _coerce_int(cfg.get("protect_last_n"), 20),
        "protect_first_n": _coerce_int(cfg.get("protect_first_n"), 3),
        "threshold": _coerce_float(cfg.get("threshold"), 0.50),
        "enabled": cfg.get("enabled", True),
        "abort_on_summary_failure": cfg.get("abort_on_summary_failure", False),
    }

    if not platform:
        return resolved

    overrides_root = cfg.get("platform_overrides")
    if not isinstance(overrides_root, dict):
        return resolved

    platform_key = str(platform).strip().lower()
    override = overrides_root.get(platform_key)
    if not isinstance(override, dict):
        return resolved

    if "target_ratio" in override:
        resolved["target_ratio"] = _coerce_float(override["target_ratio"], resolved["target_ratio"])
    if "protect_last_n" in override:
        resolved["protect_last_n"] = _coerce_int(override["protect_last_n"], resolved["protect_last_n"])
    if "protect_first_n" in override:
        resolved["protect_first_n"] = _coerce_int(override["protect_first_n"], resolved["protect_first_n"])
    if "threshold" in override:
        resolved["threshold"] = _coerce_float(override["threshold"], resolved["threshold"])

    return resolved
