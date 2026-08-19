---
description: Cap your share of the Claude Code usage window, or inspect where you stand
argument-hint: "[50 | 50 for 3h | weekly 80 | off | pause | on | status]"
allowed-tools: Bash(python3:*)
---

Run the governor CLI with the arguments below and report its output verbatim.
Nothing else - no summary, no commentary, no follow-up work.

Binary: `python3 ~/.claude/governor/bin/governor`
(if that path is missing, use `python3 "${CLAUDE_PLUGIN_ROOT}/bin/governor"`)

Arguments given: `$ARGUMENTS`

Translate them like this:

| user typed              | run                                    |
|-------------------------|----------------------------------------|
| (nothing) or `status`   | `status`                               |
| `50` / `50%`            | `set 50`                               |
| `50 for 3h`             | `set 50 --for 3h`                      |
| `weekly 80 for 2d`      | `set 80 --weekly --for 2d`             |
| `weekly 80`             | `set 80 --weekly`                      |
| `off`                   | `off`                                  |
| `off all`               | `off --all`                            |
| `pause`                 | `pause`                                |
| `on`                    | `on`                                   |
| `note <text>`           | `config note "<text>"`                 |
| `warn 0.9`              | `config warn_ratio 0.9`                |
| `mode warn` / `mode stop` | `config mode warn` / `config mode stop` |
| `install` / `uninstall` | `install` / `uninstall`                |
| anything else           | `status`, and say the argument was not understood |

A cap is a percentage of the usage window, not of your remaining budget:
`/governor 50` means "stop me once the 5-hour window is half spent".

`for <duration>` makes the cap release itself after that long - `3h`, `90m`,
`2h30m`, `2d`. A bare number is not a duration; if the user writes `for 3`, ask
whether they mean `3h` or `3m` rather than guessing.
