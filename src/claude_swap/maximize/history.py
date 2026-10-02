"""Usage history and the learned idle pattern (cc-swap fork, maximize).

The engine appends to ``<backup root>/usage_history.jsonl`` from readings it
already has — never an extra poll, never a token refresh — so the poll
budget is unchanged. Two kinds of line:

* ``{"k": "u", "t": ts, "n": "1", "p5": 62.0, "p7": 40.0, "a": 1}`` — a usage
  point: the first reading of each account in each clock hour, and whether
  it was the active account (``a``). Kept :data:`POINTS_KEEP_S` (8 days).
  :func:`burn_rate` turns them into an account's 7d pace.
* ``{"k": "s", "t": slot_start, "b": 1}`` — one observed 15-minute slot:
  whether the active account's 5h rose in it (``b``). Kept
  :data:`SLOTS_KEEP_S` (14 days). :func:`learn` and :func:`forecast` turn
  them into the idle pattern: P(busy) per local time slot, weekdays and
  weekends apart, and the quiet windows it predicts.

Only the engine holding the lease writes (:class:`Recorder`): it appends
with ``O_APPEND`` and compacts by an atomic rewrite once the file holds well
more than it keeps. Readers (``cc-swap why``, doctor, Fleet) use
:func:`read`; a torn or foreign line is skipped. Slot numbers and
percentages only — never an email or a token.

Local time (slot of day, weekday) comes from a ``localtime`` callable,
``time.localtime`` unless a caller passes another (tests use ``gmtime``).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.fsutil import replace_with_retry
from claude_swap.maximize.idle import FRESH_SAMPLE_S
from claude_swap.maximize.model import Forecast, QuietWindow, Sample

HISTORY_FILENAME = "usage_history.jsonl"
HOUR_S = 3600.0
SLOT_S = 900.0
POINTS_KEEP_S = 8 * 86400.0
SLOTS_KEEP_S = 14 * 86400.0
#: Only the newest this-many bytes are read; a longer file is compacted.
MAX_READ_BYTES = 4 * 1024 * 1024
#: Compact once the file holds more than twice what it keeps, plus this.
COMPACT_SLACK_LINES = 200
#: A reading older than this when the engine sees it is not recorded.
READING_MAX_AGE_S = 900.0
#: Two consecutive active readings further apart than this say nothing
#: about the slot the later one falls in (a sleep, a stopped engine).
PAIR_GAP_S = 1800.0
#: A slot is judged once a reading fetched inside it can no longer arrive
#: (the engine samples a reading while it is FRESH_SAMPLE_S old).
SLOT_SETTLE_S = FRESH_SAMPLE_S

# -- burn rate --
#: The 7d pace looks back this far ...
RATE_LOOKBACK_S = 48 * 3600.0
#: ... weighting an hour half as much every this-many hours of age ...
RATE_HALF_LIFE_H = 12.0
#: ... skipping holes longer than this between two points ...
RATE_MAX_GAP_S = 3 * 3600.0
#: ... and needs at least this many active hours to say anything.
RATE_MIN_HOURS = 3.0

# -- idle pattern --
LEARN_S = 14 * 86400.0
#: Fewer days with observations than this is a cold start: no pattern.
MIN_DAYS = 3
#: A slot is quiet below this P(busy) ...
QUIET_P = 0.2
#: ... and a quiet window is a run of quiet slots at least this long.
QUIET_MIN_S = 3600.0
#: How far back and ahead of ``now`` :func:`forecast` looks for windows.
FORECAST_BACK_S = 24 * 3600.0
FORECAST_AHEAD_S = 48 * 3600.0

LocalTime = Callable[[float], time.struct_time]


@dataclass(frozen=True)
class UsagePoint:
    ts: float
    number: str
    pct5: float
    pct7: float
    active: bool


@dataclass(frozen=True)
class SlotObs:
    ts: float      # slot start, a multiple of SLOT_S
    busy: bool


@dataclass(frozen=True)
class History:
    points: tuple[UsagePoint, ...] = ()   # oldest first
    slots: tuple[SlotObs, ...] = ()       # oldest first


def path_for(root: Path) -> Path:
    return Path(root) / HISTORY_FILENAME


# -- file -----------------------------------------------------------------------------


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _parse(line: str) -> UsagePoint | SlotObs | None:
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    ts = _num(raw.get("t"))
    if ts is None:
        return None
    if raw.get("k") == "u":
        number, p5, p7 = raw.get("n"), _num(raw.get("p5")), _num(raw.get("p7"))
        if not isinstance(number, str) or not number or p5 is None or p7 is None:
            return None
        return UsagePoint(ts, number, p5, p7, raw.get("a") in (1, True))
    if raw.get("k") == "s":
        return SlotObs(ts, raw.get("b") in (1, True))
    return None


def _line(item: UsagePoint | SlotObs) -> str:
    if isinstance(item, UsagePoint):
        raw = {"k": "u", "t": item.ts, "n": item.number, "p5": item.pct5,
               "p7": item.pct7, "a": int(item.active)}
    else:
        raw = {"k": "s", "t": item.ts, "b": int(item.busy)}
    return json.dumps(raw, separators=(",", ":")) + "\n"


def _read_lines(path: Path) -> tuple[list[str], bool]:
    """The file's lines (``[]`` when missing or unreadable) and whether it
    was longer than :data:`MAX_READ_BYTES` (only its tail was read)."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            truncated = size > MAX_READ_BYTES
            if truncated:
                f.seek(size - MAX_READ_BYTES)
            data = f.read()
    except OSError:
        return [], False
    lines = data.decode("utf-8", errors="replace").splitlines()
    if truncated and lines:
        lines = lines[1:]  # the first one starts mid-line
    return lines, truncated


def from_items(items: Iterable[UsagePoint | SlotObs]) -> History:
    """A History from parsed records, oldest first."""
    items = list(items)
    return History(
        points=tuple(sorted((x for x in items if isinstance(x, UsagePoint)), key=lambda p: p.ts)),
        slots=tuple(sorted((x for x in items if isinstance(x, SlotObs)), key=lambda s: s.ts)),
    )


def trimmed(h: History, now: float) -> History:
    """``h`` without what is past its keep window or in the future."""
    return History(
        points=tuple(p for p in h.points if now - POINTS_KEEP_S <= p.ts <= now),
        slots=tuple(s for s in h.slots if now - SLOTS_KEEP_S <= s.ts <= now),
    )


def compacted(h: History) -> History:
    """One point per account per clock hour (the first), one observation
    per slot (busy if any said so)."""
    points: list[UsagePoint] = []
    seen: set[tuple[str, int]] = set()
    for p in h.points:
        key = (p.number, int(p.ts // HOUR_S))
        if key not in seen:
            seen.add(key)
            points.append(p)
    slots: dict[float, bool] = {}
    for s in h.slots:
        slots[s.ts] = slots.get(s.ts, False) or s.busy
    return History(
        points=tuple(points),
        slots=tuple(SlotObs(ts, busy) for ts, busy in sorted(slots.items())),
    )


def read(root: Path, now: float | None = None) -> History:
    """The history under ``root``, oldest first; trimmed to its keep
    windows when ``now`` is given. Never raises."""
    lines, _ = _read_lines(path_for(root))
    h = compacted(from_items(x for x in map(_parse, lines) if x is not None))
    return h if now is None else trimmed(h, now)


def _append(path: Path, lines: Sequence[str]) -> None:
    data = "".join(lines).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def _rewrite(path: Path, h: History) -> None:
    """Replace the file with ``h``, atomically (0600)."""
    items = sorted([*h.points, *h.slots], key=lambda x: x.ts)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, "".join(_line(x) for x in items).encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp, str(path))
        if os.name == "posix":
            os.chmod(path, 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- recording (engine) -------------------------------------------------------------------


def slot_busy(samples: Sequence[Sample], start: float) -> bool | None:
    """Whether the active account's 5h rose in the slot starting at
    ``start``, from its oldest-first samples: a rise between consecutive
    readings, the later one inside the slot and at most :data:`PAIR_GAP_S`
    after the earlier. None when no such pair exists (not observed). A drop
    (a 5h reset) is no rise."""
    observed = busy = False
    for a, b in zip(samples, samples[1:]):
        if start <= b.ts < start + SLOT_S and b.ts - a.ts <= PAIR_GAP_S:
            observed = True
            busy = busy or b.pct5 > a.pct5
    return busy if observed else None


@dataclass
class Recorder:
    """The engine's writer, holding the parsed history in memory.

    Loaded on first use; ``write=False`` (dry runs) records in memory only.
    """

    root: Path
    history: History = field(default_factory=History)
    loaded: bool = False
    lines_on_disk: int = 0
    must_compact: bool = False
    last_hour: dict[str, int] = field(default_factory=dict)
    last_slot: float | None = None

    def load(self, now: float) -> None:
        lines, truncated = _read_lines(path_for(self.root))
        parsed = [x for x in map(_parse, lines) if x is not None]
        self.history = trimmed(compacted(from_items(parsed)), now)
        self.lines_on_disk = len(lines)
        self.must_compact = truncated or len(parsed) < len(lines)
        for p in self.history.points:
            hour = int(p.ts // HOUR_S)
            self.last_hour[p.number] = max(self.last_hour.get(p.number, hour), hour)
        if self.history.slots:
            self.last_slot = self.history.slots[-1].ts
        self.loaded = True

    def observe(
        self,
        now: float,
        active: str | None,
        readings: Mapping[str, tuple[float, float, float]],
        samples: Sequence[Sample],
        *,
        points: bool = True,
        slots: bool = True,
        write: bool = True,
    ) -> None:
        """Record this tick's news.

        ``readings``: ``{slot: (fetched_at, pct5, pct7)}``, every reading
        the tick has; the first one per account per clock hour becomes a
        point. ``samples``: the active account's oldest-first samples; each
        slot that has settled since the last one judged becomes an
        observation when it was observed at all.
        """
        if not self.loaded:
            self.load(now)
        new: list[UsagePoint | SlotObs] = []
        if points:
            for number in sorted(readings):
                ts, pct5, pct7 = readings[number]
                if ts > now or now - ts > READING_MAX_AGE_S:
                    continue
                hour = int(ts // HOUR_S)
                if self.last_hour.get(number, hour - 1) >= hour:
                    continue
                self.last_hour[number] = hour
                new.append(UsagePoint(ts, number, pct5, pct7, number == active))
        if slots:
            start = math.floor(now / SLOT_S) * SLOT_S - 3 * SLOT_S
            while start + SLOT_S + SLOT_SETTLE_S <= now:
                if self.last_slot is None or start > self.last_slot:
                    busy = slot_busy(samples, start)
                    if busy is not None:
                        new.append(SlotObs(start, busy))
                    self.last_slot = start
                start += SLOT_S
        if not new and not self.must_compact:
            return
        self.history = trimmed(
            from_items([*self.history.points, *self.history.slots, *new]), now
        )
        if not write:
            return
        path = path_for(self.root)
        kept = len(self.history.points) + len(self.history.slots)
        if self.must_compact or self.lines_on_disk + len(new) > 2 * kept + COMPACT_SLACK_LINES:
            Path(self.root).mkdir(parents=True, exist_ok=True)
            _rewrite(path, self.history)
            self.lines_on_disk, self.must_compact = kept, False
        elif new:
            _append(path, [_line(x) for x in new])
            self.lines_on_disk += len(new)


# -- 7d burn rate -----------------------------------------------------------------------------


def burn_rate(points: Sequence[UsagePoint], number: str, now: float) -> float | None:
    """Account ``number``'s 7d pace in pct per hour while it is the active
    account; None with fewer than :data:`RATE_MIN_HOURS` active hours.

    The time-weighted average of the 7d increases between its consecutive
    points that were both active, over the last :data:`RATE_LOOKBACK_S`;
    each interval's weight halves every :data:`RATE_HALF_LIFE_H` of age. A
    drop (the 7d window reset) adds 0; a hole longer than
    :data:`RATE_MAX_GAP_S` is skipped.
    """
    mine = [p for p in points if p.number == number and now - RATE_LOOKBACK_S <= p.ts <= now]
    mine.sort(key=lambda p: p.ts)
    used = hours = weighted_hours = 0.0
    for a, b in zip(mine, mine[1:]):
        gap = b.ts - a.ts
        if not (a.active and b.active) or gap <= 0 or gap > RATE_MAX_GAP_S:
            continue
        weight = 0.5 ** ((now - b.ts) / 3600.0 / RATE_HALF_LIFE_H)
        used += weight * max(b.pct7 - a.pct7, 0.0)
        weighted_hours += weight * gap / 3600.0
        hours += gap / 3600.0
    if hours < RATE_MIN_HOURS or weighted_hours <= 0:
        return None
    return used / weighted_hours


def burn_rates(points: Sequence[UsagePoint], now: float) -> dict[str, float]:
    """:func:`burn_rate` for every account that has one."""
    out: dict[str, float] = {}
    for number in sorted({p.number for p in points}):
        rate = burn_rate(points, number, now)
        if rate is not None:
            out[number] = rate
    return out


# -- idle pattern -----------------------------------------------------------------------------


def slot_key(ts: float, localtime: LocalTime = time.localtime) -> tuple[bool, int]:
    """``(weekend, slot of the day)`` of the local time ``ts`` falls in."""
    lt = localtime(ts)
    return lt.tm_wday >= 5, (lt.tm_hour * 60 + lt.tm_min) // 15


def clock_label(ts: float, localtime: LocalTime = time.localtime) -> str:
    return time.strftime("%H:%M", localtime(ts))


@dataclass(frozen=True)
class Pattern:
    """P(busy) per ``(weekend, slot of day)`` over the last 14 days."""

    days: int
    counts: Mapping[tuple[bool, int], tuple[int, int]]  # key -> (busy, observed)

    def p_busy(self, key: tuple[bool, int]) -> float | None:
        busy, seen = self.counts.get(key, (0, 0))
        return busy / seen if seen else None


def learn(
    slots: Sequence[SlotObs], now: float, localtime: LocalTime = time.localtime
) -> Pattern:
    """The pattern in the observations of the last :data:`LEARN_S`."""
    counts: dict[tuple[bool, int], tuple[int, int]] = {}
    days: set[tuple[int, int]] = set()
    for s in slots:
        if not now - LEARN_S <= s.ts <= now:
            continue
        lt = localtime(s.ts)
        days.add((lt.tm_year, lt.tm_yday))
        key = (lt.tm_wday >= 5, (lt.tm_hour * 60 + lt.tm_min) // 15)
        busy, seen = counts.get(key, (0, 0))
        counts[key] = (busy + int(s.busy), seen + 1)
    return Pattern(days=len(days), counts=counts)


def quiet_windows(
    pattern: Pattern,
    start: float,
    end: float,
    localtime: LocalTime = time.localtime,
) -> list[QuietWindow]:
    """Every quiet window between ``start`` and ``end`` (slot-aligned): a run
    of slots with a known P(busy) under :data:`QUIET_P` lasting at least
    :data:`QUIET_MIN_S`. A slot never observed is not quiet."""
    out: list[QuietWindow] = []
    run_start: float | None = None
    t = math.floor(start / SLOT_S) * SLOT_S
    while True:
        p = pattern.p_busy(slot_key(t, localtime)) if t < end else None
        if p is not None and p < QUIET_P:
            if run_start is None:
                run_start = t
        else:
            if run_start is not None and t - run_start >= QUIET_MIN_S:
                out.append(QuietWindow(
                    start=run_start,
                    end=t,
                    start_label=clock_label(run_start, localtime),
                    end_label=clock_label(t, localtime),
                ))
            run_start = None
            if t >= end:
                return out
        t += SLOT_S


def forecast(
    slots: Sequence[SlotObs], now: float, localtime: LocalTime = time.localtime
) -> Forecast | None:
    """The idle pattern as seen from ``now``; None on a cold start (fewer
    than :data:`MIN_DAYS` days observed). Pure: the TUI (``view.history_inputs``)
    and ``why`` call it on :func:`read`'s slots."""
    pattern = learn(slots, now, localtime)
    if pattern.days < MIN_DAYS:
        return None
    windows = quiet_windows(pattern, now - FORECAST_BACK_S, now + FORECAST_AHEAD_S, localtime)
    return Forecast(
        days=pattern.days,
        p_busy_now=pattern.p_busy(slot_key(now, localtime)),
        current=next((w for w in windows if w.start <= now < w.end), None),
        next=next((w for w in windows if w.start > now), None),
    )


def learned_days(
    slots: Sequence[SlotObs], now: float, localtime: LocalTime = time.localtime
) -> int:
    return learn(slots, now, localtime).days


def describe(
    slots: Sequence[SlotObs],
    now: float,
    *,
    enabled: bool = True,
    localtime: LocalTime = time.localtime,
    sep: str = ", ",
) -> str:
    """One line for doctor and ``cc-swap why``: ``idle pattern: 9 days
    learned, next quiet window 23:00–07:30``. Fleet passes ``sep=" · "``."""
    if not enabled:
        return "idle pattern: off (maximize.learnIdlePattern)"
    f = forecast(slots, now, localtime)
    if f is None:
        days = learned_days(slots, now, localtime)
        return f"idle pattern: learning ({days} of {MIN_DAYS} days observed)"
    head = f"idle pattern: {f.days} days learned"
    if f.current is not None:
        return f"{head}{sep}quiet now until {f.current.end_label}"
    if f.next is not None:
        return f"{head}{sep}next quiet window {f.next.start_label}–{f.next.end_label}"
    return f"{head}{sep}no quiet window in the next {FORECAST_AHEAD_S / 3600:.0f}h"


def summary(
    slots: Sequence[SlotObs],
    now: float,
    *,
    enabled: bool = True,
    localtime: LocalTime = time.localtime,
) -> dict:
    """The pattern as JSON for ``cc-swap why --json``."""
    f = forecast(slots, now, localtime) if enabled else None

    def window(w: QuietWindow | None) -> dict | None:
        if w is None:
            return None
        return {"start": w.start, "end": w.end, "startLabel": w.start_label, "endLabel": w.end_label}

    return {
        "enabled": enabled,
        "days": learned_days(slots, now, localtime) if enabled else 0,
        "learned": f is not None,
        "pBusyNow": None if f is None or f.p_busy_now is None else round(f.p_busy_now, 3),
        "quietNow": window(f.current) if f else None,
        "nextQuiet": window(f.next) if f else None,
        "text": describe(slots, now, enabled=enabled, localtime=localtime),
    }
