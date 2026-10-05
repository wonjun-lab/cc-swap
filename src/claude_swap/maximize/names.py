"""The one name every account goes by on screen (cc-swap fork). Pure, no I/O.

Slot numbers are upstream claude-swap's internal ids: they differ from one
machine to the next and say nothing about the account, so nothing a person
reads leads with one. Every user-facing surface (Fleet, the engine's log
lines, ``cc-swap why``/``doctor``/``history``, ``list``/``status``, desktop
notifications, CLI messages and errors) names an account by its display
name:

* its alias, when the user set one (``cc-swap alias``, Fleet ``n``);
* else the part of its address before the ``@``: ``dev.shared`` for
  ``dev.shared@example.com``.

Names stay unique, case-insensitively: when two accounts would get the same
short name (the same local part at different domains), each says where it
is from with as much of its domain as that takes, never the top-level
label: ``jordan.lee@example`` and ``jordan.lee@uni``. A short name that
reads like another account's alias gives way the same way. Two slots with
the very same address (a personal and a team account) add their
organization: ``same·personal`` and ``same·Acme``; only when that cannot
tell them apart either does the slot: ``same·4``. Aliases themselves never
change: ``cc-swap alias`` already refuses a duplicate.

A name never is a whole address. The slot (``#4``) is what an account with
neither alias nor address is called — the one place a number is the name.

The rules depend only on the set of accounts, never their order, so a name
is the same on every surface and every run until an account is added,
removed or renamed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# An email inside a free-text name (an organization named after its owner's
# address: "x@example.com's Organization").
_EMAIL_RE = re.compile(r"([^\s@]+)@[\w-]+(?:\.[\w-]+)+")
ORG_MAX = 24


def short_name(email: str) -> str:
    """The part of ``email`` before the ``@``."""
    return (email or "").split("@", 1)[0].strip()


def _domain_labels(email: str) -> list[str]:
    domain = email.split("@", 1)[1] if "@" in email else ""
    labels = [p for p in domain.split(".") if p]
    return labels[:-1] if len(labels) > 1 else labels  # never the top-level label


def org_tag(org: str) -> str:
    """An organization name fit for a display name: ``personal`` for none,
    any address inside it cut to its local part, bounded."""
    text = " ".join(_EMAIL_RE.sub(r"\1", str(org or "")).split())
    if not text:
        return "personal"
    return text if len(text) <= ORG_MAX else text[: ORG_MAX - 1] + "…"


def _row(account: tuple) -> tuple[str, str, str, str]:
    slot, email, alias, *rest = account
    org = rest[0] if rest else ""
    return str(slot), str(email or ""), str(alias or "").strip(), str(org or "")


def display_names(accounts: Iterable[tuple]) -> dict[str, str]:
    """``{slot: name}`` for ``(slot, email, alias)`` or ``(slot, email,
    alias, organizationName)`` tuples."""
    rows = [_row(a) for a in accounts]
    out: dict[str, str] = {}
    aliases = {alias.lower() for _s, _e, alias, _o in rows if alias}
    plain: dict[str, list[tuple[str, str, str]]] = {}
    for slot, email, alias, org in rows:
        if alias:
            out[slot] = alias
        elif short_name(email):
            plain.setdefault(short_name(email).lower(), []).append((slot, email, org))
        else:
            out[slot] = f"#{slot}"
    for key, group in plain.items():
        if len(group) == 1 and key not in aliases:
            slot, email, _org = group[0]
            out[slot] = short_name(email)
            continue
        # One name per distinct address: the fewest domain labels that tell
        # the addresses apart (none, when every one is the same address and
        # no alias reads like it).
        by_address: dict[str, list[tuple[str, str, str]]] = {}
        for slot, email, org in group:
            by_address.setdefault(email.lower(), []).append((slot, email, org))
        base: dict[str, str] = {}
        if len(by_address) == 1 and key not in aliases:
            only = next(iter(by_address))
            base[only] = short_name(by_address[only][0][1])
        else:
            pending = sorted(by_address)
            deepest = max(len(_domain_labels(a)) for a in pending)
            depth = 1
            while pending:
                names = {}
                for address in pending:
                    email = by_address[address][0][1]
                    labels = _domain_labels(email)[:depth]
                    names[address] = short_name(email) + ("@" + ".".join(labels) if labels else "")
                counts: dict[str, int] = {}
                for name in names.values():
                    counts[name.lower()] = counts.get(name.lower(), 0) + 1
                left = []
                for address in pending:
                    name = names[address]
                    if (counts[name.lower()] == 1 and name.lower() not in aliases) or depth >= deepest:
                        base[address] = name
                    else:
                        left.append(address)
                pending = left
                depth += 1
        named: dict[str, str] = {}
        for address, members in by_address.items():
            if len(members) == 1:
                named[members[0][0]] = base[address]
                continue
            # The very same address in two slots: the organization tells.
            tags = [org_tag(org) for _s, _e, org in members]
            unique = len({t.lower() for t in tags}) == len(tags)
            for (slot, _email, _org), tag in zip(members, tags):
                named[slot] = f"{base[address]}·{tag if unique else slot}"
        # Whatever still reads alike (``a@x.com`` and ``a@x.org``) says its slot.
        seen: dict[str, int] = {}
        for name in named.values():
            seen[name.lower()] = seen.get(name.lower(), 0) + 1
        for slot, name in named.items():
            clash = seen[name.lower()] > 1 or name.lower() in aliases
            out[slot] = f"{name}·{slot}" if clash and not name.endswith(f"·{slot}") else name
    return {slot: out[slot] for slot, _e, _a, _o in rows}


def record_names(accounts: object) -> dict[str, str]:
    """``{slot: display name}`` for ``sequence.json``'s ``accounts`` records."""
    if not isinstance(accounts, Mapping):
        return {}

    def org(r: Mapping) -> str:
        uuid = str(r.get("organizationUuid") or "")
        if not uuid:
            return ""  # personal: an organizationName left without a uuid is none
        return str(r.get("organizationName") or "") or uuid[:8]

    return display_names(
        (str(n), str(r.get("email") or ""), str(r.get("alias") or ""), org(r))
        for n, r in accounts.items() if isinstance(r, Mapping)
    )


def name_of(names: Mapping[str, str], slot: object, email: object = "") -> str:
    """``slot``'s display name in ``names``; else the local part of
    ``email``; else ``#slot`` (an account nothing else names)."""
    key = "" if slot is None else str(slot)
    found = names.get(key) if key else None
    if found:
        return found
    local = short_name(str(email or ""))
    if local:
        return local
    return f"#{key}" if key else "?"


def names_list(names: Mapping[str, str], slots: Iterable[object]) -> str:
    """``a, b and c`` for these slots."""
    shown = [name_of(names, s) for s in slots]
    if len(shown) <= 1:
        return "".join(shown)
    return ", ".join(shown[:-1]) + " and " + shown[-1]


def view_name(view: object) -> str:
    """The display name a policy ``AccountView`` (or any object with
    ``number``/``email`` and an optional ``name``) carries."""
    return str(getattr(view, "name", "") or "") or name_of(
        {}, getattr(view, "number", None), getattr(view, "email", "")
    )


def match_name(names: Mapping[str, str], typed: str) -> str | None:
    """The slot whose display name is ``typed`` (case-insensitive; a ``·``
    in the name may be typed as ``.`` or ``:``), or None."""
    want = (typed or "").strip().lower()
    if not want:
        return None
    for variant in ("·", ".", ":"):
        for slot, name in names.items():
            if name.lower().replace("·", variant) == want:
                return slot
    return None


def cli_arg(name: str) -> str:
    """``name`` as a shell argument to ``cc-swap <command> NAME`` (quoted
    only when it has to be: ``same·Acme`` and ``jo@uni`` go as they are)."""
    import shlex

    text = str(name or "")
    if re.fullmatch(r"#\d+", text):  # an account only its slot names: the number
        return text[1:]
    if re.fullmatch(r"[\w.@·+:,/-]+", text):
        return text
    return shlex.quote(text)


def labeled(name: str, email: object) -> str:
    """For interactive CLI output only (never logs or notifications):
    ``name (email)`` when the name does not already say the address (an
    alias), else just ``name``."""
    email = str(email or "")
    if not email or not name or name.lower().startswith(short_name(email).lower()):
        return name or email
    return f"{name} ({email})"
