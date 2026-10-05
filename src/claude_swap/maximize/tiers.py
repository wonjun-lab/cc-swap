"""Account tiers: excluded (``cswap disable``), last_resort, normal (spec §5.1)."""

from __future__ import annotations

from collections.abc import Mapping

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


def tier_for(record: Mapping, email: str, last_resort: tuple[str, ...]) -> Tier:
    """``record`` is the slot's ``sequence.json`` account record."""
    if record.get("disabled"):
        return "excluded"
    names = set()
    if email and email.strip():
        names.add(email.strip().lower())
    alias = record.get("alias")
    if isinstance(alias, str) and alias.strip():
        names.add(alias.strip().lower())
    wanted = {item.lower() for item in last_resort}
    if names & wanted:
        return "last_resort"
    return "normal"


# -- maximize.lastResort entries (shared by `cc-swap last-resort` and the TUI) --


def last_resort_entry(accounts: Mapping, num: str, email: str) -> str:
    """The ``maximize.lastResort`` entry that names exactly Account-``num``.

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
        "last-resort names only that account"
    )


def last_resort_matches(accounts: Mapping, entry: str) -> list[str]:
    """Slot numbers an entry marks: email or alias, case-insensitive (the
    same rule :func:`tier_for` applies)."""
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


def _entries(value: str | None) -> list[str]:
    """The comma list as written: trimmed, case-insensitively deduped, first
    spelling kept (``settings.parse_model_names``' rule, which the CLI uses)."""
    seen: dict[str, str] = {}
    for part in (value or "").split(","):
        item = part.strip()
        if item and item.lower() not in seen:
            seen[item.lower()] = item
    return list(seen.values())


def toggle_last_resort(accounts: Mapping, current: str | None, num: str) -> str:
    """The new ``maximize.lastResort`` value with Account-``num`` toggled.

    Marked by any entry → every entry that marks it is dropped (it is then
    guaranteed normal). Not marked → :func:`last_resort_entry` is appended,
    which raises ``ConfigError`` for a shared email without an alias.
    ``""`` means the key should be unset.
    """
    entries = _entries(current)
    marking = [e for e in entries if num in last_resort_matches(accounts, e)]
    if marking:
        return ",".join(e for e in entries if e not in marking)
    email = str(accounts.get(num, {}).get("email") or "")
    return ",".join([*entries, last_resort_entry(accounts, num, email)])
