"""Pausing the maximize engine while the user re-logs an account in.

A re-login replaces the live Claude Code login with another account's for a
few minutes (``claude`` → ``/login``), then cc-swap stores it and switches
back. An engine running meanwhile — the service, or another terminal — would
see that login as a manual switch: it could switch away from it, restart its
rebalance cooldown, or prime the account being repaired. So the TUI writes a
pause marker into the engine's state file and the maximize hook honours it.

``pausedUntil`` (epoch) and ``pausedReason`` sit at the top level of
``autoswitch_state.json``. The marker expires on its own: a pause is never
longer than :data:`MAX_PAUSE_S` from now, so a crashed TUI (or a bogus
far-future value) cannot stop switching for good.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from claude_swap import autoswitch as aw
from claude_swap.maximize.auto_off_flag import flag_path, read_flag
from claude_swap.settings import atomic_write_json

PAUSED_UNTIL_KEY = "pausedUntil"
PAUSED_REASON_KEY = "pausedReason"
MAX_PAUSE_S = 600.0
# Slack for clocks a little apart between the writer and the engine.
_SKEW_S = 5.0


def active_pause(state: Mapping, now: float) -> tuple[float, str] | None:
    """``(until, reason)`` while a pause marker holds at ``now``, else None.

    A marker more than :data:`MAX_PAUSE_S` in the future is ignored."""
    until = state.get(PAUSED_UNTIL_KEY)
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return None
    until = float(until)
    if not math.isfinite(until) or until <= now or until - now > MAX_PAUSE_S + _SKEW_S:
        return None
    reason = state.get(PAUSED_REASON_KEY)
    return until, reason if isinstance(reason, str) and reason else "paused"


class _StateFile:
    """The engine's own state-file helpers (same lock, same atomic write,
    same schema stamp), without an engine."""

    _state_lock = aw.AutoSwitchEngine._state_lock
    _read_state = aw.AutoSwitchEngine._read_state
    _mutate_state = aw.AutoSwitchEngine._mutate_state

    def __init__(self, backup_root: Path) -> None:
        self.state_path = Path(backup_root) / aw.STATE_FILENAME


def pause(
    backup_root: Path,
    reason: str,
    *,
    now: float,
    seconds: float = MAX_PAUSE_S,
    wanted: Callable[[], bool] | None = None,
) -> float | None:
    """Pause switching and priming until ``now + seconds`` (capped at
    :data:`MAX_PAUSE_S`); returns that time. Blocking (state-file lock).

    A renewal passes ``wanted``: it is asked under the state lock, and a
    False answer (the pause was lifted meanwhile) writes nothing and
    returns None — so a renewal racing a resume never re-pauses."""
    until = now + min(max(seconds, 0.0), MAX_PAUSE_S)
    file = _StateFile(backup_root)
    with file._state_lock():
        if wanted is not None and not wanted():
            return None
        state = file._read_state()
        state["schemaVersion"] = aw.STATE_SCHEMA_VERSION
        state[PAUSED_UNTIL_KEY] = until
        state[PAUSED_REASON_KEY] = reason
        atomic_write_json(file.state_path, state)
    return until


def resume(backup_root: Path) -> None:
    """Lift a pause now. A no-op (no write) when none is recorded; the
    check and the clear happen under the same state lock."""
    file = _StateFile(backup_root)
    if not file.state_path.exists():
        return
    with file._state_lock():
        state = file._read_state()
        if PAUSED_UNTIL_KEY not in state and PAUSED_REASON_KEY not in state:
            return
        state.pop(PAUSED_UNTIL_KEY, None)
        state.pop(PAUSED_REASON_KEY, None)
        state["schemaVersion"] = aw.STATE_SCHEMA_VERSION
        atomic_write_json(file.state_path, state)


# -- `cc-swap auto off`: automatic switching off until turned back on -------------
#
# Unlike a re-login pause, ``autoOff`` has no expiry: it is the user's
# standing choice, kept in the state file so the running service (any
# strategy) honours it on its next tick without a restart, and so it
# survives restarts. The engine keeps polling and publishing its decision;
# it only never switches and never primes. ``cc-swap switch`` and the TUI's
# manual switch still work.
#
# Semantics of a damaged marker: the key present with anything but
# ``false``/``null`` means OFF (it was set on purpose; failing open would
# switch against the user's wish). The flag is mirrored in its own file
# (``auto_off.json``, maximize/auto_off_flag.py) because a state file that
# cannot be parsed reads as ``{}`` and is rewritten without the key: the flag
# file is authoritative, and a damaged or unreadable one reads as OFF too. The
# state key stays for compatibility (older builds read only that).

AUTO_OFF_KEY = "autoOff"
#: An engine repeats its ``auto-off`` no-switch event at most this often.
AUTO_OFF_EVENT_EVERY_S = 3600.0
_AUTO_OFF_ATTR = "_auto_off_event_at"


@dataclass(frozen=True)
class AutoOff:
    since: float | None
    by: str | None
    host: str | None


def auto_off(state: Mapping) -> AutoOff | None:
    """The ``autoOff`` marker, or None while automatic switching is on."""
    if AUTO_OFF_KEY not in state:
        return None
    raw = state.get(AUTO_OFF_KEY)
    if raw is None or raw is False:
        return None
    if not isinstance(raw, Mapping):
        return AutoOff(None, None, None)
    since = raw.get("since")
    by = raw.get("by")
    host = raw.get("host")
    return AutoOff(
        float(since)
        if isinstance(since, (int, float)) and not isinstance(since, bool) and math.isfinite(since)
        else None,
        by if isinstance(by, str) and by else None,
        host if isinstance(host, str) and host else None,
    )


def effective_auto_off(backup_root: Path, state: Mapping) -> AutoOff | None:
    """The marker that holds: the flag file's (authoritative; a damaged one
    counts as off), else the state key's."""
    flag = read_flag(backup_root)
    if flag is not None:
        return auto_off({AUTO_OFF_KEY: flag}) if flag else AutoOff(None, None, None)
    return auto_off(state)


def read_auto_off(backup_root: Path) -> AutoOff | None:
    return effective_auto_off(backup_root, _StateFile(backup_root)._read_state())


def _state_is_damaged(path: Path) -> bool:
    """The state file exists but cannot be parsed into an object."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    return not isinstance(raw, dict)


def set_auto_off(
    backup_root: Path, off: bool, *, by: str, now: float, host: str | None = None
) -> bool:
    """Turn automatic switching off (``off=True``) or back on. Returns
    whether anything changed; turning it off again keeps the first ``since``."""
    file = _StateFile(backup_root)
    flag_file = flag_path(backup_root)
    if not off and not file.state_path.exists() and not os.path.lexists(flag_file):
        return False
    with file._state_lock():
        state = file._read_state()
        damaged = _state_is_damaged(file.state_path)
        flag = read_flag(backup_root)
        in_state = isinstance(state.get(AUTO_OFF_KEY), Mapping)
        if off:
            if flag:
                marker = dict(flag)
            elif in_state:
                marker = dict(state[AUTO_OFF_KEY])
            else:
                marker = {"since": now, "by": by, "host": host}
            # Already off (flag file or state key): keep the first `since`,
            # but make sure both copies exist.
            changed = flag is None and not in_state
            if not flag:
                atomic_write_json(flag_file, {"schemaVersion": 1, AUTO_OFF_KEY: marker})
            if not in_state and not damaged:
                state[AUTO_OFF_KEY] = marker
                state["schemaVersion"] = aw.STATE_SCHEMA_VERSION
                atomic_write_json(file.state_path, state)
            return changed
        changed = flag is not None or AUTO_OFF_KEY in state
        if flag is not None:
            try:
                flag_file.unlink()
            except FileNotFoundError:
                pass
        if AUTO_OFF_KEY in state and not damaged:
            state.pop(AUTO_OFF_KEY, None)
            state["schemaVersion"] = aw.STATE_SCHEMA_VERSION
            atomic_write_json(file.state_path, state)
        return changed


def auto_off_detail(off: AutoOff) -> str:
    who = f" by {off.by}" if off.by else ""
    if off.since is not None:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(off.since))
        return f"automatic switching is off (since {when}{who}); turn it on: cc-swap auto on"
    return f"automatic switching is off{who}; turn it on: cc-swap auto on"


def auto_off_hold(engine, state: Mapping) -> "aw.TickOutcome | None":
    """The engine's guard: ``NO_ACTION`` while auto switching is off, after
    one ``auto-off`` no-switch event per :data:`AUTO_OFF_EVENT_EVERY_S`;
    None (carry on) while it is on."""
    off = effective_auto_off(engine.state_path.parent, state)
    if off is None:
        if getattr(engine, _AUTO_OFF_ATTR, None) is not None:
            setattr(engine, _AUTO_OFF_ATTR, None)
        return None
    now = engine.clock()
    last = getattr(engine, _AUTO_OFF_ATTR, None)
    if last is None or not 0 <= now - last < AUTO_OFF_EVENT_EVERY_S:
        setattr(engine, _AUTO_OFF_ATTR, now)
        engine._emit(aw.NoSwitchEvent(reason="auto-off", detail=auto_off_detail(off)))
    return aw.TickOutcome.NO_ACTION


def auto_command(argv: list[str]) -> None:
    """``cc-swap auto on|off|status [--json]``."""
    import argparse
    import json
    import socket
    import sys

    from claude_swap.paths import get_backup_root

    parser = argparse.ArgumentParser(
        prog="cc-swap auto",
        description=(
            "Turn automatic switching off or back on. Off is persistent: the "
            "running engine (the service, a TUI or menu bar engine) keeps "
            "polling and showing its decision, but never switches and never "
            "primes until you turn it on again. Manual switches still work."
        ),
    )
    parser.add_argument("action", choices=("on", "off", "status"))
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)
    root = get_backup_root()
    now = time.time()
    host = socket.gethostname().split(".")[0] or None
    if args.action in ("on", "off"):
        changed = set_auto_off(root, args.action == "off", by="cli", now=now, host=host)
    else:
        changed = False
    off = read_auto_off(root)
    from claude_swap.maximize import hold as account_hold

    try:
        pinned = account_hold.holding(
            account_hold.read_hold(root, now=now), account_hold.active_slot(root), now
        )
        hold_line = account_hold.status_line(root, now) if pinned is not None else None
    except Exception:
        pinned, hold_line = None, None
    if args.json:
        print(json.dumps({
            "schemaVersion": 1,
            "autoSwitch": "off" if off else "on",
            "changed": changed,
            "since": off.since if off else None,
            "by": off.by if off else None,
            "hold": account_hold.status_payload(pinned, now),
        }))
        sys.exit(0)
    if off is None:
        print("Automatic switching is ON." + ("" if changed or args.action == "status" else " (already)"))
    else:
        print(
            "Automatic switching is OFF: the engine keeps polling but never "
            "switches or primes." + ("" if changed or args.action == "status" else " (already)")
        )
        print(auto_off_detail(off))
    if hold_line:
        print(f"{hold_line} (cc-swap hold off lifts it).")
    sys.exit(0)
