from agent_cost.models import EventKind
from agent_cost.parser import parse_transcript


def test_parses_usage_and_model(simple_transcript):
    session = parse_transcript(simple_transcript)
    usage_events = session.usage_events()
    assert len(usage_events) == 2
    first = usage_events[0]
    assert first.model.startswith("claude-sonnet")
    assert first.usage.input_tokens == 1_000_000
    assert first.usage.output_tokens == 1_000_000


def test_missing_cache_fields_are_zero(simple_transcript):
    session = parse_transcript(simple_transcript)
    u = session.usage_events()[0].usage
    assert u.cache_creation_input_tokens == 0
    assert u.cache_read_input_tokens == 0


def test_tool_result_size_measured(bloat_transcript):
    session = parse_transcript(bloat_transcript)
    big_read = session.tool_calls()[0]
    assert big_read.tool_name == "Read"
    assert big_read.output_chars == 40_000


def test_error_results_carry_exit_code(loop_transcript):
    session = parse_transcript(loop_transcript)
    failing = [e for e in session.tool_calls() if e.command][0]
    assert failing.is_error is True
    assert failing.exit_code == 1


def test_garbage_lines_skipped(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('not json\n{"type":"system"}\n', encoding="utf-8")
    session = parse_transcript(path)
    assert session.events == []


def test_event_kinds_in_order(simple_transcript):
    session = parse_transcript(simple_transcript)
    kinds = [e.kind for e in session.events]
    assert kinds == [EventKind.TEXT, EventKind.TOOL_CALL]
