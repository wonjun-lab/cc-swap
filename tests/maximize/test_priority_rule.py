"""The priority (iron) rule for maximize (maximize/policy.py docstring).

0. never used: excluded, quarantined, inside the login-expiry guard, at or
   over a hard mark; 1. first: an account whose week would expire unused
   (at risk), the soonest 7d reset first, whatever its tier; 2. the tiers;
   3. the score."""

from __future__ import annotations

from dataclasses import replace

from claude_swap.maximize import drain
from claude_swap.maximize.model import Hold, Switch
from claude_swap.maximize.policy import decide, landing_candidates
from tests.maximize.test_drain import hours
from tests.maximize.test_policy import NOW, acct, snap

H = 3600.0


def cooled(active, *accounts, **settings):
    return snap(active, *accounts, samples="idle", last_switch_min=60,
                active_changed_min=60, **settings)


class TestRule0NeverUsed:
    def test_excluded_at_risk_is_never_landed(self):
        s = cooled("1", acct("1", 96, 40), hours("2", 0, 88, 5, tier="excluded"))
        assert landing_candidates(s) == []

    def test_quarantined_at_risk_is_never_landed(self):
        s = cooled("1", acct("1", 96, 40), replace(hours("2", 0, 88, 5), quarantined=True))
        assert landing_candidates(s) == []

    def test_at_risk_inside_the_login_expiry_guard_is_never_landed(self):
        v = replace(hours("2", 0, 88, 5), login_deadline=NOW + 60)
        s = cooled("1", acct("1", 20, 40), v, acct("3", 0, 0, reset7_d=6))
        assert [x.number for x in landing_candidates(s)] == ["3"]

    def test_at_risk_at_the_5h_hard_mark_is_never_landed(self):
        s = cooled("1", acct("1", 20, 40), hours("2", 95, 88, 5), acct("3", 0, 0, reset7_d=6))
        assert [x.number for x in landing_candidates(s)] == ["3"]
        s = cooled("1", acct("1", 20, 40), hours("2", 0, 98, 5), acct("3", 0, 0, reset7_d=6))
        assert [x.number for x in landing_candidates(s)] == ["3"]


class TestRule1AtRiskFirst:
    def test_last_resort_at_risk_active_stays_past_soft_5h(self):
        # soft5h 50 / hard5h 90: 5h 60 is past soft, still no move.
        s = cooled("1", hours("1", 60, 89, 5, tier="last_resort"),
                   acct("2", 0, 0, reset7_d=6), soft_5h=50, hard_5h=90)
        got = decide(s)
        assert isinstance(got, Hold) and got.code is None and not got.pending
        assert "would expire unused" in got.reason

    def test_the_5h_hard_mark_still_moves_off_it(self):
        s = cooled("1", hours("1", 91, 89, 5, tier="last_resort"),
                   acct("2", 0, 0, reset7_d=6), soft_5h=50, hard_5h=90)
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "hard" and got.target == "2"

    def test_the_5h_ride_arms_at_the_hard_mark_not_the_soft_one(self):
        base = dict(hard_5h=99, soft_5h=50, learned_ride=True, ride_windows="5h")
        s = cooled("1", hours("1", 70, 89, 5), acct("2", 0, 0, reset7_d=6), **base)
        got = decide(s)
        assert isinstance(got, Hold) and got.code is None   # no soft move, no ride yet
        s = cooled("1", hours("1", 99, 89, 5), acct("2", 0, 0, reset7_d=6), **base)
        got = decide(s)
        assert (isinstance(got, Hold) and got.code == "ride") or (
            isinstance(got, Switch) and got.trigger == "hard")

    def test_15_points_of_7d_room_is_enough_to_go_first(self):
        v = hours("2", 60, 95.5, 5, tier="last_resort")   # (98 - 95.5) / 0.165 = 15
        s = cooled("1", acct("1", 96, 40), v, acct("3", 0, 0, reset7_d=6, tier="preferred"))
        assert drain.at_risk(v, s)
        assert [x.number for x in landing_candidates(s)] == ["2", "3"]

    def test_less_than_the_landing_room_is_not_at_risk(self):
        v = hours("2", 0, 97, 5, tier="last_resort")   # 1 / 0.165 = 6 < 10
        s = cooled("1", acct("1", 96, 40), v)
        assert not drain.at_risk(v, s) and landing_candidates(s) == []

    def test_two_at_risk_accounts_the_sooner_reset_first(self):
        s = cooled("1", acct("1", 96, 40), hours("2", 0, 80, 20), hours("3", 0, 90, 4),
                   hours("4", 0, 85, 10, tier="last_resort"))
        assert [x.number for x in landing_candidates(s)] == ["3", "4", "2"]

    def test_at_idle_the_move_goes_from_the_later_to_the_sooner_reset(self):
        s = cooled("1", hours("1", 10, 80, 20), hours("2", 0, 85, 4, tier="last_resort"))
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "2"
        assert "would expire unused" in got.reason
        back = cooled("2", hours("2", 0, 85, 4), hours("1", 10, 20, 20))
        assert isinstance(decide(back), Hold)

    def test_no_preempt_or_tier_move_off_an_at_risk_active(self):
        s = cooled("1", hours("1", 30, 84, 20, tier="last_resort"),
                   acct("2", 0, 0, reset7_d=6, tier="preferred"))
        got = decide(s)
        assert isinstance(got, Hold) and "can land" not in got.reason

    def test_non_at_risk_preferred_active_moves_to_a_landable_at_risk_normal(self):
        s = cooled("1", acct("1", 10, 20, reset7_d=6, tier="preferred"), hours("2", 60, 88, 5))
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "2"
        cool = snap("1", acct("1", 10, 20, reset7_d=6, tier="preferred"), hours("2", 60, 88, 5),
                    samples="idle", last_switch_min=10)
        held = decide(cool)
        assert isinstance(held, Hold) and "cooldown" in held.reason

    def test_the_reason_says_the_week_would_expire(self):
        v = hours("betlab.master", 0, 89, 5)
        s = cooled("1", acct("1", 96, 40), replace(v, number="2"))
        assert "7d 89% resets in 5h — would expire unused, using it first" in decide(s).reason


class TestRules2And3:
    def test_tiers_then_score_among_non_at_risk(self):
        s = cooled("1", acct("1", 96, 40), acct("2", 0, 0, reset7_d=6),
                   acct("3", 0, 50, reset7_d=6, tier="preferred"),
                   acct("4", 0, 0, reset7_d=6, tier="last_resort"))
        assert [x.number for x in landing_candidates(s)] == ["3", "2", "4"]
