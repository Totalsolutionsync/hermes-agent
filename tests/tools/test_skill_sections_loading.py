"""Section-aware skill_view loading tests."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skill_sections import extract_section_content, truncate_sections
from tools.skills_tool import skill_view


SKILL_BODY = (
    "---\n"
    "name: demo-skill\n"
    "description: Demo skill for section loading.\n"
    "metadata:\n"
    "  hermes:\n"
    "    mandatory_sections:\n"
    "      - Overview\n"
    "---\n\n"
    "# Demo\n\n"
    "## Overview\n"
    "Always keep this section intact.\n\n"
    "## Config Sections\n"
    "Line one\n"
    "Line two\n"
    "Line three with more detail about compression defaults.\n\n"
    "## Optional Deep Dive\n"
    + ("x" * 5000)
    + "\n"
)


@pytest.fixture()
def skills_env(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills" / "demo-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("tools.skills_tool.SKILLS_DIR", tmp_path / "skills"):
        yield tmp_path


def test_extract_section_content():
    section = extract_section_content(SKILL_BODY, "Config Sections")
    assert section is not None
    assert "compression defaults" in section


def test_mandatory_sections_not_truncated():
    mandatory = {"overview"}
    truncated, did_truncate = truncate_sections(
        SKILL_BODY,
        max_bytes=1200,
        mandatory_sections=mandatory,
    )
    assert did_truncate is True
    assert "Always keep this section intact." in truncated
    assert "...[section truncated]" in truncated
    overview_start = truncated.index("## Overview")
    overview_end = truncated.index("## Config Sections", overview_start)
    overview_block = truncated[overview_start:overview_end]
    assert "...[section truncated]" not in overview_block


def test_skill_view_summary_and_section(skills_env):
    summary = json.loads(skill_view("demo-skill", summary=True))
    assert summary["success"] is True
    assert summary["summary"] is True
    assert any(s["title"] == "Config Sections" for s in summary["sections"])
    assert "content" not in summary

    section = json.loads(skill_view("demo-skill", section="Config Sections"))
    assert section["success"] is True
    assert "compression defaults" in section["content"]
    assert len(section["content"]) < len(SKILL_BODY)

    full = json.loads(skill_view("demo-skill"))
    assert full["success"] is True
    assert len(full["content"]) >= len(section["content"])
