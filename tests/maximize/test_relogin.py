"""`cc-swap login N` / Fleet `r`: launch Claude Code's login for the slot in a
throwaway profile, verify the result, store it (maximize/relogin.py).

No real claude and no real Keychain: the login is a fake runner that writes
what ``claude auth login`` leaves in its ``CLAUDE_CONFIG_DIR`` (the
``.claude.json`` account and, off macOS, ``.credentials.json``; on macOS the
hashed Keychain item, here the in-memory fake from conftest).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claude_swap import cli, oauth
from claude_swap.exceptions import ConfigError, ValidationError
from claude_swap.maximize import relogin as rl
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

CLAUDE = "/opt/fake/claude"
FOUR = "four@example.com"
ORG4 = "org-4"


def _creds(rt: str, at: str | None = None) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": at or f"sk-ant-oat01-{rt}",
        "refreshToken": rt,
        "expiresAt": 99999999999000,
        "refreshTokenExpiresAt": 99999999999000,
    }})


def _account(email: str, org: str, uuid: str) -> dict:
    return {"emailAddress": email, "organizationUuid": org, "accountUuid": uuid,
            "organizationName": "Team"}


def _live(home: Path, email: str, org: str, uuid: str) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": _account(email, org, uuid), "projects": {}}),
        encoding="utf-8",
    )


def _switcher(home: Path, *, active: str = "1") -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    data = s._get_sequence_data()
    data["accounts"] = {
        "1": {"email": "one@example.com", "uuid": "uuid-1", "organizationUuid": "",
              "organizationName": ""},
        "4": {"email": FOUR, "uuid": "uuid-4", "organizationUuid": ORG4,
              "organizationName": "Team", "alias": "four"},
        "5": {"email": "key@example.com", "uuid": "", "organizationUuid": "",
              "organizationName": "", "kind": "api_key"},
    }
    data["sequence"] = [1, 4, 5]
    data["activeAccountNumber"] = int(active)
    s._write_json(s.sequence_file, data)
    s._write_account_credentials("1", "one@example.com", _creds("rt-one"))
    s._write_account_config("1", "one@example.com",
                            json.dumps({"oauthAccount": _account("one@example.com", "", "uuid-1")}))
    s._write_account_credentials("4", FOUR, _creds("rt-four-dead"))
    s._write_account_config(
        "4", FOUR,
        json.dumps({"oauthAccount": _account(FOUR, ORG4, "uuid-4"), "keep": "me"}),
    )
    if active == "4":
        _live(home, FOUR, ORG4, "uuid-4")
        s._write_credentials(_creds("rt-four-dead"))
    else:
        _live(home, "one@example.com", "", "uuid-1")
        s._write_credentials(_creds("rt-one"))
    return s


class FakeLogin:
    """Stands in for ``claude auth login``: records the call and writes the
    profile the way claude does."""

    def __init__(self, *, email=FOUR, org=ORG4, uuid="uuid-4", rt="rt-four-new",
                 code: int | None = 0, keychain=None, write=True):
        self.email, self.org, self.uuid, self.rt = email, org, uuid, rt
        self.code = code
        self.keychain = keychain  # the conftest fake: write the hashed item there
        self.write = write
        self.calls: list[tuple[list[str], dict, Path]] = []

    def __call__(self, argv, env, cwd):
        self.calls.append((list(argv), dict(env), Path(cwd)))
        profile = Path(env["CLAUDE_CONFIG_DIR"])
        assert profile == Path(cwd) and profile.is_dir()
        if self.write:
            (profile / ".claude.json").write_text(json.dumps(
                {"oauthAccount": _account(self.email, self.org, self.uuid)}))
            if self.keychain is not None:
                from claude_swap import macos_keychain
                from claude_swap.session import keychain_service_name

                self.keychain.set_password(
                    keychain_service_name(env["CLAUDE_CONFIG_DIR"]),
                    macos_keychain.keychain_account_name(), _creds(self.rt),
                )
            else:
                (profile / ".credentials.json").write_text(_creds(self.rt))
        return self.code

    @property
    def profile(self) -> Path:
        return self.calls[-1][2]


def _leftover_profiles(s) -> list[Path]:
    return list(Path(s.backup_dir).glob(f"{rl.PROFILE_PREFIX}*"))


def _slot_rt(s, num="4", email=FOUR) -> str | None:
    data = oauth.extract_oauth_data(s._read_account_credentials(num, email))
    return data.get("refreshToken") if data else None


def _run(s, login, number="4", **kw):
    return rl.relogin(s, number, claude=CLAUDE, run=login, announce=None, **kw)


# -- success ---------------------------------------------------------------------------


def test_stores_the_new_login_into_the_slot_and_leaves_the_live_login_alone(temp_home):
    s = _switcher(temp_home)
    live_before = s._read_credentials()
    login = FakeLogin()
    outcome = _run(s, login)
    assert outcome.ok and outcome.status == rl.STORED and not outcome.activated
    assert _slot_rt(s) == "rt-four-new"
    config = json.loads(s._read_account_config("4", FOUR))
    assert config["oauthAccount"]["accountUuid"] == "uuid-4"
    assert config["keep"] == "me"  # only oauthAccount is replaced
    assert s._read_credentials() == live_before  # live login untouched
    assert s.current_account_number() == "1"
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_runs_claude_auth_login_for_the_slot_email_in_a_scrubbed_private_profile(temp_home):
    s = _switcher(temp_home)
    login = FakeLogin()
    base = {"PATH": "/usr/bin", "HOME": str(temp_home),
            "ANTHROPIC_API_KEY": "sk-ant-api-x", "CLAUDE_CODE_OAUTH_TOKEN": "t",
            "ANTHROPIC_BASE_URL": "https://proxy.example.com",
            "CLAUDE_CONFIG_DIR": "/elsewhere", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
            "CLAUDECODE": "1"}
    seen_mode = []
    real = login.__call__

    def checking(argv, env, cwd):
        seen_mode.append(Path(cwd).stat().st_mode & 0o777)
        return real(argv, env, cwd)

    assert _run(s, checking, base_env=base).ok
    argv, env, cwd = login.calls[0]
    assert argv == [CLAUDE, "auth", "login", "--claudeai", "--email", FOUR]
    assert env["CLAUDE_CONFIG_DIR"] == str(cwd)
    assert Path(cwd).parent == Path(s.backup_dir)
    assert Path(cwd).name.startswith(rl.PROFILE_PREFIX)
    for gone in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDECODE"):
        assert gone not in env
    assert env["PATH"] == "/usr/bin"
    if sys.platform != "win32":
        assert seen_mode == [0o700]


def test_clears_the_dead_login_state_of_the_slot(temp_home):
    s = _switcher(temp_home)
    cleared = []
    real = s._usage_store.clear_dead_token
    s._usage_store.clear_dead_token = lambda nums, ids: (cleared.append(list(nums)), real(nums, ids))
    assert _run(s, FakeLogin()).ok
    assert cleared == [["4"]]


def test_macos_reads_the_profile_keychain_item_and_deletes_it(temp_home, monkeypatch, block_real_keychain):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    s = _switcher(temp_home)
    login = FakeLogin(keychain=block_real_keychain)
    assert _run(s, login).ok
    assert _slot_rt(s) == "rt-four-new"  # came from the Keychain: no file was written
    from claude_swap.session import keychain_service_name

    service = keychain_service_name(str(login.profile))
    assert not any(k[0] == service for k in block_real_keychain.data)
    assert not login.profile.exists()


def test_macos_cleanup_deletes_the_keychain_item_on_a_mismatch_too(temp_home, monkeypatch, block_real_keychain):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    s = _switcher(temp_home)
    login = FakeLogin(email="other@example.com", keychain=block_real_keychain)
    assert _run(s, login).status == rl.MISMATCH
    from claude_swap.session import keychain_service_name

    service = keychain_service_name(str(login.profile))
    assert not any(k[0] == service for k in block_real_keychain.data)
    assert _slot_rt(s) == "rt-four-dead"


# -- refusals ------------------------------------------------------------------------


def test_email_mismatch_stores_nothing_and_names_both_accounts(temp_home):
    s = _switcher(temp_home)
    login = FakeLogin(email="other@example.com", org="")
    outcome = _run(s, login)
    assert outcome.status == rl.MISMATCH and not outcome.ok
    assert "other@example.com" in outcome.message and FOUR in outcome.message
    assert "nothing stored" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"
    assert not _leftover_profiles(s)


def test_org_mismatch_stores_nothing(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(org=""))  # same address, personal account
    assert outcome.status == rl.MISMATCH
    assert "personal" in outcome.message and ORG4 in outcome.message
    assert _slot_rt(s) == "rt-four-dead"


def test_account_uuid_mismatch_stores_nothing(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(uuid="uuid-somebody"))
    assert outcome.status == rl.MISMATCH and "uuid-somebody" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"


def test_token_owner_oracle_refuses_a_definite_mismatch(temp_home, monkeypatch):
    s = _switcher(temp_home)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: {
        "uuid": "uuid-x", "email": "x@example.com", "organizationUuid": ORG4})
    outcome = _run(s, FakeLogin())
    assert outcome.status == rl.MISMATCH and "x@example.com" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"


def test_token_owner_oracle_agreeing_stores(temp_home, monkeypatch):
    s = _switcher(temp_home)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: {
        "uuid": "uuid-4", "email": FOUR, "organizationUuid": ORG4})
    assert _run(s, FakeLogin()).ok


@pytest.mark.parametrize("code,status", [(1, rl.FAILED), (130, rl.FAILED), (None, rl.CANCELLED)])
def test_cancel_or_nonzero_exit_stores_nothing_and_cleans_up(temp_home, code, status):
    s = _switcher(temp_home)
    login = FakeLogin(code=code)
    outcome = _run(s, login)
    assert outcome.status == status
    assert "nothing stored" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_a_login_that_saved_nothing_stores_nothing(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(write=False))
    assert outcome.status == rl.FAILED and "no login" in outcome.message
    assert not _leftover_profiles(s)


def test_claude_that_cannot_start_is_unavailable(temp_home):
    s = _switcher(temp_home)

    def missing(argv, env, cwd):
        raise FileNotFoundError(2, "No such file", argv[0])

    outcome = _run(s, missing)
    assert outcome.status == rl.UNAVAILABLE
    assert not _leftover_profiles(s)


def test_cleanup_happens_when_the_store_raises(temp_home, monkeypatch):
    s = _switcher(temp_home)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(s, "store_relogin", boom)
    login = FakeLogin()
    with pytest.raises(RuntimeError):
        _run(s, login)
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_cleanup_happens_on_ctrl_c_outside_the_child(temp_home):
    s = _switcher(temp_home)
    seen = []

    def interrupted(argv, env, cwd):
        seen.append(Path(cwd))
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(s, interrupted)
    assert not seen[0].exists() and not _leftover_profiles(s)


def test_api_key_slot_has_no_login_to_renew(temp_home):
    s = _switcher(temp_home)
    with pytest.raises(ValidationError):
        _run(s, FakeLogin(), number="5")
    assert not _leftover_profiles(s)


# -- the active slot -----------------------------------------------------------------


def test_active_slot_rewrites_the_live_login_too(temp_home):
    s = _switcher(temp_home, active="4")
    assert s.current_account_number() == "4"
    outcome = _run(s, FakeLogin())
    assert outcome.ok and outcome.activated
    assert _slot_rt(s) == "rt-four-new"
    live = oauth.extract_oauth_data(s._read_credentials())
    assert live["refreshToken"] == "rt-four-new"
    assert s.current_account_number() == "4"
    state = Path(s.backup_dir) / "autoswitch_state.json"
    if state.exists():  # the pause around the rewrite was lifted
        assert "pausedUntil" not in json.loads(state.read_text())
    assert not _leftover_profiles(s)


def test_active_slot_reports_when_the_live_rewrite_fails(temp_home, monkeypatch):
    from claude_swap.exceptions import SwitchError

    s = _switcher(temp_home, active="4")

    def refuse(*a, **k):
        raise SwitchError("locked")

    monkeypatch.setattr(s, "switch_to", refuse)
    outcome = _run(s, FakeLogin())
    assert outcome.ok and not outcome.activated
    assert "cc-swap switch 4 --force" in outcome.message
    assert _slot_rt(s) == "rt-four-new"


# -- the switcher's store ------------------------------------------------------------


def test_store_relogin_rechecks_the_identity_under_the_lock(temp_home):
    s = _switcher(temp_home)
    with pytest.raises(ConfigError):
        s.store_relogin("4", _creds("rt-x"), _account("other@example.com", ORG4, "uuid-4"))
    with pytest.raises(ConfigError):
        s.store_relogin("4", _creds("rt-x"), _account(FOUR, "", "uuid-4"))
    assert _slot_rt(s) == "rt-four-dead"


def test_store_relogin_refuses_a_credential_without_a_token_pair(temp_home):
    from claude_swap.exceptions import CredentialReadError

    s = _switcher(temp_home)
    half = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-x"}})
    with pytest.raises(CredentialReadError):
        s.store_relogin("4", half, _account(FOUR, ORG4, "uuid-4"))


# -- probes and the runner -----------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX script")
def test_login_supported_reads_auth_login_help(tmp_path):
    good = tmp_path / "claude-good"
    good.write_text("#!/bin/sh\necho '  --email <email>  Pre-populate email'\n")
    old = tmp_path / "claude-old"
    old.write_text("#!/bin/sh\necho 'unknown command auth' >&2\nexit 1\n")
    for p in (good, old):
        p.chmod(0o755)
    assert rl.login_supported(str(good))
    assert not rl.login_supported(str(old))
    assert not rl.login_supported(str(tmp_path / "missing"))


def test_run_interactive_returns_the_exit_code(tmp_path):
    argv = [sys.executable, "-c", "import sys; sys.exit(3)"]
    assert rl.run_interactive(argv, dict(__import__("os").environ), tmp_path) == 3


# -- CLI -----------------------------------------------------------------------------


def _cli(monkeypatch, argv, *, claude=CLAUDE, supported=True):
    monkeypatch.setattr("claude_swap.maximize.primer.resolve_claude_path",
                        lambda configured, **k: claude)
    monkeypatch.setattr(rl, "login_supported", lambda c, **k: supported)
    with pytest.raises(SystemExit) as exit_info:
        cli._login_command(argv)
    return exit_info.value.code


@pytest.fixture
def cli_login(monkeypatch):
    """Route ``relogin(..., run=run_interactive)`` (bound at definition) to a fake."""
    holder: dict = {}
    real = rl.relogin

    def relogin(switcher, number, *, claude, **kw):
        kw["run"] = holder["login"]
        return real(switcher, number, claude=claude, **kw)

    monkeypatch.setattr(rl, "relogin", relogin)
    return holder


def test_cli_login_stores_and_exits_zero(temp_home, monkeypatch, capsys, cli_login):
    s = _switcher(temp_home)
    cli_login["login"] = login = FakeLogin()
    assert _cli(monkeypatch, ["four"]) == 0  # an alias resolves like a number
    out = capsys.readouterr().out
    assert "#4 login stored" in out and "auth login" in out  # the banner
    assert login.calls[0][0][-1] == FOUR
    assert _slot_rt(s) == "rt-four-new"


def test_cli_login_mismatch_exits_one(temp_home, monkeypatch, capsys, cli_login):
    s = _switcher(temp_home)
    cli_login["login"] = FakeLogin(email="other@example.com")
    assert _cli(monkeypatch, ["4"]) == 1
    captured = capsys.readouterr()
    assert "other@example.com" in captured.out + captured.err
    assert _slot_rt(s) == "rt-four-dead"


def test_cli_login_without_claude_prints_the_manual_steps(temp_home, monkeypatch, capsys, cli_login):
    s = _switcher(temp_home)
    cli_login["login"] = login = FakeLogin()
    assert _cli(monkeypatch, ["4"], claude=None) == 1
    captured = capsys.readouterr()
    assert "/login" in captured.out and "cc-swap add" in captured.out
    assert "claude was not found" in captured.out + captured.err
    assert not login.calls and not _leftover_profiles(s)


def test_cli_login_with_an_old_claude_prints_the_manual_steps(temp_home, monkeypatch, capsys, cli_login):
    _switcher(temp_home)
    cli_login["login"] = login = FakeLogin()
    assert _cli(monkeypatch, ["4"], supported=False) == 1
    captured = capsys.readouterr()
    assert "auth login --email" in captured.out + captured.err
    assert not login.calls


def test_cli_login_unknown_account_exits_one(temp_home, monkeypatch, capsys, cli_login):
    _switcher(temp_home)
    cli_login["login"] = FakeLogin()
    assert _cli(monkeypatch, ["9"]) == 1


def test_login_is_a_registered_command():
    assert cli._FORK_COMMANDS["login"] == "_login_command"


def test_relogin_fix_points_at_the_login_command():
    assert oauth.relogin_fix(4) == "re-login #4: cc-swap login 4, or Fleet → select → r"
