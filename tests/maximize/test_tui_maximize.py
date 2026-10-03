"""Auto screen in maximize mode: ranked table, pending line, soft/hard ticks
and persisted threshold keys. Other strategies keep upstream's screen (see
tests/test_tui.py::TestAutoScreen)."""

from __future__ import annotations

import json
import time

import pytest
from textual.widgets import Static

from claude_swap.maximize.lease import EngineLease
from claude_swap.tui.theme import Palette
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


def _cold_entry(pct7: float) -> UsageEntry:
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
            make_account(1, active=True, entry=make_entry(72.0, 40.0)),
            make_account(2, entry=make_entry(10.0, 20.0)),
            make_account(3, entry=_cold_entry(30.0)),
            make_account(4, entry=make_entry(5.0, 5.0), disabled=True),
        ],
        root,
    )


def _state(root) -> None:
    now = time.time()
    (root / "autoswitch_state.json").write_text(json.dumps({
        "schemaVersion": 1,
        "maximizeSamples": {"account": "1", "samples": [
            [now - 660, 69.0, 40.0], [now - 60, 72.0, 40.0],
        ]},
        "primes": {"user2@example.com": {
            "windowKey": "cold", "attempts": 1,
            "lastAttemptAt": now + 7200 - 18000 + 30, "lastOutcome": "primed",
        }},
    }))


async def _open_auto(pilot) -> None:
    await settle(pilot)
    await pilot.press("g")
    await pilot.pause()
    await settle(pilot)


def _line(plain: str, needle: str) -> str:
    return next(line for line in plain.splitlines() if needle in line)


def _summary(app) -> str:
    return app.screen.query_one("#auto-summary", Static).render().plain


# -- widgets (pure renderers) --------------------------------------------------------


def _style_at(text, index: int) -> str:
    return next(str(s.style) for s in text.spans if s.start <= index < s.end)


def test_bar_cells_draws_a_warn_soft_tick_and_a_crit_hard_tick():
    from claude_swap.tui.widgets import bar_cells

    text = bar_cells(30.0, 20, threshold=50.0, hard=95.0)
    assert text.plain.count("┃") == 2
    assert text.plain.index("┃") == 10 and text.plain.rindex("┃") == 19
    assert _style_at(text, 10) == Palette.DARK.sev_warn
    assert _style_at(text, 19) == Palette.DARK.sev_crit


def test_bar_cells_hard_tick_wins_a_shared_cell():
    from claude_swap.tui.widgets import bar_cells

    text = bar_cells(30.0, 12, threshold=95.0, hard=98.0)
    assert text.plain.count("┃") == 1
    assert _style_at(text, 11) == Palette.DARK.sev_crit


def test_card_ticks_only_the_5h_and_7d_rows_in_maximize():
    from claude_swap.tui.widgets import account_card_text

    spend = {"used": 5.0, "limit": 50.0, "pct": 10.0, "resets_at": _iso_in(86400)}
    acc = make_account(1, active=True, entry=make_entry(
        47.0, 63.0, scoped=[("Fable", 20.0)], spend=spend,
    ))
    maximize = account_card_text(acc, 100, window_ticks={"5h": (50.0, 95.0), "7d": (90.0, 98.0)})
    rows = {line.split()[0]: line for line in maximize.plain.splitlines()[1:]}
    assert (rows["5h"].count("┃"), rows["7d"].count("┃")) == (2, 2)
    assert (rows["$$"].count("┃"), rows["Fable"].count("┃")) == (0, 0)
    upstream = account_card_text(acc, 100, threshold=90.0)
    assert all(line.count("┃") == 1 for line in upstream.plain.splitlines()[1:])


# -- the auto screen --------------------------------------------------------------------


@pytest.mark.asyncio
class TestMaximizeAutoScreen:
    async def test_table_shows_tier_score_landable_5h_state_and_pending(self, tmp_path, fake_engine):
        _settings(tmp_path, lastResort="user3@example.com")
        _state(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_auto(pilot)
            plain = app.screen.query_one("#candidates", Static).render().plain
            assert plain.startswith("Maximize")
            active, primed, cold, excluded = (
                _line(plain, f"user{n}@example.com") for n in (1, 2, 3, 4)
            )
            assert active.lstrip().startswith("●") and "5h running · resets" in active
            assert "normal" in primed and " yes " in primed and "5h primed · resets" in primed
            assert "last resort" in cold and "5h cold" in cold
            assert "excluded" in excluded and " no " in excluded
            order = [plain.index(f"user{n}@example.com") for n in (2, 1, 3, 4)]
            assert order == sorted(order)
            assert "waiting for idle: 5h 72%, +3%p/10min" in plain

    @pytest.mark.parametrize(
        ("code", "reason"),
        [
            ("reset-wait", "#1 5h 72% — resets in 8m; waiting it out instead of switching"),
            ("preempt", "#1 5h 72% >= soft 50%; moving to #2 once idle (preempt)"),
            ("rebalance-deferred", "rebalance to #2 deferred: the 5h window resets soon"),
        ],
    )
    async def test_a_coded_hold_shows_the_published_reason_not_waiting_for_idle(
        self, tmp_path, fake_engine, code, reason
    ):
        _settings(tmp_path)
        _state(tmp_path)
        path = tmp_path / "autoswitch_state.json"
        state = json.loads(path.read_text())
        state["maximizeDecision"] = {
            "at": time.time(), "pid": 4242, "active": "1", "decision": "hold",
            "trigger": None, "target": None, "reason": reason, "pending": False,
            "code": code,
        }
        path.write_text(json.dumps(state))
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(160, 40)) as pilot:
            await _open_auto(pilot)
            plain = app.screen.query_one("#candidates", Static).render().plain
            assert reason in plain
            assert "waiting for idle:" not in plain

    async def test_summary_shows_soft_and_hard_per_window(self, tmp_path, fake_engine):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_auto(pilot)
            assert "auto-switch · maximize · 5h 50/95% · 7d 90/98% · poll every 60s" in _summary(app)

    async def test_bars_draw_soft_and_hard_ticks(self, tmp_path, fake_engine):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await _open_auto(pilot)
            from claude_swap.tui.widgets import AccountsPanel

            panel = app.screen.query_one("#auto-active-panel", AccountsPanel).render().plain
            assert panel.count("┃") == 4
            assert app.window_ticks == {"5h": (50.0, 95.0), "7d": (90.0, 98.0)}

    async def test_threshold_keys_persist_soft_and_hard(self, tmp_path, fake_engine):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_auto(pilot)
            await pilot.press("t", "right", "right", "t", "left", "t", "t", "right")
            await pilot.pause()
            assert "5h 52/94% · 7d 90/99% (unsaved)" in _summary(app)
            assert "maximize" not in json.loads((tmp_path / "settings.json").read_text())
            await pilot.press("enter")
            await pilot.pause()
            saved = json.loads((tmp_path / "settings.json").read_text())["maximize"]
            assert saved == {"soft5h": 52.0, "hard5h": 94.0, "hard7d": 99.0}
            assert app.window_ticks == {"5h": (52.0, 94.0), "7d": (90.0, 99.0)}
            assert fake_engine.instances[0].wakes == 1
            assert "(unsaved)" not in _summary(app)

    async def test_soft_never_passes_hard(self, tmp_path, fake_engine):
        _settings(tmp_path, soft5h=94.0)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_auto(pilot)
            await pilot.press("t", "right", "right", "right", "enter")
            await pilot.pause()
            saved = json.loads((tmp_path / "settings.json").read_text())["maximize"]
            assert saved == {"soft5h": 95.0}

    async def test_escape_discards_unsaved_thresholds(self, tmp_path, fake_engine):
        _settings(tmp_path)
        before = (tmp_path / "settings.json").read_text()
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_auto(pilot)
            await pilot.press("t", "right", "escape")
            await pilot.pause()
            from claude_swap.tui.autoview import AutoScreen

            assert isinstance(app.screen, AutoScreen)
            assert (tmp_path / "settings.json").read_text() == before
            assert "5h 50/95%" in _summary(app)
            assert app.window_ticks == {"5h": (50.0, 95.0), "7d": (90.0, 98.0)}

    async def test_a_viewer_still_saves_thresholds_for_the_running_engine(self, tmp_path, fake_engine):
        _settings(tmp_path)
        other = EngineLease(tmp_path)
        assert other.acquire()
        try:
            app = make_app(_fleet(tmp_path))
            async with app.run_test(size=(120, 40)) as pilot:
                await _open_auto(pilot)
                await pilot.press("t", "right", "enter")
                await pilot.pause()
                saved = json.loads((tmp_path / "settings.json").read_text())["maximize"]
                assert saved == {"soft5h": 51.0}
                assert fake_engine.instances == []
        finally:
            other.release()

    async def test_other_strategies_keep_the_next_best_list(self, tmp_path, fake_engine):
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_auto(pilot)
            plain = app.screen.query_one("#candidates", Static).render().plain
            assert plain.startswith("Next best")
            assert app.window_ticks is None
