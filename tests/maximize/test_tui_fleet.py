"""The Fleet home screen: app wiring, the status sentence, the account
blocks and their tags, the layout at four terminal sizes, selection and
key routing. Pilot tests against FakeSwitcher, temp backup roots, a fake
service probe."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest
from textual.widgets import Static

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.maximize import pause
from claude_swap.maximize.lease import EngineLease
from claude_swap.models import AccountSnapshot
from claude_swap.tui import menus
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

FOOTER = "enter switch · r re-login · l last resort · m menu · ? help · q quit"


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


def _status(app) -> str:
    return _plain(app, "#fx-status")


def _body(app) -> str:
    return _plain(app, "#fx-body")


def _block(app, number: str) -> str:
    """The body lines of one account (its block, or its line)."""
    body = app.screen.query_one("#fx-body")
    first, count = body.layout_map.spans[number]
    return "\n".join(_body(app).splitlines()[first:first + count])


def _decision(**fields) -> dict:
    return {
        "at": time.time() - 20, "pid": 4121, "active": "1", "decision": "switch",
        "trigger": "soft", "target": "2", "reason": "#1 5h 62% >= soft 50%; idle; -> #2",
        "pending": False, **fields,
    }


async def _open(pilot) -> None:
    await settle(pilot)
    await settle(pilot)


# -- the POC's six accounts, for the size tests --------------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _six(root) -> FakeSwitcher:
    now = time.time()

    def entry(p5, r5, p7, days7):
        return UsageEntry(
            last_good={
                "five_hour": {"pct": p5, "resets_at": _iso(now + r5) if r5 else None},
                "seven_day": {"pct": p7, "resets_at": _iso(now + days7 * 86400)},
            },
            fetched_at=now - 40, age_s=40.0,
        )

    def account(n, alias, usage, *, active=False, org="", login_days=None, disabled=False):
        return AccountSnapshot(
            number=str(n), email=f"{alias}@acme.dev", org_name=org,
            org_uuid="org-1" if org else "", is_active=active, kind="oauth", switchable=True,
            usage=usage, alias=alias, disabled=disabled,
            login_expires_at=(now + login_days * 86400) * 1000.0 if login_days else None,
        )

    (root / "settings.json").write_text(json.dumps({
        "schemaVersion": 1, "autoswitch": {"strategy": "maximize"},
        "maximize": {"lastResort": "team@acme.dev", "soft5h": 50, "hard5h": 98,
                     "soft7d": 90, "hard7d": 98},
        "prime": {"enabled": True, "jitterS": "45-300"},
    }))
    _state(root, maximizeDecision=_decision(
        at=now - 50, decision="hold", trigger=None, pending=True,
        reason="#1 5h 62% >= soft 50%; waiting for idle to move to #2",
        plans={"1": "20x", "2": "5x", "3": "5x", "4": "20x", "5": "5x", "6": "team"},
    ))
    return FakeSwitcher([
        account(1, "main", entry(62.0, 1.8 * 3600, 41.0, 3.8), active=True, login_days=21),
        account(2, "side", entry(0.0, None, 35.0, 2.2), login_days=20),
        account(3, "old", UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED)),
        account(4, "work", entry(3.0, 3.3 * 3600, 22.0, 5.1), login_days=1.2),
        account(5, "alt", entry(48.0, 0.5 * 3600, 71.0, 1.5), login_days=14, disabled=True),
        account(6, "team", entry(0.0, None, 30.0, 4.0), org="Acme Team", login_days=29),
    ], root)


@pytest.fixture
def held_by_service(tmp_path, monkeypatch):
    """The engine lease held by this process, standing in for the service."""
    monkeypatch.setattr(
        "claude_swap.tui.fleet.service_status",
        lambda: {"platform": "darwin", "installed": True, "running": True,
                 "state": "running", "pid": os.getpid()},
    )
    lease = EngineLease(tmp_path)
    assert lease.acquire()
    yield lease
    lease.release()


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
            await pilot.press("m", "c")
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

    async def test_accounts_in_order_with_one_tag_each(self, tmp_path):
        _settings(tmp_path, lastResort="user2@example.com")
        _state(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:  # one column
            await _open(pilot)
            screen = app.screen
            assert screen._order == ["1", "2", "3", "4"]  # active, the pick, the rest
            assert screen._sel == "1"  # starts on the active account
            assert _block(app, "1").splitlines()[0].rstrip().endswith("● active")
            assert "62%" in _block(app, "1")
            assert _block(app, "2").splitlines()[0].rstrip().endswith("last resort")
            assert _block(app, "3").splitlines()[0].rstrip().endswith("re-login (r)")
            assert "⚠ needs re-login — select it and press r" in _block(app, "3")
            assert _block(app, "4").splitlines()[0].rstrip().endswith("excluded")
            assert "not started" in _block(app, "4")

    async def test_selection_stays_on_the_same_account_when_the_snapshot_changes(
        self, tmp_path
    ):
        _settings(tmp_path)
        fake = _fleet(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("down", "down")
            await pilot.pause()
            assert app.screen._sel == "3"
            fake._accounts = [a for a in fake._accounts if a.number != "2"]
            app.request_refresh()
            await _open(pilot)
            assert app.screen._order == ["1", "3", "4"]
            assert app.screen._sel == "3"

    async def test_a_dead_login_is_named_in_the_attention_line_and_the_menu(self, tmp_path):
        from claude_swap.tui.fleet_modals import MenuItem, MenuModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            assert _plain(app, "#fx-attention") == (
                "! #3 old needs re-login — select it, press r"
            )
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen, MenuModal)
            titles = [item.row.title for item in app.screen.query(MenuItem)]
            assert "Account settings… · 1 needs re-login" in titles

    async def test_viewer_of_another_engine_says_who_and_reads_the_store(
        self, tmp_path, fake_engine
    ):
        from claude_swap.tui.fleet_modals import MenuItem

        _settings(tmp_path)
        other = EngineLease(tmp_path)
        assert other.acquire()
        try:
            app = make_app(_fleet(tmp_path))
            async with app.run_test(size=(140, 40)) as pilot:
                await _open(pilot)
                status = _status(app)
                # No decision published yet: nothing is claimed.
                assert status.startswith(
                    "Auto ON · using #1 main · waiting for the engine's next check"
                )
                assert status.endswith(f"viewer · pid {os.getpid()} is switching")
                assert app._store_only is True
                assert fake_engine.instances == []
                await pilot.press("m")
                await pilot.pause()
                titles = [item.row.title for item in app.screen.query(MenuItem)]
                assert f"Mode: pid {os.getpid()} · viewing" in titles
        finally:
            other.release()

    async def test_the_service_is_named(self, tmp_path, held_by_service):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            assert _status(app).endswith(f"viewer · service pid {os.getpid()} is switching")

    async def test_no_engine_says_not_switching_and_never_takes_the_lease(
        self, tmp_path, fake_engine
    ):
        _settings(tmp_path)
        _state(tmp_path, maximizeDecision=_decision())
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            status = _status(app)
            # A fresh decision in the state file, but no engine: not live.
            assert status == "Not switching — no engine is running (m to start one)"
            assert "next" not in _body(app)
            assert app._store_only is False
            assert fake_engine.instances == []  # Fleet never takes the lease itself
            assert not app.engine_keeper.lease.held

    async def test_a_fresh_published_decision_drives_the_sentence(
        self, tmp_path, held_by_service
    ):
        _settings(tmp_path)
        _state(tmp_path, maximizeDecision=_decision(plans={"1": "20x"}))
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(160, 40)) as pilot:
            await _open(pilot)
            assert _status(app).startswith(
                "Auto ON · switching #1 main → #2 user2@example.com now (soft)"
            )
            assert "[20x]" in _block(app, "1").splitlines()[0]
            assert _block(app, "2").splitlines()[0].rstrip().endswith("next")

    async def test_a_stale_published_decision_says_the_engine_is_silent(
        self, tmp_path, held_by_service
    ):
        _settings(tmp_path)
        _state(tmp_path, maximizeDecision=_decision(at=time.time() - 3600, reason="old"))
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(160, 40)) as pilot:
            await _open(pilot)
            status = _status(app)
            assert status.startswith("Auto ON · the engine has not reported since ")
            assert "(1h ago)" in status and "nothing below is live" in status
            assert status.endswith("is silent")
            assert "switch to" not in status and "#2" not in status
            assert "next" not in _body(app)

    async def test_auto_off_says_so_and_hides_the_next_and_prime_times(
        self, tmp_path, held_by_service
    ):
        _settings(tmp_path)
        _state(tmp_path, maximizeDecision=_decision(),
               autoOff={"since": time.time() - 60, "by": "cli"})
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            status = _status(app)
            assert status.startswith("Auto OFF — nothing switches automatically (m to turn on)")
            assert status.endswith("is idle")
            assert "next" not in _body(app) and "prime " not in _body(app)

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
                await pilot.press("m", "f")
                await _open(pilot)
                assert None in fake.fetch_sets  # a fetch-enabled snapshot ran
        finally:
            other.release()

    async def test_classic_dashboard_f_fetches_and_fleet_restores_store_only(self, tmp_path):
        from claude_swap.tui.dashboard import DashboardScreen
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        other = EngineLease(tmp_path)
        assert other.acquire()
        try:
            fake = _fleet(tmp_path)
            app = make_app(fake)
            async with app.run_test(size=(140, 40)) as pilot:
                await _open(pilot)
                assert app._store_only is True
                await pilot.press("c")
                await settle(pilot)
                assert isinstance(app.screen, DashboardScreen)
                assert app._store_only is False  # upstream dashboard owns the lane
                await settle(pilot)
                fake.fetch_sets.clear()
                await pilot.press("f")
                await _open(pilot)
                assert None in fake.fetch_sets  # a real fetch, not a store read
                await pilot.press("ctrl+f")
                await settle(pilot)
                assert isinstance(app.screen, FleetScreen)
                assert app._store_only is True  # Fleet put the viewer lane back
        finally:
            other.release()

    async def test_help_explains_the_words_and_b_returns(self, tmp_path):
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.fleet_help import HelpScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("question_mark")
            await _open(pilot)
            assert isinstance(app.screen, HelpScreen)
            text = app.screen.query_one("#fx-help", Static).render().plain
            for word in ("soft mark", "hard mark", "next", "last resort", "pace / score",
                         "priming", "viewer / lease", "● active"):
                assert word in text, word
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)

    async def test_a_published_reset_wait_reads_as_waiting_it_out(
        self, tmp_path, held_by_service
    ):
        _settings(tmp_path)
        reason = "#1 5h 96% — resets in 8m, waiting it out (switches at once if it hits 100%)"
        _state(tmp_path, maximizeDecision=_decision(
            decision="hold", trigger=None, target=None, reason=reason, code="reset-wait",
        ))
        resetting = UsageEntry(
            last_good={
                "five_hour": {"pct": 96.0, "resets_at": _iso_in(500)},
                "seven_day": {"pct": 40.0, "resets_at": _iso_in(86400 * 3)},
            },
            fetched_at=time.time() - 5, age_s=5.0,
        )
        fake = FakeSwitcher([
            make_account(1, active=True, entry=resetting, alias="main"),
            make_account(2, entry=make_entry(10.0, 20.0)),
        ], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(160, 40)) as pilot:
            await _open(pilot)
            assert _status(app).startswith(
                "Auto ON · using #1 main · 5h 96% — resets in 8m, waiting it out "
                "(switches at once if it hits 100%)"
            )
        async with make_app(fake).run_test(size=(90, 28)) as pilot:
            await _open(pilot)
            # Narrower: the policy's own words, without the account's name.
            assert _status(pilot.app).startswith(
                "Auto ON · #1 5h 96% — resets in 8m, waiting it out"
            )

    async def test_help_names_the_learned_idle_pattern(self, tmp_path):
        from claude_swap.maximize import history
        from claude_swap.tui.fleet_help import HelpScreen

        _settings(tmp_path)
        now = time.time()
        start = now - now % history.SLOT_S
        slots = [history.SlotObs(start - k * history.SLOT_S, k % 4 == 0) for k in range(1, 5 * 96)]
        (tmp_path / history.HISTORY_FILENAME).write_text(
            "".join(history._line(s) for s in reversed(slots))
        )
        days = history.learned_days(slots, now)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            # The home screen stays quiet about it ...
            assert "idle pattern" not in _status(app) + _body(app)
            await pilot.press("question_mark")
            await _open(pilot)
            assert isinstance(app.screen, HelpScreen)
            text = app.screen.query_one("#fx-help", Static).render().plain
            # ... and help says what has been learned.
            assert "Learned so far" in text
            assert f"idle pattern        {days} days learned · " in text
            for word in ("waiting it out", "quiet time", "preempt", "rebalance deferred"):
                assert word in text, word

    async def test_engine_log_opens_the_auto_screen(self, tmp_path, fake_engine):
        from claude_swap.tui.autoview import AutoScreen
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("m", "e")
            await settle(pilot)
            assert isinstance(app.screen, AutoScreen)
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert app._store_only is False  # Fleet took the lane back


# -- the layout at four sizes -----------------------------------------------------------------------


SIZES = [((160, 45), "wide"), ((120, 36), "medium"), ((90, 28), "narrow"), ((80, 24), "narrow")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "mode"), SIZES, ids=[f"{w}x{h}" for (w, h), _ in SIZES])
async def test_every_size_keeps_the_sentence_the_attention_line_and_the_footer(
    tmp_path, held_by_service, size, mode
):
    width, height = size
    app = make_app(_six(tmp_path))
    async with app.run_test(size=size) as pilot:
        await _open(pilot)
        screen = app.screen
        assert screen._layout.mode == mode
        status = screen.query_one("#fx-status")
        attention = screen.query_one("#fx-attention")
        keys = screen.query_one("#fx-keys")
        scroll = screen.query_one("#fx-scroll")
        # The top two lines and the footer are on screen, outside the scroll.
        assert status.region.y == 1 and attention.region.y == 2
        assert keys.region.y == height - 1
        assert scroll.region.y > attention.region.y
        assert scroll.region.bottom <= keys.region.y
        assert _status(app).startswith("Auto ON · ")
        assert "#2 side when you pause" in _status(app)
        assert _plain(app, "#fx-attention").startswith(
            "! #3 old needs re-login — select it, press r"
        )
        assert _plain(app, "#fx-keys") == FOOTER
        # Order and tags (same in every layout).
        assert screen._order == ["1", "2", "4", "6", "3", "5"]
        body = _body(app)
        for tag in ("● active", "next", "login 1d left", "last resort", "re-login (r)",
                    "excluded"):
            assert tag in body, tag
        lines = body.splitlines()
        assert all(len(line) <= width - 3 for line in lines)
        expanded = screen.query_one("#fx-expanded")
        if mode == "wide":
            assert " 1  main" in lines[0] and " 2  side" in lines[0]
            assert not expanded.display
        elif mode == "medium":
            assert lines[0].startswith(" 1  main (main@acme.dev)  [personal] [20x]")
            assert "side" not in lines[0]
            assert not expanded.display
        else:
            assert len(lines) == 6 and lines[0].startswith("● 1 main")
            assert expanded.display
            detail = _plain(app, "#fx-expanded").splitlines()
            assert detail[1].startswith(" 1  main (main@acme.dev)")
            assert expanded.region.bottom <= keys.region.y
            # The expanded account sits right under the list.
            assert expanded.region.y == scroll.region.bottom


@pytest.mark.asyncio
async def test_the_layout_follows_a_live_resize(tmp_path, held_by_service):
    app = make_app(_six(tmp_path))
    async with app.run_test(size=(160, 45)) as pilot:
        await _open(pilot)
        screen = app.screen
        await pilot.press("down")  # #4, below #1 in two columns
        await pilot.pause()
        assert screen._layout.mode == "wide" and screen._sel == "4"
        for size, mode in (((120, 36), "medium"), ((80, 24), "narrow"), ((160, 45), "wide")):
            await pilot.resize_terminal(*size)
            await _open(pilot)
            assert screen._layout.mode == mode
            assert screen._sel == "4"  # the selection survives the change
            assert _status(app).startswith("Auto ON · ")
            assert screen.query_one("#fx-status").region.y == 1
            assert screen.query_one("#fx-keys").region.y == size[1] - 1
            assert screen.query_one("#fx-expanded").display is (mode == "narrow")
            assert all(len(line) <= size[0] - 3 for line in _body(app).splitlines())


@pytest.mark.asyncio
@pytest.mark.parametrize("height", [14, 10, 8, 6])
async def test_a_very_short_terminal_clips_but_never_scrolls_the_header(
    tmp_path, held_by_service, height
):
    app = make_app(_six(tmp_path))
    async with app.run_test(size=(80, height)) as pilot:
        await _open(pilot)
        screen = app.screen
        status = screen.query_one("#fx-status")
        keys = screen.query_one("#fx-keys")
        expanded = screen.query_one("#fx-expanded")
        # The mouse wheel never moves the screen itself, only the accounts.
        assert not screen.allow_vertical_scroll and screen.scroll_offset.y == 0
        assert status.region.y == 0 and status.region.height == 1  # no blank lines
        assert keys.region.y == height - 1
        assert screen.query_one("#fx-scroll").region.height >= 2
        if expanded.display:
            assert expanded.region.bottom <= keys.region.y


@pytest.mark.asyncio
async def test_80x24_with_many_accounts_scrolls_the_list_but_never_the_header(tmp_path):
    _settings(tmp_path)
    accounts = [make_account(1, active=True, entry=make_entry(30.0, 20.0), alias="main")]
    accounts += [make_account(n, entry=make_entry(5.0 + n, 10.0)) for n in range(2, 17)]
    app = make_app(FakeSwitcher(accounts, tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(pilot)
        screen = app.screen
        scroll = screen.query_one("#fx-scroll")
        for _ in range(15):
            await pilot.press("down")
        await _open(pilot)
        last = screen._order[-1]
        assert screen._sel == last
        first, _count = screen.query_one("#fx-body").layout_map.spans[last]
        top = scroll.scroll_offset.y
        assert top <= first < top + scroll.scrollable_content_region.height
        assert screen.query_one("#fx-status").region.y == 1
        assert _plain(app, "#fx-keys") == FOOTER
        assert screen.query_one("#fx-keys").region.y == 23
        assert _plain(app, "#fx-expanded").splitlines()[1].startswith(f"{last:>2}  ")


# -- keys -----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestKeys:
    async def test_arrows_move_the_selection_and_a_click_selects(self, tmp_path):
        _settings(tmp_path)
        app = make_app(_six(tmp_path))
        async with app.run_test(size=(160, 45)) as pilot:
            await _open(pilot)
            screen = app.screen
            assert screen._layout.columns == 2
            await pilot.press("down")      # 1 2 / 4 6 / 3 5: below #1 is #4
            await pilot.pause()
            assert screen._sel == "4"
            await pilot.press("right")
            await pilot.pause()
            assert screen._sel == "6"
            await pilot.press("k")
            await pilot.pause()
            assert screen._sel == "2"
            body = screen.query_one("#fx-body")
            first, _ = body.layout_map.spans["3"]
            await pilot.click("#fx-body", offset=(4, first))
            await pilot.pause()
            assert screen._sel == "3"

    async def test_enter_switches_the_selected_account(self, tmp_path):
        _settings(tmp_path)
        fake = _fleet(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open(pilot)
            await pilot.press("down", "enter")
            await _open(pilot)
            assert ("switch_to", "2") in fake.calls

    async def test_m_opens_the_menu_and_every_letter_runs_its_item(self, tmp_path):
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.fleet_modals import MenuModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            screen = app.screen
            seen: list[str] = []
            for name, action in (
                ("toggle_auto", "auto"), ("open_mode", "mode"), ("open_strategy", "strategy"),
                ("open_prime", "prime"), ("action_fetch", "fetch"),
                ("action_exclude", "exclude"), ("open_accounts", "accounts"),
                ("open_history", "history"), ("open_update", "update"),
                ("action_classic", "classic"), ("action_quit", "quit"),
            ):
                setattr(screen, name, lambda action=action: seen.append(action))
            app.action_open_auto = lambda: seen.append("engine")
            for entry in menus.MAIN_MENU:
                await pilot.press("m")
                await pilot.pause()
                assert isinstance(app.screen, MenuModal)
                await pilot.press(entry.key)
                await pilot.pause()
                assert isinstance(app.screen, FleetScreen), entry
            assert seen == [e.action for e in menus.MAIN_MENU]
            # Nothing is highlighted when it opens: a stray enter runs nothing
            # (the first item turns automatic switching off).
            await pilot.press("m", "enter")
            await pilot.pause()
            assert isinstance(app.screen, MenuModal) and len(seen) == len(menus.MAIN_MENU)
            # ↑↓ then enter work; esc closes without doing anything.
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()
            assert seen[-1] == "strategy"
            await pilot.press("m", "escape")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen) and len(seen) == len(menus.MAIN_MENU) + 1

    async def test_menu_o_turns_automatic_switching_off_and_on(self, tmp_path):
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.modals import ConfirmModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("o")  # not a home key: one stray key never turns it off
            await _open(pilot)
            assert pause.read_auto_off(tmp_path) is None
            # Turning it OFF asks first (review of rel/0.4.0): m then a stray
            # o must not stop switching. esc cancels ...
            await pilot.press("m", "o")
            await _open(pilot)
            assert isinstance(app.screen, ConfirmModal)
            assert pause.read_auto_off(tmp_path) is None
            await pilot.press("escape")
            await _open(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert pause.read_auto_off(tmp_path) is None
            assert not _status(app).startswith("Auto OFF")
            # ... y confirms.
            await pilot.press("m", "o", "y")
            await _open(pilot)
            off = pause.read_auto_off(tmp_path)
            assert off is not None and off.by == "fleet"
            assert _status(app).startswith("Auto OFF")
            # Turning it back ON is immediate.
            await pilot.press("m", "o")
            await _open(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert pause.read_auto_off(tmp_path) is None
            # enter confirms too.
            await pilot.press("m", "o")
            await _open(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("enter")
            await _open(pilot)
            assert pause.read_auto_off(tmp_path) is not None

    async def test_mode_o_asks_before_turning_automatic_switching_off(self, tmp_path):
        from claude_swap.tui.fleet_modals import ModeModal
        from claude_swap.tui.modals import ConfirmModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("m", "m")
            await _open(pilot)
            assert isinstance(app.screen, ModeModal)
            await pilot.press("o")
            await _open(pilot)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("n")
            await _open(pilot)
            assert pause.read_auto_off(tmp_path) is None

    async def test_shortcut_letters_run_the_same_item_as_the_menu(self, tmp_path):
        from claude_swap.tui.fleet_strategy import StrategyScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, StrategyScreen)
            await pilot.press("b")
            await pilot.pause()
            screen = app.screen
            seen: list[str] = []
            screen.dispatch_menu = seen.append
            for key in menus.SHORTCUT_KEYS:
                await pilot.press(key)
                await pilot.pause()
            await pilot.press("g")  # the engine log's second key
            await pilot.pause()
            by_key = {e.key: e.action for e in menus.MAIN_MENU}
            assert seen == [by_key[k] for k in menus.SHORTCUT_KEYS] + ["engine"]

    async def test_q_quits_without_asking_when_no_live_engine_runs_here(self, tmp_path):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("q")
            await pilot.pause()
        assert app.return_code == 0
