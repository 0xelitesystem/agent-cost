# agent-cost

> Your agent session cost $12. **agent-cost tells you which 30 seconds spent $9**, no proxy, no setup.

`agent-cost` is a retrospective forensic analyzer for AI agent sessions. You point it at a finished transcript and it tells you where the tokens and the money went, by model, by turn, by tool, and, critically, it **detects runaway loops after the fact**: the death-spiral where the agent gets stuck repeating one action and silently 10x's your bill.

No proxy. No SDK. No API key. It reads the transcript you already have on disk.

Zero dependencies. Pure Python stdlib. Works offline, nothing leaves your machine.

## The problem

Agents make unbounded LLM calls on your behalf. Most of the time that's fine; sometimes the agent gets stuck, re-running the same failing command, re-reading the same file, retrying a broken edit, and every iteration re-sends the (now bloated) context and pays for it again. That's the failure mode behind the [widely-reported](https://www.theregister.com/2025/04/16/cursor_ai_support_bot/) class of surprise four- and five-figure bills, and runaway cost consistently ranks at the top of the risks teams cite when agent projects stall.

The tools that exist to stop this, Agent Firewall, AgentFuse, LiteLLM budget caps, are **live, preventive proxies**: you install them *in front of* your API and they cut calls off in real time. Useful, but they're infrastructure you have to set up before the fact.

`agent-cost` is the complement. It's **retrospective and zero-setup**. The session already happened; the transcript already has the token counts. This reads them and explains the bill, including the loop the live guard would have caught, so you can see it even when you never had a guard installed.

## What it shows

```
  agent-cost, where the tokens and money went
  session demo-session · 22 events · /home/dev/acme-api

  TOTAL EST. COST  $0.61
  176.3K tokens · 117.5K in · 3.6K out · 51.2K cache-read
  2m 8s · 14 API responses · 11 tool calls
  8 transcript lines repeated a response and were counted once

  ⚠ RUNAWAY LOOPS DETECTED
  ×8 Bash, repeated action
      Bash:python -m pytest tests/test_billing.py -q
      events 5 to 20 · ~136.9K tokens wasted · ~$0.49 burned

  COST BY MODEL
       $0.43  claude-opus-4-5
             71.4K in · 2.2K out · 28.0K cache-read
       $0.18  claude-sonnet-4-5
             46.1K in · 1.3K out · 23.2K cache-read

  TOP EXPENSIVE TURNS
       $0.07  event 17  claude-opus-4-5  (320 out)
       $0.06  event 15  claude-opus-4-5  (320 out)
       ...

  CONTEXT BLOAT OFFENDERS  (big tool outputs fed back into context)
      7.6K tok  Read  event 2
             /home/dev/acme-api/src/generated/schema.py

  CACHE EFFICIENCY
  hit ratio 30%  (51.2K read / 4.0K written / 117.5K fresh)
  ~$0.19 saved by cache reads
```

That's a real report of [`examples/demo-session.jsonl`](examples/demo-session.jsonl), an agent that retried one failing test command 7 times before noticing a missing dependency, while a single 6,400-line file read sat in context inflating every prompt. The loop alone burned ~$0.49 of a $0.61 session. Run it yourself:

```bash
agent-cost report examples/demo-session.jsonl
```

## Install

```bash
pip install git+https://github.com/0xelitesystem/agent-cost
```

Python 3.10+. No other dependencies.

## Usage

```bash
# Full report for one session (path, session-id prefix, or 'latest')
agent-cost report latest
agent-cost report ./examples/demo-session.jsonl
agent-cost report 4f2a9c1               # session-id prefix

# Everything on this machine, every response counted once
agent-cost total
agent-cost total --json

# Which of my recent sessions cost the most?
agent-cost top --limit 10

# List recent transcripts
agent-cost list

# Machine-readable output / save a Markdown report
agent-cost report latest --json
agent-cost report latest --md cost-report.md

# Use it as a budget gate (exit 1 if the session blew past $5)
agent-cost report latest --fail-over 5.00

# Override the built-in prices with your own table
agent-cost report latest --prices my-prices.json
```

`--prices` takes a JSON file shaped like the built-in table, one entry per model id, rates in USD per million tokens:

```json
{
  "claude-opus-4-5":   { "input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.50 },
  "claude-sonnet-4-5": { "input": 3.0, "output": 15.0, "cache_write_5m": 3.75, "cache_write_1h": 6.0,  "cache_read": 0.30 }
}
```

Keys match the transcript's model id EXACTLY, after normalization: one trailing bracket suffix (`[1m]`) and one trailing `-YYYYMMDD` date are stripped, so `claude-haiku-4-5-20251001` finds `claude-haiku-4-5`. Substring matching is gone, because it let an unrelated id silently borrow another model's rates. `input`, `output` and `cache_read` are required; the two cache-write rates default to the documented 1.25x (5 minute) and 2x (1 hour) multipliers of the input rate.

## A note on prices

Every rate in the built-in table was read off [Anthropic's pricing page](https://platform.claude.com/docs/en/about-claude/pricing) on **2026-09-14**, and `agent_cost/pricing.py` carries that date and the source URLs for the cache, fast-mode and data-residency rules it applies. Prices still change, so verify before you trust a figure, and override per-run with `--prices FILE`.

A model that is not in the table is **unpriced**, not guessed: its tokens are counted, its id is listed under `UNPRICED MODELS`, and no dollar figure is invented for it. Earlier versions fell back to a mid-tier default rate, which produced a confident number for a model nobody had priced.

## Counting: one API response, counted once

Claude Code writes a single API response as SEVERAL JSONL records, one per content block, and every one of them repeats that response's `message.usage`. Summing usage record by record therefore counts the same tokens several times over. agent-cost counts each response once:

- deduplicate by `message.id`, globally across every transcript, because a subagent file can carry a copy of a parent's response
- fall back to `requestId`, then `uuid`, for a record that carries no message id
- split a `message.id` that carries two different non-empty `requestId`s, which is a genuine retry billed twice
- keep the record with the LARGEST `output_tokens`, because output is a running total and only the last record holds the final figure
- walk `~/.claude/projects` RECURSIVELY: subagents live in `<project>/<session>/subagents/` and workflow agents a level below that, and they are real spend

Versions before this one summed every record and looked only at the top level of each project folder. On a 689 MB real-world log set that read **$31,408** where the true API-equivalent figure was **$6,505**, a 4.8x overstatement: about 2.2x from double counting and another 2.2x from a stale Opus rate.

## Honest limitations

- **Token->$ is an estimate from a static table.** Token *counts* come straight from the transcript's usage records, so they're exact; the dollar figure is only as current as the price table. Verify / override with `--prices`.
- **Context-bloat sizing is approximate.** Tool results don't carry a token count, so offender ranking uses `chars / 4`, the standard rough tokens-per-char. It's used for ranking, never for the headline cost.
- **Loop detection is heuristic.** It flags the same action (tool + salient argument) repeated within a window. A legitimately repeated command, a deliberate retry-until-ready, can look the same as a death-spiral. The report shows you the action, the count, and the event range so you can judge.
- **The dollar figure is a lower bound.** It prices what the transcript records. Server-side tool calls that Claude Code never writes to the transcript, and anything billed outside the response record, are not in it.
- **`report` and `top` deduplicate within one transcript; `total` deduplicates across all of them.** A response copied into a subagent file is counted once by `total` and once per file by the other two.

## How it works

It parses the JSONL transcript (vendored parser, no dependencies), reduces the records to one per API response (see the counting rules above), prices each response against the table by `message.model`, including the 5-minute vs 1-hour cache-write split, fast-mode rates and the US data-residency multiplier when the usage block carries them, sizes every tool result fed back into context, and walks the tool-call stream for repeated-signature runs. That's it, pure stdlib, no network.

## Part of the agent accountability suite

- [agent-receipts](https://github.com/0xelitesystem/agent-receipts), did the agent's claims ("tests pass") match reality?
- [agent-leaks](https://github.com/0xelitesystem/agent-leaks), did it leak secrets into the transcript?
- [agent-blast-radius](https://github.com/0xelitesystem/agent-blast-radius), what irreversible actions did it take?
- [agent-rules](https://github.com/0xelitesystem/agent-rules), did it follow your `CLAUDE.md`?
- **agent-cost**, where did the tokens and money go?

## Roadmap

- Auto-pull current provider prices (opt-in, still offline by default)
- More providers and model families in the default table
- Per-project and per-day breakdowns of `agent-cost total`
- Tighter integration with the agent-receipts suite (cost + correctness in one pass)

## More

Part of a catalog of single-file browser tools and plain-language references, all MIT licensed and dependency-free: [0xelitesystem.github.io](https://0xelitesystem.github.io/). Built by [elitesystem.ai](https://elitesystem.ai).

## License

MIT © 2026 Salman Ahsan
