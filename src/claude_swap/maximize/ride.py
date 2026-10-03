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
  use. The median of the last :data:`KEEP_INTERVALS` step intervals (per
  point) from the last :data:`INTERVAL_MAX_AGE_S` is the measured ``T1``
  (:func:`observe` says which steps are timed). The engine and the policy
  take the shorter of it and the recent velocity's.
* **Learning** (``LEARN_KEY``): ``q`` per window, AIMD. A ride that switched
  before 100% adds :data:`Q_STEP`; one that saw 100% first halves it. A ride
  an idle switch ended early, and any dry run, teach nothing.

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

Q_DEFAULT = 0.3
Q_MIN = 0.05
Q_MAX = 0.9
Q_STEP = 0.05
#: Step intervals kept per account and window; T1 is their median.
KEEP_INTERVALS = 3
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
    ``{"q", "n_ok", "n_hit", "updatedAt"}`` with ``q`` clamped, defaults for
    anything missing or malformed."""
    src = raw if isinstance(raw, Mapping) else {}
    out: dict[Window, dict] = {}
    for w in WINDOWS:
        item = src.get(w) if isinstance(src.get(w), Mapping) else {}
        q = _num(item.get("q"))
        counts = {k: _num(item.get(k)) for k in ("n_ok", "n_hit")}
        out[w] = {
            "q": clamp_q(q) if q is not None else Q_DEFAULT,
            "n_ok": int(counts["n_ok"]) if counts["n_ok"] and counts["n_ok"] > 0 else 0,
            "n_hit": int(counts["n_hit"]) if counts["n_hit"] and counts["n_hit"] > 0 else 0,
            "updatedAt": _num(item.get("updatedAt")),
        }
    return out


def q_values(raw: object) -> dict[Window, float]:
    """``{window: q}`` from the ``rideLearning`` record."""
    return {w: item["q"] for w, item in learned(raw).items()}


def learn(raw: object, window: Window, outcome: Outcome, now: float) -> dict:
    """The ``rideLearning`` record after one ride on ``window`` ended in
    ``outcome``: ``ok`` (switched before 100%) adds :data:`Q_STEP`, ``hit``
    (100% came first) halves ``q``; both stay in [Q_MIN, Q_MAX]."""
    out = learned(raw)
    item = dict(out[window])
    if outcome == "ok":
        item["q"] = round(min(Q_MAX, item["q"] + Q_STEP), 4)
        item["n_ok"] += 1
    else:
        item["q"] = round(max(Q_MIN, item["q"] / 2.0), 4)
        item["n_hit"] += 1
    item["updatedAt"] = now
    out[window] = item
    return {w: dict(v) for w, v in out.items()}


def describe(raw: object, windows: tuple[str, ...], off: str | None = None) -> str:
    """The doctor and ``cc-swap why`` line: ``learned ride: 5h off
    (rideWindows; learned 0.30) · 7d rides 0.35 of the last point (3 ok,
    1 hit)``. ``windows`` are the ones that ride; ``off`` says why none
    does when the ride is off altogether."""
    if off:
        return f"learned ride: off ({off})"
    data = learned(raw)
    parts = []
    for w in WINDOWS:
        item = data[w]
        if w in windows:
            parts.append(
                f"{w} rides {item['q']:.2f} of the last point "
                f"({item['n_ok']} ok, {item['n_hit']} hit)"
            )
        else:
            parts.append(f"{w} off (rideWindows; learned {item['q']:.2f})")
    return "learned ride: " + " · ".join(parts)


# -- steps ------------------------------------------------------------------------------


def _intervals(listed: object) -> list[list[float]]:
    """``[[at, seconds per point], ...]``, leniently; an interval without
    its time (an older record's bare number) cannot be aged and is dropped."""
    out: list[list[float]] = []
    for item in listed if isinstance(listed, (list, tuple)) else ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        at, seconds = _num(item[0]), _num(item[1])
        if at is not None and seconds is not None and seconds > 0:
            out.append([at, seconds])
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
    stamped ``ts``. Only time spent working counts, so a step is timed only
    when the account was in use all the way to it:

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
                c["intervals"] = [
                    *c["intervals"], [ts, (ts - c["stepAt"]) / (pct - c["pct"])]
                ][-KEEP_INTERVALS:]
            c["stepAt"] = ts
        c["pct"] = pct
    out[number] = {**cur, "activeAt": ts if rose else active_at}
    return out


def point_seconds(raw: object, number: str, window: Window, now: float) -> float | None:
    """``T1``: seconds per point on ``window`` for account ``number``, the
    median of its timed steps from the last :data:`INTERVAL_MAX_AGE_S`;
    None when there is none."""
    src = raw if isinstance(raw, Mapping) else {}
    account = src.get(number)
    if not isinstance(account, Mapping):
        return None
    recent = [
        seconds
        for at, seconds in _window_steps(account.get(window))["intervals"]
        if now - at <= INTERVAL_MAX_AGE_S
    ]
    return float(statistics.median(recent)) if recent else None
