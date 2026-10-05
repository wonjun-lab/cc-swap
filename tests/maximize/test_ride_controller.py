"""The learned ride's controllers in simulation (maximize/ride.py).

The time rule (q). Each ride: an account busy on its 7d from 92% to 100%, every point taking
its own time (lognormal around a pace that drifts from point to point), read
every 2 minutes (±10% jitter, as poll_policy's high-usage cadence), the
engine ticking every 60 s. The real pieces decide: ``ride.observe`` /
``ride.point_seconds`` / ``idle.point_pace`` / ``ride.point_estimate`` time
the point (T1, frozen at the arm time), ``ride.arm_time`` arms it, the ride
ends at ``arm + q × T1 − policy.RIDE_MARGIN_S`` (rideMaxMin out of the way:
a capped ride teaches nothing), and ``ride.learn`` learns from it. A hit is
the 7d crossing 100.0 before the switch; whether it is read at 100% or
refused by Claude Code first, it counts the same.

The 5h measure (t), at the end of the file: the same account and 7d, its 5h
climbing ``1/k`` times as fast in whole percents (k off from the learned
one by a few percent per ride, a 5h reset in one ride in five), read every
2 minutes. The engine's own helpers fold the readings in
(``engine_hook._arm_five_h`` / ``_fold_five_h``) and the policy's
``ride_used_5h`` reads the share of the last point used each 60 s tick; it
switches once that reaches t (``ride.learn(..., by_5h=True)`` learns it),
at ``rideMaxMin`` at the latest.

Pure and seeded: no clock, no I/O.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from claude_swap.maximize import engine_hook, idle, policy, ride
from claude_swap.maximize.model import RideFiveH, Sample, Snapshot
from claude_swap.settings import MaximizeSettings

POLL_S = 120.0          # poll_policy.ACTIVE_HIGH_USAGE_INTERVAL_S
JITTER = 0.10
TICK_S = 60.0           # the engine's default intervalSeconds
SIGMA = 0.12            # per-point duration noise (lognormal)
DRIFT = 0.03            # the pace's drift from one point to the next
K7 = 0.165              # 7d points per 5h point (drain.K_20X)
SETTINGS = MaximizeSettings()


@dataclass
class Ride:
    hit: bool
    share: float        # of the last point used before the switch (1 on a hit)
    t1: float           # the T1 the ride ran on
    true_s: float       # how long the last point really took


def _ride(rng: random.Random, q: float, *, old_t1: bool = False) -> Ride:
    pace = math.log(rng.uniform(15, 45) * 60.0)
    start7 = 92.0 + rng.random()
    durations, crossings, t = [], [], 0.0
    for p in range(93, 101):
        pace += rng.gauss(0.0, DRIFT)
        d = math.exp(pace + rng.gauss(0.0, SIGMA))
        if p == 93:
            d *= 93.0 - start7
        t += d
        durations.append(d)
        crossings.append(t)  # 7d crosses p at crossings[p - 93]

    def pct7(at: float) -> float:
        for i, c in enumerate(crossings):
            if at < c:
                before = crossings[i - 1] if i else 0.0
                base = 92.0 + i if i else start7
                return base + (at - before) / durations[i] * (93.0 + i - base)
        return 100.0

    rate5 = 1.0 / (K7 * math.exp(pace))  # 5h points a second, busy all along
    steps: dict = {}
    samples: list[Sample] = []
    at, prev = -rng.uniform(0.0, POLL_S), None
    while True:
        p7 = float(math.floor(pct7(at)))
        p5 = float(math.floor(10.0 + rate5 * max(at, 0.0)))
        steps = ride.observe(steps, "1", p5, p7, at, prev)
        samples = [*samples, Sample(at, p5, p7)][-20:]
        if p7 >= 99.0:
            break
        prev = at
        at += POLL_S * (1.0 + rng.uniform(-JITTER, JITTER))
    armed = ride.arm_time(at, prev)
    velocity_s, points = idle.point_pace(samples, SETTINGS, "7d")
    steps_s = ride.point_seconds(steps, "1", "7d", at)
    if old_t1:  # before: the shorter of the median of 3 and any velocity
        known = [x for x in (ride.point_seconds(steps, "1", "7d", at, median_of=3),
                             velocity_s) if x is not None]
        t1 = min(known)
    else:
        t1 = ride.point_estimate(steps_s, velocity_s, points)
    assert t1 is not None
    until = armed + q * t1 - policy.RIDE_MARGIN_S
    phase = at + rng.uniform(0.0, TICK_S)
    switch = max(phase, phase + math.ceil((until - phase) / TICK_S) * TICK_S)
    c99, c100 = crossings[-2], crossings[-1]
    hit = c100 < switch
    share = 1.0 if hit else max(0.0, (switch - c99) / durations[-1])
    return Ride(hit, share, t1, durations[-1])


def simulate(seed: int, rides: int, *, q0: object = None) -> tuple[list[float], list[Ride]]:
    """``(q before each ride, the rides)`` from the record ``q0``."""
    rng = random.Random(seed)
    data: object = q0
    qs, out = [], []
    for _ in range(rides):
        q = ride.q_values(data)["7d"]
        r = _ride(rng, q)
        qs.append(q)
        out.append(r)
        data = ride.learn(data, "7d", "hit" if r.hit else "ok", 0.0)
    return qs, out


BURN_IN = 300


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_q_settles_near_the_edge_with_a_bounded_hit_rate(seed):
    qs, rides = simulate(seed, BURN_IN + 1500)
    settled_q = statistics.mean(qs[BURN_IN:])
    hit_rate = sum(r.hit for r in rides[BURN_IN:]) / len(rides[BURN_IN:])
    share = statistics.mean(r.share for r in rides[BURN_IN:])
    assert 0.85 <= settled_q <= 0.92, settled_q
    assert 0.08 <= hit_rate <= 0.12, hit_rate
    # ~4/5 of the last point on average, hits included as whole points.
    assert share >= 0.78, share


def test_the_hit_rate_is_pinned_by_the_steps_whatever_the_noise():
    # Away from the cap, n_hit = (Q_UP·rides − Δq) / (Q_UP + Q_DOWN).
    assert ride.Q_UP / (ride.Q_UP + ride.Q_DOWN) == pytest.approx(0.10)
    data: object = {"7d": {"q": 0.6, "settled": True, "v": ride.LEARN_VERSION}}
    rng = random.Random(7)
    hits = 0
    for _ in range(5000):
        q = ride.q_values(data)["7d"]
        hit = rng.random() < 1.0 / (1.0 + math.exp(-(q - 0.7) / 0.03))  # edge ~0.7
        hits += hit
        data = ride.learn(data, "7d", "hit" if hit else "ok", 0.0)
    assert hits / 5000 == pytest.approx(0.10, abs=0.005)


def test_from_a_fresh_start_it_gets_there_in_a_handful_of_rides():
    reached = []
    for seed in range(1, 21):
        qs, _ = simulate(seed, 40)
        reached.append(next(i for i, q in enumerate(qs) if q >= 0.85))
    # 0.6 -> 0.85 at the slow start's +0.05: five clean rides.
    assert statistics.median(reached) <= 6


def test_a_halving_era_record_migrates_and_converges_the_same():
    old = {"7d": {"q": 0.15, "n_ok": 9, "n_hit": 4}}
    qs, rides = simulate(4, BURN_IN + 1000, q0=old)
    assert qs[0] == ride.Q_START
    assert 0.85 <= statistics.mean(qs[BURN_IN:]) <= 0.92


def test_t1_is_close_to_the_real_point_where_it_used_to_be_a_third_of_it():
    rng = random.Random(11)
    new = [_ride(rng, 0.9) for _ in range(300)]
    rng = random.Random(11)
    old = [_ride(rng, 0.9, old_t1=True) for _ in range(300)]
    ratio_new = statistics.median(r.t1 / r.true_s for r in new)
    ratio_old = statistics.median(r.t1 / r.true_s for r in old)
    assert 0.9 <= ratio_new <= 1.1, ratio_new
    # One 7d step in the 10-minute velocity span read as 10 minutes a point.
    assert ratio_old < 0.5, ratio_old
    assert statistics.mean(r.share for r in old) < 0.4


# -- the 5h measure -----------------------------------------------------------------------

K_NOISE = 0.03          # the ride's real k off from the learned one (lognormal σ)
RESET_SHARE = 0.2       # rides with a 5h reset somewhere near their end
ACCOUNT = SimpleNamespace(number="1")


@dataclass
class Ride5:
    hit: bool
    share: float        # of the last point used before the switch (1 on a hit)
    capped: bool        # rideMaxMin ended it (teaches nothing)
    reset: bool         # the 5h reset during the ride


def _ride_5h(rng: random.Random, t: float) -> Ride5:
    pace = math.log(rng.uniform(15, 45) * 60.0)
    start7 = 92.0 + rng.random()
    durations, crossings, total = [], [], 0.0
    for p in range(93, 101):
        pace += rng.gauss(0.0, DRIFT)
        d = math.exp(pace + rng.gauss(0.0, SIGMA))
        if p == 93:
            d *= 93.0 - start7
        total += d
        durations.append(d)
        crossings.append(total)

    def level7(at: float) -> float:  # the 7d's true level, past 100 too
        if at <= 0:
            return start7
        for i, c in enumerate(crossings):
            if at < c:
                before = crossings[i - 1] if i else 0.0
                base = 92.0 + i if i else start7
                return base + (at - before) / durations[i] * (93.0 + i - base)
        return 100.0 + (at - crossings[-1]) / durations[-1]

    k_true = K7 * math.exp(rng.gauss(0.0, K_NOISE))
    base5 = rng.uniform(0.0, 40.0)
    reset_at = (
        rng.uniform(crossings[-3], crossings[-1] + 600.0)
        if rng.random() < RESET_SHARE else None
    )

    def level5(at: float) -> float:
        if reset_at is not None and at >= reset_at:
            return (level7(at) - level7(reset_at)) / k_true
        return base5 + (level7(at) - start7) / k_true

    def read(at: float) -> Sample:
        return Sample(at, float(math.floor(level5(at))),
                      float(math.floor(min(level7(at), 100.0))))

    def next_poll(at: float) -> float:
        return at + POLL_S * (1.0 + rng.uniform(-JITTER, JITTER))

    steps: dict = {}
    samples: list[Sample] = []
    at, prev = -rng.uniform(0.0, POLL_S), None
    while True:
        x = read(at)
        steps = ride.observe(steps, "1", x.pct5, x.pct7, at, prev)
        samples = list(idle.trim_samples([*samples, x], at))
        if x.pct7 >= 99.0:
            break
        prev, at = at, next_poll(at)
    armed = ride.arm_time(at, prev)
    five = engine_hook._arm_five_h(steps, tuple(samples), SETTINGS, "1", armed, at, prev, at)
    assert five is not None
    engine_hook._fold_five_h(five, tuple(samples))
    base = Snapshot(
        now=at, active="1", accounts=(), samples=(), last_switch_at=None,
        settings=SETTINGS, k7={"1": K7},
    )
    cap_at = armed + SETTINGS.ride_max_min * 60.0
    poll, tick = next_poll(at), at + rng.uniform(0.0, TICK_S)
    capped = False
    while True:
        while poll <= tick:
            samples = list(idle.trim_samples([*samples, read(poll)], poll))
            poll = next_poll(poll)
        engine_hook._fold_five_h(five, tuple(samples))
        if samples[-1].pct7 >= 100.0:
            break  # read at 100%: the at-limit switch, a hit
        snap = replace(
            base, now=tick, samples=tuple(samples),
            ride_5h={"7d": RideFiveH(five["rise"], five["phase"], five["pointS"])},
        )
        used = policy.ride_used_5h(snap, ACCOUNT, "7d")
        assert used is not None
        if used >= t:
            break
        if tick >= cap_at:
            capped = True
            break
        tick += TICK_S
    hit = crossings[-1] < tick
    share = 1.0 if hit else max(0.0, (tick - crossings[-2]) / durations[-1])
    reset = reset_at is not None and armed < reset_at <= tick
    return Ride5(hit, share, capped, reset)


def simulate_5h(seed: int, rides: int) -> tuple[list[float], list[Ride5]]:
    """``(t before each ride, the rides)`` from a fresh record."""
    rng = random.Random(seed)
    data: object = None
    ts, out = [], []
    for _ in range(rides):
        t = ride.t_values(data)["7d"]
        r = _ride_5h(rng, t)
        ts.append(t)
        out.append(r)
        if not r.capped:
            data = ride.learn(data, "7d", "hit" if r.hit else "ok", 0.0, by_5h=True)
    return ts, out


BURN_IN_5H = 200


@pytest.fixture(scope="module")
def runs_5h() -> dict[int, tuple[list[float], list[Ride5]]]:
    return {seed: simulate_5h(seed, BURN_IN_5H + 800) for seed in (1, 2, 3)}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_5h_measure_uses_nine_tenths_of_the_last_point_at_a_tenth_hits(runs_5h, seed):
    ts, rides = runs_5h[seed]
    settled = rides[BURN_IN_5H:]
    hit_rate = sum(r.hit for r in settled) / len(settled)
    share = statistics.mean(r.share for r in settled)
    assert 0.08 <= hit_rate <= 0.12, hit_rate
    assert share >= 0.9, share
    # t settles well inside its range, nowhere near the cap.
    assert 0.8 <= statistics.mean(ts[BURN_IN_5H:]) <= 0.95
    # rideMaxMin (60) is a backstop: the slowest points (45 min) fit in it.
    assert sum(r.capped for r in settled) <= len(settled) * 0.01


def test_a_5h_reset_mid_ride_is_summed_across(runs_5h):
    rides = [r for _, rs in runs_5h.values() for r in rs[BURN_IN_5H:]]
    across = [r for r in rides if r.reset]
    assert len(across) > 100
    # Not a hit factory: the old window's points and its phase at the
    # reset are carried into the new one.
    assert sum(r.hit for r in across) / len(across) <= 0.16
    assert statistics.mean(r.share for r in across) >= 0.88


def test_the_5h_measure_beats_the_time_rule(runs_5h):
    _, timed = simulate(1, BURN_IN + 800)
    by_time = statistics.mean(r.share for r in timed[BURN_IN:])
    by_5h = statistics.mean(r.share for _, rs in runs_5h.values() for r in rs[BURN_IN_5H:])
    assert by_5h >= by_time + 0.05, (by_time, by_5h)
