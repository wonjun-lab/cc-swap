"""Quiet repaints (issue #54): an idle TUI emits no terminal output between
polls, and a changed value still reaches the screen on the next poll."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from textual.app import App

from claude_swap.tui import quiet
from tests.maximize.test_tui_fleet import _fleet, _settings, _state
from tests.test_tui import make_app, make_entry, settle


class Frames:
    """Counts the frames the app hands its driver (what a pty would carry)."""

    def __init__(self, monkeypatch):
        self.bytes = 0
        self.count = 0
        original = App._display
        frames = self

        def display(app, screen, renderable):
            if renderable is not None:
                frames.count += 1
                frames.bytes += len(renderable.render_segments(app.console))
            return original(app, screen, renderable)

        monkeypatch.setattr(App, "_display", display)

    def reset(self) -> None:
        self.bytes = self.count = 0


async def _poll(pilot, ticks: int) -> None:
    for _ in range(ticks):
        pilot.app._tick()
        await settle(pilot)
        pilot.app._update_refresh_status()
        await pilot.pause()


def _bump(fake, pct5: float) -> None:
    acc = fake._accounts[0]
    fake._accounts[0] = dataclasses.replace(acc, usage=make_entry(pct5, 40.0))


@pytest.mark.asyncio
class TestQuietRepaint:
    async def test_fleet_is_silent_between_polls_and_shows_a_change(
        self, tmp_path, monkeypatch
    ):
        _settings(tmp_path)
        _state(tmp_path)
        fake = _fleet(tmp_path)
        frames = Frames(monkeypatch)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await settle(pilot)
            await _poll(pilot, 1)
            frames.reset()
            await _poll(pilot, 5)
            assert (frames.count, frames.bytes) == (0, 0)
            _bump(fake, 88.0)
            await _poll(pilot, 1)
            assert frames.bytes > 0
            assert "88%" in app.screen.query_one("#fx-body").render().plain
            frames.reset()
            await _poll(pilot, 3)
            assert frames.bytes == 0

    async def test_dashboard_is_silent_between_polls_and_shows_a_change(
        self, tmp_path, monkeypatch
    ):
        fake = _fleet(tmp_path)
        frames = Frames(monkeypatch)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await settle(pilot)
            await _poll(pilot, 1)
            frames.reset()
            await _poll(pilot, 5)
            assert (frames.count, frames.bytes) == (0, 0)
            _bump(fake, 88.0)
            await _poll(pilot, 1)
            assert frames.bytes > 0
            frames.reset()
            await _poll(pilot, 3)
            assert frames.bytes == 0

    async def test_accounts_screen_is_silent_between_polls(self, tmp_path, monkeypatch):
        _settings(tmp_path)
        _state(tmp_path)
        frames = Frames(monkeypatch)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            from claude_swap.tui.fleet_accounts import AccountsScreen

            assert isinstance(app.screen, AccountsScreen)
            await _poll(pilot, 1)
            frames.reset()
            await _poll(pilot, 5)
            assert (frames.count, frames.bytes) == (0, 0)

    async def test_poll_interval_comes_from_settings(self, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({"schemaVersion": 1, "tui": {"pollSeconds": 20}})
        )
        assert make_app(_fleet(tmp_path)).POLL_INTERVAL_S == 20.0

    async def test_real_timers_stay_silent(self, tmp_path, monkeypatch):
        """The app's own timers (poll + 1 s status clock), not a manual tick."""
        (tmp_path / "settings.json").write_text(
            json.dumps({"schemaVersion": 1, "tui": {"pollSeconds": 1}})
        )
        frames = Frames(monkeypatch)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await settle(pilot)
            await asyncio.sleep(1.2)
            await settle(pilot)
            frames.reset()
            await asyncio.sleep(2.5)
            await settle(pilot)
            assert (frames.count, frames.bytes) == (0, 0)


class TestSameContent:
    def test_text_equality_includes_style(self):
        from rich.text import Text

        assert quiet.same_content(Text("a", style="red"), Text("a", style="red"))
        assert not quiet.same_content(Text("a", style="red"), Text("a", style="blue"))
        assert not quiet.same_content(Text("a"), Text("b"))
        assert quiet.same_content("x", "x") and not quiet.same_content("x", "y")
