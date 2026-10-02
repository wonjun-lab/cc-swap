"""AutoScreen under the engine lease: the owner runs an engine, a viewer never
does, and a screen's own lease is never mistaken for someone else's."""

from __future__ import annotations

import asyncio
import time

import pytest
from textual.widgets import RichLog, Static

from claude_swap.maximize.lease import EngineLease
from tests.test_tui import (  # noqa: F401 (fake_engine is a fixture)
    FakeSwitcher,
    fake_engine,
    make_account,
    make_app,
    settle,
)


async def _eventually(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _open_auto(pilot) -> None:
    await settle(pilot)
    await pilot.press("g")
    await pilot.pause()
    await settle(pilot)


def _fleet(root):
    return FakeSwitcher([make_account(1, active=True), make_account(2)], root)


@pytest.mark.asyncio
class TestAutoScreenLease:
    async def test_viewer_when_another_engine_holds_the_lease(self, tmp_path, fake_engine):
        other = EngineLease(tmp_path)
        assert other.acquire()
        try:
            app = make_app(_fleet(tmp_path))
            async with app.run_test(size=(100, 40)) as pilot:
                await _open_auto(pilot)
                from claude_swap.tui.autoview import AutoScreen

                screen = app.screen
                assert isinstance(screen, AutoScreen)
                assert fake_engine.instances == []
                badge = screen.query_one("#mode-badge", Static).render().plain
                assert "VIEWER" in badge
                assert app._store_only is True  # the other engine fetches
                log = screen.query_one("#event-log", RichLog)
                assert any(
                    "another cc-swap engine is running" in line.text
                    for line in log.lines
                )
                await pilot.press("l")
                await pilot.pause()
                assert fake_engine.instances == []
                assert isinstance(app.screen, AutoScreen)  # no go-live modal
                await pilot.press("t")
                await pilot.pause()
                assert screen._adjusting is False  # no engine to steer
        finally:
            other.release()

    async def test_owner_keeps_the_lease_until_its_engine_thread_exits(
        self, tmp_path, fake_engine
    ):
        app = make_app(_fleet(tmp_path))
        probe = EngineLease(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await _open_auto(pilot)
            assert len(fake_engine.instances) == 1
            assert probe.held_elsewhere() is True
            await pilot.press("escape")
            await settle(pilot)
            assert await _eventually(lambda: not probe.held_elsewhere())

    async def test_go_live_restart_keeps_the_lease(self, tmp_path, fake_engine):
        app = make_app(_fleet(tmp_path))
        probe = EngineLease(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await _open_auto(pilot)
            first = next(w for w in app.workers if w.group == "engine")
            await pilot.press("l")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped is True
            await app.workers.wait_for_complete([first])
            assert probe.held_elsewhere() is True  # the live engine owns it

    async def test_reopening_the_screen_reclaims_the_apps_own_lease(
        self, tmp_path, fake_engine
    ):
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await _open_auto(pilot)
            await pilot.press("escape")
            await settle(pilot)
            await _open_auto(pilot)
            assert len(fake_engine.instances) == 2
            badge = app.screen.query_one("#mode-badge", Static).render().plain
            assert "VIEWER" not in badge
