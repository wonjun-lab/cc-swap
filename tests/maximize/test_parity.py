"""Surface parity: every fork CLI action is reachable from Fleet, or says why not.

cc-swap has three surfaces (CLI, Fleet, menu bar). The recurring bug class in
the sibling codex-swap was "a guard on one surface only", and its cure was a
test that forces every CLI action to be either routed to the TUI or listed in
``KNOWN_ASYMMETRY`` with a reason. This is that test for the fork's commands.

The set it covers is read from ``cli._FORK_COMMANDS`` at run time, so a command
registered there is enforced automatically: it fails here until it has a
``FLEET_ROUTES`` entry (a key Fleet binds) or a ``KNOWN_ASYMMETRY`` entry.

PENDING (not on main when this test was written; each lands with its own
batch and must be given a verdict here when it is merged):

* ``doctor``          - Fleet "Verify logins" modal
* ``init``            - likely an asymmetry (one-time onboarding checklist)
* ``why``             - Fleet's "now" line already shows the last decision
* ``history``         - Fleet engine-log tab
* ``auto on|off``     - Fleet ``m`` (Mode); handled before the auto parser, so
                        it is not in ``_FORK_COMMANDS`` and not enumerated here
* ``prime verify``    - a subcommand of ``prime`` (already routed via ``p``)
"""

from __future__ import annotations

import sys

import pytest

from claude_swap import cli
from claude_swap.tui import menus
from claude_swap.tui.app import CswapApp
from claude_swap.tui.fleet import FleetScreen

#: Fork actions that are not in ``_FORK_COMMANDS`` because they extend an
#: upstream command (``upgrade`` gained ``--check`` and the service refresh).
EXTRA_FORK_ACTIONS: tuple[str, ...] = ("upgrade",)

#: CLI action -> the Fleet key that performs the same thing. The key must be
#: bound on the Fleet screen and be a menu key or a row key.
FLEET_ROUTES: dict[str, str] = {
    "last-resort": "l",  # row key: toggle last resort on the highlighted account
    "prime": "p",  # menu: Prime now…
}

#: CLI action -> why Fleet deliberately has no twin.
KNOWN_ASYMMETRY: dict[str, str] = {
    "service": (
        "Fleet's Mode item shows the service state read-only (viewer by default); "
        "installing or removing the service from inside the TUI it would then "
        "be viewed from is a shell job"
    ),
    "upgrade": (
        "reinstalls the tool under the running TUI; run it from a shell. A Fleet "
        "'u' Update entry is a planned follow-up and will move this to FLEET_ROUTES"
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


@pytest.mark.parametrize("action,key", sorted(FLEET_ROUTES.items()))
def test_a_routed_fleet_key_is_bound_and_in_the_menu_or_row_keys(action, key):
    assert key in _fleet_keys(), f"Fleet binds no `{key}` for `cc-swap {action}`"
    assert key in menus.MAIN_KEYS + menus.ROW_KEYS, (
        f"`{key}` (for `cc-swap {action}`) is neither a Fleet menu key nor a row key"
    )


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
