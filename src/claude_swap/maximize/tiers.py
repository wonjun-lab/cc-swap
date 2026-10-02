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
