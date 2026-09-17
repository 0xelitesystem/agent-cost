"""Regression tests for the counting defect: one API response, counted once.

agent-cost used to sum `message.usage` on every assistant RECORD. Claude Code
writes one API response as several records that each repeat that usage, so the
old code multiplied real sessions by roughly 4.8x. Every test here fails if
naive per-record summing comes back.
"""

from __future__ import annotations

import json

from agent_cost.cost import analyze, aggregate, counted_responses
from agent_cost.dedup import ResponseIndex, dedupe, record_from_event
from agent_cost.parser import discover_transcripts, parse_transcript

from conftest import SONNET, asst_text, usage, write_jsonl


def test_repeated_records_count_as_one_response(repeated_response_transcript):
    """Three records, one response: 1M in and 1,000 out, counted once."""
    result = analyze(parse_transcript(repeated_response_transcript))
    assert result.responses == 1
    assert result.duplicate_lines_dropped == 2
    assert result.total_usage.input_tokens == 1_000_000  # NOT 3,000,000
    # naive summing would give $9.045 here instead of $3.015
    assert round(result.total_cost, 3) == 3.015


def test_largest_output_tokens_wins(repeated_response_transcript):
    """output_tokens is a running total; the final record holds the real one."""
    result = analyze(parse_transcript(repeated_response_transcript))
    # 300 / 700 / 1000 across the three records: keep 1000, never 300, never 2000
    assert result.total_usage.output_tokens == 1000


def test_cache_tokens_are_not_multiplied(tmp_path):
    mid, rid = "msg_cache", "req_cache"
    records = [
        asst_text("a", usage=usage(cw=500_000, cr=900_000), message_id=mid,
                  request_id=rid),
        asst_text("b", usage=usage(cw=500_000, cr=900_000), message_id=mid,
                  request_id=rid),
    ]
    path = write_jsonl(tmp_path / "cache-dupe.jsonl", records)
    result = analyze(parse_transcript(path))
    assert result.cache.cache_read_tokens == 900_000
    assert result.cache.cache_creation_tokens == 500_000


def test_retry_with_two_request_ids_is_two_responses(retry_transcript):
    """Same message id, two different requestIds: two real, billable attempts."""
    result = analyze(parse_transcript(retry_transcript))
    assert result.responses == 2
    assert result.total_usage.input_tokens == 2_000_000


def test_dedup_is_global_across_files(tmp_path):
    """A subagent transcript can carry a copy of its parent's response.

    Per-file dedup would count it twice. The shared index counts it once.
    """
    mid, rid = "msg_shared", "req_shared"
    shared = asst_text("shared response", usage=usage(inp=1_000_000, out=500),
                       message_id=mid, request_id=rid)
    main = write_jsonl(tmp_path / "main.jsonl", [shared])
    sub = write_jsonl(tmp_path / "agent-1.jsonl", [shared])

    index = ResponseIndex()
    for path in (main, sub):
        counted_responses(parse_transcript(path), index)
    records = index.unique()
    assert index.lines_seen == 2
    assert len(records) == 1
    assert aggregate(records).total_usage.input_tokens == 1_000_000


def test_records_without_any_id_are_all_kept(tmp_path):
    """No message id, no requestId, no uuid: nothing to match on, so keep both
    rather than silently merging two unrelated responses."""
    bare = {"type": "assistant", "timestamp": "2026-06-10T12:00:00.000Z",
            "message": {"role": "assistant", "model": SONNET,
                        "usage": usage(inp=1000, out=10),
                        "content": [{"type": "text", "text": "hi"}]}}
    path = tmp_path / "bare.jsonl"
    path.write_text("\n".join(json.dumps(bare) for _ in range(2)), encoding="utf-8")
    result = analyze(parse_transcript(path))
    assert result.responses == 2


def test_synthetic_records_are_not_counted(tmp_path):
    """model '<synthetic>' is a client-side placeholder, never an API call."""
    record = {"type": "assistant", "timestamp": "2026-06-10T12:00:00.000Z",
              "uuid": "u1", "requestId": "r1",
              "message": {"id": "m1", "role": "assistant", "model": "<synthetic>",
                          "usage": usage(inp=5_000, out=100),
                          "content": [{"type": "text", "text": "API error"}]}}
    path = tmp_path / "synthetic.jsonl"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = analyze(parse_transcript(path))
    assert result.responses == 0
    assert result.total_cost == 0.0


def test_dedupe_helper_keeps_one_record_per_response(repeated_response_transcript):
    session = parse_transcript(repeated_response_transcript)
    records = [record_from_event(e, session.path)
               for e in session.events if e.usage is not None]
    assert len(records) == 3
    assert len(dedupe(records)) == 1


def test_discovery_is_recursive(tmp_path, monkeypatch):
    """Subagent transcripts are nested well below the project folder."""
    from agent_cost import parser

    project = tmp_path / "projects" / "C--Users-dev-acme"
    nested = project / "session-a" / "subagents" / "workflows" / "run-1"
    nested.mkdir(parents=True)
    (project / "session-a.jsonl").write_text("", encoding="utf-8")
    (nested / "agent-7.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setattr(parser, "projects_dir", lambda: tmp_path / "projects")
    found = {p.name for p in discover_transcripts()}
    assert found == {"session-a.jsonl", "agent-7.jsonl"}
