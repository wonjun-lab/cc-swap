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
    Case("eta-ignored-on-stale-samples",
         snap("1", acct("1", 80, 40), acct("2"), samples=rows((1500, 60, 40), (900, 80, 40))),
         Hold, pending=True, reason_has="no sample in the last 10 min"),
    Case("hard-nothing-landable-falls-back-under-hard",
         snap("1", acct("1", 96, 40), acct("2", 80, 10), acct("3", 97, 10), samples="busy"),
         Switch, target="2", trigger="hard", reason_has="under the hard caps"),
    Case("hard-nothing-under-hard-is-exhausted",
         snap("1", acct("1", 96, 40), acct("2", 95, 10), acct("3", 10, 99)),
         Exhausted),
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
