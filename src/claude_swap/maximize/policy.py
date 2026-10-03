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

Unknown active usage is ``Indeterminate`` (the engine's upstream failover
path counts it).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from claude_swap.maximize import hold as account_hold
from claude_swap.maximize import idle
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


def landing_candidates(snap: Snapshot) -> list[AccountView]:
    """Every non-active landable account, best first (spec §5.2 + §5.4).

    An account inside its login-expiry guard is no landing target (soft,
    rebalance, and the first choice of hard/at-limit); the hard/at-limit
    fallbacks (``escape_candidates``, ``limit_candidates``) still take it."""
    s = snap.settings
    return rank(
        [
            v
            for v in snap.accounts
            if v.number != snap.active
            and landable(v, s)
            and not login_guarded(v, snap.now, s)
        ],
        snap.now,
        s.tie_epsilon,
    )


def escape_candidates(snap: Snapshot) -> list[AccountView]:
    """The at-limit/hard fallback pool: eligible accounts under both hard
    caps, in §5.4 order."""
    s = snap.settings
    return rank(
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
    )


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
    return sorted(out, key=lambda v: -room(v))


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
        key=lambda v: (-binding_room(v), binding_recovery(v, snap.now), slot_order(v)),
    )


def idle_note(snap: Snapshot) -> str:
    """Human summary of the idle evidence, for reasons and dry-run output."""
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


def _target(v: AccountView, now: float) -> str:
    return f"#{v.number} ({v.tier}, score {score(v, now):.2f})"


@dataclass(frozen=True)
class _Force:
    """Why the hard trigger fired, and on which window(s)."""

    reason: str
    windows: tuple[Window, ...]


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


def _eta_forced(snap: Snapshot) -> dict[Window, float]:
    """``{window: minutes}`` for each window whose recent pace reaches its
    hard cap within ``force_eta_min`` (none on stale samples)."""
    s = snap.settings
    if s.force_eta_min <= 0 or not _fresh_samples(snap):
        return {}
    eta5, eta7 = idle.eta_to_hard(snap.samples, s)
    return {
        w: eta
        for w, eta in (("5h", eta5), ("7d", eta7))
        if eta is not None and eta <= s.force_eta_min
    }


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
        return _Force(why, reached)
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


def _soft_reason(
    a: AccountView, snap: Snapshot, skip: tuple[Window, ...] = ()
) -> str | None:
    s = snap.settings
    if "5h" not in skip and a.pct5 >= s.soft_5h:
        return f"#{a.number} 5h {_pct(a.pct5)} >= soft {_pct(s.soft_5h)}"
    if "7d" not in skip and a.pct7 >= s.soft_7d:
        return f"#{a.number} 7d {_pct(a.pct7)} >= soft {_pct(s.soft_7d)}"
    return None


def _window_pct(v: AccountView, window: Window) -> float:
    return v.pct5 if window == "5h" else v.pct7


def _window_reset(v: AccountView, window: Window) -> float | None:
    return v.reset5 if window == "5h" else v.reset7


def reset_wait_left(
    snap: Snapshot, a: AccountView, window: Window, rate: float | None
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
        if pct >= mark
    )
    rates = idle.velocity(snap.samples, s) if _fresh_samples(snap) else (None, None)
    waits: dict[Window, float] = {}
    for w, rate in zip(("5h", "7d"), rates):
        if w in reached or w in forced or w in soft:
            left = reset_wait_left(snap, a, w, rate)
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
        return _hard(snap, a, landing, force)
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
        return Switch(top.number, "at-limit", f"{why}; -> {_target(top, snap.now)}")
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
        return Switch(top.number, "hard", f"{force.reason}; -> {_target(top, snap.now)}")
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
    if idle.is_idle(snap.samples, snap.now, snap.settings):
        return Switch(top.number, "soft", f"{why}; idle; -> {_target(top, snap.now)}")
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


def soft7_eta_h(v: AccountView, rate: float, snap: Snapshot) -> float | None:
    """Hours until ``v``'s 7d passes soft7d at ``rate`` pct/hour; None when
    it never does: not climbing, or its 7d window resets first."""
    if rate <= 0:
        return None
    hours = max(snap.settings.soft_7d - v.pct7, 0.0) / rate
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
    so neither lands where preempt would move off again."""
    hours = soft7_eta_h(v, landed_rate7(snap, a, v), snap)
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
    horizon, when = preempt_horizon(snap)
    hours = soft7_eta_h(a, rate, snap)
    if hours is None or hours > horizon:
        return None
    target = None
    for v in landing:
        if TIER_ORDER[v.tier] > TIER_ORDER[a.tier]:
            continue
        if crosses_soft7_within(snap, a, v, horizon) is None:
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
    if not idle.is_idle(snap.samples, snap.now, s):
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
        top = pool[0] if pool else None
        t_score = score(top, snap.now) if top is not None else -math.inf
        if top is not None and top.tier == a.tier and t_score - a_score > s.tie_epsilon:
            gain = t_score - a_score
            why = (
                f"#{top.number} score {t_score:.2f} beats #{a.number} "
                f"{a_score:.2f} by more than {s.tie_epsilon:g}"
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
    if not idle.is_idle(snap.samples, snap.now, s):
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


def decide(snap: Snapshot) -> Decision:
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
        return _hard(snap, a, landing, force)
    # Below every hard trigger: an account hold sets the rest aside.
    if soft is not None:
        return _held(snap, a, _soft(snap, landing, soft))
    pre = _preempt(snap, a, landing)
    if pre is not None:
        return _held(snap, a, pre)
    return _held(snap, a, _rebalance(snap, a, landing))
