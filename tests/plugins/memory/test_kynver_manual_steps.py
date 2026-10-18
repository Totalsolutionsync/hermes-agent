"""Briefs, reports and skills that ask Will to do by hand what is automated, or repeat a replaced note.

17 Oct: compaction had been automated since 30 Sep, but a June note, a skill file ("hand Will
compact_wsl_vhdx.ps1") and the 14 Oct REPORT.md ("Will has to run it himself") kept saying
otherwise. ``hermes kynver blockers check-brief`` now flags such lines (Kynver's
``/memory/manual-step/check`` decides) and ``hermes kynver memory lint-skill`` reports a skill
whose own files disagree. Real plugin modules and the real ``KynverAgentOSClient`` against a
local stand-in for the Kynver API.
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SLUG = "test-agent"
OCT17 = {"slug": "will-disk-preference-compaction-automated-1017", "updatedAt": "2026-10-18T03:00:00.000Z",
         "preview": "Compaction itself is no longer a disruptive manual job: the Windows \"WSL Auto-Compact\" task runs it automatically"}
JUNE = {"slug": "note-1780862368551", "updatedAt": "2026-06-07T10:00:00.000Z", "replacedBy": "will-disk-preference-compaction-automated-1017",
        "preview": "Will prefers avoiding WSL VHDX compaction when possible because it can take about an hour"}


def _verdict(text):
    low = text.lower()
    manual = any(k in low for k in ("admin powershell", "hand will", "has to run", "ask will"))
    out = {"text": text, "isManualStep": manual, "automated": False, "automation": [], "retiredManualSteps": [], "replaced": [], "advice": ""}
    if "compact" in low and manual:
        out.update(automated=True, automation=[OCT17], advice="Do not ask Will to do this by hand")
    if "avoid" in low and "compaction" in low:
        out["replaced"] = [JUNE]
    return out


class _FakeKynver:
    def __init__(self, *, broken=False, old_server=False):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, body))
                if broken:
                    status, payload = 503, {"error": "down"}
                elif self.path == f"/api/agent-os/{SLUG}/memory/limitations/check":
                    status, payload = 200, {"results": [{"status": "unverified", "matches": [], "evidence": []} for _ in body["items"]]}
                elif self.path == f"/api/agent-os/{SLUG}/memory/manual-step/check" and not old_server:
                    if "items" in body:
                        status, payload = 200, {"results": [_verdict(i["text"]) for i in body["items"]]}
                    else:
                        status, payload = 200, _verdict(body["text"])
                else:
                    status, payload = 404, {"error": "not found"}
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    server = _FakeKynver()
    yield server
    server.close()


def _client(url):
    from plugins.memory.kynver.agentos_bridge import KynverAgentOSClient, KynverAgentOSConfig

    return KynverAgentOSClient(KynverAgentOSConfig(api_url=url, api_key="k", slug=SLUG))


def _args(**kw):
    base = {"json": False}
    base.update(kw)
    return argparse.Namespace(**base)


SKILL_MANUAL = "When C: is low, hand Will compact_wsl_vhdx.ps1 and ask him to run it from an Admin PowerShell; it takes about an hour."
SKILL_AUTO = "WSL compaction is automatic: the WSL Auto-Compact scheduled task compacts the VHDX 04:00-06:00 when idle."
REPORT_LINE = "Will has to run the WSL compaction himself from Admin PowerShell after the cleanup."


def test_phrasing_mirrors_kynver():
    from plugins.memory.kynver.manual_steps import is_automation_text, is_manual_step_text

    assert is_manual_step_text(SKILL_MANUAL)
    assert is_manual_step_text(REPORT_LINE)
    assert is_manual_step_text("Ask Will to approve the Railway variable change.")
    # The cure is not the disease.
    assert not is_manual_step_text("Never ask Will to run a compaction script by hand; it is automated.")
    assert not is_manual_step_text("Do not hand Will a script and do not ask him to run Admin PowerShell.")
    assert not is_manual_step_text("Never run wsl --shutdown from inside WSL, and never hand Will a compaction script.")
    # The agent's own runbook steps are not asks of Will.
    assert not is_manual_step_text("Run the script manually with timeout 120s and capture stdout.")
    assert not is_manual_step_text("Trigger one manual run.")
    assert is_automation_text(SKILL_AUTO)
    assert not is_automation_text("Will must run the DNS update by hand; it is not automated yet.")


def test_check_brief_flags_a_by_hand_step_memory_says_is_automated(fake, tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN, EXIT_STALE, cmd_check_brief

    brief = tmp_path / "brief.md"
    brief.write_text(f"# Cleanup\nFree space on C:.\n{REPORT_LINE}\n")
    assert cmd_check_brief(_args(brief=str(brief)), client=_client(fake.url)) == EXIT_STALE
    out = capsys.readouterr().out
    assert "1 outdated line(s)" in out
    assert "AUTOMATED" in out and "brief.md:3" in out and OCT17["slug"] in out
    path, body = next((p, b) for p, b in fake.requests if p.endswith("/manual-step/check"))
    assert [i["text"] for i in body["items"]] == [REPORT_LINE]

    clean = tmp_path / "clean.md"
    clean.write_text("Free space on C:. Compaction is automatic; run request_compact.sh status afterwards.\n")
    assert cmd_check_brief(_args(brief=str(clean)), client=_client(fake.url)) == EXIT_CLEAN


def test_check_brief_flags_a_line_repeating_a_replaced_note(fake, tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_STALE, cmd_check_brief

    brief = tmp_path / "brief.md"
    brief.write_text("Avoid WSL VHDX compaction, it takes about an hour; do temp cleanup first.\n")
    assert cmd_check_brief(_args(brief=str(brief), json=True), client=_client(fake.url)) == EXIT_STALE
    data = json.loads(capsys.readouterr().out)
    assert data["blockers"] == []
    assert data["manualSteps"][0]["status"] == "replaced"
    assert data["manualSteps"][0]["replaced"][0]["replacedBy"] == OCT17["slug"]


def test_check_brief_keeps_working_against_a_kynver_without_the_route(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN, cmd_check_brief

    old = _FakeKynver(old_server=True)
    try:
        brief = tmp_path / "brief.md"
        brief.write_text(f"{REPORT_LINE}\n")
        assert cmd_check_brief(_args(brief=str(brief)), client=_client(old.url)) == EXIT_CLEAN
    finally:
        old.close()
    assert "no manual-step check yet" in capsys.readouterr().err


def test_check_brief_exit_2_when_kynver_is_down(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_UNAVAILABLE, cmd_check_brief

    broken = _FakeKynver(broken=True)
    try:
        brief = tmp_path / "brief.md"
        brief.write_text(f"{REPORT_LINE}\n")
        assert cmd_check_brief(_args(brief=str(brief)), client=_client(broken.url)) == EXIT_UNAVAILABLE
    finally:
        broken.close()


def test_lint_skill_reports_files_that_contradict_each_other_and_memory(fake, tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_STALE, cmd_lint_skill

    skill = tmp_path / "hermes-host-operations"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"# Host operations\n- {SKILL_AUTO}\n")
    (skill / "references" / "disk-space.md").write_text(f"# Disk space\n{SKILL_MANUAL}\n")
    before = {p: p.read_text() for p in skill.rglob("*.md")}

    assert cmd_lint_skill(_args(skill_dir=str(skill)), client=_client(fake.url)) == EXIT_STALE
    out = capsys.readouterr().out
    assert "1 line(s) tell Will to do by hand what another line says is automated" in out
    assert "disk-space.md:2" in out and "SKILL.md:2" in out
    assert "contradict current memory" in out and OCT17["slug"] in out
    # Dry run: nothing rewritten.
    assert {p: p.read_text() for p in skill.rglob("*.md")} == before


def test_lint_skill_clean_and_offline(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN, EXIT_UNAVAILABLE, cmd_lint_skill
    from plugins.memory.kynver.manual_steps import lint_skill

    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text(f"- {SKILL_AUTO}\n- Always post IG at 11:00.\n")
    fake = _FakeKynver()
    try:
        assert cmd_lint_skill(_args(skill_dir=str(skill)), client=_client(fake.url)) == EXIT_CLEAN
    finally:
        fake.close()
    # Without Kynver the file-vs-file lint still runs.
    (skill / "old.md").write_text(f"{SKILL_MANUAL}\n")
    assert len(lint_skill(str(skill), None)["internal"]) == 1
    broken = _FakeKynver(broken=True)
    try:
        assert cmd_lint_skill(_args(skill_dir=str(skill)), client=_client(broken.url)) == 1  # internal finding wins
    finally:
        broken.close()
    (skill / "old.md").unlink()
    broken = _FakeKynver(broken=True)
    try:
        assert cmd_lint_skill(_args(skill_dir=str(skill)), client=_client(broken.url)) == EXIT_UNAVAILABLE
    finally:
        broken.close()


def test_cli_parses_lint_skill():
    from plugins.memory.kynver.cli import kynver_command, register_cli

    parser = argparse.ArgumentParser(prog="hermes kynver")
    register_cli(parser)
    args = parser.parse_args(["memory", "lint-skill", "skills/x", "--json"])
    assert (args.kynver_command, args.memory_command, args.skill_dir, args.json, args.func) == ("memory", "lint-skill", "skills/x", True, kynver_command)


def test_recall_renders_replaced_notes_as_history():
    from plugins.memory.kynver.context import fit_whole_items

    old = {"slug": "june", "content": "Will prefers avoiding compaction",
           "contradiction": {"status": "replaced", "side": "older", "replacedBy": "oct17", "banner": "REPLACED by oct17 on 2026-10-17: compaction is automatic now. Out of date; do not use as current."}}
    md = fit_whole_items([old], token_budget=2000, max_index_items=5).markdown
    line = next(l for l in md.splitlines() if "[june]" in l)
    assert line.startswith("- [REPLACED by oct17 on 2026-10-17")
    assert "history only, not current" in line


@pytest.fixture
def provider(fake):
    from plugins.memory import load_memory_provider

    discovered = load_memory_provider("kynver", register_skills=False)
    assert discovered is not None
    return type(discovered)(client=_client(fake.url))


def test_check_manual_step_tool(fake, provider):
    names = {s["name"] for s in provider.get_tool_schemas()}
    assert "kynver_memory_check_manual_step" in names
    result = json.loads(provider.handle_tool_call("kynver_memory_check_manual_step", {"text": "run the compaction script from Admin PowerShell"}))
    assert result["success"] is True and result["automated"] is True
    assert result["automation"][0]["slug"] == OCT17["slug"]
    assert fake.requests[-1] == (f"/api/agent-os/{SLUG}/memory/manual-step/check", {"text": "run the compaction script from Admin PowerShell"})
