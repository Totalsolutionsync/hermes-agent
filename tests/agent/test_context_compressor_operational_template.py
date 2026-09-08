"""Operational continuity contracts, independent of legacy section titles."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.conversation_compression import OPERATIONAL_COMPACTION_SECTIONS


def test_operational_sections_constant():
    # The separate operational formatter retains its existing public headings.
    assert "Current User Ask" in OPERATIONAL_COMPACTION_SECTIONS
    assert "Artifact Handles" in OPERATIONAL_COMPACTION_SECTIONS
    assert "Omitted History Refs" in OPERATIONAL_COMPACTION_SECTIONS


def test_context_compressor_preserves_operational_continuity():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(model="test/model", quiet_mode=True)
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="handoff"))])
    with patch("agent.context_compressor.call_llm", return_value=response) as call:
        compressor._generate_summary([{"role": "user", "content": "Verify task-17; do not deploy."}])
    prompt = call.call_args.kwargs["messages"][0]["content"]
    headings = [line for line in prompt.splitlines() if line.startswith("## ")]
    assert len(headings) == 5
    for requirement in (
        "latest unfulfilled objective", "acceptance criteria", "plan/task IDs",
        "last observed status", "artifact/log paths", "unresolved failures",
        "Each fact belongs in one section", "Never invent evidence",
    ):
        assert requirement.lower() in prompt.lower()
    assert "Verify task-17; do not deploy." in prompt
