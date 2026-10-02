"""The Fleet screen's menus, key hints and help text (pure, no Textual).

codex-swap's convention: the menu *is* the shortcut list. Every item's key
is its first letter (drawn bold), a state is part of the item's name
(``Mode: service · viewing``), and the key hints list only what the menu
does not. The menu keys and the row keys never collide; a test pins that.
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
)
ACCOUNT_KEYS = (
    "enter select · a add · t token · r re-login · n name · d delete · b back · q quit"
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
) -> str:
    """An item's title with its state in the name."""
    entry = BY_ACTION[action]
    if action == "mode":
        return f"Mode: {mode_label or 'off'}"
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
        ("engine", "who switches: the service, another process, this TUI, or nothing"),
        ("now", "the engine's last decision (or computed here when none is fresh)"),
        ("prime", "accounts due for priming, upcoming windows, blockers"),
    ]
    return rows
