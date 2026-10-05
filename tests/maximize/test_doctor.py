"""``cc-swap doctor``: each finding against a fake machine (doctor_support.World).

No real Keychain, service manager, ``ps`` or ``claude``: every command goes
through ``World.run``, which fails the test on anything unexpected. Every
test also proves doctor stays read-only and offline and never prints a token
or an email.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from claude_swap.maximize import doctor as dr
from claude_swap.maximize import doctor_cli
from tests.maximize.doctor_support import DAY, NOW, USER, World, creds, email, files


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Doctor never touches the network: any connection attempt fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("doctor opened a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def run(world: World, **kw) -> list[dr.Finding]:
    """Run every check and assert the run wrote nothing and leaked nothing."""
    before = files(world.home, world.root)
    findings = dr.run_checks(world.probes(**kw))
    assert files(world.home, world.root) == before, "doctor wrote a file"
    text = json.dumps([f.to_json() for f in findings])
    assert "SECRET" not in text and "@example.com" not in text, text
    assert not [f for f in findings if "crashed" in f.detail], findings
    return findings


def find(findings, check, severity=None, scope=None) -> list[dr.Finding]:
    return [
        f for f in findings
        if f.check == check
        and (severity is None or f.severity == severity)
        and (scope is None or f.scope == scope)
    ]


def problems(findings) -> list[dr.Finding]:
    return [f for f in findings if f.severity in ("warn", "error")]


# -- healthy machine -------------------------------------------------------------------


def test_healthy_machine_has_no_problems_and_exits_0(world):
    findings = run(world.healthy())
    assert problems(findings) == []
    assert dr.exit_code(findings) == 0
    checks = {f.check for f in findings}
    assert {"claude", "keychain", "live-login", "upstream", "service", "lease",
            "settings", "accounts"} <= checks
    # Environment comes before the accounts.
    scopes = [f.scope for f in findings]
    assert scopes.index("env") == 0
    assert find(findings, "live-login", "ok")[0].detail.startswith("the live login is user1")


def test_doctor_runs_claude_only_with_version(world):
    run(world.healthy())
    claude_runs = [c for c in world.commands if Path(c[0]).name == "claude"]
    assert claude_runs == [[world.claude_path, "--version"]]


def test_doctor_reads_the_keychain_but_never_writes_it(world):
    run(world.healthy())
    security = [c for c in world.commands if c[0] == dr.SECURITY]
    assert security and all(c[1] == "find-generic-password" for c in security)


def test_doctor_never_refreshes_a_token(world, monkeypatch):
    from claude_swap import oauth

    def refuse(*a, **k):
        raise AssertionError("doctor refreshed a token")

    for name in dir(oauth):
        if "refresh" in name.lower() and callable(getattr(oauth, name)) and not name.startswith("_"):
            if name not in ("refresh_audit_line",):
                monkeypatch.setattr(oauth, name, refuse)
    world.healthy()
    world.store(2, creds(2, login_in_s=-DAY))  # an expired login tempts a refresh
    run(world)


# -- Keychain ---------------------------------------------------------------------------


@pytest.mark.parametrize(("rc", "meaning", "fix"), [
    (36, "errSecInteractionNotAllowed", "unlock-keychain"),
    (51, "errSecAuthFailed", "Access Control"),
    (dr.RC_TIMEOUT, "did not answer", "unlock"),
])
def test_unreadable_keychain_is_an_error_with_its_meaning(world, rc, meaning, fix):
    world.accounts(1, 2, active=1)
    world.settings()
    world.login(1, keychain_rc=rc)
    findings = run(world)
    [f] = find(findings, "keychain", "error")
    assert meaning in f.detail and f"rc={rc}" in f.detail or rc == dr.RC_TIMEOUT
    assert fix in f.fix
    assert dr.exit_code(findings) == 2


def test_rc_44_with_a_named_account_means_the_login_is_missing(world):
    world.accounts(1, 2, active=1)
    world.login(1, keychain_rc=44)
    [f] = find(run(world), "keychain", "error")
    assert "rc=44" in f.detail and "not found" in f.detail
    assert "/login" in f.fix


def test_no_login_at_all_is_a_warning(world):
    world.accounts(1, 2)
    [f] = find(run(world), "keychain", "warn")
    assert "no live login" in f.detail


# -- plaintext ---------------------------------------------------------------------------


def test_plaintext_copy_of_the_same_login_is_flagged_as_a_duplicate(world):
    world.healthy()
    world.cred_file().write_text(creds(1))
    [f] = find(run(world), "plaintext", "warn")
    assert "duplicates the Keychain" in f.detail
    assert "mv ~/.claude/.credentials.json" in f.fix


def test_plaintext_copy_that_differs_shows_both_fingerprint_prefixes(world):
    from claude_swap.oauth import fingerprint8

    world.healthy()
    stale = creds(1, rt="rt-SECRET-old")
    world.cred_file().write_text(stale)
    [f] = find(run(world), "plaintext", "warn")
    assert "differs from the Keychain" in f.detail
    assert fingerprint8(stale) in f.detail and fingerprint8(creds(1)) in f.detail


def test_plaintext_only_login_on_macos(world):
    world.accounts(1, 2, active=1)
    world.login(1, keychain_rc=44, plaintext=creds(1))
    findings = run(world)
    [f] = find(findings, "plaintext", "warn")
    assert "only as plaintext" in f.detail
    assert not find(findings, "keychain", "error")


def test_plaintext_fallback_while_keychain_is_locked_is_info(world):
    world.accounts(1, 2, active=1)
    world.login(1, keychain_rc=36, plaintext=creds(1))
    findings = run(world)
    assert find(findings, "keychain", "error")
    assert find(findings, "plaintext", "info")


#: What Claude Code writes to ~/.claude/.credentials.json while the Keychain is
#: unavailable: its MCP OAuth tokens only, no claudeAiOauth.
MCP_ONLY = json.dumps({"mcpOAuth": {"srv|abc": {
    "serverName": "srv", "accessToken": "mcp-SECRET-at", "refreshToken": "mcp-SECRET-rt",
}}})


def test_an_mcp_only_plaintext_file_is_no_stale_login(world):
    world.healthy()
    world.cred_file().write_text(MCP_ONLY)
    findings = run(world)
    assert not find(findings, "plaintext", "warn") and not find(findings, "plaintext", "error")
    [f] = find(findings, "plaintext", "info")
    assert "holds only MCP logins" in f.detail and "differs" not in f.detail
    assert "rt " not in f.detail
    assert problems(findings) == []


def test_an_mcp_only_plaintext_file_is_no_login_when_the_keychain_has_none(world):
    world.accounts(1, 2, active=1)
    world.login(1, keychain_rc=44, plaintext=MCP_ONLY)
    findings = run(world)
    # the Keychain has no login, and the file does not stand in for one
    [f] = find(findings, "keychain", "error")
    assert "rc=44" in f.detail
    assert not find(findings, "live-login", "warn")
    [p] = find(findings, "plaintext")
    assert p.severity == "info" and "holds only MCP logins" in p.detail


def test_an_mcp_only_file_on_linux_is_no_login(tmp_path):
    world = World(tmp_path, platform="linux")
    world.accounts(1, 2, active=1)
    world.login(1, plaintext=MCP_ONLY)
    [f] = find(run(world), "plaintext", "error")
    assert "names an account but ~/.claude/.credentials.json holds only MCP logins" in f.detail
    bare = World(tmp_path / "bare", platform="linux")
    bare.accounts(1, 2)
    bare.login(None, plaintext=MCP_ONLY)
    [f] = find(run(bare), "plaintext", "warn")
    assert f.detail.startswith("no live login")


def test_linux_credentials_file_readable_by_others(tmp_path):
    world = World(tmp_path, platform="linux")
    world.healthy()
    world.cred_file().chmod(0o644)
    [f] = find(run(world), "plaintext", "warn")
    assert "0644" in f.detail and f.fix == "chmod 600 ~/.claude/.credentials.json"


def test_linux_healthy_reads_the_file_and_no_keychain(tmp_path):
    world = World(tmp_path, platform="linux")
    findings = run(world.healthy())
    assert problems(findings) == []
    assert not [c for c in world.commands if c[0] == dr.SECURITY]


# -- live login ↔ slot ------------------------------------------------------------------


def test_unmanaged_live_login_says_cc_swap_add(world):
    world.accounts(1, 2, active=1)
    world.settings()
    world.login(7)  # an account no slot has
    [f] = find(run(world), "live-login", "warn")
    assert "belongs to no slot" in f.detail and f.fix.startswith("cc-swap add")


def test_live_token_of_another_slot_is_an_error(world):
    world.accounts(1, 2, active=1)
    world.login(1, creds(2))  # .claude.json says #1, token is #2's
    [f] = find(run(world), "live-login", "error")
    assert "user1" in f.detail and "user2's login" in f.detail


def test_live_login_without_a_backup(world):
    world.accounts(1, 2, active=1, s1=None)
    world.login(1)
    [f] = find(run(world), "live-login", "warn")
    assert "no stored backup" in f.detail and f.fix == "cc-swap add --slot 1"


# -- login deadlines, quarantine, duplicates ----------------------------------------------


def test_expired_and_expiring_logins(world):
    world.healthy(4)
    world.store(2, creds(2, login_in_s=-3600))
    world.store(3, creds(3, login_in_s=2 * DAY + 3600))
    world.store(4, creds(4, login_in_s=10 * DAY))
    findings = run(world)
    [expired] = find(findings, "login-deadline", "error", "#2")
    assert expired.detail.startswith("login expired") and "(1h 0m ago)" in expired.detail
    assert "UTC" not in expired.detail  # local time, like list / the auto log / Fleet
    assert expired.fix == "re-login user2: cc-swap login user2, or Fleet → select → r"
    assert expired.name == "user2" and expired.to_json()["name"] == "user2"
    [soon] = find(findings, "login-deadline", "warn", "#3")
    assert soon.detail.startswith("login expires ") and "(in 2d 1h)" in soon.detail
    assert not find(findings, "login-deadline", scope="#4")


def test_active_slot_deadline_comes_from_the_live_login(world):
    world.healthy()
    world.login(1, creds(1, login_in_s=DAY))  # backup still says 20 days
    [f] = find(run(world), "login-deadline", "warn", "#1")
    assert "1d 0h" in f.detail


def test_quarantined_slot_is_an_error_until_its_login_changes(world):
    from claude_swap.oauth import credential_fingerprint

    world.healthy()
    world.state(quarantine={
        "2": {"email": email(2), "reason": "invalid_grant",
              "refreshTokenFingerprint": credential_fingerprint(creds(2))},
        "3": {"email": email(3), "reason": "login_expired",
              "refreshTokenFingerprint": "sha256:old"},
    })
    findings = run(world)
    [dead] = find(findings, "quarantine", "error", "#2")
    assert "refresh token dead" in dead.detail and dead.fix.startswith("re-login user2: ")
    [lifting] = find(findings, "quarantine", "info", "#3")
    assert "lifts it" in lifting.detail


def test_missing_stored_login(world):
    world.healthy()
    world.keychain.pop(("claude-swap", f"account-3-{email(3)}"))
    [f] = find(run(world), "stored-login", "error", "#3")
    assert f.fix.startswith("cc-swap login user3")


def test_duplicate_lineage_names_both_slots(world):
    world.healthy()
    world.store(3, creds(2))
    [f] = find(run(world), "shared-login", "error")
    assert "user2 and user3 hold the same login" in f.detail
    assert "does not refresh user2 or user3" in f.detail
    assert f.fix == "re-login one of them: cc-swap login user3 or cc-swap login user2"


def test_duplicate_setup_token_stays_a_duplicate(world):
    """No refresh token, nothing one-time-use: the old duplicate finding."""
    world.healthy()
    token = json.dumps({"claudeAiOauth": {"accessToken": "at-SECRET-setup"}})
    world.store(2, token)
    world.store(3, token)
    findings = run(world)
    assert find(findings, "duplicate", "error") and not find(findings, "shared-login")


def _profile(world, n, value, *, slug=None):
    path = world.root / "sessions" / f"{n}-{slug or email(n).replace('@', '_')}"
    path.mkdir(parents=True, exist_ok=True)
    (path / ".credentials.json").write_text(value)
    return path


def test_slot_sharing_another_slots_cswap_run_profile(world):
    world.healthy()
    _profile(world, 2, creds(3))  # #2's profile holds #3's login
    [f] = find(run(world), "shared-login", "error", "#3")
    assert "user3 holds the same login as user2's cswap run profile" in f.detail
    assert f.fix == "re-login one of them: cc-swap login user3 or cc-swap login user2"


def test_own_cswap_run_profile_is_not_sharing(world):
    world.healthy()
    _profile(world, 2, creds(2))
    findings = run(world)
    assert not find(findings, "shared-login") and problems(findings) == []


def test_macos_profile_keychain_item_is_read(world):
    from claude_swap.session import keychain_service_name

    world.healthy()
    path = _profile(world, 2, creds(2))  # the plaintext seed is #2's own...
    world.keychain[(keychain_service_name(str(path)), USER)] = creds(3)  # ...the item is not
    [f] = find(run(world), "shared-login", "error", "#3")
    assert "user2's cswap run profile" in f.detail


def test_leftover_profile_is_named_by_slot_number_only(world):
    world.healthy()
    _profile(world, 2, creds(3), slug="old_example.com")
    [f] = find(run(world), "shared-login", "error", "#3")
    assert "a leftover cswap run profile made for user2" in f.detail
    assert f.fix == "re-login one of them: cc-swap login user3"


def test_a_stale_marked_profile_is_not_a_sharer(world):
    world.healthy()
    path = _profile(world, 2, creds(3))
    (path.parent / f".{path.name}.cswap-stale-credentials").write_text("")
    assert not find(run(world), "shared-login")


def test_a_live_setup_token_under_another_name_is_not_called_shared(world):
    world.healthy()
    token = json.dumps({"claudeAiOauth": {"accessToken": "at-SECRET-setup"}})
    world.store(2, token)
    world.login(1, token)
    [f] = find(run(world), "live-login", "error")
    assert "does not refresh" not in f.detail
    assert "re-login one of them" not in f.fix


def test_live_login_sharing_a_non_live_slot(world):
    world.healthy()
    world.login(1, creds(2))  # ~/.claude.json names #1, the token is #2's
    [f] = find(run(world), "live-login", "error")
    assert "names user1 but the live token is user2's login" in f.detail
    assert "does not refresh user1 or user2" in f.detail
    assert f.fix == "re-login one of them: cc-swap login user2 or cc-swap login user1"


# -- upstream -------------------------------------------------------------------------------


def test_upstream_installed_and_running(world):
    world.healthy()
    (world.home / ".local" / "share" / "uv" / "tools" / "claude-swap").mkdir(parents=True)
    world.ps = (
        "  101 /usr/bin/login\n"
        f"  555 {world.home}/.local/share/uv/tools/claude-swap/bin/python {world.home}/.local/bin/cswap auto\n"
        f"  556 {world.home}/.local/share/uv/tools/cc-swap/bin/python cc-swap auto\n"
    )
    findings = run(world)
    [installed] = find(findings, "upstream", "warn")
    assert installed.fix == "uv tool uninstall claude-swap"
    [running] = find(findings, "upstream", "error")
    assert "pid 555" in running.detail and "kill 555" in running.fix
    assert dr.exit_code(findings) == 2


def test_upstream_menubar_launch_agent(world):
    import plistlib

    world.healthy()
    venv = world.home / ".local" / "share" / "uv" / "tools" / "claude-swap" / "bin"
    venv.mkdir(parents=True)
    (venv / "cswap").write_text("#!/bin/sh\n")
    plist = world.home / "Library" / "LaunchAgents" / "com.cswap.menubar.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(venv / "cswap"), "menubar"]}))
    findings = run(world)
    assert any("menu bar LaunchAgent" in f.detail for f in find(findings, "upstream", "error"))


def test_our_own_menubar_launch_agent_is_not_upstream(world):
    import plistlib

    world.healthy()
    plist = world.home / "Library" / "LaunchAgents" / "com.cswap.menubar.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [world.program[0], "menubar"]}))
    assert not problems(run(world))


# -- service and lease ------------------------------------------------------------------------


def test_service_not_installed_is_info_and_lease_says_nothing_switches(world):
    world.accounts(1, 2, active=1)
    world.login(1)
    world.settings()
    findings = run(world)
    assert find(findings, "service", "info")[0].fix == "cc-swap service install"
    assert "nothing switches" in find(findings, "lease", "info")[0].detail


def test_service_file_without_cc_swap_service_env_is_stale(world):
    world.healthy()
    world.service_installed(env={"PATH": "/usr/bin"})
    [f] = find(run(world), "service", "warn")
    assert "predates cc-swap 0.2.0" in f.detail and "service install" in f.fix


def test_service_pinned_to_another_version(world):
    world.healthy()
    world.installs[world.program[0]] = ("0.1.1", NOW - 9 * DAY)
    [f] = find(run(world), "service", "warn")
    assert "pinned to cc-swap 0.1.1" in f.detail and "0.2.0" in f.detail


def test_service_process_older_than_the_install_runs_old_code(world):
    world.healthy()
    world.started_at[4121] = NOW - 3 * DAY  # install was 2 days ago
    [f] = find(run(world), "service", "warn")
    assert "still runs the old code" in f.detail


def test_service_program_missing_is_an_error(world):
    world.healthy()
    world.service_installed(program=[str(world.home / "gone" / "cc-swap")])
    findings = run(world)
    assert any("no longer exists" in f.detail for f in find(findings, "service", "error"))


def test_service_profile_differs_from_this_shell(world):
    world.healthy()
    world.service_installed(env={"CC_SWAP_SERVICE": "1", "CLAUDE_CONFIG_DIR": "/elsewhere"})
    [f] = find(run(world), "service", "warn")
    assert "CLAUDE_CONFIG_DIR" in f.detail


def test_service_installed_but_stopped(world):
    world.healthy()
    world.service_installed(running=False)
    world.lease = (False, None)
    findings = run(world)
    assert "not running" in find(findings, "service", "warn")[0].detail


def test_service_file_present_but_not_loaded_uses_the_status_wording(world, monkeypatch, capsys):
    """`service status` said "stopped" while doctor said "state unknown"."""
    from claude_swap import cli
    from claude_swap.maximize import service

    world.healthy()
    world.service_installed(running=False)
    world.service.update(loaded=False, state=None)
    world.lease = (False, None)
    [f] = find(run(world), "service", "warn")
    assert "(stopped (not loaded))" in f.detail
    cli._print_service_status({**world.service, "logs": []})
    assert "cc-swap service: stopped (not loaded)" in capsys.readouterr().out
    assert service.state_text(world.service) == "stopped (not loaded)"


def test_linux_unit_file_is_parsed(tmp_path):
    world = World(tmp_path, platform="linux")
    world.healthy()
    world.service_installed(env={"PATH": "/usr/bin"})
    [f] = find(run(world), "service", "warn")
    assert "predates" in f.detail


def test_lease_held_by_another_engine_while_the_service_waits(world):
    world.healthy()
    world.lease = (True, 9999)
    [f] = find(run(world), "lease", "warn")
    assert "pid 9999 holds the engine lease" in f.detail


def test_paused_engine_is_reported(world):
    world.healthy()
    world.state(pausedUntil=NOW + 300, pausedReason="re-login #2")
    [f] = [f for f in find(run(world), "lease", "info") if "paused" in f.detail]
    assert "re-login #2" in f.detail and "5m" in f.detail


def test_auto_off_is_reported(world):
    world.healthy()
    (world.root / "auto_off.json").write_text(json.dumps(
        {"schemaVersion": 1, "autoOff": {"since": NOW - 3600, "by": "cli"}}
    ))
    findings = run(world)
    [f] = [f for f in find(findings, "lease", "info") if "auto-switching is OFF" in f.detail]
    assert "cc-swap auto on" in f.fix
    # A standing user choice, not a problem: doctor's verdict is unchanged.
    assert dr.exit_code(findings) == 0


def test_default_lease_probe_creates_no_lock_file(tmp_path):
    held, pid = dr._lease_holder(tmp_path)
    assert (held, pid) == (False, None)
    assert not (tmp_path / ".engine.lock").exists()


# -- priming and settings ------------------------------------------------------------------


def test_priming_on_with_a_broken_claude_path_is_an_error(world):
    world.healthy()
    world.settings(prime={"enabled": True, "claudePath": str(world.home / "nope" / "claude")})
    findings = run(world)
    [f] = [f for f in find(findings, "claude", "error") if "prime.claudePath" in f.detail]
    assert "not an executable" in f.detail


def test_priming_off_with_a_broken_claude_path_is_a_warning(world):
    world.healthy()
    world.settings(prime={"claudePath": str(world.home / "nope" / "claude")})
    assert find(run(world), "claude", "warn")


def test_claude_missing(world):
    world.healthy()
    world.claude_path = None
    (world.home / "bin" / "claude").unlink()
    [f] = find(run(world), "claude", "warn")
    assert "claude not found" in f.detail


def test_claude_version_failing(world):
    world.healthy()
    world.claude_version = None
    [f] = find(run(world), "claude", "warn")
    assert "--version failed" in f.detail


def test_priming_on_reminds_about_isolation(world):
    world.healthy()
    world.settings(prime={"enabled": True})
    [f] = find(run(world), "priming", "info")
    assert "isolation" in f.detail
    assert f.fix == "cc-swap prime verify"  # nothing verified yet


def test_priming_paused_by_a_claude_update_is_a_warning_naming_prime_verify(world):
    from claude_swap.maximize import prime_verify as pv

    world.healthy()
    world.settings(prime={"enabled": True})
    pv.record_verified(world.root, "2.1.280", by=pv.VERIFIED_BY_CLI, now=1.0)
    [ok] = find(run(world), "priming", "info")
    assert "verified for claude 2.1.280" in ok.detail
    # Claude Code updated itself; the engine's gate read the new version.
    pv.note_seen(world.root, str(world.home / "claude"), "2.1.287", key=None)
    before = files(world.home, world.root)
    [f] = find(run(world), "priming", "warn")
    assert "2.1.280 -> 2.1.287" in f.detail and f.fix == "cc-swap prime verify"
    assert files(world.home, world.root) == before  # still read-only
    steps = {s.key: s for s in doctor_cli.init_steps(world.probes())}
    assert steps["priming"].status == "FIX" and steps["priming"].fix == "cc-swap prime verify"


def test_idle_pattern_is_reported_as_info(world):
    from claude_swap.maximize import history as hist

    world.healthy()
    [f] = find(run(world), "idle-pattern", "info")
    assert f.detail == "idle pattern: learning (0 of 3 days observed)"
    slots = tuple(
        hist.SlotObs(NOW - 4 * DAY + i * hist.SLOT_S, i % 96 >= 40)
        for i in range(4 * 96)
    )
    hist._rewrite(hist.path_for(world.root), hist.History(slots=slots))
    [f] = find(run(world), "idle-pattern", "info")
    # 4 x 24 h of observations touch 4 or 5 local dates, by time zone.
    assert f.detail.startswith(("idle pattern: 4 days learned, ", "idle pattern: 5 days learned, "))
    assert "@" not in hist.path_for(world.root).read_text()


def test_idle_pattern_off_or_another_strategy(world):
    world.healthy()
    world.settings(maximize={"learnIdlePattern": False})
    [f] = find(run(world), "idle-pattern", "info")
    assert f.detail == "idle pattern: off (maximize.learnIdlePattern)"
    world.settings(autoswitch={"strategy": "best"})
    assert find(run(world), "idle-pattern") == []


def test_settings_not_json_is_an_error(world):
    world.healthy()
    (world.root / "settings.json").write_text("{nope")
    [f] = find(run(world), "settings", "error")
    assert "not valid JSON" in f.detail


def test_settings_repairs_are_warnings(world):
    world.healthy()
    world.settings(maximize={"soft5h": 96, "hard5h": 90}, prime={"enabled": "yes"})
    details = [f.detail for f in find(run(world), "settings", "warn")]
    assert any("soft5h" in d for d in details)
    assert any("prime.enabled" in d for d in details)


def test_a_bool_written_as_a_string_is_a_settings_warning(world):
    world.healthy()
    world.settings(autoswitch={"strategy": "maximize", "includeApiKeyAccounts": "false"},
                   prime={"enabled": "false"})
    details = [f.detail for f in find(run(world), "settings", "warn")]
    assert any("autoswitch.includeApiKeyAccounts" in d and "read as false" in d for d in details)
    [prime] = [d for d in details if "prime.enabled" in d]  # reported once
    assert "false" in prime


# -- robustness, exit codes and the CLI -------------------------------------------------------


def test_a_crashing_check_becomes_an_error_finding(world, monkeypatch):
    world.healthy()

    def boom(ctx):
        raise RuntimeError("x")

    monkeypatch.setattr(dr, "ENV_CHECKS", (boom, *dr.ENV_CHECKS[1:]))
    findings = dr.run_checks(world.probes())
    assert find(findings, "boom", "error")[0].detail == "check crashed (RuntimeError)"


@pytest.mark.parametrize(("setup", "code"), [
    (lambda w: None, 0),
    (lambda w: w.store(2, creds(2, login_in_s=DAY)), 1),
    (lambda w: w.store(2, creds(2, login_in_s=-DAY)), 2),
])
def test_doctor_command_exit_codes(world, monkeypatch, capsys, setup, code):
    world.healthy()
    setup(world)
    monkeypatch.setattr(doctor_cli, "_probes", world.probes)
    with pytest.raises(SystemExit) as exc:
        doctor_cli.doctor_command([])
    assert exc.value.code == code
    out = capsys.readouterr().out
    assert "Environment" in out and "Accounts" in out
    assert f"exit {code}" in out
    assert "SECRET" not in out and "@example.com" not in out


def test_doctor_command_json(world, monkeypatch, capsys):
    world.healthy()
    world.store(2, creds(2, login_in_s=-DAY))
    monkeypatch.setattr(doctor_cli, "_probes", world.probes)
    with pytest.raises(SystemExit) as exc:
        doctor_cli.doctor_command(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exc.value.code == payload["exitCode"] == 2
    assert payload["schemaVersion"] == 1 and payload["counts"]["error"] == 1
    [row] = [r for r in payload["findings"] if r["severity"] == "error"]
    assert row == {
        "check": "login-deadline", "scope": "#2", "name": "user2", "severity": "error",
        "detail": row["detail"], "fix": row["fix"],
    }


def test_cli_dispatches_doctor_init_and_why_through_the_fork_hook(monkeypatch):
    from claude_swap import cli

    seen = []
    for name in ("_doctor_command", "_init_command", "_why_command"):
        monkeypatch.setattr(cli, name, lambda argv, name=name: seen.append((name, argv)))
    for verb in ("doctor", "init", "why"):
        monkeypatch.setattr("sys.argv", ["cc-swap", verb, "--json"])
        cli.main()
    assert seen == [
        ("_doctor_command", ["--json"]),
        ("_init_command", ["--json"]),
        ("_why_command", ["--json"]),
    ]


def test_help_lists_the_new_commands(monkeypatch, capsys):
    from claude_swap import cli

    monkeypatch.setattr("sys.argv", ["cc-swap", "help"])
    with pytest.raises(SystemExit):
        cli.main()
    out = capsys.readouterr().out
    for line in ("doctor [--json]", "init [--apply]", "why "):
        assert line in out


def test_keychain_account_follows_user(world):
    world.environ["USER"] = "someone"
    world.healthy()
    world.keychain[("Claude Code-credentials", "someone")] = world.keychain.pop(
        ("Claude Code-credentials", USER)
    )
    assert not problems(run(world))
