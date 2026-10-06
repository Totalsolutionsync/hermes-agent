"""Vault page operations over the Browser Use harness daemon's IPC socket.

In Browser Use mode the ``browser_harness`` daemon (one per ``BU_NAME``) owns the ONE
CDP WebSocket to the browser; ``browser_exec`` reaches it through the CLI, which relays
each helper call to that daemon over a local socket. On a consent-gated browser (Chrome's
"Allow remote debugging?" prompt fires for every NEW WebSocket client) any second client
re-prompts the user, so vault operations speak the daemon's IPC protocol directly from
this process instead of dialing the browser: no new CDP client, no subprocess, nothing
left running. No daemon answering means no page access (fail closed) — starting one would
itself dial the browser.

Secret-bearing expressions travel only in the request payload over the owner-only socket
(AF_UNIX 0600 on POSIX; token-checked TCP loopback on Windows), never argv, and the
daemon's error text is never returned for them.

Protocol (browser_harness ``_ipc`` / ``daemon.handle``): one newline-terminated JSON
request per connection, one JSON line back. ``{"meta": "ping"}`` → ``{"pong": true}``;
``{"meta": "current_tab"}`` → ``{"targetId", "url"}``; ``{"method", "params",
"session_id"?}`` → ``{"result": <CDP result>}`` (an explicit ``session_id`` is honoured, so a
tab other than the daemon's own is reached without moving the daemon); failures →
``{"error": str}``. The daemon's ``set_session`` (what ``switch_tab`` does) is deliberately
never sent: the tab ``browser_exec`` is working in stays its tab.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

UNAVAILABLE_ERROR = (
    "Persistent browser connection not available: no Browser Use harness daemon is answering, "
    "and vault operations never open their own browser connection. Open the page with the "
    "browser first (that starts or reuses the harness), then retry."
)

_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_CONNECT_TIMEOUT_S = 2.0
_REQUEST_TIMEOUT_S = 10.0
_IS_WINDOWS = os.name == "nt"


class HarnessUnavailable(RuntimeError):
    """No harness daemon answers for the session's ``BU_NAME``."""


def _harness_env(session: str = "") -> Dict[str, str]:
    """The environment ``browser_exec`` hands the CLI for ``session`` (its ``BU_NAME``; "" = the
    default session): the socket path must resolve exactly as the CLI resolves it, or the vault
    would miss the daemon that session uses."""
    from tools.browser_use_cli import _base_subprocess_env

    env = _base_subprocess_env()
    if session:
        if not _NAME_RE.match(session):
            raise HarnessUnavailable(f"invalid session name {session!r}")
        env["BU_NAME"] = session
    return env


def _endpoint_paths(env: Dict[str, str]) -> Tuple[Path, Path]:
    """(unix socket, Windows port file) for ``env``'s daemon — browser_harness
    ``paths.runtime_dir`` + ``_ipc._runtime_stem``, evaluated against ``env``."""
    name = env.get("BU_NAME") or "default"
    if not _NAME_RE.match(name):
        raise HarnessUnavailable(f"invalid BU_NAME {name!r}")
    runtime = env.get("BH_RUNTIME_DIR") or env.get("BH_TMP_DIR")
    if runtime:
        base = Path(runtime).expanduser()
        stem = f"bu-{name}" if env.get("BH_RUNTIME_DIR_SHARED") == "1" else "bu"
    else:
        home = env.get("BH_HOME") or env.get("BROWSER_HARNESS_HOME")
        if home:
            root = Path(home).expanduser()
        elif env.get("XDG_CONFIG_HOME"):
            root = Path(env["XDG_CONFIG_HOME"]).expanduser() / "browser-harness"
        else:
            user_home = Path(env["HOME"]) if env.get("HOME") else Path.home()
            root = user_home / ".config" / "browser-harness"
        base, stem = root / "runtime", f"bu-{name}"
    return base / f"{stem}.sock", base / f"{stem}.port"


def _connect(env: Dict[str, str]) -> Tuple[socket.socket, Optional[str]]:
    sock_path, port_path = _endpoint_paths(env)
    try:
        if not _IS_WINDOWS:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(_CONNECT_TIMEOUT_S)
            try:
                conn.connect(str(sock_path))
            except OSError:
                conn.close()
                raise
            return conn, None
        record = json.loads(port_path.read_text(encoding="utf-8"))
        conn = socket.create_connection(("127.0.0.1", int(record["port"])), timeout=_CONNECT_TIMEOUT_S)
        return conn, str(record["token"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HarnessUnavailable(f"{type(exc).__name__} connecting to {sock_path if not _IS_WINDOWS else port_path}") from exc


def _request(env: Dict[str, str], req: Dict[str, Any], timeout: float = _REQUEST_TIMEOUT_S) -> Any:
    """One request/response round trip; raises HarnessUnavailable when nothing answers."""
    conn, token = _connect(env)
    try:
        if token:
            req = {**req, "token": token}
        conn.settimeout(timeout)
        conn.sendall((json.dumps(req) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(1 << 16)
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise HarnessUnavailable(f"{type(exc).__name__} talking to the harness daemon") from exc
    finally:
        conn.close()
    try:
        return json.loads(data or b"{}")
    except ValueError as exc:
        raise HarnessUnavailable("harness daemon sent a non-JSON reply") from exc


class HarnessPage:
    """A session's harness daemon, driven over its IPC socket. Pages are addressed by target id:
    ``None`` is the daemon's own current tab; any other tab is attached with an explicit,
    non-activating CDP session for one call and detached again (the harness's
    ``js(target_id=...)`` pattern), so the daemon's tab and the visible tab never move.

    Construction pings the daemon (``{"pong": true}``, so a stale socket or a reused
    Windows port is not mistaken for it) and raises HarnessUnavailable otherwise."""

    def __init__(self, session: str = "", env: Optional[Dict[str, str]] = None):
        self._env = _harness_env(session) if env is None else env
        pong = _request(self._env, {"meta": "ping"}, timeout=_CONNECT_TIMEOUT_S)
        if not (isinstance(pong, dict) and pong.get("pong") is True):
            raise HarnessUnavailable("the harness endpoint did not answer ping")

    def _call(self, req: Dict[str, Any]) -> Dict[str, Any]:
        resp = _request(self._env, req)
        if not isinstance(resp, dict):
            return {"error": "malformed harness reply"}
        return resp

    def _cdp(self, method: str, params: Optional[Dict[str, Any]] = None,
             session_id: Optional[str] = None) -> Dict[str, Any]:
        req: Dict[str, Any] = {"method": method, "params": params or {}}
        if session_id:
            req["session_id"] = session_id
        return self._call(req)

    def evaluate(self, expression: str, *, target_id: Optional[str] = None,
                 secret: bool = False) -> Dict[str, Any]:
        """``Runtime.evaluate`` on ``target_id`` (None = the daemon's current tab). Same shape as
        ``CDPSupervisor.evaluate_runtime``: ``{"ok", "result"}`` or ``{"ok": False, "error"}``.
        With ``secret`` the daemon's error text is withheld (it may quote the expression)."""
        if not target_id:
            return self._evaluate(expression, None, secret)
        sid = self._cdp("Target.attachToTarget", {"targetId": target_id, "flatten": True}
                        ).get("result", {}).get("sessionId")
        if not sid:
            return {"ok": False, "error": "the page's tab is no longer open"}
        try:
            return self._evaluate(expression, sid, secret)
        finally:
            # A daemon that died mid-call must not turn a completed write into a failure.
            with contextlib.suppress(HarnessUnavailable):
                self._cdp("Target.detachFromTarget", {"sessionId": sid})

    def _evaluate(self, expression: str, session_id: Optional[str], secret: bool) -> Dict[str, Any]:
        resp = self._cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                              "awaitPromise": True, "userGesture": True}, session_id)
        if "error" in resp:
            return {"ok": False, "error": "page evaluation failed" if secret else str(resp["error"])[:300]}
        payload = resp.get("result")
        if not isinstance(payload, dict):
            payload = {}
        details = payload.get("exceptionDetails")
        if details:
            if secret:
                return {"ok": False, "error": "page script raised an exception"}
            text = details.get("text") or "JavaScript exception"
            description = (details.get("exception") or {}).get("description")
            return {"ok": False, "error": f"{text}: {description}" if description else text}
        obj = payload.get("result") or {}
        if "value" in obj:
            value = obj["value"]
        elif obj.get("type", "undefined") == "undefined":
            value = None
        else:
            value = obj.get("description") or obj.get("unserializableValue")
        return {"ok": True, "result": value}

    def focus(self, origin: str, accept: Optional[str] = None) -> Dict[str, Any]:
        """Find the open tab on ``origin`` ("" = any http(s) page) where ``accept`` is truthy:
        the daemon's current tab first (``target_id`` None), else another page target by id.
        Nothing is switched or activated. Returns ``{"ok", "url", "target_id"}`` or
        ``{"ok": False, "error"}``."""
        from agent.vault_store import normalize_origin

        def _matches(url: str) -> bool:
            if not url.startswith(("http://", "https://")):
                return False
            try:
                return not origin or normalize_origin(url) == origin
            except Exception:
                return False

        def _accepted(target_id: Optional[str]) -> bool:
            return not accept or bool(self.evaluate(accept, target_id=target_id).get("result"))

        current = self._call({"meta": "current_tab"})
        current_id, current_url = current.get("targetId"), str(current.get("url") or "")
        if "error" not in current and _matches(current_url) and _accepted(None):
            return {"ok": True, "url": current_url, "target_id": None}

        targets = self._cdp("Target.getTargets").get("result", {}).get("targetInfos", [])
        candidates = [(t["targetId"], str(t.get("url") or "")) for t in targets
                      if t.get("type") == "page" and t.get("targetId") and t.get("targetId") != current_id
                      and _matches(str(t.get("url") or ""))]
        for target_id, url in candidates:
            if _accepted(target_id):
                return {"ok": True, "url": url, "target_id": target_id}
        return {"ok": False, "error": f"no open page on {origin or 'any site'}"
                + (" with the expected form" if accept and candidates else "")}
