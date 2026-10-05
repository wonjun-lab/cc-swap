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


def test_live_login_held_by_a_slot_that_is_not_live(temp_home):
    s = _switcher(temp_home, live=_creds("rt-2"))  # ~/.claude.json names #1
    assert s.shared_login_places("2", _creds("rt-2"), is_active=False) == [
        ("the live login", "1")
    ]
    outcome, post = _consume(s, 2)
    assert outcome.error == shared_login.SHARED_LOGIN
    post.assert_not_called()


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
