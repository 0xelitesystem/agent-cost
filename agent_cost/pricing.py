"""Pricing table: model id -> per-million-token (MTok) USD rates.

WHY a hand-maintained dict and not an API call: agent-cost has ZERO runtime
dependencies and never touches the network, so prices have to live in code.
The cost is that THESE NUMBERS GO STALE. They are reasonable defaults you
should VERIFY against the provider's current price list, and override per-run
with `--prices FILE` (a JSON file of the same shape) when they drift.

Each entry has four rates:
  input:        fresh (uncached) input tokens
  output:       generated tokens
  cache_write:  tokens written into the prompt cache (a one-time premium,
                conventionally ~1.25x input for a 5-minute cache)
  cache_read:   tokens served from cache (a deep discount, ~0.1x input)

Keys are matched by substring against the transcript's model id (so
"claude-opus-4-..." picks up the "opus" tier without pinning a date),
longest key first. Unknown models fall back to DEFAULT_RATE and get
flagged "rate unknown" in the report so you never silently trust a guess.
"""

from __future__ import annotations

# Rates are USD per 1,000,000 tokens. APPROXIMATE DEFAULTS. Verify these.
# Ordered conceptually opus > sonnet > haiku; lookup sorts by key length so
# more specific ids win over generic ones.
PRICING: dict[str, dict[str, float]] = {
    # Opus tier
    "opus": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    # Sonnet tier
    "sonnet": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    # Haiku tier
    "haiku": {"input": 0.80, "output": 4.0, "cache_write": 1.0, "cache_read": 0.08},
}

# Used for any model id that matches no key. Conservatively mid-tier so an
# unknown model isn't costed at zero; the report flags it regardless.
DEFAULT_RATE: dict[str, float] = {
    "input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30,
}


def lookup_rate(model: str, table: dict[str, dict[str, float]] | None = None
                ) -> tuple[dict[str, float], bool]:
    """Return (rate_dict, rate_known) for a model id.

    Substring match, longest key first so "claude-3-5-sonnet" still resolves
    via "sonnet" but a hypothetical exact-id key would take precedence.
    rate_known is False when nothing matched and we fell back to DEFAULT_RATE.
    """
    table = PRICING if table is None else table
    model_l = (model or "").lower()
    for key in sorted(table, key=len, reverse=True):
        if key.lower() in model_l:
            return table[key], True
    return DEFAULT_RATE, False


def load_prices(path: str) -> dict[str, dict[str, float]]:
    """Load a user-supplied JSON price table ({model_key: {input,output,...}}).

    Kept dependency-free and forgiving: missing rate keys inside an entry fall
    back to DEFAULT_RATE's value for that key so a partial override still works.
    """
    import json
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("prices file must be a JSON object of {model: rates}")
    table: dict[str, dict[str, float]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        table[str(key)] = {
            field: float(entry.get(field, DEFAULT_RATE[field]))
            for field in ("input", "output", "cache_write", "cache_read")
        }
    return table
