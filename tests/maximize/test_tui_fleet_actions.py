"""Fleet actions: the account keys (enter / l / x / r), Prime now, Swap
strategy, Account settings and the guided re-login. Pilot tests against
fakes in temp backup roots: no real claude, no Keychain, no service
manager."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
from textual.widgets import DataTable, ListView, RichLog, SelectionList, Static

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.maximize.fleet_actions import relogin_store
from claude_swap.usage_store import UsageEntry
from tests.maximize.test_tui_fleet import _cold, _settings
from tests.test_tui import FakeSwitcher, make_account, make_app, make_entry, settle


class IdentitySwitcher(FakeSwitcher):
    """FakeSwitcher plus the identity surface ``relogin_store`` reads: the
    slot records and the live ``.claude.json`` identity."""

    def __init__(self, accounts, backup_dir, *, live=None, live_rt=None):
        super().__init__(accounts, backup_dir)
        self.live = live  # (email, org_uuid, account_uuid) or None
        self.live_rt = live_rt  # the live login's refresh token, or None

    def _read_credentials(self):
        if self.live_rt is None:
            return None
        return json.dumps({"claudeAiOauth": {"accessToken": "at", "refreshToken": self.live_rt}})

    def _get_sequence_data(self):
        return {"accounts": {
            a.number: {"email": a.email, "organizationUuid": a.org_uuid,
                       "uuid": f"uuid-{a.number}", "alias": a.alias}
            for a in self._accounts
        }}

    def _get_current_identity_triple(self):
        return self.live

    @staticmethod
    def _find_account_slot(data, email, org):
        for num, rec in data.get("accounts", {}).items():
            if rec.get("email") == email and rec.get("organizationUuid", "") == org:
                return num
        return None


def _accounts():
    return [
        make_account(1, active=True, entry=make_entry(62.0, 40.0), alias="main"),
        make_account(2, entry=_cold(20.0)),
        make_account(3, entry=make_entry(48.0, 20.0)),
        make_account(4, entry=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED), alias="old"),
    ]


def _fleet(root, **kw) -> IdentitySwitcher:
    return IdentitySwitcher(_accounts(), root, **kw)


def _maximize(root) -> dict:
    return json.loads((root / "settings.json").read_text()).get("maximize", {})


def _toasts(app) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    def notify(message, *, title="", severity="information", timeout=None, markup=True):
        seen.append((str(message), severity))

    app.notify = notify
    return seen


async def _open(pilot) -> None:
    await settle(pilot)
    await settle(pilot)


async def _to_row(pilot, number: str) -> None:
    """Select account ``number`` on the Fleet home screen."""
    pilot.app.screen.select(number)
    await pilot.pause()


def _tag(app, number: str) -> str:
    """The right-aligned tag on account ``number``'s first line."""
    from claude_swap.maximize import home
    from claude_swap.tui.fleet_render import Ctx

    screen = app.screen
    row = next(r for r in screen._rows if r.number == number)
    ctx = Ctx(screen._palette(), {}, time.time(), next_no=None,
              priming=screen._priming(screen._situation))
    tag = home.tag_for(row, is_next=False, now=ctx.now, priming=ctx.priming)
    first = screen.query_one("#fx-body").layout_map.spans[number][0]
    line = screen.query_one("#fx-body", Static).render().plain.splitlines()[first]
    assert tag is None or line.rstrip().endswith(tag[0])
    return tag[0] if tag else ""


@pytest.mark.asyncio
class TestRowKeys:
    async def test_enter_switches_landable_target_without_confirm(self, tmp_path):
        _settings(tmp_path)
        fake = _fleet(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "2")
            await pilot.press("enter")
            await _open(pilot)
            assert ("switch_to", "2") in fake.calls

    async def test_enter_on_non_landable_asks_first(self, tmp_path):
        from claude_swap.tui.modals import ConfirmModal

        _settings(tmp_path)
        fake = _fleet(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "3")  # 5h 48% >= soft 50 - margin 5
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            assert "may move you again" in app.screen.query_one(".modal-body", Static).render().plain
            assert not any(c[0] == "switch_to" for c in fake.calls)
            await pilot.press("y")
            await _open(pilot)
            assert ("switch_to", "3") in fake.calls

    async def test_l_toggles_last_resort_in_settings_json(self, tmp_path):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "2")
            await pilot.press("l")
            await _open(pilot)
            assert _maximize(tmp_path) == {"lastResort": "user2@example.com"}
            assert _tag(app, "2") == "last resort"
            await pilot.press("l")
            await _open(pilot)
            assert _maximize(tmp_path) == {}

    async def test_l_on_shared_email_without_alias_shows_error_toast(self, tmp_path):
        _settings(tmp_path)
        accounts = _accounts()
        accounts[1] = dataclasses.replace(accounts[1], email="shared@example.com")
        accounts[2] = dataclasses.replace(accounts[2], email="shared@example.com")
        app = make_app(IdentitySwitcher(accounts, tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            toasts = _toasts(app)
            await _to_row(pilot, "2")
            await pilot.press("l")
            await _open(pilot)
            assert any("alias first" in m and sev == "error" for m, sev in toasts)
            assert _maximize(tmp_path) == {}

    async def test_x_toggles_disabled_and_tier_shows_excl(self, tmp_path):
        _settings(tmp_path)
        fake = _fleet(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "2")
            await pilot.press("x")
            await _open(pilot)
            assert ("set_disabled", "2", True) in fake.calls
            assert _tag(app, "2") == "excluded"

    async def test_r_on_healthy_account_says_nothing_to_fix(self, tmp_path):
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            toasts = _toasts(app)
            await _to_row(pilot, "2")
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)
            assert ("#2 login works — nothing to fix", "information") in toasts


@pytest.mark.asyncio
class TestPrime:
    async def test_p_opens_prime_modal_with_skip_reasons_and_runs_selected(
        self, tmp_path, monkeypatch
    ):
        from claude_swap.tui.fleet_modals import PrimeModal

        calls: list = []

        class Report:
            def lines(self):
                return ["#2  verification pending (fake)"]

        def fake_manual_prime(switcher, numbers, *, dry_run, emit, **kw):
            calls.append((numbers, dry_run))
            return Report()

        monkeypatch.setattr("claude_swap.tui.fleet_modals.manual_prime", fake_manual_prime)
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "2")
            await pilot.press("m", "p")
            await pilot.pause()
            assert isinstance(app.screen, PrimeModal)
            options = app.screen.query_one("#fx-prime-list", SelectionList)
            labels = [str(options.get_option_at_index(i).prompt)
                      for i in range(options.option_count)]
            assert any(lbl.startswith("#1 main") and "skip (active)" in lbl for lbl in labels)
            assert any(lbl.startswith("#2") and "would prime now" in lbl for lbl in labels)
            assert any("skip (window-on)" in lbl for lbl in labels)
            assert options.selected == ["2"]
            await pilot.press("enter")
            await _open(pilot)
            assert calls == [({"2"}, False)]
            log = app.screen.query_one("#fx-prime-log", RichLog)
            assert any("verification pending (fake)" in line.text for line in log.lines)


@pytest.mark.asyncio
class TestStrategy:
    async def _open_strategy(self, pilot):
        from claude_swap.tui.fleet_strategy import StrategyScreen

        await _open(pilot)
        await pilot.press("m", "s")
        await pilot.pause()
        assert isinstance(pilot.app.screen, StrategyScreen)
        return pilot.app.screen

    async def test_strategy_save_writes_only_changed_keys_in_valid_order(self, tmp_path):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 44)) as pilot:
            await self._open_strategy(pilot)
            await pilot.press("right", "right")              # 5h soft 52
            await pilot.press("down", "down", "down", "down", "right")  # margin 6
            await pilot.pause()
            assert "*" in app.screen.query_one("#fx-st-body", Static).render().plain
            assert _maximize(tmp_path) == {}
            await pilot.press("s")
            await pilot.pause()
            assert _maximize(tmp_path) == {"soft5h": 52.0, "landingMargin": 6.0}
            assert app.window_ticks["5h"] == (52.0, 95.0)

    async def test_strategy_back_with_unsaved_edits_asks_once(self, tmp_path):
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.fleet_strategy import StrategyScreen

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 44)) as pilot:
            await self._open_strategy(pilot)
            toasts = _toasts(app)
            await pilot.press("right", "b")
            await pilot.pause()
            assert isinstance(app.screen, StrategyScreen)
            assert any("Unsaved changes" in m for m, _ in toasts)
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)
            assert _maximize(tmp_path) == {}

    async def test_strategy_preview_line_tracks_edits(self, tmp_path):
        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 44)) as pilot:
            screen = await self._open_strategy(pilot)

            def preview() -> str:
                return screen.query_one("#fx-st-preview", Static).render().plain

            assert preview().startswith("with these values: HOLD — waiting for idle")
            assert preview().endswith("saved values: same")
            for _ in range(15):                               # 5h soft 65: under it
                await pilot.press("right")
            await pilot.pause()
            assert preview().startswith("with these values: HOLD · ")
            assert "saved values: HOLD — waiting for idle" in preview()


@pytest.mark.asyncio
class TestAccounts:
    async def test_accounts_pick_mode_asks_which_account(self, tmp_path):
        from claude_swap.tui.fleet_accounts import AccountsScreen
        from claude_swap.tui.fleet_modals import ReloginModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("m", "a")
            await _open(pilot)
            screen = app.screen
            assert isinstance(screen, AccountsScreen)
            table = screen.query_one("#fx-ac-table", DataTable)
            assert "re-login needed (refresh token dead)" in [
                c.plain for c in table.get_row("4")
            ]
            menu = screen.query_one("#fx-ac-menu", ListView)
            menu.focus()
            menu.index = 2  # Re-login…
            await pilot.press("enter")
            await pilot.pause()
            prompt = screen.query_one("#fx-ac-prompt", Static).render().plain
            assert prompt.startswith("Which account to re-login?")
            assert app.focused is table
            await pilot.press("escape")  # cancels the pick, stays here
            await pilot.pause()
            assert isinstance(app.screen, AccountsScreen)
            assert screen.query_one("#fx-ac-prompt", Static).render().plain == ""
            menu.focus()
            menu.index = 2
            await pilot.press("enter")
            await pilot.pause()
            table.move_cursor(row=table.get_row_index("4"))
            await pilot.press("enter")
            await _open(pilot)
            assert isinstance(app.screen, ReloginModal)


@pytest.mark.asyncio
class TestRelogin:
    async def test_relogin_modal_pauses_the_engine_and_cancel_resumes(self, tmp_path):
        from claude_swap.tui.fleet_modals import ReloginModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        state = tmp_path / "autoswitch_state.json"
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "4")
            await pilot.press("r")
            await _open(pilot)
            assert isinstance(app.screen, ReloginModal)
            steps = app.screen.query_one("#fx-relogin-steps", Static).render().plain
            assert "/login" in steps and "user4@example.com" in steps
            assert "~/.local/bin/claude" not in steps  # nothing resolvable: plain claude
            paused = json.loads(state.read_text())
            assert paused["pausedReason"] == "relogin"
            assert 0 < paused["pausedUntil"] - time.time() <= 600
            await pilot.press("escape")
            await _open(pilot)
            assert "pausedUntil" not in json.loads(state.read_text())

    async def test_expiring_login_is_flagged_and_r_renews_it_early(self, tmp_path):
        from claude_swap.tui.fleet_modals import ReloginModal

        _settings(tmp_path)
        accounts = _accounts()
        deadline_ms = (time.time() + 2 * 86400 + 600) * 1000
        accounts[1] = dataclasses.replace(accounts[1], login_expires_at=deadline_ms)
        app = make_app(IdentitySwitcher(accounts, tmp_path))
        async with app.run_test(size=(160, 40)) as pilot:
            await _open(pilot)
            attention = app.screen.query_one("#fx-attention", Static).render().plain
            assert "#2 user2@example.com login ends in 2d 0h" in attention
            assert _tag(app, "2") == "login 2d left"
            await _to_row(pilot, "2")
            await pilot.press("r")
            await _open(pilot)
            assert isinstance(app.screen, ReloginModal)
            steps = app.screen.query_one("#fx-relogin-steps", Static).render().plain
            assert "(in 2d 0h)" in steps and "new ~30-day deadline" in steps
            await pilot.press("escape")
            await _open(pilot)

    async def test_quit_while_the_relogin_modal_is_open_lifts_the_pause(self, tmp_path):
        from claude_swap.tui.fleet_modals import ReloginModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        state = tmp_path / "autoswitch_state.json"
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "4")
            await pilot.press("r")
            await _open(pilot)
            assert isinstance(app.screen, ReloginModal)
            assert "pausedUntil" in json.loads(state.read_text())
            app.exit()
            await pilot.pause()
        assert "pausedUntil" not in json.loads(state.read_text())

    async def test_open_modal_renews_the_pause_and_a_late_renewal_never_repauses(
        self, tmp_path, monkeypatch
    ):
        from claude_swap.tui import fleet_modals
        from claude_swap.tui.fleet_modals import ReloginModal

        _settings(tmp_path)
        app = make_app(_fleet(tmp_path))
        state = tmp_path / "autoswitch_state.json"
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "4")
            await pilot.press("r")
            await _open(pilot)
            modal = app.screen
            assert isinstance(modal, ReloginModal)
            first = json.loads(state.read_text())["pausedUntil"]
            later = time.time() + 400
            monkeypatch.setattr(fleet_modals, "_now", lambda: later)
            modal._renew()
            await _open(pilot)
            renewed = json.loads(state.read_text())["pausedUntil"]
            assert renewed == later + 600 > first  # 10 min past the renewal
            await pilot.press("escape")
            await _open(pilot)
            assert "pausedUntil" not in json.loads(state.read_text())
            modal._pause_blocking()  # a renewal that was already running
            assert "pausedUntil" not in json.loads(state.read_text())

    async def test_relogin_modal_refuses_until_a_new_login_lands(self, tmp_path):
        from claude_swap.tui.fleet import FleetScreen
        from claude_swap.tui.fleet_modals import ReloginModal

        _settings(tmp_path)
        state = tmp_path / "autoswitch_state.json"
        fake = _fleet(tmp_path, live=("user4@example.com", "", "uuid-4"), live_rt="rt-old")
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            toasts = _toasts(app)
            await _to_row(pilot, "4")
            await pilot.press("r")
            await _open(pilot)
            modal = app.screen
            assert isinstance(modal, ReloginModal)
            await pilot.press("enter")  # nothing changed since the modal opened
            await _open(pilot)
            assert app.screen is modal  # stays open, still paused
            status = modal.query_one("#fx-relogin-status", Static).render().plain
            assert status.startswith("no new login yet")
            assert "rt-old" not in status
            assert ("add", None, True) not in fake.calls
            assert "pausedUntil" in json.loads(state.read_text())
            fake.live_rt = "rt-new"  # the user ran /login
            await pilot.press("enter")
            await _open(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert ("add", None, True) in fake.calls
            assert ("#4 login stored; back on #1", "information") in toasts

    async def test_relogin_modal_stores_the_right_login_and_switches_back(self, tmp_path):
        from claude_swap.tui.fleet import FleetScreen

        _settings(tmp_path)
        fake = _fleet(tmp_path, live=("user4@example.com", "", "uuid-4"))
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            toasts = _toasts(app)
            await _to_row(pilot, "4")
            await pilot.press("r")
            await _open(pilot)
            await pilot.press("enter")
            await _open(pilot)
            assert isinstance(app.screen, FleetScreen)
            assert ("add", None, True) in fake.calls
            assert ("switch_to", "1") in fake.calls
            assert ("#4 login stored; back on #1", "information") in toasts
            state = json.loads((tmp_path / "autoswitch_state.json").read_text())
            assert "pausedUntil" not in state


def test_relogin_store_requires_a_new_login(tmp_path):
    from claude_swap.maximize.fleet_actions import live_login_fingerprint

    fake = _fleet(tmp_path, live=("user4@example.com", "", "uuid-4"), live_rt="rt-old")
    before = live_login_fingerprint(fake)
    assert before and "rt-old" not in before
    result = relogin_store(fake, "4", return_to="1", before=before)
    assert result["stored"] is False
    assert result["reason"].startswith("no new login yet")
    assert fake.calls == []
    fake.live_rt = "rt-new"  # the user ran /login
    assert relogin_store(fake, "4", return_to="1", before=before)["stored"] is True
    # Unknown baseline (nothing readable when the modal opened): not refused.
    fake.calls.clear()
    assert relogin_store(fake, "4", return_to="1", before=None)["stored"] is True


def test_relogin_store_refuses_when_live_login_is_another_slot(tmp_path):
    fake = _fleet(tmp_path, live=("user2@example.com", "", "uuid-2"))
    result = relogin_store(fake, "4", return_to="1")
    assert result == {
        "stored": False, "number": "4",
        "reason": "the live login is #2, not #4; nothing stored",
    }
    assert fake.calls == []
    # Same email, other account uuid (a different person's org seat): refused too.
    fake.live = ("user4@example.com", "", "uuid-someone-else")
    assert relogin_store(fake, "4", return_to="1")["stored"] is False
    fake.live = None
    assert "no live Claude Code login" in relogin_store(fake, "4", return_to="1")["reason"]
    fake.live = ("stranger@example.com", "", "uuid-x")
    assert "an account cc-swap does not manage" in relogin_store(fake, "4", return_to=None)["reason"]
    assert fake.calls == []


def test_relogin_store_refreshes_in_place_and_switches_back(tmp_path):
    fake = _fleet(tmp_path, live=("user4@example.com", "", "uuid-4"))
    result = relogin_store(fake, "4", return_to="1")
    assert result == {"stored": True, "number": "4", "returned_to": "1"}
    assert fake.calls == [("add", None, True), ("switch_to", "1")]
    # Re-logging the active account in: nothing to switch back to.
    fake.calls.clear()
    assert relogin_store(fake, "4", return_to="4") == {"stored": True, "number": "4"}
    assert fake.calls == [("add", None, True)]


class BackingUpSwitcher(IdentitySwitcher):
    def __init__(self, *a, verdict=(True, ""), **kw):
        super().__init__(*a, **kw)
        self.verdict = verdict
        self.synced: list[str | None] = []

    def sync_active_backup(self, *, skip_number=None):
        self.synced.append(skip_number)
        return self.verdict


@pytest.mark.asyncio
class TestReloginBacksUpTheActiveAccountFirst:
    async def _open_relogin(self, pilot):
        await _open(pilot)
        await _to_row(pilot, "4")
        await pilot.press("r")
        await _open(pilot)

    async def test_refuses_to_start_when_the_active_login_cannot_be_backed_up(self, tmp_path):
        from claude_swap.tui.fleet_modals import ReloginModal

        _settings(tmp_path)
        fake = BackingUpSwitcher(
            _accounts(), tmp_path, live=("user4@example.com", "", "uuid-4"), live_rt="rt-x",
            verdict=(False, "#1's current login could not be backed up"),
        )
        app = make_app(fake)
        state = tmp_path / "autoswitch_state.json"
        async with app.run_test(size=(140, 40)) as pilot:
            await self._open_relogin(pilot)
            modal = app.screen
            assert isinstance(modal, ReloginModal)
            steps = modal.query_one("#fx-relogin-steps", Static).render().plain
            assert steps.startswith("Not starting the re-login: #1's current login")
            assert "/login" not in steps
            assert not state.exists() or "pausedUntil" not in json.loads(state.read_text())
            fake.live_rt = "rt-new"
            await pilot.press("enter")
            await _open(pilot)
            assert app.screen is modal and ("add", None, True) not in fake.calls
            await pilot.press("escape")
            await _open(pilot)
        assert fake.synced == ["4"]

    async def test_starts_once_the_active_login_is_backed_up(self, tmp_path):
        from claude_swap.tui.fleet_modals import ReloginModal

        _settings(tmp_path)
        fake = BackingUpSwitcher(_accounts(), tmp_path)
        app = make_app(fake)
        state = tmp_path / "autoswitch_state.json"
        async with app.run_test(size=(140, 40)) as pilot:
            await self._open_relogin(pilot)
            assert isinstance(app.screen, ReloginModal)
            steps = app.screen.query_one("#fx-relogin-steps", Static).render().plain
            assert "/login" in steps
            assert json.loads(state.read_text())["pausedReason"] == "relogin"
            await pilot.press("escape")
            await _open(pilot)
        assert fake.synced == ["4"]
