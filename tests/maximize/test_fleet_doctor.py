"""Fleet → Account settings → ``v`` (Verify logins): ``cc-swap doctor`` in a
modal, with a fake doctor runner (no real Keychain, service or claude)."""

from __future__ import annotations

import pytest

from claude_swap.maximize import doctor as dr


@pytest.mark.asyncio
async def test_fleet_v_opens_verify_logins_with_the_findings(tmp_path, monkeypatch):
    from textual.widgets import RichLog

    from claude_swap.tui import fleet_doctor
    from claude_swap.tui.fleet_accounts import AccountsScreen
    from tests.maximize.test_tui_fleet import _settings
    from tests.maximize.test_tui_fleet_actions import _fleet, _open
    from tests.test_tui import make_app

    calls = []

    def fake():
        calls.append(1)
        return [
            dr.Finding("keychain", "error", "live login unreadable — rc=36", "unlock it"),
            dr.Finding("login-deadline", "warn", "login expires in 2d 1h", "re-login #3", "#3"),
            dr.Finding("upstream", "ok", "no upstream claude-swap installed or running"),
        ]

    monkeypatch.setattr(fleet_doctor, "run_doctor", fake)
    _settings(tmp_path)
    app = make_app(_fleet(tmp_path))
    async with app.run_test(size=(140, 40)) as pilot:
        await _open(pilot)
        await pilot.press("a")
        await _open(pilot)
        assert isinstance(app.screen, AccountsScreen)
        keys = app.screen.query_one("#fx-ac-keys").render().plain
        assert "v verify" in keys
        await pilot.press("v")
        await _open(pilot)
        modal = app.screen
        assert isinstance(modal, fleet_doctor.DoctorModal)
        assert calls == [1]
        text = "\n".join(line.text for line in modal.query_one("#fx-doctor-log", RichLog).lines)
        assert text.index("ERROR") < text.index("WARN") < text.index("ok ")
        assert "#3 login-deadline: login expires in 2d 1h" in text
        assert "fix: unlock it" in text
        await pilot.press("r")
        await _open(pilot)
        assert calls == [1, 1]
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, AccountsScreen)


def test_finding_lines_put_problems_first_and_never_show_ok_fixes():
    from claude_swap.tui.fleet_doctor import finding_lines

    lines = [t.plain for t in finding_lines([
        dr.Finding("upstream", "ok", "fine", "never shown"),
        dr.Finding("stored-login", "error", "no stored login", "cc-swap add --slot 2", "#2"),
    ])]
    assert lines[0] == "ERROR  #2 stored-login: no stored login"
    assert lines[1] == "       fix: cc-swap add --slot 2"
    assert lines[2] == "ok     upstream: fine"
    assert "never shown" not in "\n".join(lines)
    assert lines[-1].startswith("1 error(s) · 0 warning(s)")
