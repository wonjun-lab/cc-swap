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
        "strategy", "mode", "prime", "fetch", "accounts", "engine", "history", "update",
        "classic", "quit",
    }
    assert {"auto", "exclude"} <= actions


def test_home_keys_are_the_footer_and_do_not_collide():
    assert menus.HOME_KEYS == ("enter", "r", "l", "m", "?", "q")
    assert set(menus.ROW_KEYS) - {"x"} <= set(menus.HOME_KEYS)
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
    assert menus.menu_title("exclude", selected=pick) == "Exclude #2 side"
    back = menus.Selected("5", "alt", excluded=True)
    assert menus.menu_title("exclude", selected=back) == "Include #5 alt"


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
    assert main["u"] == "update" and "u" not in account
    assert account["i"] == "verify" and "i" not in main
    assert "i" not in (*menus.ROW_KEYS, *menus.RESERVED_KEYS)


def test_account_key_hints_name_every_item():
    for key, _title, _action in menus.ACCOUNT_ITEMS:
        assert f" {key} " in f" {menus.ACCOUNT_KEYS} "


def test_help_explains_the_jargon_and_lists_every_key():
    entries = menus.help_entries()
    terms = {term for term, _what in entries}
    text = "\n".join(f"{t} {w}" for t, w in entries)
    for term in ("soft mark", "hard mark", "next", "last resort", "pace / score", "priming",
                 "viewer / lease", "landable", "dry run", "● active", "re-login (r)",
                 "excluded", "5h off · prime"):
        assert term in terms, term
    for key in (*menus.HOME_KEYS, *menus.SHORTCUT_KEYS, "w", "ctrl+f", "ctrl+t"):
        assert key in text, key
    for entry in menus.MAIN_MENU:
        assert f"{entry.key} " in text
