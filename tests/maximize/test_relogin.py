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


def _stashed_rts(s) -> list[str]:
    out = []
    for entry_id in s.list_unclaimed_credentials():
        creds, _ = s._store._read_unclaimed_credential(entry_id)
        out.append(oauth.extract_oauth_data(creds)["refreshToken"])
    return out


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
    login = FakeLogin(code=code, write=False)  # stopped before a login was saved
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


def test_a_store_that_raises_is_failed_kept_unclaimed_and_cleaned_up(temp_home, monkeypatch):
    s = _switcher(temp_home)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(s, "store_relogin", boom)
    login = FakeLogin()
    outcome = _run(s, login)
    assert outcome.status == rl.FAILED and "RuntimeError: disk on fire" in outcome.message
    assert "cc-swap unclaimed" in outcome.message
    assert _stashed_rts(s) == ["rt-four-new"]
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_ctrl_c_after_claude_saved_the_login_still_stores_it(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(code=None))  # saved, then interrupted
    assert outcome.ok and _slot_rt(s) == "rt-four-new"


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


def test_active_slot_live_rewrite_failure_rolls_both_back_and_keeps_the_login(
    temp_home, monkeypatch
):
    from claude_swap.exceptions import CredentialWriteError

    s = _switcher(temp_home, active="4")
    live_before = s._read_credentials()
    config_before = (temp_home / ".claude.json").read_text()

    real = s._write_credentials
    refused = []

    def refuse_once(creds):
        if not refused:
            refused.append(creds)
            raise CredentialWriteError("Keychain locked")
        return real(creds)  # the rollback's restore

    monkeypatch.setattr(s, "_write_credentials", refuse_once)
    outcome = _run(s, FakeLogin())
    assert outcome.status == rl.FAILED and not outcome.ok and not outcome.activated
    assert "Keychain locked" in outcome.message and "unchanged" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"  # the slot was rolled back
    assert s._read_credentials() == live_before
    assert json.loads((temp_home / ".claude.json").read_text()) == json.loads(config_before)
    assert _stashed_rts(s) == ["rt-four-new"]  # the browser login is not lost
    assert not _leftover_profiles(s)


def test_a_non_claude_error_mid_write_rolls_back(temp_home, monkeypatch):
    s = _switcher(temp_home, active="4")
    live_before = s._read_credentials()
    real = s._write_json

    def flaky(path, data):
        if path == s.sequence_file:  # the last write of the critical section
            raise OSError(28, "No space left on device")
        return real(path, data)

    monkeypatch.setattr(s, "_write_json", flaky)
    outcome = _run(s, FakeLogin())
    assert outcome.status == rl.FAILED and "OSError" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"
    assert s._read_credentials() == live_before
    config = json.loads(s._read_account_config("4", FOUR))
    assert config["keep"] == "me" and config["oauthAccount"]["accountUuid"] == "uuid-4"
    assert json.loads((temp_home / ".claude.json").read_text())["oauthAccount"]["emailAddress"] == FOUR


def test_live_account_changed_during_the_login_is_not_overwritten(temp_home):
    s = _switcher(temp_home, active="4")
    login = FakeLogin()
    real = login.__call__

    def switch_meanwhile(argv, env, cwd):
        _live(temp_home, "one@example.com", "", "uuid-1")  # the engine moved to #1
        s._write_credentials(_creds("rt-one"))
        return real(argv, env, cwd)

    outcome = _run(s, switch_meanwhile)
    assert outcome.ok and not outcome.activated
    assert _slot_rt(s) == "rt-four-new"
    assert oauth.extract_oauth_data(s._read_credentials())["refreshToken"] == "rt-one"
    assert s.current_account_number() == "1"


# -- old logins never come back over a new one -----------------------------------------


D = 99999999999000  # the new login's deadline (_creds stamps it)
DAY_MS = 86_400_000


def _blob(rt: str, deadline: int | None, expires: int = 99999999999500) -> str:
    fields = {"accessToken": f"sk-ant-oat01-{rt}", "refreshToken": rt, "expiresAt": expires}
    if deadline is not None:
        fields["refreshTokenExpiresAt"] = deadline
    return json.dumps({"claudeAiOauth": fields})


OLD = _blob("rt-four-old", D - 10 * DAY_MS, 99999999990000)  # the login renewed early


def _engine_sees_it_as_ours(monkeypatch):
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: {
        "uuid": "uuid-4", "email": FOUR, "organizationUuid": ORG4})
    monkeypatch.setattr(oauth, "try_fetch_usage_for_account",
                        lambda *a, **k: oauth.UsageOutcome(usage={"five_hour": {}}))


def _old_live_valid(s):
    """#4 is live on its OLD, still valid login (an early renewal); the slot
    backup gets the NEW login without the live store (the state B1 left)."""
    s._write_credentials(OLD)
    s._write_account_credentials("4", FOUR, OLD)
    s.store_relogin("4", _creds("rt-four-new"), _account(FOUR, ORG4, "uuid-4"),
                    activate=False)
    assert _slot_rt(s) == "rt-four-new"


def _pin(s):
    return s._get_sequence_data()["accounts"]["4"].get("reloginPin")


@pytest.mark.parametrize("live", [
    OLD,  # the replaced login itself
    _blob("rt-four-old2", D - 10 * DAY_MS + 1500),  # a rotation of it (an old session)
], ids=["replaced", "replaced-rotated"])
def test_collect_pass_resync_does_not_put_the_old_login_back(temp_home, monkeypatch, live):
    s = _switcher(temp_home, active="4")
    _old_live_valid(s)
    s._write_credentials(live)
    _engine_sees_it_as_ours(monkeypatch)
    s._fetch_active_usage("4", FOUR, live, ORG4)  # the engine's collect pass
    assert _slot_rt(s) == "rt-four-new"
    assert _pin(s)["newFp"] == oauth.credential_fingerprint(_creds("rt-four-new"))


def test_active_slot_relogin_then_a_collect_pass_keeps_the_new_login(temp_home, monkeypatch):
    s = _switcher(temp_home, active="4")
    s._write_credentials(OLD)  # the old login is still valid (early renewal)
    s._write_account_credentials("4", FOUR, OLD)
    assert _run(s, FakeLogin()).activated
    _engine_sees_it_as_ours(monkeypatch)
    s._fetch_active_usage("4", FOUR, s._read_credentials(), ORG4)
    s._fetch_active_usage("4", FOUR, OLD, ORG4)  # a pass that read before the store
    assert _slot_rt(s) == "rt-four-new"
    assert oauth.extract_oauth_data(s._read_credentials())["refreshToken"] == "rt-four-new"


# Claude Code re-stamps refreshTokenExpiresAt on every refresh as now + the
# remaining lifetime in whole seconds: one login's value jitters both ways;
# cc-swap's own refresh keeps min(known, stated), so its stamps move earlier.
JITTER_MS = [200, -200, 2000, -2000, -65_000]


@pytest.mark.parametrize("delta", JITTER_MS)
def test_a_jittered_rotation_after_a_relogin_is_still_resynced(temp_home, monkeypatch, delta):
    s = _switcher(temp_home, active="4")
    assert _run(s, FakeLogin()).activated
    rotated = _blob("rt-four-gen2", D + delta)
    s._write_credentials(rotated)
    _engine_sees_it_as_ours(monkeypatch)
    s._fetch_active_usage("4", FOUR, rotated, ORG4)
    assert _slot_rt(s) == "rt-four-gen2"
    # The new login moved on: the next check finds the pin stale and drops it.
    assert not s._relogin_pin_refuses("4", _blob("rt-four-gen2", D), OLD)
    assert _pin(s) is None


@pytest.mark.parametrize("delta", JITTER_MS)
def test_a_jittered_rotation_after_a_relogin_is_still_adopted(temp_home, monkeypatch, delta):
    s = _switcher(temp_home, active="4")
    assert _run(s, FakeLogin()).activated
    rotated = _blob("rt-four-gen2", D + delta)
    s._write_credentials(rotated)
    _engine_sees_it_as_ours(monkeypatch)
    s._probe_verdicts[s._lineage_key("4", FOUR, oauth.credential_fingerprint(rotated))] = True
    expired_read = _blob("rt-four-new", D, expires=1)  # what the pass read: expired
    s._fetch_active_usage("4", FOUR, expired_read, ORG4)  # -> the locked adopt branch
    assert _slot_rt(s) == "rt-four-gen2"


@pytest.mark.parametrize("delta", JITTER_MS)
@pytest.mark.parametrize("after_relogin", [True, False])
def test_a_jittered_rotation_is_backed_up_on_switch_and_nothing_stashed(
    temp_home, delta, after_relogin
):
    s = _switcher(temp_home, active="4")
    if after_relogin:
        assert _run(s, FakeLogin()).activated
    else:
        s._write_account_credentials("4", FOUR, _blob("rt-four-gen1", D + 300, 1000))
    s._write_credentials(_blob("rt-four-gen2", D + delta - (0 if after_relogin else 500)))
    s.switch_to("1", json_output=True)
    assert s.current_account_number() == "1"
    assert _slot_rt(s) == "rt-four-gen2"
    assert _stashed_rts(s) == []


def test_store_relogin_takes_the_consume_lock(temp_home, monkeypatch):
    from claude_swap import switcher as switcher_mod
    from claude_swap.exceptions import LockError

    s = _switcher(temp_home, active="4")
    taken = []
    real = switcher_mod.FileLock

    class Spy(real):
        def __enter__(self):
            taken.append(Path(self.lock_path).name)
            if taken[-1] == ".consume-4.lock":
                raise LockError("held by a refresh")
            return super().__enter__()

    monkeypatch.setattr(switcher_mod, "FileLock", Spy)
    outcome = _run(s, FakeLogin())
    assert outcome.status == rl.FAILED and "held by a refresh" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"
    assert _stashed_rts(s) == ["rt-four-new"]


def test_switch_time_backup_keeps_the_new_login_and_stashes_the_old(temp_home):
    s = _switcher(temp_home, active="4")
    _old_live_valid(s)
    s.switch_to("1", json_output=True)
    assert s.current_account_number() == "1"
    assert _slot_rt(s) == "rt-four-new"
    assert _stashed_rts(s) == ["rt-four-old"]  # "behind" never drops the only copy


def test_store_relogin_pins_the_event(temp_home):
    s = _switcher(temp_home, active="4")
    live_old = s._read_credentials()
    assert _run(s, FakeLogin()).activated
    pin = _pin(s)
    assert pin["newFp"] == oauth.credential_fingerprint(_creds("rt-four-new"))
    assert oauth.credential_fingerprint(live_old) in pin["oldFps"]
    assert pin["deadline"] == D


@pytest.mark.parametrize("backup,live,refuses", [
    ("new", "old", True),                       # the replaced login
    ("new", "old-rotated-days-earlier", True),  # a rotation of it
    ("new", "new", False),
    ("new", "jitter-earlier", False),           # a rotation of the new login
    ("new", "no-deadline", False),              # unknowable: pre-guard behaviour
    ("moved-on", "old", False),                 # pin stale: backup rotated past it
])
def test_relogin_pin_refuses_only_the_replaced_login(temp_home, backup, live, refuses):
    s = _switcher(temp_home)
    s._write_account_credentials("4", FOUR, OLD)
    s.store_relogin("4", _creds("rt-four-new"), _account(FOUR, ORG4, "uuid-4"),
                    activate=False)
    blobs = {
        "new": _creds("rt-four-new"), "old": OLD,
        "old-rotated-days-earlier": _blob("rt-x", D - 2 * DAY_MS),
        "jitter-earlier": _blob("rt-y", D - 2000),
        "no-deadline": _blob("rt-z", None),
        "moved-on": _blob("rt-four-gen2", D),
    }
    assert s._relogin_pin_refuses("4", blobs[backup], blobs[live]) is refuses


# -- leftovers -------------------------------------------------------------------------


def test_sweep_removes_old_profiles_and_their_keychain_items(tmp_path, monkeypatch, block_real_keychain):
    import os

    from claude_swap import macos_keychain
    from claude_swap.session import keychain_service_name

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    old = tmp_path / f"{rl.PROFILE_PREFIX}old"
    fresh = tmp_path / f"{rl.PROFILE_PREFIX}fresh"
    for d in (old, fresh):
        d.mkdir()
        (d / ".credentials.json").write_text(_creds("rt-left"))
        block_real_keychain.set_password(
            keychain_service_name(d), macos_keychain.keychain_account_name(), _creds("rt-left"))
    os.utime(old, (1, 1))
    assert rl.sweep_stale_profiles(tmp_path) == [old]
    assert not old.exists() and fresh.exists()
    services = {k[0] for k in block_real_keychain.data}
    assert keychain_service_name(old) not in services
    assert keychain_service_name(fresh) in services


def test_a_new_attempt_sweeps_leftovers(temp_home):
    import os

    s = _switcher(temp_home)
    left = Path(s.backup_dir) / f"{rl.PROFILE_PREFIX}crashed"
    left.mkdir()
    (left / ".credentials.json").write_text(_creds("rt-left"))
    os.utime(left, (1, 1))
    assert _run(s, FakeLogin()).ok
    assert not left.exists()


def test_sigterm_during_the_login_still_cleans_up(temp_home):
    import signal as _signal

    if not hasattr(_signal, "SIGTERM") or sys.platform == "win32":
        pytest.skip("POSIX signals")
    s = _switcher(temp_home)
    seen = []

    def killed(argv, env, cwd):
        seen.append(Path(cwd))
        _signal.raise_signal(_signal.SIGTERM)
        return 0

    before = _signal.getsignal(_signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        _run(s, killed)
    assert not seen[0].exists()
    assert _signal.getsignal(_signal.SIGTERM) == before  # handler restored


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


# -- session shells, interrupts, leftovers (review round 3) ----------------------------


def _inside_session_shell(s, monkeypatch):
    inside = Path(s.backup_dir) / "sessions" / f"4-{FOUR}"
    inside.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inside))


def test_store_relogin_refuses_inside_a_session_shell(temp_home, monkeypatch):
    from claude_swap.exceptions import SwitchError

    s = _switcher(temp_home, active="4")
    _inside_session_shell(s, monkeypatch)
    with pytest.raises(SwitchError):
        s.store_relogin("4", _creds("rt-four-new"), _account(FOUR, ORG4, "uuid-4"))
    with pytest.raises(SwitchError):
        s.store_relogin("4", _creds("rt-four-new"), _account(FOUR, ORG4, "uuid-4"),
                        activate=False)
    assert _slot_rt(s) == "rt-four-dead"
    assert s._get_sequence_data()["activeAccountNumber"] == 4
    assert "reloginPin" not in s._get_sequence_data()["accounts"]["4"]


def test_login_refuses_inside_a_session_shell_before_the_browser(temp_home, monkeypatch):
    from claude_swap.exceptions import SwitchError

    s = _switcher(temp_home)
    _inside_session_shell(s, monkeypatch)
    login = FakeLogin()
    with pytest.raises(SwitchError):
        _run(s, login)
    assert not login.calls and not _leftover_profiles(s)


def test_an_interrupt_during_the_store_keeps_the_login_then_unwinds(temp_home, monkeypatch):
    s = _switcher(temp_home)

    def interrupted(*a, **k):
        raise KeyboardInterrupt  # SIGTERM via terminate_as_interrupt, or Ctrl-C

    monkeypatch.setattr(s, "store_relogin", interrupted)
    login = FakeLogin()
    with pytest.raises(KeyboardInterrupt):
        _run(s, login)
    assert _stashed_rts(s) == ["rt-four-new"]
    assert not login.profile.exists()


def test_rollback_runs_every_step_through_an_interrupt(temp_home, monkeypatch):
    s = _switcher(temp_home, active="4")
    live_before = s._read_credentials()
    real_write_json = s._write_json
    real_write_creds = s._write_credentials
    state = {"stage": "store"}

    def failing_seq(path, data):
        if path == s.sequence_file and state["stage"] == "store":
            state["stage"] = "rollback"
            raise OSError(28, "full")
        return real_write_json(path, data)

    def interrupt_first_restore(creds):
        if state["stage"] == "rollback" and creds == live_before:
            state["stage"] = "done"
            real_write_creds(creds)
            raise KeyboardInterrupt  # mid-rollback; later steps must still run
        return real_write_creds(creds)

    monkeypatch.setattr(s, "_write_json", failing_seq)
    monkeypatch.setattr(s, "_write_credentials", interrupt_first_restore)
    with pytest.raises(KeyboardInterrupt):
        s.store_relogin("4", _creds("rt-four-new"), _account(FOUR, ORG4, "uuid-4"))
    assert _slot_rt(s) == "rt-four-dead"
    assert s._read_credentials() == live_before
    # the live-config restore (the step after the interrupted one) still ran
    assert json.loads((temp_home / ".claude.json").read_text())["oauthAccount"]["accountUuid"] == "uuid-4"


def test_sweep_skips_a_profile_whose_login_is_still_running(tmp_path):
    import os
    import time

    waiting = tmp_path / f"{rl.PROFILE_PREFIX}waiting"
    waiting.mkdir()
    (waiting / rl.PID_FILE).write_text(str(os.getpid()))
    two_hours_ago = time.time() - 7200  # past the age limit, owner still alive
    os.utime(waiting, (two_hours_ago, two_hours_ago))
    assert rl.sweep_stale_profiles(tmp_path) == []
    assert waiting.exists()


def test_sweep_keeps_a_profile_whose_keychain_item_could_not_be_deleted(
    tmp_path, monkeypatch
):
    import os

    from claude_swap import session

    left = tmp_path / f"{rl.PROFILE_PREFIX}stuck"
    left.mkdir()
    os.utime(left, (1, 1))
    monkeypatch.setattr(session, "delete_macos_keychain_entry", lambda p: False)
    assert rl.sweep_stale_profiles(tmp_path) == []
    assert left.exists()  # its path is the only name of the item: retry later
    monkeypatch.setattr(session, "delete_macos_keychain_entry", lambda p: True)
    assert rl.sweep_stale_profiles(tmp_path) == [left]
    assert not left.exists()
