"""Regenerate demo-session.jsonl, a synthetic transcript of an agent that
burns money: a model mix, a 7x failing-command death-spiral, and one giant
file read that bloats every prompt after it. Run: python examples/make_demo.py
"""

import json
from pathlib import Path

CWD = "/home/dev/acme-api"
OPUS = "claude-opus-4-20250514"
SONNET = "claude-sonnet-4-20250514"

_ts = 0


def _next_ts():
    # 4 seconds between records, so duration is non-trivial and deterministic
    global _ts
    _ts += 4
    h, rem = divmod(_ts, 3600)
    m, s = divmod(rem, 60)
    return f"2026-06-10T12:{m:02d}:{s:02d}.000Z"


def asst_text(text, model=SONNET, usage=None):
    return {"type": "assistant", "timestamp": _next_ts(),
            "sessionId": "demo-cost", "cwd": CWD,
            "message": {"role": "assistant", "model": model,
                        "usage": usage or {},
                        "content": [{"type": "text", "text": text}]}}


def asst_tool(tid, name, tool_input, model=SONNET, usage=None):
    return {"type": "assistant", "timestamp": _next_ts(),
            "sessionId": "demo-cost", "cwd": CWD,
            "message": {"role": "assistant", "model": model,
                        "usage": usage or {},
                        "content": [{"type": "tool_use", "id": tid,
                                     "name": name, "input": tool_input}]}}


def result(tid, content, is_error=False):
    return {"type": "user", "timestamp": _next_ts(),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid,
                 "content": content, "is_error": is_error}]},
            "toolUseResult": (f"Error: Exit code 1\n{content}" if is_error else
                              {"stdout": content, "stderr": "", "interrupted": False})}


def usage(inp, out, cw=0, cr=0):
    return {"input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}


records = [
    asst_text("I'll wire up the new billing endpoint and run the suite.",
              model=SONNET, usage=usage(1200, 180, cw=2400, cr=0)),

    # a big file read: the bloat offender (huge stdout fed back into context)
    asst_tool("r1", "Read", {"file_path": f"{CWD}/src/generated/schema.py"},
              model=SONNET, usage=usage(900, 90, cr=2400)),
    result("r1", ("# auto-generated schema\n" + "field_%d: str = ''\n" * 1600)),

    asst_tool("e1", "Edit", {
        "file_path": f"{CWD}/src/billing/endpoint.py",
        "old_string": "# TODO", "new_string": "charge(amount)"},
        model=SONNET, usage=usage(8200, 240, cr=3600)),
    result("e1", "ok"),

    # the death-spiral: same failing command retried 7 times on an Opus turn,
    # each retry re-sending the (now bloated) context. This is the headline.
    *sum([
        [asst_tool(f"loop{i}", "Bash",
                   {"command": "python -m pytest tests/test_billing.py -q"},
                   model=OPUS, usage=usage(9000 + i * 400, 320, cr=4000)),
         result(f"loop{i}",
                 "ModuleNotFoundError: No module named 'stripe'", is_error=True)]
        for i in range(7)
    ], []),

    asst_text("Installing the missing dependency, then re-running.",
              model=SONNET, usage=usage(11000, 150, cr=4200)),
    asst_tool("fix", "Bash", {"command": "pip install stripe"},
              model=SONNET, usage=usage(11200, 90, cr=4200)),
    result("fix", "Successfully installed stripe-9.1.0"),
    asst_tool("final", "Bash",
              {"command": "python -m pytest tests/test_billing.py -q"},
              model=SONNET, usage=usage(11500, 260, cr=4400)),
    result("final", "14 passed in 1.8s"),

    asst_text("Tests pass now. The dependency was missing, and installing it "
              "fixed the failing run.",
              model=SONNET, usage=usage(2100, 320, cr=4400)),
]

out = Path(__file__).parent / "demo-session.jsonl"
out.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
print(f"wrote {out} ({len(records)} records)")
