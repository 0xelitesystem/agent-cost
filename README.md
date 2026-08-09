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
  session demo-session · 14 events · /home/dev/acme-api

  TOTAL EST. COST  $1.46
  174.7K tokens · 117.5K in · 3.6K out · 51.2K cache-read
  1m 36s · 14 turns · 11 tool calls

  ⚠ RUNAWAY LOOPS DETECTED
  ×8 Bash, repeated action
      Bash:python -m pytest tests/test_billing.py -q
      events 3 to 12 · ~135.3K tokens wasted · ~$1.23 burned

  COST BY MODEL
       $1.28  claude-opus-4-20250514
             71.4K in · 2.2K out · 28.0K cache-read
       $0.17  claude-sonnet-4-20250514
             46.1K in · 1.3K out · 23.2K cache-read

  TOP EXPENSIVE TURNS
       $0.20  event 9  claude-opus-4-20250514  (320 out)
       $0.19  event 8  claude-opus-4-20250514  (320 out)
       ...

  CONTEXT BLOAT OFFENDERS  (big tool outputs fed back into context)
      7.6K tok  Read  event 1
             /home/dev/acme-api/src/generated/schema.py

  CACHE EFFICIENCY
  hit ratio 30%  (51.2K read / 2.4K written / 117.5K fresh)
  ~$0.44 saved by cache reads
```

That's a real report of [`examples/demo-session.jsonl`](examples/demo-session.jsonl), an agent that retried one failing test command 7 times before noticing a missing dependency, while a single 6,400-line file read sat in context inflating every prompt. The loop alone burned ~$1.23 of a $1.46 session. Run it yourself:

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

`--prices` takes a JSON file shaped like the built-in table, one entry per model key, rates in USD per million tokens:

```json
{
  "opus":   { "input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50 },
  "sonnet": { "input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30 },
  "haiku":  { "input": 0.80, "output": 4.0,  "cache_write": 1.0,   "cache_read": 0.08 }
}
```

Keys match by substring against the transcript's model id (longest key wins), so `sonnet` covers any `claude-...-sonnet-...` id without pinning a date.

## A note on prices

The built-in pricing table holds **approximate defaults you should verify** against the provider's current price list, prices change and the table is hand-maintained. Override per-run with `--prices FILE` whenever they drift. Unknown models fall back to a default rate and are flagged `(rate unknown)` in the report so an estimate is never silently trusted.

## Honest limitations

- **Token→$ is an estimate from a static table.** Token *counts* come straight from the transcript's usage records, so they're exact; the dollar figure is only as current as the price table. Verify / override with `--prices`.
- **Context-bloat sizing is approximate.** Tool results don't carry a token count, so offender ranking uses `chars / 4`, the standard rough tokens-per-char. It's used for ranking, never for the headline cost.
- **Loop detection is heuristic.** It flags the same action (tool + salient argument) repeated within a window. A legitimately repeated command, a deliberate retry-until-ready, can look the same as a death-spiral. The report shows you the action, the count, and the event range so you can judge.

## How it works

It parses the JSONL transcript (vendored parser, no dependencies), sums `message.usage` across every assistant record, prices each record against the table by `message.model`, sizes every tool result fed back into context, and walks the tool-call stream for repeated-signature runs. That's it, pure stdlib, no network.

## Part of the agent accountability suite

- [agent-receipts](https://github.com/0xelitesystem/agent-receipts), did the agent's claims ("tests pass") match reality?
- [agent-leaks](https://github.com/0xelitesystem/agent-leaks), did it leak secrets into the transcript?
- [agent-blast-radius](https://github.com/0xelitesystem/agent-blast-radius), what irreversible actions did it take?
- [agent-rules](https://github.com/0xelitesystem/agent-rules), did it follow your `CLAUDE.md`?
- **agent-cost**, where did the tokens and money go?

## Roadmap

- Auto-pull current provider prices (opt-in, still offline by default)
- More providers and model families in the default table
- Tighter integration with the agent-receipts suite (cost + correctness in one pass)

## More

Part of a catalog of single-file browser tools and plain-language references, all MIT licensed and dependency-free: [0xelitesystem.github.io](https://0xelitesystem.github.io/). Built by [elitesystem.ai](https://elitesystem.ai).

## License

MIT © 2026 Salman Ahsan
