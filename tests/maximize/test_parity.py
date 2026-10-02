"""Surface parity: every fork CLI action is reachable from Fleet, or says why not.

cc-swap has three surfaces (CLI, Fleet, menu bar). The recurring bug class in
the sibling codex-swap was "a guard on one surface only", and its cure was a
test that forces every CLI action to be either routed to the TUI or listed in
``KNOWN_ASYMMETRY`` with a reason. This is that test for the fork's commands.

The set it covers is read from ``cli._FORK_COMMANDS`` at run time, so a command
registered there is enforced automatically: it fails here until it has a
``FLEET_ROUTES`` entry (a key Fleet binds) or a ``KNOWN_ASYMMETRY`` entry.

Commands that extend another one (``auto on|off|status`` is handled before
the ``auto`` parser; ``prime verify`` is a ``prime`` subcommand; ``upgrade
--check`` extends upstream's ``upgrade``) are not in ``_FORK_COMMANDS`` and
are listed in ``EXTRA_FORK_ACTIONS`` so they are held to the same rule.

A route is the key path from the Fleet home screen: ``"u"`` is a main-menu
or row key; ``"a i"`` is ``a`` (Account settings) then ``i`` on that
screen; ``"m o"`` is ``m`` (Mode) then the Mode modal's ``o``.
"""

from __future__ import annotations

import sys

import pytest

from claude_swap import cli
from claude_swap.tui import menus
from claude_swap.tui.app import CswapApp
from claude_swap.tui.fleet import FleetScreen

#: Fork actions that are not in ``_FORK_COMMANDS`` because they extend
#: another command: ``upgrade`` gained ``--check`` and the service refresh;
#: ``auto on|off|status`` is caught before the ``auto`` parser; ``prime
#: verify`` is a ``prime`` subcommand.
EXTRA_FORK_ACTIONS: tuple[str, ...] = (
    "upgrade", "auto on", "auto off", "auto status", "prime verify",
)

#: CLI action -> the Fleet key path that performs the same thing.
FLEET_ROUTES: dict[str, str] = {
    "last-resort": "l",  # row key: toggle last resort on the highlighted account
    "prime": "p",  # menu: Prime now…
    "history": "v",  # menu: View switch history (the ledger, newest first)
    "claude-update": "u",  # menu: Update Claude Code (check, confirm, run)
    "doctor": "a i",  # Account settings → Inspect all logins (doctor)
    "auto off": "m o",  # Mode → o: automatic switching off (persistent)
    "auto on": "m o",  # Mode → o: automatic switching back on
    "auto status": "m",  # Mode: its facts say AUTO OFF, by whom and since when
}

#: CLI action -> why Fleet deliberately has no twin.
KNOWN_ASYMMETRY: dict[str, str] = {
    "service": (
        "Fleet's Mode item shows the service state read-only (viewer by default); "
        "installing or removing the service from inside the TUI it would then "
        "be viewed from is a shell job"
    ),
    "upgrade": (
        "reinstalls cc-swap itself under the running TUI and restarts the service; "
        "run it from a shell (Fleet's u updates Claude Code, not cc-swap)"
    ),
    "init": (
        "a one-time onboarding checklist for a machine that is not set up yet; "
        "--apply sets the strategy and installs the service, both shell jobs "
        "before Fleet is the home screen"
    ),
    "why": (
        "Fleet's now line already shows the engine's last decision and its reason "
        "live; why is that same explanation for a shell"
    ),
    "prime verify": (
        "spawns claude in a throwaway profile (and with --live spends a real prime) "
        "to re-check isolation; Fleet's prime line says when it is needed and names "
        "the command"
    ),
}


def _actions() -> tuple[str, ...]:
    return (*cli._FORK_COMMANDS, *EXTRA_FORK_ACTIONS)


def _fleet_keys() -> set[str]:
    keys: set[str] = set()
    for binding in FleetScreen.BINDINGS:
        for key in binding.key.split(","):
            keys.add("?" if key == "question_mark" else key)
    return keys


@pytest.mark.parametrize("action", _actions())
def test_every_fork_action_has_a_fleet_route_or_a_known_asymmetry(action):
    routed, excused = action in FLEET_ROUTES, action in KNOWN_ASYMMETRY
    assert routed or excused, (
        f"`cc-swap {action}` has no Fleet twin. Add it to FLEET_ROUTES in "
        "tests/maximize/test_parity.py (with the Fleet key that does the same "
        "thing) or to KNOWN_ASYMMETRY (with the reason there is none)."
    )
    assert not (routed and excused), (
        f"`cc-swap {action}` is in both FLEET_ROUTES and KNOWN_ASYMMETRY; "
        "drop the asymmetry now that Fleet routes it"
    )


def test_the_tables_name_no_action_that_does_not_exist():
    actions = set(_actions())
    stale = (set(FLEET_ROUTES) | set(KNOWN_ASYMMETRY)) - actions
    assert not stale, f"entries for commands that are not registered: {sorted(stale)}"


def _screen_keys(screen_cls) -> set[str]:
    return {k for b in screen_cls.BINDINGS for k in b.key.split(",")}


def _sub_keys(first: str) -> set[str]:
    """Keys the sub-screen that ``first`` opens answers to."""
    if first == "a":
        from claude_swap.tui.fleet_accounts import AccountsScreen

        keys = _screen_keys(AccountsScreen)
        assert keys >= {k for k, _t, _a in menus.ACCOUNT_ITEMS}, "an Account item is unbound"
        return keys
    if first == "m":
        from claude_swap.maximize import fleet as fx

        return {
            a.key
            for holder in ("none", "here-dry", "here-live", "service", "other")
            for off in (False, True)
            for a in fx.mode_transitions(holder, auto_off=off)
        }
    raise AssertionError(f"no sub-screen behind `{first}` is known to this test")


@pytest.mark.parametrize("action,route", sorted(FLEET_ROUTES.items()))
def test_a_routed_fleet_key_is_bound_and_in_the_menu_or_row_keys(action, route):
    first, *rest = route.split()
    assert first in _fleet_keys(), f"Fleet binds no `{first}` for `cc-swap {action}`"
    assert first in menus.MAIN_KEYS + menus.ROW_KEYS, (
        f"`{first}` (for `cc-swap {action}`) is neither a Fleet menu key nor a row key"
    )
    for key in rest:
        assert key in _sub_keys(first), (
            f"`{route}` (for `cc-swap {action}`): `{key}` is not bound behind `{first}`"
        )


@pytest.mark.parametrize("action", [a for a in EXTRA_FORK_ACTIONS if " " in a])
def test_every_extra_subcommand_exists(action, capsys):
    verb, sub = action.split()
    handler = {"auto": cli._auto_command, "prime": cli._prime_command}[verb]
    with pytest.raises(SystemExit) as excinfo:
        handler([sub, "--help"])
    assert excinfo.value.code == 0
    assert f"cc-swap {verb}" in capsys.readouterr().out


@pytest.mark.parametrize("action,reason", sorted(KNOWN_ASYMMETRY.items()))
def test_an_asymmetry_gives_a_real_reason(action, reason):
    assert len(reason.split()) >= 8, f"say why `cc-swap {action}` has no Fleet twin"


def test_every_fleet_menu_and_row_key_is_bound():
    unbound = [k for k in (*menus.MAIN_KEYS, *menus.ROW_KEYS) if k not in _fleet_keys()]
    # `enter` is the DataTable's own selection, not a screen binding.
    assert [k for k in unbound if k != "enter"] == []


def test_ctrl_f_is_bound_app_wide():
    assert any("ctrl+f" in b.key for b in CswapApp.BINDINGS)


@pytest.mark.parametrize("command", sorted(cli._FORK_COMMANDS))
def test_every_registered_fork_command_resolves_and_is_routed_by_main(command, monkeypatch):
    handler_name = cli._FORK_COMMANDS[command]
    assert callable(getattr(cli, handler_name, None)), f"cli.{handler_name} is missing"
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, handler_name, lambda argv: seen.append(argv))
    monkeypatch.setattr(sys, "argv", ["cc-swap", command, "--sentinel"])

    cli.main()

    assert seen == [["--sentinel"]]


def _main_help(monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["cc-swap", "help"])
    with pytest.raises(SystemExit):
        cli.main()
    return capsys.readouterr().out


@pytest.mark.parametrize("action", sorted({*_actions(), "upgrade --check"}))
def test_every_fork_action_has_a_one_line_help_listing(action, monkeypatch, capsys):
    lines = [line.split() for line in _main_help(monkeypatch, capsys).splitlines()]
    verb, *sub = action.split()

    def listed(line: list[str]) -> bool:
        if line[1:2] != [verb]:
            return False
        # `auto off|on|status` lists three subcommands on one line.
        return not sub or (len(line) > 2 and sub[0] in line[2].split("|"))

    assert any(listed(line) for line in lines), (
        f"`cc-swap help` has no one-line listing for `cc-swap {action}`"
    )
