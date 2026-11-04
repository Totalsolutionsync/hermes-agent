"""A full background pool QUEUES the child (registry-owned, never run in the caller); refusals are wound down."""

from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.turn_context import _bind_interrupt_scope
from tools import async_delegation
from tools.delegate_tool_dispatch import _Batch, _dispatch_background
from tools.interrupt import is_interrupted, set_interrupt
from tools.process_registry import process_registry


class _Parent(InterruptControlMixin):
    def __init__(self):
        self.session_id = "capacity-interrupt-parent"
        self._active_children = []
        self._active_children_lock = threading.Lock()
        self._execution_thread_id = None
        self._interrupt_requested = False
        self._hard_interrupt_requested = threading.Event()
        self.quiet_mode = True


class _ControlledChild(_Parent):
    """Replace model work while retaining real interrupt and worker-start semantics."""

    def __init__(self):
        super().__init__()
        self.session_id = "capacity-interrupt-child"
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._delegate_saved_tool_names = []
        self._credential_pool = None
        self._subagent_id = None
        self.tool_progress_callback = None
        self.model = "test-model"
        self.started = threading.Event()
        self.stop_received = threading.Event()
        self.unwinding = threading.Event()
        self.allow_finish = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.close_count = 0
        self.closed_while_running = False
        self.observed_interrupt = None

    def interrupt(self, message=None, **kwargs):
        accepted = super().interrupt(message, **kwargs)
        self.stop_received.set()
        return accepted

    def hard_interrupt(self, message=None, **kwargs):
        super().hard_interrupt(message, **kwargs)
        self.stop_received.set()

    def run_conversation(self, **_kwargs):
        # A stop can arrive before this thread exists. Use the real turn-start
        # binding so the pending agent interrupt must reach the tool thread too.
        _bind_interrupt_scope(self, lambda: SimpleNamespace(_set_interrupt=set_interrupt))
        self.started.set()
        try:
            assert self.stop_received.wait(30), "child never received cancellation"
            assert self._interrupt_requested
            assert is_interrupted(), "stop did not reach the child execution thread"
            self.observed_interrupt = (
                self._interrupt_message, self._hard_interrupt_requested.is_set(),
            )
            self.unwinding.set()
            assert self.allow_finish.wait(30), "test did not release child cleanup"
            return {
                "final_response": "", "completed": False, "interrupted": True,
                "api_calls": 0, "messages": [],
            }
        finally:
            self.clear_interrupt()
            self.finished.set()

    def get_activity_summary(self):
        return {"api_call_count": 0}

    def close(self):
        self.closed_while_running |= not self.finished.is_set()
        self.close_count += 1
        self.closed.set()


def _batch(parent, *children):
    tasks = [{"goal": f"wait until cancelled {i}"} for i in range(len(children))]
    parent._active_children.extend(children)
    return _Batch(
        task_list=tasks, children=[(i, tasks[i], child) for i, child in enumerate(children)], parent_agent=parent,
        creds={"model": children[0].model}, context=None, top_role="leaf", max_children=len(children),
        live_deleg_id=None, live_writers=[], live_paths=[], origin_wake_sid="",
        origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False, overall_start=time.monotonic(),
    )


@pytest.fixture
def registry_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (tmp_path / "config.yaml").write_text(
        "delegation:\n  max_concurrent_children: 1\n  worktree_isolation: false\n",
        encoding="utf-8",
    )
    async_delegation._reset_for_tests()
    completion_queue = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", completion_queue)
    yield completion_queue
    # Test bodies release their gates and join their workers before registry teardown.
    if async_delegation._executor is not None:
        async_delegation._executor.shutdown(wait=True)
    async_delegation._reset_for_tests()


def _occupy_only_slot(child_model):
    occupied, release = threading.Event(), threading.Event()

    def occupy_slot():
        occupied.set()
        assert release.wait(30)
        return {"status": "completed", "summary": "slot released"}

    accepted = async_delegation.dispatch_async_delegation(
        goal="occupy the only slot", context=None, toolsets=None, role="leaf",
        model=child_model, session_key="other-session", runner=occupy_slot, max_async_children=1,
    )
    assert accepted["status"] == "dispatched" and not accepted.get("queued")
    assert occupied.wait(5)
    return accepted, release


def _close_out(child):
    if not child.finished.is_set():
        child.hard_interrupt("test teardown")
    child.allow_finish.set()
    assert child.closed.wait(60)
    assert child.close_count == 1
    assert not child.closed_while_running


def test_full_pool_queues_the_child_and_returns_immediately(registry_state):
    """The regression: a full pool used to run the child SYNCHRONOUSLY inside the parent's turn."""
    parent, child = _Parent(), _ControlledChild()
    occupier, release_occupier = _occupy_only_slot(child.model)
    try:
        t0 = time.monotonic()
        result = json.loads(_dispatch_background(_batch(parent, child)))
        assert time.monotonic() - t0 < 5
        assert result["status"] == "dispatched" and result["queued"] is True and result["queue_position"] == 1
        assert "SYNCHRONOUSLY" not in result["note"] and "results" not in result
        assert not child.started.wait(0.3), "a queued child must not run while the pool is full"
        # Queued work is registry-owned like any accepted background child: the parent's stop does not reach it.
        parent.interrupt("parent turn ends")
        assert not child.stop_received.is_set()
        assert parent._active_children == []

        release_occupier.set()  # the slot frees -> the queued child starts on its own
        assert registry_state.get(timeout=5)["delegation_id"] == occupier["delegation_id"]
        assert child.started.wait(5)
        assert async_delegation.interrupt_for_session(parent_session_id=parent.session_id) == 1
        assert child.unwinding.wait(5)
        child.allow_finish.set()
        completion = registry_state.get(timeout=60)  # first child cleanup imports model_tools (slow when loaded)
        assert completion["delegation_id"] == result["delegation_id"]
        assert completion["results"][0]["status"] == "interrupted"
    finally:
        release_occupier.set()
        _close_out(child)


def test_stopping_a_queued_child_unwinds_it_without_waiting_for_a_slot(registry_state):
    parent, child = _Parent(), _ControlledChild()
    occupier, release_occupier = _occupy_only_slot(child.model)
    try:
        result = json.loads(_dispatch_background(_batch(parent, child)))
        assert result["queued"] is True
        child.allow_finish.set()
        assert async_delegation.interrupt_for_session(parent_session_id=parent.session_id) == 1
        completion = registry_state.get(timeout=60)  # delivered while the occupier still holds the slot
        assert completion["delegation_id"] == result["delegation_id"]
        assert completion["results"][0]["status"] == "interrupted"
        assert child.unwinding.is_set()
        assert async_delegation.active_task_count() == 1  # the occupier kept running
    finally:
        release_occupier.set()
        _close_out(child)
    assert registry_state.get(timeout=5)["delegation_id"] == occupier["delegation_id"]


@pytest.mark.parametrize("rejection", ["schedule_failure", "partial_schedule_failure"])
def test_refused_unit_is_wound_down_not_run_inline(registry_state, monkeypatch, tmp_path, rejection):
    """A unit the registry refuses (scheduler failure / queue overflow) is stopped before it does any work and
    reported as not_started — never executed synchronously in the caller's turn."""
    parent, child = _Parent(), _ControlledChild()
    child.allow_finish.set()  # its refusal unwind must not hang on the test gate
    background_child = None
    if rejection == "partial_schedule_failure":
        (tmp_path / "config.yaml").write_text(
            "delegation:\n  max_concurrent_children: 3\n  worktree_isolation: false\n"
            "  independent_completions: true\n", encoding="utf-8")
        background_child = _ControlledChild()
        background_child.session_id += "-background"
        batch = _batch(parent, background_child, child)
        executor = async_delegation._get_executor(3)

        class PartiallyRejectingExecutor:
            submitted = 0

            def submit(self, *args, **kwargs):
                self.submitted += 1
                if self.submitted == 2:
                    raise RuntimeError("unit submission failed")
                return executor.submit(*args, **kwargs)

        partial = PartiallyRejectingExecutor()
        monkeypatch.setattr(async_delegation, "_get_executor", lambda _n: partial)
    else:
        class RejectingExecutor:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("executor shut down")

        monkeypatch.setattr(async_delegation, "_get_executor", lambda _n: RejectingExecutor())
        batch = _batch(parent, child)
    try:
        result = json.loads(_dispatch_background(batch))
        assert "SYNCHRONOUSLY" not in json.dumps(result)
        refused = result["results"] if background_child is None else result["not_started"]
        assert result["status"] == ("rejected" if background_child is None else "dispatched")
        assert [r["status"] for r in refused] == ["not_started"]
        assert child.finished.is_set() and child.close_count == 1
        assert child.observed_interrupt[0].startswith("Not started")  # stopped before doing any work
        if background_child is not None:
            assert background_child.started.wait(5)
            assert not background_child.stop_received.is_set()
            assert async_delegation.interrupt_for_session(parent_session_id=parent.session_id) == 1
            background_child.allow_finish.set()
            assert registry_state.get(timeout=5)["delegation_id"] == result["delegation_id"]
    finally:
        _close_out(child)
        if background_child is not None:
            _close_out(background_child)


def test_accepted_background_child_keeps_registry_cancellation_ownership(registry_state):
    parent, child = _Parent(), _ControlledChild()
    try:
        result = json.loads(_dispatch_background(_batch(parent, child)))
        assert result["status"] == "dispatched"
        assert child.started.wait(5)
        parent.interrupt()
        # Parent interrupt fan-out is synchronous; observing it return establishes
        # that a detached child did not receive it without a timing-based wait.
        assert parent._interrupt_requested
        assert not child.stop_received.is_set()
        assert not child.finished.is_set()
        parent.hard_interrupt("stop the current parent turn")
        assert parent._hard_interrupt_requested.is_set()
        assert not child.stop_received.is_set()
        assert async_delegation.interrupt_for_session(parent_session_id=parent.session_id) == 1
        assert child.unwinding.wait(5)
        assert child.observed_interrupt[1] is True
        assert child.close_count == 0
        child.allow_finish.set()
        completion = registry_state.get(timeout=5)
        assert completion["delegation_id"] == result["delegation_id"]
        assert completion["results"][0]["status"] == "interrupted"
        assert child.finished.is_set()
        assert child.close_count == 1
        assert not child.closed_while_running
        assert parent._active_children == []
    finally:
        if not child.finished.is_set():
            child.hard_interrupt("test teardown")
        child.allow_finish.set()
        assert child.closed.wait(5)
