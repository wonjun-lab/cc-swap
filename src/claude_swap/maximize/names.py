"""The one name every account goes by on screen (cc-swap fork).

Slot numbers are upstream claude-swap's internal ids: they differ from one
machine to the next and say nothing about the account, so nothing a person
reads leads with one. Every user-facing surface (Fleet, the engine's log
lines, ``cc-swap why``/``doctor``/``history``, ``list``/``status``, desktop
notifications, CLI messages and errors) names an account by its display
name, and every command that takes an account takes that name too:

* its alias, when the user set one (``cc-swap alias``, Fleet ``n``);
* else the part of its address before the ``@``: ``dev.shared`` for
  ``dev.shared@example.com``.

Names stay unique. Two names are the same when they differ only in case or
in ``·``, ``.`` and ``:`` (:func:`fold`: what can be typed for a ``·``).
When two accounts would get the same short name (the same local part at
different domains), each says where it is from with as many of its domain
labels as that takes, joined by ``-`` and never the top-level label:
``jordan.lee@example`` and ``jordan.lee@uni``, ``jo@cs-stanford`` (no dot
after the ``@``, so no name reads like an address to the scrubbers in
notify.py and ledger.py). A short name that reads like another account's
alias, or that is all digits (it would read like a slot number), gives way
the same way: ``123456789@qq``. Two slots with the very same address (a
personal and a team account) add their organization: ``same·personal`` and
``same·Acme-Labs``; only when that cannot tell them apart either does the
slot: ``same·4``. Aliases themselves never change: ``cc-swap alias``
refuses one that is another account's name.

A name is never a whole address and never shortened. The slot (``#4``) is
what an account with neither alias nor address is called — the one place a
number is the name.

The rules depend only on the set of accounts, never their order, so a name
is the same on every surface and every run until an account is added,
removed or renamed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

# An email inside a free-text name (an organization named after its owner's
# address: "x@example.com's Organization").
_EMAIL_RE = re.compile(r"([^\s@]+)@[\w-]+(?:\.[\w-]+)+")
# What an organization tag keeps: typeable without quoting.
_ORG_DROP_RE = re.compile(r"[^\w.+-]+")


def short_name(email: str) -> str:
    """The part of ``email`` before the ``@``."""
    return (email or "").split("@", 1)[0].strip()


def fold(name: str) -> str:
    """The key two names compare by: case and ``·``/``.``/``:`` ignored."""
    return re.sub(r"[·.:]", ".", str(name or "").strip().lower())


def _domain_labels(email: str) -> list[str]:
    domain = email.split("@", 1)[1] if "@" in email else ""
    labels = [p for p in domain.split(".") if p]
    return labels[:-1] if len(labels) > 1 else labels  # never the top-level label


#: The organization claude.ai makes for a personal account:
#: ``x@example.com's Organization`` (or ``x's Organization``).
_PERSONAL_ORG_RE = re.compile(r"\s*(\S+)\s*['’]s\s+organi[sz]ation\s*", re.IGNORECASE)


def org_tag(org: str, email: str = "") -> str:
    """An organization name fit for a display name, typeable as it is:
    ``personal`` for none, and for the organization claude.ai names after
    its owner (``x@example.com's Organization``, or one named after
    ``email`` or its local part); else any address inside it cut to its
    local part, spaces as ``-``, nothing that needs quoting (``Acme Labs``
    → ``Acme-Labs``). Never shortened."""
    raw = str(org or "").strip()
    owner = _PERSONAL_ORG_RE.fullmatch(raw)
    if owner is not None:
        who = owner.group(1).lower()
        if "@" in who or not email or who in (email.lower(), short_name(email).lower()):
            return "personal"
    if email and raw.lower() in (email.lower(), short_name(email).lower()):
        return "personal"
    text = _EMAIL_RE.sub(r"\1", raw).replace("'", "").replace("’", "")
    text = "-".join(p for p in _ORG_DROP_RE.sub(" ", text).split() if p)
    return text or ("personal" if not str(org or "").strip() else "org")


def _row(account: tuple) -> tuple[str, str, str, str]:
    slot, email, alias, *rest = account
    org = rest[0] if rest else ""
    return str(slot), str(email or ""), str(alias or "").strip(), str(org or "")


def display_names(accounts: Iterable[tuple]) -> dict[str, str]:
    """``{slot: name}`` for ``(slot, email, alias)`` or ``(slot, email,
    alias, organizationName)`` tuples."""
    rows = [_row(a) for a in accounts]
    out: dict[str, str] = {}
    aliases = {fold(alias) for _s, _e, alias, _o in rows if alias}
    plain: dict[str, list[tuple[str, str, str]]] = {}
    for slot, email, alias, org in rows:
        if alias:
            out[slot] = alias
        elif short_name(email):
            plain.setdefault(fold(short_name(email)), []).append((slot, email, org))
        else:
            out[slot] = f"#{slot}"
    for key, group in plain.items():
        # A short name all digits would read like a slot number: it says
        # where it is from, as if another account had it.
        taken = key in aliases or key.isdigit()
        if len(group) == 1 and not taken:
            slot, email, _org = group[0]
            out[slot] = short_name(email)
            continue
        # One name per distinct address: the fewest domain labels that tell
        # the addresses apart (none, when every one is the same address and
        # nothing else reads like it).
        by_address: dict[str, list[tuple[str, str, str]]] = {}
        for slot, email, org in group:
            by_address.setdefault(email.lower(), []).append((slot, email, org))
        base: dict[str, str] = {}
        if len(by_address) == 1 and not taken:
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
                    names[address] = short_name(email) + ("@" + "-".join(labels) if labels else "")
                counts: dict[str, int] = {}
                for name in names.values():
                    counts[fold(name)] = counts.get(fold(name), 0) + 1
                left = []
                for address in pending:
                    name = names[address]
                    if (counts[fold(name)] == 1 and fold(name) not in aliases) or depth >= deepest:
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
            tags = [org_tag(org, email) for _s, email, org in members]
            unique = len({fold(t) for t in tags}) == len(tags)
            for (slot, _email, _org), tag in zip(members, tags):
                named[slot] = f"{base[address]}·{tag if unique else slot}"
        # Whatever still reads alike (``a@x.com`` and ``a@x.org``) says its slot.
        seen: dict[str, int] = {}
        for name in named.values():
            seen[fold(name)] = seen.get(fold(name), 0) + 1
        for slot, name in named.items():
            clash = seen[fold(name)] > 1 or fold(name) in aliases or fold(name).isdigit()
            out[slot] = f"{name}·{slot}" if clash and not name.endswith(f"·{slot}") else name
    # One pass over every name: a name made above may still read like one
    # from another group (``jo·personal`` for jo@x in two organizations and
    # ``jo.personal`` for jo.personal@y). Each such name that is not an
    # alias says its slot; the alias keeps its name. Repeated until no two
    # names read alike, so the outcome never depends on order.
    alias_slots = {slot for slot, _e, alias, _o in rows if alias}
    for _round in range(len(rows) + 1):
        counts: dict[str, int] = {}
        for name in out.values():
            counts[fold(name)] = counts.get(fold(name), 0) + 1
        clashing = [
            slot for slot, name in out.items()
            if counts[fold(name)] > 1 and slot not in alias_slots
            and not name.startswith("#")
        ]
        if not clashing:
            break
        for slot in clashing:
            out[slot] = f"{out[slot]}·{slot}"
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


_ROSTER_CACHE: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def roster_names(root: object) -> dict[str, str]:
    """``{slot: display name}`` from ``root``'s ``sequence.json``, read again
    only when the file changed (mtime and size); {} when it cannot be read.
    The one reader every surface without a switcher at hand uses."""
    path = Path(str(root)) / "sequence.json"
    try:
        st = path.stat()
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _ROSTER_CACHE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    names = record_names(data.get("accounts") if isinstance(data, dict) else None)
    _ROSTER_CACHE[str(path)] = (stamp, names)
    return dict(names)


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


def match_names(names: Mapping[str, str], typed: str) -> list[str]:
    """Every slot whose display name is ``typed``: the ones that match it
    exactly (case aside) if any, else the ones that match it with ``·``,
    ``.`` and ``:`` taken as one (:func:`fold`)."""
    want = (typed or "").strip()
    if not want:
        return []
    exact = [slot for slot, name in names.items() if name.lower() == want.lower()]
    if exact:
        return exact
    return [slot for slot, name in names.items() if fold(name) == fold(want)]


def match_name(names: Mapping[str, str], typed: str) -> str | None:
    """The one slot whose display name is ``typed`` (:func:`match_names`);
    None for none, and for more than one (ambiguous: never a guess)."""
    found = match_names(names, typed)
    return found[0] if len(found) == 1 else None


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
    if not email or not name:
        return name or email
    local = short_name(email).lower()
    low = name.lower()
    if low == local or low.startswith((local + "@", local + "·")):
        return name
    return f"{name} ({email})"
