"""Immutable inputs and outputs of the maximize policy (spec §4.2, §5).

``policy.decide`` sees only these types — never engine objects — so every
decision is reproducible from a Snapshot alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from claude_swap.settings import MaximizeSettings

Tier = Literal["normal", "last_resort", "excluded"]
Trigger = Literal["at-limit", "hard", "soft", "preempt", "rebalance"]
# A hold's reason code beyond pending/plain (engine NoSwitchEvent reason).
# ``hold``: an account hold (``cc-swap hold``, maximize/hold.py) set aside a
# soft, preempt or rebalance move. ``hard-stay``: a hard trigger fired but
# no account has more room than the active, so it stays until 100%.
# ``ride``: a hard mark in the last point is reached and the learned ride
# keeps using it a little longer (``maximize.learnedRide``).
HoldCode = Literal[
    "reset-wait", "preempt", "rebalance-deferred", "hold", "hard-stay", "ride"
]
# How a learned ride ended in a switch: its time was up (``due``), or an
# idle moment came first (``idle``; nothing is learned from it).
RideEnd = Literal["due", "idle"]

# Lower sorts first. ``excluded`` is listed only so every tier has an order;
# an excluded account is never landable (score.landable).
TIER_ORDER: dict[str, int] = {"normal": 0, "last_resort": 1, "excluded": 2}


@dataclass(frozen=True)
class AccountView:
    number: str
    email: str
    tier: Tier
    plan_weight: int            # 5x=1, 20x=4, unknown=1
    pct5: float | None          # None = usage unknown
    reset5: float | None        # epoch; None (or <= now) = 5h window off
    pct7: float | None
    reset7: float | None
    quarantined: bool
    api_key: bool
    # When the stored login lapses (epoch s, ``refreshTokenExpiresAt``);
    # None = the login records no deadline (never treated as expiring).
    login_deadline: float | None = None


@dataclass(frozen=True)
class Sample:
    ts: float
    pct5: float
    pct7: float


@dataclass(frozen=True)
class QuietWindow:
    """A predicted quiet stretch (maximize/history.py): epochs plus local
    ``HH:MM`` labels for reasons."""

    start: float
    end: float
    start_label: str
    end_label: str


@dataclass(frozen=True)
class Forecast:
    """The learned idle pattern as of ``Snapshot.now`` (``history.forecast``).
    None on a Snapshot means no pattern: a cold start or learning off."""

    days: int                         # days with observations, last 14
    p_busy_now: float | None          # None: this time slot was never observed
    current: QuietWindow | None       # the quiet window ``now`` is inside
    next: QuietWindow | None          # the first one starting after ``now``


@dataclass(frozen=True)
class Snapshot:
    now: float
    active: str | None
    accounts: tuple[AccountView, ...]
    samples: tuple[Sample, ...]          # active account, last 30 min, oldest first
    last_switch_at: float | None
    settings: MaximizeSettings
    # When the active account last changed by any route (an engine switch or
    # a manual login the engine noticed); None when no change was seen.
    active_changed_at: float | None = None
    # From the usage history (maximize/history.py): the idle pattern, and
    # each account's 7d burn rate (pct/hour while active; absent = unknown).
    forecast: Forecast | None = None
    rates7: Mapping[str, float] = field(default_factory=dict)
    # The active account's usage token 429'd recently (``UsageEntry.recent_429``):
    # it keeps the post-429 cadence, so it cannot be polled every 60 s.
    active_recent_429: bool = False
    # An account hold on the active account (maximize/hold.py): until then
    # (epoch s) the soft, preempt and rebalance triggers are set aside. None
    # = no hold; the engine passes one only while its slot is the active one.
    hold_until: float | None = None
    # The learned ride (maximize/ride.py), by window ("5h"/"7d"): when the
    # engine first saw the window at its hard mark (epoch s; absent = the
    # policy reads it off the samples), its seconds per point measured from
    # whole-point steps (absent = the recent velocity), and the learned
    # share of the last point to ride (absent = ``ride.Q_DEFAULT``).
    ride_armed_at: Mapping[str, float] = field(default_factory=dict)
    ride_point_s: Mapping[str, float] = field(default_factory=dict)
    ride_q: Mapping[str, float] = field(default_factory=dict)

    def view(self, number: str | None) -> AccountView | None:
        """The account with this slot number, or None."""
        if number is None:
            return None
        for v in self.accounts:
            if v.number == number:
                return v
        return None


@dataclass(frozen=True)
class Switch:
    target: str
    trigger: Trigger
    reason: str
    # A hard switch that ends a learned ride, and on which window(s).
    ride: RideEnd | None = None
    ride_windows: tuple[str, ...] = ()
    # A ``due`` ride that ``maximize.rideMaxMin`` ended before its learned
    # share: it says nothing about q, so nothing is learned from it.
    ride_capped: bool = False


@dataclass(frozen=True)
class Hold:
    reason: str
    pending: bool        # True = soft threshold crossed, waiting for idle
    # A reset-aware wait (``maximize.resetWaitMin``): when the last window
    # being waited out resets (epoch s). None for every other hold.
    reset_wait_until: float | None = None
    # The reason code when it is none of maximize-pending/-hold.
    code: HoldCode | None = None
    # A learned ride (code ``ride``): when it switches unless an idle moment
    # comes first (epoch s), and the window(s) it rides.
    ride_until: float | None = None
    ride_windows: tuple[str, ...] = ()


@dataclass(frozen=True)
class Indeterminate:
    reason: str          # active usage unknown — engine takes the upstream failover path


@dataclass(frozen=True)
class Exhausted:
    reason: str          # nothing below the hard caps — engine takes the AllExhausted path


Decision = Switch | Hold | Indeterminate | Exhausted
