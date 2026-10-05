"""maximize/names.py: the short display name of every account — its alias,
else the part of its address before the @, made unique."""

from __future__ import annotations

import pytest

from claude_swap.maximize import names


@pytest.mark.parametrize(("accounts", "expected"), [
    # The local part by default; an alias wins.
    ([("1", "dev.shared@example.com", ""), ("2", "dev.master@example.com", "main")],
     {"1": "dev.shared", "2": "main"}),
    # Same local part, different domains: both say where they are from.
    ([("3", "jordan.lee@example.com", ""), ("4", "jordan.lee@uni.example", "")],
     {"3": "jordan.lee@example", "4": "jordan.lee@uni"}),
    # The first domain label is the same too: as much of the domain as it takes.
    ([("1", "a@mail.one.example", ""), ("2", "a@mail.two.example", "")],
     {"1": "a@mail-one", "2": "a@mail-two"}),
    # A short name that would read like another account's alias gives way.
    ([("1", "side@example.com", ""), ("2", "other@example.com", "side")],
     {"1": "side@example", "2": "side"}),
    # Case does not make two names different.
    ([("1", "Dev@example.com", ""), ("2", "dev@uni.example", "")],
     {"1": "Dev@example", "2": "dev@uni"}),
    # Blank aliases are no alias; a missing email is the slot.
    ([("1", "x@example.com", "  "), ("2", "", "")], {"1": "x", "2": "#2"}),
])
def test_display_names(accounts, expected):
    assert names.display_names(accounts) == expected


def test_names_are_unique_and_never_a_whole_address():
    # Slots 1 and 4 hold the same address (a personal and a team account).
    accounts = [(str(n), f"same@host{n % 3}.example.com", "") for n in range(1, 7)]
    accounts[1] = ("2", "same@other.example.com", "")
    got = names.display_names(accounts)
    assert len({v.lower() for v in got.values()}) == len(got)
    assert all(v != email for (_n, email, _a), v in zip(accounts, got.values()))
    assert got["2"] == "same@other" and got["5"] == "same@host2"
    assert (got["1"], got["4"]) == ("same@host1·1", "same@host1·4")


@pytest.mark.parametrize(("alias", "shown", "typed", "expected"), [
    ("", "dev.shared", "work", ("set", "work")),
    ("old", "old", "  new  ", ("set", "new")),
    ("old", "old", "", ("unset", None)),          # empty: back to the short name
    ("", "dev.shared", "", None),                 # nothing to clear
    ("", "jordan.lee@uni", "jordan.lee@uni", None),  # unchanged: never sent (has an @)
    ("old", "old", "old", None),
    ("old", "old", None, None),                   # esc
])
def test_what_fleets_name_key_asks_for(alias, shown, typed, expected):
    from claude_swap.maximize import fleet as fx

    assert fx.name_request(alias, shown, typed) == expected


def test_clearing_an_alias_brings_the_short_name_back(temp_home, monkeypatch, capsys):
    import json
    import sys

    from claude_swap import cli, paths
    from claude_swap.maximize.hold import record_names

    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": 1, "sequence": [1, 2],
        "accounts": {"1": {"email": "dev.shared@example.com", "uuid": "u1"},
                     "2": {"email": "dev.master@example.com", "uuid": "u2"}},
    }))

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["cc-swap", "alias", *argv])
        try:
            cli.main()
        except SystemExit as e:
            assert e.code in (0, None)

    def shown():
        return record_names(json.loads((root / "sequence.json").read_text())["accounts"])

    run("1", "main")
    assert shown() == {"1": "main", "2": "dev.master"}
    run("1", "--unset")
    assert shown() == {"1": "dev.shared", "2": "dev.master"}


def test_short_name():
    assert names.short_name("dev.shared@example.com") == "dev.shared"
    assert names.short_name("") == ""
