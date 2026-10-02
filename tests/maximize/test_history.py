"""Usage history and the learned idle pattern (maximize/history.py). No clock,
no network: every function takes ``now``, and local time is ``time.gmtime``."""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from claude_swap.maximize import history as hist
from claude_swap.maximize.history import (
    History,
    Recorder,
    SlotObs,
    UsagePoint,
    burn_rate,
    burn_rates,
    compacted,
    describe,
    forecast,
    learn,
    read,
    slot_busy,
    summary,
    trimmed,
)
from claude_swap.maximize.model import Sample

MONDAY = 1_790_553_600.0  # 2026-09-28 00:00 UTC, a Monday
H = 3600.0
D = 86400.0
UTC = time.gmtime


def point(hours: float, number: str = "1", p7: float = 0.0, *, active: bool = True,
          p5: float = 0.0, start: float = MONDAY) -> UsagePoint:
    return UsagePoint(start + hours * H, number, p5, p7, active)


def week_slots(days: int, busy: tuple[float, float] = (9, 18), *, start: float = MONDAY,
               weekend_busy: tuple[float, float] | None = None) -> list[SlotObs]:
    """One observation per 15-min slot for ``days`` days: busy inside the
    hours ``busy`` on weekdays (``weekend_busy`` on weekends, else never)."""
    out = []
    for i in range(int(days * 96)):
        ts = start + i * hist.SLOT_S
        lt = time.gmtime(ts)
        hour = lt.tm_hour + lt.tm_min / 60
        span = busy if lt.tm_wday < 5 else weekend_busy
        out.append(SlotObs(ts, span is not None and span[0] <= hour < span[1]))
    return out


def lines(root) -> list[dict]:
    return [json.loads(x) for x in hist.path_for(root).read_text().splitlines()]


# -- the file ------------------------------------------------------------------------------


class TestFile:
    def test_read_skips_torn_and_foreign_lines(self, tmp_path):
        hist.path_for(tmp_path).write_text(
            '{"k":"u","t":100,"n":"1","p5":1,"p7":2,"a":1}\n'
            "not json\n"
            '{"k":"x","t":5}\n'
            '{"k":"u","t":"soon","n":"1","p5":1,"p7":2}\n'
            '["k"]\n'
            '{"k":"s","t":900,"b":1}\n'
            '{"k":"u","t":200,"n":"2","p5":1'
        )
        h = read(tmp_path)
        assert h.points == (UsagePoint(100.0, "1", 1.0, 2.0, True),)
        assert h.slots == (SlotObs(900.0, True),)

    def test_missing_file_is_empty(self, tmp_path):
        assert read(tmp_path) == History()

    def test_compaction_keeps_the_first_point_per_account_hour_and_one_obs_per_slot(self):
        h = History(
            points=(point(0.1), point(0.5, p7=9), point(0.2, "2"), point(1.1, p7=3)),
            slots=(SlotObs(900, False), SlotObs(900, True), SlotObs(1800, False)),
        )
        got = compacted(h)
        assert [p.number for p in got.points] == ["1", "2", "1"]
        assert [p.ts - MONDAY for p in got.points] == pytest.approx([0.1 * H, 0.2 * H, 1.1 * H])
        assert got.slots == (SlotObs(900, True), SlotObs(1800, False))

    def test_trimming_keeps_8_days_of_points_and_14_of_slots(self):
        now = MONDAY + 20 * D
        h = History(
            points=(point(-9 * 24, start=now), point(-7 * 24, start=now), point(1, start=now)),
            slots=(SlotObs(now - 15 * D, True), SlotObs(now - 13 * D, True)),
        )
        got = trimmed(h, now)
        assert [p.ts for p in got.points] == [now - 7 * D]
        assert [s.ts for s in got.slots] == [now - 13 * D]


# -- recording ------------------------------------------------------------------------------


def flat(ts: float, p5: float = 10.0) -> Sample:
    return Sample(ts, p5, 20.0)


class TestSlotBusy:
    S = MONDAY + 10 * H  # a slot start

    def test_a_rise_inside_the_slot_is_busy(self):
        assert slot_busy([flat(self.S - 300), flat(self.S + 300, 11)], self.S) is True

    def test_flat_readings_are_observed_and_quiet(self):
        assert slot_busy([flat(self.S - 300), flat(self.S + 300)], self.S) is False

    def test_a_5h_reset_drop_is_not_busy(self):
        assert slot_busy([flat(self.S - 300, 80), flat(self.S + 300, 0)], self.S) is False

    def test_no_reading_inside_or_a_long_hole_is_unobserved(self):
        assert slot_busy([flat(self.S - 600), flat(self.S - 100, 30)], self.S) is None
        assert slot_busy([flat(self.S - 3600), flat(self.S + 300, 30)], self.S) is None
        assert slot_busy([], self.S) is None


class TestRecorder:
    def test_one_point_per_account_per_hour_with_the_active_flag(self, tmp_path):
        rec = Recorder(tmp_path)
        now = MONDAY + 10 * H
        rec.observe(now, "1", {"1": (now, 5, 40), "2": (now - 60, 0, 10)}, (), slots=False)
        rec.observe(now + 600, "1", {"1": (now + 600, 6, 41)}, (), slots=False)
        rec.observe(now + H, "2", {"1": (now + H, 7, 42)}, (), slots=False)
        assert [(x["n"], x["p7"], x["a"]) for x in lines(tmp_path)] == [
            ("1", 40, 1), ("2", 10, 0), ("1", 42, 0)]

    def test_stale_and_future_readings_are_not_recorded(self, tmp_path):
        rec = Recorder(tmp_path)
        now = MONDAY + 10 * H
        rec.observe(now, "1", {"1": (now - 1000, 5, 40), "2": (now + 5, 0, 10)}, (), slots=False)
        assert not hist.path_for(tmp_path).exists()

    def test_a_slot_is_judged_once_it_has_settled(self, tmp_path):
        rec = Recorder(tmp_path)
        s = MONDAY + 10 * H
        samples = [flat(s - 300), flat(s + 300, 12), flat(s + 800, 12)]
        rec.observe(s + 900 + hist.SLOT_SETTLE_S - 1, "1", {}, samples, points=False)
        assert rec.history.slots == ()
        rec.observe(s + 900 + hist.SLOT_SETTLE_S, "1", {}, samples, points=False)
        assert rec.history.slots == (SlotObs(s, True),)
        rec.observe(s + 900 + hist.SLOT_SETTLE_S + 60, "1", {}, samples, points=False)
        assert [x["t"] for x in lines(tmp_path)] == [s]

    def test_a_restart_does_not_record_twice(self, tmp_path):
        now = MONDAY + 10 * H
        samples = [flat(now - 1200), flat(now - 700, 11)]
        Recorder(tmp_path).observe(now, "1", {"1": (now, 5, 40)}, samples)
        before = hist.path_for(tmp_path).read_text()
        Recorder(tmp_path).observe(now + 60, "1", {"1": (now + 60, 5, 40)}, samples)
        assert hist.path_for(tmp_path).read_text() == before

    def test_write_false_keeps_it_in_memory(self, tmp_path):
        rec = Recorder(tmp_path)
        now = MONDAY + 10 * H
        rec.observe(now, "1", {"1": (now, 5, 40)}, (), write=False)
        assert len(rec.history.points) == 1
        assert not hist.path_for(tmp_path).exists()

    def test_points_and_slots_can_be_off(self, tmp_path):
        rec = Recorder(tmp_path)
        s = MONDAY + 10 * H
        samples = [flat(s - 300), flat(s + 300, 12)]
        rec.observe(s + 1200, "1", {"1": (s + 1200, 5, 40)}, samples, points=False, slots=False)
        assert rec.history == History()

    def test_the_file_is_private_and_compacted_once_it_outgrows_what_it_keeps(self, tmp_path):
        rec = Recorder(tmp_path)
        start = MONDAY
        for hour in range(10 * 24):  # ten days, one account: 8 are kept
            now = start + hour * H
            rec.observe(now, "1", {"1": (now, 0, 0)}, (), slots=False)
        path = hist.path_for(tmp_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        kept = len(rec.history.points)
        assert kept == 8 * 24 + 1
        on_disk = len(path.read_text().splitlines())
        assert on_disk <= 2 * kept + hist.COMPACT_SLACK_LINES
        assert read(tmp_path, start + (10 * 24 - 1) * H).points == rec.history.points

    def test_a_file_too_big_to_read_whole_is_compacted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hist, "MAX_READ_BYTES", 400)
        now = MONDAY + 10 * H
        body = "".join(
            json.dumps({"k": "u", "t": now - i * H, "n": "1", "p5": 0, "p7": i, "a": 1}) + "\n"
            for i in range(30, 0, -1)
        )
        hist.path_for(tmp_path).write_text(body)
        Recorder(tmp_path).observe(now, "1", {}, (), slots=False)
        assert os.path.getsize(hist.path_for(tmp_path)) <= 400


# -- 7d burn rate -----------------------------------------------------------------------------


class TestBurnRate:
    NOW = MONDAY + 2 * D

    def series(self, p7s, *, active=True, number="1", step_h=1.0, end=None):
        end = self.NOW if end is None else end
        n = len(p7s)
        return [UsagePoint(end - (n - 1 - i) * step_h * H, number, 0, p, active)
                for i, p in enumerate(p7s)]

    def test_a_steady_climb(self):
        assert burn_rate(self.series([10, 12, 14, 16, 18]), "1", self.NOW) == pytest.approx(2.0)

    def test_a_7d_reset_adds_nothing(self):
        # 80 -> 2 is the weekly rollover; the hours around it still count.
        got = burn_rate(self.series([76, 78, 80, 2, 4, 6]), "1", self.NOW)
        unweighted = (2 + 2 + 0 + 2 + 2) / 5
        assert 0 < got < 2.0
        assert got == pytest.approx(unweighted, rel=0.1)

    def test_only_hours_it_was_active_count(self):
        # +10/h while another account was active (someone else's machine).
        elsewhere = self.series([20, 30, 40, 50], active=False, end=self.NOW - 5 * H)
        handover = [UsagePoint(self.NOW - 4 * H, "1", 0, 8, False)]  # inactive -> active
        assert burn_rate(elsewhere + handover + self.series([10, 12, 14, 16]),
                         "1", self.NOW) == pytest.approx(2.0)

    def test_holes_are_skipped(self):
        points = self.series([10, 12, 14, 16]) + [
            UsagePoint(self.NOW - 10 * H, "1", 0, 0, True)]  # 7 h before the next one
        assert burn_rate(points, "1", self.NOW) == pytest.approx(2.0)

    def test_fewer_than_three_active_hours_is_unknown(self):
        assert burn_rate(self.series([10, 12, 14]), "1", self.NOW) is None
        assert burn_rate([], "1", self.NOW) is None

    def test_points_older_than_48h_are_ignored(self):
        old = self.series([0, 50, 100, 150], end=self.NOW - 49 * H)
        assert burn_rate(old + self.series([10, 11, 12, 13]), "1", self.NOW) == pytest.approx(1.0)

    def test_recent_hours_weigh_more(self):
        # 12 slow hours (1/h) ending 12 h ago, then 3 fast ones (4/h) now.
        slow = self.series([float(i) for i in range(13)], end=self.NOW - 12 * H)
        fast = self.series([20, 24, 28, 32])
        got = burn_rate(slow + fast, "1", self.NOW)
        plain = (12 * 1 + 3 * 4) / 15
        assert got > plain

    def test_rates_for_every_account_that_has_one(self):
        points = self.series([1, 2, 3, 4]) + self.series([5, 7, 9, 11], number="2") + \
            self.series([1, 2], number="3")
        assert burn_rates(points, self.NOW) == pytest.approx({"1": 1.0, "2": 2.0})


# -- idle pattern -----------------------------------------------------------------------------


class TestPattern:
    def test_a_cold_start_has_no_pattern(self):
        slots = week_slots(2)
        assert forecast(slots, MONDAY + 2 * D, UTC) is None
        assert learn(slots, MONDAY + 2 * D, UTC).days == 2
        assert forecast(week_slots(3), MONDAY + 3 * D, UTC) is not None

    def test_weekdays_and_weekends_are_learned_apart(self):
        slots = week_slots(14, busy=(9, 18), weekend_busy=(20, 23))
        pattern = learn(slots, MONDAY + 14 * D, UTC)
        assert pattern.days == 14
        assert pattern.p_busy((False, 10 * 4)) == 1.0      # weekday 10:00
        assert pattern.p_busy((True, 10 * 4)) == 0.0       # weekend 10:00
        assert pattern.p_busy((True, 21 * 4)) == 1.0       # weekend 21:00
        assert pattern.p_busy((False, 21 * 4)) == 0.0

    def test_observations_older_than_14_days_are_forgotten(self):
        slots = week_slots(21)
        assert learn(slots, MONDAY + 21 * D, UTC).days == 14

    def test_quiet_windows_from_a_weekday_routine(self):
        slots = week_slots(7)  # busy 09-18 on weekdays, quiet otherwise
        now = MONDAY + 7 * D + 10 * H  # the next Monday, 10:00 (busy)
        f = forecast(slots, now, UTC)
        assert f is not None and f.days == 7
        assert f.p_busy_now == 1.0 and f.current is None
        assert (f.next.start - now) / H == 8.0                 # 18:00 today
        assert (f.next.start_label, f.next.end_label) == ("18:00", "09:00")
        inside = forecast(slots, MONDAY + 7 * D + 20 * H, UTC)
        assert inside.current is not None and inside.current.end_label == "09:00"
        assert inside.next.start_label == "18:00"

    def test_a_quiet_window_needs_an_hour_under_20_percent(self):
        # Weekdays busy 09-18 except a 45-minute break at 12:00 one day in
        # three (P(busy) 0.67 -> not quiet) and every day (but too short).
        slots = []
        for s in week_slots(7):
            lt = time.gmtime(s.ts)
            hour = lt.tm_hour + lt.tm_min / 60
            if lt.tm_wday < 5 and 12 <= hour < 12.75:
                s = SlotObs(s.ts, False)
            slots.append(s)
        f = forecast(slots, MONDAY + 7 * D + 10 * H, UTC)
        assert f.next.start_label == "18:00"   # the 45-min break is no window

    def test_slots_never_observed_are_not_quiet(self):
        # Observed only 09:00-18:00 (busy) and 18:00-19:00 (quiet): the
        # unobserved night is no quiet window, the observed hour is.
        slots = [s for s in week_slots(7)
                 if 9 <= time.gmtime(s.ts).tm_hour < 19 and time.gmtime(s.ts).tm_wday < 5]
        f = forecast(slots, MONDAY + 7 * D + 10 * H, UTC)
        assert (f.next.start_label, f.next.end_label) == ("18:00", "19:00")

    def test_describe_and_summary(self):
        now = MONDAY + 7 * D + 10 * H
        assert describe([], now, localtime=UTC) == "idle pattern: learning (0 of 3 days observed)"
        assert describe(week_slots(7), now, localtime=UTC) == (
            "idle pattern: 7 days learned, next quiet window 18:00–09:00")
        assert describe(week_slots(7), MONDAY + 7 * D + 20 * H, localtime=UTC) == (
            "idle pattern: 7 days learned, quiet now until 09:00")
        assert describe(week_slots(7), now, enabled=False) == (
            "idle pattern: off (maximize.learnIdlePattern)")
        got = summary(week_slots(7), now, localtime=UTC)
        assert got["learned"] and got["days"] == 7 and got["pBusyNow"] == 1.0
        assert got["nextQuiet"]["startLabel"] == "18:00" and got["quietNow"] is None
        json.dumps(got)
