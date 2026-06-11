from agent_cost.cost import analyze
from agent_cost.parser import parse_transcript


def _analyze(path):
    return analyze(parse_transcript(path))


def test_token_summation(simple_transcript):
    result = _analyze(simple_transcript)
    u = result.total_usage
    assert u.input_tokens == 1_100_000
    assert u.output_tokens == 1_100_000
    assert u.total == 2_200_000


def test_usd_estimation_exact(simple_transcript):
    # Sonnet: 1M in * $3 + 1M out * $15 = $18.00
    # Opus:   0.1M in * $15 + 0.1M out * $75 = $1.50 + $7.50 = $9.00
    result = _analyze(simple_transcript)
    assert round(result.total_cost, 2) == 27.00


def test_cost_by_model_ordering_and_values(simple_transcript):
    result = _analyze(simple_transcript)
    # Sonnet turn ($18) should outrank Opus turn ($9)
    assert len(result.by_model) == 2
    assert result.by_model[0].model.startswith("claude-sonnet")
    assert round(result.by_model[0].total_cost, 2) == 18.00
    assert round(result.by_model[1].total_cost, 2) == 9.00


def test_unknown_model_flagged_and_costed(unknown_model_transcript):
    result = _analyze(unknown_model_transcript)
    assert result.has_unknown_rates is True
    assert result.by_model[0].rate_known is False
    # default input rate is $3/MTok -> 1M tokens = $3.00
    assert round(result.total_cost, 2) == 3.00


def test_top_turns_ordered_by_cost(simple_transcript):
    result = _analyze(simple_transcript)
    costs = [t.cost for t in result.top_turns]
    assert costs == sorted(costs, reverse=True)
    assert round(result.top_turns[0].cost, 2) == 18.00


def test_bloat_offender_detected(bloat_transcript):
    result = _analyze(bloat_transcript)
    # only the 40K-char read clears the threshold; the "tiny" one doesn't
    assert len(result.bloat_offenders) == 1
    offender = result.bloat_offenders[0]
    assert offender.tool_name == "Read"
    assert offender.output_chars == 40_000
    assert offender.approx_tokens == 10_000  # 40000 / 4


def test_cache_stats(cache_transcript):
    result = _analyze(cache_transcript)
    c = result.cache
    assert c.cache_read_tokens == 900_000
    assert c.cache_creation_tokens == 100_000
    assert c.fresh_input_tokens == 0
    assert round(c.hit_ratio, 2) == 0.90
    # savings: 900K read tokens * (input 3 - cache_read 0.30)/MTok = 0.9 * 2.70
    assert round(c.estimated_savings, 4) == round(0.9 * 2.70, 4)


def test_duration_from_timestamps(loop_transcript):
    result = _analyze(loop_transcript)
    # first record at 12:00:00, last tool_result at 12:00:01 -> 1s span,
    # computed from real timestamps (not None) regardless of value.
    assert result.duration_seconds == 1.0


def test_prices_override(simple_transcript):
    from agent_cost.cost import analyze as _a
    custom = {
        "sonnet": {"input": 0.0, "output": 0.0, "cache_write": 0, "cache_read": 0},
        "opus": {"input": 0.0, "output": 0.0, "cache_write": 0, "cache_read": 0},
    }
    result = _a(parse_transcript(simple_transcript), prices=custom)
    assert result.total_cost == 0.0
