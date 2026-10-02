"""Fleet's menus, keys and help text (pure, no Textual).

The home screen has six keys, all in its footer: ``enter`` switch, ``r``
re-login, ``l`` last resort, ``m`` menu, ``?`` help, ``q`` quit. Everything
else is an item of the ``m`` menu (a popup), one letter each, shown in a key
column with a short note on what it does. The menu's letters also work
straight from the home screen (:data:`SHORTCUT_KEYS`), except ``o``
(automatic switching on/off) and ``m`` (Mode): one stray key never turns
switching off, and ``m`` is the menu itself.

Keys are unique per level: the menu, the row keys and the reserved keys
never share a letter except ``x`` (exclude), which is both a menu item and
a row key because it acts on the selected account either way. A
sub-screen's items (Account settings) never share one with each other or
with its ``b``/``q``. ``v`` is only *View switch history* and ``u`` only
*Update Claude Code* (menu), ``i`` only *Inspect all logins* (Account
settings).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MenuEntry:
    key: str
    title: str
    action: str
    note: str = ""  # what the item does, shown dim beside it


MAIN_MENU: tuple[MenuEntry, ...] = (
    MenuEntry("o", "Automatic switching", "auto"),
    MenuEntry("m", "Mode", "mode", "who runs the engine; run one here"),
    MenuEntry("s", "Swap strategy…", "strategy", "soft/hard marks, priming"),
    MenuEntry("p", "Prime now…", "prime", "start idle accounts' 5h windows now"),
    MenuEntry("f", "Fetch latest usage", "fetch"),
    MenuEntry("x", "Exclude", "exclude", "keep it out of automatic switching"),
    MenuEntry("a", "Account settings…", "accounts",
              "add · re-login · rename · delete · inspect logins"),
    MenuEntry("e", "Engine log", "engine", "what the engine did and why"),
    MenuEntry("v", "View switch history", "history", "every switch, newest first"),
    MenuEntry("u", "Update Claude Code", "update", "check, then run claude update"),
    MenuEntry("c", "Classic dashboard", "classic", "the upstream claude-swap screen"),
    MenuEntry("q", "Quit", "quit"),
)
MAIN_KEYS: tuple[str, ...] = tuple(e.key for e in MAIN_MENU)
BY_ACTION: dict[str, MenuEntry] = {e.action: e for e in MAIN_MENU}

#: The home screen's own keys: exactly its footer.
HOME_KEYS: tuple[str, ...] = ("enter", "r", "l", "m", "?", "q")
#: Keys that act on the selected account.
ROW_KEYS: tuple[str, ...] = ("enter", "l", "x", "r")
#: Menu letters that also work from the home screen without the menu.
SHORTCUT_KEYS: tuple[str, ...] = tuple(k for k in MAIN_KEYS if k not in ("o", "m", "q"))
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
MENU_KEYS = "letter or ↑↓ enter · esc close"


@dataclass(frozen=True)
class Selected:
    """The account the home screen has selected, as the menu names it."""

    number: str
    name: str
    excluded: bool


@dataclass(frozen=True)
class MenuRow:
    key: str
    title: str
    note: str
    action: str
    tone: str = "plain"  # "warn" when the item needs attention


def menu_title(
    action: str,
    *,
    mode_label: str | None = None,
    relogin: int = 0,
    fetching: bool = False,
    auto_off: bool = False,
    selected: Selected | None = None,
) -> str:
    """An item's title with its state in the name."""
    entry = BY_ACTION[action]
    if action == "auto":
        return f"{entry.title}: {'OFF' if auto_off else 'ON'}"
    if action == "mode":
        return f"Mode: {mode_label or 'off'}"
    if action == "exclude" and selected is not None:
        verb = "Include" if selected.excluded else "Exclude"
        return f"{verb} #{selected.number} {selected.name}"
    if action == "accounts" and relogin:
        return f"{entry.title} · {relogin} need{'s' if relogin == 1 else ''} re-login"
    if action == "fetch" and fetching:
        return f"{entry.title} — fetching…"
    return entry.title


def menu_rows(
    *,
    auto_off: bool,
    holder: str,
    mode_label: str,
    thresholds: str = "",
    relogin: int = 0,
    fetching: bool = False,
    selected: Selected | None = None,
) -> list[MenuRow]:
    """The ``m`` menu as shown: every item with its state and a note.
    ``thresholds`` is the soft/hard summary (``5h 50/95 · 7d 90/98``)."""
    out: list[MenuRow] = []
    for e in MAIN_MENU:
        title = menu_title(
            e.action, mode_label=mode_label, relogin=relogin, fetching=fetching,
            auto_off=auto_off, selected=selected,
        )
        note, tone = e.note, "plain"
        if e.action == "auto":
            note = "turn it on" if auto_off else "turn it off"
            tone = "warn" if auto_off else "plain"
        elif e.action == "mode" and holder == "none":
            tone = "warn"
        elif e.action == "strategy" and thresholds:
            note = f"soft/hard {thresholds} · priming"
        elif e.action == "exclude" and selected is not None and selected.excluded:
            note = "let automatic switching pick it again"
        elif e.action == "accounts" and relogin:
            tone = "warn"
        out.append(MenuRow(e.key, title, note, e.action, tone))
    return out


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
    """``(term, explanation)`` rows for the help screen; a row with no term
    is a section heading (or a blank line)."""
    return [
        ("", "How to read Fleet"),
        ("top line", "what automatic switching is doing now, in one sentence. "
                     "'engine silent' or 'waiting for the engine' means the engine "
                     "has not confirmed it: nothing there is live"),
        ("right note", "who runs the engine — viewer · service pid N is switching: the "
                       "background service switches, this screen only watches"),
        ("! line", "only when something needs you: a dead or expiring login, priming "
                   "paused after a Claude Code update, a service that stops at logout"),
        ("bars", "┃ amber = the soft mark, ┃ red = the hard mark. The fill is green "
                 "under soft, amber from soft, red from hard; 5h and 7d each have "
                 "their own marks (m → s changes them)"),
        ("[20x] [5x]", "the plan; [team] is an organization account"),
        ("order", "the active account, then where switching would go, best first, "
                  "then the rest; excluded accounts last"),
        ("", ""),
        ("", "Tags (each account shows the most important one)"),
        ("● active", "the account Claude Code uses now"),
        ("re-login (r)", "its stored login is dead: select it and press r"),
        ("excluded", "never picked automatically (m → x includes it again)"),
        ("next", "where automatic switching goes next"),
        ("login 3d left", "the login reaches its fixed deadline soon: r renews it early"),
        ("last resort", "used only when every other account is at its limit (l toggles)"),
        ("5h off · prime", "its 5h window has not started; priming starts it at that time"),
        ("", ""),
        ("", "Words"),
        ("soft mark", "past it, cc-swap moves you at the next pause in your work"),
        ("hard mark", "at it, cc-swap moves you at once (forced)"),
        ("pause", "an idle moment: usage rose less than maximize.idleMaxDeltaPct over "
                  "the last maximize.idleWindowMin minutes"),
        ("pace / score", "how the next account is picked: the 7d quota left per day left "
                         "(above 1 = quota to spare)"),
        ("landable", "an account switching may move you to: under both soft marks minus "
                     "the landing margin, a working login, not excluded"),
        ("priming", "starting an idle account's 5h window early with a tiny request, so "
                    "its reset comes sooner"),
        ("viewer / lease", "one process at a time runs the engine and holds its lease: the "
                           "service, a terminal cc-swap auto, the menu bar or this TUI. "
                           "Otherwise this screen is a viewer and never switches by itself"),
        ("dry run", "an engine that decides but never switches"),
        ("", ""),
        ("", "Keys"),
        ("↑ ↓ / j k", "select an account (← → across the two columns when wide)"),
        ("enter", "switch to it (asks first when switching would not land there)"),
        ("r", "re-login it (guided; cc-swap launches nothing)"),
        ("l", "last resort on/off"),
        ("m", "menu: o automatic switching on/off · m mode · s strategy · p prime · "
              "f fetch · x exclude · a accounts · e engine log · v history · "
              "u update · c classic · q quit"),
        (" ".join(SHORTCUT_KEYS), "those menu letters also work straight from here"),
        ("w / g", "watch every account / engine log"),
        ("? / h", "this help"),
        ("ctrl+f", "back to Fleet from any screen"),
        ("ctrl+t", "theme"),
        ("b / esc", "back, on every sub-screen"),
        ("q", "quit"),
    ]
