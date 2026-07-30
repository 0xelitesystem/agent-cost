"""Cost analysis: turn a parsed Session into a CostResult.

This is the accounting core. It sums tokens, prices them against the table
(pricing.py), and answers "where did it go?" four ways: by model, by turn,
by context-bloat offender, and by cache efficiency. Loop detection lives in
loops.py and is folded in by analyze().

Token->dollar note: token counts come straight from the transcript's usage
records, so model spend is as exact as the table is current. The ONLY
approximation here is tool-result sizing for the bloat table: we don't get a
token count for fed-back tool output, so we use chars/4, the standard rough
tokens-per-char for English/code. It's used for ranking offenders, never for
the headline dollar figure.
"""

from __future__ import annotations

from datetime import datetime

from .models import (
    BloatOffender,
    CacheStats,
    CostResult,
    EventKind,
    ModelCost,
    Session,
    TurnCost,
    Usage,
)
from .pricing import lookup_rate

CHARS_PER_TOKEN = 4  # rough tokens-per-char for English/code; ranking only

# A tool_result has to clear this to be worth naming as a bloat offender.
# Below ~2k chars it isn't meaningfully inflating later prompts.
BLOAT_MIN_CHARS = 2000


def _cost_for(usage: Usage, rate: dict[str, float]) -> dict[str, float]:
    """Per-component USD for one Usage at one rate. Tokens are per-MTok."""
    return {
        "input": usage.input_tokens / 1_000_000 * rate["input"],
        "output": usage.output_tokens / 1_000_000 * rate["output"],
        "cache_write": usage.cache_creation_input_tokens / 1_000_000 * rate["cache_write"],
        "cache_read": usage.cache_read_input_tokens / 1_000_000 * rate["cache_read"],
    }


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

    by_model: dict[str, ModelCost] = {}
    total = Usage()
    total_cost = 0.0
    turns: list[TurnCost] = []

    for event in session.events:
        if event.usage is None:
            continue
        usage = event.usage
        rate, known = lookup_rate(event.model, prices)
        if not known:
            result.has_unknown_rates = True
        parts = _cost_for(usage, rate)
        turn_cost = sum(parts.values())
        total_cost += turn_cost
        total.add(usage)

        # roll up per model
        mc = by_model.get(event.model)
        if mc is None:
            mc = ModelCost(model=event.model or "(unknown)", usage=Usage(),
                           rate_known=known)
            by_model[event.model] = mc
        mc.usage.add(usage)
        mc.input_cost += parts["input"]
        mc.output_cost += parts["output"]
        mc.cache_write_cost += parts["cache_write"]
        mc.cache_read_cost += parts["cache_read"]

        turns.append(TurnCost(event_index=event.index, model=event.model,
                              usage=usage, cost=turn_cost, rate_known=known))

    result.total_usage = total
    result.total_cost = total_cost
    # most expensive model first
    result.by_model = sorted(by_model.values(),
                             key=lambda m: m.total_cost, reverse=True)
    # top N most expensive assistant turns, costliest first
    result.top_turns = sorted(turns, key=lambda t: t.cost, reverse=True)[:top_n]
    result.bloat_offenders = _bloat_offenders(session, top_n)
    result.cache = _cache_stats(session, prices)
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


def _cache_stats(session: Session,
                 prices: dict[str, dict[str, float]] | None) -> CacheStats:
    """Cache read/write ratio plus a money view.

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
    for event in session.events:
        if event.usage is None:
            continue
        u = event.usage
        stats.cache_read_tokens += u.cache_read_input_tokens
        stats.cache_creation_tokens += u.cache_creation_input_tokens
        stats.fresh_input_tokens += u.input_tokens
        rate, _ = lookup_rate(event.model, prices)
        savings += (u.cache_read_input_tokens / 1_000_000
                    * (rate["input"] - rate["cache_read"]))
        left += u.input_tokens / 1_000_000 * rate["input"]
    stats.estimated_savings = savings
    stats.estimated_left_on_table = left
    return stats
