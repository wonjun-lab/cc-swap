"""`cc-swap prime [N ...] [--dry-run]` (Task 11)."""

from __future__ import annotations

import sys

import pytest

from claude_swap import cli, oauth
from claude_swap.maximize import prime_cli
from claude_swap.maximize.primer import expected_reset
from claude_swap.settings import atomic_write_json, settings_path
from tests.maximize.fake_claude import FakeClaude
from tests.maximize.primer_support import FakeUsage, H, _usage
from tests.test_autoswitch import EngineHarness

needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake claude is a POSIX shebang script"
)


@pytest.fixture
def cli_rig(temp_home, tmp_path, monkeypatch):
    harness = EngineHarness(temp_home)
    for num, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
        harness.seed(num, email)
    harness.make_live("a@example.com", 1)
    fake = FakeClaude.install(tmp_path / "fakebin")
    atomic_write_json(
        settings_path(harness.switcher.backup_dir),
        {"schemaVersion": 1, "prime": {"claudePath": str(fake.path)}},  # enabled stays false
    )
    usage = FakeUsage(harness.clock)
    now = harness.clock()
    usage.reading("1", pct5=40.0, reset5=now + 2 * H)
    usage.reading("2")
    usage.reading("3", reset5=now + H)  # already open

    def server_opens_on_prime(num):
        primes = harness.state().get("primes", {})
        entry = primes.get(harness.switcher.account_email(num))
        if fake.calls() and entry and entry.get("lastOutcome") == "launched":
            usage.reading(num, reset5=expected_reset(entry["lastAttemptAt"]))

    usage.on_fetch = server_opens_on_prime
    monkeypatch.setattr(harness.switcher, "usage_entries_by_account", usage)
    monkeypatch.setattr(prime_cli, "ClaudeAccountSwitcher", lambda debug=False: harness.switcher)
    monkeypatch.setattr(prime_cli, "_clock", harness.clock)
    monkeypatch.setattr(prime_cli, "_sleep", harness.clock.advance)
    return harness, fake


def test_dry_run_prints_plan_by_slot_only(cli_rig, capsys):
    harness, fake = cli_rig
    prime_cli.prime_command(["--dry-run"])
    out = capsys.readouterr().out
    assert "#1  skip (active)" in out
    assert "#2  would prime now (window cold, attempt 1/2)" in out
    assert "#3  skip (window-on)" in out
    assert "@example.com" not in out
    assert fake.calls() == []


@needs_posix
def test_prime_runs_even_when_disabled_and_verifies(cli_rig, capsys):
    harness, fake = cli_rig
    with pytest.raises(SystemExit) as exc:
        prime_cli.prime_command(["2"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Account-2: 5h window primed" in out
    assert "@example.com" not in out
    [call] = fake.calls()
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-2"


def test_unknown_account_is_an_error(cli_rig, capsys):
    with pytest.raises(SystemExit) as exc:
        prime_cli.prime_command(["9"])
    assert exc.value.code == 1
    assert "Error:" in capsys.readouterr().err


@pytest.fixture
def store_rig(temp_home, tmp_path, monkeypatch):
    """Like ``cli_rig`` but on the REAL usage store (claims, serve TTL,
    backoff, poll plans); only the usage endpoint is scripted:
    ``endpoint["errors"][num]`` makes that slot's next fetches fail."""
    monkeypatch.setattr("claude_swap.switcher._FETCH_STAGGER_S", 0)
    harness = EngineHarness(temp_home)
    for num, email in ((1, "a@example.com"), (2, "b@example.com")):
        harness.seed(num, email)
    harness.make_live("a@example.com", 1)
    monkeypatch.setattr(harness.switcher, "_live_session_pids", lambda *a: [])
    fake = FakeClaude.install(tmp_path / "fakebin")
    atomic_write_json(
        settings_path(harness.switcher.backup_dir),
        {"schemaVersion": 1, "prime": {"claudePath": str(fake.path)}},
    )
    now = harness.clock()
    endpoint: dict = {
        "usage": {"1": _usage(pct5=40.0, reset5=now + 2 * H), "2": _usage()},
        "errors": {},
        "fetches": [],
    }

    def fetch(num, email, creds, is_active=False, persist_credentials=None, **kwargs):
        endpoint["fetches"].append(num)
        error = endpoint["errors"].get(num)
        if error:
            return oauth.UsageOutcome(None, error=error)
        primes = harness.state().get("primes", {})
        entry = primes.get(email)
        if fake.calls() and entry and entry.get("lastOutcome") == "launched":
            endpoint["usage"][num] = _usage(reset5=expected_reset(entry["lastAttemptAt"]))
        return oauth.UsageOutcome(dict(endpoint["usage"][num]))

    monkeypatch.setattr("claude_swap.oauth.try_fetch_usage_for_account", fetch)
    monkeypatch.setattr(prime_cli, "ClaudeAccountSwitcher", lambda debug=False: harness.switcher)
    monkeypatch.setattr(prime_cli, "_clock", harness.clock)
    monkeypatch.setattr(prime_cli, "_sleep", harness.clock.advance)
    harness.switcher.usage_entries_by_account(fetch=None)  # last-good readings
    return harness, fake, endpoint


def _run_prime(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        prime_cli.prime_command(argv)
    return exc.value.code


@needs_posix
def test_dry_run_then_prime_never_silently_does_nothing(store_rig, capsys):
    """Live 2026-10-02: `prime --dry-run 4` said "would prime now", `prime 4`
    printed "Nothing to prime." in the same second, `prime 4` ~20 s later
    primed. The usage endpoint throttled the account (429): its pre-launch
    reading could not be refreshed, the attempt returned without an event,
    and the CLI reported nothing at all."""
    harness, fake, endpoint = store_rig
    harness.clock.advance(200)  # past the serve TTL: collectors re-fetch
    endpoint["errors"]["2"] = "http-429"

    prime_cli.prime_command(["--dry-run", "2"])
    assert "#2  would prime now (window cold, attempt 1/2)" in capsys.readouterr().out

    code = _run_prime(["2"])
    out = capsys.readouterr().out
    assert fake.calls() == []
    assert "Nothing to prime." not in out
    [line] = [ln for ln in out.splitlines() if "#2" in ln or "Account-2" in ln]
    assert line == (
        "#2  not primed (no usage reading from the last 60s: the last usage "
        "fetch failed (http-429); fetches back off for 30s more)"
    )
    assert "@example.com" not in out
    assert code == 1  # asked to prime, nothing was sent

    # Once the endpoint answers again, the same command primes.
    endpoint["errors"].clear()
    harness.clock.advance(600)  # past the failure backoff
    assert _run_prime(["2"]) == 0
    out = capsys.readouterr().out
    assert "Account-2: 5h window primed" in out
    assert len(fake.calls()) == 1


@needs_posix
def test_prime_reports_every_account_it_did_not_launch(store_rig, capsys, monkeypatch):
    """Without account arguments too: one line per target, slot number only."""
    harness, fake, endpoint = store_rig
    harness.clock.advance(200)
    # The CLI builds its own engine: patch the class.
    monkeypatch.setattr(
        prime_cli.AutoSwitchEngine, "_freshen_target", lambda self, num, email: "transient"
    )
    code = _run_prime([])
    out = capsys.readouterr().out
    assert fake.calls() == []
    assert "Nothing to prime." not in out
    assert "#1  skip (active)" in out
    [line] = [ln for ln in out.splitlines() if "#2" in ln or "Account-2" in ln]
    assert line == "#2  not primed (access token not ready (transient))"
    assert "@example.com" not in out
    assert code == 1


def test_manual_prime_reports_reasons_when_nothing_is_eligible(cli_rig):
    """The TUI's entry point: the same plan and checks as the CLI, returned
    as a report whose lines always say why nothing was primed."""
    harness, fake = cli_rig
    seen: list = []
    report = prime_cli.manual_prime(
        harness.switcher, {"1", "3"}, dry_run=False,
        emit=seen.append, sleep=harness.clock.advance, clock=harness.clock,
    )
    assert fake.calls() == []
    assert (report.events, report.pending, report.not_primed) == ([], [], {})
    assert report.lines() == [
        "#1  skip (active)", "#3  skip (window-on)", "Nothing to prime.",
    ]
    assert report.failed is False
    dry = prime_cli.manual_prime(
        harness.switcher, None, dry_run=True,
        emit=seen.append, sleep=harness.clock.advance, clock=harness.clock,
    )
    assert dry.lines() == [
        "#1  skip (active)",
        "#2  would prime now (window cold, attempt 1/2)",
        "#3  skip (window-on)",
    ]
    assert "@" not in "\n".join(report.lines() + dry.lines())


def _dry_run(argv: list[str]) -> int:
    """``prime --dry-run``'s exit code (0 when it simply returns)."""
    try:
        prime_cli.prime_command(["--dry-run", *argv])
    except SystemExit as exc:
        return exc.code
    return 0


@needs_posix
def test_dry_run_reports_the_version_guard_pause_like_a_real_run(cli_rig, capsys):
    """After a Claude Code update (or a failed `prime verify`) the real run is
    refused; the dry run used to say "#2 would prime now"."""
    from claude_swap.maximize import prime_verify as pv

    harness, fake = cli_rig
    pv.record_failed(harness.switcher.backup_dir, "2.1.230", ["invalid token is rejected"])

    assert _dry_run(["2"]) == 1
    out = capsys.readouterr().out
    assert "would prime now" not in out
    assert "Priming is paused: prime verify failed for claude 2.1.230" in out
    assert "#2  not primed (priming is paused, see above)" in out

    assert _run_prime(["2"]) == 1
    real = capsys.readouterr().out
    assert "prime verify failed for claude 2.1.230" in real
    assert fake.calls() == []


@needs_posix
def test_dry_run_reports_a_relogin_pause_like_a_real_run(cli_rig, capsys):
    from claude_swap.maximize import pause

    harness, fake = cli_rig
    pause.pause(harness.switcher.backup_dir, "re-login #3", now=harness.clock())

    assert _dry_run(["2"]) == 1
    dry = capsys.readouterr().out
    assert "#2  not primed (switching paused (re-login #3))" in dry

    assert _run_prime(["2"]) == 1
    real = capsys.readouterr().out
    assert "#2  not primed (switching paused (re-login #3))" in real
    assert fake.calls() == []


def test_dry_run_notes_auto_off(cli_rig, capsys):
    """`auto off` stops the engine's priming, not a manual `cc-swap prime`:
    the dry run still plans, and says so."""
    from claude_swap.maximize import pause

    harness, _fake = cli_rig
    pause.set_auto_off(harness.switcher.backup_dir, True, by="cli", now=harness.clock())
    assert _dry_run([]) == 0
    out = capsys.readouterr().out
    assert "#2  would prime now" in out
    assert "auto-switching is OFF" in out and "cc-swap prime" in out


def test_main_dispatches_prime(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(prime_cli, "prime_command", seen.append)
    monkeypatch.setattr(sys, "argv", ["cc-swap", "prime", "--dry-run", "2"])
    cli.main()
    assert seen == [["--dry-run", "2"]]
