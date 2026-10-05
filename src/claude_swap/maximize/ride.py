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
* **Learning** (``LEARN_KEY``): ``q`` per window and per account (and
  ``t``, the 5h measure's target, below), a target-hit-rate controller. A ride that switched before 100% adds :data:`Q_UP`
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

**The 7d measured on the 5h** (:func:`phase_5h`, :func:`rise_5h`,
:func:`fraction_5h`). Timing the last point from the points before it
leaves the pace's noise in every ride (the ~0.8 above). The 5h counts the
same use about six times as fast on a 20x plan (``k`` = Δ7d/Δ5h ≈ 0.165,
learned per account, maximize/drain.py), so its whole-percent steps cut
the 7d's last point into sixths and the time since its last step reads
between them: ``k × (5h points risen since the arm time + the 5h's phase
now − its phase at the 7d's estimated crossing)`` is the share used. The
policy switches once it reaches a learned target ``t`` (``"t"`` per account
and window in ``rideLearning``, below): the same controller, +:data:`T_UP` clean,
−:data:`T_DOWN` on a hit, from :data:`T_START` in [T_MIN, T_MAX], so ~10%
of rides hit. In the simulation of tests/maximize/test_ride_controller.py
(5h read in whole percents every 2 minutes, k off by ±3% per ride, a 5h
reset in one ride in five) t settles at ~0.88 with ~10% hits and a ride
uses ~0.91 of its last point (99.9%); with k off by ±5%, ~0.89. With a
persistent per-account k bias of up to ±8% each account's own t absorbs
it and each keeps to about one hit in ten. Without a trusted k, or the
readings it needs, the time rule rides.

Records the halving rule wrote (no ``"v"``: before :data:`LEARN_VERSION`)
start over from :data:`Q_START`, with the slow start still to come: their q
was learned against the old, much shorter T1 (a fifth of the point), so a
high one (0.9 after many clean rides) would ride an accurate T1 into 100%,
and a halved one says where a hit was, not where the edge is.

**Per account.** q and t are kept per window (``rideLearning["5h"]``,
``["7d"]``) and per account and window (``rideLearning["accounts"][slot]``):
an account rides by its own once it has one, else by the window's, and
every ride teaches both. The 5h measure's error is mostly the account's
k (a median of a few quantized windows, or one that still mixes an old
plan in), a bias of its own: one shared t would settle on the mix of
accounts and let the one whose k reads low hit on most rides. The
5h measure also needs a k it can trust (``policy.ride_k``: at least
``drain.K_RIDE_MIN_WINDOWS`` windows that agree within
``drain.K_RIDE_MAX_SPREAD``, and from ``drain.K_RIDE_PLAN_BAND_BELOW``
under the plan's default to ``drain.K_RIDE_PLAN_BAND`` over it), else the
time rule rides. A slot that comes to hold another login or plan loses its
own record, pace and k history (maximize/ride_slots.py).

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
#: ``rideLearning``'s per-account part: ``{slot: {window: record}}``.
ACCOUNTS_KEY = "accounts"

#: q for a window with no history (and what a halving-era q migrates to).
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
#: ``rideLearning`` records written by this controller say so (``"v"``).
LEARN_VERSION = 2

#: The 5h-measured ride (7d only, :func:`fraction_5h`): it switches once the
#: estimated share of the last point used reaches a learned target ``t``,
#: the same target-hit-rate controller as q (+T_UP clean, −T_DOWN on a hit:
#: ~10% hits). No slow start: the estimate is a share of the point, so the
#: start value is already close.
T_START = 0.85
T_MIN = 0.5
T_MAX = 0.97
T_UP = Q_UP
T_DOWN = Q_DOWN
#: The 5h's fraction of a point between two whole-percent steps is read off
#: the time since its last step, never past this (a slow point must not
#: read as the next one).
PHASE_MAX = 0.95
#: ... averaged over this many of its last steps (:func:`phase_5h`).
PHASE_STEPS = 6

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
#: The 5h measure credits the 7d's crossing at the middle of the two
#: readings around it only when they are at most this far apart (twice
#: ``poll_policy.ACTIVE_HIGH_USAGE_INTERVAL_S``): across a longer gap (a
#: sleep, a restart, the post-429 cadence) other use may have carried the 7d
#: over its mark early in it, and half the gap credited as unused rides into
#: 100%. It then counts from the arm time, as the time rule does.
MIDPOINT_MAX_GAP_S = 240.0


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


def clamp_t(t: float) -> float:
    return min(T_MAX, max(T_MIN, t))


# -- learning ---------------------------------------------------------------------------


def _item(item: Mapping, base: dict | None = None) -> dict:
    """One window's learning record, leniently. ``base`` (an account's
    record): what it falls back to field by field, the window's record."""
    current = _num(item.get("v")) == LEARN_VERSION
    q = _num(item.get("q"))
    if q is None or not current:
        # Nothing learned, or a halving-era q: learned against the old,
        # much shorter T1, it says nothing about a share of the real one.
        q = base["q"] if base is not None and q is None else Q_START
    t = _num(item.get("t"))
    if t is None:
        t = base["t"] if base is not None else T_START
    counts = {k: _num(item.get(k)) for k in ("n_ok", "n_hit")}
    settled = item.get("settled")
    return {
        "q": clamp_q(q),
        "t": clamp_t(t),
        "n_ok": int(counts["n_ok"]) if counts["n_ok"] and counts["n_ok"] > 0 else 0,
        "n_hit": int(counts["n_hit"]) if counts["n_hit"] and counts["n_hit"] > 0 else 0,
        "settled": (
            current and settled is True
            if base is None or "settled" in item else base["settled"]
        ),
        "v": LEARN_VERSION,
        "updatedAt": _num(item.get("updatedAt")),
    }


def learned(raw: object) -> dict[Window, dict]:
    """The ``rideLearning`` record's per-window part, leniently: each
    window's ``{"q", "t", "n_ok", "n_hit", "settled", "v", "updatedAt"}``
    with ``q`` and ``t`` clamped, defaults for anything missing or
    malformed. A window the halving rule wrote (no ``"v"``) is migrated: its
    q starts over from :data:`Q_START` (it was learned against the old,
    much shorter T1), and it is not ``settled`` (the slow start is ahead)."""
    src = raw if isinstance(raw, Mapping) else {}
    out: dict[Window, dict] = {}
    for w in WINDOWS:
        item = src.get(w) if isinstance(src.get(w), Mapping) else {}
        out[w] = _item(item)
    return out


_FIELDS = ("q", "t", "n_ok", "n_hit", "settled", "v", "updatedAt")


def _own(raw: object) -> dict[str, dict[Window, dict]]:
    """``rideLearning["accounts"]`` as stored: per slot and window only the
    fields that account learned itself (a 5h-measured ride writes ``t``,
    a timed one ``q``), the rest left to the window's record."""
    src = raw if isinstance(raw, Mapping) else {}
    accounts = src.get(ACCOUNTS_KEY)
    out: dict[str, dict[Window, dict]] = {}
    for number, items in (accounts.items() if isinstance(accounts, Mapping) else ()):
        if not isinstance(items, Mapping):
            continue
        mine = {
            w: {k: items[w][k] for k in _FIELDS if k in items[w]}
            for w in WINDOWS
            if isinstance(items.get(w), Mapping)
        }
        if mine:
            out[str(number)] = mine
    return out


def learned_accounts(raw: object) -> dict[str, dict[Window, dict]]:
    """The per-account part (``rideLearning["accounts"]``), leniently:
    ``{slot: {window: record}}``, only the windows an account learned for,
    each falling back field by field to the window's record (its counts
    are its own)."""
    windows = learned(raw)
    return {
        number: {w: _item(item, windows[w]) for w, item in items.items()}
        for number, items in _own(raw).items()
    }


def learned_for(raw: object, account: str | None) -> dict[Window, dict]:
    """Each window's record as account ``account`` rides it: its own where
    it learned one, else the window's (None: the window's)."""
    out = learned(raw)
    if account is not None:
        out.update(learned_accounts(raw).get(str(account), {}))
    return out


def q_values(raw: object, account: str | None = None) -> dict[Window, float]:
    """``{window: q}`` from the ``rideLearning`` record, account
    ``account``'s own where it has one."""
    return {w: item["q"] for w, item in learned_for(raw, account).items()}


def t_values(raw: object, account: str | None = None) -> dict[Window, float]:
    """``{window: t}``, the 5h-measured ride's targets, from the record,
    account ``account``'s own where it has one."""
    return {w: item["t"] for w, item in learned_for(raw, account).items()}


def _step(item: dict, outcome: Outcome, now: float, by_5h: bool) -> dict:
    item = dict(item)
    if by_5h:
        if outcome == "ok":
            item["t"] = round(min(T_MAX, item["t"] + T_UP), 4)
            item["n_ok"] += 1
        else:
            item["t"] = round(max(T_MIN, item["t"] - T_DOWN), 4)
            item["n_hit"] += 1
    elif outcome == "ok":
        step = Q_UP if item["settled"] else Q_UP_FIRST
        item["q"] = round(min(Q_MAX, item["q"] + step), 4)
        item["n_ok"] += 1
    else:
        item["q"] = round(max(Q_MIN, item["q"] - Q_DOWN), 4)
        item["n_hit"] += 1
        item["settled"] = True
    item["updatedAt"] = now
    return item


def learn(
    raw: object,
    window: Window,
    outcome: Outcome,
    now: float,
    *,
    by_5h: bool = False,
    account: str | None = None,
) -> dict:
    """The ``rideLearning`` record after one ride on ``window`` ended in
    ``outcome``: ``ok`` (switched before 100%) adds :data:`Q_UP`
    (:data:`Q_UP_FIRST` while the window has not hit since this controller
    took over), ``hit`` (100% came first) takes :data:`Q_DOWN` off and
    settles the window; q stays in [Q_MIN, Q_MAX].

    ``by_5h``: the ride ran on the 5h-measured estimate (:func:`fraction_5h`)
    and teaches its target ``t`` instead: +:data:`T_UP` / −:data:`T_DOWN`,
    in [T_MIN, T_MAX]. q is left as it is.

    ``account``: the slot that rode. Its own record (``"accounts"``, from
    the window's when it has none yet) learns the same step: the 5h
    measure's error is the account's k, a bias of its own that one shared
    t cannot absorb. The window's record learns too: it is what an account
    with nothing of its own rides by."""
    windows = learned(raw)
    own = _own(raw)
    if account is not None:
        number = str(account)
        merged = learned_accounts(raw).get(number, {}).get(window)
        if merged is None:
            merged = {**windows[window], "n_ok": 0, "n_hit": 0}
        stepped = _step(merged, outcome, now, by_5h)
        keys = ("t",) if by_5h else ("q", "settled")
        entry = own.setdefault(number, {}).setdefault(window, {})
        entry.update({k: stepped[k] for k in (*keys, "n_ok", "n_hit", "updatedAt")})
        entry["v"] = LEARN_VERSION
    windows[window] = _step(windows[window], outcome, now, by_5h)
    out: dict = {w: dict(v) for w, v in windows.items()}
    if own:
        out[ACCOUNTS_KEY] = own
    return out


def describe(raw: object, windows: tuple[str, ...], off: str | None = None) -> str:
    """The doctor and ``cc-swap why`` line: ``learned ride: 5h off
    (rideWindows; learned 0.60) · 7d rides to 0.88 of the last point by
    its 5h, else 0.62 of its time (aims for ~1 hit in 10, 5 ok, 1 hit;
    per account t: 1 0.86, 2 0.90; per account q: 2 0.70)``. ``windows`` are the ones that ride;
    ``off`` says why none does when the ride is off altogether."""
    if off:
        return f"learned ride: off ({off})"
    data = learned(raw)
    accounts = learned_accounts(raw)
    raw_own = _own(raw)
    aim = f"aims for ~1 hit in {round(1 / TARGET_HIT_RATE)}"
    parts = []
    for w in WINDOWS:
        item = data[w]
        own = [
            (number, items[w]) for number, items in sorted(accounts.items()) if w in items
        ]
        counts = f"({aim}, {item['n_ok']} ok, {item['n_hit']} hit"
        if w in windows and w == "7d":
            own_t = [(n, m) for n, m in own if "t" in raw_own.get(n, {}).get(w, {})]
            own_q = [(n, m) for n, m in own if "q" in raw_own.get(n, {}).get(w, {})]
            if own_t:
                counts += "; per account t: " + ", ".join(
                    f"{number} {mine['t']:.2f}" for number, mine in own_t
                )
            if own_q:
                counts += "; per account q: " + ", ".join(
                    f"{number} {mine['q']:.2f}" for number, mine in own_q
                )
            parts.append(
                f"{w} rides to {item['t']:.2f} of the last point by its 5h, "
                f"else {item['q']:.2f} of its time {counts})"
            )
        elif w in windows:
            own_q = [(n, m) for n, m in own if "q" in raw_own.get(n, {}).get(w, {})]
            if own_q:
                counts += "; per account: " + ", ".join(
                    f"{number} {mine['q']:.2f}" for number, mine in own_q
                )
            parts.append(f"{w} rides {item['q']:.2f} of the last point {counts})")
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


# -- the 7d's last point, measured on the 5h ---------------------------------------------


def phase_5h(
    samples, at: float, point5_s: float | None, *, limit: float | None = PHASE_MAX
) -> float:
    """How far the 5h is into its current whole point at ``at`` (0 to
    :data:`PHASE_MAX`), from ``samples`` (``Sample``: ``ts``, ``pct5``;
    oldest first) up to ``at`` and the 5h's seconds per point.

    Each whole-point step lands between two readings: with ``r`` points
    risen across them, the ``i``-th of them ``(i − ½)/r`` of the way (a
    uniform pace); a 5h reset is a step to 0 halfway between its two
    readings. A line through the last :data:`PHASE_STEPS` steps of the
    current 5h window (at least three; else the last step, carried forward
    at ``point5_s``) says where the 5h is at ``at``, so one step's polling
    error counts a fraction and the pace is the one of these steps. With
    no step seen, the first reading at the current value stands for it (a
    lower bound). 0 when the pace is unknown or nothing was read by
    ``at``. ``limit`` None: past the next whole point too (the old
    window's last stretch before a 5h reset, read after the fact)."""
    if point5_s is None or not math.isfinite(point5_s) or point5_s <= 0:
        return 0.0
    seen = [x for x in samples if x.ts <= at]
    if not seen:
        return 0.0
    value = seen[-1].pct5
    steps: list[tuple[float, float]] = []  # (the value stepped to, when)
    for before, x in zip(seen, seen[1:]):
        gap = x.ts - before.ts
        start, start_at = before.pct5, before.ts
        if x.pct5 < before.pct5:
            # A 5h reset: the window before says nothing, and the new one
            # started from 0 between the two readings.
            steps = [(0.0, before.ts + gap / 2.0)] if 0 < gap <= STEP_MAX_GAP_S else []
            start, start_at = 0.0, before.ts + gap / 2.0
        if x.pct5 == start or not 0 < gap <= STEP_MAX_GAP_S:
            continue
        risen, span = x.pct5 - start, x.ts - start_at
        r = math.ceil(risen)
        for i in range(1, r + 1):
            steps.append((start + risen * i / r, start_at + span * (i - 0.5) / r))
    steps = steps[-PHASE_STEPS:]
    if len(steps) >= 3 and steps[-1][1] > steps[0][1]:
        # A line through these last steps: the 5h's level and pace now,
        # each step's polling error averaged down.
        mv = statistics.fmean(v for v, _ in steps)
        mt = statistics.fmean(ts for _, ts in steps)
        var = sum((ts - mt) ** 2 for _, ts in steps)
        slope = sum((ts - mt) * (v - mv) for v, ts in steps) / var if var > 0 else 0.0
        level = mv + (at - mt) * (slope if slope > 0 else 1.0 / point5_s)
    elif steps:
        v, ts = steps[-1]
        level = v + (at - ts) / point5_s
    else:
        i = len(seen) - 1
        while i > 0 and seen[i - 1].pct5 == value:
            i -= 1
        level = value + (at - seen[i].ts) / point5_s
    phase = max(0.0, level - value)
    return phase if limit is None else min(limit, phase)


def rise_5h(
    rise: float,
    last: float,
    pct5: float,
    carry: float = 0.0,
    *,
    reset: bool | None = None,
) -> float:
    """5h points risen since the arm time after a new reading ``pct5``,
    from ``rise`` so far and the previous reading ``last``: a rise adds
    itself; a 5h reset adds what the new window reads plus ``carry``, how
    far the old window had gone past ``last`` by its reset (:func:`phase_5h`
    at the reset; whole points up to ``last`` were already counted).

    ``reset``: whether the 5h reset between the two readings (the old
    reading's ``resets_at`` passed); None reads it off the values, a drop.
    A window at 0-1% that resets into one reading as much or more shows no
    drop, so the caller says so when it knows."""
    if reset is None:
        reset = pct5 < last
    if not reset:
        return rise + max(pct5 - last, 0.0)
    return rise + max(carry, 0.0) + max(pct5, 0.0)


def fraction_5h(
    rise: float, phase_armed: float, phase_now: float, k: float
) -> float:
    """The share of the 7d's last point used since the arm time, from the
    5h: ``k × (whole 5h points risen + the 5h's phase now − its phase at
    the arm time)``, in [0, 1]. ``k`` is the account's 7d points per 5h
    point (maximize/drain.py), so on a 20x plan (k ≈ 0.165) a 5h point is
    a sixth of a 7d point, and the phases read between them."""
    return min(1.0, max(0.0, k * (rise + phase_now - phase_armed)))
