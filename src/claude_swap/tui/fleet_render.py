"""Fleet's renderers: the pure model (``maximize/home.py``) as Rich text.

No Textual here, so every line can be checked without a terminal. The
accounts are a table with a dim header (``home.COLUMNS``)::

    order  account          plan  5h            5h resets      …  status
      ●    main@acme.dev #1  20x   ━━━┃━━━┃ 62%  1h47m      …  ● active
      1    side@acme.dev #2  5x    ───┃───┃  0%  not started    …  next

laid out by ``home.table_plan``; under it, when there are rows to spare,
the selected account in full (:func:`render_detail`). Bars carry the soft
(amber) and hard (red) ticks and are coloured by them
(``widgets.bar_color``) at every width.

The selected row gets the panel background only: its threshold colours
stay as they are.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from rich.text import Text

from claude_swap import credits as credits_mod
from claude_swap import oauth
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import home
from claude_swap.models import AccountSnapshot
from claude_swap.tui import data
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import bar_cells, bar_color, usage_rows

#: What stands in for the bars when the stored login cannot be read: the 5h
#: column's words, longest first, then the 7d column's (the shortest fit
#: the percentage-only columns of a narrow terminal).
_LOGIN_CELLS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "relogin": (("⚠ needs re-login", "⚠ re-login", "⚠"), ("select it, press r", "press r", "")),
    "expired": (("⚠ token expired", "⚠ expired", "⚠"), ("heals itself", "")),
    "foreign": (("⚠ foreign login", "⚠ foreign", "⚠"), ("a switch repairs it", "")),
    "keychain": (("⚠ keychain locked", "⚠ keychain", "⚠"), ("",)),
    "api": (("· API key", "· API", "API"), ("no usage windows", "")),
}


def tone_style(tone: str, palette: Palette) -> str:
    """A tone (``maximize/fleet.py``, ``maximize/home.py``) as a Rich style."""
    return {
        "ok": palette.sev_ok,
        "warn": palette.sev_warn,
        "crit": palette.sev_crit,
        "dim": palette.muted,
        "accent": f"bold {palette.accent}",
        "active": f"bold {palette.accent}",
        "okb": f"bold {palette.sev_ok}",
        "warnb": f"bold {palette.sev_warn}",
        "bold": f"bold {palette.foreground}",
        "plain": palette.foreground,
    }.get(tone, palette.foreground)


@dataclass(frozen=True)
class Ctx:
    """What every account line needs besides the account."""

    palette: Palette
    ticks: Mapping[str, tuple[float, float]]
    now: float
    next_no: str | None = None
    priming: bool = True
    #: The engine's landing order (``policy.landing_candidates``) and the
    #: accounts only a forced move takes (``policy.escape_candidates``), for
    #: the ``order`` column (``home.order_marks``) …
    picks: tuple[str, ...] = ()
    forced: frozenset[str] = frozenset()
    #: … dimmed while nothing switches (no engine, auto off, paused, silent).
    live: bool = True
    #: Slot → prepaid balance / credit grant reading (``credits.py``), for
    #: the detail panel's ``credits`` line.
    credits: Mapping[str, dict] = field(default_factory=dict)

    def tag(self, row: fx.FleetRow) -> tuple[str, str] | None:
        return home.tag_for(
            row, is_next=row.number == self.next_no, now=self.now, priming=self.priming
        )

    def status(self, row: fx.FleetRow) -> tuple[str, str] | None:
        """The status column (the tag, else ``primed``)."""
        return home.status_for(
            row, is_next=row.number == self.next_no, now=self.now, priming=self.priming
        )


def pad_to(line: Text, width: int, *, overflow: str = "ellipsis") -> Text:
    """``line`` cut (with …, or cropped) or padded to exactly ``width`` cells."""
    out = line.copy()
    if out.cell_len > width:
        out.truncate(max(width, 0), overflow=overflow)
    else:
        out.append(" " * (width - out.cell_len))
    return out


def _fit(variants: Sequence[str], width: int) -> str:
    """The first (longest) of ``variants`` that fits ``width``."""
    fallback = variants[-1] if variants else ""
    return next((v for v in variants if home.cells(v) <= width), fallback)


# -- the table ------------------------------------------------------------------------------


def table_header(plan: home.TablePlan, palette: Palette) -> Text:
    """The dim column headers, each over its column."""
    line = Text(style=palette.muted, no_wrap=True, overflow="crop")
    for i, (key, width) in enumerate(plan.columns):
        if i:
            line.append(" " * plan.gap)
        line.append(pad_to(Text(home.HEADERS[key]), width))
    return pad_to(line, plan.room, overflow="crop")  # clipped as the rows are


def _order_cell(mark: str, width: int, ctx: Ctx) -> Text:
    """The ``order`` mark; every one but ``●`` dim while nothing switches
    (the order is what switching would do, not what it does)."""
    p = ctx.palette
    style = {
        home.ORDER_ACTIVE: f"bold {p.accent}", home.ORDER_NONE: p.muted,
        home.ORDER_FORCED: p.muted,
    }.get(mark, f"bold {p.foreground}" if ctx.live else p.muted)
    return Text(mark.center(width), style=style)


def _account_cell(row: fx.FleetRow, width: int, ctx: Ctx) -> Text:
    """The whole name: never cut (the column is as wide as the longest)."""
    p = ctx.palette
    return Text(row.name, style=f"bold {p.accent}" if row.active else p.foreground)


def _usage_cell(row: fx.FleetRow, window: str, plan: home.TablePlan, ctx: Ctx) -> Text:
    """A bar with its soft/hard ticks and the percentage (just the
    percentage when the plan has no room for bars)."""
    p = ctx.palette
    pct = row.pct5 if window == "5h" else row.pct7
    soft, hard = ctx.ticks.get(window, (None, None))
    dim = row.stale or row.tier == "excluded"
    t = Text()
    if plan.bar:
        t.append(bar_cells(pct, plan.bar, stale=dim, threshold=soft, hard=hard, palette=p))
        t.append(" ")
    color = bar_color(pct, threshold=soft, hard=hard, palette=p)
    t.append(f"{pct:3.0f}%" if pct is not None else "   ?",
             style=f"{color} dim" if dim else color)
    return t


def _login_cell(row: fx.FleetRow, window: str, width: int, ctx: Ctx) -> Text:
    """What stands in for a bar when the login cannot be read."""
    p = ctx.palette
    first, second = _LOGIN_CELLS.get(row.login, ((row.login,), ("",)))
    words = _fit(first if window == "5h" else second, width)
    if window == "7d" or row.login == "api":
        tone = p.muted
    else:
        tone = p.sev_crit if row.login == "relogin" else p.sev_warn
    return Text(words, style=tone)


def _resets_cell(row: fx.FleetRow, window: str, plan: home.TablePlan, ctx: Ctx) -> Text:
    """``1h47m`` with `` · 07:10`` dim after it; ``not started``, ``—``
    and ``now`` dim."""
    p = ctx.palette
    text = home.row_resets(row, window, ctx.now, clock=plan.clock)
    left, sep, clock = text.partition(" · ")
    if not sep:
        quiet = text in (home.NOT_STARTED, home.NOT_KNOWN, "now")
        return Text(text, style=p.muted if quiet else p.foreground)
    return Text(left, style=p.foreground).append(f" · {clock}", style=p.muted)


def table_row(row: fx.FleetRow, mark: str, plan: home.TablePlan, ctx: Ctx) -> Text:
    """One account's row, ``plan.room`` wide."""
    p = ctx.palette
    line = Text()
    for i, (key, width) in enumerate(plan.columns):
        if i:
            line.append(" " * plan.gap)
        if key == "order":
            cell = _order_cell(mark, width, ctx)
        elif key == "account":
            cell = _account_cell(row, width, ctx)
        elif key == "plan":
            cell = Text(home.plan_text(row), style=p.foreground)
        elif key in ("5h", "7d"):
            cell = (_usage_cell(row, key, plan, ctx) if row.login == "ok"
                    else _login_cell(row, key, width, ctx))
        elif key in ("reset5", "reset7"):
            cell = _resets_cell(row, "5h" if key == "reset5" else "7d", plan, ctx)
        else:
            status = ctx.status(row)
            cell = (Text(plan.status_text(status[0]), style=tone_style(status[1], p))
                    if status else Text())
        line.append(pad_to(cell, width))
    return pad_to(line, plan.room, overflow="crop")  # never a … inside a name


@dataclass
class Body:
    """The rendered account area plus where each account landed in it."""

    text: Text = field(default_factory=Text)
    #: number -> (first line, line count) of its row.
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: per line, ``(x0, x1, number)`` for each account drawn on it (clicks).
    hits: list[list[tuple[int, int, str]]] = field(default_factory=list)

    def append(self, line: Text, owners: list[tuple[int, int, str]] | None = None) -> None:
        if self.hits:
            self.text.append("\n")
        self.text.append(line)
        self.hits.append(owners or [])

    def number_at(self, x: int, y: int) -> str | None:
        if not 0 <= y < len(self.hits):
            return None
        return next((n for x0, x1, n in self.hits[y] if x0 <= x < x1), None)


def render_table(
    rows: Sequence[fx.FleetRow],
    plan: home.TablePlan,
    ctx: Ctx,
    *,
    selected: str | None,
    selected_bg: str,
) -> Body:
    """One line per account, in ``rows`` order (the header is
    :func:`table_header`, drawn above the scrolling area)."""
    marks = home.order_marks(rows, ctx.picks, ctx.forced)
    body = Body()
    for row in rows:
        line = table_row(row, marks.get(row.number, home.ORDER_NONE), plan, ctx)
        if row.number == selected:
            line.stylize(selected_bg)
        body.spans[row.number] = (len(body.hits), 1)
        body.append(line, [(0, plan.room, row.number)])
    return body


# -- the selected account in full -------------------------------------------------------------


def _reset_ts(window: Mapping | None) -> float | None:
    raw = window.get("resets_at") if isinstance(window, Mapping) else None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_MARKERS = ("(!)", "(ahead of pace)")


def detail_windows(
    row: fx.FleetRow, acc: AccountSnapshot | None, now: float
) -> list[tuple[str, float, str]]:
    """``(label, pct, words)`` per usage window the account has (spend, 5h,
    7d, then per-model windows such as ``Fable``), each with its exact
    reset: ``resets 07:10 (in 1h47m)``."""
    if acc is None or acc.usage.sentinel is not None:
        return []
    last_good = acc.usage.last_good
    scoped = {
        w.get("name"): w for w in (last_good or {}).get("scoped") or [] if isinstance(w, dict)
    }
    out: list[tuple[str, float, str]] = []
    for label, pct, _suffix, full in usage_rows(last_good, now, acc.usage.fetched_at):
        marks = [m for m in _MARKERS if m in full]
        if label == "$$":
            out.append((label, pct, full))
            continue
        if label == "5h":
            cold = row.state5 == "cold"
            words = home.NOT_STARTED if cold else home.exact_reset(row.reset5, now)
            if row.state5 == "primed":
                words += " · primed"
        elif label == "7d":
            words = home.exact_reset(row.reset7, now)
        else:
            words = home.exact_reset(_reset_ts(scoped.get(label)), now)
        out.append((label, pct, "  ".join([words, *marks])))
    return out


def _login_words(row: fx.FleetRow, now: float) -> tuple[str, str]:
    """``login ends Oct 24 09:12 (in 21d 0h)`` and its tone."""
    if row.login == "relogin":
        cause = "login expired" if row.login_expired else "refresh token dead"
        return f"re-login needed ({cause})", "crit"
    if row.login == "api":
        return "API key", "dim"
    left = fx.login_left(row, now)
    if left is None:
        return "login: no deadline recorded", "dim"
    if left <= 0:
        return "login expired", "crit"
    tone = "crit" if left < fx.LOGIN_URGENT_S else "warn" if left < fx.LOGIN_WARN_S else "dim"
    when = oauth.local_clock(row.login_deadline)
    return f"login ends {when} (in {oauth.login_countdown(left)})", tone


def _prime_words(row: fx.FleetRow, ctx: Ctx) -> str:
    """``5h opened by priming`` / ``next prime ≤08:30`` / why not."""
    cell = row.prime
    if row.state5 == "primed":
        return "5h opened by priming"
    if cell.kind == "active" or row.login != "ok":
        return ""
    if cell.kind in ("due", "window"):
        return f"next prime {fx.prime_text(cell)}" if ctx.priming else "priming not running"
    if cell.kind == "off":
        return "priming off"
    return f"not primed: {cell.note}" if cell.note not in ("", "—") else ""


def render_detail(
    row: fx.FleetRow | None, acc: AccountSnapshot | None, width: int, ctx: Ctx
) -> Text:
    """A rule, then the selected account in full: its name, organization,
    plan and tag; a long bar per usage window with the exact reset; the
    login deadline and priming."""
    p = ctx.palette
    text = Text("─" * width, style=p.track, no_wrap=True)
    if row is None:
        return text
    lines: list[Text] = []
    head = Text()
    alias = acc.alias if acc is not None else ""
    name_style = f"bold {p.accent}" if row.active else f"bold {p.foreground}"
    head.append(alias or row.email, style=name_style)
    if alias:
        head.append(f" ({row.email})", style=p.foreground)
    facts = [row.org] + ([home.plan_text(row)] if row.plan != "?" else [])
    head.append("  " + " · ".join(facts), style=p.muted)
    status = ctx.status(row)
    if status:
        head.append("  ")
        head.append(status[0], style=tone_style(status[1], p))
    lines.append(head)

    windows = detail_windows(row, acc, ctx.now)
    if row.login != "ok":
        words = "⚠ needs re-login — select it and press r" if row.login == "relogin" else (
            f"{_LOGIN_CELLS.get(row.login, ((row.login,), ()))[0][0]}"
        )
        tone = p.sev_crit if row.login == "relogin" else (
            p.muted if row.login == "api" else p.sev_warn
        )
        lines.append(Text("    ").append(words, style=tone))
        seen = data.last_seen_note(acc.usage) if acc is not None else None
        if seen:
            lines.append(Text(f"    └ {seen}", style=p.muted))
    elif not windows:
        lines.append(Text("    usage unavailable", style=p.muted))
    else:
        dim = row.stale or row.tier == "excluded"
        label_w = max(home.cells(w[0]) for w in windows)
        words_w = max(home.cells(w[2]) for w in windows)
        bar_w = max(10, min(60, width - 4 - label_w - 1 - 5 - 2 - words_w))
        for label, pct, words in windows:
            soft, hard = ctx.ticks.get(label, (None, None))
            line = Text("    ")
            line.append(label + " " * (label_w - home.cells(label) + 1), style=p.muted)
            line.append(bar_cells(pct, bar_w, stale=dim, threshold=soft, hard=hard, palette=p))
            color = bar_color(pct, threshold=soft, hard=hard, palette=p)
            line.append(f" {pct:3.0f}%", style=f"{color} dim" if dim else color)
            line.append(f"  {words}", style=p.foreground)
            lines.append(line)

    last_good = acc.usage.last_good if acc is not None else None
    for label, words in (
        ("cloud credit", credits_mod.cloud_credit_summary(last_good, ctx.now)),
        ("reset coupons", credits_mod.reset_coupons_summary(last_good, ctx.now)),
    ):
        if words:
            lines.append(
                Text("    ").append(f"{label} ", style=p.muted).append(words, style=p.foreground)
            )
    credit_line = credits_mod.summary(ctx.credits.get(row.number), ctx.now)
    if credit_line:
        lines.append(
            Text("    ").append("credits ", style=p.muted).append(credit_line, style=p.foreground)
        )

    info = Text("    ")
    login, tone = _login_words(row, ctx.now)
    info.append(login, style=tone_style(tone, p))
    extra = [w for w in (_prime_words(row, ctx),) if w]
    if row.stale and acc is not None and acc.usage.age_s is not None:
        extra.append(f"reading {data.format_duration(acc.usage.age_s)} old")
    for words in extra:
        info.append(f" · {words}", style=p.muted)
    lines.append(info)

    for line in lines:
        text.append("\n")
        text.append(pad_to(line, width))
    return text


def detail_height(row: fx.FleetRow | None, acc: AccountSnapshot | None, ctx: Ctx) -> int:
    """Lines :func:`render_detail` takes (its rule included)."""
    if row is None:
        return 0
    return len(render_detail(row, acc, 80, ctx).plain.splitlines())


# -- status, attention, keys ---------------------------------------------------------------------


def status_text(
    sentence: Sequence[home.Seg], note: str, width: int, palette: Palette
) -> Text:
    """The status sentence with the holder note right-aligned."""
    line = Text(no_wrap=True, overflow="ellipsis")
    for text, tone in sentence:
        line.append(text, style=tone_style(tone, palette))
    if note:
        line.append(" " * max(width - line.cell_len - len(note), 3))
        line.append(note, style=palette.muted)
    return line


def summary_text(cap: home.Capacity, width: int, now: float, palette: Palette) -> Text:
    """The capacity summary over the column headers: the longest variant
    of ``home.summary_variants`` that fits ``width``."""
    line = Text(no_wrap=True, overflow="ellipsis")
    for text, tone in home.fit_variant(home.summary_variants(cap, now), width):
        line.append(text, style=tone_style(tone, palette))
    return line


def attention_text(
    notices: Sequence[home.Notice], width: int, lines: int, palette: Palette
) -> Text:
    """The attention notes in at most ``lines`` lines (``home.attention_lines``),
    each in its own colour."""
    text = Text(no_wrap=True, overflow="ellipsis")
    for i, (line, tone) in enumerate(home.attention_lines(notices, width, lines)):
        if i:
            text.append("\n")
        text.append(line, style=f"bold {tone_style(tone, palette)}")
    return text


def keys_text(width: int, palette: Palette, *, empty: bool = False) -> Text:
    """``enter switch · r login · l last · u first · h hold · m menu · ? help
    · q quit`` (``a add · m menu · ? help · q quit`` with no account yet)."""
    keys = Text(no_wrap=True, overflow="ellipsis")
    for i, (key, what) in enumerate(home.key_hints(width, empty=empty)):
        if i:
            keys.append(" · ", style=palette.muted)
        keys.append(key, style=f"bold {palette.accent}")
        if what:
            keys.append(f" {what}", style=palette.muted)
    return keys

