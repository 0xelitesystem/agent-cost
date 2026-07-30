"""Parse Claude Code session transcripts (JSONL) into a Session.

Format notes (observed against Claude Code 2.x transcripts):
- Each line is a JSON object with a top-level "type".
- "assistant" records carry message.content (a list of blocks: type=="text"
  is prose, type=="tool_use" is a tool invocation with id/name/input) AND
  the part agent-cost cares about: message.model and message.usage. Usage
  is recorded PER assistant record; a single logical turn can be split across
  several records, so we keep a USAGE event per record and let cost.py sum.
- "user" records carry tool results: message.content blocks with
  type=="tool_result" reference the tool_use id and include is_error. A
  sibling top-level "toolUseResult" holds richer data (stdout/stderr dict on
  success, or an "Error: Exit code N" string on failure). The size of that
  fed-back text is what bloats later prompts, so we measure it (chars).
- Other record types (system, attachment, file-history-snapshot, ...) carry
  no cost signal and are skipped.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .models import Event, EventKind, Session, Usage

_EXIT_CODE_RE = re.compile(r"[Ee]xit code:? (\d+)")


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _parse_usage(message: dict) -> Usage | None:
    """Pull message.usage into a Usage; missing fields are zero, not None."""
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None

    def _int(key: str) -> int:
        val = raw.get(key)
        return val if isinstance(val, int) else 0

    return Usage(
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cache_creation_input_tokens=_int("cache_creation_input_tokens"),
        cache_read_input_tokens=_int("cache_read_input_tokens"),
    )


def _result_text(block: dict, tool_use_result) -> str:
    """Flatten a tool_result's content (string or block list) to text."""
    parts: list[str] = []
    content = block.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
    if isinstance(tool_use_result, dict):
        for key in ("stdout", "stderr"):
            val = tool_use_result.get(key)
            if isinstance(val, str) and val and val not in parts:
                parts.append(val)
    elif isinstance(tool_use_result, str) and tool_use_result not in parts:
        parts.append(tool_use_result)
    return "\n".join(p for p in parts if p)


def parse_transcript(path: str | Path) -> Session:
    path = Path(path)
    session = Session(path=str(path))
    pending: dict[str, Event] = {}  # tool_use id -> Event awaiting its result
    index = 0

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = record.get("type")
            timestamp = str(record.get("timestamp", ""))
            if timestamp:
                session.first_timestamp = session.first_timestamp or timestamp
                session.last_timestamp = timestamp

            if rtype == "assistant":
                if not session.session_id:
                    session.session_id = str(record.get("sessionId", ""))
                    session.cwd = str(record.get("cwd", ""))
                    session.git_branch = str(record.get("gitBranch", "") or "")
                    session.slug = str(record.get("slug", "") or "")
                    session.version = str(record.get("version", ""))
                sidechain = bool(record.get("isSidechain"))
                message = record.get("message", {})
                model = str(message.get("model", "") or "")
                usage = _parse_usage(message)

                # The usage on this record belongs to the whole record, not to
                # any single content block. We hang it on the first event we
                # emit for the record (or a standalone USAGE event if the record
                # is pure accounting with no text/tool blocks) so cost.py can
                # attribute it to one event index for the "top turns" table.
                usage_attached = False
                blocks = _blocks(message)
                for block in blocks:
                    btype = block.get("type")
                    if btype == "text":
                        text = str(block.get("text", "")).strip()
                        if not text:
                            continue
                        event = Event(
                            kind=EventKind.TEXT,
                            index=index,
                            timestamp=timestamp,
                            is_sidechain=sidechain,
                            text=text,
                            model=model,
                        )
                        if not usage_attached and usage is not None:
                            event.usage = usage
                            usage_attached = True
                        session.events.append(event)
                        index += 1
                    elif btype == "tool_use":
                        tool_input = block.get("input")
                        event = Event(
                            kind=EventKind.TOOL_CALL,
                            index=index,
                            timestamp=timestamp,
                            is_sidechain=sidechain,
                            tool_name=str(block.get("name", "")),
                            tool_id=str(block.get("id", "")),
                            tool_input=tool_input if isinstance(tool_input, dict) else {},
                            model=model,
                        )
                        if not usage_attached and usage is not None:
                            event.usage = usage
                            usage_attached = True
                        session.events.append(event)
                        pending[event.tool_id] = event
                        index += 1

                if usage is not None and not usage_attached:
                    # Pure-accounting record (no usable text/tool block): keep it
                    # so its tokens still count toward the session total.
                    session.events.append(Event(
                        kind=EventKind.USAGE,
                        index=index,
                        timestamp=timestamp,
                        is_sidechain=sidechain,
                        model=model,
                        usage=usage,
                    ))
                    index += 1

            elif rtype == "user":
                tool_use_result = record.get("toolUseResult")
                for block in _blocks(record.get("message", {})):
                    if block.get("type") != "tool_result":
                        continue
                    event = pending.pop(str(block.get("tool_use_id", "")), None)
                    if event is None:
                        continue
                    event.is_error = bool(block.get("is_error"))
                    event.output = _result_text(block, tool_use_result)
                    event.output_chars = len(event.output)
                    match = _EXIT_CODE_RE.search(event.output[:500])
                    if match:
                        event.exit_code = int(match.group(1))
                    elif not event.is_error:
                        event.exit_code = 0

    return session


def projects_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


def discover_transcripts(project_filter: str | None = None) -> list[Path]:
    """All transcript files under ~/.claude/projects, newest first."""
    root = projects_dir()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for project in sorted(root.iterdir()):
        if not project.is_dir():
            continue
        if project_filter and project_filter.lower() not in project.name.lower():
            continue
        found.extend(project.glob("*.jsonl"))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_target(target: str, project_filter: str | None = None) -> Path:
    """Turn a CLI target (path, session id, or 'latest') into a file path."""
    direct = Path(target)
    if direct.is_file():
        return direct
    transcripts = discover_transcripts(project_filter)
    if target == "latest":
        if not transcripts:
            raise FileNotFoundError("no transcripts found under ~/.claude/projects")
        return transcripts[0]
    for candidate in transcripts:
        if candidate.stem.startswith(target):
            return candidate
    raise FileNotFoundError(f"no transcript matching {target!r}")
