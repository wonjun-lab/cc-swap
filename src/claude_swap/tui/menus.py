"""The Fleet screen's menus, key hints and help text (pure, no Textual).

codex-swap's convention: the menu *is* the shortcut list. Every item's key
is its first letter (drawn bold), a state is part of the item's name
(``Mode: service · viewing``), and the key hints list only what the menu
does not. The menu keys and the row keys never collide; a test pins that.

Keys are unique per menu level: the main menu, the row keys and the reserved
keys never share a letter, and a sub-screen's items (Account settings) never
share one with each other or with its ``b``/``q``. A sub-screen may reuse a
main-menu letter (``a`` Add current login) as codex-swap does, but the items
added in 0.3.0 do not: ``v`` is only *View switch history* and ``u`` only
*Update Claude Code* (main menu), ``i`` only *Inspect all logins* (Account
settings). Every title starts with its key, and every short name (the folded
menu) contains it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MenuEntry:
    key: str
    title: str
    action: str
    short: str  # the folded menu's name


MAIN_MENU: tuple[MenuEntry, ...] = (
    MenuEntry("s", "Swap strategy", "strategy", "Strategy"),
    MenuEntry("m", "Mode", "mode", "Mode"),
    MenuEntry("p", "Prime now…", "prime", "Prime"),
    MenuEntry("f", "Fetch latest usage", "fetch", "Fetch"),
    MenuEntry("a", "Account settings", "accounts", "Accounts"),
    MenuEntry("e", "Engine log", "engine", "Engine"),
    MenuEntry("v", "View switch history", "history", "View swaps"),
    MenuEntry("u", "Update Claude Code", "update", "Update"),
    MenuEntry("c", "Classic dashboard", "classic", "Classic"),
    MenuEntry("q", "Quit", "quit", "Quit"),
)
MAIN_KEYS: tuple[str, ...] = tuple(e.key for e in MAIN_MENU)
MENU_SHORT: dict[str, str] = {e.action: e.short for e in MAIN_MENU}
BY_ACTION: dict[str, MenuEntry] = {e.action: e for e in MAIN_MENU}

#: Keys that act on the highlighted account row.
ROW_KEYS: tuple[str, ...] = ("enter", "l", "x", "r")
#: Keys that are deliberately not menu items (navigation, help, theme).
RESERVED_KEYS: tuple[str, ...] = ("w", "?", "h", "j", "k", "b", "g")

ACCOUNT_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("a", "Add current login", "add"),
    ("t", "Token or API key…", "token"),
    ("r", "Re-login…", "relogin"),
    ("n", "Name (alias)…", "alias"),
    ("d", "Delete account…", "delete"),
    ("i", "Inspect all logins (doctor)", "verify"),
)
ACCOUNT_KEYS = (
    "enter select · a add · t token · r re-login · n name · d delete · i inspect · "
    "b back · q quit"
)
KEY_HINTS = (
    "enter switch · l last resort · x exclude · r re-login · w watch · ? help · q quit · ↑↓ move"
)
MINIMAL_KEYS = "? help · q quit"
_KEY_VARIANTS = (
    KEY_HINTS,
    "enter switch · l last resort · x exclude · r re-login · ? help · q quit",
    "enter switch · l/x/r row keys · ? help · q quit",
    MINIMAL_KEYS,
)
SEP = "  "


def key_hints(width: int, *, minimal: bool = False) -> str:
    """The longest key-hint line that fits ``width``."""
    if minimal:
        return MINIMAL_KEYS
    return next((v for v in _KEY_VARIANTS if len(v) <= width), MINIMAL_KEYS)


def menu_title(
    action: str,
    *,
    mode_label: str | None = None,
    relogin: int = 0,
    fetching: bool = False,
    auto_off: bool = False,
) -> str:
    """An item's title with its state in the name."""
    entry = BY_ACTION[action]
    if action == "mode":
        tail = " · AUTO OFF" if auto_off else ""
        return f"Mode: {mode_label or 'off'}{tail}"
    if action == "accounts" and relogin:
        return f"{entry.title} · {relogin} need{'s' if relogin == 1 else ''} re-login"
    if action == "fetch" and fetching:
        return f"{entry.title} — fetching…"
    return entry.title


def bold_spans(title: str, key: str) -> tuple[int, int] | None:
    """``(start, end)`` of the letter drawn bold: the first ``key`` in the
    title, case-insensitive; None when the title has none."""
    index = title.lower().find(key.lower())
    return None if index < 0 else (index, index + 1)


def mode_label(holder: str, pid: int | None) -> str:
    """The Mode item's state, from ``fleet.EngineStatus.holder``."""
    if holder == "service":
        return "service · viewing"
    if holder == "other":
        return f"pid {pid} · viewing" if pid else "another engine · viewing"
    if holder == "here-live":
        return "here · live"
    if holder == "here-dry":
        return "here · dry-run"
    return "off"


def mode_short(holder: str, pid: int | None) -> str:
    """The folded menu's mode state."""
    return {
        "service": "service",
        "other": f"pid {pid}" if pid else "other",
        "here-live": "live",
        "here-dry": "dry-run",
    }.get(holder, "off")


def folded_menu(
    width: int, *, mode_label: str | None = None
) -> list[list[tuple[str, str]]]:
    """The menu folded into as few lines as fit ``width``: ``(title, key)``
    items with short names (``Strategy  Mode: service  Prime …``)."""
    state = (mode_label or "off").split(" · ")
    short_mode = state[1] if state[0] == "here" and len(state) > 1 else state[0]
    items = [
        (f"{e.short}: {short_mode}" if e.action == "mode" else e.short, e.key)
        for e in MAIN_MENU
    ]
    limit = max(width, 30) - 2
    lines: list[list[tuple[str, str]]] = [[]]
    for title, key in items:
        line = lines[-1]
        candidate = SEP.join(t for t, _ in [*line, (title, key)])
        if line and len(candidate) > limit:
            lines.append([(title, key)])
        else:
            line.append((title, key))
    return lines


@dataclass(frozen=True)
class StrategyField:
    key: str     # dotted settings key
    label: str
    unit: str
    why: str
    step: float  # ←/→ step; 0 = not steppable (e types a value)
    group: str


STRATEGY_FIELDS: tuple[StrategyField, ...] = (
    StrategyField("maximize.soft5h", "5h soft", "%",
                  "switch at the next idle moment once the active passes this", 1,
                  "when to leave the active account"),
    StrategyField("maximize.hard5h", "5h hard", "%", "switch at once", 1,
                  "when to leave the active account"),
    StrategyField("maximize.soft7d", "7d soft", "%", "", 1, "when to leave the active account"),
    StrategyField("maximize.hard7d", "7d hard", "%", "", 1, "when to leave the active account"),
    StrategyField("maximize.landingMargin", "landing margin", "%p",
                  "a target must be this far under both soft marks", 1,
                  "when to leave the active account"),
    StrategyField("maximize.idleWindowMin", "idle window", "min",
                  "idle = under the max rise over this window", 1,
                  "when to leave the active account"),
    StrategyField("maximize.idleMaxDeltaPct", "idle max rise", "%p", "", 0.5,
                  "when to leave the active account"),
    StrategyField("maximize.forceEtaMin", "force ETA", "min",
                  "switch early when a hard cap is this close at the current rate", 1,
                  "when to leave the active account"),
    StrategyField("maximize.rebalanceCooldownMin", "rebalance cooldown", "min", "", 5,
                  "when to leave the active account"),
    StrategyField("maximize.tieEpsilon", "tie epsilon", "", "scores this close count as a tie",
                  0.05, "when to leave the active account"),
    StrategyField("prime.enabled", "priming", "", "keep idle accounts' 5h windows started", 1,
                  "priming idle accounts"),
    StrategyField("prime.jitterS", "jitter", "s", "wait LO-HI seconds after a reset", 0,
                  "priming idle accounts"),
    StrategyField("prime.maxAttempts", "max attempts", "", "attempts per 5h window", 1,
                  "priming idle accounts"),
    StrategyField("prime.model", "model", "", "model for the priming call", 0,
                  "priming idle accounts"),
)
STRATEGY_KEYS = "↑↓ move · ←→ adjust · e edit · s save · b back · q quit"


def help_entries() -> list[tuple[str, str]]:
    """``(key, what it does)`` rows for the help screen, then the legend."""
    rows: list[tuple[str, str]] = [("", "Fleet — keys")]
    rows += [(e.key, menu_title(e.action, mode_label="…")) for e in MAIN_MENU]
    rows += [
        ("enter", "on a row: switch to it (asks first when maximize would not land there)"),
        ("l", "toggle last resort on the highlighted account"),
        ("x", "exclude / include the highlighted account (disable)"),
        ("r", "re-login the highlighted account (guided; cc-swap launches nothing)"),
        ("w", "watch every account (classic watch view)"),
        ("g", "engine log (same as e)"),
        ("↑ ↓ / j k", "move; ↓ past the last row reaches the menu"),
        ("? / h", "this help"),
        ("ctrl+f", "back to Fleet from any screen"),
        ("ctrl+t", "theme"),
        ("b / esc / ←", "back, on every sub-screen"),
        ("", ""),
        ("", "Columns"),
        ("*", "the active account (> marks the cursor)"),
        ("plan", "20x / 5x from the engine's published plan, else maximize.planOverride; "
                 "team = organization account; api = API key; ? = unknown"),
        ("tier", "normal · last-r (last resort) · excl (excluded = disabled)"),
        ("rank", "the order maximize would pick accounts; ? = usage unknown"),
        ("5h / 7d", "used %, coloured against the maximize soft/hard marks; ~ = stale"),
        ("7d in", "days until the 7d window resets"),
        ("pace", "remaining 7d share over an even daily allotment (>1 = quota to spare)"),
        ("land", "yes = maximize may land here; else why not (5h≥45 = under soft − margin)"),
        ("5h window", "cold (not running) · running → reset · primed → reset (opened by priming)"),
        ("next prime", "≤HH:MM due now · HH:MM–HH:MM after its reset · or why it is not primed"),
        ("", ""),
        ("", "Status lines"),
        ("engine", "who switches: the service, another process, this TUI, or nothing; "
                   "AUTO OFF = cc-swap auto off (m → o turns it back on)"),
        ("now", "the engine's last decision (or computed here when none is fresh)"),
        ("prime", "accounts due for priming, upcoming windows, blockers; paused after "
                  "a claude update until cc-swap prime verify passes"),
    ]
    return rows
