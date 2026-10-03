"""The Fleet screens' read model: pure functions, no Textual, no I/O.

The per-account rows, the engine's decision, who holds the engine and the
Mode, Re-login and Swap strategy logic are computed here from the TUI's
store snapshot, the engine's state file (:func:`view.read_state`), the
settings and — when one runs in this process — the TUI's own engine events.
``maximize/home.py`` turns them into the home screen's sentence, tags and
layout; Textual widgets only lay them out (codex-swap's ``render_lines``
discipline applied to the Textual app). ``now`` is always passed in.

Cells are ``(text, tone)`` pairs; a tone is one of ``ok``, ``warn``,
``crit``, ``dim``, ``accent``, ``plain`` or ``bold``, which the widget maps
onto the theme palette.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from claude_swap import oauth
from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_LOGIN_EXPIRED,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.maximize import idle, pause, policy
from claude_swap.maximize import primer as mxprimer
from claude_swap.maximize import view as mxview
from claude_swap.maximize.history import History as UsageHistory
from claude_swap.maximize.model import AccountView, Hold, Snapshot, Switch
from claude_swap.maximize.names import display_names
from claude_swap.maximize.plan import parse_plan_override
from claude_swap.maximize.score import days_left, landable
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.settings import SETTING_SPECS, MaximizeSettings, PrimeSettings
from claude_swap.usage_store import STALE_OK_S

LoginState = Literal["ok", "relogin", "expired", "foreign", "keychain", "api"]
Tone = str
Cell = tuple[str, Tone]

#: Warn about a login's deadline this long before it (Claude Code nudges its
#: own session at 3 days; a parked slot has no session to show that in).
LOGIN_WARN_S = 7 * 86400.0
#: Inside this the warning turns red.
LOGIN_URGENT_S = 86400.0

#: The engine rewrites an unchanged decision this often
#: (``engine_hook.PUBLISH_REFRESH_S``; pinned by a test).
PUBLISH_REFRESH_S = 300.0

_SENTINEL_LOGIN: dict[str, LoginState] = {
    USAGE_RELOGIN_REQUIRED: "relogin",
    USAGE_LOGIN_EXPIRED: "relogin",  # named by its cause on FleetRow.login_expired
    USAGE_NO_CREDENTIALS: "relogin",
    USAGE_TOKEN_EXPIRED: "expired",
    USAGE_FOREIGN_CREDENTIAL: "foreign",
    USAGE_KEYCHAIN_UNAVAILABLE: "keychain",
    USAGE_API_KEY: "api",
}
TIER_CELLS = {"normal": "normal", "last_resort": "last-r", "excluded": "excl"}


# -- time ---------------------------------------------------------------------------


def hhmm(ts: float) -> str:
    """Local ``HH:MM``."""
    return time.strftime("%H:%M", time.localtime(ts))


def day_clock(ts: float, now: float) -> str:
    """``HH:MM`` today, else ``Thu 09:00``."""
    if time.localtime(ts)[:3] == time.localtime(now)[:3]:
        return hhmm(ts)
    return time.strftime("%a %H:%M", time.localtime(ts))


def minutes_text(minutes: float) -> str:
    """``45m`` / ``1h50m``."""
    m = max(int(round(minutes)), 0)
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


# -- per account ------------------------------------------------------------------------


def login_state(acc: AccountSnapshot) -> LoginState:
    """How the stored login stands, from the usage sentinel."""
    if acc.kind == "api_key":
        return "api"
    return _SENTINEL_LOGIN.get(acc.usage.sentinel or "", "ok")


def _is_team(acc: AccountSnapshot) -> bool:
    """An organization account. Claude names a personal account's own
    organization ``<email>'s Organization``; that one is not a Team."""
    name = (acc.org_name or "").strip()
    return bool(name) and not name.lower().endswith("'s organization")


def plan_label(
    acc: AccountSnapshot, published: str | None, override: str | None
) -> str:
    """``20x``/``5x`` from the engine's published plan, else
    ``maximize.planOverride``; ``team`` for an organization account; ``api``
    for an API key; ``?`` when nothing says."""
    if acc.kind == "api_key":
        return "api"
    if published in ("20x", "5x"):
        return published
    weight = parse_plan_override(override).get((acc.email or "").strip().lower())
    if weight is not None:
        return "20x" if weight >= 4 else "5x"
    if published == "team" or _is_team(acc):
        return "team"
    return "?"


def land_note(
    view: AccountView,
    s: MaximizeSettings,
    *,
    active: bool,
    login: LoginState,
    now: float | None = None,
) -> str:
    """Whether maximize could land on this account, and if not, why."""
    if login == "relogin":
        return "re-login"
    if active:
        return "active"
    if view.api_key or login == "api":
        return "api key"
    if view.tier == "excluded":
        return "excluded"
    if view.quarantined:
        return "quarant."
    if view.pct5 is None or view.pct7 is None:
        return "usage ?"
    if view.pct5 >= s.soft_5h - s.landing_margin:
        return f"5h≥{s.soft_5h - s.landing_margin:g}"
    if view.pct7 >= s.soft_7d - s.landing_margin:
        return f"7d≥{s.soft_7d - s.landing_margin:g}"
    if now is not None and policy.login_guarded(view, now, s):
        return f"login<{minutes_text(s.login_expiry_guard_min).replace('h00m', 'h')}"
    return "yes"


@dataclass(frozen=True)
class PrimeCell:
    kind: Literal["active", "off", "due", "window", "skip"]
    lo: float | None
    hi: float | None
    note: str


_SKIP_NOTES = {
    "pending-verify": "verifying",
    "live-session": "live session",
    "7d-exhausted": "7d spent",
    "quarantined": "re-login",
    "login-expired": "login exp.",
    "usage-unknown": "usage ?",
    "api-key": "api key",
    "excluded": "—",
    "active": "—",
}


def prime_cell(
    view: AccountView,
    entry: Mapping | None,
    active: str | None,
    prime: PrimeSettings,
    now: float,
) -> PrimeCell:
    """When the primer opens this account's 5h window next, or why not."""
    if view.number == active:
        return PrimeCell("active", None, None, "—")
    if view.tier == "excluded":
        return PrimeCell("skip", None, None, "—")
    if not prime.enabled:
        return PrimeCell("off", None, None, "off")
    reason = mxprimer.skip_reason(view, active, entry, now, prime.max_attempts)
    if reason is None or reason == "window-on":
        window = mxprimer.prime_window(view, entry, active, prime, now)
        if window is not None:
            kind = "due" if reason is None else "window"
            return PrimeCell(kind, window[0], window[1], "")
        reason = reason or "?"
    if reason == "rate-limited":
        until = view.reset7
        note = "rate-ltd" + (f" → {day_clock(until, now)}" if until else "")
    elif reason == "attempts-exhausted":
        note = f"{prime.max_attempts}/{prime.max_attempts} tries"
    else:
        note = _SKIP_NOTES.get(reason, reason)
    return PrimeCell("skip", None, None, note)


def prime_text(cell: PrimeCell) -> str:
    if cell.kind == "due" and cell.hi is not None:
        return f"≤{hhmm(cell.hi)}"
    if cell.kind == "window" and cell.lo is not None and cell.hi is not None:
        lo, hi = hhmm(cell.lo), hhmm(cell.hi)
        return lo if lo == hi else f"{lo}–{hi}"
    return cell.note


@dataclass(frozen=True)
class FleetRow:
    number: str
    name: str            # alias, else the short name (maximize/names.py)
    email: str
    org: str             # display tag: org name or "personal"
    active: bool
    rank: int | None     # maximize pick order among non-excluded; None = score unknown
    plan: str
    tier: str            # normal | last_resort | excluded
    pct5: float | None
    pct7: float | None
    days7: float | None
    score: float | None
    landable: bool
    land: str
    state5: str          # cold | running | primed
    reset5: float | None
    prime: PrimeCell
    login: LoginState
    stale: bool
    fetched_at: float | None = None
    # The re-login is needed because the login reached its recorded deadline
    # (``login expired`` sentinel), not because the refresh token died.
    login_expired: bool = False
    # When the stored login lapses (epoch seconds, ``refreshTokenExpiresAt``);
    # None when the login records no deadline.
    login_deadline: float | None = None
    # When the 7d window resets (None: unknown or already past). A login
    # that cannot be read keeps the resets of its last good reading in
    # ``reset5``/``reset7`` while they are still ahead.
    reset7: float | None = None


def _seen_resets(acc: AccountSnapshot, now: float) -> tuple[float | None, float | None]:
    """The resets still ahead in the last good reading of an account whose
    usage now reads as a sentinel (a dead login, an API key …)."""
    if acc.usage.sentinel is None:
        return None, None
    from claude_swap.maximize.snapshot import usage_windows

    _p5, reset5, _p7, reset7 = usage_windows(acc.usage.last_good, now)
    return (reset5 if reset5 is not None and reset5 > now else None), reset7


def fleet_snapshot(
    snap: AccountsSnapshot,
    mx: MaximizeSettings,
    state: mxview.MaximizeState,
    *,
    now: float,
    history: UsageHistory | None = None,
) -> Snapshot:
    """The policy Snapshot the TUI decides on: :func:`view.snapshot_from_accounts`
    with the published plans, and slots without a usable stored login set
    aside like the engine does (``engine_hook._unavailable``). ``history``
    (``view.read_history``) gives the decisions Fleet computes itself the
    idle pattern and burn rates the engine's preempt and rebalance deferral
    read; None decides as if there were no history."""
    unusable = {
        a.number
        for a in snap.accounts
        if not a.switchable and not a.is_active and not a.disabled
    }
    if unusable:
        state = replace(state, quarantined=state.quarantined | unusable)
    return mxview.snapshot_from_accounts(
        snap, mx, state, now=now, plans=state.plans, history=history
    )


def _slot(number: str) -> tuple[int, str]:
    return (int(number), "") if number.isdigit() else (1 << 30, number)


def fleet_rows(
    snap: AccountsSnapshot,
    mx: MaximizeSettings,
    prime: PrimeSettings,
    state: mxview.MaximizeState,
    *,
    now: float,
) -> list[FleetRow]:
    """One row per account in slot order, carrying maximize's rank."""
    msnap = fleet_snapshot(snap, mx, state, now=now)
    ranked = mxview.rows(msnap, state.primes)
    rank: dict[str, int] = {}
    position = 0
    for row in ranked:
        if row.tier == "excluded":
            continue
        position += 1
        if row.score is not None:
            rank[row.number] = position
    by_row = {r.number: r for r in ranked}
    views = {v.number: v for v in msnap.accounts}
    shown = display_names((a.number, a.email, a.alias) for a in snap.accounts)
    out: list[FleetRow] = []
    for acc in sorted(snap.accounts, key=lambda a: _slot(a.number)):
        v = views[acc.number]
        r = by_row[acc.number]
        login = login_state(acc)
        raw = state.primes.get(acc.email)
        entry = raw if isinstance(raw, Mapping) else None
        active = acc.number == msnap.active
        seen5, seen7 = _seen_resets(acc, now)
        out.append(
            FleetRow(
                number=acc.number,
                name=shown[acc.number],
                email=acc.email,
                org=acc.display_tag,
                active=active,
                rank=rank.get(acc.number),
                plan=plan_label(acc, state.plans.get(acc.number), mx.plan_override),
                tier=v.tier,
                pct5=v.pct5,
                pct7=v.pct7,
                days7=days_left(v, now) if v.pct7 is not None else None,
                score=r.score,
                landable=(
                    not active and landable(v, mx) and not policy.login_guarded(v, now, mx)
                ),
                land=land_note(v, mx, active=active, login=login, now=now),
                state5=r.state5,
                reset5=(v.reset5 if r.state5 != "cold" else None) or seen5,
                prime=prime_cell(v, entry, msnap.active, prime, now),
                login=login,
                stale=acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S,
                fetched_at=acc.usage.fetched_at,
                login_expired=acc.usage.sentinel == USAGE_LOGIN_EXPIRED,
                login_deadline=(
                    acc.login_expires_at / 1000.0
                    if acc.login_expires_at is not None
                    else None
                ),
                reset7=v.reset7 if v.reset7 is not None else seen7,
            )
        )
    return out


# -- the decision -----------------------------------------------------------------------


DecisionKind = Literal[
    "switch", "hold", "pending", "exhausted", "indeterminate", "paused", "off", "none"
]


@dataclass(frozen=True)
class DecisionView:
    kind: DecisionKind
    active: str | None
    target: str | None
    trigger: str | None
    reason: str
    growth: float | None = None       # pct points per idle window (pending)
    window: str | None = None         # "5h" | "7d" (pending)
    window_min: int | None = None
    eta_hard_min: float | None = None
    at: float | None = None
    source: Literal["engine", "here", "computed"] = "computed"
    # kind "off" (`cc-swap auto off`): what the decision would have been.
    would: str | None = None
    # A hold's own code (``model.Hold.code``: reset-wait, preempt,
    # rebalance-deferred), from the engine or computed here; else None.
    code: str | None = None
    # reset-wait: ``(window, pct, reset epoch)`` for each window being waited
    # out, read from the current snapshot so the minutes left stay live.
    waits: tuple[tuple[str, float, float], ...] = ()
    # ride: when the learned ride switches (epoch s), so the minutes left
    # stay live; None when unknown.
    ride_until: float | None = None


_SLOT_RE = re.compile(r"#(\w+)")
#: One waited-out window in a reset-wait reason (``policy._reset_wait``):
#: ``5h 96% — resets in 8m``.
_WAIT_RE = re.compile(r"\b(5h|7d) [\d.]+% — resets in \d+m")


def _target_in(reason: str, active: str | None) -> str | None:
    """The first account a policy reason names that is not the active one
    (every reason names the target after the active account, if at all)."""
    for match in _SLOT_RE.finditer(reason or ""):
        if match.group(1) != active:
            return match.group(1)
    return None


def _kind(decision: str, pending: bool) -> DecisionKind:
    if decision == "hold":
        return "pending" if pending else "hold"
    if decision in ("switch", "exhausted", "indeterminate"):
        return decision  # type: ignore[return-value]
    return "none"


def reset_waits(reason: str, msnap: Snapshot) -> tuple[tuple[str, float, float], ...]:
    """The windows a reset-wait hold names (``5h 96% — resets in 8m``), with
    the active account's current pct and reset from ``msnap``; a window that
    has reset since (or reads no reset) is left out."""
    a = msnap.view(msnap.active)
    if a is None:
        return ()
    out: list[tuple[str, float, float]] = []
    for window in dict.fromkeys(m.group(1) for m in _WAIT_RE.finditer(reason or "")):
        pct, reset = (a.pct5, a.reset5) if window == "5h" else (a.pct7, a.reset7)
        if pct is not None and reset is not None and reset > msnap.now:
            out.append((window, pct, reset))
    return tuple(out)


def _enrich(dv: DecisionView, msnap: Snapshot) -> DecisionView:
    """Growth and the hard-cap ETA, recomputed from the samples; for a
    reset-wait hold, the windows it waits out. A reset-wait hold gets no
    hard-cap ETA: it may already be past the hard cap (``hard in ~0m``), and
    what ends it is the reset or 100%, not the cap."""
    waiting = mxview.pending(msnap) if dv.kind == "pending" else None
    reset_wait = dv.kind == "hold" and dv.code == "reset-wait"
    # A ride is past its hard mark already: what ends it is its own time.
    past_hard = reset_wait or (dv.kind == "hold" and dv.code == "ride")
    eta = (
        idle.eta_to_hard_min(msnap.samples, msnap.settings)
        if dv.kind in ("pending", "hold") and msnap.samples and not past_hard
        else None
    )
    return replace(
        dv,
        growth=waiting.growth if waiting else None,
        window=waiting.window if waiting else None,
        window_min=waiting.window_min if waiting else None,
        eta_hard_min=eta,
        waits=reset_waits(dv.reason, msnap) if reset_wait else (),
    )


def _hold_target(code: str | None, reason: str, active: str | None) -> str | None:
    """Where a coded hold is headed: a preempt waiting for idle and a
    deferred rebalance name their target in the reason; a reset-wait goes
    nowhere."""
    if code in ("preempt", "rebalance-deferred"):
        return _target_in(reason, active)
    return None


def _computed(msnap: Snapshot, *, now: float) -> DecisionView:
    decision = policy.decide(msnap)
    code: str | None = None
    ride_until = decision.ride_until if isinstance(decision, Hold) else None
    if isinstance(decision, Switch):
        target, trigger = decision.target, decision.trigger
        kind: DecisionKind = "switch"
    else:
        trigger = None
        target = None
        if isinstance(decision, Hold):
            kind = "pending" if decision.pending else "hold"
            code = decision.code
            if decision.pending:
                landing = policy.landing_candidates(msnap)
                target = landing[0].number if landing else None
            else:
                target = _hold_target(code, decision.reason, msnap.active)
        else:
            kind = _kind(type(decision).__name__.lower(), False)
    return DecisionView(
        kind=kind, active=msnap.active, target=target, trigger=trigger,
        reason=decision.reason, at=now, source="computed", code=code,
        ride_until=ride_until,
    )


def fresh_s(poll_s: float) -> float:
    """How old a published decision may be and still count as the engine's
    current one: it is rewritten at least every PUBLISH_REFRESH_S."""
    return max(3.0 * poll_s, PUBLISH_REFRESH_S + 2.0 * poll_s)


def decision_view(
    state: mxview.MaximizeState,
    msnap: Snapshot,
    *,
    now: float,
    poll_s: float,
    own=None,
    own_at: float | None = None,
) -> DecisionView:
    """What maximize decided: a re-login pause first, then this TUI's own
    engine (``own``, a ``MaximizeDecisionEvent``), then a fresh published
    decision for the current active account, else recomputed here. With
    automatic switching off the decision is shown as what it *would* do."""
    dv = _decision_view(state, msnap, now=now, poll_s=poll_s, own=own, own_at=own_at)
    if not state.auto_off or dv.kind == "paused":
        return dv
    target = f" → #{dv.target}" if dv.target else ""
    would = {
        "switch": f"switch ({dv.trigger}){target}",
        "pending": f"switch at the next idle moment{target}",
        "hold": (
            "hold (account hold)" if dv.code == "hold"
            else f"hold ({dv.code})" if dv.code else "hold"
        ),
        "exhausted": "every account is at its limit",
        "indeterminate": "fail over (usage unreadable)",
    }.get(dv.kind)
    return replace(
        dv, kind="off", target=None, trigger=None, would=would,
        at=state.auto_off_since, reason=state.auto_off_by or "", code=None, waits=(),
        ride_until=None,
    )


def _decision_view(
    state: mxview.MaximizeState,
    msnap: Snapshot,
    *,
    now: float,
    poll_s: float,
    own=None,
    own_at: float | None = None,
) -> DecisionView:
    paused = pause.active_pause(
        {"pausedUntil": state.paused_until, "pausedReason": state.paused_reason}, now
    )
    if paused is not None:
        until, why = paused
        return DecisionView(
            kind="paused", active=msnap.active, target=None, trigger=None,
            reason=why, at=until, source="engine",
        )
    if own is not None:
        code = getattr(own, "code", None)
        dv = DecisionView(
            kind=_kind(own.decision, own.pending),
            active=own.active,
            target=_target_in(own.reason, own.active),
            trigger=own.trigger,
            reason=own.reason,
            at=own_at if own_at is not None else now,
            source="here",
            code=code if own.decision == "hold" and code in mxview.HOLD_CODES else None,
            ride_until=getattr(own, "ride_until", None) if code == "ride" else None,
        )
        return _enrich(dv, msnap)
    published = state.decision
    if (
        published is not None
        and published.active == msnap.active
        and 0 <= now - published.at <= fresh_s(poll_s)
    ):
        dv = DecisionView(
            kind=_kind(published.decision, published.pending),
            active=published.active,
            target=published.target or _target_in(published.reason, published.active),
            trigger=published.trigger,
            reason=published.reason,
            at=published.at,
            source="engine",
            code=published.code,
            ride_until=published.ride_until,
        )
        return _enrich(dv, msnap)
    return _enrich(_computed(msnap, now=now), msnap)


def preview_decision(msnap: Snapshot, settings: MaximizeSettings) -> DecisionView:
    """The decision the current fleet would get under ``settings`` (unsaved
    Swap strategy edits)."""
    edited = replace(msnap, settings=settings)
    return _enrich(_computed(edited, now=msnap.now), edited)


def _head_reason(reason: str) -> str:
    return reason.split(";", 1)[0].strip()


def decision_parts(dv: DecisionView, *, now: float) -> tuple[list[tuple[str, int]], Tone]:
    """The ``now`` line as ``(text, drop priority)`` parts, plus its tone.
    Priority 0 is never dropped; the highest number goes first."""
    if dv.source == "computed":
        suffix = "computed here"
    else:
        suffix = f"{hhmm(dv.at or now)} · {'engine' if dv.source == 'engine' else 'here'}"
    target = f" → #{dv.target}" if dv.target else ""
    if dv.kind == "off":
        since = f"since {hhmm(dv.at)}" if dv.at else ""
        by = f"by {dv.reason}" if dv.reason else ""
        parts = [("AUTO OFF · no switch, no prime", 0)]
        if dv.would:
            parts.append((f"would {dv.would}", 2))
        if since or by:
            parts.append((" ".join(p for p in (since, by) if p), 3))
        parts.append(("cc-swap auto on (m → o)", 1))
        return parts, "warn"
    if dv.kind == "paused":
        until = hhmm(dv.at) if dv.at else "?"
        return [
            (f"PAUSED · {dv.reason} in progress — no switch, no prime until {until}", 0),
        ], "warn"
    if dv.kind == "pending":
        parts = [(f"HOLD — waiting for idle{target}", 0), (_head_reason(dv.reason), 2)]
        if dv.growth is not None:
            parts.append((f"{dv.growth:+.0f}%p/{dv.window_min}m", 3))
        if dv.eta_hard_min is not None:
            parts.append((f"hard in ~{minutes_text(dv.eta_hard_min)}", 4))
        return parts + [(suffix, 1)], "warn"
    if dv.kind == "switch":
        return [
            (f"SWITCH ({dv.trigger}){target}", 0), (_head_reason(dv.reason), 2), (suffix, 1)
        ], "accent"
    if dv.kind == "hold":
        parts = [(f"HOLD · {dv.reason}", 0)]
        if dv.eta_hard_min is not None:
            parts.append((f"hard in ~{minutes_text(dv.eta_hard_min)}", 4))
        return parts + [(suffix, 1)], "dim"
    if dv.kind == "exhausted":
        return [("EXHAUSTED · every account is at its limit", 0), (suffix, 1)], "crit"
    if dv.kind == "indeterminate":
        return [(f"INDETERMINATE · {dv.reason}", 0), (suffix, 1)], "warn"
    return [("no decision yet", 0)], "dim"


def now_line(dv: DecisionView, *, now: float, width: int = 1000) -> str:
    parts, _tone = decision_parts(dv, now=now)
    return _fit(parts, width)


# -- engine and service -----------------------------------------------------------------


@dataclass(frozen=True)
class EngineStatus:
    holder: Literal["service", "other", "here-live", "here-dry", "none"]
    pid: int | None
    service: Mapping | None
    # `cc-swap auto off`: whoever holds the lease neither switches nor primes.
    auto_off: bool = False


def engine_status(
    *,
    held_elsewhere: bool,
    holder_pid: int | None,
    own: str | None,
    service: Mapping | None,
    auto_off: bool = False,
) -> EngineStatus:
    """Who holds the engine lease. ``own`` is ``"live"``/``"dry"`` when this
    TUI runs the engine. The service is recognised by its pid."""
    if own == "live":
        return EngineStatus("here-live", holder_pid, service, auto_off)
    if own == "dry":
        return EngineStatus("here-dry", holder_pid, service, auto_off)
    if held_elsewhere:
        if (
            service is not None
            and service.get("running")
            and holder_pid is not None
            and service.get("pid") == holder_pid
        ):
            return EngineStatus("service", holder_pid, service, auto_off)
        return EngineStatus("other", holder_pid, service, auto_off)
    return EngineStatus("none", None, service, auto_off)


# -- text and logins ----------------------------------------------------------------------


def _fit(parts: Sequence[tuple[str, int]], width: int, *, prefix: str = "") -> str:
    """Join ``parts`` with `` · ``, dropping the highest-priority-number part
    until the line fits; priority 0 stays (the line is cut with … if even
    that is too wide)."""
    kept = list(parts)
    while True:
        line = prefix + " · ".join(text for text, _ in kept)
        if len(line) <= width:
            return line
        droppable = [p for p in kept if p[1] > 0]
        if not droppable:
            return line[: max(width - 1, 0)] + "…" if width > 0 else ""
        worst = max(droppable, key=lambda p: p[1])
        kept.remove(worst)


def relogin_count(rows: Sequence[FleetRow]) -> int:
    return sum(1 for r in rows if r.login == "relogin")


def _dead_cause(row: FleetRow) -> str:
    return "login expired" if row.login_expired else "refresh token dead"


def login_left(row: FleetRow, now: float) -> float | None:
    """Seconds until the row's login deadline (negative once past)."""
    return None if row.login_deadline is None else row.login_deadline - now


def login_due(row: FleetRow, now: float) -> bool:
    """The login still reads usable but expires within :data:`LOGIN_WARN_S`
    (or already passed): worth a re-login now."""
    left = login_left(row, now)
    return (
        row.login not in ("relogin", "api")
        and left is not None
        and left < LOGIN_WARN_S
    )


def _expiring_text(row: FleetRow, now: float) -> str:
    """``login expires Oct 9 20:04 (in 6d 2h)``: the deadline format doctor,
    list and the auto log use too (``oauth.login_expiry_note_ms``)."""
    if row.login_deadline is None:
        return "login expired"
    return oauth.login_expiry_note_ms(row.login_deadline * 1000.0, int(now * 1000)) or ""


def login_text(row: FleetRow) -> Cell:
    """The Accounts screen's login column."""
    if row.login == "relogin":
        return f"re-login needed ({_dead_cause(row)})", "crit"
    return _LOGIN_TEXT.get(row.login, (row.login, "plain"))


_LOGIN_TEXT: dict[str, Cell] = {
    "ok": ("ok", "ok"),
    "expired": ("token expired (heals itself)", "warn"),
    "foreign": ("foreign credential (a switch repairs it)", "warn"),
    "keychain": ("keychain locked or in use", "warn"),
    "api": ("API key", "dim"),
}


# -- actions --------------------------------------------------------------------------------


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def name_request(
    alias: str, shown: str, typed: str | None
) -> tuple[str, str | None] | None:
    """What Fleet's ``n`` asks of ``cc-swap alias`` for what was ``typed``
    over the name ``shown``: ``("set", name)``, ``("unset", None)`` (empty:
    back to the short name, maximize/names.py), or None — esc, nothing
    changed, or nothing to clear. The alias rules themselves are the
    switcher's (``set_alias``), exactly as the CLI's."""
    if typed is None:
        return None
    typed = typed.strip()
    if not typed:
        return ("unset", None) if alias else None
    if typed == shown:
        return None
    return "set", typed


def switch_warning(row: FleetRow, mx: MaximizeSettings) -> str | None:
    """Why switching to ``row`` deserves a confirmation, or None (switching
    is reversible, so a landable target switches at once)."""
    if row.active:
        return None
    if row.login == "relogin":
        return (
            f"#{row.number} {row.name}'s stored login is dead; Claude Code will ask "
            "you to log in. Re-login it first (r)."
        )
    if row.tier == "excluded":
        return (
            f"#{row.number} {row.name} is excluded from rotation; maximize will move "
            "you off it at the next idle moment."
        )
    if not row.landable:
        return (
            f"#{row.number} {row.name} is not a place maximize would land ({row.land}); "
            "the engine may move you again at the next idle."
        )
    return None


def over_ssh(environ: Mapping[str, str] | None = None) -> bool:
    import os

    env = os.environ if environ is None else environ
    return bool(env.get("SSH_CONNECTION") or env.get("SSH_CLIENT") or env.get("SSH_TTY"))


def relogin_steps(
    row: FleetRow,
    *,
    ssh: bool,
    host: str | None,
    claude_path: str | None,
    return_to: FleetRow | None,
    now: float | None = None,
) -> list[str]:
    """What to do to re-login ``row`` — cc-swap launches nothing itself.
    With ``now``, a login still in use but near its deadline is explained
    as an early renewal."""
    where = f"on this machine ({host or 'this host'}{', over SSH' if ssh else ''})"
    claude = claude_path or "claude"
    back = (
        f"switches back to #{return_to.number} {return_to.name}"
        if return_to is not None and return_to.number != row.number
        else "stays on it"
    )
    if row.login != "relogin" and now is not None and login_due(row, now):
        headline = (
            f"Re-login #{row.number} {row.name} ({row.email}) — its "
            f"{_expiring_text(row, now)}; a fresh login now starts a new "
            "~30-day deadline (refreshing never extends it)."
        )
    else:
        why = (
            "its login expired (Claude Code logins expire about a month after login)"
            if row.login_expired
            else "its refresh token is dead"
        )
        headline = (
            f"Re-login #{row.number} {row.name} ({row.email}) — {why}; "
            "only a fresh login fixes it."
        )
    lines = [
        headline,
        "",
        f"{where}, in another terminal:",
        f"  1. run  {claude}",
        f"  2. type /login and sign in as {row.email}",
    ]
    if ssh:
        lines.append(
            "     over SSH: open the printed URL on any device, then paste the code back"
        )
    lines += [
        "  3. quit claude (/exit), come back here and press enter",
        f"cc-swap then checks the live login is {row.email}, stores it into slot "
        f"{row.number} and {back}. It refuses a login that belongs to another slot.",
        "Switching and priming are paused while this is open (at most 10 minutes).",
        "Other machines keep their own logins: repeat this there if needed; "
        "don't copy one login between machines.",
    ]
    return lines


# -- Mode -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeAction:
    key: str
    label: str
    action: Literal[
        "start-dry", "start-live", "go-live", "go-dry", "stop", "auto-off", "auto-on"
    ]


def mode_transitions(holder: str, *, auto_off: bool | None = None) -> list[ModeAction]:
    """What the Mode modal offers for an engine holder. A viewer gets facts
    only about the engine: the service (or another process) owns switching.
    With ``auto_off`` given (the TUI always gives it), every holder also
    gets the persistent automatic-switching toggle (``cc-swap auto``),
    which whatever engine runs honours."""
    if holder == "none":
        out = [
            ModeAction("d", "Run an engine here · dry-run (watch only)", "start-dry"),
            ModeAction("l", "Run an engine here · live (switches accounts)", "start-live"),
        ]
    elif holder == "here-dry":
        out = [
            ModeAction("l", "Go live (switches accounts)", "go-live"),
            ModeAction("s", "Stop the engine here", "stop"),
        ]
    elif holder == "here-live":
        out = [
            ModeAction("d", "Back to dry-run (watch only)", "go-dry"),
            ModeAction("s", "Stop the engine here", "stop"),
        ]
    else:
        out = []
    if auto_off is True:
        out.append(ModeAction("o", "Automatic switching: OFF → turn it on", "auto-on"))
    elif auto_off is False:
        out.append(ModeAction(
            "o", "Automatic switching: on → turn it off (persistent; any engine)", "auto-off"
        ))
    return out


def mode_facts(es: EngineStatus) -> list[str]:
    """The Mode modal's text: who switches, and how to change that."""
    lines = _holder_facts(es)
    if es.auto_off:
        lines.append(
            "Automatic switching is OFF (cc-swap auto off): the engine keeps polling "
            "and deciding but never switches or primes. o / cc-swap auto on resumes it."
        )
    return lines


def _holder_facts(es: EngineStatus) -> list[str]:
    service = es.service or {}
    linux = service.get("platform") == "linux"
    stop = (
        "systemctl --user stop cc-swap.service"
        if linux
        else "launchctl bootout gui/$(id -u)/com.wonjun-lab.cc-swap"
    )
    logs = [f"  logs: {path}" for path in service.get("logs") or []]
    if es.holder == "service":
        return [
            f"The cc-swap service (pid {es.pid}) owns switching; this TUI is a viewer.",
            "To run an engine here instead, stop the service first:",
            "  cc-swap service uninstall",
            f"  (or for now: {stop})",
            *logs,
        ]
    if es.holder == "other":
        who = f"pid {es.pid}" if es.pid else "another process"
        return [
            f"{who} holds the engine lease: a terminal `cc-swap auto`, another TUI, "
            "or the menu bar's auto-switch. This TUI is a viewer.",
            "Stop that engine to run one here.",
        ]
    if es.holder in ("here-dry", "here-live"):
        mode = "LIVE: it switches accounts" if es.holder == "here-live" else (
            "DRY-RUN: it decides but never switches"
        )
        return [f"This TUI runs the engine ({mode}). Quitting the TUI stops it."]
    lines = ["Nothing is switching accounts on this machine."]
    if service.get("installed"):
        from claude_swap.maximize.service import state_text

        lines.append(f"The service is installed but not running ({state_text(service)}).")
    else:
        lines.append("For an always-on engine: cc-swap service install")
    if service.get("linger") is False:
        lines.append("Linux lingering is off: the service stops at logout "
                     "(loginctl enable-linger $USER).")
    return lines + logs


# -- Swap strategy editing ------------------------------------------------------------------

_THRESHOLD_KEYS = {dotted: knob for knob, dotted in mxview.KNOB_KEYS.items()}


def strategy_values(mx: MaximizeSettings, prime: PrimeSettings) -> dict[str, object]:
    """Every editable ``maximize.*``/``prime.*`` value, by dotted key."""
    out: dict[str, object] = {}
    for dotted, spec in SETTING_SPECS.items():
        if spec.section == "maximize":
            out[dotted] = getattr(mx, spec.field)
        elif spec.section == "prime":
            out[dotted] = getattr(prime, spec.field)
    return out


def strategy_settings(values: Mapping[str, object]) -> MaximizeSettings:
    """The ``MaximizeSettings`` an edited value map describes."""
    fields = {
        SETTING_SPECS[k].field: v
        for k, v in values.items()
        if k in SETTING_SPECS and SETTING_SPECS[k].section == "maximize"
    }
    return replace(MaximizeSettings(), **fields)


def strategy_step(
    values: Mapping[str, object], key: str, delta: float
) -> dict[str, object]:
    """One ←/→ step on ``key``: thresholds through ``view.step_knob`` (soft
    never passes hard), numbers clamped into their ``SETTING_SPECS`` range,
    booleans toggled, choices cycled (→ next, ← previous); text values do
    not step."""
    out = dict(values)
    spec = SETTING_SPECS[key]
    if key in _THRESHOLD_KEYS:
        knob = _THRESHOLD_KEYS[key]
        stepped = mxview.step_knob(strategy_settings(values), knob, delta)
        out[key] = getattr(stepped, knob)
    elif spec.kind == "bool":
        out[key] = not bool(values.get(key))
    elif spec.kind == "choice":
        choices = spec.choices
        current = values.get(key)
        i = choices.index(current) if current in choices else 0
        out[key] = choices[(i + (1 if delta > 0 else -1)) % len(choices)]
    elif spec.kind in ("float", "int"):
        current = float(values.get(key) or 0.0)
        lo = spec.lo if spec.lo is not None else -math.inf
        hi = spec.hi if spec.hi is not None else math.inf
        value = min(hi, max(lo, current + delta))
        out[key] = int(round(value)) if spec.kind == "int" else round(value, 6)
    return out


def setting_text(value: object) -> str:
    """A value as ``set_setting`` parses it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return f"{value:g}"
    return "" if value is None else str(value)


def strategy_writes(
    saved: Mapping[str, object], edited: Mapping[str, object]
) -> list[tuple[str, str]]:
    """``(dotted key, raw value)`` writes turning ``saved`` into ``edited``:
    only changed keys; the four thresholds in ``view.threshold_writes``
    order (the file never holds soft > hard between two writes)."""
    writes = [
        (key, setting_text(value))
        for key, value in mxview.threshold_writes(
            strategy_settings(saved), strategy_settings(edited)
        )
    ]
    for key, value in edited.items():
        if key in _THRESHOLD_KEYS or saved.get(key) == value:
            continue
        writes.append((key, setting_text(value)))
    return writes
