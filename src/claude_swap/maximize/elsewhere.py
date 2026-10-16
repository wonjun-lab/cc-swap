"""Accounts in use on another machine (cc-swap fork, maximize).

Several machines can be logged in to the same accounts, each running its own
engine with no coordination. Landing on an account another machine is using
doubles its burn, and both machines' reads spend the account's one usage
budget (429s). This module spots such accounts from the readings the engine
already has: no new network call, no message between machines.

An account is *in use elsewhere* from the later of two successful readings
when its 5h window rose by at least :data:`MIN_RISE` points between them,
the readings at most :data:`MAX_GAP_S` apart, and nothing on this machine
can explain the rise:

* a non-active account: nothing here uses it, unless it was the active one
  at some point since the earlier reading;
* the active account: only when Claude Code on this machine was idle the
  whole interval (the engine's local-activity detection,
  ``active_watch.local_idle``'s evidence).

The mark lasts :data:`MARK_TTL_S` after the last such rise, and ends at the
5h reset of the window it was seen in. It is kept in the engine state file
(``maximizeElsewhere``), so a restart does not forget it, and Fleet and
``cc-swap doctor`` read the same record.

Pure: everything is passed in (the engine's tick and the readers do the I/O).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

#: State-file key (``autoswitch_state.json``): ``{"slots": {slot: record},
#: "localAt": epoch s}`` — ``localAt`` is the last moment Claude Code was seen
#: busy on this machine.
STATE_KEY = "maximizeElsewhere"
#: 5h points a rise must reach to count.
MIN_RISE = 2.0
#: Two readings further apart than this say nothing about who used the account.
MAX_GAP_S = 30 * 60.0
#: A mark ends this long after the last qualifying rise.
MARK_TTL_S = 30 * 60.0


@dataclass(frozen=True)
class Reading:
    """One successful 5h reading of an account."""

    ts: float
    pct5: float
    reset5: float | None


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def marks(state: Mapping | None, now: float) -> dict[str, float]:
    """``{slot: since}`` for every account marked in use elsewhere at
    ``now``. ``state`` is the state file (or any mapping holding
    :data:`STATE_KEY`); anything unreadable is no mark."""
    block = state.get(STATE_KEY) if isinstance(state, Mapping) else None
    slots = block.get("slots") if isinstance(block, Mapping) else None
    if not isinstance(slots, Mapping):
        return {}
    out: dict[str, float] = {}
    for slot, rec in slots.items():
        if isinstance(rec, Mapping) and active_mark(rec, now):
            since = _num(rec.get("since"))
            if since is not None:
                out[str(slot)] = since
    return out


def active_mark(rec: Mapping, now: float) -> bool:
    """Whether ``rec`` (one slot's record) still marks the account."""
    since, last = _num(rec.get("since")), _num(rec.get("last"))
    if since is None or last is None or now - last >= MARK_TTL_S:
        return False
    until5 = _num(rec.get("until5"))
    return until5 is None or now < until5


def _window_rolled(prev: Mapping, cur: Reading) -> bool:
    """The 5h window reset between the readings (or the reading dropped)."""
    reset5 = _num(prev.get("win"))
    if reset5 is not None and cur.ts >= reset5:
        return True
    pct5 = _num(prev.get("pct5"))
    return pct5 is not None and cur.pct5 < pct5


def update(
    state: Mapping | None,
    readings: Mapping[str, Reading],
    *,
    active: str | None,
    local_busy_at: float | None,
    local_known: bool,
    now: float,
    roster: Collection[str] | None = None,
) -> dict[str, Any]:
    """The new ``maximizeElsewhere`` record after one tick.

    ``readings``: each slot's newest successful reading (a slot without one
    this tick keeps its record). ``local_busy_at``: the latest moment Claude
    Code is known to have been busy on this machine (now, or its newest
    transcript write; None = none seen); the record remembers the maximum.
    ``local_known``: whether this machine's activity could be told at all —
    an unknown stretch is never "idle". ``roster``: the slots that exist;
    records of others are dropped."""
    block = state.get(STATE_KEY) if isinstance(state, Mapping) else None
    block = block if isinstance(block, Mapping) else {}
    old = block.get("slots")
    old = old if isinstance(old, Mapping) else {}
    local_at = _num(block.get("localAt"))
    if local_busy_at is not None:
        local_at = local_busy_at if local_at is None else max(local_at, local_busy_at)
    slots: dict[str, dict[str, Any]] = {}
    for slot, prev in old.items():
        if isinstance(prev, Mapping) and (roster is None or slot in roster):
            slots[str(slot)] = dict(prev)
    for slot, cur in readings.items():
        prev = slots.get(slot, {})
        rec = dict(prev)
        if slot == active:
            rec["act"] = now
        prev_ts = _num(prev.get("ts"))
        prev_pct = _num(prev.get("pct5"))
        if prev_ts is not None and cur.ts <= prev_ts:
            slots[slot] = rec  # not a newer reading than the one held
            continue
        if prev_ts is not None and prev_pct is not None:
            if _window_rolled(prev, cur):
                for k in ("since", "last", "until5"):
                    rec.pop(k, None)
            elif cur.ts - prev_ts <= MAX_GAP_S and cur.pct5 - prev_pct >= MIN_RISE:
                acted = _num(prev.get("act"))
                if slot == active:
                    # Rose while idle here the whole interval, or it is ours.
                    here = (
                        not local_known
                        or (local_at is not None and local_at > prev_ts)
                    )
                else:
                    # Ours when it was the active one since the earlier reading.
                    here = acted is not None and acted >= prev_ts
                if not here:
                    if not active_mark(rec, cur.ts):
                        rec["since"] = cur.ts
                    rec["last"] = cur.ts
                    rec["until5"] = cur.reset5
        rec.update(ts=cur.ts, pct5=cur.pct5, win=cur.reset5)
        slots[slot] = rec
    for slot, rec in slots.items():
        if "last" in rec and not active_mark(rec, now):
            for k in ("since", "last", "until5"):
                rec.pop(k, None)
    out: dict[str, Any] = {"slots": slots}
    if local_at is not None:
        out["localAt"] = local_at
    return out
