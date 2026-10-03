"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

from claude_swap import __version__, paths, printer
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import error_envelope
from claude_swap.printer import (
    accent,
    bolded,
    dimmed,
    error,
    force_utf8_output,
    muted,
    warning,
)
from claude_swap.settings import load_ui_settings
from claude_swap.switcher import SWITCH_REFUSED_REASONS, ClaudeAccountSwitcher


def _prog_name() -> str:
    """The command name to show in usage/help.

    argparse otherwise defaults to ``os.path.basename(sys.argv[0])``, which for
    an installed entry-point shim renders as an ugly absolute path (e.g.
    ``python.exe C:\\Users\\me\\.local\\bin\\cswap``). We strip that down to the
    bare command the user typed (``cswap`` / ``claude-swap``), falling back to
    ``cswap`` for ``python -m claude_swap`` and odd launchers.
    """
    name = os.path.basename(sys.argv[0] or "")
    for ext in (".exe", ".pyw", ".py"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if not name or name in {"__main__", "python", "python3", "py"}:
        return "cswap"
    return name


# Memorable subcommand aliases → the long-standing flags they expand to. Lets
# users type `cswap list`, `cswap status`, `cswap add`, etc. instead of `--list`
# / `--status` / `--add-account`, which all still work. `switch` is special-cased
# below (a bare `switch` rotates; `switch <target>` jumps to one account) and
# `run`/`auto` keep their own pre-dispatch parsers, so none of those are listed here.
_SUBCOMMAND_FLAGS = {
    "help": "--help",
    "list": "--list",
    "ls": "--list",
    "status": "--status",
    "add": "--add-account",
    "add-token": "--add-token",
    "remove": "--remove-account",
    "rm": "--remove-account",
    "disable": "--disable-account",
    "enable": "--enable-account",
    "export": "--export",
    "import": "--import",
    "import-usage": "--import-usage",
    "purge": "--purge",
    "upgrade": "--upgrade",
    "update": "--upgrade",
    "tui": "--tui",
    "watch": "--watch",
    "menubar": "--menubar",
}

# cc-swap fork subcommands, pre-dispatched like `alias`/`map` (a positional
# verb can't live in the main parser's mutually-exclusive flag group). Values
# are function NAMES, resolved at call time, so tests can patch the module
# attribute (``patch("claude_swap.cli._last_resort_command")``). New fork
# commands (prime, service) register here; main() has a single hook for all.
_FORK_COMMANDS: dict[str, str] = {
    "claude-update": "_claude_update_command",
    "last-resort": "_last_resort_command",
    "prime": "_prime_command",
    "service": "_service_command",
    "doctor": "_doctor_command",
    "init": "_init_command",
    "why": "_why_command",
    "history": "_history_command",
    "hold": "_hold_command",
    "notify": "_notify_command",
}


def _translate_subcommand(argv: list[str]) -> list[str]:
    """Rewrite a leading memorable subcommand into the equivalent flag argv.

    ``argv`` is the args after the program name. The rewrite only fires when the
    first token is a recognized verb (which never starts with '-'), so the
    established ``--flag`` interface — and every existing test that drives it —
    is left untouched. Tokens after the verb pass through verbatim, so flags
    like ``--json``, ``--strategy``, ``--slot``, and ``--force`` keep combining
    exactly as before (e.g. ``cswap switch --strategy best``, ``cswap list --json``).
    """
    if not argv:
        return argv

    verb, rest = argv[0], argv[1:]

    if verb == "switch":
        # Bare `switch` rotates; `switch <num|email>` jumps to that account.
        if rest and not rest[0].startswith("-"):
            return ["--switch-to", *rest]
        return ["--switch", *rest]

    flag = _SUBCOMMAND_FLAGS.get(verb)
    if flag is not None:
        return [flag, *rest]

    return argv


def _run_command(argv: list[str]) -> None:
    """Handle `cswap run NUM|EMAIL [--no-share] [-- <claude args>]`.

    Pre-dispatched before the main parser is built: a positional subcommand
    can't coexist with main()'s mutually-exclusive flag group, and this keeps
    the existing parser untouched. Limitation: `run` must be the
    first argument (`cswap --debug run 2` is not supported; use
    `cswap run 2 --debug`).

    On POSIX this execs claude and never returns; on Windows it exits with
    claude's return code. Either way the post-dispatch update check in
    main() is unreachable, which is intended.
    """
    # Everything after the first `--` is forwarded to claude verbatim.
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} run",
        description=(
            "[EXPERIMENTAL] Launch Claude Code as a stored account in this "
            "terminal only (the default login and other terminals are "
            "unaffected)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap run 2
  cswap run user@example.com
  cswap run 2 --no-share
  cswap run 2 --share-history
  cswap run 2 --require-session
  cswap run 2 -- --resume
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to run (number or email). Omit to use the current "
        "directory's mapping (see `cswap map`).",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Don't share settings/keybindings/CLAUDE.md/skills/commands/agents "
            "from ~/.claude into the session profile (and remove previously "
            "shared items)"
        ),
    )
    parser.add_argument(
        "--share-history",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Share conversation history (projects/ and history.jsonl) from "
            "~/.claude into the session profile, so every account sees one "
            "unified history. History the profile already accumulated is "
            "merged into ~/.claude first. --no-share-history restores "
            "per-account history (the default). Not supported on Windows."
        ),
    )
    parser.add_argument(
        "--require-session",
        action="store_true",
        help=(
            "Refuse to launch when the account is already the active default "
            "login, instead of running plain claude on that login (which a "
            "later switch could pull out from under the session)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(head)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        from claude_swap.session import SessionManager

        manager = SessionManager(switcher)

        if args.account is not None:
            manager.run(
                args.account,
                tail,
                share=not args.no_share,
                share_history=args.share_history,
                require_session=args.require_session,
            )
            return  # only reachable in tests where exec/exit is mocked

        # No account given: resolve from the current directory's mapping.
        slot, email = switcher.slot_for_directory(os.getcwd())
        if slot is not None:
            manager.run(
                slot,
                tail,
                share=not args.no_share,
                share_history=args.share_history,
                require_session=args.require_session,
            )
            return  # only reachable in tests
        if email is not None:
            warning(
                f"Mapped account {email} no longer exists — "
                "launching the default account."
            )
        else:
            print(
                dimmed(
                    f"No account mapped for {os.getcwd()} — "
                    "launching the default account."
                )
            )
        manager.exec_default(tail)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _guard_root(switcher: ClaudeAccountSwitcher) -> None:
    """Refuse to run as root outside a container (shared by run/map/unmap)."""
    if sys.platform != "win32":
        if os.geteuid() == 0 and not switcher._is_running_in_container():
            error("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)


def _map_command(argv: list[str]) -> None:
    """Handle `cswap map [NUM|EMAIL] [PATH]`.

    With no NUM|EMAIL, lists all mappings. Otherwise maps PATH (default: the
    current directory) to the given account. Pre-dispatched before the main
    parser for the same reason as `run` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog="cswap map",
        description=(
            "Map a stored account to a directory so `cswap run` (with no "
            "account) auto-launches it there. With no arguments, lists all "
            "mappings."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap map 2 ~/work/client-app
  cswap map user@example.com          # map the current directory
  cswap map                           # list all mappings
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to map (number or email). Omit to list mappings.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="Directory to map (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            switcher.list_mappings()
            return

        from claude_swap.mappings import MappingStore, normalize_path

        store = MappingStore(switcher.backup_dir)
        account_num, email, org_uuid = switcher.resolve_account(args.account)
        target = args.path or os.getcwd()
        if not os.path.isdir(target):
            warning(f"Warning: {target} is not an existing directory (mapping it anyway)")
        previous = store.get(target)
        store.set(target, email, org_uuid)

        shown = normalize_path(target)
        if previous and previous.get("email") != email:
            prev_email = previous.get("email")
            print(
                f"{accent('Mapped')} {shown} → Account-{account_num} ({email}) "
                f"{muted(f'(was {prev_email})')}"
            )
        else:
            print(f"{accent('Mapped')} {shown} → Account-{account_num} ({email})")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unmap_command(argv: list[str]) -> None:
    """Handle `cswap unmap [PATH]` — remove a directory→account mapping."""
    parser = argparse.ArgumentParser(
        prog="cswap unmap",
        description="Remove a directory → account mapping (default: current directory).",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="Directory to unmap (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        from claude_swap.mappings import MappingStore, normalize_path

        store = MappingStore(switcher.backup_dir)
        target = args.path or os.getcwd()
        shown = normalize_path(target)
        if store.remove(target):
            print(f"{accent('Unmapped')} {shown}")
        else:
            print(dimmed(f"No mapping for {shown}"))
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unclaimed_command(argv: list[str]) -> None:
    """Handle `cswap unclaimed [--purge ID]` — inspect or drop a stash row.

    The stash holds credential bytes a switch or a consume gate could not
    attribute to a slot. Rows normally clear themselves (the next gate pass
    adopts or retires them), but two states need a human: a row whose bytes
    are unreadable until a keychain is unlocked or a mode is fixed, and one
    whose metadata was lost, which no pass can ever adopt. ``--json`` lists
    only bare ids, so without this there is nothing to look at and nothing to
    drop short of hand-editing the manifest.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} unclaimed",
        description=(
            "List stashed credential entries, or purge one by id. "
            "Purging deletes the bytes — recovery is /login + `cc-swap add`."
        ),
    )
    parser.add_argument(
        "--purge",
        metavar="ID",
        help="Delete this entry's bytes and manifest row",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        entries = switcher.list_unclaimed_credentials()

        if args.purge:
            if args.purge not in entries:
                error(f"Error: no unclaimed entry {args.purge}")
                sys.exit(1)
            switcher._store._remove_unclaimed_credential(args.purge)
            print(f"{accent('Purged')} {args.purge}")
            return

        if not entries:
            print(dimmed("No unclaimed credential entries"))
            return
        for entry_id, meta in sorted(entries.items()):
            slot = meta.get("configSlot") or "?"
            reason = meta.get("reason") or "orphaned (no manifest row)"
            print(f"{entry_id}  slot {slot}  {reason}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _swap_command(argv: list[str]) -> None:
    """Handle `cswap swap NUM|EMAIL|ALIAS NUM|EMAIL|ALIAS`.

    Exchanges the two accounts' slot numbers (list order and numeric
    targets). Pre-dispatched before the main parser for the same reason as
    `alias` (the main parser's required mutually-exclusive group can't hold
    a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} swap",
        description=(
            "Exchange two accounts' slot numbers, so they trade places in "
            "`cswap list` and as numeric targets. Aliases, backups, and "
            "session history move with their account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap swap 1 2
  cswap swap dev user@example.com
        """,
    )
    parser.add_argument("first", metavar="NUM|EMAIL|ALIAS", help="One account")
    parser.add_argument("second", metavar="NUM|EMAIL|ALIAS", help="The other account")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        num_a, num_b = switcher.swap_accounts(args.first, args.second)
        print(f"{accent('Swapped')} Account {num_a} and Account {num_b}:")
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        for num in sorted((num_a, num_b), key=int):
            email = accounts.get(num, {}).get("email", "")
            print(f"  {num}: {email}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _move_command(argv: list[str]) -> None:
    """Handle `cswap move NUM|EMAIL|ALIAS SLOT`.

    Assigns an account to a specific slot number. If the slot is empty the
    account is relocated there (its old slot is freed); if it is occupied the
    two accounts trade places. `swap a b` is exactly `move a <b's slot>`.
    Pre-dispatched before the main parser for the same reason as `alias`.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} move",
        description=(
            "Assign an account to a slot number. An empty slot relocates the "
            "account there and frees its old slot; an occupied slot swaps the "
            "two. Aliases, backups, and session history move with the account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap move user@example.com 1   move an account onto shortcut 1
  cswap move dev 1                by alias
  cswap move 2 1                  by number (swaps if slot 1 is taken)
        """,
    )
    parser.add_argument("account", metavar="NUM|EMAIL|ALIAS", help="Account to move")
    parser.add_argument("slot", metavar="SLOT", help="Destination slot number")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        num_src, num_target, swapped = switcher.move_account(args.account, args.slot)
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        if num_src == num_target:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{dimmed('Already in')} slot {num_target}: {email}")
        elif swapped:
            print(f"{accent('Swapped')} Account {num_src} and Account {num_target}:")
            for num in sorted((num_src, num_target), key=int):
                email = accounts.get(num, {}).get("email", "")
                print(f"  {num}: {email}")
        else:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{accent('Moved')} {email} to slot {num_target}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _alias_command(argv: list[str]) -> None:
    """Handle `cswap alias [NUM|EMAIL] [NAME] [--unset]`.

    With no arguments, lists all aliases. Otherwise sets (or, with --unset,
    removes) the alias for the given account. Pre-dispatched before the main
    parser for the same reason as `map` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog="cswap alias",
        description=(
            "Set, remove, or list a short display alias for an account. "
            "Once set, the alias can be used anywhere an account number or "
            "email is accepted (switch, remove, run, map)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap alias 2 dev
  cswap alias user@example.com dev
  cswap alias 2 --unset
  cswap alias                         # list all aliases
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to alias (number or email). Omit to list aliases.",
    )
    parser.add_argument(
        "alias_name",
        nargs="?",
        metavar="NAME",
        help="Alias to set (letters, digits, ., -, _; not purely numeric).",
    )
    parser.add_argument("--unset", action="store_true", help="Remove the account's alias")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    if args.unset and args.alias_name:
        parser.error("--unset does not take a NAME argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and not args.alias_name:
        parser.error("NAME is required (or pass --unset to remove the alias)")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            rows = switcher.list_aliases()
            if not rows:
                print(dimmed("No aliases set"))
                return
            print(bolded("Aliases:"))
            for num, alias_name, email in rows:
                print(f"  {num}: {alias_name} {muted(f'({email})')}")
            return

        if args.unset:
            account_num = switcher.unset_alias(args.account)
            print(f"{accent('Removed alias')} for Account {account_num}")
        else:
            account_num, normalized = switcher.set_alias(args.account, args.alias_name)
            print(f"{accent('Set alias')} '{normalized}' for Account {account_num}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _finite_float(value: str) -> float:
    """argparse ``type=`` for a flag that takes a number: nan and inf parse as
    floats, but they are not thresholds, so reject them here instead of
    letting them reach the settings merge."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from None
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError(f"expected a finite number, got {value!r}")
    return number


def _mark_pct(flag: str):
    """argparse ``type=`` for ``auto --soft5h/--hard5h/--soft7d/--hard7d``:
    a finite number inside that setting's range (maximize.<flag>). Out of
    range is rejected here, with the range, instead of being clamped into it
    and reported later as a soft/hard conflict."""
    from claude_swap.settings import SETTING_SPECS

    spec = SETTING_SPECS[f"maximize.{flag.lstrip('-')}"]

    def parse(value: str) -> float:
        number = _finite_float(value)
        if not spec.lo <= number <= spec.hi:
            raise argparse.ArgumentTypeError(
                f"{value!r} is out of range: expected a percentage between "
                f"{spec.lo:g} and {spec.hi:g}"
            )
        return number

    parse.__name__ = "percentage"
    return parse


def _auto_command(argv: list[str]) -> None:
    """Handle `cswap auto [--once] [--json] [...]`.

    Pre-dispatched before the main parser is built, like `run` (and with the
    same limitation: `auto` must be the first argument). Runs the auto-switch
    engine — a foreground loop by default, or a single evaluate-and-maybe-
    switch tick with --once whose exit code reports the outcome (for cron/
    systemd timers): 0 switched, 1 error, 2 no action needed, 3 blocked
    (no viable target / all accounts exhausted).
    """
    import signal
    import time as _time

    if argv and argv[0] in ("on", "off", "status"):  # cc-swap: persistent auto on/off
        from claude_swap.maximize.pause import auto_command

        auto_command(argv)
        return

    parser = argparse.ArgumentParser(
        prog="cswap auto",
        description=(
            "Automatically switch accounts when the active one nears its "
            "5h/7d rate limit. Runs a foreground polling loop; use --once "
            "for a single tick (cron-friendly)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes with --once:
  0  switched to another account
  1  error (network trouble, lock contention, ...)
  2  no action needed
  3  blocked: wanted to switch but no viable target / all exhausted

Exit code 4 (any mode): another auto-switch engine already holds the engine
lease (the cc-swap service, another `cc-swap auto`, a TUI auto screen).
`--once --dry-run` needs no lease and always runs.

Examples:
  cswap auto                       # foreground loop, switch at 90%% used
  cswap auto --threshold 80        # switch earlier
  cswap auto --model Fable         # also switch when the Fable weekly limit is hit
  cswap auto --json                # one JSON event per line (for scripts)
  cswap auto --once; echo $?       # single tick, outcome in exit code
  cswap auto --dry-run             # log decisions, never actually switch
  cc-swap auto --strategy maximize # per-window soft/hard marks (cc-swap)
  cc-swap auto --strategy maximize --soft5h 40 --hard5h 90

Defaults live in settings.json in the backup root; flags override them.
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate once, maybe switch, and exit (exit code = outcome)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON event per line on stdout",
    )
    parser.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help="Poll interval in loop mode (min 15; default 60)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="PCT",
        help=(
            "Switch when the active account's binding 5h/7d window reaches "
            "this utilization (50-99.9; default 90)"
        ),
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        metavar="SECONDS",
        help="Minimum time between proactive switches (default 300)",
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "Also switch when a per-model weekly limit is hit, not just the "
            "account-wide 5h/7d windows. One name or a comma-separated list "
            "(e.g. Fable, Opus, Sonnet, Haiku, or 'Fable,Opus'), or 'all' "
            "for every per-model window an account reports"
        ),
    )
    parser.add_argument(
        "--include-api-key-accounts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow switching onto managed API-key accounts as a last resort "
            "(they bill per token; default: excluded)"
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=("best", "consume-first", "maximize"),
        default=None,
        help=(
            "Target selection: 'best' (most quota left; default), "
            "'consume-first' (proactively use the account whose weekly window "
            "resets soonest), or 'maximize' (cc-swap: separate 5h/7d soft and "
            "hard marks, spend the weekly quota that would expire first)"
        ),
    )
    for flag, window, kind, default in (
        ("--soft5h", "5h", "soft", 50),
        ("--hard5h", "5h", "hard", 95),
        ("--soft7d", "7d", "soft", 90),
        ("--hard7d", "7d", "hard", 98),
    ):
        when = "at the next idle moment" if kind == "soft" else "immediately"
        parser.add_argument(
            flag,
            type=_mark_pct(flag),
            metavar="PCT",
            help=(
                f"maximize only: {window} {kind} mark, switch {when} once the "
                f"active account's {window} window reaches it "
                f"(1-99.9; default {default}; soft <= hard)"
            ),
        )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and report, but never switch or write state",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    from claude_swap.maximize.lease import (
        EXIT_ENGINE_BUSY,
        EngineBusyError,
        claim_for_auto,
    )
    from claude_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent, account_names
    from claude_swap.maximize.hold import display_name_hook
    from claude_swap.maximize.logrotate import LogRotator
    from claude_swap.printer import accent, print_line, stdout_gone, yellowed
    from claude_swap.settings import (
        MAXIMIZE_CLI_FLAGS,
        load_maximize_settings,
        load_settings,
        merge_maximize_cli,
        merged_with_cli,
    )

    # print_line: a closed pipe (`auto --once | head -1`) must not abort the
    # tick from inside the engine's event callback. `--once` then finishes
    # its tick silently; the loop stops after the tick it is in (nobody would
    # see it switch any more), exiting 0 with the lease released.
    running: list = []  # the loop-mode engine, once built

    def emit_line(text: str) -> None:
        print_line(text)
        if stdout_gone() and running:
            running[0].stop()

    def jsonl_emit(event: AutoSwitchEvent) -> None:
        emit_line(json.dumps(event.to_json()))

    def human_emit(event: AutoSwitchEvent) -> None:
        stamp = _time.strftime("%H:%M:%S")
        # cc-swap: under maximize, name accounts by alias / short name, not
        # address (auto.log gets pasted into issues). The strategy is read per
        # line: a hot reload can change it.
        live = running[0].settings if running else settings
        if live.strategy == "maximize":
            with account_names(display_name_hook(switcher.backup_dir)):
                line = event.human()
        else:
            line = event.human()
        if event.kind == "switch":
            line = accent(line)
        elif event.kind in ("error", "account-quarantined"):
            line = yellowed(line)
        elif event.kind in ("poll", "no-switch", "sleep"):
            line = dimmed(line)
        emit_line(f"{stamp}  {line}")

    lease = None
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        # One engine per machine (maximize/lease.py), held until this process
        # exits. `--once --dry-run` is a read-only probe and needs none.
        lease = claim_for_auto(
            switcher.backup_dir, once=args.once, dry_run=args.dry_run
        )
        settings = merged_with_cli(load_settings(switcher.backup_dir), args)
        # cc-swap: --soft5h/--hard5h/--soft7d/--hard7d only mean something to
        # the maximize strategy (flag or settings.json); reject, don't ignore.
        given = [
            f"--{attr}"
            for attr, _ in MAXIMIZE_CLI_FLAGS
            if getattr(args, attr) is not None
        ]
        if given and settings.strategy != "maximize":
            parser.error(
                f"{', '.join(given)} only {'applies' if len(given) == 1 else 'apply'}"
                " to the maximize strategy "
                "(--strategy maximize or autoswitch.strategy maximize)"
            )
        maximize = None
        engine_kwargs = {}
        if settings.strategy == "maximize":
            # Raises ConfigError (exit 1 below) when the flags put a soft mark
            # above its hard cap. The engine gets the flags themselves, not the
            # merged values, so it can re-apply them over every hot reload.
            maximize = merge_maximize_cli(
                load_maximize_settings(switcher.backup_dir), args
            )
            engine_kwargs["maximize_cli"] = args
        engine = AutoSwitchEngine(
            switcher,
            settings,
            jsonl_emit if args.json else human_emit,
            dry_run=args.dry_run,
            **engine_kwargs,
        )

        if args.once:
            sys.exit(engine.tick().value)

        running.append(engine)
        # Loop mode: SIGTERM (systemd stop) exits the loop cleanly.
        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
        # As the launchd service, keep auto.log / auto.err.log bounded: rotate
        # now and then at most hourly from the loop (maximize/logrotate.py).
        if not args.dry_run:
            rotator = LogRotator()
            if rotator.active:
                rotator.maybe_rotate()
                engine.housekeeping = rotator.maybe_rotate
        if not args.json:
            if maximize is not None:
                policy = (
                    f"strategy maximize, 5h soft {maximize.soft_5h:g}% / hard "
                    f"{maximize.hard_5h:g}%, 7d soft {maximize.soft_7d:g}% / hard "
                    f"{maximize.hard_7d:g}%"
                )
            else:
                policy = f"threshold {settings.threshold:.0f}%"
            emit_line(
                dimmed(
                    f"Auto-switch running: {policy}, "
                    f"every {settings.interval_seconds:.0f}s"
                    f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
                )
            )
        sys.exit(engine.run_loop())
    except EngineBusyError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(EXIT_ENGINE_BUSY)
    except ClaudeSwitchError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Auto-switch stopped')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)
    finally:
        if lease is not None:
            lease.release()


def _config_command(argv: list[str]) -> None:
    """Handle `cswap config [list|get KEY|set KEY VALUE|unset KEY|path]`.

    Pre-dispatched before the main parser is built, like `run` and `auto`
    (same limitation: `config` must be the first argument). Edits
    settings.json in the backup root with strict validation — unlike loading,
    which forgivingly clamps — so a typo'd key or out-of-range value errors
    loudly here instead of silently degrading at `cswap auto` time.
    """
    from claude_swap.settings import (
        SETTING_SPECS,
        effective_settings,
        format_setting_value,
        set_setting,
        setting_spec,
        settings_path,
        unset_setting,
    )

    key_lines = "\n".join(
        f"  {spec.dotted:<34}{spec.help} (default {format_setting_value(spec.default)})"
        for spec in SETTING_SPECS.values()
    )
    parser = argparse.ArgumentParser(
        prog="cswap config",
        description=(
            "Read and edit claude-swap settings (settings.json in the "
            "backup root)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Keys:
{key_lines}

Examples:
  cswap config                              # list effective settings
  cswap config get autoswitch.threshold
  cswap config set autoswitch.threshold 80
  cswap config unset autoswitch.threshold   # back to the default
  cswap config path                         # where settings.json lives
        """,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (with list or get)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="action", metavar="{list,get,set,unset,path}")

    p_list = sub.add_parser("list", help="Show all effective settings (the default)")
    p_get = sub.add_parser("get", help="Print one setting's effective value")
    p_get.add_argument("key", metavar="KEY", help="Dotted key, e.g. autoswitch.threshold")
    for p in (p_list, p_get):
        # SUPPRESS: without it the subparser's False default would clobber a
        # pre-verb `cswap config --json` in the shared namespace.
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Emit machine-readable JSON to stdout",
        )
    p_set = sub.add_parser("set", help="Validate and persist one setting")
    p_set.add_argument("key", metavar="KEY")
    p_set.add_argument("value", metavar="VALUE")
    p_unset = sub.add_parser("unset", help="Remove one setting (revert to the default)")
    p_unset.add_argument("key", metavar="KEY")
    sub.add_parser("path", help="Print the settings.json location")

    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    action = args.action or "list"
    if json_mode and action not in ("list", "get"):
        parser.error("--json can only be used with list or get")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)
        root = switcher.backup_dir

        if action == "path":
            print(settings_path(root))
        elif action == "list":
            rows = effective_settings(root)
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "path": str(settings_path(root)),
                    "settings": [
                        {"key": spec.dotted, "value": value, "isSet": is_set}
                        for spec, value, is_set in rows
                    ],
                }
                print(json.dumps(payload, indent=2))
            else:
                key_w = max(len(spec.dotted) for spec, _, _ in rows)
                val_w = max(len(format_setting_value(v)) for _, v, _ in rows)
                for spec, value, is_set in rows:
                    line = f"{spec.dotted:<{key_w}}  {format_setting_value(value):<{val_w}}"
                    print(line if is_set else f"{line}  {dimmed('(default)')}")
        elif action == "get":
            spec = setting_spec(args.key)
            value, is_set = next(
                (v, s) for sp, v, s in effective_settings(root) if sp is spec
            )
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "key": spec.dotted,
                    "value": value,
                    "isSet": is_set,
                }
                print(json.dumps(payload, indent=2))
            else:
                print(format_setting_value(value))
        elif action == "set":
            value = set_setting(root, args.key, args.value)
            print(f"{args.key} = {format_setting_value(value)}")
        elif action == "unset":
            if unset_setting(root, args.key):
                default = setting_spec(args.key).default
                print(f"{args.key} unset (default: {format_setting_value(default)})")
            else:
                print(muted(f"{args.key} is not set; nothing to do"), file=sys.stderr)
    except ClaudeSwitchError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if json_mode else sys.stdout,
        )
        sys.exit(130)


# Moved to maximize/tiers.py (the TUI toggles last-resort too).
from claude_swap.maximize import tiers as _mx_tiers  # noqa: E402

_last_resort_entry = _mx_tiers.last_resort_entry
_last_resort_matches = _mx_tiers.last_resort_matches


def _last_resort_command(argv: list[str]) -> None:
    """Handle `cc-swap last-resort add|remove|list [NUM|EMAIL|ALIAS]`.

    Convenience wrapper over the ``maximize.lastResort`` setting (spec §5.1,
    §8): those accounts are switched to only when no normal account can take
    the switch. Writes go through `set_setting`/`unset_setting`, so the file
    keeps its other keys and the 0600 mode.
    """
    from claude_swap.settings import (
        load_maximize_settings,
        load_settings,
        parse_model_names,
        set_setting,
        unset_setting,
    )

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} last-resort",
        description=(
            "Mark accounts the maximize strategy uses only when no other "
            "account can take the switch (edits maximize.lastResort)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cc-swap last-resort add 3
  cc-swap last-resort add team@example.com
  cc-swap last-resort remove dev
  cc-swap last-resort list
        """,
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="action", metavar="{add,remove,list}")
    for name, text in (
        ("add", "Use an account only as a last resort"),
        ("remove", "Return an account to normal ranking"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("account", metavar="NUM|EMAIL|ALIAS")
    sub.add_parser("list", help="Show last-resort accounts (the default)")
    args = parser.parse_args(argv)
    action = args.action or "list"

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        root = switcher.backup_dir
        # parse_model_names is a generic comma-list splitter: trimmed,
        # case-insensitively deduped, first spelling kept.
        entries = list(parse_model_names(load_maximize_settings(root).last_resort))

        if action == "list":
            accounts = (switcher._get_sequence_data() or {}).get("accounts", {})
            if not entries:
                print(dimmed("No last-resort accounts"))
                return
            print(bolded("Last-resort accounts:"))
            for entry in entries:
                nums = _last_resort_matches(accounts, entry)
                where = (
                    ", ".join(f"Account-{n}" for n in nums)
                    if nums else muted("(no matching account)")
                )
                print(f"  {entry} → {where}")
            return

        accounts = (switcher._get_sequence_data() or {}).get("accounts", {})
        dangling = [
            e for e in entries
            if e.lower() == args.account.strip().lower() and not _last_resort_matches(accounts, e)
        ]
        if action == "remove" and dangling:
            # An entry naming no account (left by an older `remove`): drop it
            # by its text, since it resolves to no account to name.
            kept = [e for e in entries if e not in dangling]
            if kept:
                set_setting(root, "maximize.lastResort", ",".join(kept))
            else:
                unset_setting(root, "maximize.lastResort")
            print(f"{accent('Removed')} {dangling[0]} from last-resort (it named no account)")
            return
        num, email, _ = switcher.resolve_account(args.account)

        if action == "add":
            if num in {n for e in entries for n in _last_resort_matches(accounts, e)}:
                print(dimmed(f"Account-{num} ({email}) is already last-resort."))
                return
            entries.append(_last_resort_entry(accounts, num, email))
            set_setting(root, "maximize.lastResort", ",".join(entries))
            print(f"{accent('Marked')} Account-{num} ({email}) last-resort")
            strategy = load_settings(root).strategy
            if strategy != "maximize":
                print(dimmed(
                    f"  Takes effect with the maximize strategy (now {strategy}): "
                    "cc-swap config set autoswitch.strategy maximize"
                ))
            return

        # remove: drop every entry that marks this account, so it is
        # guaranteed normal afterwards.
        dropped = [e for e in entries if num in _last_resort_matches(accounts, e)]
        if not dropped:
            print(dimmed(f"Account-{num} ({email}) is not last-resort."))
            return
        kept = [e for e in entries if e not in dropped]
        if kept:
            set_setting(root, "maximize.lastResort", ",".join(kept))
        else:
            unset_setting(root, "maximize.lastResort")
        print(f"{accent('Removed')} Account-{num} ({email}) from last-resort")
        also = sorted(
            {n for e in dropped for n in _last_resort_matches(accounts, e)} - {num},
            key=int,
        )
        if also:
            warning(
                f"  Also returned {', '.join(f'Account-{n}' for n in also)} "
                "(the removed entry named them too); re-add by alias if needed."
            )
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _prime_command(argv: list[str]) -> None:
    """Handle `cc-swap prime` (maximize/prime_cli.py). Imported lazily, so
    other commands never load the primer."""
    from claude_swap.maximize.prime_cli import prime_command

    prime_command(argv)


def _claude_update_command(argv: list[str]) -> None:
    """Handle `cc-swap claude-update` (maximize/claude_update.py)."""
    from claude_swap.maximize.claude_update import claude_update_command

    claude_update_command(argv)


def _doctor_command(argv: list[str]) -> None:
    """Handle `cc-swap doctor` (maximize/doctor_cli.py), imported lazily."""
    from claude_swap.maximize.doctor_cli import doctor_command

    doctor_command(argv)


def _init_command(argv: list[str]) -> None:
    """Handle `cc-swap init` (maximize/doctor_cli.py), imported lazily."""
    from claude_swap.maximize.doctor_cli import init_command

    init_command(argv)


def _why_command(argv: list[str]) -> None:
    """Handle `cc-swap why` (maximize/doctor_cli.py), imported lazily."""
    from claude_swap.maximize.doctor_cli import why_command

    why_command(argv)


def _history_command(argv: list[str]) -> None:
    """Handle `cc-swap history` (maximize/history_cli.py), imported lazily."""
    from claude_swap.maximize.history_cli import history_command

    history_command(argv)


def _hold_command(argv: list[str]) -> None:
    """Handle `cc-swap hold` (maximize/hold.py), imported lazily."""
    from claude_swap.maximize.hold import hold_command

    hold_command(argv)


def _notify_command(argv: list[str]) -> None:
    """Handle `cc-swap notify` (maximize/notify.py), imported lazily."""
    from claude_swap.maximize.notify import notify_command

    notify_command(argv)


def _use_native_tls() -> None:
    """Route TLS trust decisions through the OS-native verifier.

    Claude's token endpoint (``platform.claude.com``) serves a Let's Encrypt
    chain. Python's stdlib ``ssl`` uses OpenSSL, which on Windows loads the
    system cert store as a flat set and matches CA certs by *subject name*, so a
    stale, expired duplicate of an intermediate (e.g. an old ``ISRG Root X2``
    left in the user's store) can shadow the valid path and fail verification
    with "certificate has expired" even though the served chain is valid — which
    silently breaks inactive-account token refresh. The OS-native verifiers
    (SChannel on Windows, SecureTransport on macOS) build the chain correctly
    and don't trip on the expired duplicate — the same reason Claude Code (Node,
    with its own bundled roots) is unaffected. ``truststore`` delegates to them.

    Best-effort: on any failure fall back to stdlib ``ssl`` rather than block
    the CLI over a TLS-trust nicety.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def _menubar_service(args) -> int:
    """Handle ``menubar --install-service|--uninstall-service|--service-status``.

    Split out of the dispatch chain because these three share one import and
    one output shape, and because the menu bar branch below them is a
    non-returning call — folding the service paths inline would leave the
    reader tracing which branches fall through to launching the app.
    """
    from claude_swap import launch_agent

    if args.install_service:
        # Installing a service for a menu bar that this interpreter cannot draw
        # is the worst version of the bug: it survives reboots and shows
        # nothing. Say so here too, not only when the menu bar is launched.
        from claude_swap.menubar import framework_build_warning

        unsupported = framework_build_warning()
        result = launch_agent.install()
        print(f"Menu bar service installed ({result['label']}).")
        print(f"  plist: {result['plist']}")
        print(f"  logs:  {result['stderr_log']}")
        print(
            dimmed(
                "It starts at login from now on. Re-run this after a cswap "
                "upgrade to point launchd at the new build."
            )
        )
        if unsupported:
            # The hint printed above is about upgrades. A reinstall does not
            # restart the service that is already running, so say that here.
            warning(
                unsupported + "\n  Then run: cswap menubar --install-service",
                file=sys.stderr,
            )
        return 0

    if args.uninstall_service:
        result = launch_agent.uninstall()
        if result["was_loaded"] or result["removed_plist"]:
            print("Menu bar service removed.")
        else:
            print("Menu bar service was not installed.")
        return 0

    result = launch_agent.status()
    if not result["installed"] and not result["loaded"]:
        print("Menu bar service is not installed.")
        print(dimmed("Install it with: cswap menubar --install-service"))
        return 0
    state = result["state"] or ("loaded" if result["loaded"] else "stopped")
    pid = f" (pid {result['pid']})" if result["pid"] else ""
    print(f"Menu bar service: {state}{pid}")
    print(f"  plist: {result['plist']}")
    if not result["installed"]:
        print(dimmed("launchd still has it loaded, but the plist is gone."))
    return 0


def _print_service_install(result: dict) -> None:
    print(f"cc-swap service installed ({result['name']}).")
    print(f"  runs:   {' '.join(result['program'])}")
    print(f"  file:   {result['path']}")
    print(f"  logs:   {', '.join(result['logs'])}")
    forwarded = result.get("forwarded_env") or {}
    if forwarded:
        shown = ", ".join(f"{name}={value or '(empty)'}" for name, value in forwarded.items())
        origin = (
            "kept from the installed service"
            if result.get("env_source") == "installed"
            else "forwarded from this shell"
        )
        print(f"  env:    {shown} ({origin})")
    if result["claude_path"]:
        saved = " (saved as prime.claudePath)" if result["claude_path_saved"] else ""
        print(f"  claude: {result['claude_path']}{saved}")
    else:
        warning(
            "claude was not found on PATH or at ~/.local/bin/claude; 5h priming "
            "cannot run until you set it: cc-swap config set prime.claudePath "
            "/path/to/claude (then re-run cc-swap service install)",
            file=sys.stderr,
        )
    print(
        dimmed(
            "It starts at login and restarts after a crash. Re-run "
            "`cc-swap service install` after upgrading cc-swap."
        )
    )
    if result["platform"] == "linux" and result["linger"] is not True:
        print(
            dimmed(
                "To keep it running after you log out, run once: "
                "loginctl enable-linger $USER"
            )
        )


def _print_service_status(result: dict) -> None:
    if not result["installed"] and not result["loaded"]:
        print("cc-swap service is not installed.")
        print(dimmed("Install it with: cc-swap service install"))
        return
    from claude_swap.maximize.service import state_text

    pid = f" (pid {result['pid']})" if result["pid"] else ""
    print(f"cc-swap service: {state_text(result)}{pid}")
    print(f"  file: {result['path']}")
    print(f"  logs: {', '.join(result['logs'])}")
    if not result["installed"]:
        print(dimmed("The service manager still has it loaded, but its file is gone."))


def _service_command(argv: list[str]) -> None:
    """Handle `cc-swap service install|uninstall|status`.

    Dispatched through `_FORK_COMMANDS` like `last-resort` and `prime`.
    Runs `cc-swap auto` as a per-user service — a launchd LaunchAgent on
    macOS, a systemd user unit on Linux — so auto-switching survives
    closed terminals and reboots. Windows is refused (by the service
    module, so the API refuses too).
    """
    parser = argparse.ArgumentParser(
        prog="cc-swap service",
        description="Run the auto-switch engine (`cc-swap auto`) as a background service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
macOS: LaunchAgent com.wonjun-lab.cc-swap; logs in ~/Library/Logs/cc-swap/
Linux: systemd user unit ~/.config/systemd/user/cc-swap.service
       (logs: journalctl --user -u cc-swap; keep it running after logout
        with: loginctl enable-linger $USER)

One engine runs per machine: while the service holds the engine lease,
`cc-swap auto` refuses to start (exit 4) and the TUI auto screen and the
menu bar only display. Re-run `cc-swap service install` after upgrading.
        """,
    )
    sub = parser.add_subparsers(dest="action", metavar="{install,uninstall,status}")
    p_install = sub.add_parser("install", help="Install (or refresh) and start the service")
    p_install.add_argument(
        "--claude-path",
        metavar="PATH",
        default=None,
        help=(
            "claude executable for 5h priming (default: prime.claudePath, "
            "else found on PATH or at ~/.local/bin/claude); saved as prime.claudePath"
        ),
    )
    p_install.add_argument(
        "--reuse-installed-env",
        action="store_true",
        help=(
            "keep CLAUDE_CONFIG_DIR / CLAUDE_SECURESTORAGE_CONFIG_DIR from the "
            "installed service file instead of this shell (used by the refresh "
            "after `cc-swap upgrade`)"
        ),
    )
    sub.add_parser("uninstall", help="Stop the service and remove it")
    sub.add_parser("status", help="Report whether the service is installed and running")
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        sys.exit(2)
    if sys.platform != "win32" and os.geteuid() == 0:
        error("Error: install the service as your own user, not root")
        sys.exit(1)

    from claude_swap.maximize import service

    try:
        if args.action == "install":
            kwargs = {"claude_path": args.claude_path}
            if args.reuse_installed_env:
                kwargs["reuse_installed_env"] = True
            _print_service_install(service.install(**kwargs))
        elif args.action == "uninstall":
            result = service.uninstall()
            if result["was_running"] or result["removed"]:
                print("cc-swap service removed.")
            else:
                print("cc-swap service was not installed.")
        else:
            _print_service_status(service.status())
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    sys.exit(0)


def main() -> None:
    """Main entry point for the CLI."""
    force_utf8_output()
    _use_native_tls()
    argv = sys.argv[1:]
    try:
        from claude_swap.appearance import cli_should_probe, cli_theme
        # `run` execs a child that takes over the terminal, and `--json`
        # must stay machine-readable — never probe (and emit the OSC query)
        # in either case.
        probe = cli_should_probe(argv, colors_enabled=printer.colors_enabled())
        name = cli_theme(load_ui_settings(paths.get_backup_root()).theme, colors=probe)
        printer.set_theme(name)
    except Exception:
        pass  # theme is cosmetic; never block the CLI on it
    try:  # cc-swap: record every switch this process makes (maximize/ledger.py)
        from claude_swap.maximize import ledger

        ledger.install(source=ledger.process_source(argv))
    except Exception:
        pass

    # `run` and `auto` keep their dedicated pre-dispatch parsers.
    if argv and argv[0] == "run":
        _run_command(argv[1:])
        return  # only reachable in tests where exec/exit is mocked
    if argv and argv[0] == "auto":
        _auto_command(argv[1:])
        return  # only reachable in tests where sys.exit is mocked
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        _config_command(sys.argv[2:])
        return
    if argv and argv[0] == "map":
        _map_command(argv[1:])
        return
    if argv and argv[0] == "unmap":
        _unmap_command(argv[1:])
        return
    if argv and argv[0] == "unclaimed":
        _unclaimed_command(argv[1:])
        return
    if argv and argv[0] == "alias":
        _alias_command(argv[1:])
        return
    if argv and argv[0] == "swap":
        _swap_command(argv[1:])
        return
    if argv and argv[0] == "move":
        _move_command(argv[1:])
        return
    # cc-swap: the one hook for every fork subcommand (see _FORK_COMMANDS).
    fork_command = _FORK_COMMANDS.get(argv[0]) if argv else None
    if fork_command is not None:
        globals()[fork_command](argv[1:])
        return

    # Bare `cswap` in an interactive terminal opens the TUI dashboard (like
    # lazygit/k9s). TTY-gated on both ends so scripts and pipes keep getting
    # the usage error, and `cswap tui` stays the explicit spelling.
    if not argv and sys.stdout.isatty() and sys.stdin.isatty():
        argv = ["--tui"]

    # Memorable subcommands (`cswap switch <email>`, `cswap list`, `cswap help`, ...)
    # are rewritten to the equivalent flags so the original `--flag` interface
    # keeps working unchanged.
    argv = _translate_subcommand(argv)

    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        usage="%(prog)s <command> [args] [options]",
        description="""Multi-Account Switcher for Claude Code

Commands:
  %(prog)s help                       show this help
  %(prog)s list                       list managed accounts
  %(prog)s status                     show current account
  %(prog)s switch                     rotate to the next account
  %(prog)s switch <num|email>         switch to a specific account
  %(prog)s add                        add the current account
  %(prog)s add-token [TOKEN|-]        register a setup-token or API key
  %(prog)s remove <num|email>         remove an account
  %(prog)s disable <num|email>        hold an account out of auto-rotation
  %(prog)s enable <num|email>         return a disabled account to rotation
  %(prog)s run <num|email> [-- ...]   run as an account, this terminal only
  %(prog)s run                        run the current dir's mapped account
  %(prog)s map <num|email> [path]     map a directory to an account
  %(prog)s map                        list directory mappings
  %(prog)s unmap [path]               remove a directory mapping
  %(prog)s alias <num|email> <name>   set a short alias for an account
  %(prog)s alias <num|email> --unset  remove an account's alias
  %(prog)s alias                      list all aliases
  %(prog)s swap <a> <b>               exchange two accounts' slot numbers
  %(prog)s move <a> <slot>            assign an account to a slot (swaps if taken)
  %(prog)s auto                       auto-switch when nearing rate limits
  %(prog)s config [set KEY VALUE]     show or change settings (settings.json)
  %(prog)s unclaimed [--purge ID]     list or drop stashed credential entries
  %(prog)s export <path>              export accounts
  %(prog)s import <path>              import accounts
  %(prog)s import-usage <path>        adopt usage another machine read (list --json)
  %(prog)s tui                        interactive dashboard (also: bare %(prog)s)
  %(prog)s watch                      dashboard, opened on the live watch page
  %(prog)s menubar                    macOS menu bar app
  %(prog)s menubar --install-service  keep the menu bar running via launchd
  %(prog)s upgrade                    self-upgrade to latest
  %(prog)s upgrade --check            show what a newer release changes (exit 10 if there is one)
  %(prog)s purge                      remove all claude-swap data

cc-swap:
  %(prog)s auto --strategy maximize   per-window soft/hard auto-switching
  %(prog)s last-resort add|remove <a> use an account only as a last resort
  %(prog)s last-resort list           list last-resort accounts
  %(prog)s prime [N ...] [--dry-run]  open idle accounts' 5h windows now
  %(prog)s prime verify [--live]      re-check priming isolation after a claude update
  %(prog)s service install            run auto-switch as a background service
  %(prog)s doctor [--json]            check logins, Keychain, service; say what to fix
  %(prog)s init [--apply]             onboarding/migration checklist (ok/FIX/TODO)
  %(prog)s why                        why the engine did or didn't switch
  %(prog)s auto off|on|status         stop / resume automatic switching (persistent)
  %(prog)s hold [2h|until 23:00|off]  stay on the active account (soft moves wait)
  %(prog)s notify test|status         desktop notifications from the engine
  %(prog)s history [-n N] [--json]    recent account switches (who, why)
  %(prog)s claude-update [--check]    update Claude Code via `claude update` (exit 10 = available)

Aliases: ls=list  rm=remove  update=upgrade""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Flags combine with subcommands:
  %(prog)s switch --strategy best           # pick the account with most quota left
  %(prog)s switch --strategy next-available # rotate, skipping rate-limited accounts
  %(prog)s switch user@example.com
  %(prog)s list --token-status
  %(prog)s list --json
  %(prog)s import-usage usage.json --hold 600  # adopt another machine's list --json
  %(prog)s add --slot 3                      # add to a specific slot
  %(prog)s add-token sk-ant-oat01-... --email me@example.com
  %(prog)s run 2 -- --resume                 # forward args after '--' to claude
  %(prog)s auto --once                       # single auto-switch tick (cron-friendly)
  %(prog)s config set autoswitch.threshold 80

The original flag spellings (%(prog)s --switch, %(prog)s --list, ...) keep working.
        """,
    )

    # Version and debug flags (outside mutually exclusive group)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--token-status",
        action="store_true",
        help="Show source-labelled OAuth token diagnostics (use with 'list')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON to stdout (use with 'list', 'status', "
            "or 'switch'). See README 'JSON output for scripting'."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        metavar="{best,next-available}",
        help=(
            "With bare 'switch': pick the target by remaining 5h/7d quota. "
            "'best' jumps to the account with the most headroom; "
            "'next-available' rotates to the next account, skipping any at their limit"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "With 'switch --strategy': also count these models' per-model "
            "weekly limits when comparing accounts (comma-separated display "
            "names, or 'all'). Defaults to the autoswitch.model setting"
        ),
    )
    parser.add_argument(
        "--slot",
        type=int,
        metavar="NUM",
        help="Specify slot number when adding account (use with 'add' or 'add-token')",
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        help=(
            "Email address for the account. Optional with 'add-token'; "
            "defaults to setup-token-{slot}@token.local (or "
            "api-key-{slot}@token.local for API keys) since these tokens "
            "carry no real email metadata."
        ),
    )
    parser.add_argument(
        "--account",
        metavar="NUM|EMAIL",
        help="Limit export to one account (use with 'export')",
    )
    parser.add_argument(
        "--alias",
        metavar="NAME",
        help="Set a short display alias for the account (use with 'add')",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing accounts during import; with 'switch <num|email>', "
            "activate the stored credentials without backing up the current "
            "login first; with 'upgrade', reinstall even when already on the "
            "latest release"
        ),
    )
    parser.add_argument(
        "--allow-dead-login",
        action="store_true",
        help=(
            "With 'switch <num|email>': switch even to an account whose stored "
            "login is dead (expired or quarantined), which is refused otherwise; "
            "the current login is still backed up first"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include full ~/.claude.json in export (default: oauthAccount only)",
    )
    parser.add_argument(
        "--hold",
        type=float,
        metavar="SECONDS",
        help=(
            "With 'import-usage': keep this machine from fetching the "
            "imported accounts for this many seconds (0 lifts an earlier hold)"
        ),
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help=(
            "With 'menubar': install a launchd LaunchAgent so the menu bar "
            "starts at login and restarts on crash (macOS)"
        ),
    )
    parser.add_argument(
        "--uninstall-service",
        action="store_true",
        help="With 'menubar': stop the LaunchAgent and remove its plist (macOS)",
    )
    parser.add_argument(
        "--service-status",
        action="store_true",
        help=(
            "With 'menubar': report whether the LaunchAgent is installed "
            "and running"
        ),
    )

    # Legacy `--flag` interface. Still fully supported (bare subcommands rewrite
    # into these, see _translate_subcommand), but hidden from --help so the
    # subcommands shown in the description are the one documented interface.
    # The group is not `required` because the "no command" case is handled
    # explicitly below (a required group with every member suppressed prints a
    # broken empty-list error).
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--add-account",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--remove-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--disable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--enable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--list",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch-to",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--status",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--purge",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--export",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--import",
        dest="import_",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--import-usage",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--tui",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--watch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--menubar",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--upgrade",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "With 'upgrade': only report the installed and latest release and "
            "what changed between them; exit 0 when up to date, 10 when an "
            "update is available"
        ),
    )
    group.add_argument(
        "--add-token",
        metavar="TOKEN|-",
        nargs="?",
        const="",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args(argv)

    # No action selected: emit a clean, subcommand-oriented message rather than
    # the raw argparse "one of the arguments ... is required" (which would list
    # the now-hidden legacy flags). Value actions can be falsy-but-set
    # (--add-token uses const=""), so test those with `is not None`.
    if not (
        args.add_account
        or args.list
        or args.switch
        or args.status
        or args.purge
        or args.tui
        or args.watch
        or args.menubar
        or args.upgrade
        or args.remove_account is not None
        or args.disable_account is not None
        or args.enable_account is not None
        or args.switch_to is not None
        or args.export is not None
        or args.import_ is not None
        or args.import_usage is not None
        or args.add_token is not None
    ):
        parser.error("no command given — try '%(prog)s help'" % {"prog": _prog_name()})

    if args.token_status and not args.list:
        parser.error("--token-status can only be used with 'list'")

    if args.json and not (args.list or args.status or args.switch or args.switch_to):
        parser.error("--json can only be used with 'list', 'status', or 'switch'")

    if args.json and args.token_status:
        # Token status is not part of the JSON v1 schema; reject rather than
        # silently ignore it (a future additive field can add it).
        parser.error("--token-status cannot be combined with --json")

    if args.strategy is not None and not args.switch:
        parser.error("--strategy can only be used with bare 'switch'")

    if args.model is not None and args.strategy is None:
        # Meaningless on a direct-target switch or plain rotation — nothing
        # usage-aware reads it there, so reject loudly rather than ignore.
        parser.error(
            "--model can only be used with 'switch --strategy best' or "
            "'switch --strategy next-available'"
        )

    if args.slot is not None and not (args.add_account or args.add_token is not None):
        parser.error("--slot can only be used with 'add' or 'add-token'")

    if args.email is not None and args.add_token is None:
        parser.error("--email can only be used with 'add-token'")

    if args.account is not None and not args.export:
        parser.error("--account can only be used with 'export'")

    if args.alias is not None and not args.add_account:
        parser.error("--alias can only be used with 'add'")

    if args.force and not (args.import_ or args.switch_to or args.upgrade):
        parser.error(
            "--force can only be used with 'import', 'switch <num|email>' "
            "or 'upgrade'"
        )

    if args.allow_dead_login and not args.switch_to:
        parser.error("--allow-dead-login can only be used with 'switch <num|email>'")

    if args.check and not args.upgrade:
        parser.error("--check can only be used with 'upgrade'")

    if args.check and args.force:
        parser.error("--check only reports; it cannot be combined with --force")

    if args.full and not args.export:
        parser.error("--full can only be used with 'export'")

    if args.hold is not None and args.import_usage is None:
        parser.error("--hold can only be used with 'import-usage'")

    if args.hold is not None and not (math.isfinite(args.hold) and args.hold >= 0):
        parser.error("--hold must be a non-negative number of seconds")

    if (
        args.install_service or args.uninstall_service or args.service_status
    ) and not args.menubar:
        parser.error(
            "--install-service, --uninstall-service and --service-status "
            "can only be used with 'menubar'"
        )

    # Self-upgrade runs before switcher init so we don't touch config/keychain
    # just to upgrade the tool itself.
    if args.upgrade:
        from claude_swap.update_check import run_self_upgrade, run_upgrade_check

        try:
            if args.check:
                sys.exit(run_upgrade_check())
            sys.exit(run_self_upgrade(force=args.force))
        except KeyboardInterrupt:
            print(f"\n{dimmed('Upgrade cancelled')}")
            sys.exit(130)

    # Initialize switcher and dispatch under a single error handler so
    # init-time failures (e.g. MigrationError on a backup-dir collision)
    # are presented like every other ClaudeSwitchError: clean stderr line,
    # exit 1, no traceback.
    # JSON-capable commands return a payload; the CLI is the single point that
    # serializes it (so no command writes JSON to stdout itself).
    payload: dict | None = None
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        # Check for root (unless in container) - POSIX only
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        if args.add_account:
            switcher.add_account(slot=args.slot, alias=args.alias)
        elif args.add_token is not None:
            switcher.add_account_from_token(
                token=args.add_token,
                email=args.email,
                slot=args.slot,
            )
        elif args.remove_account:
            switcher.remove_account(args.remove_account)
        elif args.disable_account is not None:
            switcher.set_account_disabled(args.disable_account, True)
        elif args.enable_account is not None:
            switcher.set_account_disabled(args.enable_account, False)
        elif args.list:
            payload = switcher.list_accounts(
                show_token_status=args.token_status,
                json_output=args.json,
            )
        elif args.switch:
            from claude_swap.settings import load_settings, parse_model_names

            # Only the usage-aware strategies read model limits: --model wins;
            # otherwise the persistent autoswitch.model setting applies
            # (announced by switch(), never silently).
            if args.strategy is None:
                models, model_source = (), None
            elif args.model is not None:
                models, model_source = parse_model_names(args.model), "cli"
            else:
                models = parse_model_names(load_settings(switcher.backup_dir).model)
                model_source = "autoswitch.model" if models else None
            payload = switcher.switch(
                strategy=args.strategy,
                json_output=args.json,
                models=models,
                model_source=model_source,
            )
            if payload is not None and models:
                payload["models"] = list(models)
                payload["modelSource"] = model_source
        elif args.switch_to:
            payload = switcher.switch_to(
                args.switch_to,
                json_output=args.json,
                force=args.force,
                allow_dead_login=args.allow_dead_login,
            )
        elif args.status:
            payload = switcher.status(json_output=args.json)
        elif args.purge:
            switcher.purge()
        elif args.export:
            from claude_swap.transfer import export_accounts

            export_accounts(switcher, args.export, account=args.account, full=args.full)
        elif args.import_:
            from claude_swap.transfer import import_accounts

            import_accounts(switcher, args.import_, force=args.force)
        elif args.import_usage:
            from claude_swap.transfer import import_usage

            import_usage(switcher, args.import_usage, hold_s=args.hold)
        elif args.tui:
            from claude_swap.tui import run as tui_run

            sys.exit(tui_run(switcher))
        elif args.watch:
            from claude_swap.tui import run as tui_run

            sys.exit(tui_run(switcher, start="watch"))
        elif args.menubar:
            if sys.platform != "darwin":
                error("The menu bar is only available on macOS.")
                sys.exit(1)
            if args.install_service or args.uninstall_service or args.service_status:
                sys.exit(_menubar_service(args))
            # menubar is import-safe without the extra; a missing rumps
            # surfaces from run() as a ClaudeSwitchError with the install hint.
            from claude_swap.menubar import run as menubar_run

            sys.exit(menubar_run(switcher))
    except ClaudeSwitchError as e:
        # In JSON mode keep stdout pure JSON: emit the structured error envelope
        # there (exit 1) instead of a red stderr line.
        if args.json:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # Route the cancellation note to stderr in JSON mode so stdout stays
        # parseable (the guarantee covers completion / handled errors, not Ctrl-C).
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)

    if args.json and payload is not None:
        print(json.dumps(payload, indent=2))
        if (args.switch or args.switch_to) and payload.get("reason") in SWITCH_REFUSED_REASONS:
            # The human path raises SwitchRefusedError (exit 1); keep JSON in step.
            sys.exit(1)

    # Passive update notification (never fails). Skipped after --purge so we
    # don't immediately recreate <backup_root>/cache/update_check.json inside
    # the directory we just deleted. Skipped after --upgrade as a safety guard
    # in case the dispatch is later refactored to fall through.
    if not args.purge and not args.upgrade and not args.json:
        from claude_swap.update_check import check_for_update

        msg = check_for_update(__version__)
        if msg:
            print(f"\n{muted(msg)}", file=sys.stderr)


if __name__ == "__main__":
    main()
