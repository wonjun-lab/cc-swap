"""``prime.autoVerify``: after a Claude Code update the engine runs the
zero-cost ``prime verify`` itself. Fakes only: no real ``claude``, no
Keychain, no real ``~/.claude*`` (``FakeSystem`` stands in for all of it)."""

from __future__ import annotations

import json
import sys

import pytest

from claude_swap.autoswitch import ConfigWarningEvent, PrimeEvent
from claude_swap.locking import FileLock
from claude_swap.maximize import notify
from claude_swap.maximize import prime_verify as pv
from claude_swap.maximize.primer import PrimeRunResult
from tests.maximize.primer_support import Rig, StubRunner
from tests.maximize.test_prime_verify import FakeSystem, _claude

TIMEOUT = PrimeRunResult(None, True, "", "", None)
NETWORK_DOWN = PrimeRunResult(1, False, "network down", "", None)


@pytest.fixture
def rig(temp_home, tmp_path, monkeypatch) -> Rig:
    return Rig(temp_home, tmp_path, monkeypatch)


@pytest.fixture
def system(tmp_path) -> FakeSystem:
    return FakeSystem(tmp_path / "sys")


def _changed(rig, monkeypatch, *, to: str = "2.1.4") -> None:
    """Verified with 2.1.3; the installed claude now says ``to``."""
    pv.record_verified(rig.switcher.backup_dir, "2.1.3", by=pv.VERIFIED_BY_CLI, now=1.0)
    monkeypatch.setattr(pv, "read_claude_version", lambda _p: to)


def _primer(rig, system, runner=None, **settings):
    primer = rig.primer(runner=runner if runner is not None else StubRunner(rig), **settings)
    primer._verify_deps = system.deps
    return primer


def _outcomes(events) -> list[str]:
    return [e.outcome for e in events if isinstance(e, PrimeEvent)]


def _root(rig):
    return rig.switcher.backup_dir


class TestAutoVerify:
    def test_a_passing_verify_records_the_version_and_priming_resumes(
        self, rig, system, monkeypatch
    ):
        _changed(rig, monkeypatch)
        runner = StubRunner(rig)
        primer = _primer(rig, system, runner)
        events = primer.run_due(rig.snap())
        assert [type(e) for e in events] == [ConfigWarningEvent, PrimeEvent]
        assert _outcomes(events) == ["auto-verified"]
        assert "2.1.4" in events[1].detail and events[1].human().startswith("priming: claude 2.1.4")
        [call] = system.calls  # the zero-cost probe, with the invalid token
        assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == pv.BAD_TOKEN
        data = pv.load(_root(rig))
        assert data["verifiedClaudeVersion"] == "2.1.4"
        assert data["verifiedBy"] == pv.VERIFIED_BY_ENGINE
        assert runner.calls == []  # no prime in the tick that verified
        primer.run_due(rig.snap())
        assert len(runner.calls) == 1 and len(system.calls) == 1

    def test_an_isolation_failure_is_recorded_and_not_retried(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)
        system.during_run = lambda env, cwd: (cwd / ".credentials.json").write_text("{}")
        primer = _primer(rig, system)
        events = primer.run_due(rig.snap())
        assert _outcomes(events) == ["auto-verify-failed"]
        failed = pv.failed_verify(_root(rig))
        assert failed["version"] == "2.1.4"
        assert failed["checks"] == ["no .credentials.json left behind"]
        assert "cc-swap prime verify" in events[-1].detail
        assert pv.verified_version(_root(rig)) is None
        # The same version is never tried again: a manual verify is required.
        rig.clock.advance(pv.AUTO_RETRY_S * 10)
        primer.run_due(rig.snap())
        assert len(system.calls) == 1
        assert pv.paused_note(_root(rig)) == (
            "paused: prime verify failed for claude 2.1.4 (cc-swap prime verify)"
        )

    def test_an_accepted_invalid_token_is_an_isolation_failure(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)
        system.result = PrimeRunResult(0, False, "", '{"is_error":false}', False)
        events = _primer(rig, system).run_due(rig.snap())
        assert _outcomes(events) == ["auto-verify-failed"]
        assert pv.failed_verify(_root(rig))["checks"] == ["invalid token is rejected"]

    @pytest.mark.parametrize("result", [TIMEOUT, NETWORK_DOWN], ids=["timeout", "network"])
    def test_a_transient_failure_backs_off_then_gives_up(self, rig, system, monkeypatch, result):
        _changed(rig, monkeypatch)
        system.result = result
        primer = _primer(rig, system)
        events = primer.run_due(rig.snap())
        assert _outcomes(events) == ["auto-verify-retry"]
        assert pv.failed_verify(_root(rig)) is None  # not a failed verify (yet)
        assert pv.load(_root(rig))[pv.AUTO_KEY]["tries"] == 1
        # Within the backoff: no new try.
        rig.clock.advance(pv.AUTO_RETRY_S - 60)
        assert _outcomes(primer.run_due(rig.snap())) == []
        assert len(system.calls) == 1
        for tries in range(2, pv.AUTO_MAX_TRIES):
            rig.clock.advance(pv.AUTO_RETRY_S)
            assert _outcomes(primer.run_due(rig.snap())) == ["auto-verify-retry"]
            assert pv.load(_root(rig))[pv.AUTO_KEY]["tries"] == tries
        rig.clock.advance(pv.AUTO_RETRY_S)
        assert _outcomes(primer.run_due(rig.snap())) == ["auto-verify-failed"]
        assert len(system.calls) == pv.AUTO_MAX_TRIES
        assert pv.failed_verify(_root(rig))["checks"] == ["invalid token is rejected"]
        rig.clock.advance(pv.AUTO_RETRY_S * 10)
        primer.run_due(rig.snap())
        assert len(system.calls) == pv.AUTO_MAX_TRIES

    def test_a_transient_failure_then_a_pass(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)
        clean_401 = system.result
        system.result = TIMEOUT
        primer = _primer(rig, system)
        assert _outcomes(primer.run_due(rig.snap())) == ["auto-verify-retry"]
        system.result = clean_401
        rig.clock.advance(pv.AUTO_RETRY_S)
        assert _outcomes(primer.run_due(rig.snap())) == ["auto-verified"]
        assert pv.AUTO_KEY not in pv.load(_root(rig))

    def test_off_keeps_todays_behaviour(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)
        runner = StubRunner(rig)
        primer = _primer(rig, system, runner, auto_verify=False)
        first = primer.run_due(rig.snap())
        assert [type(e) for e in first] == [ConfigWarningEvent]
        assert primer.run_due(rig.snap()) == []
        assert system.calls == [] and runner.calls == []
        assert pv.verified_version(_root(rig)) == "2.1.3"

    def test_not_while_another_verify_holds_the_lock(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)
        primer = _primer(rig, system)
        lock = pv.verify_lock(_root(rig))
        assert lock.acquire()
        try:
            assert _outcomes(primer.run_due(rig.snap())) == []
            assert system.calls == []
            assert pv.AUTO_KEY not in pv.load(_root(rig))  # a skip costs no try
        finally:
            lock.release()
        assert _outcomes(primer.run_due(rig.snap())) == ["auto-verified"]

    def test_a_version_verified_meanwhile_is_not_verified_again(self, rig, system, monkeypatch):
        # Another process verifies while this engine waits for the lock.
        _changed(rig, monkeypatch)
        primer = _primer(rig, system)
        real_lock = pv.verify_lock

        def lock_after_a_manual_verify(root):
            pv.record_verified(root, "2.1.4", by=pv.VERIFIED_BY_CLI)
            return real_lock(root)

        monkeypatch.setattr(pv, "verify_lock", lock_after_a_manual_verify)
        assert _outcomes(primer.run_due(rig.snap())) == []
        assert system.calls == []
        assert pv.load(_root(rig))["verifiedBy"] == pv.VERIFIED_BY_CLI

    def test_not_when_the_version_is_unreadable(self, rig, system, monkeypatch):
        pv.record_verified(_root(rig), "2.1.3", by=pv.VERIFIED_BY_CLI)
        monkeypatch.setattr(pv, "read_claude_version", lambda _p: None)
        events = _primer(rig, system).run_due(rig.snap())
        assert [type(e) for e in events] == [ConfigWarningEvent]
        assert system.calls == []

    def test_not_when_the_version_check_crashed(self, rig, system, monkeypatch):
        def crash(_p):
            raise RuntimeError("boom")

        monkeypatch.setattr(pv, "read_claude_version", crash)
        events = _primer(rig, system).run_due(rig.snap())
        assert [type(e) for e in events] == [ConfigWarningEvent]
        assert system.calls == []

    def test_a_newer_build_after_a_failed_one_gets_its_own_verify(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)
        pv.record_failed(_root(rig), "2.1.4", ["no Keychain item left behind"], now=1.0)
        primer = _primer(rig, system)
        assert _outcomes(primer.run_due(rig.snap())) == []  # same version: manual only
        assert system.calls == []
        rig.fake.path.write_text(rig.fake.path.read_text() + "\n# 2.1.5\n")
        monkeypatch.setattr(pv, "read_claude_version", lambda _p: "2.1.5")
        primer._verify_deps = _with_version(system.deps, "2.1.5")
        assert _outcomes(primer.run_due(rig.snap())) == ["auto-verified"]
        assert pv.verified_version(_root(rig)) == "2.1.5"
        assert pv.failed_verify(_root(rig)) is None

    def test_manual_prime_never_auto_verifies(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)
        events = _primer(rig, system).prime_now(rig.snap(), sleep=rig.clock.advance)
        assert _outcomes(events) == ["disabled"]
        assert system.calls == []

    def test_a_crashing_verify_counts_as_a_transient_try(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(pv, "run_verify", boom)
        events = _primer(rig, system).run_due(rig.snap())
        assert _outcomes(events) == ["auto-verify-retry"]


def _with_version(make_deps, version):
    def deps():
        d = make_deps()
        d.version = lambda _p: version
        return d

    return deps


class TestTransientClassification:
    def _report(self, tmp_path, system):
        root = tmp_path / "root"
        root.mkdir(exist_ok=True)
        return pv.run_verify(root, "/opt/claude", deps=system.deps(), record=False)

    @pytest.mark.parametrize("result", [
        TIMEOUT,
        NETWORK_DOWN,
        PrimeRunResult(None, False, "OSError: busy", "", None),  # could not start
    ], ids=["timeout", "network", "spawn"])
    def test_transient(self, tmp_path, result):
        system = FakeSystem(tmp_path)
        system.result = result
        report = self._report(tmp_path, system)
        assert not report.ok and report.transient

    def test_an_unreadable_version_is_transient(self, tmp_path):
        system = FakeSystem(tmp_path)
        deps = system.deps()
        deps.version = lambda _p: None
        root = tmp_path / "root"
        root.mkdir()
        report = pv.run_verify(root, "/opt/claude", deps=deps, record=False)
        assert report.transient

    @pytest.mark.parametrize("what", ["accepted", "429", "keychain", "credentials", "login"])
    def test_isolation_failures_are_not(self, tmp_path, what):
        from claude_swap.session import keychain_service_name

        system = FakeSystem(tmp_path)
        if what == "accepted":
            system.result = PrimeRunResult(0, False, "", '{"is_error":false}', False)
        elif what == "429":
            # An invalid token is refused (401) before any rate limit: a 429
            # means claude authenticated with some other credential.
            system.result = PrimeRunResult(
                1, False, "", '{"is_error":true,"api_error_status":429,"result":"x"}',
                True, 429, "x",
            )
        elif what == "keychain":
            system.during_run = lambda env, cwd: system.items.__setitem__(
                keychain_service_name(cwd), "attrs")
        elif what == "credentials":
            system.during_run = lambda env, cwd: (cwd / ".credentials.json").write_text("{}")
        else:
            system.during_run = lambda env, cwd: system.config.write_text(
                json.dumps({"oauthAccount": {"emailAddress": "other@example.com"}}))
        report = self._report(tmp_path, system)
        assert not report.ok and not report.transient

    def test_a_timeout_plus_a_leftover_is_not(self, tmp_path):
        system = FakeSystem(tmp_path)
        system.result = TIMEOUT
        system.during_run = lambda env, cwd: (cwd / ".credentials.json").write_text("{}")
        assert not self._report(tmp_path, system).transient


class TestDisplays:
    def test_paused_note_names_the_engine_only_while_it_will_verify(self, tmp_path):
        root = tmp_path
        pv.record_verified(root, "2.1.3", by=pv.VERIFIED_BY_CLI)
        pv.current_version(root, _claude(tmp_path), reader=lambda _p: "2.1.4")
        assert pv.paused_state(root, auto_verify=True) == (
            f"paused: claude 2.1.3 -> 2.1.4 ({pv.AUTO_HINT})", True,
        )
        assert pv.paused_state(root, auto_verify=False) == (
            "paused: claude 2.1.3 -> 2.1.4 (cc-swap prime verify)", False,
        )
        # Waiting out a transient failure still counts as the engine's.
        pv.note_auto_retry(root, "2.1.4", "timeout", now=1.0)
        assert pv.paused_state(root, auto_verify=True)[1] is True
        pv.record_failed(root, "2.1.4", ["no Keychain item left behind"])
        assert pv.paused_state(root, auto_verify=True) == (
            "paused: prime verify failed for claude 2.1.4 (cc-swap prime verify)", False,
        )

    def test_paused_note_reads_the_setting(self, tmp_path):
        from claude_swap.settings import set_setting

        pv.record_verified(tmp_path, "2.1.3", by=pv.VERIFIED_BY_CLI)
        pv.current_version(tmp_path, _claude(tmp_path), reader=lambda _p: "2.1.4")
        assert pv.AUTO_HINT in pv.paused_note(tmp_path)
        set_setting(tmp_path, "prime.autoVerify", "false")
        assert pv.paused_note(tmp_path) == "paused: claude 2.1.3 -> 2.1.4 (cc-swap prime verify)"

    def test_doctor_says_the_engine_re_verifies(self, tmp_path):
        from claude_swap.maximize import doctor as dr

        assert "engine re-verifies" in dr.update_pause_text(True)
        assert "engine" not in dr.update_pause_text(False)


class TestNotifications:
    def _notifier(self, rig):
        return notify.EngineNotifier(rig.engine)

    def test_a_pass_is_notified(self, rig):
        notes = self._notifier(rig)._from_event(
            PrimeEvent("", "auto-verified", None, "claude 2.1.4: re-verified")
        )
        assert [(n.event, n.title) for n in notes] == [("prime-verified", "cc-swap: priming resumed")]
        assert notify.TOGGLES["prime-verified"] == "prime_paused"

    def test_a_failure_is_keyed_as_the_pause_it_leaves(self, rig):
        root = _root(rig)
        pv.record_failed(root, "2.1.4", ["invalid token is rejected"])
        [note] = self._notifier(rig)._from_event(
            PrimeEvent("", "auto-verify-failed", None, "claude 2.1.4: failed")
        )
        assert note.event == "prime-paused" and "failed" in note.title
        # The next tick's daily reminder is the same key: one notification.
        assert note.key == notify.prime_paused_note(pv.paused_note(root)).key

    def test_retries_and_account_events_are_not_notified(self, rig):
        n = self._notifier(rig)
        assert n._from_event(PrimeEvent("", "auto-verify-retry", None, "x")) == []
        assert n._from_event(PrimeEvent("2", "primed", None, "")) == []


class TestEngine:
    """Through a real maximize tick: the verify runs where priming runs, and
    the tick's paused-priming reminder gives way to the verify's own note."""

    def test_the_tick_verifies_and_notifies_once(self, temp_home, tmp_path, monkeypatch):
        from claude_swap.maximize.engine_hook import runtime_for
        from claude_swap.maximize.primer import Primer
        from claude_swap.settings import PrimeSettings
        from tests.maximize.test_engine_maximize import make, win
        from tests.maximize.test_notify import Fake

        sent = Fake()
        monkeypatch.setattr(notify, "system_backend", lambda *a, **k: sent.backend)
        h = make(temp_home)
        root = h.switcher.backup_dir
        claude = _claude(tmp_path)
        pv.record_verified(root, "2.1.3", by=pv.VERIFIED_BY_CLI, now=1.0)
        monkeypatch.setattr(pv, "read_claude_version", lambda _p: "2.1.4")
        system = FakeSystem(tmp_path / "sys")
        rt = runtime_for(h.engine)
        rt.prime_settings = PrimeSettings(enabled=True, claude_path=claude)
        rt.primer = Primer(h.engine, rt.prime_settings, clock=h.engine.clock,
                           verify_deps=system.deps)
        h.tick_with_usage({"1": win(10, 10), "2": win(0, 10), "3": win(0, 10)})
        assert len(system.calls) == 1
        assert pv.verified_version(root) == "2.1.4"
        assert [title for title, _ in sent.sent] == ["cc-swap: priming resumed"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
def test_manual_verify_refuses_while_another_verify_runs(temp_home, tmp_path, monkeypatch, capsys):
    from claude_swap import cli
    from claude_swap.maximize import primer
    from claude_swap.switcher import ClaudeAccountSwitcher

    system = FakeSystem(tmp_path)
    monkeypatch.setattr(pv, "default_deps", system.deps)
    monkeypatch.setattr(primer, "resolve_claude_path", lambda _c, **k: "/opt/claude")
    monkeypatch.setattr(pv, "VERIFY_LOCK_WAIT_S", 0.0)
    lock = pv.verify_lock(ClaudeAccountSwitcher().backup_dir)
    assert lock.acquire()
    try:
        monkeypatch.setattr(sys, "argv", ["cc-swap", "prime", "verify"])
        with pytest.raises(SystemExit) as exit_:
            cli.main()
        assert exit_.value.code == 1
        assert "another prime verify is running" in capsys.readouterr().err
        assert system.calls == []
    finally:
        lock.release()


# -- review fixes ----------------------------------------------------------------------------


class Unreadable:
    """``keychain_attrs`` for a locked keychain: reads of ``services`` (all
    when None) fail from the ``after``-th call on."""

    def __init__(self, system: FakeSystem, *, after: int = 0, services=None):
        self.system, self.after, self.services, self.calls = system, after, services, 0

    def __call__(self, service: str):
        self.calls += 1
        if self.calls > self.after and (self.services is None or service in self.services):
            raise pv.KeychainUnreadable("security exited 36")
        return self.system.items.get(service)


def _deps_with(system: FakeSystem, attrs):
    def deps():
        d = system.deps()
        d.keychain_attrs = attrs
        return d

    return deps


class TestUnreadableKeychain:
    @pytest.mark.parametrize("after", [0, 1], ids=["before-and-after", "after-only"])
    def test_an_unreadable_active_item_never_passes(self, tmp_path, after):
        system = FakeSystem(tmp_path)
        root = tmp_path / "root"
        root.mkdir()
        attrs = Unreadable(system, after=after, services={"Claude Code-credentials"})
        report = pv.run_verify(root, "/opt/claude", deps=_deps_with(system, attrs)(), record=False)
        [check] = [c for c in report.checks if c.name == "active login unchanged"]
        assert not check.ok and check.transient and "could not read" in check.detail
        assert not report.ok and report.transient

    def test_an_unreadable_probe_item_never_passes(self, tmp_path):
        from claude_swap.session import keychain_service_name

        system = FakeSystem(tmp_path)
        root = tmp_path / "root"
        root.mkdir()
        probe: list[str] = []

        def attrs(service):
            if probe and service == probe[0]:
                raise pv.KeychainUnreadable("security timed out after 5s")
            return system.items.get(service)

        system.during_run = lambda env, cwd: probe.append(keychain_service_name(cwd))
        report = pv.run_verify(root, "/opt/claude", deps=_deps_with(system, attrs)(), record=False)
        [check] = [c for c in report.checks if c.name == "no Keychain item left behind"]
        assert not check.ok and check.transient
        assert report.transient
        assert probe[0] in system.deleted  # still cleaned up

    def test_manual_verify_fails_and_records_it(self, tmp_path):
        system = FakeSystem(tmp_path)
        root = tmp_path / "root"
        root.mkdir()
        report = pv.run_verify(
            root, "/opt/claude", deps=_deps_with(system, Unreadable(system))(), now=1.0,
        )
        assert not report.ok and not report.recorded
        assert pv.failed_verify(root) is not None

    def test_the_engine_retries_it_later(self, rig, system, monkeypatch):
        _changed(rig, monkeypatch)
        primer = rig.primer(runner=StubRunner(rig))
        primer._verify_deps = _deps_with(system, Unreadable(system))
        assert _outcomes(primer.run_due(rig.snap())) == ["auto-verify-retry"]
        assert pv.verified_version(_root(rig)) == "2.1.3"  # never a pass
        assert pv.failed_verify(_root(rig)) is None

    @pytest.mark.parametrize(("rc", "expected"), [(0, "attrs"), (44, None)])
    def test_security_exit_codes(self, monkeypatch, rc, expected):
        import subprocess

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], rc, stdout="attrs", stderr=""),
        )
        assert pv.keychain_attributes("svc") == expected

    @pytest.mark.parametrize("rc", [36, 51, 1])
    def test_other_security_exits_are_unreadable(self, monkeypatch, rc):
        import subprocess

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], rc, stdout="", stderr="locked"),
        )
        with pytest.raises(pv.KeychainUnreadable, match=str(rc)):
            pv.keychain_attributes("svc")

    def test_a_security_timeout_is_unreadable(self, monkeypatch):
        import subprocess

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(a[0], k.get("timeout"))

        monkeypatch.setattr(subprocess, "run", slow)
        with pytest.raises(pv.KeychainUnreadable):
            pv.keychain_attributes("svc")


class TestFailedHistory:
    def test_a_rollback_to_a_failed_version_waits_for_a_manual_verify(
        self, rig, system, monkeypatch
    ):
        root = _root(rig)
        _changed(rig, monkeypatch)
        pv.record_failed(root, "2.1.4", ["no Keychain item left behind"], now=1.0)
        pv.record_verified(root, "2.1.5", by=pv.VERIFIED_BY_CLI)  # a later build passed
        assert pv.failed_verify(root) is None
        assert pv.failed_versions(root) == ["2.1.4"]
        # Rolled back to 2.1.4: the gate closes, but the engine does not verify it.
        primer = _primer(rig, system)
        events = primer.run_due(rig.snap())
        assert [type(e) for e in events] == [ConfigWarningEvent]
        assert system.calls == []
        assert pv.paused_note(root, auto_verify=True) == (
            "paused: claude 2.1.5 -> 2.1.4 (cc-swap prime verify)"
        )
        # A manual verify of 2.1.4 clears it from the history.
        pv.record_verified(root, "2.1.4", by=pv.VERIFIED_BY_CLI)
        assert pv.failed_versions(root) == []

    def test_the_history_is_bounded(self, tmp_path):
        for i in range(pv.FAILED_HISTORY_MAX + 5):
            pv.record_failed(tmp_path, f"2.1.{i}", ["x"], now=1.0)
        history = pv.failed_versions(tmp_path)
        assert len(history) == pv.FAILED_HISTORY_MAX
        assert history[-1] == f"2.1.{pv.FAILED_HISTORY_MAX + 4}"


class TestBackoffWithoutAWritableRecord:
    def test_a_verify_whose_outcome_cannot_be_written_still_backs_off(
        self, rig, system, monkeypatch
    ):
        _changed(rig, monkeypatch)
        system.result = TIMEOUT
        primer = _primer(rig, system)

        def readonly(*_a, **_k):
            raise OSError("read-only file system")

        monkeypatch.setattr(pv, "note_auto_retry", readonly)
        monkeypatch.setattr(pv, "record_failed", readonly)
        monkeypatch.setattr(pv, "record_verified", readonly)
        primer.run_due(rig.snap())
        assert len(system.calls) == 1
        rig.clock.advance(60)
        primer.run_due(rig.snap())
        assert len(system.calls) == 1  # not every tick
        rig.clock.advance(pv.AUTO_RETRY_S)
        primer.run_due(rig.snap())
        assert len(system.calls) == 2


def test_the_lock_wait_covers_a_whole_verify():
    worst = (
        2 * pv.VERSION_TIMEOUT_S + pv.PROBE_TIMEOUT_S + 5.0
        + 8 * pv.KEYCHAIN_TIMEOUT_S
    )
    assert pv.VERIFY_LOCK_WAIT_S > worst


def test_note_seen_keys_the_version_by_the_identity_read_before_it(tmp_path):
    from pathlib import Path

    claude = _claude(tmp_path)
    old_key = pv.identity(claude)
    Path(claude).write_text("#!/bin/sh\n# replaced by a self-update\n")  # after the read
    pv.note_seen(tmp_path, claude, "2.1.3", key=old_key)
    reader_calls = []
    assert pv.current_version(
        tmp_path, claude, reader=lambda _p: reader_calls.append(1) or "2.1.4"
    ) == "2.1.4"
    assert reader_calls == [1]  # the new binary missed the cache


def test_run_verify_reads_the_identity_before_the_version(tmp_path):
    from pathlib import Path

    system = FakeSystem(tmp_path)
    claude = _claude(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    deps = system.deps()

    def version_then_update(_p):
        Path(claude).write_text("#!/bin/sh\n# 2.1.5, a longer file\n")  # updated right after
        return "2.1.4"

    deps.version = version_then_update
    pv.run_verify(root, claude, deps=deps, record=False)
    assert pv.current_version(root, claude, reader=lambda _p: "2.1.5") == "2.1.5"
