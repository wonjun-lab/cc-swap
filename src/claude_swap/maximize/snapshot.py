"""Engine state → immutable :class:`Snapshot` (spec §4.2). No I/O.

``usage`` values are the engine's decision values: a normalized usage dict
(``five_hour``/``seven_day`` each ``{"pct", "resets_at"}``), a sentinel
string, or None.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from claude_swap.maximize.idle import trim_samples
from claude_swap.maximize.model import (
    AccountView,
    Forecast,
    RideFiveH,
    Sample,
    Snapshot,
    UsageEstimate,
)
from claude_swap.maximize.names import record_names
from claude_swap.maximize.plan import plan_name, plan_weight
from claude_swap.maximize.tiers import parse_account_list, tier_for
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.settings import MaximizeSettings

Windows = tuple[float | None, float | None, float | None, float | None]


def _window(value: Mapping, key: str) -> tuple[float | None, float | None]:
    window = value.get(key)
    if not isinstance(window, Mapping):
        return None, None
    pct = window.get("pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None, None
    return float(pct), parse_reset_ts(window.get("resets_at"))


def usage_windows(value: object, now: float) -> Windows:
    """``(pct5, reset5, pct7, reset7)`` from one decision value.

    * Not a dict, or neither window readable → all None (usage unknown).
    * Only one window readable → the other is 0% with no reset: the server
      reported nothing that could bind there (upstream ``account_headroom``
      reads such a reading the same way).
    * 5h reset at/before ``now`` → pct5 0 (the window rolled over and is off)
      but reset5 kept, so the primer can key the cold window on it.
    * 7d reset at/before ``now`` → pct7 0 and reset7 None (score then
      assumes a fresh 7-day window).
    """
    if not isinstance(value, Mapping):
        return None, None, None, None
    pct5, reset5 = _window(value, "five_hour")
    pct7, reset7 = _window(value, "seven_day")
    if pct5 is None and pct7 is None:
        return None, None, None, None
    if pct5 is None:
        pct5, reset5 = 0.0, None
    if pct7 is None:
        pct7, reset7 = 0.0, None
    if reset5 is not None and reset5 <= now:
        pct5 = 0.0
    if reset7 is not None and reset7 <= now:
        pct7, reset7 = 0.0, None
    return pct5, reset5, pct7, reset7


def build_snapshot(
    *,
    now: float,
    active: str | None,
    usage: Mapping[str, dict | str | None],
    records: Mapping[str, Mapping],
    quarantined: set[str],
    api_key_accounts: set[str],
    rate_limit_tiers: Mapping[str, str | None],
    samples: Sequence[Sample],
    last_switch_at: float | None,
    settings: MaximizeSettings,
    active_changed_at: float | None = None,
    login_deadlines: Mapping[str, float] | None = None,
    forecast: Forecast | None = None,
    rates7: Mapping[str, float] | None = None,
    active_recent_429: bool = False,
    hold_until: float | None = None,
    ride_armed_at: Mapping[str, float] | None = None,
    ride_point_s: Mapping[str, float] | None = None,
    ride_q: Mapping[str, float] | None = None,
    ride_5h: Mapping[str, RideFiveH] | None = None,
    ride_t: Mapping[str, float] | None = None,
    k7: Mapping[str, float] | None = None,
    ride_k7: Mapping[str, float] | None = None,
    ages: Mapping[str, float | None] | None = None,
    estimate: UsageEstimate | None = None,
    local_idle: bool | None = None,
) -> Snapshot:
    """One view per ``records`` entry, in ``records`` order (sequence order).

    ``ages``: how old each account's reading is (seconds), for the stale
    landing rule; ``estimate``/``local_idle``: the active account's usage is
    an estimate (``Snapshot.estimate``)."""
    last_resort = parse_account_list(settings.last_resort)
    shown = record_names(records)
    views: list[AccountView] = []
    for number, record in records.items():
        number = str(number)
        email = str(record.get("email") or "")
        pct5, reset5, pct7, reset7 = usage_windows(usage.get(number), now)
        views.append(
            AccountView(
                number=number,
                email=email,
                tier=tier_for(record, email, last_resort),
                plan_weight=plan_weight(
                    rate_limit_tiers.get(number), email, settings.plan_override
                ),
                pct5=pct5,
                reset5=reset5,
                pct7=pct7,
                reset7=reset7,
                quarantined=number in quarantined,
                api_key=number in api_key_accounts,
                login_deadline=(login_deadlines or {}).get(number),
                plan=plan_name(
                    rate_limit_tiers.get(number), email, settings.plan_override
                ),
                age_s=(ages or {}).get(number),
                name=shown.get(number, ""),
            )
        )
    return Snapshot(
        now=now,
        active=active,
        accounts=tuple(views),
        samples=trim_samples(samples, now),
        last_switch_at=last_switch_at,
        settings=settings,
        active_changed_at=active_changed_at,
        forecast=forecast,
        rates7=dict(rates7 or {}),
        active_recent_429=active_recent_429,
        hold_until=hold_until,
        ride_armed_at=dict(ride_armed_at or {}),
        ride_point_s=dict(ride_point_s or {}),
        ride_q=dict(ride_q or {}),
        ride_5h=dict(ride_5h or {}),
        ride_t=dict(ride_t or {}),
        k7=dict(k7 or {}),
        ride_k7=dict(ride_k7 or {}),
        estimate=estimate,
        local_idle=local_idle,
    )
