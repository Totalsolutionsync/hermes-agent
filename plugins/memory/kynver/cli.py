"""``hermes kynver`` subcommands.

``hermes kynver blockers scan <paths...>``    find limitation lines in skills, briefs and worker
                                              reports whose fix has shipped (Kynver decides).
                                              ``--fix-skills`` appends a FIXED note under stale
                                              lines in skills only; reports are never rewritten.
``hermes kynver blockers check-brief <file>``  guard to run before launching a worker: exits 1
                                              listing stale blockers in the brief, 0 when clean,
                                              2 when Kynver can't be asked.
"""

from __future__ import annotations

import json
import sys
from typing import Any, List, Optional

EXIT_CLEAN = 0
EXIT_STALE = 1
EXIT_UNAVAILABLE = 2


def _client() -> Any:
    from plugins.memory.kynver.agentos_bridge import KynverAgentOSClient

    return KynverAgentOSClient()


def _findings_json(findings: List[Any]) -> str:
    return json.dumps(
        [
            {
                "path": f.statement.path,
                "line": f.statement.line_no,
                "text": f.statement.text,
                "status": f.status,
                "fixedBy": f.fixed_by,
                "fixedAt": f.fixed_at,
                "matches": f.matches,
                "evidence": f.evidence,
            }
            for f in findings
        ],
        indent=2,
    )


def _check(paths: List[str], client: Any) -> Optional[List[Any]]:
    from plugins.memory.kynver.blockers import check_statements, collect_statements

    statements = collect_statements(paths)
    if not statements:
        return []
    try:
        return check_statements(client, statements)
    except Exception as exc:  # Kynver down, auth, or an old server without the route
        print(f"  Could not check limitations with Kynver: {exc}", file=sys.stderr)
        return None


def cmd_scan(args: Any, client: Any = None) -> int:
    from plugins.memory.kynver.blockers import (
        annotate_skill_file,
        default_skill_roots,
        describe,
        is_under,
    )

    findings = _check(list(args.paths), client or _client())
    if findings is None:
        return EXIT_UNAVAILABLE
    shown = findings if args.all else [f for f in findings if f.stale or f.status == "open"]
    if args.json:
        print(_findings_json(shown))
    else:
        stale = [f for f in findings if f.stale]
        print(f"  Checked {len(findings)} limitation statement(s): {len(stale)} stale (fix shipped).")
        for f in shown:
            print(f"  {describe(f)}")
    if args.fix_skills:
        roots = default_skill_roots()
        added = 0
        for path in sorted({f.statement.path for f in findings if f.stale}):
            if is_under(path, roots):
                added += annotate_skill_file(path, findings)
            else:
                print(f"  left as history (not a skill): {path}")
        print(f"  Annotated {added} stale line(s) in skills.")
    return EXIT_CLEAN


def cmd_check_brief(args: Any, client: Any = None) -> int:
    from plugins.memory.kynver.blockers import describe

    findings = _check([args.brief], client or _client())
    if findings is None:
        return EXIT_UNAVAILABLE
    stale = [f for f in findings if f.stale]
    if args.json:
        print(_findings_json(stale))
    elif stale:
        print(f"  {len(stale)} stale blocker(s) in {args.brief}: the fix already shipped. Update the brief before launching.")
        for f in stale:
            print(f"  {describe(f)}")
    else:
        print(f"  No stale blockers in {args.brief} ({len(findings)} limitation statement(s) checked).")
    return EXIT_STALE if stale else EXIT_CLEAN


def kynver_command(args: Any) -> None:
    group = getattr(args, "kynver_command", None)
    sub = getattr(args, "blockers_command", None)
    if group != "blockers" or sub not in ("scan", "check-brief"):
        print("  Usage: hermes kynver blockers scan <paths...> [--fix-skills] | check-brief <brief.md>")
        sys.exit(EXIT_UNAVAILABLE)
    code = cmd_scan(args) if sub == "scan" else cmd_check_brief(args)
    sys.exit(code)


def register_cli(subparser) -> None:
    """Build the ``hermes kynver`` argparse tree."""
    groups = subparser.add_subparsers(dest="kynver_command")
    blockers = groups.add_parser("blockers", help="Find limitation notes whose fix has shipped")
    subs = blockers.add_subparsers(dest="blockers_command")

    scan = subs.add_parser("scan", help="Scan skills, briefs and worker reports for stale limitation lines")
    scan.add_argument("paths", nargs="+", help="Files or directories (*.md, *.txt)")
    scan.add_argument("--fix-skills", action="store_true", help="Append a FIXED note under stale lines in skills only")
    scan.add_argument("--all", action="store_true", help="Also list unverified statements")
    scan.add_argument("--json", action="store_true", help="Machine-readable output")

    brief = subs.add_parser("check-brief", help="Exit 1 if a worker brief repeats a blocker whose fix shipped")
    brief.add_argument("brief", help="Brief file")
    brief.add_argument("--json", action="store_true", help="Machine-readable output")
    subparser.set_defaults(func=kynver_command)
