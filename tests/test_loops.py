from agent_cost.loops import detect_loops
from agent_cost.parser import parse_transcript


def test_loop_detected_with_count_and_range(loop_transcript):
    session = parse_transcript(loop_transcript)
    loops = detect_loops(session)
    assert len(loops) == 1
    loop = loops[0]
    assert loop.count == 6
    assert loop.tool_name == "Bash"
    assert loop.all_errored is True
    assert loop.start_index < loop.end_index


def test_loop_wasted_estimate(loop_transcript):
    session = parse_transcript(loop_transcript)
    loop = detect_loops(session)[0]
    # 6 identical calls; first is legitimate, 5 repeats are waste.
    # each call: 10K in + 200 out. 5 * 10200 = 51000 wasted tokens.
    assert loop.wasted_tokens == 5 * 10_200
    assert loop.wasted_cost > 0


def test_no_loop_in_clean_session(simple_transcript):
    session = parse_transcript(simple_transcript)
    assert detect_loops(session) == []


def test_distinct_commands_do_not_loop(tmp_path):
    import json
    from tests.conftest import asst_tool, tool_result
    records = []
    for i in range(6):
        records.append(asst_tool(f"d{i}", "Bash", {"command": f"echo {i}"}))
        records.append(tool_result(f"d{i}", "ok"))
    path = tmp_path / "distinct.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    session = parse_transcript(str(path))
    assert detect_loops(session) == []
