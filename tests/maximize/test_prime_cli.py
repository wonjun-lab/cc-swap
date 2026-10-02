"""`cc-swap prime [N ...] [--dry-run]` (Task 11)."""

from __future__ import annotations

import sys

import pytest

from claude_swap import cli
from claude_swap.maximize import prime_cli
from claude_swap.maximize.primer import expected_reset
from claude_swap.settings import atomic_write_json, settings_path
from tests.maximize.fake_claude import FakeClaude
from tests.maximize.primer_support import FakeUsage, H
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


def test_main_dispatches_prime(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(prime_cli, "prime_command", seen.append)
    monkeypatch.setattr(sys, "argv", ["cc-swap", "prime", "--dry-run", "2"])
    cli.main()
    assert seen == [["--dry-run", "2"]]
