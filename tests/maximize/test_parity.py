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

A route is the key path from the Fleet home screen. Since 0.4.0 the home
screen's keys are its footer (seven since 0.5.0 added ``h`` hold) and
everything else lives in the ``m`` menu: ``"l"`` is a home key; ``"m p"`` is
``m`` (the menu) then ``p``;
``"m a i"`` is the menu, ``a`` (Account settings), then ``i`` on that
screen; ``"m m"`` is the menu's Mode modal.
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
    "last-resort": "l",  # home key: toggle last resort on the selected account
    "prime": "m p",  # menu → Prime now…
    "history": "m v",  # menu → View switch history (the ledger, newest first)
    "claude-update": "m u",  # menu → Update Claude Code (check, confirm, run)
    "doctor": "m a i",  # menu → Account settings → Inspect all logins (doctor)
    "auto off": "m o",  # menu → o: automatic switching off (persistent)
    "auto on": "m o",  # menu → o: automatic switching back on
    # The home sentence says Auto OFF; menu → Mode's facts say by whom and since when.
    "auto status": "m m",
    "hold": "h",  # home key: hold the active account (1h/2h/4h/until/off)
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
        "Fleet's status sentence already says what the engine decided, live, and "
        "when it stopped reporting — a reset-wait, preempt or deferred rebalance in "
        "plain words — and ? shows the learned idle pattern; why is that same "
        "explanation for a shell"
    ),
    "notify": (
        "desktop notifications are for when you are not looking at Fleet, and the "
        "engine sends them; testing the system notifier and its settings is a shell job "
        "(cc-swap config set notify.* turns them off)"
    ),
    "prime verify": (
        "spawns claude in a throwaway profile (and with --live spends a real prime) "
        "to re-check isolation; Fleet's attention line says when it is needed and "
        "names the command"
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


def _menu_keys() -> set[str]:
    """Keys the ``m`` menu answers to (every item's letter, plus closing)."""
    from claude_swap.tui.fleet_modals import MenuModal

    rows = menus.menu_rows(auto_off=False, holder="service", mode_label="service · viewing")
    modal = MenuModal(rows)
    assert set(modal._by_key) == set(menus.MAIN_KEYS), "a menu item has no key"
    return set(modal._by_key) | _screen_keys(MenuModal)


def _sub_keys(path: tuple[str, ...]) -> set[str]:
    """Keys the screen or modal that the key path ``path`` opens answers to."""
    if path == ("m",):
        return _menu_keys()
    if path == ("m", "a"):
        from claude_swap.tui.fleet_accounts import AccountsScreen

        keys = _screen_keys(AccountsScreen)
        assert keys >= {k for k, _t, _a in menus.ACCOUNT_ITEMS}, "an Account item is unbound"
        return keys
    if path == ("m", "m"):
        from claude_swap.maximize import fleet as fx

        return {
            a.key
            for holder in ("none", "here-dry", "here-live", "service", "other")
            for off in (False, True)
            for a in fx.mode_transitions(holder, auto_off=off)
        }
    raise AssertionError(f"no screen behind `{' '.join(path)}` is known to this test")


@pytest.mark.parametrize("action,route", sorted(FLEET_ROUTES.items()))
def test_a_routed_fleet_key_path_is_bound_at_every_step(action, route):
    first, *rest = route.split()
    assert first in _fleet_keys(), f"Fleet binds no `{first}` for `cc-swap {action}`"
    assert first in menus.HOME_KEYS, (
        f"`{first}` (for `cc-swap {action}`) is not one of the home screen's keys"
    )
    path = (first,)
    for key in rest:
        assert key in _sub_keys(path), (
            f"`{route}` (for `cc-swap {action}`): `{key}` is not bound behind "
            f"`{' '.join(path)}`"
        )
        path += (key,)


def test_menu_routes_name_the_menu_item_that_does_it():
    assert menus.BY_ACTION["prime"].key == "p"
    assert menus.BY_ACTION["history"].key == "v"
    assert menus.BY_ACTION["update"].key == "u"
    assert menus.BY_ACTION["accounts"].key == "a"
    assert menus.BY_ACTION["auto"].key == "o"
    assert menus.BY_ACTION["mode"].key == "m"


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


def test_every_home_key_and_shortcut_is_bound():
    keys = (*menus.HOME_KEYS, *menus.ROW_KEYS, *menus.SHORTCUT_KEYS)
    assert [k for k in keys if k not in _fleet_keys()] == []
    # One stray key on the home screen never turns automatic switching off.
    assert "o" not in _fleet_keys()


def test_ctrl_f_is_bound_app_wide():
    assert any("ctrl+f" in b.key for b in CswapApp.BINDINGS)


# -- settings: `cc-swap config set` vs Fleet's Swap strategy (m → s) ---------------------------
#
# The same rule for settings: every ``maximize.*``/``prime.*`` key `config
# set` accepts is a Swap strategy field, or is listed here with the reason it
# is not. A key registered later fails until it is one or the other.

#: Fork settings the Swap strategy screen does not edit, and why.
STRATEGY_ASYMMETRY: dict[str, str] = {
    "maximize.pendingPollS": (
        "the poll interval while waiting for idle spends the per-account usage budget "
        "every machine shares; change it deliberately with cc-swap config set"
    ),
    "maximize.lastResort": (
        "a comma-separated list of emails or aliases; Fleet's l toggles last resort on "
        "the selected account and edits this same list"
    ),
    "maximize.planOverride": (
        "a per-email text map that only breaks ties; the plan normally comes from each "
        "account's stored credentials"
    ),
    "maximize.loginExpiryGuardMin": (
        "a landing guard near a login's fixed deadline; Fleet answers that deadline "
        "with login tags, the attention line and r to renew the login"
    ),
    "prime.claudePath": (
        "detected from your shell and saved by cc-swap service install; a path typed "
        "into a TUI field is easy to get wrong and launchd runs without your PATH"
    ),
}


def _fork_settings() -> set[str]:
    from claude_swap.settings import SETTING_SPECS

    return {k for k, s in SETTING_SPECS.items() if s.section in ("maximize", "prime")}


@pytest.mark.parametrize("key", sorted(_fork_settings()))
def test_every_fork_setting_is_in_swap_strategy_or_says_why_not(key):
    edited = {f.key for f in menus.STRATEGY_FIELDS}
    assert (key in edited) != (key in STRATEGY_ASYMMETRY), (
        f"{key}: add it to tui/menus.py STRATEGY_FIELDS (Swap strategy, m → s) or to "
        "STRATEGY_ASYMMETRY here with the reason Fleet does not edit it"
    )


def test_the_strategy_asymmetry_names_real_settings_with_real_reasons():
    assert set(STRATEGY_ASYMMETRY) <= _fork_settings()
    assert {f.key for f in menus.STRATEGY_FIELDS} <= _fork_settings()
    for key, reason in STRATEGY_ASYMMETRY.items():
        assert len(reason.split()) >= 8, f"say why Swap strategy does not edit {key}"


def test_every_hold_code_why_explains_fleet_words_too():
    """`cc-swap why` explains a hold's own code (doctor_cli.REASONS); Fleet's
    sentence must word the same codes, not fall back to a generic hold."""
    from claude_swap.maximize import doctor_cli
    from claude_swap.maximize import fleet as fx
    from claude_swap.maximize import home
    from claude_swap.maximize.view import HOLD_CODES
    from claude_swap.settings import MaximizeSettings

    assert HOLD_CODES <= set(doctor_cli.REASONS)
    row = fx.FleetRow(
        number="1", name="main", email="m@x", org="personal", active=True, rank=1,
        plan="20x", tier="normal", pct5=96.0, pct7=40.0, days7=3.0, score=1.0,
        landable=False, land="active", state5="running", reset5=None,
        prime=fx.PrimeCell("active", None, None, "—"), login="ok", stale=False,
    )
    es = fx.EngineStatus("service", 4121, {"running": True, "pid": 4121})

    def said(code):
        dv = fx.DecisionView("hold", "1", None, None, "#1 x", at=0.0, source="engine",
                             code=code)
        return home.status_variants(es, dv, [row], MaximizeSettings(), "live", now=0.0)

    generic = said(None)
    for code in sorted(HOLD_CODES):
        assert said(code) != generic, code


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
