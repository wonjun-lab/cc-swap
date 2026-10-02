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
at-limit/hard fall back to any eligible account under both hard caps, else
``Exhausted``; soft/rebalance ``Hold``. Unknown active usage is
``Indeterminate`` (the engine's upstream failover path counts it).
"""

from __future__ import annotations

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
    Trigger,
)
from claude_swap.maximize.score import below_hard, landable, rank, score


def _pct(value: float) -> str:
    return f"{value:g}%"


def _usage(v: AccountView) -> str:
    return f"5h {_pct(v.pct5)} / 7d {_pct(v.pct7)}"


def landing_candidates(snap: Snapshot) -> list[AccountView]:
    """Every non-active landable account, best first (spec §5.2 + §5.4)."""
    s = snap.settings
    return rank(
        [v for v in snap.accounts if v.number != snap.active and landable(v, s)],
        snap.now,
        s.tie_epsilon,
    )


def escape_candidates(snap: Snapshot) -> list[AccountView]:
    """The at-limit/hard fallback: eligible accounts under both hard caps."""
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


def _hard_reason(snap: Snapshot, a: AccountView) -> str | None:
    s = snap.settings
    if a.pct5 >= s.hard_5h:
        return f"#{a.number} 5h {_pct(a.pct5)} >= hard {_pct(s.hard_5h)}"
    if a.pct7 >= s.hard_7d:
        return f"#{a.number} 7d {_pct(a.pct7)} >= hard {_pct(s.hard_7d)}"
    if (
        s.force_eta_min > 0
        and snap.samples
        and snap.now - snap.samples[-1].ts <= s.idle_window_min * 60.0
    ):
        eta = idle.eta_to_hard_min(snap.samples, s)
        if eta is not None and eta <= s.force_eta_min:
            return (
                f"#{a.number} reaches a hard cap in ~{eta:.1f} min "
                f"(<= {s.force_eta_min} min)"
            )
    return None


def _soft_reason(a: AccountView, snap: Snapshot) -> str | None:
    s = snap.settings
    if a.pct5 >= s.soft_5h:
        return f"#{a.number} 5h {_pct(a.pct5)} >= soft {_pct(s.soft_5h)}"
    if a.pct7 >= s.soft_7d:
        return f"#{a.number} 7d {_pct(a.pct7)} >= soft {_pct(s.soft_7d)}"
    return None


def _escape(
    snap: Snapshot,
    landing: list[AccountView],
    trigger: Trigger,
    why: str,
) -> Decision:
    if landing:
        top = landing[0]
        return Switch(top.number, trigger, f"{why}; -> {_target(top, snap.now)}")
    fallback = escape_candidates(snap)
    if fallback:
        top = fallback[0]
        return Switch(
            top.number,
            trigger,
            f"{why}; nothing landable, #{top.number} is under the hard caps "
            f"({_usage(top)})",
        )
    return Exhausted(f"{why}; no account is under the hard caps")


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
    if snap.last_switch_at is not None:
        remaining_s = s.rebalance_cooldown_min * 60.0 - (
            snap.now - snap.last_switch_at
        )
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

    if a.pct5 >= 100.0 or a.pct7 >= 100.0:
        return _escape(snap, landing, "at-limit", f"#{a.number} at limit ({_usage(a)})")
    hard = _hard_reason(snap, a)
    if hard is not None:
        return _escape(snap, landing, "hard", hard)
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
