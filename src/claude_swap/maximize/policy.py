"""The maximize policy: ``decide(Snapshot) -> Decision`` (spec §5.5). Pure.

Triggers, first match wins:

1. at-limit   active 5h or 7d at 100%                  busy or not
2. hard       a hard cap reached, or reached within
              ``force_eta_min`` at the recent burn rate  busy or not
3. soft       a soft threshold reached                  waits for idle
4. preempt    the active 7d is on pace to pass soft7d
              before the next quiet window, and a
              landable account is not                   idle + cooldown
5. rebalance  (a) active is excluded/last_resort and a
              higher tier can land, or (b) the same
              tier's best beats the active by > eps     idle + cooldown

Preempt and rebalance (b) read the usage history (``Snapshot.rates7``,
``Snapshot.forecast``; maximize/history.py). Preempt projects at each
account's 7d burn rate (pct/hour while active): a target's own, but never
below the active's scaled to the target's plan, since your usage moves with
you. Its horizon is the time to the next quiet window, capped at
``preemptHorizonMaxH`` (``NO_PATTERN_HORIZON_H`` with no pattern). In a
usually-busy slot (P(busy) >= ``history.QUIET_P``, not inside a quiet window)
a rebalance (b) gaining less than ``busyRebalanceGap`` waits for a quiet
window starting within ``DEFER_WITHIN_S``. With preempt on, rebalance (b)
skips a candidate preempt would move off again (its 7d passing soft7d
within the horizon, :func:`crosses_soft7_within`): otherwise the two
triggers bounce between the same accounts every ``rebalanceCooldownMin``.

The destination is always the top of ``landing_candidates``. With none:

* at-limit: any eligible account under both hard caps (score order), then
  any eligible account under 100% on both windows (most room first), else
  ``Exhausted``.
* hard: an eligible account under both hard caps with strictly more room
  than the active on every window that forced the switch (most room
  first); else ``Hold`` with code ``hard-stay`` — the active is still
  under 100% (at 100% the at-limit trigger wins), and moving to less room
  would bounce straight back.
* soft/rebalance: ``Hold``.

Reset-aware wait (``resetWaitMin``, 0 = off): a hard or soft trigger whose
window resets within ``resetWaitMin`` minutes, and whose recent pace reaches
100% no sooner than ``RESET_WAIT_MARGIN_MIN`` after that reset, counted
from now rather than from the newest sample (with no pace known: still
under its hard cap; never past the hard cap while the active token had a
recent 429), is waited out instead — the reset clears
the reason to switch, and a switch costs a full context re-read on the new
account. The other window's triggers still apply; with none left the
decision is a ``Hold`` carrying ``reset_wait_until``, which the at-limit
trigger ends at 100%. At-limit and rebalance never wait.

Account hold (``Snapshot.hold_until``, maximize/hold.py): while the user
pins the active account, the soft, preempt and rebalance triggers are set
aside — the decision is a ``Hold`` with code ``hold`` whose reason also says
what would have happened. At-limit, hard (reached or ETA-forced) and the
reset-aware wait decide exactly as without a hold: safety always wins.

Learned ride (``learnedRide``, ``rideWindows``, ``rideMaxMin``;
maximize/ride.py): usage is reported in whole percents, floored, so a hard
mark in the last point (99 or more) fires with up to a whole point left.
When every window that reached its hard mark is listed in ``rideWindows``
and still under 100%, the hard switch waits until

    t_switch = arm time + q × T1 − ``RIDE_MARGIN_S``

(at most ``rideMaxMin`` after the arm time). The arm time is the reading
before the first one at the mark (the crossing may have come right after
it), else ``ride.ARM_UNKNOWN_GAP_S`` before that first one
(``ride.arm_time``, ``Snapshot.ride_armed_at``). ``T1`` is the time one
point takes (the shorter of ``Snapshot.ride_point_s``, measured from
whole-point steps, and the recent velocity's; unknown = no ride), ``q``
the learned share
(``Snapshot.ride_q``). Until then the decision is a ``Hold`` with code
``ride``, unless the account goes idle (the cheapest moment to switch: a
hard switch at once, ``Switch.ride == "idle"``) or every ridden window
resets first (a ``reset-wait``). At ``t_switch`` it is the hard switch
(``Switch.ride == "due"``). 100% is the at-limit trigger as always, a
recent 429 on the active token never rides (it cannot be polled every
60 s), an ETA-forced hard trigger never rides, and an account hold sets
none of it aside: the ride is the hard path, only later.

Near-reset drain (``drainHours``, maximize/drain.py): an account whose 7d
reset is close (within ``drainHours``, or so close that what is left under
``hard7d`` needs most of the 5h windows to the reset) is *draining*. Its 7d
soft mark is set aside: no ``soft`` or ``preempt`` move off it on 7d, and,
with useful room (``drain.preferred``), it is landable while its 7d is under
``hard7d − landingMargin`` (with less, by the normal rule). Within a tier
``landing_candidates`` puts a draining account with useful room
(``drain.preferred``: at least a quarter 5h window before a mark) first,
the earliest 7d reset first (:func:`drain_first`); one with less competes
on its score. Preempt takes a draining target only when its 7d would not
reach ``hard7d`` within the horizon either. Rebalance (b) tries the first
such draining candidate before the best other one, and never moves off a
draining active account except to such a one whose 7d resets sooner (so it
cannot bounce back). Hard caps, at-limit, the login-expiry guard, last resort,
quarantine and holds are unchanged. With no draining account every decision
is what it was without the drain.

Unknown active usage is ``Indeterminate`` (the engine's upstream failover
path counts it).

Estimated active usage (``Snapshot.estimate``, maximize/estimate.py): when
the active account's reading is too old (its reads 429 or fail), the engine
hands the policy a *projection* (last reading + burn rate × elapsed), or
100% on a window Claude Code reported a usage-limit refusal for. Every
trigger runs on it as on a reading. While it is a projection the samples
have stopped, so the ETA-forced hard trigger and the reset-aware wait run
at the projection's rates; idle is "no Claude Code transcript written on
this machine within ``idleWindowMin``" (``Snapshot.local_idle``) when that
is known; no learned ride starts (it needs 60 s readings). Every reason
then says so: ``(5h ~74% projected — usage reads rate-limited for 52m)``.

Stale landing (``STALE_LANDING_S``): a non-active account whose reading is
older than that is no landing target (soft, preempt, rebalance, and the
first choice of hard/at-limit); the hard/at-limit fallbacks still take
one, after every account read more recently. Its usage may have moved on
another machine since.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from claude_swap import poll_policy
from claude_swap.maximize import drain
from claude_swap.maximize import hold as account_hold
from claude_swap.maximize import idle
from claude_swap.maximize import ride as learned_ride
from claude_swap.maximize.history import QUIET_P
from claude_swap.maximize.model import (
    TIER_ORDER,
    AccountView,
    Decision,
    Exhausted,
    Hold,
    Indeterminate,
    QuietWindow,
    Snapshot,
    Switch,
)
from claude_swap.maximize.score import below_hard, landable, rank, score, slot_order

Window = Literal["5h", "7d"]
# Utilization at which a window is spent (the at-limit trigger).
LIMIT_PCT = 100.0
# The reset-aware wait holds only while the recent pace reaches 100% at
# least this many minutes after the window resets.
RESET_WAIT_MARGIN_MIN = 2.0
# Preempt's horizon while no idle pattern is learned (cold start, or off).
NO_PATTERN_HORIZON_H = 4.0
# A rebalance in a busy slot waits only for a quiet window this close.
DEFER_WITHIN_S = 6 * 3600.0
# The learned ride only rides a hard mark in the last whole point.
RIDE_FLOOR_PCT = LIMIT_PCT - 1.0
# A ride switches this long before its learned end: one urgent poll
# interval (the reading that would show 100% can be that old) plus 30 s.
RIDE_MARGIN_S = poll_policy.URGENT_INTERVAL_S + 30.0
# A non-active account read longer ago than this is no landing target
# unless nothing else can take you (its usage may have moved elsewhere, on
# another machine). Just past the slowest cadence a candidate is ever
# planned at (poll_policy.POST_429_MAX_INTERVAL_S, 30 min, plus jitter and
# a tick), so a reading on the scheduler's own plan still lands.
STALE_LANDING_S = 2100.0


def stale_reading(v: AccountView) -> bool:
    """``v``'s reading is older than :data:`STALE_LANDING_S`."""
    return v.age_s is not None and v.age_s > STALE_LANDING_S


def _fresh_first(views: list[AccountView]) -> list[AccountView]:
    """``views`` with the stale readings moved to the end (stable)."""
    return sorted(views, key=stale_reading)


def is_idle(snap: Snapshot) -> bool:
    """Whether now is an idle moment: the samples' verdict, or, while the
    active usage is projected (no samples come in), whether Claude Code
    wrote no transcript here within ``idleWindowMin`` (``local_idle``)."""
    if (
        snap.estimate is not None
        and snap.estimate.kind == "projected"
        and snap.local_idle is not None
    ):
        return snap.local_idle
    return idle.is_idle(snap.samples, snap.now, snap.settings)


def _pct(value: float) -> str:
    return f"{value:g}%"


def _usage(v: AccountView) -> str:
    return f"5h {_pct(v.pct5)} / 7d {_pct(v.pct7)}"


def login_guarded(v: AccountView, now: float, s) -> bool:
    """The account's login expires within ``loginExpiryGuardMin`` (or has
    already): landing there now would strand the user on a login about to
    die. Unknown deadlines are never guarded."""
    if v.login_deadline is None:
        return False
    return v.login_deadline - now < s.login_expiry_guard_min * 60.0


def login_lapsed(v: AccountView, now: float) -> bool:
    """Past its recorded login deadline: the next refresh is refused, so no
    switch — not even a forced fallback — should land there."""
    return v.login_deadline is not None and now >= v.login_deadline


def draining(v: AccountView, snap: Snapshot) -> bool:
    """``v`` is near its 7d reset and its 7d soft mark is set aside
    (maximize/drain.py)."""
    return drain.draining(v, snap)


def can_land(v: AccountView, snap: Snapshot) -> bool:
    """``score.landable`` with the drain's 7d limit (``hard7d − margin``)
    for a draining ``v`` with useful room (``drain.preferred``); a draining
    account with less lands by the normal rule (``soft7d − margin``)."""
    return landable(v, snap.settings, drain_room=drain.preferred(v, snap))


def drain_first(ranked: list[AccountView], snap: Snapshot) -> list[AccountView]:
    """``ranked`` (``rank`` order) with the draining accounts that have
    useful room (``drain.preferred``) first within each tier, the earliest
    7d reset first. Stable: with none it is ``ranked`` unchanged, and the
    rest (a draining account with little room included) keep their order."""
    def key(v: AccountView) -> tuple:
        if drain.preferred(v, snap):
            return (TIER_ORDER[v.tier], 0, v.reset7 if v.reset7 is not None else math.inf)
        return (TIER_ORDER[v.tier], 1, 0.0)

    return sorted(ranked, key=key)


def landing_candidates(snap: Snapshot) -> list[AccountView]:
    """Every non-active landable account, best first (spec §5.2 + §5.4),
    draining accounts first within a tier (:func:`drain_first`).

    An account inside its login-expiry guard is no landing target (soft,
    rebalance, and the first choice of hard/at-limit); the hard/at-limit
    fallbacks (``escape_candidates``, ``limit_candidates``) still take it."""
    s = snap.settings
    return drain_first(
        rank(
            [
                v
                for v in snap.accounts
                if v.number != snap.active
                and can_land(v, snap)
                and not login_guarded(v, snap.now, s)
                and not stale_reading(v)
            ],
            snap.now,
            s.tie_epsilon,
        ),
        snap,
    )


def escape_candidates(snap: Snapshot) -> list[AccountView]:
    """The at-limit/hard fallback pool: eligible accounts under both hard
    caps, in §5.4 order, a stale reading (:func:`stale_reading`) after
    every fresher one."""
    s = snap.settings
    return _fresh_first(rank(
        [
            v
            for v in snap.accounts
            if v.number != snap.active
            and v.tier != "excluded"
            and not v.quarantined
            and not v.api_key
            and not login_lapsed(v, snap.now)
            and below_hard(v, s)
        ],
        snap.now,
        s.tie_epsilon,
    ))


def hard_room(v: AccountView, window: Window, snap: Snapshot) -> float:
    """Points left on ``window`` before its hard cap (negative when over)."""
    s = snap.settings
    return s.hard_5h - v.pct5 if window == "5h" else s.hard_7d - v.pct7


def roomier_candidates(
    snap: Snapshot, active: AccountView, windows: tuple[Window, ...]
) -> list[AccountView]:
    """The hard fallback: ``escape_candidates`` with strictly more room than
    the active on every window in ``windows``, most room first.

    Room is what a forced move buys; a target with less of it on the window
    that forced the move reaches its own cap sooner and forces the move
    straight back. Room on several forcing windows is the tighter of them;
    equal room keeps the §5.4 order.
    """

    def room(v: AccountView) -> float:
        return min(hard_room(v, w, snap) for w in windows)

    out = [
        v
        for v in escape_candidates(snap)
        if all(hard_room(v, w, snap) > hard_room(active, w, snap) for w in windows)
    ]
    return sorted(out, key=lambda v: (stale_reading(v), -room(v)))


def binding_room(v: AccountView) -> float:
    """Points left before the account's fuller window hits the limit."""
    return LIMIT_PCT - max(v.pct5, v.pct7)


def binding_recovery(v: AccountView, now: float) -> float:
    """Reset of the fuller window (5h on a tie); ``inf`` when unknown/past.

    Mirrors upstream ``_binding_recovery_ts``: pick the binding window
    first, then ask for its reset.
    """
    reset = v.reset5 if v.pct5 >= v.pct7 else v.reset7
    return reset if reset is not None and reset > now else math.inf


def limit_candidates(snap: Snapshot) -> list[AccountView]:
    """The at-limit last resort: eligible accounts under 100% on both windows.

    Over a hard cap is still quota; with nothing under the hard caps an
    at-limit account is better off anywhere that has some left. Most binding
    room first, then the sooner binding reset, then slot.
    """
    out = [
        v
        for v in snap.accounts
        if v.number != snap.active
        and v.tier != "excluded"
        and not v.quarantined
        and not v.api_key
        and not login_lapsed(v, snap.now)
        and v.pct5 is not None
        and v.pct7 is not None
        and v.pct5 < LIMIT_PCT
        and v.pct7 < LIMIT_PCT
    ]
    return sorted(
        out,
        key=lambda v: (
            stale_reading(v), -binding_room(v), binding_recovery(v, snap.now), slot_order(v)
        ),
    )


def idle_note(snap: Snapshot) -> str:
    """Human summary of the idle evidence, for reasons and dry-run output."""
    if (
        snap.estimate is not None
        and snap.estimate.kind == "projected"
        and snap.local_idle is not None
    ):
        window = snap.settings.idle_window_min
        if snap.local_idle:
            return f"no Claude Code activity here in {window} min"
        return f"Claude Code active here within {window} min"
    span = idle.idle_span(snap.samples, snap.now, snap.settings)
    if span is None:
        window = snap.settings.idle_window_min
        if snap.samples and snap.now - snap.samples[-1].ts > window * 60.0:
            return f"no sample in the last {window} min"
        return f"need samples {window} min apart"
    d5, d7 = idle.span_rise(span)
    return (
        f"5h {d5:+g} / 7d {d7:+g} pts "
        f"over {(span[-1].ts - span[0].ts) / 60:.0f} min"
    )


def _target(v: AccountView, snap: Snapshot) -> str:
    out = f"#{v.number} ({v.tier}, score {score(v, snap.now):.2f})"
    if draining(v, snap):
        out += f": {drain.text(v, snap)}"
    return out


@dataclass(frozen=True)
class _Force:
    """Why the hard (or urgent) trigger fired, and on which window(s)."""

    reason: str
    windows: tuple[Window, ...]
    # The caps are reached (not merely close at the recent pace).
    reached: bool = False


def _fresh_samples(snap: Snapshot) -> bool:
    """The newest sample is recent enough to say anything about the pace."""
    return bool(snap.samples) and (
        snap.now - snap.samples[-1].ts <= snap.settings.idle_window_min * 60.0
    )


def _reached(snap: Snapshot, a: AccountView) -> tuple[Window, ...]:
    """The windows at or over their hard cap."""
    s = snap.settings
    return tuple(
        w
        for w, pct, cap in (("5h", a.pct5, s.hard_5h), ("7d", a.pct7, s.hard_7d))
        if pct >= cap
    )


def _projected(snap: Snapshot) -> bool:
    return snap.estimate is not None and snap.estimate.kind == "projected"


def _per_min(rates: object) -> tuple[float | None, float | None]:
    if not isinstance(rates, Mapping):
        return None, None
    out: list[float | None] = []
    for w in ("5h", "7d"):
        rate = rates.get(w)
        out.append(
            float(rate) / 60.0
            if isinstance(rate, (int, float)) and not isinstance(rate, bool)
            and math.isfinite(rate)
            else None
        )
    return out[0], out[1]


def pace(snap: Snapshot) -> tuple[tuple[float | None, float | None], float]:
    """``((5h, 7d) points per minute, minutes since the pace's reading)``:
    the recent velocity of fresh samples (as old as the newest sample);
    else, while the usage is projected, the projection's rates (the learned
    in-use pace, else the plan's default; projected to now); else unknown.
    Fresh readings with too few samples for a velocity (the first ticks
    after a start or a switch) keep "unknown": no ETA from a guess while
    the readings themselves are current."""
    if _fresh_samples(snap):
        velocity = idle.velocity(snap.samples, snap.settings)
        if velocity != (None, None):
            age = max(snap.now - snap.samples[-1].ts, 0.0) / 60.0
            return velocity, age
    if _projected(snap):
        return _per_min(snap.estimate.rates), 0.0  # type: ignore[union-attr]
    return (None, None), 0.0


def _eta_forced(snap: Snapshot) -> dict[Window, float]:
    """``{window: minutes}`` for each window whose pace (:func:`pace`)
    reaches its hard cap within ``force_eta_min``. Measured from the
    newest sample when the samples give the pace, else from the reading."""
    s = snap.settings
    if s.force_eta_min <= 0:
        return {}
    if _fresh_samples(snap):
        eta5, eta7 = idle.eta_to_hard(snap.samples, s)
        if (eta5, eta7) != (None, None) or idle.velocity(snap.samples, s) != (None, None):
            return {
                w: eta
                for w, eta in (("5h", eta5), ("7d", eta7))
                if eta is not None and eta <= s.force_eta_min
            }
    a = snap.view(snap.active)
    if a is None or a.pct5 is None or a.pct7 is None:
        return {}
    (r5, r7), _age = pace(snap)
    out: dict[Window, float] = {}
    for w, pct, cap, rate in (("5h", a.pct5, s.hard_5h, r5), ("7d", a.pct7, s.hard_7d, r7)):
        if rate is not None and rate > 0:
            eta = max(cap - pct, 0.0) / rate
            if eta <= s.force_eta_min:
                out[w] = eta
    return out


def _force(
    snap: Snapshot,
    a: AccountView,
    reached: tuple[Window, ...],
    forced: dict[Window, float],
) -> _Force | None:
    """The hard trigger on these windows: a reached cap wins over a pace."""
    s = snap.settings
    if reached:
        if reached[0] == "5h":
            why = f"#{a.number} 5h {_pct(a.pct5)} >= hard {_pct(s.hard_5h)}"
        else:
            why = f"#{a.number} 7d {_pct(a.pct7)} >= hard {_pct(s.hard_7d)}"
        return _Force(why, reached, reached=True)
    if forced:
        eta = min(forced.values())
        return _Force(
            f"#{a.number} reaches a hard cap in ~{eta:.1f} min "
            f"(<= {s.force_eta_min} min)",
            tuple(forced),
        )
    return None


def _hard_force(snap: Snapshot, a: AccountView) -> _Force | None:
    reached = _reached(snap, a)
    return _force(snap, a, reached, {} if reached else _eta_forced(snap))


# -- the learned ride -------------------------------------------------------------------


def ride_windows(s) -> tuple[Window, ...]:
    """The windows the learned ride may apply to (``rideWindows``); none
    while ``learnedRide`` is off or ``rideMaxMin`` is 0."""
    if not s.learned_ride or s.ride_max_min <= 0:
        return ()
    listed = {part.strip() for part in str(s.ride_windows or "").split(",")}
    return tuple(w for w in ("5h", "7d") if w in listed)


def ride_text(learning: object, s) -> str:
    """What the learned ride has learned (the ``rideLearning`` record) under
    settings ``s``, for doctor and ``cc-swap why``."""
    off = None
    if not s.learned_ride:
        off = "maximize.learnedRide is false"
    elif s.ride_max_min <= 0:
        off = "maximize.rideMaxMin is 0"
    return learned_ride.describe(learning, ride_windows(s), off)


def _hard_cap(s, window: Window) -> float:
    return s.hard_5h if window == "5h" else s.hard_7d


def rideable(snap: Snapshot, a: AccountView, window: Window) -> bool:
    """``window`` is listed in ``rideWindows``, its hard mark is in the last
    whole point, and it reads at that mark but under 100%."""
    s = snap.settings
    cap = _hard_cap(s, window)
    pct = _window_pct(a, window)
    return (
        window in ride_windows(s)
        and cap >= RIDE_FLOOR_PCT
        and pct is not None
        and cap <= pct < LIMIT_PCT
    )


def ride_point_s(snap: Snapshot, window: Window) -> float | None:
    """``T1``, seconds per point on ``window``: the shorter of the engine's
    (``Snapshot.ride_point_s``, measured steps) and the recent velocity's,
    whichever are known; None when neither is (or the window is not
    climbing). Shorter is safer: a T1 too long rides into 100%."""
    out: list[float] = []
    known = snap.ride_point_s.get(window)
    if known is not None and math.isfinite(known) and known > 0:
        out.append(float(known))
    if _fresh_samples(snap):
        v5, v7 = idle.velocity(snap.samples, snap.settings)
        rate = v5 if window == "5h" else v7
        if rate is not None and rate > 0:
            out.append(60.0 / rate)
    return min(out) if out else None


def ride_armed_at(snap: Snapshot, window: Window) -> float:
    """When ``window``'s ride counts from: the engine's record
    (``Snapshot.ride_armed_at``), else read off the samples as the engine
    reads it (``ride.arm_time``): the last sample below the mark before
    the newest run at it, else ``ride.ARM_UNKNOWN_GAP_S`` before the run's
    first sample (or ``now``)."""
    armed = snap.ride_armed_at.get(window)
    if armed is not None and math.isfinite(armed):
        return min(float(armed), snap.now)
    cap = _hard_cap(snap.settings, window)
    first = snap.now
    for x in reversed(snap.samples):
        pct = x.pct5 if window == "5h" else x.pct7
        if not cap <= pct < LIMIT_PCT:
            return learned_ride.arm_time(first, x.ts)
        first = min(first, x.ts)
    return learned_ride.arm_time(first, None)


@dataclass(frozen=True)
class RidePlan:
    """One window's ride: until when (``t_switch``), and from what."""

    until: float
    capped: bool          # ``rideMaxMin`` ends it before the learned share
    q: float
    point_s: float
    armed_at: float


def ride_plan(snap: Snapshot, window: Window) -> RidePlan | None:
    """``window``'s ride, or None when ``T1`` is unknown (no ride)."""
    point = ride_point_s(snap, window)
    if point is None:
        return None
    q = learned_ride.clamp_q(float(snap.ride_q.get(window, learned_ride.Q_DEFAULT)))
    armed = ride_armed_at(snap, window)
    learned_until = armed + q * point - RIDE_MARGIN_S
    cap_until = armed + snap.settings.ride_max_min * 60.0
    return RidePlan(
        until=min(learned_until, cap_until),
        capped=cap_until < learned_until,
        q=q,
        point_s=point,
        armed_at=armed,
    )


def _ride_reset_wait(
    snap: Snapshot, a: AccountView, windows: tuple[Window, ...], until: float
) -> Hold | None:
    """Every ridden window resets before the ride ends: wait the reset out
    (worded as ``_reset_wait`` words it)."""
    resets = [_window_reset(a, w) for w in windows]
    if not all(r is not None and snap.now < r <= until for r in resets):
        return None
    held = ", ".join(
        f"{w} {_pct(_window_pct(a, w))} — resets in {max(1, round((r - snap.now) / 60.0))}m"
        for w, r in zip(windows, resets)
        if r is not None
    )
    return Hold(
        f"#{a.number} {held}, waiting it out (switches at once if it hits 100%)",
        pending=False,
        reset_wait_until=max(r for r in resets if r is not None),
        code="reset-wait",
    )


def _hard_or_ride(
    snap: Snapshot, a: AccountView, landing: list[AccountView], force: _Force
) -> Decision:
    """The hard decision, or the learned ride that delays its switch."""
    base = _hard(snap, a, landing, force)
    if not isinstance(base, Switch) or not force.reached:
        return base
    windows = force.windows
    if not all(rideable(snap, a, w) for w in windows):
        return base
    if any(w not in windows for w in _eta_forced(snap)):
        return base  # another window is about to force the switch anyway
    if snap.active_recent_429:
        return replace(base, reason=f"{base.reason}; no ride after a recent 429")
    if snap.estimate is not None:
        return replace(base, reason=f"{base.reason}; no ride on estimated usage")
    plans = [ride_plan(snap, w) for w in windows]
    if any(p is None for p in plans):
        return replace(base, reason=f"{base.reason}; no ride (pace unknown)")
    until = min(p.until for p in plans if p is not None)
    # ``rideMaxMin`` ends the ride before its learned share: the switch
    # then says nothing about q (``Switch.ride_capped``, nothing learned).
    capped = any(p.capped and p.until == until for p in plans if p is not None)
    if snap.now >= until:
        over = "capped by rideMaxMin" if capped else "over"
        return replace(
            base, reason=f"{base.reason}; learned ride {over}",
            ride="due", ride_windows=windows, ride_capped=capped,
        )
    waited = _ride_reset_wait(snap, a, windows, until)
    if waited is not None:
        return waited
    if is_idle(snap):
        return replace(
            base, reason=f"{base.reason}; idle during the learned ride",
            ride="idle", ride_windows=windows,
        )
    label = " / ".join(f"{w} {_pct(_window_pct(a, w))}" for w in windows)
    return Hold(
        f"#{a.number} {label} — riding to the limit, switching in "
        f"~{max(1, round((until - snap.now) / 60.0))}m "
        f"({'capped' if capped else 'learned'}) or at your next pause",
        pending=False,
        code="ride",
        ride_until=until,
        ride_windows=windows,
    )


def _soft_reason(
    a: AccountView, snap: Snapshot, skip: tuple[Window, ...] = ()
) -> str | None:
    s = snap.settings
    if "5h" not in skip and a.pct5 >= s.soft_5h:
        return f"#{a.number} 5h {_pct(a.pct5)} >= soft {_pct(s.soft_5h)}"
    if "7d" not in skip and a.pct7 >= s.soft_7d and not draining(a, snap):
        return f"#{a.number} 7d {_pct(a.pct7)} >= soft {_pct(s.soft_7d)}"
    return None


def _window_pct(v: AccountView, window: Window) -> float:
    return v.pct5 if window == "5h" else v.pct7


def _window_reset(v: AccountView, window: Window) -> float | None:
    return v.reset5 if window == "5h" else v.reset7


def reset_wait_left(
    snap: Snapshot,
    a: AccountView,
    window: Window,
    rate: float | None,
    age: float | None = None,
) -> float | None:
    """Minutes until ``window`` resets if the reset-aware wait may hold for
    it, else None.

    It may when the reset is at most ``reset_wait_min`` minutes away and
    the pace (``rate``, points per minute) reaches 100% no sooner than
    ``RESET_WAIT_MARGIN_MIN`` after it. The projection starts at the newest
    sample, which may be minutes old (a sample counts as fresh for
    ``idleWindowMin``), so its age comes off the minutes to 100%. With no
    pace to project (unknown, or not climbing), only a window still under
    its hard cap waits. A window at or over its hard cap never waits while
    the active token had a recent 429: the wait leans on polling every 60 s
    to catch a climb to 100%, and that token keeps the post-429 cadence.
    """
    s = snap.settings
    reset = _window_reset(a, window)
    if s.reset_wait_min <= 0 or reset is None or reset <= snap.now:
        return None
    left = (reset - snap.now) / 60.0
    if left > s.reset_wait_min:
        return None
    pct = _window_pct(a, window)
    cap = s.hard_5h if window == "5h" else s.hard_7d
    if pct >= cap and snap.active_recent_429:
        return None
    if rate is None or rate <= 0:
        return left if pct < cap else None
    if age is None:
        age = max(snap.now - snap.samples[-1].ts, 0.0) / 60.0 if snap.samples else 0.0
    to_limit = max(LIMIT_PCT - pct, 0.0) / rate - age
    return left if to_limit >= left + RESET_WAIT_MARGIN_MIN else None


def _reset_wait(
    snap: Snapshot, a: AccountView, landing: list[AccountView]
) -> Decision | None:
    """The hard/soft decision with every window that may wait out its reset
    set aside, or None when no triggering window may (decide as usual).

    A window that is set aside triggers nothing; the other window's hard or
    soft trigger still decides. With none left, hold until the reset.
    """
    s = snap.settings
    if s.reset_wait_min <= 0:
        return None
    reached = _reached(snap, a)
    forced = _eta_forced(snap)
    soft = tuple(
        w
        for w, pct, mark in (("5h", a.pct5, s.soft_5h), ("7d", a.pct7, s.soft_7d))
        if pct >= mark and not (w == "7d" and draining(a, snap))
    )
    rates, age = pace(snap)
    waits: dict[Window, float] = {}
    for w, rate in zip(("5h", "7d"), rates):
        if w in reached or w in forced or w in soft:
            left = reset_wait_left(snap, a, w, rate, age)
            if left is not None:
                waits[w] = left
    if not waits:
        return None
    force = _force(
        snap,
        a,
        tuple(w for w in reached if w not in waits),
        {w: eta for w, eta in forced.items() if w not in waits},
    )
    if force is not None:
        return _hard_or_ride(snap, a, landing, force)
    other = _soft_reason(a, snap, skip=tuple(waits))
    if other is not None:
        return _held(snap, a, _soft(snap, landing, other))
    held = ", ".join(
        f"{w} {_pct(_window_pct(a, w))} — resets in {max(1, round(left))}m"
        for w, left in waits.items()
    )
    return Hold(
        f"#{a.number} {held}, waiting it out (switches at once if it hits 100%)",
        pending=False,
        reset_wait_until=max(
            r for r in (_window_reset(a, w) for w in waits) if r is not None
        ),
        code="reset-wait",
    )


def _at_limit(snap: Snapshot, landing: list[AccountView], why: str) -> Decision:
    if landing:
        top = landing[0]
        return Switch(top.number, "at-limit", f"{why}; -> {_target(top, snap)}")
    fallback = escape_candidates(snap)
    if fallback:
        top = fallback[0]
        return Switch(
            top.number,
            "at-limit",
            f"{why}; nothing landable, #{top.number} is under the hard caps "
            f"({_usage(top)})",
        )
    last = limit_candidates(snap)
    if last:
        top = last[0]
        return Switch(
            top.number,
            "at-limit",
            f"{why}; nothing under the hard caps, #{top.number} has "
            f"{binding_room(top):g} pts left ({_usage(top)})",
        )
    return Exhausted(f"{why}; every account is at its limit")


def _hard(
    snap: Snapshot, a: AccountView, landing: list[AccountView], force: _Force
) -> Decision:
    if landing:
        top = landing[0]
        return Switch(top.number, "hard", f"{force.reason}; -> {_target(top, snap)}")
    windows = "/".join(force.windows)
    fallback = roomier_candidates(snap, a, force.windows)
    if fallback:
        top = fallback[0]
        return Switch(
            top.number,
            "hard",
            f"{force.reason}; nothing landable, #{top.number} has the most "
            f"{windows} room under the hard caps ({_usage(top)})",
        )
    # Every reachable account would hit a cap no later than the active: stay
    # on it while it lasts. At 100% the at-limit trigger takes over. A hard
    # decision of its own (``hard-stay``): an account hold never words it.
    return Hold(
        f"{force.reason}; nothing landable and no account under the hard caps "
        f"has more {windows} room than #{a.number}; staying",
        pending=False,
        code="hard-stay",
    )


def _soft(snap: Snapshot, landing: list[AccountView], why: str) -> Decision:
    if not landing:
        return Hold(f"{why}; nothing landable", pending=False)
    top = landing[0]
    if is_idle(snap):
        return Switch(top.number, "soft", f"{why}; idle; -> {_target(top, snap)}")
    return Hold(
        f"{why}; waiting for idle to move to #{top.number} ({idle_note(snap)})",
        pending=True,
    )


def _cooldown_left(snap: Snapshot) -> float | None:
    """Seconds of the rebalance cooldown left, or None when it is over.

    It runs from the later of the last engine switch and the last change of
    active account: a manual switch restarts it too."""
    since = max(
        (t for t in (snap.last_switch_at, snap.active_changed_at) if t is not None),
        default=None,
    )
    if since is None:
        return None
    remaining_s = snap.settings.rebalance_cooldown_min * 60.0 - (snap.now - since)
    return remaining_s if remaining_s > 0 else None


def _hours(hours: float) -> str:
    return f"~{max(1, round(hours * 60))}m" if hours < 1 else f"~{hours:.0f}h"


def soft7_eta_h(
    v: AccountView, rate: float, snap: Snapshot, mark: float | None = None
) -> float | None:
    """Hours until ``v``'s 7d passes soft7d (or ``mark``) at ``rate``
    pct/hour; None when it never does: not climbing, or its 7d window
    resets first."""
    if rate <= 0:
        return None
    mark = snap.settings.soft_7d if mark is None else mark
    hours = max(mark - v.pct7, 0.0) / rate
    if v.reset7 is not None and v.reset7 <= snap.now + hours * 3600.0:
        return None
    return hours


def preempt_horizon(snap: Snapshot) -> tuple[float, str]:
    """``(hours, phrase)``: how far ahead preempt looks, and how a reason
    says so. To the next quiet window when there is one within
    ``preemptHorizonMaxH``; else that cap; with no pattern,
    ``NO_PATTERN_HORIZON_H`` (never past the cap)."""
    s = snap.settings
    cap = float(s.preempt_horizon_max_h)
    f = snap.forecast
    if f is None:
        hours = min(NO_PATTERN_HORIZON_H, cap)
        return hours, f"within the next {hours:g}h"
    if f.next is not None and f.next.start - snap.now <= cap * 3600.0:
        return (
            (f.next.start - snap.now) / 3600.0,
            f"before your usual quiet time ({f.next.start_label})",
        )
    return cap, f"within the next {cap:g}h"


def landed_rate7(snap: Snapshot, a: AccountView, v: AccountView) -> float:
    """``v``'s 7d pace (pct/hour) once you are on it, as preempt projects
    it: its own, but never below the active's scaled to ``v``'s plan, since
    your usage moves with you (a 20x -> 5x move climbs four times faster in
    pct). 0 when neither is known."""
    active = snap.rates7.get(a.number) or 0.0
    return max(snap.rates7.get(v.number, 0.0), active * max(1.0, a.plan_weight / v.plan_weight))


def crosses_soft7_within(
    snap: Snapshot, a: AccountView, v: AccountView, horizon: float
) -> float | None:
    """Hours until ``v``'s 7d would pass soft7d once you are on it
    (:func:`landed_rate7`), when that is within ``horizon``; else None. The
    one check preempt picks a target with and rebalance skips a candidate by,
    so neither lands where preempt would move off again. Never for a
    draining ``v``: its 7d soft mark is set aside, so preempt never moves
    off it (:func:`reaches_hard7_within` is preempt's check for one)."""
    if draining(v, snap):
        return None
    hours = soft7_eta_h(v, landed_rate7(snap, a, v), snap)
    return hours if hours is not None and hours <= horizon else None


def reaches_hard7_within(
    snap: Snapshot, a: AccountView, v: AccountView, horizon: float
) -> float | None:
    """Hours until ``v``'s 7d would reach hard7d once you are on it
    (:func:`landed_rate7`), when that is within ``horizon`` and before its
    7d reset; else None. Preempt's check for a draining target: a pre-emptive
    move onto an account that a hard switch would end inside the same busy
    stretch moves the forced switch, it does not avoid it."""
    hours = soft7_eta_h(v, landed_rate7(snap, a, v), snap, mark=snap.settings.hard_7d)
    return hours if hours is not None and hours <= horizon else None


def _preempt(
    snap: Snapshot, a: AccountView, landing: list[AccountView]
) -> Decision | None:
    """Move at an idle moment before the active 7d passes soft7d in a busy
    stretch; None when there is no reason to (decide goes on to rebalance).

    The active's 7d must reach soft7d within the horizon at its burn rate,
    and a landable account of no worse a tier must not
    (:func:`crosses_soft7_within`). The rebalance cooldown applies.
    """
    s = snap.settings
    rate = snap.rates7.get(a.number)
    if not s.preempt or rate is None or rate <= 0 or not landing:
        return None
    if draining(a, snap):
        return None  # its 7d soft mark is set aside: nothing to pre-empt
    horizon, when = preempt_horizon(snap)
    hours = soft7_eta_h(a, rate, snap)
    if hours is None or hours > horizon:
        return None
    target = None
    for v in landing:
        if TIER_ORDER[v.tier] > TIER_ORDER[a.tier]:
            continue
        check = reaches_hard7_within if draining(v, snap) else crosses_soft7_within
        if check(snap, a, v, horizon) is None:
            target = v
            break
    if target is None:
        return None
    why = (
        f"#{a.number} 7d {_pct(a.pct7)} would pass {_pct(s.soft_7d)} "
        f"in {_hours(hours)}, {when}"
    )
    left = _cooldown_left(snap)
    if left is not None:
        return Hold(
            f"preempt cooldown ({left / 60:.0f} min left): {why}",
            pending=False,
            code="preempt",
        )
    if not is_idle(snap):
        return Hold(
            f"{why} — will move to #{target.number} at the next idle moment "
            f"({idle_note(snap)})",
            pending=False,
            code="preempt",
        )
    return Switch(
        target.number,
        "preempt",
        f"{why} — moving to #{target.number} now while you're idle",
    )


def _preempt_would_leave(
    snap: Snapshot, a: AccountView, landing: list[AccountView]
) -> dict[str, float]:
    """``{slot: hours}`` for each candidate a same-tier rebalance must skip:
    with ``preempt`` on, one whose 7d would pass soft7d within preempt's
    horizon once you are on it (:func:`crosses_soft7_within`). Landing
    there would only have preempt move you off again a cooldown later, and
    rebalance back after the next one: a switch, and a full context
    re-read, every ``rebalanceCooldownMin``."""
    if not snap.settings.preempt:
        return {}
    horizon, _ = preempt_horizon(snap)
    out: dict[str, float] = {}
    for v in landing:
        hours = crosses_soft7_within(snap, a, v, horizon)
        if hours is not None:
            out[v.number] = hours
    return out


def _deferred_to(snap: Snapshot, gain: float) -> QuietWindow | None:
    """The quiet window a rebalance gaining ``gain`` waits for, or None to
    rebalance as usual: only in a usually-busy slot outside a quiet window,
    for a gain under ``busyRebalanceGap``, and for a window starting within
    ``DEFER_WITHIN_S``."""
    f = snap.forecast
    if f is None or f.current is not None or f.p_busy_now is None:
        return None
    if f.p_busy_now < QUIET_P or gain >= snap.settings.busy_rebalance_gap:
        return None
    if f.next is None or f.next.start - snap.now > DEFER_WITHIN_S:
        return None
    return f.next


def _rebalance_tries(pool: list[AccountView], snap: Snapshot) -> list[AccountView]:
    """The candidates rebalance (b) weighs, in turn: the first draining one
    with useful room (``drain.preferred``), then the first other one
    (``pool`` is in landing order). With none of the first kind that is
    ``pool[0]`` alone, as without the drain."""
    first_draining = next((v for v in pool if drain.preferred(v, snap)), None)
    first_other = next((v for v in pool if not drain.preferred(v, snap)), None)
    return [v for v in (first_draining, first_other) if v is not None]


def _rebalance(
    snap: Snapshot, a: AccountView, landing: list[AccountView]
) -> Decision:
    s = snap.settings
    if not landing:
        return Hold(
            f"#{a.number} under soft ({_usage(a)}); nothing else landable",
            pending=False,
        )
    top = landing[0]
    a_score = score(a, snap.now)
    gain: float | None = None
    if TIER_ORDER[top.tier] < TIER_ORDER[a.tier]:
        # Leaving an excluded or last-resort account is the user's rule:
        # never skipped for a 7d pace.
        why = f"#{a.number} is {a.tier} and #{top.number} ({top.tier}) can land"
    else:
        skipped = _preempt_would_leave(snap, a, landing)
        pool = [v for v in landing if v.number not in skipped]
        a_draining = draining(a, snap)
        if a_draining:
            # Off a draining account only onto one that resets sooner (with
            # useful room): never back to a non-draining one, and never back
            # and forth.
            pool = [
                v for v in pool
                if drain.preferred(v, snap)
                and (v.reset7 or math.inf) < (a.reset7 or math.inf)
            ]
        top = next(
            (
                v for v in _rebalance_tries(pool, snap)
                if v.tier == a.tier and score(v, snap.now) - a_score > s.tie_epsilon
            ),
            None,
        )
        if top is not None:
            t_score = score(top, snap.now)
            gain = t_score - a_score
            why = (
                f"#{top.number} score {t_score:.2f} beats #{a.number} "
                f"{a_score:.2f} by more than {s.tie_epsilon:g}"
            )
            if draining(top, snap):
                why += f"; {drain.text(top, snap)}"
        elif a_draining:
            return Hold(
                f"{drain.text(a, snap)} (7d soft {_pct(s.soft_7d)} set aside "
                f"until the reset; hard {_pct(s.hard_7d)} still switches)",
                pending=False,
            )
        else:
            best = landing[0]
            b_score = score(best, snap.now)
            if (
                best.number in skipped
                and best.tier == a.tier
                and b_score - a_score > s.tie_epsilon
            ):
                _, when = preempt_horizon(snap)
                return Hold(
                    f"#{a.number} under soft ({_usage(a)}); #{best.number} scores better "
                    f"({b_score:.2f}) but its 7d would pass {_pct(s.soft_7d)} in "
                    f"{_hours(skipped[best.number])}, {when} — staying",
                    pending=False,
                )
            return Hold(
                f"#{a.number} under soft ({_usage(a)}); no better account "
                f"(score {a_score:.2f})",
                pending=False,
            )
    remaining_s = _cooldown_left(snap)
    if remaining_s is not None:
        return Hold(
            f"rebalance cooldown ({remaining_s / 60:.0f} min left): {why}",
            pending=False,
        )
    quiet = _deferred_to(snap, gain) if gain is not None else None
    if quiet is not None:
        return Hold(
            f"rebalance deferred to your quiet time ({quiet.start_label}): {why}",
            pending=False,
            code="rebalance-deferred",
        )
    if not is_idle(snap):
        return Hold(
            f"rebalance waits for idle ({idle_note(snap)}): {why}",
            pending=False,
        )
    return Switch(top.number, "rebalance", f"{why}; idle")


def hold_left(snap: Snapshot) -> float | None:
    """Seconds an account hold still pins the active account, or None."""
    until = snap.hold_until
    if until is None or not math.isfinite(until) or until <= snap.now:
        return None
    return until - snap.now


def _held(snap: Snapshot, a: AccountView, inner: Decision) -> Decision:
    """``inner`` (a soft, preempt or rebalance decision) as the account hold
    leaves it: a ``hold`` that says what would have happened, or ``inner``
    itself when no hold is in force."""
    left = hold_left(snap)
    if left is None:
        return inner
    s = snap.settings
    hold = account_hold.AccountHold(a.number, snap.now + left)
    return Hold(
        f"#{a.number} held {account_hold.until_text(hold, snap.now)} — "
        f"{account_hold.safety_text(s.hard_5h, s.hard_7d)}; otherwise: {inner.reason}",
        pending=False,
        code="hold",
    )


def with_note(decision: Decision, note: str) -> Decision:
    """``decision`` with ``(note)`` after the first clause of its reason
    (the part Fleet's now line keeps), so every surface that shows the
    reason says the usage was estimated and why."""
    head, sep, rest = decision.reason.partition(";")
    return replace(decision, reason=f"{head} ({note}){sep}{rest}")


def decide(snap: Snapshot) -> Decision:
    decision = _decide(snap)
    if snap.estimate is None or isinstance(decision, Indeterminate):
        return decision
    return with_note(decision, snap.estimate.note)


def _decide(snap: Snapshot) -> Decision:
    a = snap.view(snap.active)
    if a is None:
        return Indeterminate("no active managed account")
    if a.pct5 is None or a.pct7 is None:
        return Indeterminate(f"#{a.number} usage unknown")
    landing = landing_candidates(snap)

    if a.pct5 >= LIMIT_PCT or a.pct7 >= LIMIT_PCT:
        return _at_limit(snap, landing, f"#{a.number} at limit ({_usage(a)})")
    force = _hard_force(snap, a)
    soft = _soft_reason(a, snap)
    if force is not None or soft is not None:
        waited = _reset_wait(snap, a, landing)
        if waited is not None:
            return waited
    if force is not None:
        return _hard_or_ride(snap, a, landing, force)
    # Below every hard trigger: an account hold sets the rest aside.
    if soft is not None:
        return _held(snap, a, _soft(snap, landing, soft))
    pre = _preempt(snap, a, landing)
    if pre is not None:
        return _held(snap, a, pre)
    return _held(snap, a, _rebalance(snap, a, landing))
