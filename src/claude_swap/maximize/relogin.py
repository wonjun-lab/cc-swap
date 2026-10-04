"""Re-login one account by launching Claude Code's own login for it.

``cc-swap login N`` and Fleet's ``r`` run ``claude auth login --claudeai
--email <slot email>`` in a throwaway profile: a fresh directory under the
backup root, handed to the child as ``CLAUDE_CONFIG_DIR`` with every
auth/endpoint override stripped (``primer.isolated_env``, the primer's own
scrub). The user only signs in in the browser — locally claude opens it,
over SSH it prints a URL and asks for the code back, so the child gets the
terminal.

Afterwards the new login is read from that profile, never from the live
one: the credential from the profile's Keychain item on macOS (Claude names
it ``Claude Code-credentials-<hash of the profile path>``), else its
``.credentials.json`` (``session.read_config_dir_credentials``, the capture
read ``cswap run`` already uses); the account from the profile's
``.claude.json`` ``oauthAccount``. It is stored only when it is the slot's
account (email + organization, and the account uuid when both sides have
one; the token-owner oracle refuses a definite mismatch), through
``switcher.store_relogin``.

``store_relogin`` takes the consume, account and Claude Code locks and, when
the slot IS the live account, writes the live login in the same critical
section (rolling both back on any failure): backup and live never disagree
where another process could copy the old login back over the new one.
Otherwise the live login is not touched — nothing switches and no engine
pause is involved. A login that could not be stored is kept as an unclaimed
entry (``cc-swap unclaimed``), so a finished browser login is never lost.

The profile — directory and Keychain item — is removed whatever happens:
success, mismatch, Ctrl-C, SIGTERM/SIGHUP, a crash. A profile a killed
process left behind is swept (with its Keychain item) by the next attempt
and when an engine starts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from claude_swap import oauth
from claude_swap.credentials import looks_like_api_key
from claude_swap.exceptions import AccountNotFoundError, ValidationError

_logger = logging.getLogger("claude-swap")

PROFILE_PREFIX = "relogin-"
PROBE_TIMEOUT_S = 20.0
INTERRUPT_GRACE_S = 5.0
STALE_PROFILE_S = 3600.0  # older than this, a profile whose process is gone is swept
OWNED_PROFILE_MAX_S = 86400.0  # a live owner protects it this long (pid reuse)

#: ``Outcome.status`` values.
STORED = "stored"
MISMATCH = "mismatch"
CANCELLED = "cancelled"
FAILED = "failed"
UNAVAILABLE = "unavailable"  # claude (or its `auth login`) cannot be run: guide instead


@dataclass(frozen=True)
class Target:
    """The slot being re-logged, as stored."""

    number: str
    email: str
    org: str
    uuid: str


@dataclass(frozen=True)
class Outcome:
    status: str
    number: str
    message: str
    activated: bool = False  # the live login got the new login too (active slot)

    @property
    def ok(self) -> bool:
        return self.status == STORED


def target_for(switcher, number: str) -> Target:
    """Slot ``number``'s stored identity. ``AccountNotFoundError`` for no
    such slot; ``ValidationError`` for an API-key slot (nothing to log in)."""
    num = str(number)
    record = ((switcher._get_sequence_data() or {}).get("accounts") or {}).get(num)
    if not isinstance(record, Mapping) or not record.get("email"):
        raise AccountNotFoundError(f"Account-{num} does not exist")
    if record.get("kind") == "api_key":
        raise ValidationError(f"Account-{num} is an API key: there is no login to renew")
    return Target(
        num,
        str(record.get("email") or ""),
        str(record.get("organizationUuid") or ""),
        str(record.get("uuid") or "").strip(),
    )


def login_argv(claude: str, email: str) -> list[str]:
    return [claude, "auth", "login", "--claudeai", "--email", email]


def login_supported(claude: str, *, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Whether ``claude`` has ``auth login --email`` (older builds do not)."""
    try:
        result = subprocess.run(
            [claude, "auth", "login", "--help"],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "--email" in (result.stdout or "")


def _reap(proc: subprocess.Popen) -> None:
    """Wait out the child after a Ctrl-C; kill it when it lingers or when
    the user presses Ctrl-C again. Returns only once it has exited, so it
    cannot write its Keychain item after the profile is cleaned up."""
    try:
        proc.wait(timeout=INTERRUPT_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    except KeyboardInterrupt:
        pass
    proc.kill()
    while True:
        try:
            proc.wait()
            return
        except KeyboardInterrupt:
            continue


def run_interactive(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> int | None:
    """Run ``argv`` on this terminal; its exit code, or None when the user
    pressed Ctrl-C (the child got the SIGINT too and has exited, or was
    killed, before this returns). ``OSError`` when it cannot be started."""
    proc = subprocess.Popen(list(argv), env=dict(env), cwd=str(cwd))
    try:
        return proc.wait()
    except KeyboardInterrupt:
        _reap(proc)
        return None
    except BaseException:  # SIGTERM/SIGHUP (raised as KeyboardInterrupt) or worse
        _reap(proc)
        raise


@contextlib.contextmanager
def terminate_as_interrupt() -> Iterator[None]:
    """SIGTERM / SIGHUP raise ``KeyboardInterrupt`` meanwhile, so a closed
    terminal or a kill still unwinds through the profile cleanup. Main
    thread only (elsewhere a no-op)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    saved: dict = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved[sig] = signal.signal(sig, interrupt)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for sig, handler in saved.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


PID_FILE = "cc-swap-login.pid"


def _remove_profile(profile: Path) -> bool:
    """The profile's Keychain item (macOS), then the directory, then the
    item again (a child that exited just now may still have written it).
    When the Keychain delete fails the directory is KEPT — its path is the
    only way to name the item — so a later sweep can retry. Returns whether
    everything is gone."""
    from claude_swap.session import delete_macos_keychain_entry

    if not delete_macos_keychain_entry(profile):
        _drop_pid_file(profile)  # nobody owns it any more: the sweep may take it
        return False
    shutil.rmtree(profile, ignore_errors=True)
    if not delete_macos_keychain_entry(profile):
        _logger.warning("A Keychain item of the removed re-login profile %s may remain",
                        profile)
        return False
    return True


def _drop_pid_file(profile: Path) -> None:
    try:
        (profile / PID_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def _owner_alive(profile: Path) -> bool:
    """Whether the process that made ``profile`` is still running (a login
    can wait in the browser for longer than the sweep's age limit)."""
    try:
        pid = int((profile / PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, another user's
    except OSError:
        return False
    return True


def sweep_stale_profiles(root: Path, *, max_age_s: float = STALE_PROFILE_S,
                         now: float | None = None) -> list[Path]:
    """Remove ``relogin-*`` profiles older than ``max_age_s`` that a killed
    attempt left in ``root`` (they may hold a login: the Linux
    ``.credentials.json``, the macOS Keychain item). Returns what was
    removed. Never raises."""
    removed: list[Path] = []
    now = time.time() if now is None else now
    try:
        candidates = list(Path(root).glob(f"{PROFILE_PREFIX}*"))
    except OSError:
        return removed
    for path in candidates:
        try:
            if not path.is_dir() or path.is_symlink():
                continue
            age = now - path.stat().st_mtime
            if age < max_age_s or (age < OWNED_PROFILE_MAX_S and _owner_alive(path)):
                continue
            if _remove_profile(path):
                removed.append(path)
        except Exception:
            _logger.warning("Could not remove the leftover re-login profile %s", path,
                            exc_info=True)
    if removed:
        _logger.info("Removed %d leftover re-login profile(s)", len(removed))
    return removed


def _profile_account(profile: Path) -> dict | None:
    """The profile's ``oauthAccount`` (claude rewrites it on every login)."""
    try:
        config = json.loads((profile / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    account = config.get("oauthAccount") if isinstance(config, dict) else None
    if not isinstance(account, dict) or not account.get("emailAddress"):
        return None
    return account


def _who(email: str, org: str) -> str:
    return f"{email} ({'org ' + org if org else 'personal'})"


def identity_problem(target: Target, account: Mapping) -> str | None:
    """Why ``account`` (a profile's ``oauthAccount``) is not the slot's
    account — naming who signed in and who was expected — or None."""
    email = str(account.get("emailAddress") or "").strip()
    org = str(account.get("organizationUuid") or "")
    uuid = str(account.get("accountUuid") or "").strip()
    expected = _who(target.email, target.org)
    if email.lower() != target.email.strip().lower():
        return f"signed in as {email or 'an unknown account'}, expected {target.email}"
    if org != target.org:
        return f"signed in as {_who(email, org)}, expected {expected}"
    if target.uuid and uuid and uuid != target.uuid:
        return (
            f"signed in as {email}, but as account {uuid}, expected account "
            f"{target.uuid}"
        )
    return None


def _full_pair(creds: str | None) -> dict | None:
    pair = None if looks_like_api_key(creds) else oauth.extract_oauth_data(creds or "")
    if pair and pair.get("accessToken") and pair.get("refreshToken"):
        return pair
    return None


class LoginAttempt:
    """One ``claude auth login`` in a throwaway profile; ``cleanup`` always."""

    def __init__(self, root: Path, target: Target, claude: str) -> None:
        self.target = target
        self.claude = claude
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        sweep_stale_profiles(root)
        self.profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX, dir=root))
        if os.name == "posix":
            os.chmod(self.profile, 0o700)
        (self.profile / PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
        self._clean = False

    def env(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        from claude_swap.maximize.primer import isolated_env

        return isolated_env(os.environ if base_env is None else base_env, self.profile)

    def argv(self) -> list[str]:
        return login_argv(self.claude, self.target.email)

    def banner(self) -> str:
        t = self.target
        return (
            f"\ncc-swap: signing in #{t.number} as {t.email} with `claude auth login`.\n"
            "Finish in the browser; over SSH open the printed URL on any device "
            "and paste the code here.\n"
            "Ctrl-C cancels. Nothing is stored unless the login is this account.\n"
        )

    def launch(
        self,
        run: Callable[[Sequence[str], Mapping[str, str], Path], int | None] = run_interactive,
        *,
        base_env: Mapping[str, str] | None = None,
    ) -> Outcome | None:
        """Run the login. None when there is a login to check (the child
        exited 0, or it was interrupted / failed AFTER saving a complete
        login: that one is not thrown away); else the outcome that ends the
        attempt."""
        num = self.target.number
        try:
            code = run(self.argv(), self.env(base_env), self.profile)
        except OSError as e:
            return Outcome(UNAVAILABLE, num, f"could not start {self.claude}: {e}")
        if code == 0:
            return None
        if self._saved_a_login():
            return None
        if code is None:
            return Outcome(CANCELLED, num, f"re-login #{num} cancelled; nothing stored")
        return Outcome(
            FAILED, num,
            f"claude auth login exited {code} (cancelled or failed); nothing stored",
        )

    def _saved_a_login(self) -> bool:
        try:
            creds, account = self.read_login()
        except Exception:
            return False
        return account is not None and _full_pair(creds) is not None

    def read_login(self) -> tuple[str | None, dict | None]:
        """The new credential and ``oauthAccount`` from the profile (macOS:
        its hashed Keychain item, else / elsewhere ``.credentials.json``)."""
        from claude_swap.session import read_config_dir_credentials

        creds = read_config_dir_credentials(str(self.profile), strict_keychain=True)
        return creds, _profile_account(self.profile)

    def finish(self, switcher) -> Outcome:
        """Verify the profile's login is the slot's account and store it
        (for the live account, the live login too, atomically)."""
        t = self.target
        try:
            creds, account = self.read_login()
        except Exception as e:
            return Outcome(FAILED, t.number, f"could not read the new login: {e}")
        pair = _full_pair(creds)
        if pair is None:
            return Outcome(FAILED, t.number, "claude saved no login; nothing stored")
        if account is None:
            return Outcome(
                FAILED, t.number, "the new login names no account; nothing stored"
            )
        problem = identity_problem(t, account)
        if problem is None:
            problem = _oracle_problem(switcher, t, pair)
        if problem is not None:
            return Outcome(
                MISMATCH, t.number, f"not #{t.number}'s account: {problem}; nothing stored"
            )
        try:
            result = switcher.store_relogin(t.number, creds, account, activate=True) or {}
        except Exception as e:
            return Outcome(FAILED, t.number, _not_stored(switcher, t, creds, e))
        except BaseException as e:  # Ctrl-C / SIGTERM mid-store: keep it, then unwind
            _not_stored(switcher, t, creds, e)
            raise
        stored = f"#{t.number} login stored ({t.email})"
        if result.get("activated"):
            return Outcome(STORED, t.number, f"{stored}; the live login now uses it",
                           activated=True)
        return Outcome(STORED, t.number, stored)

    def cleanup(self) -> None:
        """Delete the profile's Keychain item and the directory. Idempotent."""
        if self._clean:
            return
        self._clean = True
        _remove_profile(self.profile)

    def __enter__(self) -> LoginAttempt:
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


def _not_stored(switcher, target: Target, creds: str, error: Exception) -> str:
    """The failure message; the new login is kept as an unclaimed entry."""
    from claude_swap.exceptions import ClaudeSwitchError

    why = str(error) if isinstance(error, ClaudeSwitchError) and str(error) else (
        f"{type(error).__name__}: {error}"
    )
    message = f"not stored ({why}); the slot and the live login are unchanged"
    try:
        entry = switcher.stash_relogin_credential(target.number, creds, "relogin-unstored")
    except Exception:
        _logger.warning("Could not keep the unstored login of #%s", target.number,
                        exc_info=True)
        return message
    return f"{message}. The new login was kept as {entry} (cc-swap unclaimed); retry cc-swap login {target.number}"


def _oracle_problem(switcher, target: Target, pair: Mapping) -> str | None:
    """Ask the token-owner oracle (advisory: an unanswered lookup passes)."""
    if oauth.is_oauth_token_expired(pair.get("expiresAt")):
        return None
    resolved = oauth.fetch_oauth_profile(str(pair.get("accessToken") or ""))
    if not resolved:
        return None
    check = getattr(switcher, "_resolved_matches_slot_identity", None)
    if check is None or check(target.number, resolved) is not False:
        return None
    seen = resolved.get("email") or resolved.get("uuid") or "another account"
    return f"the new token belongs to {seen}, expected {target.email}"


def guided_steps(number: str, email: str, claude: str | None) -> list[str]:
    """The manual re-login, for when claude cannot be launched here."""
    return [
        f"Re-login #{number} by hand ({email}):",
        f"  1. run  {claude or 'claude'}  and type /login, sign in as {email}",
        f"  2. run  cc-swap add  (it refreshes #{number} in place)",
        "  3. switch back to the account you were on",
    ]


def relogin(
    switcher,
    number: str,
    *,
    claude: str,
    run: Callable[[Sequence[str], Mapping[str, str], Path], int | None] = run_interactive,
    announce: Callable[[str], None] | None = print,
    base_env: Mapping[str, str] | None = None,
) -> Outcome:
    """The whole re-login of slot ``number`` (the CLI's; the TUI runs the
    same steps around ``App.suspend``)."""
    target = target_for(switcher, number)
    refuse = getattr(switcher, "_refuse_session_shell", None)
    if refuse is not None:
        refuse()  # before the browser, not after: store_relogin would refuse anyway
    with terminate_as_interrupt(), LoginAttempt(switcher.backup_dir, target, claude) as attempt:
        if announce is not None:
            announce(attempt.banner())
        early = attempt.launch(run, base_env=base_env)
        if early is not None:
            return early
        return attempt.finish(switcher)
