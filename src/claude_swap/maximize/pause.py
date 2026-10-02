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

import math
from collections.abc import Mapping
from pathlib import Path

from claude_swap import autoswitch as aw

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
    backup_root: Path, reason: str, *, now: float, seconds: float = MAX_PAUSE_S
) -> float:
    """Pause switching and priming until ``now + seconds`` (capped at
    :data:`MAX_PAUSE_S`); returns that time. Blocking (state-file lock)."""
    until = now + min(max(seconds, 0.0), MAX_PAUSE_S)

    def mutate(state: dict) -> None:
        state[PAUSED_UNTIL_KEY] = until
        state[PAUSED_REASON_KEY] = reason

    _StateFile(backup_root)._mutate_state(mutate)
    return until


def resume(backup_root: Path) -> None:
    """Lift a pause now. A no-op (no write) when none is recorded."""
    file = _StateFile(backup_root)
    if not file.state_path.exists():
        return
    current = file._read_state()
    if PAUSED_UNTIL_KEY not in current and PAUSED_REASON_KEY not in current:
        return

    def mutate(state: dict) -> None:
        state.pop(PAUSED_UNTIL_KEY, None)
        state.pop(PAUSED_REASON_KEY, None)

    file._mutate_state(mutate)
