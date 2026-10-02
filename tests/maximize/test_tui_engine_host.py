"""EngineHost and Fleet's Mode: Fleet never takes the lease on its own; an
engine run here is the app's only one, and the auto screen attaches to it.
Uses the TUI tests' fake engine (patched in tui/autoview, which the host
builds its engine through)."""

from __future__ import annotations

import asyncio
import time

import pytest
from textual.widgets import RichLog, Static

from claude_swap.maximize import fleet as fx
from claude_swap.maximize.lease import EngineLease
from tests.maximize.test_tui_fleet import _fleet, _settings
from tests.test_tui import fake_engine, make_app, settle  # noqa: F401 (fixture)


async def _eventually(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _open(pilot) -> None:
    await settle(pilot)
    await settle(pilot)


def _engine_line(app) -> str:
    """Fleet's status line: the sentence and, on the right, who runs the engine."""
    return app.screen.query_one("#fx-status", Static).render().plain


async def _mode(pilot, *keys: str) -> None:
    """Menu (m) → Mode (m), then ``keys`` in the Mode modal."""
    from claude_swap.tui.fleet_modals import ModeModal

    await pilot.press("m", "m")
    await pilot.pause()
    assert isinstance(pilot.app.screen, ModeModal)
    for key in keys:
        await pilot.press(key)
        await pilot.pause()
    await settle(pilot)


@pytest.mark.parametrize(("holder", "actions"), [
    ("none", ["start-dry", "start-live"]),
    ("here-dry", ["go-live", "stop"]),
    ("here-live", ["go-dry", "stop"]),
    ("service", []),
    ("other", []),
])
def test_mode_transitions(holder, actions):
    assert [a.action for a in fx.mode_transitions(holder)] == actions
    keys = [a.key for a in fx.mode_transitions(holder)]
    assert len(set(keys)) == len(keys)


def test_mode_facts_name_the_service_and_how_to_stop_it():
    es = fx.engine_status(held_elsewhere=True, holder_pid=4121, own=None,
                          service={"platform": "darwin", "running": True, "pid": 4121,
                                   "logs": ["~/Library/Logs/cc-swap/auto.log"]})
    text = "\n".join(fx.mode_facts(es))
    assert "service (pid 4121) owns switching" in text
    assert "cc-swap service uninstall" in text and "auto.log" in text


@pytest.mark.asyncio
class TestEngineHost:
    async def test_fleet_never_claims_the_lease_without_mode(self, tmp_path, fake_engine):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        probe = EngineLease(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            assert fake_engine.instances == []
            assert not app.engine_keeper.lease.held
            assert probe.held_elsewhere() is False
            assert not app.engine_host.running

    async def test_mode_run_here_dry_run_then_live_with_confirm(self, tmp_path, fake_engine):
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.modals import ConfirmModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _mode(pilot, "d")
            assert isinstance(app.screen, FleetScreen)
            assert len(fake_engine.instances) == 1
            assert fake_engine.instances[0].dry_run is True
            assert _engine_line(app).startswith("Dry run · ")
            assert _engine_line(app).endswith("engine here · dry run")
            assert app._store_only is True  # the engine here fetches
            await _mode(pilot, "l")
            assert isinstance(app.screen, ConfirmModal)  # going live asks
            assert len(fake_engine.instances) == 1
            await pilot.press("y")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped is True
            assert fake_engine.instances[1].dry_run is False
            assert _engine_line(app).startswith("Auto ON · ")
            assert _engine_line(app).endswith("engine runs here · quitting stops it")
            await _mode(pilot, "d")
            assert len(fake_engine.instances) == 3 and fake_engine.instances[2].dry_run

    async def test_auto_screen_attaches_to_running_host_engine_no_second_engine(
        self, tmp_path, fake_engine
    ):
        from claude_swap.tui.autoview import AutoScreen
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _mode(pilot, "d")
            await pilot.press("e")
            await settle(pilot)
            assert isinstance(app.screen, AutoScreen)
            assert len(fake_engine.instances) == 1
            log = app.screen.query_one("#event-log", RichLog)
            texts = [line.text for line in log.lines]
            assert any("cooldown" in t for t in texts)  # replayed from the ring buffer
            assert any("attached to the engine this TUI runs" in t for t in texts)
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert fake_engine.instances[0].stopped is False  # still Fleet's engine
            assert app.engine_host.running and app.engine_keeper.lease.held

    async def test_auto_screen_alone_still_starts_dry_run(self, tmp_path, fake_engine):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        probe = EngineLease(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("e")
            await settle(pilot)
            assert len(fake_engine.instances) == 1 and fake_engine.instances[0].dry_run
            assert not app.engine_host.running
            await pilot.press("escape")
            await settle(pilot)
            assert fake_engine.instances[0].stopped is True
            assert await _eventually(lambda: not probe.held_elsewhere())

    async def test_quit_with_live_engine_asks(self, tmp_path, fake_engine):
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.modals import ConfirmModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _mode(pilot, "l")
            await pilot.press("y")
            await settle(pilot)
            assert app.engine_host.running and not app.engine_host.dry_run
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen) and app.engine_host.running
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
        assert fake_engine.instances[-1].stopped is True

    async def test_quit_with_dry_run_engine_does_not_ask(self, tmp_path, fake_engine):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _mode(pilot, "d")
            await pilot.press("q")
            await pilot.pause()
        assert fake_engine.instances[-1].stopped is True

    async def test_lease_released_after_host_engine_thread_exits(self, tmp_path, fake_engine):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        probe = EngineLease(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _mode(pilot, "d")
            assert probe.held_elsewhere() is True
            await _mode(pilot, "s")
            assert not app.engine_host.running
            assert await _eventually(lambda: not probe.held_elsewhere())
            assert _engine_line(app).startswith("Not switching — no engine is running")

    async def test_classic_dashboard_keeps_the_engine_running(self, tmp_path, fake_engine):
        from claude_swap.tui.dashboard import DashboardScreen
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _mode(pilot, "d")
            await pilot.press("c")
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)
            assert app.engine_host.running and not fake_engine.instances[0].stopped
            await pilot.press("ctrl+f")
            await settle(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert _engine_line(app).endswith("engine here · dry run")

    async def test_viewer_mode_offers_facts_only(self, tmp_path, fake_engine):
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        other = EngineLease(tmp_path)
        assert other.acquire()
        try:
            app = make_app(_fleet(tmp_path))
            async with app.run_test(size=(140, 40)) as pilot:
                await _open(pilot)
                await _mode(pilot, "d")  # not an option for a viewer: nothing happens
                facts = app.screen.query_one("#fx-mode-facts", Static).render().plain
                assert "holds the engine lease" in facts
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, FleetScreen)
                assert fake_engine.instances == []
        finally:
            other.release()
