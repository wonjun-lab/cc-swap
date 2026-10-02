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
from claude_swap.maximize.primer import Primer, _slot_order, prime_snapshot
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
        plan = primer.plan(snap, numbers)
        if args.dry_run:
            for num, text, _ in plan:
                print(f"#{num}  {text}")
            if not plan:
                print("No accounts.")
            return
        for num, text, would_prime in plan:
            if not would_prime:
                print(dimmed(f"#{num}  {text}"))
        events = primer.prime_now(snap, numbers, sleep=_sleep)
        for event in events:
            _print_event(event)
        pending = primer.pending_accounts(snap)
        for num in pending:
            print(dimmed(
                f"#{num}  verification pending (the running engine or the next "
                "`cc-swap prime` checks it)"
            ))
        # Every account the plan would prime gets a line: an event, pending,
        # or the reason it was not primed — never a silent no-op.
        # (A missing `claude` already printed its own event for all of them.)
        not_primed = dict(primer.not_primed)
        reported = {getattr(e, "account", None) for e in events} | set(pending)
        disabled = any(e.outcome == "disabled" for e in events)
        for num, _text, would_prime in plan:
            if would_prime and not disabled and num not in reported and num not in not_primed:
                not_primed[num] = "no longer a priming target"
        for num in sorted(not_primed, key=_slot_order):
            print(f"#{num}  not primed ({not_primed[num]})")
        if not events and not pending and not not_primed:
            print(dimmed("Nothing to prime."))
        failed = any(e.outcome in _FAILED for e in events) or bool(not_primed)
        sys.exit(1 if failed else 0)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)
