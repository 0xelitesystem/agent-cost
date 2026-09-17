"""One API response, counted once.

THE DEFECT THIS FIXES: a single Claude Code API response is written to the
transcript as SEVERAL JSONL lines, one per content block, and EVERY one of
those lines repeats the same `message.usage` object. Summing usage line by
line therefore counts the same tokens two, three, five times over. On real
transcripts that inflated agent-cost's totals by roughly 4.8x, and the error
grows with how chatty the turn was, so it is not even a constant you could
divide out.

THE RULE, proven against 689 MB of real Claude Code transcripts:

  1. One API response == one `message.id`. Deduplicate by it GLOBALLY, not
     per file: a subagent transcript can carry a copy of a parent message,
     so per-file dedup still double counts across files.
  2. Fall back to the record's `requestId`, then its `uuid`, when a record
     carries no message id. A record with none of the three cannot be matched
     to anything else and is kept as its own response.
  3. SPLIT a `message.id` that carries two or more different non-empty
     `requestId`s: that is a retry or a fallback, genuinely billed more than
     once, and collapsing it would undercount. Lines under such an id that
     carry no requestId are duplicates of one of the billed attempts, so they
     fold into the largest rather than adding a phantom response.
  4. KEEP THE LINE WITH THE LARGEST `output_tokens`. Input and cache counts
     are identical on every line of a response; output_tokens is a running
     total, so the last line (the one with stop_reason) holds the real figure.
     Keeping the first line undercounts output by about 65%.

Everything here is pure bookkeeping: no line's tokens are ever altered, only
the choice of which line represents the response.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Usage


@dataclass
class ResponseRecord:
    """One assistant record's accounting, stripped of everything else.

    Deliberately small: a whole-machine scan holds one of these per response
    instead of a whole Event with its text and tool payloads.
    """

    model: str = ""
    usage: Usage = field(default_factory=Usage)
    message_id: str = ""
    request_id: str = ""
    record_uuid: str = ""
    event_index: int = -1
    source: str = ""  # transcript path, for multi-session scans

    @property
    def identity(self) -> str:
        """message.id, else requestId, else uuid, else '' (unmatchable)."""
        return self.message_id or self.request_id or self.record_uuid


def record_from_event(event, source: str = "") -> ResponseRecord:
    return ResponseRecord(
        model=event.model,
        usage=event.usage,
        message_id=event.message_id,
        request_id=event.request_id,
        record_uuid=event.record_uuid,
        event_index=event.index,
        source=source,
    )


def _output(record: ResponseRecord) -> int:
    return record.usage.output_tokens if record.usage else 0


def _best(records: list[ResponseRecord]) -> ResponseRecord:
    """Largest output_tokens wins; the later line wins a tie."""
    return max(enumerate(records), key=lambda pair: (_output(pair[1]), pair[0]))[1]


class ResponseIndex:
    """Collects assistant records and hands back one per API response.

    Add records from as many transcripts as you like, then call `unique()`.
    Resolution is deferred to that call because rule 3 (split an id with two
    requestIds) can only be decided once every line for an id has been seen.
    """

    def __init__(self) -> None:
        self._by_identity: dict[str, list[ResponseRecord]] = {}
        self._unkeyed: list[ResponseRecord] = []

    def add(self, record: ResponseRecord) -> None:
        identity = record.identity
        if not identity:
            self._unkeyed.append(record)
            return
        self._by_identity.setdefault(identity, []).append(record)

    def add_all(self, records) -> None:
        for record in records:
            self.add(record)

    @property
    def lines_seen(self) -> int:
        return sum(len(v) for v in self._by_identity.values()) + len(self._unkeyed)

    def unique(self) -> list[ResponseRecord]:
        """One record per API response, in first-seen order."""
        kept: list[ResponseRecord] = []
        for records in self._by_identity.values():
            request_ids = {r.request_id for r in records if r.request_id}
            if len(request_ids) > 1:
                # a retry or fallback: each requestId was billed separately
                for request_id in sorted(request_ids):
                    kept.append(_best([r for r in records if r.request_id == request_id]))
            else:
                kept.append(_best(records))
        kept.extend(self._unkeyed)
        kept.sort(key=lambda r: (r.source, r.event_index))
        return kept

    @property
    def duplicates_dropped(self) -> int:
        return self.lines_seen - len(self.unique())


def dedupe(records) -> list[ResponseRecord]:
    """Convenience wrapper for a single transcript's records."""
    index = ResponseIndex()
    index.add_all(records)
    return index.unique()
