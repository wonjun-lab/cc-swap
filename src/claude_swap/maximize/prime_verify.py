"""Priming's Claude Code version guard and ``cc-swap prime verify``.

Priming runs the real ``claude`` with an access token in an isolated
profile. That isolation (no login written to the live Keychain item or
``~/.claude.json``, nothing left behind in the Keychain) is a property of a
particular Claude Code build: a new version can change where it stores or
looks for credentials. So the version priming was last verified with is
recorded, and priming pauses — with a warning — once ``claude --version``
reports anything else, until ``cc-swap prime verify`` passes again.

State lives in ``<backup root>/prime_verify.json``::

    {"verifiedClaudeVersion": "2.1.3", "verifiedAt": 1.7e9, "verifiedBy": "prime verify",
     "lastSeen": {"version": "2.1.4", "path": "...", "key": [...], "at": 1.7e9}}

A failed ``prime verify`` replaces the three ``verified*`` keys with
``verifyFailed`` (:data:`FAILED_KEY`), which pauses priming until a verify
passes.

``verifiedBy`` is ``prime verify`` or ``primed`` (the first prime the usage
endpoint confirmed, when nothing was recorded before: an install that never
ran ``prime verify`` keeps priming until ``claude`` changes). ``lastSeen``
caches the version by the executable's identity (real path, inode, mtime,
size), so the engine runs ``claude --version`` only after an update.

``verifiedClaudeVersion`` is the one record of "the claude version priming
is verified for"; nothing else writes it. cc-swap never updates Claude Code
itself: Claude Code's own updater replaces the binary, and the guard
notices through the executable's identity. (A ``claudeVersion`` key an
older cc-swap left in ``autoswitch_state.json`` is ignored.)

``prime verify`` automates the old README isolation checklist with zero-cost
checks — an invalid-token run in a throwaway profile must fail with a clean
401, leave no Keychain item and no ``.credentials.json`` behind, and leave
the active login's Keychain item *attributes* (never its secret), its
``.credentials.json`` and ``~/.claude.json`` account unchanged — and, only
with ``--live``, one real prime. Everything that touches the system goes
through :class:`VerifyDeps`, so tests run it with fakes.

With ``prime.autoVerify`` (default on) the engine runs the same zero-cost
checks itself when the gate is closed only because the installed version
changed (``primer.Primer._auto_verify``). A failure that may pass on a retry
(a timeout, a network error) is retried every :data:`AUTO_RETRY_S`, at most
:data:`AUTO_MAX_TRIES` times per version (``autoVerify`` in the record:
``{"version", "tries", "nextAt", "last"}``); anything else, or the last try,
is recorded as a failed verify, exactly as ``prime verify`` records one.
One verify runs at a time: both hold :data:`VERIFY_LOCK_FILENAME`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_swap.maximize.claude_version import parse_version

VERIFY_FILENAME = "prime_verify.json"
VERSION_TIMEOUT_S = 15.0
PROBE_TIMEOUT_S = 60.0
#: Shaped like an access token so the CLI sends it, but not a real one. Never
#: a refresh token (``build_prime_env`` refuses ``sk-ant-ort``).
BAD_TOKEN = "sk-ant-oat01-cc-swap-prime-verify-not-a-real-token-0000000000"
VERIFIED_BY_CLI = "prime verify"
VERIFIED_BY_PRIME = "primed"
VERIFIED_BY_ENGINE = "engine auto-verify"
LIVE_CHECK = "one live prime"
#: Held by every verify (``prime verify`` and the engine's), in the backup root.
VERIFY_LOCK_FILENAME = ".prime_verify.lock"
#: ``security`` calls are bounded by this (macos_keychain._TIMEOUT too).
KEYCHAIN_TIMEOUT_S = 5.0
#: The most ``security`` calls one zero-cost verify makes: attributes of up
#: to two active items before and after, the probe item's check, and the
#: probe item's deletes.
_KEYCHAIN_CALLS_MAX = 8
#: A ``claude`` run killed at its timeout gets this long to drain
#: (primer.KILL_GRACE_S).
_KILL_GRACE_S = 5.0
#: How long ``prime verify`` waits for another verify to finish: the worst
#: case of one zero-cost verify (the engine's gate may read ``--version``
#: under the lock too), plus a margin.
VERIFY_LOCK_WAIT_S = (
    2 * VERSION_TIMEOUT_S + PROBE_TIMEOUT_S + _KILL_GRACE_S
    + _KEYCHAIN_CALLS_MAX * KEYCHAIN_TIMEOUT_S + 30.0
)
#: The engine's automatic verify: a transient failure is retried this much
#: later, at most this many tries per claude version.
AUTO_KEY = "autoVerify"
AUTO_RETRY_S = 1800.0
AUTO_MAX_TRIES = 3
AUTO_HINT = "the engine re-verifies it; or cc-swap prime verify"

# -- the record ---------------------------------------------------------------------


def path_for(root: Path) -> Path:
    return Path(root) / VERIFY_FILENAME


def load(root: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path_for(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save(root: Path, data: Mapping[str, Any]) -> None:
    from claude_swap.settings import atomic_write_json

    Path(root).mkdir(parents=True, exist_ok=True)
    atomic_write_json(path_for(root), dict(data))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def verified_version(root: Path) -> str | None:
    return _text(load(root).get("verifiedClaudeVersion"))


def last_seen_version(root: Path) -> str | None:
    seen = load(root).get("lastSeen")
    return _text(seen.get("version")) if isinstance(seen, dict) else None


def record_verified(
    root: Path, version: str, *, by: str, now: float | None = None
) -> None:
    data = load(root)
    data["verifiedClaudeVersion"] = version
    data["verifiedAt"] = time.time() if now is None else now
    data["verifiedBy"] = by
    data.pop(FAILED_KEY, None)
    data.pop(AUTO_KEY, None)
    # Verifying a version that once failed (a manual verify: the engine
    # never verifies those) clears it from the history.
    history = [v for v in failed_versions(root, data) if v != version]
    if history:
        data[FAILED_HISTORY_KEY] = history
    else:
        data.pop(FAILED_HISTORY_KEY, None)
    _save(root, data)


#: Set by a failed ``prime verify``: ``{"version": "2.1.4" | None, "at": 1.7e9,
#: "checks": ["invalid token is rejected", ...]}``. It replaces the verified
#: record (a build that just failed isolation is not verified, whatever an
#: earlier run said) and pauses priming until a verify passes.
FAILED_KEY = "verifyFailed"
#: Every version a verify failed for (most recent last, at most
#: :data:`FAILED_HISTORY_MAX`), kept after a later version passes so a
#: rollback to one of them is never verified automatically.
FAILED_HISTORY_KEY = "failedVersions"
FAILED_HISTORY_MAX = 20


def record_failed(
    root: Path, version: str | None, checks: Sequence[str], *, now: float | None = None
) -> None:
    data = load(root)
    for key in ("verifiedClaudeVersion", "verifiedAt", "verifiedBy", AUTO_KEY):
        data.pop(key, None)
    data[FAILED_KEY] = {
        "version": version,
        "at": time.time() if now is None else now,
        "checks": list(checks),
    }
    if version:
        history = [v for v in failed_versions(root, data) if v != version] + [version]
        data[FAILED_HISTORY_KEY] = history[-FAILED_HISTORY_MAX:]
    _save(root, data)


def failed_versions(root: Path, data: Mapping[str, Any] | None = None) -> list[str]:
    """The versions a verify failed for (:data:`FAILED_HISTORY_KEY`)."""
    data = load(root) if data is None else data
    raw = data.get(FAILED_HISTORY_KEY)
    return [v for v in raw if _text(v)] if isinstance(raw, list) else []


def failed_verify(root: Path, data: Mapping[str, Any] | None = None) -> dict | None:
    """The failed-verify marker (see :data:`FAILED_KEY`), or None."""
    data = load(root) if data is None else data
    failed = data.get(FAILED_KEY)
    if failed is None:
        return None
    return dict(failed) if isinstance(failed, Mapping) else {}


def _failed_text(failed: Mapping) -> str:
    version = _text(failed.get("version"))
    return f"prime verify failed for claude {version}" if version else "prime verify failed"


def restore(root: Path, data: Mapping[str, Any]) -> None:
    """Put back a record read earlier with :func:`load` (``{}``: remove it)."""
    if data:
        _save(root, data)
    else:
        try:
            path_for(root).unlink()
        except OSError:
            pass


def note_verified_prime(root: Path, version: str | None) -> None:
    """A prime the usage endpoint confirmed: the first one becomes the
    baseline when nothing was recorded yet (never overrides a record, and
    never a failed ``prime verify``)."""
    if version and verified_version(root) is None and failed_verify(root) is None:
        record_verified(root, version, by=VERIFIED_BY_PRIME)


# -- reading the version -------------------------------------------------------------


# `parse_version` is the one parser (claude_version.py).


def _child_env() -> dict[str, str]:
    from claude_swap.maximize.primer import _scrubbed

    return {k: v for k, v in os.environ.items() if not _scrubbed(k)}


def read_claude_version(claude_path: str) -> str | None:
    """``claude --version``'s version number; None when it cannot be read
    (or ``claude_exec`` holds the run back). No credentials in its
    environment; stdin closed; bounded; audited (maximize/claude_exec.py)."""
    from claude_swap.maximize import claude_exec

    try:
        result = claude_exec.run(
            [claude_path, "--version"], caller="claude --version (priming guard)",
            env=_child_env(), timeout=VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_version(result.stdout) or parse_version(result.stderr)


def identity(claude_path: str) -> list[Any] | None:
    """The executable's identity: an update replaces the file (or moves a
    versioned symlink), which changes at least one of these."""
    try:
        real = os.path.realpath(claude_path)
        st = os.stat(real)
    except OSError:
        return None
    return [real, st.st_ino, st.st_mtime_ns, st.st_size]


def current_version(
    root: Path,
    claude_path: str,
    *,
    reader: Callable[[str], str | None] | None = None,
    clock: Callable[[], float] = time.time,
) -> str | None:
    """The version of ``claude_path``: cached in ``lastSeen`` while the
    executable is the same file, else read (and the cache updated)."""
    key = identity(claude_path)
    data = load(root)
    seen = data.get("lastSeen")
    if (
        key is not None
        and isinstance(seen, dict)
        and seen.get("key") == key
        and _text(seen.get("version"))
    ):
        return seen["version"]
    read = reader if reader is not None else read_claude_version
    version = read(claude_path)
    if version is not None:
        data["lastSeen"] = {"version": version, "path": claude_path, "key": key, "at": clock()}
        try:
            _save(root, data)
        except OSError:
            pass
    return version


_UNSET: Any = object()


def note_seen(
    root: Path,
    claude_path: str,
    version: str,
    *,
    key: list[Any] | None = _UNSET,
    clock: Callable[[], float] = time.time,
) -> None:
    """Put a version just read from ``claude_path`` (``prime verify``) into
    the ``lastSeen`` cache, keyed by the executable's identity exactly as
    :func:`current_version` would have. Pass ``key`` read (:func:`identity`)
    BEFORE running ``--version``: a binary replaced in between then misses
    the cache instead of pairing the new file with the old version."""
    data = load(root)
    data["lastSeen"] = {
        "version": version,
        "path": claude_path,
        "key": identity(claude_path) if key is _UNSET else key,
        "at": clock(),
    }
    _save(root, data)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _killed(root: Path, claude_path: str) -> dict | None:
    """``claude_exec``'s killed-by-the-OS mark for this exact binary."""
    from claude_swap.maximize import claude_exec

    try:
        return claude_exec.killed_entry(root, claude_exec.stat_binary(claude_path))
    except Exception:
        return None


PAUSED_UNTIL = "priming paused until `cc-swap prime verify` passes"


#: ``Gate.cause`` values the engine may lift by verifying on its own: the
#: gate is closed only because the installed version changed.
AUTO_CAUSES = frozenset({"changed"})


@dataclass(frozen=True)
class Gate:
    ok: bool
    current: str | None
    verified: str | None
    reason: str
    # verified | no-record | failed | unreadable | changed | killed | error
    cause: str = ""
    failed_version: str | None = None  # cause "failed": the version that failed


def gate(
    root: Path,
    claude_path: str,
    *,
    reader: Callable[[str], str | None] | None = None,
    clock: Callable[[], float] = time.time,
) -> Gate:
    """Whether priming may run with ``claude_path`` now.

    One verified version (``verifiedClaudeVersion``, :func:`verified_version`)
    is compared with the installed one, ``claude --version`` (cached per
    executable in ``lastSeen``). A difference pauses priming until ``cc-swap
    prime verify`` (or the engine's own verify) records the new version."""
    data = load(root)
    verified = _text(data.get("verifiedClaudeVersion"))
    killed = _killed(root, claude_path)
    if killed is not None:
        # Running it again would only be killed again: no `--version`.
        from claude_swap.maximize.claude_exec import killed_text

        return Gate(
            False, last_seen_version(root), verified,
            f"{killed_text(killed)}; priming paused until it runs again", "killed",
        )
    current = current_version(root, claude_path, reader=reader, clock=clock)
    failed = failed_verify(root, data)
    if failed is not None:
        return Gate(
            False, current, None, f"{_failed_text(failed)}; {PAUSED_UNTIL}",
            "failed", _text(failed.get("version")),
        )
    if verified is None:
        return Gate(True, current, None, "no verified claude version recorded yet", "no-record")
    if current is None:
        return Gate(
            False, None, verified,
            f"could not read `claude --version`; {PAUSED_UNTIL}",
            "unreadable",
        )
    if current != verified:
        return Gate(
            False, current, verified,
            f"claude changed {verified} -> {current} since priming isolation was "
            f"last verified; {PAUSED_UNTIL}",
            "changed",
        )
    return Gate(True, current, verified, "verified", "verified")


def auto_verify_due(
    root: Path, verdict: Gate, now: float, data: Mapping[str, Any] | None = None
) -> str | None:
    """The claude version the engine should verify on its own now, or None.

    Only for a gate closed because the installed version changed (never an
    unreadable version or a crashed check), and never for a version a
    verify ever failed for (:func:`failed_versions`, so a rollback too):
    that one waits for a manual ``prime verify``.
    A newer build after a failed one gets its own automatic verify. Past a
    transient failure, the next try waits until ``nextAt``; after
    :data:`AUTO_MAX_TRIES` the last one has recorded a failed verify."""
    current = verdict.current
    if verdict.ok or current is None:
        return None
    if verdict.cause == "failed":
        if verdict.failed_version is None or verdict.failed_version == current:
            return None
    elif verdict.cause not in AUTO_CAUSES:
        return None
    data = load(root) if data is None else data
    if current in failed_versions(root, data):
        return None  # failed once (a rollback to it, say): manual verify only
    state = data.get(AUTO_KEY)
    if isinstance(state, Mapping) and state.get("version") == current:
        tries = state.get("tries")
        if isinstance(tries, int) and not isinstance(tries, bool) and tries >= AUTO_MAX_TRIES:
            return None
        next_at = _num(state.get("nextAt"))
        if next_at is not None and now < next_at:
            return None
    return current


def auto_tries(root: Path, version: str, data: Mapping[str, Any] | None = None) -> int:
    """Transient automatic-verify failures recorded for ``version``."""
    data = load(root) if data is None else data
    state = data.get(AUTO_KEY)
    if not isinstance(state, Mapping) or state.get("version") != version:
        return 0
    tries = state.get("tries")
    return tries if isinstance(tries, int) and not isinstance(tries, bool) and tries > 0 else 0


def note_auto_retry(root: Path, version: str, last: str, *, now: float) -> int:
    """Record a transient automatic-verify failure for ``version``; the
    next try waits :data:`AUTO_RETRY_S`. Returns the tries so far."""
    data = load(root)
    tries = auto_tries(root, version, data) + 1
    data[AUTO_KEY] = {
        "version": version, "tries": tries, "nextAt": now + AUTO_RETRY_S, "last": last,
    }
    _save(root, data)
    return tries


def verify_lock(root: Path):
    """The lock every verify holds (``prime verify`` waits for it, the
    engine skips a tick when it is taken)."""
    from claude_swap.locking import FileLock

    return FileLock(Path(root) / VERIFY_LOCK_FILENAME, timeout=0)


def _auto_verify_setting(root: Path) -> bool:
    from claude_swap.settings import load_prime_settings

    try:
        return bool(load_prime_settings(Path(root)).auto_verify)
    except Exception:
        return False


@dataclass(frozen=True)
class PausedView:
    """Why priming is paused, for displays (:func:`paused_view`): Fleet
    words it for the width it has (``home.guard_notice``).

    ``kind``: ``killed`` (the OS kills ``claude`` at launch), ``settle``
    (waiting for an update to settle, until ``until``), ``changed`` (a new
    version not verified yet: ``previous`` -> ``version``) or ``failed`` (a
    verify of ``version`` failed). ``auto``: no reminder is due — the
    engine lifts it by itself (an update settling, a version it
    re-verifies), or, for ``killed``, you were notified once already (the
    mark clears only when the ``claude`` path changes or a run of it
    succeeds: Fleet still asks you to act on it)."""

    kind: str
    note: str
    auto: bool
    version: str | None = None
    previous: str | None = None
    until: float | None = None
    #: Who kills ``claude`` (``macOS``: its code signing; else ``the OS``).
    system: str = "the OS"


def paused_view(
    root: Path, *, auto_verify: bool | None = None, now: float | None = None
) -> PausedView | None:
    """:func:`paused_state` as a :class:`PausedView`, or None."""
    from claude_swap.maximize import claude_exec

    now = time.time() if now is None else now
    held = claude_exec.display_state(root, now)
    if held is not None:
        # Killed by the OS (notified once on its own) or waiting for an
        # update to settle (the engine resumes by itself): no reminder.
        kind, value = held
        if kind == "killed":
            return PausedView(
                "killed", f"paused: {claude_exec.killed_text(value)}", True,
                version=_text(value.get("version")),
                system="macOS" if sys.platform == "darwin" else "the OS",
            )
        return PausedView(
            "settle", f"paused: {claude_exec.settle_text(value - now)}", True, until=value,
        )
    note, auto, kind, version, previous = _verify_pause(root, auto_verify)
    if note is None:
        return None
    return PausedView(kind, note, auto, version=version, previous=previous)


def paused_state(root: Path, *, auto_verify: bool | None = None) -> tuple[str | None, bool]:
    """``(note, auto)``: :func:`paused_note`'s text, and whether the engine
    will lift this pause itself (``prime.autoVerify``, read from settings
    when not given) — a version change it has not given up on yet."""
    view = paused_view(root, auto_verify=auto_verify)
    return (view.note, view.auto) if view is not None else (None, False)


def _verify_pause(
    root: Path, auto_verify: bool | None
) -> tuple[str | None, bool, str, str | None, str | None]:
    """``(note, auto, kind, version, previous)`` for a pause the recorded
    verify state makes (a failed verify, a version change), else a None note."""
    data = load(root)
    if auto_verify is None:
        auto_verify = _auto_verify_setting(root)
    seen = data.get("lastSeen")
    current = _text(seen.get("version")) if isinstance(seen, dict) else None

    def auto(cause: str, failed_version: str | None = None) -> bool:
        if not auto_verify:
            return False
        verdict = Gate(False, current, None, "", cause, failed_version)
        # nextAt is ignored: a retry that is merely waiting still counts.
        return auto_verify_due(root, verdict, float("inf"), data) is not None

    def hint(engine: bool) -> str:
        return AUTO_HINT if engine else "cc-swap prime verify"

    failed = failed_verify(root, data)
    if failed is not None:
        version = _text(failed.get("version"))
        engine = auto("failed", version)
        return f"paused: {_failed_text(failed)} ({hint(engine)})", engine, "failed", version, None
    verified = _text(data.get("verifiedClaudeVersion"))
    if verified is not None and current is not None and current != verified:
        engine = auto("changed")
        return (f"paused: claude {verified} -> {current} ({hint(engine)})", engine,
                "changed", current, verified)
    return None, False, "", None, None


def paused_note(root: Path, *, auto_verify: bool | None = None) -> str | None:
    """For displays (no subprocess): why priming is paused, from what the
    engine last saw, or None.
    The same rule as :func:`gate`, minus the unreadable-version case."""
    return paused_state(root, auto_verify=auto_verify)[0]


# -- prime verify ------------------------------------------------------------------------


class KeychainUnreadable(Exception):
    """``security`` could not say whether an item exists (a locked
    keychain, a timeout, any exit but 0 and 44): a check built on it proves
    nothing, so it must not pass."""


#: ``security``'s exit for "no such item" (errSecItemNotFound).
_NOT_FOUND_RC = 44


def keychain_attributes(service: str) -> str | None:
    """The attributes ``security`` prints for a generic-password item —
    no ``-w``/``-g``, so the secret is never read and never prompted for.
    None when there is no such item (exit 44); :class:`KeychainUnreadable`
    when it cannot be looked at."""
    from claude_swap import macos_keychain

    try:
        result = subprocess.run(
            [
                macos_keychain._SECURITY, "find-generic-password",
                "-a", macos_keychain.keychain_account_name(), "-s", service,
            ],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise KeychainUnreadable(f"security timed out after {KEYCHAIN_TIMEOUT_S:.0f}s")
    except OSError as e:
        raise KeychainUnreadable(f"security could not run ({type(e).__name__})")
    if result.returncode == 0:
        return result.stdout
    if result.returncode == _NOT_FOUND_RC:
        return None
    raise KeychainUnreadable(f"security exited {result.returncode}")


def _keychain_delete(service: str) -> None:
    from claude_swap import macos_keychain

    try:
        macos_keychain.delete_password(service, macos_keychain.keychain_account_name())
    except macos_keychain.KEYCHAIN_ERRORS:
        pass


def _is_macos() -> bool:
    from claude_swap.models import Platform

    return Platform.detect() == Platform.MACOS


def _active_services() -> list[str]:
    from claude_swap.credentials import _active_oauth_keychain_services

    return _active_oauth_keychain_services()


def _default_runner(argv, env, cwd, timeout):
    from claude_swap.maximize.primer import run_prime

    return run_prime(argv, env, cwd, timeout, caller="prime verify probe")


@dataclass
class VerifyDeps:
    """Everything ``prime verify`` does to the system, replaceable in tests."""

    run: Callable[[Sequence[str], Mapping[str, str], Path, float], Any] = _default_runner
    version: Callable[[str], str | None] = read_claude_version
    keychain_attrs: Callable[[str], str | None] = keychain_attributes
    keychain_delete: Callable[[str], None] = _keychain_delete
    macos: Callable[[], bool] = _is_macos
    active_services: Callable[[], list[str]] = _active_services
    credentials_path: Callable[[], Path] | None = None
    config_path: Callable[[], Path] | None = None


def default_deps() -> VerifyDeps:
    return VerifyDeps()


#: Exit statuses of a ``claude`` the OS killed: -9, or 137 through a wrapper.
KILLED_RCS = (-9, 137)
KILLED_DETAIL = (
    "claude was killed by the OS at launch (SIGKILL), not an isolation result; "
    "see cc-swap doctor"
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    # A failure that may pass on a retry (a timeout, claude not starting, a
    # network error) rather than one that saw isolation break.
    transient: bool = False


@dataclass
class VerifyReport:
    claude_path: str | None
    version: str | None
    previous: str | None
    checks: list[Check] = field(default_factory=list)
    recorded: bool = False
    # claude was killed by the OS (SIGKILL) during the verify: nothing about
    # isolation was learned, so nothing is recorded (maximize/claude_exec.py).
    killed: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "", *, transient: bool = False) -> bool:
        self.checks.append(Check(name, ok, detail, transient and not ok))
        return ok

    def failures(self) -> list[str]:
        """The failed checks a failed-verify record names (the live prime
        finding nothing to prime says nothing about isolation)."""
        return [c.name for c in self.checks if not c.ok and c.name != LIVE_CHECK]

    @property
    def transient(self) -> bool:
        """Failed, and only on checks that may pass on a retry."""
        failed = [c for c in self.checks if not c.ok]
        return bool(failed) and all(c.transient for c in failed)

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "ok": self.ok,
            "claudePath": self.claude_path,
            "claudeVersion": self.version,
            "previousVerified": self.previous,
            "recorded": self.recorded,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def _sha(data: bytes | str | None) -> str | None:
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str | None:
    try:
        return _sha(path.read_bytes())
    except OSError:
        return None


def _account_hash(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    account = raw.get("oauthAccount") if isinstance(raw, dict) else None
    return None if account is None else _sha(json.dumps(account, sort_keys=True))


def active_fingerprint(deps: VerifyDeps) -> dict[str, str | None]:
    """Hashes of what identifies the active login: Keychain item
    attributes (macOS), ``.credentials.json`` and ``~/.claude.json``'s
    ``oauthAccount``. Hashes only — nothing here is ever printed."""
    from claude_swap.paths import get_credentials_path, get_global_config_path

    out: dict[str, str | None] = {}
    if deps.macos():
        for service in deps.active_services():
            try:
                out[f"keychain:{service}"] = _sha(deps.keychain_attrs(service))
            except KeychainUnreadable:
                out[f"keychain:{service}"] = UNREADABLE
    creds = deps.credentials_path() if deps.credentials_path else get_credentials_path()
    config = deps.config_path() if deps.config_path else get_global_config_path()
    out["credentials-file"] = _file_hash(creds)
    out["account"] = _account_hash(config)
    return out


#: A fingerprint entry that could not be read: never compared as equal.
UNREADABLE = "<unreadable>"


def _unreadable(*prints: Mapping[str, str | None]) -> list[str]:
    keys = {k for fp in prints for k, v in fp.items() if v == UNREADABLE}
    return ["Keychain item " + k.split(":", 1)[-1] for k in sorted(keys)]


def _add_unchanged(
    report: VerifyReport, name: str, before: Mapping[str, str | None],
    after: Mapping[str, str | None], ok_detail: str,
) -> None:
    """The "active login unchanged" check: fails on a change, and fails
    (transient: worth a retry) when the Keychain could not be read, before
    or after — two unreadable items compare equal and prove nothing."""
    unreadable = _unreadable(before, after)
    if unreadable:
        report.add(
            name, False, "could not read the attributes of " + ", ".join(unreadable)
            + " (locked keychain?)", transient=True,
        )
        return
    changed = _changed(before, after)
    report.add(name, not changed, "changed: " + ", ".join(changed) if changed else ok_detail)


def _leftover_check(
    report: VerifyReport, name: str, service: str, deps: VerifyDeps, found: str, none: str
) -> None:
    """A probe's Keychain item must be gone; an unreadable Keychain fails
    the check (transient) instead of passing it."""
    try:
        left = deps.keychain_attrs(service) is not None
    except KeychainUnreadable as e:
        report.add(name, False, f"could not read the Keychain ({e})", transient=True)
        return
    if left:
        deps.keychain_delete(service)
    report.add(name, not left, found if left else none)


def _changed(before: Mapping[str, str | None], after: Mapping[str, str | None]) -> list[str]:
    labels = {"credentials-file": ".credentials.json", "account": "~/.claude.json account"}
    out = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            out.append(labels.get(key, "Keychain item " + key.split(":", 1)[-1]))
    return out


def _probe(report: VerifyReport, claude: str, root: Path, deps: VerifyDeps, model: str) -> None:
    """The invalid-token run in a throwaway profile, and what it left."""
    from claude_swap.maximize.primer import build_prime_argv, build_prime_env, classify_failure
    from claude_swap.session import keychain_service_name

    Path(root).mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="prime-verify-", dir=root))
    service = keychain_service_name(profile)
    macos = deps.macos()
    try:
        if os.name == "posix":
            os.chmod(profile, 0o700)
        env = build_prime_env(os.environ, profile, BAD_TOKEN)
        result = deps.run(build_prime_argv(claude, model), env, profile, PROBE_TIMEOUT_S)
        if result.timed_out:
            report.add("invalid token is rejected", False,
                       f"no answer in {PROBE_TIMEOUT_S:.0f}s", transient=True)
        elif result.returncode in KILLED_RCS:
            # The OS killed claude at launch: says nothing about isolation.
            report.killed = True
            report.add("invalid token is rejected", False, KILLED_DETAIL, transient=True)
        elif result.returncode is None:
            report.add("invalid token is rejected", False,
                       "could not start claude: " + (result.stderr_tail or "")[-200:],
                       transient=True)
        elif result.returncode == 0 and result.is_error is not True:
            report.add(
                "invalid token is rejected", False,
                "claude ACCEPTED an invalid token: it authenticated with "
                "something else (the live login?) — keep priming off",
            )
        else:
            kind = classify_failure(result)
            report.add(
                "invalid token is rejected", kind == "auth",
                f"clean 401 (exit {result.returncode})" if kind == "auth"
                else f"failed, but not with an auth error ({kind}, exit {result.returncode})",
                # A network error says nothing about isolation yet. A 429 does:
                # an invalid token is refused (401) before any rate limit, so
                # claude authenticated with something else. Not retried.
                transient=kind == "other",
            )
        if macos:
            _leftover_check(
                report, "no Keychain item left behind", service, deps,
                "the probe profile's item was left behind (deleted now)",
                "none for the probe profile",
            )
        stray = (profile / ".credentials.json").exists()
        report.add(
            "no .credentials.json left behind", not stray,
            "the probe profile got a .credentials.json" if stray else "none in the probe profile",
        )
    finally:
        if macos:
            deps.keychain_delete(service)
        shutil.rmtree(profile, ignore_errors=True)


def run_verify(
    root: Path,
    claude_path: str | None,
    *,
    deps: VerifyDeps | None = None,
    model: str = "haiku",
    live: Callable[[], tuple[bool, str]] | None = None,
    record: bool = True,
    now: float | None = None,
) -> VerifyReport:
    """The checks, in order; records the version when every one passed.

    ``live`` runs one real prime and returns ``(ok, detail)``; it is only
    called after the zero-cost checks passed."""
    deps = deps or default_deps()
    root = Path(root)
    report = VerifyReport(claude_path, None, verified_version(root))
    if not report.add(
        "claude found", claude_path is not None,
        claude_path or "no claude at prime.claudePath or ~/.local/bin/claude",
    ):
        return report
    assert claude_path is not None
    key = identity(claude_path)  # before --version: see note_seen
    version = deps.version(claude_path)
    report.version = version
    if version is not None:
        # What was just read is what the guard's cache must say for this
        # exact binary; a stale entry would otherwise outlive the verify.
        try:
            note_seen(root, claude_path, version, key=key)
        except OSError:
            pass
    if version is None and _killed(root, claude_path) is not None:
        report.killed = True
    if not report.add(
        "claude --version", version is not None,
        version or (KILLED_DETAIL if report.killed else "unreadable"), transient=True,
    ):
        return report
    before = active_fingerprint(deps)
    _probe(report, claude_path, root, deps, model)
    _add_unchanged(
        report, "active login unchanged", before, active_fingerprint(deps),
        ("Keychain item attributes, " if deps.macos() else "")
        + ".credentials.json and ~/.claude.json account",
    )
    if live is not None and report.ok:
        before = active_fingerprint(deps)
        ok, detail = live()
        report.add(LIVE_CHECK, ok, detail)
        _add_unchanged(
            report, "active login unchanged by the live prime", before,
            active_fingerprint(deps), "unchanged",
        )
        if deps.macos():
            from claude_swap.maximize.primer import PROFILE_DIRNAME
            from claude_swap.session import keychain_service_name

            _leftover_check(
                report, "no Keychain item left by the live prime",
                keychain_service_name(root / PROFILE_DIRNAME), deps,
                "prime-profile's item was left behind (deleted now)", "none",
            )
    if report.ok and record and version is not None:
        # The live prime has to bypass the gate it is about to lift, so it
        # runs first; the record is written only once everything passed.
        record_verified(root, version, by=VERIFIED_BY_CLI, now=now)
        report.recorded = True
    elif record and not report.killed and (failures := report.failures()):
        # A build that just failed is not verified, whatever an earlier run
        # recorded: drop that record so priming pauses (engine, `prime`,
        # `prime --dry-run` and doctor all read it) until a verify passes.
        # (A missing claude or an unreadable version returned above; a live
        # prime that found nothing to prime says nothing about isolation.)
        try:
            record_failed(root, version, failures, now=now)
        except OSError:
            pass
    return report


def report_lines(report: VerifyReport) -> list[str]:
    head = "cc-swap prime verify"
    if report.version:
        head += f" — claude {report.version}"
    lines = [head]
    for check in report.checks:
        mark = "ok  " if check.ok else "FAIL"
        lines.append(f"  {mark}  {check.name}" + (f": {check.detail}" if check.detail else ""))
    if report.ok:
        was = (
            f" (was {report.previous})"
            if report.previous and report.previous != report.version
            else ""
        )
        lines.append(
            f"Priming isolation verified for claude {report.version}{was}; "
            "priming runs again from the engine's next tick."
        )
    else:
        lines.append(
            "Not verified: priming stays paused for this claude version. "
            "Fix the FAIL lines (or keep prime.enabled false)."
        )
    return lines


def _live_prime(switcher, target: str | None) -> Callable[[], tuple[bool, str]]:
    def run() -> tuple[bool, str]:
        from claude_swap.maximize.prime_cli import manual_prime

        plan = manual_prime(
            switcher, None, dry_run=True, emit=lambda _e: None, check_version=False
        ).plan
        candidates = [num for num, _text, would in plan if would]
        pick = target if target not in (None, "auto") else (candidates[0] if candidates else None)
        if pick is None:
            return False, "no idle account with its 5h window off to prime"
        if pick not in candidates:
            reason = next((t for n, t, _w in plan if n == pick), "not an account")
            return False, f"#{pick} cannot be primed now ({reason})"
        report = manual_prime(
            switcher, {pick}, dry_run=False, emit=lambda _e: None, check_version=False
        )
        outcomes = ", ".join(
            str(getattr(e, "outcome", "?")) for e in report.events if getattr(e, "account", "") == pick
        ) or ("verification pending" if pick in report.pending else "no outcome")
        return not report.failed, f"#{pick}: {outcomes}"

    return run


def verify_command(argv: list[str]) -> None:
    """``cc-swap prime verify [--live [NUM]] [--json]``."""
    from claude_swap.exceptions import ClaudeSwitchError
    from claude_swap.maximize.primer import resolve_claude_path
    from claude_swap.printer import error
    from claude_swap.settings import load_prime_settings
    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(
        prog="cc-swap prime verify",
        description=(
            "Check that priming is still isolated with the installed Claude Code "
            "(priming pauses after every Claude Code update until this passes; the "
            "engine runs it itself unless prime.autoVerify is false). "
            "Zero-cost by default: an invalid-token run in a throwaway profile must "
            "fail with a clean 401 and leave the Keychain and the active login "
            "untouched (Keychain item attributes only — no secret is read)."
        ),
    )
    parser.add_argument(
        "--live",
        nargs="?",
        const="auto",
        metavar="NUM",
        help="Also run one real prime (an idle account with its 5h window off, or NUM)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable report")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32" and os.geteuid() == 0 and not switcher._is_running_in_container():
            error("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)
        root = switcher.backup_dir
        prime = load_prime_settings(root)
        claude = resolve_claude_path(prime.claude_path)
        live = None
        if args.live is not None:
            target = args.live if args.live == "auto" else switcher.resolve_account(args.live)[0]
            live = _live_prime(switcher, target)
        # One verify at a time: the engine may be re-verifying on its own.
        lock = verify_lock(root)
        if not lock.acquire(timeout=VERIFY_LOCK_WAIT_S):
            error("Error: another prime verify is running (the engine re-verifying "
                  "after a Claude Code update?); try again in a minute")
            sys.exit(1)
        try:
            from claude_swap.maximize import claude_exec
            from claude_swap.printer import warning

            # The user's own run: a just-updated claude runs with a warning.
            with claude_exec.manual(
                "cc-swap prime verify", warn=lambda m: warning(m, file=sys.stderr)
            ):
                report = run_verify(
                    root, claude, deps=default_deps(), model=prime.model, live=live,
                )
        finally:
            lock.release()
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled")
        sys.exit(130)
    if args.json:
        print(json.dumps(report.to_json()))
    else:
        for line in report_lines(report):
            print(line)
    sys.exit(0 if report.ok else 1)
