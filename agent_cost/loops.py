"""Runaway-loop / death-spiral detection — agent-cost's headline feature.

The pattern that silently 10x's a bill isn't expensive single calls; it's the
agent getting stuck and repeating the SAME action over and over — re-running a
failing command, re-reading the same file, retrying a broken edit — while every
iteration re-sends the (now bloated) context and pays for it again. Live
circuit-breakers stop this in real time; agent-cost finds it after the fact in
the transcript you already have.

Detection is deliberately simple and explainable (these are heuristics, never
convictions): walk the tool calls in order, group maximal runs of the same
signature (tool + salient arg, see Event.signature) that occur within a sliding
window, and flag any run of MIN_REPEATS or more. A run where every repeat
errored is the worst kind — a stuck retry — and is called out as such.

Wasted cost: the first attempt is legitimate work; everything after it in the
run is the waste. We attribute the usage of the assistant turns that issued the
redundant repeats (count - 1 of them) as wasted tokens/$.
"""

from __future__ import annotations

from .models import EventKind, Loop, Session
from .pricing import lookup_rate

MIN_REPEATS = 3  # a run this long or longer is a loop
WINDOW = 12  # repeats must fall within this many tool calls to count as one run


def _run_cost(session: Session, start_index: int, end_index: int,
              prices: dict[str, dict[str, float]] | None) -> tuple[int, float]:
    """Tokens and $ for assistant usage in [start_index, end_index]."""
    tokens = 0
    cost = 0.0
    for event in session.events:
        if not (start_index <= event.index <= end_index):
            continue
        if event.usage is None:
            continue
        u = event.usage
        tokens += u.total
        rate, _ = lookup_rate(event.model, prices)
        cost += (u.input_tokens / 1_000_000 * rate["input"]
                 + u.output_tokens / 1_000_000 * rate["output"]
                 + u.cache_creation_input_tokens / 1_000_000 * rate["cache_write"]
                 + u.cache_read_input_tokens / 1_000_000 * rate["cache_read"])
    return tokens, cost


def detect_loops(session: Session,
                 prices: dict[str, dict[str, float]] | None = None) -> list[Loop]:
    calls = [e for e in session.events if e.kind is EventKind.TOOL_CALL]
    loops: list[Loop] = []
    i = 0
    n = len(calls)

    while i < n:
        sig = calls[i].signature
        # extend the run while the same signature keeps recurring within WINDOW
        members = [calls[i]]
        j = i + 1
        last_match = i
        while j < n and (j - last_match) <= WINDOW:
            if calls[j].signature == sig:
                members.append(calls[j])
                last_match = j
            j += 1

        if len(members) >= MIN_REPEATS:
            start_event = members[0]
            end_event = members[-1]
            all_errored = all(m.is_error for m in members)
            # the first call is real work; the repeats after it are the waste
            redundant = members[1:]
            wasted_tokens, wasted_cost = _run_cost(
                session,
                redundant[0].index if redundant else end_event.index,
                end_event.index,
                prices,
            )
            loops.append(Loop(
                signature=sig[:100],
                tool_name=start_event.tool_name,
                count=len(members),
                start_index=start_event.index,
                end_index=end_event.index,
                all_errored=all_errored,
                wasted_tokens=wasted_tokens,
                wasted_cost=wasted_cost,
            ))
            # resume scanning past this run so we don't double-report it
            i = last_match + 1
        else:
            i += 1

    # loudest (most repeats) first
    loops.sort(key=lambda l: l.count, reverse=True)
    return loops
