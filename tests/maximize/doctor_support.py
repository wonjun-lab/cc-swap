"""A fake machine for ``cc-swap doctor`` / ``init`` tests.

Everything doctor reads from outside its temp directories goes through
``Probes``; this world answers those calls from memory: a dict Keychain
(``security`` exit codes included), a ``claude`` that only knows
``--version``, a ``ps`` listing, a service manager and an engine lease. Any
other command fails the test, so nothing real can run. Tokens and emails
carry ``SECRET`` / ``@example.com`` so tests can assert they never leak.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.maximize import doctor as dr

NOW = 1_790_000_000.0  # fixed clock
DAY = 86400.0
USER = "tester"
LIVE_SERVICE = "Claude Code-credentials"


def creds(n: int | str, *, login_in_s: float | None = 20 * DAY, rt: str | None = None) -> str:
    oauth = {
        "accessToken": f"at-SECRET-{n}",
        "refreshToken": rt or f"rt-SECRET-{n}",
        "expiresAt": int((NOW + 3600) * 1000),
    }
    if login_in_s is not None:
        oauth["refreshTokenExpiresAt"] = int((NOW + login_in_s) * 1000)
    return json.dumps({"claudeAiOauth": oauth})


def email(n: int | str) -> str:
    return f"user{n}@example.com"


@dataclass
class World:
    tmp: Path
    platform: str = "darwin"
    keychain: dict[tuple[str, str], str | int] = field(default_factory=dict)
    ps: str = "  101 /usr/bin/login\n"
    claude_version: str | None = "2.1.230 (Claude Code)"
    claude_path: str | None = None  # on PATH; default <home>/bin/claude
    service: dict | None = None  # service.status() shape; None = not installed
    lease: tuple[bool | None, int | None] = (False, None)
    started_at: dict[int, float] = field(default_factory=dict)
    installs: dict[str, tuple[str | None, float | None]] = field(default_factory=dict)
    program: list[str] | None = None
    environ: dict[str, str] = field(default_factory=lambda: {"USER": USER})
    version: str = "0.2.0"
    commands: list[list[str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.home = self.tmp / "home"
        self.root = self.tmp / "root"
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        bin_dir = self.home / "bin"
        bin_dir.mkdir(exist_ok=True)
        claude = bin_dir / "claude"
        claude.write_text("#!/bin/sh\n")
        claude.chmod(0o755)
        if self.claude_path is None:
            self.claude_path = str(claude)
        cc = bin_dir / "cc-swap"
        cc.write_text("#!/bin/sh\n")
        cc.chmod(0o755)
        if self.program is None:
            self.program = [str(cc)]

    # -- seeding -------------------------------------------------------------------

    def accounts(self, *numbers: int, active: int | None = None, **creds_by_slot) -> None:
        """Slots with stored logins; ``creds_by_slot`` overrides (``s2=...``,
        ``None`` = no stored login)."""
        seq = {
            "activeAccountNumber": active,
            "sequence": list(numbers),
            "accounts": {
                str(n): {"email": email(n), "organizationUuid": "org", "uuid": f"uuid-{n}"}
                for n in numbers
            },
        }
        (self.root / "sequence.json").write_text(json.dumps(seq))
        for n in numbers:
            value = creds_by_slot.get(f"s{n}", creds(n))
            if value is not None:
                self.store(n, value)

    def store(self, n: int, value: str) -> None:
        if self.platform == "darwin":
            self.keychain[("claude-swap", f"account-{n}-{email(n)}")] = value
        else:
            enc = self.root / "credentials" / f".creds-{n}-{email(n)}.enc"
            enc.parent.mkdir(parents=True, exist_ok=True)
            enc.write_text(base64.b64encode(value.encode()).decode())

    def login(self, n: int | None, value: str | None = None, *, plaintext: str | None = None,
              keychain_rc: int | None = None) -> None:
        """The live login: ``~/.claude.json`` names slot ``n``'s account; the
        credential lives in the Keychain (macOS) or the plaintext file."""
        if n is not None:
            (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {
                "emailAddress": email(n), "organizationUuid": "org", "accountUuid": f"uuid-{n}",
            }}))
        value = value if value is not None else (creds(n) if n is not None else None)
        if self.platform == "darwin":
            if keychain_rc is not None:
                self.keychain[(LIVE_SERVICE, USER)] = keychain_rc
            elif value is not None:
                self.keychain[(LIVE_SERVICE, USER)] = value
            if plaintext is not None:
                self.cred_file().write_text(plaintext)
        elif value is not None or plaintext is not None:
            path = self.cred_file()
            path.write_text(plaintext if plaintext is not None else value)
            path.chmod(0o600)

    def cred_file(self) -> Path:
        return self.home / ".claude" / ".credentials.json"

    def settings(self, **sections) -> None:
        payload = {"schemaVersion": 1, "autoswitch": {"strategy": "maximize"}}
        payload.update(sections)
        (self.root / "settings.json").write_text(json.dumps(payload))

    def state(self, **keys) -> None:
        (self.root / "autoswitch_state.json").write_text(json.dumps(keys))

    def service_installed(self, *, pid: int = 4121, running: bool = True,
                          env: dict | None = None, program: list[str] | None = None) -> None:
        """A healthy installed service (plist / unit written under home)."""
        program = program or self.program
        env = {"PATH": "/usr/bin", "CC_SWAP_SERVICE": "1"} if env is None else env
        if self.platform == "darwin":
            import plistlib

            plist = self.home / "Library" / "LaunchAgents" / "com.wonjun-lab.cc-swap.plist"
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_bytes(plistlib.dumps({
                "Label": "com.wonjun-lab.cc-swap",
                "ProgramArguments": [*program, "auto"],
                "EnvironmentVariables": env,
            }))
            path = plist
        else:
            unit = self.home / ".config" / "systemd" / "user" / "cc-swap.service"
            unit.parent.mkdir(parents=True, exist_ok=True)
            lines = ["[Service]", "ExecStart=" + " ".join(f'"{a}"' for a in [*program, "auto"])]
            lines += [f'Environment="{k}={v}"' for k, v in env.items()]
            unit.write_text("\n".join(lines) + "\n")
            path = unit
        self.service = {
            "installed": True, "loaded": True, "running": running,
            "state": "running" if running else "waiting", "pid": pid if running else None,
            "path": str(path),
        }
        if running:
            self.lease = (True, pid)
            self.started_at.setdefault(pid, NOW - 3600)
        self.installs.setdefault(program[0], (self.version, NOW - 2 * DAY))

    # -- probes ----------------------------------------------------------------------

    def run(self, argv: list[str], timeout: float) -> dr.RunResult:
        self.commands.append(list(argv))
        if argv[0] == dr.SECURITY:
            assert argv[1] == "find-generic-password" and "-w" in argv, argv
            account = argv[argv.index("-a") + 1]
            service = argv[argv.index("-s") + 1]
            value = self.keychain.get((service, account), dr.RC_NOT_FOUND)
            if isinstance(value, int):
                return dr.RunResult(value, "", f"security: rc {value}")
            return dr.RunResult(0, value + "\n")
        if argv[0] == self.claude_path or Path(argv[0]).name == "claude":
            assert argv[1:] == ["--version"], f"doctor ran claude {argv[1:]}"
            if self.claude_version is None:
                return dr.RunResult(1, "", "boom")
            return dr.RunResult(0, self.claude_version + "\n")
        if argv[0] == "ps":
            return dr.RunResult(0, self.ps)
        raise AssertionError(f"unexpected command {argv}")

    def which(self, name: str) -> str | None:
        return self.claude_path if name == "claude" else None

    def probes(self, now: float = NOW) -> dr.Probes:
        return dr.Probes(
            backup_root=self.root,
            home=self.home,
            platform=self.platform,
            environ=dict(self.environ),
            now=now,
            run=self.run,
            which=self.which,
            is_executable=lambda p: Path(p).is_file() and os.access(p, os.X_OK),
            service_status=lambda: self.service if self.service else {
                "installed": False, "loaded": False, "running": False, "state": None, "pid": None,
            },
            lease_holder=lambda root: self.lease,
            process_started_at=lambda pid: self.started_at.get(pid),
            program_install=lambda prog: self.installs.get(prog, (None, None)),
            current_program=lambda: list(self.program or []),
            version=self.version,
        )

    def healthy(self, n_accounts: int = 3) -> World:
        """Logged in as #1, n accounts stored, maximize, service running."""
        self.accounts(*range(1, n_accounts + 1), active=1)
        self.login(1)
        self.settings()
        self.service_installed()
        return self


def files(*roots: Path) -> dict[str, tuple[bytes, float]]:
    """Every file under ``roots`` with its bytes and mtime (write detector)."""
    out = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
    return out
