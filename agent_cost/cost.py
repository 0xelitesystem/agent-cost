"""Cost analysis: turn a parsed Session into a CostResult.

This is the accounting core. It counts each API response ONCE (dedup.py),
prices it against the table (pricing.py), and answers "where did it go?" four
ways: by model, by turn, by context-bloat offender, and by cache efficiency.
Loop detection lives in loops.py and is folded in by analyze().

Counting note: a transcript writes one API response across several records
that each repeat the same usage block, so the tokens are read from ONE record
per response (the one with the largest output_tokens). Summing every record,
which is what agent-cost used to do, overstated real sessions by about 4.8x.

Pricing notes: cache writes are billed by TTL, 1.25x input for the 5-minute
cache and 2x for the 1-hour cache, so the split on usage.cache_creation is
used when the transcript carries it. A model that is not in the price table
is UNPRICED: its tokens are counted and its name reported, and no dollar
figure is invented for it.

Token->dollar note: token counts come straight from the transcript's usage
records, so model spend is as exact as the table is current. The ONLY
approximation here is tool-result sizing for the bloat table: we don't get a
token count for fed-back tool output, so we use chars/4, the standard rough
tokens-per-char for English/code. It's used for ranking offenders, never for
the headline dollar figure.
"""

from __future__ import annotations

from datetime import datetime

from .dedup import ResponseIndex, ResponseRecord, record_from_event
from .models import (
    Aggregate,
    BloatOffender,
    CacheStats,
    CostResult,
    EventKind,
    ModelCost,
    Session,
    TurnCost,
    Usage,
)
from .pricing import lookup_rate, normalize_model

CHARS_PER_TOKEN = 4  # rough tokens-per-char for English/code; ranking only

# A tool_result has to clear this to be worth naming as a bloat offender.
# Below ~2k chars it isn't meaningfully inflating later prompts.
BLOAT_MIN_CHARS = 2000

_ZERO_PARTS = {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}


def _cache_write_split(usage: Usage) -> tuple[int, int]:
    """(5-minute writes, 1-hour writes) for one usage block.

    The sub-object is authoritative when it agrees with the total. When it
    disagrees, rescale it onto the total in integers rather than trusting
    either half. When it is absent, price every write at the 5-minute rate,
    the documented fallback, which is also the cheaper of the two.
    """
    total = usage.cache_creation_input_tokens
    write_5m, write_1h = usage.cache_creation_5m, usage.cache_creation_1h
    if write_5m + write_1h == total:
        return write_5m, write_1h
    if write_5m + write_1h <= 0:
        return total, 0
    scaled_5m = total * write_5m // (write_5m + write_1h)
    return scaled_5m, total - scaled_5m


def _parts_at_rate(usage: Usage, rate: dict[str, float]) -> dict[str, float]:
    """Per-component USD for one usage block at one rate. Rates are per MTok.

    A table handed in programmatically may carry only the input/output/read
    rates, so the two cache-write rates fall back to the multipliers Anthropic
    documents, 1.25x input for 5 minutes and 2x for 1 hour.
    """
    write_5m, write_1h = _cache_write_split(usage)
    input_rate = rate["input"]
    rate_5m = rate.get("cache_write_5m", rate.get("cache_write", input_rate * 1.25))
    rate_1h = rate.get("cache_write_1h", input_rate * 2)
    return {
        "input": usage.input_tokens / 1_000_000 * input_rate,
        "output": usage.output_tokens / 1_000_000 * rate["output"],
        "cache_write": (write_5m / 1_000_000 * rate_5m
                        + write_1h / 1_000_000 * rate_1h),
        "cache_read": usage.cache_read_input_tokens / 1_000_000 * rate["cache_read"],
    }


def cost_parts(model: str, usage: Usage,
               prices: dict[str, dict[str, float]] | None = None
               ) -> tuple[dict[str, float], bool]:
    """(per-component USD, rate_known) for one API response.

    rate_known is False when the model is not in the table; the components are
    then all zero, because an unpriced model gets no invented dollar figure.
    A refusal fallback (usage.iterations) bills every attempt that produced
    output at the rates of the model that ran it.
    """
    if usage.iterations:
        total = dict(_ZERO_PARTS)
        last = len(usage.iterations) - 1
        for position, (attempt_model, attempt_usage) in enumerate(usage.iterations):
            if attempt_usage.output_tokens == 0 and position != last:
                continue  # declined before producing output: reported, not billed
            rate, known = lookup_rate(attempt_model or model, prices,
                                      usage.speed, usage.inference_geo)
            if rate is None or not known:
                return dict(_ZERO_PARTS), False
            for field, value in _parts_at_rate(attempt_usage, rate).items():
                total[field] += value
        return total, True

    rate, known = lookup_rate(model, prices, usage.speed, usage.inference_geo)
    if rate is None or not known:
        return dict(_ZERO_PARTS), False
    return _parts_at_rate(usage, rate), True


def response_cost(model: str, usage: Usage,
                  prices: dict[str, dict[str, float]] | None = None) -> float:
    """Total USD for one API response (0.0 when the model is unpriced)."""
    parts, _ = cost_parts(model, usage, prices)
    return sum(parts.values())


def aggregate(records: list[ResponseRecord],
              prices: dict[str, dict[str, float]] | None = None,
              top_n: int = 5) -> Aggregate:
    """Roll deduplicated responses up into totals, by-model and by-turn views.

    Takes records that have ALREADY been deduplicated, so it is equally usable
    for one session and for a whole-machine scan.
    """
    result = Aggregate()
    by_model: dict[str, ModelCost] = {}
    turns: list[TurnCost] = []

    for record in records:
        usage = record.usage
        parts, known = cost_parts(record.model, usage, prices)
        turn_cost = sum(parts.values())
        result.total_cost += turn_cost
        result.total_usage.add(usage)
        if not known:
            result.has_unknown_rates = True
            name = normalize_model(record.model) or "(unknown)"
            result.unpriced_models[name] = result.unpriced_models.get(name, 0) + 1

        model_cost = by_model.get(record.model)
        if model_cost is None:
            model_cost = ModelCost(model=record.model or "(unknown)", usage=Usage(),
                                   rate_known=known)
            by_model[record.model] = model_cost
        model_cost.usage.add(usage)
        model_cost.input_cost += parts["input"]
        model_cost.output_cost += parts["output"]
        model_cost.cache_write_cost += parts["cache_write"]
        model_cost.cache_read_cost += parts["cache_read"]

        turns.append(TurnCost(event_index=record.event_index, model=record.model,
                              usage=usage, cost=turn_cost, rate_known=known))

    result.responses = len(records)
    # most expensive model first
    result.by_model = sorted(by_model.values(),
                             key=lambda m: m.total_cost, reverse=True)
    # top N most expensive responses, costliest first
    result.top_turns = sorted(turns, key=lambda t: t.cost, reverse=True)[:top_n]
    return result


def counted_responses(session: Session, index: ResponseIndex | None = None
                      ) -> tuple[list[ResponseRecord], int]:
    """(one record per API response, transcript lines that repeated a response).

    Pass a shared `index` to deduplicate across several transcripts at once;
    it then collects the records and returns nothing for this session alone.
    """
    records = [record_from_event(e, session.path)
               for e in session.events if e.usage is not None]
    if index is not None:
        index.add_all(records)
        return [], 0
    local = ResponseIndex()
    local.add_all(records)
    unique = local.unique()
    return unique, len(records) - len(unique)


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        # transcripts use ISO 8601, usually with a trailing Z
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(session: Session) -> float | None:
    start = _parse_ts(session.first_timestamp)
    end = _parse_ts(session.last_timestamp)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def analyze(session: Session, prices: dict[str, dict[str, float]] | None = None,
            top_n: int = 5) -> CostResult:
    """Cost everything except loops (cli.analyze_cost folds those in)."""
    result = CostResult(session=session)
    records, duplicates = counted_responses(session)
    rolled = aggregate(records, prices, top_n)

    result.total_usage = rolled.total_usage
    result.total_cost = rolled.total_cost
    result.by_model = rolled.by_model
    result.top_turns = rolled.top_turns
    result.has_unknown_rates = rolled.has_unknown_rates
    result.unpriced_models = rolled.unpriced_models
    result.responses = rolled.responses
    result.duplicate_lines_dropped = duplicates
    result.bloat_offenders = _bloat_offenders(session, top_n)
    result.cache = _cache_stats(records, prices)
    result.duration_seconds = _duration_seconds(session)
    return result


def _label_for(event) -> str:
    """A short human label for the action behind a tool_result."""
    if event.command:
        return " ".join(event.command.split())[:80]
    if event.file_path:
        return event.file_path
    for key in ("url", "pattern", "query"):
        val = event.tool_input.get(key)
        if val:
            return str(val)[:80]
    return event.tool_name


def _bloat_offenders(session: Session, top_n: int) -> list[BloatOffender]:
    """Largest tool_result outputs: the file reads / command dumps that
    bloated context. Ranked by size; only those clearing BLOAT_MIN_CHARS.
    """
    offenders: list[BloatOffender] = []
    for event in session.events:
        if event.kind is not EventKind.TOOL_CALL:
            continue
        if event.output_chars < BLOAT_MIN_CHARS:
            continue
        offenders.append(BloatOffender(
            event_index=event.index,
            tool_name=event.tool_name,
            label=_label_for(event),
            output_chars=event.output_chars,
            approx_tokens=event.output_chars // CHARS_PER_TOKEN,
        ))
    offenders.sort(key=lambda o: o.output_chars, reverse=True)
    return offenders[:top_n]


def _cache_stats(records: list[ResponseRecord],
                 prices: dict[str, dict[str, float]] | None) -> CacheStats:
    """Cache read/write ratio plus a money view, over deduplicated responses.

    savings: what cache reads would have cost at the full input rate minus
             what they actually cost at the cache-read rate (money the cache
             saved you).
    left_on_table: what the fresh/uncached input cost, work that re-paid full
             price and, with better cache reuse, some of which could have been
             a discounted read. A soft "headroom" figure, not a guarantee.
    """
    stats = CacheStats()
    savings = 0.0
    left = 0.0
    for record in records:
        usage = record.usage
        stats.cache_read_tokens += usage.cache_read_input_tokens
        stats.cache_creation_tokens += usage.cache_creation_input_tokens
        stats.fresh_input_tokens += usage.input_tokens
        rate, known = lookup_rate(record.model, prices, usage.speed,
                                  usage.inference_geo)
        if rate is None or not known:
            continue  # unpriced model: tokens counted above, dollars not guessed
        savings += (usage.cache_read_input_tokens / 1_000_000
                    * (rate["input"] - rate["cache_read"]))
        left += usage.input_tokens / 1_000_000 * rate["input"]
    stats.estimated_savings = savings
    stats.estimated_left_on_table = left
    return stats
