"""Bare `cc-swap login` / Fleet's *Sign in (add or renew)*: Claude Code's own
login in a throwaway profile, then — by who signed in — the slot holding
that account is renewed (like `cc-swap login N`), a new account is added
(like `--new`), and a partial match stores nothing (maximize/relogin.py
``sign_in`` / ``match_login``).

Same fakes as test_relogin: no real claude, no real Keychain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import cli, oauth
from claude_swap.maximize import relogin as rl
from tests.maximize.test_relogin import (
    CLAUDE,
    FOUR,
    ORG4,
    FakeLogin,
    _creds,
    _leftover_profiles,
    _slot_rt,
    _stashed_rts,
    _switcher,
)

NEW = "new@example.com"


def _run(s, login, new: rl.NewAccount | None = None, **kw):
    return rl.sign_in(s, new or rl.NewAccount(), claude=CLAUDE, run=login,
                      announce=None, **kw)


def _new_login(**kw) -> FakeLogin:
    kw.setdefault("email", NEW)
    kw.setdefault("org", "org-new")
    kw.setdefault("uuid", "uuid-new")
    kw.setdefault("rt", "rt-new")
    return FakeLogin(**kw)


def _live_files(home: Path, s) -> tuple[str | None, str]:
    return s._read_credentials(), (home / ".claude.json").read_text()


def _sequence(s) -> str:
    return json.dumps(s._get_sequence_data(), sort_keys=True)


@pytest.fixture(autouse=True)
def _never_ask(monkeypatch):
    """The bare form never prompts."""
    def ask(*a, **k):
        raise AssertionError("bare cc-swap login must not prompt")

    monkeypatch.setattr("builtins.input", ask)


# -- an account cc-swap has: renewed --------------------------------------------------------


def test_an_existing_account_is_renewed_and_the_live_login_left_alone(temp_home):
    s = _switcher(temp_home)  # live: #1
    live_before = _live_files(temp_home, s)
    login = FakeLogin()  # signs in as #4's account
    outcome = _run(s, login)
    assert outcome.ok and outcome.number == "4" and not outcome.activated
    assert outcome.message.startswith("updated #4 four (token renewed; login now ends ")
    assert "logged in as" not in outcome.message
    assert _slot_rt(s) == "rt-four-new"
    assert set(s._get_sequence_data()["accounts"]) == {"1", "4", "5"}  # nothing added
    assert _live_files(temp_home, s) == live_before
    assert s.current_account_number() == "1"
    assert _stashed_rts(s) == []
    assert login.calls[0][0] == [CLAUDE, "auth", "login", "--claudeai"]
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_the_live_account_is_renewed_live_too_and_stays_the_account(temp_home):
    s = _switcher(temp_home, active="4")
    outcome = _run(s, FakeLogin())
    assert outcome.ok and outcome.number == "4" and outcome.activated
    assert "#4 is the account Claude Code is logged in as" in outcome.message
    assert "same account, nothing switched" in outcome.message
    assert _slot_rt(s) == "rt-four-new"
    assert oauth.extract_oauth_data(s._read_credentials())["refreshToken"] == "rt-four-new"
    assert s.current_account_number() == "4"
    assert not _leftover_profiles(s)


def test_renew_matches_the_email_case_insensitively(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(email=FOUR.upper()))
    assert outcome.ok and outcome.number == "4"


def test_renew_runs_the_token_owner_check_and_keeps_a_refused_login(temp_home, monkeypatch):
    s = _switcher(temp_home)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: {
        "uuid": "uuid-x", "email": "x@example.com", "organizationUuid": ORG4})
    before = _sequence(s)
    outcome = _run(s, FakeLogin())
    assert outcome.status == rl.MISMATCH and "x@example.com" in outcome.message
    assert _slot_rt(s) == "rt-four-dead" and _sequence(s) == before
    assert "cc-swap unclaimed" in outcome.message and _stashed_rts(s) == ["rt-four-new"]


def test_a_slot_asked_for_an_existing_account_is_not_used(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(), rl.NewAccount(slot="6"))
    assert outcome.ok and outcome.number == "4"
    assert "--slot 6 not used: this account is already #4" in outcome.message
    assert "6" not in s._get_sequence_data()["accounts"]


# -- an account cc-swap does not have: added ------------------------------------------------


def test_a_new_account_is_added_to_the_next_free_slot(temp_home):
    s = _switcher(temp_home)
    live_before = _live_files(temp_home, s)
    login = _new_login()
    outcome = _run(s, login)
    assert outcome.ok and outcome.number == "6"
    assert outcome.message == "added #6 new@example.com [Team]"
    assert _slot_rt(s, "6", NEW) == "rt-new"
    assert _slot_rt(s) == "rt-four-dead"  # #4 untouched
    assert _live_files(temp_home, s) == live_before
    assert s.current_account_number() == "1"
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_slot_is_honoured_for_a_new_account(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, _new_login(), rl.NewAccount(slot="3"))
    assert outcome.ok and outcome.number == "3"
    assert s._get_sequence_data()["sequence"] == [1, 3, 4, 5]


def test_a_malformed_slot_is_refused_before_the_browser(temp_home):
    s = _switcher(temp_home)
    login = _new_login()
    with pytest.raises(Exception, match="slot number"):
        _run(s, login, rl.NewAccount(slot="zero"))
    assert not login.calls and not _leftover_profiles(s)


def test_a_taken_slot_does_not_block_a_renew_of_that_account(temp_home):
    """`cc-swap login --slot 4` signing in as #4 itself: a renew, not refused."""
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(), rl.NewAccount(slot="4"))
    assert outcome.ok and outcome.number == "4" and _slot_rt(s) == "rt-four-new"
    assert "--slot" not in outcome.message  # the slot asked for IS the account's


def test_adding_to_a_taken_slot_is_refused_when_storing_and_kept(temp_home):
    s = _switcher(temp_home)
    before = _sequence(s)
    login = _new_login()
    outcome = _run(s, login, rl.NewAccount(slot="4"))
    assert login.calls  # the slot's use was known only after the sign-in
    assert outcome.status == rl.FAILED and "slot 4 is taken" in outcome.message
    assert "cc-swap unclaimed" in outcome.message and _stashed_rts(s) == ["rt-new"]
    assert _sequence(s) == before and _slot_rt(s) == "rt-four-dead"
    assert not _leftover_profiles(s)


def test_the_email_only_pre_fills(temp_home):
    s = _switcher(temp_home)
    login = _new_login()
    outcome = _run(s, login, rl.NewAccount(email="typo@example.com"))
    assert login.calls[0][0][-2:] == ["--email", "typo@example.com"]
    assert outcome.ok and s._get_sequence_data()["accounts"][outcome.number]["email"] == NEW


def test_the_plan_is_in_the_added_line(temp_home):
    s = _switcher(temp_home)
    tiered = json.dumps({"claudeAiOauth": {
        **json.loads(_creds("rt-new"))["claudeAiOauth"],
        "rateLimitTier": "default_claude_max_20x",
    }})
    login = _new_login()

    def with_tier(argv, env, cwd):
        code = login(argv, env, cwd)
        (Path(cwd) / ".credentials.json").write_text(tiered)
        return code

    outcome = _run(s, with_tier)
    assert outcome.ok and outcome.message == "added #6 new@example.com [Team · 20x]"


# -- a partial match: nothing stored, the commands named ------------------------------------


def test_same_email_in_another_org_is_not_guessed(temp_home):
    s = _switcher(temp_home)
    before, live_before = _sequence(s), _live_files(temp_home, s)
    outcome = _run(s, FakeLogin(org="org-other", uuid="uuid-4"))
    assert outcome.status == rl.AMBIGUOUS and outcome.number == "4"
    assert ("#4 is four@example.com in org org-4; you signed in to org org-other"
            in outcome.message)
    assert "cc-swap login 4" in outcome.message
    assert "cc-swap login --new [--slot N]" in outcome.message  # adding it is allowed
    assert "cc-swap unclaimed" in outcome.message and _stashed_rts(s) == ["rt-four-new"]
    assert _sequence(s) == before and _slot_rt(s) == "rt-four-dead"
    assert _live_files(temp_home, s) == live_before
    assert not _leftover_profiles(s)


def test_the_same_account_id_under_a_changed_email_is_not_guessed(temp_home):
    s = _switcher(temp_home)
    before = _sequence(s)
    outcome = _run(s, FakeLogin(email="renamed@example.com"))  # uuid-4, org-4
    assert outcome.status == rl.AMBIGUOUS and outcome.number == "4"
    assert "#4 is this account id under another email, four@example.com" in outcome.message
    assert "cc-swap remove 4; cc-swap login" in outcome.message
    assert "--new" not in outcome.message  # --new would refuse it as #4's
    assert _sequence(s) == before and _slot_rt(s) == "rt-four-dead"
    assert _stashed_rts(s) == ["rt-four-new"]


def test_the_same_email_under_another_account_id_is_not_guessed(temp_home):
    s = _switcher(temp_home)
    before = _sequence(s)
    outcome = _run(s, FakeLogin(uuid="uuid-other"))
    assert outcome.status == rl.AMBIGUOUS
    assert "another account id" in outcome.message
    assert _sequence(s) == before and _slot_rt(s) == "rt-four-dead"


def test_an_api_key_slot_under_that_email_is_not_guessed(temp_home):
    s = _switcher(temp_home)
    before = _sequence(s)
    outcome = _run(s, FakeLogin(email="key@example.com", org="", uuid="uuid-k"))
    assert outcome.status == rl.AMBIGUOUS and "#5 is an API key" in outcome.message
    assert "cc-swap remove 5; cc-swap login" in outcome.message
    assert _sequence(s) == before


def test_a_slot_without_an_organization_is_offered_a_replacement_not_login_n(temp_home):
    """A setup-token / add-token slot stores no organization: `cc-swap login 4`
    compares it strictly and could never succeed, so it is not suggested."""
    s = _switcher(temp_home)
    data = s._get_sequence_data()
    data["accounts"]["4"]["organizationUuid"] = ""
    s._write_json(s.sequence_file, data)
    outcome = _run(s, FakeLogin())  # four@example.com in org-4
    assert outcome.status == rl.AMBIGUOUS
    assert "cc-swap remove 4; cc-swap login" in outcome.message
    assert "stored without an organization" in outcome.message
    assert "cc-swap login 4 " not in outcome.message + " "
    assert "cc-swap login --new" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"


def test_match_login_rules():
    data = {"accounts": {
        "4": {"email": FOUR, "organizationUuid": ORG4, "uuid": "uuid-4"},
        "7": {"email": FOUR, "organizationUuid": "", "uuid": "uuid-4"},
        "8": {"email": "olduuidless@example.com", "organizationUuid": "", "uuid": ""},
    }}

    def match(email, org, uuid=""):
        return rl.match_login(data, {"emailAddress": email, "organizationUuid": org,
                                     "accountUuid": uuid})

    # Both orgs stored: each sign-in renews its own slot.
    assert match(FOUR, ORG4, "uuid-4") == rl.LoginMatch(rl.RENEW, "4")
    assert match(FOUR, "", "uuid-4") == rl.LoginMatch(rl.RENEW, "7")
    assert match("OLDUUIDLESS@example.com", "", "uuid-9").kind == rl.RENEW  # no uuid stored
    assert match(NEW, "", "uuid-new").kind == rl.ADD
    assert match(FOUR, "org-third", "uuid-4").kind == rl.AMBIGUOUS
    twice = {"accounts": {**data["accounts"], "9": dict(data["accounts"]["4"])}}
    m = rl.match_login(twice, {"emailAddress": FOUR, "organizationUuid": ORG4,
                               "accountUuid": "uuid-4"})
    assert m.kind == rl.AMBIGUOUS and {"cc-swap login 4", "cc-swap login 9"} <= {
        cmd for cmd, _ in m.fixes}


# -- cancel / failure -----------------------------------------------------------------------


@pytest.mark.parametrize("code, status", [(None, rl.CANCELLED), (1, rl.FAILED)])
def test_cancel_or_failure_stores_nothing_and_cleans_up(temp_home, code, status):
    s = _switcher(temp_home)
    before, live_before = _sequence(s), _live_files(temp_home, s)
    login = FakeLogin(code=code, write=False)
    outcome = _run(s, login)
    assert outcome.status == status and "nothing stored" in outcome.message
    assert _sequence(s) == before and _live_files(temp_home, s) == live_before
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_a_login_that_names_no_account_is_kept_and_says_sign_in_again(temp_home):
    s = _switcher(temp_home)
    before = _sequence(s)
    login = FakeLogin()

    def no_account(argv, env, cwd):
        code = login(argv, env, cwd)
        (Path(cwd) / ".claude.json").write_text("{}")
        return code

    outcome = _run(s, no_account)
    assert outcome.status == rl.FAILED and "names no account" in outcome.message
    assert "cc-swap unclaimed" in outcome.message and _stashed_rts(s) == ["rt-four-new"]
    assert outcome.message.endswith("Sign in again: cc-swap login")
    assert _sequence(s) == before and not _leftover_profiles(s)


def test_an_unreadable_login_is_salvaged_best_effort(temp_home, monkeypatch):
    s = _switcher(temp_home)

    def unreadable(self):
        raise OSError("profile torn")

    monkeypatch.setattr(rl.SignInAttempt, "read_login", unreadable)
    outcome = _run(s, FakeLogin())
    assert outcome.status == rl.FAILED and "could not read the new login" in outcome.message
    assert _stashed_rts(s) == ["rt-four-new"]  # the credential itself was readable
    assert "Sign in again: cc-swap login" in outcome.message
    assert not _leftover_profiles(s)


def test_a_login_claude_did_not_save_says_sign_in_again(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(write=False))  # exit 0, nothing written
    assert outcome.status == rl.FAILED and "claude saved no login" in outcome.message
    assert "Sign in again: cc-swap login" in outcome.message and _stashed_rts(s) == []


@pytest.mark.parametrize("make, retry", [
    (lambda: rl.relogin, "cc-swap login 4"),
    (lambda: rl.login_new, "cc-swap login --new"),
])
def test_the_explicit_forms_say_how_to_retry_too(temp_home, make, retry):
    s = _switcher(temp_home)
    fn = make()
    first = "4" if fn is rl.relogin else rl.NewAccount()
    outcome = fn(s, first, claude=CLAUDE, run=FakeLogin(write=False), announce=None)
    assert outcome.status == rl.FAILED and "nothing stored" in outcome.message
    assert outcome.message.endswith(f"Sign in again: {retry}")


def test_failure_warnings_name_the_type_and_slot_not_the_email(temp_home, monkeypatch, caplog):
    s = _switcher(temp_home)

    def stash(*a, **k):
        raise OSError(f"cannot write .creds-4-{FOUR}.enc")

    monkeypatch.setattr(s, "stash_relogin_credential", stash)
    with caplog.at_level("WARNING", logger="claude-swap"):
        outcome = _run(s, FakeLogin(email="renamed@example.com"))
    assert outcome.status == rl.AMBIGUOUS
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("OSError" in w and "#4" in w for w in warnings)
    assert not any(FOUR in w or "renamed@example.com" in w for w in warnings)
    assert not any(r.exc_info for r in caplog.records if r.levelname == "WARNING")


def test_a_store_failure_keeps_the_login(temp_home, monkeypatch):
    s = _switcher(temp_home)
    real = s._write_json

    def failing(path, data):
        if Path(path) == Path(s.sequence_file) and "6" in (data.get("accounts") or {}):
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(s, "_write_json", failing)
    outcome = _run(s, _new_login())
    assert outcome.status == rl.FAILED and "disk full" in outcome.message
    assert "cc-swap unclaimed" in outcome.message and _stashed_rts(s) == ["rt-new"]
    assert "6" not in s._get_sequence_data()["accounts"]
    assert not _leftover_profiles(s)


def test_refuses_inside_a_session_shell_before_the_browser(temp_home, monkeypatch):
    s = _switcher(temp_home)
    inside = s.backup_dir / "sessions" / "4-four_example.com"
    inside.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inside))
    login = FakeLogin()
    with pytest.raises(Exception, match="(?i)session"):
        _run(s, login)
    assert not login.calls


def test_sign_in_never_logs_tokens_or_emails(temp_home, caplog):
    s = _switcher(temp_home)
    with caplog.at_level("INFO", logger="claude-swap"):
        assert _run(s, FakeLogin()).ok
        assert _run(s, _new_login()).ok
        assert _run(s, FakeLogin(email="renamed@example.com", rt="rt-r")).status == rl.AMBIGUOUS
    for secret in ("rt-four-new", "rt-new", "rt-r", FOUR, NEW, "renamed@example.com"):
        assert secret not in caplog.text


# -- CLI ------------------------------------------------------------------------------------


@pytest.fixture
def cli_sign_in(monkeypatch):
    """Route ``sign_in(..., run=run_interactive)`` to a fake login."""
    holder: dict = {}
    real = rl.sign_in

    def sign_in(switcher, new, *, claude, **kw):
        kw["run"] = holder["login"]
        return real(switcher, new, claude=claude, **kw)

    monkeypatch.setattr(rl, "sign_in", sign_in)
    return holder


def _cli(monkeypatch, argv, *, claude=CLAUDE, supported=True):
    monkeypatch.setattr("claude_swap.maximize.primer.resolve_claude_path",
                        lambda configured, **k: claude)
    monkeypatch.setattr(rl, "login_supported", lambda c, **k: supported)
    with pytest.raises(SystemExit) as exit_info:
        cli._login_command(argv)
    return exit_info.value.code


def test_cli_bare_renews_and_exits_zero(temp_home, monkeypatch, capsys, cli_sign_in):
    s = _switcher(temp_home)
    cli_sign_in["login"] = FakeLogin()
    assert _cli(monkeypatch, []) == 0
    out = capsys.readouterr().out
    assert "updated #4 four (token renewed; login now ends " in out
    assert _slot_rt(s) == "rt-four-new"


def test_cli_bare_adds_with_slot_and_email(temp_home, monkeypatch, capsys, cli_sign_in):
    s = _switcher(temp_home)
    cli_sign_in["login"] = login = _new_login()
    assert _cli(monkeypatch, ["--slot", "2", "--email", NEW]) == 0
    assert "added #2 new@example.com [Team]" in capsys.readouterr().out
    assert login.calls[0][0][-2:] == ["--email", NEW]
    assert _slot_rt(s, "2", NEW) == "rt-new"


def test_cli_bare_partial_match_exits_one_and_names_the_commands(temp_home, monkeypatch,
                                                                  capsys, cli_sign_in):
    s = _switcher(temp_home)
    before = _sequence(s)
    cli_sign_in["login"] = FakeLogin(org="org-other")
    assert _cli(monkeypatch, []) == 1
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "cc-swap login 4" in text and "cc-swap login --new" in text
    assert _sequence(s) == before


def test_cli_bare_slot_of_the_signed_in_account_renews(temp_home, monkeypatch, capsys,
                                                       cli_sign_in):
    s = _switcher(temp_home)
    cli_sign_in["login"] = FakeLogin()
    assert _cli(monkeypatch, ["--slot", "4"]) == 0
    assert "updated #4 four" in capsys.readouterr().out
    assert _slot_rt(s) == "rt-four-new"


def test_cli_bare_taken_slot_for_a_new_account_exits_one(temp_home, monkeypatch, capsys,
                                                          cli_sign_in):
    s = _switcher(temp_home)
    cli_sign_in["login"] = _new_login()
    assert _cli(monkeypatch, ["--slot", "4"]) == 1
    err = capsys.readouterr().err
    assert "slot 4 is taken" in err and "cc-swap unclaimed" in err
    assert _slot_rt(s) == "rt-four-dead"


def test_cli_explicit_new_still_refuses_a_taken_slot_before_the_browser(
    temp_home, monkeypatch, capsys
):
    _switcher(temp_home)
    login = _new_login()
    real_new = rl.login_new
    monkeypatch.setattr(rl, "login_new", lambda sw, new, *, claude, **kw: real_new(
        sw, new, claude=claude, **{**kw, "run": login}))
    assert _cli(monkeypatch, ["--new", "--slot", "4"]) == 1
    assert "slot 4 is taken" in capsys.readouterr().err
    assert not login.calls


def test_cli_bare_without_claude_prints_the_manual_steps(temp_home, monkeypatch, capsys,
                                                         cli_sign_in):
    s = _switcher(temp_home)
    cli_sign_in["login"] = login = _new_login()
    assert _cli(monkeypatch, [], claude=None) == 1
    out = capsys.readouterr().out
    assert "Sign in by hand" in out and "cc-swap add" in out
    assert not login.calls and not _leftover_profiles(s)


def test_cli_explicit_forms_are_unchanged(temp_home, monkeypatch, capsys):
    """`login N` still refuses another account; `--new` still refuses a duplicate."""
    s = _switcher(temp_home)
    real_relogin, real_new = rl.relogin, rl.login_new
    monkeypatch.setattr(rl, "relogin", lambda sw, n, *, claude, **kw: real_relogin(
        sw, n, claude=claude, **{**kw, "run": FakeLogin(email=NEW)}))
    assert _cli(monkeypatch, ["4"]) == 1
    assert "not #4's account" in capsys.readouterr().err
    monkeypatch.setattr(rl, "relogin", lambda sw, n, *, claude, **kw: real_relogin(
        sw, n, claude=claude, **{**kw, "run": FakeLogin()}))
    assert _cli(monkeypatch, ["4"]) == 0
    assert "#4 login stored" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    monkeypatch.setattr(rl, "login_new", lambda sw, new, *, claude, **kw: real_new(
        sw, new, claude=claude, **{**kw, "run": FakeLogin(rt="rt-again")}))
    assert _cli(monkeypatch, ["--new"]) == 1
    assert "already #4" in capsys.readouterr().err
    assert set(s._get_sequence_data()["accounts"]) == {"1", "4", "5"}


def test_relogin_steps_name_the_bare_command():
    assert oauth.RELOGIN_STEPS == "cc-swap login, or Fleet → select → r"
