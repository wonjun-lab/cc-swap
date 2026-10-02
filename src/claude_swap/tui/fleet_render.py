"""Fleet's renderers: the pure model (``maximize/home.py``) as Rich text.

No Textual here, so every line can be checked without a terminal. An
account block keeps the upstream dashboard's look
(``1  main (main@acme.dev)  [personal] [20x]`` over its 5h/7d bars); the
narrow layout puts one account on a line
(``● 1 main   5h ━━━┃━ 62%  7d ━━──┃ 41%      ● active``). Bars carry the
soft (amber) and hard (red) ticks and are colored by them
(``widgets.bar_color``), the same in every layout.

A selected account gets the panel background only: its threshold colors
stay as they are.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from rich.text import Text

from claude_swap.json_output import USAGE_API_KEY
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import home
from claude_swap.models import AccountSnapshot
from claude_swap.tui import data
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import bar_cells, bar_color, usage_rows

_LOGIN_NOTES = {
    "relogin": "needs re-login",
    "expired": "token expired",
    "foreign": "foreign login",
    "keychain": "keychain locked",
    "api": "API key",
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

    def tag(self, row: fx.FleetRow) -> tuple[str, str] | None:
        return home.tag_for(
            row, is_next=row.number == self.next_no, now=self.now, priming=self.priming
        )


def pad_to(line: Text, width: int) -> Text:
    """``line`` cut (with …) or padded to exactly ``width`` cells."""
    out = line.copy()
    if out.cell_len > width:
        out.truncate(max(width, 0), overflow="ellipsis")
    else:
        out.append(" " * (width - out.cell_len))
    return out


def with_tag(left: Text, tag: tuple[str, str] | None, width: int, palette: Palette) -> Text:
    """``left`` with ``tag`` right-aligned in ``width`` cells; the tag goes
    when fewer than 12 cells would be left for ``left``."""
    if tag is None:
        return pad_to(left, width)
    text, tone = tag
    room = width - len(text) - 2
    if room < 12:
        return pad_to(left, width)
    out = pad_to(left, room) if left.cell_len > room else left.copy()
    out.append(" " * (width - out.cell_len - len(text)))
    out.append(text, style=tone_style(tone, palette))
    return out


# -- one account ---------------------------------------------------------------------------


def header_line(
    row: fx.FleetRow, acc: AccountSnapshot | None, width: int, ctx: Ctx
) -> Text:
    """``1  main (main@acme.dev)  [personal] [20x]`` plus the tag; the email,
    then the org and plan, drop when the line is too narrow."""
    p = ctx.palette
    tag = ctx.tag(row)
    alias = acc.alias if acc is not None else ""
    age = data.format_age(acc.usage.age_s) if acc is not None and row.stale else None

    def build(email: bool, org: bool) -> Text:
        t = Text()
        t.append(f"{row.number:>2}  ", style=f"bold {p.foreground}")
        if alias:
            t.append(alias, style=f"bold {p.accent}")
            if email:
                t.append(f" ({row.email})", style=p.foreground)
        else:
            t.append(row.email, style=p.foreground)
        if org:
            t.append(f"  [{row.org}]", style=p.muted)
            if row.plan not in ("?", "api"):
                t.append(f" [{row.plan}]", style=p.muted)
            if age:
                t.append(f"  {age}", style=p.muted)
        return t

    need = len(tag[0]) + 3 if tag else 0
    for email, org in ((True, True), (False, True), (False, False)):
        left = build(email, org)
        if left.cell_len + need <= width:
            break
    return with_tag(left, tag, width, p)


def body_lines(
    row: fx.FleetRow, acc: AccountSnapshot | None, width: int, ctx: Ctx, *, max_bar: int
) -> list[Text]:
    """The block under the header: one bar per usage window (5h, 7d, then
    any spend or per-model window), or what stands in for them."""
    p = ctx.palette
    if acc is None:
        return []
    sentinel = acc.usage.sentinel
    if row.login == "relogin":
        out = [Text("    ").append("⚠ needs re-login — select it and press r", style=p.sev_crit)]
        seen = data.last_seen_note(acc.usage)
        if seen:
            out.append(Text(f"    └ {seen}", style=p.muted))
        return [pad_to(line, width) for line in out]
    if sentinel is not None:
        api = sentinel == USAGE_API_KEY
        mark = "·" if api else "⚠"
        line = Text("    ").append(
            f"{mark} {data.sentinel_label(sentinel)}", style=p.muted if api else p.sev_warn
        )
        return [pad_to(line, width)]
    rows = usage_rows(acc.usage.last_good, ctx.now, acc.usage.fetched_at)
    if not rows:
        return [pad_to(Text("    usage unavailable", style=p.muted), width)]
    dim = row.stale or row.tier == "excluded"
    label_w = max(len(r[0]) for r in rows)
    bar_w = max(10, min(max_bar, width - 4 - label_w - 1 - 5 - 2 - 28))
    out: list[Text] = []
    for label, pct, suffix, suffix_full in rows:
        if label == "5h" and row.state5 == "cold":
            suffix = suffix_full = "not started"
        elif label == "5h" and row.state5 == "primed":
            suffix, suffix_full = f"{suffix} · primed", f"{suffix_full} · primed"
        if 4 + label_w + 1 + bar_w + 5 + 2 + len(suffix_full) <= width:
            suffix = suffix_full
        soft, hard = ctx.ticks.get(label, (None, None))
        line = Text("    ")
        line.append(f"{label:<{label_w}} ", style=p.muted)
        line.append(bar_cells(pct, bar_w, stale=dim, threshold=soft, hard=hard, palette=p))
        color = bar_color(pct, threshold=soft, hard=hard, palette=p)
        line.append(f" {pct:3.0f}%", style=f"{color} dim" if dim else color)
        if suffix:
            line.append(f"  {suffix}", style=p.muted)
        out.append(pad_to(line, width))
    return out


def block_lines(
    row: fx.FleetRow, acc: AccountSnapshot | None, width: int, ctx: Ctx, *, max_bar: int
) -> list[Text]:
    """A whole account block: the header and its bars, ``width`` wide."""
    return [
        pad_to(header_line(row, acc, width, ctx), width),
        *body_lines(row, acc, width, ctx, max_bar=max_bar),
    ]


@dataclass(frozen=True)
class MiniWidths:
    number: int
    name: int
    bar: int


def mini_widths(rows: Sequence[fx.FleetRow], width: int, ctx: Ctx) -> MiniWidths:
    """One set of widths for every one-line row, so the bars line up."""
    number = max((len(r.number) for r in rows), default=1)
    name = max(4, min(10, max((len(r.name) for r in rows), default=4)))
    tags = [ctx.tag(r) for r in rows]
    tag_w = max([12, *(len(t[0]) for t in tags if t)])
    fixed = 2 + number + 1 + name + 1 + 2 * (3 + 5) + 2 + tag_w + 2
    return MiniWidths(number, name, max(5, min(14, (width - fixed) // 2)))


def mini_line(row: fx.FleetRow, width: int, ctx: Ctx, widths: MiniWidths) -> Text:
    """``● 1 main   5h ━━━┃━ 62%  7d ━━──┃ 41%      ● active``."""
    p = ctx.palette
    t = Text()
    t.append("● " if row.active else "  ", style=f"bold {p.accent}")
    t.append(f"{row.number:>{widths.number}} ", style=f"bold {p.foreground}")
    t.append(
        fx.clip(row.name, widths.name).ljust(widths.name + 1),
        style=f"bold {p.accent}" if row.active else p.foreground,
    )
    if row.login != "ok":
        tone = p.sev_crit if row.login == "relogin" else (
            p.muted if row.login == "api" else p.sev_warn
        )
        t.append(_LOGIN_NOTES.get(row.login, row.login), style=tone)
    else:
        dim = row.stale or row.tier == "excluded"
        for i, (label, pct) in enumerate((("5h", row.pct5), ("7d", row.pct7))):
            soft, hard = ctx.ticks.get(label, (None, None))
            if i:
                t.append("  ")
            t.append(f"{label} ", style=p.muted)
            t.append(bar_cells(pct, widths.bar, stale=dim, threshold=soft, hard=hard, palette=p))
            color = bar_color(pct, threshold=soft, hard=hard, palette=p)
            t.append(f" {pct:3.0f}%" if pct is not None else "    ?",
                     style=f"{color} dim" if dim else color)
    return with_tag(t, ctx.tag(row), width, p)


# -- the account area ------------------------------------------------------------------------


@dataclass
class Body:
    """The rendered account area plus where each account landed in it."""

    text: Text = field(default_factory=Text)
    #: number -> (first line, line count) of its block or row.
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


def render_blocks(
    rows: Sequence[fx.FleetRow],
    accounts: Mapping[str, AccountSnapshot],
    width: int,
    ctx: Ctx,
    layout: home.HomeLayout,
    *,
    selected: str | None,
    selected_bg: str,
) -> Body:
    """Wide and medium: account blocks, ``layout.columns`` side by side
    (row by row), a blank line between block rows."""
    cols = layout.columns
    cw = home.column_width(width, layout)
    blocks: list[list[Text]] = []
    for row in rows:
        lines = block_lines(row, accounts.get(row.number), cw, ctx, max_bar=layout.max_bar)
        if row.number == selected:
            for line in lines:
                line.stylize(selected_bg)
        blocks.append(lines)
    body = Body()
    for start in range(0, len(rows), cols):
        group = blocks[start:start + cols]
        numbers = [r.number for r in rows[start:start + cols]]
        if start:
            body.append(Text(""))
        first = len(body.hits)
        tall = max(len(b) for b in group)
        for i in range(tall):
            line = Text()
            owners: list[tuple[int, int, str]] = []
            for j, block in enumerate(group):
                if j:
                    line.append(" " * layout.gap)
                x0 = line.cell_len
                line.append(block[i] if i < len(block) else Text(" " * cw))
                owners.append((x0, x0 + cw, numbers[j]))
            body.append(line, owners)
        for j, number in enumerate(numbers):
            body.spans[number] = (first, len(group[j]))
    return body


def render_list(
    rows: Sequence[fx.FleetRow],
    width: int,
    ctx: Ctx,
    *,
    selected: str | None,
    selected_bg: str,
) -> Body:
    """Narrow: one line per account."""
    widths = mini_widths(rows, width, ctx)
    body = Body()
    for row in rows:
        line = pad_to(mini_line(row, width, ctx, widths), width)
        if row.number == selected:
            line.stylize(selected_bg)
        body.spans[row.number] = (len(body.hits), 1)
        body.append(line, [(0, width, row.number)])
    return body


def render_expanded(
    row: fx.FleetRow | None, acc: AccountSnapshot | None, width: int, ctx: Ctx, *, max_bar: int
) -> Text:
    """Narrow: a rule, then the selected account as a full block."""
    text = Text("─" * width, style=ctx.palette.track)
    if row is None:
        return text
    for line in block_lines(row, acc, width, ctx, max_bar=max_bar):
        text.append("\n")
        text.append(line)
    return text


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


def attention_text(parts: Sequence[str], tone: str, width: int, palette: Palette) -> Text:
    return Text(
        home.attention_line(parts, width), style=f"bold {tone_style(tone, palette)}",
        no_wrap=True, overflow="ellipsis",
    )


def keys_text(width: int, palette: Palette) -> Text:
    """``enter switch · r re-login · l last resort · m menu · ? help · q quit``."""
    keys = Text(no_wrap=True, overflow="ellipsis")
    for i, (key, what) in enumerate(home.key_hints(width)):
        if i:
            keys.append(" · ", style=palette.muted)
        keys.append(key, style=f"bold {palette.accent}")
        if what:
            keys.append(f" {what}", style=palette.muted)
    return keys
