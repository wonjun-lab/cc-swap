"""`cc-swap auto` under the engine lease: exit 4 while another engine runs."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.maximize.lease import EXIT_ENGINE_BUSY, EngineLease
from claude_swap.paths import get_backup_root


class FakeEngine:
    instances: list["FakeEngine"] = []
    held_during_loop: bool | None = None

    def __init__(self, switcher, settings, on_event, *, dry_run=False,
                 state_path=None, clock=None):
        self.switcher = switcher
        self.settings = settings
        self.dry_run = dry_run
        type(self).instances.append(self)

    def tick(self):
        from claude_swap.autoswitch import TickOutcome

        return TickOutcome.NO_ACTION

    def run_loop(self):
        type(self).held_during_loop = EngineLease(
            self.switcher.backup_dir
        ).held_elsewhere()
        return 0

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_fake():
    FakeEngine.instances = []
    FakeEngine.held_during_loop = None


def _run(argv: list[str]) -> int:
    with patch("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["cc-swap", "auto", *argv]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
    return excinfo.value.code


def _hold_lease() -> EngineLease:
    root = get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    lease = EngineLease(root)
    assert lease.acquire()
    return lease


def test_loop_refuses_with_exit_4_when_another_engine_runs(temp_home, capsys):
    other = _hold_lease()
    try:
        code = _run([])
    finally:
        other.release()
    assert code == EXIT_ENGINE_BUSY == 4
    assert FakeEngine.instances == []
    err = capsys.readouterr().err
    assert "Error: another cc-swap auto-switch engine is already running" in err


def test_once_live_also_refuses(temp_home):
    other = _hold_lease()
    try:
        assert _run(["--once"]) == EXIT_ENGINE_BUSY
    finally:
        other.release()
    assert FakeEngine.instances == []


def test_loop_dry_run_is_still_an_engine_and_refuses(temp_home):
    other = _hold_lease()
    try:
        assert _run(["--dry-run"]) == EXIT_ENGINE_BUSY
    finally:
        other.release()


def test_once_dry_run_probe_runs_beside_another_engine(temp_home):
    other = _hold_lease()
    try:
        code = _run(["--once", "--dry-run"])
    finally:
        other.release()
    assert code == 2  # NO_ACTION from the fake tick
    assert FakeEngine.instances and FakeEngine.instances[0].dry_run is True


def test_busy_json_is_an_error_envelope(temp_home, capsys):
    other = _hold_lease()
    try:
        code = _run(["--json"])
    finally:
        other.release()
    assert code == EXIT_ENGINE_BUSY
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["schemaVersion"] == 1
    assert payload["error"]["type"] == "EngineBusyError"


def test_loop_holds_the_lease_while_running_and_releases_it_on_exit(temp_home):
    assert _run([]) == 0
    assert FakeEngine.held_during_loop is True
    probe = EngineLease(get_backup_root())
    assert probe.acquire()
    probe.release()


@pytest.mark.skipif(sys.platform == "win32", reason="pid is recorded on POSIX only")
def test_busy_message_names_the_holder_pid(temp_home, capsys):
    other = _hold_lease()
    try:
        _run([])
    finally:
        other.release()
    assert f"(pid {os.getpid()})" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="patches fcntl.flock")
def test_a_lock_that_cannot_be_taken_exits_1_not_4(temp_home, capsys, monkeypatch):
    import errno

    from claude_swap.maximize import lease as lease_mod

    def flock(fd, operation):
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(lease_mod.fcntl, "flock", flock)
    assert _run([]) == 1
    assert FakeEngine.instances == []
    err = capsys.readouterr().err
    assert "cannot take the engine lease" in err
    assert "already running" not in err
