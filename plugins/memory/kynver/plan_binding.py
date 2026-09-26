"""Per-session todo plan resolution — which AgentOS plan a Hermes session's todos land on.

Kynver owns the resolution order (explicit → session binding → task's plan → workspace
Inbox) behind ``POST /api/agent-os/{slug}/todos/plan-binding``. Hermes only supplies the
session key (``hermes:<compression-lineage root>``, i.e. the todo scope) and its linked task,
then projects that session's rows into the returned plan. The binding is persisted in Kynver,
so compression, gateway restarts and a fresh agent on the child session keep the same plan.

Lists grow into plans automatically (no link command): one-off lists (1–2 items) live in the
workspace Inbox; once a session's list reaches ``PROMOTE_MIN_ITEMS`` while it is on the Inbox,
the store asks Kynver to ``promote`` it — Kynver gives the session its own plan and moves the
rows already written there. Sessions bound to a task/context/explicit plan never move.

``KYNVER_PLAN_ID`` is a legacy fallback only: used when the binding endpoint does not exist
(an older Kynver deploy answers 404/405) or when ``KYNVER_TODO_PLAN_BINDING=off``. Transient
failures raise, so the todo store degrades to its local cache and retries — they never
silently redirect a session's rows to the legacy plan.
"""

from __future__ import annotations

import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional

from .agentos_bridge import KynverAgentOSClient, KynverAgentOSError
from .operating_config import OperatingLinkage

SESSION_KEY_PREFIX = "hermes:"
BINDING_PATH = "/todos/plan-binding"
_MISSING_ENDPOINT = re.compile(r"\bHTTP (404|405)\b")
_DEFAULT_TTL_SECONDS = 300.0
_MAX_CACHED_SCOPES = 512
# A list this long is planned work, not an Inbox one-off (Kynver TODO_PLAN_PROMOTE_MIN_ITEMS).
PROMOTE_MIN_ITEMS = 3
_PROMOTE_TITLE_CHARS = 120


def plan_binding_enabled(env: Mapping[str, str] | None = None) -> bool:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    raw = (merged.get("KYNVER_TODO_PLAN_BINDING") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def session_key_for_scope(scope: str) -> str:
    return f"{SESSION_KEY_PREFIX}{scope}"


@dataclass(frozen=True)
class ResolvedTodoPlan:
    plan_id: str
    title: str
    source: str  # explicit | context | task | inbox | promoted | legacy
    bound: bool

    @property
    def is_inbox(self) -> bool:
        return self.source == "inbox"


class TodoPlanResolver:
    """Resolve and cache a session scope's todo plan (TTL-bounded, LRU-capped)."""

    def __init__(
        self,
        client: KynverAgentOSClient,
        linkage: OperatingLinkage,
        *,
        enabled: bool = True,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._linkage = linkage
        self._enabled = enabled
        self._ttl = max(0.0, ttl_seconds)
        self._clock = clock
        self._cache: "OrderedDict[str, tuple[ResolvedTodoPlan, float]]" = OrderedDict()
        self._endpoint_missing_at: Optional[float] = None
        # scope -> when a promote request came back still on the Inbox (Kynver without
        # promote support); not retried within the TTL.
        self._promote_refused: "OrderedDict[str, float]" = OrderedDict()

    def legacy(self) -> Optional[ResolvedTodoPlan]:
        plan_id = self._linkage.plan_id
        if not plan_id:
            return None
        return ResolvedTodoPlan(plan_id=plan_id, title="", source="legacy", bound=False)

    def _endpoint_missing(self) -> bool:
        at = self._endpoint_missing_at
        return at is not None and self._clock() - at < self._ttl

    def _cached(self, scope: str) -> Optional[ResolvedTodoPlan]:
        hit = self._cache.get(scope)
        if not hit or self._clock() - hit[1] >= self._ttl:
            return None
        self._cache.move_to_end(scope)
        return hit[0]

    def _remember(self, scope: str, plan: ResolvedTodoPlan) -> ResolvedTodoPlan:
        self._cache[scope] = (plan, self._clock())
        self._cache.move_to_end(scope)
        while len(self._cache) > _MAX_CACHED_SCOPES:
            self._cache.popitem(last=False)
        return plan

    def _request(self, body: dict[str, Any]) -> ResolvedTodoPlan:
        payload = self._client.post(BINDING_PATH, body)
        plan_id = str((payload or {}).get("planId") or "").strip() if isinstance(payload, dict) else ""
        if not plan_id:
            raise KynverAgentOSError("Kynver todo plan binding returned no planId")
        return ResolvedTodoPlan(
            plan_id=plan_id,
            title=str(payload.get("planTitle") or ""),
            source=str(payload.get("source") or ""),
            bound=bool(payload.get("bound")),
        )

    def plan_for(self, scope: str) -> Optional[ResolvedTodoPlan]:
        """The plan for ``scope``; None only when nothing (not even legacy) is configured.

        Raises KynverAgentOSError on transient failures so callers degrade and retry."""

        if not self._enabled or not scope or self._endpoint_missing():
            return self.legacy()
        cached = self._cached(scope)
        if cached:
            return cached
        body: dict[str, Any] = {"sessionKey": session_key_for_scope(scope)}
        if self._linkage.task_id:
            body["hints"] = {"taskId": self._linkage.task_id}
        try:
            return self._remember(scope, self._request(body))
        except KynverAgentOSError as exc:
            if _MISSING_ENDPOINT.search(str(exc)):
                self._endpoint_missing_at = self._clock()
                return self.legacy()
            raise

    def promote(self, scope: str, *, title: str = "") -> Optional[ResolvedTodoPlan]:
        """Move an Inbox-bound ``scope`` onto its own plan (Kynver moves its rows there).

        Returns the new plan, or None when nothing changed: not on the Inbox, binding off,
        or a Kynver that does not promote (remembered for the TTL so writes do not keep
        asking). Raises KynverAgentOSError on transient failures."""

        current = self.plan_for(scope)
        if current is None or not current.is_inbox:
            return None
        refused_at = self._promote_refused.get(scope)
        if refused_at is not None and self._clock() - refused_at < self._ttl:
            return None
        body: dict[str, Any] = {
            "sessionKey": session_key_for_scope(scope),
            "promote": {"title": " ".join(title.split())[:_PROMOTE_TITLE_CHARS]},
        }
        if self._linkage.task_id:
            body["hints"] = {"taskId": self._linkage.task_id}
        plan = self._remember(scope, self._request(body))
        if plan.is_inbox:
            self._promote_refused[scope] = self._clock()
            while len(self._promote_refused) > _MAX_CACHED_SCOPES:
                self._promote_refused.popitem(last=False)
            return None
        return plan

    def linkage_for(self, scope: str) -> OperatingLinkage:
        """``linkage`` with ``plan_id`` set to the scope's plan (None → local only)."""

        plan = self.plan_for(scope)
        return replace(self._linkage, plan_id=plan.plan_id if plan else None)


_SHARED: "dict[tuple, TodoPlanResolver]" = {}


def shared_plan_resolver(client: KynverAgentOSClient, linkage: OperatingLinkage) -> TodoPlanResolver:
    """One resolver per (Kynver target, legacy linkage) per process, so the todo store and the
    pre_tool_call guard share a cache and agree on each session's plan."""

    cfg = client.config
    key = (cfg.api_url, cfg.slug, linkage.plan_id, linkage.task_id, plan_binding_enabled())
    resolver = _SHARED.get(key)
    if resolver is None:
        resolver = TodoPlanResolver(client, linkage, enabled=key[-1])
        _SHARED[key] = resolver
    return resolver
