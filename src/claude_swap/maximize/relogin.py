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

``cc-swap login --new`` runs the same login without ``--email`` (or with
the one the user gave) to ADD an account: :class:`NewLoginAttempt` reads the
identity from the profile, refuses an account that is already in a slot
(pointing at ``cc-swap login N``; the CLI may offer to keep the login as
that slot's re-login instead), and otherwise stores it in the next free
slot (or ``--slot N``) through ``switcher.store_new_login``. The live login
is never read or written.

Bare ``cc-swap login`` (and Fleet's *Sign in (add or renew)*) runs that
login too and decides by who signed in (:func:`match_login`, the duplicate
rule ``store_new_login`` uses): an account a slot already holds gets its
login renewed exactly like ``cc-swap login N`` (:class:`SignInAttempt`),
one cc-swap does not have is added like ``--new``, and a login that
matches a slot only in part — the same email in another organization, the
same account id under another email — is not guessed at: nothing is
stored, the login is kept unclaimed and the message names the match and
the commands that resolve it.

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
from claude_swap.maximize.names import cli_arg, name_of, record_names

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
DUPLICATE = "duplicate"  # login --new: that account is already in a slot (``number``)
AMBIGUOUS = "ambiguous"  # bare login: matches a slot only in part (``number`` = the first)


@dataclass(frozen=True)
class Target:
    """The slot being re-logged, as stored."""

    number: str
    email: str
    org: str
    uuid: str
    # The display name (maximize/names.py) every message calls it by.
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or name_of({}, self.number, self.email)


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
    accounts = (switcher._get_sequence_data() or {}).get("accounts") or {}
    record = accounts.get(num)
    if not isinstance(record, Mapping) or not record.get("email"):
        raise AccountNotFoundError(f"No account {num}")
    name = name_of(record_names(accounts), num, record.get("email"))
    if record.get("kind") == "api_key":
        raise ValidationError(f"{name} is an API key: there is no login to renew")
    return Target(
        num,
        str(record.get("email") or ""),
        str(record.get("organizationUuid") or ""),
        str(record.get("uuid") or "").strip(),
        name,
    )


def login_argv(claude: str, email: str) -> list[str]:
    """``claude auth login --claudeai``, pre-filled with ``email`` if any."""
    argv = [claude, "auth", "login", "--claudeai"]
    return [*argv, "--email", email] if email else argv


def _manual():
    """Re-login is always the user's own run (``claude_exec.manual``): the
    CLI sets one with a printed warning; Fleet's threads get this one."""
    from claude_swap.maximize import claude_exec

    return claude_exec.current_manual() or claude_exec.Manual("Fleet: re-login")


def login_supported(claude: str, *, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Whether ``claude`` has ``auth login --email`` (older builds do not).

    Run like the login itself: every auth/endpoint override stripped and
    ``CLAUDE_CONFIG_DIR`` pointing at a throwaway directory (removed
    afterwards), so even ``--help`` never sees the live profile or a token."""
    from claude_swap.maximize import claude_exec
    from claude_swap.maximize.primer import isolated_env

    try:
        probe = Path(tempfile.mkdtemp(prefix="cc-swap-login-probe-"))
    except OSError:
        return False
    try:
        result = claude_exec.run(
            [claude, "auth", "login", "--help"], caller="claude auth login --help",
            timeout=timeout, manual=_manual(), env=isolated_env(os.environ, probe), cwd=probe,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)
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
    killed, before this returns). ``OSError`` when it cannot be started.
    Audited, with ``DISABLE_AUTOUPDATER=1`` (maximize/claude_exec.py)."""
    from claude_swap.maximize import claude_exec

    launch = claude_exec.Launch(argv, caller="claude auth login", manual=_manual())
    proc = launch.popen(env=claude_exec.child_env(env), cwd=str(cwd))
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        _reap(proc)
        launch.finish(proc.returncode, error="interrupted")
        return None
    except BaseException as e:  # SIGTERM/SIGHUP (raised as KeyboardInterrupt) or worse
        _reap(proc)
        launch.finish(proc.returncode, error=type(e).__name__)
        raise
    launch.finish(code)
    return code


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
        except Exception as e:  # the type only: the text is not ours to log at WARNING
            _logger.warning("Could not remove the leftover re-login profile %s: %s", path,
                            type(e).__name__)
            _logger.debug("Removing %s failed", path, exc_info=True)
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
            f"\ncc-swap: signing in {t.label} as {t.email} with `claude auth login`.\n"
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
            return Outcome(CANCELLED, num, f"{self.what()} cancelled; nothing stored")
        return Outcome(
            FAILED, num,
            f"claude auth login exited {code} (cancelled or failed); nothing stored",
        )

    def what(self) -> str:
        """The attempt, as messages name it."""
        return f"re-login {self.target.label}"

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
            return self.salvage(switcher, t.number, f"could not read the new login: {e}")
        pair = _full_pair(creds)
        if pair is None:
            return self.salvage(switcher, t.number, "claude saved no login")
        if account is None:
            return self.salvage(switcher, t.number, "the new login names no account")
        problem = identity_problem(t, account)
        if problem is None:
            problem = _oracle_problem(switcher, t, pair)
        if problem is not None:
            return Outcome(
                MISMATCH, t.number, f"not {t.label}'s account: {problem}; nothing stored"
            )
        committed: list[dict] = []
        try:
            result = switcher.store_relogin(
                t.number, creds, account, activate=True, on_commit=committed.append,
            ) or {}
        except Exception as e:
            if not committed:
                return Outcome(FAILED, t.number, _not_stored(switcher, t, creds, e))
            _log_failure("re-login: stored, then the after-work failed", t.number, e)
            result = committed[0]  # stored; only the after-work failed
        except BaseException as e:  # Ctrl-C / SIGTERM mid-store: keep it, then unwind
            if not committed:  # once stored, a stash would only duplicate it
                _not_stored(switcher, t, creds, e)
            raise
        stored = f"{t.label} login stored ({t.email})"
        if result.get("activated"):
            return Outcome(STORED, t.number, f"{stored}; the live login now uses it",
                           activated=True)
        return Outcome(STORED, t.number, stored)

    def retry(self) -> str:
        """The command that runs this sign-in again."""
        return f"cc-swap login {cli_arg(self.target.label)}"

    def salvage(self, switcher, number: str, why: str) -> Outcome:
        """The browser step finished but its login cannot be checked or
        stored (unreadable, incomplete, names no account): keep whatever
        credential the profile still yields as an unclaimed entry (best
        effort, before the cleanup deletes it) and never end silently —
        the message always says to sign in again."""
        from claude_swap.session import read_config_dir_credentials

        creds = None
        try:
            creds = read_config_dir_credentials(str(self.profile), strict_keychain=True)
        except Exception as e:
            _log_failure("reading the new login", number, e)
        message = f"{why}; nothing stored"
        if _full_pair(creds) is not None:
            try:
                entry = switcher.stash_relogin_credential(
                    number or "new", creds, "login-unreadable",
                )
            except Exception as e:
                _log_failure("keeping the unreadable login", number, e)
            else:
                message += f". The new login was kept as {entry} (cc-swap unclaimed)"
        return Outcome(FAILED, number, f"{message}. Sign in again: {self.retry()}")

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
    except Exception as e:
        _log_failure("keeping the unstored login", target.number, e)
        return message
    return (
        f"{message}. The new login was kept as {entry} (cc-swap unclaimed); "
        f"retry cc-swap login {cli_arg(target.label)}"
    )


def _log_failure(what: str, number: str, error: BaseException) -> None:
    """Log a failure by its exception type and slot number only (an
    internal log, read with the code): the text can carry a config or
    credential filename, which holds the account's email. The full text
    (and traceback) goes to DEBUG."""
    _logger.warning("%s (#%s): %s", what, number or "new", type(error).__name__)
    _logger.debug("%s (#%s): %s", what, number or "new", error, exc_info=True)


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


@dataclass(frozen=True)
class NewAccount:
    """``cc-swap login --new``: the slot asked for (None: the next free one)
    and the email to pre-fill (``""``: the user picks in the browser)."""

    slot: str | None = None
    email: str = ""
    number: str = ""  # Outcome.number until a slot is chosen


class NewLoginAttempt(LoginAttempt):
    """``claude auth login`` for an account cc-swap does not have yet."""

    def __init__(self, root: Path, new: NewAccount, claude: str) -> None:
        super().__init__(root, new, claude)  # type: ignore[arg-type]

    def what(self) -> str:
        return "sign-in of a new account"

    def retry(self) -> str:
        return "cc-swap login --new"

    def banner(self) -> str:
        who = f" as {self.target.email}" if self.target.email else ""
        return (
            f"\ncc-swap: signing in a new account{who} with `claude auth login`.\n"
            "Finish in the browser; over SSH open the printed URL on any device "
            "and paste the code here.\n"
            "Ctrl-C cancels. The live login is not touched; an account cc-swap "
            "already has is not added twice.\n"
        )

    def finish(  # type: ignore[override]
        self,
        switcher,
        *,
        adopt_existing: Callable[[str, str], bool] | None = None,
    ) -> Outcome:
        """Store the profile's login as a new account. An account already in
        a slot is refused (DUPLICATE, ``number`` = that slot) unless
        ``adopt_existing(number, email)`` says to keep it as that slot's
        re-login, which then runs the re-login checks and store. A login
        that is not stored is kept unclaimed whatever went wrong."""
        try:
            creds, account = self.read_login()
        except Exception as e:
            return self.salvage(switcher, "", f"could not read the new login: {e}")
        if _full_pair(creds) is None:
            return self.salvage(switcher, "", "claude saved no login")
        if account is None:
            return self.salvage(switcher, "", "the new login names no account")
        try:
            return self._place(switcher, creds, account, adopt_existing)
        except Exception as e:  # e.g. a torn sequence.json: keep the login
            return Outcome(FAILED, "", _new_not_stored(switcher, creds, e))

    def _place(
        self,
        switcher,
        creds: str,
        account: Mapping,
        adopt_existing: Callable[[str, str], bool] | None,
    ) -> Outcome:
        """:meth:`finish` once the profile's login is read. Every refusal
        it returns has kept the login unclaimed; what it raises, ``finish``
        keeps."""
        from claude_swap.exceptions import DuplicateAccountError

        # ``--email`` only pre-fills the browser form: whoever signed in is
        # the new account (checked against the slots below).
        email = str(account.get("emailAddress") or "").strip()
        data = switcher._get_sequence_data() or {}
        existing = switcher.slot_for_login(
            data, email, str(account.get("organizationUuid") or ""),
            str(account.get("accountUuid") or "").strip(),
        )
        if existing is None:
            committed: list[str] = []
            try:
                number = switcher.store_new_login(
                    creds, account, slot=self.target.slot, on_commit=committed.append,
                )
            except DuplicateAccountError as e:
                existing = e.number  # added meanwhile: same answer as below
            except Exception as e:
                if committed:  # stored; only the after-work failed
                    _log_failure("new account: stored, then the after-work failed",
                                 committed[0], e)
                    return self._stored(committed[0], email, account, creds)
                return Outcome(FAILED, "", _new_not_stored(switcher, creds, e))
            except BaseException as e:  # Ctrl-C / SIGTERM mid-store: keep it, then unwind
                if not committed:
                    _new_not_stored(switcher, creds, e)
                raise
            else:
                return self._stored(number, email, account, creds)
        if adopt_existing is not None and adopt_existing(existing, email):
            try:
                self.target = target_for(switcher, existing)  # type: ignore[assignment]
            except (AccountNotFoundError, ValidationError) as e:
                return Outcome(FAILED, existing, _kept(
                    switcher, existing, creds, f"{e}; not stored",
                ))
            outcome = LoginAttempt.finish(self, switcher)
            if outcome.status == MISMATCH:  # e.g. the same account under a new email
                return Outcome(MISMATCH, existing, _kept(
                    switcher, existing, creds, outcome.message,
                ))
            return outcome
        return Outcome(DUPLICATE, existing, _kept(
            switcher, existing, creds,
            f"{email} is already {_nm(switcher, existing, email)}; not stored as a new "
            f"account. To renew that login: cc-swap login {cli_arg(_nm(switcher, existing, email))}",
        ))

    @staticmethod
    def _stored(number: str, email: str, account: Mapping, creds: str) -> Outcome:
        """The success message: the account, its organization and the plan
        its credential names (``rateLimitTier``, as every slot's plan)."""
        tag = _plan_tag(account, creds)
        return Outcome(STORED, number, f"new account {email} stored [{tag}]")


def _plan_tag(account: Mapping, creds: str) -> str:
    """``Team · 20x``: the organization and the plan the credential names."""
    from claude_swap.maximize.plan import plan_label, rate_limit_tier_from_credentials

    org = str(account.get("organizationName") or "") or (
        "org" if account.get("organizationUuid") else "personal"
    )
    plan = plan_label(rate_limit_tier_from_credentials(creds))
    return f"{org} · {plan}" if plan else org


def _kept(switcher, number: str, creds: str, message: str) -> str:
    """``message``, after keeping a fresh login that was not stored as an
    unclaimed entry (``cc-swap unclaimed``): a finished browser login is
    never simply thrown away."""
    try:
        entry = switcher.stash_relogin_credential(number, creds, "login-new-not-stored")
    except Exception as e:
        _log_failure("keeping the unstored new login", number, e)
        return message
    return f"{message}. This login was kept as {entry} (cc-swap unclaimed)"


def _new_not_stored(switcher, creds: str, error: BaseException) -> str:
    """The failure message of a new account; the login is kept unclaimed."""
    from claude_swap.exceptions import ClaudeSwitchError

    why = str(error) if isinstance(error, ClaudeSwitchError) and str(error) else (
        f"{type(error).__name__}: {error}"
    )
    message = f"not stored ({why}); the live login is unchanged"
    try:
        entry = switcher.stash_relogin_credential("new", creds, "login-new-unstored")
    except Exception as e:
        _log_failure("keeping the unstored new login", "", e)
        return message
    return f"{message}. The new login was kept as {entry} (cc-swap unclaimed)"


# -- bare `cc-swap login`: add or renew, by who signed in ---------------------------------

#: :attr:`LoginMatch.kind` values (and :data:`AMBIGUOUS`).
RENEW = "renew"
ADD = "add"


@dataclass(frozen=True)
class LoginMatch:
    """Who signed in, held against the slots (:func:`match_login`)."""

    kind: str  # RENEW | ADD | AMBIGUOUS
    number: str = ""  # RENEW: the slot; AMBIGUOUS: the first slot it matches in part
    partial: tuple[str, ...] = ()  # AMBIGUOUS: what matched, one line per slot
    fixes: tuple[tuple[str, str], ...] = ()  # AMBIGUOUS: (command, what it does)


def _org_label(name: str, org_uuid: str) -> str:
    return name or (f"org {org_uuid}" if org_uuid else "personal")


def match_login(data: Mapping | None, account: Mapping) -> LoginMatch:
    """Which slot the login ``account`` (a profile's ``oauthAccount``) is.

    RENEW: exactly one OAuth slot holds that account — the email
    (case-insensitive) and organization agree and the account uuids do not
    disagree (``identity_problem``'s rule, which the re-login re-checks).
    ADD: no slot has it in any form. AMBIGUOUS otherwise — the same email in
    another organization, the same email under another account uuid, the
    same account uuid under another email (an email change), an API-key
    slot under that email, or two slots for one account: cc-swap does not
    guess, and says what matched and which commands resolve it."""
    email = str(account.get("emailAddress") or "").strip()
    org = str(account.get("organizationUuid") or "")
    uuid = str(account.get("accountUuid") or "").strip()
    here = _org_label(str(account.get("organizationName") or ""), org)
    exact: list[tuple[str, str]] = []
    partial: list[str] = []
    fixes: list[tuple[str, str]] = []
    first = ""
    accounts = (data or {}).get("accounts") or {}
    names = record_names(accounts)
    for raw_num, rec in accounts.items():
        if not isinstance(rec, Mapping) or not rec.get("email"):
            continue
        num = str(raw_num)
        nm = name_of(names, num, rec.get("email"))
        arg = cli_arg(nm)
        rec_email = str(rec.get("email") or "").strip()
        rec_org = str(rec.get("organizationUuid") or "")
        rec_uuid = str(rec.get("uuid") or "").strip()
        there = _org_label(str(rec.get("organizationName") or ""), rec_org)
        same_email = rec_email.lower() == email.lower()
        same_id = bool(uuid and rec_uuid and uuid == rec_uuid)
        other_id = bool(uuid and rec_uuid and uuid != rec_uuid)
        api_key = rec.get("kind") == "api_key"
        if same_email and rec_org == org and not other_id and not api_key:
            exact.append((num, there))
            continue
        if not (same_email or same_id):
            continue
        first = first or num
        if api_key:
            partial.append(f"{nm} is an API key under {rec_email}")
            fixes.append((f"cc-swap remove {arg}; cc-swap login",
                          f"replace the API key {nm} with this login"))
        elif same_email and rec_org != org:
            ours = here
            if there == here:  # two organizations under one name: tell them apart
                there, ours = _org_label("", rec_org), _org_label("", org)
            partial.append(f"{nm} is {rec_email} in {there}; you signed in to {ours}")
            if rec_org:
                fixes.append((f"cc-swap login {arg}",
                              f"renew {nm}: sign in to {there} this time"))
            else:
                # Stored without an organization (a setup-token / add-token
                # slot): `cc-swap login N` compares the org strictly and can
                # never match a browser login, so replacing it is the way.
                fixes.append((f"cc-swap remove {arg}; cc-swap login",
                              f"replace {nm} (stored without an organization) with this login"))
        elif same_email:
            partial.append(f"{nm} is {rec_email} in {there}, but another account id")
            fixes.append((f"cc-swap remove {arg}; cc-swap login",
                          f"replace {nm} with the account you signed in as"))
        else:
            partial.append(f"{nm} is this account id under another email, {rec_email}")
            fixes.append((f"cc-swap remove {arg}; cc-swap login",
                          f"replace {nm} (its email changed)"))
    if len(exact) == 1:
        return LoginMatch(RENEW, exact[0][0])
    if exact:  # two slots claim one account (a hand-edited sequence.json)
        first = exact[0][0]
        for num, there in exact:
            nm = name_of(names, num, email)
            partial.insert(0, f"{nm} is {email} in {there} too")
            fixes.insert(0, (f"cc-swap login {cli_arg(nm)}", f"renew {nm}"))
    if not partial:
        return LoginMatch(ADD)
    from claude_swap.switcher import ClaudeAccountSwitcher

    if ClaudeAccountSwitcher.slot_for_login(dict(data or {}), email, org, uuid) is None:
        fixes.append(("cc-swap login --new [--slot N]", "add it as a separate account"))
    return LoginMatch(AMBIGUOUS, first, tuple(partial), tuple(fixes))


def _day(deadline_ms: int) -> str:
    """``Nov 4``: the local date of a login deadline."""
    from datetime import datetime, timezone

    when = datetime.fromtimestamp(deadline_ms / 1000.0, tz=timezone.utc).astimezone()
    return when.strftime(f"%b {when.day}")


def _slot_name(data: Mapping, number: str) -> str:
    """Slot ``number``'s display name (maximize/names.py), as every surface
    names it."""
    accounts = (data or {}).get("accounts") or {}
    record = accounts.get(str(number)) if isinstance(accounts, Mapping) else None
    email = record.get("email") if isinstance(record, Mapping) else ""
    return name_of(record_names(accounts), number, email)


def _with_email(name: str, email: str) -> str:
    """``new@example.com`` when the name is its local part, else ``name
    (email)``: a terminal line, the address says which account."""
    from claude_swap.maximize.names import short_name

    if not email or name.lower() == short_name(email).lower():
        return email or name
    return f"{name} ({email})"


def _nm(switcher, number: object, email: object = "") -> str:
    """Slot ``number``'s display name, read off ``switcher``."""
    try:
        return switcher.account_name(number, email)
    except Exception:
        return name_of({}, number, email)


class SignInAttempt(NewLoginAttempt):
    """Bare ``cc-swap login``: ``claude auth login`` with no account named,
    then add or renew by who signed in (:func:`match_login`). Never asks."""

    def what(self) -> str:
        return "sign-in"

    def retry(self) -> str:
        return "cc-swap login"

    def banner(self) -> str:
        who = f" as {self.target.email}" if self.target.email else ""
        return (
            f"\ncc-swap: signing in{who} with `claude auth login`.\n"
            "Finish in the browser; over SSH open the printed URL on any device "
            "and paste the code here.\n"
            "Ctrl-C cancels. An account cc-swap has gets its login renewed, a new "
            "one is added; the live login changes only when you sign in as the "
            "account it is logged in as.\n"
        )

    def finish(self, switcher) -> Outcome:  # type: ignore[override]
        """Renew the slot that holds the signed-in account, or add it to a
        free slot (``--slot``), or — a partial match — store nothing. A
        login that is not stored is kept unclaimed whatever went wrong."""
        try:
            creds, account = self.read_login()
        except Exception as e:
            return self.salvage(switcher, "", f"could not read the new login: {e}")
        if _full_pair(creds) is None:
            return self.salvage(switcher, "", "claude saved no login")
        if account is None:
            return self.salvage(switcher, "", "the new login names no account")
        try:
            return self._decide(switcher, creds, account)
        except Exception as e:  # e.g. a torn sequence.json: keep the login
            return Outcome(FAILED, "", _new_not_stored(switcher, creds, e))

    def _decide(self, switcher, creds: str, account: Mapping, *, again: bool = True) -> Outcome:
        from claude_swap.exceptions import DuplicateAccountError

        data = switcher._get_sequence_data() or {}
        match = match_login(data, account)
        if match.kind == RENEW:
            return self._renew(switcher, match.number, creds, data)
        if match.kind == AMBIGUOUS:
            return self._ambiguous(switcher, match, creds, account)
        email = str(account.get("emailAddress") or "").strip()
        committed: list[str] = []
        try:
            number = switcher.store_new_login(
                creds, account, slot=self.target.slot, on_commit=committed.append,
            )
        except DuplicateAccountError:
            if again:  # added meanwhile: decide again on what is stored now
                return self._decide(switcher, creds, account, again=False)
            raise
        except Exception as e:
            if committed:  # stored; only the after-work failed
                _log_failure("sign-in: added, then the after-work failed", committed[0], e)
                number = committed[0]
            else:
                return Outcome(FAILED, "", _new_not_stored(switcher, creds, e))
        except BaseException as e:  # Ctrl-C / SIGTERM mid-store: keep it, then unwind
            if not committed:
                _new_not_stored(switcher, creds, e)
            raise
        return Outcome(STORED, number,
                       f"added {_with_email(_nm(switcher, number, email), email)} "
                       f"[{_plan_tag(account, creds)}]")

    def _renew(self, switcher, number: str, creds: str, data: Mapping) -> Outcome:
        """Exactly ``cc-swap login N``'s checks and store (``store_relogin``,
        which rewrites the live login only when slot ``number`` IS the live
        account)."""
        asked = self.target.slot
        try:
            self.target = target_for(switcher, number)  # type: ignore[assignment]
        except (AccountNotFoundError, ValidationError) as e:
            return Outcome(FAILED, number, _kept(switcher, number, creds, f"{e}; not stored"))
        outcome = LoginAttempt.finish(self, switcher)
        if outcome.status == MISMATCH:  # the token-owner oracle disagreed
            return Outcome(MISMATCH, number, _kept(switcher, number, creds, outcome.message))
        if not outcome.ok:
            return outcome
        deadline = oauth.login_expires_at_ms(creds)
        ends = f"; login now ends {_day(deadline)}" if deadline else ""
        name = _slot_name(data, number)
        lines = [f"updated {name} (token renewed{ends})"]
        if outcome.activated:
            lines.append(
                f"{name} is the account Claude Code is logged in as: the live "
                "login now uses the new token too (same account, nothing switched)"
            )
        if asked is not None and str(int(str(asked))) != number:
            lines.append(f"--slot {asked} not used: this account is already {name}")
        return Outcome(STORED, number, "\n".join(lines), activated=outcome.activated)

    @staticmethod
    def _ambiguous(switcher, match: LoginMatch, creds: str, account: Mapping) -> Outcome:
        """Store nothing; keep the login unclaimed; say what matched and the
        commands that resolve it (CLI and Fleet output, never a log)."""
        email = str(account.get("emailAddress") or "").strip()
        here = _org_label(str(account.get("organizationName") or ""),
                          str(account.get("organizationUuid") or ""))
        lines = [
            f"signed in as {email} ({here}), which matches an account cc-swap "
            "has only in part, so nothing was stored:",
            *(f"  {p}" for p in match.partial),
        ]
        if match.fixes:
            lines.append("Run the one you mean:")
            width = max(len(cmd) for cmd, _ in match.fixes)
            lines += [f"  {cmd.ljust(width)}  {what}" for cmd, what in match.fixes]
        try:
            entry = switcher.stash_relogin_credential(
                match.number or "new", creds, "login-ambiguous",
            )
        except Exception as e:
            _log_failure("keeping the unstored sign-in", match.number, e)
            lines.append("The slots and the live login are unchanged.")
        else:
            lines.append(
                f"This login was kept as {entry} (cc-swap unclaimed); the slots "
                "and the live login are unchanged."
            )
        return Outcome(AMBIGUOUS, match.number, "\n".join(lines))


def sign_in(
    switcher,
    new: NewAccount,
    *,
    claude: str,
    run: Callable[[Sequence[str], Mapping[str, str], Path], int | None] = run_interactive,
    announce: Callable[[str], None] | None = print,
    base_env: Mapping[str, str] | None = None,
) -> Outcome:
    """The whole bare ``cc-swap login`` (Fleet runs the same steps around
    ``App.suspend``). ``new.email`` only pre-fills; ``new.slot`` is where a
    new account goes. Only its number is checked before the browser: a
    renew does not use it, and adding to a taken one is refused when
    storing (the login is then kept unclaimed)."""
    check_new_slot(switcher, new.slot, free=False)
    refuse = getattr(switcher, "_refuse_session_shell", None)
    if refuse is not None:
        refuse()  # before the browser: storing would refuse anyway
    with terminate_as_interrupt(), SignInAttempt(switcher.backup_dir, new, claude) as attempt:
        if announce is not None:
            announce(attempt.banner())
        early = attempt.launch(run, base_env=base_env)
        if early is not None:
            return early
        return attempt.finish(switcher)


def guided_signin_steps(claude: str | None) -> list[str]:
    """Signing in by hand, for when claude cannot be launched here."""
    return [
        "Sign in by hand:",
        f"  1. run  {claude or 'claude'}  and type /login, sign in as the account",
        "  2. run  cc-swap add  (adds it, or refreshes the slot it is already in)",
        "  3. switch back to the account you were on",
    ]


def login_new(
    switcher,
    new: NewAccount,
    *,
    claude: str,
    run: Callable[[Sequence[str], Mapping[str, str], Path], int | None] = run_interactive,
    announce: Callable[[str], None] | None = print,
    base_env: Mapping[str, str] | None = None,
    adopt_existing: Callable[[str, str], bool] | None = None,
) -> Outcome:
    """The whole ``cc-swap login --new`` (the TUI runs the same steps
    around ``App.suspend``). A taken ``--slot`` is refused before the
    browser (and re-checked when storing)."""
    check_new_slot(switcher, new.slot)
    refuse = getattr(switcher, "_refuse_session_shell", None)
    if refuse is not None:
        refuse()  # before the browser, not after: store_new_login would refuse anyway
    with terminate_as_interrupt(), NewLoginAttempt(switcher.backup_dir, new, claude) as attempt:
        if announce is not None:
            announce(attempt.banner())
        early = attempt.launch(run, base_env=base_env)
        if early is not None:
            return early
        return attempt.finish(switcher, adopt_existing=adopt_existing)


def check_new_slot(switcher, slot: str | None, *, free: bool = True) -> None:
    """``ValidationError`` when ``slot`` is not a slot number or, with
    ``free``, is taken. The bare ``cc-swap login`` checks only the number
    before the browser: whether the slot must be free depends on who signs
    in (a renew does not use it; ``store_new_login`` re-checks an add)."""
    if slot is None:
        return
    if not str(slot).isdigit() or int(str(slot)) < 1:
        raise ValidationError(f"--slot takes a slot number >= 1, not {slot}")
    if not free:
        return
    accounts = (switcher._get_sequence_data() or {}).get("accounts") or {}
    if str(int(str(slot))) in accounts:
        raise ValidationError(
            f"slot {int(str(slot))} is taken (by "
            f"{name_of(record_names(accounts), str(int(str(slot))))}); "
            "pick a free one, or omit --slot"
        )


def guided_new_steps(claude: str | None) -> list[str]:
    """Adding an account by hand, for when claude cannot be launched here."""
    return [
        "Add a new account by hand:",
        f"  1. run  {claude or 'claude'}  and type /login, sign in as the new account",
        "  2. run  cc-swap add",
        "  3. switch back to the account you were on",
    ]


def guided_steps(
    number: str, email: str, claude: str | None, name: str | None = None
) -> list[str]:
    """The manual re-login, for when claude cannot be launched here."""
    who = name or name_of({}, number, email)
    return [
        f"Re-login {who} by hand ({email}):",
        f"  1. run  {claude or 'claude'}  and type /login, sign in as {email}",
        f"  2. run  cc-swap add  (it refreshes {who} in place)",
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
