"""``cc-swap prime [N ...] [--dry-run]`` — open idle accounts' 5h windows now.

Manual counterpart of the engine's automatic priming (spec §8.1 CLI): same
target rules and safety checks, no jitter, and it works with
``prime.enabled`` off. Prints slot numbers only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from claude_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.maximize.primer import Primer, prime_snapshot
from claude_swap.printer import dimmed, error
from claude_swap.settings import load_prime_settings, load_settings
from claude_swap.switcher import ClaudeAccountSwitcher

# Indirections so tests can drive time without sleeping.
_clock = time.time
_sleep = time.sleep

_FAILED = frozenset({"failed", "timeout", "unverified", "disabled"})


def _print_event(event: AutoSwitchEvent) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {event.human()}", flush=True)


def prime_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cc-swap prime",
        description=(
            "Open idle accounts' 5-hour windows now: one tiny request each "
            "through the official claude CLI, with only the access token, in "
            "an isolated profile. Same safety checks as automatic priming; "
            "works even when prime.enabled is false."
        ),
    )
    parser.add_argument(
        "accounts",
        nargs="*",
        metavar="NUM|EMAIL|ALIAS",
        help="Only these accounts (default: every eligible account)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be primed and why the rest are skipped; send nothing",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if (
            sys.platform != "win32"
            and os.geteuid() == 0
            and not switcher._is_running_in_container()
        ):
            error("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)
        numbers = (
            {switcher.resolve_account(a)[0] for a in args.accounts}
            if args.accounts
            else None
        )
        engine = AutoSwitchEngine(
            switcher,
            load_settings(switcher.backup_dir),
            _print_event,
            dry_run=args.dry_run,
            clock=_clock,
        )
        primer = Primer(engine, load_prime_settings(switcher.backup_dir), clock=_clock)
        entries = switcher.usage_entries_by_account(fetch=None)
        usage = {num: entry.decision_value() for num, entry in entries.items()}
        snap = prime_snapshot(engine, usage, _clock())
        lines = primer.plan_lines(snap, numbers)
        if args.dry_run:
            for line in lines or ["No accounts."]:
                print(line)
            return
        for line in lines:
            if " skip (" in line:
                print(dimmed(line))
        events = primer.prime_now(snap, numbers, sleep=_sleep)
        for event in events:
            _print_event(event)
        for num in primer.pending_accounts(snap):
            print(dimmed(
                f"#{num}  verification pending (the running engine or the next "
                "`cc-swap prime` checks it)"
            ))
        if not events and not primer.pending_accounts(snap):
            print(dimmed("Nothing to prime."))
        sys.exit(1 if any(e.outcome in _FAILED for e in events) else 0)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)
