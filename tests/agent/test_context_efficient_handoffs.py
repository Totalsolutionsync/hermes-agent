"""Handoff request contracts and evidence identity across compactions.

These exercise actual request construction, not LLM semantic compliance.
"""
from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from tools.delegate_tool import _build_child_system_prompt


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_repeated_compaction_retains_evidence_identity_and_redacts_prior_summary():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(model="test/model", quiet_mode=True)
    evidence = "/tmp/worktree/results.log"
    turns = [
        {"role": "user", "content": "Verify fix; acceptance: focused tests pass; do not push."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-verify-17", "type": "function", "function": {"name": "terminal", "arguments": '{"command":"scripts/run_tests.sh tests/unit.py > /tmp/worktree/results.log"}'}}]},
        {"role": "tool", "tool_call_id": "call-verify-17", "content": "exit_code=0; 8 passed; /tmp/worktree/results.log"},
    ]
    prior = f"Verified: 8 passed; {evidence}; call-verify-17"
    with patch("agent.context_compressor.call_llm", side_effect=[response(prior), response("current handoff")]) as call:
        compressor._generate_summary(turns)
        compressor._generate_summary([{"role": "user", "content": "Now inspect the diff, still do not push."}])
    first, second = [args.kwargs["messages"][0]["content"] for args in call.call_args_list]
    assert "[tool_call_id=call-verify-17]" in first
    assert "[TOOL RESULT call-verify-17]" in first
    assert evidence in first and evidence in second
    assert second.count(prior) == 1
    assert "Now inspect the diff" in second
    for prompt in (first, second):
        assert "acceptance criteria" in prompt
        assert "Verified Results & Evidence" in prompt
        assert "not a quota" in prompt
        assert "continue numbering" not in prompt
    assert "later user corrections take precedence" in second


def test_object_tool_calls_keep_evidence_identity():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(model="test/model", quiet_mode=True)
    turns = [{"role": "assistant", "content": "", "tool_calls": [
        SimpleNamespace(id="call-object-17", function=SimpleNamespace(name="terminal"))
    ]}]
    with patch("agent.context_compressor.call_llm", return_value=response("safe")) as call:
        compressor._generate_summary(turns)
    assert "terminal(...) [tool_call_id=call-object-17]" in call.call_args.kwargs["messages"][0]["content"]


def test_previous_summary_is_redacted_before_auxiliary_request():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(model="test/model", quiet_mode=True)
    secret = "sk-" + "a" * 48
    compressor._previous_summary = f"API key: {secret}; evidence: /tmp/check.log"
    with patch("agent.context_compressor.call_llm", return_value=response("safe")) as call:
        compressor._generate_summary([{"role": "user", "content": "continue"}])
    prompt = call.call_args.kwargs["messages"][0]["content"]
    assert secret not in prompt
    assert "/tmp/check.log" in prompt


def test_child_handoff_preserves_exact_task_and_workspace():
    prompt = _build_child_system_prompt("Return JSON with verified checks", workspace_path="/tmp/isolated-repo")
    assert "YOUR TASK:\nReturn JSON with verified checks" in prompt
    assert "WORKSPACE PATH:\n/tmp/isolated-repo" in prompt
    assert "acceptance criteria" in prompt
    assert "artifact/log or source pointers" in prompt
    assert "never invent verification" in prompt
    assert "task-specific output format" in prompt
