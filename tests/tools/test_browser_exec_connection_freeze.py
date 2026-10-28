"""browser.connection_freeze stops browser_exec before it discovers the CLI or resolves a route."""
import json

import tools.browser_use_cli as bu_cli


def _fail(*_a, **_k):
    raise AssertionError("browser transport touched while browser.connection_freeze is active")


def test_frozen_browser_exec_opens_no_transport(monkeypatch):
    monkeypatch.setattr(bu_cli, "_read_browser_cfg", lambda: {"connection_freeze": True})
    monkeypatch.setattr(bu_cli, "_find_cli", _fail)
    monkeypatch.setattr(bu_cli, "_route_backend", _fail)

    result = json.loads(bu_cli.browser_exec('new_tab("https://example.com")'))

    assert "freeze" in result["error"].lower()


def test_unfrozen_browser_exec_reaches_cli_discovery(monkeypatch):
    monkeypatch.setattr(bu_cli, "_read_browser_cfg", lambda: {"connection_freeze": False})
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)

    result = json.loads(bu_cli.browser_exec('print(1)'))

    assert "freeze" not in result.get("error", "").lower()
