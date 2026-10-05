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
    """Drives one active account's 5h (climbing ``step5`` a tick unless
    ``idle``: busy) and 7d through the engine."""

    def __init__(self, h, peers: dict[str, dict], *, step5: float = 2.0):
        self.h = h
        self.peers = peers
        self.p5 = 10.0
        self.step5 = step5

    def tick(self, active: int, p7: float, *, advance: float = 600, idle: bool = False):
        self.h.clock.advance(advance)
        if not idle:
            self.p5 += self.step5
        usage = {k: v for k, v in self.peers.items() if k != str(active)}
        usage[str(active)] = win(self.p5, p7)
        return self.h.tick_with_usage(usage)


#: Near the limit the planner polls a moving active account every 60 s.
URGENT = 60.0


def approach(c: Climb, active: int, step_s: float = 600.0, *, start: int = 96,
             quiet_last: int = 0) -> TickOutcome:
    """The 7d from ``start`` to 99, one point every ``step_s``, read every
    60 s; the outcome of the first 99 reading. The 5h stops climbing for the
    last ``quiet_last`` readings."""
    readings = [p for p in range(start, 99) for _ in range(int(step_s // URGENT))] + [99]
    out = None
    for i, p7 in enumerate(readings):
        out = c.tick(active, p7, advance=URGENT, idle=i >= len(readings) - quiet_last)
    return out


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


def busy(h, peers, **kw) -> Climb:
    """A 5h climbing a quarter point a minute: busy, far from its marks."""
    return Climb(h, peers, step5=0.25, **kw)


def set_q(h, q: float) -> None:
    h.engine._mutate_state(lambda s: s.__setitem__(ride.LEARN_KEY, {"7d": {"q": q}}))


def test_q_rises_after_successes_and_halves_after_a_hit(temp_home):
    h = make(temp_home, n=4, maximize=MARKS)
    peers = {"1": win(5, 99), "2": win(5, 99), "3": win(5, 10), "4": win(5, 20)}
    c = busy(h, peers)

    def ride_once(active: int, last: float = 99) -> list[TickOutcome]:
        """Up to 99 (T1 600 s: the steps and the velocity agree), then a
        reading every 60 s (``last``: its 7d, e.g. 100) until it switches."""
        out = [approach(c, active)]
        while out[-1] is TickOutcome.NO_ACTION and len(out) < 20:
            out.append(c.tick(active, last, advance=URGENT))
        return out

    # Ride 1 on #1: armed at the last 98 (60 s back), q 0.3: the switch is
    # due 0.3 x 600 - 90 = 90 s after it, at the next reading.
    out = ride_once(1)
    assert out == [TickOutcome.NO_ACTION, TickOutcome.SWITCHED]
    assert no_switch_reasons(h)[-1] == "ride"
    [first] = of(h, SwitchEvent)
    assert first.trigger == "hard"
    assert learning(h)["7d"]["q"] == pytest.approx(0.35)
    assert learning(h)["7d"]["n_ok"] == 1
    landed = h.active_number()
    assert landed == 3

    # Ride 2 on #3: its own steps from its own tenure, q 0.35 -> another success.
    out = ride_once(landed)
    assert out[-1] is TickOutcome.SWITCHED
    assert learning(h)["7d"]["q"] == pytest.approx(0.40)
    assert learning(h)["7d"]["n_ok"] == 2
    landed = h.active_number()
    assert landed == 4

    # Ride 3 on #4: q 0.4 -> due 150 s after the last 98, but 100% shows
    # up at the next reading: a hit halves q, and at-limit moves on at once.
    out = ride_once(landed, last=100)
    assert out == [TickOutcome.NO_ACTION, TickOutcome.SWITCHED]
    assert [e.trigger for e in of(h, SwitchEvent)][-1] == "at-limit"
    data = learning(h)["7d"]
    assert (data["q"], data["n_ok"], data["n_hit"]) == (pytest.approx(0.2), 2, 1)
    assert learning(h)["5h"]["q"] == 0.3  # the 5h never rode


def test_the_ride_is_published_with_its_switch_time(temp_home):
    h = make(temp_home, maximize=MARKS)
    set_q(h, 0.9)
    c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
    assert approach(c, 1) is TickOutcome.NO_ACTION
    record = h.state()[DECISION_KEY]
    assert record["code"] == "ride" and record["decision"] == "hold"
    armed_at = h.clock.now - URGENT                  # the last 98 reading
    assert record["rideUntil"] == pytest.approx(armed_at + 0.9 * 600 - 90)
    assert h.state()[RIDE_KEY]["riding"] == ["7d"]
    [event] = of(h, MaximizeDecisionEvent)[-1:]
    assert event.ride_until == pytest.approx(record["rideUntil"])
    # The ride polls the active account at the 120 s high-usage cadence
    # (its switch is within 15 minutes).
    entry = h.switcher._usage_store.entries({"1": (EMAILS[1], "")})["1"]
    assert entry.next_poll_at == pytest.approx(h.clock.now + 120)


def armed(h, window: str = "7d", account: str = "1") -> dict:
    return h.state()[RIDE_KEY]["accounts"][account][window]


class TestArmTime:
    """The 99 is first read up to a poll after the window crossed 99.0: the
    ride counts from the reading before it (conservative)."""

    def test_armed_at_the_last_reading_below_the_mark(self, temp_home):
        h = make(temp_home, maximize=MARKS)
        c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
        approach(c, 1)
        assert armed(h)["at"] == pytest.approx(h.clock.now - URGENT)
        assert armed(h)["pointS"] == pytest.approx(600.0)

    def test_after_a_slow_poll_the_whole_gap_comes_off_the_ride(self, temp_home):
        # The 98 was read 300 s before the 99: 99.0 may have been crossed
        # right after it. q 0.3 x 600 - 90 = 90 s < 300 s: due at once.
        h = make(temp_home, maximize=MARKS)
        c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
        for p7 in [97] * 10 + [98] * 5:
            c.tick(1, p7, advance=URGENT)
        assert c.tick(1, 99, advance=300) is TickOutcome.SWITCHED
        assert of(h, MaximizeDecisionEvent)[-1].reason.endswith("; learned ride over")

    def test_with_no_earlier_reading_it_is_armed_a_slow_poll_back(self, temp_home):
        h = make(temp_home, maximize=MARKS)
        h.tick_with_usage({"1": win(40, 99), "2": win(0, 10), "3": win(0, 50)})
        assert armed(h)["at"] == pytest.approx(h.clock.now - ride.ARM_UNKNOWN_GAP_S)


def seed_armed(h, at: float, *, account: str = "1", point_s: float = 600.0) -> None:
    """A ``maximizeRide`` record arming account ``account``'s 7d at ``at``."""
    record = {"account": account, "riding": [],
              "accounts": {account: {"7d": {"at": at, "pointS": point_s}}}}
    h.engine._mutate_state(lambda s: s.__setitem__(RIDE_KEY, record))


class TestAwayAndBack:
    """Each account keeps its own arm time: a switch away mid-ride and back
    to the same 99 window does not start the ride over."""

    @staticmethod
    def setup(temp_home):
        h = make(temp_home, maximize={**MARKS, "rideMaxMin": 120})
        set_q(h, 0.9)                                  # a 450 s ride
        c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
        assert approach(c, 1) is TickOutcome.NO_ACTION
        return h, c, armed(h)["at"]

    def test_a_return_to_the_same_99_keeps_the_arm_time(self, temp_home):
        h, c, first = self.setup(temp_home)
        h.make_live(EMAILS[2], 2)                      # a manual switch away
        c.peers["1"] = win(30, 99)
        c.tick(2, 10, advance=URGENT)
        assert h.state()[RIDE_KEY]["account"] == "2"
        assert h.state()[RIDE_KEY]["riding"] == []
        assert armed(h)["at"] == first                 # #1's is kept
        h.make_live(EMAILS[1], 1)                      # ... and back
        c.peers["2"] = win(30, 10)
        assert c.tick(1, 99, advance=URGENT) is TickOutcome.NO_ACTION
        assert armed(h)["at"] == first
        decision = of(h, MaximizeDecisionEvent)[-1]
        assert decision.ride_until == pytest.approx(first + 0.9 * 600 - 90)

    def test_a_reset_while_away_drops_it(self, temp_home):
        h, c, first = self.setup(temp_home)
        h.make_live(EMAILS[2], 2)
        c.peers["1"] = win(30, 3)                      # #1's 7d reset meanwhile
        c.tick(2, 10, advance=URGENT)
        assert "1" not in h.state()[RIDE_KEY]["accounts"]
        h.make_live(EMAILS[1], 1)
        c.tick(1, 99, advance=URGENT)                  # at 99 again: a new ride
        assert armed(h)["at"] == pytest.approx(h.clock.now - ride.ARM_UNKNOWN_GAP_S)

    def test_a_reset_time_passed_drops_it_without_a_reading(self, temp_home):
        h, c, first = self.setup(temp_home)
        seeded = h.state()[RIDE_KEY]
        seeded["accounts"]["1"]["7d"]["reset"] = h.clock.now + 60
        h.engine._mutate_state(lambda s: s.__setitem__(RIDE_KEY, seeded))
        h.make_live(EMAILS[2], 2)
        assert "1" not in c.peers                      # #1 unread this tick
        c.tick(2, 10, advance=120)
        assert "1" not in h.state()[RIDE_KEY]["accounts"]


def test_an_arm_time_in_the_future_is_pulled_back_and_kept(temp_home):
    # The clock stepped back an hour after the window was armed. Clamping
    # only at decision time would count the ride from "now" on every tick:
    # it would slide forward forever.
    h = make(temp_home, maximize=MARKS)
    seed_armed(h, h.clock.now + 3600)
    c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
    assert c.tick(1, 99, advance=URGENT) is TickOutcome.NO_ACTION   # due in 90 s
    pulled = h.clock.now
    assert armed(h)["at"] == pytest.approx(pulled)
    assert c.tick(1, 99, advance=URGENT) is TickOutcome.NO_ACTION
    assert armed(h)["at"] == pytest.approx(pulled)
    assert c.tick(1, 99, advance=URGENT) is TickOutcome.SWITCHED


def test_an_idle_moment_switches_during_the_ride_without_learning(temp_home):
    h = make(temp_home, maximize={**MARKS, "rideMaxMin": 120})
    set_q(h, 0.9)                                     # a 0.9 x 600 - 90 = 450 s ride
    c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
    # The 5h stops climbing 5 readings before the 99: not idle yet at the 99 ...
    assert approach(c, 1, quiet_last=5) is TickOutcome.NO_ACTION
    assert no_switch_reasons(h)[-1] == "ride"
    # ... idle over the 10-minute window a minute later, well inside the
    # ride: the hard switch happens at once, and teaches nothing.
    assert c.tick(1, 99, advance=URGENT, idle=True) is TickOutcome.SWITCHED
    [switch] = of(h, SwitchEvent)
    assert switch.trigger == "hard"
    assert of(h, MaximizeDecisionEvent)[-1].reason.endswith("; idle during the learned ride")
    data = learning(h)["7d"]
    assert (data["q"], data["n_ok"], data["n_hit"]) == (pytest.approx(0.9), 0, 0)


def test_a_ride_cut_short_by_ride_max_min_teaches_nothing(temp_home):
    # q 0.9 would ride 0.9 x 600 - 90 = 450 s; rideMaxMin 3 ends it 180 s
    # after the arm time. That switch says nothing about whether q was safe.
    h = make(temp_home, maximize={**MARKS, "rideMaxMin": 3})
    set_q(h, 0.9)
    c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
    assert approach(c, 1) is TickOutcome.NO_ACTION
    assert "(capped)" in of(h, MaximizeDecisionEvent)[-1].reason
    assert c.tick(1, 99, advance=URGENT) is TickOutcome.NO_ACTION
    assert c.tick(1, 99, advance=URGENT) is TickOutcome.SWITCHED
    assert of(h, MaximizeDecisionEvent)[-1].reason.endswith("capped by rideMaxMin")
    data = learning(h)["7d"]
    assert (data["q"], data["n_ok"], data["n_hit"]) == (pytest.approx(0.9), 0, 0)


def test_dry_runs_never_learn_or_write(temp_home):
    h = make(temp_home, maximize=MARKS)
    h.engine.dry_run = True
    c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
    assert approach(c, 1) is TickOutcome.NO_ACTION
    assert no_switch_reasons(h)[-1] == "ride"
    c.tick(1, 100, advance=URGENT)               # a hit, were it live
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
    c = busy(h, {"2": win(0, 10), "3": win(0, 50)})
    for p7 in [97] * 10 + [98] * 10:
        c.tick(1, p7, advance=URGENT)
    pause.set_auto_off(h.switcher.backup_dir, True, by="test", now=h.clock.now)
    c.tick(1, 99, advance=URGENT)
    assert h.state()[RIDE_KEY]["riding"] == []      # nothing acts on it
    c.tick(1, 100, advance=URGENT)
    assert learning(h)["7d"]["n_hit"] == 0
    assert h.active_number() == 1
