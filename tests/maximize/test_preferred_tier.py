"""The preferred tier (``maximize.preferred``): one tier above normal.

Tiers come first, everything else inside a tier: a landable preferred-tier
account is chosen before any normal one, rebalance moves up into it (off a
draining normal account too), and a preferred-tier account left at a mark is
not re-entered until it can land by the usual rule. With the list unset
every decision is what it was before the tier existed.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.maximize import policy, score
from claude_swap.maximize.model import TIER_ORDER, Hold, Switch
from claude_swap.maximize.policy import decide, landing_candidates
from claude_swap.maximize.tiers import (
    LAST_RESORT,
    OTHER,
    PREFERRED,
    account_entry,
    account_matches,
    last_resort_matches,
    tier_for,
    toggle_entry,
    toggle_preferred,
    without_account,
)
from tests.maximize import test_drain as td
from tests.maximize import test_policy as tp
from tests.maximize.test_policy import NOW, acct, snap

H = 3600.0


def pref(number: str, pct5: float = 0.0, pct7: float = 0.0, **kw):
    return acct(number, pct5, pct7, tier="preferred", **kw)


# -- tiers ------------------------------------------------------------------------------


class TestTierFor:
    def test_order_puts_preferred_above_normal(self):
        assert sorted(TIER_ORDER, key=TIER_ORDER.__getitem__) == [
            "preferred", "normal", "last_resort", "excluded",
        ]

    def test_preferred_by_email_or_alias_case_insensitive(self):
        assert tier_for({}, "A@Example.com", (), ("a@example.com",)) == "preferred"
        assert tier_for({"alias": "Work"}, "w@example.com", (), ("work",)) == "preferred"
        assert tier_for({"alias": "work"}, "w@example.com", (), ("team",)) == "normal"

    def test_disabled_then_last_resort_win(self):
        both = ("a@example.com",)
        assert tier_for({"disabled": True}, "a@example.com", both, both) == "excluded"
        assert tier_for({}, "a@example.com", both, both) == "last_resort"

    def test_the_old_call_is_unchanged(self):
        assert tier_for({}, "a@example.com", ("a@example.com",)) == "last_resort"
        assert tier_for({}, "a@example.com", ()) == "normal"


ACCOUNTS = {
    "1": {"email": "a@example.com"},
    "2": {"email": "team@example.com", "alias": "work"},
    "3": {"email": "team@example.com"},
}


class TestTierLists:
    def test_the_two_lists_are_each_others_other(self):
        assert OTHER[LAST_RESORT.key] is PREFERRED and OTHER[PREFERRED.key] is LAST_RESORT
        assert (PREFERRED.key, PREFERRED.field, PREFERRED.command) == (
            "maximize.preferred", "preferred", "prefer",
        )

    def test_toggle_preferred_adds_then_removes(self):
        assert toggle_preferred(ACCOUNTS, None, "1") == "a@example.com"
        assert toggle_preferred(ACCOUNTS, "a@example.com", "1") == ""
        assert toggle_preferred(ACCOUNTS, "A@Example.com, work", "1") == "work"
        # A shared email: the alias, which names only that account.
        assert toggle_preferred(ACCOUNTS, None, "2") == "work"

    def test_shared_email_without_alias_names_the_list(self):
        with pytest.raises(ConfigError, match="so preferred names only that account"):
            toggle_preferred(ACCOUNTS, None, "3")
        with pytest.raises(ConfigError, match="so last-resort names only that account"):
            account_entry(ACCOUNTS, "3", "team@example.com", LAST_RESORT)

    def test_without_account_drops_every_entry_that_marks_it(self):
        assert without_account(ACCOUNTS, "a@example.com,work,A@EXAMPLE.com", "1") == (
            "work", ["a@example.com"],
        )
        assert without_account(ACCOUNTS, "team@example.com", "3") == ("", ["team@example.com"])
        assert without_account(ACCOUNTS, None, "1") == ("", [])

    def test_matching_is_shared(self):
        assert account_matches is last_resort_matches
        assert account_matches(ACCOUNTS, "team@example.com") == ["2", "3"]
        assert toggle_entry(ACCOUNTS, None, "1") == "a@example.com"  # last resort by default


def test_snapshot_reads_maximize_preferred():
    from claude_swap.maximize.snapshot import build_snapshot
    from claude_swap.settings import MaximizeSettings

    def build(**settings):
        return build_snapshot(
            now=NOW, active="1", usage={}, quarantined=set(), api_key_accounts=set(),
            records={
                "1": {"email": "a@example.com"},
                "2": {"email": "b@example.com", "alias": "bee"},
                "3": {"email": "c@example.com"},
            },
            rate_limit_tiers={}, samples=[], last_switch_at=None,
            settings=MaximizeSettings(**settings),
        )

    tiers = {v.number: v.tier for v in build().accounts}
    assert tiers == {"1": "normal", "2": "normal", "3": "normal"}
    tiers = {
        v.number: v.tier
        for v in build(preferred="BEE, c@example.com", last_resort="c@example.com").accounts
    }
    assert tiers == {"1": "normal", "2": "preferred", "3": "last_resort"}


# -- landing order ----------------------------------------------------------------------


class TestLandingOrder:
    def test_a_preferred_account_lands_before_a_better_scored_normal_one(self):
        s = snap("1", acct("1", 30, 50), acct("2", 0, 0, reset7_d=0.5),
                 pref("3", 30, 80, reset7_d=6))
        assert [v.number for v in landing_candidates(s)] == ["3", "2"]

    def test_the_drain_orders_only_within_a_tier(self):
        draining = replace(acct("2", 0, 50), reset7=NOW + 18 * H)
        s = snap("1", acct("1", 30, 50), draining, pref("3", 0, 80, reset7_d=6),
                 pref("4", 0, 20, reset7_d=6))
        assert policy.drain.preferred(draining, s)
        assert [v.number for v in landing_candidates(s)] == ["4", "3", "2"]
        pdrain = replace(pref("5", 0, 50), reset7=NOW + 18 * H)
        s = replace(s, accounts=(*s.accounts, pdrain))
        assert [v.number for v in landing_candidates(s)] == ["5", "4", "3", "2"]

    def test_soft_lands_on_the_preferred_account(self):
        got = decide(snap("1", acct("1", 55, 40), acct("2", 0, 0), pref("3", 30, 70),
                          samples="idle"))
        assert isinstance(got, Switch) and got.trigger == "soft" and got.target == "3"

    def test_hard_and_at_limit_land_on_it_too(self):
        got = decide(snap("1", acct("1", 96, 40), acct("2", 0, 0), pref("3", 30, 70),
                          samples="busy"))
        assert isinstance(got, Switch) and got.trigger == "hard" and got.target == "3"
        got = decide(snap("1", acct("1", 100, 40), acct("2", 0, 0), pref("3", 30, 70)))
        assert isinstance(got, Switch) and got.trigger == "at-limit" and got.target == "3"

    def test_a_preferred_account_that_cannot_land_is_not_chosen(self):
        got = decide(snap("1", acct("1", 55, 40), acct("2", 0, 0), pref("3", 46, 0),
                          samples="idle"))
        assert isinstance(got, Switch) and got.target == "2"


# -- rebalance up into the preferred tier -----------------------------------------------


class TestRebalanceUp:
    def test_normal_active_moves_to_a_landable_preferred_account_at_idle(self):
        got = decide(snap("1", acct("1", 10, 10), pref("2", 30, 80), samples="idle"))
        assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "2"
        assert got.reason == "1 is normal and 2 (preferred) can land; idle"

    def test_it_waits_for_idle_and_the_cooldown(self):
        busy = decide(snap("1", acct("1", 10, 10), pref("2", 30, 80), samples="busy"))
        assert isinstance(busy, Hold) and "rebalance waits for idle" in busy.reason
        cool = decide(snap("1", acct("1", 10, 10), pref("2", 30, 80), samples="idle",
                           last_switch_min=10))
        assert isinstance(cool, Hold) and cool.reason.startswith("rebalance cooldown")

    def test_last_resort_active_moves_to_the_preferred_account_first(self):
        got = decide(snap("1", acct("1", 10, 10, tier="last_resort"), acct("2", 0, 0),
                          pref("3", 30, 80), samples="idle"))
        assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "3"

    def test_a_preferred_active_never_rebalances_down_for_score(self):
        got = decide(snap("1", pref("1", 30, 80, reset7_d=6), acct("2", 0, 0, reset7_d=0.5),
                          samples="idle"))
        assert isinstance(got, Hold) and "no better account" in got.reason

    def test_within_the_tier_the_score_still_rebalances(self):
        got = decide(snap("1", pref("1", 30, 80, reset7_d=6), pref("2", 0, 0, reset7_d=6),
                          acct("3", 0, 0), samples="idle"))
        assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "2"
        assert "beats" in got.reason

    def test_the_tier_wins_over_a_draining_active_account(self):
        draining = replace(acct("1", 10, 88), reset7=NOW + 18 * H)
        normal = decide(snap("1", draining, acct("2", 0, 0), samples="idle"))
        assert isinstance(normal, Hold) and "drain" in normal.reason  # the drain's rule
        got = decide(snap("1", draining, pref("2", 30, 80), samples="idle"))
        assert policy.draining(draining, snap("1", draining))
        assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "2"

    def test_a_hold_sets_the_tier_move_aside(self):
        s = replace(snap("1", acct("1", 10, 10), pref("2", 30, 80), samples="idle"),
                    hold_until=NOW + 2 * H)
        got = decide(s)
        assert isinstance(got, Hold) and got.code == "hold"
        assert "1 is normal and 2 (preferred) can land" in got.reason

    def test_preempt_never_moves_down_from_the_preferred_tier(self):
        s = tp.preempt_snap(active=pref("1", 30, 84), candidate=acct("2", 10, 10, reset7_d=6))
        got = decide(s)
        assert not (isinstance(got, Switch) and got.trigger == "preempt")
        assert getattr(got, "code", None) != "preempt"
        up = tp.preempt_snap(candidate=pref("2", 10, 10, reset7_d=6))
        got = decide(up)
        assert isinstance(got, Switch) and got.target == "2"


# -- no flapping: left at a mark, re-entered only once it can land ----------------------


def test_a_preferred_account_left_at_soft_stays_off_until_its_window_resets():
    reset5_h = 2.0

    def walk(active: str, p5: float, **kw):
        return decide(snap(active, pref("1", p5, 30, reset5_h=reset5_h),
                           acct("2", 10, 20), samples="idle", **kw))

    # On the preferred account at 40%: nothing to do.
    got = walk("1", 40)
    assert isinstance(got, Hold) and "no better account" in got.reason
    # 55% passes soft 50: the soft move off it at idle.
    got = walk("1", 55)
    assert isinstance(got, Switch) and got.trigger == "soft" and got.target == "2"
    # On #2 now, long past the cooldown: #1 at 55% (and 45%, the margin's
    # edge) cannot land, so no rebalance back.
    for p5 in (55, 50, 45):
        got = walk("2", p5)
        assert isinstance(got, Hold), p5
        assert got.reason.endswith("nothing else landable")
    # Its 5h window resets: landable again, and the tier moves back.
    got = walk("2", 0)
    assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "1"
    # ... but never inside the cooldown of the soft move.
    got = walk("2", 0, last_switch_min=5)
    assert isinstance(got, Hold) and got.reason.startswith("rebalance cooldown")


# -- unset: every decision as before ----------------------------------------------------


OLD_ORDER = {"normal": 0, "last_resort": 1, "excluded": 2}
REGRESSION = list(td._regression_snaps())


def test_the_regression_set_has_no_preferred_account():
    assert len(REGRESSION) >= 60
    assert not any(v.tier == "preferred" for _n, s in REGRESSION for v in s.accounts)


@pytest.mark.parametrize(("name", "s"), REGRESSION, ids=[n for n, _ in REGRESSION])
def test_without_a_preferred_account_decides_as_the_old_tier_order(name, s, monkeypatch):
    new = (decide(s), landing_candidates(s), policy.escape_candidates(s))
    monkeypatch.setattr(policy, "TIER_ORDER", OLD_ORDER)
    monkeypatch.setattr(score, "TIER_ORDER", OLD_ORDER)
    assert (decide(s), landing_candidates(s), policy.escape_candidates(s)) == new


# -- surfaces ---------------------------------------------------------------------------


class TestSurfaces:
    def test_labels_and_cells(self):
        from claude_swap.maximize import fleet as fx
        from claude_swap.maximize import view

        assert fx.TIER_CELLS["preferred"] == "pref"
        assert view.TIER_LABELS["preferred"] == "preferred"
        assert set(fx.TIER_CELLS) == set(view.TIER_LABELS) == set(TIER_ORDER)

    def test_fleet_rows_and_tag(self):
        from claude_swap.maximize import fleet as fx
        from claude_swap.maximize import home
        from claude_swap.maximize.view import MaximizeState
        from claude_swap.settings import MaximizeSettings
        from tests.maximize.test_fleet import NOW as FNOW
        from tests.maximize.test_fleet import PRIME, acc, accounts, usage

        snap_ = accounts(
            replace(acc(1, usage(10, 10), active=True), email="a@example.com"),
            replace(acc(2, usage(20, 70)), email="b@example.com"),
            replace(acc(3, usage(0, 0)), email="c@example.com"),
        )
        mx = MaximizeSettings(preferred="b@example.com")
        rows = {r.number: r for r in fx.fleet_rows(snap_, mx, PRIME, MaximizeState(), now=FNOW)}
        assert rows["2"].tier == "preferred" and rows["3"].tier == "normal"
        assert rows["2"].rank == 1 and rows["3"].rank == 2  # the tier first
        assert home.tag_for(rows["2"], is_next=False, now=FNOW) == ("preferred", "dim")
        assert home.tag_for(rows["2"], is_next=True, now=FNOW) == ("next", "accent")
        assert "preferred" in home.TAG_PRIORITY
        assert home.TAG_PRIORITY.index("drain") < home.TAG_PRIORITY.index("preferred")

    def test_why_rows_and_json_carry_the_tier(self):
        from claude_swap.maximize.report import decision_rows, render_rows

        s = snap("1", acct("1", 10, 10), pref("2", 30, 80))
        rows = decision_rows(s)
        assert [r["tier"] for r in rows] == ["normal", "preferred"]
        assert json.loads(json.dumps(rows))[1]["tier"] == "preferred"
        assert "preferred" in "\n".join(render_rows(rows))

    def test_help_and_doctor_name_it(self):
        from claude_swap.maximize.doctor_cli import TRIGGERS
        from claude_swap.tui import menus

        assert "preferred" in TRIGGERS["rebalance"]
        assert "u" in menus.HOME_KEYS and "u" in menus.ROW_KEYS
        entries = dict(menus.help_entries())
        assert "u toggles" in entries["preferred"]
        assert entries["u"].startswith("preferred on/off")
        keys = [k for k, _w in menus.help_entries()]
        assert keys.index("u") == keys.index("l") + 1  # next to l
