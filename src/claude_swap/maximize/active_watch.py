"""The engine's watch on the active account between readings (cc-swap fork).

Each tick ``AutoSwitchEngine._collect_scheduled_usage`` asks
:func:`estimate_active` what the active account's usage should be decided
on: its reading when that is fresh, else a projection
(maximize/estimate.py), raised to 100% on a window Claude Code reported a
usage-limit refusal for (maximize/limit_watch.py). Strategy-agnostic: the
learned rates come from the maximize records in the state file when they
exist and fall back to the plan defaults otherwise.

Never raises into a tick: the engine wraps every call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from claude_swap.maximize import estimate as est
from claude_swap.maximize import limit_watch
from claude_swap.maximize import ride as learned_ride

_logger = logging.getLogger("claude-swap")

WATCH_ATTR = "_active_watch"
#: Refusals remembered between ticks, at most.
MAX_HITS = 32


@dataclass
class ActiveWatch:
    """Per-engine memory: the transcript watcher, the refusals it found, and
    when this engine saw the live account change."""

    watcher: limit_watch.TranscriptWatcher
    hits: list[limit_watch.LimitHit] = field(default_factory=list)
    #: (slot, when this engine first saw it live); None before the first tick.
    seen: tuple[str, float] | None = None
    #: whether ``seen`` is a change this engine witnessed (not its first look)
    witnessed: bool = False
    logged: set[tuple[float, str]] = field(default_factory=set)
    last_note: str | None = None
    #: (slot, {window: pct/hour}, {window: source}) from the last estimate:
    #: the policy's pace when the samples cannot measure one.
    rates: tuple[str, dict[str, float], dict[str, str]] | None = None

    def poll(self, now: float) -> None:
        new = self.watcher.poll(now)
        if new:
            self.hits = (self.hits + new)[-MAX_HITS:]
        self.hits = [
            h for h in self.hits
            if (h.resets_at is None and now - h.ts <= est.REPORTED_MAX_AGE_S)
            or (h.resets_at is not None and h.resets_at > now)
        ]

    def note_active(self, current: str, now: float) -> None:
        if self.seen is None:
            self.seen = (current, now)
        elif self.seen[0] != current:
            self.seen, self.witnessed = (current, now), True


def watch_for(engine: Any) -> ActiveWatch:
    watch = getattr(engine, WATCH_ATTR, None)
    if not isinstance(watch, ActiveWatch):
        watch = ActiveWatch(watcher=limit_watch.TranscriptWatcher(root=limit_watch.default_root))
        setattr(engine, WATCH_ATTR, watch)
    return watch


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _since(state: Mapping, current: str, watch: ActiveWatch) -> float | None:
    """When ``current`` became the live account, as far as anyone recorded
    it: the engine's last switch onto it, the maximize samples' change
    stamp, or a change this engine witnessed."""
    from claude_swap.maximize.engine_hook import CHANGED_KEY, SAMPLES_KEY

    times: list[float] = []
    last = _num(state.get("lastSwitchAt"))
    if last is not None:
        times.append(last)
    record = state.get(SAMPLES_KEY)
    if isinstance(record, Mapping) and str(record.get("account")) == current:
        changed = _num(record.get(CHANGED_KEY))
        if changed is not None:
            times.append(changed)
    if watch.witnessed and watch.seen is not None and watch.seen[0] == current:
        times.append(watch.seen[1])
    return max(times) if times else None


def _plan_and_idle_window(engine: Any, current: str) -> tuple[str | None, float]:
    from claude_swap.maximize.engine_hook import RUNTIME_ATTR
    from claude_swap.maximize.plan import plan_name

    rt = getattr(engine, RUNTIME_ATTR, None)
    if rt is None:
        return None, 10.0
    settings = getattr(rt, "settings", None)
    window = float(getattr(settings, "idle_window_min", 10) or 10)
    cached = getattr(rt, "tier_cache", {}).get(current)
    if not cached:
        return None, window
    email, tier, _at = cached
    try:
        return plan_name(tier, email, getattr(settings, "plan_override", None)), window
    except Exception:
        return None, window


def estimate_active(
    engine: Any,
    current: str,
    entries: Mapping[str, Any],
    usage: Mapping[str, object],
    *,
    now: float,
    poll: bool = True,
    state: Mapping | None = None,
) -> est.Estimate | None:
    """What ``current``'s usage is decided on this tick when that is not its
    stored reading (``None``: decide on the reading)."""
    from claude_swap.maximize.engine_hook import _stored_samples

    watch = watch_for(engine)
    watch.note_active(current, now)
    if poll:
        watch.poll(now)
    if state is None:
        state = engine._read_state()
    entry = entries.get(current)
    value = usage.get(current)
    plan, idle_window = _plan_and_idle_window(engine, current)
    rates, sources = est.burn_rates(
        number=current,
        steps=state.get(learned_ride.STEPS_KEY),
        samples=_stored_samples(state, current),
        plan=plan,
        idle_window_min=idle_window,
        now=now,
    )
    watch.rates = (current, dict(rates), dict(sources))
    projected = (
        est.project(
            number=current, value=value, entry=entry, now=now,
            rates=rates, sources=sources,
        )
        if isinstance(value, Mapping)
        else None
    )
    since = _since(state, current, watch)
    reported = None
    if watch.hits and not isinstance(value, str):
        reported = est.reported(
            number=current,
            value=value,
            entry=entry,
            hits=watch.hits,
            since=None if since is None else since + est.SWITCH_GRACE_S,
            now=now,
            base=projected,
        )
    result = reported or projected
    _log_once(watch, result, current)
    return result


def _log_once(watch: ActiveWatch, result: est.Estimate | None, current: str) -> None:
    """One log line when the estimate starts, or changes kind."""
    if result is None:
        watch.last_note = None
        return
    if result.kind == "reported" and result.reported_at is not None:
        key = (result.reported_at, current)
        if key not in watch.logged:
            watch.logged.add(key)
            _logger.warning(
                "Account-%s: %s; treating it as at its limit", current, result.note
            )
        return
    kind = f"{current}:{result.kind}"
    if watch.last_note != kind:
        watch.last_note = kind
        _logger.warning(
            "Account-%s: deciding on projected usage (%s; rates %s)",
            current,
            result.note,
            ", ".join(
                f"{w} {r:.1f}%/h ({result.sources.get(w, '?')})"
                for w, r in result.rates.items()
            ),
        )


def local_idle(engine: Any, now: float, idle_window_min: float) -> bool | None:
    """Whether no Claude Code transcript on this machine was written in the
    last ``idle_window_min`` minutes; None when no transcript was ever seen
    (the samples decide then)."""
    watch = getattr(engine, WATCH_ATTR, None)
    if not isinstance(watch, ActiveWatch):
        return None
    last = watch.watcher.last_write
    if not watch.watcher.available or last is None:
        return None
    return now - last >= idle_window_min * 60.0


def quiet_for_s(engine: Any, now: float) -> float | None:
    watch = getattr(engine, WATCH_ATTR, None)
    if not isinstance(watch, ActiveWatch) or watch.watcher.last_write is None:
        return None
    return max(0.0, now - watch.watcher.last_write)


def fallback_rates(engine: Any, current: str) -> tuple[dict[str, float], dict[str, str]]:
    """``current``'s burn rate (pct/hour by window) and where it came from,
    as this tick's estimate worked it out; empty when there is none."""
    watch = getattr(engine, WATCH_ATTR, None)
    if not isinstance(watch, ActiveWatch) or watch.rates is None:
        return {}, {}
    number, rates, sources = watch.rates
    return (dict(rates), dict(sources)) if number == current else ({}, {})
