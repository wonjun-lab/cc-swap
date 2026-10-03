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
  stepped up one whole point while that account was the active one. The
  median of the last :data:`KEEP_INTERVALS` step intervals (per point) is
  ``T1``. A step after a hole in the readings (``STEP_MAX_GAP_S``: a parked
  account, a sleep, a 429 backoff) is not timed — its moment is unknown.
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


def _window_steps(raw: object) -> dict:
    src = raw if isinstance(raw, Mapping) else {}
    listed = src.get("intervals")
    listed = listed if isinstance(listed, (list, tuple)) else ()
    intervals = [x for x in (_num(v) for v in listed) if x is not None and x > 0]
    return {
        "pct": _num(src.get("pct")),
        "stepAt": _num(src.get("stepAt")),
        "intervals": intervals[-KEEP_INTERVALS:],
    }


def observe(
    raw: object,
    number: str,
    pct5: float,
    pct7: float,
    ts: float,
    prev_ts: float | None,
) -> dict:
    """The ``rideSteps`` record after the active account ``number`` read
    ``pct5``/``pct7`` at ``ts``, a new reading; ``prev_ts`` is when it was
    read before while active (None: not since it became the active one).

    Per window: a rise from the previous reading is a step at ``ts``; the
    time since the previous timed step, per point risen, is an interval.
    A first reading, a drop (a reset) or a hole longer than
    :data:`STEP_MAX_GAP_S` leaves the next step untimed. Unchanged readings
    change nothing, so the record is only rewritten on a step."""
    src = raw if isinstance(raw, Mapping) else {}
    out: dict = {str(k): dict(v) for k, v in src.items() if isinstance(v, Mapping)}
    account = out.get(number, {})
    continuous = prev_ts is not None and 0 < ts - prev_ts <= STEP_MAX_GAP_S
    for w, pct in (("5h", pct5), ("7d", pct7)):
        cur = _window_steps(account.get(w))
        if cur["pct"] is None or pct < cur["pct"] or not continuous:
            cur.update(pct=pct, stepAt=None)
        elif pct > cur["pct"]:
            if cur["stepAt"] is not None and ts > cur["stepAt"]:
                cur["intervals"] = [
                    *cur["intervals"], (ts - cur["stepAt"]) / (pct - cur["pct"])
                ][-KEEP_INTERVALS:]
            cur.update(pct=pct, stepAt=ts)
        account[w] = cur
    out[number] = account
    return out


def point_seconds(raw: object, number: str, window: Window) -> float | None:
    """``T1``: seconds per point on ``window`` for account ``number``, the
    median of its last timed steps; None before any step was timed."""
    src = raw if isinstance(raw, Mapping) else {}
    account = src.get(number)
    if not isinstance(account, Mapping):
        return None
    intervals = _window_steps(account.get(window))["intervals"]
    return float(statistics.median(intervals)) if intervals else None
