"""Unit tests for _SupervisorRegistry cache-hit healthcheck.

Verifies that get_or_start() does NOT return a cached supervisor whose
thread has exited or whose event loop has stopped. Avoids a real Chrome —
the only thing under test is the registry's cache decision.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tools import browser_supervisor as bs


class _FakeLoop:
    def __init__(self, running: bool) -> None:
        self._running = running

    def is_running(self) -> bool:
        return self._running


def _make_fake_supervisor(cdp_url: str, *, thread_alive: bool, loop_running: bool):
    """Build a minimal stand-in for a CDPSupervisor entry in the registry.

    Only the attributes touched by the healthcheck (_thread, _loop, cdp_url)
    and by the teardown path (stop()) need to exist.
    """

    if thread_alive:
        # A thread that is actually running — parks on an Event we never set.
        hold = threading.Event()
        t = threading.Thread(target=hold.wait, daemon=True)
        t.start()
        # Attach the release hook so the test can let the thread exit.
        setattr(t, "_release", hold.set)
    else:
        # An un-started thread — is_alive() returns False.
        t = threading.Thread(target=lambda: None)

    stop_calls: list[bool] = []

    fake = SimpleNamespace(
        cdp_url=cdp_url,
        _thread=t,
        _loop=_FakeLoop(loop_running),
        stop=lambda: stop_calls.append(True),
    )
    fake._stop_calls = stop_calls  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def isolated_registry():
    """A fresh registry instance, independent of the global SUPERVISOR_REGISTRY."""
    return bs._SupervisorRegistry()


@pytest.fixture
def stub_cdp_supervisor(monkeypatch):
    """Replace CDPSupervisor in the module so recreate paths don't touch Chrome.

    Returns a callable that reads the last-constructed fake out.
    """
    created: list[SimpleNamespace] = []

    class _StubSupervisor:
        def __init__(self, *, task_id, cdp_url, dialog_policy, dialog_timeout_s, reconnect_on_drop):
            self.task_id = task_id
            self.cdp_url = cdp_url
            self.dialog_policy = dialog_policy
            self.dialog_timeout_s = dialog_timeout_s
            self.reconnect_on_drop = reconnect_on_drop
            # Healthy by default — real thread, running "loop".
            hold = threading.Event()
            self._thread = threading.Thread(target=hold.wait, daemon=True)
            self._thread.start()
            self._thread_release = hold.set  # type: ignore[attr-defined]
            self._loop = _FakeLoop(True)
            self.start_called = False
            self.stop_called = False
            created.append(self)

        def start(self, timeout: float = 15.0) -> None:
            self.start_called = True

        def stop(self) -> None:
            self.stop_called = True
            # Release the parked thread so the process exits cleanly.
            release = getattr(self, "_thread_release", None)
            if release is not None:
                release()

    monkeypatch.setattr(bs, "CDPSupervisor", _StubSupervisor)
    yield created
    # Teardown: release any parked threads in stubs the test left behind.
    for s in created:
        release = getattr(s, "_thread_release", None)
        if release is not None:
            release()


def test_cache_hit_returns_same_instance_when_healthy(
    isolated_registry, stub_cdp_supervisor
):
    """A repeated Browser Use bind preserves the one healthy lazy supervisor."""
    endpoint = "http://h/1"
    isolated_registry.set_lazy_endpoint("t1", endpoint)
    first = isolated_registry.get_or_start(task_id="t1", cdp_url=endpoint)
    isolated_registry.set_lazy_endpoint("t1", endpoint)
    second = isolated_registry.get_or_start(task_id="t1", cdp_url=endpoint)
    assert first is second
    assert first.stop_called is False
    # Only one CDPSupervisor was ever constructed.
    assert len(stub_cdp_supervisor) == 1
    first.stop()


def test_inflight_lazy_start_cannot_survive_route_rebind(
    isolated_registry, stub_cdp_supervisor, monkeypatch
):
    """A route change during consent fails closed before the old browser enters the registry."""
    old = "ws://h/old"
    new = "ws://h/new"
    isolated_registry.set_lazy_endpoint("race", old)
    bound, endpoint, token = isolated_registry.get_lazy_binding("race")
    assert bound and endpoint == old and token is not None

    entered_start = threading.Event()
    release_start = threading.Event()
    original_start = bs.CDPSupervisor.start

    def blocking_start(supervisor, timeout=15.0):
        entered_start.set()
        assert release_start.wait(timeout=2.0)
        original_start(supervisor, timeout=timeout)

    monkeypatch.setattr(bs.CDPSupervisor, "start", blocking_start)
    errors = []

    def start_old_route():
        try:
            isolated_registry.get_or_start(
                task_id="race",
                cdp_url=old,
                reconnect_on_drop=False,
                lazy_binding_token=token,
            )
        except RuntimeError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=start_old_route)
    worker.start()
    assert entered_start.wait(timeout=2.0)
    isolated_registry.set_lazy_endpoint("race", new)
    release_start.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert errors == ["Browser route changed while the lazy supervisor was connecting"]
    assert isolated_registry.get("race") is None
    assert len(stub_cdp_supervisor) == 1
    assert stub_cdp_supervisor[0].stop_called is True


def test_secret_eval_refuses_a_rebound_route(isolated_registry):
    """Even an old supervisor reference cannot write after its Browser Use route changed."""
    events = []
    supervisor = SimpleNamespace(
        evaluate_runtime=lambda expression: events.append(expression) or {"ok": True},
        stop=lambda: events.append("stop"),
    )
    isolated_registry.set_lazy_endpoint("write-race", "ws://h/old")
    _bound, _endpoint, token = isolated_registry.get_lazy_binding("write-race")
    isolated_registry._by_task["write-race"] = supervisor
    isolated_registry.set_lazy_endpoint("write-race", "ws://h/new")

    result = isolated_registry.evaluate_runtime_if_current(
        "write-race", supervisor, token, "fill_secret()"
    )

    assert result == {"ok": False, "error": "supervisor route changed before secret evaluation"}
    assert events == ["stop"]


def test_missing_thread_and_loop_attrs_trigger_recreate(
    isolated_registry, stub_cdp_supervisor
):
    """Defensive: None _thread or None _loop counts as unhealthy."""
    cdp_url = "http://h/4"
    broken = SimpleNamespace(
        cdp_url=cdp_url,
        _thread=None,
        _loop=None,
        stop=lambda: None,
    )
    isolated_registry._by_task["t4"] = broken

    fresh = isolated_registry.get_or_start(task_id="t4", cdp_url=cdp_url)
    assert fresh is not broken
    assert isolated_registry._by_task["t4"] is fresh
    fresh.stop()
