"""The Fleet home screen's pure model: no Textual, no Rich, no I/O.

Fleet (``tui/fleet.py``) is the maximize home screen. It shows:

* one plain-English sentence about the engine (:func:`status_variants`), with
  a dim note on the right saying who runs it (:func:`holder_variants`);
* at most one attention line, only when something needs you
  (:func:`attention_parts`);
* every account as a block in the upstream dashboard's style, in the order
  :func:`ordered_rows` gives, each with at most one tag (:func:`tag_for`);
* a layout picked from the terminal size alone (:func:`home_layout`).

The sentence never presents an old decision as the engine's current one:
:func:`situation` tells a live decision from one the engine has not
confirmed yet (``waiting``) and from an engine that stopped reporting
(``stale``). Everything here takes ``now``; the widget only lays it out.

Tones are ``maximize/fleet.py``'s (``ok``, ``warn``, ``crit``, ``dim``,
``accent``, ``plain``, ``bold``) plus ``okb``/``warnb`` (bold ok/warn) and
``active``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from claude_swap import oauth
from claude_swap.maximize import fleet as fx
from claude_swap.settings import MaximizeSettings

Mode = Literal["wide", "medium", "narrow"]
Tone = str
Seg = tuple[str, Tone]

#: At least this many columns (and NARROW_MAX_ROWS rows): two columns of blocks.
WIDE_MIN_COLS = 140
#: Below this many columns: one line per account plus the selected one expanded.
MEDIUM_MIN_COLS = 100
#: Fewer rows than this is narrow whatever the width (blocks would not fit).
NARROW_MAX_ROWS = 30
#: Fewer rows than this drops the blank lines around the status block.
BLANKS_MIN_ROWS = 20


# -- layout -----------------------------------------------------------------------------


def layout_mode(width: int, height: int) -> Mode:
    """``wide`` (≥140 cols), ``medium`` (100–139) or ``narrow`` (<100 cols or
    <30 rows). Depends only on the terminal size."""
    if width < MEDIUM_MIN_COLS or height < NARROW_MAX_ROWS:
        return "narrow"
    if width >= WIDE_MIN_COLS:
        return "wide"
    return "medium"


@dataclass(frozen=True)
class HomeLayout:
    mode: Mode
    columns: int   # account blocks side by side (narrow: the one-line list)
    gap: int       # spaces between two columns of blocks
    max_bar: int   # the longest a usage bar gets
    blanks: bool   # blank lines around the status block


def home_layout(width: int, height: int) -> HomeLayout:
    """Everything about the layout that follows from the terminal size."""
    mode = layout_mode(width, height)
    return HomeLayout(
        mode=mode,
        columns=2 if mode == "wide" else 1,
        gap=4,
        max_bar={"wide": 34, "medium": 60, "narrow": 40}[mode],
        blanks=height >= BLANKS_MIN_ROWS,
    )


def column_width(width: int, layout: HomeLayout) -> int:
    """One block's width when ``layout.columns`` blocks share ``width``."""
    return (width - layout.gap * (layout.columns - 1)) // layout.columns


def step_selection(
    order: Sequence[str], selected: str | None, direction: str, columns: int = 1
) -> str | None:
    """The account selected after one arrow key. ``order`` is the display
    order, laid out row by row in ``columns`` columns: up/down move a whole
    row (to the block above or below, or to the last block when the row
    below is shorter), left/right one block within a row. Never wraps; an
    unknown selection starts at the first account."""
    if not order:
        return None
    if selected not in order:
        return order[0]
    i = order.index(selected)
    cols = max(columns, 1)
    last = len(order) - 1
    if direction == "down":
        j = min(i + cols, last) if i // cols < last // cols else i
    elif direction == "up":
        j = i - cols if i - cols >= 0 else i
    elif direction == "right":
        j = i + 1 if cols > 1 and i % cols < cols - 1 and i + 1 < len(order) else i
    elif direction == "left":
        j = i - 1 if cols > 1 and i % cols > 0 else i
    else:
        j = i
    return order[j]


# -- order and tags --------------------------------------------------------------------


def ordered_rows(rows: Sequence[fx.FleetRow], picks: Sequence[str]) -> list[fx.FleetRow]:
    """The active account, then the engine's pick order (``picks``:
    ``policy.landing_candidates``), then the rest by rank and slot, with
    excluded accounts last."""
    by_number = {r.number: r for r in rows}
    out = [r for r in rows if r.active]
    out += [by_number[n] for n in picks if n in by_number and not by_number[n].active]
    seen = {r.number for r in out}
    rest = [r for r in rows if r.number not in seen]
    rest.sort(key=lambda r: (r.tier == "excluded", r.rank is None, r.rank or 0))
    return out + rest


def short_left(seconds: float) -> str:
    """``3d`` / ``5h`` for a login's time left (``expired`` once past)."""
    if seconds <= 0:
        return "expired"
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    return f"{max(int(seconds // 3600), 1)}h"


#: The tags in priority order: an account shows only the first that applies.
TAG_PRIORITY: tuple[str, ...] = (
    "active", "re-login", "excluded", "next", "login", "last resort", "5h off",
)


def tag_for(
    row: fx.FleetRow, *, is_next: bool, now: float, priming: bool = True
) -> tuple[str, Tone] | None:
    """The one right-aligned tag: the most important thing about the
    account (:data:`TAG_PRIORITY`). ``priming`` False hides the next prime
    time (priming does not run while automatic switching is off)."""
    if row.active:
        return "● active", "active"
    if row.login == "relogin":
        return "re-login (r)", "crit"
    if row.tier == "excluded":
        return "excluded", "dim"
    if is_next:
        return "next", "accent"
    if fx.login_due(row, now):
        left = fx.login_left(row, now) or 0.0
        tone = "crit" if left < fx.LOGIN_URGENT_S else "warn"
        return (f"login {short_left(left)} left" if left > 0 else "login expired"), tone
    if row.tier == "last_resort":
        return "last resort", "dim"
    if row.state5 == "cold" and row.login == "ok":  # an API key has no 5h window
        when = None
        cell = row.prime
        if priming and cell.kind == "due":
            when = "now" if (cell.hi or 0.0) <= now else fx.hhmm(cell.hi or now)
        elif priming and cell.kind == "window" and cell.lo is not None:
            when = fx.hhmm(cell.lo)
        return (f"5h off · prime {when}" if when else "5h off"), "dim"
    return None


# -- how live the engine's word is ------------------------------------------------------


Situation = Literal["paused", "auto-off", "no-engine", "stale", "waiting", "live"]


def situation(
    es: fx.EngineStatus,
    dv: fx.DecisionView,
    *,
    active: str | None,
    published_at: float | None,
    now: float,
    poll_s: float,
) -> Situation:
    """What the status sentence may claim.

    ``live``: the decision is the engine's own, recent, about the account
    active now. ``waiting``: the engine runs but has not decided about the
    current account yet (it just started, or the active account just
    changed). ``stale``: the engine holds the lease but its last decision is
    older than ``fleet.fresh_s`` — it stopped reporting. ``published_at``
    is when the state file's decision was written (None: never)."""
    if dv.kind == "paused":
        return "paused"
    if es.auto_off or dv.kind == "off":
        return "auto-off"
    if es.holder == "none":
        return "no-engine"
    fresh = fx.fresh_s(poll_s)
    if dv.source in ("engine", "here"):
        if dv.at is not None and now - dv.at > fresh:
            return "stale"
        return "live" if dv.active == active else "waiting"
    # Computed here: no fresh published decision about the active account.
    if es.holder in ("here-live", "here-dry"):
        return "waiting"  # our own engine has not decided yet
    if published_at is None or now - published_at < 0:
        return "waiting"
    return "stale" if now - published_at > fresh else "waiting"


def switching_live(sit: Situation) -> bool:
    """Whether the engine may switch on its own right now (``next`` tags and
    the next prime time are shown)."""
    return sit in ("live", "waiting")


def next_number(dv: fx.DecisionView, picks: Sequence[str], sit: Situation) -> str | None:
    """The account automatic switching goes to next: the decision's target
    while it is moving, else the engine's first pick. None while nothing
    switches or the engine stopped reporting."""
    if not switching_live(sit):
        return None
    if dv.kind in ("pending", "switch") and dv.target:
        return dv.target
    return picks[0] if picks else None


# -- the status sentence ---------------------------------------------------------------


def eta_text(minutes: float) -> str:
    """``~40m`` / ``~2h`` / ``~1h20m``."""
    m = max(int(round(minutes)), 1)
    if m < 60:
        return f"~{m}m"
    h, r = divmod(m, 60)
    if r < 10:
        return f"~{h}h"
    if r > 50:
        return f"~{h + 1}h"
    return f"~{h}h{r:02d}m"


def ago_text(seconds: float) -> str:
    """``25m`` / ``3h`` / ``2d`` ago, for an engine that stopped reporting."""
    s = max(int(seconds), 0)
    if s < 3600:
        return f"{max(s // 60, 1)}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _past_soft(row: fx.FleetRow, mx: MaximizeSettings) -> tuple[str, float, float] | None:
    """The window (label, pct, soft) the account is past its soft mark in."""
    if row.pct5 is not None and row.pct5 >= mx.soft_5h:
        return "5h", row.pct5, mx.soft_5h
    if row.pct7 is not None and row.pct7 >= mx.soft_7d:
        return "7d", row.pct7, mx.soft_7d
    return None


def status_variants(
    es: fx.EngineStatus,
    dv: fx.DecisionView,
    rows: Sequence[fx.FleetRow],
    mx: MaximizeSettings,
    sit: Situation,
    *,
    now: float,
    published_at: float | None = None,
) -> list[list[Seg]]:
    """The status sentence as tone segments, longest variant first; the
    widget shows the first that fits (it never wraps)."""
    by = {r.number: r for r in rows}

    def name(n: str | None) -> str:
        r = by.get(n or "")
        return f"#{n} {r.name}" if r else (f"#{n}" if n else "?")

    if sit == "paused":
        until = fx.hhmm(dv.at) if dv.at else "?"
        why = "re-login" if dv.reason == "relogin" else (dv.reason or "a pause")
        return [
            [("Paused", "warnb"), (f" · {why} in progress — nothing switches until {until}", "plain")],
            [("Paused", "warnb"), (f" until {until}", "plain")],
            [("Paused", "warnb")],
        ]
    if sit == "auto-off":
        return [
            [("Auto OFF", "warnb"), (" — nothing switches automatically ", "plain"),
             ("(m to turn on)", "dim")],
            [("Auto OFF", "warnb"), (" — nothing switches ", "plain"), ("(m)", "dim")],
            [("Auto OFF", "warnb")],
        ]
    if sit == "no-engine":
        stopped = bool(es.service and es.service.get("installed"))
        why = "the service is installed but stopped" if stopped else "no engine is running"
        return [
            [("Not switching", "warnb"), (f" — {why} ", "plain"), ("(m to start one)", "dim")],
            [("Not switching", "warnb"), (" — no engine ", "plain"), ("(m)", "dim")],
            [("Not switching", "warnb")],
        ]
    dry = es.holder == "here-dry"
    head: Seg = ("Dry run", "warnb") if dry else ("Auto ON", "okb")
    if sit == "stale":
        last = published_at if dv.source == "computed" else dv.at
        when = fx.hhmm(last) if last else "?"
        ago = f" ({ago_text(now - last)} ago)" if last else ""
        stale_head: Seg = (head[0], "warnb")
        return [
            [stale_head, (f" · the engine has not reported since {when}{ago} — "
                          "nothing below is live ", "warn"), ("(m → e engine log)", "dim")],
            [stale_head, (f" · engine silent since {when}{ago} ", "warn"), ("(m → e)", "dim")],
            [stale_head, (f" · engine silent since {when}", "warn")],
            [stale_head, (" · engine silent", "warn")],
        ]
    act = by.get(dv.active or "") or next((r for r in rows if r.active), None)
    if sit == "waiting":
        using = f" · using {name(act.number)}" if act else ""
        return [
            [head, (f"{using} · ", "plain"), ("waiting for the engine's next check", "dim")],
            [head, (f"{using} · ", "plain"), ("waiting for the engine", "dim")],
            [head],
        ]
    will = "would switch" if dry else "will switch"
    if dv.kind == "pending" and act is not None:
        win = dv.window or "5h"
        pct = act.pct5 if win == "5h" else act.pct7
        soft = mx.soft_5h if win == "5h" else mx.soft_7d
        hard = mx.hard_5h if win == "5h" else mx.hard_7d
        p = f"{pct:.0f}%" if pct is not None else "?"
        eta = dv.eta_hard_min
        forced = f"(forced at {hard:g}%" + (f", {eta_text(eta)})" if eta is not None else ")")
        target = name(dv.target) if dv.target else "the next account"
        short_target = f"#{dv.target}" if dv.target else "the next account"
        return [
            [head, (f" · using {name(act.number)} · {win} {p} past soft {soft:g}", "plain"),
             (f" — {will} to {target} when you pause ", "plain"), (forced, "dim")],
            [head, (f" · #{act.number} {win} {p} past soft {soft:g} — to {target} "
                    "when you pause ", "plain"),
             (f"(forced {eta_text(eta)})" if eta is not None else "", "dim")],
            [head, (f" · #{act.number} {win} {p} past soft {soft:g} — to {target} "
                    "when you pause", "plain")],
            [head, (f" · #{act.number} {win} {p} — to {short_target} when you pause", "plain")],
            [head, (f" · to {short_target} on pause", "plain")],
            [head],
        ]
    if dv.kind == "switch":
        trigger = f" ({dv.trigger})" if dv.trigger else ""
        verb = "would switch" if dry else "switching"
        return [
            [head, (f" · {verb} {name(dv.active)} → {name(dv.target)} now{trigger}", "plain")],
            [head, (f" · {verb} → #{dv.target}", "plain")],
            [head],
        ]
    if dv.kind == "exhausted":
        return [
            [head, (" · every account is at its limit — waiting for a reset", "crit")],
            [head, (" · all accounts at their limit", "crit")],
            [head],
        ]
    if dv.kind == "indeterminate":
        return [
            [head, (f" · {dv.reason} — it switches only if it must", "warn")],
            [head, (" · usage unreadable", "warn")],
            [head],
        ]
    if act is not None:
        past = _past_soft(act, mx)
        if past is not None:
            win, pct, soft = past
            hard = mx.hard_5h if win == "5h" else mx.hard_7d
            return [
                [head, (f" · using {name(act.number)} · {win} {pct:.0f}% past soft {soft:g}"
                        " — nowhere better to go yet, it stays ", "plain"),
                 (f"(forced at {hard:g}%)", "dim")],
                [head, (f" · #{act.number} {win} {pct:.0f}% past soft {soft:g} — it stays",
                        "plain")],
                [head],
            ]
        p = f"{act.pct5:.0f}%" if act.pct5 is not None else "?"
        return [
            [head, (f" · using {name(act.number)} · all fine ", "plain"),
             (f"(5h {p}, moves on past {mx.soft_5h:g}%)", "dim")],
            [head, (f" · using {name(act.number)} · all fine", "plain")],
            [head],
        ]
    return [[head, (" · waiting for the first reading", "dim")], [head]]


def holder_variants(es: fx.EngineStatus, sit: Situation) -> list[str]:
    """The dim right-aligned note on the status line, longest first."""
    if es.holder in ("service", "other"):
        who = f"service pid {es.pid}" if es.holder == "service" else (
            f"pid {es.pid}" if es.pid else "another process"
        )
        verb = {
            "auto-off": "is idle", "paused": "is paused", "stale": "is silent",
        }.get(sit, "is switching")
        return [f"viewer · {who} {verb}", "viewer", ""]
    if es.holder == "here-live":
        return ["engine runs here · quitting stops it", "engine here", ""]
    if es.holder == "here-dry":
        return ["engine here · dry run", ""]
    return [""]


def seg_len(segs: Sequence[Seg]) -> int:
    return sum(len(text) for text, _tone in segs)


def fit_variant(variants: Sequence[Sequence[Seg]], width: int) -> list[Seg]:
    """The first (longest) variant that fits ``width``; the last one, cut,
    when none does."""
    for variant in variants:
        if seg_len(variant) <= width:
            return list(variant)
    out: list[Seg] = []
    room = max(width, 0)
    for text, tone in variants[-1]:
        if room <= 0:
            break
        if len(text) > room:
            text = fx.clip(text, room)
        out.append((text, tone))
        room -= len(text)
    return out


def status_line(
    variants: Sequence[Sequence[Seg]], notes: Sequence[str], width: int
) -> tuple[list[Seg], str]:
    """The sentence that fits, and the longest note that still fits beside
    it (at least three spaces apart; ``""`` when none does)."""
    sentence = fit_variant(variants, width)
    used = seg_len(sentence)
    note = next((n for n in notes if n and used + 3 + len(n) <= width), "")
    return sentence, note


# -- the attention line ------------------------------------------------------------------


def attention_parts(
    rows: Sequence[fx.FleetRow],
    *,
    now: float,
    prime_guard: str | None = None,
    priming: bool = False,
    linger_off: bool = False,
) -> tuple[list[str], Tone] | None:
    """The attention line's parts, most important first (the widget keeps
    as many as fit), and its tone: red for a dead login or one inside its
    last day, amber otherwise. None when nothing needs you.

    ``prime_guard`` (``prime_verify.paused_note``) is named only while
    priming would otherwise run (``priming``)."""
    dead = [r for r in rows if r.login == "relogin"]
    due = sorted(
        (r for r in rows if fx.login_due(r, now)),
        key=lambda r: fx.login_left(r, now) or 0.0,
    )
    parts: list[str] = []
    if dead:
        r = dead[0]
        more = f" (+{len(dead) - 1} more)" if len(dead) > 1 else ""
        parts.append(f"! #{r.number} {r.name} needs re-login{more} — select it, press r")
    for r in due:
        left = fx.login_left(r, now) or 0.0
        when = "has expired" if left <= 0 else f"ends in {oauth.login_countdown(left)}"
        if parts:
            parts.append(f"#{r.number} {r.name} login {when}")
        else:
            parts.append(f"! #{r.number} {r.name} login {when} — select it, press r")
    if prime_guard and priming:
        guard = f"priming {prime_guard}"
        parts.append(guard if parts else f"! {guard}")
    if linger_off:
        note = "the service stops at logout (loginctl enable-linger $USER)"
        parts.append(note if parts else f"! {note}")
    if not parts:
        return None
    urgent = any((fx.login_left(r, now) or 0.0) < fx.LOGIN_URGENT_S for r in due)
    return parts, "crit" if dead or urgent else "warn"


def attention_line(parts: Sequence[str], width: int) -> str:
    """The first part (cut to ``width``), then as many more as fit."""
    text = fx.clip(parts[0], width) if parts else ""
    for extra in parts[1:]:
        if len(text) + 3 + len(extra) <= width:
            text += " · " + extra
    return text


# -- the footer ----------------------------------------------------------------------------


KEY_HINTS: tuple[tuple[str, str], ...] = (
    ("enter", "switch"), ("r", "re-login"), ("l", "last resort"),
    ("m", "menu"), ("?", "help"), ("q", "quit"),
)


def key_hints(width: int) -> list[tuple[str, str]]:
    """The footer's ``(key, what)`` pairs: every word when it fits
    (``enter switch · r re-login · …``), else the keys with only menu, help
    and quit spelled out."""
    full = " · ".join(f"{k} {w}" for k, w in KEY_HINTS)
    if len(full) <= width:
        return list(KEY_HINTS)
    return [(k, w if k in ("m", "?", "q") else "") for k, w in KEY_HINTS]
