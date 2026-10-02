"""Blocking Fleet actions (thread workers only): the re-login store step and
the last-resort toggle.

``relogin_store`` is the half of a re-login cc-swap does itself: the user
ran ``claude`` and ``/login`` (cc-swap launches nothing); this checks that
the live login really is the slot's account, stores it through the existing
``add`` path (which refreshes a known identity in place), and switches back
to the account that was active before. It refuses to store a login that
belongs to another slot, so a slip (logging in as the wrong account) can
never overwrite a slot. Results name accounts by slot number only.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from claude_swap import oauth
from claude_swap.maximize.tiers import (
    last_resort_matches,
    parse_account_list,
    toggle_last_resort,
)
from claude_swap.settings import load_maximize_settings, set_setting, unset_setting


def _who(switcher, live: tuple[str, str, str]) -> str:
    """The live login, by slot number when it is a managed account."""
    data = switcher._get_sequence_data() or {}
    slot = switcher._find_account_slot(data, live[0], live[1])
    return f"#{slot}" if slot else "an account cc-swap does not manage"


NO_NEW_LOGIN = (
    "no new login yet — run claude, /login as this account, then press enter"
)


def live_login_fingerprint(switcher) -> str | None:
    """``oauth.credential_fingerprint`` of the live login (a hash of its
    refresh token — never the token), or None when nothing is readable."""
    read = getattr(switcher, "_read_credentials", None)
    if read is None:
        return None
    try:
        creds = read()
    except Exception:
        return None
    return oauth.credential_fingerprint(creds) if creds else None


def relogin_store(
    switcher, number: str, *, return_to: str | None, before: str | None = None
) -> dict:
    """Store the live login into slot ``number`` if — and only if — it is
    that slot's account AND a new login (``before`` is the live login's
    fingerprint when the re-login started; None = unknown, not checked);
    then switch back to ``return_to``.

    Returns ``{"stored": bool, "number", "reason"?, "returned_to"?}``.
    Raises nothing for a wrong login: that is a refusal, not an error.
    """
    number = str(number)
    data = switcher._get_sequence_data() or {}
    record = (data.get("accounts") or {}).get(number)
    if not isinstance(record, Mapping):
        return {"stored": False, "number": number, "reason": f"there is no account #{number}"}
    live = switcher._get_current_identity_triple()
    if live is None:
        return {
            "stored": False,
            "number": number,
            "reason": "no live Claude Code login found — run claude and /login first",
        }
    # The slot may already hold this very login (its own, now dead): storing
    # it again would "succeed" and change nothing.
    if before is not None and live_login_fingerprint(switcher) == before:
        return {"stored": False, "number": number, "reason": NO_NEW_LOGIN}
    email, org, account_uuid = live
    want_email = str(record.get("email") or "")
    want_org = str(record.get("organizationUuid") or "")
    want_uuid = str(record.get("uuid") or "")
    same = (
        email.strip().lower() == want_email.strip().lower()
        and org == want_org
        and (not want_uuid or not account_uuid or account_uuid == want_uuid)
    )
    # The add path re-finds the slot by (email, org); it must be this one.
    slot = switcher._find_account_slot(data, email, org) if same else None
    if not same or slot != number:
        return {
            "stored": False,
            "number": number,
            "reason": f"the live login is {_who(switcher, live)}, not #{number}; nothing stored",
        }
    switcher.add_account(slot=None, assume_yes=True)
    out: dict = {"stored": True, "number": number}
    if return_to and str(return_to) != number:
        switcher.switch_to(str(return_to), json_output=True)
        out["returned_to"] = str(return_to)
    return out


def toggle_last_resort_setting(
    backup_root: Path, accounts: Mapping[str, Mapping], number: str
) -> bool:
    """Toggle Account-``number`` in ``maximize.lastResort``; True when it is
    last-resort afterwards. ``ConfigError`` for a shared email without an
    alias (the entry would mark both accounts)."""
    current = load_maximize_settings(backup_root).last_resort
    value = toggle_last_resort(accounts, current, number)
    if value:
        set_setting(backup_root, "maximize.lastResort", value)
    else:
        unset_setting(backup_root, "maximize.lastResort")
    marked = parse_account_list(load_maximize_settings(backup_root).last_resort)
    return any(number in last_resort_matches(accounts, e) for e in marked)
