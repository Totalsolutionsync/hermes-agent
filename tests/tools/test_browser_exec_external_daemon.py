"""browser.external_daemons: browser_exec attaches to an operator-owned harness daemon (e.g. the
chrome-harness.service user unit) and never spawns, reloads or stops a daemon under that name."""
import json

import pytest

import tools.browser_use_cli as bu_cli
from tools import browser_session as bs


@pytest.fixture(autouse=True)
def _routing(monkeypatch, tmp_path):
    monkeypatch.delenv("BU_NAME", raising=False)
    monkeypatch.delenv("BH_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("BH_TMP_DIR", raising=False)
    monkeypatch.delenv("BH_REQUIRE_EXISTING_DAEMON", raising=False)
    monkeypatch.setattr(bs, "build_browser_env", lambda: {"PATH": "/usr/bin:/bin"})
    monkeypatch.setattr(bu_cli, "_harness_credentials", dict)
    monkeypatch.setattr(bu_cli, "_served_profile_tag", lambda: "")
    monkeypatch.setattr(bu_cli, "_route_exec_current",
                        lambda env, *a, **k: ({**env, "BU_CDP_WS": "ws://127.0.0.1:9333/x"}, None, False, 0))
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda env, task_id: None)
    monkeypatch.setattr(bu_cli, "_driven_daemons", {})
    monkeypatch.setattr(bu_cli, "_exec_route_is_sidecar", {})
    cli = tmp_path / "browser-harness"
    cli.write_text('#!/bin/sh\ncat >/dev/null; printf "%s|%s" "${BU_NAME:-default}" "${BH_REQUIRE_EXISTING_DAEMON:-0}"\n')
    cli.chmod(0o755)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(cli)])
    monkeypatch.setattr("tools.browser_use_shot.harness_command", lambda cmd, activate=None: cmd)


def _cfg(monkeypatch, **cfg):
    monkeypatch.setattr(bu_cli, "_read_browser_cfg", lambda: cfg)


def test_listed_default_daemon_is_attach_only_and_never_tracked(monkeypatch):
    _cfg(monkeypatch, external_daemons=["default"])
    result = json.loads(bu_cli.browser_exec("print(1)"))
    assert result["output"] == "default|1"
    assert bu_cli._driven_daemons == {}  # shutdown/atexit/--reload can never stop it


def test_named_sessions_still_own_their_daemons(monkeypatch):
    _cfg(monkeypatch, external_daemons=["default"])
    result = json.loads(bu_cli.browser_exec("print(1)", session="w1"))
    assert result["output"] == "w1|0"
    assert len(bu_cli._driven_daemons) == 1


def test_unlisted_default_keeps_upstream_behaviour(monkeypatch):
    _cfg(monkeypatch)
    result = json.loads(bu_cli.browser_exec("print(1)"))
    assert result["output"] == "default|0"
    assert len(bu_cli._driven_daemons) == 1


def test_namespaced_runtime_dir_is_not_the_external_daemon(monkeypatch):
    _cfg(monkeypatch, external_daemons="default")
    assert bu_cli._is_external_daemon({"BU_NAME": "default"}) is True
    assert bu_cli._is_external_daemon({"BU_NAME": "default", "BH_RUNTIME_DIR": "/x/p-abc"}) is False


def test_republish_never_reloads_external_daemon(monkeypatch):
    _cfg(monkeypatch, external_daemons=["default"])
    monkeypatch.setattr(bu_cli, "_live_generation", lambda: 2)
    reloaded = []
    monkeypatch.setattr(bu_cli, "_reload_daemons", reloaded.extend)
    bu_cli._reload_if_republished({"BU_NAME": "default"}, 1)
    assert reloaded == []
    bu_cli._reload_if_republished({"BU_NAME": "other"}, 1)
    assert len(reloaded) == 1
