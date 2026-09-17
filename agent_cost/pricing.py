"""Pricing table: model id -> per-million-token (MTok) USD rates.

WHY a hand-maintained dict and not an API call: agent-cost has ZERO runtime
dependencies and never touches the network, so prices have to live in code.
The cost is that THESE NUMBERS GO STALE, so every row below carries the date
it was read off the vendor's own price list, and you can override the whole
table per-run with `--prices FILE` (a JSON file of the same shape).

Rates verified 2026-09-14 against https://platform.claude.com/docs/en/about-claude/pricing
Cache TTL split:   https://platform.claude.com/docs/en/build-with-claude/prompt-caching
Fast mode:         https://platform.claude.com/docs/en/build-with-claude/fast-mode
US inference geo:  https://platform.claude.com/docs/en/manage-claude/data-residency
Model id format:   https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions

Each entry has five rates:
  input:           fresh (uncached) input tokens
  output:          generated tokens
  cache_write_5m:  tokens written into the 5-minute prompt cache (1.25x input)
  cache_write_1h:  tokens written into the 1-hour prompt cache (2x input)
  cache_read:      tokens served from cache (0.1x input on most models; 0.025x
                   on Claude Fable 5.1, per the pricing page footnote)

Lookup is EXACT against a normalized model id, never a substring: a raw id is
normalized by stripping one trailing bracket suffix (`[1m]`) and then one
trailing `-YYYYMMDD` date. A model that is not in the table is UNPRICED. Its
tokens are still counted and it is flagged in the report; it is never costed
at a guessed rate and never at zero. Substring matching is what let a
`claude-opus-4-1` id silently pick up Opus 4 rates, so it is gone.
"""

from __future__ import annotations

import re

# USD per 1,000,000 tokens. Read off the pricing page on the date above.
PRICING: dict[str, dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5,
                         "cache_write_1h": 20.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5,
                       "cache_write_1h": 20.0, "cache_read": 1.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25,
                      "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25,
                        "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25,
                        "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25,
                        "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25,
                        "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write_5m": 2.5,
                        "cache_write_1h": 4.0, "cache_read": 0.2},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write_5m": 3.75,
                          "cache_write_1h": 6.0, "cache_read": 0.3},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_write_5m": 3.75,
                          "cache_write_1h": 6.0, "cache_read": 0.3},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write_5m": 1.25,
                         "cache_write_1h": 2.0, "cache_read": 0.1},
}

# usage.speed == "fast". Input 10 / output 50 are printed on the pricing page;
# the cache rates follow from the stated rule that the caching multipliers
# apply on top of fast-mode pricing.
FAST_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5,
                      "cache_write_1h": 20.0, "cache_read": 1.0},
    "claude-opus-4-8": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5,
                        "cache_write_1h": 20.0, "cache_read": 1.0},
}

# usage.inference_geo == "us" on Claude 4.6 and later: every rate x 1.1.
GEO_US_MULTIPLIER = 1.1
GEO_US_MODELS = frozenset({
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
})

RATE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")

_BRACKET_SUFFIX = re.compile(r"\[[^\]]*\]$")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize_model(model: str) -> str:
    """`claude-opus-5[1m]` -> `claude-opus-5`, `claude-haiku-4-5-20251001` ->
    `claude-haiku-4-5`. One bracket suffix, then one date suffix, in that order.
    """
    name = _BRACKET_SUFFIX.sub("", (model or "").strip())
    return _DATE_SUFFIX.sub("", name)


def lookup_rate(model: str, table: dict[str, dict[str, float]] | None = None,
                speed: str = "", inference_geo: str = ""
                ) -> tuple[dict[str, float] | None, bool]:
    """Return (rates, rate_known) for a model id.

    rates is None when the normalized id is not in the table: the caller counts
    the tokens, prices nothing, and flags the model. It is NEVER a guess.
    A custom `table` (from --prices) is matched exactly the same way, so an
    override file keys on the same normalized ids.
    """
    table = PRICING if table is None else table
    name = normalize_model(model)
    rate = table.get(name)
    if rate is None:
        return None, False
    if speed == "fast" and name in FAST_PRICING and table is PRICING:
        rate = FAST_PRICING[name]
    if inference_geo == "us" and name in GEO_US_MODELS and table is PRICING:
        rate = {field: rate[field] * GEO_US_MULTIPLIER for field in RATE_FIELDS}
    return rate, True


def load_prices(path: str) -> dict[str, dict[str, float]]:
    """Load a user-supplied JSON price table ({model_id: rates}).

    Kept dependency-free and forgiving about the two cache-write rates, which
    most published price lists express as multipliers: a missing
    cache_write_5m defaults to 1.25x input and cache_write_1h to 2x input,
    the multipliers Anthropic documents. `cache_write` is accepted as a legacy
    alias for the 5-minute rate. input, output and cache_read are required;
    nothing is inferred for them.
    """
    import json
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("prices file must be a JSON object of {model_id: rates}")
    table: dict[str, dict[str, float]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        missing = [f for f in ("input", "output", "cache_read") if f not in entry]
        if missing:
            raise ValueError(f"{key}: price entry is missing {', '.join(missing)}")
        inp = float(entry["input"])
        write_5m = entry.get("cache_write_5m", entry.get("cache_write", inp * 1.25))
        table[str(key)] = {
            "input": inp,
            "output": float(entry["output"]),
            "cache_write_5m": float(write_5m),
            "cache_write_1h": float(entry.get("cache_write_1h", inp * 2)),
            "cache_read": float(entry["cache_read"]),
        }
    return table
