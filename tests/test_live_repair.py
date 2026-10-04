"""``cc-swap repair-live`` (maximize/live_repair.py) and the refresh path's
under-lock degraded re-read (switcher._fetch_active_usage).

The mixed live login: a ``/login`` while the Keychain was locked (over
SSH) saved account X's login in plaintext and pointed ``~/.claude.json`` at
X, while the Keychain still holds managed slot #2's token. macOS is faked in
a temp HOME on the in-memory Keychain (``block_real_keychain``); the
token-owner lookup is patched. Nothing touches the real Keychain.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain as kc
from claude_swap import oauth
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE, ActiveCredentials
from claude_swap.exceptions import ConfigError, CredentialReadError
from claude_swap.maximize import live_repair as lr
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord
from tests.test_switch_degraded_read import _seed, rc36_on_active

NOW = time.time()
# Slot #2's login, issued 9 hours ago (its access token expired an hour ago).
CRED2 = json.dumps({"claudeAiOauth": {"accessToken": "sk-2", "refreshToken": "rt-2",
                                      "expiresAt": int((NOW - 3600) * 1000)}})
OLD1 = json.dumps({"claudeAiOauth": {"accessToken": "sk-old-1", "refreshToken": "rt-old-1",
                                     "expiresAt": int((NOW - 3600) * 1000)}})
# The plaintext /login: fresh.
FRESH = json.dumps({"claudeAiOauth": {"accessToken": "sk-x-new", "refreshToken": "rt-x-new",
                                      "expiresAt": int((NOW + 8 * 3600) * 1000)}})


def _rig(temp_home: Path, store, *, x_managed: bool):
    sw = ClaudeAccountSwitcher()
    sw.platform = Platform.MACOS
    sw._setup_directories()
    sw._init_sequence_file()
    _seed(sw, 1, "a@example.com", OLD1)
    _seed(sw, 2, "b@example.com", CRED2)
    email, uuid = ("a@example.com", "uuid-1") if x_managed else ("x@example.com", "uuid-x")
    (temp_home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": email, "accountUuid": uuid}}))
    store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name(), CRED2)
    (temp_home / ".claude" / ".credentials.json").write_text(FRESH)
    return sw, email, uuid


def _keychain(store) -> str | None:
    return store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name())


def _owner(email, uuid):
    return patch(
        "claude_swap.oauth.fetch_oauth_profile",
        return_value={"uuid": uuid, "email": email, "organizationUuid": None},
    )


@pytest.fixture
def unmanaged(temp_home, block_real_keychain):
    sw, email, uuid = _rig(temp_home, block_real_keychain, x_managed=False)
    return sw, temp_home, block_real_keychain, email, uuid


@pytest.fixture
def managed(temp_home, block_real_keychain):
    sw, email, uuid = _rig(temp_home, block_real_keychain, x_managed=True)
    return sw, temp_home, block_real_keychain, email, uuid


def test_detects_the_mixed_login(unmanaged):
    sw, home, store, email, _ = unmanaged
    m = lr.detect(sw)
    assert m is not None and m.email == email and m.y_slot == "2" and m.x_slot is None
    text = lr.explain(m)
    assert "Keychain was locked" in text and "cc-swap repair-live" in text
    assert "rt-" not in text and "sk-" not in text


def test_a_plaintext_older_than_the_keychain_token_is_not_mixed(unmanaged):
    sw, home, store, *_ = unmanaged
    old = NOW - 12 * 3600
    os.utime(home / ".claude" / ".credentials.json", (old, old))
    assert lr.detect(sw) is None


def test_repairs_an_unmanaged_account_into_the_keychain(unmanaged):
    sw, home, store, email, uuid = unmanaged
    asked: list[str] = []
    with _owner(email, uuid):
        message = lr.repair(sw, confirm=lambda q: asked.append(q) or True)
    assert message.startswith("Repaired") and "cc-swap add" in message
    assert _keychain(store) and "rt-x-new" in _keychain(store)
    assert not (home / ".claude" / ".credentials.json").exists()
    assert sw._read_account_credentials("2", "b@example.com") == CRED2  # Y kept
    assert "Keychain was locked" in asked[0]


def test_repairs_a_managed_account_into_the_keychain_and_its_slot(managed):
    sw, home, store, email, uuid = managed
    with _owner(email, uuid):
        message = lr.repair(sw, confirm=lambda q: True)
    assert message.startswith("Repaired: the Keychain and #1")
    assert "rt-x-new" in _keychain(store)
    assert "rt-x-new" in sw._read_account_credentials("1", "a@example.com")
    assert sw._read_account_credentials("2", "b@example.com") == CRED2
    assert not (home / ".claude" / ".credentials.json").exists()


def test_refuses_while_the_keychain_is_unreadable(unmanaged, monkeypatch):
    sw, home, store, email, uuid = unmanaged
    rc36_on_active(store, monkeypatch)
    with _owner(email, uuid), pytest.raises(CredentialReadError, match="unreadable"):
        lr.repair(sw, confirm=lambda q: True)
    assert (home / ".claude" / ".credentials.json").read_text() == FRESH


def test_refuses_a_plaintext_login_that_is_not_the_named_account(unmanaged):
    sw, home, store, *_ = unmanaged
    with _owner("someone@example.com", "uuid-other"), pytest.raises(ConfigError, match="not"):
        lr.repair(sw, confirm=lambda q: True)
    assert _keychain(store) == CRED2
    assert (home / ".claude" / ".credentials.json").read_text() == FRESH


def test_refuses_when_the_owner_lookup_does_not_answer(unmanaged):
    sw, home, store, *_ = unmanaged
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        with pytest.raises(ConfigError, match="Nothing was changed"):
            lr.repair(sw, confirm=lambda q: True)
    assert _keychain(store) == CRED2


def test_a_declined_confirmation_changes_nothing(unmanaged):
    sw, home, store, email, uuid = unmanaged
    with _owner(email, uuid):
        assert lr.repair(sw, confirm=lambda q: False).startswith("Cancelled")
    assert _keychain(store) == CRED2
    assert (home / ".claude" / ".credentials.json").read_text() == FRESH


def test_the_command_needs_yes_or_a_confirmation(unmanaged, monkeypatch, capsys):
    sw, home, store, email, uuid = unmanaged
    monkeypatch.setattr("claude_swap.switcher.ClaudeAccountSwitcher", lambda **k: sw)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with _owner(email, uuid):
        assert lr.command([]) == 1
        assert _keychain(store) == CRED2
        assert lr.command(["--yes"]) == 0
    assert "rt-x-new" in _keychain(store)


def test_the_engine_names_repair_live_for_a_mixed_login(unmanaged):
    from claude_swap.autoswitch import (
        AutoSwitchEngine,
        ConfigWarningEvent,
        NoSwitchEvent,
        TickOutcome,
    )
    from claude_swap.settings import AutoSwitchSettings

    sw, *_ = unmanaged
    events: list = []
    engine = AutoSwitchEngine(sw, AutoSwitchSettings(), events.append, dry_run=True)
    assert engine.tick() is TickOutcome.NO_ACTION
    [warning] = [e for e in events if isinstance(e, ConfigWarningEvent)]
    assert "mixed live login" in warning.message and "cc-swap repair-live" in warning.message
    [held] = [e for e in events if isinstance(e, NoSwitchEvent)]
    assert held.reason == "unmanaged-active-account" and "repair-live" in held.detail


def test_add_names_repair_live_for_a_mixed_login(managed):
    sw, home, store, *_ = managed
    # The Keychain token resolves to #2's account: add refuses, and says why.
    with patch(
        "claude_swap.oauth.fetch_oauth_profile",
        return_value={"uuid": "uuid-2", "email": "b@example.com", "organizationUuid": None},
    ):
        # (#2's access token expired an hour ago; the guard only resolves a
        # live one, so it is read as unexpired here.)
        with patch.object(oauth, "is_oauth_token_expired", return_value=False):
            with pytest.raises(ConfigError, match="cc-swap repair-live"):
                sw._reject_foreign_credential_capture(CRED2, "a@example.com", "", "uuid-1")


# -- refresh: the under-lock re-read reports a degraded read (fix 5) ---------------------------


def test_the_refresh_defers_when_the_under_lock_read_is_degraded(temp_home, monkeypatch):
    from claude_swap.json_output import USAGE_KEYCHAIN_UNAVAILABLE

    expired = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-active", "refreshToken": "rt-orig", "expiresAt": 1000}})
    (temp_home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "test@example.com", "accountUuid": "uuid-1"}}))
    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    sw._init_sequence_file()
    _seed(sw, 1, "test@example.com", expired)
    # The pre-lock pass was clean; under the locks the Keychain stopped
    # answering and the bytes came from the plaintext fallback.
    monkeypatch.setattr(
        sw._store, "_read_active_credentials",
        lambda: ActiveCredentials(expired, False, True),
    )
    with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
        record = sw._fetch_active_usage("1", "test@example.com", expired)
    post.assert_not_called()
    assert isinstance(record, FetchRecord) and record.sentinel == USAGE_KEYCHAIN_UNAVAILABLE


# -- re-review: under-lock re-check, shared fields, 401, the detection cache -----------------


def test_a_keychain_rotated_between_detect_and_write_aborts_with_nothing_written(managed):
    sw, home, store, email, uuid = managed
    rotated = json.dumps({"claudeAiOauth": {"accessToken": "sk-2b", "refreshToken": "rt-2b",
                                            "expiresAt": int((NOW + 3600) * 1000)}})

    def confirm_while_claude_rotates(question):
        # A running Claude session rotates #2's login meanwhile.
        store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name(), rotated)
        return True

    with _owner(email, uuid), pytest.raises(ConfigError, match="changed since it was checked"):
        lr.repair(sw, confirm=confirm_while_claude_rotates)
    assert _keychain(store) == rotated
    assert sw._read_account_credentials("1", "a@example.com") == OLD1
    assert (home / ".claude" / ".credentials.json").read_text() == FRESH


def test_mcp_logins_from_both_sides_survive_the_repair(unmanaged):
    sw, home, store, email, uuid = unmanaged
    plain = json.loads(FRESH)
    plain["mcpOAuth"] = {"made-over-ssh": {"accessToken": "mcp-new"}}
    (home / ".claude" / ".credentials.json").write_text(json.dumps(plain))
    keychain = json.loads(CRED2)
    keychain["mcpOAuth"] = {"older": {"accessToken": "mcp-old"},
                            "made-over-ssh": {"accessToken": "mcp-stale"}}
    store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name(),
                       json.dumps(keychain))
    sw._write_account_credentials("2", "b@example.com", json.dumps(keychain))
    with _owner(email, uuid):
        assert lr.repair(sw, confirm=lambda q: True).startswith("Repaired")
    live = json.loads(_keychain(store))
    assert live["claudeAiOauth"]["refreshToken"] == "rt-x-new"
    assert live["mcpOAuth"] == {"older": {"accessToken": "mcp-old"},
                                "made-over-ssh": {"accessToken": "mcp-new"}}


def test_a_rejected_token_is_reported_as_rejected(unmanaged):
    sw, *_ = unmanaged

    def rejected(token):
        oauth.PROFILE_STATUS.code = 401
        return None

    with patch("claude_swap.oauth.fetch_oauth_profile", side_effect=rejected):
        with pytest.raises(ConfigError, match=r"rejected \(401\)"):
            lr.repair(sw, confirm=lambda q: True)


def test_repair_refuses_inside_a_session_shell(unmanaged, monkeypatch):
    sw, home, store, email, uuid = unmanaged

    def refuse():
        raise ConfigError("inside a cswap run shell")

    monkeypatch.setattr(sw, "_refuse_session_shell", refuse)
    with _owner(email, uuid), pytest.raises(ConfigError, match="cswap run shell"):
        lr.repair(sw, confirm=lambda q: True)
    assert _keychain(store) == CRED2


def test_detection_reads_the_slots_once_while_the_login_stays_mixed(unmanaged, monkeypatch):
    sw, *_ = unmanaged
    reads: list[str] = []
    real = sw._read_account_credentials
    monkeypatch.setattr(
        sw, "_read_account_credentials", lambda n, e: reads.append(n) or real(n, e)
    )
    assert lr.detect(sw) is not None
    first = len(reads)
    assert first >= 2
    assert lr.detect(sw) is not None and len(reads) == first
