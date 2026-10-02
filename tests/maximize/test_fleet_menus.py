"""tui/menus.py: the Fleet menus, keys and help (pure, no Textual)."""

from __future__ import annotations

import pytest

from claude_swap.tui import menus


def test_menu_letters_are_bold_and_first_letters():
    for entry in menus.MAIN_MENU:
        title = menus.menu_title(entry.action)
        assert title[0].lower() == entry.key
        assert menus.bold_spans(title, entry.key) == (0, 1)
    for key, title, _action in menus.ACCOUNT_ITEMS:
        assert menus.bold_spans(title, key) == (0, 1)
    # A key that is not the first letter is found where it first appears.
    assert menus.bold_spans("Engine log", "l") == (7, 8)
    assert menus.bold_spans("Quit", "z") is None


def test_menu_and_row_keys_do_not_collide():
    main = set(menus.MAIN_KEYS)
    assert len(main) == len(menus.MAIN_KEYS)
    assert not main & set(menus.ROW_KEYS)
    assert not main & set(menus.RESERVED_KEYS)
    assert not set(menus.ROW_KEYS) & set(menus.RESERVED_KEYS)
    account = [key for key, _title, _action in menus.ACCOUNT_ITEMS]
    assert len(set(account)) == len(account)
    assert "b" not in account  # b is back on every sub-screen


def test_menu_titles_carry_their_state():
    assert menus.menu_title("mode", mode_label="service · viewing") == "Mode: service · viewing"
    assert menus.menu_title("accounts", relogin=1) == "Account settings · 1 needs re-login"
    assert menus.menu_title("accounts", relogin=2) == "Account settings · 2 need re-login"
    assert menus.menu_title("fetch", fetching=True) == "Fetch latest usage — fetching…"
    assert menus.menu_title("strategy") == "Swap strategy"


@pytest.mark.parametrize(("holder", "pid", "label", "short"), [
    ("service", 4121, "service · viewing", "service"),
    ("other", 5521, "pid 5521 · viewing", "pid 5521"),
    ("here-live", 7310, "here · live", "live"),
    ("here-dry", 7310, "here · dry-run", "dry-run"),
    ("none", None, "off", "off"),
])
def test_mode_labels(holder, pid, label, short):
    assert menus.mode_label(holder, pid) == label
    assert menus.mode_short(holder, pid) == short


@pytest.mark.parametrize("width", [120, 100, 80, 60, 40])
def test_folded_menu_keeps_every_item_in_order_within_the_width(width):
    lines = menus.folded_menu(width, mode_label="service · viewing")
    flat = [title for line in lines for title, _key in line]
    assert [t.split(":")[0] for t in flat] == [e.short for e in menus.MAIN_MENU]
    assert "Mode: service" in flat
    for line in lines:
        assert len(menus.SEP.join(title for title, _ in line)) + 2 <= max(width, 30)
    if width >= 100:  # ten items since 0.3.0 (u Update, v View swaps)
        assert len(lines) == 1


def test_every_short_name_contains_its_key():
    for entry in menus.MAIN_MENU:
        assert menus.bold_spans(entry.short, entry.key) is not None, entry


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


def test_help_lists_every_key_and_column():
    text = "\n".join(f"{k} {d}" for k, d in menus.help_entries())
    for key in (*menus.MAIN_KEYS, *menus.ROW_KEYS, "w", "?", "ctrl+f", "ctrl+t"):
        assert key in text
    for column in ("plan", "tier", "rank", "pace", "land", "5h window", "next prime"):
        assert column in text
