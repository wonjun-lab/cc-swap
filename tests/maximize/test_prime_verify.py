"""maximize/prime_verify.py: priming pauses after a Claude Code update until
``cc-swap prime verify`` passes. Fakes only: no real ``claude``, no
Keychain, no real ``~/.claude*``."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from claude_swap.autoswitch import ConfigWarningEvent, PrimeEvent
from claude_swap.maximize import prime_verify as pv
from claude_swap.maximize.primer import PrimeRunResult
from tests.maximize.primer_support import Rig, StubRunner

REAL_READER = pv.read_claude_version

NOW = 1_800_000_000.0


@pytest.fixture
def rig(temp_home, tmp_path, monkeypatch) -> Rig:
    return Rig(temp_home, tmp_path, monkeypatch)


def _claude(tmp_path: Path, name: str = "claude") -> str:
    path = tmp_path / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


class Reader:
    def __init__(self, version: str | None):
        self.version = version
        self.calls = 0

    def __call__(self, _path: str) -> str | None:
        self.calls += 1
        return self.version


# -- the version and the gate --------------------------------------------------------------


class TestGate:
    @pytest.mark.parametrize(("text", "version"), [
        ("2.1.3 (Claude Code)\n", "2.1.3"),
        ("claude 10.0.12-beta.1", "10.0.12-beta.1"),
        ("no version here", None),
    ])
    def test_parse_version(self, text, version):
        assert pv.parse_version(text) == version

    def test_version_is_cached_until_the_executable_changes(self, tmp_path):
        claude = _claude(tmp_path)
        reader = Reader("2.1.3")
        assert pv.current_version(tmp_path, claude, reader=reader) == "2.1.3"
        assert pv.current_version(tmp_path, claude, reader=reader) == "2.1.3"
        assert reader.calls == 1
        Path(claude).write_text("#!/bin/sh\necho updated\n")  # `claude update`
        reader.version = "2.1.4"
        assert pv.current_version(tmp_path, claude, reader=reader) == "2.1.4"
        assert reader.calls == 2
        assert pv.last_seen_version(tmp_path) == "2.1.4"

    def test_no_record_lets_priming_run(self, tmp_path):
        verdict = pv.gate(tmp_path, _claude(tmp_path), reader=Reader("2.1.3"))
        assert verdict.ok and verdict.current == "2.1.3" and verdict.verified is None

    def test_a_changed_version_pauses_priming(self, tmp_path):
        claude = _claude(tmp_path)
        pv.record_verified(tmp_path, "2.1.3", by=pv.VERIFIED_BY_CLI, now=NOW)
        assert pv.gate(tmp_path, claude, reader=Reader("2.1.3")).ok
        Path(claude).write_text("#!/bin/sh\n# new build\n")
        verdict = pv.gate(tmp_path, claude, reader=Reader("2.1.4"))
        assert not verdict.ok
        assert "2.1.3 -> 2.1.4" in verdict.reason and "cc-swap prime verify" in verdict.reason
        assert pv.paused_note(tmp_path, auto_verify=False) == (
            "paused: claude 2.1.3 -> 2.1.4 (cc-swap prime verify)"
        )
        assert pv.paused_note(tmp_path) == (
            "paused: claude 2.1.3 -> 2.1.4 (the engine re-verifies it; or cc-swap prime verify)"
        )

    def test_fleet_attention_line_names_the_pause(self, tmp_path):
        from claude_swap.maximize import home
        from claude_swap.tui.fleet import prime_guard

        assert prime_guard(tmp_path) is None
        pv.record_verified(tmp_path, "2.1.3", by=pv.VERIFIED_BY_CLI)
        pv.current_version(tmp_path, _claude(tmp_path), reader=Reader("2.1.4"))
        guard = prime_guard(tmp_path)
        assert (guard.kind, guard.previous, guard.version, guard.auto) == (
            "changed", "2.1.3", "2.1.4", True,
        )
        assert guard.note == pv.paused_note(tmp_path)
        notices = home.attention_notices([], now=0.0, prime_guard=guard, priming=True)
        # The engine lifts it by itself: amber, no "!", at every width.
        assert home.attention_lines(notices, 117) == [(
            "priming paused: claude 2.1.3→2.1.4, the engine re-verifies it on its own "
            "(or cc-swap prime verify)", "warn",
        )]
        assert home.attention_lines(notices, 77) == [
            ("priming paused: claude 2.1.3→2.1.4, re-verifying on its own", "warn"),
        ]
        # With prime.autoVerify off only you can lift it: "!" and the command.
        manual = pv.paused_view(tmp_path, auto_verify=False)
        lines = home.attention_lines(
            home.attention_notices([], now=0.0, prime_guard=manual, priming=True), 50,
        )
        assert lines == [("! priming paused: run cc-swap prime verify", "warn")]

    def test_an_unreadable_version_pauses_once_a_version_was_verified(self, tmp_path):
        pv.record_verified(tmp_path, "2.1.3", by=pv.VERIFIED_BY_CLI)
        verdict = pv.gate(tmp_path, _claude(tmp_path), reader=Reader(None))
        assert not verdict.ok and "--version" in verdict.reason

    def test_version_output_that_is_not_utf8_is_read_not_crashed_on(self, tmp_path):
        claude = tmp_path / "bin" / "claude"
        claude.parent.mkdir(parents=True)
        claude.write_text("#!/bin/sh\nprintf '\\377\\376 2.1.3 (Claude Code)\\n'\n")
        claude.chmod(0o755)
        assert REAL_READER(str(claude)) == "2.1.3"

    def test_first_verified_prime_becomes_the_baseline_but_never_overrides(self, tmp_path):
        pv.note_verified_prime(tmp_path, "2.1.3")
        assert pv.verified_version(tmp_path) == "2.1.3"
        assert pv.load(tmp_path)["verifiedBy"] == "primed"
        pv.note_verified_prime(tmp_path, "2.1.4")
        assert pv.verified_version(tmp_path) == "2.1.3"


# -- the primer honours the gate -----------------------------------------------------------


class TestPrimerGate:
    def _changed(self, rig, monkeypatch):
        pv.record_verified(rig.switcher.backup_dir, "2.1.3", by=pv.VERIFIED_BY_CLI)
        monkeypatch.setattr(pv, "read_claude_version", lambda _p: "2.1.4")

    def test_engine_priming_pauses_with_one_warning(self, rig, monkeypatch):
        self._changed(rig, monkeypatch)
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner, auto_verify=False)
        first = primer.run_due(rig.snap())
        assert [type(e) for e in first] == [ConfigWarningEvent]
        assert "2.1.3 -> 2.1.4" in first[0].message
        assert primer.run_due(rig.snap()) == []  # warned once per change
        assert runner.calls == []

    def test_manual_prime_fails_with_the_reason(self, rig, monkeypatch):
        self._changed(rig, monkeypatch)
        runner = StubRunner(rig)
        events = rig.primer(runner=runner).prime_now(rig.snap(), sleep=rig.clock.advance)
        disabled = [e for e in events if isinstance(e, PrimeEvent)]
        assert [e.outcome for e in disabled] == ["disabled"]
        assert runner.calls == []

    @pytest.mark.parametrize("boom", [RuntimeError("boom"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x")])
    def test_a_crashing_version_check_keeps_priming_paused(self, rig, monkeypatch, boom):
        def crash(_p):
            raise boom

        monkeypatch.setattr(pv, "read_claude_version", crash)
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        first = primer.run_due(rig.snap())
        assert [type(e) for e in first] == [ConfigWarningEvent]
        assert "could not read claude version" in first[0].message
        assert primer.run_due(rig.snap()) == []  # warned once
        assert runner.calls == []

    def test_a_crashing_version_check_fails_a_manual_prime_with_the_reason(self, rig, monkeypatch):
        def crash(_p):
            raise RuntimeError("boom")

        monkeypatch.setattr(pv, "read_claude_version", crash)
        runner = StubRunner(rig)
        events = rig.primer(runner=runner).prime_now(rig.snap(), sleep=rig.clock.advance)
        disabled = [e for e in events if isinstance(e, PrimeEvent)]
        assert [e.outcome for e in disabled] == ["disabled"]
        assert "could not read claude version" in disabled[0].detail
        assert runner.calls == []

    def test_a_recovered_version_check_lifts_the_pause(self, rig, monkeypatch):
        calls = []

        def flaky(_p):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return "9.9.9"

        monkeypatch.setattr(pv, "read_claude_version", flaky)
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        primer.run_due(rig.snap())
        assert runner.calls == []
        primer.run_due(rig.snap())
        assert len(runner.calls) == 1

    # -- item 4: re-check just before the launch ------------------------------------

    def _during_precheck(self, rig, primer, action):
        """Run ``action`` once the first launch's usage pre-check starts: after
        the tick's version gate, before the launch."""
        original = primer._fresh_reading
        done = []

        def wrapped(*a, **kw):
            if not done:
                done.append(1)
                action()
            return original(*a, **kw)

        primer._fresh_reading = wrapped

    def test_a_binary_swapped_after_the_gate_is_not_launched(self, rig):
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        self._during_precheck(
            rig, primer, lambda: rig.fake.path.write_text(rig.fake.path.read_text() + "\n# updated\n")
        )
        primer.run_due(rig.snap())
        assert runner.calls == []
        assert rig.primes() == {}  # nothing claimed: the next tick retries
        primer.run_due(rig.snap())  # the next tick gates the new binary afresh
        assert len(runner.calls) == 1

    def test_manual_prime_reports_why_a_swapped_binary_was_held_back(self, rig):
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        self._during_precheck(
            rig, primer, lambda: rig.fake.path.write_text(rig.fake.path.read_text() + "\n# updated\n")
        )
        primer.prime_now(rig.snap(), sleep=rig.clock.advance)
        assert runner.calls == []
        assert any("changed" in why for why in primer.not_primed.values())

    def test_verify_lifts_the_pause_without_a_restart(self, rig, monkeypatch):
        self._changed(rig, monkeypatch)
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner, auto_verify=False)
        primer.run_due(rig.snap())
        pv.record_verified(rig.switcher.backup_dir, "2.1.4", by=pv.VERIFIED_BY_CLI)
        primer.run_due(rig.snap())
        assert len(runner.calls) == 1

    def test_gate_off_for_prime_verify_live(self, rig, monkeypatch):
        self._changed(rig, monkeypatch)
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        primer._version_gate = False
        primer.run_due(rig.snap())
        assert len(runner.calls) == 1

    def test_a_verified_prime_records_the_baseline(self, rig):
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        primer.run_due(snap)
        assert pv.verified_version(rig.switcher.backup_dir) is None
        rig.clock.advance(31)
        events = primer.run_due(snap)
        assert [e.outcome for e in events] == ["primed"]
        assert pv.verified_version(rig.switcher.backup_dir) == "9.9.9"  # conftest's fake


# -- prime verify -------------------------------------------------------------------------------


class FakeSystem:
    """A fake Keychain, credential files and runner for run_verify."""

    def __init__(self, tmp_path: Path, *, macos: bool = True):
        self.items: dict[str, str] = {"Claude Code-credentials": 'acct "me"\nmdat "2026"\n'}
        self.deleted: list[str] = []
        self.calls: list[dict] = []
        self.creds = tmp_path / "fake-home" / ".claude" / ".credentials.json"
        self.config = tmp_path / "fake-home" / ".claude.json"
        self.creds.parent.mkdir(parents=True)
        self.creds.write_text('{"claudeAiOauth": {"accessToken": "sk-live"}}')
        self.config.write_text(json.dumps({"oauthAccount": {"emailAddress": "me@x.com"}}))
        self.result = PrimeRunResult(
            1, False, "", '{"is_error":true,"api_error_status":401,"result":"Invalid bearer token"}',
            True, 401, "Invalid bearer token",
        )
        self.during_run = None
        self.macos = macos

    def run(self, argv, env, cwd, timeout):
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": Path(cwd)})
        if self.during_run is not None:
            self.during_run(env, Path(cwd))
        return self.result

    def deps(self) -> pv.VerifyDeps:
        return pv.VerifyDeps(
            run=self.run,
            version=lambda _p: "2.1.4",
            keychain_attrs=self.items.get,
            keychain_delete=lambda s: (self.deleted.append(s), self.items.pop(s, None)),
            macos=lambda: self.macos,
            active_services=lambda: ["Claude Code-credentials"],
            credentials_path=lambda: self.creds,
            config_path=lambda: self.config,
        )


def _verify(tmp_path, system: FakeSystem, **kw) -> pv.VerifyReport:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return pv.run_verify(root, "/opt/claude", deps=system.deps(), now=NOW, **kw)


def failed(report: pv.VerifyReport) -> list[str]:
    return [c.name for c in report.checks if not c.ok]


class TestRunVerify:
    def test_clean_401_passes_and_records_the_version(self, tmp_path):
        system = FakeSystem(tmp_path)
        pv.record_verified(tmp_path / "root", "2.1.3", by=pv.VERIFIED_BY_CLI)
        report = _verify(tmp_path, system)
        assert report.ok and report.recorded and failed(report) == []
        assert pv.verified_version(tmp_path / "root") == "2.1.4"
        [call] = system.calls
        env = call["env"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == pv.BAD_TOKEN
        assert env["CLAUDE_CONFIG_DIR"] == str(call["cwd"])
        assert call["cwd"].parent == tmp_path / "root"
        assert not call["cwd"].exists()  # the throwaway profile is gone
        lines = "\n".join(pv.report_lines(report))
        assert "(was 2.1.3)" in lines and "verified for claude 2.1.4" in lines
        # Nothing secret, nothing from the attributes, no email.
        assert "sk-live" not in lines and "mdat" not in lines and "@" not in lines

    def test_a_verify_refreshes_the_binary_identity_version_cache(self, tmp_path):
        system = FakeSystem(tmp_path)
        claude = _claude(tmp_path)
        root = tmp_path / "root"
        root.mkdir()
        pv.note_seen(root, claude, "2.1.3")  # a stale reading of this very file
        report = pv.run_verify(root, claude, deps=system.deps(), now=NOW)
        assert report.ok
        reader = Reader("0.0.0")
        assert pv.current_version(root, claude, reader=reader) == "2.1.4"
        assert reader.calls == 0  # served from the refreshed cache

    def test_a_failed_verify_still_refreshes_the_cache(self, tmp_path):
        system = FakeSystem(tmp_path)
        system.result = PrimeRunResult(0, False, "", '{"is_error":false}', False)
        claude = _claude(tmp_path)
        root = tmp_path / "root"
        root.mkdir()
        report = pv.run_verify(root, claude, deps=system.deps(), now=NOW)
        assert not report.ok
        assert pv.last_seen_version(root) == "2.1.4"

    def test_an_accepted_invalid_token_fails(self, tmp_path):
        system = FakeSystem(tmp_path)
        system.result = PrimeRunResult(0, False, "", '{"is_error":false}', False)
        report = _verify(tmp_path, system)
        assert failed(report) == ["invalid token is rejected"]
        assert "ACCEPTED" in report.checks[2].detail
        assert not report.recorded and pv.verified_version(tmp_path / "root") is None

    def test_a_failed_verify_invalidates_an_earlier_verification(self, tmp_path):
        # Verified 2.1.4 earlier; now the same build fails isolation. The old
        # record must not keep priming going.
        root = tmp_path / "root"
        root.mkdir()
        claude = _claude(tmp_path)
        system = FakeSystem(tmp_path)
        clean_401 = system.result
        assert pv.run_verify(root, claude, deps=system.deps(), now=NOW).ok
        assert pv.gate(root, claude, reader=Reader("2.1.4")).ok

        system.result = PrimeRunResult(0, False, "", '{"is_error":false}', False)
        report = pv.run_verify(root, claude, deps=system.deps(), now=NOW + 60)
        assert not report.ok and not report.recorded
        assert pv.verified_version(root) is None
        verdict = pv.gate(root, claude, reader=Reader("2.1.4"))
        assert not verdict.ok
        assert "failed" in verdict.reason and "cc-swap prime verify" in verdict.reason
        assert pv.paused_note(root) is not None and "failed" in pv.paused_note(root)
        # A primed run after the failure must not quietly adopt a baseline.
        pv.note_verified_prime(root, "2.1.4")
        assert pv.verified_version(root) is None

        # Passing again lifts it.
        system.result = clean_401
        assert pv.run_verify(root, claude, deps=system.deps(), now=NOW + 120).ok
        assert pv.gate(root, claude, reader=Reader("2.1.4")).ok
        assert pv.paused_note(root) is None

    def test_a_failed_first_verify_pauses_priming_too(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        claude = _claude(tmp_path)
        system = FakeSystem(tmp_path)
        system.result = PrimeRunResult(0, False, "", '{"is_error":false}', False)
        assert not pv.run_verify(root, claude, deps=system.deps(), now=NOW).ok
        assert not pv.gate(root, claude, reader=Reader("2.1.4")).ok

    def test_doctor_reports_a_failed_verify_as_paused(self, tmp_path):
        from claude_swap.maximize import doctor as dr

        root = tmp_path / "root"
        root.mkdir()
        system = FakeSystem(tmp_path)
        assert pv.run_verify(root, _claude(tmp_path), deps=system.deps(), now=NOW).ok
        system.result = PrimeRunResult(0, False, "", '{"is_error":false}', False)
        pv.run_verify(root, _claude(tmp_path), deps=system.deps(), now=NOW + 60)
        note, verified = dr.priming_guard(root)
        assert note is not None and verified is None

    def test_a_failure_that_is_not_auth_fails(self, tmp_path):
        system = FakeSystem(tmp_path)
        system.result = PrimeRunResult(1, False, "network down", "", None)
        assert failed(_verify(tmp_path, system)) == ["invalid token is rejected"]

    def test_a_stray_keychain_item_fails_and_is_deleted(self, tmp_path):
        system = FakeSystem(tmp_path)

        def leave_item(env, cwd):
            from claude_swap.session import keychain_service_name

            system.items[keychain_service_name(cwd)] = "attrs"

        system.during_run = leave_item
        report = _verify(tmp_path, system)
        assert failed(report) == ["no Keychain item left behind"]
        assert len([s for s in system.items if s.startswith("Claude Code-credentials-")]) == 0

    def test_a_stray_credentials_file_fails(self, tmp_path):
        system = FakeSystem(tmp_path)
        system.during_run = lambda env, cwd: (cwd / ".credentials.json").write_text("{}")
        assert failed(_verify(tmp_path, system)) == ["no .credentials.json left behind"]

    @pytest.mark.parametrize("what", ["keychain", "file", "account"])
    def test_a_touched_active_login_fails(self, tmp_path, what):
        system = FakeSystem(tmp_path)

        def touch(env, cwd):
            if what == "keychain":
                system.items["Claude Code-credentials"] = 'acct "me"\nmdat "2027"\n'
            elif what == "file":
                system.creds.write_text('{"claudeAiOauth": {"accessToken": "sk-other"}}')
            else:
                system.config.write_text(json.dumps({"oauthAccount": {"emailAddress": "o@x.com"}}))

        system.during_run = touch
        report = _verify(tmp_path, system)
        assert failed(report) == ["active login unchanged"]
        assert "@" not in report.checks[-1].detail and "sk-" not in report.checks[-1].detail

    def test_linux_has_no_keychain_checks(self, tmp_path):
        system = FakeSystem(tmp_path, macos=False)
        report = _verify(tmp_path, system)
        assert report.ok
        assert all("Keychain" not in c.name for c in report.checks)

    def test_missing_claude(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        report = pv.run_verify(root, None, deps=FakeSystem(tmp_path).deps())
        assert failed(report) == ["claude found"] and not report.recorded

    def test_live_runs_only_after_the_zero_cost_checks_pass(self, tmp_path):
        system = FakeSystem(tmp_path)
        system.result = PrimeRunResult(0, False, "", '{"is_error":false}', False)
        ran = []
        _verify(tmp_path, system, live=lambda: (ran.append(1), (True, "ok"))[1])
        assert ran == []

    def test_live_pass_and_fail(self, tmp_path):
        report = _verify(tmp_path, FakeSystem(tmp_path), live=lambda: (True, "#2: primed"))
        assert report.ok and report.recorded
        assert [c.name for c in report.checks][-3:] == [
            "one live prime", "active login unchanged by the live prime",
            "no Keychain item left by the live prime",
        ]
        tmp2 = tmp_path / "second"
        tmp2.mkdir()
        report = _verify(tmp2, FakeSystem(tmp2), live=lambda: (False, "no idle account"))
        assert failed(report) == ["one live prime"] and not report.recorded


class TestVerifyCommand:
    def test_cli_runs_with_injected_deps_and_prints_json(self, temp_home, tmp_path, monkeypatch, capsys):
        from claude_swap import cli
        from claude_swap.maximize import primer

        system = FakeSystem(tmp_path)
        monkeypatch.setattr(pv, "default_deps", system.deps)
        monkeypatch.setattr(primer, "resolve_claude_path", lambda _c, **k: "/opt/claude")
        monkeypatch.setattr(sys, "argv", ["cc-swap", "prime", "verify", "--json"])
        with pytest.raises(SystemExit) as exit_:
            cli.main()
        assert exit_.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is True and data["claudeVersion"] == "2.1.4" and data["recorded"]
        assert [c["name"] for c in data["checks"]][:2] == ["claude found", "claude --version"]

    def test_cli_failure_exits_1(self, temp_home, tmp_path, monkeypatch, capsys):
        from claude_swap import cli
        from claude_swap.maximize import primer

        system = FakeSystem(tmp_path)
        system.result = PrimeRunResult(0, False, "", "", None)
        monkeypatch.setattr(pv, "default_deps", system.deps)
        monkeypatch.setattr(primer, "resolve_claude_path", lambda _c, **k: "/opt/claude")
        monkeypatch.setattr(sys, "argv", ["cc-swap", "prime", "verify"])
        with pytest.raises(SystemExit) as exit_:
            cli.main()
        assert exit_.value.code == 1
        out = capsys.readouterr().out
        assert "FAIL  invalid token is rejected" in out and "Not verified" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
    def test_read_claude_version_runs_without_credentials(self, tmp_path, monkeypatch):
        # The real reader (bound at import, before conftest's stand-in),
        # against a stub script — never a real claude.
        script = tmp_path / "claude"
        script.write_text(
            "#!/bin/sh\n"
            'if [ -n "$CLAUDE_CODE_OAUTH_TOKEN$ANTHROPIC_API_KEY" ]; then echo leaked; exit 3; fi\n'
            'echo "2.1.7 (Claude Code)"\n'
        )
        script.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-should-not-pass")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-not-pass")
        assert REAL_READER(str(script)) == "2.1.7"
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"]  # ours untouched
