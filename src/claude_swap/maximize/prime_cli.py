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
from collections.abc import Callable
from dataclasses import dataclass, field

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


_PENDING_NOTE = (
    "verification pending (the running engine or the next `cc-swap prime` checks it)"
)
_NOTHING = "Nothing to prime."
# Report lines the CLI prints dimmed (the rest print plain, as before).
_DIM_SUFFIXES = (_PENDING_NOTE, _NOTHING)


@dataclass(frozen=True)
class PrimeReport:
    """What one manual priming pass did, by slot number only (no emails).

    ``plan`` is ``Primer.plan``: ``(slot, text, would_prime)`` in slot order.
    Every account the plan would prime ends up in ``events``, ``pending`` or
    ``not_primed`` — never silently nowhere."""

    dry_run: bool
    plan: list[tuple[str, str, bool]]
    events: list[AutoSwitchEvent] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    not_primed: dict[str, str] = field(default_factory=dict)
    # Dry run only: what the real run would be stopped by (Primer.preflight),
    # and notes that change nothing about it (auto-off).
    blocked: str | None = None
    blocked_all: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        if self.dry_run:
            # Exactly when the real run would fail on the same blocker.
            return self.blocked is not None and (
                self.blocked_all or any(would for _n, _t, would in self.plan)
            )
        return any(getattr(e, "outcome", None) in _FAILED for e in self.events) or bool(
            self.not_primed
        )

    def tail_lines(self) -> list[str]:
        """The lines after the events: pending, not primed, or nothing to do."""
        lines = [f"#{num}  {_PENDING_NOTE}" for num in self.pending]
        lines += [
            f"#{num}  not primed ({self.not_primed[num]})"
            for num in sorted(self.not_primed, key=_slot_order)
        ]
        if not self.events and not self.pending and not self.not_primed:
            lines.append(_NOTHING)
        return lines

    def lines(self) -> list[str]:
        """The whole report as plain text: the plan's skip lines (all of the
        plan on a dry run), the events, then :meth:`tail_lines`."""
        if self.dry_run:
            head = [f"Priming is paused: {self.blocked}"] if self.blocked_all else []
            held = "priming is paused, see above" if self.blocked_all else self.blocked
            rows = [
                f"#{num}  not primed ({held})"
                if would and self.blocked is not None
                else f"#{num}  {text}"
                for num, text, would in self.plan
            ] or ["No accounts."]
            return head + rows + list(self.notes)
        out = [f"#{num}  {text}" for num, text, would in self.plan if not would]
        out += [event.human() for event in self.events]
        return out + self.tail_lines()


def _print_skips(plan: list[tuple[str, str, bool]]) -> None:
    for num, text, would_prime in plan:
        if not would_prime:
            print(dimmed(f"#{num}  {text}"))


def _auto_off_notes(backup_root) -> list[str]:
    from claude_swap.maximize.pause import read_auto_off

    try:
        off = read_auto_off(backup_root)
    except Exception:
        return []
    if off is None:
        return []
    return [
        "Note: auto-switching is OFF, so the engine does not prime; "
        "a manual cc-swap prime still does (cc-swap auto on turns it back on)."
    ]


def manual_prime(
    switcher,
    numbers: set[str] | None,
    *,
    dry_run: bool,
    emit: Callable[[AutoSwitchEvent], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    on_plan: Callable[[list[tuple[str, str, bool]]], None] | None = None,
    check_version: bool = True,
) -> PrimeReport:
    """One manual priming pass (``cc-swap prime``; the TUI's Prime now).

    Blocking: reads usage, may refresh tokens and run ``claude``. ``emit``
    receives the engine's own events (a quarantine); the primer's events come
    back in the report. ``on_plan`` sees the plan before anything launches,
    so a caller can show the skips while the launches run. ``check_version``
    False skips the Claude Code version guard (``prime verify --live`` only).

    Every ``claude`` it runs is a manual one (``claude_exec.manual``): a
    freshly changed binary runs with a warning instead of waiting."""
    from claude_swap.maximize import claude_exec

    if claude_exec.current_manual() is None:
        with claude_exec.manual("Fleet: prime now"):
            return manual_prime(
                switcher, numbers, dry_run=dry_run, emit=emit, sleep=sleep, clock=clock,
                on_plan=on_plan, check_version=check_version,
            )
    engine = AutoSwitchEngine(
        switcher,
        load_settings(switcher.backup_dir),
        emit,
        dry_run=dry_run,
        clock=clock,
    )
    primer = Primer(
        engine, load_prime_settings(switcher.backup_dir), clock=clock,
        version_gate=check_version,
    )
    entries = switcher.usage_entries_by_account(fetch=None)
    usage = {num: entry.decision_value() for num, entry in entries.items()}
    snap = prime_snapshot(engine, usage, clock())
    plan = primer.plan(snap, numbers)
    if dry_run:
        # Same blockers as the real run below, so the two never disagree.
        blocked, blocked_all = primer.preflight()
        return PrimeReport(
            True, plan, blocked=blocked, blocked_all=blocked_all,
            notes=_auto_off_notes(switcher.backup_dir),
        )
    if on_plan is not None:
        on_plan(plan)
    events = primer.prime_now(snap, numbers, sleep=sleep)
    pending = primer.pending_accounts(snap)
    # Every account the plan would prime gets a line: an event, pending,
    # or the reason it was not primed — never a silent no-op.
    # (A missing `claude` already produced its own event for all of them.)
    not_primed = dict(primer.not_primed)
    reported = {getattr(e, "account", None) for e in events} | set(pending)
    disabled = any(getattr(e, "outcome", None) == "disabled" for e in events)
    for num, _text, would_prime in plan:
        if would_prime and not disabled and num not in reported and num not in not_primed:
            not_primed[num] = "no longer a priming target"
    return PrimeReport(False, plan, list(events), pending, not_primed)


def _warn(message: str) -> None:
    """A ``claude_exec`` warning (a freshly changed claude run anyway), on stderr."""
    from claude_swap.printer import warning

    warning(message, file=sys.stderr)


def prime_command(argv: list[str]) -> None:
    if argv and argv[0] == "verify":
        from claude_swap.maximize.prime_verify import verify_command

        verify_command(argv[1:])
        return
    parser = argparse.ArgumentParser(
        prog="cc-swap prime",
        description=(
            "Open idle accounts' 5-hour windows now: one tiny request each "
            "through the official claude CLI, with only the access token, in "
            "an isolated profile. Same safety checks as automatic priming; "
            "works even when prime.enabled is false. After a Claude Code "
            "update, priming pauses until `cc-swap prime verify` passes (the "
            "engine runs it itself unless prime.autoVerify is false)."
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
        from claude_swap.maximize import claude_exec

        with claude_exec.manual("cc-swap prime", warn=_warn):
            report = manual_prime(
                switcher,
                numbers,
                dry_run=args.dry_run,
                emit=_print_event,
                sleep=_sleep,
                clock=_clock,
                on_plan=_print_skips,
            )
        if args.dry_run:
            for line in report.lines():
                print(line)
            if report.failed:
                sys.exit(1)
            return
        for event in report.events:
            _print_event(event)
        for line in report.tail_lines():
            print(dimmed(line) if line.endswith(_DIM_SUFFIXES) else line)
        sys.exit(1 if report.failed else 0)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)
