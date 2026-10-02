"""Immutable inputs and outputs of the maximize policy (spec §4.2, §5).

``policy.decide`` sees only these types — never engine objects — so every
decision is reproducible from a Snapshot alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from claude_swap.settings import MaximizeSettings

Tier = Literal["normal", "last_resort", "excluded"]
Trigger = Literal["at-limit", "hard", "soft", "rebalance"]

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


@dataclass(frozen=True)
class Hold:
    reason: str
    pending: bool        # True = soft threshold crossed, waiting for idle


@dataclass(frozen=True)
class Indeterminate:
    reason: str          # active usage unknown — engine takes the upstream failover path


@dataclass(frozen=True)
class Exhausted:
    reason: str          # nothing below the hard caps — engine takes the AllExhausted path


Decision = Switch | Hold | Indeterminate | Exhausted
