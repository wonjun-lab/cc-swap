"""The priming child process and its event (Task 11, cycle A)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from claude_swap.autoswitch import PrimeEvent
from claude_swap.maximize.primer import (
    PrimeRunResult,
    build_prime_argv,
    build_prime_env,
    run_prime,
)
from tests.maximize.fake_claude import FakeClaude

needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake claude is a POSIX shebang script"
)


class TestPrimeEvent:
    def test_json_and_human_carry_slot_not_email(self):
        event = PrimeEvent("3", "primed", "2026-10-02T14:20:00Z")
        payload = event.to_json()
        assert payload["event"] == "prime"
        assert payload["account"] == "3"
        assert payload["outcome"] == "primed"
        assert payload["resetsAt"] == "2026-10-02T14:20:00Z"
        assert event.human() == "Account-3: 5h window primed, resets 2026-10-02T14:20:00Z"
        assert PrimeEvent("", "disabled", None, "x").human() == "priming: 5h window disabled (x)"


@needs_posix
class TestRunPrime:
    def test_runs_isolated_child_with_closed_stdin(self, tmp_path):
        fake = FakeClaude.install(tmp_path / "bin")
        profile = tmp_path / "profile"
        profile.mkdir()
        env = build_prime_env({"PATH": os.environ.get("PATH", "")}, profile, "sk-ant-oat01-abcdefgh")
        result = run_prime(build_prime_argv(str(fake.path), "claude-haiku-4-5"), env, profile)
        assert result == PrimeRunResult(
            0, False, "", '{"type": "result", "subtype": "success", "is_error": false, "result": "OK"}', False
        )
        [call] = fake.calls()
        assert call["argv"] == build_prime_argv("x", "claude-haiku-4-5")[1:]
        assert Path(call["cwd"]).resolve() == profile.resolve()
        assert call["stdinIsDevnull"] is True
        assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abcdefgh"

    def test_output_is_masked_and_errors_parsed(self, tmp_path):
        fake = FakeClaude.install(tmp_path / "bin")
        fake.behave({
            "exitCode": 1,
            "stdout": '{"is_error": true, "result": "Invalid API key"}',
            "stderr": "token sk-ant-oat01-abcdefgh rejected for b@example.com",
        })
        env = build_prime_env({}, tmp_path, "sk-ant-oat01-abcdefgh")
        result = run_prime(build_prime_argv(str(fake.path), "m"), env, tmp_path)
        assert result.returncode == 1
        assert result.is_error is True
        assert "sk-ant-oat01" not in result.stderr_tail
        assert "b@example.com" not in result.stderr_tail

    def test_timeout_kills_the_child(self, tmp_path):
        fake = FakeClaude.install(tmp_path / "bin")
        fake.behave({"sleep": 30})
        env = build_prime_env({}, tmp_path, "sk-ant-oat01-abcdefgh")
        started = time.monotonic()
        result = run_prime(build_prime_argv(str(fake.path), "m"), env, tmp_path, timeout_s=1.0)
        assert result.timed_out is True
        assert result.returncode is None
        assert time.monotonic() - started < 10

    def test_missing_executable_is_reported_not_raised(self, tmp_path):
        env = build_prime_env({}, tmp_path, "sk-ant-oat01-abcdefgh")
        result = run_prime([str(tmp_path / "nope"), "-p"], env, tmp_path)
        assert result.returncode is None
        assert result.timed_out is False
        assert "No such file" in result.stderr_tail
