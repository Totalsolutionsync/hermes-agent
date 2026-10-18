"""Stale blocker lines in skills, briefs and worker reports are caught before they are repeated.

Kynver retires a limitation memory when its linked fix ships, but a copied brief or a skill
keeps the old "held because X can't do Y" text. ``hermes kynver blockers`` finds those lines
and asks Kynver's ``/memory/limitations/check``; Kynver decides. These tests run the real
plugin modules and the real ``KynverAgentOSClient`` (urllib) against a local stand-in for the
Kynver API that answers like the real route.
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SLUG = "test-agent"
FB_FIX = {"status": "fixed", "fixedBy": "Totalsolutionsync/KariAdV2#398", "fixedAt": "2026-10-16T18:41:28Z",
          "matches": [{"slug": "kariad-fb-ai-label-hold", "status": "fixed", "fixRef": "Totalsolutionsync/KariAdV2#398"}],
          "evidence": ["KariAdV2 #398 merged at 2026-10-16T18:32:00Z"]}
TT_OPEN = {"status": "open", "fixedBy": None, "fixedAt": None,
           "matches": [{"slug": "tiktok-carousel", "status": "open", "fixRef": "Org/App#12"}], "evidence": []}

REPORT = """DONE: Brunnchen is scheduled on 4 of 5 platforms. Facebook (Sat 10:00) is built but held, because KariAd can't send Meta's AI label to Facebook. One command releases it.

- TikTok carousels are not supported by the publisher yet.
- The cover is full size.

```
echo "this can't be a finding, it's in a code fence"
```
"""


def _verdict(text):
    low = text.lower()
    if "facebook" in low and "label" in low:
        # Like the real route: the match is fixed either way; textIsLimitation says whether
        # the line itself still asserts the limitation.
        return {**FB_FIX, "textIsLimitation": "no longer" not in low}
    if "tiktok" in low:
        return TT_OPEN
    return {"status": "unverified", "matches": [], "evidence": ["No recorded limitation matches"]}


class _FakeKynver:
    def __init__(self, *, broken=False):
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
                    if "items" in body:
                        status, payload = 200, {"results": [_verdict(i.get("text") or "") for i in body["items"]]}
                    else:
                        status, payload = 200, _verdict(body.get("text") or body.get("subject") or "")
                elif self.path == f"/api/agent-os/{SLUG}/memory":
                    status, payload = 201, {"ok": True}
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
    base = {"json": False, "all": False, "fix_skills": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_finds_the_held_sentence_not_the_code_fence(tmp_path):
    from plugins.memory.kynver.blockers import find_statements

    texts = [s.text for s in find_statements("r.md", REPORT)]
    assert "Facebook (Sat 10:00) is built but held, because KariAd can't send Meta's AI label to Facebook." in texts
    assert "TikTok carousels are not supported by the publisher yet." in texts
    assert not any("code fence" in t for t in texts)
    assert not any("cover is full size" in t for t in texts)


def test_scan_reports_the_fixed_hold_as_stale_and_never_rewrites_reports(fake, tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN, cmd_scan

    report = tmp_path / "REPORT.md"
    report.write_text(REPORT)
    code = cmd_scan(_args(paths=[str(report)], fix_skills=True), client=_client(fake.url))
    out = capsys.readouterr().out
    assert code == EXIT_CLEAN
    assert "STALE" in out and "fixed by Totalsolutionsync/KariAdV2#398 on 2026-10-16" in out
    assert "OPEN" in out and "Org/App#12" in out
    assert "left as history (not a skill)" in out
    assert report.read_text() == REPORT
    path, body = fake.requests[0]
    assert path == f"/api/agent-os/{SLUG}/memory/limitations/check"
    assert len(body["items"]) == 2


def test_fix_skills_annotates_skills_once_and_keeps_the_original_line(fake, tmp_path, monkeypatch):
    from plugins.memory.kynver import blockers
    from plugins.memory.kynver.cli import cmd_scan

    skills = tmp_path / "skills"
    skill = skills / "social" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Scheduling\n- Hold Facebook for the AI label: KariAd can't send it.\n- Post IG at 11:00.\n")
    monkeypatch.setattr(blockers, "default_skill_roots", lambda: [skills.resolve()])
    for _ in range(2):
        cmd_scan(_args(paths=[str(skills)], fix_skills=True), client=_client(fake.url))
    lines = skill.read_text().splitlines()
    assert lines[1] == "- Hold Facebook for the AI label: KariAd can't send it."
    assert lines[2].startswith("> **Kynver: FIXED**") and "Totalsolutionsync/KariAdV2#398 on 2026-10-16" in lines[2]
    assert sum(1 for line in lines if line.startswith("> **Kynver: FIXED**")) == 1
    assert lines[3] == "- Post IG at 11:00."


def test_check_brief_exits_nonzero_listing_the_stale_blocker(fake, tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN, EXIT_STALE, cmd_check_brief

    brief = tmp_path / "brief.md"
    brief.write_text("# Worker brief\nSchedule the Short on all platforms, but hold Facebook for the AI label.\n")
    assert cmd_check_brief(_args(brief=str(brief)), client=_client(fake.url)) == EXIT_STALE
    out = capsys.readouterr().out
    assert "1 stale blocker(s)" in out and "brief.md:2" in out

    clean = tmp_path / "clean.md"
    clean.write_text("Schedule the Short on all five platforms.\nTikTok carousels are not supported yet.\n")
    assert cmd_check_brief(_args(brief=str(clean)), client=_client(fake.url)) == EXIT_CLEAN


def test_lines_that_already_reflect_the_fix_or_only_quote_it_are_not_stale(fake, tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_CLEAN, cmd_check_brief

    brief = tmp_path / "brief.md"
    brief.write_text(
        "Facebook is no longer held for the AI label.\n"
        'Background: Forge said "Facebook may be held because KariAd can\'t send the AI label".\n'
    )
    assert cmd_check_brief(_args(brief=str(brief)), client=_client(fake.url)) == EXIT_CLEAN
    sent = [item["text"] for _, body in fake.requests for item in body["items"]]
    assert sent == ["Facebook is no longer held for the AI label."]


def test_check_brief_says_so_when_kynver_cannot_be_asked(tmp_path, capsys):
    from plugins.memory.kynver.cli import EXIT_UNAVAILABLE, cmd_check_brief

    broken = _FakeKynver(broken=True)
    try:
        brief = tmp_path / "brief.md"
        brief.write_text("Hold Facebook for the AI label.\n")
        assert cmd_check_brief(_args(brief=str(brief)), client=_client(broken.url)) == EXIT_UNAVAILABLE
    finally:
        broken.close()
    assert "Could not check limitations with Kynver" in capsys.readouterr().err


def test_cli_tree_parses_both_subcommands():
    from plugins.memory.kynver.cli import kynver_command, register_cli

    parser = argparse.ArgumentParser(prog="hermes kynver")
    register_cli(parser)
    scan = parser.parse_args(["blockers", "scan", "a.md", "dir", "--fix-skills"])
    assert (scan.kynver_command, scan.blockers_command, scan.paths, scan.fix_skills) == ("blockers", "scan", ["a.md", "dir"], True)
    brief = parser.parse_args(["blockers", "check-brief", "b.md"])
    assert (brief.blockers_command, brief.brief, brief.func) == ("check-brief", "b.md", kynver_command)


def test_recall_renders_fixed_limitations_as_history_and_flags_unverified():
    from plugins.memory.kynver.context import fit_whole_items

    fixed = {"slug": "fb", "content": "KariAd can't send Meta's AI label; hold FB rows",
             "limitation": {"status": "fixed", "fixedBy": "Org/KariAdV2#398", "fixedAt": "2026-10-16T18:41:28Z"}}
    unverified = {"slug": "x", "content": "[UNVERIFIED blocker noted 2026-10-01 with no linked fix: recheck before relying on it.] X threads cap at 5",
                  "limitation": {"status": "unverified"}, "verificationState": "unverified"}
    md = fit_whole_items([fixed, unverified], token_budget=2000, max_index_items=5).markdown
    fb_line = next(line for line in md.splitlines() if "[fb]" in line)
    assert fb_line.startswith("- [FIXED by Org/KariAdV2#398 on 2026-10-16")
    assert "not a current blocker" in fb_line
    x_line = next(line for line in md.splitlines() if "[x]" in line)
    assert "recheck before relying on it" in x_line


@pytest.fixture
def provider(fake):
    from plugins.memory import load_memory_provider

    discovered = load_memory_provider("kynver", register_skills=False)
    assert discovered is not None
    return type(discovered)(client=_client(fake.url))


def test_limitation_check_tool_and_fixref_on_writes(fake, provider):
    names = {s["name"] for s in provider.get_tool_schemas()}
    assert "kynver_memory_limitation_check" in names
    result = json.loads(provider.handle_tool_call("kynver_memory_limitation_check", {"text": "hold Facebook for the AI label"}))
    assert result["success"] is True and result["status"] == "fixed"
    assert fake.requests[-1] == (f"/api/agent-os/{SLUG}/memory/limitations/check", {"text": "hold Facebook for the AI label"})

    provider.handle_tool_call(
        "kynver_memory_write",
        {"content": "KariAd can't post IG carousels", "fixRef": "Totalsolutionsync/KariAdV2#401"},
    )
    path, body = fake.requests[-1]
    assert path == f"/api/agent-os/{SLUG}/memory"
    assert body["metadata"]["fixRef"] == "Totalsolutionsync/KariAdV2#401"
