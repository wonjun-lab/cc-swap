"""Active-account usage velocity: idle detection and hard-cap ETA (spec §5.6).

Samples are ``(ts, pct5, pct7)`` readings of the ACTIVE account, one per
fresh fetch, stamped with the fetch time (``fetchedAt``) — a cached reading
served again is the same sample, never a new one. Utilization is reported in
whole percent, so a slow trickle reads as a plateau — that is what "idle"
means here: no more than ``idle_max_delta_pct`` of 5h (and 1 point of 7d)
over ``idle_window_min``. Too few samples, or a hole in them (laptop sleep,
a 429 episode), is never idle: the policy waits.

Usage over a span is the sum of the increases between consecutive samples
(``span_rise``), never last-minus-first: a window reset inside the span
drops the reading (76% -> 3%), and a net difference would read that busy
stretch as a negative, i.e. idle. The drop itself adds nothing; climbing
on either side of it counts. Idle and the hard-cap ETA share this measure.
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


def span_rise(span: Sequence[Sample]) -> tuple[float, float]:
    """``(5h, 7d)`` points used across ``span`` (oldest first).

    The sum of the increases between consecutive samples. A drop is a window
    reset and adds nothing; increases after it still count, so a busy span
    that crosses a rollover never nets out to "unused".
    """
    d5 = d7 = 0.0
    for a, b in zip(span, span[1:]):
        d5 += max(b.pct5 - a.pct5, 0.0)
        d7 += max(b.pct7 - a.pct7, 0.0)
    return d5, d7


def idle_span(
    samples: Sequence[Sample], now: float, s: MaximizeSettings
) -> tuple[Sample, ...] | None:
    """The oldest-first samples idle is judged on, or None if there are none.

    The span ends at the latest sample, which must itself be within the
    window of ``now`` (older evidence says nothing about the present), and
    starts at the latest sample at least ``idle_window_min`` before it. No
    two consecutive samples in it may be more than ``idle_window_min``
    apart: a hole is missing observation, not a plateau.
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
            return tuple(span)
    return None


def idle_evidence(
    samples: Sequence[Sample], now: float, s: MaximizeSettings
) -> tuple[Sample, Sample] | None:
    """The ``(older, newest)`` ends of :func:`idle_span`, or None."""
    span = idle_span(samples, now, s)
    return None if span is None else (span[0], span[-1])


def is_idle(samples: Sequence[Sample], now: float, s: MaximizeSettings) -> bool:
    span = idle_span(samples, now, s)
    if span is None:
        return False
    d5, d7 = span_rise(span)
    return d5 <= s.idle_max_delta_pct and d7 <= IDLE_MAX_DELTA_7D_PCT


def velocity(
    samples: Sequence[Sample], s: MaximizeSettings
) -> tuple[float | None, float | None]:
    """``(5h, 7d)`` points per minute at the recent burn rate.

    :func:`span_rise` over the span from the earliest sample within
    ``idle_window_min`` of the newest (the nearest older sample when polling
    is sparser than the window), so a reset inside the span cannot hide a
    climb after it. A window that did not climb reads 0; both are None when
    the span is too short to trust.
    """
    ordered = _ordered(samples)
    if len(ordered) < 2:
        return None, None
    newest = ordered[-1]
    window = s.idle_window_min * 60.0
    inside = [i for i, x in enumerate(ordered[:-1]) if newest.ts - x.ts <= window]
    start = inside[0] if inside else len(ordered) - 2
    span = ordered[start:]
    span_s = newest.ts - span[0].ts
    if span_s < MIN_ETA_SPAN_S:
        return None, None
    span_min = span_s / 60.0
    d5, d7 = span_rise(span)
    return d5 / span_min, d7 / span_min


def eta_to_hard(
    samples: Sequence[Sample], s: MaximizeSettings
) -> tuple[float | None, float | None]:
    """``(5h, 7d)`` minutes until each hard cap at the recent burn rate
    (:func:`velocity`, from the newest sample's reading). A window that did
    not climb has no ETA (None); both are None when the span is too short
    to trust.
    """
    v5, v7 = velocity(samples, s)
    if v5 is None or v7 is None:
        return None, None
    newest = _ordered(samples)[-1]

    def eta(rate: float, now_pct: float, cap: float) -> float | None:
        return max(cap - now_pct, 0.0) / rate if rate > 0 else None

    return eta(v5, newest.pct5, s.hard_5h), eta(v7, newest.pct7, s.hard_7d)


def eta_to_hard_min(
    samples: Sequence[Sample], s: MaximizeSettings
) -> float | None:
    """Minutes until the first hard cap (see :func:`eta_to_hard`), or None
    when neither window is climbing or the span is too short to trust."""
    etas = [e for e in eta_to_hard(samples, s) if e is not None]
    return min(etas) if etas else None
