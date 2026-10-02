"""Active-account usage velocity: idle detection and hard-cap ETA (spec §5.6).

Samples are ``(ts, pct5, pct7)`` readings of the ACTIVE account, one per
fresh fetch, stamped with the fetch time (``fetchedAt``) — a cached reading
served again is the same sample, never a new one. Utilization is reported in
whole percent, so a slow trickle reads as a plateau — that is what "idle"
means here: no more than ``idle_max_delta_pct`` of 5h (and 1 point of 7d)
over ``idle_window_min``. Too few samples, or a hole in them (laptop sleep,
a 429 episode), is never idle: the policy waits.
"""

from __future__ import annotations

from collections.abc import Sequence

from claude_swap.maximize.model import Sample
from claude_swap.settings import MaximizeSettings

SAMPLE_KEEP_S = 1800.0
# A reading is a sample only while fresh (spec §5.6: within 3 minutes).
FRESH_SAMPLE_S = 180.0
# Spec §5.6 fixes the 7d allowance at 1 point; only the 5h one is a setting.
IDLE_MAX_DELTA_7D_PCT = 1.0
# Whole-percent readings taken close together turn rounding into a fake
# velocity (+1 point over 30 s reads as 2 pts/min); never extrapolate a span
# shorter than this.
MIN_ETA_SPAN_S = 120.0


def _ordered(samples: Sequence[Sample]) -> list[Sample]:
    return sorted(samples, key=lambda x: x.ts)


def trim_samples(
    samples: Sequence[Sample], now: float, keep_s: float = SAMPLE_KEEP_S
) -> tuple[Sample, ...]:
    """Oldest-first samples no older than ``keep_s``, one per timestamp."""
    out: list[Sample] = []
    for x in _ordered(samples):
        if x.ts < now - keep_s:
            continue
        if out and x.ts == out[-1].ts:
            continue  # the same reading recorded twice
        out.append(x)
    return tuple(out)


def idle_evidence(
    samples: Sequence[Sample], now: float, s: MaximizeSettings
) -> tuple[Sample, Sample] | None:
    """The ``(older, newest)`` pair idle is judged on, or None if there is none.

    ``newest`` is the latest sample and must itself be within the window of
    ``now`` (older evidence says nothing about the present); ``older`` is the
    latest sample at least ``idle_window_min`` before it. No two consecutive
    samples between them may be more than ``idle_window_min`` apart: a hole
    is missing observation, not a plateau.
    """
    ordered = _ordered(samples)
    if len(ordered) < 2:
        return None
    window = s.idle_window_min * 60.0
    newest = ordered[-1]
    if now - newest.ts > window:
        return None
    for i in range(len(ordered) - 2, -1, -1):
        if newest.ts - ordered[i].ts >= window:
            span = ordered[i:]
            if any(b.ts - a.ts > window for a, b in zip(span, span[1:])):
                return None
            return ordered[i], newest
    return None


def is_idle(samples: Sequence[Sample], now: float, s: MaximizeSettings) -> bool:
    pair = idle_evidence(samples, now, s)
    if pair is None:
        return False
    older, newest = pair
    return (
        newest.pct5 - older.pct5 <= s.idle_max_delta_pct
        and newest.pct7 - older.pct7 <= IDLE_MAX_DELTA_7D_PCT
    )


def eta_to_hard_min(
    samples: Sequence[Sample], s: MaximizeSettings
) -> float | None:
    """Minutes until the first hard cap at the recent burn rate, or None.

    Velocity is measured from the earliest sample within ``idle_window_min``
    of the newest (the nearest older sample when polling is sparser than the
    window). A window with zero or negative velocity has no ETA; None when
    neither window is climbing or the span is too short to trust.
    """
    ordered = _ordered(samples)
    if len(ordered) < 2:
        return None
    newest = ordered[-1]
    window = s.idle_window_min * 60.0
    inside = [x for x in ordered[:-1] if newest.ts - x.ts <= window]
    older = inside[0] if inside else ordered[-2]
    span_s = newest.ts - older.ts
    if span_s < MIN_ETA_SPAN_S:
        return None
    span_min = span_s / 60.0
    etas: list[float] = []
    for new_pct, old_pct, cap in (
        (newest.pct5, older.pct5, s.hard_5h),
        (newest.pct7, older.pct7, s.hard_7d),
    ):
        velocity = (new_pct - old_pct) / span_min
        if velocity > 0:
            etas.append(max(cap - new_pct, 0.0) / velocity)
    return min(etas) if etas else None
