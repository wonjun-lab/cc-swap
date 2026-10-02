"""Size-based rotation for the launchd service's own log files.

launchd opens ``StandardOutPath`` / ``StandardErrorPath`` with ``O_APPEND`` and
never rotates them, so ``auto.log`` and ``auto.err.log`` grow for as long as the
agent is installed. The ``cc-swap auto`` loop rotates them itself, but only when
it knows it is the service (``CC_SWAP_SERVICE=1``, set by the generated plist
and unit) and only the files under the service's own log directory.

Rotation copies the file to ``<name>.1`` (shifting ``.1`` to ``.2`` and ``.2``
to ``.3``, dropping the oldest) and truncates the original *in place*. A rename
would leave launchd writing to the renamed file; truncating keeps its
descriptor valid, and ``O_APPEND`` makes its next write land at the new end.
Lines written between the copy and the truncate are lost, which a log can bear.

On Linux the unit writes to the journal, which rotates itself: nothing to do.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

#: A log larger than this is rotated.
MAX_BYTES = 10 * 1024 * 1024
#: Rotated copies kept: ``<name>.1`` .. ``<name>.3``.
GENERATIONS = 3
#: Sizes are looked at on start and then at most this often.
CHECK_INTERVAL_S = 3600.0
#: Set to ``1`` by the generated plist / unit.
SERVICE_ENV = "CC_SWAP_SERVICE"


def should_rotate(size: int, max_bytes: int = MAX_BYTES) -> bool:
    return size > max_bytes


def check_due(last_check: float | None, now: float) -> bool:
    """Whether the sizes should be looked at again.

    A clock that went backwards counts as due rather than waiting out the
    difference.
    """
    return last_check is None or now < last_check or now - last_check >= CHECK_INTERVAL_S


def rotate_names(path: Path, generations: int = GENERATIONS) -> list[tuple[Path, Path]]:
    """``(source, destination)`` moves, oldest first, so none overwrites a
    file that has not moved yet: ``.2 -> .3``, ``.1 -> .2``, ``base -> .1``."""
    sources = [path.with_name(f"{path.name}.{n}") for n in range(generations - 1, 0, -1)]
    sources.append(path)
    return [
        (src, path.with_name(f"{path.name}.{generations - i}"))
        for i, src in enumerate(sources)
    ]


def rotate_file(path: Path) -> None:
    """Rotate ``path`` now: shift the old generations, copy, truncate in place."""
    if not path.is_file() or path.is_symlink():
        return
    moves = rotate_names(path)
    for src, dst in moves[:-1]:
        if src.is_file():
            os.replace(src, dst)
    base, first = moves[-1]
    shutil.copyfile(base, first)
    os.truncate(base, 0)


def service_log_files(
    *, home: Path | None = None, env: Mapping[str, str] | None = None
) -> list[Path]:
    """The log files this process may rotate: none unless it runs as the
    macOS service, and only regular files directly inside the service log
    directory (a symlinked log pointing elsewhere is not ours to truncate)."""
    from claude_swap.maximize import service

    if (os.environ if env is None else env).get(SERVICE_ENV) != "1":
        return []
    if sys.platform != "darwin":
        return []
    logs = service.log_paths(home)
    log_dir = logs[0].parent
    return [p for p in logs if p.parent == log_dir and not p.is_symlink()]


class LogRotator:
    """Rotates the service logs at startup and at most once an hour after."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._home = home
        self._env = env
        self._clock = clock
        self._last: float | None = None

    @property
    def active(self) -> bool:
        return bool(service_log_files(home=self._home, env=self._env))

    def maybe_rotate(self) -> None:
        """Rotate any oversized log if a check is due. Never raises: a full
        disk or a permissions problem must not stop the engine."""
        now = self._clock()
        if not check_due(self._last, now):
            return
        self._last = now
        try:
            for path in service_log_files(home=self._home, env=self._env):
                try:
                    if path.is_file() and should_rotate(path.stat().st_size):
                        rotate_file(path)
                except OSError:
                    continue
        except OSError:
            return
