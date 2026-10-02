"""A switch never runs on a degraded read of the live credential.

2026-10-03: right after a ``/login`` the macOS Keychain answered rc=36
(errSecInteractionNotAllowed) for minutes. ``_perform_switch`` read the live
credential through ``_read_credentials`` — which drops the ``degraded`` flag —
so it backed up the stale plaintext mirror and overwrote the Keychain item
holding the only copy of the new login. macOS is faked in a temp HOME on the
in-memory Keychain (``block_real_keychain``); nothing touches the real one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain as kc
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE
from claude_swap.exceptions import CredentialReadError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

OLD1 = json.dumps({"claudeAiOauth": {"accessToken": "sk-old-1", "refreshToken": "rt-old-1",
                                     "expiresAt": 1_000}})
NEW1 = json.dumps({"claudeAiOauth": {"accessToken": "sk-NEW-1", "refreshToken": "rt-NEW-1",
                                     "expiresAt": 9_999_999_999_000}})
CRED2 = json.dumps({"claudeAiOauth": {"accessToken": "sk-2", "refreshToken": "rt-2",
                                      "expiresAt": 9_999_999_999_000}})
MESSAGE = "Keychain unreadable right now — not switching"


def _seed(sw, num, email, creds):
    sw._write_account_credentials(str(num), email, creds)
    sw._write_account_config(str(num), email, json.dumps(
        {"oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"}}))
    data = sw._get_sequence_data()
    data["accounts"][str(num)] = {"email": email, "uuid": f"uuid-{num}",
                                  "organizationUuid": "", "organizationName": "",
                                  "added": "2024-01-01T00:00:00Z"}
    data["sequence"] = sorted(set(data["sequence"]) | {num})
    if data["activeAccountNumber"] is None:
        data["activeAccountNumber"] = num
    sw._write_json(sw.sequence_file, data)


@pytest.fixture
def mac(temp_home: Path, block_real_keychain):
    sw = ClaudeAccountSwitcher()
    sw.platform = Platform.MACOS
    sw._setup_directories()
    sw._init_sequence_file()
    _seed(sw, 1, "a@example.com", OLD1)
    _seed(sw, 2, "b@example.com", CRED2)
    (temp_home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-1"}}))
    # Stale plaintext mirror (CC rotates / logs in keychain-only on macOS).
    (temp_home / ".claude" / ".credentials.json").write_text(OLD1)
    # The fresh /login lives only in the Keychain.
    block_real_keychain.set_password(
        CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name(), NEW1)
    return sw, temp_home, block_real_keychain


def rc36_on_active(store, monkeypatch, *, delete_fails: bool = False) -> None:
    real_get, real_del = store.get_password, store.delete_password

    def get(service, account):
        if service == CLAUDE_CODE_KEYCHAIN_SERVICE:
            raise kc.KeychainError("rc=36 errSecInteractionNotAllowed")
        return real_get(service, account)

    def delete(service, account):
        if delete_fails and service == CLAUDE_CODE_KEYCHAIN_SERVICE:
            raise kc.KeychainError("rc=36")
        return real_del(service, account)

    monkeypatch.setattr(kc, "get_password", get)
    monkeypatch.setattr(kc, "delete_password", delete)


def _new_login_still_in_keychain(store) -> bool:
    return any("rt-NEW-1" in v for v in store.data.values())


@pytest.mark.parametrize("force", [False, True], ids=["switch", "force"])
@pytest.mark.parametrize("delete_fails", [False, True])
def test_switch_refuses_during_an_rc36_window(mac, monkeypatch, delete_fails, force):
    sw, home, store = mac
    rc36_on_active(store, monkeypatch, delete_fails=delete_fails)
    assert sw._read_active_credentials().degraded  # premise: degraded plaintext read
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        with pytest.raises(CredentialReadError, match=MESSAGE):
            sw.switch_to("2", json_output=True, force=force)
    assert _new_login_still_in_keychain(store)
    assert sw._read_account_credentials("1", "a@example.com") == OLD1
    assert sw.current_account_number() == "1"


def test_degraded_switch_never_poisons_a_healthy_backup(mac, monkeypatch):
    sw, home, store = mac
    cur = json.dumps({"claudeAiOauth": {"accessToken": "sk-cur-1", "refreshToken": "rt-cur-1",
                                        "expiresAt": 9_999_999_999_000}})
    older = json.dumps({"claudeAiOauth": {"accessToken": "sk-older-1",
                                          "refreshToken": "rt-older-1", "expiresAt": 1_000}})
    sw._write_account_credentials("1", "a@example.com", cur)
    store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name(), cur)
    (home / ".claude" / ".credentials.json").write_text(older)
    rc36_on_active(store, monkeypatch, delete_fails=True)
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        with pytest.raises(CredentialReadError):
            sw.switch_to("2", json_output=True)
    assert "rt-cur-1" in sw._read_account_credentials("1", "a@example.com")


def test_cli_switch_exits_1_with_the_reason(mac, monkeypatch, capsys):
    from claude_swap import cli

    sw, home, store = mac
    rc36_on_active(store, monkeypatch)
    from claude_swap import oauth

    with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=sw), \
         patch("claude_swap.oauth.fetch_oauth_profile", return_value=None), \
         patch("claude_swap.oauth.try_refresh_oauth_credentials",
               return_value=oauth.RefreshOutcome(None, "transient")), \
         patch("claude_swap.oauth.request_usage_data", side_effect=OSError("offline")), \
         patch.object(sys, "argv", ["cc-swap", "switch", "2"]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
    assert excinfo.value.code == 1
    assert MESSAGE in capsys.readouterr().err
    assert _new_login_still_in_keychain(store)


@pytest.mark.asyncio
async def test_tui_switch_shows_a_toast(tmp_path):
    """The TUI's Enter-to-switch surfaces the refusal as an error toast."""
    from tests.test_tui import FakeSwitcher, make_account, make_app, settle

    class Refusing(FakeSwitcher):
        def switch_to(self, identifier, json_output=False, force=False):
            raise CredentialReadError(f"{MESSAGE}; retry in a GUI terminal")

    fake = Refusing([make_account(1, active=True), make_account(2)], tmp_path)
    app = make_app(fake)
    seen: list[tuple[str, str]] = []
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)
        app.notify = lambda message, **kw: seen.append((str(message), kw.get("severity", "")))
        screens = len(app.screen_stack)
        app.do_switch("2")
        await settle(pilot)
        await settle(pilot)
        assert len(app.screen_stack) == screens  # no failure modal
    assert any(sev == "error" and MESSAGE in msg for msg, sev in seen)


def test_fleet_relogin_switch_back_failure_keeps_the_stored_login(tmp_path):
    from claude_swap.maximize.fleet_actions import relogin_store

    class S:
        def __init__(self):
            self.calls = []

        def _get_sequence_data(self):
            return {"accounts": {"4": {"email": "d@x", "organizationUuid": "", "uuid": "u4"}}}

        def _get_current_identity_triple(self):
            return ("d@x", "", "u4")

        @staticmethod
        def _find_account_slot(data, e, o):
            return "4"

        def add_account(self, **kw):
            self.calls.append("add")

        def switch_to(self, *a, **k):
            raise CredentialReadError(f"{MESSAGE}; retry in a GUI terminal")

    s = S()
    out = relogin_store(s, "4", return_to="1")
    assert out["stored"] is True and "returned_to" not in out
    assert MESSAGE in out["switch_back_error"]
