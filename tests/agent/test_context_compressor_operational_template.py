"""Operational compaction summary template checks."""

from pathlib import Path

from agent.conversation_compression import OPERATIONAL_COMPACTION_SECTIONS


def test_operational_sections_constant():
    assert "Current User Ask" in OPERATIONAL_COMPACTION_SECTIONS
    assert "Artifact Handles" in OPERATIONAL_COMPACTION_SECTIONS
    assert "Omitted History Refs" in OPERATIONAL_COMPACTION_SECTIONS


def test_context_compressor_template_contains_operational_sections():
    source = Path(__file__).resolve().parents[2] / "agent" / "context_compressor.py"
    text = source.read_text(encoding="utf-8")
    for section in (
        "## Current User Ask",
        "## Active Task/Plan IDs",
        "## Latest Verified State",
        "## Artifact Handles",
        "## Omitted History Refs",
    ):
        assert section in text
    assert "chronological" in text.lower()
