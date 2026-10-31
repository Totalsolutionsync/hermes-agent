"""Kynver /model route guard: provider-prefixed model ids route to the provider they name.

Regression: ``/model custom:venice:qwen-3-6-plus`` for a ``providers.venice`` entry kept the
current provider (Anthropic) and sent the whole string as the model id. The plugin's
``pre_gateway_dispatch`` hook rewrites it to ``/model qwen-3-6-plus --provider venice``, which
Hermes routes correctly, and turns an unresolvable ``custom:<slug>`` into an explicit
``--provider`` so Hermes refuses it loudly instead of falling back.
"""

from types import SimpleNamespace

import pytest

from gateway.platforms.event import MessageEvent
from hermes_cli.model_switch import parse_model_flags_detailed
from hermes_cli.providers import resolve_provider_full
from plugins.memory.kynver import model_route_guard as guard

PROVIDERS = {
    "venice": {
        "name": "Venice AI",
        "api": "https://api.venice.ai/api/v1",
        "default_model": "qwen-3-6-plus",
        "models": ["qwen-3-6-plus"],
    }
}
CUSTOM = [{"name": "lab-box", "base_url": "http://10.0.0.5:8000/v1"}]


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(guard, "_load_providers", lambda: (PROVIDERS, CUSTOM))


def _rewrite(args):
    return guard.rewrite_model_args(args, loader=lambda: (PROVIDERS, CUSTOM))


@pytest.mark.parametrize(
    "typed",
    [
        "custom:venice:qwen-3-6-plus",
        "custom:venice/qwen-3-6-plus",
        "custom:venice-ai:qwen-3-6-plus",
        "custom:Venice-AI/qwen-3-6-plus",
        "venice:qwen-3-6-plus",
        "venice/qwen-3-6-plus",
        "venice-ai:qwen-3-6-plus",
    ],
)
def test_configured_provider_prefix_becomes_explicit_provider(typed):
    new = _rewrite(typed)
    assert new == "qwen-3-6-plus --provider venice"
    parsed = parse_model_flags_detailed(new)
    assert (parsed.model_input, parsed.explicit_provider) == ("qwen-3-6-plus", "venice")
    # Hermes resolves the rewritten provider to the Venice endpoint (no fallback path).
    pdef = resolve_provider_full(parsed.explicit_provider, PROVIDERS, None)
    assert pdef is not None and pdef.base_url == "https://api.venice.ai/api/v1"


def test_flags_are_kept_and_model_with_slashes_survives():
    assert _rewrite("--global custom:venice:org/model:free --reasoning high") == (
        "--global org/model:free --reasoning high --provider venice"
    )
    assert _rewrite("--reasoning low venice:qwen-3-6-plus") == "--reasoning low qwen-3-6-plus --provider venice"


def test_unresolvable_custom_slug_is_made_explicit_so_hermes_refuses_it():
    new = _rewrite("custom:veniec:qwen-3-6-plus")
    assert new == "qwen-3-6-plus --provider custom:veniec"
    parsed = parse_model_flags_detailed(new)
    # Hermes' explicit-provider path fails with "Unknown provider ..." when this is None.
    assert resolve_provider_full(parsed.explicit_provider, PROVIDERS, None) is None


def test_legacy_custom_providers_entry_keeps_hermes_custom_id():
    assert _rewrite("custom:lab-box:llama-3") == "llama-3 --provider custom:lab-box"


@pytest.mark.parametrize(
    "typed",
    [
        "qwen-3-6-plus",                       # bare model id
        "qwen-3-6-plus --provider venice",     # already explicit
        "qwen-3-6-plus \u2014provider venice",  # iOS em dash still counts as explicit
        "anthropic/claude-opus-4",             # not a providers: entry -> Hermes' own routing
        "qwen3.5:4b",                          # Ollama tag stays intact
        "--global",                            # no model token
        "",
    ],
)
def test_everything_else_is_left_alone(typed):
    assert _rewrite(typed) is None


def _event(text):
    return MessageEvent(text=text)


def test_hook_rewrites_model_command_text():
    out = guard.on_pre_gateway_dispatch(event=_event("/model custom:venice:qwen-3-6-plus"))
    assert out == {"action": "rewrite", "text": "/model qwen-3-6-plus --provider venice"}


def test_hook_handles_telegram_bot_suffix():
    out = guard.on_pre_gateway_dispatch(event=_event("/model@KynverBot venice:qwen-3-6-plus"))
    assert out == {"action": "rewrite", "text": "/model qwen-3-6-plus --provider venice"}


@pytest.mark.parametrize(
    "text",
    ["/qwen", "/model qwen-3-6-plus --provider venice", "hello venice:qwen-3-6-plus", "/models venice:x"],
)
def test_hook_passes_other_messages_through(text):
    assert guard.on_pre_gateway_dispatch(event=_event(text)) is None


def test_hook_never_raises():
    assert guard.on_pre_gateway_dispatch(event=SimpleNamespace(text=None)) is None
    assert guard.on_pre_gateway_dispatch(event=None) is None


def test_plugin_register_subscribes_pre_gateway_dispatch(monkeypatch):
    import plugins.memory.kynver as kynver_plugin

    hooks = []
    ctx = SimpleNamespace(
        register_memory_provider=lambda *_a, **_k: None,
        register_hook=lambda name, cb, **_k: hooks.append((name, cb)),
        register_cli_command=lambda *_a, **_k: None,
        register_tool=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(kynver_plugin, "_register_compaction_flush_task", lambda _ctx: None, raising=False)
    kynver_plugin.register(ctx)
    assert ("pre_gateway_dispatch", guard.on_pre_gateway_dispatch) in hooks
