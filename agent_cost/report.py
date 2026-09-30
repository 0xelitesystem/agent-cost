"""Render a CostResult: ANSI terminal report, Markdown, or JSON."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .models import Aggregate, CostResult

# Text from a transcript is untrusted: a command, path, url, model id or cwd
# can carry escape sequences that would drive the terminal (rewrite the
# clipboard, redraw the report, retitle the window) or Markdown that would
# forge headings and links in a shared report. It is made inert where it is
# written out.
_TERMINAL_UNSAFE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_LINE_BREAKING = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069"
    r"\u2028\u2029]+")
# '[', ']', '|' and '`', plus a backslash only where it would escape the next
# character (before ASCII punctuation), so a Windows path is written as is.
_MD_PUNCTUATION = re.compile(r"(\\(?=[!-/:-@\[-\x60{-~])|[\[\]|`])")
_BACKTICK_RUN = re.compile(r"`+")


def _escape_char(match: re.Match) -> str:
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def _safe(value) -> str:
    """Transcript text for the terminal: control characters (tab and newline
    aside), DEL, C1 and bidi controls are shown as escapes, never obeyed."""
    return _TERMINAL_UNSAFE.sub(_escape_char, str(value))


def _md_line(value) -> str:
    """Transcript text on one Markdown line: line breaks and other control
    characters collapse to a single space."""
    return _LINE_BREAKING.sub(" ", str(value)).strip()


def _md_text(value) -> str:
    """Transcript text as inert Markdown prose on one line: '<' and '>' become
    entities so no HTML tag or autolink can form, and the characters that
    build links and images, split table cells or open and close code spans
    are backslash-escaped. A backtick is one of them: a lone one in a tool
    name would otherwise pair with a backtick inside the loop signature's
    code span and end that span early. '&', '_' and '*' are left alone so
    ordinary urls and paths render unchanged."""
    text = _md_line(value).replace("<", "&lt;").replace(">", "&gt;")
    return _MD_PUNCTUATION.sub(r"\\\1", text)


def _md_code(value, in_table: bool = False) -> str:
    """Transcript text as a Markdown code span that nothing inside can close:
    the fence is one backtick longer than the longest run in the text."""
    text = _md_line(value)
    if in_table:
        text = text.replace("|", "\\|")
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _paint(text: str, *styles: str, enabled: bool = True) -> str:
    if not enabled or not styles:
        return text
    return "".join(styles) + text + _RESET


def _usd(amount: float) -> str:
    """Money formatting that doesn't lie about sub-cent amounts."""
    if amount and abs(amount) < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def _tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def render_terminal(result: CostResult, color: bool | None = None) -> str:
    color = _colors_enabled() if color is None else color
    session = result.session
    lines: list[str] = []
    out = lines.append

    title = _safe(session.slug or Path(session.path).stem[:12])
    tool_calls = session.tool_calls()
    out("")
    out(_paint("  agent-cost", _BOLD, _CYAN, enabled=color)
        + _paint(", where the tokens and money went", _DIM, enabled=color))
    out(_paint(f"  session {title} · {len(session.events)} events"
               + (f" · {_safe(session.cwd)}" if session.cwd else ""),
               _DIM, enabled=color))
    out("")

    # ---- summary ----------------------------------------------------------
    cost_style = _GREEN if result.total_cost < 1 else (
        _YELLOW if result.total_cost < 10 else _RED)
    out(f"  {_paint('TOTAL EST. COST', _BOLD, enabled=color)}  "
        + _paint(_usd(result.total_cost), _BOLD, cost_style, enabled=color)
        + (_paint("  (some models unpriced, see below)", _YELLOW, enabled=color)
           if result.has_unknown_rates else ""))
    u = result.total_usage
    out(_paint(
        f"  {_tok(u.total)} tokens · {_tok(u.input_tokens)} in · "
        f"{_tok(u.output_tokens)} out · {_tok(u.cache_read_input_tokens)} cache-read",
        _DIM, enabled=color))
    out(_paint(
        f"  {_duration(result.duration_seconds)} · "
        f"{result.responses} API responses · "
        f"{len(tool_calls)} tool calls",
        _DIM, enabled=color))
    if result.duplicate_lines_dropped:
        out(_paint(
            f"  {result.duplicate_lines_dropped} transcript lines repeated a "
            "response and were counted once", _DIM, enabled=color))
    out("")

    # ---- loop warnings up top (the headline) ------------------------------
    if result.loops:
        out(_paint("  ⚠ RUNAWAY LOOPS DETECTED", _BOLD, _RED, enabled=color))
        for loop in result.loops:
            tag = "stuck retry, all errored" if loop.all_errored else "repeated action"
            out(f"  {_paint('×' + str(loop.count), _RED, _BOLD, enabled=color)} "
                f"{_safe(loop.tool_name)}, {tag}")
            out(_paint(f"      {_safe(loop.signature)}", _DIM, enabled=color))
            out(_paint(
                f"      events {loop.start_index} to {loop.end_index} · "
                f"~{_tok(loop.wasted_tokens)} tokens wasted · "
                f"~{_usd(loop.wasted_cost)} burned",
                _DIM, enabled=color))
        out("")

    # ---- cost by model ----------------------------------------------------
    if result.by_model:
        out(_paint("  COST BY MODEL", _BOLD, enabled=color))
        for mc in result.by_model:
            flag = "" if mc.rate_known else _paint(
                " (unpriced, tokens only)", _YELLOW, enabled=color)
            out(f"  {_usd(mc.total_cost):>10}  {_safe(mc.model)}{flag}")
            out(_paint(
                f"             {_tok(mc.usage.input_tokens)} in · "
                f"{_tok(mc.usage.output_tokens)} out · "
                f"{_tok(mc.usage.cache_read_input_tokens)} cache-read",
                _DIM, enabled=color))
        out("")

    # ---- top expensive turns ----------------------------------------------
    if result.top_turns:
        out(_paint("  TOP EXPENSIVE TURNS", _BOLD, enabled=color))
        for t in result.top_turns:
            out(f"  {_usd(t.cost):>10}  event {t.event_index}  "
                f"{_paint(_safe(t.model), _DIM, enabled=color)}  "
                + _paint(f"({_tok(t.usage.output_tokens)} out)", _DIM, enabled=color))
        out("")

    # ---- context bloat offenders ------------------------------------------
    if result.bloat_offenders:
        out(_paint("  CONTEXT BLOAT OFFENDERS", _BOLD, enabled=color)
            + _paint("  (big tool outputs fed back into context)", _DIM, enabled=color))
        for b in result.bloat_offenders:
            out(f"  {_tok(b.approx_tokens):>8} tok  {_safe(b.tool_name)}  "
                + _paint(f"event {b.event_index}", _DIM, enabled=color))
            out(_paint(f"             {_safe(b.label)}", _DIM, enabled=color))
        out("")

    # ---- cache efficiency -------------------------------------------------
    c = result.cache
    out(_paint("  CACHE EFFICIENCY", _BOLD, enabled=color))
    out(f"  hit ratio {c.hit_ratio * 100:.0f}%  "
        + _paint(f"({_tok(c.cache_read_tokens)} read / "
                 f"{_tok(c.cache_creation_tokens)} written / "
                 f"{_tok(c.fresh_input_tokens)} fresh)", _DIM, enabled=color))
    out(_paint(
        f"  ~{_usd(c.estimated_savings)} saved by cache reads",
        _DIM, enabled=color))
    out("")

    if result.unpriced_models:
        out(_paint("  UNPRICED MODELS", _BOLD, _YELLOW, enabled=color)
            + _paint("  (tokens counted, no rate in the table, nothing guessed)",
                     _DIM, enabled=color))
        for model, count in sorted(result.unpriced_models.items()):
            out(f"  {count:>10}  {_safe(model)}")
        out(_paint("  supply rates with --prices FILE to cost these",
                   _DIM, enabled=color))
        out("")

    return "\n".join(lines)


def render_total(rolled: Aggregate, transcripts: int, duplicate_lines: int,
                 color: bool | None = None) -> str:
    """Whole-machine view: every transcript, every response counted once."""
    color = _colors_enabled() if color is None else color
    lines: list[str] = []
    out = lines.append
    u = rolled.total_usage
    out("")
    out(_paint("  agent-cost", _BOLD, _CYAN, enabled=color)
        + _paint(f", {transcripts:,} transcripts, "
                 f"{rolled.responses:,} API responses", _DIM, enabled=color))
    out("")
    out(f"  {_paint('TOTAL EST. COST', _BOLD, enabled=color)}  "
        + _paint(_usd(rolled.total_cost), _BOLD, _CYAN, enabled=color)
        + (_paint("  (some models unpriced)", _YELLOW, enabled=color)
           if rolled.has_unknown_rates else ""))
    out(_paint(
        f"  {_tok(u.total)} tokens · {_tok(u.input_tokens)} in · "
        f"{_tok(u.output_tokens)} out · "
        f"{_tok(u.cache_creation_input_tokens)} cache-write · "
        f"{_tok(u.cache_read_input_tokens)} cache-read", _DIM, enabled=color))
    if duplicate_lines:
        out(_paint(f"  {duplicate_lines:,} repeated transcript lines counted once",
                   _DIM, enabled=color))
    out("")
    if rolled.by_model:
        out(_paint("  COST BY MODEL", _BOLD, enabled=color))
        for mc in rolled.by_model:
            flag = "" if mc.rate_known else _paint(" (unpriced, tokens only)",
                                                   _YELLOW, enabled=color)
            out(f"  {_usd(mc.total_cost):>12}  {_safe(mc.model)}{flag}")
            out(_paint(
                f"               {_tok(mc.usage.input_tokens)} in · "
                f"{_tok(mc.usage.output_tokens)} out · "
                f"{_tok(mc.usage.cache_read_input_tokens)} cache-read",
                _DIM, enabled=color))
        out("")
    return "\n".join(lines)


def total_json(rolled: Aggregate, transcripts: int, duplicate_lines: int) -> str:
    return json.dumps({
        "transcripts": transcripts,
        "responses": rolled.responses,
        "duplicate_lines_dropped": duplicate_lines,
        "total_cost": round(rolled.total_cost, 6),
        "total_usage": _usage_dict(rolled.total_usage),
        "has_unknown_rates": rolled.has_unknown_rates,
        "unpriced_models": rolled.unpriced_models,
        "by_model": [{
            "model": m.model,
            "rate_known": m.rate_known,
            "total_cost": round(m.total_cost, 6),
            "usage": _usage_dict(m.usage),
        } for m in rolled.by_model],
    }, indent=2)


# ---- JSON ----------------------------------------------------------------


def _usage_dict(u) -> dict:
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": u.cache_creation_input_tokens,
        "cache_creation_5m": u.cache_creation_5m,
        "cache_creation_1h": u.cache_creation_1h,
        "cache_read_input_tokens": u.cache_read_input_tokens,
        "total": u.total,
    }


def render_json(result: CostResult) -> str:
    return json.dumps({
        "transcript": result.session.path,
        "session_id": result.session.session_id,
        "cwd": result.session.cwd,
        "total_cost": round(result.total_cost, 6),
        "total_usage": _usage_dict(result.total_usage),
        "duration_seconds": result.duration_seconds,
        "responses": result.responses,
        "duplicate_lines_dropped": result.duplicate_lines_dropped,
        "tool_calls": len(result.session.tool_calls()),
        "has_unknown_rates": result.has_unknown_rates,
        "unpriced_models": result.unpriced_models,
        "by_model": [{
            "model": m.model,
            "rate_known": m.rate_known,
            "total_cost": round(m.total_cost, 6),
            "input_cost": round(m.input_cost, 6),
            "output_cost": round(m.output_cost, 6),
            "cache_write_cost": round(m.cache_write_cost, 6),
            "cache_read_cost": round(m.cache_read_cost, 6),
            "usage": _usage_dict(m.usage),
        } for m in result.by_model],
        "top_turns": [{
            "event_index": t.event_index,
            "model": t.model,
            "cost": round(t.cost, 6),
            "rate_known": t.rate_known,
            "usage": _usage_dict(t.usage),
        } for t in result.top_turns],
        "bloat_offenders": [{
            "event_index": b.event_index,
            "tool_name": b.tool_name,
            "label": b.label,
            "output_chars": b.output_chars,
            "approx_tokens": b.approx_tokens,
        } for b in result.bloat_offenders],
        "cache": {
            "hit_ratio": round(result.cache.hit_ratio, 4),
            "cache_read_tokens": result.cache.cache_read_tokens,
            "cache_creation_tokens": result.cache.cache_creation_tokens,
            "fresh_input_tokens": result.cache.fresh_input_tokens,
            "estimated_savings": round(result.cache.estimated_savings, 6),
            "estimated_left_on_table": round(result.cache.estimated_left_on_table, 6),
        },
        "loops": [{
            "signature": loop.signature,
            "tool_name": loop.tool_name,
            "count": loop.count,
            "start_index": loop.start_index,
            "end_index": loop.end_index,
            "all_errored": loop.all_errored,
            "wasted_tokens": loop.wasted_tokens,
            "wasted_cost": round(loop.wasted_cost, 6),
        } for loop in result.loops],
    }, indent=2)


# ---- Markdown ------------------------------------------------------------


def render_markdown(result: CostResult) -> str:
    c = result.cache
    lines = [
        "# agent-cost report",
        "",
        f"- **Transcript:** {_md_code(Path(result.session.path).name)}",
        f"- **Project:** {_md_code(result.session.cwd or 'unknown')}",
        f"- **Total est. cost:** {_usd(result.total_cost)}"
        + ("  (some models unpriced, tokens counted)"
           if result.has_unknown_rates else ""),
        f"- **Tokens:** {result.total_usage.total:,} "
        f"({result.total_usage.input_tokens:,} in / "
        f"{result.total_usage.output_tokens:,} out)",
        f"- **Duration:** {_duration(result.duration_seconds)} · "
        f"{result.responses} API responses · "
        f"{len(result.session.tool_calls())} tool calls",
        "",
    ]

    if result.loops:
        lines += ["## ⚠ Runaway loops", ""]
        for loop in result.loops:
            tag = "stuck retry (all errored)" if loop.all_errored else "repeated action"
            lines.append(
                f"- **×{loop.count} {_md_text(loop.tool_name)}**, {tag} · "
                f"events {loop.start_index} to {loop.end_index} · "
                f"~{loop.wasted_tokens:,} tokens / {_usd(loop.wasted_cost)} wasted  "
                f"\n  {_md_code(loop.signature)}")
        lines.append("")

    lines += ["## Cost by model", "", "| Model | Cost | In | Out | Cache-read |",
              "|---|---|---|---|---|"]
    for m in result.by_model:
        flag = "" if m.rate_known else " (unpriced, tokens only)"
        lines.append(
            f"| {_md_code(m.model, in_table=True)}{flag} | {_usd(m.total_cost)} | "
            f"{m.usage.input_tokens:,} | {m.usage.output_tokens:,} | "
            f"{m.usage.cache_read_input_tokens:,} |")
    lines.append("")

    if result.top_turns:
        lines += ["## Top expensive turns", "", "| Event | Model | Cost |",
                  "|---|---|---|"]
        for t in result.top_turns:
            lines.append(f"| {t.event_index} | {_md_code(t.model, in_table=True)} | "
                         f"{_usd(t.cost)} |")
        lines.append("")

    if result.bloat_offenders:
        lines += ["## Context bloat offenders", "",
                  "| Event | Tool | Approx tokens | Source |",
                  "|---|---|---|---|"]
        for b in result.bloat_offenders:
            lines.append(f"| {b.event_index} | {_md_text(b.tool_name)} | "
                         f"{b.approx_tokens:,} | {_md_text(b.label)} |")
        lines.append("")

    lines += [
        "## Cache efficiency", "",
        f"- Hit ratio: {c.hit_ratio * 100:.0f}%",
        f"- Read: {c.cache_read_tokens:,} · Written: {c.cache_creation_tokens:,} "
        f"· Fresh: {c.fresh_input_tokens:,}",
        f"- Estimated savings from cache reads: {_usd(c.estimated_savings)}",
        "",
    ]
    return "\n".join(lines)
