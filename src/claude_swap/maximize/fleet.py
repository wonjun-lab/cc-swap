"""The Fleet screen's read model: pure functions, no Textual, no I/O.

Everything the maximize home screen shows is computed here from the TUI's
store snapshot, the engine's state file (:func:`view.read_state`), the
settings and — when one runs in this process — the TUI's own engine events.
Textual widgets only lay out the cells (codex-swap's ``render_lines``
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

from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.maximize import idle, pause, policy
from claude_swap.maximize import primer as mxprimer
from claude_swap.maximize import view as mxview
from claude_swap.maximize.model import AccountView, Hold, Snapshot, Switch
from claude_swap.maximize.plan import parse_plan_override
from claude_swap.maximize.score import days_left, landable
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.settings import MaximizeSettings, PrimeSettings
from claude_swap.usage_store import STALE_OK_S

LoginState = Literal["ok", "relogin", "expired", "foreign", "keychain", "api"]
Tone = str
Cell = tuple[str, Tone]

#: The engine rewrites an unchanged decision this often
#: (``engine_hook.PUBLISH_REFRESH_S``; pinned by a test).
PUBLISH_REFRESH_S = 300.0

_SENTINEL_LOGIN: dict[str, LoginState] = {
    USAGE_RELOGIN_REQUIRED: "relogin",
    USAGE_NO_CREDENTIALS: "relogin",
    USAGE_TOKEN_EXPIRED: "expired",
    USAGE_FOREIGN_CREDENTIAL: "foreign",
    USAGE_KEYCHAIN_UNAVAILABLE: "keychain",
    USAGE_API_KEY: "api",
}
# What a sentinel's 5h cell says (re-login is crit; the rest heal or are notes).
_LOGIN_CELLS: dict[str, Cell] = {
    "relogin": ("re-login", "crit"),
    "expired": ("token exp.", "warn"),
    "foreign": ("foreign", "warn"),
    "keychain": ("keychain", "warn"),
    "api": ("api", "dim"),
}
TIER_CELLS = {"normal": "normal", "last_resort": "last-r", "excluded": "excl"}
TIER_SUFFIX = {"last_resort": "·LR", "excluded": "·X"}


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
    view: AccountView, s: MaximizeSettings, *, active: bool, login: LoginState
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
    name: str            # alias, else email
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


def fleet_snapshot(
    snap: AccountsSnapshot, mx: MaximizeSettings, state: mxview.MaximizeState, *, now: float
) -> Snapshot:
    """The policy Snapshot the TUI decides on: :func:`view.snapshot_from_accounts`
    with the published plans, and slots without a usable stored login set
    aside like the engine does (``engine_hook._unavailable``)."""
    unusable = {
        a.number
        for a in snap.accounts
        if not a.switchable and not a.is_active and not a.disabled
    }
    if unusable:
        state = replace(state, quarantined=state.quarantined | unusable)
    return mxview.snapshot_from_accounts(snap, mx, state, now=now, plans=state.plans)


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
    out: list[FleetRow] = []
    for acc in sorted(snap.accounts, key=lambda a: _slot(a.number)):
        v = views[acc.number]
        r = by_row[acc.number]
        login = login_state(acc)
        raw = state.primes.get(acc.email)
        entry = raw if isinstance(raw, Mapping) else None
        active = acc.number == msnap.active
        out.append(
            FleetRow(
                number=acc.number,
                name=acc.alias or acc.email,
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
                landable=not active and landable(v, mx),
                land=land_note(v, mx, active=active, login=login),
                state5=r.state5,
                reset5=v.reset5 if r.state5 != "cold" else None,
                prime=prime_cell(v, entry, msnap.active, prime, now),
                login=login,
                stale=acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S,
                fetched_at=acc.usage.fetched_at,
            )
        )
    return out


# -- the decision -----------------------------------------------------------------------


DecisionKind = Literal[
    "switch", "hold", "pending", "exhausted", "indeterminate", "paused", "none"
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


_SLOT_RE = re.compile(r"#(\w+)")


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


def _enrich(dv: DecisionView, msnap: Snapshot) -> DecisionView:
    """Growth and the hard-cap ETA, recomputed from the samples."""
    waiting = mxview.pending(msnap) if dv.kind == "pending" else None
    eta = (
        idle.eta_to_hard_min(msnap.samples, msnap.settings)
        if dv.kind in ("pending", "hold") and msnap.samples
        else None
    )
    return replace(
        dv,
        growth=waiting.growth if waiting else None,
        window=waiting.window if waiting else None,
        window_min=waiting.window_min if waiting else None,
        eta_hard_min=eta,
    )


def _computed(msnap: Snapshot, *, now: float) -> DecisionView:
    decision = policy.decide(msnap)
    if isinstance(decision, Switch):
        target, trigger = decision.target, decision.trigger
        kind: DecisionKind = "switch"
    else:
        trigger = None
        target = None
        if isinstance(decision, Hold):
            kind = "pending" if decision.pending else "hold"
            if decision.pending:
                landing = policy.landing_candidates(msnap)
                target = landing[0].number if landing else None
        else:
            kind = _kind(type(decision).__name__.lower(), False)
    return DecisionView(
        kind=kind, active=msnap.active, target=target, trigger=trigger,
        reason=decision.reason, at=now, source="computed",
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
    decision for the current active account, else recomputed here."""
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
        dv = DecisionView(
            kind=_kind(own.decision, own.pending),
            active=own.active,
            target=_target_in(own.reason, own.active),
            trigger=own.trigger,
            reason=own.reason,
            at=own_at if own_at is not None else now,
            source="here",
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


def engine_status(
    *,
    held_elsewhere: bool,
    holder_pid: int | None,
    own: str | None,
    service: Mapping | None,
) -> EngineStatus:
    """Who holds the engine lease. ``own`` is ``"live"``/``"dry"`` when this
    TUI runs the engine. The service is recognised by its pid."""
    if own == "live":
        return EngineStatus("here-live", holder_pid, service)
    if own == "dry":
        return EngineStatus("here-dry", holder_pid, service)
    if held_elsewhere:
        if (
            service is not None
            and service.get("running")
            and holder_pid is not None
            and service.get("pid") == holder_pid
        ):
            return EngineStatus("service", holder_pid, service)
        return EngineStatus("other", holder_pid, service)
    return EngineStatus("none", None, service)


def _manager(service: Mapping | None) -> str:
    return "systemd" if service and service.get("platform") == "linux" else "launchd"


def engine_parts(es: EngineStatus) -> tuple[list[tuple[str, int]], Tone]:
    service = es.service
    if es.holder == "service":
        parts = [
            ("● service", 0),
            (f"{_manager(service)} · pid {es.pid}", 2),
            ("holds the lease — this TUI is a viewer", 1),
        ]
        tone = "plain"
    elif es.holder == "other":
        who = f"● pid {es.pid}" if es.pid else "● another process"
        parts = [
            (who, 0),
            ("not the service: a terminal cc-swap auto or the menu bar", 2),
            ("viewer", 1),
        ]
        tone = "plain"
    elif es.holder == "here-dry":
        parts = [("● here · DRY-RUN", 0), ("watching only, nothing switches", 1)]
        tone = "warn"
    elif es.holder == "here-live":
        pid = f" (pid {es.pid})" if es.pid else ""
        parts = [(f"● here · LIVE{pid}", 0), ("quitting stops it", 1)]
        tone = "accent"
    else:
        if service is not None and service.get("installed"):
            state = service.get("state") or "not running"
            why = f"service stopped ({_manager(service)}: {state})"
        elif service is not None:
            why = "no service (cc-swap service install)"
        else:
            why = ""
        parts = [("○ nothing is switching", 0)]
        if why:
            parts.append((why, 2))
        parts.append(("m to run one here", 1))
        tone = "warn"
    if service is not None and service.get("linger") is False:
        parts.append(("linger off: stops at logout", 3))
        tone = "warn"
    return parts, tone


def engine_line(es: EngineStatus, width: int = 1000) -> Cell:
    parts, tone = engine_parts(es)
    return _fit(parts, width), tone


# -- lines --------------------------------------------------------------------------------


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


def _prime_parts(
    rows: Sequence[FleetRow], prime: PrimeSettings
) -> tuple[list[tuple[str, int]], Tone]:
    if not prime.enabled:
        cold = sum(
            1 for r in rows
            if r.state5 == "cold" and not r.active and r.tier != "excluded" and r.login == "ok"
        )
        parts = [("priming off (s → Swap strategy)", 0)]
        if cold:
            parts.append((f"{cold} cold account{'s' if cold != 1 else ''} idle", 1))
        return parts, "dim"
    due = [r for r in rows if r.prime.kind == "due"]
    windows = sorted(
        (r for r in rows if r.prime.kind == "window"), key=lambda r: r.prime.lo or 0.0
    )
    blocked = [r for r in rows if r.prime.kind == "skip" and r.prime.note not in ("—", "")]
    parts: list[tuple[str, int]] = []
    if due:
        by = max(r.prime.hi or 0.0 for r in due)
        parts.append((" ".join(f"#{r.number}" for r in due) + f" due ≤{hhmm(by)}", 0))
    for i, r in enumerate(windows):
        parts.append((f"#{r.number} {prime_text(r.prime)}", 3 + i))
    for r in blocked:
        note = "needs re-login" if r.prime.note == "re-login" else r.prime.note
        parts.append((f"#{r.number} {note}", 2))
    if not parts:
        parts.append(("nothing to prime", 0))
    return parts, "plain"


def status_lines(
    es: EngineStatus,
    dv: DecisionView,
    rows: Sequence[FleetRow],
    mx: MaximizeSettings,
    prime: PrimeSettings,
    *,
    now: float,
    width: int,
) -> list[Cell]:
    """The three status lines under the header: engine, now, prime."""
    out: list[Cell] = []
    for label, (parts, tone) in (
        ("engine  ", engine_parts(es)),
        ("now     ", decision_parts(dv, now=now)),
        ("prime   ", _prime_parts(rows, prime)),
    ):
        out.append((_fit(parts, width, prefix=label), tone))
    return out


def relogin_count(rows: Sequence[FleetRow]) -> int:
    return sum(1 for r in rows if r.login == "relogin")


def header_line(
    mx: MaximizeSettings,
    prime: PrimeSettings,
    rows: Sequence[FleetRow],
    *,
    host: str | None,
    ssh: bool,
    now: float,
    width: int,
) -> str:
    """``cc-swap @ host (ssh) · maximize · 5h 50/95 · 7d 90/98 · margin 5 ·
    priming on`` with the clock right-aligned; parts drop when narrow."""
    head = "cc-swap"
    if host:
        head += f" @ {host}" + (" (ssh)" if ssh else "")
    parts: list[tuple[str, int]] = [
        (head, 0),
        ("maximize", 3),
        (f"5h {mx.soft_5h:g}/{mx.hard_5h:g}", 4),
        (f"7d {mx.soft_7d:g}/{mx.hard_7d:g}", 4),
        (f"margin {mx.landing_margin:g}", 6),
        (f"priming {'on' if prime.enabled else 'off'}", 5),
    ]
    count = relogin_count(rows)
    if count:
        parts.append((f"{count} need{'s' if count == 1 else ''} re-login", 0))
    clock = time.strftime("%a %H:%M", time.localtime(now))
    room = width - len(clock) - 1
    left = _fit(parts, room)
    if not left.endswith("…") and len(left) <= room:
        return left + " " * (width - len(left) - len(clock)) + clock
    return _fit(parts, width)  # the clock goes before anything essential


def attention(rows: Sequence[FleetRow]) -> str | None:
    """One crit line naming every account whose login only a re-login fixes."""
    dead = [r for r in rows if r.login == "relogin"]
    if not dead:
        return None
    names = ", ".join(f"#{r.number} {r.name}" for r in dead)
    if len(dead) == 1:
        return f"⚠ {names} needs re-login (refresh token dead) — select it and press r"
    return f"⚠ {names} need re-login (refresh tokens dead) — select one and press r"


# -- table ----------------------------------------------------------------------------------


ALL_COLUMNS: tuple[str, ...] = (
    "mark", "#", "account", "plan", "tier", "rank",
    "5h", "7d", "7d in", "pace", "land", "5h window", "next prime",
)
COLUMN_LABELS = {
    "mark": "", "#": "#", "account": "account", "plan": "plan", "tier": "tier",
    "rank": "rank", "5h": "5h", "7d": "7d", "7d in": "7d in", "pace": "pace",
    "land": "land", "5h window": "5h window", "5h win": "5h", "next prime": "next prime",
}


def columns_for(width: int) -> tuple[str, ...]:
    """The table's columns at ``width``: always mark, #, account, 5h, 7d,
    land and the 5h window; then pace, next prime, tier, plan, 7d in and
    rank, dropped in reverse order as the screen narrows."""
    drop: set[str] = set()
    if width < 112:
        drop |= {"rank", "7d in"}
    if width < 100:
        drop |= {"plan", "tier"}
    if width < 64:
        drop |= {"next prime"}
    cols = [c for c in ALL_COLUMNS if c not in drop]
    if width < 64:
        cols = ["5h win" if c == "5h window" else c for c in cols]
    return tuple(cols)


def _pct_cell(pct: float | None, soft: float, hard: float, stale: bool) -> Cell:
    if pct is None:
        return "?", "dim"
    text = f"{'~' if stale else ''}{pct:.0f}%"
    if stale:
        return text, "dim"
    if pct >= hard:
        return text, "crit"
    if pct >= soft:
        return text, "warn"
    return text, "ok"


def _window_cell(row: FleetRow, *, short: bool) -> Cell:
    if row.login == "relogin":
        return "?", "crit"
    if row.state5 == "cold" or row.reset5 is None:
        return "cold", "dim"
    clock = hhmm(row.reset5)
    if short:
        return (f"prim {clock}" if row.state5 == "primed" else f"run {clock}"), (
            "accent" if row.state5 == "primed" else "plain"
        )
    if row.state5 == "primed":
        return f"primed → {clock}", "accent"
    return f"running → {clock}", "plain"


def row_cells(
    row: FleetRow, columns: Sequence[str], *, now: float, mx: MaximizeSettings
) -> tuple[Cell, ...]:
    """The table cells of one row, in ``columns`` order."""
    dead = row.login == "relogin"
    dim_tier = row.tier != "normal"
    name = row.name
    if "tier" not in columns and row.tier in TIER_SUFFIX:
        name += TIER_SUFFIX[row.tier]
    cells: list[Cell] = []
    for col in columns:
        if col == "mark":
            cell: Cell = ("*", "bold") if row.active else (" ", "plain")
        elif col == "#":
            cell = (row.number, "crit" if dead else ("bold" if row.active else "plain"))
        elif col == "account":
            cell = (name, "crit" if dead else ("bold" if row.active else "plain"))
        elif col == "plan":
            cell = (row.plan, "dim" if row.plan in ("?", "api") else "plain")
        elif col == "tier":
            cell = (TIER_CELLS.get(row.tier, row.tier), "dim" if dim_tier else "plain")
        elif col == "rank":
            if row.tier == "excluded":
                cell = ("—", "dim")
            else:
                cell = (str(row.rank) if row.rank is not None else "?", "plain")
        elif col == "5h":
            if row.login != "ok":
                cell = _LOGIN_CELLS[row.login]
            else:
                cell = _pct_cell(row.pct5, mx.soft_5h, mx.hard_5h, row.stale)
        elif col == "7d":
            if row.login != "ok" and row.pct7 is None:
                cell = ("—", "crit" if dead else "dim")
            else:
                cell = _pct_cell(row.pct7, mx.soft_7d, mx.hard_7d, row.stale)
        elif col == "7d in":
            cell = (f"{row.days7:.1f}d", "plain") if row.days7 is not None else ("—", "dim")
        elif col == "pace":
            cell = (f"{row.score:.2f}", "plain") if row.score is not None else ("—", "dim")
        elif col == "land":
            if dead:
                cell = ("re-login", "crit")
            elif row.land == "yes":
                cell = ("yes", "ok")
            elif row.land == "active":
                cell = ("active", "bold")
            else:
                cell = (row.land, "dim")
        elif col in ("5h window", "5h win"):
            cell = _window_cell(row, short=col == "5h win")
        elif col == "next prime":
            text = prime_text(row.prime)
            if row.prime.kind == "due":
                tone = "accent"
            elif text == "re-login":
                tone = "crit"
            else:
                tone = "plain" if row.prime.kind == "window" else "dim"
            cell = (text, tone)
        else:
            cell = ("", "plain")
        cells.append(cell)
    return tuple(cells)


def detail_line(row: FleetRow, mx: MaximizeSettings) -> str:
    """The fork line under the detail card: rank, plan, pace explained, the
    landing verdict in words and the 5h window."""
    parts: list[str] = []
    if row.tier == "excluded":
        parts.append("excluded from rotation")
    else:
        parts.append(f"rank {row.rank}" if row.rank is not None else "rank ?")
    parts.append(row.plan)
    if row.score is not None and row.pct7 is not None and row.days7 is not None:
        parts.append(
            f"pace {row.score:.2f} ({100 - row.pct7:.0f}% left over {row.days7:.1f}d)"
        )
    land = {
        "yes": "landable",
        "active": "active",
        "re-login": "needs re-login (press r)",
        "excluded": "excluded (x includes it)",
        "quarant.": "quarantined (no usable stored login)",
        "usage ?": "usage unknown",
        "api key": "API key (no quota)",
    }.get(row.land)
    if land is None:
        land = f"not landable ({row.land}: under both soft marks minus {mx.landing_margin:g})"
    parts.append(land)
    if row.tier == "last_resort":
        parts.append("last resort (l toggles)")
    if row.state5 == "primed" and row.reset5 is not None:
        parts.append(f"5h primed → {hhmm(row.reset5)}")
    elif row.state5 == "running" and row.reset5 is not None:
        parts.append(f"5h running → {hhmm(row.reset5)}")
    else:
        parts.append("5h cold")
    return " · ".join(parts)


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
) -> list[str]:
    """What to do to re-login ``row`` — cc-swap launches nothing itself."""
    where = f"on this machine ({host or 'this host'}{', over SSH' if ssh else ''})"
    claude = claude_path or "claude"
    back = (
        f"switches back to #{return_to.number} {return_to.name}"
        if return_to is not None and return_to.number != row.number
        else "stays on it"
    )
    lines = [
        f"Re-login #{row.number} {row.name} ({row.email}) — its refresh token is dead; "
        "only a fresh login fixes it.",
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


# -- layout -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutPlan:
    detail: bool
    prime_line: bool
    menu: Literal["full", "folded"]
    keys: Literal["full", "minimal"]
    blanks: bool
    columns: tuple[str, ...]


def fit_layout(height: int, width: int, n_rows: int, *, attention: bool) -> LayoutPlan:
    """What fits on a ``height``×``width`` terminal. Depends only on the
    size and the account count, never on the cursor. Kept in order: the
    attention line, the engine/now lines and every row; the menu (vertical,
    then folded); the key hints; the prime line; the detail card; blank
    lines. The thresholds are for 6 accounts; each extra row costs a line."""
    h = height - max(0, n_rows - 6)
    return LayoutPlan(
        detail=h >= 29,
        prime_line=h >= 18,
        menu="full" if h >= 24 else "folded",
        keys="full" if h >= 18 else "minimal",
        blanks=h >= 18,
        columns=columns_for(width),
    )
