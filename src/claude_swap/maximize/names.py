"""Short display names for accounts (cc-swap fork). Pure, no I/O.

Fleet's table, its top sentence and attention line, desktop notifications
and the fork's CLI lines name an account by a short name, never its whole
address (only Fleet's selected-account panel shows that):

* its alias, when the user set one (``cc-swap alias``, Fleet ``n``);
* else the part of its address before the ``@``: ``dev.shared`` for
  ``dev.shared@example.com``.

Names stay unique, case-insensitively: when two accounts would get the same
short name (the same local part at different domains), each says where it
is from with as much of its domain as that takes, never the top-level
label: ``jordan.lee@example`` and ``jordan.lee@uni``. A short name that
reads like another account's alias gives way the same way. Two slots with
the very same address (a personal and a team account) add their slot:
``same@host·4``. Aliases themselves never change: ``cc-swap alias``
already refuses a duplicate.
"""

from __future__ import annotations

from collections.abc import Iterable


def short_name(email: str) -> str:
    """The part of ``email`` before the ``@``."""
    return (email or "").split("@", 1)[0].strip()


def _domain_labels(email: str) -> list[str]:
    domain = email.split("@", 1)[1] if "@" in email else ""
    labels = [p for p in domain.split(".") if p]
    return labels[:-1] if len(labels) > 1 else labels  # never the top-level label


def display_names(accounts: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """``{slot: name}`` for ``(slot, email, alias)`` triples."""
    rows = [(str(slot), email or "", (alias or "").strip()) for slot, email, alias in accounts]
    out: dict[str, str] = {}
    aliases = {alias.lower() for _s, _e, alias in rows if alias}
    plain: dict[str, list[tuple[str, str]]] = {}
    for slot, email, alias in rows:
        if alias:
            out[slot] = alias
        elif short_name(email):
            plain.setdefault(short_name(email).lower(), []).append((slot, email))
        else:
            out[slot] = f"#{slot}"
    for key, group in plain.items():
        if len(group) == 1 and key not in aliases:
            slot, email = group[0]
            out[slot] = short_name(email)
            continue
        # Each account takes the fewest domain labels that tell it apart.
        pending = list(group)
        deepest = max(len(_domain_labels(email)) for _s, email in group)
        depth = 1
        while pending:
            names = {
                slot: short_name(email) + (
                    "@" + ".".join(_domain_labels(email)[:depth]) if _domain_labels(email) else ""
                )
                for slot, email in pending
            }
            counts: dict[str, int] = {}
            for name in names.values():
                counts[name.lower()] = counts.get(name.lower(), 0) + 1
            left = []
            for slot, email in pending:
                name = names[slot]
                if counts[name.lower()] == 1 and name.lower() not in aliases:
                    out[slot] = name
                elif depth >= deepest:  # the very same address: the slot tells
                    labels = _domain_labels(email)
                    first = short_name(email) + (f"@{labels[0]}" if labels else "")
                    out[slot] = f"{first}·{slot}"
                else:
                    left.append((slot, email))
            pending = left
            depth += 1
    return {slot: out[slot] for slot, _e, _a in rows}
