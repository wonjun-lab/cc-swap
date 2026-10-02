"""Pure primer functions (Task 10): target selection, reset math, env/argv."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from claude_swap.maximize.model import AccountView, Snapshot
from claude_swap.maximize.primer import (
    COLD_KEY,
    DEFAULT_JITTER,
    PrimeRunResult,
    attempts_used,
    build_prime_argv,
    build_prime_env,
    classify_failure,
    due_targets,
    expected_reset,
    floor10,
    iso_minute,
    mask_secrets,
    resolve_claude_path,
    skip_reason,
    verified,
    window_key,
)
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS
from claude_swap.settings import MaximizeSettings, PrimeSettings

# 2026-10-02T14:20:00Z is a 10-minute boundary; NOW sits 400 s into its bucket.
BUCKET = 1_790_950_800.0
NOW = BUCKET + 400.0
H = 3600.0


def _epoch(iso: str) -> float:
    ts = parse_reset_ts(iso)
    assert ts is not None, iso
    return ts


def _view(
    number: str,
    email: str | None = None,
    *,
    tier: str = "normal",
    pct5: float | None = 0.0,
    reset5: float | None = None,
    pct7: float | None = 10.0,
    reset7: float | None = None,
    quarantined: bool = False,
    api_key: bool = False,
) -> AccountView:
    return AccountView(
        number=number,
        email=email or f"u{number}@example.com",
        tier=tier,
        plan_weight=1,
        pct5=pct5,
        reset5=reset5,
        pct7=pct7,
        reset7=reset7,
        quarantined=quarantined,
        api_key=api_key,
    )


def _snap(views: list[AccountView], *, active: str | None = "1", now: float = NOW) -> Snapshot:
    return Snapshot(
        now=now,
        active=active,
        accounts=tuple(views),
        samples=(),
        last_switch_at=None,
        settings=MaximizeSettings(),
    )


ACTIVE = _view("1", pct5=40.0, reset5=NOW + 2 * H)
SETTINGS = PrimeSettings(enabled=True)


class TestResetMath:
    def test_floor10(self):
        assert floor10(BUCKET) == BUCKET
        assert floor10(BUCKET + 599.9) == BUCKET
        assert floor10(BUCKET - 0.1) == BUCKET - 600.0

    @pytest.mark.parametrize(
        "prime_at, reset",
        [
            ("2026-10-02T09:19:35Z", "2026-10-02T14:10:00Z"),  # spec §3.1 example
            ("2026-10-02T09:29:00Z", "2026-10-02T14:20:00Z"),  # spec §3.1 example
            ("2026-10-02T09:20:00Z", "2026-10-02T14:20:00Z"),  # exactly on a boundary
        ],
    )
    def test_expected_reset_floors_to_ten_minutes(self, prime_at, reset):
        assert expected_reset(_epoch(prime_at)) == _epoch(reset)

    def test_verified_tolerates_server_jitter(self):
        prime_at = _epoch("2026-10-02T09:19:35Z")  # expects 14:10:00Z
        assert verified(prime_at, _epoch("2026-10-02T14:09:59.63Z"))
        assert verified(prime_at, _epoch("2026-10-02T14:10:00.8+00:00"))
        assert verified(prime_at, _epoch("2026-10-02T14:11:59Z"))
        assert not verified(prime_at, _epoch("2026-10-02T14:12:01Z"))
        assert not verified(prime_at, _epoch("2026-10-02T14:20:00Z"))
        assert not verified(prime_at, None)
        # Window keys see through the same noise and both spellings.
        now = _epoch("2026-10-02T14:12:00Z")
        a = _view("2", reset5=_epoch("2026-10-02T14:09:59.63Z"))
        b = _view("2", reset5=_epoch("2026-10-02T14:10:00.8+00:00"))
        assert window_key(a, None, now) == window_key(b, None, now) == "2026-10-02T14:10:00Z"
        assert iso_minute(_epoch("2026-10-02T14:10:00Z")) == "2026-10-02T14:10:00Z"


class TestSkipReasons:
    """One row per rule of spec §6.1 plus the retry bookkeeping of §6.3."""

    @pytest.mark.parametrize(
        "view, entry, reason",
        [
            (_view("2"), None, None),  # cold window
            (_view("2", reset5=NOW - 120), None, None),  # reset passed, no reading since
            (_view("2", tier="last_resort"), None, None),  # last_resort is primed too
            (_view("1"), None, "active"),
            (_view("2", tier="excluded"), None, "excluded"),
            (_view("2", api_key=True), None, "api-key"),
            (_view("2", quarantined=True), None, "quarantined"),
            (_view("2", pct5=None), None, "usage-unknown"),
            (_view("2", pct7=None), None, "usage-unknown"),
            (_view("2", pct7=100.0), None, "7d-exhausted"),
            (_view("2", reset5=NOW + 60), None, "window-on"),
            (
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 1, "lastAttemptAt": NOW - 10, "lastOutcome": "launched"},
                "pending-verify",
            ),
            (
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 1, "lastAttemptAt": NOW - 10, "lastOutcome": "timeout"},
                "pending-verify",
            ),
            (
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 0, "lastAttemptAt": NOW - 60, "lastOutcome": "skipped-live"},
                "live-session",
            ),
            (
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 0, "lastAttemptAt": NOW - 700, "lastOutcome": "skipped-live"},
                None,
            ),
            (
                _view("2", reset7=NOW + 2 * H),
                {"windowKey": COLD_KEY, "attempts": 1, "lastAttemptAt": NOW - 2 * H, "lastOutcome": "rate-limited"},
                "rate-limited",
            ),
            (
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 1, "lastAttemptAt": NOW - 60, "lastOutcome": "rate-limited"},
                "rate-limited",
            ),
            (
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 1, "lastAttemptAt": NOW - 3700, "lastOutcome": "rate-limited"},
                None,
            ),
            (
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 2, "lastAttemptAt": NOW - 100, "lastOutcome": "unverified"},
                "attempts-exhausted",
            ),
            (  # given up more than 5 h ago: a new off-period, attempts start over
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 2, "lastAttemptAt": NOW - 5 * H - 1, "lastOutcome": "unverified"},
                None,
            ),
            (  # our own verification saw it open; a stale cold snapshot must not re-prime
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 1, "lastAttemptAt": NOW - 60, "lastOutcome": "primed", "resetsAt": NOW + 4 * H},
                "window-on",
            ),
            (  # pre-check found it opened elsewhere
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 0, "lastAttemptAt": NOW - 60, "lastOutcome": "already-on", "resetsAt": NOW + H},
                "window-on",
            ),
            (  # a successful prime ends the period even under the same key
                _view("2"),
                {"windowKey": COLD_KEY, "attempts": 2, "lastAttemptAt": NOW - 6 * H, "lastOutcome": "primed"},
                None,
            ),
        ],
    )
    def test_skip_reason_table(self, view, entry, reason):
        assert skip_reason(view, "1", entry, NOW, max_attempts=2) == reason
        state = {view.email: entry} if entry is not None else {}
        targets = due_targets(_snap([ACTIVE, view]), state, SETTINGS, NOW, random.Random(1))
        assert [t.number for t in targets] == ([] if reason else [view.number])


class TestWindowKeys:
    def test_cold_reading_after_attempt_keeps_window_key(self):
        reset = BUCKET  # the elapsed reset, seen by the reading before the attempt
        entry = {
            "windowKey": iso_minute(reset),
            "attempts": 1,
            "lastAttemptAt": NOW - 100,
            "lastOutcome": "unverified",
        }
        cold = _view("2")  # the first post-reset reading: resets_at null
        assert window_key(cold, entry, NOW) == iso_minute(reset)
        assert attempts_used(entry, iso_minute(reset), NOW) == 1
        [target] = due_targets(_snap([ACTIVE, cold]), {cold.email: entry}, SETTINGS, NOW, random.Random(1))
        assert target.window_key == iso_minute(reset)
        assert target.due_at == NOW  # an "unverified" retry runs at once
        exhausted = {**entry, "attempts": 2}
        assert skip_reason(cold, "1", exhausted, NOW, 2) == "attempts-exhausted"

    def test_new_reset_is_a_new_period(self):
        entry = {
            "windowKey": "2026-10-02T09:10:00Z",
            "attempts": 2,
            "lastAttemptAt": NOW - 60,
            "lastOutcome": "unverified",
        }
        view = _view("2", reset5=NOW - 30)
        assert window_key(view, entry, NOW) == iso_minute(NOW - 30)
        assert attempts_used(entry, window_key(view, entry, NOW), NOW) == 0
        assert skip_reason(view, "1", entry, NOW, 2) is None


class TestDueTargets:
    def test_cold_window_is_due_within_the_current_bucket(self):
        [target] = due_targets(_snap([ACTIVE, _view("2")]), {}, SETTINGS, NOW, random.Random(3))
        lo, hi = DEFAULT_JITTER
        assert target.number == "2"
        assert target.window_key == COLD_KEY
        assert BUCKET + lo <= target.due_at <= BUCKET + hi

    def test_reset_passed_jitters_from_the_reset(self):
        reset = NOW - 120
        [target] = due_targets(
            _snap([ACTIVE, _view("2", reset5=reset)]), {}, SETTINGS, NOW, random.Random(3)
        )
        lo, hi = DEFAULT_JITTER
        assert target.window_key == iso_minute(reset)
        assert reset + lo <= target.due_at <= reset + hi

    def test_configured_jitter_is_used_and_bad_jitter_falls_back(self):
        view = _view("2", reset5=NOW - 120)
        fixed = PrimeSettings(enabled=True, jitter_s="10-10")
        [target] = due_targets(_snap([ACTIVE, view]), {}, fixed, NOW, random.Random(3))
        assert target.due_at == NOW - 110
        broken = PrimeSettings(enabled=True, jitter_s="garbage")
        [target] = due_targets(_snap([ACTIVE, view]), {}, broken, NOW, random.Random(3))
        assert NOW - 120 + DEFAULT_JITTER[0] <= target.due_at <= NOW - 120 + DEFAULT_JITTER[1]

    def test_many_due_targets_get_distinct_jitter(self):
        now = BUCKET + 10  # just woke up: every reset elapsed hours ago
        views = [_view(str(n), reset5=now - 3 * H) for n in range(2, 6)]
        snap = _snap([_view("1", reset5=now + 600), *views], now=now)
        targets = due_targets(snap, {}, SETTINGS, now, random.Random(7))
        dues = [t.due_at for t in targets]
        assert len(targets) == 4
        assert len(set(dues)) == 4
        assert all(BUCKET + 45 <= d <= BUCKET + 300 for d in dues)
        assert all(d > now for d in dues)  # nothing fires the moment the lid opens
        assert dues == sorted(dues)

    def test_state_for_other_accounts_is_ignored(self):
        state = {"someone@example.com": {"windowKey": COLD_KEY, "attempts": 9, "lastAttemptAt": NOW, "lastOutcome": "launched"}}
        targets = due_targets(_snap([ACTIVE, _view("2")]), state, SETTINGS, NOW, random.Random(1))
        assert [t.number for t in targets] == ["2"]


BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/u",
    "LANG": "en_US.UTF-8",
    "HTTPS_PROXY": "http://proxy:3128",
    "ANTHROPIC_API_KEY": "sk-ant-api03-userkey",
    "ANTHROPIC_AUTH_TOKEN": "bearer",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-someone-else",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": "3",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": "4",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
    "CLAUDE_CONFIG_DIR": "/elsewhere",
    "ANTHROPIC_BASE_URL": "https://gateway.example",
    "ANTHROPIC_MODEL": "opus",
    "ANTHROPIC_CUSTOM_HEADERS": "x-a: b",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "CLAUDE_CODE_USE_MANTLE": "1",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS": "1",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    "AWS_BEARER_TOKEN_BEDROCK": "aws",
    "CLOUD_ML_REGION": "us-east5",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_SSE_PORT": "4242",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "sk-ant-ort01-never",
    "MY_REFRESH_TOKEN": "sk-ant-ort01-never",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR": "5",
    "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR": "6",
    "CLAUDE_BRIDGE_OAUTH_TOKEN": "bridge",
    "CLAUDE_TRUSTED_DEVICE_TOKEN": "device",
}


class TestPrimeEnv:
    def test_only_config_dir_and_token_are_added(self, tmp_path):
        profile = tmp_path / "prime-profile"
        env = build_prime_env(BASE_ENV, profile, "sk-ant-oat01-target")
        assert set(env) == {
            "PATH", "HOME", "LANG", "HTTPS_PROXY",
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
        }
        assert env["CLAUDE_CONFIG_DIR"] == str(profile)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-target"

    @pytest.mark.parametrize("name", AUTH_OVERRIDE_ENV_VARS)
    def test_every_upstream_auth_override_is_scrubbed(self, name, tmp_path):
        env = build_prime_env({name: "x", "PATH": "/bin"}, tmp_path, "sk-ant-oat01-t")
        if name == "CLAUDE_CODE_OAUTH_TOKEN":
            assert env[name] == "sk-ant-oat01-t"  # replaced, not inherited
        else:
            assert name not in env

    @pytest.mark.parametrize(
        "name",
        [
            "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
            "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
            "CLAUDE_BRIDGE_OAUTH_TOKEN",
            "CLAUDE_TRUSTED_DEVICE_TOKEN",
            "CLAUDE_CODE_SOME_FUTURE_TOKEN",            # any CLAUDE*_TOKEN
            "CLAUDE_CODE_SOME_FUTURE_FILE_DESCRIPTOR",  # any fd-passed secret
        ],
    )
    def test_other_credential_channels_are_scrubbed(self, name, tmp_path):
        env = build_prime_env({name: "x", "PATH": "/bin"}, tmp_path, "sk-ant-oat01-t")
        assert name not in env

    def test_unrelated_claude_settings_pass_through(self, tmp_path):
        env = build_prime_env(
            {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "100", "PATH": "/bin"}, tmp_path, "sk-ant-oat01-t"
        )
        assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "100"

    def test_never_contains_a_refresh_token(self, tmp_path):
        env = build_prime_env(BASE_ENV, tmp_path, "sk-ant-oat01-target")
        assert not any("REFRESH_TOKEN" in k.upper() for k in env)
        assert not any(v.startswith("sk-ant-ort") for v in env.values())

    @pytest.mark.parametrize("token", ["", "   ", "sk-ant-ort01-abcdef"])
    def test_refuses_missing_or_refresh_token(self, token, tmp_path):
        with pytest.raises(ValueError):
            build_prime_env(BASE_ENV, tmp_path, token)

    def test_base_env_is_not_mutated(self, tmp_path):
        before = dict(BASE_ENV)
        build_prime_env(BASE_ENV, tmp_path, "sk-ant-oat01-target")
        assert BASE_ENV == before


def test_build_prime_argv_matches_spec():
    assert build_prime_argv("/bin/claude", "claude-haiku-4-5") == [
        "/bin/claude", "-p", "--model", "claude-haiku-4-5", "--safe-mode",
        "--tools", "", "--no-session-persistence", "--max-turns", "1",
        "--output-format", "json", "Reply OK",
    ]


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


class TestResolveClaudePath:
    def test_configured_path_wins(self, tmp_path):
        configured = _exe(tmp_path / "opt" / "claude")
        _exe(tmp_path / "home" / ".local" / "bin" / "claude")
        assert resolve_claude_path(str(configured), home=tmp_path / "home") == str(configured)

    def test_falls_back_to_local_bin(self, tmp_path):
        local = _exe(tmp_path / "home" / ".local" / "bin" / "claude")
        home = tmp_path / "home"
        assert resolve_claude_path(str(tmp_path / "gone" / "claude"), home=home) == str(local)
        assert resolve_claude_path(None, home=home) == str(local)

    def test_nothing_found(self, tmp_path):
        assert resolve_claude_path(None, home=tmp_path) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit")
    def test_non_executable_file_is_skipped(self, tmp_path):
        plain = tmp_path / "claude"
        plain.write_text("not a program")
        plain.chmod(0o644)
        assert resolve_claude_path(str(plain), home=tmp_path / "h") is None


@pytest.mark.parametrize(
    "stdout, stderr, kind",
    [
        ('{"type":"result","is_error":true,"result":"Invalid API key · Please run /login"}', "", "auth"),
        ("", 'API Error: 401 {"type":"error","error":{"type":"authentication_error"}}', "auth"),
        ('{"is_error":true,"result":"OAuth token has expired. Please obtain a new token"}', "", "auth"),
        ("", "API Error: 429 rate_limit_error", "rate-limited"),
        ('{"is_error":true,"result":"Claude AI usage limit reached|1790950800"}', "", "rate-limited"),
        ("", 'API Error: 404 {"type":"error","error":{"type":"not_found_error","message":"model: claude-haiku-4-5"}}', "model-not-found"),
        ('{"is_error":true,"result":"There\'s an issue with the selected model (claude-haiku-4-5). It may not exist or you may not have access to it."}', "", "model-not-found"),
        ("", "TypeError: undefined is not a function", "other"),
        # Structured fields decide; numbers elsewhere in the JSON never match.
        ('{"is_error":true,"duration_api_ms":401,"result":"API Error: 429 rate_limit_error"}', "", "rate-limited"),
        ('{"is_error":true,"api_error_status":401,"result":"Failed to authenticate"}', "", "auth"),
        ('{"is_error":true,"api_error_status":429,"duration_api_ms":401,"result":"Request failed"}', "", "rate-limited"),
        ('{"is_error":true,"api_error_status":404,"result":"Request failed"}', "", "model-not-found"),
        ('{"is_error":true,"duration_ms":429,"num_turns":401,"session_id":"0-401-429","result":"Execution error"}', "", "other"),
        ('{"is_error":true,"duration_api_ms":401,"result":"Execution error"}', "API Error: 429 rate_limit_error", "rate-limited"),
        # Non-JSON stdout is still read as text.
        ("API Error: 401 Unauthorized", "", "auth"),
    ],
)
def test_classify_failure(stdout, stderr, kind):
    result = PrimeRunResult.from_output(1, stdout, stderr)
    assert classify_failure(result) == kind


def test_from_output_reads_the_structured_fields():
    result = PrimeRunResult.from_output(
        1,
        '{"is_error":true,"api_error_status":429,"result":"limited for b@example.com"}',
        "",
        secret="s3cr3t",
    )
    assert (result.is_error, result.api_error_status) == (True, 429)
    assert result.result_text == "limited for <email>"
    plain = PrimeRunResult.from_output(1, "not json", "")
    assert (plain.is_error, plain.api_error_status, plain.result_text) == (None, None, None)


def test_mask_secrets():
    text = "auth sk-ant-oat01-AAAAbbbbCCCC for b@example.com, raw=s3cr3t-value " + "x" * 600
    masked = mask_secrets(text, "s3cr3t-value", limit=10_000)
    assert "sk-ant-oat01" not in masked
    assert "b@example.com" not in masked
    assert "s3cr3t-value" not in masked
    assert len(mask_secrets(text, "s3cr3t-value")) == 500
    assert mask_secrets(None) == ""
