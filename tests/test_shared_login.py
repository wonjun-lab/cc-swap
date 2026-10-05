"""One refresh token in two places (cc-swap fork, shared_login.py).

Refresh tokens are one-time use, so two places holding one cannot both stay
logged in: cc-swap must not be the one that decides which copy dies. The
consume gate (usage fetch, freshen, priming) and the active fetch path refuse
to refresh such a login with ``shared-login`` until one side is re-logged.
No network: the refresh POST is patched and asserted never to run.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from claude_swap import oauth, shared_login
from claude_swap.autoswitch import _SYSTEMIC_STATUSES
from claude_swap.session import session_dir_for
from claude_swap.switcher import ERROR_NOTES, ClaudeAccountSwitcher

EXPIRED = 1000  # expiresAt in ms: long gone
FRESH = 9_999_999_999_000


def _creds(rt: str, *, expires: int = EXPIRED) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": f"at-{rt}", "refreshToken": rt, "expiresAt": expires,
    }})


def _email(n: int | str) -> str:
    return f"user{n}@example.com"


def _switcher(home: Path, *, live: str | None = None) -> ClaudeAccountSwitcher:
    """Slots 1-3, each with its own login; #1 is the live account."""
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    data = s._get_sequence_data()
    data["accounts"] = {
        str(n): {"email": _email(n), "uuid": f"uuid-{n}", "organizationUuid": "",
                 "organizationName": ""}
        for n in (1, 2, 3)
    }
    data["sequence"] = [1, 2, 3]
    data["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, data)
    for n in (1, 2, 3):
        s._write_account_credentials(str(n), _email(n), _creds(f"rt-{n}"))
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": _email(1), "organizationUuid": "", "accountUuid": "uuid-1",
    }}))
    s._write_credentials(live or _creds("rt-1"))
    return s


def _live(home: Path, n: int) -> None:
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": _email(n), "organizationUuid": "", "accountUuid": f"uuid-{n}",
    }}))


def _profile(s, n, value: str, *, email: str | None = None) -> Path:
    path = session_dir_for(s.backup_dir, str(n), email or _email(n))
    path.mkdir(parents=True, exist_ok=True)
    (path / ".credentials.json").write_text(value)
    return path


def _consume(s, n):
    with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
        post.return_value = oauth.RefreshOutcome(_creds(f"rt-{n}-next", expires=FRESH), None)
        outcome = s.consume_backup_grant(str(n), _email(n), s._read_account_credentials(
            str(n), _email(n)))
    return outcome, post


def test_two_slots_with_one_refresh_token_are_not_refreshed(temp_home):
    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-2"))  # #3 got #2's login
    for n in (2, 3):
        outcome, post = _consume(s, n)
        assert outcome.error == shared_login.SHARED_LOGIN and outcome.credentials is None
        post.assert_not_called()
    assert s._read_account_credentials("2", _email(2)) == _creds("rt-2")  # untouched


def test_the_places_name_slots_never_tokens(temp_home):
    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-2"))
    places = s.shared_login_places("2", _creds("rt-2"), is_active=False)
    assert places == [("#3", "3")]


def test_a_slot_of_its_own_is_refreshed(temp_home):
    s = _switcher(temp_home)
    outcome, post = _consume(s, 2)
    assert outcome.error is None
    post.assert_called_once()


def test_after_a_relogin_of_one_side_it_is_refreshed_again(temp_home):
    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-2"))
    assert _consume(s, 2)[0].error == shared_login.SHARED_LOGIN
    s._write_account_credentials("3", _email(3), _creds("rt-3-new"))  # cc-swap login 3
    outcome, post = _consume(s, 2)
    assert outcome.error is None
    post.assert_called_once()


def test_own_cswap_run_profile_is_not_sharing(temp_home):
    s = _switcher(temp_home)
    _profile(s, 2, _creds("rt-2"))
    assert s.shared_login_places("2", _creds("rt-2"), is_active=False) == []
    assert _consume(s, 2)[0].error is None


def test_another_slots_cswap_run_profile_is_sharing(temp_home):
    s = _switcher(temp_home)
    _profile(s, 3, _creds("rt-2"))
    assert s.shared_login_places("2", _creds("rt-2"), is_active=False) == [
        ("#3's cswap run profile", "3")
    ]
    outcome, post = _consume(s, 2)
    assert outcome.error == shared_login.SHARED_LOGIN
    post.assert_not_called()


def test_a_leftover_profile_is_named_without_its_email(temp_home):
    s = _switcher(temp_home)
    _profile(s, 3, _creds("rt-2"), email="gone@example.com")
    [(label, slot)] = s.shared_login_places("2", _creds("rt-2"), is_active=False)
    assert label == "a leftover cswap run profile made for #3" and slot is None


def test_live_login_held_by_a_slot_that_is_not_live_is_deferred(temp_home):
    """Not POSTed, but a deferral rather than ``shared-login``: the shape is
    also a switch's moment; doctor's live-login check reports the lasting one."""
    s = _switcher(temp_home, live=_creds("rt-2"))  # ~/.claude.json names #1
    assert s.shared_login_places("2", _creds("rt-2"), is_active=False) == [
        ("the live login", "1")
    ]
    outcome, post = _consume(s, 2)
    assert outcome.error == "transient"
    post.assert_not_called()


def test_a_switch_landing_during_the_live_read_is_not_sharing(temp_home, monkeypatch):
    """The live account is resolved again right after the live read."""
    s = _switcher(temp_home)
    real = s._read_active_credentials

    def switched_meanwhile():
        _live(temp_home, 2)  # the switch to #2 writes its identity now
        s._write_credentials(_creds("rt-2"))
        return real()

    monkeypatch.setattr(s, "_read_active_credentials", switched_meanwhile)
    assert s._live_shares("2", shared_login.refresh_fingerprint(_creds("rt-2"))) is None


def test_the_live_accounts_backup_is_its_own_copy(temp_home):
    s = _switcher(temp_home)  # live rt-1 == #1's backup
    assert s.shared_login_places("1", _creds("rt-1"), is_active=True) == []


def test_active_path_does_not_refresh_a_shared_live_login(temp_home):
    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-1"))  # #3 holds the live login
    with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
        record = s._fetch_active_usage("1", _email(1), _creds("rt-1"))
    post.assert_not_called()
    assert record.error == shared_login.SHARED_LOGIN


def test_usage_fetch_of_an_expired_shared_slot_never_posts(temp_home):
    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-2"))
    info = (2, _email(2), "", "", False, _creds("rt-2"), "")
    with (
        patch("claude_swap.oauth.try_refresh_oauth_credentials") as post,
        patch("claude_swap.oauth.request_usage_data") as usage,
    ):
        record = s._fetch_account_usage(info)
    post.assert_not_called()
    usage.assert_not_called()  # the known-expired token is not even tried
    assert record.error == shared_login.SHARED_LOGIN


def test_usage_fetch_of_a_valid_shared_slot_still_reads_usage(temp_home):
    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-2", expires=FRESH))
    s._write_account_credentials("2", _email(2), _creds("rt-2", expires=FRESH))
    info = (2, _email(2), "", "", False, _creds("rt-2", expires=FRESH), "")
    with (
        patch("claude_swap.oauth.try_refresh_oauth_credentials") as post,
        patch("claude_swap.oauth.request_usage_data", return_value={}),
    ):
        s._fetch_account_usage(info)
    post.assert_not_called()


def test_the_kind_has_a_remedy_everywhere():
    assert shared_login.SHARED_LOGIN in ERROR_NOTES
    assert shared_login.SHARED_LOGIN in oauth._DETERMINISTIC_REFRESH_ERRORS
    assert shared_login.SHARED_LOGIN in _SYSTEMIC_STATUSES  # a tick names it, not "network?"
    assert "cc-swap doctor" in ERROR_NOTES[shared_login.SHARED_LOGIN]


def test_freshen_reports_the_kind_and_the_tick_skips_the_candidate(temp_home):
    from claude_swap.autoswitch import AutoSwitchEngine

    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-2"))
    engine = AutoSwitchEngine.__new__(AutoSwitchEngine)
    engine.switcher = s
    engine.clock = lambda: 1_790_000_000.0
    with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
        assert engine._freshen_target("2", _email(2)) == shared_login.SHARED_LOGIN
    post.assert_not_called()


def test_fix_names_every_slot_once():
    assert shared_login.fix(["3", "2", "3"]) == (
        "re-login one of them: cc-swap login 3 or cc-swap login 2"
    )
    assert shared_login.fix([]).startswith("re-login")


def test_a_stale_marked_profile_is_not_a_sharer(temp_home):
    from claude_swap.session import mark_session_stale

    s = _switcher(temp_home)
    path = _profile(s, 3, _creds("rt-2"))
    mark_session_stale(path)
    assert s.shared_login_places("2", _creds("rt-2"), is_active=False) == []


def test_an_unreadable_keychain_item_is_unknown_and_keeps_keychain_mode(
    temp_home, monkeypatch, block_real_keychain
):
    from claude_swap import macos_keychain
    from claude_swap.models import Platform

    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    s = _switcher(temp_home)
    s._store._host.platform = Platform.MACOS
    for n in (1, 2, 3):  # no .enc files: every backup lives in the Keychain
        (s.credentials_dir / f".creds-{n}-{_email(n)}.enc").unlink(missing_ok=True)
    real = macos_keychain.get_password

    def denied(service, account):
        if account.startswith("account-3-"):
            raise macos_keychain.KEYCHAIN_ERRORS[0]("rc=36")
        return real(service, account)

    monkeypatch.setattr(macos_keychain, "get_password", denied)
    assert s._store.peek_account_credentials("3", _email(3)) is None
    assert s.shared_login_places("2", _creds("rt-2"), is_active=True) == []
    assert s._store._keychain_usable_cache is not False


# -- the active account: only the POST is refused, never a restore ------------------------


def _new_login_on_1(s) -> str:
    new = _creds("rt-1-new", expires=FRESH)
    s.store_relogin("1", new, {"emailAddress": _email(1), "organizationUuid": "",
                              "accountUuid": "uuid-1"})
    return new


def test_pinned_relogin_heal_runs_although_the_old_login_is_shared(temp_home):
    """#1 and #3 shared rt-1 → cc-swap login 1 (live) → an old session writes
    the old login back: the live bytes are #3's token under #1's name. The
    pin restores the new login without a POST — nothing may refuse that."""
    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-1"))
    new = _new_login_on_1(s)
    s._write_credentials(_creds("rt-1"))  # the old session's write-back
    with (
        patch("claude_swap.oauth.try_refresh_oauth_credentials") as post,
        patch("claude_swap.oauth.request_usage_data", return_value={}),
    ):
        record = s._fetch_active_usage("1", _email(1), _creds("rt-1"))
    post.assert_not_called()
    assert record.error != shared_login.SHARED_LOGIN
    assert oauth.extract_oauth_data(s._read_credentials())["refreshToken"] == "rt-1-new"
    assert s._read_account_credentials("1", _email(1)) == new


def test_a_newer_backup_is_restored_although_the_stale_live_is_shared(temp_home):
    """A stale sync put #3's (expired) token live under #1's name; #1's own
    backup is newer and valid: restored, no POST, no refusal."""
    s = _switcher(temp_home)
    s._write_account_credentials("1", _email(1), _creds("rt-1", expires=FRESH))
    s._write_credentials(_creds("rt-3"))
    with (
        patch("claude_swap.oauth.try_refresh_oauth_credentials") as post,
        patch("claude_swap.oauth.request_usage_data", return_value={}),
    ):
        record = s._fetch_active_usage("1", _email(1), _creds("rt-3"))
    post.assert_not_called()
    assert record.error != shared_login.SHARED_LOGIN
    assert oauth.extract_oauth_data(s._read_credentials())["refreshToken"] == "rt-1"


def test_a_backup_chosen_for_the_post_is_checked_too(temp_home):
    """The bytes POSTed are the backup (the live bytes are older and not its
    lineage) and #3 holds that backup's token: refused at the POST."""
    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-1"))  # #3 = #1's backup
    older = _creds("rt-x", expires=500)
    s._write_credentials(older)
    with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
        record = s._fetch_active_usage("1", _email(1), older)
    post.assert_not_called()
    assert record.error == shared_login.SHARED_LOGIN


# -- freshen, manual switch, cswap run ----------------------------------------------------


def test_freshen_refuses_a_shared_candidate_even_with_a_valid_token(temp_home):
    from claude_swap.autoswitch import AutoSwitchEngine

    s = _switcher(temp_home)
    for n in (2, 3):
        s._write_account_credentials(str(n), _email(n), _creds("rt-2", expires=FRESH))
    engine = AutoSwitchEngine.__new__(AutoSwitchEngine)
    engine.switcher = s
    engine.clock = lambda: 1_790_000_000.0
    assert engine._freshen_target("2", _email(2)) == shared_login.SHARED_LOGIN
    s._write_account_credentials("3", _email(3), _creds("rt-3", expires=FRESH))
    assert engine._freshen_target("2", _email(2)) == "ok"


def test_a_manual_switch_onto_a_shared_slot_is_refused(temp_home):
    import pytest

    from claude_swap.exceptions import SwitchRefusedError

    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-2"))
    with pytest.raises(SwitchRefusedError) as info:
        s.switch_to("2")
    assert info.value.reason == shared_login.SHARED_LOGIN
    assert "also held by #3" in str(info.value) and "--allow-dead-login" in str(info.value)
    payload = s.switch_to("2", json_output=True)
    assert payload["reason"] == shared_login.SHARED_LOGIN and not payload["switched"]
    assert s.current_account_number() == "1"


def test_cswap_run_refuses_to_seed_a_shared_login(temp_home):
    import pytest

    from claude_swap.exceptions import SessionError
    from claude_swap.session import SessionManager

    s = _switcher(temp_home)
    s._write_account_credentials("3", _email(3), _creds("rt-2"))
    with (
        patch("claude_swap.oauth.try_refresh_oauth_credentials") as post,
        pytest.raises(SessionError, match="also held by #3"),
    ):
        SessionManager(s).setup_session("2", share=False)
    post.assert_not_called()
    assert not session_dir_for(s.backup_dir, "2", _email(2)).exists()
