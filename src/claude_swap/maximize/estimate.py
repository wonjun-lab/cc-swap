"""What the active account's usage probably is when the reading is old.

The 2026-10-06 incident: three machines shared one account's usage-endpoint
budget, every read 429'd for 90 minutes, and the engine kept deciding on the
last reading (5h 20%, trusted as a lower bound until the window resets)
while the account it was using climbed to 100%. A lower bound is the wrong
end to decide on for the account being consumed. So, for the ACTIVE account
only:

* **Projected** (:func:`project`): when its reading is older than the normal
  cadence explains (``FAILING_AFTER_S`` once a fetch has failed,
  ``STALE_AFTER_S`` otherwise), decide on ``last reading + burn rate ×
  elapsed``, capped at 100%. The burn rate is the one this account was
  measured at while in use (:func:`burn_rates`: the learned ride's
  whole-point steps, the recent velocity of the samples, the faster of the
  two), else a conservative per-plan default (``DEFAULT_RATE_5H``). Soft,
  hard, ETA and at-limit triggers all run on it.
* **Reported** (:func:`reported`): Claude Code wrote a usage-limit refusal
  into a transcript after the account became active (maximize/limit_watch.py)
  — that window is at 100% whatever the usage endpoint last said, until a
  fresh reading taken after the refusal says otherwise.

Non-active accounts keep the store's trust rules: nothing on this machine
consumes them. (The policy refuses to *land* on one whose reading is older
than ``policy.STALE_LANDING_S`` unless nothing else can take you.)

Pure apart from reading the clock-free inputs it is handed. Slot numbers and
percentages only.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from claude_swap import poll_policy
from claude_swap.maximize import drain, idle
from claude_swap.maximize import ride as learned_ride
from claude_swap.maximize.model import Sample, UsageEstimate
from claude_swap.poll_policy import parse_reset_ts

#: A reading this much older than the slowest normal active cadence (its
#: ceiling plus jitter, plus one engine tick) cannot be explained by the
#: schedule: something is keeping the engine from reading the account.
STALE_AFTER_S = (
    poll_policy.ACTIVE_MAX_INTERVAL_S * (1.0 + poll_policy.JITTER_FRAC) + 60.0
)
#: Once a read of the active account has failed, project from the serve TTL.
FAILING_AFTER_S = poll_policy.SERVE_TTL_S
#: 5h points per hour of heavy use when nothing was learned, by plan:
#: measured across this fork's machines (2026-10, usage history and learned
#: steps), busy hours on 20x accounts run at a median ~25 and p90 ~45 %/h,
#: and the learned in-use pace reaches ~60; 5x accounts ~115. A 20x full
#: window in ~2.5h of heavy use, a 5x one in ~50 min.
DEFAULT_RATE_5H = {"20x": 40.0, "5x": 120.0}
#: 7d points per hour when nothing was learned: the 5h default times the
#: plan's 7d-per-5h ratio (maximize/drain.py).
DEFAULT_K = {"20x": drain.K_20X, "5x": drain.K_5X}
WINDOW_KEYS = (("5h", "five_hour", 5 * 3600.0), ("7d", "seven_day", 7 * 86400.0))
#: The projection's learned pace: the median of the last this many timed
#: whole-point steps (``ride.point_seconds``'s ``median_of``).
PROJECTION_STEPS = 3
#: A refusal holds until a reading taken at least this long after it says
#: otherwise (the endpoint's view of the window can trail the refusal).
READING_AFTER_S = 30.0
#: A refusal that carries no reset counts for at most one 5h window.
REPORTED_MAX_AGE_S = 5 * 3600.0
#: A refusal this soon after the account became the live one may still be
#: a Claude Code session on the previous login's token.
SWITCH_GRACE_S = 90.0
#: A refusal's ``resetsAt`` and the reading's ``resets_at`` for the same
#: window of the same account agree to the second (both sit on 10-minute
#: marks); this much slack tolerates rounding without taking a neighbour's.
RESET_MATCH_S = 120.0


@dataclass(frozen=True)
class Estimate:
    """The active account's usage as the engine decides on it, when that is
    not simply its last reading."""

    number: str
    kind: str                         # "projected" | "reported"
    value: dict                       # the usage dict decisions run on
    note: str                         # why / Fleet / poll line wording
    #: pct per hour per window ("5h"/"7d") the projection used
    rates: Mapping[str, float] = field(default_factory=dict)
    #: where each rate came from: "learned", "recent pace", "default 20x" ...
    sources: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, float] = field(default_factory=dict)
    projected: Mapping[str, float] = field(default_factory=dict)
    age_s: float = 0.0
    cause: str = ""
    #: a reported limit: when Claude Code reported it (epoch s)
    reported_at: float | None = None
    #: a reported limit: the windows ("5h"/"7d") Claude Code was refused for
    refused: tuple[str, ...] = ()

    def for_snapshot(self) -> UsageEstimate:
        return UsageEstimate(kind=self.kind, note=self.note, rates=dict(self.rates))

    def to_json(self) -> dict:
        out: dict = {"kind": self.kind, "note": self.note}
        if self.kind == "projected":
            out.update(
                ageS=round(self.age_s),
                cause=self.cause,
                ratesPctPerHour={w: round(r, 2) for w, r in self.rates.items()},
                rateSources=dict(self.sources),
                lastPct=dict(self.raw),
                projectedPct=dict(self.projected),
            )
        if self.reported_at is not None:
            out["reportedAt"] = self.reported_at
        return out


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def stale_age(entry: object, now: float) -> float | None:
    """How old the active account's reading is when it is old enough to
    project (``STALE_AFTER_S``, or ``FAILING_AFTER_S`` once a fetch failed),
    else None."""
    fetched_at = _num(getattr(entry, "fetched_at", None))
    if fetched_at is None:
        return None
    age = now - fetched_at
    failing = (getattr(entry, "consecutive_failures", 0) or 0) > 0
    return age if age > (FAILING_AFTER_S if failing else STALE_AFTER_S) else None


def cause_text(entry: object) -> str:
    """Why there is no fresher reading, for the note."""
    error = getattr(entry, "last_error", None)
    if error == "http-429":
        return "usage reads rate-limited"
    if error:
        return f"usage reads failing ({error})"
    return "no usage read"


def duration_text(seconds: float) -> str:
    minutes = max(1, round(seconds / 60.0))
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def burn_rates(
    *,
    number: str,
    steps: object,
    samples: Sequence[Sample],
    plan: str | None,
    idle_window_min: float,
    now: float,
) -> tuple[dict[str, float], dict[str, str]]:
    """``({window: pct per hour}, {window: source})`` for the projection.

    Learned first: the faster of the learned ride's step pace
    (``ride.point_seconds``, the median of its last three timed steps over
    the last 6h) and the samples' recent velocity (however old the
    samples). A 7d with neither
    follows the 5h at the plan's 7d-per-5h ratio. Anything still unknown
    takes the plan's default (an unknown plan reads as 20x)."""
    rates: dict[str, float] = {}
    sources: dict[str, str] = {}
    v5 = v7 = None
    if len(samples) >= 2:
        window = idle_window_min * 60.0
        newest = max(x.ts for x in samples)
        recent = [x for x in samples if newest - x.ts <= max(window, 1800.0)]
        span = (max(x.ts for x in recent) - min(x.ts for x in recent)) if recent else 0.0
        if span >= idle.MIN_ETA_SPAN_S:
            d5, d7 = idle.span_rise(sorted(recent, key=lambda x: x.ts))
            v5, v7 = d5 / span * 3600.0, d7 / span * 3600.0
    for w, velocity in (("5h", v5), ("7d", v7)):
        known: list[tuple[float, str]] = []
        point = learned_ride.point_seconds(  # type: ignore[arg-type]
            steps, number, w, now, median_of=PROJECTION_STEPS
        )
        if point is not None and point > 0:
            known.append((3600.0 / point, "learned"))
        if velocity is not None and velocity > 0:
            known.append((velocity, "recent pace"))
        if known:
            rate, source = max(known)
            rates[w], sources[w] = rate, source
    name = plan if plan in DEFAULT_RATE_5H else "20x"
    if "5h" not in rates:
        rates["5h"], sources["5h"] = DEFAULT_RATE_5H[name], f"default {name}"
    if "7d" not in rates:
        k = DEFAULT_K[name]
        rates["7d"] = rates["5h"] * k
        sources["7d"] = (
            f"default {name}" if sources["5h"].startswith("default") else f"5h × {k:g}"
        )
    return rates, sources


def _windows(value: Mapping) -> dict[str, tuple[float, float | None]]:
    out: dict[str, tuple[float, float | None]] = {}
    for label, key, _span in WINDOW_KEYS:
        window = value.get(key)
        if isinstance(window, Mapping):
            pct = _num(window.get("pct"))
            if pct is not None:
                out[label] = (pct, parse_reset_ts(window.get("resets_at")))
    return out


def project(
    *,
    number: str,
    value: object,
    entry: object,
    now: float,
    rates: Mapping[str, float],
    sources: Mapping[str, str] | None = None,
) -> Estimate | None:
    """The active account's projected usage, or None when its reading is
    fresh enough (:func:`stale_age`) or carries no window to project.

    Each window: ``pct + rate × elapsed``, capped at 100. Elapsed runs from
    the reading, or from the window's reset when that has passed since (the
    window started over at 0 then; its next reset is a full window on)."""
    if not isinstance(value, Mapping):
        return None
    age = stale_age(entry, now)
    if age is None:
        return None
    fetched_at = now - age
    windows = _windows(value)
    if not windows:
        return None
    out = copy.deepcopy(dict(value))
    raw: dict[str, float] = {}
    projected: dict[str, float] = {}
    for label, key, span in WINDOW_KEYS:
        if label not in windows:
            continue
        pct, reset = windows[label]
        rate = max(0.0, float(rates.get(label, 0.0)))
        if reset is not None and reset <= now:
            # Rolled over since the reading: it restarted from 0.
            start, base = max(reset, fetched_at), 0.0
            out[key]["resets_at"] = _iso(reset + span)
        else:
            start, base = fetched_at, pct
        new = min(100.0, base + rate * max(0.0, now - start) / 3600.0)
        new = round(new, 1) if new < 100.0 else 100.0
        out[key]["pct"] = new
        raw[label] = pct
        projected[label] = new
    cause = cause_text(entry)
    shown = [
        f"{w} ~{projected[w]:.0f}%"
        for w in ("5h", "7d")
        if w in projected and (w == "5h" or abs(projected[w] - raw[w]) >= 1.0)
    ]
    note = (
        f"{' / '.join(shown) or 'usage'} projected — {cause} for {duration_text(age)}"
    )
    return Estimate(
        number=number,
        kind="projected",
        value=out,
        note=note,
        rates={w: float(rates.get(w, 0.0)) for w in projected},
        sources={w: (sources or {}).get(w, "") for w in projected},
        raw=raw,
        projected=projected,
        age_s=age,
        cause=cause,
    )


def clock_text(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def reported(
    *,
    number: str,
    value: object,
    entry: object,
    hits: Sequence[object],
    since: float | None,
    now: float,
    base: Estimate | None = None,
) -> Estimate | None:
    """The active account at 100% on a window Claude Code refused a request
    for, or None.

    The transcripts do not say which account a refusal was for, and they
    can hold other accounts' (a session still on the previous login's
    token, a ``cswap run --share-history`` session, a refusal from before a
    switch the engine did not see). So a refusal counts for the live
    account only when it is provably its own:

    * its ``resetsAt`` is the live account's own reset for that window, as
      its last reading reports it (within ``RESET_MATCH_S``; both are on
      10-minute marks) — and, when ``since`` is known, it came after it
      (two accounts' windows often reset on the same 10-minute mark after
      a priming pass), or
    * that window of the reading has no future reset (rolled over since,
      or off) and the refusal came after ``since`` — when this account
      became the live one, plus a grace for a session still on the old
      token. A refusal with no reset at all (older text-only records) also
      needs ``since``. With ``since`` unknown, only a reset match counts.

    Its reported reset must not have passed, and no reading taken after it
    may show that window under 100% (the usage endpoint, when it answers,
    wins). ``base`` is the projection to raise (or None: the reading)."""
    latest: dict[str, object] = {}
    fetched_at = _num(getattr(entry, "fetched_at", None))
    current = base.value if base is not None else value
    read = _windows(value) if isinstance(value, Mapping) else {}
    for hit in hits:
        ts = getattr(hit, "ts", None)
        window = getattr(hit, "window", None)
        resets = getattr(hit, "resets_at", None)
        if not isinstance(ts, (int, float)) or window not in ("5h", "7d"):
            continue
        if ts > now + 300.0:
            continue
        if since is not None and ts < since:
            continue  # before this account went live: another account's
        own_reset = read[window][1] if window in read else None
        matched = (
            resets is not None
            and own_reset is not None
            and abs(resets - own_reset) <= RESET_MATCH_S
        )
        if not matched:
            after_since = since is not None and ts >= since
            if resets is not None and own_reset is not None and own_reset > now:
                continue  # another account's window
            if not after_since:
                continue
        if resets is not None and resets <= now:
            continue
        if resets is None and now - ts > REPORTED_MAX_AGE_S:
            continue
        if (
            fetched_at is not None
            and fetched_at > ts + READING_AFTER_S
            and window in read
            and read[window][0] < 100.0
        ):
            continue  # a reading taken after the refusal says it is not at 100%
        prior = latest.get(window)
        if prior is None or ts > getattr(prior, "ts", 0.0):
            latest[window] = hit
    if not latest:
        return None
    out = copy.deepcopy(dict(current)) if isinstance(current, Mapping) else {}
    parts = []
    first_ts = min(getattr(h, "ts") for h in latest.values())
    for label, key, _span in WINDOW_KEYS:
        hit = latest.get(label)
        if hit is None:
            continue
        window = out.get(key) if isinstance(out.get(key), dict) else {}
        window = dict(window)
        window["pct"] = 100.0
        resets = getattr(hit, "resets_at", None)
        if resets is not None:
            window["resets_at"] = _iso(resets)
        out[key] = window
        parts.append(label)
    note = (
        f"Claude Code reported the {'/'.join(parts)} usage limit at "
        f"{clock_text(first_ts)}"
    )
    if base is not None:
        note = f"{note}; {base.note}"
    return Estimate(
        number=number,
        kind="reported",
        value=out,
        note=note,
        rates=dict(base.rates) if base is not None else {},
        sources=dict(base.sources) if base is not None else {},
        raw=dict(base.raw) if base is not None else {},
        projected=dict(base.projected) if base is not None else {},
        age_s=base.age_s if base is not None else 0.0,
        cause=base.cause if base is not None else "",
        reported_at=first_ts,
        refused=tuple(parts),
    )
