"""Fleet → ``u`` (Update Claude Code): ``cc-swap claude-update`` in a modal.

Fake check/update runners only — no real ``claude``, no network."""

from __future__ import annotations

import pytest

from claude_swap.maximize import claude_update as cu
from claude_swap.tui import menus


def test_update_is_a_main_menu_entry_with_its_own_letter():
    entry = menus.BY_ACTION["update"]
    assert entry.key == "u" and entry.title == "Update Claude Code"
    assert "u" in menus.SHORTCUT_KEYS
    assert "u" not in menus.ROW_KEYS and "u" not in menus.RESERVED_KEYS
    assert "u" not in [k for k, _t, _a in menus.ACCOUNT_ITEMS]


class _Fakes:
    def __init__(self, check: cu.CheckResult, result: cu.UpdateResult | None = None):
        self.check = check
        self.result = result
        self.checks = 0
        self.updates = 0

    def run_check(self, _root):
        self.checks += 1
        return self.check

    def run_update(self, _root, sink):
        self.updates += 1
        sink.write("Checking for updates to latest version...\n")
        sink.write("Successfully updated from 2.1.280 to version 2.1.287\n")
        return self.result


def _available() -> cu.CheckResult:
    return cu.CheckResult(
        claude="/fake/claude", installed="2.1.280", latest="2.1.287", channel="latest"
    )


def _updated(prime: bool) -> cu.UpdateResult:
    return cu.UpdateResult(
        claude="/fake/claude", before="2.1.280", after="2.1.287", recorded=True,
        run=cu.UpdateRun(0, False, ""), prime_enabled=prime,
    )


async def _fleet_modal(tmp_path, monkeypatch, fakes):
    from claude_swap.tui import fleet_update
    from tests.maximize.test_tui_fleet import _settings
    from tests.maximize.test_tui_fleet_actions import _fleet
    from tests.test_tui import make_app

    monkeypatch.setattr(fleet_update, "run_check", fakes.run_check)
    monkeypatch.setattr(fleet_update, "run_update", fakes.run_update)
    _settings(tmp_path)
    return make_app(_fleet(tmp_path))


@pytest.mark.asyncio
async def test_u_checks_then_y_updates_and_shows_the_output_and_the_prime_hint(
    tmp_path, monkeypatch
):
    from claude_swap.tui.fleet import FleetScreen
    from claude_swap.tui.fleet_update import HINT_CONFIRM, HINT_DONE, ClaudeUpdateModal
    from tests.maximize.test_tui_fleet_actions import _open

    fakes = _Fakes(_available(), _updated(prime=True))
    app = await _fleet_modal(tmp_path, monkeypatch, fakes)
    async with app.run_test(size=(140, 40)) as pilot:
        await _open(pilot)
        await pilot.press("m", "u")
        await _open(pilot)
        modal = app.screen
        assert isinstance(modal, ClaudeUpdateModal)
        assert fakes.checks == 1 and fakes.updates == 0  # nothing runs unconfirmed
        assert "Update 2.1.280 -> 2.1.287?" in modal.log_text()
        assert modal.query_one("#fx-update-hint").render().plain == HINT_CONFIRM
        await pilot.press("y")
        await _open(pilot)
        await _open(pilot)
        assert fakes.updates == 1
        text = modal.log_text()
        assert "Successfully updated from 2.1.280 to version 2.1.287" in text
        assert "Claude Code updated: 2.1.280 -> 2.1.287" in text
        assert text.rstrip().endswith("Run: cc-swap prime verify")
        assert modal.query_one("#fx-update-hint").render().plain == HINT_DONE
        await pilot.press("y")  # a second run needs a fresh check
        await _open(pilot)
        assert fakes.updates == 1
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, FleetScreen)


@pytest.mark.asyncio
async def test_up_to_date_offers_no_update(tmp_path, monkeypatch):
    from claude_swap.tui.fleet_update import ClaudeUpdateModal
    from tests.maximize.test_tui_fleet_actions import _open

    fakes = _Fakes(cu.CheckResult(
        claude="/fake/claude", installed="2.1.287", latest="2.1.287", channel="stable"
    ))
    app = await _fleet_modal(tmp_path, monkeypatch, fakes)
    async with app.run_test(size=(140, 40)) as pilot:
        await _open(pilot)
        await pilot.press("m", "u")
        await _open(pilot)
        modal = app.screen
        assert isinstance(modal, ClaudeUpdateModal)
        assert "Claude Code 2.1.287 is up to date." in modal.log_text()
        await pilot.press("y")
        await _open(pilot)
        assert fakes.updates == 0
        await pilot.press("r")
        await _open(pilot)
        assert fakes.checks == 2


@pytest.mark.asyncio
async def test_a_failed_check_shows_the_error_and_offers_no_update(tmp_path, monkeypatch):
    from tests.maximize.test_tui_fleet_actions import _open

    fakes = _Fakes(cu.CheckResult(error=cu._NO_CLAUDE))
    app = await _fleet_modal(tmp_path, monkeypatch, fakes)
    async with app.run_test(size=(140, 40)) as pilot:
        await _open(pilot)
        await pilot.press("m", "u")
        await _open(pilot)
        assert "Error: claude was not found" in app.screen.log_text()
        await pilot.press("y")
        await _open(pilot)
        assert fakes.updates == 0


@pytest.mark.asyncio
async def test_a_failed_update_shows_the_error_and_no_hint(tmp_path, monkeypatch):
    from tests.maximize.test_tui_fleet_actions import _open

    failed = cu.UpdateResult(
        claude="/fake/claude", before="2.1.280", after="2.1.280",
        run=cu.UpdateRun(3, False, ""), error="`claude update` exited with status 3",
        prime_enabled=True,
    )
    fakes = _Fakes(_available(), failed)
    app = await _fleet_modal(tmp_path, monkeypatch, fakes)
    async with app.run_test(size=(140, 40)) as pilot:
        await _open(pilot)
        await pilot.press("m", "u")
        await _open(pilot)
        await pilot.press("y")
        await _open(pilot)
        await _open(pilot)
        text = app.screen.log_text()
        assert "Error: `claude update` exited with status 3" in text
        assert "prime verify" not in text


def test_the_log_sink_splits_lines_and_flushes_the_tail():
    from claude_swap.tui.fleet_update import _LogSink

    lines: list[str] = []
    sink = _LogSink(lines.append)
    sink.write("a\nb")
    sink.write("c\n")
    sink.write("tail")
    sink.flush()
    assert lines == ["a", "bc"]
    sink.close()
    assert lines == ["a", "bc", "tail"]
