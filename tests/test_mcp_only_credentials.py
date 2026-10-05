"""A ``~/.claude/.credentials.json`` holding only MCP logins is no login
(cc-swap fork).

2026-10-04: Claude Code wrote its MCP OAuth tokens to the plaintext file
while the macOS Keychain was unavailable (``{"mcpOAuth": …}``, no
``claudeAiOauth``); the login stayed in the Keychain. Such a file must never
be read, backed up, stashed or exported as a login, nor be turned into a
plaintext copy of one. Its MCP state still composes into an activated login.
macOS is faked in a temp HOME on the in-memory Keychain
(``block_real_keychain``); nothing touches the real one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain as kc
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE, holds_only_shared_fields
from claude_swap.exceptions import CredentialReadError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

CRED1 = json.dumps({"claudeAiOauth": {"accessToken": "sk-1", "refreshToken": "rt-1",
                                      "expiresAt": 9_999_999_999_000}})
CRED2 = json.dumps({"claudeAiOauth": {"accessToken": "sk-2", "refreshToken": "rt-2",
                                      "expiresAt": 9_999_999_999_000}})
MCP_ONLY = json.dumps({"mcpOAuth": {"srv|abc": {
    "serverName": "srv", "accessToken": "mcp-at", "refreshToken": "mcp-rt",
}}})


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
    _seed(sw, 1, "a@example.com", CRED1)
    _seed(sw, 2, "b@example.com", CRED2)
    (temp_home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-1"}}))
    (temp_home / ".claude" / ".credentials.json").write_text(MCP_ONLY)
    block_real_keychain.set_password(
        CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name(), CRED1)
    return sw, temp_home, block_real_keychain


def _live_keychain(store) -> str | None:
    return store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name())


def test_what_counts_as_mcp_only():
    assert holds_only_shared_fields(MCP_ONLY)
    assert holds_only_shared_fields(json.dumps({"mcpOAuth": {}, "pluginSecrets": {}}))
    assert not holds_only_shared_fields(CRED1)
    both = json.loads(CRED1) | json.loads(MCP_ONLY)
    assert not holds_only_shared_fields(json.dumps(both))
    for other in ("", None, "{}", "not json", "sk-ant-api03-x", json.dumps({"x": 1}), "[1]"):
        assert not holds_only_shared_fields(other)


def test_a_degraded_read_never_serves_mcp_only_bytes_as_the_login(mac, monkeypatch):
    sw, _home, _store = mac

    def locked(service, account):
        raise kc.KeychainError("rc=36 errSecInteractionNotAllowed")

    monkeypatch.setattr(kc, "get_password", locked)
    read = sw._read_active_credentials()
    assert read.value == "" and read.keychain_unavailable and read.degraded


def test_the_keychain_login_is_read_and_the_file_ignored(mac):
    sw, _home, _store = mac
    read = sw._read_active_credentials()
    assert read.value == CRED1 and not read.degraded


def test_a_keychain_write_never_puts_a_login_into_the_mcp_only_file(mac):
    sw, home, store = mac
    cred = home / ".claude" / ".credentials.json"
    os.utime(cred, (1_000_000_000, 1_000_000_000))
    old = cred.stat().st_mtime_ns
    sw._write_credentials(CRED2)
    assert _live_keychain(store) == CRED2
    assert cred.read_text() == MCP_ONLY  # no plaintext copy of the login
    assert cred.stat().st_mtime_ns > old  # still the hot-reload trigger


def test_a_switch_never_backs_up_mcp_only_bytes_over_the_slots_login(mac):
    sw, home, store = mac
    store.delete_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name())
    # premise: the Keychain has no item, so the plaintext file answers
    assert sw._read_active_credentials().value == MCP_ONLY
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        sw.switch_to("2", json_output=True)
    assert sw._read_account_credentials("1", "a@example.com") == CRED1
    live = json.loads(_live_keychain(store))
    assert live["claudeAiOauth"]["refreshToken"] == "rt-2"
    assert live["mcpOAuth"] == json.loads(MCP_ONLY)["mcpOAuth"]  # still composed in
    assert sw.list_unclaimed_credentials() == {}  # nothing stashed as a login


@pytest.fixture
def linux(temp_home: Path, block_real_keychain):
    sw = ClaudeAccountSwitcher()
    sw.platform = Platform.LINUX
    sw._setup_directories()
    sw._init_sequence_file()
    _seed(sw, 1, "a@example.com", CRED1)
    _seed(sw, 2, "b@example.com", CRED2)
    (temp_home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-1"}}))
    cred = temp_home / ".claude" / ".credentials.json"
    cred.write_text(MCP_ONLY)
    cred.chmod(0o600)
    return sw, temp_home


def test_add_never_captures_mcp_only_bytes_on_macos(mac):
    sw, _home, store = mac
    store.delete_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name())
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        with pytest.raises(CredentialReadError, match="only MCP logins, no Claude login"):
            sw.add_account(slot=1, assume_yes=True)
    assert sw._read_account_credentials("1", "a@example.com") == CRED1


def test_add_never_captures_mcp_only_bytes_on_linux(linux):
    sw, _home = linux
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        with pytest.raises(CredentialReadError, match="only MCP logins, no Claude login"):
            sw.add_account(slot=1, assume_yes=True)
    assert sw._read_account_credentials("1", "a@example.com") == CRED1


def test_a_linux_switch_writes_the_full_login_with_the_mcp_logins(linux):
    sw, home = linux
    assert sw._read_active_credentials().value == MCP_ONLY  # Linux: the file is the store
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        sw.switch_to("2", json_output=True)
    live = json.loads((home / ".claude" / ".credentials.json").read_text())
    assert live["claudeAiOauth"]["refreshToken"] == "rt-2"
    assert live["mcpOAuth"] == json.loads(MCP_ONLY)["mcpOAuth"]
    assert sw._read_account_credentials("1", "a@example.com") == CRED1
    assert sw.list_unclaimed_credentials() == {}


def test_nothing_to_back_up_when_the_live_credential_is_mcp_only(mac):
    sw, _home, store = mac
    store.delete_password(CLAUDE_CODE_KEYCHAIN_SERVICE, kc.keychain_account_name())
    assert sw.sync_active_backup() == (True, "")
    assert sw._read_account_credentials("1", "a@example.com") == CRED1
