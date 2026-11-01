"""Per-provider daily spend cap.

``providers.<name>.daily_spend_cap_usd`` stops main-loop calls to that provider once today's
recorded spend reaches the cap; the turn ends with a plain message instead of calling the API.
Spend is accumulated per local calendar day in ``<HERMES_HOME>/state/provider_spend/<date>.json``
from the cost Hermes already estimates for each call. Hermes has no built-in price for many custom
providers (Venice returns "unknown"), so ``providers.<name>.pricing`` may give
``{input_per_mtok, output_per_mtok}`` (USD per million tokens) to price them; with a cap but no
usable price nothing is recorded, so set both.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _provider_key(provider: Any) -> str:
    name = str(provider or "").strip().lower()
    return name.split(":", 1)[1] if name.startswith("custom:") else name


def _provider_cfg(provider: Any) -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        cfg = (load_config().get("providers") or {}).get(_provider_key(provider)) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def daily_cap_usd(provider: Any) -> Optional[float]:
    cap = _provider_cfg(provider).get("daily_spend_cap_usd")
    return float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0 else None


def _ledger_path(today: Optional[dt.date] = None):
    return get_hermes_home() / "state" / "provider_spend" / f"{(today or dt.date.today()).isoformat()}.json"


def spent_today_usd(provider: Any, today: Optional[dt.date] = None) -> float:
    try:
        data = json.loads(_ledger_path(today).read_text(encoding="utf-8-sig"))
        return float(data.get(_provider_key(provider), 0.0))
    except Exception:
        return 0.0


def price_usd(provider: Any, usage: Any) -> Optional[float]:
    """Cost from ``providers.<name>.pricing`` for a CanonicalUsage, else None."""
    p = _provider_cfg(provider).get("pricing")
    if not isinstance(p, dict):
        return None
    try:
        return (usage.input_tokens * float(p["input_per_mtok"])
                + (usage.output_tokens + usage.reasoning_tokens) * float(p["output_per_mtok"])) / 1e6
    except Exception:
        return None


def record(provider: Any, amount_usd: Optional[float], today: Optional[dt.date] = None) -> None:
    """Add ``amount_usd`` to today's ledger for ``provider`` (only providers with a cap are tracked)."""
    if not amount_usd or amount_usd <= 0 or daily_cap_usd(provider) is None:
        return
    path = _ledger_path(today)
    try:
        from utils import atomic_json_write
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}
        key = _provider_key(provider)
        data[key] = round(float(data.get(key, 0.0)) + float(amount_usd), 6)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, data)
    except Exception:
        logger.warning("Could not record provider spend", exc_info=True)


def cap_reached_message(provider: Any) -> Optional[str]:
    """Plain-English refusal when today's spend on ``provider`` has reached its cap, else None."""
    cap = daily_cap_usd(provider)
    if cap is None:
        return None
    spent = spent_today_usd(provider)
    if spent < cap:
        return None
    name = _provider_key(provider)
    return (f"Daily spend cap reached for {name}: ${spent:.2f} of ${cap:.2f} used today. No request was sent. "
            f"Switch model (/model) or raise providers.{name}.daily_spend_cap_usd in config.yaml.")
