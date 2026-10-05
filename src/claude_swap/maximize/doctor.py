"""``cc-swap doctor``: one read-only pass that says what is wrong and what to type.

Every check returns :class:`Finding` rows — a severity, one line of detail and
one line of fix — and the CLI (``maximize/doctor_cli.py``), ``cc-swap init``
and Fleet's *Inspect all logins* modal (``tui/fleet_doctor.py``) only present them.
Environment checks come first (codex-swap's ordering rule: a broken Keychain
explains every red row below it).

Strictly read-only:

* never refreshes a token and makes no network request at all;
* never writes a credential, ``sequence.json``, ``autoswitch_state.json`` or
  ``settings.json`` (the settings are parsed with the non-logging primitives,
  and the engine lease is only probed when its file already exists);
* runs no ``claude`` other than ``claude --version``, and that one
  record-only (``claude_exec``: one audit line in ``claude-exec.jsonl`` is
  the only thing doctor ever appends — no state, no notification, no
  pause); skipped while the binary is still settling after an update, or
  while the OS kills it.

All I/O goes through :class:`Probes`, so tests replace the Keychain, the
service manager, ``ps`` and ``claude`` with fakes. Output names accounts by
slot number and credentials by an 8-hex refresh-token fingerprint prefix
(``oauth.fingerprint8``) — never a token, never an email.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from claude_swap import __version__, oauth, paths, shared_login

Severity = Literal["ok", "info", "warn", "error"]
SEVERITIES: tuple[Severity, ...] = ("ok", "info", "warn", "error")

#: ``security`` exit codes are the low byte of the ``OSStatus``.
RC_INTERACTION_NOT_ALLOWED = 36  # errSecInteractionNotAllowed (-25308)
RC_AUTH_FAILED = 51  # errSecAuthFailed (-25293)
RC_NOT_FOUND = 44  # errSecItemNotFound (-25300)
#: ``Probes.run``'s answer when a command timed out or could not start.
RC_TIMEOUT = -1
RC_NO_BINARY = -2

SECURITY = "/usr/bin/security"
KEYCHAIN_TIMEOUT_S = 5.0
CLAUDE_VERSION_TIMEOUT_S = 10.0
LOGIN_WARN_S = 7 * 86400.0

#: Keychain service of the per-slot backups (``credentials.SECURITY_SERVICE``).
BACKUP_KEYCHAIN_SERVICE = "claude-swap"
#: Upstream claude-swap's tool directory name under uv / pipx.
UPSTREAM_DIST = "claude-swap"
UPSTREAM_MENUBAR_LABEL = "com.cswap.menubar"
SERVICE_ENV = "CC_SWAP_SERVICE"


# -- findings ---------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One doctor result: ``scope`` is ``env``, ``accounts`` (all slots) or
    one slot (``#3``)."""

    check: str
    severity: Severity
    detail: str
    fix: str = ""
    scope: str = "env"

    def to_json(self) -> dict:
        return {
            "check": self.check,
            "scope": self.scope,
            "severity": self.severity,
            "detail": self.detail,
            "fix": self.fix or None,
        }


def exit_code(findings: Iterable[Finding]) -> int:
    """0 all good · 1 warnings · 2 errors."""
    levels = {f.severity for f in findings}
    if "error" in levels:
        return 2
    if "warn" in levels:
        return 1
    return 0


def counts(findings: Iterable[Finding]) -> dict[str, int]:
    out = dict.fromkeys(SEVERITIES, 0)
    for f in findings:
        out[f.severity] += 1
    return out


# -- probes (all I/O) --------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    rc: int
    stdout: str = ""
    stderr: str = ""


def _run(argv: list[str], timeout: float) -> RunResult:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return RunResult(RC_TIMEOUT)
    except OSError:
        return RunResult(RC_NO_BINARY)
    return RunResult(done.returncode, done.stdout or "", done.stderr or "")


def _run_claude(argv: list[str], timeout: float) -> RunResult:
    """``claude --version`` through ``claude_exec`` (audited, SIGKILL noticed)."""
    from claude_swap.maximize import claude_exec

    try:
        done = claude_exec.run(
            argv, caller="claude --version (doctor)", timeout=timeout,
            manual=claude_exec.Manual("cc-swap doctor"), record_only=True,
        )
    except subprocess.TimeoutExpired:
        return RunResult(RC_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return RunResult(RC_NO_BINARY)
    return RunResult(done.returncode, done.stdout or "", done.stderr or "")


def _service_status() -> dict | None:
    from claude_swap.exceptions import ClaudeSwitchError
    from claude_swap.maximize import service

    try:
        return service.status()
    except (ClaudeSwitchError, OSError):
        return None


def _lease_holder(backup_root: Path) -> tuple[bool | None, int | None]:
    """``(held, pid)``. Probes only an existing lock file, so a doctor run on
    a machine that never ran an engine creates nothing."""
    from claude_swap.maximize.lease import EngineLease

    lease = EngineLease(backup_root)
    if not lease.path.exists():
        return False, None
    try:
        held = lease.held_elsewhere()
    except OSError:
        return None, None
    return held, (lease.holder_pid() if held else None)


def _process_started_at(pid: int) -> float | None:
    from claude_swap.process_detection import process_started_at

    value = process_started_at(pid)
    return float(value) if value is not None else None


def _dist_info_dirs(venv: Path) -> list[Path]:
    found = list(venv.glob("lib/python*/site-packages/cc_swap-*.dist-info"))
    found += list(venv.glob("Lib/site-packages/cc_swap-*.dist-info"))
    return found


def _program_install(program: str) -> tuple[str | None, float | None]:
    """``(version, installed_at)`` of the cc-swap install behind a console
    script, read from its shebang's virtualenv. ``(None, None)`` if unknown."""
    try:
        with open(program, "rb") as fh:
            head = fh.read(512).decode("utf-8", "replace")
    except OSError:
        return None, None
    match = re.search(r"(/[^\s'\"]+/bin/python[0-9.]*)", head)
    if not match:
        return None, None
    venv = Path(match.group(1)).parent.parent
    dirs = _dist_info_dirs(venv)
    if len(dirs) != 1:
        return None, None
    version = dirs[0].name.removeprefix("cc_swap-").removesuffix(".dist-info")
    try:
        return version, dirs[0].stat().st_mtime
    except OSError:
        return version, None


def _current_program() -> list[str]:
    from claude_swap.maximize import service

    return service.resolve_program()


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


@dataclass
class Probes:
    """Everything doctor reads from outside the backup root and ``$HOME``."""

    backup_root: Path
    home: Path
    platform: str
    environ: Mapping[str, str]
    now: float
    run: Callable[[list[str], float], RunResult]
    which: Callable[[str], str | None]
    is_executable: Callable[[Path], bool]
    service_status: Callable[[], dict | None]
    lease_holder: Callable[[Path], tuple[bool | None, int | None]]
    process_started_at: Callable[[int], float | None]
    program_install: Callable[[str], tuple[str | None, float | None]]
    current_program: Callable[[], list[str]]
    version: str = __version__
    #: ``claude --version`` (``claude_exec``); None: :attr:`run`.
    run_claude: Callable[[list[str], float], RunResult] | None = None

    @classmethod
    def system(cls) -> Probes:
        return cls(
            backup_root=paths.get_backup_root(),
            home=Path.home(),
            platform=_platform(),
            environ=os.environ,
            now=time.time(),
            run=_run,
            which=shutil.which,
            is_executable=_is_executable,
            service_status=_service_status,
            lease_holder=_lease_holder,
            process_started_at=_process_started_at,
            program_install=_program_install,
            current_program=_current_program,
            run_claude=_run_claude,
        )


# -- small helpers ------------------------------------------------------------------


def _tilde(path: Path | str, home: Path) -> str:
    text = str(path)
    root = str(home)
    if root and root != "/" and (text == root or text.startswith(root + os.sep)):
        return "~" + text[len(root):]
    return text


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _clock(epoch_s: float) -> str:
    return oauth.local_clock(epoch_s)


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    """``(data, problem)``: ``(None, None)`` when absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"unreadable ({type(e).__name__})"
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, f"not valid JSON ({e})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, None


def _slot_key(number: str) -> tuple[int, str]:
    return (int(number), number) if number.isdigit() else (1 << 30, number)


# -- context: what every check reads once --------------------------------------------


@dataclass
class Slot:
    number: str
    email: str
    org: str
    uuid: str
    kind: str
    disabled: bool
    credentials: str | None = None  # None: absent; "" never stored
    unreadable: str | None = None  # why the stored login could not be read

    @property
    def fp(self) -> str | None:
        return oauth.credential_fingerprint(self.credentials) if self.credentials else None


@dataclass
class Profile:
    """A ``cswap run`` profile (``<backup_root>/sessions/<num>-<slug>``)."""

    owner: str  # the slot number its directory name gives
    path: Path
    credentials: str | None = None

    @property
    def rt_fp(self) -> str | None:
        return shared_login.refresh_fingerprint(self.credentials)


@dataclass
class LiveLogin:
    keychain_rc: int | None = None  # macOS only; None when not asked
    keychain_value: str | None = None
    keychain_service: str | None = None
    file_path: Path | None = None
    file_value: str | None = None
    file_mode: int | None = None
    file_problem: str | None = None
    identity: tuple[str, str, str] | None = None  # (email, org, accountUuid); never printed

    @property
    def file_mcp_only(self) -> bool:
        """The plaintext file holds only MCP logins (``mcpOAuth`` …), no
        Claude login: Claude Code keeps them there while the Keychain is
        unavailable (cc-swap fork)."""
        from claude_swap.credentials import holds_only_shared_fields

        return holds_only_shared_fields(self.file_value)

    @property
    def file_login(self) -> str | None:
        """The plaintext file, when it holds a login (not only MCP logins)."""
        return None if self.file_mcp_only else self.file_value

    @property
    def value(self) -> str | None:
        """The login Claude Code would use: the Keychain item on macOS (the
        plaintext file only when the Keychain has none), else the file. An
        unreadable Keychain yields None — the file may be a stale copy. A
        file with only MCP logins is no login."""
        if self.keychain_rc is None:
            return self.file_login
        if self.keychain_value:
            return self.keychain_value
        return self.file_login if self.keychain_rc == RC_NOT_FOUND else None


@dataclass
class Context:
    probes: Probes
    raw_settings: dict | None
    settings_problem: str | None
    sequence: dict | None
    sequence_problem: str | None
    slots: dict[str, Slot]
    state: dict
    live: LiveLogin
    profiles: list[Profile] = field(default_factory=list)
    _service: tuple[dict | None] | None = field(default=None, repr=False)

    def service_status(self) -> dict | None:
        """The service manager's answer, asked once per doctor run."""
        if self._service is None:
            p = self.probes
            self._service = (
                p.service_status() if p.platform in ("darwin", "linux") else None,
            )
        return self._service[0]

    @property
    def strategy(self) -> str:
        section = (self.raw_settings or {}).get("autoswitch")
        value = section.get("strategy") if isinstance(section, dict) else None
        return value if isinstance(value, str) else "best"

    @property
    def prime_section(self) -> dict:
        section = (self.raw_settings or {}).get("prime")
        return section if isinstance(section, dict) else {}

    @property
    def prime_enabled(self) -> bool:
        return self.prime_section.get("enabled") is True

    @property
    def prime_auto_verify(self) -> bool:
        from claude_swap import settings as st

        try:
            return st.prime_from_raw(self.prime_section, []).auto_verify
        except Exception:
            return True

    @property
    def claude_path_setting(self) -> str | None:
        value = self.prime_section.get("claudePath")
        return value if isinstance(value, str) and value else None


def _keychain_read(p: Probes, service: str, account: str) -> tuple[int, str | None]:
    done = p.run(
        [SECURITY, "find-generic-password", "-a", account, "-w", "-s", service],
        KEYCHAIN_TIMEOUT_S,
    )
    value = done.stdout.removesuffix("\n") if done.rc == 0 else ""
    if done.rc == 0 and value:
        return 0, value
    return (RC_NOT_FOUND if done.rc == 0 else done.rc), None


def _keychain_account(p: Probes) -> str:
    user = p.environ.get("USER")
    if user:
        return user
    from claude_swap.macos_keychain import keychain_account_name

    return keychain_account_name()


def _active_keychain_services(p: Probes) -> list[str]:
    """``credentials._active_oauth_keychain_services`` against ``p.environ``."""
    from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE
    from claude_swap.session import keychain_service_name

    secure = p.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if secure is not None:
        return [keychain_service_name(secure)] if secure else [CLAUDE_CODE_KEYCHAIN_SERVICE]
    config_dir = p.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        return [CLAUDE_CODE_KEYCHAIN_SERVICE]
    services = [keychain_service_name(config_dir)]
    try:
        if Path(config_dir).resolve() == (p.home / ".claude").resolve():
            services.append(CLAUDE_CODE_KEYCHAIN_SERVICE)
    except OSError:
        pass
    return services


def _config_home(p: Probes) -> Path:
    env = p.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else p.home / ".claude"


def _global_config_path(p: Probes) -> Path:
    legacy = _config_home(p) / ".config.json"
    if legacy.exists():
        return legacy
    env = p.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(env) if env else p.home) / ".claude.json"


def _read_live(p: Probes) -> LiveLogin:
    live = LiveLogin()
    if p.platform == "darwin":
        account = _keychain_account(p)
        for service in _active_keychain_services(p):
            rc, value = _keychain_read(p, service, account)
            live.keychain_rc, live.keychain_service = rc, service
            if rc == 0 and value:
                live.keychain_value = value
                break
            if rc != RC_NOT_FOUND:
                break  # unreadable is a property of the Keychain, not the item
    cred_file = _config_home(p) / ".credentials.json"
    live.file_path = cred_file
    try:
        live.file_mode = cred_file.stat().st_mode & 0o777
        text = cred_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        live.file_mode = None
    except OSError as e:
        live.file_problem = type(e).__name__
    else:
        live.file_value = text if text.strip() else None
    config, _problem = _read_json(_global_config_path(p))
    account_info = (config or {}).get("oauthAccount")
    if isinstance(account_info, dict) and account_info.get("emailAddress"):
        live.identity = (
            str(account_info.get("emailAddress")),
            str(account_info.get("organizationUuid") or ""),
            str(account_info.get("accountUuid") or ""),
        )
    return live


def _read_slot_credentials(p: Probes, slot: Slot) -> None:
    enc = p.backup_root / "credentials" / f".creds-{slot.number}-{slot.email}.enc"
    try:
        encoded = enc.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        encoded = ""
    except OSError as e:
        slot.unreadable = f"backup file unreadable ({type(e).__name__})"
        encoded = ""
    if encoded:
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            decoded = ""
        if decoded:
            slot.credentials = decoded
            return
    if p.platform == "darwin":
        rc, value = _keychain_read(
            p, BACKUP_KEYCHAIN_SERVICE, f"account-{slot.number}-{slot.email}"
        )
        if rc == 0 and value:
            slot.credentials = value
            slot.unreadable = None
        elif rc not in (0, RC_NOT_FOUND):
            slot.unreadable = f"Keychain rc={rc}"


def _read_profile_credentials(p: Probes, path: Path) -> str | None:
    """A ``cswap run`` profile's login the way Claude reads it: on macOS its
    hashed Keychain item first, else its ``.credentials.json``."""
    if p.platform == "darwin":
        from claude_swap.session import keychain_service_name

        rc, value = _keychain_read(p, keychain_service_name(str(path)), _keychain_account(p))
        if rc == 0 and value:
            return value
    try:
        text = (path / ".credentials.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return text if text.strip() else None


def gather(p: Probes) -> Context:
    raw_settings, settings_problem = _read_json(p.backup_root / "settings.json")
    sequence, sequence_problem = _read_json(p.backup_root / "sequence.json")
    state, _ = _read_json(p.backup_root / "autoswitch_state.json")
    slots: dict[str, Slot] = {}
    accounts = (sequence or {}).get("accounts")
    for number, rec in (accounts.items() if isinstance(accounts, dict) else ()):
        if not isinstance(rec, dict):
            continue
        slot = Slot(
            number=str(number),
            email=str(rec.get("email") or ""),
            org=str(rec.get("organizationUuid") or ""),
            uuid=str(rec.get("uuid") or ""),
            kind="api_key" if rec.get("kind") == "api_key" else "oauth",
            disabled=bool(rec.get("disabled")),
        )
        _read_slot_credentials(p, slot)
        slots[slot.number] = slot
    return Context(
        probes=p,
        raw_settings=raw_settings,
        settings_problem=settings_problem,
        sequence=sequence,
        sequence_problem=sequence_problem,
        slots=dict(sorted(slots.items(), key=lambda kv: _slot_key(kv[0]))),
        state=state or {},
        live=_read_live(p),
        profiles=[
            Profile(owner, path, _read_profile_credentials(p, path))
            for owner, path in shared_login.session_profiles(p.backup_root)
        ],
    )


# -- checks: environment ------------------------------------------------------------


def resolve_claude(ctx: Context) -> tuple[str | None, str | None]:
    """``(path, configured_problem)``: ``prime.claudePath`` → PATH →
    ``~/.local/bin/claude``, the order ``service install`` uses."""
    p = ctx.probes
    problem = None
    configured = ctx.claude_path_setting
    if configured:
        path = Path(os.path.expanduser(configured))
        if p.is_executable(path):
            return str(path), None
        problem = f"prime.claudePath {configured} is not an executable file"
    found = p.which("claude")
    if found:
        return found, problem
    local = p.home / ".local" / "bin" / "claude"
    if p.is_executable(local):
        return str(local), problem
    return None, problem


def check_claude(ctx: Context) -> list[Finding]:
    p = ctx.probes
    out: list[Finding] = []
    path, problem = resolve_claude(ctx)
    if problem:
        out.append(Finding(
            "claude", "error" if ctx.prime_enabled else "warn", problem,
            "cc-swap config set prime.claudePath <path to claude> "
            "(or cc-swap config unset prime.claudePath)",
        ))
    if path is None:
        out.append(Finding(
            "claude", "error" if ctx.prime_enabled else "warn",
            "claude not found (prime.claudePath, PATH, ~/.local/bin/claude)",
            "install Claude Code, or cc-swap config set prime.claudePath <path>",
        ))
        return out
    from claude_swap.maximize import claude_exec

    binary = claude_exec.stat_binary(path)
    if claude_exec.current_killed(p.backup_root) is not None and (
        claude_exec.killed_entry(p.backup_root, binary) is not None
    ):
        return out  # check_claude_exec says it: running it would be killed again
    left = claude_exec.settle_left(binary, claude_exec.settle_seconds(p.backup_root), p.now)
    if left > 0:
        out.append(Finding(
            "claude", "info",
            f"{_tilde(path, p.home)} changed moments ago; --version not run until it "
            f"settles ({left:.0f}s left, claude.settleS)",
        ))
        return out
    done = (p.run_claude or p.run)([path, "--version"], CLAUDE_VERSION_TIMEOUT_S)
    version = done.stdout.strip().splitlines()[0] if done.rc == 0 and done.stdout.strip() else ""
    if done.rc != 0 or not version:
        out.append(Finding(
            "claude", "warn",
            f"{_tilde(path, p.home)} --version failed "
            f"({'timed out' if done.rc == RC_TIMEOUT else f'exit {done.rc}'})",
            "reinstall Claude Code, or point prime.claudePath at a working claude",
        ))
    else:
        out.append(Finding("claude", "ok", f"{_tilde(path, p.home)} ({version})"))
    return out


def check_claude_exec(ctx: Context) -> list[Finding]:
    """What ``claude_exec`` recorded about the ``claude`` cc-swap runs: the
    OS killing it at launch (an error, with the fix, never applied here), a
    binary rewritten in place after cc-swap ran it, an update still
    settling. Reads ``claude_exec_state.json`` only."""
    from claude_swap.maximize import claude_exec

    p = ctx.probes
    state = claude_exec.load_state(p.backup_root)
    out: list[Finding] = []
    # Only while its launcher path still resolves to the killed file: a new
    # version (a new real path) or the fix retires it.
    killed = claude_exec.current_killed(p.backup_root)
    if killed is not None:
        real = str(killed.get("real") or killed.get("path") or "claude")
        diag = killed.get("diagnostics")
        more = f"; diagnostics: {_tilde(diag, p.home)}" if diag else ""
        times = int(killed.get("count") or 1)
        when = _clock(float(killed.get("lastAt") or killed.get("at") or p.now))
        who = "macOS" if p.platform == "darwin" else "the OS"
        why = " (code-signing cache)" if p.platform == "darwin" else ""
        out.append(Finding(
            "claude-exec", "error",
            f"{who} is killing {real} at launch{why}. Fix: "
            f"{claude_exec.fix_command(real)} — SIGKILL {times}x, last at {when}; "
            f"priming is paused{more}",
            f"{claude_exec.fix_command(real)} (cc-swap never runs it for you)",
        ))
    rewritten = state.get("rewritten")
    at = rewritten.get("at") if isinstance(rewritten, dict) else None
    if (
        isinstance(at, (int, float)) and rewritten.get("sameInode") is True
        and 0 <= p.now - at < claude_exec.REWRITE_RECENT_S
    ):
        ran = rewritten.get("execAt")
        ran_text = _clock(float(ran)) if isinstance(ran, (int, float)) else "?"
        how = "in place (same inode)"
        out.append(Finding(
            "claude-exec", "warn",
            f"claude binary at {rewritten.get('real')} was rewritten {how} at {_clock(float(at))} "
            f"after cc-swap had executed it (at {ran_text}); macOS may kill it at launch",
            "if claude dies with SIGKILL (exit 137): cc-swap doctor names the fix",
        ))
    if killed is None:
        external = _external_kills(p, claude_exec.current_external(p.backup_root))
        if external is not None:
            out.append(external)
        note = claude_exec.display_note(p.backup_root, p.now)
        if note is not None:
            out.append(Finding("claude-exec", "info", f"the engine is {note}"))
    return out


def _external_kills(p: Probes, ext: dict | None) -> Finding | None:
    """Kills of the current ``claude`` that cc-swap did not launch
    (``claude_exec.note_external_kill``): who launched them, how often, and
    what the engine's own ``claude --version`` said. Never a pause: a probe
    the OS kills becomes the killed-by-the-OS error above instead."""
    from claude_swap.maximize import claude_exec

    if not isinstance(ext, dict):
        return None
    first, last = ext.get("firstAt"), ext.get("lastAt")
    if not isinstance(last, (int, float)) or not isinstance(first, (int, float)):
        return None
    if p.now - last >= claude_exec.EXTERNAL_RECENT_S:
        return None
    n = claude_exec.external_count(ext)
    launchers = ext.get("launchers") if isinstance(ext.get("launchers"), dict) else {}
    apps = sorted(launchers, key=lambda a: (-int(launchers[a] or 0), a))
    by = f"launched by {', '.join(apps)}" if apps else "not launched by cc-swap"
    version = ext.get("version") or os.path.basename(str(ext.get("real") or "")) or "claude"
    probe = ext.get("probe") if isinstance(ext.get("probe"), dict) else {}
    result, probed = probe.get("result"), probe.get("at")
    when = _clock(float(probed)) if isinstance(probed, (int, float)) else "?"
    if result == "ok":
        verdict = f"cc-swap's own launches work (claude --version ran fine at {when})"
    elif result == "running" and claude_exec.probe_stale(ext, p.now):
        verdict = (
            f"the engine's claude --version check started at {when} never finished "
            "(its engine stopped?); a running engine checks again; priming is not paused"
        )
    elif result == "running":
        verdict = (
            f"the engine is checking claude --version now (since {when}); priming is "
            "not paused meanwhile"
        )
    elif isinstance(ext.get("probeDueAt"), (int, float)):
        verdict = (
            "the engine checks claude --version itself shortly; priming is not "
            "paused meanwhile"
        )
    elif result:
        verdict = f"the engine's claude --version check at {when}: {result}"
    else:
        verdict = "no check by the engine yet (it runs while an engine runs)"
    real = _tilde(str(ext.get("real") or ext.get("path") or "claude"), p.home)
    provenance = ext.get("provenance") if isinstance(ext.get("provenance"), dict) else None
    app = apps[0] if apps else "the app that launched it"
    if provenance:
        err = provenance.get("error")
        fix = (
            f"macOS's AppleSystemPolicy could not apply its provenance sandbox to claude "
            f"launched by {app} (ASP error {err if err is not None else '?'}): a problem "
            f"between macOS and {app}, not cc-swap — quit and reopen {app}. If claude "
            f"also dies when you run it yourself: {claude_exec.fix_command(real)}"
        )
    else:
        fix = (
            f"if it keeps happening, quit and reopen {app}; if claude also dies when you "
            f"run it yourself: {claude_exec.fix_command(real)}"
        )
    severity = "warn" if p.now - last < claude_exec.EXTERNAL_WARN_S else "info"
    times = f"{n} time{'s' if n != 1 else ''}"
    asp = "; macOS logged 'ASP: Unable to apply provenance sandbox' with it" if provenance else ""
    return Finding(
        "claude-exec", severity,
        f"macOS killed claude {version} {by} {times} (first {_clock(float(first))}, "
        f"last {_clock(float(last))}){asp}; {verdict}",
        fix,
    )


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def check_codesign_kills(ctx: Context) -> list[Finding]:
    """macOS: crash reports of the last week in which the OS killed a native
    ``claude`` for an invalid code signature, per version — count, first and
    last kill, and the latest ``claude`` cc-swap launched before the first
    (``claude-exec.jsonl``), so a correlation shows. Read-only."""
    from claude_swap.maximize import claude_exec, codesign_watch

    p = ctx.probes
    if p.platform != "darwin":
        return []
    out: list[Finding] = []
    for e in codesign_watch.episodes(codesign_watch.scan_crash_reports(p.home, p.now)):
        launch = claude_exec.last_launch_before(p.backup_root, e["first"])
        if launch is not None:
            before = (
                f"latest cc-swap claude launch before the first: {launch.get('caller')} "
                f"at {_when(float(launch['at']))} ({e['first'] - float(launch['at']):.0f}s before)"
            )
        else:
            before = "no cc-swap claude launch recorded before it"
        n = e["count"]
        launched = f"; launched by {', '.join(e['parents'])}" if e.get("parents") else ""
        out.append(Finding(
            "codesign-kills", "warn",
            f"macOS killed claude {e['version']} {n} time{'s' if n != 1 else ''} at launch "
            f"(SIGKILL, Code Signature Invalid; ~/Library/Logs/DiagnosticReports): first "
            f"{_when(e['first'])}, last {_when(e['last'])}{launched}; {before}",
            f"if claude {e['version']} still dies at launch: "
            f"{claude_exec.fix_command(_tilde(e['path'], p.home))}",
        ))
    return out


_KEYCHAIN_MEANINGS = {
    RC_INTERACTION_NOT_ALLOWED: (
        "rc=36 errSecInteractionNotAllowed: the login keychain is locked, this "
        "session cannot reach it (SSH, or launchd before you logged in), or a "
        "/login is still rewriting the item",
        "unlock it (security unlock-keychain ~/Library/Keychains/login.keychain-db) "
        "or wait a minute after /login; the engine holds until a read succeeds",
    ),
    RC_AUTH_FAILED: (
        "rc=51 errSecAuthFailed: the Keychain refused /usr/bin/security (the "
        "item's access list or the keychain password no longer allows it)",
        "Keychain Access → the 'Claude Code-credentials' item → Access Control: "
        "allow /usr/bin/security, or run claude and /login again",
    ),
    RC_TIMEOUT: (
        "the Keychain did not answer within 5 s (a prompt nobody can see?)",
        "unlock the login keychain in a GUI session, then run cc-swap doctor again",
    ),
    RC_NO_BINARY: (
        "/usr/bin/security could not be started",
        "check that /usr/bin/security exists and is executable",
    ),
}


def check_keychain(ctx: Context) -> list[Finding]:
    p, live = ctx.probes, ctx.live
    if p.platform != "darwin":
        return []
    rc = live.keychain_rc
    if rc == 0 and live.keychain_value:
        return [Finding(
            "keychain", "ok",
            f"live login readable from the Keychain "
            f"(rt {oauth.fingerprint8(live.keychain_value)})",
        )]
    if rc == RC_NOT_FOUND:
        if live.file_login:
            return []  # check_plaintext reports the plaintext-only login
        if live.identity is None:
            return [Finding(
                "keychain", "warn", "no live login (rc=44: no Keychain item)",
                "run claude and /login",
            )]
        return [Finding(
            "keychain", "error",
            "~/.claude.json names an account but the Keychain has no login "
            "(rc=44: item not found)",
            "run claude and /login again",
        )]
    meaning, fix = _KEYCHAIN_MEANINGS.get(
        rc if rc is not None else RC_NO_BINARY,
        (f"rc={rc}: the Keychain read failed",
         "run: security find-generic-password -s 'Claude Code-credentials' "
         ">/dev/null; echo $?"),
    )
    return [Finding("keychain", "error", f"live login unreadable — {meaning}", fix)]


def check_plaintext(ctx: Context) -> list[Finding]:
    p, live = ctx.probes, ctx.live
    shown = _tilde(live.file_path or "", p.home)
    if live.file_problem:
        return [Finding(
            "plaintext", "error", f"{shown} exists but is unreadable ({live.file_problem})",
            f"check its owner and mode: ls -l {shown}",
        )]
    if live.file_mcp_only:
        return _plaintext_mcp_only(p, live, shown)
    if live.file_value is None:
        if p.platform != "darwin" and live.identity is not None:
            return [Finding(
                "plaintext", "error",
                f"~/.claude.json names an account but {shown} is missing",
                "run claude and /login again",
            )]
        if p.platform != "darwin":
            return [Finding("plaintext", "warn", "no live login", "run claude and /login")]
        return []
    fp = oauth.fingerprint8(live.file_value)
    if p.platform != "darwin":
        if live.file_mode is not None and live.file_mode & 0o077:
            return [Finding(
                "plaintext", "warn",
                f"{shown} has mode {live.file_mode:04o}; other users can read it",
                f"chmod 600 {shown}",
            )]
        return [Finding("plaintext", "ok", f"live login readable from {shown} (rt {fp})")]
    fix = (
        f"once claude works, move it aside: mv {shown} {shown}.bak "
        "(Claude Code reads the Keychain first)"
    )
    if live.keychain_value:
        same = oauth.credential_fingerprint(live.file_value) == oauth.credential_fingerprint(
            live.keychain_value
        )
        if same:
            return [Finding(
                "plaintext", "warn",
                f"plaintext {shown} duplicates the Keychain login (rt {fp})", fix,
            )]
        return [Finding(
            "plaintext", "warn",
            f"plaintext {shown} differs from the Keychain (rt {fp} vs "
            f"{oauth.fingerprint8(live.keychain_value)}): a stale copy the engine "
            "would fall back to while the Keychain is unreadable",
            fix,
        )]
    if live.keychain_rc == RC_NOT_FOUND:
        return [Finding(
            "plaintext", "warn",
            f"the live login exists only as plaintext {shown} (rt {fp}); "
            "the Keychain has none",
            "run claude and /login again so Claude Code stores it in the Keychain",
        )]
    return [Finding(
        "plaintext", "info",
        f"plaintext fallback {shown} present (rt {fp}); while the Keychain is "
        "unreadable the engine treats it as possibly stale and holds",
        "fix the Keychain finding above first",
    )]


def _plaintext_mcp_only(p: Probes, live: LiveLogin, shown: str) -> list[Finding]:
    """The plaintext file holds only MCP logins (``mcpOAuth`` …), no Claude
    login: nothing stale to warn about. On macOS Claude Code wrote it while
    the Keychain was unavailable and reads the Keychain first; elsewhere the
    file is the login store, so there is no login."""
    what = f"{shown} holds only MCP logins (no Claude login)"
    if p.platform == "darwin":
        return [Finding(
            "plaintext", "info",
            f"{what}: Claude Code keeps them there while the Keychain is unavailable",
        )]
    if live.identity is not None:
        return [Finding(
            "plaintext", "error",
            f"~/.claude.json names an account but {what}",
            "run claude and /login again",
        )]
    return [Finding("plaintext", "warn", f"no live login: {what}", "run claude and /login")]


def _slot_for_identity(ctx: Context) -> str | None:
    if ctx.live.identity is None:
        return None
    email, org, uuid = ctx.live.identity
    for slot in ctx.slots.values():
        if slot.email == email and slot.org == org:
            return slot.number
    if uuid:
        for slot in ctx.slots.values():
            if slot.uuid == uuid and slot.org == org:
                return slot.number
    return None


def live_slot(ctx: Context) -> str | None:
    """The slot the live login belongs to (by ``~/.claude.json`` identity)."""
    return _slot_for_identity(ctx)


#: Appended to every shared-login detail (the engine side: shared_login.py).
_SHARED_TAIL = (
    "a refresh token is used once, so refreshing one copy logs the other "
    "out — cc-swap does not refresh {} until one is re-logged"
)


def check_live_login(ctx: Context) -> list[Finding]:
    live = ctx.live
    if live.identity is None:
        if live.value:
            return [Finding(
                "live-login", "warn",
                "a live credential exists but ~/.claude.json names no account",
                "run claude and /login again",
            )]
        return []  # check_keychain / check_plaintext already said "no live login"
    number = _slot_for_identity(ctx)
    live_fp = oauth.credential_fingerprint(live.value) if live.value else None
    if number is None:
        owner = next(
            (s.number for s in ctx.slots.values() if live_fp and s.fp == live_fp), None
        )
        if owner:
            return [Finding(
                "live-login", "error",
                f"the live token is #{owner}'s login, but ~/.claude.json names an "
                f"account no slot has; {_SHARED_TAIL.format(f'#{owner}')}",
                "run claude and /login as the account you want, then cc-swap add; "
                f"or re-login #{owner}: cc-swap login {owner}",
            )]
        return [Finding(
            "live-login", "warn",
            "the live login belongs to no slot: the engine holds (unmanaged login)",
            "cc-swap add (adds the live login to a slot)",
        )]
    slot = ctx.slots[number]
    if live_fp:
        other = next(
            (s.number for s in ctx.slots.values()
             if s.number != number and s.fp and s.fp == live_fp),
            None,
        )
        if other:
            return [Finding(
                "live-login", "error",
                f"~/.claude.json names #{number} but the live token is #{other}'s "
                f"login; {_SHARED_TAIL.format(f'#{number} or #{other}')}",
                shared_login.fix([other, number]),
            )]
    if slot.credentials is None and slot.unreadable is None:
        return [Finding(
            "live-login", "warn",
            f"the live login is #{number}, which has no stored backup",
            f"cc-swap add --slot {number}",
        )]
    active = (ctx.sequence or {}).get("activeAccountNumber")
    note = ""
    if active is not None and str(active) != number:
        note = f"; sequence.json still says #{active} (corrected on the next switch)"
    if live_fp and slot.fp and slot.fp != live_fp:
        return [Finding(
            "live-login", "info",
            f"the live login is #{number}; its backup is another generation "
            f"(rt {oauth.fingerprint8(slot.credentials)} vs "
            f"{oauth.fingerprint8(live.value)}){note}",
            "nothing to do unless a re-login just happened: then cc-swap add "
            f"--slot {number}",
        )]
    return [Finding("live-login", "ok", f"the live login is #{number}{note}")]


_UPSTREAM_PATH = re.compile(
    rf"[/\\](?:tools|venvs)[/\\]{re.escape(UPSTREAM_DIST)}[/\\]", re.IGNORECASE
)


def _upstream_dirs(p: Probes) -> list[Path]:
    uv = p.environ.get("UV_TOOL_DIR")
    pipx = p.environ.get("PIPX_HOME")
    candidates = [
        Path(uv) if uv else p.home / ".local" / "share" / "uv" / "tools",
        Path(pipx) / "venvs" if pipx else p.home / ".local" / "pipx" / "venvs",
        p.home / ".local" / "share" / "pipx" / "venvs",
    ]
    seen: list[Path] = []
    for root in candidates:
        path = root / UPSTREAM_DIST
        if path not in seen and path.is_dir():
            seen.append(path)
    return seen


def check_upstream(ctx: Context) -> list[Finding]:
    p = ctx.probes
    out: list[Finding] = []
    for path in _upstream_dirs(p):
        tool = "pipx" if "pipx" in path.parts else "uv tool"
        out.append(Finding(
            "upstream", "warn",
            f"upstream claude-swap is still installed ({_tilde(path, p.home)})",
            f"{tool} uninstall claude-swap",
        ))
    if p.platform in ("darwin", "linux"):
        done = p.run(["ps", "-axo", "pid=,command="], 5.0)
        if done.rc == 0:
            for line in done.stdout.splitlines():
                pid, _, command = line.strip().partition(" ")
                if pid.isdigit() and _UPSTREAM_PATH.search(command):
                    out.append(Finding(
                        "upstream", "error",
                        f"upstream claude-swap is running (pid {pid}): two engines "
                        "fight over the active login",
                        f"quit it (kill {pid}), then uninstall claude-swap",
                    ))
    if p.platform == "darwin":
        plist = p.home / "Library" / "LaunchAgents" / f"{UPSTREAM_MENUBAR_LABEL}.plist"
        program = _plist_program(plist)
        if program and _UPSTREAM_PATH.search(_realpath(program[0])):
            out.append(Finding(
                "upstream", "error",
                f"the upstream claude-swap menu bar LaunchAgent is installed "
                f"({_tilde(plist, p.home)})",
                f"launchctl bootout gui/$(id -u)/{UPSTREAM_MENUBAR_LABEL}; rm {_tilde(plist, p.home)}",
            ))
    if not out:
        out.append(Finding("upstream", "ok", "no upstream claude-swap installed or running"))
    return out


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _plist_data(path: Path) -> dict | None:
    from claude_swap.maximize import service

    return service._plist_data(path)


def _plist_program(path: Path) -> list[str] | None:
    data = _plist_data(path)
    program = (data or {}).get("ProgramArguments")
    if isinstance(program, list) and program and all(isinstance(a, str) for a in program):
        return program
    return None


def service_file(p: Probes) -> tuple[list[str] | None, dict[str, str]] | None:
    """``(program argv minus 'auto', environment)`` from the installed service
    file, or None when there is none."""
    from claude_swap.maximize import service

    return service.read_installed(platform=p.platform, home=p.home)


def check_service(ctx: Context) -> list[Finding]:
    p = ctx.probes
    if p.platform not in ("darwin", "linux"):
        return [Finding("service", "info", "no service on this platform; run cc-swap auto in a terminal")]
    status = ctx.service_status()
    if status is None:
        return [Finding(
            "service", "warn", "could not ask the service manager about cc-swap",
            "cc-swap service status",
        )]
    installed, running = bool(status.get("installed")), bool(status.get("running"))
    if not installed and not status.get("loaded"):
        return [Finding(
            "service", "info",
            "service not installed: nothing switches unless an engine runs elsewhere",
            "cc-swap service install",
        )]
    if not installed:
        return [Finding(
            "service", "warn", "the service manager still runs cc-swap but its file is gone",
            "cc-swap service uninstall, then cc-swap service install",
        )]
    out: list[Finding] = []
    pid = status.get("pid") if isinstance(status.get("pid"), int) else None
    if not running:
        from claude_swap.maximize.service import state_text

        out.append(Finding(
            "service", "warn",
            f"service installed but not running ({state_text(status)})",
            "cc-swap service install (restarts it); then cc-swap service status",
        ))
    parsed = service_file(p)
    program, env = parsed if parsed else (None, {})
    stale_fix = "cc-swap service install (rewrites the service file and restarts it)"
    if parsed is not None:
        if SERVICE_ENV not in env:
            out.append(Finding(
                "service", "warn",
                f"service file predates cc-swap 0.2.0 (no {SERVICE_ENV}): no log "
                "rotation, no restart on a stuck Keychain hold",
                stale_fix,
            ))
        if not program:
            out.append(Finding("service", "error", "service file names no program", stale_fix))
        elif program[0] != sys.executable and not p.is_executable(Path(program[0])):
            out.append(Finding(
                "service", "error",
                f"service runs {_tilde(program[0], p.home)}, which no longer exists",
                stale_fix,
            ))
        else:
            current = p.current_program()
            if current and current[0] != program[0] and Path(current[0]).name == "cc-swap":
                out.append(Finding(
                    "service", "warn",
                    f"service runs {_tilde(program[0], p.home)} but this shell runs "
                    f"{_tilde(current[0], p.home)}",
                    "cc-swap service install from the shell whose cc-swap you want",
                ))
            version, installed_at = p.program_install(program[0])
            if version and version != p.version:
                out.append(Finding(
                    "service", "warn",
                    f"service is pinned to cc-swap {version}, this shell runs {p.version}",
                    stale_fix,
                ))
            started = p.process_started_at(pid) if (pid and running) else None
            if started is not None and installed_at is not None and started < installed_at - 1:
                out.append(Finding(
                    "service", "warn",
                    f"service process (pid {pid}) started before cc-swap was last "
                    "installed: it still runs the old code",
                    stale_fix,
                ))
        for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
            if env.get(name) != p.environ.get(name):
                out.append(Finding(
                    "service", "warn",
                    f"service and this shell disagree on {name} "
                    f"({env.get(name) or 'unset'} vs {p.environ.get(name) or 'unset'}): "
                    "they read different logins",
                    f"cc-swap service install from the shell with the right {name}",
                ))
    if running and not any(f.severity in ("warn", "error") for f in out):
        out.append(Finding(
            "service", "ok", f"service running (pid {pid or '?'}) on cc-swap {p.version}",
        ))
    return out


def check_lease(ctx: Context) -> list[Finding]:
    p = ctx.probes
    held, holder = p.lease_holder(p.backup_root)
    status = ctx.service_status()
    service_pid = (status or {}).get("pid")
    service_running = bool((status or {}).get("running"))
    out: list[Finding] = []
    if held is None:
        out.append(Finding("lease", "info", "could not probe the engine lease"))
    elif held:
        if holder is not None and holder == service_pid:
            out.append(Finding("lease", "ok", f"the service (pid {holder}) holds the engine lease"))
        elif service_running:
            out.append(Finding(
                "lease", "warn",
                f"pid {holder or '?'} holds the engine lease, so the service only waits",
                "stop that engine (a terminal cc-swap auto, a TUI Mode engine or the "
                "menu bar's auto-switch); the service takes over within a minute",
            ))
        else:
            out.append(Finding(
                "lease", "info", f"an engine runs outside the service (pid {holder or '?'})",
            ))
    elif service_running:
        out.append(Finding(
            "lease", "warn", "the service runs but holds no engine lease (starting or crash-looping)",
            "read the service log (cc-swap service status lists it)",
        ))
    else:
        out.append(Finding(
            "lease", "info", "no engine is running: nothing switches automatically",
            "cc-swap service install"
            if p.platform in ("darwin", "linux")
            else "run cc-swap auto in a terminal",
        ))
    from claude_swap.maximize.pause import active_pause, effective_auto_off

    paused = active_pause(ctx.state, p.now)
    if paused is not None:
        until, why = paused
        out.append(Finding(
            "lease", "info",
            f"switching is paused for {duration(until - p.now)} more ({why})",
            "finish or cancel the Fleet re-login; the pause ends by itself",
        ))
    off = effective_auto_off(p.backup_root, ctx.state)
    if off is not None:
        since = f" since {_clock(off.since)}" if off.since is not None else ""
        by = f" by {off.by}" if off.by else ""
        out.append(Finding(
            "lease", "info",
            f"auto-switching is OFF{since}{by}: the engine decides but never switches or primes",
            "cc-swap auto on (or Fleet: m → o)",
        ))
    return out


def check_hold(ctx: Context) -> list[Finding]:
    """An account hold (``cc-swap hold``; info only): the active account it
    pins and until when, or one left over on a slot that is no longer
    active (no engine has cleared it yet). Writes nothing."""
    from claude_swap import settings as st
    from claude_swap.maximize import hold as account_hold

    p = ctx.probes
    found = account_hold.read_hold(p.backup_root, now=p.now, state=ctx.state)
    if found is None:
        return []
    # The active slot the way the CLI, Fleet and the engine resolve it: the
    # live login first, sequence.json only when nobody is logged in.
    active = account_hold.live_slot(p.backup_root, config_path=_global_config_path(p))
    if found.slot != active:
        return [Finding(
            "hold", "info",
            f"a hold on #{found.slot} is left over; #{active or '?'} is the active account, "
            "so it no longer applies (the engine clears it on its next tick)",
            "cc-swap hold off",
        )]
    if account_hold.moved_away(p.backup_root, found):
        return [Finding(
            "hold", "info",
            f"a hold on #{found.slot} no longer applies: the active account changed since it "
            "was set (switches.jsonl); the engine clears it on its next tick",
            "cc-swap hold off",
        )]
    try:
        mx = st._section_from_raw(
            (ctx.raw_settings or {}).get("maximize"), "maximize", st.MaximizeSettings
        )
    except TypeError:
        mx = st.MaximizeSettings()
    return [Finding(
        "hold", "info",
        f"holding #{found.slot} {account_hold.until_text(found, p.now)}: soft, preempt and "
        f"rebalance moves wait — {account_hold.safety_text(mx.hard_5h, mx.hard_7d)} "
        "(cc-swap hold off lifts it)",
    )]


def check_settings(ctx: Context) -> list[Finding]:
    from claude_swap import settings as st

    p = ctx.probes
    shown = _tilde(p.backup_root / "settings.json", p.home)
    if ctx.settings_problem:
        return [Finding(
            "settings", "error", f"{shown} is {ctx.settings_problem}; every engine uses defaults",
            f"fix or remove {shown} (cc-swap config path)",
        )]
    raw = ctx.raw_settings or {}
    problems: list[str] = []
    sections = (
        ("autoswitch", st.AutoSwitchSettings),
        ("ui", st.UiSettings),
        ("maximize", st.MaximizeSettings),
        ("notify", st.NotifySettings),
        ("claude", st.ClaudeSettings),
    )
    loaded = {}
    for name, cls in sections:
        try:
            loaded[name] = st._section_from_raw(raw.get(name), name, cls, problems)
        except TypeError:
            problems.append(f"{name} section has keys of the wrong shape")
    if "maximize" in loaded:
        problems += [f"{m}; using defaults for both" for *_, m in st._maximize_pair_errors(loaded["maximize"])]
    try:
        # The loader's own rules (only a JSON true enables priming; a bad
        # jitterS reverts), so doctor and the engine agree.
        st.prime_from_raw(raw.get("prime"), problems)
    except TypeError:
        problems.append("prime section has keys of the wrong shape")
    out = [
        Finding("settings", "warn", f"settings.json: {m}", "cc-swap config set <key> <value> (cc-swap config lists the ranges)")
        for m in problems
    ]
    if not out:
        out.append(Finding(
            "settings", "ok",
            f"settings valid (strategy {ctx.strategy}, priming {'on' if ctx.prime_enabled else 'off'})",
        ))
    return out


def check_priming(ctx: Context) -> list[Finding]:
    if not ctx.prime_enabled:
        return []
    path, _ = resolve_claude(ctx)
    if path is None:
        return []  # check_claude already reported it as an error
    auto = ctx.prime_auto_verify
    note, verified = priming_guard(ctx.probes.backup_root, auto_verify=auto)
    if note is not None:
        return [Finding(
            "priming", "warn", f"priming is {note}", "cc-swap prime verify",
        )]
    if verified is None:
        return [Finding(
            "priming", "info",
            "priming is on; its isolation was never verified with this claude "
            f"({update_pause_text(auto)})",
            "cc-swap prime verify",
        )]
    return [Finding(
        "priming", "info",
        f"priming is on; isolation verified for claude {verified} "
        f"({update_pause_text(auto)})",
    )]


def update_pause_text(auto_verify: bool) -> str:
    """What happens to priming after a Claude Code update."""
    if auto_verify:
        return (
            "it pauses after a Claude Code update until the engine re-verifies it "
            "(prime.autoVerify) or cc-swap prime verify passes"
        )
    return "it pauses after a Claude Code update until cc-swap prime verify passes"


def check_idle_pattern(ctx: Context) -> list[Finding]:
    """What maximize has learned of your busy and quiet times (info only;
    ``maximize`` strategy only). Reads the usage history, writes nothing."""
    from claude_swap import settings as st
    from claude_swap.maximize import history

    if ctx.strategy != "maximize":
        return []
    raw = ctx.raw_settings or {}
    try:
        enabled = st._section_from_raw(
            raw.get("maximize"), "maximize", st.MaximizeSettings
        ).learn_idle_pattern
    except TypeError:
        enabled = True
    p = ctx.probes
    slots = history.read(p.backup_root, p.now).slots
    return [Finding("idle-pattern", "info", history.describe(slots, p.now, enabled=enabled))]


def check_learned_ride(ctx: Context) -> list[Finding]:
    """What the learned ride has learned, per window (info only; ``maximize``
    strategy only): the share of the last point each window rides, and how
    many rides switched before 100% or hit it. Reads the state file's
    ``rideLearning``, writes nothing."""
    from claude_swap import settings as st
    from claude_swap.maximize import policy
    from claude_swap.maximize import ride as learned_ride

    if ctx.strategy != "maximize":
        return []
    try:
        mx = st._section_from_raw(
            (ctx.raw_settings or {}).get("maximize"), "maximize", st.MaximizeSettings
        )
    except TypeError:
        mx = st.MaximizeSettings()
    text = policy.ride_text(ctx.state.get(learned_ride.LEARN_KEY), mx)
    return [Finding("learned-ride", "info", text)]


def priming_guard(
    backup_root: Path, *, auto_verify: bool | None = None
) -> tuple[str | None, str | None]:
    """``(paused note, verified version)`` from the priming version guard's
    records — read-only, no ``claude --version``."""
    from claude_swap.maximize import prime_verify

    try:
        return (
            prime_verify.paused_note(backup_root, auto_verify=auto_verify),
            prime_verify.verified_version(backup_root),
        )
    except Exception:
        return None, None


# -- checks: per slot ---------------------------------------------------------------


_QUARANTINE_WHY = {
    "invalid_grant": "refresh token dead",
    "login_expired": "login expired",
    "identity-conflict": "stored login belongs to another account",
}


def _relogin_fix(number: str) -> str:
    return oauth.relogin_fix(number)


def check_slots(ctx: Context) -> list[Finding]:
    p = ctx.probes
    out: list[Finding] = []
    if ctx.sequence_problem:
        return [Finding(
            "accounts", "error",
            f"sequence.json is {ctx.sequence_problem}: no account can be read",
            "restore it from a backup (cc-swap import) or remove it and re-add accounts",
            "accounts",
        )]
    if not ctx.slots:
        return [Finding(
            "accounts", "warn", "no accounts yet", "run claude, /login, then cc-swap add", "accounts",
        )]
    live_number = _slot_for_identity(ctx)
    quarantine = ctx.state.get("quarantine")
    quarantine = quarantine if isinstance(quarantine, dict) else {}
    checked = 0
    for slot in ctx.slots.values():
        scope = f"#{slot.number}"
        creds = ctx.live.value if (slot.number == live_number and ctx.live.value) else slot.credentials
        if slot.unreadable and not creds:
            out.append(Finding(
                "stored-login", "warn", f"stored login unreadable ({slot.unreadable})",
                "fix the Keychain first; nothing is lost", scope,
            ))
            continue
        if not creds:
            out.append(Finding(
                "stored-login", "error", "no stored login",
                f"cc-swap login {slot.number} (or Fleet → select → r)",
                scope,
            ))
            continue
        checked += 1
        entry = quarantine.get(slot.number)
        if isinstance(entry, dict):
            reason = str(entry.get("reason") or "unknown")
            recovered = (
                entry.get("refreshTokenFingerprint") is not None
                and slot.fp is not None
                and entry.get("refreshTokenFingerprint") != slot.fp
            )
            if recovered:
                out.append(Finding(
                    "quarantine", "info",
                    f"quarantined ({_QUARANTINE_WHY.get(reason, reason)}) but its login "
                    "was replaced since: the engine lifts it on its next tick",
                    "", scope,
                ))
            else:
                out.append(Finding(
                    "quarantine", "error",
                    f"quarantined: {_QUARANTINE_WHY.get(reason, reason)} — never a switch target",
                    _relogin_fix(slot.number), scope,
                ))
        if slot.kind == "api_key":
            continue
        deadline_ms = oauth.login_expires_at_ms(creds)
        if deadline_ms is not None:
            left = deadline_ms / 1000.0 - p.now
            if left <= 0:
                out.append(Finding(
                    "login-deadline", "error",
                    oauth.login_expiry_note_ms(deadline_ms, int(p.now * 1000)) or "login expired",
                    _relogin_fix(slot.number), scope,
                ))
            elif left < LOGIN_WARN_S:
                out.append(Finding(
                    "login-deadline", "warn",
                    oauth.login_expiry_note_ms(deadline_ms, int(p.now * 1000)) or "login expires soon",
                    _relogin_fix(slot.number) + " (a new login starts a new ~30-day deadline)",
                    scope,
                ))
    out += _duplicates(ctx)
    out += _shared_with_profiles(ctx)
    if checked and not any(f.severity in ("warn", "error") for f in out):
        out.append(Finding(
            "accounts", "ok",
            f"{checked} stored login{'s' if checked != 1 else ''}: none expiring "
            "within 7 days, none quarantined",
            scope="accounts",
        ))
    return out


def _duplicates(ctx: Context) -> list[Finding]:
    """``switcher._duplicate_account_warnings``, by slot number only."""
    out: list[Finding] = []
    by_fp: dict[str, str] = {}
    by_identity: dict[tuple[str, str], str] = {}
    for slot in ctx.slots.values():
        fp = slot.fp
        if fp:
            other = by_fp.get(fp)
            if other and shared_login.refresh_fingerprint(slot.credentials):
                # cc-swap: one refresh token in two slots (shared_login.py).
                out.append(Finding(
                    "shared-login", "error",
                    f"#{other} and #{slot.number} hold the same login (one slot's "
                    f"backup was overwritten); "
                    f"{_SHARED_TAIL.format(f'#{other} or #{slot.number}')}",
                    shared_login.fix([slot.number, other]),
                    f"#{slot.number}",
                ))
            elif other:
                out.append(Finding(
                    "duplicate", "error",
                    f"#{other} and #{slot.number} hold the same login: one slot's "
                    "backup was overwritten",
                    f"log in as the account #{slot.number} should hold and run "
                    f"cc-swap add --slot {slot.number}",
                    f"#{slot.number}",
                ))
            else:
                by_fp[fp] = slot.number
        if slot.uuid.strip():
            key = (slot.uuid.strip(), slot.org)
            other = by_identity.get(key)
            if other and other != slot.number:
                out.append(Finding(
                    "duplicate", "error",
                    f"#{other} and #{slot.number} authenticate as the same account",
                    f"remove one of them: cc-swap remove {slot.number}",
                    f"#{slot.number}",
                ))
            elif not other:
                by_identity[key] = slot.number
    return out


def _shared_with_profiles(ctx: Context) -> list[Finding]:
    """cc-swap fork (shared_login.py): a slot, or the live login, holding the
    same refresh token as a ``cswap run`` profile that is not its own. The
    slot's own profile shares it by design (kept in step by the engine)."""
    from claude_swap.session import session_dir_for

    if not ctx.profiles:
        return []
    root = ctx.probes.backup_root
    live_number = _slot_for_identity(ctx)
    holders: list[tuple[str, str | None, str | None]] = [
        (f"#{s.number}", s.number, shared_login.refresh_fingerprint(s.credentials))
        for s in ctx.slots.values() if s.kind != "api_key"
    ]
    live_fp = shared_login.refresh_fingerprint(ctx.live.value)
    if live_fp and not any(fp == live_fp for _l, _n, fp in holders):
        holders.append(("the live login", live_number, live_fp))
    out: list[Finding] = []
    for label, number, fp in holders:
        if fp is None:
            continue
        own = (
            session_dir_for(root, number, ctx.slots[number].email)
            if number in ctx.slots else None
        )
        for profile in ctx.profiles:
            if profile.path == own or profile.rt_fp != fp:
                continue
            owner = profile.owner if (
                profile.owner in ctx.slots
                and profile.path == session_dir_for(
                    root, profile.owner, ctx.slots[profile.owner].email
                )
            ) else None
            where = (
                shared_login.profile_label(owner) if owner
                else f"a leftover cswap run profile made for #{profile.owner}"
            )
            out.append(Finding(
                "shared-login", "error",
                f"{label} holds the same login as {where}; "
                f"{_SHARED_TAIL.format(label)}",
                shared_login.fix([n for n in (number, owner) if n]),
                f"#{number}" if number else "accounts",
            ))
    return out


# -- entry point ----------------------------------------------------------------------


ENV_CHECKS: tuple[Callable[[Context], list[Finding]], ...] = (
    check_claude,
    check_claude_exec,
    check_codesign_kills,
    check_keychain,
    check_plaintext,
    check_live_login,
    check_upstream,
    check_service,
    check_lease,
    check_hold,
    check_settings,
    check_priming,
    check_idle_pattern,
    check_learned_ride,
)


def run_checks(probes: Probes | None = None) -> list[Finding]:
    """Every check, environment first, then per slot. Never raises for a
    single broken check: it becomes an error finding instead."""
    p = probes or Probes.system()
    ctx = gather(p)
    findings: list[Finding] = []
    for check in (*ENV_CHECKS, check_slots):
        try:
            findings += check(ctx)
        except Exception as e:  # one broken probe must not hide the others
            findings.append(Finding(
                check.__name__.removeprefix("check_"), "error",
                f"check crashed ({type(e).__name__})",
                "report it: https://github.com/wonjun-lab/cc-swap/issues",
            ))
    return findings
