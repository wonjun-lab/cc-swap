"""``strategy == "maximize"`` through the real engine (EngineHarness)."""

from __future__ import annotations

import json
import os
import sys
import types
from argparse import Namespace
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    AllExhaustedEvent,
    ConfigWarningEvent,
    ErrorEvent,
    MaximizeDecisionEvent,
    NoSwitchEvent,
    QuarantineEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.maximize import pause
from claude_swap.maximize.engine_hook import (
    DECISION_KEY,
    SAMPLES_KEY,
    _primer_class,
    apply_maximize_settings,
    runtime_for,
)
from claude_swap.settings import MaximizeSettings, PrimeSettings
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _iso_at

EMAILS = {1: "a@example.com", 2: "b@example.com", 3: "c@example.com", 4: "d@example.com"}


def win(p5: float, p7: float, *, r5: float | None = None, r7: float | None = None) -> dict:
    five: dict = {"pct": p5}
    seven: dict = {"pct": p7}
    if r5 is not None:
        five["resets_at"] = _iso_at(r5)
    if r7 is not None:
        seven["resets_at"] = _iso_at(r7)
    return {"five_hour": five, "seven_day": seven}


def make(temp_home, n: int = 3, *, maximize: dict | None = None, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, strategy="maximize", **settings)
    for i in range(1, n + 1):
        h.seed(i, EMAILS[i])
    h.make_live(EMAILS[1], 1)
    if maximize is not None:
        write_settings(h, {"maximize": maximize})
    return h


def write_settings(h: EngineHarness, data: dict) -> None:
    path = h.switcher.backup_dir / "settings.json"
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps(data))
    # Guarantee a visible mtime change even on coarse-timestamp filesystems.
    bumped = max(path.stat().st_mtime_ns, before + 1_000_000_000)
    os.utime(path, ns=(bumped, bumped))


def disable(h: EngineHarness, num: int) -> None:
    data = h.switcher._get_sequence_data()
    data["accounts"][str(num)]["disabled"] = True
    h.switcher._write_json(h.switcher.sequence_file, data)


def of(h: EngineHarness, cls) -> list:
    return [e for e in h.events if isinstance(e, cls)]


def no_switch_reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in of(h, NoSwitchEvent)]


class FakePrimer:
    def __init__(self, fail: bool = False):
        self.calls: list[str | None] = []
        self.fail = fail

    def run_due(self, snap):
        self.calls.append(snap.active)
        if self.fail:
            raise RuntimeError("boom sk-secret")
        return []


class TestHook:
    def test_best_strategy_is_untouched(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, EMAILS[1])
        h.seed(2, EMAILS[2])
        h.make_live(EMAILS[1], 1)
        assert h.tick_with_usage({"1": win(62, 40), "2": win(0, 0)}) is TickOutcome.NO_ACTION
        assert not of(h, MaximizeDecisionEvent)
        assert no_switch_reasons(h) == ["below-threshold"]

    def test_poll_thresholds_follow_the_hard_caps(self, temp_home):
        # Spec §4.1: escalation AND urgent mode key on min(hard), never soft.
        h = make(temp_home, maximize={"soft5h": 40, "soft7d": 85, "hard5h": 90})
        h.tick_with_usage({"1": win(10, 10), "2": win(0, 0), "3": win(0, 0)})
        assert h.engine.settings.threshold == 90.0          # escalation: min(hard)
        assert h.switcher._poll_policy_inputs()[0] == 90.0  # urgent mode: min(hard)

    def test_decision_event_carries_scores_and_rows_without_email(self, temp_home):
        h = make(temp_home)
        h.tick_with_usage({"1": win(10, 30), "2": win(0, 10), "3": win(0, 50)})
        [event] = of(h, MaximizeDecisionEvent)
        assert event.decision == "hold" and event.active == "1"
        assert set(event.scores) == {"1", "2", "3"}
        assert [r["number"] for r in event.rows] == ["1", "2", "3"]
        payload = json.dumps(event.to_json())
        assert "@" not in payload and "sk-" not in payload
        assert event.human().startswith("maximize: hold on Account-1: ")


class TestSoftAndHard:
    def test_soft_waits_for_idle_then_switches(self, temp_home):
        h = make(temp_home)
        usage = {"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        h.clock.advance(300)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        h.clock.advance(300)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert no_switch_reasons(h) == ["maximize-pending", "maximize-pending"]
        [switch] = of(h, SwitchEvent)
        assert switch.trigger == "soft"
        assert h.state()[SAMPLES_KEY] == {
            "account": "2", "samples": [], "activeChangedAt": h.clock.now,
        }

    def test_soft_keeps_waiting_while_busy(self, temp_home):
        h = make(temp_home)
        for pct in (62, 64, 66):
            usage = {"1": win(pct, 40), "2": win(0, 10), "3": win(0, 50)}
            assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
            h.clock.advance(300)
        assert h.active_number() == 1
        assert no_switch_reasons(h) == ["maximize-pending"] * 3

    def test_hard_switches_on_first_tick(self, temp_home):
        h = make(temp_home)
        outcome = h.tick_with_usage({"1": win(96, 40), "2": win(0, 10), "3": win(0, 50)})
        assert outcome is TickOutcome.SWITCHED
        assert [e.trigger for e in of(h, SwitchEvent)] == ["hard"]
        assert h.active_number() == 2

    def test_hard_with_nothing_roomier_holds_on_the_active(self, temp_home):
        # Over the hard cap but under 100%, and no peer under both hard caps:
        # keep using the active; at-limit takes over once it is spent.
        h = make(temp_home)
        outcome = h.tick_with_usage({"1": win(96, 40), "2": win(97, 10), "3": win(10, 99)})
        assert outcome is TickOutcome.NO_ACTION
        assert no_switch_reasons(h) == ["hard-stay"]
        assert h.state()[DECISION_KEY]["code"] == "hard-stay"
        assert not of(h, AllExhaustedEvent)
        assert h.active_number() == 1

    def test_pending_hold_pulls_the_active_poll(self, temp_home):
        h = make(temp_home)
        h.tick_with_usage({"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)})
        entry = h.switcher._usage_store.entries({"1": (EMAILS[1], "")})["1"]
        assert entry.next_poll_at == pytest.approx(h.clock.now + 180)

    def test_pending_pull_never_beats_the_poll_floor(self, temp_home):
        # A session override (no clamp) below upstream's MIN_INTERVAL_S must
        # not schedule the active poll sooner than fetchedAt + 180 s.
        h = make(temp_home)
        apply_maximize_settings(h.engine, MaximizeSettings(pending_poll_s=60))
        now = h.clock.now
        entries = {
            "1": UsageEntry(last_good=win(62, 40), fetched_at=now - 100, age_s=100.0),
            "2": UsageEntry(last_good=win(0, 10), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 50), fetched_at=now, age_s=0.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        assert no_switch_reasons(h) == ["maximize-pending"]
        entry = h.switcher._usage_store.entries({"1": (EMAILS[1], "")})["1"]
        assert entry.next_poll_at == pytest.approx(now - 100 + 180)

    def test_pending_hold_respects_a_recent_429(self, temp_home):
        h = make(temp_home)
        now = h.clock.now
        entries = {
            "1": UsageEntry(last_good=win(62, 40), fetched_at=now, age_s=0.0,
                            last_429_at=now - 60),
            "2": UsageEntry(last_good=win(0, 10), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 50), fetched_at=now, age_s=0.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        entry = h.switcher._usage_store.entries({"1": (EMAILS[1], "")})["1"]
        assert entry.next_poll_at is None


class TestResetWait:
    """``maximize.resetWaitMin``: a window about to reset is waited out."""

    @staticmethod
    def usage(h: EngineHarness, p5: float, reset_at: float) -> dict:
        # #1 has the best score, so only its own 5h can make it move; #2 is
        # a ready landing target the whole time.
        return {"1": win(p5, 10, r5=reset_at), "2": win(0, 40), "3": win(0, 50)}

    @staticmethod
    def next_poll(h: EngineHarness) -> float | None:
        return h.switcher._usage_store.entries({"1": (EMAILS[1], "")})["1"].next_poll_at

    def test_climbing_to_the_reset_never_switches(self, temp_home):
        h = make(temp_home)
        reset_at = h.clock.now + 13 * 60
        for p5 in (93, 94, 95, 96):     # 1 pt / 3 min: 100% always well after the reset
            assert h.tick_with_usage(self.usage(h, p5, reset_at)) is TickOutcome.NO_ACTION
            assert self.next_poll(h) == pytest.approx(h.clock.now + 60)   # urgent poll
            h.clock.advance(180)
        h.clock.advance(300)            # past the reset: 5h restarts near zero
        assert h.tick_with_usage(
            self.usage(h, 1, h.clock.now + 5 * 3600)
        ) is TickOutcome.NO_ACTION
        assert no_switch_reasons(h) == ["reset-wait"] * 4 + ["maximize-hold"]
        assert not of(h, SwitchEvent) and h.active_number() == 1
        decisions = of(h, MaximizeDecisionEvent)
        assert decisions[0].reason == (
            "#1 5h 93% — resets in 13m, waiting it out (switches at once if it hits 100%)"
        )
        assert decisions[3].reason.startswith("#1 5h 96% — resets in 4m")

    def test_the_wait_is_published_for_why(self, temp_home):
        h = make(temp_home)
        h.tick_with_usage(self.usage(h, 93, h.clock.now + 600))
        record = h.state()[DECISION_KEY]
        assert (record["decision"], record["pending"], record["code"]) == ("hold", False, "reset-wait")
        assert record["target"] is None
        h.clock.advance(900)            # reset over: an ordinary hold carries no code
        h.tick_with_usage(self.usage(h, 1, h.clock.now + 5 * 3600))
        assert "code" not in h.state()[DECISION_KEY]

    def test_hitting_100_during_the_wait_switches_at_once(self, temp_home):
        h = make(temp_home)
        reset_at = h.clock.now + 10 * 60
        assert h.tick_with_usage(self.usage(h, 93, reset_at)) is TickOutcome.NO_ACTION
        h.clock.advance(60)
        assert h.tick_with_usage(self.usage(h, 100, reset_at)) is TickOutcome.SWITCHED
        assert [e.trigger for e in of(h, SwitchEvent)] == ["at-limit"]
        assert h.active_number() == 2

    def test_a_faster_pace_ends_the_wait(self, temp_home):
        h = make(temp_home)
        reset_at = h.clock.now + 10 * 60
        assert h.tick_with_usage(self.usage(h, 93, reset_at)) is TickOutcome.NO_ACTION
        h.clock.advance(180)            # +6 pts in 3 min: 100% in 30 s, before the reset
        assert h.tick_with_usage(self.usage(h, 99, reset_at)) is TickOutcome.SWITCHED
        assert [e.trigger for e in of(h, SwitchEvent)] == ["hard"]
        assert no_switch_reasons(h) == ["reset-wait"]

    def test_over_hard_with_no_pace_yet_switches(self, temp_home):
        h = make(temp_home)
        outcome = h.tick_with_usage(self.usage(h, 96, h.clock.now + 8 * 60))
        assert outcome is TickOutcome.SWITCHED
        assert [e.trigger for e in of(h, SwitchEvent)] == ["hard"]

    def test_zero_turns_it_off(self, temp_home):
        h = make(temp_home, maximize={"resetWaitMin": 0})
        h.tick_with_usage(self.usage(h, 93, h.clock.now + 8 * 60))
        assert no_switch_reasons(h) == ["maximize-pending"]
        assert self.next_poll(h) == pytest.approx(h.clock.now + 180)

    def test_urgent_polls_only_in_the_last_15_minutes(self, temp_home):
        h = make(temp_home, maximize={"resetWaitMin": 60})
        reset_at = h.clock.now + 40 * 60
        h.tick_with_usage(self.usage(h, 93, reset_at))
        assert no_switch_reasons(h) == ["reset-wait"]
        assert self.next_poll(h) is None            # the planner's own cadence
        h.clock.advance(26 * 60)
        h.tick_with_usage(self.usage(h, 93, reset_at))
        assert no_switch_reasons(h) == ["reset-wait"] * 2
        assert self.next_poll(h) == pytest.approx(h.clock.now + 60)

    def test_urgent_poll_respects_a_recent_429(self, temp_home):
        h = make(temp_home)
        now = h.clock.now
        entries = {
            "1": UsageEntry(last_good=win(93, 10, r5=now + 600), fetched_at=now, age_s=0.0,
                            last_429_at=now - 60),
            "2": UsageEntry(last_good=win(0, 40), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 50), fetched_at=now, age_s=0.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        assert no_switch_reasons(h) == ["reset-wait"]
        assert self.next_poll(h) is None

    def test_over_the_hard_cap_a_recent_429_switches_instead(self, temp_home):
        # 1 pt / 3 min: without the 429 every tick waits (as above). At 95%
        # (hard) a token that just 429'd cannot be polled every 60 s to
        # catch a climb to 100%: the hard switch happens.
        h = make(temp_home)
        reset_at = h.clock.now + 13 * 60
        for p5 in (93, 94):
            assert h.tick_with_usage(self.usage(h, p5, reset_at)) is TickOutcome.NO_ACTION
            h.clock.advance(180)
        now = h.clock.now
        usage = self.usage(h, 95, reset_at)
        entries = {
            "1": UsageEntry(last_good=usage["1"], fetched_at=now, age_s=0.0,
                            last_429_at=now - 60),
            "2": UsageEntry(last_good=usage["2"], fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=usage["3"], fetched_at=now, age_s=0.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
        assert no_switch_reasons(h) == ["reset-wait"] * 2
        assert [e.trigger for e in of(h, SwitchEvent)] == ["hard"]

    def test_hot_reload_rejects_an_out_of_range_value(self, temp_home):
        h = make(temp_home, maximize={"resetWaitMin": 20})
        h.tick_with_usage({"1": win(10, 10), "2": win(0, 40), "3": win(0, 50)})
        write_settings(h, {"maximize": {"resetWaitMin": 90}})
        h.tick_with_usage({"1": win(10, 10), "2": win(0, 40), "3": win(0, 50)})
        [warning] = of(h, ConfigWarningEvent)
        assert "maximize.resetWaitMin" in warning.message
        assert warning.message.endswith("keeping the previous maximize settings")
        assert runtime_for(h.engine).settings.reset_wait_min == 20


class TestSamples:
    def test_samples_reset_when_active_changes_externally(self, temp_home):
        h = make(temp_home)
        usage = {"1": win(10, 10), "2": win(10, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        h.clock.advance(120)
        h.tick_with_usage(usage)
        stored = h.state()[SAMPLES_KEY]
        assert stored["account"] == "1" and len(stored["samples"]) == 2
        assert "activeChangedAt" not in stored   # first run: no change seen
        h.make_live(EMAILS[2], 2)          # manual switch outside the engine
        h.clock.advance(120)
        h.tick_with_usage(usage)
        stored = h.state()[SAMPLES_KEY]
        assert stored == {
            "account": "2",
            "samples": [[h.clock.now, 10.0, 10.0]],
            "activeChangedAt": h.clock.now,
        }
        snap = runtime_for(h.engine).last_snapshot
        assert snap.samples[0].ts == h.clock.now
        assert snap.active_changed_at == h.clock.now
        changed = h.clock.now
        h.clock.advance(120)
        h.tick_with_usage(usage)
        assert h.state()[SAMPLES_KEY]["activeChangedAt"] == changed   # kept

    def test_manual_switch_restarts_the_rebalance_cooldown(self, temp_home):
        # 2 beats 3 by far on the weekly score; no engine switch ever ran,
        # so only the manual login can hold the rebalance back.
        h = make(temp_home)
        now = h.clock.now

        def usage() -> dict:
            return {
                "1": win(10, 40),
                "2": win(0, 70, r7=now + 0.5 * 86400),
                "3": win(10, 10, r7=now + 6 * 86400),
            }

        h.tick_with_usage(usage())
        h.make_live(EMAILS[3], 3)          # manual switch outside the engine
        h.clock.advance(60)
        changed = h.clock.now
        outcomes = [h.tick_with_usage(usage())]
        for _ in range(4):                 # 11 idle minutes on 3
            h.clock.advance(165)
            outcomes.append(h.tick_with_usage(usage()))
        assert h.clock.now - changed == 660
        assert outcomes == [TickOutcome.NO_ACTION] * 5
        detail = of(h, NoSwitchEvent)[-1].detail
        assert detail.startswith("rebalance cooldown (19 min left)"), detail
        assert "lastSwitchAt" not in h.state()
        while h.clock.now - changed < 1800:
            h.clock.advance(300)
            outcome = h.tick_with_usage(usage())
        assert outcome is TickOutcome.SWITCHED
        assert of(h, SwitchEvent)[-1].trigger == "rebalance"
        assert h.active_number() == 2

    def test_cached_reading_not_resampled(self, temp_home):
        # The store serves one fetch for several ticks (serve TTL, backoff):
        # it is one sample, so an unchanged duplicate never fakes a plateau.
        h = make(temp_home)
        fetched = h.clock.now

        def entries() -> dict:
            return {
                "1": UsageEntry(last_good=win(62, 40), fetched_at=fetched,
                                age_s=h.clock.now - fetched),
                "2": UsageEntry(last_good=win(0, 10), fetched_at=h.clock.now, age_s=0.0),
                "3": UsageEntry(last_good=win(0, 50), fetched_at=h.clock.now, age_s=0.0),
            }

        for _ in range(3):
            assert h.tick_with_entries(entries()) is TickOutcome.NO_ACTION
            h.clock.advance(60)
        assert h.state()[SAMPLES_KEY]["samples"] == [[fetched, 62.0, 40.0]]
        h.clock.advance(600)                 # still the same fetch: still one sample
        h.tick_with_entries(entries())
        assert h.state()[SAMPLES_KEY]["samples"] == [[fetched, 62.0, 40.0]]
        assert h.active_number() == 1

    def test_dry_run_keeps_samples_in_memory_and_prints_the_table(self, temp_home):
        h = make(temp_home)
        h.engine = h._make_engine(dry_run=True)
        usage = {"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)}
        outcomes = []
        for _ in range(3):
            outcomes.append(h.tick_with_usage(usage))
            h.clock.advance(300)
        assert outcomes[-1] is TickOutcome.SWITCHED
        assert of(h, SwitchEvent)[0].dry_run is True
        assert h.active_number() == 1
        assert h.state() == {}
        text = of(h, MaximizeDecisionEvent)[-1].human()
        lines = text.splitlines()
        assert lines[0].startswith("maximize: switch (soft) on Account-1")
        assert lines[1].split()[:3] == ["#", "tier", "plan"]
        assert lines[2].split()[:6] == ["*", "1", "normal", "std", "62%", "40%"]
        assert lines[2].split()[-1] == "idle"
        assert "@" not in text


class TestSettings:
    def test_hot_reload_applies_new_thresholds(self, temp_home):
        h = make(temp_home)
        usage = {"1": win(45, 10), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        assert no_switch_reasons(h) == ["maximize-hold"]
        write_settings(h, {"maximize": {"soft5h": 40, "hard5h": 92}})
        h.tick_with_usage(usage)
        assert no_switch_reasons(h)[-1] == "maximize-pending"
        assert h.engine.settings.threshold == 92.0          # min(hard) re-applied
        assert not of(h, ConfigWarningEvent)

    def test_hot_reload_invalid_keeps_previous(self, temp_home):
        h = make(temp_home, maximize={"soft5h": 40})
        usage = {"1": win(10, 10), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        write_settings(h, {"maximize": {"soft5h": 99}})   # above hard5h 95
        h.tick_with_usage(usage)
        [warning] = of(h, ConfigWarningEvent)
        assert "maximize.soft5h (99) must not exceed maximize.hard5h (95)" in warning.message
        assert warning.message.endswith("keeping the previous maximize settings")
        assert runtime_for(h.engine).settings.soft_5h == 40.0
        write_settings(h, {"maximize": {"soft5h": 40, "pendingPollS": 5}})  # range 180-600
        h.tick_with_usage(usage)
        last = of(h, ConfigWarningEvent)[-1].message
        assert "maximize.pendingPollS" in last
        assert last.endswith("keeping the previous maximize settings")
        assert runtime_for(h.engine).settings.pending_poll_s == 180
        path = h.switcher.backup_dir / "settings.json"
        path.write_text("{not json")
        os.utime(path, ns=(1, 1))
        h.tick_with_usage(usage)
        assert "unreadable" in of(h, ConfigWarningEvent)[-1].message
        assert runtime_for(h.engine).settings.soft_5h == 40.0
        assert len(of(h, ConfigWarningEvent)) == 3

    def test_int_truncation_is_not_a_rejection(self, temp_home):
        h = make(temp_home)
        usage = {"1": win(10, 10), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        write_settings(h, {"maximize": {"idleWindowMin": 12.0}})  # integral float
        h.tick_with_usage(usage)
        assert runtime_for(h.engine).settings.idle_window_min == 12
        assert not of(h, ConfigWarningEvent)

    def test_session_override_applies_now(self, temp_home):
        h = make(temp_home)
        apply_maximize_settings(h.engine, MaximizeSettings(soft_5h=30.0, hard_7d=93.0))
        assert h.engine.settings.threshold == 93.0          # min(hard_5h 95, hard_7d 93)
        h.tick_with_usage({"1": win(35, 10), "2": win(0, 10), "3": win(0, 50)})
        assert no_switch_reasons(h) == ["maximize-pending"]

    def test_cli_flags_from_the_engine_survive_reload(self, temp_home):
        h = make(temp_home)
        h.engine = h._make_engine(
            maximize_cli=Namespace(soft5h=30.0, hard5h=None, soft7d=None, hard7d=None)
        )
        usage = {"1": win(10, 10), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        assert runtime_for(h.engine).settings.soft_5h == 30.0
        write_settings(h, {"maximize": {"soft5h": 45, "tieEpsilon": 0.5}})
        h.tick_with_usage(usage)
        s = runtime_for(h.engine).settings
        assert (s.soft_5h, s.tie_epsilon) == (30.0, 0.5)

    def test_flags_contradicting_a_reloaded_file_keep_previous(self, temp_home):
        h = make(temp_home)
        h.engine = h._make_engine(
            maximize_cli=Namespace(soft5h=90.0, hard5h=None, soft7d=None, hard7d=None)
        )
        usage = {"1": win(10, 10), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        write_settings(h, {"maximize": {"hard5h": 85}})   # below the flag's soft 90
        h.tick_with_usage(usage)
        [warning] = of(h, ConfigWarningEvent)
        assert "maximize.soft5h (90) must not exceed maximize.hard5h (85)" in warning.message
        s = runtime_for(h.engine).settings
        assert (s.soft_5h, s.hard_5h) == (90.0, 95.0)

    def test_rate_limit_tier_from_stored_credentials(self, temp_home):
        h = make(temp_home)
        h.switcher._write_account_credentials("3", EMAILS[3], json.dumps({
            "claudeAiOauth": {"accessToken": "sk-3", "refreshToken": "rt-3",
                              "rateLimitTier": "default_claude_max_20x"},
        }))
        # 2 and 3 tie on score; the 20x wins the soft move.
        usage = {"1": win(62, 40), "2": win(0, 10), "3": win(0, 10)}
        for _ in range(3):
            h.tick_with_usage(usage)
            h.clock.advance(300)
        snap = runtime_for(h.engine).last_snapshot
        assert [v.plan_weight for v in snap.accounts] == [1, 1, 4]
        assert h.active_number() == 3
        assert "sk-3" not in json.dumps([e.to_json() for e in h.events])


class TestUpstreamPaths:
    def test_unknown_active_falls_through_to_upstream_failover(self, temp_home):
        h = make(temp_home)
        disable(h, 3)
        usage = {"1": None, "2": win(0, 10), "3": win(0, 0)}
        outcomes = [h.tick_with_usage(usage) for _ in range(3)]
        assert outcomes == [TickOutcome.NO_ACTION, TickOutcome.NO_ACTION, TickOutcome.SWITCHED]
        assert no_switch_reasons(h) == ["active-usage-unknown"] * 2
        assert [e.decision for e in of(h, MaximizeDecisionEvent)] == ["indeterminate"] * 3
        [switch] = of(h, SwitchEvent)
        assert switch.trigger == "failover" and switch.to_ref["number"] == 2

    def test_exhausted_sleeps_until_the_first_account_under_the_limit(self, temp_home):
        h = make(temp_home)
        now = h.clock.now
        outcome = h.tick_with_usage({
            "1": win(100, 40, r5=now + 7200),
            "2": win(100, 10, r5=now + 3600),
            "3": win(10, 100, r7=now + 3 * 86400),
        })
        assert outcome is TickOutcome.BLOCKED
        [event] = of(h, AllExhaustedEvent)
        assert event.earliest_reset_at == _iso_at(now + 3600)
        assert h.engine._sleep_until_ts == pytest.approx(now + 3600 + 60)

    def test_exhausted_wakes_when_a_peer_drops_under_100_not_under_hard(self, temp_home):
        # 2's 7d (99%) is over the hard cap but under the limit: once its 5h
        # rolls over (1 h) it is a valid at-limit landing, so the sleep must
        # not wait for its 7d reset (3 days).
        h = make(temp_home, n=2)
        now = h.clock.now
        outcome = h.tick_with_usage({
            "1": win(100, 40, r5=now + 7200),
            "2": win(100, 99, r5=now + 3600, r7=now + 3 * 86400),
        })
        assert outcome is TickOutcome.BLOCKED
        [event] = of(h, AllExhaustedEvent)
        assert event.earliest_reset_at == _iso_at(now + 3600)

    def test_at_limit_lands_on_any_quota_left(self, temp_home):
        h = make(temp_home)
        outcome = h.tick_with_usage({"1": win(100, 60), "2": win(96, 40), "3": win(0, 98.5)})
        assert outcome is TickOutcome.SWITCHED
        assert [e.trigger for e in of(h, SwitchEvent)] == ["at-limit"]
        assert h.active_number() == 2
        assert not of(h, AllExhaustedEvent)

    def test_unreadable_peer_is_not_all_exhausted(self, temp_home):
        h = make(temp_home)
        outcome = h.tick_with_usage({"1": win(100, 40), "2": win(100, 10), "3": None})
        assert outcome is TickOutcome.BLOCKED
        assert not of(h, AllExhaustedEvent)
        assert no_switch_reasons(h) == ["no-qualifying-candidate"]
        assert h.engine._sleep_until_ts is None

    def test_failed_freshen_quarantines_and_redecides(self, temp_home):
        h = make(temp_home)
        statuses = {"2": "invalid_grant", "3": "ok"}
        with patch.object(h.engine, "_freshen_target", side_effect=lambda n, e: statuses[n]):
            outcome = h.tick_with_usage({"1": win(96, 40), "2": win(0, 10), "3": win(0, 50)})
        assert outcome is TickOutcome.SWITCHED
        assert [e.number for e in of(h, QuarantineEvent)] == ["2"]
        assert h.active_number() == 3

    def test_transient_freshen_everywhere_at_limit_is_an_error(self, temp_home):
        # Re-deciding without the failed targets ends in Exhausted, not a
        # Hold: upstream's transient handling stands.
        h = make(temp_home)
        with patch.object(h.engine, "_freshen_target", return_value="transient"):
            outcome = h.tick_with_usage({"1": win(100, 40), "2": win(0, 10), "3": win(0, 50)})
        assert outcome is TickOutcome.ERROR
        assert "network" in of(h, ErrorEvent)[0].message
        assert h.active_number() == 1

    @pytest.mark.parametrize("status", ["skip-live-session", "transient"])
    def test_failed_preparation_then_hold_is_a_normal_hold(self, temp_home, status):
        # Hard on 1; 2 is the only target (3 is over the hard cap). With 2
        # set aside, the policy holds on 1: that is a hold, not a failure.
        h = make(temp_home)
        with patch.object(h.engine, "_freshen_target", return_value=status):
            outcome = h.tick_with_usage({"1": win(96, 40), "2": win(0, 10), "3": win(97, 10)})
        assert outcome is TickOutcome.NO_ACTION
        assert not of(h, ErrorEvent)
        [event] = of(h, NoSwitchEvent)
        assert event.reason == "hard-stay"  # a hold of the hard trigger's own
        assert "no account under the hard caps has more 5h room than #1" in event.detail
        assert f"set aside #2 ({status})" in event.detail
        assert h.active_number() == 1

    def test_failed_preparation_then_hold_keeps_systemic_errors(self, temp_home):
        h = make(temp_home)
        with patch.object(h.engine, "_freshen_target", return_value="store-unmirrored"):
            outcome = h.tick_with_usage({"1": win(96, 40), "2": win(0, 10), "3": win(97, 10)})
        assert outcome is TickOutcome.ERROR
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" in of(h, ErrorEvent)[0].message
        assert not of(h, NoSwitchEvent)


class TestPrimerHook:
    def test_primer_failure_is_reported_not_raised(self, temp_home):
        h = make(temp_home)
        rt = runtime_for(h.engine)
        rt.primer, rt.prime_settings = FakePrimer(fail=True), PrimeSettings(enabled=True)
        assert h.tick_with_usage({"1": win(10, 10), "2": win(0, 0), "3": win(0, 0)}) is (
            TickOutcome.NO_ACTION
        )
        [error] = of(h, ErrorEvent)
        assert error.message == "prime: RuntimeError"

    def test_primer_never_runs_on_dry_run(self, temp_home):
        h = make(temp_home)
        h.engine = h._make_engine(dry_run=True)
        rt = runtime_for(h.engine)
        rt.primer, rt.prime_settings = FakePrimer(), PrimeSettings(enabled=True)
        h.tick_with_usage({"1": win(10, 10), "2": win(0, 0), "3": win(0, 0)})
        assert rt.primer.calls == []

    def test_primer_module_without_the_class_means_no_primer(self, monkeypatch):
        # Between Task 10 and Task 11, primer.py exists without Primer: that
        # ImportError means "not there yet", not a broken tick.
        name = "claude_swap.maximize.primer"
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        assert _primer_class() is None


class TestPublishDecision:
    """The engine writes its decision to the state file for TUI viewers."""

    def test_live_tick_publishes_decision_slot_numbers_only(self, temp_home):
        h = make(temp_home)
        h.switcher._write_account_credentials("3", EMAILS[3], json.dumps({
            "claudeAiOauth": {"accessToken": "sk-3", "refreshToken": "rt-3",
                              "rateLimitTier": "default_claude_max_20x"},
        }))
        h.tick_with_usage({"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)})
        record = h.state()[DECISION_KEY]
        assert record == {
            "at": h.clock.now, "pid": os.getpid(), "active": "1",
            "decision": "hold", "trigger": None, "target": "2",
            "reason": record["reason"], "pending": True,
            "plans": {"1": None, "2": None, "3": "20x"},
        }
        assert record["reason"].startswith("#1 5h 62% >= soft 50")
        text = json.dumps(h.state()[DECISION_KEY])
        assert "@" not in text and "sk-" not in text

    def test_switch_is_published_with_its_trigger_and_target(self, temp_home):
        h = make(temp_home)
        assert h.tick_with_usage(
            {"1": win(96, 40), "2": win(0, 10), "3": win(0, 50)}
        ) is TickOutcome.SWITCHED
        record = h.state()[DECISION_KEY]
        assert (record["decision"], record["trigger"], record["target"]) == ("switch", "hard", "2")

    def test_dry_run_tick_does_not_publish(self, temp_home):
        h = make(temp_home)
        h.engine = h._make_engine(dry_run=True)
        h.tick_with_usage({"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)})
        assert DECISION_KEY not in h.state()

    def test_publish_skips_unchanged_decision_within_300s(self, temp_home):
        h = make(temp_home)
        usage = {"1": win(10, 10), "2": win(0, 10), "3": win(0, 10)}
        h.tick_with_usage(usage)
        first = h.state()[DECISION_KEY]["at"]
        h.clock.advance(200)
        h.tick_with_usage(usage)
        assert h.state()[DECISION_KEY]["at"] == first
        h.clock.advance(150)
        h.tick_with_usage(usage)
        assert h.state()[DECISION_KEY]["at"] == h.clock.now
        h.clock.advance(10)
        h.tick_with_usage({**usage, "1": win(62, 10)})  # a new decision: written at once
        assert h.state()[DECISION_KEY]["at"] == h.clock.now


class TestPause:
    """A re-login in the TUI pauses the engine (pausedUntil in the state file)."""

    def test_paused_tick_neither_switches_nor_primes(self, temp_home):
        h = make(temp_home)
        rt = runtime_for(h.engine)
        primer = FakePrimer()
        rt.primer, rt.prime_settings = primer, PrimeSettings(enabled=True)
        pause.pause(h.switcher.backup_dir, "relogin", now=h.clock.now)
        usage = {"1": win(100, 40), "2": win(0, 10), "3": win(0, 50)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.active_number() == 1 and primer.calls == []
        assert no_switch_reasons(h) == ["maximize-paused"]
        assert "relogin" in of(h, NoSwitchEvent)[0].detail
        assert not of(h, MaximizeDecisionEvent)
        # It expires on its own after 10 minutes.
        h.clock.advance(pause.MAX_PAUSE_S + 1)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert primer.calls == ["2"]

    def test_pause_landing_mid_tick_blocks_the_switch_under_the_lock(self, temp_home):
        # The tick read the state before the pause was written: the locked
        # re-check right before the switch still sees it.
        from claude_swap.maximize import engine_hook

        h = make(temp_home)
        root = h.switcher.backup_dir
        real = engine_hook.policy.decide

        def decide_then_pause(snap):
            decision = real(snap)
            pause.pause(root, "relogin", now=h.clock.now)
            return decision

        usage = {"1": win(100, 40), "2": win(0, 10), "3": win(0, 50)}
        with patch.object(engine_hook.policy, "decide", side_effect=decide_then_pause):
            assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert not of(h, SwitchEvent)
        assert "maximize-paused" in no_switch_reasons(h)
        assert "lastSwitchAt" not in h.state()

    def test_resume_lifts_the_pause_at_once(self, temp_home):
        h = make(temp_home)
        root = h.switcher.backup_dir
        pause.pause(root, "relogin", now=h.clock.now)
        pause.resume(root)
        assert "pausedUntil" not in h.state()
        usage = {"1": win(100, 40), "2": win(0, 10), "3": win(0, 50)}
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED


def set_login_deadline(h: EngineHarness, num: int, seconds_left: float) -> None:
    """Rewrite slot ``num``'s backup with a login deadline ``seconds_left`` away."""
    blob = {"accessToken": f"sk-{num}", "refreshToken": f"rt-{num}",
            "refreshTokenExpiresAt": int((h.clock.now + seconds_left) * 1000)}
    h.switcher._write_account_credentials(
        str(num), EMAILS[num], json.dumps({"claudeAiOauth": blob})
    )


def login_warnings(h: EngineHarness) -> list[str]:
    return [e.message for e in of(h, ConfigWarningEvent) if " login expire" in e.message]


class TestDeadLoginTargets:
    """A target switch_to refuses as login-dead is set aside and the policy
    decides again, like a target that failed to freshen."""

    def test_a_login_dead_refusal_re_decides_without_that_target(self, temp_home):
        h = make(temp_home)
        real = h.switcher.switch_to

        def refuse_2(number, **kw):
            if str(number) == "2":
                return {"switched": False, "reason": "login-dead", "loginProblem": "x"}
            return real(number, **kw)

        with patch.object(h.switcher, "switch_to", side_effect=refuse_2), \
             patch.object(h.switcher, "list_accounts"):
            outcome = h.tick_with_usage({"1": win(100, 30), "2": win(10, 10), "3": win(50, 50)})
        assert outcome is TickOutcome.SWITCHED
        assert h.state()["lastSwitchTo"] == "3"

    def test_a_freshen_login_dead_is_set_aside(self, temp_home):
        h = make(temp_home)
        with patch.object(
            type(h.engine), "_freshen_target",
            lambda self, n, e: "login-dead" if n == "2" else "ok",
        ), patch.object(h.switcher, "list_accounts"):
            outcome = h.tick_with_usage({"1": win(100, 30), "2": win(10, 10), "3": win(50, 50)})
        assert outcome is TickOutcome.SWITCHED
        assert h.state()["lastSwitchTo"] == "3"


class TestPollLineMarks:
    """Under maximize the per-tick line said "switch at 90%" (the legacy
    autoswitch.threshold) on the first tick and "switch at 95%" later;
    neither is when maximize switches."""

    def test_every_tick_names_the_maximize_marks(self, temp_home):
        from claude_swap.autoswitch import PollEvent

        h = make(temp_home, maximize={"soft5h": 50, "hard5h": 95, "soft7d": 90, "hard7d": 98})
        usage = {"1": win(10, 30), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        h.tick_with_usage(usage)
        lines = [e.human() for e in of(h, PollEvent)]
        assert len(lines) == 2
        for line in lines:
            assert "switch at" not in line
            assert "(5h soft 50/hard 95 · 7d soft 90/hard 98)" in line

    def test_other_strategies_keep_the_threshold_label(self, temp_home):
        from claude_swap.autoswitch import PollEvent

        h = EngineHarness(temp_home)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)
        h.tick_with_usage({"1": win(10, 30), "2": win(0, 10)})
        [poll] = of(h, PollEvent)
        assert "(switch at " in poll.human()


class TestLoginExpiryWarning:
    def test_one_engine_log_line_per_account_per_day_inside_the_last_week(self, temp_home):
        h = make(temp_home)
        set_login_deadline(h, 2, 2 * 86400)
        set_login_deadline(h, 3, 20 * 86400)       # outside the week: silent
        usage = {"1": win(10, 30), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        [line] = login_warnings(h)
        assert line.startswith("Account-2 login expires ") and " (in 2d 0h)" in line
        assert line.endswith("before then, re-login #2: cc-swap login 2, or Fleet → "
                             "select → r")
        assert "@" not in line and "rt-2" not in line and "sk-2" not in line
        h.clock.advance(3600)
        h.tick_with_usage(usage)
        assert len(login_warnings(h)) == 1          # once a day
        h.clock.advance(86400)
        h.tick_with_usage(usage)
        assert len(login_warnings(h)) == 2

    def test_the_active_slot_is_read_from_the_live_login(self, temp_home):
        h = make(temp_home)
        live = {"accessToken": "sk-live", "refreshToken": "rt-live",
                "refreshTokenExpiresAt": int((h.clock.now - 60) * 1000)}
        (h.temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": live})
        )
        h.tick_with_usage({"1": win(10, 30), "2": win(0, 10), "3": win(0, 50)})
        [line] = login_warnings(h)
        assert line.startswith("Account-1 login expired ")


class TestActiveCredentialUnreadableMaximize:
    def test_rc36_on_the_active_read_holds_maximize_without_writes(self, temp_home):
        from claude_swap.credentials import ActiveCredentials
        from tests.test_autoswitch import tick_with_active_read

        h = make(temp_home)
        denied = ActiveCredentials("", True, True)
        state_path = h.switcher.backup_dir / "autoswitch_state.json"
        before = state_path.read_bytes() if state_path.exists() else None
        for usage in (
            {"1": None, "2": win(0, 10), "3": win(0, 50)},          # usage unknown
            {"1": win(100, 40), "2": win(0, 10), "3": win(0, 50)},  # even at limit
        ):
            for _ in range(5):
                assert tick_with_active_read(h, usage, denied) is TickOutcome.NO_ACTION
                h.clock.advance(60)
        assert h.active_number() == 1 and not of(h, SwitchEvent)
        assert not of(h, MaximizeDecisionEvent)
        assert h.engine._unhealthy_ticks == 0
        assert (state_path.read_bytes() if state_path.exists() else None) == before
        assert [m for m in (e.message for e in of(h, ConfigWarningEvent))
                if "Keychain unreadable" in m] != []


def test_maximize_adopts_a_new_login_on_a_dead_active_slot(temp_home):
    from claude_swap import oauth
    from claude_swap.autoswitch import LoginAdoptedEvent
    from claude_swap.credentials import ActiveCredentials
    from claude_swap.json_output import USAGE_LOGIN_EXPIRED
    from tests.test_autoswitch import tick_with_active_read

    h = make(temp_home)
    h.engine._quarantine("1", EMAILS[1], "login_expired")
    live = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-fresh", "refreshToken": "rt-fresh", "expiresAt": 9_999_999_999_000,
    }})
    (h.temp_home / ".claude" / ".credentials.json").write_text(live)
    profile = {"uuid": "uuid-1", "email": EMAILS[1], "organizationUuid": None}
    usage = {"1": USAGE_LOGIN_EXPIRED, "2": win(0, 10), "3": win(0, 50)}
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=profile):
        out = tick_with_active_read(h, usage, ActiveCredentials(live, False, False))
    assert out is TickOutcome.NO_ACTION and h.active_number() == 1
    assert of(h, LoginAdoptedEvent) and not of(h, SwitchEvent)
    stored = h.switcher.read_account_credentials("1", EMAILS[1])
    assert oauth.credential_fingerprint(stored) == oauth.credential_fingerprint(live)


class TestLoginExpiryGuardEngine:
    def test_soft_switch_lands_past_an_account_whose_login_is_about_to_expire(
        self, temp_home
    ):
        h = make(temp_home)
        set_login_deadline(h, 2, 90 * 60)  # inside the 120-minute guard
        usage = {"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)}
        for _ in range(3):
            if h.tick_with_usage(usage) is TickOutcome.SWITCHED:
                break
            h.clock.advance(300)
        assert h.active_number() == 3
        assert of(h, SwitchEvent)[-1].trigger == "soft"


def test_combined_soft_idle_last_resort_excluded_and_priming(temp_home):
    """Spec §10: the features together — the shape where separately green
    upstream PRs misbehaved once combined."""
    h = make(temp_home, n=4, maximize={"lastResort": EMAILS[3]})
    disable(h, 4)
    rt = runtime_for(h.engine)
    primer = FakePrimer()
    rt.primer, rt.prime_settings = primer, PrimeSettings(enabled=True)

    # A: 1 crosses soft; the only normal peer (2) cannot land, so the
    # last_resort account (3) is used — after idle. 4 (excluded) is never it.
    usage = {"1": win(62, 40), "2": win(48, 10), "3": win(0, 10), "4": win(0, 0)}
    outcomes = []
    for _ in range(3):
        outcomes.append(h.tick_with_usage(usage))
        h.clock.advance(300)
    assert outcomes == [TickOutcome.NO_ACTION, TickOutcome.NO_ACTION, TickOutcome.SWITCHED]
    assert h.active_number() == 3
    assert of(h, SwitchEvent)[-1].trigger == "soft"
    assert primer.calls == ["1", "1", "3"]          # last call sees the new active

    # B: 2's 5h rolls over; leaving last_resort is a rebalance, so it waits
    # out the 30-minute cooldown and an idle stretch on 3.
    usage = {"1": win(62, 40), "2": win(0, 10), "3": win(5, 12), "4": win(0, 0)}
    outcomes = []
    for _ in range(6):
        outcomes.append(h.tick_with_usage(usage))
        if outcomes[-1] is TickOutcome.SWITCHED:
            break
        h.clock.advance(300)
    assert outcomes[-1] is TickOutcome.SWITCHED and len(outcomes) == 6
    assert h.active_number() == 2
    assert of(h, SwitchEvent)[-1].trigger == "rebalance"
    holds = [e.detail for e in of(h, NoSwitchEvent)][-5:]
    assert all(d.startswith("rebalance cooldown") for d in holds)

    # C: the user logs into the excluded account by hand; at idle (and past
    # the cooldown, which the manual login restarted when the first tick saw
    # it) the engine moves off it to the best normal account.
    h.make_live(EMAILS[4], 4)
    usage = {"1": win(0, 40), "2": win(30, 20), "3": win(5, 12), "4": win(10, 5)}
    outcomes = []
    for _ in range(8):
        h.clock.advance(300)
        outcomes.append(h.tick_with_usage(usage))
        if outcomes[-1] is TickOutcome.SWITCHED:
            break
    assert outcomes[-1] is TickOutcome.SWITCHED and len(outcomes) == 7
    assert h.active_number() == 2
    assert of(h, SwitchEvent)[-1].trigger == "rebalance"

    assert all(e.to_ref["number"] != 4 for e in of(h, SwitchEvent))
    assert len(primer.calls) == len(of(h, MaximizeDecisionEvent))
    assert primer.calls[-1] == "2"


class TestSharedLogin:
    """A login also held elsewhere (shared_login.py): a switch onto it is
    refused, so the policy never targets it, and ``cc-swap why`` says so."""

    @staticmethod
    def _entries(h, usage: dict, shared: set[str]) -> dict:
        from dataclasses import replace

        from claude_swap.shared_login import SHARED_LOGIN
        from tests.test_autoswitch import _entry_for

        out = {}
        for num, value in usage.items():
            entry = _entry_for(value, h.clock.now)
            out[num] = replace(entry, last_error=SHARED_LOGIN) if num in shared else entry
        return out

    def test_a_shared_slot_is_never_the_published_target(self, temp_home):
        h = make(temp_home)
        usage = {"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_entries(self._entries(h, usage, {"2"}))
        record = h.state()[DECISION_KEY]
        assert (record["decision"], record["pending"], record["target"]) == ("hold", True, "3")
        assert record["shared"] == ["2"]

    def test_a_hard_switch_goes_past_a_shared_slot(self, temp_home):
        h = make(temp_home)
        usage = {"1": win(96, 40), "2": win(0, 10), "3": win(0, 50)}
        assert h.tick_with_entries(self._entries(h, usage, {"2"})) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert h.state()[DECISION_KEY]["target"] == "3"

    def test_why_names_the_skip(self, temp_home):
        from claude_swap.maximize.doctor_cli import _why_lines, published_why

        h = make(temp_home)
        usage = {"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_entries(self._entries(h, usage, {"2"}))
        why = published_why(h.switcher.backup_dir, now=h.clock.now)
        assert why is not None and why["shared"] == ["2"] and why["target"] == "3"
        lines = "\n".join(_why_lines(why))
        assert "skipped  #2 shares its login — re-login one of them (cc-swap login 2)" in lines

    def test_no_shared_slot_publishes_no_shared_key(self, temp_home):
        h = make(temp_home)
        h.tick_with_usage({"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)})
        assert "shared" not in h.state()[DECISION_KEY]
