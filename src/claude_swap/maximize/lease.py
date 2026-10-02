"""Engine lease: at most one auto-switch engine per machine.

Two engines on one machine — the cc-swap service, a terminal ``cc-swap
auto``, the TUI auto screen, the menu bar's auto-switch — would each rank the
same accounts and fight over the one active login, possibly with different
strategies. The lease rules that out: an engine runs only while its process
holds an exclusive, non-blocking OS lock on ``<backup_root>/.engine.lock``.

The lock is ``flock`` on POSIX and ``msvcrt.locking`` on Windows — the
primitives :class:`claude_swap.locking.FileLock` uses — taken on a descriptor
that lives exactly as long as the lease. The kernel drops the lock when that
descriptor closes, so a crashed or SIGKILLed engine never leaves a stale
lease behind: there is no pid file to clean up. ``FileLock`` itself is not
reused: it opens the file with ``"w"`` (every probe would truncate the
holder's pid) and it polls until a timeout instead of failing at once.

``os.open`` descriptors are non-inheritable (PEP 446), so a ``claude`` child
spawned for priming can never keep the lease alive after its engine dies.

The holder writes its pid into the file for messages. POSIX only: on Windows
the locked byte is unreadable to other processes, so
:meth:`EngineLease.holder_pid` returns None there; the lease itself works.
"""

from __future__ import annotations

import errno
import logging
import os
import sys
import threading
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from claude_swap.exceptions import ClaudeSwitchError

LEASE_FILENAME = ".engine.lock"

#: ``cc-swap auto`` exit code when another engine holds the lease. Distinct
#: from the ``--once`` outcomes (0-3) and from Ctrl-C (130), so a service
#: manager or a script can tell "someone else is switching" from a failure.
EXIT_ENGINE_BUSY = 4

_logger = logging.getLogger("claude-swap")


class EngineBusyError(ClaudeSwitchError):
    """Another process holds the engine lease."""


class EngineLease:
    """Exclusive, non-blocking, process-lifetime lock on ``.engine.lock``."""

    def __init__(self, backup_root: Path) -> None:
        self.path = Path(backup_root) / LEASE_FILENAME
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """Whether this object currently holds the lease."""
        return self._fd is not None

    def acquire(self) -> bool:
        """Take the lease without waiting.

        True when this object holds it afterwards (already holding counts),
        False when another holder has it. Failing to create or open the lock
        file, or a lock call that fails for any reason other than another
        holder (``ENOLCK`` on a filesystem without lock support, say), raises
        ``OSError``; the caller decides whether that is fatal
        (:func:`claim_for_auto`) or degradable (:func:`should_run_engine`).
        """
        if self._fd is not None:
            return True
        fd = self._open()
        try:
            locked = _try_lock(fd)
        except BaseException:
            os.close(fd)
            raise
        if not locked:
            os.close(fd)
            return False
        self._fd = fd
        _record_pid(fd)
        return True

    def release(self) -> None:
        """Drop the lease; a no-op when not held."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        _unlock(fd)
        os.close(fd)

    def held_elsewhere(self) -> bool:
        """Whether another holder has the lease right now.

        A momentary probe: a free lease is taken and dropped at once, so it
        never steals. False while this object is the holder. Raises
        ``OSError`` exactly where :meth:`acquire` does.
        """
        if self._fd is not None:
            return False
        fd = self._open()
        try:
            if _try_lock(fd):
                _unlock(fd)
                return False
            return True
        finally:
            os.close(fd)

    def holder_pid(self) -> int | None:
        """The pid the current holder recorded (POSIX), for messages only.

        Meaningful only while someone holds the lease — a released lease
        leaves its last pid in the file. Always None on Windows.
        """
        try:
            raw = self.path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return int(raw) if raw.isdigit() else None

    def _open(self) -> int:
        # O_RDWR|O_CREAT, never O_TRUNC: a probe must not wipe the holder's pid.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)


def _is_held_elsewhere(error: OSError) -> bool:
    """Whether a failed non-blocking lock call means "another holder has it".

    Only that: ``BlockingIOError`` (``EWOULDBLOCK``/``EAGAIN``) from ``flock``,
    and on Windows the lock violation ``msvcrt.locking`` reports for a byte
    another process has locked (``PermissionError``/``EACCES``, or
    ``EDEADLK``). Anything else (``ENOLCK``, ``EBADF``, ``EINVAL``...) is a
    lock that cannot be taken at all and must not read as a free-or-busy
    answer, or a broken lock would look like a permanently busy engine.
    """
    if sys.platform == "win32":
        return isinstance(error, PermissionError) or error.errno in (
            errno.EACCES,
            errno.EDEADLK,
        )
    return isinstance(error, BlockingIOError)


def _try_lock(fd: int) -> bool:
    """True when the lock is taken, False when another holder has it; any
    other failure raises ``OSError``."""
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if _is_held_elsewhere(e):
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the descriptor releases it regardless


def _record_pid(fd: int) -> None:
    if sys.platform == "win32":
        return  # other processes cannot read the locked byte anyway
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        pass  # the pid is diagnostics; the lock is what matters


def busy_message(pid: int | None) -> str:
    """Why ``cc-swap auto`` refused, and what to do about it."""
    who = f" (pid {pid})" if pid else ""
    return (
        f"another cc-swap auto-switch engine is already running{who}. "
        "Only one engine may run at a time: stop it first (cc-swap service "
        "status, a terminal running cc-swap auto, or a TUI auto screen), or "
        "inspect without an engine: cc-swap auto --once --dry-run"
    )


def should_run_engine(lease: EngineLease) -> bool:
    """Whether a UI host (TUI auto screen, menu bar) may run its own engine.

    True means the caller now holds ``lease`` (or already did) and must
    release it once its engine has stopped. False means another process owns
    auto-switching: show its results read-only (store-only snapshots, the
    state file) and never start an engine. A lock file that cannot be created
    or locked (``OSError`` other than another holder) degrades to True —
    upstream behaviour without a lease — rather than leaving the host unable
    to auto-switch at all.
    """
    try:
        return lease.acquire()
    except OSError as e:
        _logger.warning("engine lease unavailable (%s); running without it", e)
        return True


class LeaseKeeper:
    """Holds the engine lease for a UI that runs engines on worker threads.

    The lease must outlive every engine thread still inside a tick, not just
    the engine object the UI points at: ``engine.stop()`` only asks the loop
    to end, and a tick in flight can still switch accounts. So a host claims
    once, counts the engine threads it starts, and ``close()`` hands the
    release to whichever thread finishes last. A later ``claim()`` (reopened
    screen, toggled switch, restarted engine) keeps a still-held lease.
    """

    def __init__(self, lease: EngineLease) -> None:
        self.lease = lease
        self._lock = threading.Lock()
        self._running = 0
        self._closing = False

    def claim(self) -> bool:
        with self._lock:
            self._closing = False
            return should_run_engine(self.lease)

    def engine_started(self) -> None:
        with self._lock:
            self._running += 1

    def engine_exited(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)
            if self._closing and self._running == 0:
                self.lease.release()

    def close(self) -> None:
        with self._lock:
            self._closing = True
            if self._running == 0:
                self.lease.release()


def claim_for_auto(
    backup_root: Path, *, once: bool, dry_run: bool
) -> EngineLease | None:
    """The lease for one ``cc-swap auto`` run, held until the process exits.

    ``--once --dry-run`` is a read-only probe (one evaluation, no switch, no
    state write) and needs none, so it keeps working while the service runs.
    Every other run is an engine. Raises :class:`EngineBusyError` when
    another engine holds the lease, ``ClaudeSwitchError`` when the lock file
    cannot be created or locked.
    """
    if once and dry_run:
        return None
    lease = EngineLease(backup_root)
    try:
        acquired = lease.acquire()
    except OSError as e:
        raise ClaudeSwitchError(
            f"cannot take the engine lease {lease.path}: {e}"
        ) from e
    if not acquired:
        raise EngineBusyError(busy_message(lease.holder_pid()))
    return lease
