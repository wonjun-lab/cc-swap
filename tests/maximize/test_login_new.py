"""`cc-swap login --new` / Fleet's *Sign in a new account*: Claude Code's own
login in a throwaway profile, stored as a NEW account (maximize/relogin.py,
``switcher.store_new_login``). The live login is never touched.

Same fakes as test_relogin: no real claude, no real Keychain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import cli, oauth
from claude_swap.exceptions import DuplicateAccountError, ValidationError
from claude_swap.maximize import relogin as rl
from claude_swap.models import Platform
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


def _new_login(**kw) -> FakeLogin:
    kw.setdefault("email", NEW)
    kw.setdefault("org", "org-new")
    kw.setdefault("uuid", "uuid-new")
    kw.setdefault("rt", "rt-new")
    return FakeLogin(**kw)


def _run(s, login, new: rl.NewAccount | None = None, **kw):
    return rl.login_new(s, new or rl.NewAccount(), claude=CLAUDE, run=login,
                        announce=None, **kw)


def _live_files(home: Path, s) -> tuple[str | None, str]:
    return s._read_credentials(), (home / ".claude.json").read_text()


# -- a new account ------------------------------------------------------------------------


def test_stores_a_new_account_in_the_next_free_slot_and_leaves_the_live_login(temp_home):
    s = _switcher(temp_home)
    live_before = _live_files(temp_home, s)
    active_before = s._get_sequence_data()["activeAccountNumber"]
    login = _new_login()
    outcome = _run(s, login)
    assert outcome.ok and outcome.number == "6"  # slots 1, 4, 5 → next is 6
    assert "new account new@example.com stored [Team]" == outcome.message
    record = s._get_sequence_data()["accounts"]["6"]
    assert record["email"] == NEW and record["uuid"] == "uuid-new"
    assert record["organizationUuid"] == "org-new" and record["organizationName"] == "Team"
    assert "alias" not in record
    assert 6 in s._get_sequence_data()["sequence"]
    assert _slot_rt(s, "6", NEW) == "rt-new"
    config = json.loads(s._read_account_config("6", NEW))
    assert config["oauthAccount"]["emailAddress"] == NEW
    # The live login: not read into, not written, nothing switched.
    assert _live_files(temp_home, s) == live_before
    assert s._get_sequence_data()["activeAccountNumber"] == active_before
    assert s.current_account_number() == "1"
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_runs_auth_login_without_an_email_unless_one_is_given(temp_home):
    s = _switcher(temp_home)
    login = _new_login()
    assert _run(s, login).ok
    assert login.calls[0][0] == [CLAUDE, "auth", "login", "--claudeai"]
    env = login.calls[0][1]
    assert env["CLAUDE_CONFIG_DIR"] == str(login.calls[0][2])
    other = _new_login(email="pre@example.com", uuid="uuid-pre")
    assert _run(s, other, rl.NewAccount(email="pre@example.com")).ok
    assert other.calls[0][0][-2:] == ["--email", "pre@example.com"]


def test_the_email_only_pre_fills(temp_home):
    """Whoever signs in is the new account; --email is a form pre-fill."""
    s = _switcher(temp_home)
    outcome = _run(s, _new_login(), rl.NewAccount(email="typo@example.com"))
    assert outcome.ok and s._get_sequence_data()["accounts"][outcome.number]["email"] == NEW


def test_the_plan_is_detected_from_the_credential(temp_home, monkeypatch):
    s = _switcher(temp_home)
    tiered = json.dumps({"claudeAiOauth": {
        **json.loads(_creds("rt-new"))["claudeAiOauth"],
        "rateLimitTier": "default_claude_max_20x",
    }})
    login = _new_login()
    real = login.__call__

    def with_tier(argv, env, cwd):
        code = real(argv, env, cwd)
        (Path(cwd) / ".credentials.json").write_text(tiered)
        return code

    outcome = _run(s, with_tier)
    assert outcome.ok and outcome.message.endswith("[Team · 20x]")
    from claude_swap.maximize.plan import rate_limit_tier_from_credentials

    assert rate_limit_tier_from_credentials(
        s._read_account_credentials(outcome.number, NEW)
    ) == "default_claude_max_20x"


def test_slot_choice(temp_home):
    s = _switcher(temp_home)
    outcome = _run(s, _new_login(), rl.NewAccount(slot="3"))
    assert outcome.ok and outcome.number == "3"
    assert s._get_sequence_data()["sequence"] == [1, 3, 4, 5]


def test_a_taken_slot_is_refused_before_the_browser(temp_home):
    s = _switcher(temp_home)
    login = _new_login()
    with pytest.raises(ValidationError, match="slot 4 is taken"):
        _run(s, login, rl.NewAccount(slot="4"))
    with pytest.raises(ValidationError):
        _run(s, login, rl.NewAccount(slot="zero"))
    assert not login.calls and not _leftover_profiles(s)


def test_a_slot_taken_meanwhile_is_rechecked_when_storing(temp_home):
    s = _switcher(temp_home)
    with pytest.raises(ValidationError, match="slot 4 is taken"):
        s.store_new_login(_creds("rt-x"), {"emailAddress": NEW}, slot=4)


def test_macos_reads_and_deletes_the_profile_keychain_item(temp_home, monkeypatch,
                                                          block_real_keychain):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    s = _switcher(temp_home)
    login = _new_login(keychain=block_real_keychain)
    outcome = _run(s, login)
    assert outcome.ok and _slot_rt(s, outcome.number, NEW) == "rt-new"
    from claude_swap import macos_keychain
    from claude_swap.session import keychain_service_name

    assert block_real_keychain.get_password(
        keychain_service_name(str(login.profile)), macos_keychain.keychain_account_name()
    ) is None


# -- an account cc-swap already has -------------------------------------------------------


def test_an_account_already_in_a_slot_is_refused(temp_home):
    s = _switcher(temp_home)
    before = json.dumps(s._get_sequence_data(), sort_keys=True)
    login = FakeLogin()  # signs in as #4's account
    outcome = _run(s, login)
    assert outcome.status == rl.DUPLICATE and outcome.number == "4"
    assert "already four" in outcome.message and "cc-swap login four" in outcome.message
    assert _slot_rt(s) == "rt-four-dead"  # #4 untouched
    assert json.dumps(s._get_sequence_data(), sort_keys=True) == before
    assert not _leftover_profiles(s)
    # The fresh login is not thrown away: kept unclaimed.
    assert "cc-swap unclaimed" in outcome.message and _stashed_rts(s) == ["rt-four-new"]


def test_a_relogin_offer_that_mismatches_keeps_the_login(temp_home):
    """Matched by account uuid under a new email: the re-login's identity
    check refuses it, and the fresh login is kept unclaimed."""
    s = _switcher(temp_home)
    outcome = _run(s, FakeLogin(email="renamed@example.com"),
                   adopt_existing=lambda n, e: True)
    assert outcome.status == rl.MISMATCH and outcome.number == "4"
    assert "cc-swap unclaimed" in outcome.message and _stashed_rts(s) == ["rt-four-new"]
    assert _slot_rt(s) == "rt-four-dead"


def test_a_stale_previous_generation_on_the_new_key_is_dropped(temp_home):
    s = _switcher(temp_home)
    prev = s._store._prev_backup_path("6", NEW)
    prev.write_text("stale")
    assert _run(s, _new_login()).ok
    assert not prev.exists()


def test_the_duplicate_check_ignores_email_case_and_matches_the_uuid(temp_home):
    s = _switcher(temp_home)
    assert _run(s, FakeLogin(email=FOUR.upper())).status == rl.DUPLICATE
    renamed = FakeLogin(email="renamed@example.com")  # same account uuid, same org
    assert _run(s, renamed).status == rl.DUPLICATE
    other_org = FakeLogin(org="org-other", uuid="uuid-other")  # same email, other org
    assert _run(s, other_org).ok


def test_a_duplicate_can_be_kept_as_that_slots_relogin(temp_home):
    s = _switcher(temp_home)
    asked = []
    outcome = _run(s, FakeLogin(), adopt_existing=lambda n, e: asked.append((n, e)) or True)
    assert asked == [("4", FOUR)]
    assert outcome.ok and outcome.number == "4" and "four login stored" in outcome.message
    assert _slot_rt(s) == "rt-four-new"
    assert "6" not in s._get_sequence_data()["accounts"]


def test_store_new_login_refuses_a_duplicate_under_the_lock(temp_home):
    s = _switcher(temp_home)
    with pytest.raises(DuplicateAccountError) as info:
        s.store_new_login(_creds("rt-x"), {"emailAddress": FOUR, "organizationUuid": ORG4})
    assert info.value.number == "4"


# -- cancel / failure ---------------------------------------------------------------------


@pytest.mark.parametrize("code, status", [(None, rl.CANCELLED), (1, rl.FAILED)])
def test_cancel_or_failure_stores_nothing_and_cleans_up(temp_home, code, status):
    s = _switcher(temp_home)
    before = json.dumps(s._get_sequence_data(), sort_keys=True)
    login = _new_login(code=code, write=False)
    outcome = _run(s, login)
    assert outcome.status == status and "nothing stored" in outcome.message
    assert json.dumps(s._get_sequence_data(), sort_keys=True) == before
    assert not login.profile.exists() and not _leftover_profiles(s)


def test_a_store_failure_leaves_no_half_slot_and_keeps_the_login(temp_home, monkeypatch):
    s = _switcher(temp_home)
    real = s._write_json

    def failing(path, data):
        if Path(path) == Path(s.sequence_file) and "6" in (data.get("accounts") or {}):
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(s, "_write_json", failing)
    login = _new_login()
    outcome = _run(s, login)
    assert outcome.status == rl.FAILED and "disk full" in outcome.message
    assert "cc-swap unclaimed" in outcome.message
    assert "6" not in s._get_sequence_data()["accounts"]
    assert not s._read_account_credentials("6", NEW)
    assert not (Path(s.configs_dir) / f".claude-config-6-{NEW}.json").exists()
    assert _stashed_rts(s) == ["rt-new"]
    assert not _leftover_profiles(s)


def test_refuses_inside_a_session_shell_before_the_browser(temp_home, monkeypatch):
    s = _switcher(temp_home)
    inside = s.backup_dir / "sessions" / "4-four_example.com"
    inside.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inside))
    login = _new_login()
    with pytest.raises(Exception, match="(?i)session"):
        _run(s, login)
    assert not login.calls


# -- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def cli_new(monkeypatch):
    """Route ``login_new(..., run=run_interactive)`` to a fake login."""
    holder: dict = {}
    real = rl.login_new

    def login_new(switcher, new, *, claude, **kw):
        kw["run"] = holder["login"]
        return real(switcher, new, claude=claude, **kw)

    monkeypatch.setattr(rl, "login_new", login_new)
    return holder


def _cli(monkeypatch, argv, *, claude=CLAUDE, supported=True):
    monkeypatch.setattr("claude_swap.maximize.primer.resolve_claude_path",
                        lambda configured, **k: claude)
    monkeypatch.setattr(rl, "login_supported", lambda c, **k: supported)
    with pytest.raises(SystemExit) as exit_info:
        cli._login_command(argv)
    return exit_info.value.code


def test_cli_new_stores_and_exits_zero(temp_home, monkeypatch, capsys, cli_new):
    s = _switcher(temp_home)
    cli_new["login"] = _new_login()
    assert _cli(monkeypatch, ["--new", "--slot", "2", "--email", NEW]) == 0
    out = capsys.readouterr().out
    assert "new account new@example.com stored" in out and "new account" in out  # + the banner
    assert _slot_rt(s, "2", NEW) == "rt-new"


def test_cli_new_duplicate_exits_one_without_a_terminal(temp_home, monkeypatch, capsys,
                                                       cli_new):
    s = _switcher(temp_home)
    cli_new["login"] = FakeLogin()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert _cli(monkeypatch, ["--new"]) == 1
    captured = capsys.readouterr()
    assert "cc-swap login four" in captured.out + captured.err
    assert _slot_rt(s) == "rt-four-dead"


def test_cli_new_duplicate_offers_the_relogin_on_a_terminal(temp_home, monkeypatch, capsys,
                                                           cli_new):
    s = _switcher(temp_home)
    cli_new["login"] = FakeLogin()
    asked = []
    monkeypatch.setattr(cli, "_ask_adopt_existing",
                        lambda n, e, **kw: asked.append(kw) or True)
    assert _cli(monkeypatch, ["--new"]) == 0
    assert _slot_rt(s) == "rt-four-new"
    assert asked == [{"live": False, "name": "four"}]  # #1 is live, not #4


def test_cli_new_taken_slot_exits_one_before_the_browser(temp_home, monkeypatch, capsys,
                                                        cli_new):
    _switcher(temp_home)
    cli_new["login"] = login = _new_login()
    assert _cli(monkeypatch, ["--new", "--slot", "4"]) == 1
    assert "slot 4 is taken" in capsys.readouterr().err
    assert not login.calls


@pytest.mark.parametrize("argv", [["--new", "4"], ["4", "--slot", "2"],
                                  ["4", "--email", NEW]])
def test_cli_argument_combinations(temp_home, monkeypatch, argv):
    _switcher(temp_home)
    assert _cli(monkeypatch, argv) == 2


def test_cli_new_without_claude_prints_the_manual_steps(temp_home, monkeypatch, capsys,
                                                       cli_new):
    s = _switcher(temp_home)
    cli_new["login"] = login = _new_login()
    assert _cli(monkeypatch, ["--new"], claude=None) == 1
    out = capsys.readouterr().out
    assert "Add a new account by hand" in out and "cc-swap add" in out
    assert not login.calls and not _leftover_profiles(s)


def test_the_offer_says_when_it_rewrites_the_live_login(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli._ask_adopt_existing("4", FOUR, live=True) is False
    captured = capsys.readouterr()
    assert "replaces the live login too" in captured.out + captured.err
    cli._ask_adopt_existing("4", FOUR, live=False)
    captured = capsys.readouterr()
    assert "live login" not in captured.out + captured.err


def test_ask_adopt_existing_needs_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli._ask_adopt_existing("4", FOUR) is False
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert cli._ask_adopt_existing("4", FOUR) is True


def test_new_account_never_logs_tokens(temp_home, caplog):
    s = _switcher(temp_home)
    with caplog.at_level("INFO", logger="claude-swap"):
        assert _run(s, _new_login()).ok
    assert "rt-new" not in caplog.text and oauth.fingerprint8(_creds("rt-new")) in caplog.text



def test_a_torn_sequence_during_the_relogin_offer_keeps_the_login(temp_home):
    s = _switcher(temp_home)

    def offer(number, email):
        s.sequence_file.write_text("{torn")  # current_account_number() now raises
        s.current_account_number()
        return True

    outcome = _run(s, FakeLogin(), adopt_existing=offer)
    assert outcome.status == rl.FAILED and "cc-swap unclaimed" in outcome.message
    assert _stashed_rts(s) == ["rt-four-new"]
    assert not _leftover_profiles(s)
