"""check-brief must not refuse a line that restates the newest version of a replaced rule.

18 Oct: three briefs were refused for lines that restated current rules: "Cold open" in a
chapter-rename line, "QA gate", and "whole script in one continuous generation". Kynver matched
each to a replaced note, but the newest note agreed with the line (or the match was a chat
summary, or the old note had only moved). The memory texts below are the real Kynver notes
from that night (trimmed).
"""

import argparse
import json
from urllib.parse import parse_qs, urlparse

PACE_1009 = {
    "slug": "frame-longform-pace-hook-pilot-1009",
    "content": (
        "[REPLACED by longform-first-30s-most-gripping-1018 on 2026-10-18: ...] Will (2026-10-09) decisions for The Frame long-forms (bridges #1 and later):\n"
        "1. Pace: slightly slower than Theo's Shorts read (180–200 wpm), so ~170 wpm.\n"
        "2. Chapter titles: beat names, not questions.\n"
        "3. Cold open = flash-forward. YouTube autoplays ~30 s when people scroll, so the most exciting moment goes in the first ~30 s, "
        "followed by a mystery line (no answer), a quick tease of what's coming, then a cut back to the start of the story.\n"
        "4. No boring chapters. Diagram-heavy physics is told through a person's investigation, with no diagram-only stretch over ~60–90 s.\n"
        "5. Pilot first: after the script is approved, build only the first ~2–3 minutes to test pacing and the hook before producing the full video."
    ),
    "metadata": {"contradiction": {
        "status": "replaced", "replacedBy": "longform-first-30s-most-gripping-1018",
        "replacedReason": "Old note's cold-open strategy (flash-forward with mystery line) is explicitly replaced: new note forbids "
                          "footage-only cold opens and requires narration from second one with gripping content, not the f"}},
}
GRIPPING_1018 = {
    "slug": "longform-first-30s-most-gripping-1018",
    "content": "Will (18 Oct 2026) on long-form openings: the first 30 s must be the most gripping 30 s of the video. No footage-only cold "
               "open (v5's 20 s silent opening is out), no slow storytelling: narrate from second one, give the viewer a reason to stay "
               "with mystery, suspense, drama and stakes. Supersedes the 9 Oct point 3 (footage flash-forward cold open).",
    "metadata": {},
}
LESSONS_0930 = {
    "slug": "frame-shorts-production-lessons-0930",
    "content": "[REPLACED by frame-shorts-production-lessons-0930-split on 2026-09-30: ...] The Frame Explained Short-building lessons "
               "(Will, 2026-09-30): (1) record each continuous voice passage as ONE TTS take; Will hears every mid-thought splice, gain "
               "matching doesn't hide it. (2) Never hard-cut between two framings of the same shot. (3) Captions always word-highlight.",
    "metadata": {"supersededBy": "frame-shorts-production-lessons-0930-split",
                 "supersededReason": "Frame lessons moved from shared skill into dedicated the-frame-shorts skill at Will's request"},
}
SPLIT_0930 = {
    "slug": "frame-shorts-production-lessons-0930-split",
    "content": "Shorts skills are split per channel (Will, 2026-09-30, to cut context bloat): The Frame work loads ONLY skill "
               "`the-frame-shorts`. Frame lessons (one-take voice passages, no same-shot framing jumps, karaoke captions) live in the-frame-shorts.",
    "metadata": {},
}
JUNE = {
    "slug": "note-1780862368551",
    "content": "Will prefers avoiding WSL VHDX compaction when possible because it can take about an hour and blocks the machine.",
    "metadata": {"supersededBy": "will-disk-preference-compaction-automated-1017"},
}
OCT17 = {
    "slug": "will-disk-preference-compaction-automated-1017",
    "content": "Compaction itself is no longer a disruptive manual job: the Windows \"WSL Auto-Compact\" task runs it automatically.",
    "metadata": {},
}
NOTES = {n["slug"]: n for n in (PACE_1009, GRIPPING_1018, LESSONS_0930, SPLIT_0930, JUNE, OCT17)}

CHAPTER_LINE = ("1. **Chapters (Will):** the first chapter needs a real story name instead of a generic label. Current list: Cold open · "
                "Galloping Gertie · The professor's model · Quebec, 1907. Rename ALL nine so a stranger reading only the chapter list knows "
                "what happens there and wants to click (concrete event, person or question).")
QA_LINE = ("- QA gate: the KariAd QA gate must pass, plus inspect_stored_video, a seam check, a hearing check on every sentence, "
           "and the AI-seconds total.")
TAKE_LINE = "- Ellie's voice: house voice on eleven_v3, speed 1.2. Each take is the whole script in one continuous generation, never stitched."
OLD_LINE = "Always avoid WSL VHDX compaction, it takes about an hour; do temp cleanup first."


def _rec(old, new, sim=0.02):
    return {"slug": old, "replacedBy": new, "similarity": sim, "preview": NOTES.get(old, {}).get("content", "")[:200]}


VERDICTS = {
    CHAPTER_LINE: [_rec(PACE_1009["slug"], GRIPPING_1018["slug"], 0.03)],
    QA_LINE: [{"slug": "gateway-conv:05ce28f7-0819-4764-bd05-1900b01f3a8c", "replacedBy": "8819f6ea-7e5c-477c-a68b-ebc754148e55",
               "preview": "Conversation \"[PARITY TEST] Make the Unlikely Humans Short for 20 October\" — recent exchanges: ..."}],
    TAKE_LINE: [_rec(LESSONS_0930["slug"], SPLIT_0930["slug"])],
    OLD_LINE: [_rec(JUNE["slug"], OCT17["slug"])],
}


class FakeClient:
    """In-process stand-in for KynverAgentOSClient (post = batch checks, get = exact-key reads)."""

    def __init__(self, notes=NOTES, readable=True):
        self.notes, self.readable, self.gets = notes, readable, []

    def post(self, path, body, timeout=None):
        if path.endswith("/limitations/check"):
            return {"results": [{"status": "unverified", "matches": [], "evidence": []} for _ in body["items"]]}
        results = []
        for item in body["items"]:
            text = item["text"]
            hit = next((v for k, v in VERDICTS.items() if text in k or k in text), [])
            results.append({"text": text, "isManualStep": False, "automated": False, "automation": [],
                            "retiredManualSteps": [], "replaced": [dict(r) for r in hit], "advice": ""})
        return {"results": results}

    def get(self, path, timeout=None):
        key = parse_qs(urlparse(path).query).get("key", [""])[0]
        self.gets.append(key)
        if not self.readable:
            raise RuntimeError("Kynver down")
        note = self.notes.get(key)
        return {"items": [note] if note else []}


def _run(tmp_path, capsys, text, client=None):
    from plugins.memory.kynver.cli import cmd_check_brief

    brief = tmp_path / "brief.md"
    brief.write_text(text + "\n")
    rc = cmd_check_brief(argparse.Namespace(brief=str(brief), json=True), client=client or FakeClient())
    return rc, json.loads(capsys.readouterr().out)


def test_chapter_rename_line_mentioning_cold_open_is_not_flagged(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN

    rc, data = _run(tmp_path, capsys, CHAPTER_LINE)
    assert rc == EXIT_CLEAN and data["manualSteps"] == []
    why = data["clearedReplaced"][0]["cleared"][0]["why"]
    assert "still current" in why and "Chapter titles" in why


def test_qa_gate_line_matching_a_chat_summary_is_not_flagged(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN

    rc, data = _run(tmp_path, capsys, QA_LINE)
    assert rc == EXIT_CLEAN
    assert "chat summary" in data["clearedReplaced"][0]["cleared"][0]["why"]


def test_one_continuous_take_line_is_not_flagged_when_the_old_note_only_moved(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN

    rc, data = _run(tmp_path, capsys, TAKE_LINE)
    assert rc == EXIT_CLEAN
    assert "moved" in data["clearedReplaced"][0]["cleared"][0]["why"]


def test_line_repeating_the_old_wording_is_still_refused_with_the_reason(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_STALE

    rc, data = _run(tmp_path, capsys, OLD_LINE)
    assert rc == EXIT_STALE
    rec = data["manualSteps"][0]["replaced"][0]
    assert rec["newest"] == OCT17["slug"]
    assert "old wording" in rec["why"] and "vhdx" in rec["why"]


def test_unreadable_newer_note_keeps_kynvers_flag(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_STALE

    rc, data = _run(tmp_path, capsys, CHAPTER_LINE, client=FakeClient(readable=False))
    assert rc == EXIT_STALE
    assert "could not read" in data["manualSteps"][0]["replaced"][0]["why"]


def test_chain_is_followed_to_the_newest_note():
    from plugins.memory.kynver.manual_steps import GuardFinding
    from plugins.memory.kynver.blockers import Statement
    from plugins.memory.kynver.replaced_check import refine_replaced

    mid = {"slug": "mid", "content": "Prefer manual VHDX compaction on Sundays.", "metadata": {"supersededBy": OCT17["slug"]}}
    first = {"slug": "first", "content": "Avoid WSL VHDX compaction, it takes about an hour.", "metadata": {"supersededBy": "mid"}}
    notes = {**NOTES, "mid": mid, "first": first}
    f = GuardFinding(statement=Statement(path="b.md", line_no=1, text="Compaction runs automatically via the WSL Auto-Compact task."),
                     manual_step=False, automated=False, replaced=[{"slug": "first", "replacedBy": "mid"}])
    refine_replaced([f], FakeClient(notes=notes))
    assert f.replaced == [] and f.cleared[0]["newest"] == OCT17["slug"]


def test_human_output_names_the_reason(tmp_path, capsys):
    from plugins.memory.kynver.cli import cmd_check_brief

    brief = tmp_path / "brief.md"
    brief.write_text(OLD_LINE + "\n" + TAKE_LINE + "\n")
    cmd_check_brief(argparse.Namespace(brief=str(brief), json=False), client=FakeClient())
    out = capsys.readouterr().out
    assert "REPLACED" in out and "why: uses old wording" in out
    assert "agree with the newest one" in out and "moved/regrouped" in out
