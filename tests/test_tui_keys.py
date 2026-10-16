"""Key robustness of the whole TUI: a key (or two in one burst) that reaches a
screen which has already gone must do nothing, and no key sequence on any
screen may crash the app.

Pilot tests against fakes in temp backup roots (example.com emails only): no
real ``claude``, no network, no service manager. ``q`` (quit) is left out of
the sweeps — ending the app is its job.
"""

from __future__ import annotations

import random
import string

import pytest
from textual import events
from textual.app import ScreenStackError

from claude_swap.tui import menus
from tests.maximize.test_tui_fleet_actions import (
    StoringSwitcher,
    _accounts,
    _fake_claude_login,
    _launchable,
)
from tests.maximize.test_tui_fleet import _settings
from tests.test_tui import make_app, screen_ready, settle, wait_until

SPECIAL = [
    "enter", "escape", "tab", "shift+tab", "up", "down", "left", "right",
    "pageup", "pagedown", "home", "end", "space", "backspace", "delete",
    "question_mark", "ctrl+f", "ctrl+t",
]
# ``q`` quits the app by design; everything else is fair game.
LETTERS = [c for c in string.ascii_lowercase if c != "q"]
DIGITS = list(string.digits)
ALL_KEYS = SPECIAL + LETTERS + DIGITS + ["J", "K", "Y", "N"]
SIZES = [(80, 24), (120, 40)]


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    """The guided flows only: nothing may resolve or run a real ``claude``."""
    monkeypatch.setattr("claude_swap.tui.fleet._resolve_claude", lambda app: None)


class _World(StoringSwitcher):
    """Switching and removing only record: the same dialogs can be opened
    again after a key confirmed them."""

    def switch_to(self, identifier, json_output=False, force=False):
        self.calls.append(("switch_to", str(identifier)))
        return {"switched": False, "reason": "test world"}

    def remove_account(self, identifier, assume_yes=False):
        self.calls.append(("remove", str(identifier), assume_yes))

    def set_alias(self, number, alias):
        self.calls.append(("set_alias", str(number), alias))

    def unset_alias(self, number):
        self.calls.append(("unset_alias", str(number)))


def _world(root) -> StoringSwitcher:
    _settings(root)
    return _World(_accounts(), root)


def _snapshot(app) -> tuple:
    return tuple(id(sc) for sc in app.screen_stack), len(app.workers)


async def _stable(pilot, polls: int = 5) -> None:
    """Pump the app until its screens and workers stop changing for ``polls``
    pauses in a row (a callback queued behind a key lands within a few loop
    turns, and a loaded machine needs more of them than an idle one)."""
    seen = [None, 0]

    def unchanged() -> bool:
        now = _snapshot(pilot.app)
        seen[1] = seen[1] + 1 if now == seen[0] else 0
        seen[0] = now
        return seen[1] >= polls

    await wait_until(pilot, unchanged, "the screens to stop changing")


async def _open(pilot) -> None:
    await settle(pilot)
    await _stable(pilot)


def _cls(app) -> str:
    return type(app.screen).__name__


def _depth(app) -> int:
    return len(app.screen_stack)


def _alive(app) -> None:
    assert app.is_running, "the app stopped"
    assert app._exception is None, f"the app crashed: {app._exception!r}"


async def _send(pilot, key: str) -> None:
    """One key the way the driver delivers it."""
    pilot.app.post_message(events.Key(key, key if len(key) == 1 else None))
    await pilot.pause()


async def _select_row(pilot, number: str) -> None:
    pilot.app.screen.select(number)
    await pilot.pause()


# -- every screen and modal reachable from Fleet home: how to get there ---------------------------


async def _keys(pilot, *keys: str) -> None:
    """Navigation: one key at a time, each one fully handled (its screen
    pushed or popped, its callback run) before the next goes in."""
    for key in keys:
        await pilot.press(key)
        await _open(pilot)


async def _home(pilot):
    return None


async def _menu(pilot):
    await _keys(pilot, "m")


async def _menu_mode(pilot):
    await _keys(pilot, "m", "m")


async def _menu_auto_confirm(pilot):
    # Turning auto off asks; turning it on does not. The home screen's
    # auto_off can lag a just-confirmed toggle, so do not trust one read:
    # toggle until the confirm shows (at most twice: on, then off).
    for _ in range(2):
        await _keys(pilot, "m", "o")
        try:
            await wait_until(pilot, lambda: _cls(pilot.app) == "ConfirmModal", "confirm", timeout=1.5)
            return
        except AssertionError:
            await _go_home(pilot)


async def _hold(pilot):
    await _keys(pilot, "h")


async def _strategy(pilot):
    await _keys(pilot, "s")


async def _strategy_input(pilot):
    await _keys(pilot, "s", "e")


async def _accounts_screen(pilot):
    await _keys(pilot, "a")


async def _accounts_alias(pilot):
    await _keys(pilot, "a", "n")


async def _accounts_doctor(pilot):
    await _keys(pilot, "a", "i")


async def _accounts_delete_confirm(pilot):
    await _keys(pilot, "a", "d")


async def _help(pilot):
    await _keys(pilot, "question_mark")


async def _history(pilot):
    await _keys(pilot, "v")


async def _prime(pilot):
    await _keys(pilot, "p")


async def _rename(pilot):
    await _keys(pilot, "n")


async def _guided_relogin(pilot):
    await _select_row(pilot, "4")
    await _keys(pilot, "r")


async def _switch_confirm(pilot):
    await _select_row(pilot, "4")
    await _keys(pilot, "enter")


async def _engine(pilot):
    await _keys(pilot, "e")


async def _watch(pilot):
    await _keys(pilot, "w")


async def _classic(pilot):
    await _keys(pilot, "c")


#: name -> (how to get there, class name of the screen that must be on top).
SCREENS = {
    "home": (_home, "FleetScreen"),
    "menu": (_menu, "MenuModal"),
    "menu-mode": (_menu_mode, "ModeModal"),
    "menu-auto-confirm": (_menu_auto_confirm, "ConfirmModal"),
    "hold-picker": (_hold, "MenuModal"),
    "strategy": (_strategy, "StrategyScreen"),
    "strategy-input": (_strategy_input, "TextInputModal"),
    "accounts": (_accounts_screen, "AccountsScreen"),
    "accounts-alias": (_accounts_alias, "TextInputModal"),
    "accounts-doctor": (_accounts_doctor, "DoctorModal"),
    "accounts-delete-confirm": (_accounts_delete_confirm, "ConfirmModal"),
    "help": (_help, "HelpScreen"),
    "history": (_history, "OutputModal"),
    "prime": (_prime, "PrimeModal"),
    "rename": (_rename, "TextInputModal"),
    "guided-relogin": (_guided_relogin, "ReloginModal"),
    "switch-confirm": (_switch_confirm, "ConfirmModal"),
    "engine": (_engine, "AutoScreen"),
    "watch": (_watch, "WatchScreen"),
    "classic": (_classic, "DashboardScreen"),
}


async def _go_home(pilot) -> None:
    """Pop everything above Fleet (and re-open Fleet if a key left it)."""
    app = pilot.app
    for _ in range(12):
        if _cls(app) == "FleetScreen":
            break
        if _cls(app) == "DashboardScreen" and app.is_screen_installed("fleet"):
            app.action_open_fleet()
        else:
            app.pop_screen()
        await pilot.pause()
    assert _cls(app) == "FleetScreen"


async def _reach(pilot, name: str) -> None:
    how, expect = SCREENS[name]
    await _go_home(pilot)
    await how(pilot)
    await wait_until(
        pilot, lambda: _cls(pilot.app) == expect and screen_ready(pilot.app),
        f"{name}: {expect} on top and ready (on {_cls(pilot.app)})",
    )


# -- regression: two action keys in one burst -------------------------------------------------------


def _count_menu_results(monkeypatch) -> list:
    """Every result the home screen's menu callback receives."""
    from claude_swap.tui.fleet import FleetScreen

    seen: list = []
    real = FleetScreen._on_menu

    def counted(self, action):
        seen.append(action)
        return real(self, action)

    monkeypatch.setattr(FleetScreen, "_on_menu", counted)
    return seen


def _post_twice(app, key: str, char: str | None) -> None:
    """Two key events queued on the app (as its driver does) before it runs
    the first."""
    for _ in range(2):
        app.post_message(events.Key(key, char))


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [e for e in menus.MAIN_MENU if e.key != "q"],
                         ids=lambda e: e.action)
@pytest.mark.parametrize("mode", ["posted", "pressed"])
async def test_menu_two_keys_one_action(tmp_path, monkeypatch, entry, mode):
    seen = _count_menu_results(monkeypatch)
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        await _keys(pilot, "m")
        menu_depth = _depth(app)
        if mode == "posted":
            _post_twice(app, entry.key, entry.key)
        else:
            await pilot.press(entry.key, entry.key)
        await wait_until(pilot, lambda: seen, "the menu's action")
        await _open(pilot)
        _alive(app)
        assert seen == [entry.action]  # one key's action, once
        assert _depth(app) <= menu_depth + 1  # never two screens, never a pop too many
        assert _cls(app) != "MenuModal"
        assert app.screen_stack[0].id == "_default" and len(app.screen_stack) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["posted", "pressed"])
async def test_menu_history_burst_opens_one_modal(tmp_path, mode):
    """The reported crash: ``m`` then ``v`` ``v`` in one burst."""
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        base = _depth(app)
        await _keys(pilot, "m")
        if mode == "posted":
            _post_twice(app, "v", "v")
        else:
            await pilot.press("v", "v")
        await _open(pilot)
        _alive(app)
        assert _cls(app) == "OutputModal" and _depth(app) == base + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["posted", "pressed"])
async def test_hold_picker_two_keys_one_choice(tmp_path, monkeypatch, mode):
    from claude_swap.tui.fleet import FleetScreen

    chosen: list = []
    real = FleetScreen._on_hold_choice
    monkeypatch.setattr(
        FleetScreen, "_on_hold_choice",
        lambda self, number, action: (chosen.append(action), real(self, number, action))[1],
    )
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        base = _depth(app)
        await _keys(pilot, "h")
        key = app.screen._rows[0].key
        if mode == "posted":
            _post_twice(app, key, key)
        else:
            await pilot.press(key, key)
        await wait_until(pilot, lambda: chosen, "the hold choice")
        await _open(pilot)
        _alive(app)
        assert len(chosen) == 1
        assert _depth(app) <= base + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["posted", "pressed"])
async def test_mode_modal_two_keys_one_choice(tmp_path, monkeypatch, mode):
    from claude_swap.tui.fleet import FleetScreen

    chosen: list = []
    monkeypatch.setattr(FleetScreen, "_on_mode", lambda self, action: chosen.append(action))
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        base = _depth(app)
        await _keys(pilot, "m", "m")
        await wait_until(pilot, lambda: _cls(app) == "ModeModal" and screen_ready(app), "the Mode modal")
        modal = app.screen
        actions = modal._actions
        if not actions:  # a viewer lane: facts only, nothing to press
            await pilot.press("escape", "escape")
            await wait_until(pilot, lambda: chosen, "the Mode close")
            await _open(pilot)
            _alive(app)
            assert _depth(app) == base and chosen == [None]
            return
        key = actions[0].key
        if mode == "posted":
            _post_twice(app, key, key)
        else:
            await pilot.press(key, key)
        await wait_until(pilot, lambda: chosen, "the Mode choice")
        await _open(pilot)
        _alive(app)
        assert chosen == [actions[0].action] and _depth(app) == base


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["posted", "pressed"])
@pytest.mark.parametrize("kind", ["confirm-yes", "confirm-esc", "text-enter",
                                  "text-esc", "help", "output", "doctor", "prime"])
async def test_modal_two_dismissals_one_result(tmp_path, kind, mode):
    from claude_swap.tui.fleet_doctor import DoctorModal
    from claude_swap.tui.fleet_help import HelpScreen
    from claude_swap.tui.fleet_modals import PrimeChoice, PrimeModal, TextInputModal
    from claude_swap.tui.modals import ConfirmModal, OutputModal

    app = make_app(_world(tmp_path))
    results: list = []
    key = {"confirm-yes": "y", "confirm-esc": "escape",
           "text-enter": "enter", "text-esc": "escape", "help": "b", "output": "escape",
           "doctor": "escape", "prime": "escape"}[kind]
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        base = _depth(app)
        modal = {
            "confirm-yes": lambda: ConfirmModal("sure?"),
            "confirm-esc": lambda: ConfirmModal("sure?"),
            "text-enter": lambda: TextInputModal("t", "p", "x"),
            "text-esc": lambda: TextInputModal("t", "p", "x"),
            "help": lambda: HelpScreen(),
            "output": lambda: OutputModal("o", "text"),
            "doctor": lambda: DoctorModal(),
            "prime": lambda: PrimeModal([PrimeChoice("2", "two", True)], set(), lambda n: []),
        }[kind]()
        app.push_screen(modal, results.append)
        await wait_until(pilot, lambda: _depth(app) == base + 1 and screen_ready(app), f"the {kind} modal to open")
        await _open(pilot)
        assert _depth(app) == base + 1
        char = key if len(key) == 1 else None
        if mode == "posted":
            _post_twice(app, key, char)
        else:
            await pilot.press(key, key)
        await wait_until(pilot, lambda: _depth(app) == base, f"the {kind} modal to close")
        await _open(pilot)
        _alive(app)
        assert _depth(app) == base and _cls(app) == "FleetScreen"
        if kind.startswith(("confirm", "text", "output")):
            assert len(results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["posted", "pressed"])
@pytest.mark.parametrize("name", ["strategy", "accounts"])
async def test_screen_back_twice_pops_once(tmp_path, name, mode):
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        base = _depth(app)
        await _reach(pilot, name)
        await wait_until(pilot, lambda: _depth(app) == base + 1, f"{name} to open")
        assert _depth(app) == base + 1
        if mode == "posted":
            _post_twice(app, "b", "b")
        else:
            await pilot.press("b", "b")
        await wait_until(pilot, lambda: _depth(app) == base, f"{name} to go back")
        await _open(pilot)
        _alive(app)
        assert _cls(app) == "FleetScreen" and _depth(app) == base


@pytest.mark.asyncio
async def test_dismiss_and_pop_of_a_gone_screen_are_noops(tmp_path):
    from claude_swap.tui.fleet_modals import MenuModal

    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        base = _depth(app)
        seen: list = []
        modal = MenuModal(app.screen._menu_rows())
        app.push_screen(modal, seen.append)
        await _open(pilot)
        modal.dismiss("history")
        await _open(pilot)
        for _ in range(3):
            modal.dismiss("history")
            modal.dismiss(None)
        await _open(pilot)
        await wait_until(pilot, lambda: seen, "the menu's result")
        assert seen == ["history"] and _depth(app) == base and _cls(app) == "FleetScreen"
        app.push_screen(modal := MenuModal([]))
        await _open(pilot)
        modal.pop_self()  # the active screen may pop itself
        modal.pop_self()  # ...but only once
        await _open(pilot)
        assert _depth(app) == base
        _alive(app)


@pytest.mark.asyncio
async def test_first_dismiss_is_never_dropped_when_the_stack_is_unreadable(tmp_path, monkeypatch):
    """The guard refuses a *second* dismiss, not one that arrives while the
    stack cannot be inspected (Textual mid-transition, app tearing down): a
    real key must not turn into a no-op. Fails on a guard that treats "can't
    tell if I'm the active screen" as "not active"."""
    from textual.app import App

    from claude_swap.tui.fleet_modals import ModeModal

    app = make_app(_world(tmp_path))
    seen: list = []
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        base = _depth(app)
        modal = ModeModal(app.screen._engine_status())
        app.push_screen(modal, seen.append)
        await wait_until(pilot, lambda: _depth(app) == base + 1, "the Mode modal")
        await _open(pilot)
        real = App.screen_stack

        def unreadable(self):
            raise RuntimeError("mid-transition")

        monkeypatch.setattr(App, "screen_stack", property(unreadable))
        modal.dismiss("stop")  # the first one goes through
        modal.dismiss("stop")  # a second one is refused, stack unreadable or not
        modal.dismiss(None)
        monkeypatch.setattr(App, "screen_stack", real)
        await wait_until(pilot, lambda: seen, "the Mode result")
        await _open(pilot)
        assert seen == ["stop"] and _depth(app) == base
        _alive(app)


@pytest.mark.asyncio
async def test_a_reused_screen_can_be_dismissed_again_after_it_is_pushed_again(tmp_path):
    """Fleet is an installed screen pushed again and again: the "already
    dismissed" mark must not outlive the pop."""
    from claude_swap.tui.fleet_help import HelpScreen

    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        base = _depth(app)
        help_screen = HelpScreen()
        app.install_screen(help_screen, "help-reused")
        for _ in range(3):
            app.push_screen("help-reused")
            await wait_until(pilot, lambda: _depth(app) == base + 1, "help to open")
            help_screen.dismiss(None)
            help_screen.dismiss(None)
            await wait_until(pilot, lambda: _depth(app) == base, "help to close")
        await _open(pilot)
        _alive(app)


@pytest.mark.asyncio
async def test_app_pop_of_the_last_screen_is_logged_not_fatal(tmp_path, caplog):
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        # Reach the one-screen state the crash report had: drop every screen
        # but the base one, then pop again.
        while len(app.screen_stack) > 1:
            app._screen_stack.pop()
        with caplog.at_level("WARNING", logger="claude_swap.tui.app"):
            app.pop_screen()
        assert any("ignored pop_screen" in r.message for r in caplog.records)
        assert app._exception is None


@pytest.mark.asyncio
async def test_other_errors_are_not_swallowed(tmp_path, monkeypatch):
    from textual.app import App

    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)

        def boom(self):
            raise RuntimeError("not a stack error")

        monkeypatch.setattr(App, "pop_screen", boom)
        with pytest.raises(RuntimeError):
            app.pop_screen()
        assert ScreenStackError is not RuntimeError


# -- the sweep: every key on every screen -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(SCREENS))
async def test_key_sweep(tmp_path, name):
    """Every key once on the screen (re-opened whenever a key moved off it);
    the terminal shrinks to 80x24 halfway."""
    _, expect = SCREENS[name]
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        await _reach(pilot, name)
        for i, key in enumerate(ALL_KEYS):
            if i == len(ALL_KEYS) // 2:
                await pilot.resize_terminal(80, 24)
            if _cls(app) != expect:
                await _reach(pilot, name)
            await _send(pilot, key)
            try:
                _alive(app)
            except AssertionError as e:
                raise AssertionError(f"screen {name!r} key {key!r}: {e}") from e
        await _open(pilot)
        _alive(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["menu-auto-confirm", "accounts-delete-confirm",
                                  "guided-relogin", "strategy-input"])
async def test_key_pairs_on_dialogs(tmp_path, name):
    """Every key twice in one burst on the dialogs that act on a key."""
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        _, expect = SCREENS[name]
        await _reach(pilot, name)
        for key in SPECIAL + LETTERS:
            if _cls(app) != expect:
                await _reach(pilot, name)
            char = key if len(key) == 1 else None
            _post_twice(app, key, char)
            await _open(pilot)
            try:
                _alive(app)
            except AssertionError as e:
                raise AssertionError(f"screen {name!r} key {key!r} x2: {e}") from e
        await _open(pilot)
        _alive(app)


# -- seeded random sequences ------------------------------------------------------------------------

SEEDS = range(30)
SEQ_LEN = 60


def _sequence(seed: int) -> list[str]:
    rng = random.Random(seed)
    return [rng.choice(ALL_KEYS) for _ in range(SEQ_LEN)]


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", SEEDS)
async def test_random_keys_from_home(tmp_path, seed):
    """60 keys from home: the first 30 one at a time, then the terminal
    changes size (80x24 and 120x40 alternate with the seed), then the rest in
    bursts of six with no awaits between the keys."""
    seq = _sequence(seed)
    size, other = (SIZES[0], SIZES[1]) if seed % 2 == 0 else (SIZES[1], SIZES[0])
    app = make_app(_world(tmp_path))
    async with app.run_test(size=size) as pilot:
        await _open(pilot)
        try:
            for key in seq[:SEQ_LEN // 2]:
                await _send(pilot, key)
                _alive(app)
            await pilot.resize_terminal(*other)
            for i in range(SEQ_LEN // 2, SEQ_LEN, 6):
                for key in seq[i:i + 6]:
                    app.post_message(events.Key(key, key if len(key) == 1 else None))
                await pilot.pause()
                _alive(app)
            await _open(pilot)
            _alive(app)
        except BaseException as e:
            raise AssertionError(
                f"seed={seed} size={size}->{other}\nkeys={seq}\n{type(e).__name__}: {e}"
            ) from e


# -- the in-app login: keys typed while claude owned the terminal -----------------------------------


@pytest.mark.asyncio
async def test_keys_buffered_during_login_do_not_crash(tmp_path, monkeypatch):
    """The TUI is suspended for ``claude auth login``; keys typed meanwhile
    reach the app in a burst once it resumes. stdin is flushed on resume and
    whatever still arrives must do nothing harmful."""
    from claude_swap.tui import fleet as tui_fleet

    flushed: list[bool] = []
    monkeypatch.setattr(tui_fleet, "_flush_stdin", lambda: flushed.append(True))
    app = make_app(_world(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot)
        suspended = _launchable(
            monkeypatch, app, _fake_claude_login("user4@example.com")
        )
        monkeypatch.setattr(
            "claude_swap.tui.fleet._resolve_claude", lambda app: "/opt/fake/claude"
        )
        await _select_row(pilot, "4")
        await pilot.press("r")
        for _ in range(3):
            # the "buffered" keys: what a user typed into claude's prompt
            for key in "vvmmhh":
                app.post_message(events.Key(key, key))
            await _open(pilot)
            await app.workers.wait_for_complete()
        await _open(pilot)
        _alive(app)
        assert suspended == [True] and flushed == [True]
        await _go_home(pilot)


@pytest.mark.asyncio
async def test_flush_stdin_survives_without_a_tty(monkeypatch):
    from claude_swap.tui import fleet as tui_fleet

    monkeypatch.setattr("sys.stdin", None)
    tui_fleet._flush_stdin()  # must not raise
