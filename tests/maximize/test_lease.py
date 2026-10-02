"""Engine lease: exclusivity within and across processes, release on holder
death, and the decision helpers the UI hosts and `cc-swap auto` build on it."""

from __future__ import annotations

import errno
import logging
import os
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

import claude_swap
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.maximize import lease as lease_mod
from claude_swap.maximize.lease import (
    EXIT_ENGINE_BUSY,
    EngineBusyError,
    EngineLease,
    LeaseKeeper,
    busy_message,
    claim_for_auto,
    should_run_engine,
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the holder pid is recorded on POSIX only"
)

_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from claude_swap.maximize.lease import EngineLease

    lease = EngineLease(Path(sys.argv[1]))
    print("acquired" if lease.acquire() else "busy", flush=True)
    sys.stdin.readline()  # hold until the parent closes stdin or kills us
    """
)


def _spawn_holder(root: Path, home: Path) -> subprocess.Popen:
    """A separate interpreter that takes the lease and holds it."""
    home.mkdir(parents=True, exist_ok=True)
    src = str(Path(claude_swap.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = env["USERPROFILE"] = str(home)
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)
    proc.stdin.close()
    proc.stdout.close()


def _eventually(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _refuse_open(self):
    raise PermissionError(13, "Permission denied", str(self.path))


# -- one process --------------------------------------------------------------


def test_a_second_lease_on_the_same_root_is_refused(tmp_path):
    first, second = EngineLease(tmp_path), EngineLease(tmp_path)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
        assert second.held is False
    finally:
        first.release()


def test_acquire_is_idempotent_for_the_holder_and_release_is_safe_twice(tmp_path):
    lease = EngineLease(tmp_path)
    assert lease.acquire() and lease.acquire()
    assert lease.held
    lease.release()
    assert not lease.held
    lease.release()


def test_release_lets_another_instance_take_it(tmp_path):
    first, second = EngineLease(tmp_path), EngineLease(tmp_path)
    assert first.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_held_elsewhere_probes_without_stealing(tmp_path):
    holder, probe = EngineLease(tmp_path), EngineLease(tmp_path)
    assert probe.held_elsewhere() is False
    assert probe.acquire()  # the probe above dropped what it briefly took
    probe.release()
    assert holder.acquire()
    try:
        assert holder.held_elsewhere() is False  # ours, not "elsewhere"
        assert probe.held_elsewhere() is True
        assert probe.acquire() is False  # probing did not steal it
    finally:
        holder.release()


def test_the_lease_file_lives_in_the_backup_root(tmp_path):
    root = tmp_path / "root"  # created on demand
    lease = EngineLease(root)
    assert lease.path == root / ".engine.lock"
    assert lease.acquire()
    lease.release()
    assert lease.path.exists()


@posix_only
def test_holder_pid_survives_a_refused_acquire_and_a_probe(tmp_path):
    holder, other = EngineLease(tmp_path), EngineLease(tmp_path)
    assert holder.acquire()
    try:
        assert other.acquire() is False
        assert other.held_elsewhere() is True
        assert other.holder_pid() == os.getpid()
    finally:
        holder.release()


# -- contention vs a lock that cannot be taken at all ----------------------------------


def _flock_raises(monkeypatch, code: int) -> None:
    def flock(fd, operation):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(lease_mod.fcntl, "flock", flock)


@posix_only
@pytest.mark.parametrize("code", [errno.EWOULDBLOCK, errno.EAGAIN])
def test_a_would_block_lock_error_means_held_elsewhere(tmp_path, monkeypatch, code):
    _flock_raises(monkeypatch, code)
    lease = EngineLease(tmp_path)
    assert lease.held_elsewhere() is True
    assert lease.acquire() is False
    assert not lease.held


@posix_only
def test_a_lock_error_that_is_not_contention_propagates_and_leaks_no_descriptor(
    tmp_path, monkeypatch
):
    _flock_raises(monkeypatch, errno.ENOLCK)
    opened: list[int] = []
    real_open = EngineLease._open

    def spying_open(self):
        fd = real_open(self)
        opened.append(fd)
        return fd

    monkeypatch.setattr(EngineLease, "_open", spying_open)
    lease = EngineLease(tmp_path)
    with pytest.raises(OSError) as excinfo:
        lease.acquire()
    assert excinfo.value.errno == errno.ENOLCK
    with pytest.raises(OSError) as excinfo:
        lease.held_elsewhere()
    assert excinfo.value.errno == errno.ENOLCK
    assert not lease.held
    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError):  # EBADF: both attempts closed theirs
            os.fstat(fd)


@posix_only
def test_should_run_engine_degrades_to_running_when_locking_is_unavailable(
    tmp_path, monkeypatch, caplog
):
    _flock_raises(monkeypatch, errno.ENOLCK)
    lease = EngineLease(tmp_path)
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        assert should_run_engine(lease) is True
    assert not lease.held
    assert "engine lease unavailable" in caplog.text


@posix_only
def test_claim_for_auto_reports_a_lock_failure_as_an_error_not_as_busy(tmp_path, monkeypatch):
    _flock_raises(monkeypatch, errno.ENOLCK)
    with pytest.raises(ClaudeSwitchError, match="cannot take the engine lease") as excinfo:
        claim_for_auto(tmp_path, once=False, dry_run=False)
    assert not isinstance(excinfo.value, EngineBusyError)
    assert os.strerror(errno.ENOLCK) in str(excinfo.value)


def _fake_msvcrt(monkeypatch, error: OSError | None):
    def locking(fd, mode, nbytes):
        if error is not None and mode == 2:  # LK_NBLCK
            raise error

    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    monkeypatch.setattr(lease_mod, "msvcrt", fake, raising=False)
    monkeypatch.setattr(lease_mod.sys, "platform", "win32")


@pytest.mark.parametrize("error", [
    PermissionError(errno.EACCES, "Permission denied"),
    OSError(errno.EACCES, "Permission denied"),
    OSError(errno.EDEADLK, "Resource deadlock avoided"),
])
def test_windows_lock_violations_mean_held_elsewhere(tmp_path, monkeypatch, error):
    fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT)
    try:
        _fake_msvcrt(monkeypatch, error)
        assert lease_mod._try_lock(fd) is False
    finally:
        os.close(fd)


def test_other_windows_lock_errors_propagate(tmp_path, monkeypatch):
    fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT)
    try:
        _fake_msvcrt(monkeypatch, OSError(errno.EINVAL, "Invalid argument"))
        with pytest.raises(OSError) as excinfo:
            lease_mod._try_lock(fd)
        assert excinfo.value.errno == errno.EINVAL
        _fake_msvcrt(monkeypatch, None)
        assert lease_mod._try_lock(fd) is True
    finally:
        os.close(fd)


# -- two processes --------------------------------------------------------------


def test_lease_is_exclusive_across_processes_and_dies_with_its_holder(tmp_path):
    root = tmp_path / "root"
    proc = _spawn_holder(root, tmp_path / "home")
    try:
        assert proc.stdout.readline().strip() == "acquired"
        mine = EngineLease(root)
        assert mine.acquire() is False
        assert mine.held_elsewhere() is True
        if sys.platform != "win32":
            assert mine.holder_pid() == proc.pid
        proc.kill()  # SIGKILL / TerminateProcess: no cleanup code runs
        proc.wait(timeout=10)
        assert _eventually(mine.acquire), "a dead holder's lease was not released"
        mine.release()
    finally:
        _reap(proc)


def test_a_child_process_is_refused_while_we_hold_the_lease(tmp_path):
    root = tmp_path / "root"
    mine = EngineLease(root)
    assert mine.acquire()
    proc = _spawn_holder(root, tmp_path / "home")
    try:
        assert proc.stdout.readline().strip() == "busy"
    finally:
        _reap(proc)
        mine.release()


# -- UI hosts: should_run_engine / LeaseKeeper --------------------------------------


def test_should_run_engine_claims_a_free_lease(tmp_path):
    lease = EngineLease(tmp_path)
    assert should_run_engine(lease) is True
    assert lease.held
    lease.release()


def test_should_run_engine_is_false_while_another_engine_holds_it(tmp_path):
    other = EngineLease(tmp_path)
    assert other.acquire()
    try:
        lease = EngineLease(tmp_path)
        assert should_run_engine(lease) is False
        assert not lease.held
    finally:
        other.release()


def test_should_run_engine_degrades_to_running_when_the_lock_file_cannot_be_made(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(EngineLease, "_open", _refuse_open)
    lease = EngineLease(tmp_path)
    assert should_run_engine(lease) is True
    assert not lease.held


def test_keeper_close_without_engines_releases_at_once(tmp_path):
    keeper, probe = LeaseKeeper(EngineLease(tmp_path)), EngineLease(tmp_path)
    assert keeper.claim()
    assert probe.held_elsewhere()
    keeper.close()
    assert not probe.held_elsewhere()


def test_keeper_holds_until_the_last_engine_thread_exits(tmp_path):
    keeper, probe = LeaseKeeper(EngineLease(tmp_path)), EngineLease(tmp_path)
    assert keeper.claim()
    keeper.engine_started()
    keeper.close()  # the UI is gone, but a tick may still be in flight
    assert probe.held_elsewhere()
    keeper.engine_exited()
    assert not probe.held_elsewhere()


def test_keeper_restart_keeps_the_lease(tmp_path):
    """The TUI's dry↔live toggle and the menu bar's _restart_engine: stop the
    old engine, start a new one, and the old thread exits last."""
    keeper, probe = LeaseKeeper(EngineLease(tmp_path)), EngineLease(tmp_path)
    assert keeper.claim()
    keeper.engine_started()  # engine A
    keeper.close()  # menu bar _stop_engine
    assert keeper.claim()
    keeper.engine_started()  # engine B
    keeper.engine_exited()  # A's thread finally leaves its tick
    assert probe.held_elsewhere()  # B still owns the lease
    keeper.close()
    keeper.engine_exited()
    assert not probe.held_elsewhere()


def test_keeper_claim_is_false_while_another_engine_holds_it(tmp_path):
    other = EngineLease(tmp_path)
    assert other.acquire()
    try:
        keeper = LeaseKeeper(EngineLease(tmp_path))
        assert keeper.claim() is False
        keeper.close()  # harmless: it never held anything
        assert other.held
    finally:
        other.release()


# -- cc-swap auto: claim_for_auto -----------------------------------------------------


def test_claim_for_auto_skips_the_lease_for_a_once_dry_run_probe(tmp_path):
    assert claim_for_auto(tmp_path / "root", once=True, dry_run=True) is None
    assert not (tmp_path / "root" / ".engine.lock").exists()


@pytest.mark.parametrize("once,dry_run", [(False, False), (True, False), (False, True)])
def test_claim_for_auto_takes_the_lease_for_every_engine_run(tmp_path, once, dry_run):
    lease = claim_for_auto(tmp_path, once=once, dry_run=dry_run)
    try:
        assert lease is not None and lease.held
    finally:
        lease.release()


def test_claim_for_auto_raises_busy_naming_the_holder(tmp_path):
    other = EngineLease(tmp_path)
    assert other.acquire()
    try:
        with pytest.raises(EngineBusyError) as excinfo:
            claim_for_auto(tmp_path, once=False, dry_run=False)
    finally:
        other.release()
    message = str(excinfo.value)
    assert message.startswith("another cc-swap auto-switch engine is already running")
    if sys.platform != "win32":
        assert f"(pid {os.getpid()})" in message


def test_claim_for_auto_turns_an_unwritable_root_into_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setattr(EngineLease, "_open", _refuse_open)
    with pytest.raises(ClaudeSwitchError, match="cannot take the engine lease") as excinfo:
        claim_for_auto(tmp_path, once=False, dry_run=False)
    assert not isinstance(excinfo.value, EngineBusyError)


def test_busy_message_without_a_pid_names_no_pid():
    assert "pid" not in busy_message(None)
    assert "cc-swap auto --once --dry-run" in busy_message(None)


def test_busy_message_points_at_uninstalling_the_service_not_at_status():
    message = busy_message(123)
    assert "cc-swap service uninstall" in message
    assert "service status" not in message
    assert "(pid 123)" in message


def test_busy_exit_code_is_distinct_from_every_once_outcome_and_ctrl_c():
    from claude_swap.autoswitch import TickOutcome

    assert EXIT_ENGINE_BUSY == 4
    assert EXIT_ENGINE_BUSY not in {o.value for o in TickOutcome} | {130}
