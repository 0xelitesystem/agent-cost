"""agent-cost: read a finished agent session and explain where the money went.

Usage:
  agent-cost report <transcript.jsonl | session-id-prefix | latest> [options]
  agent-cost top [--project NAME] [--limit N]
  agent-cost list [--project NAME] [--limit N]

Options:
  --project NAME    only consider transcripts whose project folder matches
  --json            emit machine-readable JSON instead of the terminal report
  --md FILE         also write a Markdown report to FILE
  --prices FILE     override the built-in pricing table with a JSON file
  --fail-over USD   exit 1 if the estimated cost exceeds USD (gate/alert)
  --no-color        disable ANSI colors

No proxy, no API key, no setup. It reads the transcript you already have.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .cost import analyze
from .loops import detect_loops
from .models import CostResult
from .parser import discover_transcripts, parse_transcript, resolve_target
from .pricing import load_prices
from .report import _usd, render_json, render_markdown, render_terminal


def analyze_cost(target: str, project: str | None = None,
                 prices: dict | None = None, prices_file: str | None = None
                 ) -> CostResult:
    """Library entry point: analyze a transcript and return the CostResult.

    `prices` is an already-loaded table; `prices_file` is a path to load one
    from. Either may be supplied (prices wins); both omitted uses the defaults.
    """
    if prices is None and prices_file:
        prices = load_prices(prices_file)
    path = resolve_target(target, project)
    session = parse_transcript(path)
    result = analyze(session, prices=prices)
    result.loops = detect_loops(session, prices=prices)
    return result


def _load_prices_arg(args: argparse.Namespace) -> dict | None:
    return load_prices(args.prices) if getattr(args, "prices", None) else None


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        prices = _load_prices_arg(args)
        result = analyze_cost(args.target, args.project, prices=prices)
    except FileNotFoundError as exc:
        print(f"agent-cost: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"agent-cost: could not load prices: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(render_json(result))
    else:
        color = False if args.no_color else None
        print(render_terminal(result, color=color))

    if args.md:
        Path(args.md).write_text(render_markdown(result), encoding="utf-8")
        if not args.json:
            print(f"  markdown report written to {args.md}\n")

    # gate: over-budget sessions exit non-zero so this can run in CI / alerts
    if args.fail_over is not None and result.total_cost > args.fail_over:
        return 1
    return 0


def _cmd_top(args: argparse.Namespace) -> int:
    """Rank recent sessions by estimated cost: 'which cost me the most?'."""
    transcripts = discover_transcripts(args.project)
    if not transcripts:
        print("no transcripts found under ~/.claude/projects", file=sys.stderr)
        return 2

    try:
        prices = _load_prices_arg(args)
    except (ValueError, OSError) as exc:
        print(f"agent-cost: could not load prices: {exc}", file=sys.stderr)
        return 2

    rows = []
    # scan a generous slice; ranking N sessions means parsing N transcripts
    for path in transcripts[: max(args.limit * 4, args.limit)]:
        try:
            session = parse_transcript(path)
        except (OSError, ValueError):
            continue
        result = analyze(session, prices=prices)
        result.loops = detect_loops(session, prices=prices)
        rows.append((result.total_cost, path, result))
    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[: args.limit]

    if args.json:
        import json
        print(json.dumps([{
            "transcript": str(p),
            "session": p.stem[:8],
            "total_cost": round(cost, 6),
            "loops": len(res.loops),
        } for cost, p, res in rows], indent=2))
        return 0

    print()
    print(f"  {'COST':>10}  {'LOOPS':>5}  SESSION   PROJECT")
    for cost, path, res in rows:
        loop_flag = f"{len(res.loops)}" if res.loops else "-"
        print(f"  {_usd(cost):>10}  {loop_flag:>5}  {path.stem[:8]}  "
              f"{path.parent.name}")
    print()
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    transcripts = discover_transcripts(args.project)[: args.limit]
    if not transcripts:
        print("no transcripts found under ~/.claude/projects", file=sys.stderr)
        return 2
    for path in transcripts:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        size_kb = path.stat().st_size // 1024
        print(f"{path.stem[:8]}  {mtime:%Y-%m-%d %H:%M}  {size_kb:>6} KB  "
              f"{path.parent.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-cost",
        description="Read a finished agent session and explain where the money went.",
    )
    parser.add_argument("--version", action="version",
                        version=f"agent-cost {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="full cost report for one session")
    report.add_argument("target",
                        help="transcript path, session-id prefix, or 'latest'")
    report.add_argument("--project", help="filter session discovery by project name")
    report.add_argument("--json", action="store_true", help="JSON output")
    report.add_argument("--md", metavar="FILE", help="write Markdown report to FILE")
    report.add_argument("--prices", metavar="FILE",
                        help="JSON pricing table to override the defaults")
    report.add_argument("--fail-over", type=float, metavar="USD",
                        help="exit 1 if estimated cost exceeds USD (gate)")
    report.add_argument("--no-color", action="store_true", help="plain output")
    report.set_defaults(func=_cmd_report)

    top = sub.add_parser("top", help="rank recent sessions by estimated cost")
    top.add_argument("--project", help="filter by project folder name")
    top.add_argument("--limit", type=int, default=15)
    top.add_argument("--prices", metavar="FILE", help="JSON pricing table override")
    top.add_argument("--json", action="store_true", help="JSON output")
    top.set_defaults(func=_cmd_top)

    lst = sub.add_parser("list", help="list recent session transcripts")
    lst.add_argument("--project", help="filter by project folder name")
    lst.add_argument("--limit", type=int, default=15)
    lst.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often default to cp1252, which can't print ⚠/×/ - .
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
