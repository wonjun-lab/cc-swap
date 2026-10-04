"""The Fleet home screen's pure model: no Textual, no Rich, no I/O.

Fleet (``tui/fleet.py``) is the maximize home screen. It shows:

* one plain-English sentence about the engine (:func:`status_variants`), with
  a dim note on the right saying who runs it (:func:`holder_variants`); an
  account hold (``cc-swap hold``, ``h``) on the active account reads
  ``Holding #1 until 15:30 (2h left) — only hard 98%/100% will move you``;
* at most one attention line, only when something needs you
  (:func:`attention_parts`);
* a capacity summary over the table (:func:`capacity`,
  :func:`summary_variants`): ``5h free: 4 accounts · next 5h back 07:10
  (#3) · 7d left this week ≈ 2.3 accounts · next 7d reset Oct 5 12:51``;
* every account as one row of a table with column headers (``order ·
  account · plan · 5h · 5h resets · 7d · 7d resets · status``), in the order
  :func:`ordered_rows` gives: the ``order`` column numbers where automatic
  switching would go (:func:`order_marks`), every row says when both of its
  windows reset (:func:`resets_text`), and the status column, right after
  ``7d resets``, holds at most one tag (:func:`tag_for`);
* the table's columns picked from the terminal size and what the rows need
  (:func:`table_plan`), and under the table the selected account in full
  when there are rows to spare.

The sentence never presents an old decision as the engine's current one:
:func:`situation` tells a live decision from one the engine has not
confirmed yet (``waiting``) and from an engine that stopped reporting
(``stale``). A hold with its own code (``model.Hold.code``) gets its own
words: waiting out a reset (``reset-wait``, minutes counted from ``now``),
a pre-emptive move waiting for idle (``preempt``), a rebalance deferred
to your quiet time (``rebalance-deferred``) and a learned ride through the
last point (``ride``, minutes counted from ``now``); a ``preempt`` switch says why
it moved early. Everything here takes ``now``; the widget only lays it out.

Tones are ``maximize/fleet.py``'s (``ok``, ``warn``, ``crit``, ``dim``,
``accent``, ``plain``, ``bold``) plus ``okb``/``warnb`` (bold ok/warn) and
``active``.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from claude_swap import oauth
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import hold as account_hold
from claude_swap.settings import MaximizeSettings

Tone = str
Seg = tuple[str, Tone]

#: Fewer rows than this drops the blank lines around the status block.
BLANKS_MIN_ROWS = 20
#: Fewer rows than this never shows the capacity summary (every line goes
#: to the table).
SUMMARY_MIN_ROWS = 12


def step_selection(order: Sequence[str], selected: str | None, direction: str) -> str | None:
    """The account selected after one arrow key: up/down move one row of
    the table (``order`` is the display order). Never wraps; an unknown
    selection starts at the first account."""
    if not order:
        return None
    if selected not in order:
        return order[0]
    i = order.index(selected)
    if direction == "down":
        i = min(i + 1, len(order) - 1)
    elif direction == "up":
        i = max(i - 1, 0)
    return order[i]


# -- order and tags --------------------------------------------------------------------


def unusable(row: fx.FleetRow, now: float | None = None) -> bool:
    """Automatic switching never goes here: a dead login (re-login), an
    excluded account, or (given ``now``) a login past its deadline."""
    if row.login == "relogin" or row.tier == "excluded":
        return True
    left = fx.login_left(row, now) if now is not None else None
    return left is not None and left <= 0 and row.login != "api"


def ordered_rows(
    rows: Sequence[fx.FleetRow], picks: Sequence[str], *, now: float | None = None
) -> list[fx.FleetRow]:
    """The active account, then the engine's pick order (``picks``:
    ``policy.landing_candidates``), then the rest by rank and slot; the
    accounts switching never goes to (:func:`unusable`) after those, and
    excluded accounts last."""
    by_number = {r.number: r for r in rows}
    out = [r for r in rows if r.active]
    out += [by_number[n] for n in picks if n in by_number and not by_number[n].active]
    seen = {r.number for r in out}
    rest = [r for r in rows if r.number not in seen]
    rest.sort(key=lambda r: (
        r.tier == "excluded", unusable(r, now), r.rank is None, r.rank or 0,
    ))
    return out + rest


#: The ``order`` column: the active account, and an account switching never
#: goes to.
ORDER_ACTIVE = "●"
ORDER_NONE = "–"


def order_marks(rows: Sequence[fx.FleetRow], now: float) -> dict[str, str]:
    """The ``order`` column for ``rows`` in display order
    (:func:`ordered_rows`): ``●`` on the active account, ``1``, ``2``, ``3``
    … on the others in the order automatic switching would try them, and
    ``–`` on an account it never goes to (:func:`unusable`)."""
    out: dict[str, str] = {}
    n = 0
    for row in rows:
        if row.active:
            out[row.number] = ORDER_ACTIVE
        elif unusable(row, now):
            out[row.number] = ORDER_NONE
        else:
            n += 1
            out[row.number] = str(n)
    return out


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
    """The one tag in the status column: the most important thing about the
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


def status_for(
    row: fx.FleetRow, *, is_next: bool, now: float, priming: bool = True
) -> tuple[str, Tone] | None:
    """The status column: the account's tag (:func:`tag_for`), else a dim
    ``primed`` when priming opened the 5h window it is in."""
    tag = tag_for(row, is_next=is_next, now=now, priming=priming)
    if tag is None and row.state5 == "primed" and row.login == "ok":
        return "primed", "dim"
    return tag


# -- when the windows reset ----------------------------------------------------------------

#: A reset nothing says anything about, and a 5h window that is not running.
NOT_KNOWN = "—"
NOT_STARTED = "not started"


def countdown(seconds: float) -> str:
    """``0h47m`` / ``1h47m`` / ``3d19h`` / ``3d02h``: the time left until a
    reset, fixed width so a column of them lines up."""
    s = max(int(seconds), 0)
    if s < 86400:
        h, m = divmod(max(s // 60, 1), 60)
        return f"{h}h{m:02d}m"
    d, h = divmod(s // 3600, 24)
    return f"{d}d{h:02d}h"


def reset_clock(ts: float, now: float, *, date: bool = True, pad: bool = False) -> str:
    """Local ``07:10`` today (or always, without ``date``), else ``Oct 7
    02:18`` (``Oct  7 02:18`` with ``pad``, so a column of them lines up)."""
    at = time.localtime(ts)
    if not date or at[:3] == time.localtime(now)[:3]:
        return time.strftime("%H:%M", at)
    day = f"{at.tm_mday:>2}" if pad else str(at.tm_mday)
    return time.strftime("%b ", at) + day + time.strftime(" %H:%M", at)


def resets_text(reset: float | None, now: float, *, clock: bool, date: bool = True) -> str:
    """``3d19h · Oct  7 02:18`` (``clock``) or ``3d19h``; ``now`` once it
    has passed, ``—`` when unknown. ``date`` False never names the day (a
    5h window is at most five hours away: ``1h47m · 07:10``)."""
    if reset is None:
        return NOT_KNOWN
    if reset <= now:
        return "now"
    left = countdown(reset - now)
    return f"{left} · {reset_clock(reset, now, date=date, pad=True)}" if clock else left


def exact_reset(reset: float | None, now: float) -> str:
    """The detail panel's ``resets 07:10 (in 1h47m)``."""
    if reset is None:
        return NOT_KNOWN
    if reset <= now:
        return "resets now"
    return f"resets {reset_clock(reset, now)} (in {countdown(reset - now)})"


def row_resets(row: fx.FleetRow, window: str, now: float, *, clock: bool) -> str:
    """One row's ``5h resets`` (``1h47m``: the countdown alone at every
    width, ``clock`` or not — the detail panel has the exact time) or ``7d
    resets`` (``3d19h · Oct 7 02:18``, the clock only while ``clock``) cell:
    ``not started`` for a 5h window that is not running (a working login),
    else :func:`resets_text`."""
    if window == "5h":
        if row.login == "ok" and row.pct5 is not None and row.state5 == "cold":
            return NOT_STARTED
        return resets_text(row.reset5, now, clock=False)
    return resets_text(row.reset7, now, clock=clock)


# -- the table ----------------------------------------------------------------------------------
#
# One row per account, a header over it. The columns, left to right:
#
#   order  account           plan  5h            5h resets      7d  …  7d resets  status
#     ●    main@acme.dev #1  20x   ━━━┃━━━┃ 62%  1h47m      …      …          ● active
#
# ``status`` follows ``7d resets`` directly, never the terminal's right
# edge. :func:`table_plan` fits the columns to the terminal.

#: Column keys in order, and their headers.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("order", "order"), ("account", "account"), ("plan", "plan"), ("5h", "5h"),
    ("reset5", "5h resets"), ("7d", "7d"), ("reset7", "7d resets"), ("status", "status"),
)
HEADERS = dict(COLUMNS)
#: Columns (and the text the screen lays out) leave this many terminal
#: columns: one of padding on the left, two on the right (the scrollbar).
MARGIN = 3
#: The widest a usage bar gets, and the narrowest before it gives way.
MAX_BAR = 24
MIN_BAR = 6
#: The percentage after a bar: `` 62%``.
PCT_W = 4
#: The account column is never wider than this for the name itself (the
#: dim `` #4`` slot after it comes on top), …
NAME_CAP = 32
#: … and gives up characters only down to this before the bars go (the
#: percentages stay): ``team.shared@…`` tells accounts apart, ``team.s…``
#: does not.
MIN_NAME = 13
#: Blank columns between two columns, and when room is tight.
GAP, TIGHT_GAP = 2, 1


def cells(text: str) -> int:
    """Terminal cells ``text`` takes (wide East Asian characters take two)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def text_width(width: int) -> int:
    """The width every line of the home screen is laid out in."""
    return max(width - MARGIN, 20)


@dataclass(frozen=True)
class TableNeeds:
    """What the rows need, measured from them (:func:`table_needs`)."""

    rows: int = 0
    name: int = 0          # the longest display name, in cells
    slot: int = 3          # the longest `` #4`` after a name, its space included
    plan: int = 0          # the longest plan label
    reset5: int = 0        # the longest 5h resets cell, with the clock …
    reset5_short: int = 0  # … and without it
    reset7: int = 0
    reset7_short: int = 0
    status: int = 0        # the longest tag
    detail: int = 0        # lines the selected account's panel takes (0: none)


def plan_text(row: fx.FleetRow) -> str:
    return NOT_KNOWN if row.plan == "?" else row.plan


def account_slot(row: fx.FleetRow) -> str:
    """The dim slot number after the name: `` #4`` (what the attention line
    and the CLI call it)."""
    return f" #{row.number}"


def table_needs(
    rows: Sequence[fx.FleetRow],
    statuses: Mapping[str, tuple[str, Tone] | None],
    *,
    now: float,
    detail: int = 0,
) -> TableNeeds:
    """Measure ``rows`` (``statuses``: number -> :func:`status_for`)."""
    def widest(texts) -> int:
        return max((cells(t) for t in texts), default=0)

    return TableNeeds(
        rows=len(rows),
        name=widest(r.name for r in rows),
        slot=widest(account_slot(r) for r in rows),
        plan=widest(plan_text(r) for r in rows),
        reset5=widest(row_resets(r, "5h", now, clock=True) for r in rows),
        reset5_short=widest(row_resets(r, "5h", now, clock=False) for r in rows),
        reset7=widest(row_resets(r, "7d", now, clock=True) for r in rows),
        reset7_short=widest(row_resets(r, "7d", now, clock=False) for r in rows),
        status=widest(s[0] for s in statuses.values() if s),
        detail=detail,
    )


@dataclass(frozen=True)
class TablePlan:
    """How the table fits the terminal (:func:`table_plan`)."""

    bar: int                                  # cells per usage bar (0: the % only)
    clock: bool                               # reset clocks after the countdowns
    plan: bool                                # the plan column is shown
    gap: int                                  # blank columns between columns
    columns: tuple[tuple[str, int], ...]      # (key, width) left to right
    detail: bool                              # the selected account's panel shows
    blanks: bool                              # blank lines around the status block
    room: int                                 # the width the screen lays text out in
    summary: bool = False                     # the capacity summary over the headers

    def width(self, key: str) -> int:
        return dict(self.columns)[key]

    def x(self, key: str) -> int:
        """Where column ``key`` starts."""
        x = 0
        for k, w in self.columns:
            if k == key:
                return x
            x += w + self.gap
        raise KeyError(key)

    @property
    def total(self) -> int:
        """The table's width."""
        return sum(w for _k, w in self.columns) + self.gap * (len(self.columns) - 1)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(k for k, _w in self.columns)


def _columns(
    needs: TableNeeds, *, bar: int, clock: bool, plan: bool, name: int
) -> list[tuple[str, int]]:
    def head(key: str, width: int) -> tuple[str, int]:
        return key, max(width, cells(HEADERS[key]))

    usage = bar + 1 + PCT_W if bar else PCT_W
    out = [head("order", 1), head("account", name + needs.slot)]
    if plan:
        out.append(head("plan", needs.plan))
    out += [
        head("5h", usage),
        head("reset5", needs.reset5 if clock else needs.reset5_short),
        head("7d", usage),
        head("reset7", needs.reset7 if clock else needs.reset7_short),
        head("status", needs.status),
    ]
    return out


def _span(columns: Sequence[tuple[str, int]], gap: int) -> int:
    return sum(w for _k, w in columns) + gap * (len(columns) - 1)


def _name_room(room: int, needs: TableNeeds, columns: Sequence[tuple[str, int]], gap: int) -> int:
    """Cells left for the name when every other column takes its width."""
    return room - (_span(columns, gap) - dict(columns)["account"]) - needs.slot


def table_plan(
    width: int,
    height: int,
    needs: TableNeeds,
    *,
    attention: bool = False,
    summary: bool = False,
) -> TablePlan:
    """The table's columns for a ``width`` x ``height`` terminal.

    Everything shows while it fits: bars up to :data:`MAX_BAR`, the reset
    clocks, the plan, the whole name (up to :data:`NAME_CAP`). When it does
    not, in this order: the bars shorten to :data:`MIN_BAR`; the columns
    move closer (:data:`TIGHT_GAP`); the reset clocks go (the countdowns
    stay); the plan column goes; the name shortens with … (to
    :data:`MIN_NAME`); then the bars go, leaving the percentages, and the
    name takes what is left. ``order``, the resets and the status columns
    never go, so a terminal too narrow even for that clips the row's end.

    Height: the selected account's panel (``needs.detail`` lines) shows
    under the table only when every row fits above it. The capacity summary
    (``summary``: there is one to show) takes one line over the headers; on
    a short terminal it goes first, before the panel — it shows only where
    it costs the panel nothing, and never below :data:`SUMMARY_MIN_ROWS`
    rows. The table itself is used at every size."""
    room = text_width(width)
    name = min(needs.name, NAME_CAP)
    columns: list[tuple[str, int]] | None = None
    for clock, plan, gap in ((True, True, GAP), (True, True, TIGHT_GAP),
                             (False, True, TIGHT_GAP), (False, False, TIGHT_GAP)):
        # Everything but the two bars, at the full name.
        fixed = _span(_columns(needs, bar=0, clock=clock, plan=plan, name=name), gap)
        bar = min((room - fixed) // 2 - 1, MAX_BAR)
        if bar >= MIN_BAR:
            columns = _columns(needs, bar=bar, clock=clock, plan=plan, name=name)
            break
    if columns is None:  # the name shortens, then the bars go
        clock, plan, gap = False, False, TIGHT_GAP
        bar = MIN_BAR
        fit = _name_room(room, needs, _columns(needs, bar=bar, clock=False, plan=False,
                                               name=0), gap)
        if fit < min(name, MIN_NAME):
            bar = 0
            fit = _name_room(room, needs, _columns(needs, bar=0, clock=False, plan=False,
                                                   name=0), gap)
        columns = _columns(needs, bar=bar, clock=clock, plan=plan,
                           name=max(min(name, fit), 1))
    blanks = height >= BLANKS_MIN_ROWS
    fixed_lines = 1 + 1 + 1 + int(attention) + 2 * int(blanks)  # status, header, footer
    rest = height - fixed_lines
    detail = needs.detail > 0 and needs.rows + needs.detail <= rest
    shown = summary and height >= SUMMARY_MIN_ROWS and (
        not detail or needs.rows + needs.detail + 1 <= rest
    )
    return TablePlan(
        bar=bar, clock=clock, plan=plan, gap=gap, columns=tuple(columns),
        detail=detail, blanks=blanks, room=room, summary=shown,
    )


# -- the capacity summary ------------------------------------------------------------------
#
#   5h free: 4 accounts · next 5h back 07:10 (#3) · 7d left this week ≈ 2.3 accounts
#   · next 7d reset Oct 5 12:51
#
# Over the accounts automatic switching can use (:func:`usable_for_capacity`).
# "7d left ≈ N accounts" adds up (100 − 7d%)/100 per account, NOT weighted by
# plan: a 20x and a 5x account half spent read as one account left between
# them (``?`` help says so).


@dataclass(frozen=True)
class Capacity:
    """What the summary line says (:func:`capacity`)."""

    usable: int                              # accounts counted
    free5: int                               # under the 5h soft mark, 7d not spent
    back5: tuple[float, str] | None          # (reset, slot): the soonest 5h back
    left7: float                             # Σ (100 − 7d%)/100
    next7: float | None                      # the soonest 7d reset


def usable_for_capacity(row: fx.FleetRow, now: float) -> bool:
    """An account the summary counts: one automatic switching may use
    (not :func:`unusable`), with usage windows (no API key) it can read."""
    return (
        not unusable(row, now)
        and row.login != "api"
        and row.pct5 is not None
        and row.pct7 is not None
    )


def capacity(
    rows: Sequence[fx.FleetRow], mx: MaximizeSettings, now: float
) -> Capacity | None:
    """The fleet's capacity right now, or None with no account to count.

    ``free5``: accounts (the active one included) whose 5h is under its
    soft mark and whose 7d is under its hard mark. ``back5``: among the
    others whose 7d is not spent, the soonest 5h reset. ``left7``: the 7d
    room left, as whole accounts. ``next7``: the soonest 7d reset."""
    usable = [r for r in rows if usable_for_capacity(r, now)]
    if not usable:
        return None
    week_ok = [r for r in usable if (r.pct7 or 0.0) < mx.hard_7d]
    free = [r for r in week_ok if (r.pct5 or 0.0) < mx.soft_5h]
    waiting = [
        (r.reset5, r.number) for r in week_ok
        if (r.pct5 or 0.0) >= mx.soft_5h and r.reset5 is not None and r.reset5 > now
    ]
    resets7 = [r.reset7 for r in usable if r.reset7 is not None and r.reset7 > now]
    left7 = sum(max(0.0, 100.0 - min(r.pct7 or 0.0, 100.0)) / 100.0 for r in usable)
    return Capacity(
        usable=len(usable),
        free5=len(free),
        back5=min(waiting) if waiting else None,
        left7=left7,
        next7=min(resets7) if resets7 else None,
    )


def summary_variants(cap: Capacity, now: float) -> list[list[Seg]]:
    """The summary line as tone segments, longest first: the clocks go
    first (the 7d reset, then the 5h one), then the 7d part."""
    n = cap.free5
    head: list[Seg] = [
        ("5h free: ", "dim"),
        (f"{n} account{'' if n == 1 else 's'}" if n else "none", "plain" if n else "warn"),
    ]
    back: list[Seg] = []
    if cap.back5 is not None:
        reset, slot = cap.back5
        back = [(" · next 5h back ", "dim"),
                (f"{reset_clock(reset, now, date=False)} (#{slot})", "plain")]
    week: list[Seg] = [(" · 7d left this week ≈ ", "dim"), (f"{cap.left7:.1f} accounts", "plain")]
    reset7: list[Seg] = []
    if cap.next7 is not None:
        reset7 = [(" · next 7d reset ", "dim"), (reset_clock(cap.next7, now), "plain")]
    out = [head + back + week + reset7, head + back + week, head + week, head]
    unique: list[list[Seg]] = []
    for variant in out:
        if variant not in unique:
            unique.append(variant)
    return unique


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


def priming_runs(
    enabled: bool, es: fx.EngineStatus, sit: Situation, guard: str | None = None
) -> bool:
    """Whether an engine primes idle accounts now, so the next prime time is
    worth showing: priming on, switching live, not paused after a Claude
    Code update (``guard``), and not a dry run here (it never primes, and
    while it holds the lease no other engine runs)."""
    return enabled and switching_live(sit) and es.holder != "here-dry" and not guard


def next_number(dv: fx.DecisionView, picks: Sequence[str], sit: Situation) -> str | None:
    """The account automatic switching goes to next: the decision's target
    while it is moving (a pending switch, a switch, a preempt waiting for
    idle, a rebalance deferred to a quiet time), else the engine's first
    pick. None while nothing switches or the engine stopped reporting."""
    if not switching_live(sit):
        return None
    moving = dv.kind in ("pending", "switch") or (
        dv.kind == "hold" and dv.code in ("preempt", "rebalance-deferred")
    )
    if moving and dv.target:
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


# -- the policy's own words, for the hold codes ---------------------------------------
#
# The policy (``maximize/policy.py``) writes these reasons; the patterns
# below pick out the parts the sentence quotes. A reason they do not match
# (a newer engine's wording) is quoted whole instead.

#: ``#1 7d 84% would pass 90% in ~3h, before your usual quiet time (23:00)``
#: (``policy._preempt``): the part after the slot number.
_PREEMPT_WHY_RE = re.compile(
    r"#\w+ (7d [\d.]+% would pass [\d.]+% in ~\d+[mh], [^—]*?)\s*(?:—|$)"
)
#: ``preempt cooldown (12 min left): …``
_PREEMPT_COOLDOWN_RE = re.compile(r"preempt cooldown \((\d+) min left\)")
#: ``rebalance deferred to your quiet time (23:00): …`` (``policy._rebalance``)
_DEFERRED_RE = re.compile(r"rebalance deferred to your quiet time \((\d{1,2}:\d{2})\)")
RESET_WAIT_TAIL = "(switches at once if it hits 100%)"


def waits_text(waits: Sequence[tuple[str, float, float]], now: float) -> str:
    """``5h 96% — resets in 8m`` per waited-out window, the policy's words
    with the minutes counted from ``now``."""
    return ", ".join(
        f"{window} {pct:.0f}% — resets in {max(1, round((reset - now) / 60.0))}m"
        for window, pct, reset in waits
    )


def _quoted(reason: str) -> str:
    """A reason with the slot number it starts with dropped (the sentence
    has already named the account)."""
    return re.sub(r"^#\w+ ", "", reason.strip())


def _reset_wait_variants(
    head: Seg, act: fx.FleetRow, dv: fx.DecisionView, name, now: float
) -> list[list[Seg]]:
    if not dv.waits:  # the window reset since, or a reason this cannot read
        return [
            [head, (f" · using {name(act.number)} · {_quoted(dv.reason)}", "plain")],
            [head, (f" · {dv.reason}", "plain")],
            [head, (" · waiting out a reset", "plain")],
            [head],
        ]
    waits = waits_text(dv.waits, now)
    first = waits_text(dv.waits[:1], now)
    return [
        [head, (f" · using {name(act.number)} · {waits}, waiting it out ", "plain"),
         (RESET_WAIT_TAIL, "dim")],
        [head, (f" · #{act.number} {waits}, waiting it out {RESET_WAIT_TAIL}", "plain")],
        [head, (f" · #{act.number} {waits}, waiting it out", "plain")],
        [head, (f" · #{act.number} {first}, waiting", "plain")],
        [head, (" · waiting out a reset", "plain")],
        [head],
    ]


def _preempt_why(reason: str) -> str | None:
    """``7d 84% would pass 90% in ~3h, before your usual quiet time (23:00)``."""
    m = _PREEMPT_WHY_RE.search(reason or "")
    return m.group(1).strip() if m else None


def _preempt_hold_variants(
    head: Seg, act: fx.FleetRow, dv: fx.DecisionView, name, dry: bool
) -> list[list[Seg]]:
    why = _preempt_why(dv.reason)
    cooldown = _PREEMPT_COOLDOWN_RE.search(dv.reason or "")
    target = name(dv.target) if dv.target else "the next account"
    short_target = f"#{dv.target}" if dv.target else "the next account"
    move = "would move" if dry else "will move"
    if cooldown:  # it still waits for a pause once the cooldown is over
        when = f"when you pause after the cooldown ({cooldown.group(1)}m left)"
        short_when = "after the cooldown"
    else:
        when, short_when = "when you pause", "on pause"
    if why is None:
        return [
            [head, (f" · using {name(act.number)} · {_quoted(dv.reason)}", "plain")],
            [head, (f" · to {short_target} {short_when} (preempt)", "plain")],
            [head],
        ]
    pace = why.split(", ", 1)[0]  # 7d 84% would pass 90% in ~3h
    return [
        [head, (f" · using {name(act.number)} · {why} — {move} to {target} {when}", "plain")],
        [head, (f" · #{act.number} {why} — to {short_target} {when}", "plain")],
        [head, (f" · #{act.number} {pace} — to {short_target} {short_when}", "plain")],
        [head, (f" · to {short_target} {short_when} (preempt)", "plain")],
        [head],
    ]


def _deferred_variants(
    head: Seg, act: fx.FleetRow, dv: fx.DecisionView, name
) -> list[list[Seg]]:
    m = _DEFERRED_RE.search(dv.reason or "")
    at = m.group(1) if m else None
    deferred = "rebalance deferred to your quiet time" + (f" ({at})" if at else "")
    better = f"({name(dv.target)} scores better; this is usually a busy time)" if dv.target else (
        "(this is usually a busy time)"
    )
    return [
        [head, (f" · using {name(act.number)} · all fine — {deferred} ", "plain"),
         (better, "dim")],
        [head, (f" · using {name(act.number)} · {deferred}", "plain")],
        [head, (f" · {deferred}", "plain")],
        [head, (f" · rebalance at {at}" if at else " · rebalance deferred", "plain")],
        [head],
    ]


#: ``#1 held until 15:30 (2h left) — …`` (``policy._held``): the end and
#: the time left, for a hold the engine words that Fleet has not read.
_HELD_RE = re.compile(r"held until (.+?) \((\w+) left\)")


def _safety_moving(dv: fx.DecisionView) -> bool:
    """The decision is one an account hold never sets aside: a switch, a
    reset-aware wait, a hard mark with nowhere roomier to go, a learned
    ride (the hard switch, later), every account at its limit, unreadable
    usage."""
    return dv.kind in ("switch", "exhausted", "indeterminate") or (
        dv.kind == "hold" and dv.code in ("reset-wait", "hard-stay", "ride")
    )


#: ``#1 7d 99% — riding to the limit, switching in ~2m (learned) or at your
#: next pause`` (``policy._hard_or_ride``): the windows, the minutes, how.
_RIDE_RE = re.compile(
    r"#\w+ ((?:5h|7d) [\d.]+%(?: / (?:5h|7d) [\d.]+%)*) — riding to the limit, "
    r"switching in ~(\d+)m \((\w+)\)"
)


def _ride_variants(
    head: Seg, act: fx.FleetRow, dv: fx.DecisionView, name, now: float
) -> list[list[Seg]]:
    """A learned ride: ``7d 99% — riding to the limit, switching in ~2m
    (learned) or at your next pause``, the minutes counted from ``now``."""
    m = _RIDE_RE.search(dv.reason or "")
    if m is None:
        return [
            [head, (f" · using {name(act.number)} · {_quoted(dv.reason)}", "plain")],
            [head, (f" · #{act.number} riding to the limit", "plain")],
            [head],
        ]
    label, how = m.group(1), m.group(3)
    if dv.ride_until is not None:
        left = dv.ride_until - now
        when = "switching now" if left <= 0 else f"switching in ~{max(1, round(left / 60.0))}m"
    else:
        when = f"switching in ~{m.group(2)}m"
    short = when.replace("switching in ", "")
    return [
        [head, (f" · using {name(act.number)} · {label} — riding to the limit, {when} "
                f"({how}) or at your next pause", "plain")],
        [head, (f" · #{act.number} {label} — riding to the limit, {when} ({how}) "
                "or at your next pause", "plain")],
        [head, (f" · #{act.number} {label} — riding, {when} or on pause", "plain")],
        [head, (f" · #{act.number} riding, {short}", "plain")],
        [head],
    ]


def _hard_stay_variants(
    head: Seg, act: fx.FleetRow, dv: fx.DecisionView, name
) -> list[list[Seg]]:
    """Past a hard mark, but no account has more room: it stays until 100%."""
    why = _quoted((dv.reason or "").split(";", 1)[0])
    return [
        [head, (f" · using {name(act.number)} · {why} — no account has more room, it stays ",
                "plain"), ("(switches at once at 100%)", "dim")],
        [head, (f" · #{act.number} {why} — no account has more room, it stays", "plain")],
        [head, (f" · #{act.number} past hard — nowhere roomier, it stays", "plain")],
        [head],
    ]


def _hold_variants(
    act: fx.FleetRow,
    dv: fx.DecisionView,
    name,
    mx: MaximizeSettings,
    now: float,
    hold: "account_hold.AccountHold | None",
    dry: bool,
) -> list[list[Seg]]:
    """``Holding #1 until 15:30 (2h left) — only hard 98%/100% will move you``."""
    if hold is not None:
        clock = account_hold.clock_text(hold.until, now)
        left: str | None = account_hold.left_text(hold.until - now)
    else:  # the engine's word only: its reason names the end
        m = _HELD_RE.search(dv.reason or "")
        clock, left = (m.group(1), m.group(2)) if m else (None, None)
    head: list[Seg] = (
        [("Dry run", "warnb"), (" · holding", "plain")] if dry else [("Holding", "okb")]
    )
    until = f" until {clock}" if clock else ""
    span = f"{until} ({left} left)" if left else until
    safety = account_hold.safety_text(mx.hard_5h, mx.hard_7d)
    short = f" #{act.number}"
    return [
        [*head, (f" {name(act.number)}{span} — {safety} ", "plain"), ("(h to change)", "dim")],
        [*head, (f"{short}{span} — {safety}", "plain")],
        [*head, (f"{short}{span} — only hard/100% moves you", "plain")],
        [*head, (f"{short}{span}", "plain")],
        [*head, (f"{short}{until}", "plain")],
        [*head, (short, "plain")],
    ]


def _preempt_switch_variants(
    head: Seg, dv: fx.DecisionView, name, verb: str
) -> list[list[Seg]]:
    why = _preempt_why(dv.reason)
    move = f" · {verb} {name(dv.active)} → {name(dv.target)} now while you're idle"
    out: list[list[Seg]] = []
    if why:
        out.append([head, (f"{move} ", "plain"), (f"— {why}", "dim")])
    out += [
        [head, (f"{move} (preempt)", "plain")],
        [head, (f" · {verb} → #{dv.target} while idle (preempt)", "plain")],
        [head, (f" · {verb} → #{dv.target}", "plain")],
        [head],
    ]
    return out


def status_variants(
    es: fx.EngineStatus,
    dv: fx.DecisionView,
    rows: Sequence[fx.FleetRow],
    mx: MaximizeSettings,
    sit: Situation,
    *,
    now: float,
    published_at: float | None = None,
    hold: "account_hold.AccountHold | None" = None,
    hold_read: bool = False,
) -> list[list[Seg]]:
    """The status sentence as tone segments, longest variant first; the
    widget shows the first that fits (it never wraps).

    ``hold`` is the account hold marker (``view.MaximizeState.hold``) and
    ``hold_read`` says the caller read it: then a hold pinning the active
    account is worded at once (before the engine's next tick says so),
    unless the decision is one a hold never sets aside. Without
    ``hold_read`` only a decision coded ``hold`` is."""
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
    live_act = next((r for r in rows if r.active), None) or act
    if live_act is not None and sit in ("live", "waiting"):
        pinned = account_hold.holding(hold, live_act.number, now) if hold_read else None
        coded = (
            not hold_read and dv.kind == "hold" and dv.code == "hold"
            and dv.active == live_act.number
        )
        if (pinned is not None and not _safety_moving(dv)) or coded:
            return _hold_variants(live_act, dv, name, mx, now, pinned, dry)
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
    if dv.kind == "hold" and act is not None:
        if dv.code == "reset-wait":
            return _reset_wait_variants(head, act, dv, name, now)
        if dv.code == "preempt":
            return _preempt_hold_variants(head, act, dv, name, dry)
        if dv.code == "rebalance-deferred":
            return _deferred_variants(head, act, dv, name)
        if dv.code == "hard-stay":
            return _hard_stay_variants(head, act, dv, name)
        if dv.code == "ride":
            return _ride_variants(head, act, dv, name, now)
    if dv.kind == "switch":
        trigger = f" ({dv.trigger})" if dv.trigger else ""
        verb = "would switch" if dry else "switching"
        if dv.trigger == "preempt":
            return _preempt_switch_variants(head, dv, name, verb)
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
    ("enter", "switch"), ("r", "re-login"), ("l", "last resort"), ("h", "hold"),
    ("m", "menu"), ("?", "help"), ("q", "quit"),
)


def key_hints(width: int) -> list[tuple[str, str]]:
    """The footer's ``(key, what)`` pairs: every word when it fits
    (``enter switch · r re-login · …``), else the keys with only menu, help
    and quit spelled out, else the keys alone."""
    def text(pairs) -> str:
        return " · ".join(f"{k} {w}" if w else k for k, w in pairs)

    if len(text(KEY_HINTS)) <= width:
        return list(KEY_HINTS)
    short = [(k, w if k in ("m", "?", "q") else "") for k, w in KEY_HINTS]
    if len(text(short)) <= width:
        return short
    return [(k, "") for k, _w in KEY_HINTS]
