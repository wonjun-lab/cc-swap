"""The Fleet home screen (read-only parts): app wiring, table, status lines,
viewer/owner detection and layout degradation. Pilot tests against
FakeSwitcher, temp backup roots, a fake service probe."""

from __future__ import annotations

import json
import os
import time

import pytest
from textual.widgets import DataTable, Static

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.maximize.lease import EngineLease
from claude_swap.usage_store import UsageEntry
from tests.test_tui import (  # noqa: F401 (fake_engine is a fixture)
    FakeSwitcher,
    _iso_in,
    fake_engine,
    make_account,
    make_app,
    make_entry,
    settle,
)


def _settings(root, **maximize) -> None:
    payload = {"schemaVersion": 1, "autoswitch": {"strategy": "maximize"}}
    if maximize:
        payload["maximize"] = maximize
    (root / "settings.json").write_text(json.dumps(payload))


def _cold(pct7: float) -> UsageEntry:
    return UsageEntry(
        last_good={
            "five_hour": {"pct": 0.0, "resets_at": None},
            "seven_day": {"pct": pct7, "resets_at": _iso_in(86400 * 3)},
        },
        fetched_at=time.time() - 5,
        age_s=5.0,
    )


def _fleet(root) -> FakeSwitcher:
    return FakeSwitcher(
        [
            make_account(1, active=True, entry=make_entry(62.0, 40.0), alias="main"),
            make_account(2, entry=make_entry(10.0, 20.0)),
            make_account(3, entry=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED), alias="old"),
            make_account(4, entry=_cold(30.0), disabled=True),
        ],
        root,
    )


def _state(root, **extra) -> None:
    now = time.time()
    payload = {
        "schemaVersion": 1,
        "maximizeSamples": {"account": "1", "samples": [
            [now - 660, 59.0, 40.0], [now - 60, 62.0, 40.0],
        ]},
        "quarantine": {"3": {"reason": "invalid_grant"}},
    }
    payload.update(extra)
    (root / "autoswitch_state.json").write_text(json.dumps(payload))


def _plain(app, selector: str) -> str:
    return app.screen.query_one(selector, Static).render().plain


def _row(app, key: str) -> list[str]:
    table = app.screen.query_one("#fx-table", DataTable)
    return [cell.plain for cell in table.get_row(key)]


def _cursor_key(app) -> str:
    table = app.screen.query_one("#fx-table", DataTable)
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


async def _open(pilot) -> None:
    await settle(pilot)
    await settle(pilot)


@pytest.mark.asyncio
class TestFleetScreen:
    async def test_maximize_strategy_opens_fleet_over_the_dashboard(self, tmp_path):
        from claude_swap.tui.dashboard import DashboardScreen
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            assert isinstance(app.screen, FleetScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)  # Esc never leaves home
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            await pilot.press("ctrl+f")
            await settle(pilot)
            assert isinstance(app.screen, FleetScreen)
            # From a screen stacked over Fleet, ctrl+f pops back down to it.
            await pilot.press("w")
            await pilot.pause()
            assert not isinstance(app.screen, FleetScreen)
            await pilot.press("ctrl+f")
            await settle(pilot)
            assert isinstance(app.screen, FleetScreen)

    async def test_other_strategies_keep_the_upstream_dashboard_home(self, tmp_path):
        from claude_swap.tui.dashboard import DashboardScreen

        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            assert isinstance(app.screen, DashboardScreen)
            await pilot.press("ctrl+f")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)

    async def test_watch_entry_point_still_opens_watch(self, tmp_path):
        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.dashboard import WatchScreen

        _settings(tmp_path)
        app = CswapApp(_fleet(tmp_path), start="watch")
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            assert isinstance(app.screen, WatchScreen)

    async def test_table_rows_in_slot_order_with_rank_tier_land_window_prime(self, tmp_path):
        _settings(tmp_path, lastResort="user2@example.com")
        _state(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            table = app.screen.query_one("#fx-table", DataTable)
            assert [k.value for k in table.rows] == ["1", "2", "3", "4"]
            active = _row(app, "1")
            assert active[:3] == ["*", "1", "main"]
            assert "62%" in active and "active" in active
            assert any(cell.startswith("running → ") for cell in active)
            second = _row(app, "2")
            assert "last-r" in second and "yes" in second
            dead = _row(app, "3")
            assert "re-login" in dead and "?" in dead
            excluded = _row(app, "4")
            assert "excl" in excluded and "—" in excluded and "cold" in excluded

    async def test_cursor_stays_on_the_same_account_when_snapshot_reorders(self, tmp_path):
        _settings(tmp_path)
        fake = _fleet(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            assert _cursor_key(app) == "1"  # starts on the active account
            await pilot.press("down", "down")
            await pilot.pause()
            assert _cursor_key(app) == "3"
            fake._accounts = [a for a in fake._accounts if a.number != "2"]
            app.request_refresh()
            await _open(pilot)
            assert [k.value for k in app.screen.query_one("#fx-table", DataTable).rows] == [
                "1", "3", "4",
            ]
            assert _cursor_key(app) == "3"

    async def test_relogin_account_is_red_named_in_attention_and_header(self, tmp_path):
        from claude_swap.tui.theme import Palette

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            assert _plain(app, "#fx-attention") == (
                "⚠ #3 old needs re-login (refresh token dead) — select it and press r"
            )
            assert "1 needs re-login" in _plain(app, "#fx-head")
            table = app.screen.query_one("#fx-table", DataTable)
            crit = Palette.from_theme(app.current_theme).sev_crit
            assert all(crit in str(cell.style) for cell in table.get_row("3")[1:])
            menu = [item.query_one(Static).render().plain
                    for item in app.screen.query("FleetMenuItem")]
            assert "Account settings · 1 needs re-login" in menu

    async def test_viewer_when_lease_held_elsewhere_shows_pid_and_store_only(
        self, tmp_path, fake_engine
    ):
        _settings(tmp_path)
        other = EngineLease(tmp_path)
        assert other.acquire()
        try:
            app = make_app(_fleet(tmp_path))
            async with app.run_test(size=(140, 40)) as pilot:
                await _open(pilot)
                engine = _plain(app, "#fx-engine")
                assert f"● pid {os.getpid()}" in engine and "viewer" in engine
                assert app._store_only is True
                assert fake_engine.instances == []
                menu = [item.query_one(Static).render().plain
                        for item in app.screen.query("FleetMenuItem")]
                assert f"Mode: pid {os.getpid()} · viewing" in menu
        finally:
            other.release()

    async def test_service_holder_is_named_service(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "claude_swap.tui.fleet.service_status",
            lambda: {"platform": "darwin", "installed": True, "running": True,
                     "state": "running", "pid": os.getpid()},
        )
        _settings(tmp_path)
        other = EngineLease(tmp_path)
        assert other.acquire()
        try:
            app = make_app(_fleet(tmp_path))
            async with app.run_test(size=(140, 40)) as pilot:
                await _open(pilot)
                engine = _plain(app, "#fx-engine")
                assert "● service" in engine and "holds the lease — this TUI is a viewer" in engine
        finally:
            other.release()

    async def test_no_engine_line_warns_and_fetch_lane_is_normal(self, tmp_path, fake_engine):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            assert "○ nothing is switching" in _plain(app, "#fx-engine")
            assert app._store_only is False
            assert fake_engine.instances == []  # Fleet never takes the lease itself
            assert not app.engine_keeper.lease.held

    async def test_f_fetches_even_as_a_viewer(self, tmp_path):
        _settings(tmp_path)
        other = EngineLease(tmp_path)
        assert other.acquire()
        try:
            fake = _fleet(tmp_path)
            app = make_app(fake)
            async with app.run_test(size=(140, 40)) as pilot:
                await _open(pilot)
                assert app._store_only is True
                fake.fetch_sets.clear()
                await pilot.press("f")
                await _open(pilot)
                assert None in fake.fetch_sets  # a fetch-enabled snapshot ran
        finally:
            other.release()

    async def test_published_decision_drives_the_now_line(self, tmp_path):
        _settings(tmp_path)
        _state(tmp_path, maximizeDecision={
            "at": time.time() - 20, "pid": 4121, "active": "1", "decision": "switch",
            "trigger": "soft", "target": "2", "reason": "#1 5h 62% >= soft 50%; idle; -> #2",
            "pending": False, "plans": {"1": "20x"},
        })
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            now_line = _plain(app, "#fx-now")
            assert now_line.startswith("now     SWITCH (soft) → #2")
            assert now_line.endswith("· engine")
            assert _row(app, "1")[3] == "20x"

    async def test_stale_published_decision_says_computed_here(self, tmp_path):
        _settings(tmp_path)
        _state(tmp_path, maximizeDecision={
            "at": time.time() - 3600, "pid": 4121, "active": "1", "decision": "switch",
            "trigger": "soft", "target": "2", "reason": "old", "pending": False,
        })
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            now_line = _plain(app, "#fx-now")
            assert now_line.startswith("now     HOLD — waiting for idle → #2")
            assert now_line.endswith("computed here")

    @pytest.mark.parametrize(("size", "detail", "menu", "folded"), [
        ((112, 32), True, True, False),
        ((100, 24), False, True, False),
        ((80, 18), False, False, True),
    ])
    async def test_short_terminal_folds_menu_and_drops_detail(
        self, tmp_path, size, detail, menu, folded
    ):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=size) as pilot:
            await _open(pilot)
            screen = app.screen
            assert screen.query_one("#fx-detail").display is detail
            assert screen.query_one("#fx-menu").display is menu
            assert screen.query_one("#fx-menu-folded").display is folded
            if folded:
                assert "Strategy" in _plain(app, "#fx-menu-folded")
            if detail:
                assert "rank" in _plain(app, "#fx-detail")

    async def test_arrow_down_past_the_last_row_reaches_the_menu(self, tmp_path):
        from textual.widgets import ListView

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("down", "down", "down", "down")
            await pilot.pause()
            menu = app.screen.query_one("#fx-menu", ListView)
            assert app.focused is menu and menu.index == 0
            await pilot.press("up")
            await pilot.pause()
            assert app.focused is app.screen.query_one("#fx-table")

    async def test_help_opens_and_b_returns(self, tmp_path):
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.fleet_help import HelpScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            assert "next prime" in app.screen.query_one("#fx-help", Static).render().plain
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)

    async def test_engine_log_key_opens_the_auto_screen(self, tmp_path, fake_engine):
        from claude_swap.tui.autoview import AutoScreen
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("e")
            await settle(pilot)
            assert isinstance(app.screen, AutoScreen)
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert app._store_only is False  # Fleet took the lane back
