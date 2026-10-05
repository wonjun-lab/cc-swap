"""Every ``claude`` cc-swap runs goes through here (cc-swap fork).

Why: on 2026-10-04 macOS started killing the user's ``claude`` at launch
(SIGKILL, exit 137, even ``--version``) right after Claude Code updated
itself; a copy of the same bytes under a new inode ran fine. The suspected
cause is the kernel's code-signing cache going stale when a file that was
already executed is rewritten in place. cc-swap was probably not involved,
but nothing could prove it. So every launch (:class:`Launch`, :func:`run`)
now does four things:

1. **Audit.** One structured record per run — caller, ``argv[1:]`` (emails
   and token-shaped text masked, never the environment), the symlink and the
   real file, the real file's inode/size/mtime/ctime/birthtime, the link's
   own mtime, the file's age, pid, exit code, signal, duration — in the
   cc-swap log and as a JSON line in ``<backup root>/claude-exec.jsonl``
   (rotated to ``.1`` past :data:`EXEC_LOG_MAX_BYTES`).
2. **Watch.** A new identity of the resolved binary (:func:`observe`) is
   logged with ``codesign`` / ``xattr`` facts on macOS; the same real path
   changing after cc-swap executed it is a loud "rewritten in place"
   warning — exactly the suspected trigger.
3. **Settle.** A binary younger than ``claude.settleS`` (default 600 s; the
   youngest of the file's mtime/ctime and the symlink's mtime) is not run by
   the engine: it defers to a later tick (:func:`engine_hold`). A command the
   user typed (:func:`manual`) runs it anyway with a printed warning: the
   user asked for that run and would run ``claude`` by hand otherwise, a
   ten-minute countdown would only stall them, and the record keeps the
   override (``settleOverrideS``). ``cc-swap doctor`` skips its
   ``--version`` instead (it never asked to run claude).
4. **SIGKILL.** A run that ends with signal 9 (or 137 through a wrapper
   script) marks the binary "killed by the OS" in
   ``claude_exec_state.json``, writes ``claude-kill-<ts>.txt`` diagnostics
   (codesign, xattr names, ``log show`` on macOS), notifies once per binary
   identity, pauses priming (never a failed verify) and makes ``cc-swap
   doctor`` print the fix. A new identity of that path, or any successful
   run of it, clears the mark. The engine never re-runs a killed identity.

A kill of a ``claude`` cc-swap did NOT launch (the kernel's line in the
unified log, a crash report — :mod:`codesign_watch`) never pauses anything by
itself: on 2026-10-04 macOS killed 37 ``claude`` launched by another app
(``ASP: Unable to apply provenance sandbox``) while ``claude --version`` ran
fine from a shell. It is recorded as evidence (:func:`note_external_kill`,
``external`` in the state file) and the engine runs its own bounded probe
(:func:`probe_external`: ``claude --version`` through :func:`run`, a minute
after the latest kill, at most once per :data:`PROBE_EVERY_S` per identity).
A probe that runs records the success (old evidence can never mark the file
again) and doctor says cc-swap's own launches work; a probe the OS kills
takes the SIGKILL path above.

Every child also gets ``DISABLE_AUTOUPDATER=1``: priming launches several
``claude`` right after each 5h reset, and each would otherwise start Claude
Code's background updater against the shared install — racing on the very
``versions/<ver>`` file the incident was about.

Nothing here raises into a caller except :class:`ExecRefused` (an engine
launch the guard holds back) and the child's own spawn errors.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger("claude-swap")

EXEC_LOG_FILENAME = "claude-exec.jsonl"
EXEC_LOG_MAX_BYTES = 2 * 1024 * 1024
STATE_FILENAME = "claude_exec_state.json"
LOCK_FILENAME = ".claude_exec.lock"
KILL_PREFIX = "claude-kill-"
DEFAULT_SETTLE_S = 600
AUTOUPDATER_ENV = "DISABLE_AUTOUPDATER"
SIGKILL_NUM = 9  # signal.SIGKILL, which Windows does not define
#: A child killed by SIGKILL inside a wrapper shell exits 128 + 9.
SHELL_KILLED_RC = 128 + SIGKILL_NUM
#: ``codesign`` / ``xattr`` / ``log show`` are bounded by these.
TOOL_TIMEOUT_S = 10.0
LOG_SHOW_TIMEOUT_S = 30.0
LOG_SHOW_MAX_CHARS = 200_000
#: A child killed at its timeout gets this long to drain.
KILL_GRACE_S = 5.0
#: Binaries and executed files remembered in the state file.
REMEMBER_MAX = 20
#: ``cc-swap doctor`` still mentions a rewrite seen this recently.
REWRITE_RECENT_S = 7 * 86400.0

#: Kills of a ``claude`` cc-swap did not launch (:func:`note_external_kill`):
#: the engine probes ``claude --version`` this long after the latest one (not
#: during the episode), at the latest this long after the first one still
#: unprobed, and at most once per :data:`PROBE_EVERY_S` per binary identity.
PROBE_DELAY_S = 60.0
PROBE_MAX_DEFER_S = 300.0
PROBE_EVERY_S = 600.0
PROBE_TIMEOUT_S = 30.0
PROBE_CALLER = "external-kill probe"
#: ``cc-swap doctor`` shows such kills this long (a warning for the first day).
EXTERNAL_RECENT_S = 7 * 86400.0
EXTERNAL_WARN_S = 86400.0
#: Crash reports remembered per identity (each is counted once).
EXTERNAL_REPORTS_MAX = 600
#: 0.5.3 recorded kills seen outside cc-swap's runs as a killed mark with one
#: of these callers; :func:`load_state` turns such a mark into evidence.
LEGACY_EXTERNAL_CALLERS = frozenset({"log stream", "crash report"})
#: A killed mark recorded by this version: a ``claude`` cc-swap launched.
ORIGIN_CC_SWAP = "cc-swap"
#: A started ``claude`` whose end was never recorded is forgotten this late.
INFLIGHT_MAX_S = 3600.0
#: A ``running`` probe this old died with its engine: another may run.
PROBE_STALE_S = PROBE_TIMEOUT_S + KILL_GRACE_S + 60.0

CODESIGN = "/usr/bin/codesign"
XATTR = "/usr/bin/xattr"
LOG = "/usr/bin/log"

# -- the binary ---------------------------------------------------------------------


@dataclass(frozen=True)
class Binary:
    """What ``stat`` says about a ``claude`` path just before a run."""

    path: str
    real: str | None
    ino: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    birthtime_ns: int | None = None
    link_mtime_ns: int | None = None  # the symlink's own mtime, when ``path`` is one

    @property
    def identity(self) -> list[Any] | None:
        """``prime_verify.identity``'s shape: ``[real, inode, mtime_ns, size]``."""
        if self.real is None or self.ino is None:
            return None
        return [self.real, self.ino, self.mtime_ns, self.size]

    def youngest_ns(self) -> int | None:
        stamps = [t for t in (self.mtime_ns, self.ctime_ns, self.link_mtime_ns) if t is not None]
        return max(stamps) if stamps else None

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "real": self.real,
            "inode": self.ino,
            "size": self.size,
            "mtimeNs": self.mtime_ns,
            "ctimeNs": self.ctime_ns,
            "birthtimeNs": self.birthtime_ns,
            "linkMtimeNs": self.link_mtime_ns,
            "identity": self.identity,
        }


def stat_binary(path: str) -> Binary:
    """``path``'s facts; fields it cannot read stay None. Never raises."""
    path = str(path)
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
    except (OSError, ValueError):
        return Binary(path, None)
    birth = getattr(st, "st_birthtime", None)
    link_mtime = None
    try:
        lst = os.lstat(path)
        if stat.S_ISLNK(lst.st_mode):
            link_mtime = lst.st_mtime_ns
    except (OSError, ValueError):
        pass
    return Binary(
        path, real, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
        int(birth * 1e9) if isinstance(birth, (int, float)) else None, link_mtime,
    )


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


# -- settle -------------------------------------------------------------------------------


def settle_seconds(root: Path) -> float:
    """``claude.settleS``; the default when the settings cannot be read."""
    from claude_swap.settings import load_claude_settings

    try:
        return float(load_claude_settings(Path(root)).settle_s)
    except Exception:
        return float(DEFAULT_SETTLE_S)


#: The real reader (tests replace :func:`settle_seconds` and restore this).
settle_seconds_real = settle_seconds


def settle_left(binary: Binary, settle_s: float, now: float) -> float:
    """Seconds until ``binary`` is ``settle_s`` old (0: settled). A stamp in
    the future (clock skew) counts as now: each check answers ``settle_s``
    until the clock passes the stamp, then the wait runs down from there."""
    young = binary.youngest_ns()
    if young is None or settle_s <= 0:
        return 0.0
    return min(float(settle_s), max(0.0, young / 1e9 + settle_s - now))


def settle_text(left: float) -> str:
    return f"waiting for the claude update to settle ({left:.0f}s left)"


# -- engine or user -----------------------------------------------------------------------


@dataclass(frozen=True)
class Manual:
    """A run the user asked for (``cc-swap prime verify``, ``cc-swap login``
    …): the settle delay warns instead of refusing; ``warn`` shows it."""

    command: str
    warn: Callable[[str], None] | None = None


_MANUAL: contextvars.ContextVar[Manual | None] = contextvars.ContextVar(
    "cc_swap_claude_exec_manual", default=None
)


@contextlib.contextmanager
def manual(command: str, warn: Callable[[str], None] | None = None):
    """Runs inside are the user's ``command``. Outside (the engine, its
    threads): the guard holds launches back."""
    token = _MANUAL.set(Manual(command, warn))
    try:
        yield
    finally:
        _MANUAL.reset(token)


def current_manual() -> Manual | None:
    return _MANUAL.get()


class ExecRefused(subprocess.SubprocessError):
    """The guard held an engine launch back (settling, or killed by the OS)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``env`` (default: ours) plus ``DISABLE_AUTOUPDATER=1``."""
    out = dict(os.environ if env is None else env)
    out[AUTOUPDATER_ENV] = "1"
    return out


# -- records and state ----------------------------------------------------------------------

_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def masked_args(argv: Sequence[str]) -> list[str]:
    """``argv[1:]`` fit for a log: emails and token-shaped text masked."""
    return [_EMAIL_RE.sub("<email>", _TOKEN_RE.sub("<redacted>", str(a))) for a in argv[1:]]


def _default_root() -> Path | None:
    try:
        from claude_swap.paths import get_backup_root

        return get_backup_root()
    except Exception:
        return None


def append_record(root: Path | None, record: Mapping[str, Any]) -> None:
    """One JSON line in ``claude-exec.jsonl`` (:func:`append_jsonl`)."""
    append_jsonl(root, EXEC_LOG_FILENAME, record)


def append_jsonl(root: Path | None, filename: str, record: Mapping[str, Any]) -> None:
    """One JSON line in ``<root>/<filename>`` (0600); the file moves to
    ``.1`` first when this line would take it past
    :data:`EXEC_LOG_MAX_BYTES`. Never raises."""
    if root is None:
        return
    from claude_swap.locking import FileLock

    line = (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8")
    path = Path(root) / filename
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        # Rotation and the append under one lock: two writers rotating at
        # once would otherwise drop a generation. Unlocked (busy for 2 s),
        # the line is still appended — O_APPEND keeps it whole — but the
        # file is not rotated this time.
        lock = FileLock(Path(root) / f".{filename}.lock", timeout=2.0)
        held = lock.acquire()
        try:
            if held:
                try:
                    if path.stat().st_size + len(line) > EXEC_LOG_MAX_BYTES:
                        os.replace(path, path.with_name(path.name + ".1"))
                except FileNotFoundError:
                    pass
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            if held:
                lock.release()
    except Exception as e:  # an audit line never breaks a launch
        _logger.debug("claude exec: could not append to %s: %s", path, type(e).__name__)


def read_jsonl(root: Path | None, filename: str = EXEC_LOG_FILENAME) -> list[dict]:
    """The records of ``filename`` and its ``.1``, oldest first; unreadable
    lines are skipped."""
    if root is None:
        return []
    out: list[dict] = []
    base = Path(root) / filename
    for path in (base.with_name(base.name + ".1"), base):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def last_launch_before(root: Path | None, at: float) -> dict | None:
    """The latest ``claude`` run cc-swap started before ``at`` (epoch s)."""
    best = None
    for rec in read_jsonl(root):
        t = rec.get("at")
        if rec.get("kind") != "exec" or not isinstance(t, (int, float)) or t >= at:
            continue
        if best is None or t > best["at"]:
            best = rec
    return best


def load_state(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {}
    try:
        raw = json.loads((Path(root) / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    _upgrade_legacy_external(raw)
    return raw


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _upgrade_legacy_external(state: dict) -> None:
    """A killed mark 0.5.3 set from a kill it only SAW (``log stream``, a
    crash report) is evidence now, not a pause: it becomes the ``external``
    entry with a probe due at once (:func:`probe_external`). In memory for
    every reader; written by the next update of the state. A mark a launch
    of cc-swap's was killed under later on (``lastCaller`` one of its own
    callers) keeps the pause: that kill was real."""
    killed = state.get("killed")
    if not (
        isinstance(killed, dict) and "origin" not in killed
        and killed.get("caller") in LEGACY_EXTERNAL_CALLERS
    ):
        return
    last_caller = killed.get("lastCaller")
    if last_caller and last_caller not in LEGACY_EXTERNAL_CALLERS:
        return
    state.pop("killed", None)
    ext = state.get("external")
    if isinstance(ext, dict) and ext.get("identity") == killed.get("identity"):
        return
    first = _num(killed.get("at")) or 0.0
    last = _num(killed.get("lastAt")) or first
    state["external"] = {
        "path": killed.get("path"), "real": killed.get("real"),
        "identity": killed.get("identity"), "version": killed.get("version"),
        "firstAt": first, "lastAt": last,
        "sources": {str(killed.get("caller")): int(killed.get("count") or 1)},
        "launchers": {}, "reports": [], "provenance": None,
        "diagnostics": killed.get("diagnostics"), "upgraded": True,
        "probe": None, "pendingSince": last, "probeDueAt": last,
    }


def _mutate(root: Path | None, fn: Callable[[dict], bool]) -> None:
    """Read-modify-write the state under its lock; written only when ``fn``
    returns True. Best effort: when the lock cannot be had within 5 s the
    update is skipped (never written unlocked)."""
    if root is None:
        return
    from claude_swap.locking import FileLock
    from claude_swap.settings import atomic_write_json

    lock = FileLock(Path(root) / LOCK_FILENAME, timeout=5.0)
    try:
        held = lock.acquire()
    except Exception:
        held = False
    if not held:
        _logger.debug("claude exec: state lock busy; this update is skipped")
        return
    try:
        state = load_state(root)
        if fn(state):
            atomic_write_json(Path(root) / STATE_FILENAME, state)
    except Exception as e:
        _logger.debug("claude exec: state not written: %s", type(e).__name__)
    finally:
        lock.release()


def _section(state: dict, key: str) -> dict:
    value = state.get(key)
    if not isinstance(value, dict):
        value = state[key] = {}
    return value


def _trim(section: dict) -> None:
    while len(section) > REMEMBER_MAX:
        section.pop(next(iter(section)))


# -- tools (codesign, xattr, log) ------------------------------------------------------------


def _tool(argv: Sequence[str], timeout: float) -> tuple[int | None, str, str]:
    """``(rc, stdout, stderr)`` of a bounded diagnostic tool; rc None when it
    did not finish. The one seam tests replace (no real codesign/log)."""
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout:.0f}s"
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, "", f"could not run ({type(e).__name__})"
    return done.returncode, done.stdout or "", done.stderr or ""


def _is_macos() -> bool:
    return sys.platform == "darwin"


_libc: Any = None


def _darwin_listxattr(path: str) -> list[str] | None:
    """``listxattr(2)`` through ctypes (Python's ``os.listxattr`` is Linux only)."""
    global _libc
    import ctypes
    import ctypes.util

    try:
        if _libc is None:
            lib = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            lib.listxattr.argtypes = [
                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int,
            ]
            lib.listxattr.restype = ctypes.c_ssize_t
            _libc = lib
        raw = os.fsencode(path)
        size = _libc.listxattr(raw, None, 0, 0)
        if size <= 0:
            return [] if size == 0 else None
        buf = ctypes.create_string_buffer(size)
        size = _libc.listxattr(raw, buf, size, 0)
        if size < 0:
            return None
        names = buf.raw[:size].split(bytes(1))
        return sorted(n.decode("utf-8", "replace") for n in names if n)
    except Exception:
        return None


def xattr_names(path: str | None) -> list[str] | None:
    """The file's extended attribute NAMES (never values), sorted; None when
    they cannot be read. A ``listxattr`` call, no subprocess: cheap enough
    for every launch."""
    if path is None:
        return None
    lister = getattr(os, "listxattr", None)
    if lister is not None:
        try:
            return sorted(lister(path))
        except OSError:
            return None
    if sys.platform == "darwin":
        return _darwin_listxattr(path)
    return None


_CODESIGN_KEYS = ("Identifier=", "CDHash=", "TeamIdentifier=", "Timestamp=")


def signing_facts(real: str | None) -> dict[str, Any]:
    """macOS: ``codesign -dv`` summary lines, ``codesign --verify``'s result
    and the file's xattr names (never values). ``{}`` elsewhere."""
    if real is None or not _is_macos():
        return {}
    out: dict[str, Any] = {}
    rc, _o, err = _tool([CODESIGN, "-dv", "--verbose=2", real], TOOL_TIMEOUT_S)
    lines = [ln.strip() for ln in err.splitlines() if ln.strip().startswith(_CODESIGN_KEYS)]
    out["codesign"] = lines if lines else (err.strip().splitlines() or [f"rc {rc}"])[:3]
    rc, _o, err = _tool([CODESIGN, "--verify", real], TOOL_TIMEOUT_S)
    out["codesignVerify"] = "ok" if rc == 0 else (
        f"failed (rc {rc}): " + " ".join(err.strip().splitlines()[:2])
    )
    rc, names, err = _tool([XATTR, real], TOOL_TIMEOUT_S)
    out["xattrs"] = names.split() if rc == 0 else [f"unreadable: {err.strip()[:80]}"]
    return out


# -- the watcher ------------------------------------------------------------------------------


def observe(root: Path | None, binary: Binary, *, now: float | None = None) -> None:
    """Note ``binary``'s identity: a new one is logged ("claude binary
    changed", with :func:`signing_facts`); the same file (same inode)
    rewritten after cc-swap executed it is a loud warning — a new inode at
    the path (an update's rename, the ``cp -p && mv`` fix, an npm install)
    is not; a killed mark for an older identity of the launcher path or the
    real file is cleared (a new version gets a new real path). Never
    raises."""
    if root is None or binary.identity is None:
        return
    now = time.time() if now is None else now
    found: dict[str, Any] = {}

    def mutate(state: dict) -> bool:
        changed = False
        seen = _section(state, "binaries")
        old = seen.get(binary.path)
        if not isinstance(old, dict) or old.get("identity") != binary.identity:
            found["changed"] = old if isinstance(old, dict) else None
            seen.pop(binary.path, None)
            seen[binary.path] = {**binary.to_json(), "seenAt": now}
            _trim(seen)
            changed = True
        ran = _section(state, "executed").get(binary.real)
        if isinstance(ran, dict) and ran.get("identity") != binary.identity:
            state["executed"].pop(binary.real, None)
            if (ran.get("identity") or [None, None])[1] == binary.ino:
                found["rewritten"] = dict(ran)
                state["rewritten"] = {
                    "real": binary.real, "at": now, "execAt": ran.get("at"),
                    "caller": ran.get("caller"), "sameInode": True,
                }
            changed = True
        killed = state.get("killed")
        if (
            isinstance(killed, dict)
            and _same_binary(killed, binary)
            and killed.get("identity") != binary.identity
        ):
            found["unkilled"] = killed
            state.pop("killed", None)
            changed = True
        ext = state.get("external")
        if (
            isinstance(ext, dict) and _same_binary(ext, binary)
            and ext.get("identity") != binary.identity
        ):
            state.pop("external", None)  # about an earlier file at this path
            changed = True
        return changed

    try:
        _mutate(root, mutate)
        if "changed" in found:
            old = found["changed"]
            record = {
                "kind": "binary-changed", "ts": _iso(now), "at": now,
                "old": old, "new": binary.to_json(), **signing_facts(binary.real),
            }
            _logger.info("claude binary changed: %s", json.dumps(record, sort_keys=True, default=str))
            append_record(root, record)
        if "rewritten" in found:
            ran = found["rewritten"]
            _logger.warning(
                "claude binary at %s was rewritten in place after it had been executed "
                "(by cc-swap at %s, %s): same inode, new content; macOS may kill it at "
                "launch (code-signing cache)",
                binary.real, _iso(ran.get("at")), ran.get("caller"),
            )
            append_record(root, {
                "kind": "rewritten-after-exec", "ts": _iso(now), "at": now, "real": binary.real,
                "sameInode": True, "executed": ran, "now": binary.to_json(),
            })
        if "unkilled" in found:
            _logger.info(
                "claude at %s changed since the OS killed it; the next run decides",
                binary.real,
            )
    except Exception as e:  # the watcher never breaks a launch or a tick
        _logger.debug("claude exec: observe failed: %s", type(e).__name__)


# -- killed by the OS ----------------------------------------------------------------------------


def _same_binary(killed: Mapping[str, Any], binary: Binary) -> bool:
    """Whether a killed mark is about ``binary``'s launcher path or real file."""
    return (killed.get("path") == binary.path) or (
        binary.real is not None and killed.get("real") == binary.real
    )


def current_killed(root: Path | None) -> dict | None:
    """The killed mark, only while its launcher path still resolves to the
    killed identity (a new version, or the fix, retires it). Read-only."""
    killed = any_killed(root)
    if killed is None:
        return None
    for path in (killed.get("path"), killed.get("real")):
        if isinstance(path, str) and path:
            if stat_binary(path).identity == killed.get("identity"):
                return killed
            return None
    return None


def killed_entry(root: Path | None, binary: Binary) -> dict | None:
    """The "killed by the OS" mark for exactly this binary identity, or None."""
    killed = load_state(root).get("killed")
    if isinstance(killed, dict) and binary.identity is not None and (
        killed.get("identity") == binary.identity
    ):
        return killed
    return None


def any_killed(root: Path | None) -> dict | None:
    killed = load_state(root).get("killed")
    return killed if isinstance(killed, dict) else None


def killed_text(killed: Mapping[str, Any]) -> str:
    version = killed.get("version")
    what = f"claude {version}" if version else str(killed.get("real") or "claude")
    return f"{what} is killed by the OS at launch (SIGKILL; see cc-swap doctor)"


def fix_command(real: str) -> str:
    return f"cp -p {real} {real}.tmp && mv {real}.tmp {real}"


def _known_version(root: Path, binary: Binary) -> str | None:
    """The version the priming guard cached for this identity, if any."""
    try:
        from claude_swap.maximize import prime_verify

        seen = prime_verify.load(root).get("lastSeen")
    except Exception:
        return None
    if isinstance(seen, dict) and seen.get("key") == binary.identity:
        version = seen.get("version")
        return version if isinstance(version, str) else None
    return None


def _log_show(pid: int | None) -> str:
    clauses = [
        'eventMessage CONTAINS[c] "claude"',
        'eventMessage CONTAINS "AMFI"',
        'eventMessage CONTAINS[c] "CODE SIGNING"',
        'sender == "AppleMobileFileIntegrity"',
        'eventMessage CONTAINS "provenance sandbox"',
    ]
    if pid:
        clauses.insert(0, f"processID == {int(pid)}")
    rc, out, err = _tool(
        [LOG, "show", "--last", "2m", "--style", "compact", "--predicate", " OR ".join(clauses)],
        LOG_SHOW_TIMEOUT_S,
    )
    text = out if rc == 0 else f"(log show rc {rc}: {err.strip()[:200]})\n{out}"
    return text[-LOG_SHOW_MAX_CHARS:]


def write_kill_diagnostics(root: Path, record: Mapping[str, Any], binary: Binary,
                           now: float) -> Path | None:
    """``claude-kill-<ts>.txt``: the exec record, signing facts and, on
    macOS, two minutes of the unified log around it. Best effort."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    path = Path(root) / f"{KILL_PREFIX}{stamp}.txt"
    parts = [
        "cc-swap: claude was killed by the OS (SIGKILL)",
        "",
        "## exec record",
        json.dumps(record, indent=2, sort_keys=True, default=str),
        "",
        "## binary",
        json.dumps(binary.to_json(), indent=2, sort_keys=True),
    ]
    if _is_macos() and binary.real:
        for title, argv in (
            ("codesign -dv --verbose=2", [CODESIGN, "-dv", "--verbose=2", binary.real]),
            ("codesign --verify", [CODESIGN, "--verify", "--verbose=2", binary.real]),
            ("xattr (names)", [XATTR, binary.real]),
        ):
            rc, out, err = _tool(argv, TOOL_TIMEOUT_S)
            parts += ["", f"## {title} (rc {rc})", out.strip(), err.strip()]
        parts += [
            "", "## log show --last 2m (claude / AMFI / CODE SIGNING / provenance sandbox)",
            _log_show(record.get("pid")),
        ]
    ext = load_state(root).get("external")
    if isinstance(ext, dict) and ext.get("identity") == binary.identity:
        parts += [
            "", "## kills of this file cc-swap did not launch (log stream, crash reports)",
            json.dumps(ext, indent=2, sort_keys=True, default=str),
        ]
    parts += ["", f"Fix (not applied): {fix_command(binary.real or binary.path)}", ""]
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, "\n".join(parts).encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError as e:
        _logger.warning("claude exec: could not write %s: %s", path, type(e).__name__)
        return None
    return path


def _notify_killed(root: Path, binary: Binary, now: float) -> None:
    from claude_swap.maximize import notify

    ident = "/".join(str(x) for x in (binary.identity or [binary.path]))
    note = notify.Note(
        "claude-killed", f"claude-killed:{ident}",
        "cc-swap: Claude Code is killed at launch",
        f"the OS kills {binary.real or binary.path} (SIGKILL); priming is paused — "
        "run cc-swap doctor for the fix",
    )
    notify.deliver(root, note, now=now)


def _on_killed(root: Path, record: dict, binary: Binary, now: float) -> None:
    first: list[bool] = []

    def mutate(state: dict) -> bool:
        prev = state.get("killed")
        if isinstance(prev, dict) and prev.get("identity") == binary.identity:
            prev["count"] = int(prev.get("count") or 1) + 1
            prev["lastAt"] = now
            prev["lastCaller"] = record.get("caller")
            return True
        state["killed"] = {
            "path": binary.path, "real": binary.real, "identity": binary.identity,
            "version": _known_version(root, binary), "at": now, "lastAt": now,
            "caller": record.get("caller"), "pid": record.get("pid"),
            "exit": record.get("exit"), "count": 1, "diagnostics": None,
            "origin": ORIGIN_CC_SWAP,
        }
        first.append(True)
        return True

    _mutate(root, mutate)
    if not first:
        _logger.error(
            "claude at %s was killed by the OS again (SIGKILL; %s)",
            binary.real, record.get("caller"),
        )
        return
    _logger.error(
        "claude at %s was killed by the OS at launch (SIGKILL; %s). Priming pauses. "
        "Likely the macOS code-signing cache; fix: %s",
        binary.real, record.get("caller"), fix_command(binary.real or binary.path),
    )
    diagnostics = write_kill_diagnostics(root, record, binary, now)
    if diagnostics is not None:
        def note_file(state: dict) -> bool:
            killed = state.get("killed")
            if isinstance(killed, dict) and killed.get("identity") == binary.identity:
                killed["diagnostics"] = str(diagnostics)
                return True
            return False

        _mutate(root, note_file)
    _notify_killed(root, binary, now)


def mark_killed_by_os(
    root: Path,
    claude_path: str,
    *,
    source: str,
    at: float,
    detail: str,
    pid: int | None = None,
) -> bool:
    """A kill of a ``claude`` cc-swap launched, seen afterwards (a crash
    report whose pid is one of cc-swap's runs: :func:`cc_swap_launch`) for
    the binary at ``claude_path``: the same "killed by the OS" state as a
    SIGKILL of a cc-swap run (priming paused, one notification per
    identity, diagnostics). A kill nobody can tie to cc-swap goes to
    :func:`note_external_kill` instead. A kill older than the file's current
    ctime is about an earlier file at that path and is ignored; one already
    counted is too, and so is one from before a successful run of this
    identity (:func:`ran_ok_since`): only new evidence marks it again.
    Returns whether it was recorded."""
    binary = stat_binary(claude_path)
    if binary.identity is None or binary.ctime_ns is None or at < binary.ctime_ns / 1e9:
        return False
    killed = killed_entry(root, binary)
    last = killed.get("lastAt") if killed else None
    if isinstance(last, (int, float)) and at <= last:
        return False
    if ran_ok_since(root, binary, at):
        # It ran fine after this kill (and the mark, if any, was cleared):
        # only newer evidence may mark it again — across engine restarts.
        return False
    record = {
        "kind": "seen-kill", "ts": _iso(at), "at": at, "caller": source,
        "launchedBy": ORIGIN_CC_SWAP,
        "pid": pid, "exit": None, "signal": SIGKILL_NUM, "detail": detail[:500],
        **binary.to_json(),
    }
    _logger.error("claude killed by the OS (%s): %s", source, json.dumps(record, default=str))
    append_record(root, record)
    _on_killed(root, record, binary, at)
    return True


def _note_inflight(root: Path | None, pid: int, at: float, caller: str,
                   real: str | None, finishes: bool) -> None:
    """A ``claude`` cc-swap just started (pid, start): a kill seen in the
    log before its own end is recorded must still count as cc-swap's
    (:func:`cc_swap_launch`). ``finishes``: its :class:`Launch` will record
    how it ended (not so for ``cswap run``, which execs ``claude`` in its
    own process). Entries older than :data:`INFLIGHT_MAX_S` are dropped."""

    def mutate(state: dict) -> bool:
        section = _section(state, "inflight")
        for key in [k for k, v in section.items()
                    if not isinstance(v, dict) or not (at - (_num(v.get("at")) or 0.0)
                                                       < INFLIGHT_MAX_S)]:
            section.pop(key, None)
        section[str(pid)] = {
            "kind": "inflight", "pid": pid, "at": at, "caller": caller, "real": real,
            "finishes": finishes,
        }
        _trim(section)
        return True

    _mutate(root, mutate)


def _drop_inflight(root: Path | None, pid: int) -> None:
    current = load_state(root).get("inflight")
    if not isinstance(current, dict) or str(pid) not in current:
        return
    _mutate(root, lambda state: _section(state, "inflight").pop(str(pid), None) is not None)


def inflight(root: Path | None) -> list[dict]:
    """The ``claude`` runs cc-swap started and has not seen end yet."""
    section = load_state(root).get("inflight")
    return [v for v in section.values() if isinstance(v, dict)] if isinstance(section, dict) else []


def inflight_near(root: Path | None, real: str | None, at: float, window_s: float) -> dict | None:
    """A running ``claude`` of cc-swap's of the file ``real`` started in the
    ``window_s`` before ``at`` (a kill line naming no process may be its)."""
    for entry in inflight(root):
        t = _num(entry.get("at"))
        if entry.get("real") == real and t is not None and at - window_s <= t <= at + 1.0:
            return entry
    return None


def cc_swap_launch(root: Path | None, pid: int | None, at: float, *,
                   window_s: float = 3600.0, records: list[dict] | None = None) -> dict | None:
    """The ``claude`` run cc-swap started with process id ``pid`` within
    ``window_s`` before ``at`` (pids are reused; the window keeps an old run
    from matching), or None: one still running (``kind`` ``inflight``, from
    the state: its own end has not been recorded yet), else a finished one
    in ``claude-exec.jsonl`` (or ``records`` read from it already)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    for entry in inflight(root):
        t = _num(entry.get("at"))
        if entry.get("pid") == pid and t is not None and at - window_s <= t <= at + 5.0:
            return entry
    for rec in reversed(read_jsonl(root) if records is None else records):
        t = _num(rec.get("at"))
        if (
            rec.get("kind") == "exec" and rec.get("pid") == pid and t is not None
            and at - window_s <= t <= at + 5.0
        ):
            return rec
    return None


# -- kills of a claude cc-swap did not launch ---------------------------------------------------


def external_count(ext: Mapping[str, Any]) -> int:
    """How many kills the evidence stands for: the larger of the per-source
    counts (one kill shows up both as a kernel line and as a crash report)."""
    sources = ext.get("sources")
    counts = [
        int(v) for v in (sources.values() if isinstance(sources, dict) else ())
        if isinstance(v, int) and not isinstance(v, bool)
    ]
    return max(counts) if counts else 0


def _ok_at(state: Mapping[str, Any], binary: Binary) -> float | None:
    """When this identity last ran fine (:func:`_note_ok`), or None."""
    ok = state.get("ok")
    entry = ok.get(binary.real) if isinstance(ok, dict) else None
    if isinstance(entry, dict) and entry.get("identity") == binary.identity:
        return _num(entry.get("at"))
    return None


def _schedule_probe(state: dict, ext: dict, binary: Binary, at: float) -> None:
    """A probe of ``binary`` is due :data:`PROBE_DELAY_S` after the kill at
    ``at`` (no later than :data:`PROBE_MAX_DEFER_S` after the first kill not
    yet probed), never sooner than :data:`PROBE_EVERY_S` after the last
    probe — unless a run of this identity, or a probe, came after it."""
    ok_at = _ok_at(state, binary)
    if ok_at is not None and at <= ok_at:
        return
    probe = ext.get("probe")
    probed = _num(probe.get("at")) if isinstance(probe, dict) else None
    if probed is not None and at <= probed:
        return
    if _num(ext.get("probeDueAt")) is None:
        ext["pendingSince"] = at
    pending = _num(ext.get("pendingSince"))
    pending = at if pending is None else pending
    due = min(at + PROBE_DELAY_S, pending + PROBE_MAX_DEFER_S)
    if probed is not None:
        due = max(due, probed + PROBE_EVERY_S)
    ext["probeDueAt"] = due


def note_external_kill(
    root: Path,
    claude_path: str,
    *,
    source: str,
    at: float,
    detail: str,
    pid: int | None = None,
    launcher: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    report: str | None = None,
) -> bool:
    """A kill of the ``claude`` at ``claude_path`` that cc-swap did not
    launch (or cannot tell: the kernel's line names no process): evidence,
    never a pause. Counted in the state's ``external`` entry for this
    identity (per source, per launching app, the ASP provenance line that
    came with it; a crash report ``report`` once), logged in
    ``claude-exec.jsonl``, and a probe scheduled (:func:`_schedule_probe`,
    :func:`probe_external`). A kill older than the file's current ctime is
    about an earlier file at that path and is ignored. Returns whether it
    was recorded."""
    binary = stat_binary(claude_path)
    if binary.identity is None or binary.ctime_ns is None or at < binary.ctime_ns / 1e9:
        return False
    recorded: list[bool] = []

    def mutate(state: dict) -> bool:
        ext = state.get("external")
        if not isinstance(ext, dict) or ext.get("identity") != binary.identity:
            ext = state["external"] = {
                "path": binary.path, "real": binary.real, "identity": binary.identity,
                "version": _known_version(Path(root), binary),
                "firstAt": at, "lastAt": at, "sources": {}, "launchers": {},
                "reports": [], "provenance": None, "probe": None,
                "pendingSince": None, "probeDueAt": None,
            }
        reports = ext.get("reports") if isinstance(ext.get("reports"), list) else []
        if report and report in reports:
            return False
        if report:
            reports.append(report)
            ext["reports"] = reports[-EXTERNAL_REPORTS_MAX:]
        sources = _section(ext, "sources")
        sources[source] = int(sources.get(source) or 0) + 1
        if launcher:
            launchers = _section(ext, "launchers")
            launchers[launcher] = int(launchers.get(launcher) or 0) + 1
        if provenance:
            ext["provenance"] = dict(provenance)
        first, last = _num(ext.get("firstAt")), _num(ext.get("lastAt"))
        ext["firstAt"] = at if first is None else min(first, at)
        ext["lastAt"] = at if last is None else max(last, at)
        _schedule_probe(state, ext, binary, at)
        recorded.append(True)
        return True

    _mutate(root, mutate)
    if not recorded:
        return False
    record = {
        "kind": "external-kill", "ts": _iso(at), "at": at, "caller": source,
        "pid": pid, "launcher": launcher, "signal": SIGKILL_NUM, "detail": detail[:500],
        "provenance": dict(provenance) if provenance else None, **binary.to_json(),
    }
    _logger.warning(
        "claude killed by the OS, not a cc-swap launch (%s%s); priming goes on and "
        "the engine checks claude itself: %s",
        source, f", launched by {launcher}" if launcher else "",
        json.dumps(record, default=str),
    )
    append_record(root, record)
    return True


def current_external(root: Path | None) -> dict | None:
    """The kill evidence of :func:`note_external_kill`, only while its
    launcher path still resolves to that identity. Read-only."""
    ext = load_state(root).get("external")
    if not isinstance(ext, dict):
        return None
    for path in (ext.get("path"), ext.get("real")):
        if isinstance(path, str) and path:
            return ext if stat_binary(path).identity == ext.get("identity") else None
    return None


def probe_stale(ext: Mapping[str, Any], now: float) -> bool:
    """Whether the evidence's probe says ``running`` but started longer ago
    than any probe can take (:data:`PROBE_STALE_S`): its engine stopped
    mid-probe, and nothing would ever run it again."""
    probe = ext.get("probe")
    started = _num(probe.get("at")) if isinstance(probe, dict) else None
    return (
        isinstance(probe, dict) and probe.get("result") == "running"
        and started is not None and now - started > PROBE_STALE_S
    )


def _due_at(ext: Mapping[str, Any], now: float) -> float | None:
    """When the evidence's probe is due: its ``probeDueAt``, or now when a
    ``running`` probe is stale (:func:`probe_stale`)."""
    due = _num(ext.get("probeDueAt"))
    if due is None and probe_stale(ext, now):
        return now
    return due


def probe_pending(root: Path | None, now: float) -> bool:
    """Whether a probe of any identity is due (no ``stat``: the tick's
    cheap pre-check before :func:`external_probe_due`)."""
    ext = load_state(root).get("external")
    due = _due_at(ext, now) if isinstance(ext, dict) else None
    return due is not None and due <= now


def external_probe_due(root: Path | None, binary: Binary, now: float) -> bool:
    """Whether :func:`probe_external` would run ``binary`` now."""
    ext = load_state(root).get("external")
    if not isinstance(ext, dict) or binary.identity is None:
        return False
    due = _due_at(ext, now)
    return ext.get("identity") == binary.identity and due is not None and due <= now


def probe_external(root: Path, claude_path: str, *, now: float | None = None) -> str | None:
    """The engine's own check after kills cc-swap did not launch: ``claude
    --version`` through :func:`run` as an engine launch (the guard still
    applies: a file still settling or already killed is not run). Exit 0
    records a successful run (no older evidence can mark this identity
    again) and the probe's result; a SIGKILL takes the killed-by-the-OS
    path (pause, one notification, doctor's error with the fix). Runs only
    when a probe is due (:func:`external_probe_due`), claimed under the
    state lock so two engines never both run it; when a run of this file
    already succeeded after the latest kill, that is the answer and nothing
    runs. Returns the result (``ok``, ``killed``, ``exit N`` …) or None when
    nothing was due. Never raises."""
    now = time.time() if now is None else now
    binary = stat_binary(claude_path)
    claimed: dict[str, Any] = {}

    def claim(state: dict) -> bool:
        ext = state.get("external")
        if (
            binary.identity is None or not isinstance(ext, dict)
            or ext.get("identity") != binary.identity
        ):
            return False
        due = _due_at(ext, now)
        if due is None or due > now:
            return False
        ext["probeDueAt"] = None
        ext["pendingSince"] = None
        ok_at = _ok_at(state, binary)
        last = _num(ext.get("lastAt")) or 0.0
        if ok_at is not None and last <= ok_at:
            ext["probe"] = {"at": ok_at, "result": "ok", "by": "a cc-swap run"}
            claimed["result"] = "ok"
            return True
        ext["probe"] = {"at": now, "result": "running"}
        claimed["run"] = True
        return True

    try:
        _mutate(root, claim)
        if "result" in claimed:
            return claimed["result"]
        if "run" not in claimed:
            return None
        result, retry_at = _run_probe(root, claude_path, binary, now)
    except Exception as e:  # the probe never breaks a tick
        _logger.debug("claude exec: external-kill probe failed: %s", type(e).__name__)
        result, retry_at = f"error ({type(e).__name__})", None

    def settle(state: dict) -> bool:
        ext = state.get("external")
        if not isinstance(ext, dict) or ext.get("identity") != binary.identity:
            return False
        ext["probe"] = {"at": now, "result": result}
        if retry_at is not None and _num(ext.get("probeDueAt")) is None:
            ext["probeDueAt"] = retry_at
            ext["pendingSince"] = now
        return True

    try:
        _mutate(root, settle)
    except Exception:
        pass
    log = _logger.info if result == "ok" else _logger.warning
    log("claude exec: external-kill probe of %s: %s", binary.real, result)
    return result


def _run_probe(root: Path, claude_path: str, binary: Binary,
               now: float) -> tuple[str, float | None]:
    """``(result, retry_at)`` of one ``claude --version`` probe."""
    try:
        done = run(
            [claude_path, "--version"], caller=PROBE_CALLER, timeout=PROBE_TIMEOUT_S,
            root=root, manual=None,
        )
    except ExecRefused as e:
        if killed_entry(root, binary) is not None:
            return "not run: " + e.reason, None
        # Still settling after an update: again once it has.
        left = settle_left(binary, settle_seconds(root), now)
        return "not run: " + e.reason, now + max(left, PROBE_DELAY_S)
    except subprocess.TimeoutExpired:
        return f"timed out after {PROBE_TIMEOUT_S:.0f}s", None
    except OSError as e:
        return f"could not run ({type(e).__name__})", None
    rc = done.returncode
    if rc == 0:
        return "ok", None
    if rc in (-SIGKILL_NUM, SHELL_KILLED_RC):
        return "killed", None
    return f"exit {rc}", None


def _note_ok(state: dict, binary: Binary, now: float) -> None:
    """``binary`` ran fine at ``now``: kill evidence from before then
    (a crash report, a log line) can no longer mark this identity."""
    ok = _section(state, "ok")
    ok.pop(binary.real, None)
    ok[binary.real] = {"identity": binary.identity, "at": now}
    _trim(ok)


def ran_ok_since(root: Path | None, binary: Binary, at: float) -> bool:
    """Whether ``binary`` (this identity) ran fine at or after ``at``."""
    ok = load_state(root).get("ok")
    entry = ok.get(binary.real) if isinstance(ok, dict) else None
    last = entry.get("at") if isinstance(entry, dict) else None
    return (
        isinstance(last, (int, float)) and entry.get("identity") == binary.identity
        and at <= last
    )


def _clear_killed(root: Path, binary: Binary, now: float | None = None) -> None:
    cleared: list[dict] = []
    now = time.time() if now is None else now

    def mutate(state: dict) -> bool:
        killed = state.get("killed")
        if isinstance(killed, dict) and _same_binary(killed, binary):
            cleared.append(state.pop("killed"))
            if binary.identity is not None:
                _note_ok(state, binary, now)
            return True
        return False

    _mutate(root, mutate)
    if cleared:
        _logger.info("claude at %s runs again; the killed-by-the-OS mark is cleared", binary.real)


# -- the engine's hold ------------------------------------------------------------------------


def engine_hold(root: Path, binary: Binary, *, now: float) -> str | None:
    """Why the engine must not run ``binary`` this tick (killed by the OS,
    or not settled yet), else None. Records the settle wait for displays
    (:func:`display_note`) and clears it once settled."""
    killed = killed_entry(root, binary)
    if killed is not None:
        _set_settling(root, None)
        return killed_text(killed)
    left = settle_left(binary, settle_seconds(root), now)
    if left > 0:
        _set_settling(root, {"path": binary.path, "real": binary.real, "until": now + left, "at": now})
        return settle_text(left)
    _set_settling(root, None)
    return None


def _set_settling(root: Path, value: dict | None) -> None:
    current = load_state(root).get("settling")
    if value is None and current is None:
        return

    def mutate(state: dict) -> bool:
        if value is None:
            return state.pop("settling", None) is not None
        state["settling"] = value
        return True

    _mutate(root, mutate)


def display_note(root: Path | None, now: float | None = None) -> str | None:
    """For displays (no subprocess): the killed mark, else a settle wait
    the engine recorded that is still running, else None."""
    state = load_state(root)
    killed = current_killed(root)
    if killed is not None:
        return killed_text(killed)
    settling = state.get("settling")
    now = time.time() if now is None else now
    if isinstance(settling, dict):
        until = settling.get("until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
            return settle_text(until - now)
    return None


# -- one launch ----------------------------------------------------------------------------------

_UNSET: Any = object()


class Launch:
    """One ``claude`` run: stat + watch + guard on creation, then
    :meth:`popen` (or :meth:`mark_started` for a caller that spawns it
    itself), then :meth:`finish` with how it ended — which writes the record
    and handles a SIGKILL. As a context manager an unfinished launch is
    recorded on exit."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        caller: str,
        root: Path | None = None,
        manual: Manual | None = _UNSET,
        clock: Callable[[], float] = time.time,
        record_only: bool = False,
    ):
        # record_only (cc-swap doctor): the audit line and nothing else — no
        # watcher, no guard, no state, no kill handling, no notification.
        self.record_only = record_only
        self.argv = [str(a) for a in argv]
        self.caller = caller
        self.root = Path(root) if root is not None else _default_root()
        self.manual = current_manual() if manual is _UNSET else manual
        self.clock = clock
        self.binary = stat_binary(self.argv[0])
        # Before and after the run (:meth:`finish`): does running it change
        # the file's xattrs or ctime (com.apple.provenance …)?
        self.xattrs_before = xattr_names(self.binary.real)
        self.pid: int | None = None
        self.record: dict[str, Any] | None = None
        self.settle_override: float | None = None
        self._start: float | None = None
        self._start_at: float | None = None
        now = clock()
        if not record_only:
            observe(self.root, self.binary, now=now)
            self._guard(now)

    def _guard(self, now: float) -> None:
        if self.root is None:
            return
        left = settle_left(self.binary, settle_seconds(self.root), now)
        if self.manual is None:
            killed = killed_entry(self.root, self.binary)
            if killed is not None:
                self._refuse(killed_text(killed), now)
            if left > 0:
                self._refuse(settle_text(left), now)
            return
        if left > 0:
            self.settle_override = round(left)
            young = self.binary.youngest_ns()
            age = now - young / 1e9 if young is not None else 0.0
            message = (
                f"{self.binary.path} changed {age:.0f}s ago (claude.settleS "
                f"{settle_seconds(self.root):.0f}s, {left:.0f}s left); running it anyway "
                f"for {self.manual.command}"
            )
            _logger.warning("claude exec: %s", message)
            if self.manual.warn is not None:
                try:
                    self.manual.warn(f"Warning: {message}")
                except Exception:
                    pass

    def _refuse(self, reason: str, now: float) -> None:
        record = self._base(now) | {"kind": "refused", "reason": reason}
        _logger.info("claude exec refused: %s", json.dumps(record, sort_keys=True, default=str))
        append_record(self.root, record)
        raise ExecRefused(reason)

    def _base(self, now: float) -> dict[str, Any]:
        b = self.binary
        mtime = b.mtime_ns / 1e9 if b.mtime_ns is not None else None
        return {
            "ts": _iso(now), "at": now, "caller": self.caller,
            "manual": self.manual.command if self.manual else None,
            "args": masked_args(self.argv), **b.to_json(),
            "ageS": round(now - mtime, 3) if mtime is not None else None,
        }

    # -- running ---------------------------------------------------------------------------

    def mark_started(self, pid: int | None = None) -> None:
        """Started now; ``pid`` given: a caller that runs ``claude`` in its
        own process (``cswap run`` execs it), so no end will be recorded."""
        self._start = time.monotonic()
        self._start_at = self.clock()
        self.pid = pid
        if pid is not None:
            self._note_inflight(finishes=False)

    def _note_inflight(self, *, finishes: bool) -> None:
        if self.root is None or self.record_only or self.pid is None:
            return
        try:
            _note_inflight(
                self.root, self.pid, self._start_at or self.clock(), self.caller,
                self.binary.real, finishes,
            )
        except Exception as e:  # bookkeeping never breaks a launch
            _logger.debug("claude exec: in-flight note failed: %s", type(e).__name__)

    def popen(self, **kwargs) -> subprocess.Popen:
        """``subprocess.Popen(argv, **kwargs)``, recorded; a spawn error is
        recorded and re-raised."""
        self.mark_started()
        try:
            proc = subprocess.Popen(self.argv, **kwargs)
        except OSError as e:
            self.finish(None, error=f"{type(e).__name__}: {e.strerror or e}")
            raise
        self.pid = proc.pid
        self._note_inflight(finishes=True)
        return proc

    def finish(
        self,
        returncode: int | None,
        *,
        timed_out: bool = False,
        error: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record how the run ended (once). A signal-9 end that cc-swap did
        not cause itself (no timeout, no error) is the OS killing it."""
        if self.record is not None:
            return self.record
        now = self.clock()
        signum = None
        via_shell = False
        if isinstance(returncode, int) and returncode < 0:
            signum = -returncode
        elif returncode == SHELL_KILLED_RC:
            signum, via_shell = SIGKILL_NUM, True
        record = self._base(self._start_at if self._start_at is not None else now) | {
            "kind": "exec",
            "pid": self.pid,
            "exit": returncode,
            "signal": signum,
            "durationS": round(time.monotonic() - self._start, 3) if self._start else None,
            "timedOut": timed_out,
            "error": error,
        }
        if via_shell:
            record["signalViaShell"] = True
        if self.settle_override is not None:
            record["settleOverrideS"] = self.settle_override
        if self._start is not None:
            self._note_after(record)
        if note:
            record["note"] = note
        self.record = record
        killed = signum == SIGKILL_NUM and not timed_out and error is None
        line = json.dumps(record, sort_keys=True, default=str)
        if killed:
            _logger.error("claude exec KILLED: %s", line)
        else:
            _logger.info("claude exec: %s", line)
        append_record(self.root, record)
        if self.root is not None and not self.record_only:
            if self.pid is not None:
                try:  # after the record: one of the two always names it
                    _drop_inflight(self.root, self.pid)
                except Exception:
                    pass
            try:
                self._after(record, killed, now)
            except Exception as e:
                _logger.debug("claude exec: bookkeeping failed: %s", type(e).__name__)
        return record

    def _note_after(self, record: dict) -> None:
        """The binary's xattr names and ctime after the run, next to the ones
        read before it; a change is a warning with both values."""
        b = self.binary
        after = stat_binary(b.path)
        xattrs = xattr_names(after.real)
        record["xattrsBefore"] = self.xattrs_before
        record["xattrsAfter"] = xattrs
        record["ctimeAfterNs"] = after.ctime_ns
        if after.real != b.real or b.real is None:
            return
        changed = []
        if xattrs != self.xattrs_before:
            changed.append(f"xattrs {self.xattrs_before} -> {xattrs}")
        if after.ctime_ns != b.ctime_ns:
            changed.append(f"ctime {b.ctime_ns} -> {after.ctime_ns}")
        if changed:
            record["binaryChangedByRun"] = True
            _logger.warning(
                "claude binary at %s changed during a cc-swap run (%s): %s",
                b.real, self.caller, "; ".join(changed),
            )

    def _after(self, record: dict, killed: bool, now: float) -> None:
        b = self.binary
        if b.real is None or self._start is None:
            return
        if b.identity is not None:
            ran_ok = record.get("exit") == 0

            def mutate(state: dict) -> bool:
                executed = _section(state, "executed")
                executed.pop(b.real, None)
                executed[b.real] = {"identity": b.identity, "at": now, "caller": self.caller}
                _trim(executed)
                if ran_ok:
                    _note_ok(state, b, now)
                return True

            _mutate(self.root, mutate)
        if killed:
            _on_killed(self.root, record, b, now)
        elif record.get("exit") == 0:
            _clear_killed(self.root, b)

    def __enter__(self) -> Launch:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.record is None:
            self.finish(None, error=exc_type.__name__ if exc_type else "not finished")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass


def run(
    argv: Sequence[str],
    *,
    caller: str,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    root: Path | None = None,
    manual: Manual | None = _UNSET,
    record_only: bool = False,
) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, capture_output=True, text=True, timeout=...)``
    through a :class:`Launch`: stdin closed, ``DISABLE_AUTOUPDATER=1``, its
    own process group (a timeout kills the whole tree). Raises what
    ``subprocess.run`` would (``TimeoutExpired``, ``OSError``) and
    :class:`ExecRefused`."""
    launch = Launch(argv, caller=caller, root=root, manual=manual, record_only=record_only)
    proc = launch.popen(
        env=child_env(env), cwd=None if cwd is None else str(cwd),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", start_new_session=os.name == "posix",
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        launch.finish(proc.returncode, timed_out=True)
        raise subprocess.TimeoutExpired(launch.argv, timeout, output=out, stderr=err)
    except BaseException as e:
        _kill_group(proc)
        try:
            proc.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        launch.finish(None, error=type(e).__name__)
        raise
    launch.finish(proc.returncode)
    return subprocess.CompletedProcess(launch.argv, proc.returncode, out, err)
