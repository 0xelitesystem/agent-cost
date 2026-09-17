"""Regenerate demo-session.jsonl, a synthetic transcript of an agent that
burns money: a model mix, a 7x failing-command death-spiral, and one giant
file read that bloats every prompt after it. Run: python examples/make_demo.py

The transcript is written the way Claude Code writes one: a single API
response becomes SEVERAL records, one per content block, and every record
repeats that response's usage with a running output_tokens. They share a
message id and a requestId, which is how agent-cost counts the response once
instead of once per record. Sum the records naively and this demo reads about
2x its true cost.
"""

import json
from pathlib import Path

CWD = "/home/dev/acme-api"
OPUS = "claude-opus-4-5"
SONNET = "claude-sonnet-4-5"

_ts = 0
_seq = 0


def _next_ts():
    # 4 seconds between records, so duration is non-trivial and deterministic
    global _ts
    _ts += 4
    h, rem = divmod(_ts, 3600)
    m, s = divmod(rem, 60)
    return f"2026-06-10T12:{m:02d}:{s:02d}.000Z"


def usage(inp, out, cw=0, cr=0, write_1h=0):
    """One response's usage. cw is the total cache write; write_1h is how much
    of it went to the 1-hour cache (billed 2x input, vs 1.25x for 5 minutes).
    """
    block = {"input_tokens": inp, "output_tokens": out,
             "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}
    if cw:
        block["cache_creation"] = {
            "ephemeral_5m_input_tokens": cw - write_1h,
            "ephemeral_1h_input_tokens": write_1h,
        }
    return block


def response(blocks, model=SONNET, usage_block=None):
    """One API response, written as one record per content block.

    Every record repeats the usage; output_tokens ramps up across them and is
    final only on the last, exactly as Claude Code writes it.
    """
    global _seq
    _seq += 1
    message_id = f"msg_demo_{_seq:04d}"
    request_id = f"req_demo_{_seq:04d}"
    usage_block = usage_block or usage(0, 0)
    final_out = usage_block.get("output_tokens", 0)
    records = []
    for position, block in enumerate(blocks, start=1):
        running = dict(usage_block)
        # a running total on every record but the last
        running["output_tokens"] = (final_out if position == len(blocks)
                                    else max(1, final_out * position // len(blocks)))
        records.append({
            "type": "assistant", "timestamp": _next_ts(),
            "sessionId": "demo-cost", "cwd": CWD,
            "uuid": f"{message_id}-{position}", "requestId": request_id,
            "message": {"id": message_id, "role": "assistant", "model": model,
                        "usage": running, "content": [block]},
        })
    return records


def text_block(text):
    return {"type": "text", "text": text}


def tool_block(tid, name, tool_input):
    return {"type": "tool_use", "id": tid, "name": name, "input": tool_input}


def result(tid, content, is_error=False):
    return [{"type": "user", "timestamp": _next_ts(),
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": tid,
                  "content": content, "is_error": is_error}]},
             "toolUseResult": (f"Error: Exit code 1\n{content}" if is_error else
                               {"stdout": content, "stderr": "",
                                "interrupted": False})}]


records = [
    *response([text_block("I'll wire up the new billing endpoint and run the suite.")],
              model=SONNET, usage_block=usage(1200, 180, cw=2400)),

    # a big file read: the bloat offender (huge stdout fed back into context).
    # Two blocks, one response: the thinking-out-loud line and the tool call.
    *response([text_block("Reading the generated schema first."),
               tool_block("r1", "Read", {"file_path": f"{CWD}/src/generated/schema.py"})],
              model=SONNET, usage_block=usage(900, 90, cr=2400)),
    *result("r1", ("# auto-generated schema\n" + "field_%d: str = ''\n" * 1600)),

    *response([tool_block("e1", "Edit", {
        "file_path": f"{CWD}/src/billing/endpoint.py",
        "old_string": "# TODO", "new_string": "charge(amount)"})],
        model=SONNET, usage_block=usage(8200, 240, cr=3600)),
    *result("e1", "ok"),

    # the death-spiral: same failing command retried 7 times on an Opus turn,
    # each retry re-sending the (now bloated) context. This is the headline.
    *sum([
        [*response([text_block("Running the billing tests."),
                    tool_block(f"loop{i}", "Bash",
                               {"command": "python -m pytest tests/test_billing.py -q"})],
                   model=OPUS,
                   usage_block=usage(9000 + i * 400, 320, cr=4000)),
         *result(f"loop{i}",
                 "ModuleNotFoundError: No module named 'stripe'", is_error=True)]
        for i in range(7)
    ], []),

    *response([text_block("Installing the missing dependency, then re-running.")],
              model=SONNET, usage_block=usage(11000, 150, cr=4200)),
    *response([tool_block("fix", "Bash", {"command": "pip install stripe"})],
              model=SONNET, usage_block=usage(11200, 90, cr=4200)),
    *result("fix", "Successfully installed stripe-9.1.0"),
    *response([tool_block("final", "Bash",
                          {"command": "python -m pytest tests/test_billing.py -q"})],
              model=SONNET,
              usage_block=usage(11500, 260, cw=1600, cr=4400, write_1h=1600)),
    *result("final", "14 passed in 1.8s"),

    *response([text_block("Tests pass now. The dependency was missing, and "
                          "installing it fixed the failing run.")],
              model=SONNET, usage_block=usage(2100, 320, cr=4400)),
]

out = Path(__file__).parent / "demo-session.jsonl"
out.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
print(f"wrote {out} ({len(records)} records)")
