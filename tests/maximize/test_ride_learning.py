"""The learned ride's memory (maximize/ride.py) and its engine bookkeeping
(maximize/engine_hook.py): AIMD on q, whole-point steps, and several rides
through the real engine (EngineHarness)."""

from __future__ import annotations

import json

import pytest

from claude_swap.autoswitch import MaximizeDecisionEvent, NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.maximize import ride
from claude_swap.maximize.engine_hook import DECISION_KEY, RIDE_KEY, runtime_for
from tests.maximize.test_engine_maximize import EMAILS, make, no_switch_reasons, of, win

# -- AIMD --------------------------------------------------------------------------------


class TestLearn:
    def test_defaults_for_a_missing_or_broken_record(self):
        for raw in (None, {}, "x", {"7d": {"q": "high", "n_ok": -3}}, {"5h": []}):
            data = ride.learned(raw)
            assert data["7d"] == {"q": 0.3, "n_ok": 0, "n_hit": 0, "updatedAt": None}
            assert data["5h"]["q"] == 0.3

    def test_success_adds_a_step(self):
        out = ride.learn(None, "7d", "ok", 100.0)
        assert out["7d"] == {"q": 0.35, "n_ok": 1, "n_hit": 0, "updatedAt": 100.0}
        assert out["5h"]["q"] == 0.3  # the other window is untouched

    def test_a_hit_halves_q(self):
        out = ride.learn({"7d": {"q": 0.4, "n_ok": 2}}, "7d", "hit", 5.0)
        assert out["7d"] == {"q": 0.2, "n_ok": 2, "n_hit": 1, "updatedAt": 5.0}

    def test_q_stays_between_005_and_09(self):
        data: object = None
        for _ in range(30):
            data = ride.learn(data, "5h", "ok", 0.0)
        assert ride.q_values(data)["5h"] == 0.9
        for _ in range(10):
            data = ride.learn(data, "5h", "hit", 0.0)
        assert ride.q_values(data)["5h"] == 0.05
        assert ride.q_values({"7d": {"q": 7}})["7d"] == 0.9

    def test_the_record_survives_json(self):
        data = ride.learn(ride.learn(None, "7d", "ok", 1.0), "5h", "hit", 2.0)
        assert ride.learned(json.loads(json.dumps(data))) == ride.learned(data)

    def test_describe(self):
        data = ride.learn(None, "7d", "ok", 1.0)
        assert ride.describe(data, ("7d",)) == (
            "learned ride: 5h off (rideWindows; learned 0.30) · "
            "7d rides 0.35 of the last point (1 ok, 0 hit)"
        )
        assert ride.describe(data, (), "maximize.learnedRide is false") == (
            "learned ride: off (maximize.learnedRide is false)"
        )


# -- steps -------------------------------------------------------------------------------


POLL = 300.0  # the active account's slowest normal cadence


def feed(
    readings: list[tuple[float, float]],
    *,
    gap_after: int | None = None,
    raw: dict | None = None,
    busy: bool = True,
) -> dict:
    """7d readings ``(ts, pct)`` of account 1, each the next fresh sample;
    the 5h rises by one point on every reading while ``busy``."""
    raw = raw or {}
    prev = None
    for i, (ts, pct) in enumerate(readings):
        p5 = 10.0 + i if busy else 10.0
        raw = ride.observe(raw, "1", p5, pct, ts, None if i == gap_after else prev)
        prev = ts
    return raw


def polled(start: float, end: float, p7_at) -> list[tuple[float, float]]:
    """Readings every :data:`POLL` from ``start`` to ``end`` inclusive, the
    7d given by ``p7_at(ts)``."""
    out, ts = [], start
    while ts <= end:
        out.append((ts, p7_at(ts)))
        ts += POLL
    return out


def seconds(raw, now, window="7d", number="1"):
    return ride.point_seconds(raw, number, window, now)


class TestSteps:
    def test_the_first_step_is_not_timed_the_second_is(self):
        raw = feed([(0, 96), (300, 96), (600, 97)])
        assert seconds(raw, 600) is None
        raw = feed([(0, 96), (300, 96), (600, 97), (900, 97), (1200, 97), (1500, 98)])
        assert seconds(raw, 1500) == 900.0

    def test_t1_is_the_median_of_the_last_three_intervals(self):
        readings = [(0, 90), (100, 91), (500, 92), (800, 93), (1300, 94), (1600, 95)]
        raw = feed(readings)
        # intervals 400, 300, 500, 300 -> the last three: 300, 500, 300
        assert [s for _, s in raw["1"]["7d"]["intervals"]] == [300.0, 500.0, 300.0]
        assert seconds(raw, 1600) == 300.0

    def test_a_jump_of_two_points_is_per_point(self):
        raw = feed([(0, 95), (60, 96), (360, 97), (660, 97), (960, 99)])
        assert seconds(raw, 960) == 300.0  # 300, then 600 s for two points

    def test_a_hole_or_a_new_tenure_leaves_the_next_step_untimed(self):
        raw = feed([(0, 95), (60, 96), (300, 96), (600, 97)], gap_after=3)
        assert seconds(raw, 600) is None
        raw = feed([(0, 95), (60, 96), (60 + ride.STEP_MAX_GAP_S + 1, 97)])
        assert seconds(raw, 60 + ride.STEP_MAX_GAP_S + 1) is None

    def test_unchanged_readings_change_nothing(self):
        raw = feed([(0, 95), (60, 96)])
        again = ride.observe(raw, "1", 11.0, 96.0, 120.0, 60.0)
        assert again["1"]["7d"] == raw["1"]["7d"]

    def test_accounts_are_kept_apart(self):
        raw = feed([(0, 95), (60, 96), (360, 97)])
        raw = ride.observe(raw, "2", 0.0, 50.0, 700.0, None)
        assert seconds(raw, 700) == 300.0
        assert seconds(raw, 700, number="2") is None


class TestStaleT1:
    """Reviewer's case: a T1 hours too long rides into 100%."""

    def test_an_overnight_idle_gap_is_never_timed(self):
        # Busy 17:30-18:00 (7d 96 -> 97 at 18:00), idle all night (the
        # service still polls every 300 s), busy again from 08:50 (7d 98 at
        # 09:00, 99 at 09:20).
        h = 3600.0
        evening = polled(17.5 * h, 18 * h, lambda t: 96 if t < 18 * h else 97)
        night = polled(18 * h + POLL, 8.75 * h + 24 * h, lambda t: 97)
        morning = polled(8.75 * h + 24 * h + POLL, 9 * h + 24 * h + 1200,
                         lambda t: 97 if t < 33 * h else 98 if t < 33 * h + 1200 else 99)
        raw: dict = {}
        prev = None
        for i, (ts, p7) in enumerate(evening + night + morning):
            busy = not (18 * h < ts <= 8.75 * h + 24 * h)
            p5 = 10.0 + i if busy else 10.0 + len(evening)
            raw = ride.observe(raw, "1", p5, p7, ts, prev)
            prev = ts
        now = 33 * h + 1200
        # The 18:00 -> 09:00 interval spans the idle night: only 09:00 ->
        # 09:20 is timed. (Untouched, the median was 7.67 h.)
        assert seconds(raw, now) == 1200.0

    def test_an_interval_spanning_a_pause_longer_than_the_idle_window_is_dropped(self):
        raw: dict = {}
        prev = None
        p5 = 10.0
        for ts, p7, rising in [(0, 96, True), (300, 96, True), (600, 97, True),
                               (900, 97, False), (1200, 97, False), (1500, 97, False),
                               (1800, 98, True), (2100, 98, True), (2400, 99, True)]:
            p5 += 1 if rising else 0
            raw = ride.observe(raw, "1", p5, p7, ts, prev, quiet_s=600.0)
            prev = ts
        # 600 -> 1800 had 900 s with no rise anywhere: dropped. 1800 -> 2400 counts.
        assert seconds(raw, 2400) == 600.0

    def test_intervals_older_than_six_hours_are_ignored(self):
        raw = feed([(0, 95), (60, 96), (360, 97), (660, 98)])
        assert seconds(raw, 660) == 300.0
        assert seconds(raw, 660 + ride.INTERVAL_MAX_AGE_S - 1) == 300.0
        assert seconds(raw, 360 + ride.INTERVAL_MAX_AGE_S + 1) == 300.0  # one left
        assert seconds(raw, 660 + ride.INTERVAL_MAX_AGE_S + 1) is None

    def test_a_7d_reset_clears_the_history(self):
        raw = feed([(0, 95), (60, 96), (360, 97), (660, 98)])
        raw = ride.observe(raw, "1", 30.0, 3.0, 960.0, 660.0)
        assert seconds(raw, 960) is None
        assert seconds(raw, 960, window="5h") is None

    def test_a_new_tenure_clears_the_history(self):
        raw = feed([(0, 95), (60, 96), (360, 97), (660, 98)])
        assert seconds(raw, 660) == 300.0
        raw = ride.observe(raw, "1", 40.0, 98.0, 5000.0, None)  # was parked
        assert seconds(raw, 5000) is None

    def test_old_unstamped_intervals_are_ignored(self):
        raw = {"1": {"7d": {"pct": 98, "stepAt": 0.0, "intervals": [9000.0, 9000.0]}}}
        assert seconds(raw, 100) is None


# -- the engine --------------------------------------------------------------------------

MARKS = {"hard5h": 95, "hard7d": 99, "forceEtaMin": 3}


class Climb:
    """Drives one active account's 5h (always climbing: never idle) and 7d
    through the engine, 10 minutes apart."""

    def __init__(self, h, peers: dict[str, dict]):
        self.h = h
        self.peers = peers
        self.p5 = 10.0

    def tick(self, active: int, p7: float, *, advance: float = 600, idle: bool = False):
        self.h.clock.advance(advance)
        if not idle:
            self.p5 += 2
        usage = {k: v for k, v in self.peers.items() if k != str(active)}
        usage[str(active)] = win(self.p5, p7)
        return self.h.tick_with_usage(usage)


def learning(h) -> dict:
    return ride.learned(h.state().get(ride.LEARN_KEY))


def test_the_reviewers_overnight_case_rides_minutes_not_hours(temp_home):
    """7d 97 at 18:00, idle all night (polled every 300 s), busy from
    08:50, 98 at 09:00, 99 at 09:20. Untouched, T1 was 7.67 h and even
    q 0.05 rode 22 min."""
    # soft7d 99 and peers at 7d 95: nothing soft-switches or rebalances
    # overnight; at 99 the hard path lands on the roomier #2.
    h = make(temp_home, maximize={**MARKS, "soft7d": 99, "rideMaxMin": 120})
    h.engine._mutate_state(lambda s: s.__setitem__(ride.LEARN_KEY, {"7d": {"q": 0.05}}))
    c = Climb(h, {"2": win(0, 95), "3": win(0, 95)})
    for p7 in (96, 96, 97):                      # 17:50 - 18:00, busy
        c.tick(1, p7, advance=300)
    for _ in range(14 * 12 + 9):                  # 18:05 - 08:45, idle
        c.tick(1, 97, advance=300, idle=True)
    for p7 in (97, 98, 98, 98, 98):              # 08:50 - 09:15, busy
        c.tick(1, p7, advance=300)
    assert h.active_number() == 1
    steps = h.state()[ride.STEPS_KEY]
    assert ride.point_seconds(steps, "1", "7d", h.clock.now) is None  # 18:00 -> 09:00 dropped
    c.tick(1, 99, advance=300)                   # 09:20
    steps = h.state()[ride.STEPS_KEY]
    assert ride.point_seconds(steps, "1", "7d", h.clock.now) == 1200.0
    decision = of(h, MaximizeDecisionEvent)[-1]
    # T1 is at most 20 min (the velocity's 10 min here), so q 0.05 rides
    # at most a minute: it is due at once.
    assert (decision.decision, decision.trigger) == ("switch", "hard")
    assert h.active_number() == 2


def test_q_rises_after_successes_and_halves_after_a_hit(temp_home):
    h = make(temp_home, n=4, maximize=MARKS)
    peers = {"1": win(5, 99), "2": win(5, 99), "3": win(5, 10), "4": win(5, 20)}
    c = Climb(h, peers)

    def ride_once(active: int, step_s: float, last: float | None) -> list[TickOutcome]:
        """7d 97 -> 98 -> 99 with ``step_s`` per point, then ticks every 60 s
        until it switches (``last``: the 7d of the final tick, e.g. 100)."""
        out = [c.tick(active, 97, advance=step_s), c.tick(active, 98, advance=step_s),
               c.tick(active, 99, advance=step_s)]
        while out[-1] is TickOutcome.NO_ACTION and len(out) < 40:
            out.append(c.tick(active, last if last is not None else 99, advance=60))
        return out

    # Ride 1 on #1: T1 = 600 s, q 0.3 -> switch 90 s after the first 99.
    c.tick(1, 96, advance=0)
    out = ride_once(1, 600, None)
    assert out[2] is TickOutcome.NO_ACTION and out[-1] is TickOutcome.SWITCHED
    assert no_switch_reasons(h)[-1] == "ride"
    [first] = of(h, SwitchEvent)
    assert first.trigger == "hard"
    assert learning(h)["7d"]["q"] == pytest.approx(0.35)
    assert learning(h)["7d"]["n_ok"] == 1
    landed = h.active_number()
    assert landed == 3

    # Ride 2 on #3: its own steps (T1 900 s), q 0.35 -> another success.
    c.peers["1"] = win(5, 99)
    c.tick(landed, 96, advance=60)
    out = ride_once(landed, 900, None)
    assert out[-1] is TickOutcome.SWITCHED
    assert learning(h)["7d"]["q"] == pytest.approx(0.40)
    assert learning(h)["7d"]["n_ok"] == 2
    landed = h.active_number()
    assert landed == 4

    # Ride 3 on #4: T1 1200 s, q 0.4 -> 390 s of ride, but 100% shows up
    # 60 s in: a hit halves q, and the at-limit switch moves on at once.
    c.tick(landed, 96, advance=60)
    out = ride_once(landed, 1200, 100)
    assert out[-1] is TickOutcome.SWITCHED
    assert [e.trigger for e in of(h, SwitchEvent)][-1] == "at-limit"
    data = learning(h)["7d"]
    assert (data["q"], data["n_ok"], data["n_hit"]) == (pytest.approx(0.2), 2, 1)
    assert learning(h)["5h"]["q"] == 0.3  # the 5h never rode


def test_the_ride_is_published_with_its_switch_time(temp_home):
    h = make(temp_home, maximize=MARKS)
    peers = {"2": win(0, 10), "3": win(0, 50)}
    c = Climb(h, peers)
    for p7 in (96, 97, 98, 99):
        c.tick(1, p7)
    record = h.state()[DECISION_KEY]
    assert record["code"] == "ride" and record["decision"] == "hold"
    assert record["rideUntil"] == pytest.approx(h.clock.now + 0.3 * 600 - 90)
    assert h.state()[RIDE_KEY]["riding"] == ["7d"]
    [event] = of(h, MaximizeDecisionEvent)[-1:]
    assert event.ride_until == pytest.approx(record["rideUntil"])
    # The ride polls the active account at the urgent 60 s cadence (its
    # switch is within 15 minutes).
    entry = h.switcher._usage_store.entries({"1": (EMAILS[1], "")})["1"]
    assert entry.next_poll_at == pytest.approx(h.clock.now + 60)


def test_an_idle_moment_switches_during_the_ride_without_learning(temp_home):
    h = make(temp_home, maximize={**MARKS, "rideMaxMin": 120})
    c = Climb(h, {"2": win(0, 10), "3": win(0, 50)})
    for p7 in (96, 97, 98):
        c.tick(1, p7, advance=1200)                     # T1 = 1200 s
    # q 0.9 (learned before): a 1200 x 0.9 - 90 = 990 s ride.
    h.engine._mutate_state(lambda s: s.__setitem__(ride.LEARN_KEY, {"7d": {"q": 0.9}}))
    assert c.tick(1, 99, advance=1200) is TickOutcome.NO_ACTION
    assert no_switch_reasons(h)[-1] == "ride"
    # The 5h stops climbing: not yet idle 300 s in ...
    assert c.tick(1, 99, advance=300, idle=True) is TickOutcome.NO_ACTION
    assert no_switch_reasons(h)[-1] == "ride"
    # ... idle over the 10-minute window 600 s in, well before the 990 s:
    # the hard switch happens at once, and a ride an idle moment ended
    # teaches nothing.
    assert c.tick(1, 99, advance=300, idle=True) is TickOutcome.SWITCHED
    [switch] = of(h, SwitchEvent)
    assert switch.trigger == "hard"
    assert of(h, MaximizeDecisionEvent)[-1].reason.endswith("; idle during the learned ride")
    data = learning(h)["7d"]
    assert (data["q"], data["n_ok"], data["n_hit"]) == (pytest.approx(0.9), 0, 0)


def test_a_ride_cut_short_by_ride_max_min_teaches_nothing(temp_home):
    # q 0.9 would ride 0.9 x T1 - 90 s, minutes; rideMaxMin 1 ends it after
    # a minute. That switch says nothing about whether q was safe.
    h = make(temp_home, maximize={**MARKS, "rideMaxMin": 1})
    h.engine._mutate_state(lambda s: s.__setitem__(ride.LEARN_KEY, {"7d": {"q": 0.9}}))
    c = Climb(h, {"2": win(0, 10), "3": win(0, 50)})
    for p7 in (96, 97, 98):
        c.tick(1, p7)
    assert c.tick(1, 99, advance=300) is TickOutcome.NO_ACTION
    assert "(capped)" in of(h, MaximizeDecisionEvent)[-1].reason
    assert c.tick(1, 99, advance=60) is TickOutcome.SWITCHED
    assert of(h, MaximizeDecisionEvent)[-1].reason.endswith("capped by rideMaxMin")
    data = learning(h)["7d"]
    assert (data["q"], data["n_ok"], data["n_hit"]) == (pytest.approx(0.9), 0, 0)


def test_dry_runs_never_learn_or_write(temp_home):
    h = make(temp_home, maximize=MARKS)
    h.engine.dry_run = True
    c = Climb(h, {"2": win(0, 10), "3": win(0, 50)})
    for p7 in (96, 97, 98, 99):
        c.tick(1, p7)
    assert no_switch_reasons(h)[-1] == "ride"
    c.tick(1, 100, advance=60)                   # a hit, were it live
    state = h.state()
    assert ride.LEARN_KEY not in state and RIDE_KEY not in state
    assert ride.STEPS_KEY not in state
    assert runtime_for(h.engine).dry_ride[RIDE_KEY]["account"] == "1"


def test_5h_switches_at_its_hard_mark_with_the_default_ride_windows(temp_home):
    h = make(temp_home, maximize={**MARKS, "hard5h": 99})
    outcome = h.tick_with_usage({"1": win(99, 40), "2": win(0, 10), "3": win(0, 50)})
    assert outcome is TickOutcome.SWITCHED
    assert [e.trigger for e in of(h, SwitchEvent)] == ["hard"]
    assert not of(h, NoSwitchEvent)


def test_auto_off_never_counts_a_ride(temp_home):
    from claude_swap.maximize import pause

    h = make(temp_home, maximize=MARKS)
    c = Climb(h, {"2": win(0, 10), "3": win(0, 50)})
    for p7 in (96, 97, 98):
        c.tick(1, p7)
    pause.set_auto_off(h.switcher.backup_dir, True, by="test", now=h.clock.now)
    c.tick(1, 99)
    assert h.state()[RIDE_KEY]["riding"] == []      # nothing acts on it
    c.tick(1, 100, advance=60)
    assert learning(h)["7d"]["n_hit"] == 0
    assert h.active_number() == 1
