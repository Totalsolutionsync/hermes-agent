"""Behavior contracts for topic-triggered recall of compression-pruned skills.

A skill pruned to a ``[SKILL_PRUNED: ...]`` marker must come back — as a one-shot note on the
turn's injected context — when a later human message touches its topic, and must cost nothing
otherwise.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent.context_compressor import _skill_pruned_marker
from agent.pruned_skill_recall import pruned_skill_recall_note

LEGAL_BODY = """# Pro Se Legal Case Support

1. Read the complaint and the docket before drafting anything; cite the rule for every deadline.
2. Draft motions in the court's format and keep a filing checklist with service dates.
15b. **Mediation-day deliverables:** print the cards and, if asked, email the full packet to Will
as numbered PDFs in open-first order (cards, statement and exhibits, their papers, filed papers),
with a copy in Windows Downloads. Printing from WSL: Windows lists no real printer, but Will HAS a
Wi-Fi printer. Never tell him printing is impossible; send to it over IPP as PWG raster (recipe in
`case-pitfalls.md`, script `~/.hermes/scripts/print-brother.py`).
16. Before the hearing, rehearse the opening statement and the counterclaim timeline with Will.
"""

FILLER_TOPICS = [
    ("deploy-pipeline", "Use when deploying services.", "Deploy the service, check logs, send a status update to the team."),
    ("video-shorts", "Use when editing shorts.", "Cut the clip, add captions, send the draft video for approval."),
    ("email-triage", "Use when triaging email.", "Read each email thread, draft replies, send nothing without approval."),
    ("crm-contacts", "Use when editing CRM records.", "Update the record, check each field, send a summary of changes."),
    ("browser-ops", "Use when driving the browser.", "Open the tab, check the page loaded, send a screenshot."),
    ("db-migrations", "Use when writing migrations.", "Write the migration, check it on a copy, send the plan for review."),
    ("market-notes", "Use when writing market notes.", "Pull the data, check the sources, send the note."),
    ("docs-maps", "Use when editing docs maps.", "Edit the map, check links, send the diff."),
]


def _write_skill(root: Path, name: str, description: str, body: str) -> Path:
    path = root / "skills" / "misc" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture()
def skills_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_skill(home, "pro-se-legal-case-support", "Use when helping Will with his own court case.", LEGAL_BODY)
    for name, desc, body in FILLER_TOPICS:
        _write_skill(home, name, desc, f"# {name}\n\n1. {body}\n2. Keep the work small and verify it.\n")
    return home


def _pruned_history(*skills: str) -> list:
    summary = "[CONTEXT COMPACTION] earlier turns summarized.\n\n## Pruned Skills\n" + "\n".join(
        _skill_pruned_marker(s) for s in skills
    )
    return [
        {"role": "user", "content": summary},
        {"role": "assistant", "content": "Understood."},
    ]


def _note(history: list, text) -> str:
    messages = history + [{"role": "user", "content": text}]
    return pruned_skill_recall_note(messages, len(history), text)


INCIDENT = "Please print what I need to sign and I'll send you a picture"
UNRELATED = "how many contacts do we have for each company?"


def test_incident_message_surfaces_pruned_skill_with_its_printing_step(skills_home):
    note = _note(_pruned_history("pro-se-legal-case-support"), INCIDENT)

    assert "pro-se-legal-case-support" in note
    assert "skill_view(name='pro-se-legal-case-support')" in note
    assert "print" in note.split("matched:", 1)[1].split(")", 1)[0]
    # The key fact arrives even if the model never reloads.
    assert "Wi-Fi printer" in note
    assert "print-brother.py" in note


def test_unrelated_message_adds_nothing(skills_home):
    assert _note(_pruned_history("pro-se-legal-case-support"), UNRELATED) == ""


def test_skill_that_was_never_pruned_is_not_surfaced(skills_home):
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert _note(history, INCIDENT) == ""


def test_skill_reloaded_after_prune_is_not_surfaced_again(skills_home):
    history = _pruned_history("pro-se-legal-case-support") + [
        {"role": "user", "content": "open the case skill"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "skill_view", "arguments": json.dumps({"name": "pro-se-legal-case-support"})},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": LEGAL_BODY},
        {"role": "assistant", "content": "Loaded."},
    ]
    assert _note(history, INCIDENT) == ""


def test_quoted_bot_reply_does_not_trigger(skills_home):
    text = '[Replying to: "I printed the signature pages on the Wi-Fi printer"]\n\nhow many contacts per company?'
    assert _note(_pruned_history("pro-se-legal-case-support"), text) == ""


def test_word_shared_by_most_skills_never_triggers(skills_home):
    # "send" and "check" appear in every installed skill, including the pruned one.
    assert _note(_pruned_history("pro-se-legal-case-support"), "send it and check it") == ""


def test_same_step_is_surfaced_once_per_prune(skills_home):
    history = _pruned_history("pro-se-legal-case-support")
    first = _note(history, INCIDENT)
    assert first
    history += [
        {"role": "user", "content": INCIDENT, "api_content": INCIDENT + "\n\n" + first},
        {"role": "assistant", "content": "Printing now."},
    ]
    assert _note(history, "Can you print the signature pages too?") == ""


def test_at_most_two_skills_per_turn(skills_home):
    topics = ["notary", "scanner", "courier"]
    for topic in topics:
        _write_skill(
            skills_home, f"{topic}-runbook", f"Use when dealing with the {topic}.",
            f"# {topic}\n\n1. " + " ".join([f"Call the {topic} first; the {topic} handles it."] * 4),
        )
    history = _pruned_history("pro-se-legal-case-support", *[f"{t}-runbook" for t in topics])
    note = _note(history, INCIDENT + " via the notary, the scanner and the courier")
    assert len([line for line in note.splitlines() if line.startswith("[Pruned skill")]) == 2


def test_skill_edit_is_picked_up_without_restart(skills_home):
    history = _pruned_history("deploy-pipeline")
    assert _note(history, "the arbitrator wants a stipulation") == ""

    path = skills_home / "skills" / "misc" / "deploy-pipeline" / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "3. If an arbitrator asks for a stipulation, draft the stipulation and send it to the arbitrator.\n",
        encoding="utf-8",
    )
    later = time.time() + 5
    os.utime(path, (later, later))
    assert "deploy-pipeline" in _note(history, "the arbitrator wants a stipulation")


def test_term_that_is_this_conversations_background_does_not_trigger_alone(skills_home):
    history = _pruned_history("pro-se-legal-case-support")
    for i in range(6):
        history += [
            {"role": "user", "content": f"printer status update {i} on the shipping labels"},
            {"role": "assistant", "content": "ok"},
        ]
    assert _note(history, "Is the printer done?") == ""


def test_multimodal_turn_matches_on_its_text_part(skills_home):
    parts = [{"type": "text", "text": INCIDENT}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    assert "pro-se-legal-case-support" in _note(_pruned_history("pro-se-legal-case-support"), parts)


def test_turn_context_appends_note_to_plugin_context_only_on_match(skills_home):
    from agent.turn_context import _merge_pruned_skill_recall

    history = _pruned_history("pro-se-legal-case-support")
    matched = history + [{"role": "user", "content": INCIDENT}]
    merged = _merge_pruned_skill_recall(matched, len(history), INCIDENT, "plugin ctx")
    assert merged.startswith("plugin ctx\n\n[Pruned skill 'pro-se-legal-case-support'")

    unmatched = history + [{"role": "user", "content": UNRELATED}]
    assert _merge_pruned_skill_recall(unmatched, len(history), UNRELATED, "plugin ctx") == "plugin ctx"
    assert _merge_pruned_skill_recall(unmatched, len(history), UNRELATED, "") == ""
