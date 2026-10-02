"""``cc-swap history [-n N] [--json]`` — the switch ledger (maximize/ledger.py).

Slot numbers, host, trigger, who and why; no emails. The same lines feed
Fleet's history view.
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


def _slot(value: object) -> str:
    return f"#{value}" if value is not None else "(none)"


def entry_line(entry: Mapping[str, Any]) -> str:
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
        f"{_slot(entry.get('from'))} -> {_slot(entry.get('to'))}  "
        f"{entry.get('trigger') or '?'} ({', '.join(who)})"
    )
    reason = entry.get("reason")
    if isinstance(reason, str) and reason:
        line += f"  {reason}"
    return line


def history_lines(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    if not entries:
        return [
            "No switches recorded yet. Every switch from now on is recorded "
            "(engine, CLI, TUI, menu bar), and so is a login changed outside cc-swap."
        ]
    return [entry_line(e) for e in entries]


def drift_line(root: Path, live: str | None) -> str | None:
    """A note when the live login is not where the ledger last landed."""
    previous = ledger.drift(root, live)
    if previous is None:
        return None
    where = f"#{live}" if live is not None else "an unmanaged (or no) login"
    return (
        f"Note: the live login is {where}, but the last recorded switch went to "
        f"{_slot(previous.get('to'))} — it changed outside cc-swap "
        "(a /login in a Claude Code session?)"
    )


def history_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cc-swap history",
        description=(
            "Recent account switches on this machine: when, from which slot to "
            "which, what triggered it and who made it (engine, CLI, TUI, menu "
            "bar, or a login changed outside cc-swap). Slot numbers only."
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
    for line in history_lines(entries):
        print(line)
    note = drift_line(root, live) if known else None
    if note:
        print(note)
    sys.exit(0)
