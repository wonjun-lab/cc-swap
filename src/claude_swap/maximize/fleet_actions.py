"""Blocking Fleet actions (thread workers only): the re-login store step and
the last-resort and preferred-tier toggles.

``relogin_store`` is the half of a re-login cc-swap does itself: the user
ran ``claude`` and ``/login`` (cc-swap launches nothing); this checks that
the live login really is the slot's account, stores it through the existing
``add`` path (which refreshes a known identity in place), and switches back
to the account that was active before. It refuses to store a login that
belongs to another slot, so a slip (logging in as the wrong account) can
never overwrite a slot. Results name accounts by slot number only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from claude_swap import oauth
from claude_swap.exceptions import CredentialReadError
from claude_swap.maximize.model import TIER_LABELS, Tier
from claude_swap.maximize.tiers import (
    LAST_RESORT,
    OTHER,
    PREFERRED,
    TierList,
    account_matches,
    parse_account_list,
    tier_for,
    toggle_entry,
    without_account,
)
from claude_swap.settings import load_maximize_settings, set_setting, unset_setting


def _who(switcher, live: tuple[str, str, str]) -> str:
    """The live login, by its display name when it is a managed account."""
    data = switcher._get_sequence_data() or {}
    slot = switcher._find_account_slot(data, live[0], live[1])
    return switcher.account_name(slot, live[0], data=data) if slot else (
        "an account cc-swap does not manage"
    )


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


def backup_active_before_relogin(switcher, number: str) -> tuple[bool, str]:
    """Before a re-login of ``number`` replaces the live login, the active
    account's newest refresh token must be in its backup — the login about to
    be replaced is otherwise its only copy. ``(ok, reason)``; ``ok`` only
    when the live and backup fingerprints match afterwards. Re-logging the
    active slot itself needs no backup (its login is what gets replaced)."""
    sync = getattr(switcher, "sync_active_backup", None)
    if sync is None:
        return True, ""
    return sync(skip_number=str(number))


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
        return {"stored": False, "number": number,
                "reason": "that account is no longer managed"}
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
            "reason": (
                f"the live login is {_who(switcher, live)}, not "
                f"{switcher.account_name(number, want_email, data=data)}; nothing stored"
            ),
        }
    switcher.add_account(slot=None, assume_yes=True)
    out: dict = {"stored": True, "number": number}
    if return_to and str(return_to) != number:
        try:
            switcher.switch_to(str(return_to), json_output=True)
        except CredentialReadError as exc:
            # Stored; only the way back was refused (degraded live read).
            out["switch_back_error"] = str(exc)
            return out
        out["returned_to"] = str(return_to)
    return out


@dataclass(frozen=True)
class TierToggle:
    """What a Fleet tier toggle did (:func:`toggle_tier_setting`)."""

    marked: bool    # the account is in the toggled list afterwards
    moved: bool     # marking it dropped it from the other list
    before: Tier    # its tier before the write (``tier_for``) ...
    after: Tier     # ... and after it
    # Per list the write dropped entries from: the other accounts those
    # entries named too (a shared email), which left that list with it.
    also: tuple[tuple[TierList, tuple[str, ...]], ...] = ()


def _tier_of(accounts: Mapping[str, Mapping], number: str, mx) -> Tier:
    record = accounts.get(number, {})
    return tier_for(
        {"alias": record.get("alias")},
        str(record.get("email") or ""),
        parse_account_list(mx.last_resort),
        parse_account_list(mx.preferred),
    )


def toggle_tier_setting(
    backup_root: Path, accounts: Mapping[str, Mapping], number: str, tl: TierList
) -> TierToggle:
    """Toggle slot ``number``'s account in tier list ``tl``
    (``maximize.lastResort`` / ``maximize.preferred``). Marking it also
    drops it from the other list (the two exclude each other).
    ``ConfigError`` for a shared email without an alias (the entry would
    mark both accounts)."""

    def store(key: str, value: str) -> None:
        if value:
            set_setting(backup_root, key, value)
        else:
            unset_setting(backup_root, key)

    mx = load_maximize_settings(backup_root)
    before = _tier_of(accounts, number, mx)
    current = getattr(mx, tl.field)
    _rest, unmarking = without_account(accounts, current, number)
    store(tl.key, toggle_entry(accounts, current, number, tl))
    marked_by = parse_account_list(getattr(load_maximize_settings(backup_root), tl.field))
    marked = any(number in account_matches(accounts, e) for e in marked_by)
    dropped: list[tuple[TierList, list[str]]] = [(tl, unmarking)] if unmarking else []
    moved = False
    if marked:
        other = OTHER[tl.key]
        rest, gone = without_account(
            accounts, getattr(load_maximize_settings(backup_root), other.field), number
        )
        if gone:
            store(other.key, rest)
            dropped.append((other, gone))
            moved = True
    also = []
    for lst, entries in dropped:
        others = sorted(
            {n for e in entries for n in account_matches(accounts, e)} - {number}, key=int
        )
        if others:
            also.append((lst, tuple(others)))
    after = _tier_of(accounts, number, load_maximize_settings(backup_root))
    return TierToggle(marked, moved, before, after, tuple(also))


def toggle_message(
    result: TierToggle, tl: TierList, who: str, name: Callable[[str], str] = str
) -> str:
    """The toast for a tier toggle, worded from the account's actual tier
    after the write: an account in both lists (last resort wins) is named
    by what it is now, and a dropped shared entry names the other accounts
    it took off its list too (``cc-swap last-resort``/``prefer`` warn the
    same). ``name`` turns a slot into a display name."""
    label = TIER_LABELS
    lists = {LAST_RESORT.key: label["last_resort"], PREFERRED.key: label["preferred"]}
    if result.marked:
        message = f"{who} is {label[result.after]}"
        if result.moved:
            message += f" (no longer {lists[OTHER[tl.key].key]})"
    elif result.after == "normal":
        message = f"{who} is back to normal"
    else:
        still = "still " if result.after == result.before else ""
        message = f"{who} is {still}{label[result.after]} (no longer {lists[tl.key]})"
    for lst, numbers in result.also:
        message += (
            f"; also took {', '.join(name(n) for n in numbers)} off {lists[lst.key]} "
            "(the removed entry named them too)"
        )
    return message


def toggle_last_resort_setting(
    backup_root: Path, accounts: Mapping[str, Mapping], number: str
) -> bool:
    """Toggle slot ``number``'s account in ``maximize.lastResort``; True when it is
    last-resort afterwards (:func:`toggle_tier_setting`)."""
    return toggle_tier_setting(backup_root, accounts, number, LAST_RESORT).marked


def toggle_preferred_setting(
    backup_root: Path, accounts: Mapping[str, Mapping], number: str
) -> bool:
    """Toggle slot ``number``'s account in ``maximize.preferred`` (the
    preferred tier); True when it is preferred afterwards
    (:func:`toggle_tier_setting`)."""
    return toggle_tier_setting(backup_root, accounts, number, PREFERRED).marked
