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
(UTC ISO, ``...Z``). Read them with :func:`recorded_claude_version`. They are
written under the engine's own state lock and the engine preserves unknown
keys, so the two writers coexist. ``--check`` never writes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.autoswitch import STATE_FILENAME, STATE_SCHEMA_VERSION
from claude_swap.exceptions import LockError
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.locking import FileLock
from claude_swap.maximize.primer import resolve_claude_path
from claude_swap.paths import get_backup_root, get_claude_config_home
from claude_swap.printer import accent, dimmed, error
from claude_swap.settings import atomic_write_json, load_prime_settings

PACKAGE = "@anthropic-ai/claude-code"
DIST_TAGS_URL = f"https://registry.npmjs.org/-/package/{PACKAGE}/dist-tags"
LOCK_FILENAME = ".claude_update.lock"
DEFAULT_UPDATE_TIMEOUT = 600.0
VERSION_TIMEOUT = 30.0
LOOKUP_TIMEOUT = 10.0

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UPDATE_AVAILABLE = 10

KEY_VERSION = "claudeVersion"
KEY_PREVIOUS = "claudeVersionPrevious"
KEY_CHANGED_AT = "claudeVersionChangedAt"

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?")


# -- versions ---------------------------------------------------------------


def parse_version(text: str | None) -> str | None:
    """``2.1.287 (Claude Code)`` -> ``2.1.287``; None when unreadable."""
    match = _VERSION.search(text or "")
    return match.group(0) if match else None


def _key(version: str) -> tuple[int, int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(version)
    major, minor, patch, pre = match.groups()
    # Same number: a release outranks its pre-release (2.2.0-beta.1 < 2.2.0).
    return int(major), int(minor), int(patch), 0 if pre else 1


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


# -- the command ----------------------------------------------------------------


def _emit_json(payload: dict) -> None:
    print(json.dumps({"schemaVersion": SCHEMA_VERSION, **payload}, indent=2))


def _fail(args, message: str, **fields) -> int:
    if args.json:
        _emit_json({"ok": False, "error": message, **fields})
    else:
        error(f"Error: {message}")
    return EXIT_ERROR


_NO_CLAUDE = (
    "claude was not found (looked at prime.claudePath, ~/.local/bin/claude, then PATH); "
    "set it with: cc-swap config set prime.claudePath /path/to/claude"
)


def _check(args, root: Path) -> int:
    claude = find_claude(root)
    if claude is None:
        return _fail(args, _NO_CLAUDE, installed=None, latest=None, updateAvailable=None)
    installed = installed_version(claude)
    if installed is None:
        return _fail(
            args,
            f"could not read `{claude} --version`",
            claudePath=claude, installed=None, latest=None, updateAvailable=None,
        )
    channel = update_channel()
    latest = latest_version(channel)
    if latest is None:
        return _fail(
            args,
            "could not read the latest Claude Code version from the npm registry "
            f"({DIST_TAGS_URL})",
            claudePath=claude, installed=installed, latest=None, channel=channel,
            updateAvailable=None,
        )
    available = is_newer(latest, installed)
    if args.json:
        _emit_json(
            {
                "ok": True,
                "claudePath": claude,
                "installed": installed,
                "latest": latest,
                "channel": channel,
                "updateAvailable": available,
                "source": DIST_TAGS_URL,
            }
        )
    elif available:
        print(f"Claude Code {installed} is installed; {accent(latest)} is available ({channel}).")
        print(dimmed("Run `cc-swap claude-update` to update."))
    else:
        print(f"Claude Code {installed} is up to date ({channel}: {latest}).")
    return EXIT_UPDATE_AVAILABLE if available else EXIT_OK


def _update(args, root: Path) -> int:
    claude = find_claude(root)
    if claude is None:
        return _fail(args, _NO_CLAUDE)
    lock = FileLock(root / LOCK_FILENAME, timeout=0)
    if not lock.acquire():
        return _fail(args, "another `claude update` is already in progress; try again when it ends")
    try:
        before = installed_version(claude)
        # --json keeps stdout a single document; claude's output goes to stderr.
        sink = sys.stderr if args.json else sys.stdout
        run = run_claude_update(claude, timeout=args.timeout, sink=sink)
        after = installed_version(claude)
    finally:
        lock.release()

    changed = after is not None and after != before
    recorded = False
    if after is not None and (changed or run.ok):
        try:
            recorded = record_version(root, after, before if changed else None)
        except (OSError, LockError):
            recorded = False

    if run.timed_out:
        problem = f"`claude update` timed out after {args.timeout:g}s and was killed"
    elif run.returncode != 0:
        problem = f"`claude update` exited with status {run.returncode}"
    else:
        problem = None

    hint = changed and load_prime_settings(root).enabled
    if args.json:
        payload = {
            "ok": problem is None,
            "claudePath": claude,
            "before": before,
            "after": after,
            "changed": changed,
            "recorded": recorded,
            "exitCode": run.returncode,
            "timedOut": run.timed_out,
            "primeVerifyAdvised": hint,
        }
        if problem:
            payload["error"] = problem
        _emit_json(payload)
        return EXIT_ERROR if problem else EXIT_OK

    if problem:
        error(f"Error: {problem}")
    shown = f"{before or 'unknown'} -> {after or 'unknown'}"
    if changed:
        print(f"Claude Code updated: {accent(shown)}")
    elif after is not None and problem is None:
        print(f"Claude Code is already at {after} (no change).")
    else:
        print(f"Claude Code version: {shown}")
    if hint:
        print(
            dimmed(
                "Priming is enabled and the Claude Code version changed: "
                "run `cc-swap prime verify` before relying on it."
            )
        )
    return EXIT_ERROR if problem else EXIT_OK


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
