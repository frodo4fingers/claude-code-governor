# governor — todo

## v0.1 (done)
- [x] Establish where a real usage percentage can be read from
      → status line stdin `rate_limits.five_hour.used_percentage`, from the
        `anthropic-ratelimit-unified-5h-*` response headers (Claude Code 2.1.234)
- [x] Bridge status-line-only data to hooks via `~/.claude/governor/state.json`
- [x] `PreToolUse` guard halting with `{"continue": false, "stopReason": …}`
- [x] `UserPromptSubmit` guard so a halted task cannot just be re-prompted
- [x] Self-call whitelist so `/governor off` can never be blocked (deadlock guard)
- [x] Expired-sample handling — fails open when the window has rolled over
- [x] Status line renderer: bar, cap marker, reset countdown, weekly, token count
- [x] Incremental transcript token index, deduped by message id
- [x] `/governor` slash command + install/uninstall with status line passthrough
- [x] Verified: differential `claude -p` run (control answers, armed halts)

## Next
- [ ] `--for <duration>` — cap only for the next N hours, then auto-release
- [ ] Warn escalation: single warning at warn_ratio, second at 95% of cap
- [ ] `governor log` — history of caps hit, to see whether the split is fair
- [ ] Handle `seven_day_opus` / `seven_day_sonnet` sub-limits (headers exist:
      `anthropic-ratelimit-unified-7d_oi-*`, model-specific weekly buckets)
- [ ] Optional token-budget cap for API-key users (no rate_limits headers)
- [x] One-shot install.sh - marketplace + plugin + status line via settings alone
- [x] Publish as a git repo so the other account holder can install it by URL

## Tests
- [x] `tests/test_governor.py` — stdlib unittest, no deps, hermetic
      (`GOVERNOR_HOME` + `GOVERNOR_TRANSCRIPTS` both redirected to a tmpdir)
- [x] Self-call whitelist: 9 spellings allowed, 8 foreign commands blocked,
      1 documented permissive case (`echo "governor off"`) pinned so a future
      tightening is a visible diff
- [x] `read_limit` expiry, staleness, malformed samples — the fail-open path
- [x] `evaluate` bands, disabled config, worst-window-wins ranking
- [x] Index: dedupe by message id, horizon, incremental offsets, half-written
      line, truncated file
- [x] Guard as a subprocess: the actual `{"continue": false}` JSON, warn mode,
      throttling, the three no-op paths, garbage stdin
- [ ] CI (GitHub Actions) running the suite on push

## Review

Data source was the whole question. Two options existed: estimate from
transcript tokens (what ccusage-style tools do) or find the real number. The
real number turned out to be reachable — Claude Code already parses the unified
rate-limit headers and hands them to the status line — so the cap is exact
rather than a guess, and no plan-specific token budget has to be configured.

The one structural awkwardness: hooks are not given that block, so the status
line has to persist it for them. That makes the status line a required
component rather than a cosmetic one, and makes headless runs depend on a
sample taken by an interactive session. Documented rather than hidden.

## Publishing
- [x] MIT LICENSE, author frodo4fingers, homepage/repository metadata
- [x] Public README (no local paths, marketplace install instructions)
- [x] statusLine points at ~/.claude/governor/bin/governor, not a versioned cache dir
      (a plugin update deletes the old directory and would break the gauge)
- [x] `install` skips marketplace registration when already installed from one
- [x] push to github.com/frodo4fingers/claude-code-governor
- [x] tag v0.2.0 once pushed (`claude plugin tag` validates manifest agreement)
