"""The learned ride's memory: the pace of the last point, and what was learned.

The usage API reports whole percents, floored: ``99`` means 99.0-99.99, and
at 100% Claude Code's next request fails. A window whose hard mark sits in
that last point would switch the moment it reads 99 and leave most of the
point unused. The learned ride (``maximize.learnedRide``, maximize/policy.py)
keeps using the account for a learned share ``q`` of the time one point
takes (``T1``), counted from the first reading at the hard mark.

Two pure pieces live here; the engine (maximize/engine_hook.py) persists both
in the state file:

* **Steps** (``STEPS_KEY``): per account and window, the moments the reading
  stepped up one whole point while that account was the active one and in
  use. The pace over the last :data:`KEEP_INTERVALS` timed step intervals
  from the last :data:`INTERVAL_MAX_AGE_S` (their total time over their
  total points, an interval slower than :data:`SLOW_OUTLIER` × their median
  left out) is the measured ``T1`` (:func:`observe` says which steps are
  timed, :func:`point_seconds` reads it). The engine and the policy prefer
  it to the recent velocity over ``idleWindowMin``, which takes over only
  when no step was timed or when it shows a burst (:func:`point_estimate`).
* **Learning** (``LEARN_KEY``): ``q`` per window, a target-hit-rate
  controller. A ride that switched before 100% adds :data:`Q_UP`
  (:data:`Q_UP_FIRST` until the window's first hit); one that saw 100% —
  read, or a limit refusal Claude Code reported — first takes
  :data:`Q_DOWN` off. A ride an idle switch ended early, one ``rideMaxMin``
  cut short, and any dry run, teach nothing.

Why those steps: with hit probability ``p(q)`` rising in ``q``, the mean
change per ride is ``(1 − p)·Q_UP − p·Q_DOWN``, zero at
``p* = Q_UP / (Q_UP + Q_DOWN)`` — :data:`TARGET_HIT_RATE`, 10%. Over any run
of rides that stays inside [Q_MIN, Q_MAX] the hits are exactly
``(Q_UP·rides − Δq) / (Q_UP + Q_DOWN)``, so the long-run hit rate is p*
whatever the pace's noise; at the cap a clean ride adds nothing, so it is
lower there. The noise only decides where ``p(q) = p*`` falls, i.e. how
much of the point a ride gets. The scale of the pair trades how close q
sits under that edge (a hit drops it by Q_DOWN, then it climbs back over
Q_DOWN / Q_UP clean rides) against how fast it gets there; rides are rare
(about one per account and week on 7d), so the first approach climbs at
:data:`Q_UP_FIRST` until the first hit, like TCP's slow start. In the
simulation of tests/maximize/test_ride_controller.py (per-point durations
lognormal σ 0.12 with a 3% drift per point, 2-minute polling, 60 s engine
ticks) q settles at a mean of ~0.88 with ~9% hits and a ride uses ~0.81 of
its last point; +0.02/−0.18 settles at ~0.85, ~8% and ~0.79, and from 0.6
reaches 0.85 in 13 clean rides where the slow start takes 5.

Records the halving rule wrote (no ``"v"``: before :data:`LEARN_VERSION`)
keep their q when it is at least :data:`Q_START` and start from Q_START
when lower (a halved q says where a hit was, not where the edge is), with
the slow start still to come.

Slot numbers and percentages only — never an email or a token.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from typing import Literal

Window = Literal["5h", "7d"]
WINDOWS: tuple[Window, Window] = ("5h", "7d")
Outcome = Literal["ok", "hit"]

#: State-file keys (``autoswitch_state.json``).
STEPS_KEY = "rideSteps"
LEARN_KEY = "rideLearning"

#: q for a window with no history (and the floor a halving-era q migrates to).
Q_START = 0.6
#: Kept for callers that ask for "the q of a window nothing was learned for".
Q_DEFAULT = Q_START
Q_MIN = 0.3
Q_MAX = 0.95
#: A clean ride (switched before 100%) adds this; a hit takes Q_DOWN off.
Q_UP = 0.01
Q_DOWN = 0.09
#: A clean ride's step until the window's first hit (the slow start).
Q_UP_FIRST = 0.05
#: The hit rate the controller settles at below the cap: Q_UP/(Q_UP+Q_DOWN).
TARGET_HIT_RATE = Q_UP / (Q_UP + Q_DOWN)
#: The share of the last point it aims for (~99.9%), as the surfaces say it.
TARGET_SHARE = 0.9
#: ``rideLearning`` records written by this controller say so (``"v"``).
LEARN_VERSION = 2

#: Step intervals kept per account and window; T1 is their pace.
KEEP_INTERVALS = 6
#: An interval this many times the median of the kept ones is a pause
#: shorter than the idle window, not the pace: left out of T1.
SLOW_OUTLIER = 2.0
#: The recent velocity over ``idleWindowMin`` overrides the timed steps only
#: when it rose at least this many points: one whole-percent step in a short
#: span is mostly rounding (the step may have been 0.01 or 1.99 points).
BURST_POINTS = 2.0
#: Two readings further apart than this leave the moment of a step between
#: them unknown (the active account polls at most every 5 minutes, 30 after
#: a run of 429s).
STEP_MAX_GAP_S = 1800.0
#: A timed step older than this says nothing about the pace now.
INTERVAL_MAX_AGE_S = 6 * 3600.0
#: ``maximize.idleWindowMin``'s default, in seconds: a stretch this long
#: with no rise on either window is an idle period (``observe``'s
#: ``quiet_s``; the engine passes the setting).
DEFAULT_QUIET_S = 600.0
#: The arm time. A reading is floored and taken a poll after the one
#: before it, so a window first read at its mark may have crossed it any
#: time after the previous reading. The ride counts from that previous
#: reading (the earliest the crossing can be: conservative) when it is at
#: most ``STEP_MAX_GAP_S`` older; with no such reading, from this long
#: before the first one — ``poll_policy.ACTIVE_MAX_INTERVAL_S``, the
#: longest the active account's normal cadence leaves between readings.
ARM_UNKNOWN_GAP_S = 300.0


def arm_time(read_at: float, previous_at: float | None) -> float:
    """When a window first read at its hard mark at ``read_at`` is armed:
    the previous reading's time (``previous_at``, below the mark) when it
    is recent, else :data:`ARM_UNKNOWN_GAP_S` before ``read_at``."""
    if previous_at is not None and 0 <= read_at - previous_at <= STEP_MAX_GAP_S:
        return previous_at
    return read_at - ARM_UNKNOWN_GAP_S


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def clamp_q(q: float) -> float:
    return min(Q_MAX, max(Q_MIN, q))


# -- learning ---------------------------------------------------------------------------


def learned(raw: object) -> dict[Window, dict]:
    """The ``rideLearning`` record, leniently: each window's
    ``{"q", "n_ok", "n_hit", "settled", "v", "updatedAt"}`` with ``q``
    clamped, defaults for anything missing or malformed. A window the
    halving rule wrote (no ``"v"``) is migrated: its q is at least
    :data:`Q_START`, and it is not ``settled`` (the slow start is ahead)."""
    src = raw if isinstance(raw, Mapping) else {}
    out: dict[Window, dict] = {}
    for w in WINDOWS:
        item = src.get(w) if isinstance(src.get(w), Mapping) else {}
        q = _num(item.get("q"))
        current = _num(item.get("v")) == LEARN_VERSION
        if q is None:
            q = Q_START
        elif not current:
            q = max(q, Q_START)
        counts = {k: _num(item.get(k)) for k in ("n_ok", "n_hit")}
        out[w] = {
            "q": clamp_q(q),
            "n_ok": int(counts["n_ok"]) if counts["n_ok"] and counts["n_ok"] > 0 else 0,
            "n_hit": int(counts["n_hit"]) if counts["n_hit"] and counts["n_hit"] > 0 else 0,
            "settled": current and item.get("settled") is True,
            "v": LEARN_VERSION,
            "updatedAt": _num(item.get("updatedAt")),
        }
    return out


def q_values(raw: object) -> dict[Window, float]:
    """``{window: q}`` from the ``rideLearning`` record."""
    return {w: item["q"] for w, item in learned(raw).items()}


def learn(raw: object, window: Window, outcome: Outcome, now: float) -> dict:
    """The ``rideLearning`` record after one ride on ``window`` ended in
    ``outcome``: ``ok`` (switched before 100%) adds :data:`Q_UP`
    (:data:`Q_UP_FIRST` while the window has not hit since this controller
    took over), ``hit`` (100% came first) takes :data:`Q_DOWN` off and
    settles the window; q stays in [Q_MIN, Q_MAX]."""
    out = learned(raw)
    item = dict(out[window])
    if outcome == "ok":
        step = Q_UP if item["settled"] else Q_UP_FIRST
        item["q"] = round(min(Q_MAX, item["q"] + step), 4)
        item["n_ok"] += 1
    else:
        item["q"] = round(max(Q_MIN, item["q"] - Q_DOWN), 4)
        item["n_hit"] += 1
        item["settled"] = True
    item["updatedAt"] = now
    out[window] = item
    return {w: dict(v) for w, v in out.items()}


def describe(raw: object, windows: tuple[str, ...], off: str | None = None) -> str:
    """The doctor and ``cc-swap why`` line: ``learned ride: 5h off
    (rideWindows; learned 0.60) · 7d rides 0.62 of the last point (target
    ~0.9, 5 ok, 1 hit)``. ``windows`` are the ones that ride; ``off`` says
    why none does when the ride is off altogether."""
    if off:
        return f"learned ride: off ({off})"
    data = learned(raw)
    parts = []
    for w in WINDOWS:
        item = data[w]
        if w in windows:
            parts.append(
                f"{w} rides {item['q']:.2f} of the last point "
                f"(target ~{TARGET_SHARE:g}, {item['n_ok']} ok, {item['n_hit']} hit)"
            )
        else:
            parts.append(f"{w} off (rideWindows; learned {item['q']:.2f})")
    return "learned ride: " + " · ".join(parts)


# -- the pace of a point ----------------------------------------------------------------


def point_estimate(
    steps_s: float | None,
    velocity_s: float | None,
    velocity_points: float = 0.0,
) -> float | None:
    """``T1`` from the timed steps' pace (``steps_s``, :func:`point_seconds`)
    and the recent velocity over ``idleWindowMin`` (``velocity_s`` seconds
    per point, from ``velocity_points`` points risen).

    The steps win while there are any: they time whole points across the
    account's continuous use, where a short span of whole-percent readings
    on 7d holds one step or none (one step in 10 minutes reads as 10
    minutes a point whatever the pace: a T1 several times too short, a ride
    a fraction of what it could be). The velocity decides when no step was
    timed (any rise), or when it shows a burst — at least
    :data:`BURST_POINTS` risen and faster than the steps: a T1 too long
    rides into 100%. None when neither is known."""
    steps = steps_s if steps_s is not None and math.isfinite(steps_s) and steps_s > 0 else None
    velocity = (
        velocity_s
        if velocity_s is not None and math.isfinite(velocity_s) and velocity_s > 0
        else None
    )
    if steps is None:
        return velocity
    if velocity is not None and velocity_points >= BURST_POINTS and velocity < steps:
        return velocity
    return steps


# -- steps ------------------------------------------------------------------------------


def _intervals(listed: object) -> list[list[float]]:
    """``[[at, seconds per point, points], ...]``, leniently; an interval
    without its time (an older record's bare number) cannot be aged and is
    dropped, one without its points (older records) counts one."""
    out: list[list[float]] = []
    for item in listed if isinstance(listed, (list, tuple)) else ():
        if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
            continue
        at, seconds = _num(item[0]), _num(item[1])
        points = _num(item[2]) if len(item) == 3 else 1.0
        if at is None or seconds is None or seconds <= 0:
            continue
        out.append([at, seconds, points if points is not None and points > 0 else 1.0])
    return out[-KEEP_INTERVALS:]


def _window_steps(raw: object) -> dict:
    src = raw if isinstance(raw, Mapping) else {}
    return {
        "pct": _num(src.get("pct")),
        "stepAt": _num(src.get("stepAt")),
        "intervals": _intervals(src.get("intervals")),
    }


def observe(
    raw: object,
    number: str,
    pct5: float,
    pct7: float,
    ts: float,
    prev_ts: float | None,
    *,
    quiet_s: float = DEFAULT_QUIET_S,
) -> dict:
    """The ``rideSteps`` record after the active account ``number`` read
    ``pct5``/``pct7`` at ``ts``, a new reading; ``prev_ts`` is when it was
    read before while active (None: not since it became the active one).

    Per window, a rise from the previous reading is a step at ``ts``, and
    the time since the window's previous step, per point risen, an interval
    stamped ``ts`` (with the points it spans). Only time spent working
    counts, so a step is timed only when the account was in use all the way
    to it:

    * a new tenure (``prev_ts`` None: the account was parked in between) or
      a hole longer than :data:`STEP_MAX_GAP_S` clears the account's history;
    * a 7d reset clears the account's history, a 5h reset that window's;
    * a stretch longer than ``quiet_s`` (``idleWindowMin``) with no rise on
      either window is an idle period: the next step on every window is
      not timed (an evening step and a morning one are not one interval).
    """
    src = raw if isinstance(raw, Mapping) else {}
    out: dict = {str(k): dict(v) for k, v in src.items() if isinstance(v, Mapping)}
    continuous = prev_ts is not None and 0 < ts - prev_ts <= STEP_MAX_GAP_S
    account = out.get(number, {}) if continuous else {}
    cur = {w: _window_steps(account.get(w)) for w in WINDOWS}
    if cur["7d"]["pct"] is not None and pct7 < cur["7d"]["pct"]:
        cur = {w: _window_steps(None) for w in WINDOWS}  # a 7d reset
    elif cur["5h"]["pct"] is not None and pct5 < cur["5h"]["pct"]:
        cur["5h"] = _window_steps(None)
    rose = any(
        c["pct"] is not None and pct > c["pct"]
        for c, pct in ((cur["5h"], pct5), (cur["7d"], pct7))
    )
    active_at = _num(account.get("activeAt"))
    if active_at is None:
        active_at = ts  # observation starts here
    if rose and ts - active_at > quiet_s:
        for c in cur.values():
            c["stepAt"] = None  # an idle period since the last rise
    for w, pct in (("5h", pct5), ("7d", pct7)):
        c = cur[w]
        if c["pct"] is not None and pct > c["pct"]:
            if c["stepAt"] is not None and ts > c["stepAt"]:
                risen = pct - c["pct"]
                c["intervals"] = [
                    *c["intervals"], [ts, (ts - c["stepAt"]) / risen, risen]
                ][-KEEP_INTERVALS:]
            c["stepAt"] = ts
        c["pct"] = pct
    out[number] = {**cur, "activeAt": ts if rose else active_at}
    return out


def point_seconds(
    raw: object,
    number: str,
    window: Window,
    now: float,
    *,
    median_of: int | None = None,
) -> float | None:
    """``T1``: seconds per point on ``window`` for account ``number`` at the
    pace of its timed steps from the last :data:`INTERVAL_MAX_AGE_S` —
    their total time over their total points, any interval slower than
    :data:`SLOW_OUTLIER` × their median left out. Consecutive intervals
    share their ends, so the total is the time from the first timed step to
    the last: each step's polling error counts once, not once per interval.
    None when there is none.

    ``median_of``: the median of the last that many intervals' per-point
    times instead (the usage projection's burn rate, maximize/estimate.py,
    which reads the pace the way it always has)."""
    src = raw if isinstance(raw, Mapping) else {}
    account = src.get(number)
    if not isinstance(account, Mapping):
        return None
    recent = [
        (seconds, points)
        for at, seconds, points in _window_steps(account.get(window))["intervals"]
        if now - at <= INTERVAL_MAX_AGE_S
    ]
    if not recent:
        return None
    if median_of is not None:
        return float(statistics.median(s for s, _ in recent[-median_of:]))
    middle = statistics.median(seconds for seconds, _ in recent)
    kept = [(s, p) for s, p in recent if s <= SLOW_OUTLIER * middle]
    return float(sum(s * p for s, p in kept) / sum(p for _, p in kept))
