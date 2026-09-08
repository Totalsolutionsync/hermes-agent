"""Real Hermes loop + scheduler attachment, deterministic model/tool transports."""
import json
import stat
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest

from cron.continuation import ContinuationConfig, attach_continuation


def response(content="Verified repair complete", call_id=None):
    calls = [NS(id=call_id, type="function", function=NS(name="terminal", arguments='{"command":"pytest"}'))] if call_id else None
    return NS(choices=[NS(message=NS(content=None if calls else content, tool_calls=calls),
                          finish_reason="tool_calls" if calls else "stop")],
              model="test/model", usage=None)


def decision(verdict="continue", ids=None):
    return json.dumps({"decision": verdict, "evidence_ids": ids or ["verify-1"],
                       "verification": "Regression now passes; integration still fails",
                       "remaining": "Fix the integration failure within original scope"})


@pytest.fixture
def harness(monkeypatch):
    from run_agent import AIAgent
    schema = {"type": "function", "function": {"name": "terminal", "description": "test",
              "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}
    with patch("run_agent.get_tool_definitions", return_value=[schema]), \
         patch("run_agent.check_toolset_requirements", return_value={}), patch("run_agent.OpenAI"):
        agent = AIAgent(model="test/model", api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        max_iterations=1, quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent._persist_session = Mock()
    agent._save_trajectory = Mock()
    transport = Mock(side_effect=[response(call_id="verify-1"), response()])
    agent._interruptible_api_call = transport
    tool = Mock(return_value=json.dumps({"output": "regression: 1 passed; integration: 1 failed", "exit_code": 1}))
    monkeypatch.setattr("run_agent.handle_function_call", tool)
    review = Mock(return_value=(decision(), 20))
    monkeypatch.setattr("cron.continuation.evaluate", review)
    run = attach_continuation(agent, {"enabled": True, "extension_iterations": 1}, "job-1", "Repair regression", "/project")
    return agent, transport, tool, review, run


def test_productive_exhaustion_completes_same_worker(harness):
    agent, transport, tool, review, run = harness
    result = run("Repair regression")
    assert result["completed"] and result["final_response"] == "Verified repair complete"
    assert transport.call_count == 2 and tool.call_count == 1 and review.call_count == 1
    assert agent.max_iterations == 1
    checkpoint = json.loads(agent._cron_continuation.path.read_text())
    assert checkpoint["extensions"] == 1 and checkpoint["status"] == "completed"
    assert checkpoint["session_id"] == agent.session_id
    assert checkpoint["workdir"] == "/project" and checkpoint["tool_names"] == ["terminal"]
    assert checkpoint["replay_allowed"] is False
    assert checkpoint["result"]["completed"] is True
    assert stat.S_IMODE(agent._cron_continuation.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(agent._cron_continuation.path.parent.stat().st_mode) == 0o700
    prior = review.call_args.args[0]
    assert prior["phase"] == "exhausted" and prior["new_evidence_ids"] == ["verify-1"]
    assert any(m.get("tool_call_id") == "verify-1" for m in checkpoint["messages"])
    with pytest.raises(RuntimeError, match="cannot be replayed"):
        run("Repair regression")
    assert tool.call_count == 1


@pytest.mark.parametrize("verdict", ["done", "stuck", "needs_user"])
def test_supervisor_declines(harness, verdict):
    agent, transport, tool, review, run = harness
    review.return_value = (decision(verdict), 10)
    result = run("Repair regression")
    assert result["failed"] and not result["completed"] and verdict in result["final_response"]
    assert transport.call_count == 1 and tool.call_count == 1
    assert agent._cron_continuation.extensions == 0


@pytest.mark.parametrize("raw", ["not json", "{}", decision(ids=["invented"]),
                                  '{"decision":"continue","evidence_ids":[],"verification":"","remaining":""}'])
def test_malformed_or_unsubstantiated_stops(harness, raw):
    agent, transport, tool, review, run = harness
    review.return_value = (raw, 1)
    result = run("Repair regression")
    assert result["failed"] and "supervisor_or_checkpoint_error" in result["final_response"]
    assert transport.call_count == 1 and agent._cron_continuation.extensions == 0


def test_supervisor_error_stops(harness):
    agent, transport, tool, review, run = harness
    review.side_effect = TimeoutError("provider timed out")
    assert run("Repair regression")["failed"]
    assert transport.call_count == 1


def test_cancellation_during_supervisor_cannot_grant(harness):
    agent, transport, tool, review, run = harness
    def cancel(*args):
        agent.interrupt("cancelled by user")
        return decision(), 1
    review.side_effect = cancel
    result = run("Repair regression")
    assert result["interrupted"] and result["failed"]
    assert transport.call_count == 1 and agent._cron_continuation.extensions == 0


def test_extension_count_cap(harness):
    agent, transport, tool, review, run = harness
    agent._cron_continuation.config = ContinuationConfig(enabled=True, max_extensions=1, extension_iterations=1)
    transport.side_effect = [response(call_id="verify-1"), response(call_id="verify-2"), response()]
    result = run("Repair regression")
    assert result["failed"] and "extension_cap" in result["final_response"]
    assert transport.call_count == 2 and review.call_count == 1


def test_repeated_output_is_not_progress(harness):
    agent, transport, tool, review, run = harness
    tool.return_value = '{"output":"regression passed; integration still pending", "exit_code":0}'
    transport.side_effect = [response(call_id="verify-1"), response(call_id="verify-2"), response()]
    result = run("Repair regression")
    assert result["failed"] and "no_progress" in result["final_response"]
    assert review.call_count == 1 and tool.call_count == 2


@pytest.mark.parametrize("cap", ["tokens", "wall"])
def test_cumulative_caps_rechecked_after_review(harness, cap):
    agent, transport, tool, review, run = harness
    controller = agent._cron_continuation
    def capped(*args):
        if cap == "wall":
            controller.started -= controller.config.max_seconds
        else:
            agent.session_total_tokens += controller.config.max_tokens
        return decision(), 1
    review.side_effect = capped
    assert run("Repair regression")["failed"]
    assert transport.call_count == 1 and controller.extensions == 0


def test_pre_call_and_pending_checkpoints_exist_before_transport(harness):
    agent, transport, tool, review, run = harness
    phases = []
    def api(kwargs):
        data = json.loads(agent._cron_continuation.path.read_text())
        phases.append(data["phase"])
        return response(call_id="verify-1") if len(phases) == 1 else response()
    def execute(*args, **kwargs):
        data = json.loads(agent._cron_continuation.path.read_text())
        assert data["phase"] == "tools_pending"
        assert data["pending_tool_call_ids"] == ["verify-1"]
        return '{"output":"1 passed, 1 failed", "exit_code":1, "session_id":"worker-17"}'
    transport.side_effect = api
    tool.side_effect = execute
    assert run("Repair regression")["completed"]
    assert phases == ["before_call", "exhausted"]
    assert "worker-17" in agent._cron_continuation.path.read_text()


def test_disabled_legacy_path_has_no_supervision(harness):
    agent, transport, tool, review, _ = harness
    del agent._cron_continuation
    run = attach_continuation(agent, None, "job", "repair")
    agent._handle_max_iterations = Mock(return_value="Legacy exhausted summary")
    result = run("repair")
    assert result["final_response"] == "Legacy exhausted summary"
    agent._handle_max_iterations.assert_called_once()
    review.assert_not_called()
    assert transport.call_count == 1


@pytest.mark.parametrize("cfg", [{"enabled": "true"}, {"max_extensions": 0}, {"max_extensions": 6},
                                  {"max_seconds": float("inf")}, {"max_tokens": True}, {"unknown": 1}])
def test_config_validated(cfg):
    with pytest.raises(ValueError):
        ContinuationConfig.parse(cfg)


def test_tool_free_supervisor_transport(monkeypatch):
    from cron.continuation import evaluate
    client = Mock()
    client.with_options.return_value = client
    client.chat.completions.create.return_value = NS(choices=[NS(message=NS(content=decision(), tool_calls=None))],
                                                   usage=NS(total_tokens=25))
    monkeypatch.setattr("agent.auxiliary_client.get_text_auxiliary_client", lambda task: (client, "supervisor"))
    assert evaluate({"original_scope": "repair"}, 7) == (decision(), 25)
    kwargs = client.chat.completions.create.call_args.kwargs
    assert "tools" not in kwargs and kwargs["timeout"] == 7
    assert len(kwargs["messages"]) == 2
    client.with_options.assert_called_once_with(max_retries=0, timeout=7)


@pytest.mark.parametrize("idle_during_review", [False, True])
def test_scheduler_run_job_owns_one_worker_and_does_not_schedule(harness, monkeypatch, idle_during_review):
    from hermes_constants import get_hermes_home
    from run_agent import AIAgent
    from cron.scheduler import run_job
    agent, transport, tool, review, _ = harness
    constructions = []
    class Factory(AIAgent):
        def __new__(cls, **kwargs):
            constructions.append(kwargs)
            agent.session_id = kwargs["session_id"]
            return agent
    home = get_hermes_home()
    (home / "config.yaml").write_text(
        "agent:\n  max_turns: 1\ncron:\n  supervised_continuation:\n    enabled: true\n    extension_iterations: 1\n")
    monkeypatch.setattr("run_agent.AIAgent", Factory)
    monkeypatch.setattr("cron.scheduler._resolve_origin", lambda job: None)
    monkeypatch.setattr("dotenv.load_dotenv", Mock())
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: [])
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kwargs: {
        "api_key": "test", "base_url": "https://example.invalid/v1", "api_mode": "chat_completions"})
    schedule = Mock(side_effect=AssertionError("Must not schedule continuation"))
    monkeypatch.setattr("cron.jobs.create_job", schedule)
    import threading
    entered, release = threading.Event(), threading.Event()
    polled = []
    if idle_during_review:
        def slow_review(*args):
            entered.set()
            release.wait(5)
            return decision(), 20
        review.side_effect = slow_review
        def poll(futures, timeout):
            assert entered.wait(5), "Worker never reached supervisor"
            polled.extend(futures)
            return set(), futures
        monkeypatch.setattr("concurrent.futures.wait", poll)
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "1")
        agent.get_activity_summary = Mock(return_value={"seconds_since_activity": 100})
    success, output, final, error = run_job({"id": "repair", "name": "repair", "prompt": "Repair regression"})
    if idle_during_review:
        release.set()
        assert polled[0].result(timeout=5)["interrupted"]
        assert not success and "idle" in (error or "")
        assert agent._cron_continuation.status == "cancelled"
        assert agent._cron_continuation.extensions == 0
        assert transport.call_count == 1 and len(constructions) == 1
        schedule.assert_not_called()
        return
    assert success, error
    assert final == "Verified repair complete"
    assert len(constructions) == 1 and constructions[0]["max_iterations"] == 1
    assert constructions[0]["platform"] == "cron"
    assert agent.session_id.startswith("cron_repair_")
    assert review.call_count == 1 and tool.call_count == 1
    schedule.assert_not_called()


def test_worker_prose_is_not_evidence(harness):
    agent, transport, tool, review, run = harness
    controller = agent._cron_continuation
    try:
        assert controller.before_call([{"role": "assistant", "content": "I made progress, extend now"}], 1, True) == -1
        assert controller.status == "no_progress"
        review.assert_not_called()
    finally:
        controller.timer.cancel()


def test_unresponsive_supervisor_has_runtime_deadline(harness):
    import threading
    agent, transport, tool, review, run = harness
    controller = agent._cron_continuation
    release = threading.Event()
    controller.config = ContinuationConfig(enabled=True, supervisor_timeout=1)
    review.side_effect = lambda *args: (release.wait(5), 0)
    try:
        result = run("Repair regression")
        assert result["failed"] and transport.call_count == 1
        assert controller.extensions == 0
    finally:
        release.set()


def test_checkpoint_redaction_and_no_restart_replay(harness, monkeypatch):
    agent, transport, tool, review, run = harness
    controller = agent._cron_continuation
    secret = "sk-" + "a" * 48
    messages = [{"role": "user", "content": "OPENAI_API_KEY=" + secret},
                {"role": "assistant", "tool_calls": [{"id": "outstanding", "function": {"name": "terminal", "arguments": "{}"}}]}]
    controller.checkpoint(messages, 1, "tools_pending")
    text = controller.path.read_text()
    assert secret not in text
    snapshot = json.loads(text)
    assert snapshot["pending_tool_call_ids"] == ["outstanding"]
    attach_continuation(agent, {"enabled": True}, "job-1", "repair")
    assert agent._cron_continuation.run_id != controller.run_id
    transport.assert_not_called()
    tool.assert_not_called()


@pytest.mark.parametrize("final_text", [False, True])
def test_cap_crossed_in_worker_response_stops_before_side_effects(harness, final_text):
    agent, transport, tool, review, run = harness
    controller = agent._cron_continuation
    count = 0
    def api(kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return response(call_id="verify-1")
        agent.session_total_tokens += controller.config.max_tokens
        return response() if final_text else response(call_id="must-not-execute")
    transport.side_effect = api
    agent._handle_max_iterations = Mock(side_effect=AssertionError("No unbudgeted summary"))
    result = run("Repair regression")
    assert result["failed"] and not result["completed"]
    assert transport.call_count == 2 and tool.call_count == 1
    agent._handle_max_iterations.assert_not_called()
    assert json.loads(controller.path.read_text())["status"] == "cap_reached"


def test_checkpoint_failure_denies_without_review(harness, monkeypatch):
    agent, transport, tool, review, run = harness
    controller = agent._cron_continuation
    monkeypatch.setattr(controller, "_save", Mock(side_effect=OSError("disk full")))
    assert controller.before_call([], 0, False) == -1
    assert controller.status == "supervisor_or_checkpoint_error"
    review.assert_not_called()
    transport.assert_not_called()


def test_pending_tool_result_denies_review(harness):
    agent, transport, tool, review, run = harness
    controller = agent._cron_continuation
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "verified"}, {"id": "pending"}]},
        {"role": "tool", "tool_call_id": "verified", "content": "test passed"},
    ]
    try:
        assert controller.before_call(messages, 1, True) == -1
        assert controller.status == "no_progress"
        review.assert_not_called()
    finally:
        controller.timer.cancel()


@pytest.mark.parametrize("mode", ["codex_app_server", "acp"])
def test_external_runtime_rejected(harness, mode):
    agent, *_ = harness
    agent.api_mode = mode
    with pytest.raises(ValueError, match="Hermes conversation loop"):
        attach_continuation(agent, {"enabled": True}, "job", "repair")


def test_supervisor_rejects_tool_calls(monkeypatch):
    from cron.continuation import evaluate
    client = Mock()
    client.with_options.return_value = client
    client.chat.completions.create.return_value = NS(
        choices=[NS(message=NS(content=decision(), tool_calls=[NS(id="bad")]))])
    monkeypatch.setattr("agent.auxiliary_client.get_text_auxiliary_client", lambda task: (client, "supervisor"))
    with pytest.raises(ValueError, match="attempted tool use"):
        evaluate({}, 1)


def evidence_pair(call_id, output):
    return [{"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
             "function": {"name": "terminal", "arguments": '{"command":"pytest integration"}'}}]},
            {"role": "tool", "tool_call_id": call_id, "content": output}]


def test_huge_history_reviews_bounded_evidence_and_keeps_full_checkpoint(harness):
    from cron.continuation import MAX_REVIEW_CHARS
    agent, _, _, review, _ = harness
    controller = agent._cron_continuation
    messages = [{"role": "user", "content": "Repair regression"}]
    for i in range(300):
        messages += evidence_pair(f"old-{i}", f"iteration {i}: " + "historical log " * 300)
    messages += evidence_pair("verify-1", "regression passed; integration failed; session_id=worker-17")
    messages += [{"role": "assistant", "content": "Final working state: integration remains broken"}]
    try:
        assert controller.before_call(messages, 300, True) == 1
        request = review.call_args.args[0]
        assert len(json.dumps(request)) <= MAX_REVIEW_CHARS
        assert request["original_scope"] == {"text": "Repair regression", "complete": True}
        assert not request["history_complete"] and "UNKNOWN" in request["context_warning"]
        assert "messages" not in request and request["total_message_count"] == len(messages)
        pair = request["evidence"][0]
        assert pair["tool_call_id"] == "verify-1" and pair["eligible"]
        assert "pytest integration" in pair["command"]["text"]
        assert "worker-17" in pair["output"]["text"]
        assert "integration remains broken" in request["working_state"][0]["content"]["text"]
        checkpoint = json.loads(controller.path.read_text())
        assert checkpoint["messages"] == messages
        assert len(controller.path.read_text()) > 250000
    finally:
        controller.timer.cancel()


@pytest.mark.parametrize("missing", ["omitted", "truncated"])
def test_unseen_or_truncated_evidence_cannot_grant(harness, missing):
    agent, _, _, review, _ = harness
    controller = agent._cron_continuation
    messages = evidence_pair("verify-1", "x" * 20000 if missing == "truncated" else "old verified test")
    for i in range(3 if missing == "truncated" else 15):
        messages += evidence_pair(f"recent-{i}", f"test {i} passed; integration failed")
    # The review maliciously cites a real ID which is NOT eligible in its view.
    try:
        assert controller.before_call(messages, 20, True) == -1
        assert "verify-1" not in review.call_args.args[0]["new_evidence_ids"]
        assert controller.extensions == 0
    finally:
        controller.timer.cancel()


def test_only_oversized_output_is_not_verified_evidence(harness):
    agent, _, _, review, _ = harness
    controller = agent._cron_continuation
    try:
        assert controller.before_call(evidence_pair("verify-1", "passed " * 20000), 1, True) == -1
        assert controller.status == "no_complete_evidence"
        review.assert_not_called()
    finally:
        controller.timer.cancel()


def test_oversized_scope_cannot_drop_constraints(harness):
    agent, _, _, review, _ = harness
    controller = agent._cron_continuation
    controller.identity["original_scope"] = "scope " * 10000 + "DO NOT deploy"
    try:
        assert controller.before_call(evidence_pair("verify-1", "test passed"), 1, True) == -1
        assert controller.status == "scope_too_large"
        review.assert_not_called()
        assert json.loads(controller.path.read_text())["original_scope"].endswith("DO NOT deploy")
    finally:
        controller.timer.cancel()


def test_grant_preserves_live_budget_identity_and_charges_grace(harness):
    agent, transport, _, review, run = harness
    budgets = []
    def api(kwargs):
        budgets.append(agent.iteration_budget)
        if len(budgets) == 1:
            agent._budget_grace_call = True
            return response(call_id="verify-1")
        return response()
    transport.side_effect = api
    assert run("Repair regression")["completed"]
    assert budgets[0] is budgets[1]
    assert budgets[0].used == 2 and budgets[0].max_total == 2
    assert not agent._budget_grace_call
    assert review.call_count == 1


def test_projection_bounds_unicode_deduplicates_and_retains_prior_decisions(harness):
    from cron.continuation import MAX_REVIEW_CHARS, supervisor_projection
    agent, *_ = harness
    controller = agent._cron_continuation
    controller.decisions = [json.loads(decision())]
    messages = evidence_pair("old", "same verification") + evidence_pair("new", "same verification")
    messages += evidence_pair("huge", "🛠\n" * 20000)
    messages += [{"role": "assistant", "content": "🛠" * 20000}]
    controller.checkpoint(messages, 300, "exhausted")
    request = supervisor_projection(controller.snapshot, {"old", "new", "huge"})
    assert len(json.dumps(request)) <= MAX_REVIEW_CHARS
    assert request["new_evidence_ids"] == ["new"]
    assert [p["tool_call_id"] for p in request["evidence"]] == ["huge", "new"]
    assert not request["evidence"][0]["output"]["complete"]
    assert not request["working_state"][0]["content"]["complete"]
    assert json.loads(request["prior_decisions"]["text"]) == controller.decisions


def test_budget_extension_is_atomic_for_shared_consumers():
    from agent.iteration_budget import IterationBudget
    from concurrent.futures import ThreadPoolExecutor
    budget = IterationBudget(1)
    assert budget.consume()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: budget.extend(1), range(50)))
        consumed = list(pool.map(lambda _: budget.consume(), range(100)))
    assert sum(consumed) == 50
    assert budget.used == 51 and budget.remaining == 0
    with pytest.raises(ValueError):
        budget.extend(True)


