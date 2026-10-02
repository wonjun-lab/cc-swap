"""The maximize policy: ``decide(Snapshot) -> Decision`` (spec §5.5). Pure.

Triggers, first match wins:

1. at-limit   active 5h or 7d at 100%                  busy or not
2. hard       a hard cap reached, or reached within
              ``force_eta_min`` at the recent burn rate  busy or not
3. soft       a soft threshold reached                  waits for idle
4. rebalance  (a) active is excluded/last_resort and a
              higher tier can land, or (b) the same
              tier's best beats the active by > eps     idle + cooldown

The destination is always the top of ``landing_candidates``. With none:

* at-limit: any eligible account under both hard caps (score order), then
  any eligible account under 100% on both windows (most room first), else
  ``Exhausted``.
* hard: an eligible account under both hard caps with strictly more room
  than the active on every window that forced the switch (most room
  first); else ``Hold`` — the active is still under 100% (at 100% the
  at-limit trigger wins), and moving to less room would bounce straight
  back.
* soft/rebalance: ``Hold``.

Unknown active usage is ``Indeterminate`` (the engine's upstream failover
path counts it).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from claude_swap.maximize import idle
from claude_swap.maximize.model import (
    TIER_ORDER,
    AccountView,
    Decision,
    Exhausted,
    Hold,
    Indeterminate,
    Snapshot,
    Switch,
)
from claude_swap.maximize.score import below_hard, landable, rank, score, slot_order

Window = Literal["5h", "7d"]
# Utilization at which a window is spent (the at-limit trigger).
LIMIT_PCT = 100.0


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


def _hard_force(snap: Snapshot, a: AccountView) -> _Force | None:
    s = snap.settings
    reached: tuple[Window, ...] = tuple(
        w
        for w, pct, cap in (("5h", a.pct5, s.hard_5h), ("7d", a.pct7, s.hard_7d))
        if pct >= cap
    )
    if reached:
        if reached[0] == "5h":
            why = f"#{a.number} 5h {_pct(a.pct5)} >= hard {_pct(s.hard_5h)}"
        else:
            why = f"#{a.number} 7d {_pct(a.pct7)} >= hard {_pct(s.hard_7d)}"
        return _Force(why, reached)
    if (
        s.force_eta_min > 0
        and snap.samples
        and snap.now - snap.samples[-1].ts <= s.idle_window_min * 60.0
    ):
        eta5, eta7 = idle.eta_to_hard(snap.samples, s)
        forced: tuple[Window, ...] = tuple(
            w
            for w, eta in (("5h", eta5), ("7d", eta7))
            if eta is not None and eta <= s.force_eta_min
        )
        if forced:
            eta = min(e for e in (eta5, eta7) if e is not None)
            return _Force(
                f"#{a.number} reaches a hard cap in ~{eta:.1f} min "
                f"(<= {s.force_eta_min} min)",
                forced,
            )
    return None


def _soft_reason(a: AccountView, snap: Snapshot) -> str | None:
    s = snap.settings
    if a.pct5 >= s.soft_5h:
        return f"#{a.number} 5h {_pct(a.pct5)} >= soft {_pct(s.soft_5h)}"
    if a.pct7 >= s.soft_7d:
        return f"#{a.number} 7d {_pct(a.pct7)} >= soft {_pct(s.soft_7d)}"
    return None


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
    # on it while it lasts. At 100% the at-limit trigger takes over.
    return Hold(
        f"{force.reason}; nothing landable and no account under the hard caps "
        f"has more {windows} room than #{a.number}; staying",
        pending=False,
    )


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
    t_score = score(top, snap.now)
    if TIER_ORDER[top.tier] < TIER_ORDER[a.tier]:
        why = f"#{a.number} is {a.tier} and #{top.number} ({top.tier}) can land"
    elif top.tier == a.tier and t_score - a_score > s.tie_epsilon:
        why = (
            f"#{top.number} score {t_score:.2f} beats #{a.number} "
            f"{a_score:.2f} by more than {s.tie_epsilon:g}"
        )
    else:
        return Hold(
            f"#{a.number} under soft ({_usage(a)}); no better account "
            f"(score {a_score:.2f})",
            pending=False,
        )
    # The cooldown runs from the later of the last engine switch and the
    # last change of active account: a manual switch restarts it too.
    since = max(
        (t for t in (snap.last_switch_at, snap.active_changed_at) if t is not None),
        default=None,
    )
    if since is not None:
        remaining_s = s.rebalance_cooldown_min * 60.0 - (snap.now - since)
        if remaining_s > 0:
            return Hold(
                f"rebalance cooldown ({remaining_s / 60:.0f} min left): {why}",
                pending=False,
            )
    if not idle.is_idle(snap.samples, snap.now, s):
        return Hold(
            f"rebalance waits for idle ({idle_note(snap)}): {why}",
            pending=False,
        )
    return Switch(top.number, "rebalance", f"{why}; idle")


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
    if force is not None:
        return _hard(snap, a, landing, force)
    soft = _soft_reason(a, snap)
    if soft is not None:
        if not landing:
            return Hold(f"{soft}; nothing landable", pending=False)
        top = landing[0]
        if idle.is_idle(snap.samples, snap.now, snap.settings):
            return Switch(
                top.number, "soft", f"{soft}; idle; -> {_target(top, snap.now)}"
            )
        return Hold(
            f"{soft}; waiting for idle to move to #{top.number} ({idle_note(snap)})",
            pending=True,
        )
    return _rebalance(snap, a, landing)
