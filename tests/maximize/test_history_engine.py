"""The usage history, preempt and the idle pattern through the real engine
(EngineHarness), on a simulated clock. Local time is pinned to UTC."""

from __future__ import annotations

import json
import os
import time

import pytest

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.maximize import doctor_cli, pause
from claude_swap.maximize import history as hist
from claude_swap.maximize.engine_hook import DECISION_KEY
from claude_swap.maximize.history import History, SlotObs, UsagePoint
from tests.maximize.test_engine_maximize import make, no_switch_reasons, of, win

MONDAY = 1_790_553_600.0  # 2026-09-28 00:00 UTC
H = 3600.0
D = 86400.0
TICK = 600.0


@pytest.fixture(autouse=True)
def utc():
    """Slots of the day and weekdays are local time: pin it."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def busy_hour(ts: float) -> bool:
    lt = time.gmtime(ts)
    return lt.tm_wday < 5 and 9 <= lt.tm_hour < 18


# -- a simulated week -------------------------------------------------------------------


class TestAWeekOfUse:
    """Mon-Thu the user works 09:00-18:00 on #1, its 7d climbing 0.6 pt/h;
    the engine learns the routine. Friday morning is a heavy burst (9 pt/h):
    by lunch #1's 7d would pass 90% before 18:00, so maximize moves at the
    first idle moment — not at 90% mid-afternoon."""

    def test_preempt_moves_at_lunch_before_a_busy_afternoon_crossing(self, temp_home):
        h = make(temp_home, maximize={"tieEpsilon": 2})  # no rebalance noise
        h.clock.now = MONDAY
        p5, p7 = 0.0, 30.0
        friday_burst_end = MONDAY + 4 * D + 12 * H + 50 * 60
        before_friday = 0
        while h.clock.now < MONDAY + 4 * D + 14 * H and h.active_number() == 1:
            now = h.clock.now
            friday = now >= MONDAY + 4 * D
            if not friday:
                before_friday = len(no_switch_reasons(h)) + 1
            busy = busy_hour(now) and (not friday or now < friday_burst_end)
            if busy:
                p5 = (p5 + 1) % 40
                p7 += 1.5 if friday else 0.1
            h.tick_with_usage({"1": win(p5, p7), "2": win(0, 10), "3": win(0, 20)})
            h.clock.advance(TICK)

        [switch] = of(h, SwitchEvent)
        assert switch.trigger == "preempt" and h.active_number() == 2
        switched_at = h.clock.now - TICK
        assert MONDAY + 4 * D + 12 * H + 40 * 60 <= switched_at <= MONDAY + 4 * D + 13 * H
        decision = h.state()[DECISION_KEY]
        assert decision["decision"] == "switch" and decision["trigger"] == "preempt"
        assert "before your usual quiet time (18:00)" in decision["reason"]
        assert decision["reason"].endswith("— moving to b now while you're idle")

        reasons = no_switch_reasons(h)
        # Mon-Thu: nothing but ordinary holds (one per tick); Friday's burst
        # first shows the wish to move while there is no idle moment for it.
        assert before_friday == 4 * 24 * 6
        assert set(reasons[:before_friday]) == {"maximize-hold"}
        assert set(reasons[before_friday:]) == {"maximize-hold", "preempt"}
        held = [e for e in of(h, NoSwitchEvent) if e.reason == "preempt"]
        assert all("at the next idle moment" in e.detail for e in held)

        # What it learned, and that it kept only slot numbers and numbers.
        text = hist.path_for(h.switcher.backup_dir).read_text()
        assert "@" not in text
        kept = hist.read(h.switcher.backup_dir, switched_at)
        assert {p.number for p in kept.points} == {"1", "2", "3"}
        assert len(kept.slots) > 4 * 90
        pattern = doctor_cli.idle_pattern(h.switcher.backup_dir, now=switched_at)
        assert pattern["learned"] and pattern["days"] == 5
        assert pattern["nextQuiet"]["startLabel"] == "18:00"
        assert pattern["pBusyNow"] == 1.0


# -- seeded history: one decision at a time ------------------------------------------------


FRIDAY_NOON = MONDAY + 4 * D + 12 * H


def seed(h, *, now: float = FRIDAY_NOON, rate: float | None = 2.0, p7: float = 84.0) -> None:
    """Four weekdays of a 09:00-18:00 routine before ``now``, and #1 active
    for the last six hours with its 7d climbing ``rate`` pt/h up to ``p7``."""
    slots = []
    t = MONDAY
    while t < now - H:
        slots.append(SlotObs(t, busy_hour(t)))
        t += hist.SLOT_S
    points = [] if rate is None else [
        UsagePoint(now - k * H, "1", 30.0, p7 - k * rate, True) for k in range(6, 0, -1)
    ]
    hist._rewrite(hist.path_for(h.switcher.backup_dir), History(tuple(points), tuple(slots)))


USAGE = {"1": win(30, 84), "2": win(0, 10), "3": win(0, 20)}


def two_idle_ticks(h, usage=USAGE) -> TickOutcome:
    h.clock.now = FRIDAY_NOON
    h.tick_with_usage(usage)
    h.clock.advance(TICK)
    return h.tick_with_usage(usage)


class TestSeeded:
    def test_preempt_switches_at_the_first_idle_tick(self, temp_home):
        h = make(temp_home, maximize={"tieEpsilon": 2})
        seed(h)
        assert two_idle_ticks(h) is TickOutcome.SWITCHED
        assert no_switch_reasons(h) == ["preempt"]          # first tick: not idle yet
        assert [e.trigger for e in of(h, SwitchEvent)] == ["preempt"]
        assert h.active_number() == 2

    def test_preempt_off_keeps_the_old_behaviour(self, temp_home):
        h = make(temp_home, maximize={"tieEpsilon": 2, "preempt": False})
        seed(h)
        assert two_idle_ticks(h) is TickOutcome.NO_ACTION
        assert no_switch_reasons(h) == ["maximize-hold"] * 2

    def test_no_burn_rate_yet_means_no_preempt(self, temp_home):
        h = make(temp_home, maximize={"tieEpsilon": 2})
        seed(h, rate=None)
        assert two_idle_ticks(h) is TickOutcome.NO_ACTION
        assert "preempt" not in no_switch_reasons(h)

    def test_auto_off_still_decides_but_never_switches(self, temp_home):
        h = make(temp_home, maximize={"tieEpsilon": 2})
        seed(h)
        pause.set_auto_off(h.switcher.backup_dir, True, by="cli", now=FRIDAY_NOON)
        assert two_idle_ticks(h) is TickOutcome.NO_ACTION
        assert h.active_number() == 1 and not of(h, SwitchEvent)
        assert h.state()[DECISION_KEY]["trigger"] == "preempt"
        assert "auto-off" in no_switch_reasons(h)

    def test_a_relogin_pause_blocks_it(self, temp_home):
        h = make(temp_home, maximize={"tieEpsilon": 2})
        seed(h)
        pause.pause(h.switcher.backup_dir, "relogin", now=FRIDAY_NOON)  # 10 minutes
        h.clock.now = FRIDAY_NOON
        for _ in range(3):
            assert h.tick_with_usage(USAGE) is TickOutcome.NO_ACTION
            h.clock.advance(250)
        assert no_switch_reasons(h) == ["maximize-paused"] * 3
        assert h.active_number() == 1

    def test_the_hold_is_published_with_its_code(self, temp_home):
        h = make(temp_home, maximize={"tieEpsilon": 2})
        seed(h)
        h.clock.now = FRIDAY_NOON
        h.tick_with_usage(USAGE)
        record = h.state()[DECISION_KEY]
        assert (record["decision"], record["code"]) == ("hold", "preempt")

    def test_a_small_rebalance_in_a_busy_hour_waits_for_the_quiet_window(self, temp_home):
        h = make(temp_home)
        seed(h, now=FRIDAY_NOON + H, rate=None)   # 13:00: 5 h before 18:00
        r7 = {"1": FRIDAY_NOON + 7 * D, "2": FRIDAY_NOON + 6 * D}
        usage = {"1": win(10, 30, r7=r7["1"]), "2": win(0, 25, r7=r7["2"])}
        h.clock.now = FRIDAY_NOON + H
        h.tick_with_usage(usage)
        h.clock.advance(TICK)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert no_switch_reasons(h)[-1] == "rebalance-deferred"
        record = h.state()[DECISION_KEY]
        assert record["code"] == "rebalance-deferred"
        assert record["reason"].startswith("rebalance deferred to your quiet time (18:00)")

    def test_learning_off_neither_records_slots_nor_defers(self, temp_home):
        h = make(temp_home, maximize={"learnIdlePattern": False})
        h.clock.now = FRIDAY_NOON + H
        r7 = {"1": FRIDAY_NOON + 7 * D, "2": FRIDAY_NOON + 6 * D}
        usage = {"1": win(10, 30, r7=r7["1"]), "2": win(0, 25, r7=r7["2"])}
        for _ in range(4):
            h.tick_with_usage(usage)
            h.clock.advance(TICK)
        assert "rebalance-deferred" not in no_switch_reasons(h)
        assert hist.read(h.switcher.backup_dir).slots == ()

    def test_a_dry_run_reads_the_history_but_writes_none(self, temp_home):
        h = make(temp_home, maximize={"tieEpsilon": 2})
        seed(h)
        before = hist.path_for(h.switcher.backup_dir).read_bytes()
        h.engine = h._make_engine(dry_run=True)
        two_idle_ticks(h)
        assert [e.trigger for e in of(h, SwitchEvent)] == ["preempt"]
        assert hist.path_for(h.switcher.backup_dir).read_bytes() == before

    def test_a_broken_history_never_breaks_a_tick(self, temp_home, monkeypatch):
        h = make(temp_home, maximize={"tieEpsilon": 2})

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(hist.Recorder, "observe", boom)
        assert two_idle_ticks(h) is TickOutcome.NO_ACTION
        assert no_switch_reasons(h) == ["maximize-hold"] * 2

    def test_the_history_holds_one_point_per_account_per_hour(self, temp_home):
        h = make(temp_home)
        h.clock.now = FRIDAY_NOON
        for _ in range(12):                      # two hours of 10-minute ticks
            h.tick_with_usage({"1": win(10, 10), "2": win(0, 10), "3": win(0, 20)})
            h.clock.advance(TICK)
        rows = [json.loads(x) for x in
                hist.path_for(h.switcher.backup_dir).read_text().splitlines()]
        points = [(r["n"], r["a"]) for r in rows if r["k"] == "u"]
        assert sorted(points) == [("1", 1), ("1", 1), ("2", 0), ("2", 0), ("3", 0), ("3", 0)]


def test_why_shows_the_pattern(temp_home, monkeypatch, capsys):
    h = make(temp_home)
    seed(h, rate=None)
    root = h.switcher.backup_dir
    monkeypatch.setattr(doctor_cli.paths, "get_backup_root", lambda: root)
    with pytest.raises(SystemExit):
        doctor_cli.why_command(["--no-fallback"], clock=lambda: FRIDAY_NOON)
    out = capsys.readouterr().out
    # Mon-Fri observed; the weekend never was, so tonight's window ends at
    # Saturday 00:00 (weekdays and weekends are learned apart).
    assert "pattern  5 days learned, next quiet window 18:00–00:00" in out
