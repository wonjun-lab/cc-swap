"""The near-reset 7d drain (maximize/drain.py, maximize/policy.py).

An account near its 7d reset has its 7d soft mark set aside and is used
first, so its weekly quota does not expire unused. Pure tests first (the
predicate, k learning, the policy), then the surfaces (Fleet, why, doctor)
and the engine end to end."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from claude_swap.maximize import drain, policy
from claude_swap.maximize.history import History, UsagePoint
from claude_swap.maximize.model import AccountView, Hold, Snapshot, Switch
from claude_swap.maximize.policy import decide, landing_candidates
from claude_swap.settings import MaximizeSettings
from tests.maximize import test_policy as tp
from tests.maximize.test_policy import NOW, acct, pattern, quiet, snap

H = 3600.0
D = 86400.0


def hours(number, pct5, pct7, reset_h, **kw) -> AccountView:
    """``acct`` with its 7d resetting ``reset_h`` hours from NOW."""
    return replace(acct(number, pct5, pct7, **kw), reset7=NOW + reset_h * H)


def with_plan(v: AccountView, plan: str | None) -> AccountView:
    return replace(v, plan=plan)


def one(v: AccountView, **settings) -> Snapshot:
    return snap(None, v, **settings)


# -- the predicate ----------------------------------------------------------------------


class TestPredicate:
    def test_the_hours_rule(self):
        assert drain.rule(hours("1", 0, 86, 18), one(hours("1", 0, 86, 18))) == "hours"
        at = hours("1", 0, 86, 24)
        assert drain.rule(at, one(at)) == "hours"   # at most drainHours
        later = hours("1", 0, 88, 30)
        assert drain.rule(later, one(later)) is None

    def test_drain_hours_sets_the_hours_rule(self):
        v = hours("1", 0, 88, 30)
        assert drain.rule(v, one(v, drain_hours=36)) == "hours"
        assert drain.rule(v, one(v, drain_hours=12)) is None

    def test_drain_hours_zero_is_off(self):
        for v in (hours("1", 0, 86, 2), hours("1", 0, 0, 30)):
            assert drain.rule(v, one(v)) is not None
            assert drain.rule(v, one(v, drain_hours=0)) is None

    def test_an_unknown_reset_never_drains(self):
        v = acct("1", 0, 88, reset7_d=None)
        assert drain.rule(v, one(v)) is None
        past = hours("1", 0, 88, -1)
        assert drain.rule(past, one(past)) is None

    def test_unknown_usage_or_nothing_under_the_hard_cap_never_drains(self):
        unknown = hours("1", None, None, 2)
        assert drain.rule(unknown, one(unknown)) is None
        spent = hours("1", 0, 98, 2)   # at hard7d (98): nothing left to drain
        assert drain.rule(spent, one(spent)) is None
        assert drain.rule(hours("1", 0, 97, 2), one(hours("1", 0, 97, 2))) == "hours"

    def test_the_headroom_rule(self):
        # 30 h left: 6 windows. A 20x (k 0.165) drains 6 x 16.5 = 99 pts in
        # them; the rule fires once the room needs 80% of that (79.2 pts).
        assert drain.HEADROOM_SHARE == 0.8
        lots = with_plan(hours("1", 0, 10, 30), "20x")     # 88 pts left
        assert drain.rule(lots, one(lots)) == "headroom"
        some = with_plan(hours("1", 0, 30, 30), "20x")     # 68 pts left
        assert drain.rule(some, one(some)) is None
        # Off with the hours rule: drainHours 0 turns the drain off entirely.
        assert drain.rule(lots, one(lots, drain_hours=0)) is None

    def test_the_headroom_rule_reads_the_plan_and_the_learned_k(self):
        v = hours("1", 0, 40, 30)                          # 58 pts left
        assert drain.rule(with_plan(v, "20x"), one(with_plan(v, "20x"))) is None
        five = with_plan(v, "5x")                          # 6 x 10.5 x 0.8 = 50.4
        assert drain.rule(five, one(five)) == "headroom"
        learned = replace(one(with_plan(v, "20x")), k7={"1": 0.10})   # 48
        assert drain.rule(with_plan(v, "20x"), learned) == "headroom"

    def test_the_headroom_rule_only_within_72_hours(self):
        # A low k (0.05) would make an empty account "draining" days ahead.
        assert drain.HEADROOM_MAX_H == 72
        for left, expected in ((70, "headroom"), (72, "headroom"), (80, None), (150, None)):
            v = hours("1", 0, 0, left)
            s = replace(one(v), k7={"1": 0.05})
            assert drain.rule(v, s) == expected, left
        # The hours rule is not capped by it.
        v = hours("1", 0, 88, 100)
        assert drain.rule(v, one(v, drain_hours=120)) == "hours"

    def test_the_soft_band_is_left_to_the_hours_rule_by_default(self):
        # 85-98% leaves at most 13 pts: the headroom rule fires only within
        # ~5 h (20x) or ~8 h (5x) of the reset, inside the hours rule.
        for plan, edge_h in (("20x", 13 / (0.8 * 16.5) * 5), ("5x", 13 / (0.8 * 10.5) * 5)):
            v = with_plan(hours("1", 0, 85, edge_h - 0.01), plan)
            assert drain.rule(v, one(v, drain_hours=1)) == "headroom", plan
            w = with_plan(hours("1", 0, 85, edge_h + 0.01), plan)
            assert drain.rule(w, one(w, drain_hours=1)) is None, plan

    def test_words(self):
        v = hours("1", 0, 86, 18)
        assert drain.text(v, one(v)) == "#1 7d 86% resets in 18h — draining it first"
        assert drain.tag(18.2) == "drain 18h"
        assert drain.left_text(0.4) == "24m" and drain.left_text(72) == "3d"


# -- learning k -------------------------------------------------------------------------


def window(number: str, start: float, p5: tuple[float, ...], p7: tuple[float, ...]):
    """Hourly points of one 5h window."""
    return [UsagePoint(start + i * H, number, a, b, True) for i, (a, b) in enumerate(zip(p5, p7))]


class TestLearnK:
    def test_median_of_single_windows(self):
        points = [
            *window("1", 0, (0, 30, 60, 90), (50, 55, 60, 65)),        # 15/90 = 0.1667
            *window("1", 6 * H, (5, 45, 85), (65, 71, 78)),           # 13/80 = 0.1625
            *window("1", 12 * H, (0, 50, 100), (78, 85, 95)),         # 17/100 = 0.17
        ]
        assert drain.window_ratios(points, "1") == pytest.approx([15 / 90, 13 / 80, 0.17])
        assert drain.learn_k(points) == {"1": pytest.approx(15 / 90, abs=1e-4)}

    def test_short_windows_are_rounding_noise_and_skipped(self):
        # 5h +20 with 7d +5 (floored readings: really 0.2 or so) would say
        # k 0.25; under K_MIN_D5 it is not counted.
        points = [
            *window("1", 0, (0, 20), (10, 15)),
            *window("1", 6 * H, (0, 50, 90), (15, 23, 30)),
            *window("1", 12 * H, (0, 45, 80), (30, 37, 43)),
            *window("1", 18 * H, (0, 50, 100), (43, 51, 60)),
        ]
        assert len(drain.window_ratios(points, "1")) == 3
        assert drain.learn_k(points)["1"] == pytest.approx(15 / 90, abs=1e-4)

    def test_windows_whose_7d_did_not_move_say_nothing(self):
        # A reading stuck on its 7d (or a whole 5h window under one 7d
        # point) would say k 0: such windows used to drag k to its floor,
        # and the headroom rule then fired most of every week.
        stuck = [p for i in range(4) for p in window("1", i * 6 * H, (0, 50, 100), (40, 40, 40))]
        assert drain.window_ratios(stuck, "1") == []
        assert drain.learn_k(stuck) == {}
        mixed = stuck + [*window("1", 30 * H, (0, 50, 100), (40, 48, 56)),
                         *window("1", 36 * H, (0, 50, 100), (56, 64, 73)),
                         *window("1", 42 * H, (0, 50, 100), (73, 80, 90))]
        assert drain.learn_k(mixed) == {"1": 0.17}

    def test_k_has_a_floor(self):
        low = [p for i in range(3)
               for p in window("1", i * 6 * H, (0, 50, 100), (10 + i, 10 + i, 11 + i))]
        assert drain.window_ratios(low, "1") == pytest.approx([0.01] * 3)
        assert drain.K_MIN == 0.05 and drain.learn_k(low) == {"1": 0.05}

    def test_a_window_ends_at_a_5h_or_7d_drop_or_after_5_hours(self):
        # One stretch of hourly points: a 5h reset (90 -> 10) splits it, a
        # 7d reset (95 -> 2) splits it, and so does a run longer than 5 h.
        drop5 = [UsagePoint(i * H, "1", p5, p7, True) for i, (p5, p7) in enumerate(
            [(0, 10), (40, 16), (90, 25), (10, 26), (60, 34)])]
        assert drain.window_ratios(drop5, "1") == pytest.approx([15 / 90, 8 / 50])
        drop7 = [UsagePoint(i * H, "1", p5, p7, True) for i, (p5, p7) in enumerate(
            [(0, 80), (50, 88), (60, 2), (99, 8)])]
        # 50 -> 60 across the 7d reset is not one window; 60 -> 99 is too short.
        assert drain.window_ratios(drop7, "1") == pytest.approx([8 / 50])
        long = [UsagePoint(i * H, "1", 10 * i, 50 + 1.5 * i, True) for i in range(8)]
        ratios = drain.window_ratios(long, "1")
        assert ratios and all(r == pytest.approx(0.15) for r in ratios)
        assert len(ratios) == 1   # 0..5 h, then 6..7 h (+10: too short)

    def test_too_few_windows_fall_back_to_the_plan(self):
        assert drain.K_MIN_WINDOWS == 3
        points = [*window("1", 0, (0, 50, 100), (10, 18, 27)),
                  *window("1", 6 * H, (0, 50, 100), (27, 35, 44))]
        assert drain.learn_k(points) == {}
        v = acct("1")
        assert drain.k_for(with_plan(v, "20x"), {}) == (0.165, False)
        assert drain.k_for(with_plan(v, "5x"), {}) == (0.105, False)
        assert drain.k_for(with_plan(v, None), {}) == (0.165, False)
        assert drain.k_for(with_plan(v, "5x"), {"1": 0.11}) == (0.11, True)

    def test_a_misread_k_stays_in_range_and_accounts_are_apart(self):
        points = [
            *window("1", 0, (0, 50), (0, 40)), *window("1", 6 * H, (0, 50), (0, 45)),
            *window("1", 12 * H, (0, 50), (0, 30)),
            *window("2", 0, (0, 50, 100), (50, 55, 60)), *window("2", 6 * H, (0, 50), (60, 65)),
            *window("2", 12 * H, (0, 50), (65, 70)),
        ]
        assert drain.learn_k(points) == {"1": drain.K_MAX, "2": 0.1}

    def test_future_points_are_ignored(self):
        points = [*window("1", 0, (0, 50), (0, 8)), *window("1", 6 * H, (0, 50), (8, 16)),
                  *window("1", 12 * H, (0, 50), (16, 24))]
        assert drain.learn_k(points, now=8 * H) == {}
        assert drain.learn_k(points, now=18 * H) == {"1": 0.16}


# -- the policy -------------------------------------------------------------------------


class TestLanding:
    def test_lands_on_a_draining_88_percent_account(self):
        s = snap("1", acct("1", 96, 40), hours("2", 0, 88, 18), acct("3", 0, 30, reset7_d=5))
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "hard" and got.target == "2"
        assert got.reason.endswith("-> #2 (normal, score 1.12): #2 7d 88% resets in 18h "
                                   "— draining it first")
        off = decide(replace(s, settings=MaximizeSettings(drain_hours=0)))
        assert isinstance(off, Switch) and off.target == "3"

    def test_landable_under_hard_minus_margin_only(self):
        # With room (5x plans: k 0.105), under hard7d - margin (93) only.
        s = snap("1", acct("1", 96, 40), with_plan(hours("2", 0, 93, 18), "5x"),
                 with_plan(hours("3", 0, 90, 18), "5x"))
        assert [v.number for v in landing_candidates(s)] == ["3"]
        # The 5h rule is unchanged: 45 = soft5h 50 - margin 5.
        s = snap("1", acct("1", 96, 40), hours("2", 45, 88, 18))
        assert landing_candidates(s) == []

    def test_draining_first_earliest_reset_first_then_score(self):
        s = snap(
            "1", acct("1", 96, 40),
            acct("2", 0, 0, reset7_d=6),          # best score, not draining
            hours("3", 0, 87, 20),
            hours("4", 0, 88, 10),
            hours("5", 0, 60, 30, tier="last_resort"),
            hours("6", 0, 80, 12, tier="last_resort"),
            acct("7", 0, 50, reset7_d=6),
        )
        assert [v.number for v in landing_candidates(s)] == ["4", "3", "2", "7", "6", "5"]

    def test_past_the_normal_7d_limit_only_with_room(self):
        # 7d 90% is past soft7d - margin (85). A 5x plan (k 0.105) has
        # 3 / 0.105 = 28.6 5h points of 7d room under hard7d - margin: with
        # a fresh 5h it has room and lands; at 5h 30 (15 pts) it has not,
        # and the normal 7d rule keeps it off like any other account.
        def landable(p5):
            v = with_plan(hours("2", p5, 90, 18), "5x")
            s = snap("1", acct("1", 96, 40), v)
            assert drain.draining(v, s)
            return [x.number for x in landing_candidates(s)] == ["2"]

        assert landable(0)
        assert not landable(30)
        # Under the normal limit a draining account without room still lands.
        s = snap("1", acct("1", 96, 40), hours("2", 30, 80, 18))
        assert not drain.preferred(s.accounts[1], s)
        assert [v.number for v in landing_candidates(s)] == ["2"]

    def test_a_draining_account_with_little_5h_room_competes_on_its_score(self):
        # #2 drains (7d 88%, 20 h) but its 5h is at 40: 5 pts to soft5h -
        # margin, a few minutes before the 5h soft mark moves you on. Without
        # room the normal 7d rule (85) keeps it off; with #2's 5h fresh it
        # goes first.
        def landing(p5):
            s = snap("1", acct("1", 96, 40), hours("2", p5, 88, 20), acct("3", 0, 40, reset7_d=2))
            return [v.number for v in landing_candidates(s)]

        assert drain.MIN_ROOM_5H == 25
        assert landing(40) == ["3"]
        assert landing(20) == ["2", "3"]   # 25 pts of 5h room: the floor
        assert landing(21) == ["3"]
        # Under soft7d - margin it lands as any account, on its score.
        s = snap("1", acct("1", 96, 40), hours("2", 40, 80, 20), acct("3", 0, 40, reset7_d=2))
        assert [v.number for v in landing_candidates(s)] == ["3", "2"]

    def test_a_draining_account_with_little_7d_room_competes_on_its_score(self):
        # 7d 92% leaves 1 pt to hard7d - margin: ~6 5h points at k 0.165.
        def landing(p7, k7=None):
            s = snap("1", acct("1", 96, 40), hours("2", 0, p7, 20), acct("3", 0, 40, reset7_d=2))
            if k7 is not None:
                s = replace(s, k7=k7)
            return [v.number for v in landing_candidates(s)]

        assert landing(92) == ["3"]           # no room: the normal 7d rule
        assert landing(87) == ["2", "3"]      # 6 pts / 0.165 = 36 5h points
        assert landing(88.9) == ["3"]         # 4.1 / 0.165 = 24.8: under the floor
        assert landing(88.9, {"2": 0.1}) == ["2", "3"]   # 41 at its learned k

    def test_the_guards_still_apply(self):
        guarded = replace(hours("2", 0, 88, 18), login_deadline=NOW + 60)
        for v in (guarded, replace(hours("2", 0, 88, 18), quarantined=True),
                  hours("2", 0, 88, 18, tier="excluded"), hours("2", 0, 88, 18, api_key=True)):
            assert landing_candidates(snap("1", acct("1", 20, 40), v)) == [], v


class TestActiveDraining:
    def test_no_soft_7d_switch_away(self):
        s = snap("1", hours("1", 20, 92, 18), acct("2", 0, 10, reset7_d=6), samples="idle",
                 last_switch_min=60)
        got = decide(s)
        assert isinstance(got, Hold) and not got.pending and got.code is None
        assert got.reason == (
            "#1 7d 92% resets in 18h — draining it first (7d soft 90% set aside until "
            "the reset; hard 98% still switches)"
        )
        off = decide(replace(s, settings=MaximizeSettings(drain_hours=0)))
        assert isinstance(off, Switch) and off.trigger == "soft" and off.target == "2"

    def test_no_pending_while_busy(self):
        s = snap("1", hours("1", 20, 92, 18), acct("2", 0, 10, reset7_d=6), samples="busy")
        got = decide(s)
        assert isinstance(got, Hold) and not got.pending

    def test_the_hard_cap_still_switches(self):
        for pct7 in (98, 99):
            s = snap("1", hours("1", 20, pct7, 18), acct("2", 0, 10, reset7_d=6))
            got = decide(s)
            assert isinstance(got, Switch) and got.trigger == "hard" and got.target == "2"
        at_limit = decide(snap("1", hours("1", 20, 100, 18), acct("2", 0, 10, reset7_d=6)))
        assert isinstance(at_limit, Switch) and at_limit.trigger == "at-limit"

    def test_the_eta_forced_hard_switch_still_fires(self):
        # 7d +4 in 10 min: 92 -> 98 in ~15 min ... forceEtaMin 20 forces it.
        samples = tp.rows((600, 20, 88), (300, 20, 90), (0, 20, 92))
        s = snap("1", hours("1", 20, 92, 18), acct("2", 0, 10, reset7_d=6),
                 samples=samples, force_eta_min=20)
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "hard"

    def test_the_5h_soft_mark_still_moves_you(self):
        s = snap("1", hours("1", 60, 92, 18), acct("2", 0, 10, reset7_d=6), samples="idle")
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "soft" and got.target == "2"

    def test_no_preempt_off_a_draining_account(self):
        # 84% climbing 2 pts/h passes 90 in 3 h, before the quiet window.
        def make(**settings):
            s = snap("1", hours("1", 30, 84, 20), acct("2", 10, 10, reset7_d=6),
                     samples="idle", **settings)
            return tp.with_history(s, forecast=pattern(next=quiet(5, 13)), _1=2.0)

        off = decide(make(drain_hours=0))
        assert isinstance(off, Switch) and off.trigger == "preempt"
        got = decide(make())
        assert isinstance(got, Hold) and "draining it first" in got.reason

    def test_preempt_onto_a_draining_target_checks_its_hard_mark(self):
        # Drain-on counterpart of test_policy's crossing-candidate case: #2
        # drains (6 h to its reset) and its soft mark is set aside, but at
        # the active's 2 pts/h it would reach hard7d 98 in 4 h, inside the
        # 5 h horizon and before its reset. Not a target; #3 is.
        crossing = acct("2", 10, 90, reset7_d=0.25)
        s = tp.preempt_snap(candidate=crossing)
        assert drain.draining(crossing, s)
        got = decide(s)
        assert not (isinstance(got, Switch) and got.trigger == "preempt")
        both = replace(s, accounts=(*s.accounts, acct("3", 10, 60, reset7_d=6)))
        got = decide(both)
        assert isinstance(got, Switch) and got.trigger == "preempt" and got.target == "3"
        # 86%: 6 h to 98, past the horizon: a fine target.
        slow = tp.preempt_snap(candidate=acct("2", 10, 86, reset7_d=0.25))
        got = decide(slow)
        assert isinstance(got, Switch) and got.trigger == "preempt" and got.target == "2"

    def test_an_account_hold_words_it_as_a_hold(self):
        s = replace(snap("1", hours("1", 20, 92, 18), acct("2", 0, 10, reset7_d=6),
                         samples="idle"), hold_until=NOW + H)
        got = decide(s)
        assert isinstance(got, Hold) and got.code == "hold"
        assert "otherwise: #1 7d 92% resets in 18h — draining it first" in got.reason


class TestRebalance:
    def cooled(self, *accounts, **settings):
        return snap(accounts[0].number, *accounts, samples="idle",
                    last_switch_min=60, active_changed_min=60, **settings)

    def test_never_off_a_draining_account_to_a_better_scored_one(self):
        s = self.cooled(hours("1", 10, 92, 18), acct("2", 0, 0, reset7_d=1.5))
        assert policy.score(s.accounts[1], NOW) > policy.score(s.accounts[0], NOW) + 1
        got = decide(s)
        assert isinstance(got, Hold) and "draining it first" in got.reason
        off = decide(replace(s, settings=MaximizeSettings(drain_hours=0)))
        assert isinstance(off, Switch)   # without the drain: soft 7d (92 >= 90)

    def test_on_to_a_draining_account_that_resets_sooner(self):
        s = self.cooled(hours("1", 10, 80, 20), hours("2", 0, 85, 4))
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "2"
        assert got.reason.endswith("#2 7d 85% resets in 4h — draining it first; idle")
        # The other way round: #2 resets later, whatever its score.
        back = self.cooled(hours("2", 0, 85, 4), hours("1", 10, 20, 20))
        assert isinstance(decide(back), Hold)

    def test_a_draining_candidate_is_preferred_to_a_better_scored_one(self):
        s = self.cooled(acct("1", 10, 60, reset7_d=3), hours("2", 0, 80, 20),
                        acct("3", 0, 0, reset7_d=2))
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "2"
        # No gain on the draining one: the best other one, as without it.
        s = self.cooled(acct("1", 10, 30, reset7_d=3), hours("2", 0, 92, 20),
                        acct("3", 0, 0, reset7_d=2))
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "3"

    def test_rebalance_onto_a_draining_account_only_with_room(self):
        # #2 (5h 30: 15 pts of room) drains and scores best; #3 drains with
        # room. Rebalance prefers #3; #2 is weighed on its score only.
        s = self.cooled(acct("1", 10, 60, reset7_d=3), hours("2", 30, 80, 20),
                        hours("3", 0, 85, 22))
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "3"
        s = self.cooled(acct("1", 10, 60, reset7_d=3), hours("2", 30, 80, 20),
                        hours("3", 30, 85, 22))
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "2"   # plain score order
        # Off a draining account: never onto one without room.
        s = self.cooled(hours("1", 10, 80, 20), hours("2", 30, 85, 4))
        assert isinstance(decide(s), Hold)

    def test_cooldown_and_idle_still_hold_it(self):
        s = snap("1", acct("1", 10, 60, reset7_d=3), hours("2", 0, 80, 20), samples="idle",
                 last_switch_min=10)
        assert isinstance(decide(s), Hold) and "cooldown" in decide(s).reason
        s = snap("1", acct("1", 10, 60, reset7_d=3), hours("2", 0, 80, 20), samples="busy",
                 last_switch_min=60)
        assert isinstance(decide(s), Hold) and "waits for idle" in decide(s).reason

    def test_a_draining_candidate_is_never_skipped_for_its_7d_pace(self):
        # #1 7d 80% resetting in 20 h climbs 3 pts/h: without the drain,
        # preempt would leave it within 4 h, so rebalance skips it.
        s = replace(self.cooled(acct("2", 0, 30, reset7_d=6), hours("1", 0, 80, 20)),
                    rates7={"1": 3.0})
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "1"
        off = decide(replace(s, settings=MaximizeSettings(drain_hours=0)))
        assert isinstance(off, Hold) and "would pass 90%" in off.reason


# -- with nothing draining, nothing changes ---------------------------------------------


def _regression_snaps():
    for case in tp.CASES:
        yield f"case:{case.id}", case.snap
    yield "preempt", tp.preempt_snap()
    yield "preempt-busy", tp.preempt_snap(samples=tp.BUSY5)
    for active in ("1", "2"):
        base = tp.ping(active, drain_hours=24)
        far = replace(base, accounts=tuple(replace(v, reset7=NOW + 3 * D) for v in base.accounts))
        yield f"ping-{active}", far


REGRESSION = [(name, s) for name, s in _regression_snaps()
              if not any(drain.draining(v, s) for v in s.accounts)]


def test_the_regression_set_is_most_of_the_policy_table():
    # The policy table's cases with an account resetting within a day are
    # drain cases now (and still decide as their table says).
    assert len(REGRESSION) >= 60


@pytest.mark.parametrize(("name", "s"), REGRESSION, ids=[n for n, _ in REGRESSION])
def test_no_draining_account_decides_exactly_as_without_the_drain(name, s):
    assert s.settings.drain_hours > 0
    off = replace(s, settings=replace(s.settings, drain_hours=0))
    assert decide(s) == decide(off)
    assert landing_candidates(s) == landing_candidates(off)


# -- surfaces ---------------------------------------------------------------------------


class TestSurfaces:
    def test_fleet_rows_tag_and_land(self):
        from claude_swap.maximize import fleet as fx
        from claude_swap.maximize import home
        from claude_swap.maximize.view import MaximizeState
        from tests.maximize.test_fleet import MX, NOW as FNOW, PRIME, acc, accounts, usage

        snap_ = accounts(
            acc(1, usage(62, 40), active=True),
            acc(2, usage(0, 88, days7=0.75)),
            acc(3, usage(0, 88, days7=3.0)),
        )
        rows = {r.number: r for r in fx.fleet_rows(snap_, MX, PRIME, MaximizeState(), now=FNOW)}
        assert rows["2"].drain and rows["2"].landable and rows["2"].land == "yes"
        assert not rows["3"].drain and not rows["3"].landable and rows["3"].land == "7d≥85"
        assert home.tag_for(rows["2"], is_next=False, now=FNOW) == ("drain 18h", "ok")
        assert home.tag_for(rows["2"], is_next=True, now=FNOW) == ("next", "accent")
        assert home.tag_for(rows["3"], is_next=False, now=FNOW)[0].startswith("prime")
        assert len("drain 18h") <= len("reading 25m old")
        off = {r.number: r for r in fx.fleet_rows(
            snap_, replace(MX, drain_hours=0), PRIME, MaximizeState(), now=FNOW)}
        assert not off["2"].drain and not off["2"].landable

    def test_fleet_sentence_and_capacity_for_a_draining_active_account(self):
        from claude_swap.maximize import fleet as fx
        from claude_swap.maximize import home
        from claude_swap.maximize.view import MaximizeState
        from tests.maximize.test_fleet import MX, NOW as FNOW, PRIME, acc, accounts, usage

        snap_ = accounts(acc(1, usage(20, 92, days7=0.75), active=True),
                         acc(2, usage(60, 10)))
        rows = fx.fleet_rows(snap_, MX, PRIME, MaximizeState(), now=FNOW)
        act = next(r for r in rows if r.active)
        assert act.drain
        cap = home.capacity(rows, MX, FNOW)
        assert cap.free5 == 1   # the active one stays: its 7d soft mark is set aside

        from tests.maximize.test_fleet_home import SERVICE, _plain

        msnap = fx.fleet_snapshot(snap_, MX, MaximizeState(), now=FNOW)
        dv = replace(fx.preview_decision(msnap, MX), source="engine", at=FNOW - 20)
        assert dv.kind == "hold"
        said = [_plain(v) for v in home.status_variants(SERVICE, dv, rows, MX, "live", now=FNOW)]
        assert said[0].endswith(
            "7d 92% resets in 18h — draining it first (forced at 98%)"
        ), said
        assert "past soft" not in " ".join(said)

    def test_dry_run_rows_flag_it(self):
        from claude_swap.maximize.report import decision_rows

        s = snap("1", acct("1", 20, 40), hours("2", 0, 88, 18))
        rows = {r["number"]: r for r in decision_rows(s)}
        assert rows["2"]["flags"] == "drain" and rows["2"]["landable"]
        assert rows["1"]["flags"] == ""

    def test_describe(self):
        assert drain.describe(0, {}) == "7d drain: off (maximize.drainHours is 0)"
        assert drain.describe(24, {}) == (
            "7d drain: within 24h of a 7d reset · k by plan (20x 0.165, 5x 0.105) until learned"
        )
        assert drain.describe(24, {"2": 0.167}, [("1", 18.2)]) == (
            "7d drain: within 24h of a 7d reset · draining #1 (18h) · k learned #2 0.167; "
            "others by plan (20x 0.165, 5x 0.105)"
        )

    def test_view_drain_k7_follows_the_settings_like_the_engine(self):
        from claude_swap.maximize import view as mxview

        points = (*window("1", 0, (0, 50), (0, 8)), *window("1", 6 * H, (0, 50), (8, 16)),
                  *window("1", 12 * H, (0, 50), (16, 24)))
        h = History(points=points)
        assert mxview.drain_k7(h, MaximizeSettings(), 18 * H) == {"1": 0.16}
        assert mxview.drain_k7(h, MaximizeSettings(drain_hours=0), 18 * H) == {}
        assert mxview.drain_k7(None, MaximizeSettings(), 18 * H) == {}


# -- the engine end to end --------------------------------------------------------------


class TestEngine:
    def test_a_draining_active_account_is_kept_and_published(self, temp_home):
        from claude_swap.maximize import doctor_cli
        from claude_swap.maximize.engine_hook import DECISION_KEY
        from tests.maximize.test_engine_maximize import make, of, win
        from claude_swap.autoswitch import SwitchEvent

        h = make(temp_home)
        r7 = h.clock.now + 18 * H
        usage = {"1": win(20, 92, r7=r7), "2": win(0, 10), "3": win(0, 20)}
        for _ in range(4):   # readings 5 min apart, nothing rising: idle
            h.tick_with_usage(usage)
            h.clock.advance(300)
        assert of(h, SwitchEvent) == [] and h.active_number() == 1
        record = h.state()[DECISION_KEY]
        assert record["decision"] == "hold"
        assert record["reason"].startswith("#1 7d 92% resets in 18h — draining it first")
        assert record["draining"] == {"1": pytest.approx(r7, abs=1)}
        status = doctor_cli.drain_status(
            h.switcher.backup_dir, now=h.clock.now, draining=record["draining"]
        )
        assert status["draining"][0]["slot"] == "1"
        assert "draining #1 (18h)" in status["text"]

    def test_drain_off_is_the_soft_switch(self, temp_home):
        from tests.maximize.test_engine_maximize import make, of, win
        from claude_swap.autoswitch import SwitchEvent

        h = make(temp_home, maximize={"drainHours": 0})
        r7 = h.clock.now + 18 * H
        usage = {"1": win(20, 92, r7=r7), "2": win(0, 10), "3": win(0, 20)}
        for _ in range(4):
            h.tick_with_usage(usage)
            h.clock.advance(300)
        [switch] = of(h, SwitchEvent)
        assert switch.trigger == "soft"

    def test_the_engine_learns_k_from_the_usage_history(self, temp_home):
        from claude_swap.maximize import history as hist
        from claude_swap.maximize.engine_hook import runtime_for
        from tests.maximize.test_engine_maximize import make, win

        h = make(temp_home)
        now = h.clock.now
        points = [*window("2", now - 30 * H, (0, 50, 100), (10, 18, 27)),
                  *window("2", now - 20 * H, (0, 50, 100), (27, 35, 44)),
                  *window("2", now - 10 * H, (0, 50, 100), (44, 52, 61))]
        path = hist.path_for(h.switcher.backup_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(
            json.dumps({"k": "u", "t": p.ts, "n": p.number, "p5": p.pct5, "p7": p.pct7, "a": 1})
            + "\n" for p in points
        ))
        h.tick_with_usage({"1": win(20, 40), "2": win(0, 10), "3": win(0, 20)})
        snap_ = runtime_for(h.engine).last_snapshot
        assert snap_.k7 == {"2": pytest.approx(0.17, abs=1e-3)}
