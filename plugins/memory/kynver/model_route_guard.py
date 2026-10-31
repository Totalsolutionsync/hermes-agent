"""Make ``/model <provider>:<model>`` route to the provider it names, or fail loudly.

Hermes' ``/model`` resolves ``custom:<name>:<model>`` only against the legacy
``custom_providers:`` list. For an entry under ``providers:`` (e.g. ``providers.venice``)
``/model custom:venice:qwen-3-6-plus`` reported success, kept the CURRENT provider and sent
the whole string as the model id (only a "not found in /v1/models" warning) — a silent
fallback to Anthropic. ``/model qwen-3-6-plus --provider venice`` routes correctly and an
unknown ``--provider`` is refused with "Unknown provider ...".

So, on the messaging gateway (``pre_gateway_dispatch``, before slash-command parsing), this
rewrites the model token into the ``--provider`` form:

* ``custom:<slug>:<model>`` / ``custom:<slug>/<model>`` -> ``<model> --provider <key>``
  for a configured provider; an UNRESOLVABLE slug becomes ``--provider custom:<slug>`` so
  Hermes refuses it loudly instead of falling back.
* ``<slug>:<model>`` / ``<slug>/<model>`` -> ``<model> --provider <key>`` only when ``<slug>``
  is a ``providers:`` entry (key, or its display name slugified: ``Venice AI`` -> ``venice-ai``).
  Anything else (``anthropic/claude-…``, Ollama tags ``qwen3.5:4b``) is left untouched.

Already-explicit ``--provider`` commands and every non-``/model`` message pass through.
Plugin-only: no Hermes core code is changed.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_MODEL_COMMANDS = frozenset({"model"})
_PROVIDER_FLAG = "--provider"
# Flags of /model that take a value (the token after them is not the model).
_VALUE_FLAGS = frozenset({_PROVIDER_FLAG, "--reasoning"})
# Telegram/iOS turn "--" into an em/en dash; Hermes normalizes these the same way.
_DASHED_FLAG = re.compile(r"[\u2012\u2013\u2014\u2015](provider|reasoning|global|session|refresh|once)")


def _slugify(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _load_providers() -> tuple[dict, list]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        logger.debug("model route guard: config unavailable", exc_info=True)
        return {}, []
    providers = cfg.get("providers")
    custom = cfg.get("custom_providers")
    return (providers if isinstance(providers, dict) else {}), (custom if isinstance(custom, list) else [])


def _user_provider_key(slug: str, providers: dict) -> Optional[str]:
    """The ``providers:`` key that ``slug`` names (key or slugified display name), else None."""
    want = _slugify(slug)
    if not want:
        return None
    for key, entry in providers.items():
        names = {_slugify(key)}
        if isinstance(entry, dict) and entry.get("name"):
            names.add(_slugify(entry["name"]))
        if want in names:
            return str(key)
    return None


def _custom_provider_slug(slug: str, custom: list) -> Optional[str]:
    """``custom:<name>`` for a legacy ``custom_providers:`` entry named ``slug``, else None."""
    want = _slugify(slug)
    for entry in custom:
        if isinstance(entry, dict) and want and _slugify(entry.get("name")) == want:
            return f"custom:{entry.get('name')}"
    return None


def _split_slug(token: str) -> Optional[tuple[str, str]]:
    """``slug:model`` / ``slug/model`` -> (slug, model) on the FIRST separator, else None."""
    match = re.match(r"^([^:/\s]+)[:/](.+)$", token)
    if not match or not match.group(2).strip():
        return None
    return match.group(1), match.group(2).strip()


def rewrite_model_args(
    args: str, loader: Optional[Callable[[], tuple[dict, list]]] = None
) -> Optional[str]:
    """New ``/model`` argument string, or None to leave the command as typed."""
    # Same tokenizing as Hermes' parse_model_flags_detailed: plain whitespace split.
    tokens = _DASHED_FLAG.sub(r"--\1", args).split()
    if not tokens or _PROVIDER_FLAG in tokens:
        return None
    idx, skip = None, False
    for i, tok in enumerate(tokens):
        if skip:
            skip = False
            continue
        if tok.startswith("--"):
            skip = tok in _VALUE_FLAGS
            continue
        idx = i
        break
    if idx is None:
        return None
    model_token = tokens[idx]
    is_custom = model_token.lower().startswith("custom:")
    split = _split_slug(model_token[len("custom:"):] if is_custom else model_token)
    if split is None:
        return None
    slug, model = split
    providers, custom = (loader or _load_providers)()
    provider = _user_provider_key(slug, providers)
    if provider is None and is_custom:
        # Legacy custom_providers entry -> Hermes' own custom:<name> id; otherwise keep the
        # unresolvable id so Hermes answers "Unknown provider" instead of a silent fallback.
        provider = _custom_provider_slug(slug, custom) or f"custom:{slug}"
    if provider is None:
        return None
    rest = tokens[:idx] + [model] + tokens[idx + 1:] + [_PROVIDER_FLAG, provider]
    return " ".join(rest)


def on_pre_gateway_dispatch(event: Any = None, **_: Any) -> Optional[dict]:
    """``pre_gateway_dispatch`` hook: rewrite a provider-prefixed ``/model`` into ``--provider`` form."""
    try:
        text = getattr(event, "text", None)
        if not isinstance(text, str) or not text.lstrip().startswith("/"):
            return None
        command = event.get_command() if hasattr(event, "get_command") else None
        if command not in _MODEL_COMMANDS:
            return None
        args = event.get_command_args() if hasattr(event, "get_command_args") else ""
        new_args = rewrite_model_args(str(args or "").strip())
        if new_args is None:
            return None
        logger.info("kynver model route guard: /model %s -> /model %s", args.strip(), new_args)
        return {"action": "rewrite", "text": f"/{command} {new_args}"}
    except Exception:
        logger.debug("kynver model route guard failed; leaving /model untouched", exc_info=True)
        return None


def register_model_route_guard(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
