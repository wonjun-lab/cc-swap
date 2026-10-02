"""``cc-swap claude-update [--check] [--json]`` — update Claude Code itself.

Modelled on codex-swap's ``update --codex``: cc-swap does not reimplement any
install method. Claude Code's built-in ``claude update`` knows how it was
installed (native, npm, ...), so the update runs *that*; this module only finds
the real binary, compares versions, serialises concurrent runs, and leaves a
trace other components can react to.

**Which binary.** The same rule as 5h priming (``resolve_claude_path``):
``prime.claudePath`` -> ``~/.local/bin/claude``; ``shutil.which`` only as the
last fallback. A shell alias is invisible to all of these, which is the point:
an alias may point at a wrapper that does something other than update.

**Where "latest" comes from** (``--check`` only). The npm registry's dist-tags
document, ``https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags``,
one small GET returning ``{"stable": ..., "latest": ..., "next": ...}``. Why
this and not the alternatives:

- ``claude update`` has no dry-run mode, and running it mutates.
- ``.../@anthropic-ai/claude-code/latest`` returns the whole package manifest
  (tens of KB) for the same one field.
- The dist-tags document carries ``stable`` as well, and Claude Code's
  ``autoUpdatesChannel`` setting (``latest`` or ``stable``) decides which tag
  ``claude update`` follows. Reading the right tag is what keeps ``--check``
  from announcing an update that ``claude update`` will not install.

The registry is the publication source for every install method (the native
installer ships the same builds), so it is a good reference even for a native
install. If it cannot be read, ``--check`` fails (exit 1): it never claims
"up to date" for something it could not look up.

**Exit codes.** ``--check``: 0 up to date, 10 update available, 1 error.
Without ``--check``: 0 when ``claude update`` succeeded, 1 otherwise.

**The version-change signal.** After a run that observes a Claude Code version
different from the recorded one, ``autoswitch_state.json`` (backup root) gets
``claudeVersion``, ``claudeVersionPrevious`` and ``claudeVersionChangedAt``
(UTC ISO, ``...Z``). Read them with :func:`recorded_claude_version` (or
:func:`recorded_claude_change` for a change). They are written under the
engine's own state lock and the engine preserves unknown keys, so the two
writers coexist. ``--check`` never writes.

**Priming.** The version read after the update also goes into the priming
version guard's ``claude --version`` cache (``prime_verify.note_seen``), and
the guard reads the recorded change: a run that changes the version pauses
priming until ``cc-swap prime verify`` passes, even on an install that never
recorded a verified version. The *verified* version itself lives only in
``prime_verify.json`` (``prime_verify.verified_version``); this module never
writes it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.autoswitch import STATE_FILENAME, STATE_SCHEMA_VERSION
from claude_swap.exceptions import LockError
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.locking import FileLock
from claude_swap.maximize.claude_version import (
    UPDATE_LOCK_FILENAME,
    VERSION_RE,
    parse_version,
    sort_key,
)
from claude_swap.maximize.primer import resolve_claude_path
from claude_swap.paths import get_backup_root, get_claude_config_home
from claude_swap.printer import accent, dimmed, error
from claude_swap.settings import atomic_write_json, load_prime_settings

PACKAGE = "@anthropic-ai/claude-code"
DIST_TAGS_URL = f"https://registry.npmjs.org/-/package/{PACKAGE}/dist-tags"
LOCK_FILENAME = UPDATE_LOCK_FILENAME
DEFAULT_UPDATE_TIMEOUT = 600.0
VERSION_TIMEOUT = 30.0
LOOKUP_TIMEOUT = 10.0

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UPDATE_AVAILABLE = 10

KEY_VERSION = "claudeVersion"
KEY_PREVIOUS = "claudeVersionPrevious"
KEY_CHANGED_AT = "claudeVersionChangedAt"

# -- versions ---------------------------------------------------------------
# One parser, shared with the priming version guard (claude_version.py): the
# version recorded here must equal the one `prime verify` verifies.

_VERSION = VERSION_RE
_key = sort_key


def is_newer(latest: str, current: str) -> bool:
    """Whether ``latest`` is strictly newer; an older ``latest`` is no update."""
    return _key(latest) > _key(current)


# -- finding claude / reading versions --------------------------------------


def find_claude(root: Path) -> str | None:
    """The real ``claude``: ``prime.claudePath`` -> ``~/.local/bin/claude``,
    then ``shutil.which`` as a last resort. None when none is executable."""
    configured = load_prime_settings(root).claude_path
    return resolve_claude_path(configured) or shutil.which("claude")


def installed_version(claude: str, *, timeout: float = VERSION_TIMEOUT) -> str | None:
    """``claude --version``; None when it cannot be read (claude may be broken)."""
    try:
        out = subprocess.run(
            [claude, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(out.stdout) if out.returncode == 0 else None


def update_channel() -> str:
    """``stable`` when Claude Code's ``autoUpdatesChannel`` says so, else ``latest``.

    Read-only look at ``<config home>/settings.json``; anything unreadable
    means the default channel.
    """
    try:
        raw = json.loads((get_claude_config_home() / "settings.json").read_text("utf-8"))
    except (OSError, ValueError):
        return "latest"
    if isinstance(raw, dict) and raw.get("autoUpdatesChannel") == "stable":
        return "stable"
    return "latest"


def latest_version(channel: str = "latest", *, timeout: float = LOOKUP_TIMEOUT) -> str | None:
    """The version npm's dist-tags name for ``channel``; None on any failure."""
    request = urllib.request.Request(
        DIST_TAGS_URL,
        headers={"Accept": "application/json", "User-Agent": "cc-swap-claude-update"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    value = data.get(channel) if isinstance(data, dict) else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _VERSION.fullmatch(value) else None


# -- the recorded version (the signal other components read) -----------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def recorded_claude_version(root: Path | None = None) -> tuple[str | None, str | None]:
    """``(claudeVersion, claudeVersionChangedAt)`` from ``autoswitch_state.json``.

    ``(None, None)`` when nothing was recorded yet. This is the function (and
    ``claudeVersion`` the key) a consumer such as the priming version guard
    should read to learn which Claude Code version cc-swap last saw.
    """
    state = _read_state((get_backup_root() if root is None else root) / STATE_FILENAME)
    version, changed_at = state.get(KEY_VERSION), state.get(KEY_CHANGED_AT)
    return (
        version if isinstance(version, str) else None,
        changed_at if isinstance(changed_at, str) else None,
    )


def _iso_epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def recorded_claude_change(root: Path) -> tuple[str, str, float | None] | None:
    """``(claudeVersionPrevious, claudeVersion, changedAt epoch)`` when the
    record is a version *change* (a previous version different from the
    current one); None for no record or a first observation.

    The priming version guard (``prime_verify``) reads this so that a
    ``claude-update`` that changes the version pauses priming until
    ``cc-swap prime verify`` passes, even when no verified version was
    recorded before.
    """
    state = _read_state(Path(root) / STATE_FILENAME)
    version, previous = state.get(KEY_VERSION), state.get(KEY_PREVIOUS)
    if not (isinstance(version, str) and version and isinstance(previous, str) and previous):
        return None
    if previous == version:
        return None
    return previous, version, _iso_epoch(state.get(KEY_CHANGED_AT))


def record_version(root: Path, version: str, previous: str | None = None) -> bool:
    """Record ``version`` unless it already is the recorded one; True if written.

    ``previous`` is the version this run saw before updating; it wins over the
    older recorded value as ``claudeVersionPrevious``.

    Read-modify-write under the engine's state lock, preserving every other key.
    """
    path = root / STATE_FILENAME
    with FileLock(root / ".autoswitch_state.lock"):
        state = _read_state(path)
        if state.get(KEY_VERSION) == version:
            return False
        state["schemaVersion"] = STATE_SCHEMA_VERSION
        state[KEY_PREVIOUS] = previous or state.get(KEY_VERSION)
        state[KEY_VERSION] = version
        state[KEY_CHANGED_AT] = _now_iso()
        atomic_write_json(path, state)
    return True


# -- running `claude update` --------------------------------------------------


class UpdateRun:
    """What one ``claude update`` did."""

    def __init__(self, returncode: int | None, timed_out: bool, output: str):
        self.returncode = returncode
        self.timed_out = timed_out
        self.output = output

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def run_claude_update(claude: str, *, timeout: float, sink) -> UpdateRun:
    """Run ``claude update``, streaming its output (stdout+stderr merged) to
    ``sink`` as it arrives. After ``timeout`` seconds the whole process group
    is killed."""
    proc = subprocess.Popen(
        [claude, "update"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=os.name != "nt",
    )
    chunks: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            sink.write(line)
            sink.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        proc.wait()
    except BaseException:
        _kill_tree(proc)
        raise
    # A killed child's pipe closes at once; a survivor holding it must not hang us.
    reader.join(timeout=5)
    return UpdateRun(proc.returncode, timed_out, "".join(chunks))


# -- check / update, shared by the CLI and Fleet ------------------------------

_NO_CLAUDE = (
    "claude was not found (looked at prime.claudePath, ~/.local/bin/claude, then PATH); "
    "set it with: cc-swap config set prime.claudePath /path/to/claude"
)
_BUSY = "another `claude update` is already in progress; try again when it ends"
#: The exact command that lifts the priming pause after a version change.
PRIME_VERIFY_COMMAND = "cc-swap prime verify"


@dataclass
class CheckResult:
    """What ``--check`` found; ``error`` is set when it could not decide."""

    claude: str | None = None
    installed: str | None = None
    latest: str | None = None
    channel: str | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        return (
            self.error is None
            and self.installed is not None
            and self.latest is not None
            and is_newer(self.latest, self.installed)
        )


def check_versions(root: Path) -> CheckResult:
    """The installed and the latest version; changes nothing."""
    claude = find_claude(root)
    if claude is None:
        return CheckResult(error=_NO_CLAUDE)
    installed = installed_version(claude)
    if installed is None:
        return CheckResult(claude=claude, error=f"could not read `{claude} --version`")
    channel = update_channel()
    latest = latest_version(channel)
    if latest is None:
        return CheckResult(
            claude=claude, installed=installed, channel=channel,
            error="could not read the latest Claude Code version from the npm registry "
            f"({DIST_TAGS_URL})",
        )
    return CheckResult(claude=claude, installed=installed, latest=latest, channel=channel)


@dataclass
class UpdateResult:
    """What one ``claude-update`` run did. ``run`` is None when nothing ran
    (no claude, or another update holds the lock)."""

    claude: str | None = None
    before: str | None = None
    after: str | None = None
    recorded: bool = False
    run: UpdateRun | None = None
    error: str | None = None
    prime_enabled: bool = False

    @property
    def changed(self) -> bool:
        return self.after is not None and self.after != self.before

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def prime_hint(self) -> str | None:
        """The line naming the command that lifts the priming pause, or None."""
        if not (self.changed and self.prime_enabled):
            return None
        return (
            f"Priming is paused for Claude Code {self.after} until its isolation is "
            f"verified again. Run: {PRIME_VERIFY_COMMAND}"
        )


def _note_for_priming(root: Path, claude: str, version: str) -> None:
    """Hand the version just read to the priming version guard's cache, so
    the pause (and Fleet's attention line) shows at once, with no second
    ``claude --version``."""
    from claude_swap.maximize import prime_verify

    try:
        prime_verify.note_seen(root, claude, version)
    except OSError:
        pass


def perform_update(root: Path, *, timeout: float, sink) -> UpdateResult:
    """Run ``claude update`` (its output streamed to ``sink``), then record
    the version for the rest of cc-swap."""
    claude = find_claude(root)
    if claude is None:
        return UpdateResult(error=_NO_CLAUDE)
    lock = FileLock(root / LOCK_FILENAME, timeout=0)
    if not lock.acquire():
        return UpdateResult(claude=claude, error=_BUSY)
    try:
        before = installed_version(claude)
        run = run_claude_update(claude, timeout=timeout, sink=sink)
        after = installed_version(claude)
    except OSError as e:
        # `claude update` could not even be started (not executable, wrong
        # format, vanished since the lookup): nothing ran, nothing changed.
        return UpdateResult(
            claude=claude,
            error=f"could not run `{claude} update`: {e.strerror or e}",
        )
    finally:
        lock.release()

    result = UpdateResult(claude=claude, before=before, after=after, run=run)
    if after is not None and (result.changed or run.ok):
        try:
            result.recorded = record_version(root, after, before if result.changed else None)
        except (OSError, LockError):
            result.recorded = False
        _note_for_priming(root, claude, after)

    if run.timed_out:
        result.error = f"`claude update` timed out after {timeout:g}s and was killed"
    elif run.returncode != 0:
        result.error = f"`claude update` exited with status {run.returncode}"
    try:
        result.prime_enabled = load_prime_settings(root).enabled
    except Exception:
        result.prime_enabled = False
    return result


def _shown(result: UpdateResult) -> str:
    return f"{result.before or 'unknown'} -> {result.after or 'unknown'}"


def summary_line(result: UpdateResult) -> str:
    """``Claude Code updated: a -> b`` and its siblings, uncoloured."""
    if result.changed:
        return f"Claude Code updated: {_shown(result)}"
    if result.after is not None and result.ok:
        return f"Claude Code is already at {result.after} (no change)."
    return f"Claude Code version: {_shown(result)}"


# -- the command ----------------------------------------------------------------


def _emit_json(payload: dict) -> None:
    print(json.dumps({"schemaVersion": SCHEMA_VERSION, **payload}, indent=2))


def _fail(args, message: str, **fields) -> int:
    if args.json:
        _emit_json({"ok": False, "error": message, **fields})
    else:
        error(f"Error: {message}")
    return EXIT_ERROR


def _check(args, root: Path) -> int:
    c = check_versions(root)
    if c.error is not None:
        fields: dict = {}
        if c.claude is not None:
            fields["claudePath"] = c.claude
        fields.update(installed=c.installed, latest=None)
        if c.channel is not None:
            fields["channel"] = c.channel
        fields["updateAvailable"] = None
        return _fail(args, c.error, **fields)
    available = c.available
    if args.json:
        _emit_json(
            {
                "ok": True,
                "claudePath": c.claude,
                "installed": c.installed,
                "latest": c.latest,
                "channel": c.channel,
                "updateAvailable": available,
                "source": DIST_TAGS_URL,
            }
        )
    elif available:
        print(
            f"Claude Code {c.installed} is installed; {accent(c.latest)} is available "
            f"({c.channel})."
        )
        print(dimmed("Run `cc-swap claude-update` to update."))
    else:
        print(f"Claude Code {c.installed} is up to date ({c.channel}: {c.latest}).")
    return EXIT_UPDATE_AVAILABLE if available else EXIT_OK


def _update(args, root: Path) -> int:
    # --json keeps stdout a single document; claude's output goes to stderr.
    sink = sys.stderr if args.json else sys.stdout
    result = perform_update(root, timeout=args.timeout, sink=sink)
    if result.run is None:  # nothing ran: no claude, or another update holds the lock
        return _fail(args, result.error or "claude update did not run")
    run = result.run
    hint = result.prime_hint
    if args.json:
        payload = {
            "ok": result.ok,
            "claudePath": result.claude,
            "before": result.before,
            "after": result.after,
            "changed": result.changed,
            "recorded": result.recorded,
            "exitCode": run.returncode,
            "timedOut": run.timed_out,
            "primeVerifyAdvised": hint is not None,
        }
        if hint is not None:
            payload["primeVerifyCommand"] = PRIME_VERIFY_COMMAND
        if result.error:
            payload["error"] = result.error
        _emit_json(payload)
        return EXIT_ERROR if result.error else EXIT_OK

    if result.error:
        error(f"Error: {result.error}")
    if result.changed:
        print(f"Claude Code updated: {accent(_shown(result))}")
    else:
        print(summary_line(result))
    if hint:
        print(hint)
    return EXIT_ERROR if result.error else EXIT_OK


def run(argv: list[str]) -> int:
    """Parse ``argv`` and run; returns the exit code (usage errors exit 2)."""
    parser = argparse.ArgumentParser(
        prog="cc-swap claude-update",
        description=(
            "Update Claude Code with its built-in `claude update`, then show "
            "the version before and after."
        ),
        epilog=(
            "Exit status with --check: 0 up to date, 10 update available, 1 error. "
            f"The latest version is read from {DIST_TAGS_URL} "
            "(the stable or latest tag, following Claude Code's autoUpdatesChannel)."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only report the installed and latest versions; change nothing",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_UPDATE_TIMEOUT,
        metavar="SECONDS",
        help=f"Kill `claude update` after this long (default {DEFAULT_UPDATE_TIMEOUT:g})",
    )
    args = parser.parse_args(argv)
    root = get_backup_root()
    return _check(args, root) if args.check else _update(args, root)


def claude_update_command(argv: list[str]) -> None:
    """Entry point for ``cc-swap claude-update``; exits with :func:`run`'s code."""
    try:
        code = run(argv)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        code = 130
    sys.exit(code)
