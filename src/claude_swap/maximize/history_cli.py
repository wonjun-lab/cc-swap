"""``cc-swap history [-n N] [--json]`` — the switch ledger (maximize/ledger.py).

Account names (maximize/names.py), host, trigger, who and why; no emails.
The ledger keeps slot numbers (``--json``), the lines name the accounts.
The same lines feed Fleet's history view.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from claude_swap.maximize import ledger

DEFAULT_COUNT = 20


def root_names(root: Path) -> dict[str, str]:
    """``{slot: display name}`` from ``root``'s sequence.json; {} when it
    cannot be read."""
    from claude_swap.maximize.names import record_names

    try:
        data = json.loads((Path(root) / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return record_names(data.get("accounts") if isinstance(data, dict) else None)


def _slot(value: object, names: Mapping[str, str] | None = None, stored: object = None) -> str:
    """An account in a line: its display name now, else the name the
    ledger recorded with the switch (an account removed since), else ``#N``."""
    from claude_swap.maximize.names import name_of

    if value is None:
        return "(none)"
    if str(value) not in (names or {}) and isinstance(stored, str) and stored:
        return stored
    return name_of(names or {}, value)


def entry_line(entry: Mapping[str, Any], names: Mapping[str, str] | None = None) -> str:
    """One ledger entry as a line: local time, host, from → to, trigger,
    who (actor/source, strategy), reason."""
    ts = entry.get("ts")
    when = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
        if isinstance(ts, (int, float)) and not isinstance(ts, bool)
        else "?"
    )
    who = [str(entry.get("source") or "?")]
    if entry.get("actor") == ledger.ACTOR_ENGINE:
        who.insert(0, "engine")
    if entry.get("strategy"):
        who.append(str(entry["strategy"]))
    line = (
        f"{when}  {entry.get('host') or '?'}  "
        f"{_slot(entry.get('from'), names, entry.get('fromName'))} -> "
        f"{_slot(entry.get('to'), names, entry.get('toName'))}  "
        f"{entry.get('trigger') or '?'} ({', '.join(who)})"
    )
    reason = entry.get("reason")
    if isinstance(reason, str) and reason:
        line += f"  {reason}"
    return line


def history_lines(
    entries: Sequence[Mapping[str, Any]], names: Mapping[str, str] | None = None
) -> list[str]:
    if not entries:
        return [
            "No switches recorded yet. Every switch from now on is recorded "
            "(engine, CLI, TUI, menu bar), and so is a login changed outside cc-swap."
        ]
    return [entry_line(e, names) for e in entries]


def drift_line(root: Path, live: str | None) -> str | None:
    """A note when the live login is not where the ledger last landed."""
    previous = ledger.drift(root, live)
    if previous is None:
        return None
    names = root_names(root)
    where = _slot(live, names) if live is not None else "an unmanaged (or no) login"
    return (
        f"Note: the live login is {where}, but the last recorded switch went to "
        f"{_slot(previous.get('to'), names, previous.get('toName'))} — it changed outside cc-swap "
        "(a /login in a Claude Code session?)"
    )


def history_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cc-swap history",
        description=(
            "Recent account switches on this machine: when, from which slot to "
            "which, what triggered it and who made it (engine, CLI, TUI, menu "
            "bar, or a login changed outside cc-swap). Accounts by name; no emails."
        ),
    )
    parser.add_argument(
        "-n", type=int, default=DEFAULT_COUNT, metavar="N",
        help=f"Show the last N switches (default {DEFAULT_COUNT}; 0 = all)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)
    if args.n < 0:
        parser.error("-n must be 0 or more")

    from claude_swap.paths import get_backup_root

    root = get_backup_root()
    live: str | None = None
    known = False
    try:
        from claude_swap.switcher import ClaudeAccountSwitcher

        switcher = ClaudeAccountSwitcher()
        root = switcher.backup_dir
        live = switcher.current_account_number()
        known = True
    except Exception:
        pass
    entries = ledger.read(root, None if args.n == 0 else args.n)
    if args.json:
        print(json.dumps({
            "schemaVersion": 1,
            "file": str(ledger.path_for(root)),
            "live": int(live) if live is not None and live.isdigit() else None,
            "entries": entries,
        }))
        sys.exit(0)
    for line in history_lines(entries, root_names(root)):
        print(line)
    note = drift_line(root, live) if known else None
    if note:
        print(note)
    sys.exit(0)
