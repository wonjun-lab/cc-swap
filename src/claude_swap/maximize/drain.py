"""Near-reset 7d drain (cc-swap fork, maximize). Pure.

The 7d soft mark keeps an account from being landed on once its 7d passes
``soft7d − landingMargin`` and moves you off it at ``soft7d``. Near the
account's 7d reset that reserve is exactly what expires unused: an account
parked at 86–92% is reachable only after every other account reaches a
hard cap, and on a lighter day nobody does. So an account is *draining*
(``maximize.drainHours``, 0 = off) when its 7d reset is known, it is under
its 7d hard cap, and either

* the reset is at most ``drainHours`` hours away (the **hours rule**), or
* draining what is left under the hard cap needs at least
  :data:`HEADROOM_SHARE` of the 5h windows left before the reset, at its
  learned rate (the **headroom rule**)::

      hard7d − pct7 ≥ HEADROOM_SHARE × windows_left × k × 100
      windows_left  = hours to the 7d reset ÷ 5

  ``k`` is how many 7d points one 5h point costs on that account
  (:func:`learn_k`; one full 5h window is ``k × 100`` 7d points). In the
  soft-mark band (85–98%) this fires only within a few hours of the reset,
  which the hours rule already covers by default; it matters for an
  account with a lot left close to its reset, which it ranks first.

``windows_left`` counts every 5h window to the reset, busy or not, rather
than discounting by the learned idle pattern: the policy's Snapshot carries
only the pattern's summary (``Forecast``), and an overcount only makes the
headroom rule fire later, never move you off anything.

What draining changes (maximize/policy.py): its 7d soft trigger does not
fire (no ``soft`` or ``preempt`` move off it on 7d); it is landable while
its 7d is under ``hard7d − landingMargin`` (the 5h rules are unchanged);
within a tier draining accounts are tried first, the earliest 7d reset
first; and ``rebalance`` never moves off a draining account except to
another draining one that resets sooner. Hard caps, at-limit, the
login-expiry guard, last resort, quarantine and holds are unchanged.

``k`` is learned from the usage history the engine already keeps
(``usage_history.jsonl``, maximize/history.py): each account's first
reading per clock hour, 8 days. Nothing new is stored. :func:`learn_k`
splits an account's points into single 5h windows and takes the median of
Δ7d/Δ5h over windows that used at least :data:`K_MIN_D5` points of 5h
(both readings are whole percents, floored, so a short window's ratio is
mostly rounding). Fewer than :data:`K_MIN_WINDOWS` such windows: the plan's
default (:func:`fallback_k`).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Literal

from claude_swap.maximize.model import AccountView, Snapshot

HOUR_S = 3600.0
#: One 5h window, in hours and seconds.
WINDOW_H = 5.0
WINDOW_S = WINDOW_H * HOUR_S
#: The headroom rule fires when the 7d room needs this share of the 5h
#: windows left before the reset.
HEADROOM_SHARE = 0.8

#: 7d points per 5h point when nothing is learned, by plan (measured on
#: real 20x and 5x accounts: a full 5h window is ~16.5% / ~10.5% of 7d).
K_20X = 0.165
K_5X = 0.105
K_DEFAULT = K_20X
#: A window counts toward k only when its 5h rose at least this much ...
K_MIN_D5 = 40.0
#: ... and k is learned from at least this many such windows.
K_MIN_WINDOWS = 2
#: A learned k outside this range is a misread (a window split wrong): kept
#: inside it.
K_MIN = 0.02
K_MAX = 0.5

Rule = Literal["hours", "headroom"]


def fallback_k(plan: str | None) -> float:
    """The plan's default k: 5x :data:`K_5X`, 20x and unknown :data:`K_20X`."""
    return K_5X if plan == "5x" else K_DEFAULT


# -- learning k -------------------------------------------------------------------------


def window_ratios(points: Sequence, number: str) -> list[float]:
    """Δ7d/Δ5h for each single 5h window of account ``number`` whose 5h rose
    at least :data:`K_MIN_D5`, oldest first.

    ``points`` are usage-history points (``history.UsagePoint``: ``ts``,
    ``number``, ``pct5``, ``pct7``), any order. A window ends where its 5h
    or 7d drops (a reset) or where the next point is more than 5 hours after
    the window's first one (no 5h window is longer)."""
    mine = sorted((p for p in points if p.number == number), key=lambda p: p.ts)
    windows: list[list] = []
    cur: list = []
    for p in mine:
        if cur and (
            p.pct5 < cur[-1].pct5 or p.pct7 < cur[-1].pct7 or p.ts - cur[0].ts > WINDOW_S
        ):
            windows.append(cur)
            cur = []
        cur.append(p)
    if cur:
        windows.append(cur)
    out: list[float] = []
    for w in windows:
        d5 = w[-1].pct5 - w[0].pct5
        d7 = w[-1].pct7 - w[0].pct7
        if d5 >= K_MIN_D5 and d7 >= 0:
            out.append(d7 / d5)
    return out


def learn_k(points: Sequence, now: float | None = None) -> dict[str, float]:
    """``{slot: k}`` for every account with at least :data:`K_MIN_WINDOWS`
    counted windows (:func:`window_ratios`): their median, kept inside
    [:data:`K_MIN`, :data:`K_MAX`]. ``now`` drops points from the future."""
    if now is not None:
        points = [p for p in points if p.ts <= now]
    out: dict[str, float] = {}
    for number in sorted({p.number for p in points}):
        ratios = window_ratios(points, number)
        if len(ratios) >= K_MIN_WINDOWS:
            k = float(statistics.median(ratios))
            out[number] = round(min(K_MAX, max(K_MIN, k)), 4)
    return out


def k_for(v: AccountView, k7: Mapping[str, float]) -> tuple[float, bool]:
    """``(k, learned)`` for account ``v``: the learned one, else its plan's."""
    learned = k7.get(v.number)
    if isinstance(learned, (int, float)) and math.isfinite(learned) and learned > 0:
        return float(learned), True
    return fallback_k(v.plan), False


# -- the predicate ----------------------------------------------------------------------


def hours_left(v: AccountView, now: float) -> float | None:
    """Hours until ``v``'s 7d reset; None when unknown or past."""
    if v.reset7 is None or v.reset7 <= now:
        return None
    return (v.reset7 - now) / HOUR_S


def rule(v: AccountView, snap: Snapshot) -> Rule | None:
    """Why ``v`` is draining (``hours`` or ``headroom``), or None when it is
    not: drain off (``drainHours`` 0), 7d or its reset unknown, or nothing
    left under the 7d hard cap."""
    s = snap.settings
    if s.drain_hours <= 0 or v.pct7 is None:
        return None
    left = hours_left(v, snap.now)
    if left is None:
        return None
    room = s.hard_7d - v.pct7
    if room <= 0:
        return None
    if left <= s.drain_hours:
        return "hours"
    k, _ = k_for(v, snap.k7)
    if room >= HEADROOM_SHARE * (left / WINDOW_H) * k * 100.0:
        return "headroom"
    return None


def draining(v: AccountView, snap: Snapshot) -> bool:
    return rule(v, snap) is not None


# -- words ------------------------------------------------------------------------------


def left_text(hours: float) -> str:
    """``40m`` / ``18h`` / ``3d``: the time to a 7d reset, short."""
    if hours < 1:
        return f"{max(1, round(hours * 60))}m"
    if hours < 48:
        return f"{round(hours)}h"
    return f"{round(hours / 24)}d"


def text(v: AccountView, snap: Snapshot) -> str:
    """``#1 7d 86% resets in 18h — draining it first`` (a draining ``v``)."""
    left = hours_left(v, snap.now)
    when = f"resets in {left_text(left)}" if left is not None else "resets soon"
    pct = f"{v.pct7:g}%" if v.pct7 is not None else "?"
    return f"#{v.number} 7d {pct} {when} — draining it first"


def tag(hours: float) -> str:
    """Fleet's status tag: ``drain 18h``."""
    return f"drain {left_text(hours)}"


def now_draining(snap: Snapshot) -> list[tuple[str, float]]:
    """``[(slot, hours to its 7d reset), ...]`` for every draining account,
    the earliest reset first."""
    out = [
        (v.number, hours_left(v, snap.now) or 0.0)
        for v in snap.accounts
        if draining(v, snap)
    ]
    return sorted(out, key=lambda item: item[1])


def describe(
    drain_hours: int,
    k7: Mapping[str, float],
    draining_now: Sequence[tuple[str, float]] = (),
) -> str:
    """One line for doctor and ``cc-swap why``: ``7d drain: within 24h of a
    7d reset · draining #1 (18h) · k learned #2 0.167; others by plan (20x
    0.165, 5x 0.105)``."""
    if drain_hours <= 0:
        return "7d drain: off (maximize.drainHours is 0)"
    parts = [f"within {drain_hours}h of a 7d reset"]
    if draining_now:
        parts.append("draining " + ", ".join(
            f"#{number} ({left_text(hours)})" for number, hours in draining_now
        ))
    plans = f"(20x {K_20X:g}, 5x {K_5X:g})"
    learned = sorted(k7.items(), key=lambda item: (len(item[0]), item[0]))
    if learned:
        parts.append(
            "k learned " + ", ".join(f"#{n} {k:.3f}" for n, k in learned)
            + f"; others by plan {plans}"
        )
    else:
        parts.append(f"k by plan {plans} until learned")
    return "7d drain: " + " · ".join(parts)
