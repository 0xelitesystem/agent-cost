"""Data models for agent-cost.

A Session is an ordered list of Events. The shapes mirror agent-receipts
(its sibling project) so the parser conventions match, but cost analysis
needs two things receipts didn't track:

- Token usage lives on ASSISTANT records (one Usage per assistant record;
  a single logical turn can span several records — we keep them separate
  and let cost.py sum them). So Event carries an optional `usage` and the
  `model` that produced it.
- Tool RESULTS feed text back into the next prompt, which is what actually
  costs input tokens. We measure each result's size (chars) so the bloat
  detector can name the offenders. Tokens are approximated as chars/4 —
  see cost.py for why that's good enough here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class EventKind(enum.Enum):
    TEXT = "text"
    TOOL_CALL = "tool_call"
    USAGE = "usage"  # an assistant record's token accounting, standalone


@dataclass
class Usage:
    """Per-assistant-record token counts, straight from message.usage.

    Cache fields are frequently absent in older transcripts; missing means
    zero, never None, so downstream math never has to guard for it.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0  # tokens written INTO the cache (a premium)
    cache_read_input_tokens: int = 0  # tokens served FROM cache (a discount)

    @property
    def total(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_input_tokens + self.cache_read_input_tokens)

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens


@dataclass
class Event:
    """One thing that happened in the session, in transcript order."""

    kind: EventKind
    index: int  # position in the session event stream
    timestamp: str = ""
    is_sidechain: bool = False

    # TEXT events
    text: str = ""

    # USAGE / assistant-record events
    model: str = ""
    usage: Usage | None = None

    # TOOL_CALL events
    tool_name: str = ""
    tool_id: str = ""
    tool_input: dict = field(default_factory=dict)
    output: str = ""
    output_chars: int = 0  # size of the tool_result text fed back into context
    is_error: bool = False
    exit_code: int | None = None

    @property
    def command(self) -> str:
        """Shell command for Bash/PowerShell calls, else empty."""
        if self.tool_name in ("Bash", "PowerShell"):
            return str(self.tool_input.get("command", ""))
        return ""

    @property
    def file_path(self) -> str:
        """Target path for file-reading/mutating tools, else empty."""
        return str(self.tool_input.get("file_path", ""))

    @property
    def signature(self) -> str:
        """A stable identity for loop detection: tool name + its salient arg.

        Two calls collapse to the same signature when they'd do the same
        thing — same command, same file, same url, same query. That's what
        a death-spiral repeats, so it's what we hash on.
        """
        if self.command:
            return f"{self.tool_name}:{' '.join(self.command.split())}"
        if self.file_path:
            return f"{self.tool_name}:{self.file_path}"
        for key in ("url", "pattern", "query"):
            val = self.tool_input.get(key)
            if val:
                return f"{self.tool_name}:{val}"
        return self.tool_name


@dataclass
class Session:
    """A parsed agent session transcript."""

    path: str
    session_id: str = ""
    cwd: str = ""
    git_branch: str = ""
    slug: str = ""
    version: str = ""
    events: list[Event] = field(default_factory=list)
    first_timestamp: str = ""
    last_timestamp: str = ""

    def tool_calls(self) -> list[Event]:
        return [e for e in self.events if e.kind is EventKind.TOOL_CALL]

    def text_events(self) -> list[Event]:
        return [e for e in self.events if e.kind is EventKind.TEXT]

    def usage_events(self) -> list[Event]:
        return [e for e in self.events if e.usage is not None]


# ---- cost-analysis result shapes ----------------------------------------


@dataclass
class ModelCost:
    """Rolled-up tokens and dollars for one model id across the session."""

    model: str
    usage: Usage
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_write_cost: float = 0.0
    cache_read_cost: float = 0.0
    rate_known: bool = True  # False when we fell back to the default rate

    @property
    def total_cost(self) -> float:
        return (self.input_cost + self.output_cost
                + self.cache_write_cost + self.cache_read_cost)


@dataclass
class TurnCost:
    """One assistant record's contribution, for the 'top expensive turns' table."""

    event_index: int
    model: str
    usage: Usage
    cost: float
    rate_known: bool = True


@dataclass
class BloatOffender:
    """A tool_result big enough to have meaningfully inflated later prompts."""

    event_index: int
    tool_name: str
    label: str  # the command / file / url that produced it
    output_chars: int
    approx_tokens: int


@dataclass
class CacheStats:
    """Cache efficiency, in tokens and (approximate) dollars."""

    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    fresh_input_tokens: int = 0  # uncached input — could have been cache hits
    estimated_savings: float = 0.0  # $ saved by reads vs. paying full input
    estimated_left_on_table: float = 0.0  # $ of fresh input that re-paid full price

    @property
    def hit_ratio(self) -> float:
        """cache reads / all input-side tokens (reads + creation + fresh)."""
        denom = (self.cache_read_tokens + self.cache_creation_tokens
                 + self.fresh_input_tokens)
        return self.cache_read_tokens / denom if denom else 0.0


@dataclass
class Loop:
    """A detected runaway/death-spiral: one action repeated past reason."""

    signature: str  # the repeated action (tool + arg), trimmed for display
    tool_name: str
    count: int
    start_index: int  # event index of the first repeat in the run
    end_index: int  # event index of the last repeat in the run
    all_errored: bool  # every repeat came back is_error — a stuck retry
    wasted_tokens: int = 0  # output+input attributable to the redundant repeats
    wasted_cost: float = 0.0


@dataclass
class CostResult:
    """Everything the cost analysis produced for one session."""

    session: Session
    total_usage: Usage = field(default_factory=Usage)
    total_cost: float = 0.0
    by_model: list[ModelCost] = field(default_factory=list)
    top_turns: list[TurnCost] = field(default_factory=list)
    bloat_offenders: list[BloatOffender] = field(default_factory=list)
    cache: CacheStats = field(default_factory=CacheStats)
    loops: list[Loop] = field(default_factory=list)
    duration_seconds: float | None = None
    has_unknown_rates: bool = False

    @property
    def loop_wasted_cost(self) -> float:
        return sum(loop.wasted_cost for loop in self.loops)
