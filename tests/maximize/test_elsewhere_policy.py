"""What an account in use on another machine changes in the policy
(maximize.avoidElsewhere): landing order within a tier, rebalance off a
shared active account, and the priority-rule exception for at-risk ones."""

from __future__ import annotations

from dataclasses import replace

from claude_swap.maximize.model import Hold, Switch
from claude_swap.maximize.policy import avoided_slots, decide, landing_candidates
from tests.maximize.test_drain import hours
from tests.maximize.test_policy import NOW, acct, snap


def shared(active, *accounts, elsewhere=(), local_idle=True, **settings):
    """A cooled-down snapshot (samples idle, cooldown over) with
    ``elsewhere`` slots marked and Claude Code ``local_idle`` here."""
    s = snap(active, *accounts, samples="idle", last_switch_min=60,
             active_changed_min=60, **settings)
    return replace(s, elsewhere={n: NOW - 600 for n in elsewhere}, local_idle=local_idle)


def order(s):
    return [v.number for v in landing_candidates(s)]


class TestLandingOrder:
    def test_within_a_tier_accounts_not_in_use_elsewhere_come_first(self):
        accounts = (acct("1", 10, 10), acct("2", 0, 0, reset7_d=2), acct("3", 0, 40))
        assert order(shared("1", *accounts)) == ["2", "3"]  # score decides
        assert order(shared("1", *accounts, elsewhere={"2"})) == ["3", "2"]

    def test_an_account_in_use_elsewhere_stays_landable(self):
        s = shared("1", acct("1", 10, 10), acct("2", 0, 0), elsewhere={"2"})
        assert order(s) == ["2"]

    def test_the_tiers_still_come_first(self):
        s = shared("1", acct("1", 10, 10), acct("2", 0, 0, tier="preferred"),
                   acct("3", 0, 0), elsewhere={"2"})
        assert order(s) == ["2", "3"]

    def test_an_at_risk_account_in_use_elsewhere_is_still_first(self):
        s = shared("1", acct("1", 10, 10), acct("2", 0, 0),
                   hours("3", 0, 88, 5, tier="last_resort"), elsewhere={"2", "3"})
        assert order(s) == ["3", "2"]
        assert avoided_slots(s) == {"2"}

    def test_the_setting_off_restores_the_order(self):
        accounts = (acct("1", 10, 10), acct("2", 0, 0, reset7_d=2), acct("3", 0, 40))
        s = shared("1", *accounts, elsewhere={"2"}, avoid_elsewhere=False)
        assert order(s) == order(shared("1", *accounts))
        assert avoided_slots(s) == frozenset()


class TestRebalanceOffASharedAccount:
    def test_moves_off_at_an_idle_moment(self):
        s = shared("1", acct("1", 20, 40), acct("2", 5, 40), elsewhere={"1"})
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "rebalance"
        assert got.target == "2"
        assert "1 is in use on another machine — moving to 2" in got.reason

    def test_prefers_a_target_not_in_use_elsewhere(self):
        s = shared("1", acct("1", 20, 40), acct("2", 0, 0, reset7_d=2), acct("3", 5, 40),
                   elsewhere={"1", "2"})
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "3"

    def test_a_higher_tier_target_is_fine(self):
        s = shared("1", acct("1", 20, 40), acct("2", 5, 40, tier="preferred"),
                   elsewhere={"1"})
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "2"

    def test_no_move_while_claude_code_is_busy_here(self):
        s = shared("1", acct("1", 20, 40), acct("2", 5, 40), elsewhere={"1"},
                   local_idle=False)
        got = decide(s)  # the samples say idle: the local evidence is the tiebreak
        assert isinstance(got, Hold)
        assert "rebalance waits for idle" in got.reason
        assert "in use on another machine — moving to 2" in got.reason

    def test_waits_out_the_cooldown(self):
        s = replace(shared("1", acct("1", 20, 40), acct("2", 5, 40), elsewhere={"1"}),
                    last_switch_at=NOW - 5 * 60, active_changed_at=NOW - 5 * 60)
        got = decide(s)
        assert isinstance(got, Hold) and "rebalance cooldown" in got.reason

    def test_never_onto_another_account_in_use_elsewhere(self):
        s = shared("1", acct("1", 20, 40), acct("2", 5, 40), elsewhere={"1", "2"})
        assert not isinstance(decide(s), Switch)

    def test_never_onto_a_lower_tier(self):
        s = shared("1", acct("1", 20, 40), acct("2", 5, 40, tier="last_resort"),
                   elsewhere={"1"})
        assert not isinstance(decide(s), Switch)

    def test_never_off_an_at_risk_account(self):
        s = shared("1", hours("1", 20, 88, 5), acct("2", 5, 40), elsewhere={"1"})
        got = decide(s)
        assert not isinstance(got, Switch)
        assert "would expire unused" in got.reason

    def test_never_back_onto_a_shared_account_for_a_score_gain(self):
        s = shared("1", acct("1", 20, 80), acct("2", 5, 10, reset7_d=2), elsewhere={"2"})
        assert not isinstance(decide(s), Switch)

    def test_the_setting_off_is_todays_behavior(self):
        accounts = (acct("1", 20, 40), acct("2", 5, 40))
        off = shared("1", *accounts, elsewhere={"1"}, avoid_elsewhere=False)
        plain = shared("1", *accounts)
        assert decide(off) == decide(plain)
        assert not isinstance(decide(off), Switch)

    def test_the_setting_off_keeps_a_score_move_onto_a_shared_account(self):
        accounts = (acct("1", 20, 80), acct("2", 5, 10, reset7_d=2))
        off = shared("1", *accounts, elsewhere={"2"}, avoid_elsewhere=False)
        got = decide(off)
        assert isinstance(got, Switch) and got.target == "2"
