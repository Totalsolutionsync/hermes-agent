"""Tests for profile-safe tool output artifact storage."""

import json
from pathlib import Path

import pytest

from tools.tool_output_artifacts import (
    HANDLE_PREFIX,
    compact_tool_output,
    resolve_tool_output_artifact,
    store_tool_output_artifact,
)


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_compact_small_output_unchanged(hermes_home):
    text = "hello\nworld"
    assert compact_tool_output(text, threshold=1000) == text


def test_compact_large_output_stores_artifact_and_preview(hermes_home):
    text = "\n".join(f"line-{i}" for i in range(5000))
    compact = compact_tool_output(
        text,
        threshold=1000,
        kind="terminal",
        command="npm test",
        exit_code=1,
    )
    assert len(compact) < len(text)
    assert HANDLE_PREFIX in compact
    assert "first lines" in compact
    assert "last lines" in compact
    assert "npm test" in compact

    handle = compact.split("Handle: ", 1)[1].splitlines()[0].strip()
    restored = resolve_tool_output_artifact(handle)
    assert restored == text


def test_store_and_resolve_round_trip(hermes_home):
    payload = "alpha\nbeta\ngamma"
    record = store_tool_output_artifact(payload, kind="process", command="make", exit_code=0)
    assert record.path.is_file()
    meta_path = record.path.parent / f"{record.artifact_id}.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["command"] == "make"
    assert resolve_tool_output_artifact(record.handle) == payload
