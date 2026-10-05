"""``cc-swap repair-live``: undo a mixed live login (cc-swap fork).

The shape (macOS, 2026-10-04): a ``/login`` ran while the login Keychain
was locked — typically over SSH. Claude Code then saved the new login in
plaintext (``~/.claude/.credentials.json``) and pointed ``~/.claude.json``'s
``oauthAccount`` at it (account X), but the Keychain item it reads FIRST
still holds the previous login: a managed slot Y's token. Every reader now
pairs X's name with Y's token. The engine holds (an unmanaged or foreign
live login) and ``cc-swap add`` refuses, both rightly — neither can tell
which side is the truth.

:func:`detect` recognises it locally, without a network call: the config
names X; the Keychain holds exactly slot Y's stored login (Y is not X); the
plaintext file holds a full token pair that is no other slot's, written
after the Keychain token was issued. :func:`repair` then, after an explicit
confirmation:

1. refuses while the Keychain is unreadable (it must be written);
2. asks the token-owner endpoint whose the plaintext token is — X, or no
   repair (nothing is guessed, an unanswered lookup refuses too);
3. under the same locks as a switch, writes it into the Keychain and slot X
   (``switcher.store_relogin``; an unmanaged X gets the Keychain only, and
   ``cc-swap add`` afterwards), then removes the plaintext file once the
   Keychain holds it.

Nothing is lost: the Keychain's old token is slot Y's stored login.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_swap import oauth
from claude_swap.exceptions import ConfigError, CredentialReadError
from claude_swap.maximize.names import name_of

#: A claude.ai access token lives this long; a token's issue time is its
#: ``expiresAt`` minus this.
ACCESS_TOKEN_LIFETIME_S = 8 * 3600.0
COMMAND = "cc-swap repair-live"


@dataclass(frozen=True)
class MixedLogin:
    email: str            # what ~/.claude.json names (account X)
    org: str
    uuid: str
    x_slot: str | None    # X's managed slot, or None (unmanaged)
    y_slot: str           # the managed slot whose login the Keychain holds
    file_path: Path
    file_mtime: float
    file_creds: str       # never printed
    keychain_creds: str
    # The display names (maximize/names.py) of X's and Y's slots.
    x_name: str = ""
    y_name: str = ""

    @property
    def x_label(self) -> str:
        """X by name; an unmanaged X by its address's local part (this
        text reaches the engine's log)."""
        return self.x_name or name_of({}, self.x_slot, self.email)

    @property
    def y_label(self) -> str:
        return self.y_name or name_of({}, self.y_slot)


def explain(m: MixedLogin) -> str:
    return (
        f"mixed live login: ~/.claude.json names {m.x_label}, but the Keychain still holds "
        f"{m.y_label}'s login and ~/.claude/.credentials.json a newer one — a /login "
        f"that ran while the Keychain was locked (e.g. over SSH) saved its token in "
        f"plaintext. Run: {COMMAND}"
    )


def _keychain(switcher) -> tuple[str | None, bool]:
    """``(value, failed)`` of the live OAuth Keychain item."""
    return switcher._store._read_active_oauth_keychain()


def _pair(creds: str | None) -> dict | None:
    data = oauth.extract_oauth_data(creds or "") if creds else None
    if data and data.get("accessToken") and data.get("refreshToken"):
        return data
    return None


def _issued_at(pair: dict) -> float | None:
    expires = pair.get("expiresAt")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None
    return expires / 1000.0 - ACCESS_TOKEN_LIFETIME_S


def detect(switcher) -> MixedLogin | None:
    """The mixed shape (module docstring), read locally; None for anything
    else, or when the Keychain cannot be read. Never raises."""
    try:
        return _detect(switcher)
    except Exception:
        return None


def _detect(switcher) -> MixedLogin | None:
    from claude_swap.paths import get_credentials_path

    if not switcher._use_keychain():
        return None
    identity = switcher._get_current_identity_triple()
    if identity is None:
        return None
    email, org, uuid = identity
    kc, failed = _keychain(switcher)
    if failed or not kc:
        return None
    path = get_credentials_path()
    try:
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    kc_pair, file_pair = _pair(kc), _pair(text)
    if kc_pair is None or file_pair is None:
        return None
    kc_fp, file_fp = oauth.credential_fingerprint(kc), oauth.credential_fingerprint(text)
    if not kc_fp or not file_fp or kc_fp == file_fp:
        return None
    issued = _issued_at(kc_pair)
    if issued is None or mtime <= issued:
        return None  # the plaintext is not the newer login
    # The engine asks every tick while the login stays mixed: the slot scan
    # below (a Keychain read per slot) runs once per (identity, Keychain
    # login, plaintext file) instead.
    key = (identity, kc_fp, file_fp, mtime)
    cached = getattr(switcher, "_mixed_login_cache", None)
    if isinstance(cached, tuple) and cached[0] == key:
        return cached[1]
    found = _scan_slots(switcher, identity, kc_fp, file_fp, path, mtime, text, kc)
    try:
        switcher._mixed_login_cache = (key, found)
    except AttributeError:
        pass
    return found


def _scan_slots(switcher, identity, kc_fp, file_fp, path, mtime, text, kc) -> MixedLogin | None:
    email, org, uuid = identity
    data = switcher._get_sequence_data() or {}
    x_slot = switcher._find_account_slot(data, email, org)
    y_slot = None
    for num, rec in (data.get("accounts") or {}).items():
        if not isinstance(rec, dict) or not rec.get("email"):
            continue
        fp = oauth.credential_fingerprint(
            switcher._read_account_credentials(str(num), str(rec["email"])) or ""
        )
        if fp == file_fp and str(num) != x_slot:
            return None  # the plaintext is another slot's login, not X's
        if fp == kc_fp:
            y_slot = str(num)
    if y_slot is None or y_slot == x_slot:
        return None
    names = switcher.account_names() if hasattr(switcher, "account_names") else {}
    return MixedLogin(
        email, org, uuid, x_slot, y_slot, path, mtime, text, kc,
        name_of(names, x_slot, email) if x_slot else "", name_of(names, y_slot),
    )


def _verify_owner(m: MixedLogin) -> None:
    """The token-owner lookup must say the plaintext login is X's."""
    pair = _pair(m.file_creds) or {}
    if oauth.is_oauth_token_expired(pair.get("expiresAt")):
        raise ConfigError(
            "The plaintext login's access token has expired, so whose it is cannot be "
            "checked. Nothing was changed. Unlock the Keychain and run claude /login "
            f"as {m.email} in a GUI terminal instead."
        )
    oauth.PROFILE_STATUS.code = None
    profile = oauth.fetch_oauth_profile(str(pair.get("accessToken") or ""))
    if not profile:
        if getattr(oauth.PROFILE_STATUS, "code", None) == 401:
            raise ConfigError(
                "The plaintext login's token was rejected (401) by the token-owner "
                "lookup: it is not a usable login. Nothing was changed. Unlock the "
                f"Keychain and run claude /login as {m.email} in a GUI terminal."
            )
        raise ConfigError(
            "Could not look up whose the plaintext login is (offline, or the lookup "
            "failed). Nothing was changed; retry when online."
        )
    seen_uuid = str(profile.get("uuid") or "").strip()
    seen_email = str(profile.get("email") or "").strip()
    if m.uuid:
        mine = seen_uuid == m.uuid
    else:
        mine = bool(seen_email) and seen_email.lower() == m.email.lower()
    seen_org = profile.get("organizationUuid")
    if mine and seen_org is not None and str(seen_org).strip() != (m.org or ""):
        mine = False
    if not mine:
        raise ConfigError(
            f"The plaintext login is not {m.email}'s (it resolves to "
            f"{seen_email or seen_uuid or 'another account'}). Nothing was changed."
        )


def merged_login(file_creds: str, keychain_creds: str) -> str:
    """The plaintext login with the machine-shared fields (``mcpOAuth`` …)
    of both sides: the Keychain's, overlaid entry by entry with the
    plaintext's (newer — MCP logins made during the SSH session live only
    there). Account fields (``claudeAiOauth`` …) are the plaintext's."""
    from claude_swap.credentials import SHARED_CREDENTIAL_KEYS

    try:
        plain = json.loads(file_creds)
        kc = json.loads(keychain_creds)
    except ValueError:
        return file_creds
    if not isinstance(plain, dict) or not isinstance(kc, dict):
        return file_creds
    out = dict(plain)
    for key in SHARED_CREDENTIAL_KEYS:
        mine, theirs = plain.get(key), kc.get(key)
        if isinstance(mine, dict) and isinstance(theirs, dict):
            out[key] = {**theirs, **mine}
        elif key not in plain and key in kc:
            out[key] = theirs
    return json.dumps(out)


def _same_login(a: str | None, b: str | None) -> bool:
    fa, fb = oauth.credential_fingerprint(a or ""), oauth.credential_fingerprint(b or "")
    return bool(fa) and fa == fb


def repair(switcher, *, confirm: Callable[[str], bool]) -> str:
    """Repair the mixed live login (module docstring); the message to show.
    Raises ``ConfigError`` / ``CredentialReadError`` with nothing changed."""
    from claude_swap.claude_locks import claude_config_lock, claude_credentials_lock
    from claude_swap.locking import FileLock

    if not switcher._use_keychain():
        raise ConfigError(f"{COMMAND} repairs a macOS Keychain login; nothing to do here")
    _kc, failed = _keychain(switcher)
    if failed:
        raise CredentialReadError(
            "The macOS Keychain is unreadable right now (locked, or no GUI session), "
            f"and the repair has to write it. Nothing was changed; run {COMMAND} from a "
            "GUI terminal."
        )
    m = detect(switcher)
    if m is None:
        raise ConfigError(
            "Nothing to repair: the live login is not a mixed one (the Keychain and "
            "~/.claude.json agree, or ~/.claude/.credentials.json holds no newer login)."
        )
    _verify_owner(m)
    question = (
        f"{explain(m)}\n\nThe plaintext login is {m.email}'s (checked). Write it into the "
        f"Keychain{f' and {m.x_label}' if m.x_slot else ''} and remove "
        f"{m.file_path}? {m.y_label}'s login stays stored in its slot."
    )
    if not confirm(question):
        return "Cancelled; nothing was changed."
    # Inside a `cswap run` shell CLAUDE_CONFIG_DIR points at a session
    # profile, not the live login: refuse, like add and login.
    switcher._refuse_session_shell()
    login = merged_login(m.file_creds, m.keychain_creds)

    def unchanged() -> None:
        """Under the locks: the live login is still exactly what was
        detected — the same account named, the Keychain holding Y's very
        login (a running Claude may have rotated it since: slot Y would
        then keep a spent token), the plaintext file the same."""
        kc_now, failed = _keychain(switcher)
        try:
            file_now = m.file_path.read_text(encoding="utf-8")
        except OSError:
            file_now = None
        if (
            failed
            or kc_now != m.keychain_creds
            or file_now != m.file_creds
            or switcher._get_current_identity_triple() != (m.email, m.org, m.uuid)
        ):
            raise ConfigError(
                f"The live login changed since it was checked; nothing was changed. "
                f"Run {COMMAND} again."
            )

    if m.x_slot:
        config = json.loads(switcher._get_claude_config_path().read_text(encoding="utf-8"))
        account = config.get("oauthAccount") if isinstance(config, dict) else None
        if not isinstance(account, dict):
            raise ConfigError("~/.claude.json names no account any more; nothing was changed")
        # The slot and the live login in one critical section, under the
        # switch's locks; it re-checks the slot's identity there, and
        # `unchanged` the live login, before writing anything.
        switcher.store_relogin(
            m.x_slot, login, account, activate=True, precheck=unchanged,
            live_shared_from=login,
        )
    else:
        with (
            FileLock(switcher.lock_file),
            claude_credentials_lock(),
            claude_config_lock(),
        ):
            unchanged()
            switcher._write_credentials(login)
    kc_after, failed = _keychain(switcher)
    if failed or not _same_login(kc_after, m.file_creds):
        raise CredentialReadError(
            f"The Keychain does not hold the repaired login; {m.file_path} was kept"
        )
    with claude_credentials_lock():
        try:
            if _same_login(m.file_path.read_text(encoding="utf-8"), m.file_creds):
                m.file_path.unlink()
        except OSError:
            pass
    switcher._logger.info(
        "repair-live: plaintext login (rt %s) moved into the Keychain%s",
        oauth.fingerprint8(m.file_creds), f" and {m.x_label}" if m.x_slot else "",
    )
    if m.x_slot:
        return (
            f"Repaired: the Keychain and {m.x_label} hold {m.email}'s login; "
            f"{m.file_path} was removed."
        )
    return (
        f"Repaired: the Keychain holds {m.email}'s login and {m.file_path} was removed. "
        "Run cc-swap add to manage it."
    )


def command(argv: list[str]) -> int:
    """``cc-swap repair-live [--yes]``; the exit code."""
    import argparse
    import sys

    from claude_swap.exceptions import ClaudeSwitchError
    from claude_swap.printer import error
    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description=(
            "Repair a mixed live login: ~/.claude.json names one account while the "
            "Keychain still holds a managed slot's token and ~/.claude/.credentials.json "
            "a newer login (a /login while the Keychain was locked, e.g. over SSH). "
            "Checks whose the plaintext login is, then writes it into the Keychain and "
            "its slot and removes the plaintext file."
        ),
    )
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    def ask(question: str) -> bool:
        print(question)
        if args.yes:
            return True
        try:
            return input("Repair now? [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        message = repair(switcher, confirm=ask)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        return 1
    print(message)
    return 0 if message.startswith("Repaired") else 1
