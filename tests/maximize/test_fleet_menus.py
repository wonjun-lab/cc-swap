"""tui/menus.py: Fleet's menu, keys and help (pure, no Textual)."""

from __future__ import annotations

import pytest

from claude_swap.tui import menus


def test_menu_keys_are_unique_and_every_title_names_its_item():
    keys = [e.key for e in menus.MAIN_MENU]
    assert len(set(keys)) == len(keys)
    for entry in menus.MAIN_MENU:
        assert len(entry.key) == 1 and entry.title
    # The menu shows its keys in their own column, so a title need not start
    # with its letter; most still do.
    firsts = [e for e in menus.MAIN_MENU if e.title[0].lower() == e.key]
    assert {e.key for e in menus.MAIN_MENU} - {e.key for e in firsts} == {"o", "x"}
    for key, title, _action in menus.ACCOUNT_ITEMS:
        assert menus.bold_spans(title, key) == (0, 1)
    assert menus.bold_spans("Engine log", "l") == (7, 8)
    assert menus.bold_spans("Quit", "z") is None


def test_the_menu_holds_every_former_fleet_menu_action():
    actions = {e.action for e in menus.MAIN_MENU}
    assert actions >= {
        "strategy", "mode", "prime", "fetch", "accounts", "engine", "history",
        "classic", "quit",
    }
    assert {"auto", "exclude"} <= actions


def test_home_keys_are_the_footer_and_do_not_collide():
    assert menus.HOME_KEYS == ("enter", "r", "l", "h", "m", "?", "q")
    # x (exclude) is a menu item too; n (name) is a row key the footer does
    # not list: it would not fit 80 columns (? help and Account settings do).
    assert set(menus.ROW_KEYS) - {"x", "n"} <= set(menus.HOME_KEYS)
    assert "n" in menus.ROW_KEYS
    assert "n" not in (*menus.MAIN_KEYS, *menus.RESERVED_KEYS)
    # Shortcuts: the menu's letters that also work from home, never o (one
    # stray key must not turn switching off) nor m (the menu itself).
    assert "o" not in menus.SHORTCUT_KEYS and "m" not in menus.SHORTCUT_KEYS
    assert set(menus.SHORTCUT_KEYS) <= set(menus.MAIN_KEYS)
    assert not set(menus.SHORTCUT_KEYS) & (set(menus.HOME_KEYS) | set(menus.RESERVED_KEYS))
    assert not set(menus.HOME_KEYS) & (set(menus.RESERVED_KEYS) - {"?"})
    account = [key for key, _title, _action in menus.ACCOUNT_ITEMS]
    assert len(set(account)) == len(account)
    assert "b" not in account and "b" not in menus.MAIN_KEYS  # b is back everywhere


def test_menu_titles_carry_their_state():
    assert menus.menu_title("auto", auto_off=False) == "Automatic switching: ON"
    assert menus.menu_title("auto", auto_off=True) == "Automatic switching: OFF"
    assert menus.menu_title("mode", mode_label="service · viewing") == "Mode: service · viewing"
    assert menus.menu_title("accounts", relogin=1) == "Account settings… · 1 needs re-login"
    assert menus.menu_title("accounts", relogin=2) == "Account settings… · 2 need re-login"
    assert menus.menu_title("fetch", fetching=True) == "Fetch latest usage — fetching…"
    assert menus.menu_title("strategy") == "Swap strategy…"
    pick = menus.Selected("2", "side", excluded=False)
    assert menus.menu_title("exclude", selected=pick) == "Exclude side"
    back = menus.Selected("5", "alt", excluded=True)
    assert menus.menu_title("exclude", selected=back) == "Include alt"


def test_menu_rows_explain_and_flag():
    rows = menus.menu_rows(
        auto_off=True, holder="none", mode_label="off", thresholds="5h 50/95 · 7d 90/98",
        relogin=1, selected=menus.Selected("5", "alt", excluded=True),
    )
    by = {r.action: r for r in rows}
    assert [r.key for r in rows] == list(menus.MAIN_KEYS)
    assert (by["auto"].title, by["auto"].note, by["auto"].tone) == (
        "Automatic switching: OFF", "turn it on", "warn"
    )
    assert by["mode"].tone == "warn"  # nothing runs the engine
    assert by["strategy"].note == "soft/hard 5h 50/95 · 7d 90/98 · priming"
    assert by["exclude"].note == "let automatic switching pick it again"
    assert by["accounts"].tone == "warn" and "inspect logins" in by["accounts"].note
    on = {r.action: r for r in menus.menu_rows(auto_off=False, holder="service",
                                                 mode_label="service · viewing")}
    assert (on["auto"].note, on["auto"].tone, on["mode"].tone) == ("turn it off", "plain", "plain")


@pytest.mark.parametrize(("holder", "pid", "label"), [
    ("service", 4121, "service · viewing"),
    ("other", 5521, "pid 5521 · viewing"),
    ("other", None, "another engine · viewing"),
    ("here-live", 7310, "here · live"),
    ("here-dry", 7310, "here · dry-run"),
    ("none", None, "off"),
])
def test_mode_labels(holder, pid, label):
    assert menus.mode_label(holder, pid) == label


def test_the_0_3_0_keys_mean_one_thing_across_fleet():
    """`v` was both View switch history (main) and Verify logins (Account
    settings) when Batch 1 and 2 met; each new letter now has one meaning."""
    account = {key: action for key, _title, action in menus.ACCOUNT_ITEMS}
    main = {e.key: e.action for e in menus.MAIN_MENU}
    assert main["v"] == "history" and "v" not in account
    assert "u" not in main  # Update Claude Code is gone: cc-swap never updates it
    assert account["i"] == "verify" and "i" not in main
    assert "i" not in (*menus.ROW_KEYS, *menus.RESERVED_KEYS)


def test_account_key_hints_name_every_item():
    for key, _title, _action in menus.ACCOUNT_ITEMS:
        assert f" {key} " in f" {menus.ACCOUNT_KEYS} "
    from claude_swap.maximize import home

    assert len(menus.ACCOUNT_KEYS) <= home.text_width(80)  # one line in 80 columns


def test_help_explains_the_jargon_and_lists_every_key():
    entries = menus.help_entries()
    terms = {term for term, _what in entries}
    text = "\n".join(f"{t} {w}" for t, w in entries)
    for term in ("soft mark", "hard mark", "next", "last resort", "pace / score", "priming",
                 "viewer / lease", "landable", "dry run", "● active", "re-login (r)",
                 "excluded", "prime 19:30", "keychain locked (f)", "reading 2h old",
                 "! lines"):
        assert term in terms, term
    for key in (*menus.HOME_KEYS, *menus.SHORTCUT_KEYS, "w", "ctrl+f", "ctrl+t"):
        assert key in text, key
    for entry in menus.MAIN_MENU:
        assert f"{entry.key} " in text


def test_help_explains_the_reset_wait_and_idle_pattern_words():
    terms = {term for term, _what in menus.help_entries()}
    assert {"waiting it out", "quiet time", "preempt", "rebalance deferred"} <= terms
    assert "idle pattern" not in terms  # nothing learned to show without it
    from claude_swap.tui.fleet_help import TERM_WIDTH

    assert max(len(t) for t in terms) < TERM_WIDTH  # a gap before every explanation


def test_help_shows_what_has_been_learned_when_given():
    line = "idle pattern: 9 days learned · next quiet window 23:00–07:30"
    entries = dict(menus.help_entries(line))
    assert entries["idle pattern"].startswith("9 days learned · next quiet window 23:00–07:30")
    assert entries["idle pattern"].endswith("(m → s: learn idle pattern turns it off)")
    assert ("", "Learned so far") in menus.help_entries(line)
    off = dict(menus.help_entries("idle pattern: off (maximize.learnIdlePattern)"))
    assert off["idle pattern"] == (
        "off: nothing is learned (m → s: learn idle pattern turns it on)"
    )


NEW_STRATEGY_KEYS = (
    "maximize.resetWaitMin", "maximize.learnIdlePattern", "maximize.preempt",
    "maximize.preemptHorizonMaxH", "maximize.busyRebalanceGap",
)


def test_swap_strategy_edits_the_reset_wait_and_idle_pattern_settings():
    from claude_swap.settings import SETTING_SPECS

    fields = {f.key: f for f in menus.STRATEGY_FIELDS}
    for key in NEW_STRATEGY_KEYS:
        assert key in fields, key
        spec = SETTING_SPECS[key]
        field = fields[key]
        assert field.step > 0, key  # ←/→ adjust (and e types a value)
        if spec.kind != "bool":
            assert field.step <= (spec.hi - spec.lo) / 10, key
    assert fields["maximize.resetWaitMin"].group == "when to leave the active account"
    assert {fields[k].group for k in NEW_STRATEGY_KEYS[1:]} == {menus.QUIET_GROUP}
    # Groups are contiguous (the screen prints a heading per run).
    groups = [f.group for f in menus.STRATEGY_FIELDS]
    runs = [g for i, g in enumerate(groups) if i == 0 or groups[i - 1] != g]
    assert len(runs) == len(set(runs))
    assert all(len(f.label) <= 19 for f in menus.STRATEGY_FIELDS)
    assert len({f.key for f in menus.STRATEGY_FIELDS}) == len(menus.STRATEGY_FIELDS)
    assert all(f.key in SETTING_SPECS for f in menus.STRATEGY_FIELDS)


@pytest.mark.parametrize(("key", "delta", "start", "expected"), [
    ("maximize.resetWaitMin", -1, 0, 0),           # 0 = off is the floor
    ("maximize.resetWaitMin", 1, 60, 60),          # 60 is the ceiling
    ("maximize.preemptHorizonMaxH", -1, 1, 1),
    ("maximize.preemptHorizonMaxH", 1, 48, 48),
    ("maximize.busyRebalanceGap", 0.1, 5.0, 5.0),
    ("maximize.busyRebalanceGap", -0.1, 0.05, 0.0),
    ("maximize.busyRebalanceGap", 0.1, 0.5, 0.6),
    ("maximize.preempt", 1, True, False),          # a bool toggles either way
    ("maximize.learnIdlePattern", -1, False, True),
])
def test_swap_strategy_steps_stay_in_range(key, delta, start, expected):
    from claude_swap.maximize import fleet as fx
    from claude_swap.settings import MaximizeSettings, PrimeSettings

    values = {**fx.strategy_values(MaximizeSettings(), PrimeSettings()), key: start}
    stepped = fx.strategy_step(values, key, delta)[key]
    assert stepped == pytest.approx(expected) and type(stepped) is type(expected)


@pytest.mark.parametrize(("key", "raw"), [
    ("maximize.resetWaitMin", "61"), ("maximize.resetWaitMin", "-1"),
    ("maximize.resetWaitMin", "7.5"), ("maximize.preemptHorizonMaxH", "0"),
    ("maximize.preemptHorizonMaxH", "49"), ("maximize.busyRebalanceGap", "5.5"),
    ("maximize.busyRebalanceGap", "nan"), ("maximize.preempt", "maybe"),
    ("maximize.learnIdlePattern", ""),
])
def test_swap_strategy_typed_values_are_validated(key, raw):
    """``e`` parses as ``cc-swap config set`` does (``parse_setting_value``)."""
    from claude_swap.exceptions import ClaudeSwitchError
    from claude_swap.settings import SETTING_SPECS, parse_setting_value

    with pytest.raises(ClaudeSwitchError):
        parse_setting_value(SETTING_SPECS[key], raw)


def test_swap_strategy_writes_the_new_settings_as_config_set_reads_them():
    from claude_swap.maximize import fleet as fx
    from claude_swap.settings import MaximizeSettings, PrimeSettings

    saved = fx.strategy_values(MaximizeSettings(), PrimeSettings())
    edited = fx.strategy_step(saved, "maximize.preempt", 1)
    edited = fx.strategy_step(edited, "maximize.busyRebalanceGap", 0.1)
    edited = fx.strategy_step(edited, "maximize.resetWaitMin", -1)
    assert sorted(fx.strategy_writes(saved, edited)) == [
        ("maximize.busyRebalanceGap", "0.6"), ("maximize.preempt", "false"),
        ("maximize.resetWaitMin", "14"),
    ]
    s = fx.strategy_settings(edited)
    assert (s.preempt, s.busy_rebalance_gap, s.reset_wait_min) == (False, 0.6, 14)


def test_the_hold_picker_offers_1_2_4_hours_until_and_off():
    from claude_swap.maximize.hold import clock_text

    now = 1_790_000_000.0
    rows = menus.hold_rows(None, now)
    # Letters, never digits: a digit starts typing a time (12:00), so it can
    # never set a 1-hour hold by accident.
    assert [r.key for r in rows] == ["h", "t", "f", "u", "o"]
    assert not any(r.key.isdigit() for r in rows)
    assert [r.action for r in rows] == [
        "hold:3600", "hold:7200", "hold:14400", menus.HOLD_UNTIL, menus.HOLD_OFF]
    assert [r.title for r in rows[:3]] == ["One hour", "Two hours", "Four hours"]
    assert rows[1].note == f"until {clock_text(now + 7200, now)}"
    assert (rows[-1].note, rows[-1].tone) == ("no hold now", "plain")
    held = menus.hold_rows(now + 1800, now)[-1]
    assert held.note == f"lift the hold (it ends {clock_text(now + 1800, now)})"
    assert held.tone == "warn"
    keys = [r.key for r in rows]
    assert len(set(keys)) == len(keys) and "b" not in keys  # b and esc close it
    for row in rows:
        if row.key.isalpha():
            assert menus.bold_spans(row.title, row.key) is not None


def test_n_names_the_selected_account_and_help_says_so():
    from claude_swap.maximize import home

    entries = dict(menus.help_entries())
    assert entries["n"].startswith("name the selected account")
    assert "cc-swap alias" in entries["n"]
    # Why it is not in the footer: with it the footer no longer fits 80 columns.
    with_n = " · ".join(f"{k} {w}" for k, w in (*home.KEY_HINTS, ("n", "name")))
    assert len(with_n) > home.text_width(80)
    assert "n" not in dict(home.KEY_HINTS)
    account_settings = {key for key, _t, _a in menus.ACCOUNT_ITEMS}
    assert "n" in account_settings  # m → a → n names an account too


def test_help_explains_the_hold_and_the_summary():
    entries = dict(menus.help_entries())
    assert "h" in entries and "1, 2 or 4 hours" in entries["h"]
    assert entries["holding"].startswith("you asked to stay on the active account")
    assert "not weighted by plan" in entries["summary"]
    assert "? / h" not in entries and entries["?"] == "this help"
