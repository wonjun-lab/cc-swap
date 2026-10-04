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
is verified for"; nothing else writes it. ``cc-swap claude-update`` feeds
the guard two ways: it puts the version it read after updating into
``lastSeen`` (:func:`note_seen`), and the change it records in
``autoswitch_state.json`` (``claudeVersionPrevious`` -> ``claudeVersion``)
pauses priming by itself (:func:`pending_update`) — also on an install
with no verified version yet, which would otherwise keep priming and adopt
the new build as its baseline.

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

from claude_swap.maximize.claude_version import UPDATE_LOCK_FILENAME, parse_version

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
#: How long ``prime verify`` waits for another verify to finish (one probe).
VERIFY_LOCK_WAIT_S = PROBE_TIMEOUT_S + 30.0
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
    _save(root, data)


#: Set by a failed ``prime verify``: ``{"version": "2.1.4" | None, "at": 1.7e9,
#: "checks": ["invalid token is rejected", ...]}``. It replaces the verified
#: record (a build that just failed isolation is not verified, whatever an
#: earlier run said) and pauses priming until a verify passes.
FAILED_KEY = "verifyFailed"


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
    _save(root, data)


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


# `parse_version` is the one parser shared with `cc-swap claude-update`.


def _child_env() -> dict[str, str]:
    from claude_swap.maximize.primer import _scrubbed

    return {k: v for k, v in os.environ.items() if not _scrubbed(k)}


def read_claude_version(claude_path: str) -> str | None:
    """``claude --version``'s version number; None when it cannot be read.
    No credentials in its environment; stdin closed; bounded."""
    try:
        result = subprocess.run(
            [claude_path, "--version"],
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=VERSION_TIMEOUT_S,
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


def update_in_progress(root: Path) -> bool:
    """Whether ``cc-swap claude-update`` holds its lock (``claude update`` is
    replacing the binary). A lock that cannot be checked counts as held:
    this guards a launch, and holding one back is the safe error."""
    from claude_swap.locking import FileLock

    lock = FileLock(Path(root) / UPDATE_LOCK_FILENAME, timeout=0)
    try:
        if lock.acquire():
            lock.release()
            return False
    except OSError:
        pass
    return True


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


def note_seen(
    root: Path, claude_path: str, version: str, *, clock: Callable[[], float] = time.time
) -> None:
    """Put a version someone else just read from ``claude_path`` (``cc-swap
    claude-update``) into the ``lastSeen`` cache, keyed by the executable's
    identity exactly as :func:`current_version` would have."""
    data = load(root)
    data["lastSeen"] = {
        "version": version, "path": claude_path, "key": identity(claude_path), "at": clock(),
    }
    _save(root, data)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def pending_update(root: Path, data: Mapping[str, Any] | None = None) -> tuple[str, str] | None:
    """A version change ``cc-swap claude-update`` recorded (``claudeVersion``
    in ``autoswitch_state.json``) that the verified version does not cover:
    ``(previous, version)``, or None.

    It is covered once ``version`` is the verified one, or once a
    verification was recorded after the change (``prime verify`` run on a
    build that was rolled back since)."""
    from claude_swap.maximize.claude_update import recorded_claude_change

    change = recorded_claude_change(Path(root))
    if change is None:
        return None
    previous, version, changed_at = change
    data = load(root) if data is None else data
    verified = _text(data.get("verifiedClaudeVersion"))
    if version == verified:
        return None
    verified_at = _num(data.get("verifiedAt"))
    if verified is not None and verified_at is not None and changed_at is not None:
        # The change is stamped to the second; a verification later in that
        # same second still counts as before it (pause, never miss one).
        if verified_at >= changed_at + 1.0:
            return None
    return previous, version


PAUSED_UNTIL = "priming paused until `cc-swap prime verify` passes"


#: ``Gate.cause`` values the engine may lift by verifying on its own: the
#: gate is closed only because the installed version changed.
AUTO_CAUSES = frozenset({"changed", "update"})


@dataclass(frozen=True)
class Gate:
    ok: bool
    current: str | None
    verified: str | None
    reason: str
    # verified | no-record | failed | unreadable | changed | update | error
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
    is compared with two observations of the installed one: ``claude
    --version`` (cached per executable in ``lastSeen``) and the change
    ``cc-swap claude-update`` recorded (:func:`pending_update`). Either one
    disagreeing pauses priming until ``cc-swap prime verify`` records the
    new version."""
    data = load(root)
    verified = _text(data.get("verifiedClaudeVersion"))
    current = current_version(root, claude_path, reader=reader, clock=clock)
    update = pending_update(root, data)
    failed = failed_verify(root, data)
    if failed is not None:
        return Gate(
            False, current, None, f"{_failed_text(failed)}; {PAUSED_UNTIL}",
            "failed", _text(failed.get("version")),
        )
    if verified is None:
        if update is None:
            return Gate(True, current, None, "no verified claude version recorded yet", "no-record")
        previous, version = update
        return Gate(
            False, current, None,
            f"cc-swap claude-update changed claude {previous} -> {version} and priming "
            f"isolation was never verified; {PAUSED_UNTIL}",
            "update",
        )
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
    if update is not None:
        return Gate(
            False, current, verified,
            f"cc-swap claude-update recorded claude {update[1]} after priming isolation "
            f"was verified with {verified}; {PAUSED_UNTIL}",
            "update",
        )
    return Gate(True, current, verified, "verified", "verified")


def auto_verify_due(
    root: Path, verdict: Gate, now: float, data: Mapping[str, Any] | None = None
) -> str | None:
    """The claude version the engine should verify on its own now, or None.

    Only for a gate closed because the installed version changed (never an
    unreadable version or a crashed check), and never for a version a
    verify already failed for: that one waits for a manual ``prime verify``.
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


def paused_state(root: Path, *, auto_verify: bool | None = None) -> tuple[str | None, bool]:
    """``(note, auto)``: :func:`paused_note`'s text, and whether the engine
    will lift this pause itself (``prime.autoVerify``, read from settings
    when not given) — a version change it has not given up on yet."""
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

    def note(head: str, engine: bool) -> tuple[str, bool]:
        return f"paused: {head} ({AUTO_HINT if engine else 'cc-swap prime verify'})", engine

    failed = failed_verify(root, data)
    if failed is not None:
        return note(_failed_text(failed), auto("failed", _text(failed.get("version"))))
    verified = _text(data.get("verifiedClaudeVersion"))
    if verified is not None and current is not None and current != verified:
        return note(f"claude {verified} -> {current}", auto("changed"))
    update = pending_update(root, data)
    if update is not None:
        return note(f"claude {verified or update[0]} -> {update[1]}", auto("update"))
    return None, False


def paused_note(root: Path, *, auto_verify: bool | None = None) -> str | None:
    """For displays (no subprocess): why priming is paused, from what the
    engine last saw and what ``cc-swap claude-update`` recorded, or None.
    The same rule as :func:`gate`, minus the unreadable-version case."""
    return paused_state(root, auto_verify=auto_verify)[0]


# -- prime verify ------------------------------------------------------------------------


def keychain_attributes(service: str) -> str | None:
    """The attributes ``security`` prints for a generic-password item —
    no ``-w``/``-g``, so the secret is never read and never prompted for.
    None when there is no such item (or it cannot be looked at)."""
    from claude_swap import macos_keychain

    try:
        result = subprocess.run(
            [
                macos_keychain._SECURITY, "find-generic-password",
                "-a", macos_keychain.keychain_account_name(), "-s", service,
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


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

    return run_prime(argv, env, cwd, timeout)


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
            out[f"keychain:{service}"] = _sha(deps.keychain_attrs(service))
    creds = deps.credentials_path() if deps.credentials_path else get_credentials_path()
    config = deps.config_path() if deps.config_path else get_global_config_path()
    out["credentials-file"] = _file_hash(creds)
    out["account"] = _account_hash(config)
    return out


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
                # A network error or a 429 says nothing about isolation yet;
                # anything else is not a blip, so it is not retried.
                transient=kind in ("other", "rate-limited"),
            )
        if macos:
            left = deps.keychain_attrs(service) is not None
            if left:
                deps.keychain_delete(service)
            report.add(
                "no Keychain item left behind", not left,
                "the probe profile's item was left behind (deleted now)" if left
                else "none for the probe profile",
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
    version = deps.version(claude_path)
    report.version = version
    if version is not None:
        # What was just read is what the guard's cache must say for this
        # exact binary; a stale entry would otherwise outlive the verify.
        try:
            note_seen(root, claude_path, version)
        except OSError:
            pass
    if not report.add(
        "claude --version", version is not None, version or "unreadable", transient=True
    ):
        return report
    before = active_fingerprint(deps)
    _probe(report, claude_path, root, deps, model)
    changed = _changed(before, active_fingerprint(deps))
    report.add(
        "active login unchanged", not changed,
        "changed: " + ", ".join(changed) if changed
        else ("Keychain item attributes, " if deps.macos() else "")
        + ".credentials.json and ~/.claude.json account",
    )
    if live is not None and report.ok:
        before = active_fingerprint(deps)
        ok, detail = live()
        report.add(LIVE_CHECK, ok, detail)
        changed = _changed(before, active_fingerprint(deps))
        report.add(
            "active login unchanged by the live prime", not changed,
            "changed: " + ", ".join(changed) if changed else "unchanged",
        )
        if deps.macos():
            from claude_swap.maximize.primer import PROFILE_DIRNAME
            from claude_swap.session import keychain_service_name

            service = keychain_service_name(root / PROFILE_DIRNAME)
            left = deps.keychain_attrs(service) is not None
            if left:
                deps.keychain_delete(service)
            report.add(
                "no Keychain item left by the live prime", not left,
                "prime-profile's item was left behind (deleted now)" if left else "none",
            )
    if report.ok and record and version is not None:
        # The live prime has to bypass the gate it is about to lift, so it
        # runs first; the record is written only once everything passed.
        record_verified(root, version, by=VERIFIED_BY_CLI, now=now)
        report.recorded = True
    elif record and (failures := report.failures()):
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
            "(priming pauses after every claude update until this passes; the "
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
            report = run_verify(root, claude, deps=default_deps(), model=prime.model, live=live)
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
