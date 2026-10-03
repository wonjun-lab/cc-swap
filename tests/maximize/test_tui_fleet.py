"""The Fleet home screen: app wiring, the status sentence, the account
table and its tags, the table at five terminal sizes, selection and key
routing. Pilot tests against FakeSwitcher, temp backup roots, a fake
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

FOOTER = "enter switch · r re-login · l last resort · h hold · m menu · ? help · q quit"


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
    """One account's row of the table."""
    body = app.screen.query_one("#fx-body")
    first, count = body.layout_map.spans[number]
    return "\n".join(_body(app).splitlines()[first:first + count])


def _cell(app, number: str, key: str) -> str:
    """One cell of account ``number``'s row, by the screen's table plan."""
    plan = app.screen._plan
    x = plan.x(key)
    return _block(app, number)[x:x + plan.width(key)].strip()


def _decision(**fields) -> dict:
    return {
        "at": time.time() - 20, "pid": 4121, "active": "1", "decision": "switch",
        "trigger": "soft", "target": "2", "reason": "#1 5h 62% >= soft 50%; idle; -> #2",
        "pending": False, **fields,
    }


async def _open(pilot) -> None:
    await settle(pilot)
    await settle(pilot)


# -- six accounts like a real fleet, for the size tests -------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


#: Full emails, no aliases (``tools/fleet_screenshots.py``'s fleet).
EMAILS = [
    "dev.shared@example.com", "dev.master@example.com", "jordan.lee@example.com",
    "jordan.lee@uni.example", "dev.llm0@example.com", "nightowl@example.com",
]


def _six(root) -> FakeSwitcher:
    """#1 active past its 5h soft mark (with a Fable window); #2 next, 5h
    not started; #3 login ends in a day; #4 Team, last resort; #5 a dead
    login; #6 primed."""
    now = time.time()

    def window(pct, reset_in):
        return {"pct": pct, "resets_at": _iso(now + reset_in) if reset_in else None}

    def entry(p5, r5, p7, days7, *, fable=None, sentinel=None, age=40.0):
        last_good = {"five_hour": window(p5, r5), "seven_day": window(p7, days7 * 86400)}
        if fable is not None:
            last_good["scoped"] = [{"name": "Fable", **window(fable, days7 * 86400)}]
        return UsageEntry(sentinel=sentinel, last_good=last_good, fetched_at=now - age,
                          age_s=age)

    def account(n, usage, *, active=False, org="", login_days=None):
        return AccountSnapshot(
            number=str(n), email=EMAILS[n - 1], org_name=org,
            org_uuid="org-1" if org else "", is_active=active, kind="oauth", switchable=True,
            usage=usage, alias="",
            login_expires_at=(now + login_days * 86400) * 1000.0 if login_days else None,
        )

    (root / "settings.json").write_text(json.dumps({
        "schemaVersion": 1, "autoswitch": {"strategy": "maximize"},
        "maximize": {"lastResort": EMAILS[3], "soft5h": 50, "hard5h": 98,
                     "soft7d": 90, "hard7d": 98},
        "prime": {"enabled": True, "jitterS": "45-300"},
    }))
    reset6 = now + 3.3 * 3600
    _state(
        root,
        quarantine={"5": {"email": EMAILS[4], "reason": "invalid_grant"}},
        primes={EMAILS[5]: {"windowKey": "w", "attempts": 1,
                            "lastAttemptAt": reset6 - 5 * 3600 + 60, "lastOutcome": "primed"}},
        maximizeDecision=_decision(
            at=now - 50, decision="hold", trigger=None, pending=True,
            reason="#1 5h 62% >= soft 50%; waiting for idle to move to #2",
            plans={"1": "20x", "2": "20x", "3": "5x", "4": "team", "5": "5x", "6": "20x"},
        ),
    )
    return FakeSwitcher([
        account(1, entry(62.0, 1.8 * 3600, 41.0, 3.8, fable=38.0), active=True, login_days=21),
        account(2, entry(0.0, None, 35.0, 2.2), login_days=20),
        account(3, entry(22.0, 2.6 * 3600, 18.0, 5.4), login_days=1.2),
        account(4, entry(0.0, None, 30.0, 4.0), org="Acme Team", login_days=29),
        account(5, entry(0.0, None, 57.0, 1.6, sentinel=USAGE_RELOGIN_REQUIRED, age=9 * 3600)),
        account(6, entry(3.0, 3.3 * 3600, 22.0, 5.1), login_days=14),
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
            assert _block(app, "1").rstrip().endswith("● active")
            assert "62%" in _block(app, "1")
            assert _block(app, "2").rstrip().endswith("last resort")
            assert _block(app, "3").rstrip().endswith("re-login (r)")
            assert _cell(app, "3", "5h") in ("⚠ needs re-login", "⚠ re-login")
            assert _block(app, "4").rstrip().endswith("excluded")
            assert _cell(app, "4", "reset5") == "not started"
            orders = [_cell(app, n, "order") for n in screen._order]
            assert orders == ["●", "1", "–", "–"]  # #3 a dead login, #4 excluded

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
            assert _cell(app, "1", "plan") == "20x"
            assert _cell(app, "2", "plan") == "—"  # nothing says
            assert _block(app, "2").rstrip().endswith("next")

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


# -- the table at every size ---------------------------------------------------------------------------


SIZES = [(160, 45), (120, 36), (100, 30), (80, 24), (200, 16)]
HEADERS = ("order", "account", "5h", "5h resets", "7d", "7d resets", "status")
SIX_ORDER = ["1", "2", "6", "3", "4", "5"]


def _rows(app) -> dict[str, str]:
    return {n: _block(app, n) for n in app.screen._order}


@pytest.mark.asyncio
@pytest.mark.parametrize("size", SIZES, ids=[f"{w}x{h}" for w, h in SIZES])
async def test_every_size_shows_the_table_with_headers_and_both_resets(
    tmp_path, held_by_service, size
):
    width, height = size
    app = make_app(_six(tmp_path))
    async with app.run_test(size=size) as pilot:
        await _open(pilot)
        screen = app.screen
        plan = screen._plan
        status = screen.query_one("#fx-status")
        attention = screen.query_one("#fx-attention")
        head = screen.query_one("#fx-head")
        keys = screen.query_one("#fx-keys")
        scroll = screen.query_one("#fx-scroll")
        detail = screen.query_one("#fx-detail")
        summary = screen.query_one("#fx-summary")
        # The sentence, the attention line, the capacity summary, the
        # headers and the footer are on screen, outside the scroll; blank
        # lines only when tall enough.
        top = 1 if height >= 20 else 0
        assert status.region.y == top and attention.region.y == top + 1
        # 200x16 has the rows for the panel but not for the summary too:
        # the summary goes first.
        assert plan.summary is (height >= 20)
        if plan.summary:
            assert summary.display and summary.region.y == attention.region.y + 1 + top
            assert head.region.y == summary.region.y + 1
            assert _plain(app, "#fx-summary").startswith("5h free: ")
        else:
            assert not summary.display
            assert head.region.y == attention.region.y + 1 + top
        assert scroll.region.y == head.region.y + 1
        assert keys.region.y == height - 1 and scroll.region.bottom <= keys.region.y
        assert _status(app).startswith("Auto ON · ")
        assert "#2" in _status(app) and "when you pause" in _status(app)
        assert _plain(app, "#fx-attention").startswith(
            f"! #5 {EMAILS[4]} needs re-login — select it, press r"
        )
        assert _plain(app, "#fx-keys") == FOOTER
        # The headers, each over its column (plan only when it fits).
        header = _plain(app, "#fx-head")
        for word in HEADERS:
            assert word in header, word
        assert header.index("order") < header.index("account") < header.index("5h")
        assert header.index("7d resets") < header.index("status")
        assert header[plan.x("status"):].strip() == "status"
        # Every row: its order, both resets, its status right after them.
        assert screen._order == SIX_ORDER
        orders = {n: _cell(app, n, "order") for n in SIX_ORDER}
        assert orders == {"1": "●", "2": "1", "6": "2", "3": "3", "4": "4", "5": "–"}
        for number, line in _rows(app).items():
            assert len(line) <= width - 3
            assert _cell(app, number, "account").endswith(f"#{number}")
            assert _cell(app, number, "reset5"), number
            assert _cell(app, number, "reset7"), number
        assert _cell(app, "2", "reset5") == "not started"
        assert _cell(app, "4", "reset5") == "not started"
        assert _cell(app, "5", "reset5") == "—"            # dead login, 5h unknown
        assert _cell(app, "5", "reset7").startswith("1d")  # its last reading's 7d
        assert _cell(app, "1", "reset7").startswith("3d")
        tags = {n: _cell(app, n, "status") for n in SIX_ORDER}
        assert tags == {"1": "● active", "2": "next", "6": "primed", "3": "login 1d left",
                        "4": "last resort", "5": "re-login (r)"}
        # No tag at the terminal's right edge when the table is narrower.
        if plan.total < plan.room:
            assert all(len(line.rstrip()) < width - 3 for line in _rows(app).values())
        if width >= 160:
            assert all(len(line.rstrip()) <= plan.total for line in _rows(app).values())
            assert _cell(app, "1", "account") == f"{EMAILS[0]} #1"  # whole names
        # The selected account in full under the table, when it fits.
        assert detail.display
        assert detail.region.y == scroll.region.bottom
        assert detail.region.bottom <= keys.region.y
        panel = _plain(app, "#fx-detail")
        assert f"{EMAILS[0]} #1  personal · 20x  ● active" in panel
        assert "Fable" in panel and "Fable" not in header  # per-model: the panel only
        assert "login ends " in panel


@pytest.mark.asyncio
async def test_the_table_follows_a_live_resize(tmp_path, held_by_service):
    app = make_app(_six(tmp_path))
    async with app.run_test(size=(160, 45)) as pilot:
        await _open(pilot)
        screen = app.screen
        await pilot.press("down")  # the row below #1
        await pilot.pause()
        assert screen._sel == "2" and screen._plan.clock
        for size, clock in (((120, 36), True), ((80, 24), False), ((200, 16), True),
                            ((160, 45), True)):
            await pilot.resize_terminal(*size)
            await _open(pilot)
            assert screen._plan.clock is clock
            assert screen._sel == "2"  # the selection survives the change
            assert _status(app).startswith("Auto ON · ")
            assert screen.query_one("#fx-status").region.y == (1 if size[1] >= 20 else 0)
            assert screen.query_one("#fx-keys").region.y == size[1] - 1
            assert "7d resets" in _plain(app, "#fx-head")
            assert all(len(line) <= size[0] - 3 for line in _body(app).splitlines())
            assert EMAILS[1] in _plain(app, "#fx-detail")


@pytest.mark.asyncio
@pytest.mark.parametrize("height", [14, 10, 8, 6])
async def test_a_very_short_terminal_drops_the_panel_but_keeps_the_table(
    tmp_path, held_by_service, height
):
    app = make_app(_six(tmp_path))
    async with app.run_test(size=(80, height)) as pilot:
        await _open(pilot)
        screen = app.screen
        status = screen.query_one("#fx-status")
        keys = screen.query_one("#fx-keys")
        # The mouse wheel never moves the screen itself, only the accounts.
        assert not screen.allow_vertical_scroll and screen.scroll_offset.y == 0
        assert status.region.y == 0 and status.region.height == 1  # no blank lines
        assert keys.region.y == height - 1
        assert screen.query_one("#fx-scroll").region.height >= 2
        assert not screen.query_one("#fx-detail").display  # it goes first
        assert screen.query_one("#fx-head").display and screen._plan is not None


@pytest.mark.asyncio
async def test_200x16_is_still_the_table(tmp_path, held_by_service):
    """Wide but short (the real install's terminal): the table with its
    headers and every reset, no blank lines; the panel only when it fits."""
    app = make_app(_six(tmp_path))
    async with app.run_test(size=(200, 16)) as pilot:
        await _open(pilot)
        screen = app.screen
        assert screen.has_class("-compact")
        assert screen._plan.clock and screen._plan.plan and screen._plan.bar == 24
        assert screen.query_one("#fx-head").region.y == 2
        assert all(_cell(app, n, "reset5") and _cell(app, n, "reset7") for n in SIX_ORDER)
        assert screen._plan.total < screen._plan.room


@pytest.mark.asyncio
async def test_80x24_with_many_accounts_scrolls_the_table_but_never_the_header(tmp_path):
    _settings(tmp_path)
    accounts = [make_account(1, active=True, entry=make_entry(30.0, 20.0), alias="main")]
    accounts += [make_account(n, entry=make_entry(5.0 + n, 10.0)) for n in range(2, 26)]
    app = make_app(FakeSwitcher(accounts, tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(pilot)
        screen = app.screen
        scroll = screen.query_one("#fx-scroll")
        head = screen.query_one("#fx-head")
        head_y = head.region.y
        for _ in range(24):
            await pilot.press("down")
        await _open(pilot)
        last = screen._order[-1]
        assert screen._sel == last
        first, _count = screen.query_one("#fx-body").layout_map.spans[last]
        top = scroll.scroll_offset.y
        assert top > 0
        assert top <= first < top + scroll.scrollable_content_region.height
        assert screen.query_one("#fx-status").region.y == 1
        assert head.region.y == head_y and "order" in _plain(app, "#fx-head")
        assert _plain(app, "#fx-keys") == FOOTER
        assert screen.query_one("#fx-keys").region.y == 23
        assert not screen.query_one("#fx-detail").display  # 25 rows: no room for it


# -- keys -----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestKeys:
    async def test_arrows_move_the_selection_and_a_click_selects(self, tmp_path):
        _settings(tmp_path)
        app = make_app(_six(tmp_path))
        async with app.run_test(size=(160, 45)) as pilot:
            await _open(pilot)
            screen = app.screen
            assert screen._order == SIX_ORDER
            await pilot.press("down")      # one row down the table
            await pilot.pause()
            assert screen._sel == "2"
            await pilot.press("j", "j")
            await pilot.pause()
            assert screen._sel == "3"
            await pilot.press("right")     # no columns to move across
            await pilot.pause()
            assert screen._sel == "3"
            await pilot.press("k")
            await pilot.pause()
            assert screen._sel == "6"
            body = screen.query_one("#fx-body")
            first, _ = body.layout_map.spans["5"]
            await pilot.click("#fx-body", offset=(60, first))
            await pilot.pause()
            assert screen._sel == "5"
            assert EMAILS[4] in _plain(app, "#fx-detail")

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


# -- h: hold the active account ------------------------------------------------------------------


@pytest.mark.asyncio
class TestHoldKey:
    async def test_h_holds_the_active_account_and_the_sentence_says_so(
        self, tmp_path, held_by_service
    ):
        from claude_swap.maximize import hold
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.fleet_modals import MenuModal

        app = make_app(_six(tmp_path))
        async with app.run_test(size=(200, 40)) as pilot:
            await _open(pilot)
            await pilot.press("down", "down")  # the selection never matters: the active one
            await pilot.press("h")
            await _open(pilot)
            assert isinstance(app.screen, MenuModal)
            assert app.screen._title == f"Hold #1 {EMAILS[0]} — stay on this account"
            started = time.time()
            await pilot.press("2")
            await _open(pilot)
            assert isinstance(app.screen, FleetScreen)
            held = hold.read_hold(tmp_path, now=time.time())
            assert held.slot == "1" and held.by == "fleet"
            assert started + 7200 - 5 <= held.until <= time.time() + 7200
            status = _status(app)
            assert status.startswith(f"Holding #1 {EMAILS[0]} until ")
            assert "(2h left) — only hard 98%/100% will move you (h to change)" in status
            # h → o lifts it; the sentence goes back to the engine's word.
            await pilot.press("h", "o")
            await _open(pilot)
            assert hold.read_hold(tmp_path, now=time.time()) is None
            assert _status(app).startswith("Auto ON · ")

    async def test_h_until_a_time(self, tmp_path, held_by_service):
        from claude_swap.maximize import hold
        from claude_swap.tui.fleet_modals import TextInputModal

        app = make_app(_six(tmp_path))
        async with app.run_test(size=(160, 45)) as pilot:
            await _open(pilot)
            await pilot.press("h", "u")
            await _open(pilot)
            assert isinstance(app.screen, TextInputModal)
            await pilot.press(*"23:00", "enter")
            await _open(pilot)
            held = hold.read_hold(tmp_path, now=time.time())
            assert held is not None
            assert held.until == hold.parse_until("23:00", held.since)
            # A time it cannot read changes nothing.
            await pilot.press("h", "u")
            await _open(pilot)
            await pilot.press(*"later", "enter")
            await _open(pilot)
            assert hold.read_hold(tmp_path, now=time.time()) == held

    async def test_esc_closes_the_picker_without_a_hold(self, tmp_path, held_by_service):
        from claude_swap.maximize import hold
        from claude_swap.tui.fleet import FleetScreen

        app = make_app(_six(tmp_path))
        async with app.run_test(size=(160, 45)) as pilot:
            await _open(pilot)
            await pilot.press("h", "escape")
            await _open(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert not (tmp_path / hold.HOLD_FILENAME).exists()

    async def test_question_mark_is_help_and_h_is_not(self, tmp_path):
        from claude_swap.tui.fleet_help import HelpScreen
        from claude_swap.tui.fleet_modals import MenuModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape", "h")
            await pilot.pause()
            assert isinstance(app.screen, MenuModal)


@pytest.mark.asyncio
async def test_the_capacity_summary_sits_over_the_headers(tmp_path, held_by_service):
    app = make_app(_six(tmp_path))
    async with app.run_test(size=(160, 45)) as pilot:
        await _open(pilot)
        screen = app.screen
        summary = _plain(app, "#fx-summary")
        # Usable: #1 (62% 5h, past soft), #2, #3, #4, #6 (#5's login is dead).
        assert summary.startswith("5h free: 4 accounts · next 5h back ")
        assert "(#1) · 7d left this week ≈ 3.5 accounts · next 7d reset " in summary
        assert screen.query_one("#fx-head").region.y == screen.query_one(
            "#fx-summary").region.y + 1
        await pilot.resize_terminal(80, 24)
        await _open(pilot)
        assert _plain(app, "#fx-summary") == "5h free: 4 accounts · 7d left this week ≈ 3.5 accounts"
        await pilot.resize_terminal(80, 10)
        await _open(pilot)
        assert not screen.query_one("#fx-summary").display
