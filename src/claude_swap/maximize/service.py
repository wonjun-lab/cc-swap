"""Run ``cc-swap auto`` as a per-user background service.

macOS: a launchd LaunchAgent, ``~/Library/LaunchAgents/com.wonjun-lab.cc-swap.plist``.
Linux: a systemd *user* unit, ``~/.config/systemd/user/cc-swap.service``.
Windows: refused — run ``cc-swap auto`` in a terminal there.

The principles are ``launch_agent.py``'s (the menu bar's LaunchAgent):

* The service pins the console script, never ``sys.executable``: ``uv tool
  install --force`` rebuilds the tool virtualenv but keeps the script path,
  so an upgrade needs a restart (re-run ``cc-swap service install``), not a
  new service file. The path is made absolute but not resolved — resolving
  ``~/.local/bin/cc-swap`` would pin the virtualenv-internal target.
* Logs go to ``~/Library/Logs/cc-swap/`` (macOS) or the journal (Linux),
  never ``/tmp``.
* launchd and systemd start jobs with a bare PATH that finds neither a
  Homebrew nor a ``~/.local/bin`` ``claude``. Install detects ``claude`` in
  the installing shell, puts its directory on the service PATH and saves it
  as ``prime.claudePath``, so the primer needs no PATH lookup at all. The
  ``~/.local/bin/claude`` symlink is likewise kept unresolved: Claude Code's
  installer repoints it at every new version.

The service sees only the environment the manager gives it, so install forwards
``CLAUDE_CONFIG_DIR`` and ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` from the installing
shell (and says which): a user who keeps their login in a custom profile
would otherwise get a service reading the default one. Installing from inside a
``cswap run`` session is refused — that profile belongs to one account and one
terminal, and a service pinned to it would auto-switch the wrong store.

A second engine is refused by the engine lease (``maximize/lease.py``):
``cc-swap auto`` then exits with ``EXIT_ENGINE_BUSY`` (4). Both managers
count that as a failure and retry after ``RESTART_DELAY_S``, so a service that
started while another engine held the lease takes over within a minute of that
engine letting go — without spinning, and on systemd without tripping the
start-rate limit (``StartLimitIntervalSec=0``). Once the service holds the
lease it is the one that refuses: to run an engine in a terminal instead,
uninstall the service first.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from claude_swap import launch_agent, paths
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.session import session_profile_containing
from claude_swap.settings import load_prime_settings, set_setting

LABEL = "com.wonjun-lab.cc-swap"
UNIT_NAME = "cc-swap.service"
CONSOLE_SCRIPT = "cc-swap"
RESTART_DELAY_S = 60
DOCS_URL = "https://github.com/wonjun-lab/cc-swap"
#: Profile-selecting variables the installing shell hands on to the service.
FORWARDED_ENV_VARS = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR")

_MAC_SYSTEM_DIRS = (
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
)
_LINUX_SYSTEM_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")


def _platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ClaudeSwitchError(
        "cc-swap service supports macOS (launchd) and Linux (systemd --user) "
        "only; on this platform run `cc-swap auto` in a terminal instead."
    )


# --- locations -----------------------------------------------------------------


def plist_path(home: Path | None = None) -> Path:
    return launch_agent.plist_path(LABEL, home)


def log_paths(home: Path | None = None) -> tuple[Path, Path]:
    """``(stdout, stderr)`` of the macOS agent."""
    logs = (home or Path.home()) / "Library" / "Logs" / "cc-swap"
    return logs / "auto.log", logs / "auto.err.log"


def unit_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config" / "systemd" / "user" / UNIT_NAME


# --- what to run -----------------------------------------------------------------


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_program() -> list[str]:
    """Argv prefix for the service, minus ``auto``.

    The ``cc-swap`` console script that is running (or, while ``cswap`` is
    still registered as a second script, the ``cc-swap`` beside it), then
    ``cc-swap`` on PATH, then this interpreter with ``-m claude_swap``.
    """
    candidate = sys.argv[0] if sys.argv and sys.argv[0] else None
    if candidate:
        absolute = Path(os.path.abspath(candidate))
        if absolute.name == CONSOLE_SCRIPT and absolute.is_file():
            return [str(absolute)]
        sibling = absolute.with_name(CONSOLE_SCRIPT)
        if absolute.name == "cswap" and sibling.is_file():
            return [str(sibling)]
    which = shutil.which(CONSOLE_SCRIPT)
    if which:
        return [os.path.abspath(which)]
    return [sys.executable, "-m", "claude_swap"]


def detect_claude_path(configured: str | None, *, home: Path | None = None) -> str | None:
    """The ``claude`` executable the service should prime with.

    ``configured`` (``prime.claudePath``) when it is executable, then the
    installing shell's PATH, then ``~/.local/bin/claude`` (the native
    installer's link). Paths are absolute but never resolved.
    """
    if configured:
        path = Path(os.path.expanduser(configured))
        if _is_executable(path):
            return os.path.abspath(path)
    which = shutil.which("claude")
    if which:
        return os.path.abspath(which)
    local = (home or Path.home()) / ".local" / "bin" / "claude"
    if _is_executable(local):
        return str(local)
    return None


def service_path(
    program: list[str], claude_path: str | None, *, platform: str, home: Path | None = None
) -> str:
    """PATH for the service: the program's dir, claude's dir, ~/.local/bin,
    then the platform's system dirs — de-duplicated, in that order."""
    system = _MAC_SYSTEM_DIRS if platform == "darwin" else _LINUX_SYSTEM_DIRS
    candidates = [
        str(Path(program[0]).parent),
        str(Path(claude_path).parent) if claude_path else "",
        str((home or Path.home()) / ".local" / "bin"),
        *system,
    ]
    dirs: list[str] = []
    for d in candidates:
        if d and d != "." and d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def service_env(
    program: list[str],
    claude_path: str | None,
    *,
    platform: str,
    home: Path | None = None,
    xdg_data_home: str | None = None,
    forward_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = {
        "PATH": service_path(program, claude_path, platform=platform, home=home),
        "PYTHONUNBUFFERED": "1",
    }
    if platform == "linux" and xdg_data_home:
        # The Linux backup root follows $XDG_DATA_HOME (paths.get_backup_root);
        # a user manager started without it would read a different store.
        env["XDG_DATA_HOME"] = xdg_data_home
    env.update(forward_env or {})
    return env


def _xdg_data_home() -> str | None:
    raw = os.environ.get("XDG_DATA_HOME", "")
    expanded = os.path.expanduser(raw) if raw else ""
    return expanded if expanded and os.path.isabs(expanded) else None


def forwarded_env() -> dict[str, str]:
    """The profile-selecting variables set in this process, verbatim.

    Verbatim because Claude hashes the raw ``CLAUDE_CONFIG_DIR`` string into
    its keychain service name (``session.keychain_service_name``). Empty
    ``CLAUDE_CONFIG_DIR`` is "unset" to Claude and is skipped;
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` defined-but-empty selects the default
    secure store, so it is kept.
    """
    env: dict[str, str] = {}
    for name in FORWARDED_ENV_VARS:
        value = os.environ.get(name)
        if value is None or (not value and name == "CLAUDE_CONFIG_DIR"):
            continue
        env[name] = value
    return env


def _refuse_session_profile(forward: Mapping[str, str], backup_root: Path) -> None:
    config_dir = forward.get("CLAUDE_CONFIG_DIR")
    profile = session_profile_containing(config_dir, backup_root) if config_dir else None
    if profile is not None:
        raise ClaudeSwitchError(
            f"CLAUDE_CONFIG_DIR points at a `cswap run` session profile ({profile}); "
            "a service installed from here would be pinned to that one account's "
            "profile instead of your login. Install from a terminal outside the "
            "session, or run: unset CLAUDE_CONFIG_DIR"
        )


# --- service files -----------------------------------------------------------------


def build_plist(
    program: list[str],
    *,
    claude_path: str | None,
    home: Path | None = None,
    forward_env: Mapping[str, str] | None = None,
) -> bytes:
    out_log, err_log = log_paths(home)
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [*program, "auto"],
            "RunAtLoad": True,
            # Restart a crash or a lease-busy exit (4), not a deliberate stop:
            # `launchctl bootout` sends SIGTERM, which ends `cc-swap auto` with 0.
            "KeepAlive": {"SuccessfulExit": False},
            # A lease-busy exit retries once a minute instead of every 10 s.
            "ThrottleInterval": RESTART_DELAY_S,
            # Not Background: its timer coalescing would stretch the 120 s
            # pending-switch polls the idle detection depends on.
            "ProcessType": "Standard",
            "EnvironmentVariables": service_env(
                program, claude_path, platform="darwin", home=home, forward_env=forward_env
            ),
            "StandardOutPath": str(out_log),
            "StandardErrorPath": str(err_log),
        }
    )


def _systemd_quote(value: str, *, exec_arg: bool = False) -> str:
    """One double-quoted unit-file word. ``%`` is a specifier in every
    setting; ``$`` is expanded only on Exec lines."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if exec_arg:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def build_unit(
    program: list[str],
    *,
    claude_path: str | None,
    home: Path | None = None,
    xdg_data_home: str | None = None,
    forward_env: Mapping[str, str] | None = None,
) -> str:
    env = service_env(
        program,
        claude_path,
        platform="linux",
        home=home,
        xdg_data_home=xdg_data_home,
        forward_env=forward_env,
    )
    exec_start = " ".join(_systemd_quote(arg, exec_arg=True) for arg in [*program, "auto"])
    lines = [
        "# Generated by `cc-swap service install`; re-run it instead of editing this file.",
        "[Unit]",
        "Description=cc-swap auto-switch engine (cc-swap auto)",
        f"Documentation={DOCS_URL}",
        "# A lease-busy exit (code 4) must keep retrying until the other engine stops.",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        f"RestartSec={RESTART_DELAY_S}",
        *(f"Environment={_systemd_quote(f'{k}={v}')}" for k, v in env.items()),
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"


# --- systemd helpers -----------------------------------------------------------------


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as e:
        raise ClaudeSwitchError(
            "systemctl not found; cc-swap service needs a systemd user session on Linux"
        ) from e


def _checked(*args: str) -> subprocess.CompletedProcess:
    done = _systemctl(*args)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise ClaudeSwitchError(
            f"systemctl --user {' '.join(args)} failed (exit {done.returncode})"
            + (f": {detail}" if detail else "")
        )
    return done


def _is_active() -> bool:
    return _systemctl("is-active", "--quiet", UNIT_NAME).returncode == 0


def linger_enabled(user: str | None = None) -> bool | None:
    """Whether systemd keeps this user's services after logout; None if unknown."""
    try:
        shown = subprocess.run(
            ["loginctl", "show-user", user or getpass.getuser(), "--property=Linger", "--value"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    value = shown.stdout.strip() if shown.returncode == 0 else ""
    return {"yes": True, "no": False}.get(value)


# --- public API -----------------------------------------------------------------------


def install(
    *,
    claude_path: str | None = None,
    home: Path | None = None,
    program: list[str] | None = None,
    uid: int | None = None,
    backup_root: Path | None = None,
    user: str | None = None,
) -> dict:
    """Write the service file, (re)start the service, and save ``prime.claudePath``.

    Idempotent: re-running after an upgrade reloads a running service onto
    the new build. ``claude_path`` (``--claude-path``) must be executable;
    without it the path is detected (see :func:`detect_claude_path`).
    """
    platform = _platform()
    root = backup_root or paths.get_backup_root()
    forward = forwarded_env()
    _refuse_session_profile(forward, root)
    if claude_path is not None and not _is_executable(Path(os.path.expanduser(claude_path))):
        raise ClaudeSwitchError(f"--claude-path {claude_path} is not an executable file")
    configured = load_prime_settings(root).claude_path
    resolved = (
        os.path.abspath(os.path.expanduser(claude_path))
        if claude_path
        else detect_claude_path(configured, home=home)
    )
    saved = False
    if resolved and resolved != configured:
        set_setting(root, "prime.claudePath", resolved)
        saved = True
    program = program or resolve_program()
    if platform == "darwin":
        result = _install_darwin(program, resolved, home, uid, forward)
    else:
        result = _install_linux(program, resolved, home, user, forward)
    result.update(claude_path=resolved, claude_path_saved=saved, forwarded_env=forward)
    return result


def _install_darwin(
    program: list[str],
    claude_path: str | None,
    home: Path | None,
    uid: int | None,
    forward: Mapping[str, str],
) -> dict:
    target = plist_path(home)
    out_log, err_log = log_paths(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        build_plist(program, claude_path=claude_path, home=home, forward_env=forward)
    )
    # launch_agent.install's reload dance: boot the old job out and wait until
    # launchd has dropped it, or bootstrap fails with EEXIST / "in progress".
    settled = True
    if launch_agent.is_loaded(LABEL, uid):
        launch_agent._launchctl("bootout", launch_agent.service_target(LABEL, uid))
        settled = launch_agent._wait_until_unloaded(LABEL, uid)
    booted = launch_agent._launchctl("bootstrap", launch_agent.domain_target(uid), str(target))
    if booted.returncode != 0:
        detail = (booted.stderr or booted.stdout or "").strip()
        if not settled:
            detail = f"{detail}; the previous instance was still shutting down".lstrip("; ")
        raise ClaudeSwitchError(
            f"launchctl bootstrap failed (exit {booted.returncode})"
            + (f": {detail}" if detail else "")
        )
    return {
        "platform": "darwin",
        "name": LABEL,
        "path": str(target),
        "program": [*program, "auto"],
        "logs": [str(out_log), str(err_log)],
        "linger": None,
    }


def _install_linux(
    program: list[str],
    claude_path: str | None,
    home: Path | None,
    user: str | None,
    forward: Mapping[str, str],
) -> dict:
    unit = unit_path(home)
    was_active = _is_active()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        build_unit(
            program,
            claude_path=claude_path,
            home=home,
            xdg_data_home=_xdg_data_home(),
            forward_env=forward,
        ),
        encoding="utf-8",
    )
    _checked("daemon-reload")
    _checked("enable", "--now", UNIT_NAME)
    if was_active:
        # enable --now leaves a running unit alone; move it onto the new build.
        _checked("restart", UNIT_NAME)
    return {
        "platform": "linux",
        "name": UNIT_NAME,
        "path": str(unit),
        "program": [*program, "auto"],
        "logs": [f"journalctl --user -u {UNIT_NAME} -f"],
        "linger": linger_enabled(user),
    }


def uninstall(*, home: Path | None = None, uid: int | None = None) -> dict:
    """Stop the service and delete its file; tolerant of every partial state."""
    platform = _platform()
    if platform == "darwin":
        done = launch_agent.uninstall(label=LABEL, home=home, uid=uid)
        return {
            "platform": "darwin",
            "name": LABEL,
            "was_running": done["was_loaded"],
            "removed": done["removed_plist"],
        }
    unit = unit_path(home)
    was_active = _is_active()
    _systemctl("disable", "--now", UNIT_NAME)  # "not loaded" is fine: the goal is "gone"
    if _is_active():
        raise ClaudeSwitchError(
            f"{UNIT_NAME} is still running after `systemctl --user disable --now`; "
            f"stop it with: systemctl --user stop {UNIT_NAME}"
        )
    existed = unit.exists()
    if existed:
        unit.unlink()
    _systemctl("daemon-reload")
    return {"platform": "linux", "name": UNIT_NAME, "was_running": was_active, "removed": existed}


def status(*, home: Path | None = None, uid: int | None = None) -> dict:
    platform = _platform()
    if platform == "darwin":
        shown = launch_agent.status(label=LABEL, uid=uid, home=home)
        out_log, err_log = log_paths(home)
        return {
            "platform": "darwin",
            "name": LABEL,
            "installed": shown["installed"],
            "loaded": shown["loaded"],
            "running": shown["state"] == "running",
            "state": shown["state"],
            "pid": shown["pid"],
            "path": shown["plist"],
            "logs": [str(out_log), str(err_log)],
        }
    unit = unit_path(home)
    state = _systemctl("is-active", UNIT_NAME).stdout.strip() or None
    raw_pid = _systemctl("show", UNIT_NAME, "--property=MainPID", "--value").stdout.strip()
    pid = int(raw_pid) if raw_pid.isdigit() and int(raw_pid) > 0 else None
    return {
        "platform": "linux",
        "name": UNIT_NAME,
        "installed": unit.exists(),
        "loaded": state not in (None, "inactive", "unknown"),
        "running": state == "active",
        "state": state,
        "pid": pid,
        "path": str(unit),
        "logs": [f"journalctl --user -u {UNIT_NAME} -f"],
    }
