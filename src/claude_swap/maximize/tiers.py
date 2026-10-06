"""Account tiers: preferred (``maximize.preferred``), normal, last_resort
(``maximize.lastResort``), excluded (``cswap disable``) (spec §5.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from claude_swap.maximize.model import Tier


def parse_account_list(value: str | None) -> tuple[str, ...]:
    """Comma-separated emails/aliases → trimmed, lowercased, deduplicated."""
    if not value:
        return ()
    out: list[str] = []
    for part in value.split(","):
        item = part.strip().lower()
        if item and item not in out:
            out.append(item)
    return tuple(out)


def tier_for(
    record: Mapping,
    email: str,
    last_resort: tuple[str, ...],
    preferred: tuple[str, ...] = (),
) -> Tier:
    """``record`` is the slot's ``sequence.json`` account record. Disabled
    wins, then ``last_resort`` (an account in both lists is last resort),
    then ``preferred``; anything else is normal."""
    if record.get("disabled"):
        return "excluded"
    names = set()
    if email and email.strip():
        names.add(email.strip().lower())
    alias = record.get("alias")
    if isinstance(alias, str) and alias.strip():
        names.add(alias.strip().lower())
    if names & {item.lower() for item in last_resort}:
        return "last_resort"
    if names & {item.lower() for item in preferred}:
        return "preferred"
    return "normal"


@dataclass(frozen=True)
class TierList:
    """One of the account-list settings that set a tier: what the CLI, the
    TUI and ``remove`` edit it by."""

    tier: Tier
    key: str          # the dotted setting, e.g. ``maximize.lastResort``
    field: str        # its ``MaximizeSettings`` field
    command: str      # the ``cc-swap`` subcommand that edits it
    label: str        # how output names membership, e.g. ``last-resort``


LAST_RESORT = TierList(
    "last_resort", "maximize.lastResort", "last_resort", "last-resort", "last-resort"
)
PREFERRED = TierList(
    "preferred", "maximize.preferred", "preferred", "prefer", "preferred"
)
# Each list's opposite: an account is in at most one of them.
OTHER: dict[str, TierList] = {
    LAST_RESORT.key: PREFERRED,
    PREFERRED.key: LAST_RESORT,
}


# -- tier-list entries (shared by `cc-swap last-resort`/`prefer`, the TUI and `remove`) --


def account_entry(
    accounts: Mapping, num: str, email: str, tl: TierList = LAST_RESORT
) -> str:
    """The ``tl`` (``maximize.lastResort`` by default) entry that names
    exactly Account-``num``.

    The email (slot numbers move under swap/move, spec §5.1) — unless another
    managed account shares it (a personal and a Team login), where the email
    would mark both; then the account's alias, which is unique.
    ``accounts`` is ``sequence.json``'s ``accounts`` map (slot → record).
    """
    shared = sorted(
        (
            n for n, rec in accounts.items()
            if n != num and (rec.get("email") or "").lower() == email.lower()
        ),
        key=int,
    )
    if not shared:
        return email
    alias = accounts.get(num, {}).get("alias")
    if alias:
        return alias
    from claude_swap.exceptions import ConfigError
    from claude_swap.maximize.names import cli_arg, name_of, names_list, record_names

    names = record_names(accounts)
    name = name_of(names, num, email)
    raise ConfigError(
        f"{email} is shared by {names_list(names, [num, *shared])}; "
        f"give {name} an alias first (cc-swap alias {cli_arg(name)} NAME) so "
        f"{tl.label} names only that account"
    )


def last_resort_entry(accounts: Mapping, num: str, email: str) -> str:
    """:func:`account_entry` for ``maximize.lastResort``."""
    return account_entry(accounts, num, email, LAST_RESORT)


def account_matches(accounts: Mapping, entry: str) -> list[str]:
    """Slot numbers an entry marks: email or alias, case-insensitive (the
    same rule :func:`tier_for` applies, for either list)."""
    needle = entry.lower()
    return sorted(
        (
            n for n, rec in accounts.items()
            if needle in {
                (rec.get("email") or "").lower(),
                (rec.get("alias") or "").lower(),
            }
        ),
        key=int,
    )


# The name every caller of the last-resort list knew it by.
last_resort_matches = account_matches


def _entries(value: str | None) -> list[str]:
    """The comma list as written: trimmed, case-insensitively deduped, first
    spelling kept (``settings.parse_model_names``' rule, which the CLI uses)."""
    seen: dict[str, str] = {}
    for part in (value or "").split(","):
        item = part.strip()
        if item and item.lower() not in seen:
            seen[item.lower()] = item
    return list(seen.values())


def without_account(accounts: Mapping, current: str | None, num: str) -> tuple[str, list[str]]:
    """``(value, dropped)``: the list ``current`` without every entry that
    marks Account-``num`` (``""`` means the key should be unset), and those
    entries."""
    entries = _entries(current)
    dropped = [e for e in entries if num in account_matches(accounts, e)]
    return ",".join(e for e in entries if e not in dropped), dropped


def toggle_entry(
    accounts: Mapping, current: str | None, num: str, tl: TierList = LAST_RESORT
) -> str:
    """The new ``tl`` value with Account-``num`` toggled.

    Marked by any entry → every entry that marks it is dropped (it is then
    guaranteed out of that list). Not marked → :func:`account_entry` is
    appended, which raises ``ConfigError`` for a shared email without an
    alias. ``""`` means the key should be unset.
    """
    value, dropped = without_account(accounts, current, num)
    if dropped:
        return value
    email = str(accounts.get(num, {}).get("email") or "")
    return ",".join([*_entries(current), account_entry(accounts, num, email, tl)])


def toggle_last_resort(accounts: Mapping, current: str | None, num: str) -> str:
    """:func:`toggle_entry` for ``maximize.lastResort``."""
    return toggle_entry(accounts, current, num, LAST_RESORT)


def toggle_preferred(accounts: Mapping, current: str | None, num: str) -> str:
    """:func:`toggle_entry` for ``maximize.preferred`` (the preferred tier)."""
    return toggle_entry(accounts, current, num, PREFERRED)
