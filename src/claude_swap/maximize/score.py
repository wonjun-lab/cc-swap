"""Score, landing eligibility and ranking (spec §5.2-§5.4). Pure functions."""

from __future__ import annotations

import math
from collections.abc import Sequence

from claude_swap.maximize.model import TIER_ORDER, AccountView
from claude_swap.settings import MaximizeSettings

DAY_S = 86400.0
# Floor on days-to-reset: one hour. Without it a reset seconds away divides by
# ~0 and any scrap of weekly quota outranks everything (spec §5.3).
MIN_DAYS_LEFT = 1.0 / 24.0
UNKNOWN_RESET_DAYS = 7.0


def days_left(view: AccountView, now: float) -> float:
    """Days until the 7d reset, floored at one hour; 7 when unknown."""
    if view.reset7 is None:
        return UNKNOWN_RESET_DAYS
    return max((view.reset7 - now) / DAY_S, MIN_DAYS_LEFT)


def score(view: AccountView, now: float) -> float:
    """Remaining 7d share over an even daily allotment; >1 means quota would
    be left over at the reset at an even pace. ``-inf`` when 7d is unknown."""
    if view.pct7 is None:
        return -math.inf
    return (100.0 - view.pct7) / (days_left(view, now) * 100.0 / 7.0)


def landable(view: AccountView, s: MaximizeSettings, *, drain_room: bool = False) -> bool:
    """Spec §5.2: a healthy, known, eligible place to land. ``drain_room``:
    a draining account with useful room (``drain.preferred``, near its 7d
    reset) has its 7d soft mark set aside: its 7d need only be under the
    hard cap less the margin."""
    if view.tier == "excluded" or view.quarantined or view.api_key:
        return False
    if view.pct5 is None or view.pct7 is None:
        return False
    soft7 = s.hard_7d if drain_room else s.soft_7d
    return (
        view.pct5 < s.soft_5h - s.landing_margin
        and view.pct7 < soft7 - s.landing_margin
    )


def below_hard(view: AccountView, s: MaximizeSettings) -> bool:
    """Both windows known and under their hard caps."""
    if view.pct5 is None or view.pct7 is None:
        return False
    return view.pct5 < s.hard_5h and view.pct7 < s.hard_7d


def window_off(view: AccountView, now: float) -> bool:
    """The 5h window is not running: never started, or its reset has passed."""
    return view.reset5 is None or view.reset5 <= now


def slot_order(view: AccountView) -> int:
    try:
        return int(view.number)
    except ValueError:
        return 1 << 30


def _tie_key(view: AccountView, now: float) -> tuple:
    reset5 = math.inf if window_off(view, now) else view.reset5
    return (-view.plan_weight, reset5, slot_order(view))


def rank(
    views: Sequence[AccountView], now: float, eps: float
) -> list[AccountView]:
    """Spec §5.4: tier, then score descending in eps-wide tie groups, then
    plan weight (20x first), sooner running 5h reset (off windows last), slot.

    A tie group is anchored on its highest score: a member joins while it is
    within ``eps`` of that anchor, so ties never chain past ``eps`` in total.
    """
    out: list[AccountView] = []
    for tier in sorted(TIER_ORDER, key=TIER_ORDER.__getitem__):
        members = sorted(
            (v for v in views if v.tier == tier),
            key=lambda v: (-score(v, now), slot_order(v)),
        )
        i = 0
        while i < len(members):
            anchor = score(members[i], now)
            j = i + 1
            while j < len(members):
                gap = anchor - score(members[j], now)
                if not (gap <= eps):  # nan (-inf vs -inf) ends the group too
                    break
                j += 1
            out.extend(sorted(members[i:j], key=lambda v: _tie_key(v, now)))
            i = j
    return out
