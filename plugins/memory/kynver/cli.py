"""``hermes kynver`` subcommands.

``hermes kynver blockers scan <paths...>``    find limitation lines in skills, briefs and worker
                                              reports whose fix has shipped (Kynver decides).
                                              ``--fix-skills`` appends a FIXED note under stale
                                              lines in skills only; reports are never rewritten.
``hermes kynver blockers check-brief <file>``  guard to run before launching a worker: exits 1
                                              listing stale blockers in the brief, 0 when clean,
                                              2 when Kynver can't be asked. Also flags lines that
                                              ask Will to do something by hand that memory says
                                              is automated, and lines repeating a replaced note.
``hermes kynver memory lint-skill <dir>``     report-only: a skill whose files contradict each
                                              other (one says automated, another says do it by
                                              hand) or contradict current memory. Exits 1 with
                                              findings, 0 clean, 2 when Kynver can't be asked
                                              (the file-vs-file lint still prints).
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


def _guard(paths: List[str], client: Any) -> Optional[List[Any]]:
    """Manual-step / replaced-note lines checked against Kynver. [] when the server predates the route."""
    from plugins.memory.kynver.manual_steps import RouteMissing, check_guard_statements, find_guard_statements
    from plugins.memory.kynver.blockers import iter_files

    statements = []
    for f in iter_files(paths):
        try:
            statements.extend(find_guard_statements(str(f), f.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    if not statements:
        return []
    try:
        return check_guard_statements(client, statements)
    except RouteMissing:
        print("  Kynver has no manual-step check yet; skipped the by-hand / replaced-note lines.", file=sys.stderr)
        return []
    except Exception as exc:
        print(f"  Could not check by-hand steps with Kynver: {exc}", file=sys.stderr)
        return None


def _guard_json(findings: List[Any]) -> List[dict]:
    return [
        {
            "path": g.statement.path,
            "line": g.statement.line_no,
            "text": g.statement.text,
            "status": "replaced" if g.replaced else "automated",
            "automation": g.automation,
            "replaced": g.replaced,
            "retiredManualSteps": g.retired_manual_steps,
            "advice": g.advice,
        }
        for g in findings
    ]


def cmd_check_brief(args: Any, client: Any = None) -> int:
    from plugins.memory.kynver.blockers import describe
    from plugins.memory.kynver.manual_steps import describe_guard

    client = client or _client()
    findings = _check([args.brief], client)
    if findings is None:
        return EXIT_UNAVAILABLE
    guard = _guard([args.brief], client)
    if guard is None:
        return EXIT_UNAVAILABLE
    stale = [f for f in findings if f.stale]
    outdated = [g for g in guard if g.stale]
    if args.json:
        print(json.dumps({"blockers": json.loads(_findings_json(stale)), "manualSteps": _guard_json(outdated)}, indent=2))
    elif stale or outdated:
        if stale:
            print(f"  {len(stale)} stale blocker(s) in {args.brief}: the fix already shipped. Update the brief before launching.")
            for f in stale:
                print(f"  {describe(f)}")
        if outdated:
            print(f"  {len(outdated)} outdated line(s) in {args.brief}: memory says these are automated or were replaced. Update the brief; don't ask Will to do them by hand.")
            for g in outdated:
                print(f"  {describe_guard(g)}")
    else:
        print(
            f"  No stale blockers or outdated by-hand steps in {args.brief} "
            f"({len(findings)} limitation statement(s), {len(guard)} by-hand/advice line(s) checked)."
        )
    return EXIT_STALE if (stale or outdated) else EXIT_CLEAN


def cmd_lint_skill(args: Any, client: Any = None) -> int:
    from plugins.memory.kynver.manual_steps import describe_guard, lint_skill

    report = lint_skill(args.skill_dir, client or _client())
    internal, memory = report["internal"], report["memory"]
    if args.json:
        print(json.dumps({
            "skill": report["skill"],
            "files": report["files"],
            "internal": [
                {"manual": {"path": c.manual.path, "line": c.manual.line_no, "text": c.manual.text},
                 "automated": {"path": c.automated.path, "line": c.automated.line_no, "text": c.automated.text},
                 "shared": c.shared}
                for c in internal
            ],
            "memory": _guard_json(memory),
            "memoryChecked": report["memoryChecked"],
            "memoryError": report["memoryError"],
        }, indent=2))
    else:
        print(f"  Skill {report['skill']}: {len(report['files'])} file(s). Dry run: nothing is changed.")
        if internal:
            print(f"  {len(internal)} line(s) tell Will to do by hand what another line says is automated:")
            for c in internal:
                print(f"  CONFLICT {c.manual.path}:{c.manual.line_no}  vs  {c.automated.path}:{c.automated.line_no}  (shared: {', '.join(c.shared)})")
                print(f"       by hand:   \"{c.manual.text}\"")
                print(f"       automated: \"{c.automated.text}\"")
        if memory:
            print(f"  {len(memory)} line(s) contradict current memory:")
            for g in memory:
                print(f"  {describe_guard(g)}")
        if report["memoryError"]:
            print(f"  Could not compare with Kynver memory: {report['memoryError']}", file=sys.stderr)
        if not internal and not memory:
            print("  No contradictions found.")
    if internal or memory:
        return EXIT_STALE
    return EXIT_UNAVAILABLE if report["memoryError"] else EXIT_CLEAN


def kynver_command(args: Any) -> None:
    group = getattr(args, "kynver_command", None)
    if group == "memory" and getattr(args, "memory_command", None) == "lint-skill":
        sys.exit(cmd_lint_skill(args))
    sub = getattr(args, "blockers_command", None)
    if group != "blockers" or sub not in ("scan", "check-brief"):
        print("  Usage: hermes kynver blockers scan <paths...> [--fix-skills] | check-brief <brief.md>")
        print("         hermes kynver memory lint-skill <skill-dir>")
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

    memory = groups.add_parser("memory", help="Check skills against Kynver memory")
    memory_subs = memory.add_subparsers(dest="memory_command")
    lint = memory_subs.add_parser("lint-skill", help="Report a skill whose files contradict each other or current memory (dry run)")
    lint.add_argument("skill_dir", help="Skill directory")
    lint.add_argument("--json", action="store_true", help="Machine-readable output")
    subparser.set_defaults(func=kynver_command)
