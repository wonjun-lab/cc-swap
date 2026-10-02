"""Idle detection and hard-cap ETA (spec §5.6)."""

from __future__ import annotations

import pytest

from claude_swap.maximize.idle import (
    eta_to_hard_min,
    idle_evidence,
    is_idle,
    trim_samples,
)
from claude_swap.maximize.model import Sample
from claude_swap.settings import MaximizeSettings

NOW = 1_000_000.0
S = MaximizeSettings()   # idle window 10 min, 5h delta 1 pt, hard 95/98


def samples(*rows: tuple[float, float, float]) -> tuple[Sample, ...]:
    """Rows of (seconds before NOW, pct5, pct7)."""
    return tuple(Sample(NOW - ago, p5, p7) for ago, p5, p7 in rows)


class TestTrim:
    def test_keeps_last_30_minutes_oldest_first(self):
        got = trim_samples(samples((60, 3, 1), (1900, 1, 1), (900, 2, 1)), NOW)
        assert [round(NOW - x.ts) for x in got] == [900, 60]

    def test_custom_keep(self):
        got = trim_samples(samples((60, 1, 1), (400, 1, 1)), NOW, keep_s=300)
        assert len(got) == 1

    def test_one_sample_per_timestamp(self):
        got = trim_samples(samples((60, 5, 1), (60, 5, 1), (0, 6, 1)), NOW)
        assert [round(NOW - x.ts) for x in got] == [60, 0]


class TestIdle:
    @pytest.mark.parametrize(
        "rows",
        [
            (),
            ((0, 60, 40),),
            ((300, 60, 40), (0, 60, 40)),          # span 5 min < 10 min
        ],
    )
    def test_insufficient_samples_is_not_idle(self, rows):
        assert idle_evidence(samples(*rows), NOW, S) is None
        assert is_idle(samples(*rows), NOW, S) is False

    def test_integer_plateau_over_ten_minutes_is_idle(self):
        rows = samples((600, 62, 40), (300, 62, 40), (0, 63, 40))
        assert is_idle(rows, NOW, S) is True

    def test_two_points_in_ten_minutes_is_not_idle(self):
        rows = samples((600, 62, 40), (300, 63, 40), (0, 64, 40))
        assert is_idle(rows, NOW, S) is False

    def test_moving_7d_blocks_idle(self):
        rows = samples((600, 62, 40), (0, 62, 42))
        assert is_idle(rows, NOW, S) is False

    def test_judged_on_the_latest_qualifying_pair(self):
        # Busy 20-10 min ago, flat since: the pair is (10 min ago, now).
        rows = samples((1200, 50, 40), (600, 60, 40), (0, 60, 40))
        older, newest = idle_evidence(rows, NOW, S)
        assert NOW - older.ts == 600 and newest.ts == NOW
        assert is_idle(rows, NOW, S) is True

    def test_duplicate_readings_do_not_make_idle(self):
        # One cached reading recorded three times is one observation, not
        # ten minutes of plateau.
        rows = samples((0, 62, 40), (0, 62, 40), (0, 62, 40))
        assert idle_evidence(rows, NOW, S) is None
        assert is_idle(rows, NOW, S) is False

    def test_gap_longer_than_window_is_not_idle(self):
        # Laptop asleep for 25 minutes: one pre-sleep and one fresh reading.
        assert is_idle(samples((1500, 60, 40), (0, 60, 40)), NOW, S) is False
        # The same span observed every 5 minutes is a real plateau.
        covered = samples(*((ago, 60, 40) for ago in range(1500, -1, -300)))
        assert is_idle(covered, NOW, S) is True

    def test_stale_evidence_is_not_idle(self):
        # Flat, but the newest reading is 11 minutes old.
        rows = samples((1500, 60, 40), (660, 60, 40))
        assert is_idle(rows, NOW, S) is False

    def test_delta_setting_is_honoured(self):
        rows = samples((600, 60, 40), (0, 63, 40))
        loose = MaximizeSettings(idle_max_delta_pct=3.0)
        assert is_idle(rows, NOW, S) is False
        assert is_idle(rows, NOW, loose) is True

    def test_window_reset_drop_counts_as_idle(self):
        rows = samples((600, 80, 40), (0, 0, 40))
        assert is_idle(rows, NOW, S) is True


class TestEta:
    def test_five_hour_eta(self):
        rows = samples((600, 60, 10), (0, 80, 10))   # 2 pts/min, 15 to go
        assert eta_to_hard_min(rows, S) == pytest.approx(7.5)

    def test_smaller_of_both_windows(self):
        rows = samples((600, 10, 90), (0, 20, 95))   # 5h 1/min (75 min); 7d 0.5/min (6 min)
        assert eta_to_hard_min(rows, S) == pytest.approx(6.0)

    def test_uses_earliest_sample_inside_window(self):
        rows = samples((1200, 0, 10), (600, 60, 10), (300, 70, 10), (0, 80, 10))
        assert eta_to_hard_min(rows, S) == pytest.approx(7.5)

    def test_sparse_polling_uses_nearest_older_sample(self):
        rows = samples((1200, 50, 10), (0, 80, 10))   # 1.5/min over 20 min
        assert eta_to_hard_min(rows, S) == pytest.approx(10.0)

    def test_already_over_the_cap_is_zero(self):
        rows = samples((600, 90, 10), (0, 96, 10))
        assert eta_to_hard_min(rows, S) == 0.0

    @pytest.mark.parametrize(
        "rows",
        [
            ((600, 60, 40), (0, 60, 40)),     # flat
            ((600, 80, 40), (0, 10, 40)),     # 5h rolled over
            ((0, 60, 40),),                   # one sample
            ((60, 60, 40), (0, 61, 40)),      # span under 2 minutes
        ],
    )
    def test_no_eta(self, rows):
        assert eta_to_hard_min(samples(*rows), S) is None
