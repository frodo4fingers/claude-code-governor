"""Tests for the governor engine.

Run: python3 -m unittest discover -s tests -v

Two things here carry real risk and are covered first:

  * the self-call whitelist - a miss locks the user out of `/governor off`
  * `read_limit` expiry - a miss wedges the cap on after the window rolls over

Everything else is arithmetic and formatting.
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin" / "governor"

# Import the extensionless script as a module. GOVERNOR_HOME must be set first:
# the path constants are evaluated at import time, and we never want a test run
# to touch the real ~/.claude/governor.
os.environ["GOVERNOR_HOME"] = tempfile.mkdtemp(prefix="governor-import-")
os.environ["NO_COLOR"] = "1"

_loader = importlib.machinery.SourceFileLoader("governor", str(BIN))
_spec = importlib.util.spec_from_loader("governor", _loader)
gov = importlib.util.module_from_spec(_spec)
_loader.exec_module(gov)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_at(pct, updated_at, resets_at=None, key="five_hour", weekly=None):
    limits = {key: {"used_percentage": pct, "resets_at": resets_at}}
    if weekly:
        limits["seven_day"] = weekly
    return {"limits": limits, "limits_updated_at": updated_at}


class Sandboxed(unittest.TestCase):
    """Redirect every module path constant into a throwaway directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gov-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self._swap({
            "HOME": self.tmp,
            "CONFIG_PATH": self.tmp / "config.json",
            "STATE_PATH": self.tmp / "state.json",
            "INDEX_PATH": self.tmp / "index.json",
            "WARN_PATH": self.tmp / "warn.json",
            "LOG_PATH": self.tmp / "log.jsonl",
            "STABLE_BIN": self.tmp / "bin" / "governor",
            "TRANSCRIPT_GLOB": str(self.projects / "*" / "*.jsonl"),
        })

    def _swap(self, values):
        for name, value in values.items():
            original = getattr(gov, name)
            setattr(gov, name, value)
            self.addCleanup(setattr, gov, name, original)


# --------------------------------------------------------------------- deadlock

class TestSelfCall(Sandboxed):
    """The guard must never block a command that lifts the cap."""

    def assertSelf(self, command):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        self.assertTrue(gov.is_self_call("PreToolUse", payload), command)

    def assertForeign(self, command):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        self.assertFalse(gov.is_self_call("PreToolUse", payload), command)

    def test_every_spelling_of_the_binary_is_recognised(self):
        for command in [
            "governor off",
            "governor status",
            "python3 /home/u/.claude/governor/bin/governor off",
            "python3 ~/.claude/governor/bin/governor set 50",
            'python3 "/opt/plugins/governor/bin/governor" guard --event PreToolUse',
            "'/opt/governor' status",
            "governor --json status",
            "cd /tmp && governor on",
            "governor session-start",
        ]:
            self.assertSelf(command)

    def test_stable_bin_path_alone_is_enough(self):
        # No subcommand on the line, but it is unmistakably our binary.
        self.assertSelf(f"python3 {gov.STABLE_BIN}")

    def test_unrelated_commands_are_not_whitelisted(self):
        for command in [
            "pytest -q",
            "echo governor",
            "rm -rf build",
            'git commit -m "governor"',
            "governorctl status",
            "cat governor.md",
            "grep governor bin/*",
            "./governor-helper status",
        ]:
            self.assertForeign(command)

    def test_known_permissive_case(self):
        # A quoted literal that happens to read like an invocation matches. This
        # errs toward letting a command through, never toward a lockout; the
        # assertion is here so that tightening the regex is a visible change.
        self.assertSelf('echo "governor off"')

    def test_non_bash_tools_are_never_self_calls(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/x/governor status"}}
        self.assertFalse(gov.is_self_call("PreToolUse", payload))

    def test_slash_command_prompts(self):
        for prompt, expected in [
            ("/governor off", True),
            ("/governor 40", True),
            ("/governor", True),
            ("  /GOVERNOR status  ", True),
            ("/user:governor status", True),
            ("/gov off", False),
            ("write a governor for the queue", False),
            ("", False),
        ]:
            got = gov.is_self_call("UserPromptSubmit", {"prompt": prompt})
            self.assertEqual(got, expected, prompt)


# ------------------------------------------------------------------- fail open

class TestEpoch(unittest.TestCase):
    def test_seconds_pass_through(self):
        self.assertEqual(gov._epoch(1_700_000_000), 1_700_000_000.0)

    def test_milliseconds_are_scaled(self):
        self.assertEqual(gov._epoch(1_700_000_000_000), 1_700_000_000.0)

    def test_unusable_values(self):
        for value in (0, -1, None, "", "later", {}):
            self.assertIsNone(gov._epoch(value), value)


class TestReadLimit(unittest.TestCase):
    def test_fresh_sample(self):
        now = 1000.0
        sample = gov.read_limit(state_at(41.0, now - 30, now + 3600), "five_hour", now)
        self.assertEqual(sample["pct"], 41.0)
        self.assertEqual(sample["age"], 30)

    def test_expired_window_is_dropped(self):
        # The utilisation belongs to a window that no longer exists. Trusting it
        # would keep the cap engaged forever.
        now = 1000.0
        state = state_at(99.0, now - 30, resets_at=now - 1)
        self.assertIsNone(gov.read_limit(state, "five_hour", now))

    def test_reset_exactly_now_is_dropped(self):
        now = 1000.0
        self.assertIsNone(gov.read_limit(state_at(99.0, now, now), "five_hour", now))

    def test_stale_sample_is_dropped(self):
        now = 1000.0 + gov.FIVE_HOURS
        state = state_at(99.0, 1000.0 - 1)  # older than the 5h window, no reset time
        self.assertIsNone(gov.read_limit(state, "five_hour", now))

    def test_sample_exactly_one_window_old_survives(self):
        now = 1000.0 + gov.FIVE_HOURS
        state = state_at(99.0, 1000.0)
        self.assertIsNotNone(gov.read_limit(state, "five_hour", now))

    def test_weekly_uses_the_longer_window(self):
        now = 1000.0 + 6 * 86400
        state = {"limits": {"seven_day": {"used_percentage": 63.0, "resets_at": None}},
                 "limits_updated_at": 1000.0}
        self.assertIsNotNone(gov.read_limit(state, "seven_day", now))
        state["limits"]["five_hour"] = {"used_percentage": 63.0, "resets_at": None}
        self.assertIsNone(gov.read_limit(state, "five_hour", now))

    def test_malformed_samples(self):
        now = 1000.0
        for limits in ({}, {"five_hour": None}, {"five_hour": {"used_percentage": "n/a"}},
                       {"five_hour": {}}):
            state = {"limits": limits, "limits_updated_at": now}
            self.assertIsNone(gov.read_limit(state, "five_hour", now), limits)

    def test_missing_update_timestamp_is_treated_as_ancient(self):
        now = time.time()
        state = {"limits": {"five_hour": {"used_percentage": 99.0, "resets_at": None}}}
        self.assertIsNone(gov.read_limit(state, "five_hour", now))


class TestEvaluate(unittest.TestCase):
    def verdict(self, pct, now=1000.0, **cfg):
        base = dict(gov.DEFAULT_CONFIG)
        base.update(cfg)
        return gov.evaluate(base, state_at(pct, now - 10, now + 3600), now)

    def test_no_cap_never_stops(self):
        self.assertEqual(self.verdict(99.0)["action"], "allow")

    def test_below_warn_ratio(self):
        self.assertEqual(self.verdict(30.0, five_hour_cap=50)["action"], "allow")

    def test_warn_band(self):
        v = self.verdict(41.0, five_hour_cap=50)  # 0.8 * 50 = 40
        self.assertEqual(v["action"], "warn")
        self.assertEqual(v["cap"], 50.0)
        self.assertEqual(v["key"], "five_hour")

    def test_warn_boundary_is_inclusive(self):
        self.assertEqual(self.verdict(40.0, five_hour_cap=50)["action"], "warn")

    def test_stop_at_the_cap(self):
        v = self.verdict(50.0, five_hour_cap=50)
        self.assertEqual(v["action"], "stop")
        self.assertEqual(v["pct"], 50.0)

    def test_disabled_config_allows_everything(self):
        self.assertEqual(self.verdict(99.0, five_hour_cap=50, enabled=False)["action"], "allow")

    def test_stale_sample_allows(self):
        cfg = dict(gov.DEFAULT_CONFIG, five_hour_cap=50)
        now = 1000.0
        stale = state_at(99.0, now - gov.FIVE_HOURS - 1)
        self.assertEqual(gov.evaluate(cfg, stale, now)["action"], "allow")

    def test_tiers(self):
        for pct, action, tier in [(39.0, "allow", None), (40.0, "warn", "warn"),
                                  (47.4, "warn", "warn"), (47.5, "warn", "final"),
                                  (49.9, "warn", "final"), (50.0, "stop", "stop")]:
            v = self.verdict(pct, five_hour_cap=50)
            self.assertEqual((v["action"], v["tier"]), (action, tier), pct)

    def test_an_escalation_below_the_first_warning_is_switched_off(self):
        # It cannot be reached without swallowing the first warning, so the
        # second tier simply does not exist for that config - warnings stay
        # warnings all the way to the cap.
        cfg = dict(five_hour_cap=50, warn_ratio=0.8, escalate_ratio=0.5)
        self.assertEqual(self.verdict(30.0, **cfg)["action"], "allow")
        self.assertEqual(self.verdict(41.0, **cfg)["tier"], "warn")
        self.assertEqual(self.verdict(49.9, **cfg)["tier"], "warn")

    def test_an_escalation_equal_to_the_first_warning_is_switched_off(self):
        cfg = dict(five_hour_cap=50, warn_ratio=0.8, escalate_ratio=0.8)
        self.assertEqual(self.verdict(49.9, **cfg)["tier"], "warn")

    def test_a_final_warning_outranks_a_first_one_in_another_window(self):
        now = 1000.0
        cfg = dict(gov.DEFAULT_CONFIG, five_hour_cap=50, seven_day_cap=60)
        state = state_at(41.0, now - 10, now + 3600,          # 5h: plain warn
                         weekly={"used_percentage": 58.0,      # 7d: final warn
                                 "resets_at": now + 86400})
        v = gov.evaluate(cfg, state, now)
        self.assertEqual((v["tier"], v["key"]), ("final", "seven_day"))

    def test_worst_window_wins(self):
        now = 1000.0
        cfg = dict(gov.DEFAULT_CONFIG, five_hour_cap=50, seven_day_cap=60)
        state = state_at(41.0, now - 10, now + 3600,
                         weekly={"used_percentage": 75.0, "resets_at": now + 86400})
        v = gov.evaluate(cfg, state, now)
        self.assertEqual(v["action"], "stop")       # weekly stop outranks the 5h warn
        self.assertEqual(v["key"], "seven_day")


# --------------------------------------------------------------- self-release

class TestParseDuration(unittest.TestCase):
    def test_accepted_forms(self):
        for text, seconds in [("45s", 45), ("90m", 5400), ("3h", 10800), ("2d", 172800),
                              ("2h30m", 9000), ("2h 30m", 9000), ("3 h", 10800),
                              ("1.5h", 5400), ("3H", 10800)]:
            self.assertEqual(gov._parse_duration(text), seconds, text)

    def test_a_bare_number_is_refused(self):
        # `--for 3` reads as hours or minutes with equal plausibility, and
        # guessing wrong on a self-releasing cap fails silently.
        with self.assertRaises(ValueError):
            gov._parse_duration("3")

    def test_other_rejects(self):
        for text in ("", "abc", "h", "3x", "-1h", "3h30", "0h", "0s"):
            with self.assertRaises(ValueError, msg=text):
                gov._parse_duration(text)

    def test_upper_bound_is_a_week(self):
        self.assertEqual(gov._parse_duration("7d"), gov.SEVEN_DAYS)
        with self.assertRaises(ValueError):
            gov._parse_duration("8d")


class TestExpiringCaps(unittest.TestCase):
    def cfg(self, **kw):
        return dict(gov.DEFAULT_CONFIG, five_hour_cap=50, **kw)

    def test_a_live_cap_still_stops(self):
        now = 1000.0
        v = gov.evaluate(self.cfg(five_hour_expires_at=now + 600),
                         state_at(60.0, now, now + 3600), now)
        self.assertEqual(v["action"], "stop")
        self.assertEqual(v["expires_at"], now + 600)

    def test_an_expired_cap_no_longer_stops(self):
        now = 1000.0
        v = gov.evaluate(self.cfg(five_hour_expires_at=now - 1),
                         state_at(99.0, now, now + 3600), now)
        self.assertEqual(v["action"], "allow")

    def test_expiry_is_per_window(self):
        now = 1000.0
        cfg = self.cfg(five_hour_expires_at=now - 1, seven_day_cap=60)
        state = state_at(99.0, now, now + 3600,
                         weekly={"used_percentage": 70.0, "resets_at": now + 86400})
        v = gov.evaluate(cfg, state, now)
        self.assertEqual(v["action"], "stop")        # the weekly cap has no expiry
        self.assertEqual(v["key"], "seven_day")

    def test_expire_caps_clears_only_what_has_passed(self):
        now = 1000.0
        cfg = self.cfg(five_hour_expires_at=now - 1,
                       seven_day_cap=60, seven_day_expires_at=now + 60)
        self.assertTrue(gov.expire_caps(cfg, now))
        self.assertIsNone(cfg["five_hour_cap"])
        self.assertIsNone(cfg["five_hour_expires_at"])
        self.assertEqual(cfg["seven_day_cap"], 60)

    def test_expire_caps_is_a_no_op_without_expiries(self):
        cfg = self.cfg()
        self.assertFalse(gov.expire_caps(cfg, 1000.0))
        self.assertEqual(cfg["five_hour_cap"], 50)

    def test_the_halt_says_the_cap_releases_itself(self):
        now = 1000.0
        verdict = {"action": "stop", "key": "five_hour", "pct": 55.0, "cap": 50.0,
                   "resets_at": None, "expires_at": now + 5400}
        text = gov.stop_message(verdict, dict(gov.DEFAULT_CONFIG), now)
        self.assertIn("lifts itself in 1h30m", text)


class TestParseTokens(unittest.TestCase):
    def test_accepted_forms(self):
        for text, count in [("4M", 4_000_000), ("500k", 500_000), ("4000000", 4_000_000),
                            ("1.5M", 1_500_000), ("4,000,000", 4_000_000),
                            ("4_000_000", 4_000_000), (" 2m ", 2_000_000)]:
            self.assertEqual(gov._parse_tokens(text), count, text)

    def test_rejects(self):
        for text in ("", "abc", "-5", "4g", "4MB", "1.2.3"):
            with self.assertRaises(ValueError, msg=text):
                gov._parse_tokens(text)


class TestWindowStart(unittest.TestCase):
    def test_anchored_to_the_real_reset_when_headers_gave_one(self):
        now = 1_000_000.0
        state = state_at(41.0, now, now + 3600)
        self.assertEqual(gov.window_start(state, now), now + 3600 - gov.FIVE_HOURS)

    def test_rolling_when_there_is_no_sample(self):
        now = 1_000_000.0
        self.assertEqual(gov.window_start({}, now), now - gov.FIVE_HOURS)

    def test_weekly_span(self):
        now = 1_000_000.0
        self.assertEqual(gov.window_start({}, now, "seven_day"), now - gov.SEVEN_DAYS)


class TestTokenBudget(unittest.TestCase):
    def verdict(self, used, now=1000.0, state=None, **cfg):
        base = dict(gov.DEFAULT_CONFIG)
        base.update(cfg)
        return gov.evaluate(base, state if state is not None else {}, now,
                            {"five_hour": used, "seven_day": used})

    def test_under_budget(self):
        self.assertEqual(self.verdict(500_000, token_budget=1_000_000)["action"], "allow")

    def test_warn_band(self):
        v = self.verdict(850_000, token_budget=1_000_000)
        self.assertEqual((v["action"], v["tier"], v["source"]), ("warn", "warn", "tokens"))

    def test_final_band(self):
        self.assertEqual(self.verdict(960_000, token_budget=1_000_000)["tier"], "final")

    def test_at_the_budget(self):
        v = self.verdict(1_000_000, token_budget=1_000_000)
        self.assertEqual(v["action"], "stop")
        self.assertEqual(v["used"], 1_000_000)
        self.assertEqual(v["budget"], 1_000_000)

    def test_no_budget_means_no_opinion(self):
        self.assertEqual(self.verdict(9_000_000)["action"], "allow")

    def test_no_count_means_no_opinion(self):
        cfg = dict(gov.DEFAULT_CONFIG, token_budget=1_000)
        self.assertEqual(gov.evaluate(cfg, {}, 1000.0, None)["action"], "allow")

    def test_a_zero_budget_is_ignored_rather_than_always_stopping(self):
        self.assertEqual(self.verdict(1, token_budget=0)["action"], "allow")

    def test_the_weekly_budget_is_separate(self):
        v = self.verdict(600_000, token_budget=None, token_budget_weekly=500_000)
        self.assertEqual((v["action"], v["key"]), ("stop", "seven_day"))

    def test_a_percentage_cap_and_a_budget_coexist(self):
        now = 1000.0
        v = self.verdict(100, now=now, state=state_at(60.0, now, now + 3600),
                         five_hour_cap=50, token_budget=1_000_000)
        self.assertEqual((v["action"], v["source"]), ("stop", "window"))

    def test_the_stricter_of_the_two_wins(self):
        now = 1000.0
        v = self.verdict(1_000_000, now=now, state=state_at(41.0, now, now + 3600),
                         five_hour_cap=50, token_budget=1_000_000)
        self.assertEqual((v["tier"], v["source"]), ("stop", "tokens"))

    def test_disabled_config_ignores_budgets(self):
        self.assertEqual(self.verdict(9_000_000, token_budget=1_000,
                                      enabled=False)["action"], "allow")

    def test_the_halt_message_speaks_tokens(self):
        v = self.verdict(1_200_000, token_budget=1_000_000)
        text = gov.stop_message(v, dict(gov.DEFAULT_CONFIG), 1000.0)
        self.assertIn("1.2M of your 1.0M token budget", text)
        self.assertIn("/governor budget off", text)
        self.assertNotIn("<percent>", text)

    def test_the_warning_speaks_tokens(self):
        v = self.verdict(850_000, token_budget=1_000_000)
        self.assertIn("850k of your 1.0M token budget", gov.warn_message(v, 1000.0))

    def test_the_final_warning_counts_tokens_left(self):
        v = self.verdict(960_000, token_budget=1_000_000)
        self.assertIn("40k left", gov.warn_message(v, 1000.0))


# ------------------------------------------------------------------ formatting

class TestFormatting(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(gov.fmt_duration(0), "0s")
        self.assertEqual(gov.fmt_duration(-5), "0s")
        self.assertEqual(gov.fmt_duration(45), "45s")
        self.assertEqual(gov.fmt_duration(90), "1m")
        self.assertEqual(gov.fmt_duration(3600), "1h00m")
        self.assertEqual(gov.fmt_duration(8040), "2h14m")

    def test_tokens(self):
        self.assertEqual(gov.fmt_tokens(0), "0")
        self.assertEqual(gov.fmt_tokens(999), "999")
        self.assertEqual(gov.fmt_tokens(1000), "1k")
        self.assertEqual(gov.fmt_tokens(2_949_009), "2.9M")

    def test_bar_marks_the_cap(self):
        self.assertEqual(gov.bar(41.0, 50.0), "█████░│░░░░░")

    def test_bar_past_the_cap_fills_the_marker(self):
        self.assertEqual(gov.bar(60.0, 50.0), "███████░░░░░")

    def test_bar_without_a_cap_has_no_marker(self):
        self.assertNotIn("│", gov.bar(41.0, None))

    def test_bar_ignores_a_100_percent_cap(self):
        self.assertNotIn("│", gov.bar(41.0, 100.0))

    def test_bar_clamps(self):
        self.assertEqual(gov.bar(150.0, None), "█" * 12)
        self.assertEqual(gov.bar(-5.0, None), "░" * 12)

    def test_bar_width_is_constant(self):
        for pct in (0, 1, 33.3, 50, 99.9, 100):
            self.assertEqual(len(gov.bar(pct, 50.0)), 12, pct)

    def test_tone_thresholds(self):
        green, amber, red = "38;5;114", "38;5;179", "38;5;203"
        self.assertEqual(gov.tone(10.0, 50.0, 0.8), green)
        self.assertEqual(gov.tone(40.0, 50.0, 0.8), amber)
        self.assertEqual(gov.tone(50.0, 50.0, 0.8), red)
        self.assertEqual(gov.tone(10.0, None, 0.8), "38;5;245")


class TestMessages(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.verdict = {"action": "stop", "key": "five_hour", "pct": 55.4,
                        "cap": 55.0, "resets_at": self.now + 8040}

    def test_stop_message_states_the_numbers_and_the_way_out(self):
        text = gov.stop_message(self.verdict, dict(gov.DEFAULT_CONFIG), self.now)
        self.assertIn("5h window at 55.4%", text)
        self.assertIn("your cap is 55%", text)
        self.assertIn("2h14m", text)
        self.assertIn("/governor off", text)

    def test_note_is_appended_with_punctuation(self):
        cfg = dict(gov.DEFAULT_CONFIG, note="sharing with Ana")
        self.assertIn("sharing with Ana.", gov.stop_message(self.verdict, cfg, self.now))

    def test_note_keeps_existing_punctuation(self):
        cfg = dict(gov.DEFAULT_CONFIG, note="sharing with Ana!")
        text = gov.stop_message(self.verdict, cfg, self.now)
        self.assertIn("sharing with Ana!", text)
        self.assertNotIn("Ana!.", text)

    def test_warn_message(self):
        v = dict(self.verdict, action="warn", pct=45.0)
        text = gov.warn_message(v, self.now)
        self.assertIn("5h usage 45.0% of your 55% cap", text)
        self.assertIn("resets in 2h14m", text)

    def test_final_warning_counts_the_room_left(self):
        v = dict(self.verdict, action="warn", tier="final", pct=53.5, cap=55.0)
        text = gov.warn_message(v, self.now)
        self.assertIn("1.5 points under your 55% cap", text)
        self.assertIn("The next turn may reach it", text)

    def test_first_warning_keeps_the_plain_wording(self):
        v = dict(self.verdict, action="warn", tier="warn", pct=45.0)
        self.assertIn("Work will stop at the cap.", gov.warn_message(v, self.now))

    def test_weekly_wording(self):
        v = dict(self.verdict, key="seven_day")
        self.assertIn("weekly window", gov.stop_message(v, dict(gov.DEFAULT_CONFIG), self.now))


class TestWarnThrottle(Sandboxed):
    def test_the_same_tier_is_throttled(self):
        self.assertTrue(gov.throttled_warn("warn", now=1000.0))
        self.assertFalse(gov.throttled_warn("warn", now=1010.0))

    def test_the_interval_eventually_lapses(self):
        gov.throttled_warn("warn", now=1000.0)
        self.assertTrue(gov.throttled_warn("warn", now=1400.0))

    def test_a_change_of_tier_is_never_held_back(self):
        # Escalation delivered after the halt it was meant to precede is useless.
        gov.throttled_warn("warn", now=1000.0)
        self.assertTrue(gov.throttled_warn("final", now=1001.0))

    def test_the_new_tier_then_throttles_on_its_own(self):
        gov.throttled_warn("warn", now=1000.0)
        gov.throttled_warn("final", now=1001.0)
        self.assertFalse(gov.throttled_warn("final", now=1002.0))


class TestHistory(Sandboxed):
    def test_round_trip(self):
        gov.log_event("set", now=1000.0, key="five_hour", cap=50.0)
        gov.log_event("stop", now=1010.0, key="five_hour", pct=55.4, cap=50.0)
        records = gov.read_log()
        self.assertEqual([r["event"] for r in records], ["set", "stop"])
        self.assertEqual(records[1]["pct"], 55.4)

    def test_none_fields_are_dropped(self):
        gov.log_event("pause", now=1000.0, key=None, cap=None)
        self.assertEqual(gov.read_log()[0], {"ts": 1000.0, "event": "pause"})

    def test_unreadable_lines_are_skipped(self):
        gov.log_event("set", now=1000.0, cap=50.0)
        with open(gov.LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("not json\n\n[1,2,3]\n")
        gov.log_event("off", now=1001.0)
        self.assertEqual([r["event"] for r in gov.read_log()], ["set", "off"])

    def test_since_filter(self):
        gov.log_event("set", now=1000.0)
        gov.log_event("off", now=2000.0)
        self.assertEqual([r["event"] for r in gov.read_log(since=1500.0)], ["off"])

    def test_trim_drops_what_is_past_the_horizon(self):
        now = 1_000_000.0
        gov.log_event("set", now=now - (gov.LOG_KEEP_DAYS + 1) * 86400)
        gov.log_event("off", now=now - 60)
        gov.trim_log(now)
        self.assertEqual([r["event"] for r in gov.read_log()], ["off"])

    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(gov.read_log(), [])


class TestStopIsLoggedOncePerWindow(Sandboxed):
    def verdict(self, resets_at, pct=55.0):
        return {"action": "stop", "key": "five_hour", "pct": pct, "cap": 50.0,
                "resets_at": resets_at}

    def test_repeated_calls_write_one_line(self):
        # The guard fires before every tool call; a naive append would write
        # hundreds of identical lines for one cap being reached.
        for _ in range(5):
            gov.log_stop_once(self.verdict(2000.0), now=1000.0)
        self.assertEqual(len(gov.read_log()), 1)

    def test_a_new_window_is_logged_again(self):
        gov.log_stop_once(self.verdict(2000.0), now=1000.0)
        gov.log_stop_once(self.verdict(9999.0), now=3000.0)
        self.assertEqual(len(gov.read_log()), 2)

    def test_the_marker_survives_a_warning(self):
        # Both live in warn.json; the throttle used to replace the whole file.
        gov.log_stop_once(self.verdict(2000.0), now=1000.0)
        gov.throttled_warn("final", now=1001.0)
        gov.log_stop_once(self.verdict(2000.0), now=1002.0)
        self.assertEqual(len(gov.read_log()), 1)


# ---------------------------------------------------------------- token index

class TestTokenAccounting(unittest.TestCase):
    def test_bucket_tokens_sums_every_field(self):
        usage = {"input_tokens": 1, "output_tokens": 2,
                 "cache_creation_input_tokens": 4, "cache_read_input_tokens": 8}
        self.assertEqual(gov._bucket_tokens(usage), 15)

    def test_bucket_tokens_survives_junk(self):
        self.assertEqual(gov._bucket_tokens({"input_tokens": None, "output_tokens": "x",
                                             "cache_read_input_tokens": 5}), 5)

    def test_tokens_since_respects_the_start(self):
        idx = {"buckets": {"600": 10, "660": 20, "720": 30}}
        self.assertEqual(gov.tokens_since(660, idx), 50)
        self.assertEqual(gov.tokens_since(0, idx), 60)
        self.assertEqual(gov.tokens_since(1000, idx), 0)

    def test_parse_ts(self):
        self.assertEqual(gov._parse_ts("2026-08-19T07:00:00.123Z"),
                         datetime(2026, 8, 19, 7, 0, 0, tzinfo=timezone.utc).timestamp())
        for bad in (None, 12345, "2026-08-19", "not a timestamp at all"):
            self.assertIsNone(gov._parse_ts(bad), bad)


class TestIndexUpdate(Sandboxed):
    def write(self, name, records, mode="w"):
        path = self.projects / "proj-a"
        path.mkdir(exist_ok=True)
        target = path / name
        with open(target, mode, encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return target

    def rec(self, uid, ts, tokens):
        return {"type": "assistant", "timestamp": iso(ts),
                "message": {"id": uid, "usage": {"input_tokens": tokens}}}

    def test_counts_assistant_usage(self):
        now = time.time()
        self.write("a.jsonl", [self.rec("msg_aaaaaaaaaaaaaaaa", now - 60, 100),
                               self.rec("msg_bbbbbbbbbbbbbbbb", now - 30, 250)])
        idx = gov.index_update(budget_s=5, now=now)
        self.assertEqual(sum(idx["buckets"].values()), 350)

    def test_duplicate_message_ids_are_counted_once(self):
        # Forked and resumed sessions copy earlier turns into a new transcript.
        now = time.time()
        shared = self.rec("msg_aaaaaaaaaaaaaaaa", now - 60, 100)
        self.write("a.jsonl", [shared])
        self.write("b.jsonl", [shared, self.rec("msg_bbbbbbbbbbbbbbbb", now - 30, 7)])
        idx = gov.index_update(budget_s=5, now=now)
        self.assertEqual(sum(idx["buckets"].values()), 107)

    def test_records_older_than_the_horizon_are_ignored(self):
        now = time.time()
        self.write("a.jsonl", [self.rec("msg_oooooooooooooooo", now - gov.SEVEN_DAYS - 60, 999),
                               self.rec("msg_nnnnnnnnnnnnnnnn", now - 60, 5)])
        idx = gov.index_update(budget_s=5, now=now)
        self.assertEqual(sum(idx["buckets"].values()), 5)

    def test_non_assistant_records_are_skipped(self):
        now = time.time()
        self.write("a.jsonl", [
            {"type": "user", "timestamp": iso(now - 60), "message": {"id": "u1"}},
            {"type": "assistant", "timestamp": iso(now - 60), "message": {"id": "m1"}},
            self.rec("msg_cccccccccccccccc", now - 60, 42),
        ])
        idx = gov.index_update(budget_s=5, now=now)
        self.assertEqual(sum(idx["buckets"].values()), 42)

    def test_second_pass_only_reads_appended_bytes(self):
        now = time.time()
        self.write("a.jsonl", [self.rec("msg_aaaaaaaaaaaaaaaa", now - 60, 100)])
        first = gov.index_update(budget_s=5, now=now)
        offset = first["files"][str(self.projects / "proj-a" / "a.jsonl")]["offset"]
        self.write("a.jsonl", [self.rec("msg_bbbbbbbbbbbbbbbb", now - 30, 25)], mode="a")
        second = gov.index_update(budget_s=5, now=now)
        self.assertEqual(sum(second["buckets"].values()), 125)
        self.assertGreater(second["files"][str(self.projects / "proj-a" / "a.jsonl")]["offset"],
                           offset)

    def test_a_half_written_line_is_not_consumed(self):
        now = time.time()
        target = self.write("a.jsonl", [self.rec("msg_aaaaaaaaaaaaaaaa", now - 60, 100)])
        complete = json.dumps(self.rec("msg_bbbbbbbbbbbbbbbb", now - 30, 25))
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(complete[:20])                     # transcript mid-write
        idx = gov.index_update(budget_s=5, now=now)
        self.assertEqual(sum(idx["buckets"].values()), 100)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(complete[20:] + "\n")
        idx = gov.index_update(budget_s=5, now=now)
        self.assertEqual(sum(idx["buckets"].values()), 125)

    def test_truncated_file_is_reread_from_zero(self):
        now = time.time()
        target = self.write("a.jsonl", [self.rec("msg_aaaaaaaaaaaaaaaa", now - 60, 100)])
        gov.index_update(budget_s=5, now=now)
        self.write("a.jsonl", [self.rec("msg_cccccccccccccccc", now - 30, 7)])  # rewrite, shorter
        idx = gov.index_update(budget_s=5, now=now)
        self.assertEqual(idx["files"][str(target)]["offset"], os.path.getsize(target))


# ------------------------------------------------------------------- rendering

class TestRender(Sandboxed):
    def cfg(self, **kw):
        return dict(gov.DEFAULT_CONFIG, show_tokens=False, **kw)

    def test_no_sample_shows_a_placeholder(self):
        self.assertEqual(gov.render(self.cfg(), {}, 1000.0), "⛽ 5h —")

    def test_gauge_with_cap_and_countdown(self):
        now = 1000.0
        line = gov.render(self.cfg(five_hour_cap=50), state_at(41.0, now, now + 8040), now)
        self.assertEqual(line, "⛽ 5h █████░│░░░░░ 41% cap 50% ↻2h14m")

    def test_the_gauge_shows_how_long_the_cap_has_left(self):
        now = 1000.0
        line = gov.render(self.cfg(five_hour_cap=50, five_hour_expires_at=now + 5400),
                          state_at(41.0, now, now + 8040), now)
        self.assertIn("cap 50% for 1h30m", line)

    def test_an_expired_countdown_is_not_shown(self):
        now = 1000.0
        line = gov.render(self.cfg(five_hour_cap=50, five_hour_expires_at=now - 1),
                          state_at(41.0, now, now + 8040), now)
        self.assertIn("cap 50%", line)
        self.assertNotIn("for ", line)

    def test_stopped_swaps_the_icon(self):
        now = 1000.0
        line = gov.render(self.cfg(five_hour_cap=50), state_at(55.0, now, now + 60), now)
        self.assertTrue(line.startswith("⛔"), line)

    def test_warn_mode_never_shows_the_stop_icon(self):
        now = 1000.0
        line = gov.render(self.cfg(five_hour_cap=50, mode="warn"),
                          state_at(55.0, now, now + 60), now)
        self.assertTrue(line.startswith("⛽"), line)

    def test_weekly_appears_once_it_matters(self):
        now = 1000.0
        state = state_at(10.0, now, now + 60,
                         weekly={"used_percentage": 63.0, "resets_at": now + 86400})
        self.assertIn("7d 63%", gov.render(self.cfg(), state, now))

    def test_quiet_weekly_stays_hidden_on_auto(self):
        now = 1000.0
        state = state_at(10.0, now, now + 60,
                         weekly={"used_percentage": 12.0, "resets_at": now + 86400})
        self.assertNotIn("7d", gov.render(self.cfg(), state, now))

    def test_always_shows_a_quiet_weekly(self):
        now = 1000.0
        state = state_at(10.0, now, now + 60,
                         weekly={"used_percentage": 12.0, "resets_at": now + 86400})
        self.assertIn("7d 12%", gov.render(self.cfg(show_weekly="always"), state, now))

    def test_the_gauge_shows_the_budget(self):
        line = gov.render(dict(gov.DEFAULT_CONFIG, token_budget=1_000_000), {}, 1000.0,
                          tokens={"five_hour": 900_000, "seven_day": 900_000})
        self.assertIn("900k/1.0M tok", line)

    def test_the_gauge_stops_without_any_rate_limit_headers(self):
        # A token budget exists precisely for transports that send no headers,
        # so the placeholder gauge still has to show that work has halted.
        line = gov.render(dict(gov.DEFAULT_CONFIG, token_budget=800_000), {}, 1000.0,
                          tokens={"five_hour": 900_000, "seven_day": 900_000})
        self.assertTrue(line.startswith("⛔"), line)

    def test_paused_cap_is_labelled(self):
        now = 1000.0
        line = gov.render(self.cfg(five_hour_cap=50, enabled=False),
                          state_at(41.0, now, now + 60), now)
        self.assertIn("cap paused", line)


class TestUpdateState(Sandboxed):
    def test_status_line_payload_is_persisted_for_the_hooks(self):
        payload = {"session_id": "s1", "model": {"id": "claude-opus-5"},
                   "rate_limits": {"five_hour": {"used_percentage": 31.2,
                                                 "resets_at": 1_700_000_000_000},
                                   "seven_day": {"used_percentage": 63.0,
                                                 "resets_at": 1_700_500_000}}}
        state = gov.update_state(payload)
        self.assertEqual(state["limits"]["five_hour"]["used_percentage"], 31.2)
        self.assertEqual(state["limits"]["five_hour"]["resets_at"], 1_700_000_000.0)
        self.assertEqual(state["session_id"], "s1")
        self.assertEqual(json.loads(gov.STATE_PATH.read_text())["model"], "claude-opus-5")

    def test_a_payload_without_limits_keeps_the_last_good_sample(self):
        gov.update_state({"rate_limits": {"five_hour": {"used_percentage": 31.2,
                                                        "resets_at": 1_700_000_000}}})
        state = gov.update_state({"session_id": "s2"})
        self.assertEqual(state["limits"]["five_hour"]["used_percentage"], 31.2)
        self.assertEqual(state["session_id"], "s2")


# ----------------------------------------------------------------- the halt

class TestGuardProcess(Sandboxed):
    """End-to-end: what the hook actually writes on stdout."""

    def run_guard(self, payload, event="PreToolUse", env=None, argv=("guard",)):
        environ = dict(os.environ, GOVERNOR_HOME=str(self.tmp),
                       GOVERNOR_TRANSCRIPTS=gov.TRANSCRIPT_GLOB)
        environ.pop("GOVERNOR_DISABLE", None)
        environ.update(env or {})
        proc = subprocess.run([sys.executable, str(BIN), *argv, "--event", event],
                              input=json.dumps(payload), capture_output=True,
                              text=True, env=environ, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.strip()
        return json.loads(out) if out else None

    def write_transcript(self, tokens, count=9):
        project = self.projects / "proj"
        project.mkdir(exist_ok=True)
        now = time.time()
        with open(project / "a.jsonl", "w", encoding="utf-8") as fh:
            for i in range(count):
                fh.write(json.dumps({
                    "type": "assistant", "timestamp": iso(now - 600),
                    "message": {"id": f"msg_{i:016d}",
                                "usage": {"input_tokens": tokens // count}}}) + "\n")

    def arm(self, pct=60.0, **cfg):
        now = time.time()
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, five_hour_cap=50, **cfg)))
        (self.tmp / "state.json").write_text(json.dumps(
            state_at(pct, now, now + 3600)))

    def test_over_the_cap_halts_the_agent(self):
        self.arm()
        out = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(out["continue"], False)
        self.assertIn("/governor off", out["stopReason"])

    def test_the_command_that_lifts_the_cap_is_never_halted(self):
        self.arm()
        payload = {"tool_name": "Bash",
                   "tool_input": {"command": f"python3 {self.tmp}/bin/governor off"}}
        self.assertIsNone(self.run_guard(payload))

    def test_governor_prompts_are_never_halted(self):
        self.arm()
        self.assertIsNone(self.run_guard({"prompt": "/governor off"},
                                         event="UserPromptSubmit"))

    def test_warn_mode_reports_without_halting(self):
        self.arm(mode="warn")
        out = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertNotIn("continue", out)
        self.assertIn("governor:", out["systemMessage"])

    def test_warnings_are_throttled(self):
        self.arm(pct=45.0)  # warn band
        first = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIn("systemMessage", first)
        self.assertIsNone(self.run_guard({"tool_name": "Bash",
                                          "tool_input": {"command": "ls"}}))

    def test_escalation_reaches_the_session_immediately(self):
        # First warning at 41%, then 48.5% crosses escalate_ratio well inside the
        # 300s throttle window. The second message must still get through.
        self.arm(pct=41.0)
        first = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIn("Work will stop at the cap", first["systemMessage"])
        now = time.time()
        (self.tmp / "state.json").write_text(json.dumps(state_at(48.5, now, now + 3600)))
        second = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIn("points under your 50% cap", second["systemMessage"])

    def test_a_halt_is_logged_once_however_many_calls_it_blocks(self):
        self.arm()
        for _ in range(4):
            self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        records = [r for r in gov.read_log() if r["event"] == "stop"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["cap"], 50)

    def test_a_halt_in_warn_mode_is_still_logged(self):
        self.arm(mode="warn")
        self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual([r["event"] for r in gov.read_log()], ["stop"])

    def test_a_token_budget_halts_without_any_rate_limit_sample(self):
        self.write_transcript(900_000)
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, token_budget=800_000)))
        out = self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(out["continue"], False)
        self.assertIn("token budget", out["stopReason"])

    def test_a_token_stop_is_logged_in_tokens(self):
        self.write_transcript(900_000)
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, token_budget=800_000)))
        self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        record = [r for r in gov.read_log() if r["event"] == "stop"][0]
        self.assertEqual((record["used"], record["budget"], record["source"]),
                         (900_000, 800_000, "tokens"))

    def test_the_index_is_only_built_when_a_budget_needs_it(self):
        # The guard runs before every tool call. Counting tokens unconditionally
        # would put the index on the hot path for everyone.
        self.write_transcript(900_000)
        self.arm()                                     # percentage cap, no budget
        self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertFalse((self.tmp / "index.json").exists())
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, token_budget=800_000)))
        self.run_guard({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertTrue((self.tmp / "index.json").exists())

    def test_a_budget_under_the_count_still_allows(self):
        self.write_transcript(100_000)
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, token_budget=800_000)))
        self.assertIsNone(self.run_guard({"tool_name": "Bash",
                                          "tool_input": {"command": "ls"}}))

    def test_no_cap_is_a_no_op(self):
        (self.tmp / "config.json").write_text(json.dumps(dict(gov.DEFAULT_CONFIG)))
        self.assertIsNone(self.run_guard({"tool_name": "Bash",
                                          "tool_input": {"command": "ls"}}))

    def test_paused_cap_is_a_no_op(self):
        self.arm(enabled=False)
        self.assertIsNone(self.run_guard({"tool_name": "Bash",
                                          "tool_input": {"command": "ls"}}))

    def test_escape_hatch_env_var(self):
        self.arm()
        self.assertIsNone(self.run_guard({"tool_name": "Bash",
                                          "tool_input": {"command": "ls"}},
                                         env={"GOVERNOR_DISABLE": "1"}))

    def test_a_stale_sample_fails_open(self):
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, five_hour_cap=50)))
        (self.tmp / "state.json").write_text(json.dumps(
            state_at(99.0, time.time() - gov.FIVE_HOURS - 60)))
        self.assertIsNone(self.run_guard({"tool_name": "Bash",
                                          "tool_input": {"command": "ls"}}))

    def test_an_expired_window_fails_open(self):
        now = time.time()
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, five_hour_cap=50)))
        (self.tmp / "state.json").write_text(json.dumps(
            state_at(99.0, now, resets_at=now - 1)))
        self.assertIsNone(self.run_guard({"tool_name": "Bash",
                                          "tool_input": {"command": "ls"}}))

    def test_garbage_on_stdin_does_not_crash_the_hook(self):
        self.arm()
        environ = dict(os.environ, GOVERNOR_HOME=str(self.tmp),
                       GOVERNOR_TRANSCRIPTS=gov.TRANSCRIPT_GLOB)
        proc = subprocess.run([sys.executable, str(BIN), "guard", "--event", "PreToolUse"],
                              input="not json", capture_output=True, text=True,
                              env=environ, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestCliProcess(Sandboxed):
    def run_cli(self, *argv, stdin=""):
        environ = dict(os.environ, GOVERNOR_HOME=str(self.tmp),
                       GOVERNOR_TRANSCRIPTS=gov.TRANSCRIPT_GLOB)
        return subprocess.run([sys.executable, str(BIN), *argv], input=stdin,
                              capture_output=True, text=True, env=environ, timeout=30)

    def config(self):
        return json.loads((self.tmp / "config.json").read_text())

    def test_set_off_on_round_trip(self):
        self.assertEqual(self.run_cli("set", "40").returncode, 0)
        self.assertEqual(self.config()["five_hour_cap"], 40.0)
        self.run_cli("pause")
        self.assertIs(self.config()["enabled"], False)
        self.run_cli("on")
        self.assertIs(self.config()["enabled"], True)
        self.run_cli("off")
        self.assertIsNone(self.config()["five_hour_cap"])

    def test_percent_suffix_is_accepted(self):
        self.run_cli("set", "55%")
        self.assertEqual(self.config()["five_hour_cap"], 55.0)

    def test_weekly_cap_is_separate(self):
        self.run_cli("set", "80", "--weekly")
        self.assertEqual(self.config()["seven_day_cap"], 80.0)
        self.assertIsNone(self.config()["five_hour_cap"])

    def test_out_of_range_percentages_are_rejected(self):
        for value in ("0", "101", "-5", "abc"):
            proc = self.run_cli("set", value)
            self.assertNotEqual(proc.returncode, 0, value)
        self.assertFalse((self.tmp / "config.json").exists())

    def test_set_for_records_an_expiry(self):
        self.run_cli("set", "50", "--for", "3h")
        expires = self.config()["five_hour_expires_at"]
        self.assertAlmostEqual(expires - time.time(), 3 * 3600, delta=30)

    def test_setting_a_cap_again_without_for_clears_the_expiry(self):
        self.run_cli("set", "50", "--for", "3h")
        self.run_cli("set", "60")
        self.assertIsNone(self.config()["five_hour_expires_at"])

    def test_an_unreadable_duration_writes_nothing(self):
        proc = self.run_cli("set", "50", "--for", "3")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot read duration", proc.stderr)
        self.assertFalse((self.tmp / "config.json").exists())

    def test_off_clears_the_expiry_with_the_cap(self):
        self.run_cli("set", "50", "--for", "3h")
        self.run_cli("off")
        self.assertIsNone(self.config()["five_hour_cap"])
        self.assertIsNone(self.config()["five_hour_expires_at"])

    def test_config_note_leaves_an_expiry_alone(self):
        # The slash command routes `note` through `config`, not `set`, precisely
        # so that leaving a note cannot silently drop a running --for.
        self.run_cli("set", "50", "--for", "3h")
        before = self.config()["five_hour_expires_at"]
        self.run_cli("config", "note", "sharing with Ana")
        self.assertEqual(self.config()["five_hour_expires_at"], before)
        self.assertEqual(self.config()["note"], "sharing with Ana")

    def test_the_statusline_clears_a_cap_that_has_run_out(self):
        (self.tmp / "config.json").write_text(json.dumps(dict(
            gov.DEFAULT_CONFIG, show_tokens=False, five_hour_cap=50,
            five_hour_expires_at=time.time() - 1)))
        self.run_cli("statusline", stdin="{}")
        self.assertIsNone(self.config()["five_hour_cap"])
        self.assertIsNone(self.config()["five_hour_expires_at"])

    def test_session_start_is_silent_once_the_cap_has_run_out(self):
        (self.tmp / "config.json").write_text(json.dumps(dict(
            gov.DEFAULT_CONFIG, five_hour_cap=50,
            five_hour_expires_at=time.time() - 1)))
        proc = self.run_cli("session-start", stdin="{}")
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIsNone(self.config()["five_hour_cap"])

    def test_an_expired_cap_does_not_halt_the_guard(self):
        (self.tmp / "config.json").write_text(json.dumps(dict(
            gov.DEFAULT_CONFIG, five_hour_cap=50,
            five_hour_expires_at=time.time() - 1)))
        now = time.time()
        (self.tmp / "state.json").write_text(json.dumps(state_at(99.0, now, now + 3600)))
        environ = dict(os.environ, GOVERNOR_HOME=str(self.tmp),
                       GOVERNOR_TRANSCRIPTS=gov.TRANSCRIPT_GLOB)
        proc = subprocess.run([sys.executable, str(BIN), "guard", "--event", "PreToolUse"],
                              input=json.dumps({"tool_name": "Bash",
                                                "tool_input": {"command": "ls"}}),
                              capture_output=True, text=True, env=environ, timeout=30)
        self.assertEqual(proc.stdout.strip(), "")

    def test_budget_round_trip(self):
        self.run_cli("budget", "4M")
        self.assertEqual(self.config()["token_budget"], 4_000_000)
        self.run_cli("budget", "20M", "--weekly")
        self.assertEqual(self.config()["token_budget_weekly"], 20_000_000)
        self.run_cli("budget", "off")
        self.assertIsNone(self.config()["token_budget"])
        self.assertEqual(self.config()["token_budget_weekly"], 20_000_000)

    def test_an_unreadable_budget_writes_nothing(self):
        proc = self.run_cli("budget", "4GB")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot read token count", proc.stderr)
        self.assertFalse((self.tmp / "config.json").exists())

    def test_budget_changes_are_recorded(self):
        self.run_cli("budget", "4M")
        self.run_cli("budget", "off")
        events = [r["event"] for r in json.loads(self.run_cli("log", "--json").stdout)]
        self.assertEqual(events, ["budget", "budget_off"])

    def test_cap_changes_are_recorded(self):
        self.run_cli("set", "50")
        self.run_cli("pause")
        self.run_cli("on")
        self.run_cli("off")
        events = [r["event"] for r in json.loads(
            self.run_cli("log", "--json").stdout)]
        self.assertEqual(events, ["set", "pause", "resume", "off"])

    def test_off_without_a_cap_records_nothing(self):
        self.run_cli("off")
        self.assertEqual(json.loads(self.run_cli("log", "--json").stdout), [])

    def test_an_expired_cap_is_recorded_when_it_is_cleared(self):
        (self.tmp / "config.json").write_text(json.dumps(dict(
            gov.DEFAULT_CONFIG, show_tokens=False, five_hour_cap=50,
            five_hour_expires_at=time.time() - 1)))
        self.run_cli("statusline", stdin="{}")
        records = json.loads(self.run_cli("log", "--json").stdout)
        self.assertEqual([r["event"] for r in records], ["expired"])
        self.assertEqual(records[0]["cap"], 50)

    def test_log_days_filters(self):
        self.run_cli("set", "50")
        recent = json.loads(self.run_cli("log", "--days", "7", "--json").stdout)
        self.assertEqual(len(recent), 1)
        # A window that ended before the entry was written excludes it.
        with open(self.tmp / "log.jsonl", "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time() - 40 * 86400, "event": "set"}) + "\n")
        self.assertEqual(json.loads(self.run_cli("log", "--days", "7", "--json").stdout), [])
        self.assertEqual(len(json.loads(self.run_cli("log", "--days", "0", "--json").stdout)), 1)

    def test_log_says_so_when_there_is_nothing(self):
        self.assertIn("no history", self.run_cli("log").stdout)

    def test_statusline_persists_state_and_prints_a_gauge(self):
        now = time.time()
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, show_tokens=False, five_hour_cap=50)))
        payload = {"rate_limits": {"five_hour": {"used_percentage": 41.0,
                                                 "resets_at": now + 8040}}}
        proc = self.run_cli("statusline", stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("41%", proc.stdout)
        self.assertIn("cap 50%", proc.stdout)
        stored = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(stored["limits"]["five_hour"]["used_percentage"], 41.0)

    def test_statusline_survives_an_empty_payload(self):
        proc = self.run_cli("statusline", stdin="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("5h", proc.stdout)

    def test_session_start_announces_an_armed_cap(self):
        (self.tmp / "config.json").write_text(json.dumps(
            dict(gov.DEFAULT_CONFIG, five_hour_cap=55, note="sharing with Ana")))
        proc = self.run_cli("session-start", stdin="{}")
        out = json.loads(proc.stdout)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("55%", context)
        self.assertIn("sharing with Ana", context)

    def test_session_start_is_silent_without_a_cap(self):
        proc = self.run_cli("session-start", stdin="{}")
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
