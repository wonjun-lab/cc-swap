"""The learned ride in ``policy.decide`` (maximize/policy.py). No I/O.

The marks are the user's: hard5h 95 (5h never rides), hard7d 99 (7d rides
its last point), forceEtaMin 3.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from claude_swap.maximize import policy
from claude_swap.maximize.model import Hold, RideFiveH, Snapshot, Switch
from claude_swap.maximize.policy import RIDE_MARGIN_S, decide
from tests.maximize.test_policy import NOW, acct, resets, rows, snap

MARKS = {"hard_5h": 95.0, "hard_7d": 99.0, "force_eta_min": 3}
# The active account's 5h climbs 3 points over 10 minutes (busy, never
# idle, nowhere near 95); its 7d sits at 99.
BUSY = rows((600, 37, 99), (300, 38.5, 99), (0, 40, 99))
IDLE = rows((600, 40, 99), (300, 40, 99), (0, 40, 99))


def ride_snap(
    p5: float = 40,
    p7: float = 99,
    *,
    samples=BUSY,
    armed: float | None = 0.0,
    point_s: float | None = 1800.0,
    q: float | None = None,
    window: str = "7d",
    target=None,
    **settings,
) -> Snapshot:
    """#1 active at ``p5``/``p7``, #2 a ready landing target. ``armed`` is
    seconds before NOW the engine first saw ``window`` at its mark (None:
    not passed); ``point_s`` its T1 (None: not passed)."""
    s = snap(
        "1", acct("1", p5, p7), target or acct("2", 0, 10),
        samples=samples, **{**MARKS, **settings},
    )
    return replace(
        s,
        ride_armed_at={} if armed is None else {window: NOW - armed},
        ride_point_s={} if point_s is None else {window: point_s},
        ride_q={} if q is None else {window: q},
    )


class TestArming:
    def test_7d_at_99_rides_instead_of_switching(self):
        d = decide(ride_snap())
        assert isinstance(d, Hold) and d.code == "ride" and not d.pending
        assert d.ride_windows == ("7d",)
        # first seen now + 0.6 x 30 min - 90 s (q starts at 0.6)
        assert d.ride_until == pytest.approx(NOW + 0.6 * 1800 - RIDE_MARGIN_S)
        assert d.reason == (
            "1 7d 99% — riding to the limit, switching in ~16m (learned) "
            "or at your next pause"
        )

    def test_the_switch_time_counts_from_the_first_reading_at_the_mark(self):
        d = decide(ride_snap(armed=400, q=0.5))
        assert d.ride_until == pytest.approx(NOW - 400 + 0.5 * 1800 - RIDE_MARGIN_S)

    def test_without_an_engine_record_the_samples_say_when_it_first_read_99(self):
        # The 98 -> 99 step is older than the 10-minute velocity window, so
        # only the engine's T1 (40 min) is known.
        samples = rows((900, 36, 98), (600, 37, 99), (300, 38.5, 99), (0, 40, 99))
        d = decide(ride_snap(samples=samples, armed=None, point_s=2400))
        # 99.0 was crossed after the 98 reading, perhaps right after it.
        assert d.ride_until == pytest.approx(NOW - 900 + 0.6 * 2400 - RIDE_MARGIN_S)

    def test_with_no_reading_below_the_mark_it_arms_a_slow_poll_back(self):
        from claude_swap.maximize import ride

        samples = rows((300, 38.5, 99), (0, 40, 99))
        d = decide(ride_snap(samples=samples, armed=None, point_s=2400))
        assert d.ride_until == pytest.approx(
            NOW - 300 - ride.ARM_UNKNOWN_GAP_S + 0.6 * 2400 - RIDE_MARGIN_S
        )
        d = decide(ride_snap(samples="none", armed=None, point_s=2400))
        assert d.ride_until == pytest.approx(
            NOW - ride.ARM_UNKNOWN_GAP_S + 0.6 * 2400 - RIDE_MARGIN_S
        )

    def test_q_is_clamped(self):
        assert decide(ride_snap(q=5.0)).ride_until == pytest.approx(
            NOW + 0.95 * 1800 - RIDE_MARGIN_S
        )
        assert decide(ride_snap(q=0.0)).ride_until == pytest.approx(
            NOW + 0.3 * 1800 - RIDE_MARGIN_S
        )
        # 0.3 x 300 s = 90 s, all margin: the ride is over before it starts.
        d = decide(ride_snap(q=0.0, point_s=300))
        assert isinstance(d, Switch) and d.ride == "due"


class TestEnd:
    def test_at_the_switch_time_it_is_the_hard_switch(self):
        d = decide(ride_snap(armed=0.6 * 1800 - RIDE_MARGIN_S))
        assert isinstance(d, Switch)
        assert (d.target, d.trigger, d.ride, d.ride_windows) == ("2", "hard", "due", ("7d",))
        assert d.reason.endswith("; learned ride over")

    def test_an_idle_moment_switches_at_once(self):
        d = decide(ride_snap(p5=40, samples=IDLE))
        assert isinstance(d, Switch)
        assert (d.target, d.trigger, d.ride) == ("2", "hard", "idle")
        assert d.reason.endswith("; idle during the learned ride")

    def test_100_is_the_at_limit_switch(self):
        d = decide(ride_snap(p7=100))
        assert isinstance(d, Switch) and d.trigger == "at-limit" and d.ride is None

    def test_ride_max_min_caps_it(self):
        d = decide(ride_snap(point_s=7200, ride_max_min=5))
        assert d.code == "ride" and d.ride_until == pytest.approx(NOW + 300)
        assert "(capped)" in d.reason
        due = decide(ride_snap(armed=300, point_s=7200, ride_max_min=5))
        assert (due.ride, due.ride_capped) == ("due", True)
        assert due.reason.endswith("; learned ride capped by rideMaxMin")

    def test_a_ride_that_ran_its_learned_course_is_not_capped(self):
        d = decide(ride_snap(armed=0.6 * 1800 - RIDE_MARGIN_S))
        assert (d.ride, d.ride_capped) == ("due", False)

    def test_ride_max_min_zero_never_rides(self):
        d = decide(ride_snap(ride_max_min=0))
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None


class TestNoRide:
    def test_unknown_pace_switches_at_once(self):
        d = decide(ride_snap(samples="none", point_s=None))
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None
        assert d.reason.endswith("; no ride (pace unknown)")

    def test_a_flat_7d_is_no_pace_either(self):
        # BUSY: the 7d did not move in the last 10 minutes.
        d = decide(ride_snap(point_s=None))
        assert isinstance(d, Switch) and d.trigger == "hard"

    def test_the_recent_velocity_stands_in_for_the_measured_steps(self):
        # 98 -> 99 over 10 minutes: 0.1 pt/min, so T1 = 600 s.
        samples = rows((600, 37, 98), (300, 38.5, 98), (0, 40, 99))
        d = decide(ride_snap(samples=samples, point_s=None, armed=0))
        assert d.code == "ride"
        assert d.ride_until == pytest.approx(NOW + 0.6 * 600 - RIDE_MARGIN_S)

    def test_the_timed_steps_win_over_one_step_in_the_idle_window(self):
        # 98 -> 99 over 10 minutes reads as 600 s a point whatever the pace
        # (the step may have been 0.01 or 1.99 points): the engine's steps,
        # timed across the busy stretch, say 30 min and decide.
        samples = rows((600, 37, 98), (300, 38.5, 98), (0, 40, 99))
        d = decide(ride_snap(samples=samples, point_s=1800, armed=0))
        assert d.ride_until == pytest.approx(NOW + 0.6 * 1800 - RIDE_MARGIN_S)

    def test_a_burst_in_the_idle_window_wins_over_the_steps(self):
        # 96 -> 99 over 10 minutes: 200 s a point, a burst against the
        # steps' 30 min. A T1 too long rides into 100%.
        samples = rows((600, 37, 96), (300, 38.5, 97.0), (0, 40, 99))
        d = decide(ride_snap(samples=samples, point_s=1800, armed=0))
        assert d.ride_until == pytest.approx(NOW + 0.6 * 200 - RIDE_MARGIN_S)
        # ... but never a slower one.
        d = decide(ride_snap(samples=samples, point_s=180, armed=0))
        assert d.ride_until == pytest.approx(NOW + 0.6 * 180 - RIDE_MARGIN_S)

    def test_a_recent_429_switches_at_hard(self):
        s = replace(ride_snap(), active_recent_429=True)
        d = decide(s)
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None
        assert "recent 429" in d.reason

    def test_learned_ride_off_switches_at_hard(self):
        d = decide(ride_snap(learned_ride=False))
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None
        assert d.reason == "1 7d 99% >= hard 99%; -> 2 (normal, score 2.10)"

    def test_a_hard_mark_under_99_never_rides(self):
        d = decide(ride_snap(hard_7d=98.0))
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None

    def test_nowhere_roomier_is_hard_stay_not_a_ride(self):
        d = decide(ride_snap(target=acct("2", 97, 99.5)))
        assert isinstance(d, Hold) and d.code == "hard-stay"


class TestWindows:
    def test_5h_switches_at_its_hard_mark_by_default(self):
        d = decide(ride_snap(p5=96, p7=40, window="5h"))
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None

    def test_5h_at_99_does_not_ride_unless_listed(self):
        kw = dict(p5=99, p7=40, window="5h", hard_5h=99.0,
                  samples=rows((600, 97.5, 40), (300, 98, 40), (0, 99, 40)))
        d = decide(ride_snap(**kw))
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None
        assert decide(ride_snap(**kw, ride_windows="5h,7d")).code == "ride"
        assert decide(ride_snap(**kw, ride_windows="5h")).code == "ride"

    def test_no_window_rides_with_an_empty_list(self):
        d = decide(ride_snap(ride_windows=""))
        assert isinstance(d, Switch) and d.ride is None

    def test_a_5h_crossing_during_a_7d_ride_switches_at_once(self):
        d = decide(ride_snap(p5=96))
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None
        assert d.reason.startswith("1 5h 96% >= hard 95%")

    def test_a_5h_about_to_force_ends_the_ride(self):
        # 5h 93% climbing 1 pt/min: 95% in ~2 min, within forceEtaMin 3.
        samples = rows((600, 83, 99), (300, 88, 99), (0, 93, 99))
        d = decide(ride_snap(p5=93, samples=samples))
        assert isinstance(d, Switch) and d.trigger == "hard" and d.ride is None

    def test_ride_windows_reads_the_setting(self):
        from claude_swap.settings import MaximizeSettings

        assert policy.ride_windows(MaximizeSettings()) == ("7d",)
        assert policy.ride_windows(MaximizeSettings(ride_windows="5h,7d")) == ("5h", "7d")
        assert policy.ride_windows(MaximizeSettings(ride_windows="")) == ()
        assert policy.ride_windows(MaximizeSettings(learned_ride=False)) == ()


class TestResetAndHold:
    def test_a_reset_before_the_switch_time_is_waited_out(self):
        s = ride_snap(point_s=3600)
        s = replace(s, accounts=(resets(s.accounts[0], m7=5), *s.accounts[1:]))
        d = decide(s)
        assert isinstance(d, Hold) and d.code == "reset-wait"
        assert d.reset_wait_until == pytest.approx(NOW + 300)
        assert d.reason == (
            "1 7d 99% — resets in 5m, waiting it out (switches at once if it hits 100%)"
        )

    def test_a_reset_after_the_switch_time_does_not_stop_the_ride(self):
        s = ride_snap(point_s=1800)
        s = replace(s, accounts=(resets(s.accounts[0], m7=30), *s.accounts[1:]))
        assert decide(s).code == "ride"

    def test_an_account_hold_does_not_block_the_ride_or_its_switch(self):
        held = replace(ride_snap(), hold_until=NOW + 3600)
        d = decide(held)
        assert isinstance(d, Hold) and d.code == "ride"
        due = replace(ride_snap(armed=3600), hold_until=NOW + 3600)
        d = decide(due)
        assert isinstance(d, Switch) and d.ride == "due" and d.trigger == "hard"


# -- the 7d's last point, measured on the 5h ----------------------------------------------


def five_snap(rise: float, *, k: float | None = 0.165, t: float | None = None,
              point5_s: float | None = None, **kw) -> Snapshot:
    """``ride_snap`` with a 5h measure: ``rise`` 5h points since the arm
    time, phase 0.2 at it, the 5h's phase now 0 (``point5_s`` None), so
    the share used is ``k × (rise − 0.2)``."""
    s = ride_snap(**kw)
    return replace(
        s,
        ride_5h={"7d": RideFiveH(rise, 0.2, point5_s)},
        k7={} if k is None else {"1": k},
        ride_t={} if t is None else {"7d": t},
    )


class TestMeasuredOnThe5h:
    def test_rides_until_the_share_used_reaches_t(self):
        d = decide(five_snap(4.0))
        used = 0.165 * 3.8
        assert isinstance(d, Hold) and d.code == "ride"
        assert d.ride_by_5h == ("7d",)
        # The rest of t at T1 (30 min): the countdown and the poll cadence.
        assert d.ride_until == pytest.approx(NOW + (0.85 - used) * 1800)
        assert d.reason == (
            "1 7d 99% — riding to the limit, 0.63 of the last point used "
            "(switching at 0.85, on the 5h), switching in ~7m (learned) "
            "or at your next pause"
        )

    def test_switches_once_it_does_whatever_the_time_rule_says(self):
        # The time rule would ride 0.6 x 30 min; the 5h says 0.87 is used.
        d = decide(five_snap(5.5, armed=60))
        assert isinstance(d, Switch) and d.trigger == "hard"
        assert (d.ride, d.ride_windows, d.ride_by_5h, d.ride_capped) == (
            "due", ("7d",), ("7d",), False
        )
        assert d.reason.endswith(
            "; learned ride over (0.87 of the last point used, measured on the 5h)"
        )

    def test_the_learned_target_decides(self):
        assert isinstance(decide(five_snap(5.5, t=0.9)), Hold)
        assert isinstance(decide(five_snap(5.5, t=0.8)), Switch)
        assert isinstance(decide(five_snap(5.5, t=5.0)), Hold)    # clamped to 0.97

    def test_the_5h_phase_now_counts(self):
        # Samples a minute apart, the 5h a point a minute: its line puts
        # the 5h half a point past its last reading at NOW.
        samples = rows((180, 37, 99), (120, 38, 99), (60, 39, 99), (0, 40, 99))
        snap = five_snap(5.0, samples=samples, point5_s=60.0)
        assert policy.ride_used_5h(snap, snap.view("1"), "7d") == pytest.approx(0.165 * 5.3)

    def test_without_a_learned_k_the_time_rule_rides(self):
        d = decide(five_snap(5.5, k=None))
        assert isinstance(d, Hold) and d.ride_by_5h == ()
        assert d.ride_until == pytest.approx(NOW + 0.6 * 1800 - RIDE_MARGIN_S)

    def test_without_fresh_readings_the_time_rule_rides(self):
        stale = rows((1500, 37, 99), (1200, 40, 99))
        d = decide(five_snap(5.5, samples=stale))
        assert isinstance(d, Hold) and d.ride_by_5h == ()

    def test_the_5h_window_itself_never_rides_by_it(self):
        snap = five_snap(9.0, window="5h", hard_5h=99.0)
        assert policy.ride_used_5h(snap, snap.view("1"), "5h") is None

    def test_t1_unknown_still_rides_on_the_5h(self):
        d = decide(five_snap(4.0, point_s=None))
        assert policy.ride_point_s(five_snap(4.0, point_s=None), "7d") is None
        assert isinstance(d, Hold) and d.ride_by_5h == ("7d",)

    def test_ride_max_min_still_ends_it(self):
        d = decide(five_snap(4.0, armed=3600))
        assert isinstance(d, Switch) and d.ride == "due" and d.ride_capped
        assert d.reason.endswith("; learned ride capped by rideMaxMin")

    def test_an_idle_moment_switches_at_once(self):
        d = decide(five_snap(4.0, samples=IDLE))
        assert isinstance(d, Switch) and d.ride == "idle" and d.ride_by_5h == ("7d",)

    def test_100_switches_at_once(self):
        d = decide(five_snap(4.0, p7=100))
        assert isinstance(d, Switch) and d.trigger == "at-limit"
