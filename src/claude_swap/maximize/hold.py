"""Account hold — "stay on this account" (``cc-swap hold``, Fleet ``h``).

A switch makes Claude Code re-read the whole context on the new account, so
during a long task staying put is worth more than a slightly emptier
account. A hold pins the active account until a time, at most
:data:`MAX_HOLD_S` ahead. While it lasts and its slot is still the active
account, maximize sets aside its ``soft``, ``preempt`` and ``rebalance``
moves (reason code ``hold``). A hard mark (reached, or reached within
``forceEtaMin`` at the recent pace), 100% (``at-limit``) and a reset-aware
wait (``reset-wait``) behave exactly as they do without one: safety always
wins.

The marker lives in its own small file, like ``auto_off.json``::

    {"schemaVersion": 1,
     "hold": {"slot": "1", "until": 1.7e9, "since": 1.7e9, "by": "cli", "host": "mbp"}}

and is mirrored under ``accountHold`` in ``autoswitch_state.json``. Read here
and nowhere else:

* The file is authoritative; with no file, the state mirror counts.
* A hold ends on its own at ``until``. One more than :data:`MAX_HOLD_S`
  ahead (a bogus far-future value) is no hold.
* It applies only while its slot is the active account. Any change of the
  active account — a manual switch, an external ``/login``, a forced
  switch — ends it: the engine clears the marker on its next tick, and every
  reader treats a hold on another slot as none meanwhile.
* A damaged marker is no hold. Without a slot and an expiry nothing can be
  pinned, and the safety triggers never depended on it.

Reading is dependency-free (the engine, the CLI and the Fleet read model
all import it); writing takes the engine's state lock (``pause._StateFile``).
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOLD_FILENAME = "hold.json"
#: The marker's key inside :data:`HOLD_FILENAME`.
HOLD_KEY = "hold"
#: The mirror's key in ``autoswitch_state.json``.
STATE_KEY = "accountHold"
#: A hold is never longer than this.
MAX_HOLD_S = 24 * 3600.0
#: Slack for clocks a little apart between the writer and a reader.
_SKEW_S = 60.0

_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?")
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")


@dataclass(frozen=True)
class AccountHold:
    """A hold marker: ``slot`` is pinned until ``until`` (epoch s)."""

    slot: str
    until: float
    since: float | None = None
    by: str | None = None
    host: str | None = None

    def to_json(self) -> dict:
        return {
            "slot": self.slot, "until": self.until, "since": self.since,
            "by": self.by, "host": self.host,
        }


def hold_path(root: Path) -> Path:
    return Path(root) / HOLD_FILENAME


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_marker(raw: object) -> AccountHold | None:
    """A marker mapping as written, or None when it pins nothing."""
    if not isinstance(raw, Mapping):
        return None
    slot = raw.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, (str, int)) or str(slot) == "":
        return None
    until = _number(raw.get("until"))
    if until is None:
        return None
    return AccountHold(
        slot=str(slot), until=until, since=_number(raw.get("since")),
        by=_text(raw.get("by")), host=_text(raw.get("host")),
    )


def marker(root: Path, state: Mapping | None = None) -> AccountHold | None:
    """The hold marker as recorded, whether or not it still holds: the
    file's (a damaged one is none), else the state mirror's."""
    try:
        raw = json.loads(hold_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        mirror = state.get(STATE_KEY) if isinstance(state, Mapping) else None
        return parse_marker(mirror)
    except (OSError, ValueError, RecursionError):  # unreadable, not JSON, nested too deep
        return None
    return parse_marker(raw.get(HOLD_KEY) if isinstance(raw, dict) else None)


def current(hold: AccountHold | None, now: float) -> AccountHold | None:
    """``hold`` while it has not ended at ``now``; None once past ``until``
    or when ``until`` is further ahead than any hold can be."""
    if hold is None or hold.until <= now or hold.until - now > MAX_HOLD_S + _SKEW_S:
        return None
    return hold


def holding(
    hold: AccountHold | None, active: object, now: float, *, root: Path | None = None
) -> AccountHold | None:
    """The hold that pins ``active`` at ``now``, else None. With ``root``,
    also none once the switch ledger saw the active account leave the held
    slot after the hold was set (:func:`moved_away`), even if it is back."""
    live = current(hold, now)
    if live is None or active is None or live.slot != str(active):
        return None
    if root is not None and moved_away(root, live):
        return None
    return live


def moved_away(root: Path, hold: AccountHold) -> bool:
    """Whether the switch ledger (``switches.jsonl``, maximize/ledger.py)
    records the active account off ``hold.slot`` after ``hold.since``: an
    entry since then that leaves another slot or lands on one. A switch
    away and back between two engine ticks ends a hold too. Without a
    ledger (or a ``since``) nothing says it moved: False."""
    if hold.since is None:
        return False
    try:
        from claude_swap.maximize import ledger

        for path in ledger._generations(Path(root)):  # newest first
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in reversed(text.splitlines()):
                entry = ledger._parse(line)
                ts = _number(entry.get("ts")) if entry is not None else None
                if ts is None:
                    continue
                if ts <= hold.since:
                    return False
                src, dst = entry.get("from"), entry.get("to")
                if str(dst) != hold.slot or (src is not None and str(src) != hold.slot):
                    return True
    except Exception:  # an unreadable ledger says nothing
        return False
    return False


def read_state(root: Path) -> dict:
    """``autoswitch_state.json`` as a dict (``{}`` when missing or unreadable)."""
    try:
        raw = json.loads((Path(root) / "autoswitch_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return raw if isinstance(raw, dict) else {}


def read_hold(root: Path, *, now: float, state: Mapping | None = None) -> AccountHold | None:
    """The hold in force at ``now`` (any slot), or None."""
    return current(marker(root, read_state(root) if state is None else state), now)


def _sequence(root: Path) -> dict:
    try:
        data = json.loads((Path(root) / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def active_slot(root: Path) -> str | None:
    """The active slot as ``sequence.json`` records it, or None."""
    number = _sequence(root).get("activeAccountNumber")
    if isinstance(number, bool) or not isinstance(number, (int, str)) or str(number) == "":
        return None
    return str(number)


def live_identity(config_path: Path | None = None) -> tuple[str, str] | None:
    """``(email, organizationUuid)`` of the live login in ``~/.claude.json``
    (``paths.get_global_config_path``), or None when nobody is logged in."""
    from claude_swap.paths import get_global_config_path

    path = config_path if config_path is not None else get_global_config_path()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    account = raw.get("oauthAccount") if isinstance(raw, dict) else None
    email = account.get("emailAddress") if isinstance(account, dict) else None
    if not isinstance(email, str) or not email:
        return None
    org = account.get("organizationUuid")
    return email, org if isinstance(org, str) else ""


def live_slot(root: Path, *, config_path: Path | None = None) -> str | None:
    """The active slot the way ``ClaudeAccountSwitcher.current_account_number``
    resolves it — the slot holding the live login's (email, organization) —
    read-only (no switcher is built: ``why`` and ``doctor`` write nothing).
    A live login no slot holds is None, never a guess; only with nobody
    logged in does ``sequence.json``'s ``activeAccountNumber`` count. The
    CLI, ``auto status``, ``why`` and ``doctor`` all read the active slot
    here, so they agree with Fleet and the engine."""
    from claude_swap.switcher import ClaudeAccountSwitcher

    identity = live_identity(config_path)
    if identity is None:
        return active_slot(root)
    found = ClaudeAccountSwitcher._find_account_slot(_sequence(root), *identity)
    return None if found is None else str(found)


# -- writing -------------------------------------------------------------------------


def set_hold(
    root: Path,
    slot: str,
    until: float,
    *,
    by: str,
    now: float,
    host: str | None = None,
) -> AccountHold:
    """Pin ``slot`` until ``until`` (capped at ``now`` + :data:`MAX_HOLD_S`);
    returns the marker written. Writes the file, then the state mirror,
    under the engine's state lock (a damaged state file is left alone)."""
    from claude_swap import autoswitch as aw
    from claude_swap.maximize.pause import _StateFile, _state_is_damaged
    from claude_swap.settings import atomic_write_json

    until = min(float(until), now + MAX_HOLD_S)
    if until <= now:
        raise ValueError("a hold must end in the future")
    hold = AccountHold(str(slot), until, now, by, host)
    file = _StateFile(root)
    with file._state_lock():
        atomic_write_json(hold_path(root), {"schemaVersion": 1, HOLD_KEY: hold.to_json()})
        if not _state_is_damaged(file.state_path):
            state = file._read_state()
            state[STATE_KEY] = hold.to_json()
            state["schemaVersion"] = aw.STATE_SCHEMA_VERSION
            atomic_write_json(file.state_path, state)
    return hold


def clear_hold(root: Path) -> bool:
    """Lift any hold now (the file and the state mirror); whether there was
    one to lift. No write when there is none."""
    from claude_swap import autoswitch as aw
    from claude_swap.maximize.pause import _StateFile, _state_is_damaged
    from claude_swap.settings import atomic_write_json

    file = _StateFile(root)
    path = hold_path(root)
    if not path.exists() and not path.is_symlink() and STATE_KEY not in read_state(root):
        return False
    with file._state_lock():
        changed = False
        try:
            path.unlink()
            changed = True
        except FileNotFoundError:
            pass
        if not _state_is_damaged(file.state_path):
            state = file._read_state()
            if STATE_KEY in state:
                state.pop(STATE_KEY, None)
                state["schemaVersion"] = aw.STATE_SCHEMA_VERSION
                atomic_write_json(file.state_path, state)
                changed = True
        return changed


# -- words ------------------------------------------------------------------------------


def parse_duration(text: str) -> float:
    """``1h`` / ``90m`` / ``2h30m`` as seconds. Raises ValueError for
    anything else, and for zero."""
    m = _DURATION_RE.fullmatch(text.strip().lower()) if isinstance(text, str) else None
    if m is None or not (m.group(1) or m.group(2)):
        raise ValueError(f"expected a duration like 1h, 90m or 2h30m, got {text!r}")
    seconds = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    if seconds <= 0:
        raise ValueError(f"a hold needs a duration above zero, got {text!r}")
    return float(seconds)


def parse_until(text: str, now: float) -> float:
    """``23:00`` (local time) as the next such moment after ``now``: later
    today, else tomorrow, by the calendar (``date + 1 day``, never ``now +
    86400 s``: a daylight-saving day has 23 or 25 hours). On a fall-back
    day a time that happens twice is its next occurrence; a time the clocks
    skip (02:30 on a spring-forward day) is the next day it exists, which
    can be more than 24 hours away. Raises ValueError for anything else."""
    from datetime import date, timedelta

    m = _CLOCK_RE.fullmatch(text.strip()) if isinstance(text, str) else None
    if m is None:
        raise ValueError(f"expected a local time like 23:00, got {text!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"expected a local time like 23:00, got {text!r}")
    today = date.fromtimestamp(now)
    for offset in range(3):
        day = today + timedelta(days=offset)
        wanted = (day.year, day.month, day.day, hour, minute)
        found = []
        for isdst in (-1, 0, 1):  # both readings of an hour that happens twice
            at = time.mktime((*wanted, 0, 0, 0, isdst))
            if time.localtime(at)[:5] == wanted and at > now:  # it exists that day
                found.append(at)
        if found:
            return min(found)
    raise ValueError(f"{text} does not occur in the next days")


def left_text(seconds: float) -> str:
    """``2h`` / ``1h30m`` / ``45m`` (to the nearest minute, at least 1m)."""
    minutes = max(int(round(seconds / 60.0)), 1)
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h" if m == 0 else f"{h}h{m:02d}m"


def clock_text(ts: float, now: float) -> str:
    """Local ``15:30`` today, else ``Oct 4 09:00``."""
    at = time.localtime(ts)
    if at[:3] == time.localtime(now)[:3]:
        return time.strftime("%H:%M", at)
    return time.strftime("%b ", at) + str(at.tm_mday) + time.strftime(" %H:%M", at)


def until_text(hold: AccountHold, now: float) -> str:
    """``until 15:30 (2h left)``."""
    return f"until {clock_text(hold.until, now)} ({left_text(hold.until - now)} left)"


def held_message(
    hold: AccountHold,
    now: float,
    *,
    asked: float | None = None,
    name: str | None = None,
    root: Path | None = None,
) -> str:
    """``Holding dev until 15:30 (2h left)``, saying so when the end that
    was ``asked`` for lay more than 24h away and the hold was capped.
    ``name``: the held account's display name; without one it is read off
    ``root``'s sequence.json (no ``root``: ``#slot``)."""
    if not name:
        name = _label(hold.slot, _names(root) if root is not None else {})
    text = f"Holding {name} {until_text(hold, now)}"
    if asked is not None and asked - now > MAX_HOLD_S:
        clock = time.strftime("%H:%M", time.localtime(asked))
        text += f" ({clock} is {left_text(asked - now)} away; a hold is at most 24h)"
    return text


def safety_text(hard_5h: float, hard_7d: float) -> str:
    """What still moves you while a hold lasts: ``only hard 98%/100% will
    move you`` (one mark for both windows), else both marks named."""
    if hard_5h == hard_7d:
        return f"only hard {hard_5h:g}%/100% will move you"
    return f"only a hard mark (5h {hard_5h:g}%, 7d {hard_7d:g}%) or 100% will move you"


def short_name(record: Mapping | None) -> str:
    """An account's short name for messages that must carry no email: its
    alias, else the part of its address before the ``@``."""
    if not isinstance(record, Mapping):
        return ""
    alias = record.get("alias")
    name = alias if isinstance(alias, str) and alias.strip() else str(record.get("email") or "")
    return name.split("@", 1)[0].strip()[:32]


# -- `cc-swap hold` ------------------------------------------------------------------------


def _names(root: Path) -> dict[str, str]:
    from claude_swap.maximize.names import roster_names

    return roster_names(root)


def record_names(accounts: object) -> dict[str, str]:
    """``{slot: display name}`` for ``sequence.json``'s account records
    (maximize/names.py: the alias, else the short name, made unique)."""
    from claude_swap.maximize.names import record_names as names_of_records

    return names_of_records(accounts)


def display_name_hook(root: Path):
    """``autoswitch.account_names`` hook: ``(slot, email) -> short name``
    (alias, else the part before the ``@``), read fresh from sequence.json so
    a new alias shows on the next line. An unknown slot gets the short name of
    its address."""
    from claude_swap.maximize.names import name_of

    def name(slot: str, email: str) -> str:
        return name_of(_names(root), slot, email)

    return name


def _label(slot: str, names: Mapping[str, str]) -> str:
    from claude_swap.maximize.names import name_of

    return name_of(names, slot)


def _marks(root: Path) -> tuple[float, float]:
    from claude_swap.settings import load_maximize_settings

    try:
        mx = load_maximize_settings(root)
    except Exception:
        from claude_swap.settings import MaximizeSettings

        mx = MaximizeSettings()
    return mx.hard_5h, mx.hard_7d


def status_payload(hold: AccountHold | None, now: float) -> dict | None:
    """The hold as ``--json`` (and ``auto status --json``) print it."""
    if hold is None:
        return None
    return {
        "slot": hold.slot, "until": hold.until, "leftS": round(max(hold.until - now, 0.0)),
        "since": hold.since, "by": hold.by,
    }


def status_line(root: Path, now: float, *, state: Mapping | None = None) -> str | None:
    """``Holding main until 15:30 (2h left) — only hard 98%/100% will
    move you`` while a hold pins the active account (:func:`live_slot`),
    else None."""
    hold = holding(read_hold(root, now=now, state=state), live_slot(root), now, root=root)
    if hold is None:
        return None
    hard5, hard7 = _marks(root)
    return (
        f"Holding {_label(hold.slot, _names(root))} {until_text(hold, now)} — "
        f"{safety_text(hard5, hard7)}"
    )


USAGE = "cc-swap hold [DURATION | until HH:MM | off | status] [--json]"


def hold_command(argv: list[str], *, clock=None) -> None:
    """``cc-swap hold [DURATION|until HH:MM|off|status] [--json]``."""
    import argparse
    import socket
    import sys

    from claude_swap.paths import get_backup_root

    parser = argparse.ArgumentParser(
        prog="cc-swap hold",
        usage=USAGE,
        description=(
            "Stay on the active account for a while, so a long task is not moved "
            "mid-way (a switch makes Claude Code re-read the whole context). While "
            "the hold lasts, maximize skips its soft, preempt and rebalance moves; "
            "a hard mark, 100% and a reset-aware wait still switch. It ends by "
            "itself at its end time (at most 24h) or when the active account "
            "changes for any reason."
        ),
        epilog=(
            "examples: cc-swap hold 2h · cc-swap hold 90m · cc-swap hold 2h30m · "
            "cc-swap hold until 23:00 (local time, the next one) · cc-swap hold off · "
            "cc-swap hold status (also: no argument)"
        ),
    )
    parser.add_argument("what", nargs="*", metavar="DURATION|until HH:MM|off|status")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)
    root = get_backup_root()
    now = (clock or time.time)()
    words = [w.strip() for w in args.what if w.strip()]
    action = "status"
    until: float | None = None
    capped = ""
    if words in ([], ["status"]):
        action = "status"
    elif words == ["off"]:
        action = "off"
    else:
        try:
            if len(words) == 2 and words[0].lower() == "until":
                until = parse_until(words[1], now)
                if until - now > MAX_HOLD_S:  # a daylight-saving day: say so
                    capped = f"{words[1]} is {left_text(until - now)} away; a hold is at most 24h"
                    until = now + MAX_HOLD_S
            elif len(words) == 1:
                seconds = parse_duration(words[0])
                capped = "a hold is at most 24h" if seconds > MAX_HOLD_S else ""
                until = now + min(seconds, MAX_HOLD_S)
            else:
                raise ValueError(f"expected one of: {USAGE.removeprefix('cc-swap hold ')}")
        except ValueError as e:
            parser.error(str(e))
        action = "set"
    names = _names(root)
    changed = False
    active = live_slot(root)
    if action == "set":
        if active is None:
            print("No active account to hold (log in and cc-swap add first).", file=sys.stderr)
            sys.exit(1)
        host = socket.gethostname().split(".")[0] or None
        try:
            set_hold(root, active, until, by="cli", now=now, host=host)
        except ValueError as e:
            print(f"cc-swap hold: {e}", file=sys.stderr)
            sys.exit(2)
        changed = True
    elif action == "off":
        changed = clear_hold(root)
    live = holding(read_hold(root, now=now), active, now, root=root)
    if args.json:
        print(json.dumps({
            "schemaVersion": 1, "action": action, "changed": changed,
            "hold": status_payload(live, now),
        }))
        sys.exit(0)
    hard5, hard7 = _marks(root)
    if action == "off":
        print("Hold lifted: maximize moves you as usual again." if changed else "No hold to lift.")
        sys.exit(0)
    if live is None:
        stale = read_hold(root, now=now)
        if stale is not None:
            print(
                f"No hold: the hold on {_label(stale.slot, names)} no longer applies "
                f"({_label(active or '?', names)} is the active account)."
            )
        else:
            print("No hold. cc-swap hold 2h keeps you on the active account for two hours.")
        sys.exit(0)
    head = f"Holding {_label(live.slot, names)} {until_text(live, now)}"
    if capped:
        head += f" ({capped})"
    print(f"{head} — {safety_text(hard5, hard7)}.")
    print(
        "Soft, preempt and rebalance moves wait; it ends by itself, or when the "
        "active account changes. cc-swap hold off lifts it."
    )
    sys.exit(0)
