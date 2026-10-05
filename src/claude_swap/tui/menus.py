"""Fleet's menus, keys and help text (pure, no Textual).

The home screen has seven keys, all in its footer: ``enter`` switch, ``r``
re-login, ``l`` last resort, ``h`` hold (stay on the active account: a small
picker, :func:`hold_rows`), ``m`` menu, ``?`` help, ``q`` quit. Everything
else is an item of the ``m`` menu (a popup), one letter each, shown in a key
column with a short note on what it does. The menu's letters also work
straight from the home screen (:data:`SHORTCUT_KEYS`), except ``o``
(automatic switching on/off) and ``m`` (Mode): one stray key never turns
switching off, and ``m`` is the menu itself.

Keys are unique per level: the menu, the row keys and the reserved keys
never share a letter except ``x`` (exclude), which is both a menu item and
a row key because it acts on the selected account either way. A
sub-screen's items (Account settings) never share one with each other or
with its ``b``/``q``. ``v`` is only *View switch history* (menu), ``i`` only *Inspect all
logins* (Account settings).
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
              "add · sign in · re-login · rename · delete · inspect logins"),
    MenuEntry("e", "Engine log", "engine", "what the engine did and why"),
    MenuEntry("v", "View switch history", "history", "every switch, newest first"),
    MenuEntry("c", "Classic dashboard", "classic", "the upstream claude-swap screen"),
    MenuEntry("q", "Quit", "quit"),
)
MAIN_KEYS: tuple[str, ...] = tuple(e.key for e in MAIN_MENU)
BY_ACTION: dict[str, MenuEntry] = {e.action: e for e in MAIN_MENU}

#: The home screen's own keys: exactly its footer.
HOME_KEYS: tuple[str, ...] = ("enter", "r", "l", "h", "m", "?", "q")
#: Keys that act on the selected account. ``n`` (name it) is not in the
#: footer, which would no longer fit 80 columns: ``?`` help lists it, and
#: Account settings (m → a) has the same ``n``.
ROW_KEYS: tuple[str, ...] = ("enter", "l", "x", "r", "n")
#: Menu letters that also work from the home screen without the menu.
SHORTCUT_KEYS: tuple[str, ...] = tuple(k for k in MAIN_KEYS if k not in ("o", "m", "q"))
#: Keys that are deliberately not menu items (navigation, help, theme).
RESERVED_KEYS: tuple[str, ...] = ("w", "?", "j", "k", "b", "g")

ACCOUNT_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("a", "Add current login", "add"),
    ("s", "Sign in a new account…", "new"),
    ("t", "Token or API key…", "token"),
    ("r", "Re-login…", "relogin"),
    ("n", "Name (alias)…", "alias"),
    ("d", "Delete account…", "delete"),
    ("i", "Inspect all logins (doctor)", "verify"),
)
#: One line within 80 columns: ``enter`` selects and ``q`` quits everywhere.
ACCOUNT_KEYS = "a add · s new · t token · r re-login · n name · d delete · i inspect · b back"
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


#: ``h``'s fixed choices: (key, title, hours). Letters, never digits: a
#: digit typed in the picker starts a time (``12:00``) instead, so it can
#: never set a 1-hour hold by accident. ``h h`` holds one hour.
HOLD_HOURS: tuple[tuple[str, str, int], ...] = (
    ("h", "One hour", 1), ("t", "Two hours", 2), ("f", "Four hours", 4),
)
#: The hold picker's actions: ``hold:<seconds>``, then these two, and a
#: digit typed in the picker (``hold:typed:<digit>``).
HOLD_UNTIL, HOLD_OFF, HOLD_TYPED = "hold:until", "hold:off", "hold:typed:"


def hold_rows(held_until: float | None, now: float) -> list[MenuRow]:
    """The ``h`` picker: one / two / four hours / until a time / off, each
    with when it would end. ``held_until`` is the end of the hold on the
    active account (None: no hold)."""
    from claude_swap.maximize.hold import clock_text

    rows = [
        MenuRow(key, title, f"until {clock_text(now + hours * 3600, now)}",
                f"hold:{hours * 3600}")
        for key, title, hours in HOLD_HOURS
    ]
    rows.append(MenuRow("u", "Until a time…", "or just type it: HH:MM, local time",
                        HOLD_UNTIL))
    if held_until is not None:
        rows.append(MenuRow("o", "Off", f"lift the hold (it ends {clock_text(held_until, now)})",
                            HOLD_OFF, "warn"))
    else:
        rows.append(MenuRow("o", "Off", "no hold now", HOLD_OFF))
    return rows


HOLD_TITLE = "Hold #{number} {name} — stay on this account"
#: Under the hold picker's title: what a hold does and does not stop.
HOLD_NOTE = (
    "soft, preempt and rebalance moves wait; a hard mark, 100% and a reset wait "
    "still switch"
)


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


#: The Swap strategy group of the idle-pattern knobs; its heading also says
#: what has been learned (``view.idle_pattern_text``).
QUIET_GROUP = "your busy and quiet times"

#: Every Swap strategy field, in screen order. Values are validated where
#: they change: ←/→ clamp into the key's ``SETTING_SPECS`` range (soft never
#: passes hard; a bool toggles), and ``e`` parses strictly
#: (``settings.parse_setting_value``: type, range, finite), as ``cc-swap
#: config set`` does.
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
    StrategyField("maximize.resetWaitMin", "reset wait", "min",
                  "wait out a window that resets this soon instead of switching (0 = off)", 1,
                  "when to leave the active account"),
    StrategyField("maximize.learnedRide", "learned ride", "",
                  "past a hard mark of 99+, use a learned share of the last 1% first", 1,
                  "when to leave the active account"),
    StrategyField("maximize.rideWindows", "ride windows", "",
                  "windows that ride (5h switches at its hard mark unless listed)", 1,
                  "when to leave the active account"),
    StrategyField("maximize.rideMaxMin", "ride max", "min",
                  "a ride lasts at most this long (0 = no ride)", 5,
                  "when to leave the active account"),
    StrategyField("maximize.rebalanceCooldownMin", "rebalance cooldown", "min", "", 5,
                  "when to leave the active account"),
    StrategyField("maximize.tieEpsilon", "tie epsilon", "", "scores this close count as a tie",
                  0.05, "when to leave the active account"),
    StrategyField("maximize.learnIdlePattern", "learn idle pattern", "",
                  "learn your usual busy and quiet times from the usage history", 1,
                  QUIET_GROUP),
    StrategyField("maximize.preempt", "preempt", "",
                  "move at an idle moment when the 7d would pass soft before your quiet time",
                  1, QUIET_GROUP),
    StrategyField("maximize.preemptHorizonMaxH", "preempt horizon", "h",
                  "look at most this far ahead for a pre-emptive move", 1, QUIET_GROUP),
    StrategyField("maximize.busyRebalanceGap", "busy rebalance gap", "",
                  "in a busy time, rebalance at once only for a score gain this large", 0.1,
                  QUIET_GROUP),
    StrategyField("prime.enabled", "priming", "", "keep idle accounts' 5h windows started", 1,
                  "priming idle accounts"),
    StrategyField("prime.jitterS", "jitter", "s", "wait LO-HI seconds after a reset", 0,
                  "priming idle accounts"),
    StrategyField("prime.maxAttempts", "max attempts", "", "attempts per 5h window", 1,
                  "priming idle accounts"),
    StrategyField("prime.model", "model", "", "model for the priming call", 0,
                  "priming idle accounts"),
    StrategyField("prime.autoVerify", "auto-verify", "",
                  "after a Claude Code update, re-run the zero-cost prime verify", 1,
                  "priming idle accounts"),
)
STRATEGY_KEYS = "↑↓ move · ←→ adjust · e edit · s save · b back · q quit"



def help_entries(idle_pattern: str | None = None) -> list[tuple[str, str]]:
    """``(term, explanation)`` rows for the help screen; a row with no term
    is a section heading (or a blank line). ``idle_pattern``
    (``view.idle_pattern_text``: ``idle pattern: 9 days learned · next quiet
    window 23:00–07:30``) adds what the engine has learned so far."""
    learned: list[tuple[str, str]] = []
    if idle_pattern:
        text = idle_pattern.removeprefix("idle pattern: ")
        if text.startswith("off"):
            text = "off: nothing is learned (m → s: learn idle pattern turns it on)"
        else:
            text += " (m → s: learn idle pattern turns it off)"
        learned = [("", ""), ("", "Learned so far"), ("idle pattern", text)]
    return [
        ("", "How to read Fleet"),
        ("top line", "what automatic switching is doing now, in one sentence. "
                     "'engine silent' or 'waiting for the engine' means the engine "
                     "has not confirmed it: nothing there is live. 'Holding #1 until "
                     "15:30' means you asked to stay on it (h)"),
        ("right note", "who runs the engine — viewer · service pid N is switching: the "
                       "background service switches, this screen only watches"),
        ("! lines", "only when something needs you, with what to do: a dead or expiring "
                    "login, priming paused (claude killed by the OS at launch, an update "
                    "settling, a new version not verified yet), a locked keychain, a "
                    "service that stops at logout. ! = something for you to do; a pause "
                    "that lifts by itself has none. Up to three lines when the table "
                    "leaves rows over"),
        ("summary", "the line over the table, over the accounts automatic switching can "
                    "use (not a dead, expired or excluded login, not an API key). 5h free: "
                    "those it could switch to now (both windows under their soft marks "
                    "less the landing margin, no login about to end), plus the active one "
                    "while under its soft marks. next back in: how long until another "
                    "account, kept off only by its 5h, comes back. 7d left "
                    "this week ≈ N accounts: the 7d room left, (100 − 7d%)/100 "
                    "per account added up as whole accounts — not weighted by plan, so a "
                    "20x and a 5x account count alike. next 7d in: how long until the "
                    "soonest weekly reset, and whose. A short terminal drops it first"),
        ("order", "● the active account; 1, 2, 3 … the accounts automatic switching "
                  "would land on, in the order it would try them (the engine's own "
                  "list, over the readings it trusts); · only when forced (a hard mark, "
                  "or every account at its limit); – not now (a dead or expired login, "
                  "excluded, a locked keychain, a login shared with another place, a "
                  "reading too old to trust or none, past its hard marks). Dim while "
                  "nothing is switching. The table "
                  "lists the accounts in this order"),
        ("account", "the alias, else the part of the address before the @ (two alike "
                    "say where they are from: jordan.lee@uni), then #N: its slot number, "
                    "which the attention line and cc-swap commands use. The panel below "
                    "shows the whole address"),
        ("plan", "20x / 5x; team is an organization account"),
        ("bars", "┃ amber = the soft mark, ┃ red = the hard mark. The fill is green "
                 "under soft, amber from soft, red from hard; 5h and 7d each have "
                 "their own marks (m → s changes them)"),
        ("5h / 7d resets", "when each window resets, for every account: 1h47m under 5h "
                           "resets = in 1h47m (the panel below says at what time); "
                           "3d19h · Oct  7 02:18 under 7d resets = in 3d19h, on Oct 7 at "
                           "02:18. 'not started' = no 5h window is running; — = not known"),
        ("panel", "under the table when the terminal has room: the selected account "
                  "in full, with every usage window and its exact reset"),
        ("", ""),
        ("", "Status (each account shows the most important one)"),
        ("● active", "the account Claude Code uses now"),
        ("re-login (r)", "its stored login is dead: select it and press r"),
        ("keychain locked (f)", "its login cannot be read from the macOS keychain: "
                                "unlock it, then f fetches"),
        ("excluded", "never picked automatically (m → x includes it again)"),
        ("next", "where automatic switching goes next"),
        ("login 3d left", "the login reaches its fixed deadline soon: r renews it early"),
        ("reading 2h old", "its usage was last read that long ago. Amber: the engine no "
                           "longer trusts it and counts the account as unknown (never "
                           "next, not in the summary); dim: still trusted"),
        ("last resort", "used only when every other account is at its limit (l toggles)"),
        ("prime 19:30", "its 5h window has not started; priming starts it then (now: at "
                        "the next tick; 5h off: priming does not run)"),
        ("primed", "priming opened the 5h window it is in (shown when nothing above "
                   "applies)"),
        ("", ""),
        ("", "Words"),
        ("soft mark", "past it, cc-swap moves you at the next pause in your work"),
        ("hard mark", "at it, cc-swap moves you at once (forced)"),
        ("pause", "an idle moment: usage rose less than maximize.idleMaxDeltaPct over "
                  "the last maximize.idleWindowMin minutes"),
        ("waiting it out", "a mark is crossed, but that window resets within "
                           "maximize.resetWaitMin minutes: cc-swap waits for the reset "
                           "instead of switching (a switch makes Claude Code re-read the "
                           "whole context), and switches at once if it hits 100%"),
        ("riding", "a window in maximize.rideWindows reads a hard mark of 99%+: usage is "
                   "shown in whole percents, so up to a point is left; cc-swap uses a "
                   "learned share of it, then switches (at once if you pause or it hits "
                   "100%)"),
        ("quiet time", "when you are usually idle, learned from the last 14 days: an hour "
                       "or more that was busy less than 20% of the time (weekdays and "
                       "weekends apart, after 3 days)"),
        ("preempt", "the active 7d is on pace to pass its soft mark before your next quiet "
                    "time: cc-swap moves at an idle moment now instead of being forced to "
                    "in a busy stretch"),
        ("rebalance deferred", "a slightly better account exists but this is usually a "
                               "busy time: the move waits for your quiet time"),
        ("holding", "you asked to stay on the active account (h, or cc-swap hold) so a long "
                    "task keeps its context: soft, preempt and rebalance moves wait until the "
                    "hold ends (at most 24h). A hard mark, 100% and a reset wait still "
                    "switch, and any change of the active account ends the hold"),
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
        *learned,
        ("", ""),
        ("", "Keys"),
        ("↑ ↓ / j k", "select an account (a click selects too)"),
        ("enter", "switch to it (asks first when switching would not land there)"),
        ("r", "re-login it (guided; cc-swap launches nothing)"),
        ("l", "last resort on/off"),
        ("n", "name the selected account: an alias shown instead of its short name "
              "(enter saves, an empty name brings the short name back, esc cancels; the "
              "rules of cc-swap alias). Account settings (m → a) has it too"),
        ("h", "hold:stay on the active account for 1, 2 or 4 hours (h, t, f) or until a "
              "time (u, or just type it: 12:00), or lift the hold (o)"),
        ("m", "menu: o automatic switching on/off · m mode · s strategy · p prime · "
              "f fetch · x exclude · a accounts · e engine log · v history · "
              "c classic · q quit"),
        (" ".join(SHORTCUT_KEYS), "those menu letters also work straight from here"),
        ("w / g", "watch every account / engine log"),
        ("?", "this help"),
        ("ctrl+f", "back to Fleet from any screen"),
        ("ctrl+t", "theme"),
        ("b / esc", "back, on every sub-screen"),
        ("q", "quit"),
    ]
