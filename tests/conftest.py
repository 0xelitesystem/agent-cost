"""Fixture transcripts in Claude Code's JSONL shape, with KNOWN usage numbers
so the cost math is deterministic and assertable.
"""

from __future__ import annotations

import json

import pytest

# pin model ids to rows in the price table so expected dollars are computable
# by hand. Lookup is exact after normalization, so these are real table ids.
SONNET = "claude-sonnet-4-5"  # input 3 / output 15 / cw5m 3.75 / cr 0.30 per MTok
OPUS = "claude-opus-4-5"      # input 5 / output 25 / cw5m 6.25 / cr 0.50 per MTok
HAIKU = "claude-haiku-4-5"    # input 1 / output 5 / cw5m 1.25 / cr 0.10 per MTok

_ids = {"n": 0}


def _identity():
    """A fresh (message id, requestId) pair, one per logical API response."""
    _ids["n"] += 1
    return f"msg_{_ids['n']:03d}", f"req_{_ids['n']:03d}"


def asst_text(text, model=SONNET, usage=None, ts="2026-06-10T12:00:00.000Z",
              message_id=None, request_id=None):
    mid, rid = _identity()
    return {
        "type": "assistant", "timestamp": ts,
        "sessionId": "fixture-session", "cwd": "C:\\fake\\project",
        "uuid": f"{message_id or mid}-{request_id or rid}-text",
        "requestId": request_id or rid,
        "message": {"id": message_id or mid, "role": "assistant", "model": model,
                    "usage": usage or {},
                    "content": [{"type": "text", "text": text}]},
    }


def asst_tool(tool_id, name, tool_input, model=SONNET, usage=None,
              ts="2026-06-10T12:00:00.000Z", message_id=None, request_id=None):
    mid, rid = _identity()
    return {
        "type": "assistant", "timestamp": ts,
        "sessionId": "fixture-session", "cwd": "C:\\fake\\project",
        "uuid": f"{message_id or mid}-{request_id or rid}-{tool_id}",
        "requestId": request_id or rid,
        "message": {"id": message_id or mid, "role": "assistant", "model": model,
                    "usage": usage or {},
                    "content": [{"type": "tool_use", "id": tool_id,
                                 "name": name, "input": tool_input}]},
    }


def tool_result(tool_id, content, is_error=False, ts="2026-06-10T12:00:01.000Z"):
    return {
        "type": "user", "timestamp": ts,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": content, "is_error": is_error}]},
        "toolUseResult": (f"Error: Exit code 1\n{content}" if is_error
                          else {"stdout": content, "stderr": "", "interrupted": False}),
    }


def usage(inp=0, out=0, cw=0, cr=0):
    return {"input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}


def write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(path)


@pytest.fixture
def simple_transcript(tmp_path):
    """Two responses, two models, KNOWN tokens for hand-checkable dollar math.

    Sonnet: 1,000,000 in + 1,000,000 out -> $3.00 + $15.00 = $18.00
    Opus:     100,000 in +   100,000 out -> $0.50 +  $2.50 =  $3.00
    Total = $21.00 exactly.
    """
    records = [
        asst_text("Working on it.", model=SONNET,
                  usage=usage(inp=1_000_000, out=1_000_000)),
        asst_tool("t1", "Bash", {"command": "pytest -q"}, model=OPUS,
                  usage=usage(inp=100_000, out=100_000)),
        tool_result("t1", "ok"),
    ]
    return write_jsonl(tmp_path / "simple.jsonl", records)


@pytest.fixture
def unknown_model_transcript(tmp_path):
    """A model id that matches no pricing key -> falls back, flagged unknown."""
    records = [
        asst_text("hi", model="some-future-model-x9",
                  usage=usage(inp=1_000_000, out=0)),
    ]
    return write_jsonl(tmp_path / "unknown.jsonl", records)


@pytest.fixture
def loop_transcript(tmp_path):
    """Same Bash command repeated 6×, every one failing: a stuck retry."""
    records = [asst_text("starting", usage=usage(inp=1000, out=50))]
    for i in range(6):
        records.append(asst_tool(
            f"L{i}", "Bash", {"command": "pytest tests/test_api.py"},
            model=SONNET, usage=usage(inp=10_000, out=200)))
        records.append(tool_result(f"L{i}", "ImportError: boom", is_error=True))
    return write_jsonl(tmp_path / "loop.jsonl", records)


@pytest.fixture
def bloat_transcript(tmp_path):
    """One huge tool_result (big file read) plus a small one below threshold."""
    huge = "x" * 40_000  # ~10K approx tokens
    records = [
        asst_tool("b1", "Read", {"file_path": "C:\\fake\\project\\big.py"},
                  usage=usage(inp=500, out=20)),
        tool_result("b1", huge),
        asst_tool("b2", "Read", {"file_path": "C:\\fake\\project\\small.py"},
                  usage=usage(inp=500, out=20)),
        tool_result("b2", "tiny"),
    ]
    return write_jsonl(tmp_path / "bloat.jsonl", records)


@pytest.fixture
def cache_transcript(tmp_path):
    """High cache-read ratio: 900K read vs 100K written vs 0 fresh."""
    records = [
        asst_text("cached work", model=SONNET,
                  usage=usage(inp=0, out=1000, cw=100_000, cr=900_000)),
    ]
    return write_jsonl(tmp_path / "cache.jsonl", records)


@pytest.fixture
def repeated_response_transcript(tmp_path):
    """ONE API response written as three records that repeat its usage.

    This is the exact shape Claude Code writes and the exact shape that made
    agent-cost overstate cost: output_tokens ramps up (300 -> 700 -> 1000) and
    every record carries the same input/cache counts. The response is worth
    1,000,000 in + 1,000 out on Sonnet = $3.015, NOT three times that.
    """
    mid, rid = "msg_repeat", "req_repeat"
    records = [
        asst_text("thinking", usage=usage(inp=1_000_000, out=300),
                  message_id=mid, request_id=rid),
        asst_tool("t1", "Bash", {"command": "pytest -q"},
                  usage=usage(inp=1_000_000, out=700),
                  message_id=mid, request_id=rid),
        asst_text("done", usage=usage(inp=1_000_000, out=1000),
                  message_id=mid, request_id=rid),
        tool_result("t1", "ok"),
    ]
    return write_jsonl(tmp_path / "repeated.jsonl", records)


@pytest.fixture
def retry_transcript(tmp_path):
    """One message id, TWO requestIds: a retry, genuinely billed twice."""
    mid = "msg_retry"
    records = [
        asst_text("attempt one", usage=usage(inp=1_000_000, out=1000),
                  message_id=mid, request_id="req_a"),
        asst_text("attempt two", usage=usage(inp=1_000_000, out=1000),
                  message_id=mid, request_id="req_b"),
    ]
    return write_jsonl(tmp_path / "retry.jsonl", records)
