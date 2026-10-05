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
ACCOUNT = SimpleNamespace(number="1", plan=None)
#: The 5h's ``resets_at`` when no reset falls in the ride: far ahead.
NO_RESET = 1e9


@dataclass
class Ride5:
    hit: bool
    share: float        # of the last point used before the switch (1 on a hit)
    capped: bool        # rideMaxMin ended it (teaches nothing)
    reset: bool         # the 5h reset during the ride


def _ride_5h(
    rng: random.Random,
    t: float,
    *,
    k_learned: float = K7,
    pre_gap: float | None = None,
    pace_min: tuple[float, float] = (15, 45),
) -> Ride5:
    """One 7d ride measured on the 5h at target ``t``. The ride's real k is
    :data:`K7` off by :data:`K_NOISE`; the policy reads ``k_learned`` (an
    account's persistent bias when it is not K7). ``pre_gap``: no reading
    for that long before the first one at 99 (a sleep, a restart), the 7d
    crossing 99 uniformly within it."""
    pace = math.log(rng.uniform(*pace_min) * 60.0)
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

    def resets_at(at: float) -> float:  # the 5h's, as a reading at ``at`` says
        if reset_at is None:
            return NO_RESET
        return reset_at if at < reset_at else reset_at + 5 * 3600.0

    def read(at: float) -> Sample:
        return Sample(at, float(math.floor(level5(at))),
                      float(math.floor(min(level7(at), 100.0))))

    def next_poll(at: float) -> float:
        return at + POLL_S * (1.0 + rng.uniform(-JITTER, JITTER))

    steps: dict = {}
    samples: list[Sample] = []
    gap_from = crossings[-2] - rng.uniform(0.0, pre_gap) if pre_gap else None
    at, prev = -rng.uniform(0.0, POLL_S), None
    while True:
        x = read(at)
        steps = ride.observe(steps, "1", x.pct5, x.pct7, at, prev)
        samples = list(idle.trim_samples([*samples, x], at))
        if x.pct7 >= 99.0:
            break
        nxt = next_poll(at)
        if gap_from is not None and nxt >= gap_from:
            # The last reading below 99 at ``gap_from``, then nothing for
            # ``pre_gap``.
            nxt = gap_from if gap_from > at else at + pre_gap
        prev, at = at, nxt
    if samples[-1].pct7 >= 100.0:
        return Ride5(True, 1.0, False, False)  # the gap ran past 100: no ride
    armed = ride.arm_time(at, prev)
    five = engine_hook._arm_five_h(
        steps, tuple(samples), SETTINGS, "1", armed, at, prev, at, resets_at(at)
    )
    assert five is not None
    engine_hook._fold_five_h(five, tuple(samples), resets_at(at))
    base = Snapshot(
        now=at, active="1", accounts=(), samples=(), last_switch_at=None,
        settings=SETTINGS, ride_k7={"1": k_learned},
    )
    cap_at = armed + SETTINGS.ride_max_min * 60.0
    poll, tick = next_poll(at), at + rng.uniform(0.0, TICK_S)
    capped = False
    while True:
        while poll <= tick:
            samples = list(idle.trim_samples([*samples, read(poll)], poll))
            poll = next_poll(poll)
        engine_hook._fold_five_h(five, tuple(samples), resets_at(samples[-1].ts))
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


# -- a persistent k bias per account (R2) -------------------------------------------------


def simulate_accounts(
    seed: int, biases: tuple[float, ...], rides_each: int, *, per_account: bool = True
) -> tuple[dict[str, list[float]], dict[str, list[Ride5]]]:
    """Accounts taking turns, each riding on a learned k off from its real
    one by its own persistent ``biases[i]`` (a median of a few quantized
    windows, or one that still mixes an old plan in), with the usual ±3%
    per ride on top. ``per_account``: each rides by its own t
    (``ride.t_values(data, slot)``), else all by the window's one."""
    rng = random.Random(seed)
    data: object = None
    ts: dict[str, list[float]] = {str(i + 1): [] for i in range(len(biases))}
    out: dict[str, list[Ride5]] = {n: [] for n in ts}
    for _ in range(rides_each):
        for i, bias in enumerate(biases):
            number = str(i + 1)
            slot = number if per_account else None
            t = ride.t_values(data, slot)["7d"]
            r = _ride_5h(rng, t, k_learned=K7 * (1.0 + bias))
            ts[number].append(t)
            out[number].append(r)
            if not r.capped:
                data = ride.learn(
                    data, "7d", "hit" if r.hit else "ok", 0.0, by_5h=True, account=slot
                )
    return ts, out


BIASES = [(-0.08, 0.05), (-0.08, 0.0, 0.06)]


@pytest.fixture(scope="module")
def runs_accounts() -> dict[tuple[float, ...], tuple[dict, dict]]:
    return {b: simulate_accounts(5, b, BURN_IN_5H + 500) for b in BIASES}


@pytest.mark.parametrize("biases", BIASES)
def test_each_accounts_own_t_absorbs_its_k_bias(runs_accounts, biases):
    ts, rides = runs_accounts[biases]
    for number, bias in zip(sorted(rides), biases):
        settled = rides[number][BURN_IN_5H:]
        hit_rate = sum(r.hit for r in settled) / len(settled)
        share = statistics.mean(r.share for r in settled)
        assert hit_rate <= 0.15, (number, bias, hit_rate)
        assert share >= 0.88, (number, bias, share)
    # An account whose k reads low (used under-read) settles at a lower t,
    # one whose k reads high at a higher one.
    low, high = sorted(rides)[0], sorted(rides)[-1]
    assert statistics.mean(ts[low][BURN_IN_5H:]) < statistics.mean(ts[high][BURN_IN_5H:]) - 0.05


def test_one_shared_t_lets_the_account_whose_k_reads_low_hit_far_more():
    # Why t is per account: the shared t settles on the mix, and the account
    # whose k is 8% low switches 8% late.
    _, rides = simulate_accounts(5, BIASES[1], BURN_IN_5H + 300, per_account=False)
    settled = rides["1"][BURN_IN_5H:]
    assert sum(r.hit for r in settled) / len(settled) > 0.15


def test_an_account_with_nothing_of_its_own_rides_by_the_window():
    data = ride.learn(None, "7d", "hit", 1.0, by_5h=True, account="1")
    assert ride.t_values(data, "1")["7d"] == pytest.approx(0.76)
    assert ride.t_values(data, "2")["7d"] == pytest.approx(0.76)  # the window's
    data = ride.learn(data, "7d", "ok", 2.0, by_5h=True, account="2")
    # #2 starts from the window's 0.76; the window learns every ride.
    assert ride.t_values(data, "2")["7d"] == pytest.approx(0.77)
    assert ride.t_values(data, "1")["7d"] == pytest.approx(0.76)
    assert ride.t_values(data)["7d"] == pytest.approx(0.77)
    assert ride.t_values(data, "3")["7d"] == pytest.approx(0.77)
    # q too, per account, and the record survives JSON.
    data = ride.learn(data, "7d", "ok", 3.0, account="1")
    assert ride.q_values(data, "1")["7d"] == pytest.approx(0.65)
    assert ride.q_values(data, "2")["7d"] == pytest.approx(0.65)  # the window's
    import json
    assert ride.learned_accounts(json.loads(json.dumps(data))) == ride.learned_accounts(data)


# -- the stricter k the 5h measure takes (R2) ---------------------------------------------


def _windows(number: str, ratios: list[float], *, d5: float = 60.0) -> list:
    """Usage-history points: one 5h window per ratio, Δ5h ``d5``."""
    out, ts, p7 = [], 0.0, 10.0
    for r in ratios:
        out.append(SimpleNamespace(ts=ts, number=number, pct5=0.0, pct7=p7))
        p7 += r * d5
        out.append(SimpleNamespace(ts=ts + 4 * 3600.0, number=number, pct5=d5, pct7=p7))
        ts += 5 * 3600.0 + 60.0
    return out


def test_the_ride_takes_only_a_k_from_enough_windows_that_agree():
    from claude_swap.maximize import drain

    tight = [10 / 60, 10 / 60, 10 / 60, 11 / 60, 10 / 60]
    assert drain.ride_k(_windows("1", tight)) == {"1": pytest.approx(10 / 60, abs=1e-4)}
    # Three windows are enough for the drain, not for the ride.
    assert drain.learn_k(_windows("1", tight[:3])) and drain.ride_k(_windows("1", tight[:3])) == {}
    # A spread past 10% of the median (quantization, a plan change) is not.
    loose = [8 / 60, 10 / 60, 10 / 60, 12 / 60, 13 / 60]
    assert drain.learn_k(_windows("1", loose)) and drain.ride_k(_windows("1", loose)) == {}


@pytest.mark.parametrize(
    ("plan", "k", "ok"),
    [(None, 0.165, True), (None, 0.22, True), (None, 0.23, False), (None, 0.105, False),
     (None, 0.135, True), (None, 0.13, False),
     ("5x", 0.105, True), ("5x", 0.141, True), ("5x", 0.142, False),
     ("5x", 0.085, True), ("5x", 0.083, False), ("5x", 0.165, False),
     # A 5x login's k on a 20x slot: a third low, it would ride into 100%.
     ("20x", 0.11, False), ("20x", 0.12, False), ("20x", 0.10, False)],
)
def test_the_ride_takes_a_k_only_from_20_percent_below_to_35_percent_above_the_plan(
    plan, k, ok
):
    snap = Snapshot(
        now=0.0, active="1", accounts=(), samples=(), last_switch_at=None,
        settings=SETTINGS, ride_k7={"1": k},
    )
    got = policy.ride_k(snap, SimpleNamespace(number="1", plan=plan))
    assert (got == k) if ok else got is None


# -- a long gap before the arming reading (R1) --------------------------------------------


def test_the_midpoint_is_credited_only_across_a_short_gap():
    # The 5h a point every 300 s; the reading before the 7d's first 99 at 0.
    def p5(ts: float) -> float:
        return float(math.floor((ts + 1200.0) / 300.0))

    samples = tuple(Sample(float(ts), p5(ts), 98.0) for ts in range(-1200, 1, 120))
    steps: dict = {}
    for prev, x in zip((None, *samples), samples):
        steps = ride.observe(steps, "1", x.pct5, x.pct7, x.ts, prev.ts if prev else None)
    phases = {}
    for gap in (120.0, 240.0, 1200.0):
        at = samples[-1].ts
        five = engine_hook._arm_five_h(
            steps, (*samples, Sample(at + gap, p5(at + gap), 99.0)), SETTINGS, "1",
            at, at + gap, at, at + gap,
        )
        assert five is not None
        phases[gap] = five["phase"]
    point = phases[240.0] - phases[120.0]
    assert point == pytest.approx(60.0 / 300.0, abs=0.02)   # half of 120 s more
    # 20 minutes: counted from the arm time (as the time rule does), no credit.
    assert phases[1200.0] < phases[120.0]


@pytest.mark.parametrize("gap", [600.0, 1200.0])
def test_a_long_gap_before_the_arm_does_not_ride_into_100(gap, monkeypatch):
    def run() -> list[Ride5]:
        rng = random.Random(5)
        return [_ride_5h(rng, 0.88, pre_gap=gap, pace_min=(30, 45)) for _ in range(300)]

    rides = run()
    assert sum(r.hit for r in rides) / len(rides) <= 0.05
    # Before: half the gap credited as unused, whatever its length.
    monkeypatch.setattr(ride, "MIDPOINT_MAX_GAP_S", math.inf)
    before = run()
    assert sum(r.hit for r in before) / len(before) >= 0.2


# -- a multi-window ride teaches only the windows that reached their share (R3) -----------


def _both(*, armed5: float, armed7: float, **settings) -> Snapshot:
    from tests.maximize.test_policy import NOW, acct, rows, snap

    # 5h and 7d at 99, both listed; the 5h still climbing (not idle).
    s = snap(
        "1", acct("1", 99, 99), acct("2", 0, 10),
        samples=rows((600, 97, 99), (300, 98.5, 99), (0, 99, 99)),
        **{"hard_5h": 99.0, "hard_7d": 99.0, "force_eta_min": 3, "ride_windows": "5h,7d",
           **settings},
    )
    return replace(
        s,
        ride_armed_at={"5h": NOW - armed5, "7d": NOW - armed7},
        ride_point_s={"5h": 300.0, "7d": 1800.0},
        ride_5h={"7d": RideFiveH(2.0, 0.2, None)},   # 0.165 × 1.8 ≈ 0.3 used
        ride_k7={"1": K7},
    )


def test_a_window_not_yet_due_learns_nothing_from_the_others_switch():
    from claude_swap.maximize.model import Switch

    # The 5h's time rule is up (0.6 × 300 s − 90 s after its arm); the 7d
    # has used ~0.3 of its point, far from t and from rideMaxMin.
    d = policy.decide(_both(armed5=200.0, armed7=60.0))
    assert isinstance(d, Switch) and d.ride == "due"
    assert d.ride_windows == ("5h", "7d") and not d.ride_capped
    assert d.ride_ok == ("5h",)


def test_a_window_due_only_by_the_cap_learns_nothing_either():
    from claude_swap.maximize.model import Switch

    d = policy.decide(_both(armed5=200.0, armed7=3700.0))
    assert isinstance(d, Switch) and d.ride == "due" and not d.ride_capped
    assert d.ride_ok == ("5h",)
    # Both capped (rideMaxMin 1 ends both before their share): nothing learns.
    d = policy.decide(_both(armed5=200.0, armed7=200.0, ride_max_min=1))
    assert d.ride_capped and d.ride_ok == ()


# -- a halving-era q of 0.9 (R4) ----------------------------------------------------------


def test_a_legacy_q_of_09_starts_over_and_converges_without_a_burst_of_hits():
    old = {"7d": {"q": 0.9, "n_ok": 30, "n_hit": 1}}
    qs, rides = simulate(6, BURN_IN + 600, q0=old)
    assert qs[0] == ride.Q_START
    assert qs[1] in (pytest.approx(0.65), pytest.approx(0.51))
    assert sum(r.hit for r in rides[:10]) <= 2
    assert 0.85 <= statistics.mean(qs[BURN_IN:]) <= 0.92


# -- a 5h reset near 0% (R5) --------------------------------------------------------------


def test_a_5h_reset_with_no_drop_is_seen_by_its_resets_at():
    # The 5h a point every 300 s: it stepped 0 -> 1 at 300 and was folded
    # at 360 reading 1, its window resetting at 500 (1.67 by then). The new
    # window reads 1 (1.33) at 900: no drop.
    samples = (Sample(0.0, 0.0, 99.0), Sample(120.0, 0.0, 99.0), Sample(240.0, 0.0, 99.0),
               Sample(360.0, 1.0, 99.0), Sample(900.0, 1.0, 99.0))

    def fold(r5: float | None) -> dict:
        five = {"p5": 1.0, "readAt": 360.0, "rise": 3.0, "phase": 0.0, "pointS": 300.0,
                "r5": r5}
        engine_hook._fold_five_h(five, samples, 500.0 + 5 * 3600.0)
        return five

    seen = fold(500.0)
    # The old window's 0.67 past its last reading, and the new one's 1.
    assert seen["rise"] == pytest.approx(3.0 + 2.0 / 3.0 + 1.0, abs=0.01)
    assert seen["r5"] == 500.0 + 5 * 3600.0
    # By value alone (as before) the reset is missed: nothing counted.
    assert fold(None)["rise"] == pytest.approx(3.0)
    # A drop is still a reset without resets_at.
    assert ride.rise_5h(3.0, 40.0, 1.0, 0.5) == pytest.approx(4.5)
    assert ride.rise_5h(3.0, 1.0, 1.0, 0.5, reset=True) == pytest.approx(4.5)


# -- describe: per account, only what an account learned itself ---------------------------


def test_describe_shows_an_accounts_own_t_and_own_q_only():
    data = ride.learn(None, "7d", "ok", 1.0, account="2")             # q only
    data = ride.learn(data, "7d", "ok", 2.0, by_5h=True, account="1")  # t only
    line = ride.describe(data, ("7d",))
    t1 = ride.t_values(data, "1")["7d"]
    q2 = ride.q_values(data, "2")["7d"]
    assert line.endswith(f"; per account t: 1 {t1:.2f}; per account q: 2 {q2:.2f})")
    # The review's example: a q-only account shows no t of its own.
    only_q = ride.describe(ride.learn(None, "7d", "ok", 1, account="2"), ("7d",))
    assert "per account t" not in only_q and "per account q: 2 " in only_q
    # The 5h (time rule only) lists own q's, never a t-only account.
    five = ride.learn(None, "5h", "ok", 1.0, by_5h=True, account="3")
    assert "per account" not in ride.describe(five, ("5h",))
    five = ride.learn(five, "5h", "ok", 2.0, account="4")
    assert "per account: 4 " in ride.describe(five, ("5h",))
