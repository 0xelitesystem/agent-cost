"""Fixture transcripts in Claude Code's JSONL shape, with KNOWN usage numbers
so the cost math is deterministic and assertable.
"""

from __future__ import annotations

import json

import pytest

# pin model ids to the pricing tiers so expected dollars are computable by hand
SONNET = "claude-sonnet-4-20250514"  # input 3 / output 15 / cw 3.75 / cr 0.30 per MTok
OPUS = "claude-opus-4-20250514"      # input 15 / output 75 / cw 18.75 / cr 1.50 per MTok
HAIKU = "claude-haiku-4-20250514"    # input 0.8 / output 4 / cw 1 / cr 0.08 per MTok


def asst_text(text, model=SONNET, usage=None, ts="2026-06-10T12:00:00.000Z"):
    return {
        "type": "assistant", "timestamp": ts,
        "sessionId": "fixture-session", "cwd": "C:\\fake\\project",
        "message": {"role": "assistant", "model": model, "usage": usage or {},
                    "content": [{"type": "text", "text": text}]},
    }


def asst_tool(tool_id, name, tool_input, model=SONNET, usage=None,
              ts="2026-06-10T12:00:00.000Z"):
    return {
        "type": "assistant", "timestamp": ts,
        "sessionId": "fixture-session", "cwd": "C:\\fake\\project",
        "message": {"role": "assistant", "model": model, "usage": usage or {},
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
    """Two turns, two models, KNOWN tokens for hand-checkable dollar math.

    Sonnet turn: 1,000,000 in + 1,000,000 out  -> $3.00 + $15.00 = $18.00
    Opus   turn:   100,000 in +   100,000 out  -> $1.50 +  $7.50 =  $9.00
    Total = $27.00 exactly.
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
    """Same Bash command repeated 6×, every one failing — a stuck retry."""
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
