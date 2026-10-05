"""The 2026-10-06 incident as a replayable fixture (tests/maximize/test_stale_active.py).

ml-main, 23:44 KST: a ``/login`` put the account (here slot 1) live on ml-main
while ml-a6000 was also using it. The real 5h utilization below is read off
the two engines' logs (ml-a6000 kept reading it every ~3 minutes; ml-main's
reads 429'd from 00:10 to 01:36): 0% at 23:44, 20% at 00:06, 53% at 00:54,
74% at 01:26, 87% at 01:36, 100% at ~01:44. The 7d went 86% → 92%.

Minutes are counted from 23:44 (``T0``). Slot numbers and percentages only.
"""

from __future__ import annotations

from dataclasses import dataclass

from claude_swap.usage_store import UsageEntry

#: (minute since 23:44, real 5h %) — piecewise linear between the points.
REAL_5H: tuple[tuple[float, float], ...] = (
    (0, 0.0), (4, 3.0), (10, 8.0), (15, 11.0), (18, 15.0), (22, 20.0),
    (28, 26.0), (36, 28.0), (70, 53.0), (80, 60.0), (91, 67.0), (102, 74.0),
    (108, 83.0), (112, 87.0), (116, 92.0), (120, 100.0),
)
#: 7d: 86% at 23:44, 88% by 00:06, 92% by 01:44.
REAL_7D: tuple[tuple[float, float], ...] = ((0, 86.0), (22, 88.0), (120, 92.0))
#: ml-main's reads of the active account 429'd from 00:10 (minute 26) and
#: answered again at 01:36 (minute 112).
FAIL_FROM_MIN = 26.0
FAIL_UNTIL_MIN = 112.0
#: ml-main read every ~60 s before the 429s (urgent mode on the 7d band).
POLL_S = 60.0


def _interp(points: tuple[tuple[float, float], ...], minute: float) -> float:
    if minute <= points[0][0]:
        return points[0][1]
    for (m0, p0), (m1, p1) in zip(points, points[1:]):
        if minute <= m1:
            return p0 + (p1 - p0) * (minute - m0) / (m1 - m0)
    return points[-1][1]


def real_5h(minute: float) -> float:
    return min(100.0, _interp(REAL_5H, minute))


def real_7d(minute: float) -> float:
    return _interp(REAL_7D, minute)


def reading(minute: float) -> dict:
    """What the usage endpoint reports at ``minute``: whole percents, floored."""
    return {
        "five_hour": {"pct": float(int(real_5h(minute)))},
        "seven_day": {"pct": float(int(real_7d(minute)))},
    }


@dataclass
class ActiveReads:
    """The active account's store row as ml-main's collector left it:
    fresh while reads succeed, frozen at the last success with a 429
    streak while they fail."""

    t0: float
    fail_from_min: float | None = FAIL_FROM_MIN
    fail_until_min: float | None = FAIL_UNTIL_MIN
    poll_s: float = POLL_S
    last_ok_min: float | None = None
    failures: int = 0
    first_429: float | None = None

    def failing(self, minute: float) -> bool:
        return (
            self.fail_from_min is not None
            and self.fail_until_min is not None
            and self.fail_from_min <= minute < self.fail_until_min
        )

    def entry(self, now: float) -> UsageEntry:
        minute = (now - self.t0) / 60.0
        if not self.failing(minute):
            due = self.last_ok_min is None or (minute - self.last_ok_min) * 60.0 >= self.poll_s - 1
            if due:
                self.last_ok_min = minute
                self.failures = 0
        else:
            self.failures += 1
            if self.first_429 is None:
                self.first_429 = now
        assert self.last_ok_min is not None
        fetched_at = self.t0 + self.last_ok_min * 60.0
        return UsageEntry(
            last_good=reading(self.last_ok_min),
            fetched_at=fetched_at,
            age_s=now - fetched_at,
            consecutive_failures=self.failures,
            last_error="http-429" if self.failures else None,
            last_429_at=now if self.failures else self.first_429,
            backoff_until=now + 300.0 if self.failures else None,
            trust_extended=bool(self.failures),
        )
