"""Kynver AgentOS HTTP bridge for Hermes operating-state tools.

Stays inside the plugin package: Kynver integration is an external adapter boundary,
not core Hermes. Scope: memory, sessions, tasks/todo, plans, skills, context envelope.
Do not use for local machine-control tools (terminal, file, browser, media).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home

_DEFAULT_API_URL = "https://www.kynver.com"
_DEFAULT_AGENT_OS_SLUG = "ghost"
_DEFAULT_TIMEOUT_SECONDS = 120.0


class KynverAgentOSError(RuntimeError):
    """AgentOS bridge failure with redacted message text."""


@dataclass(frozen=True)
class KynverAgentOSConfig:
    """Runtime config for Kynver AgentOS API calls."""

    api_url: str = _DEFAULT_API_URL
    api_key: str = ""
    slug: str = _DEFAULT_AGENT_OS_SLUG
    timeout: float = _DEFAULT_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return bool(self.api_url and self.api_key and self.slug)


def _active_env_path() -> Path:
    return get_hermes_home() / ".env"


def _load_profile_env(path: Path | None = None) -> dict[str, str]:
    env_path = path or _active_env_path()
    out: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = value.strip().strip('"\'')
    return out


def load_kynver_agentos_config(env: Mapping[str, str] | None = None) -> KynverAgentOSConfig:
    """Load Kynver config from process env over active profile ``.env``."""

    profile_env = _load_profile_env()
    merged: dict[str, str] = dict(profile_env)
    merged.update(dict(env or os.environ))

    api_url = (merged.get("KYNVER_API_URL") or _DEFAULT_API_URL).strip().rstrip("/")
    api_key = (merged.get("KYNVER_API_KEY") or "").strip()
    slug = (merged.get("KYNVER_AGENT_OS_SLUG") or _DEFAULT_AGENT_OS_SLUG).strip()
    raw_timeout = (merged.get("KYNVER_FETCH_TIMEOUT_MS") or "").strip()
    timeout = _DEFAULT_TIMEOUT_SECONDS
    if raw_timeout:
        try:
            timeout = max(1.0, float(raw_timeout) / 1000.0)
        except ValueError:
            timeout = _DEFAULT_TIMEOUT_SECONDS
    return KynverAgentOSConfig(api_url=api_url, api_key=api_key, slug=slug, timeout=timeout)


def _redact(text: str) -> str:
    import re

    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", text)
    redacted = re.sub(
        r"(?i)(api[_-]?key|token|secret|password)=([^\s&]+)",
        r"\1=[REDACTED]",
        redacted,
    )
    return redacted[:2000]


class KynverAgentOSClient:
    """Small REST client aligned with ``@kynver-app/mcp-agent-os`` routes."""

    def __init__(self, config: KynverAgentOSConfig | None = None):
        self.config = config or load_kynver_agentos_config()

    def api_path(self, path: str, *, slug: str | None = None) -> str:
        if "\x00" in path or ".." in path.split("?")[0].split("/"):
            raise KynverAgentOSError("Invalid AgentOS API path")
        clean = path if path.startswith("/") else f"/{path}"
        if clean.startswith("/api/"):
            return clean
        if clean.startswith("/agent-os/"):
            return f"/api{clean}"
        effective_slug = urllib.parse.quote(
            (slug or self.config.slug or _DEFAULT_AGENT_OS_SLUG).strip(),
            safe="",
        )
        return f"/api/agent-os/{effective_slug}{clean}"

    def request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        slug: str | None = None,
    ) -> Any:
        if not self.config.enabled:
            raise KynverAgentOSError(
                "Kynver AgentOS is not configured: set KYNVER_API_URL, "
                "KYNVER_API_KEY, and KYNVER_AGENT_OS_SLUG in the active Hermes profile .env."
            )
        url = f"{self.config.api_url}{self.api_path(path, slug=slug)}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "User-Agent": "hermes-forge-kynver-agentos-bridge/0.2",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as res:  # nosec B310
                payload = res.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise KynverAgentOSError(_redact(f"Kynver AgentOS HTTP {exc.code}: {detail}")) from exc
        except Exception as exc:
            raise KynverAgentOSError(_redact(f"Kynver AgentOS request failed: {exc}")) from exc
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload

    def get(self, path: str, *, slug: str | None = None) -> Any:
        return self.request("GET", path, slug=slug)

    def post(self, path: str, body: Any, *, slug: str | None = None) -> Any:
        return self.request("POST", path, body, slug=slug)

    def patch(self, path: str, body: Any, *, slug: str | None = None) -> Any:
        return self.request("PATCH", path, body, slug=slug)

    def delete(self, path: str, *, slug: str | None = None) -> Any:
        return self.request("DELETE", path, slug=slug)


def agentos_available(env: Mapping[str, str] | None = None) -> bool:
    return load_kynver_agentos_config(env).enabled


def probe_agentos_health(client: KynverAgentOSClient | None = None) -> bool:
    """Lightweight health check via GET ``/stats`` (same route as MCP context tool)."""

    try:
        c = client or KynverAgentOSClient()
        if not c.config.enabled:
            return False
        c.get("/stats")
        return True
    except Exception:
        return False
