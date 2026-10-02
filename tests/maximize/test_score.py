"""Score, landing and ranking (spec §5.2-§5.4)."""

from __future__ import annotations

import math

import pytest

from claude_swap.maximize.model import AccountView
from claude_swap.maximize.score import (
    below_hard,
    days_left,
    landable,
    rank,
    score,
)
from claude_swap.settings import MaximizeSettings

NOW = 1_000_000.0
H = 3600.0
D = 86400.0
S = MaximizeSettings()


def view(
    number: str,
    *,
    pct5: float | None = 0.0,
    pct7: float | None = 0.0,
    reset5: float | None = None,
    reset7: float | None = NOW + 3 * D,
    tier: str = "normal",
    weight: int = 1,
    quarantined: bool = False,
    api_key: bool = False,
) -> AccountView:
    return AccountView(
        number=number,
        email=f"{number}@example.com",
        tier=tier,
        plan_weight=weight,
        pct5=pct5,
        reset5=reset5,
        pct7=pct7,
        reset7=reset7,
        quarantined=quarantined,
        api_key=api_key,
    )


def numbers(views) -> list[str]:
    return [v.number for v in views]


class TestScore:
    def test_user_example_same_reset_more_left_wins(self):
        # Same 7d reset: account 1 has 90% left, account 2 has 50% left.
        a1 = view("1", pct7=10.0, reset7=NOW + 4 * D)
        a2 = view("2", pct7=50.0, reset7=NOW + 4 * D)
        assert score(a1, NOW) > score(a2, NOW)
        assert numbers(rank([a2, a1], NOW, S.tie_epsilon)) == ["1", "2"]

    def test_user_example_30pct_in_12h_beats_90pct_in_6d(self):
        soon = view("1", pct7=70.0, reset7=NOW + 12 * H)   # 30% left, 12h
        late = view("2", pct7=10.0, reset7=NOW + 6 * D)    # 90% left, 6 days
        assert score(soon, NOW) == pytest.approx(4.2)
        assert score(late, NOW) == pytest.approx(1.05)
        assert numbers(rank([late, soon], NOW, S.tie_epsilon)) == ["1", "2"]

    def test_days_left_floor_is_one_hour(self):
        in_one_min = view("1", pct7=99.0, reset7=NOW + 60)
        in_one_hour = view("2", pct7=99.0, reset7=NOW + H)
        already_past = view("3", pct7=99.0, reset7=NOW - 600)
        assert days_left(in_one_min, NOW) == pytest.approx(1 / 24)
        assert score(in_one_min, NOW) == pytest.approx(score(in_one_hour, NOW))
        assert score(already_past, NOW) == pytest.approx(score(in_one_hour, NOW))
        assert score(in_one_hour, NOW) == pytest.approx(1.0 / (100 / 7 / 24))

    def test_unknown_reset_counts_as_seven_days(self):
        v = view("1", pct7=30.0, reset7=None)
        assert days_left(v, NOW) == 7.0
        assert score(v, NOW) == pytest.approx(0.7)

    def test_unknown_7d_scores_minus_infinity(self):
        assert score(view("1", pct7=None), NOW) == -math.inf


class TestLandable:
    @pytest.mark.parametrize(
        ("pct5", "pct7", "expected"),
        [
            (44.9, 84.9, True),     # soft 50/90 minus margin 5
            (45.0, 10.0, False),
            (10.0, 85.0, False),
            (None, 10.0, False),
            (10.0, None, False),
            (0.0, 0.0, True),       # an off 5h window is 0% and lands
        ],
    )
    def test_margins_and_unknowns(self, pct5, pct7, expected):
        assert landable(view("2", pct5=pct5, pct7=pct7), S) is expected

    @pytest.mark.parametrize(
        "kw",
        [{"tier": "excluded"}, {"quarantined": True}, {"api_key": True}],
    )
    def test_ineligible_accounts_never_land(self, kw):
        assert landable(view("2", **kw), S) is False

    def test_last_resort_is_landable(self):
        assert landable(view("2", tier="last_resort"), S) is True

    def test_below_hard(self):
        assert below_hard(view("2", pct5=94.9, pct7=97.9), S) is True
        assert below_hard(view("2", pct5=95.0, pct7=0.0), S) is False
        assert below_hard(view("2", pct5=0.0, pct7=98.0), S) is False
        assert below_hard(view("2", pct5=None, pct7=0.0), S) is False


class TestRank:
    def test_normal_before_last_resort_whatever_the_score(self):
        lr = view("1", pct7=0.0, reset7=NOW + 12 * H, tier="last_resort")
        normal = view("2", pct7=80.0, reset7=NOW + 6 * D)
        assert numbers(rank([lr, normal], NOW, S.tie_epsilon)) == ["2", "1"]

    def test_tie_within_eps_prefers_20x(self):
        five = view("2", pct7=0.0, reset7=NOW + 7 * D)              # 1.00
        twenty = view("3", pct7=5.0, reset7=NOW + 7 * D, weight=4)  # 0.95
        assert numbers(rank([five, twenty], NOW, 0.1)) == ["3", "2"]

    def test_gap_beyond_eps_is_not_a_tie(self):
        five = view("2", pct7=0.0, reset7=NOW + 7 * D)               # 1.00
        twenty = view("3", pct7=15.0, reset7=NOW + 7 * D, weight=4)  # 0.85
        assert numbers(rank([twenty, five], NOW, 0.1)) == ["2", "3"]

    def test_tie_then_sooner_running_5h_reset_off_windows_last(self):
        off = view("2", reset5=None)
        later = view("3", reset5=NOW + 4 * H)
        sooner = view("4", reset5=NOW + 1 * H)
        rolled = view("5", reset5=NOW - 60)   # reset passed: window is off
        ranked = rank([off, later, sooner, rolled], NOW, 0.1)
        assert numbers(ranked) == ["4", "3", "2", "5"]

    def test_tie_then_lower_slot(self):
        assert numbers(rank([view("10"), view("3"), view("7")], NOW, 0.1)) == [
            "3", "7", "10",
        ]

    def test_tie_groups_do_not_chain(self):
        # 1.00 / 0.95 / 0.88 with eps 0.1: 0.88 is within eps of 0.95 but not
        # of the group's anchor 1.00, so the 20x on 0.88 cannot jump ahead.
        a = view("2", pct7=0.0, reset7=NOW + 7 * D)
        b = view("3", pct7=5.0, reset7=NOW + 7 * D)
        c = view("4", pct7=12.0, reset7=NOW + 7 * D, weight=4)
        assert numbers(rank([c, b, a], NOW, 0.1)) == ["2", "3", "4"]

    def test_unknown_scores_rank_last_in_slot_order(self):
        known = view("5", pct7=90.0)
        u1 = view("3", pct7=None)
        u2 = view("2", pct7=None)
        assert numbers(rank([u1, known, u2], NOW, 0.1)) == ["5", "2", "3"]
