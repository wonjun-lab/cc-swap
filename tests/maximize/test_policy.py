"""Table test for ``policy.decide`` (spec §5.5, §10). No I/O."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from claude_swap.maximize.model import (
    AccountView,
    Exhausted,
    Hold,
    Indeterminate,
    Sample,
    Snapshot,
    Switch,
)
from claude_swap.maximize.policy import decide, escape_candidates, landing_candidates
from claude_swap.settings import MaximizeSettings

NOW = 1_000_000.0
H = 3600.0
D = 86400.0


def acct(
    number: str,
    pct5: float | None = 0.0,
    pct7: float | None = 0.0,
    *,
    reset7_d: float | None = 3.0,
    reset5_h: float | None = None,
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
        reset5=None if reset5_h is None else NOW + reset5_h * H,
        pct7=pct7,
        reset7=None if reset7_d is None else NOW + reset7_d * D,
        quarantined=quarantined,
        api_key=api_key,
    )


def rows(*items: tuple[float, float, float]) -> tuple[Sample, ...]:
    """(seconds before NOW, pct5, pct7), any order."""
    return tuple(sorted((Sample(NOW - ago, p5, p7) for ago, p5, p7 in items), key=lambda x: x.ts))


def _samples(kind, active: AccountView | None) -> tuple[Sample, ...]:
    if isinstance(kind, tuple):
        return kind
    if kind == "none" or active is None or active.pct5 is None or active.pct7 is None:
        return ()
    p5, p7 = active.pct5, active.pct7
    if kind == "idle":     # whole-percent plateau over 10 minutes
        return rows((600, p5, p7), (300, p5, p7), (0, p5, p7))
    if kind == "busy":     # +3 pts of 5h over 10 minutes (ETA far off)
        return rows((600, p5 - 3, p7), (300, p5 - 1.5, p7), (0, p5, p7))
    raise ValueError(kind)


def snap(
    active: str | None,
    *accounts: AccountView,
    samples="none",
    last_switch_min: float | None = None,
    active_changed_min: float | None = None,
    **settings,
) -> Snapshot:
    a = next((v for v in accounts if v.number == active), None)
    return Snapshot(
        now=NOW,
        active=active,
        accounts=tuple(accounts),
        samples=_samples(samples, a),
        last_switch_at=None if last_switch_min is None else NOW - last_switch_min * 60,
        settings=MaximizeSettings(**settings),
        active_changed_at=(
            None if active_changed_min is None else NOW - active_changed_min * 60
        ),
    )


@dataclass(frozen=True)
class Case:
    id: str
    snap: Snapshot
    kind: type
    target: str | None = None
    trigger: str | None = None
    pending: bool | None = None
    reason_has: str | None = None


CASES = [
    # -- 1. at-limit: immediate, idle not needed ---------------------------
    Case("at-limit-5h-switches-while-busy",
         snap("1", acct("1", 100, 40), acct("2", 10, 10), acct("3", 0, 50), samples="busy"),
         Switch, target="2", trigger="at-limit"),
    Case("at-limit-7d",
         snap("1", acct("1", 10, 100), acct("2", 10, 10), samples="busy"),
         Switch, target="2", trigger="at-limit"),
    Case("at-limit-fallback-skips-ineligible",
         snap("1", acct("1", 100, 40), acct("2", tier="excluded"),
              acct("3", quarantined=True), acct("4", api_key=True), acct("5", 80, 10)),
         Switch, target="5", trigger="at-limit", reason_has="under the hard caps"),
    Case("excluded-is-never-a-target",
         snap("1", acct("1", 100, 40), acct("2", tier="excluded")),
         Exhausted),
    Case("at-limit-last-fallback-takes-any-quota-left",
         # Nothing under the hard caps, but 2 has 4 pts of 5h and 3 has
         # 1.5 pts of 7d: the most binding room wins over Exhausted.
         snap("1", acct("1", 100, 60), acct("2", 96, 40), acct("3", 0, 98.5)),
         Switch, target="2", trigger="at-limit", reason_has="4 pts left"),
    Case("at-limit-last-fallback-skips-ineligible",
         snap("1", acct("1", 100, 60), acct("2", 99, 0, tier="excluded"),
              acct("3", 99, 0, quarantined=True), acct("4", 99, 0, api_key=True),
              acct("5", None, None), acct("6", 100, 10), acct("7", 10, 99.5)),
         Switch, target="7", trigger="at-limit"),
    Case("at-limit-last-fallback-ties-on-sooner-recovery",
         # All three have 3 pts left; 3's binding reset is unknown (last).
         snap("1", acct("1", 100, 50), acct("2", 97, 10, reset5_h=3),
              acct("3", 97, 10), acct("4", 97, 10, reset5_h=1)),
         Switch, target="4", trigger="at-limit"),
    Case("at-limit-everyone-at-limit-is-exhausted",
         snap("1", acct("1", 100, 60), acct("2", 100, 10), acct("3", 10, 100)),
         Exhausted),
    # -- 2. hard: immediate, includes the ETA force -----------------------
    Case("hard-5h-switches-while-busy",
         snap("1", acct("1", 96, 40), acct("2", 10, 10), samples="busy"),
         Switch, target="2", trigger="hard"),
    Case("hard-7d-beats-soft",
         snap("1", acct("1", 10, 98), acct("2", 10, 10), samples="idle"),
         Switch, target="2", trigger="hard"),
    Case("hard-by-eta",
         snap("1", acct("1", 80, 40), acct("2"), samples=rows((600, 60, 40), (0, 80, 40))),
         Switch, target="2", trigger="hard", reason_has="hard cap in ~7.5 min"),
    Case("eta-off-when-force-eta-zero",
         snap("1", acct("1", 80, 40), acct("2"), samples=rows((600, 60, 40), (0, 80, 40)),
              force_eta_min=0),
         Hold, pending=True),
    Case("hard-by-eta-after-a-reset-mid-span",
         # 5h rolled over 5 min ago and climbed 30 pts since: 3 pts/min.
         snap("1", acct("1", 30, 40), acct("2"),
              samples=rows((600, 80, 40), (300, 0, 40), (0, 30, 40)), force_eta_min=30),
         Switch, target="2", trigger="hard", reason_has="hard cap in ~21.7 min"),
    Case("eta-ignored-on-stale-samples",
         snap("1", acct("1", 80, 40), acct("2"), samples=rows((1500, 60, 40), (900, 80, 40))),
         Hold, pending=True, reason_has="no sample in the last 10 min"),
    Case("hard-nothing-landable-falls-back-under-hard",
         snap("1", acct("1", 96, 40), acct("2", 80, 10), acct("3", 97, 10), samples="busy"),
         Switch, target="2", trigger="hard", reason_has="under the hard caps"),
    Case("hard-nothing-under-hard-holds",
         # The active still has quota below 100%: stay; at-limit decides
         # once it is spent.
         snap("1", acct("1", 96, 40), acct("2", 95, 10), acct("3", 10, 99)),
         Hold, pending=False, reason_has="no account under the hard caps has more 5h room"),
    Case("hard-fallback-ranks-by-room-not-score",
         # 2 scores higher but sits 1 pt under the cap; 3 has 35 pts of room.
         snap("1", acct("1", 95, 30), acct("2", 94, 10, reset7_d=1),
              acct("3", 60, 50, reset7_d=6)),
         Switch, target="3", trigger="hard", reason_has="most 5h room"),
    Case("eta-forced-on-7d-needs-more-7d-room",
         # 7d climbs 0.5 pt/min (ETA 6 min, 3 pts of room). 2 has less 7d
         # room, 3 is over the 5h cap; 4 and 5 qualify, 4 has the most room.
         snap("1", acct("1", 10, 95), acct("2", 10, 96), acct("3", 96, 10),
              acct("4", 70, 94), acct("5", 60, 94.5),
              samples=rows((600, 10, 90), (0, 10, 95))),
         Switch, target="4", trigger="hard", reason_has="most 7d room"),
    Case("hard-on-both-windows-ranks-by-the-tighter-room",
         # 2: 5 pts of 5h / 48 of 7d; 3: 35 of 5h but 1 of 7d.
         snap("1", acct("1", 96, 98.5), acct("2", 90, 50), acct("3", 60, 97)),
         Switch, target="2", trigger="hard", reason_has="most 5h/7d room"),
    Case("eta-forced-never-moves-to-less-room",
         # Reviewer's bounce: 1 at 85% burning 1 pt/min (ETA 10 min); 2 at
         # 90% has less 5h room, so moving there would bounce straight back.
         snap("1", acct("1", 85, 30), acct("2", 90, 30),
              samples=rows((600, 75, 30), (300, 80, 30), (0, 85, 30))),
         Hold, pending=False, reason_has="hard cap in ~10.0 min"),
    Case("eta-forced-takes-an-account-with-more-room",
         snap("1", acct("1", 85, 30), acct("2", 90, 30), acct("3", 70, 30),
              samples=rows((600, 75, 30), (300, 80, 30), (0, 85, 30))),
         Switch, target="3", trigger="hard"),
    Case("hard-ignores-cooldown",
         snap("1", acct("1", 96, 40), acct("2"), last_switch_min=1),
         Switch, target="2", trigger="hard"),
    # -- 3. soft: waits for idle -------------------------------------------
    Case("soft-busy-holds-pending",
         snap("1", acct("1", 62, 40), acct("2"), samples="busy"),
         Hold, pending=True, reason_has="waiting for idle to move to #2"),
    Case("soft-idle-switches",
         snap("1", acct("1", 62, 40), acct("2"), samples="idle"),
         Switch, target="2", trigger="soft"),
    Case("soft-without-samples-waits",
         snap("1", acct("1", 62, 40), acct("2")),
         Hold, pending=True, reason_has="need samples 10 min apart"),
    Case("soft-7d-only",
         snap("1", acct("1", 10, 91), acct("2", 0, 80), samples="idle"),
         Switch, target="2", trigger="soft"),
    Case("soft-busy-across-a-5h-reset-is-not-idle",
         # ~1 pt/min before and after the 5h rollover: 72 -> 3 is not a plateau.
         snap("1", acct("1", 3, 92), acct("2", 0, 20),
              samples=rows((720, 70, 91), (600, 72, 91), (480, 74, 91), (360, 76, 91),
                           (240, 1, 92), (120, 2, 92), (0, 3, 92))),
         Hold, pending=True, reason_has="5h +6 / 7d +1 pts over 10 min"),
    Case("soft-nothing-landable-holds-not-pending",
         snap("1", acct("1", 62, 40), acct("2", 46, 10), acct("3", 0, 86), samples="idle"),
         Hold, pending=False, reason_has="nothing landable"),
    Case("soft-ignores-cooldown",
         snap("1", acct("1", 62, 40), acct("2"), samples="idle", last_switch_min=1),
         Switch, target="2", trigger="soft"),
    Case("user-example-same-reset-more-left-wins",
         snap("3", acct("1", 0, 10, reset7_d=4), acct("2", 0, 50, reset7_d=4),
              acct("3", 62, 40, reset7_d=4), samples="idle"),
         Switch, target="1", trigger="soft"),
    Case("user-example-30pct-12h-beats-90pct-6d",
         snap("3", acct("1", 0, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5),
              acct("3", 62, 40), samples="idle"),
         Switch, target="2", trigger="soft"),
    Case("unknown-candidate-never-lands",
         snap("1", acct("1", 62, 40), acct("2", None, None), acct("3", 0, 50), samples="idle"),
         Switch, target="3", trigger="soft"),
    Case("only-unknown-candidates-holds",
         snap("1", acct("1", 62, 40), acct("2", None, None), samples="idle"),
         Hold, pending=False, reason_has="nothing landable"),
    Case("tie-prefers-20x",
         snap("1", acct("1", 62, 40), acct("2", 0, 0, reset7_d=7),
              acct("3", 0, 5, reset7_d=7, weight=4), samples="idle"),
         Switch, target="3", trigger="soft"),
    Case("tie-then-sooner-running-5h-reset",
         snap("1", acct("1", 62, 40), acct("2", 10, 0), acct("3", 10, 0, reset5_h=2),
              samples="idle"),
         Switch, target="3", trigger="soft"),
    Case("tie-then-lower-slot",
         snap("1", acct("1", 62, 40), acct("4"), acct("2"), samples="idle"),
         Switch, target="2", trigger="soft"),
    # -- tiers ------------------------------------------------------------
    Case("last-resort-only-when-normals-fail",
         snap("1", acct("1", 62, 40), acct("2", 0, 0, reset7_d=0.5, tier="last_resort"),
              acct("3", 0, 60), samples="idle"),
         Switch, target="3", trigger="soft"),
    Case("last-resort-when-no-normal-lands",
         snap("1", acct("1", 62, 40), acct("2", tier="last_resort"), acct("3", 60, 10),
              samples="idle"),
         Switch, target="2", trigger="soft"),
    Case("last-resort-active-returns-to-normal-at-idle",
         snap("2", acct("2", 10, 10, tier="last_resort"), acct("3", 0, 80), samples="idle"),
         Switch, target="3", trigger="rebalance"),
    Case("last-resort-active-busy-stays",
         snap("2", acct("2", 10, 10, tier="last_resort"), acct("3", 0, 80), samples="busy"),
         Hold, pending=False, reason_has="waits for idle"),
    Case("excluded-active-leaves-at-idle",
         snap("1", acct("1", 10, 10, tier="excluded"), acct("2", 0, 50), samples="idle"),
         Switch, target="2", trigger="rebalance"),
    Case("excluded-active-busy-holds",
         snap("1", acct("1", 10, 10, tier="excluded"), acct("2", 0, 50), samples="busy"),
         Hold, pending=False),
    Case("excluded-active-takes-last-resort-when-no-normal-lands",
         snap("1", acct("1", 10, 10, tier="excluded"), acct("2", tier="last_resort"),
              acct("3", 70, 10), samples="idle"),
         Switch, target="2", trigger="rebalance"),
    # -- 4. rebalance -------------------------------------------------------
    Case("below-soft-equal-scores-holds",
         snap("1", acct("1", 20, 30), acct("2", 0, 30), samples="idle"),
         Hold, pending=False, reason_has="no better account"),
    Case("rebalance-score-gap-at-idle",
         snap("1", acct("1", 10, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5), samples="idle"),
         Switch, target="2", trigger="rebalance"),
    Case("rebalance-within-eps-holds",
         snap("1", acct("1", 10, 30, reset7_d=7), acct("2", 0, 25, reset7_d=7), samples="idle"),
         Hold, pending=False, reason_has="no better account"),
    Case("rebalance-never-normal-to-last-resort",
         snap("1", acct("1", 10, 50), acct("2", 0, 0, reset7_d=0.5, tier="last_resort"),
              samples="idle"),
         Hold, pending=False),
    Case("rebalance-in-cooldown-holds",
         snap("1", acct("1", 10, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5),
              samples="idle", last_switch_min=10),
         Hold, pending=False, reason_has="cooldown (20 min left)"),
    Case("rebalance-after-cooldown-switches",
         snap("1", acct("1", 10, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5),
              samples="idle", last_switch_min=31),
         Switch, target="2", trigger="rebalance"),
    Case("rebalance-cooldown-restarts-on-a-manual-switch",
         # No engine switch for hours, but the user switched by hand 11 min ago.
         snap("1", acct("1", 10, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5),
              samples="idle", last_switch_min=300, active_changed_min=11),
         Hold, pending=False, reason_has="cooldown (19 min left)"),
    Case("rebalance-cooldown-after-manual-switch-alone",
         snap("1", acct("1", 10, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5),
              samples="idle", active_changed_min=11),
         Hold, pending=False, reason_has="cooldown (19 min left)"),
    Case("rebalance-cooldown-uses-the-later-of-switch-and-change",
         snap("1", acct("1", 10, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5),
              samples="idle", last_switch_min=10, active_changed_min=200),
         Hold, pending=False, reason_has="cooldown (20 min left)"),
    Case("rebalance-after-manual-switch-cooldown-switches",
         snap("1", acct("1", 10, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5),
              samples="idle", last_switch_min=300, active_changed_min=31),
         Switch, target="2", trigger="rebalance"),
    Case("rebalance-busy-across-a-5h-reset-holds",
         snap("1", acct("1", 3, 41, reset7_d=6), acct("2", 0, 20, reset7_d=1),
              samples=rows((720, 70, 40), (600, 72, 40), (480, 74, 40), (360, 76, 40),
                           (240, 1, 41), (120, 2, 41), (0, 3, 41)),
              last_switch_min=120),
         Hold, pending=False, reason_has="waits for idle"),
    Case("rebalance-busy-holds",
         snap("1", acct("1", 10, 10, reset7_d=6), acct("2", 0, 70, reset7_d=0.5), samples="busy"),
         Hold, pending=False, reason_has="waits for idle"),
    # -- unknowns -------------------------------------------------------------
    Case("active-usage-unknown-is-indeterminate",
         snap("1", acct("1", None, None), acct("2")),
         Indeterminate),
    Case("no-active-is-indeterminate",
         snap(None, acct("1"), acct("2")),
         Indeterminate),
]


def test_table_has_at_least_twenty_cases():
    assert len(CASES) >= 20
    assert len({c.id for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_decide(case: Case):
    got = decide(case.snap)
    assert isinstance(got, case.kind), got
    if case.target is not None:
        assert got.target == case.target, got
    if case.trigger is not None:
        assert got.trigger == case.trigger, got
    if case.pending is not None:
        assert got.pending is case.pending, got
    if case.reason_has is not None:
        assert case.reason_has in got.reason, got.reason
    assert got.reason and "\n" not in got.reason


def test_landing_and_escape_candidates_exclude_the_active():
    s = snap("1", acct("1"), acct("2", 80, 10), acct("3"))
    assert [v.number for v in landing_candidates(s)] == ["3"]
    assert [v.number for v in escape_candidates(s)] == ["3", "2"]


def test_reasons_carry_no_email():
    for case in CASES:
        assert "@" not in decide(case.snap).reason, case.id


# -- login expiry guard (maximize.loginExpiryGuardMin) --------------------------------


def expiring(view: AccountView, minutes_left: float) -> AccountView:
    from dataclasses import replace

    return replace(view, login_deadline=NOW + minutes_left * 60)


class TestLoginExpiryGuard:
    def test_soft_switch_skips_a_target_whose_login_expires_within_the_guard(self):
        s = snap("1", acct("1", 60, 40), expiring(acct("2", 0, 10), 90), acct("3", 0, 30),
                 samples="idle")
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "soft" and got.target == "3"
        assert [v.number for v in landing_candidates(s)] == ["3"]

    def test_soft_holds_when_the_only_target_is_inside_the_guard(self):
        s = snap("1", acct("1", 60, 40), expiring(acct("2", 0, 10), 30), samples="idle")
        assert isinstance(decide(s), Hold)

    def test_rebalance_skips_it_too(self):
        s = snap("1", acct("1", 10, 60), expiring(acct("2", 0, 0), 60),
                 samples="idle", last_switch_min=120)
        assert isinstance(decide(s), Hold)
        fine = snap("1", acct("1", 10, 60), expiring(acct("2", 0, 0), 180),
                    samples="idle", last_switch_min=120)
        got = decide(fine)
        assert isinstance(got, Switch) and got.trigger == "rebalance" and got.target == "2"

    def test_at_limit_still_falls_back_to_it(self):
        s = snap("1", acct("1", 100, 40), expiring(acct("2", 0, 10), 30), samples="busy")
        got = decide(s)
        assert isinstance(got, Switch) and got.trigger == "at-limit" and got.target == "2"

    def test_guard_is_configurable_and_unknown_deadlines_are_not_guarded(self):
        s = snap("1", acct("1", 60, 40), expiring(acct("2", 0, 10), 90), samples="idle",
                 login_expiry_guard_min=60)
        assert decide(s).target == "2"
        unknown = snap("1", acct("1", 60, 40), acct("2", 0, 10), samples="idle")
        assert decide(unknown).target == "2"


class TestFallbacksSkipLapsedLogins:
    """The at-limit/hard fallbacks may use an account inside the guard, but
    never one already past its login deadline (its next refresh is refused)."""

    def test_escape_candidates_skip_a_lapsed_login(self):
        s = snap("1", acct("1", 100, 40), expiring(acct("2", 0, 10), -5), acct("3", 80, 10))
        assert [v.number for v in escape_candidates(s)] == ["3"]
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "3"

    def test_limit_candidates_skip_a_lapsed_login(self):
        from claude_swap.maximize.policy import limit_candidates

        s = snap("1", acct("1", 100, 40), expiring(acct("2", 96, 10), -5), acct("3", 99, 10))
        assert [v.number for v in limit_candidates(s)] == ["3"]
        only = snap("1", acct("1", 100, 40), expiring(acct("2", 96, 10), -5))
        assert isinstance(decide(only), Exhausted)

    def test_inside_the_guard_but_not_lapsed_is_still_a_fallback(self):
        s = snap("1", acct("1", 100, 40), expiring(acct("2", 0, 10), 30))
        got = decide(s)
        assert isinstance(got, Switch) and got.target == "2"
