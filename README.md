# governor

A self-imposed usage cap for Claude Code.

It shows how much of your rate-limit window you have spent, right under the prompt,
and **stops a running task automatically** once you cross a cap you set yourself.

```
⛽ 5h █████░│░░░░░ 41% cap 50% ↻2h14m · 7d 63% · 2.9M tok
        └ used     └ your cap   └ window resets in
```

Built for the case where an account is shared: cap yourself at 50%, work
normally, and the agent halts on its own before it eats the other half. It is
equally useful alone — as a guard against one runaway task burning a window you
wanted for the rest of the day.

```
/governor 50          stop me once the 5-hour window is half spent
/governor weekly 80   same for the weekly window
/governor status      where do I stand
/governor pause       keep the cap, stop enforcing it
/governor off         drop the cap
```

## Install

```
/plugin marketplace add frodo4fingers/claude-code-governor
/plugin install governor@governor
```

Restart Claude Code, then wire up the status line and the short `/governor` command:

```
/governor install
```

Restart once more — the status line is read at startup — and set your cap:

```
/governor 50
```

<details>
<summary>What <code>/governor install</code> changes</summary>

It edits `~/.claude/settings.json` (backing it up first) to add the status line:

```json
"statusLine": {
  "type": "command",
  "command": "python3 \"~/.claude/governor/bin/governor\" statusline",
  "refreshInterval": 10
}
```

and links `~/.claude/commands/governor.md` so that plain `/governor` works.
Without it you still get `/governor:governor` (plugin skills are always
namespaced) but no gauge — and the gauge is what samples your usage, so the cap
cannot enforce anything without it.

If you already have a status line, it is kept: the old command is stored as
`passthrough` and chained, so your existing badge keeps rendering to the left of
the gauge. `/governor uninstall` puts everything back.

</details>

Requirements: Python 3.8+ on `PATH`, Claude Code 2.1+, and a subscription login
(see [limitations](#what-it-is-not)).

## Where the number comes from

The percentage is **not** an estimate. Anthropic returns your real utilisation on
every API response:

```
anthropic-ratelimit-unified-5h-utilization / -5h-reset
anthropic-ratelimit-unified-7d-utilization / -7d-reset
```

Claude Code parses those headers and passes them to the status line command on
stdin:

```json
"rate_limits": {
  "five_hour": {"used_percentage": 41.2, "resets_at": 1755530000},
  "seven_day": {"used_percentage": 63.0, "resets_at": 1755900000}
}
```

Hooks do **not** get that block — only the status line does. So the status line
is the sampler: it writes what it sees to `~/.claude/governor/state.json`, and
the hooks read it back. That file is the whole architecture.

The token counter next to it is separate: it folds `usage` from the session
transcripts in `~/.claude/projects/*/*.jsonl` into per-minute buckets,
incrementally (only bytes appended since the last render) and deduplicated by
message id — resumed and forked sessions copy earlier turns into new
transcripts, which would otherwise more than double the count.

## How the stop works

A `PreToolUse` hook fires before every tool call. Over the cap, it answers:

```json
{"continue": false, "stopReason": "governor: 5h window at 55.0%, your cap is 50% …"}
```

`continue: false` halts the agent mid-task — Claude Code records it as a
`hook_stopped_continuation` and shows your reason. A `UserPromptSubmit` hook
does the same for new prompts, so a long task cannot simply be restarted.

Commands that *lift* the cap are always allowed through, otherwise the guard
would deadlock: `/governor …` prompts, and any shell call that invokes the
governor binary. `GOVERNOR_DISABLE=1` in the environment bypasses everything.

## What "stop" means

It ends the turn; it does not freeze one. The session and its context survive
untouched — lift the cap and say "continue" and the work picks up where it
stopped. Nothing is suspended and nothing is resumed automatically.

The stop lands at the **next tool-call boundary**, so whatever is already in
flight finishes:

- the current assistant turn completes and is paid for — one turn is on the
  order of 100k tokens on a large context, so expect to land a point or two
  past your cap. Set the cap slightly under the number you actually mean.
- a command already running keeps running. `PreToolUse` fires *before* a call,
  so a task started earlier — especially a backgrounded one — is never killed.
- a turn that is pure text with no tool call is not interrupted at all; the
  guard gets its say on the next tool use or the next prompt.

Once stopped, `UserPromptSubmit` refuses new prompts too, so the task cannot be
casually restarted. When the window rolls over, the sample expires and the
guard releases on its own — no command needed.

## Config

`~/.claude/governor/config.json`, editable with `/governor` or
`governor config <key> <value>`:

| key | default | meaning |
|---|---|---|
| `five_hour_cap` | `null` | percent of the 5h window you allow yourself |
| `seven_day_cap` | `null` | same for the weekly window |
| `warn_ratio` | `0.8` | warn once past this fraction of the cap |
| `mode` | `stop` | `stop` halts the agent, `warn` only tells you |
| `show_tokens` | `true` | token counter in the status line |
| `show_weekly` | `auto` | `auto` shows 7d when capped or above 50% |
| `passthrough` | `null` | status line command to render before ours |
| `note` | `null` | reminder shown when the cap fires |
| `enabled` | `true` | `pause` sets this to false |

Nothing is written inside the plugin — all state lives in `~/.claude/governor/`.

## What it is not

- **Not a security boundary.** It is a cap you set on yourself; anything that
  can run a shell can lift it. It stops runaway tasks, not a determined person.
- **The status line is the sampler.** Headless `claude -p` runs render no status
  line, so the guard uses the last sample an interactive session took. A sample
  is discarded once its own window has expired, so a stale sample can never keep
  the cap engaged forever — it fails open, not shut.
- **No `rate_limits` block, no percentage cap.** API-key, Bedrock and Vertex
  users do not get those headers; the gauge shows `5h —` and only the token
  counter works.

## Development

```bash
git clone git@github.com:frodo4fingers/claude-code-governor.git
cd claude-code-governor
claude --plugin-dir .        # load this checkout live, no install
```

Installing copies the directory into
`~/.claude/plugins/cache/governor/governor/<version>/`, and the cache is keyed by
version — editing the checkout does not affect an installed copy. After a change:

```bash
claude plugin validate .                  # the host's own manifest checker
# bump "version" in .claude-plugin/plugin.json
claude plugin update governor@governor    # restart to apply
```

`./install.sh` is the same setup as `/governor install`, for use from a checkout:
`./install.sh --dry-run`, `--cap 50`, `--no-statusline`, `--uninstall`.

```
bin/governor              engine: statusline, guard, CLI (stdlib only)
hooks/hooks.json          SessionStart, UserPromptSubmit, PreToolUse
commands/governor.md      the /governor command
.claude-plugin/           plugin + marketplace manifests
```

## License

MIT — see [LICENSE](LICENSE).
