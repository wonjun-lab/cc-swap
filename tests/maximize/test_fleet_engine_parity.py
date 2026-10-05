"""Fleet's ``order`` column and ``next`` tag against the engine's own policy.

For every fixture the engine's snapshot is built by the engine's own
assembly, ``engine_hook.policy_snapshot`` (the function
``run_maximize_tick`` calls): ``UsageEntry.decision_value()`` (a reading too
old to trust is unknown), the stored records, accounts without a usable
backup and logins shared with another place set aside — independently of
Fleet's code. Fleet's numbered rows, in order, must be
``policy.landing_candidates`` of that snapshot; its dim ``·`` rows the rest
of ``policy.escape_candidates``; and its ``next`` the target the engine's
decision moves to, whenever it moves.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_RELOGIN_REQUIRED,
)
from claude_swap.maximize import engine_hook, home, policy
from claude_swap.maximize import fleet as fx
from claude_swap.maximize.model import Hold, Switch
from claude_swap.maximize.view import MaximizeState
from claude_swap.shared_login import SHARED_LOGIN
from claude_swap.usage_store import UsageEntry
from tests.maximize.test_fleet import H, MX, NOW, PRIME, acc, accounts, mockup, usage


def _old(pct5, pct7, *, age_s, trusted=False, reset5=None) -> UsageEntry:
    """A reading ``age_s`` old; ``trusted`` = deliberate staleness the store
    still trusts (``trust_extended``: its own cadence, a failure streak)."""
    return replace(usage(pct5, pct7, reset5=reset5, age_s=age_s), trust_extended=trusted)


def _engine_snapshot(snap, mx, state):
    """What ``engine_hook.run_maximize_tick`` decides on, from the same
    store: its own assembly (``engine_hook.policy_snapshot``) over what the
    tick reads — the usage entries and their decision values
    (``autoswitch``: ``entry.decision_value()``), the ``sequence.json``
    records, the switchable slots (``switchable_account_numbers``: a usable
    backup, not disabled)."""
    entries = {a.number: a.usage for a in snap.accounts}
    records = {
        a.number: {"email": a.email, "alias": a.alias, "disabled": a.disabled,
                   **({"kind": "api_key"} if a.kind == "api_key" else {})}
        for a in snap.accounts
    }
    esnap, _shared = engine_hook.policy_snapshot(
        now=NOW,
        current=snap.active_number,
        entries=entries,
        usage={n: e.decision_value() for n, e in entries.items()},
        records=records,
        quarantined=set(state.quarantined),
        switchable={a.number for a in snap.accounts if a.switchable and not a.disabled},
        rate_limit_tiers={a.number: state.plans.get(a.number) for a in snap.accounts},
        samples=state.samples if state.samples_account == snap.active_number else (),
        last_switch_at=state.last_switch_at,
        settings=mx,
        login_deadlines={
            a.number: a.login_expires_at / 1000.0
            for a in snap.accounts if a.login_expires_at is not None
        },
    )
    return esnap


def _deadline(acc_snapshot, seconds):
    return replace(acc_snapshot, login_expires_at=(NOW + seconds) * 1000.0)


def _fixtures():
    snap, mx, state = mockup()
    yield "mockup", snap, mx, state
    plain = MaximizeState()
    # The best account's reading is 2h old: the engine counts it as unknown.
    yield "stale best", accounts(
        acc(1, usage(62, 40), active=True),
        acc(2, _old(0, 5, age_s=2 * H)),
        acc(3, usage(20, 30)),
        acc(4, usage(10, 60)),
    ), MX, plain
    # 40 minutes old but on the scheduler's own cadence: still trusted.
    yield "trusted old", accounts(
        acc(1, usage(62, 40), active=True),
        acc(2, _old(0, 5, age_s=40 * 60, trusted=True)),
        acc(3, usage(20, 30)),
    ), MX, plain
    # Keychain locked, an API key, an excluded one, a dead login, no backup.
    yield "unusable kinds", accounts(
        acc(1, usage(30, 30), active=True),
        acc(2, sentinel=USAGE_KEYCHAIN_UNAVAILABLE),
        acc(3, sentinel=USAGE_API_KEY, kind="api_key"),
        acc(4, usage(5, 10), disabled=True),
        acc(5, sentinel=USAGE_RELOGIN_REQUIRED),
        acc(6, usage(5, 10), switchable=False),
        acc(7, usage(40, 20)),
        acc(8, usage(0, 10)),
    ), MX, plain
    # Forced-only: past soft but under hard, a login inside the expiry
    # guard; past hard: never. The active one is past its hard mark.
    yield "forced", accounts(
        acc(1, usage(99, 40, reset5=NOW + 3 * H), active=True),
        acc(2, usage(70, 30)),
        _deadline(acc(3, usage(0, 10)), 30 * 60),
        acc(4, usage(99.5, 30)),
        acc(5, usage(10, 20)),
    ), MX, plain
    # Last resort, and every candidate's week nearly spent.
    yield "last resort", accounts(
        acc(1, usage(80, 50), active=True),
        acc(2, usage(0, 85, days7=1.0)),
        acc(3, usage(0, 10), org="Acme"),
        acc(4, usage(5, 20)),
    ), replace(MX, last_resort="u3@x.com"), plain
    # #2 would be the best pick, but its login is shared.
    yield "shared", accounts(
        acc(1, usage(62, 40), active=True),
        acc(2, replace(usage(0, 5), last_error=SHARED_LOGIN)),
        acc(3, usage(20, 30)),
        acc(4, usage(70, 30)),
    ), MX, plain
    # Nothing to land on: the active one stays.
    yield "nowhere", accounts(
        acc(1, usage(55, 40), active=True),
        acc(2, usage(60, 30)),
        acc(3, _old(0, 0, age_s=3 * H)),
    ), MX, plain
    # The near-reset drain (maximize/drain.py): #2 and #3 reset within a
    # day, so their 7d soft mark is set aside (landable under 93) and they
    # go first, the sooner reset first; #5 is past 93: forced only.
    yield "draining", accounts(
        acc(1, usage(62, 40), active=True),
        acc(2, usage(0, 88, days7=0.75)),
        acc(3, usage(0, 91, days7=0.4)),
        acc(4, usage(0, 30)),
        acc(5, usage(0, 94, days7=0.5)),
        acc(6, usage(0, 0, days7=6.0)),
    ), MX, plain
    # Draining by its room: a 5x plan (k 0.105) at 40% with 30 h left needs
    # 58 of the ~63 points six 5h windows give it.
    yield "draining by room", accounts(
        acc(1, usage(62, 40), active=True),
        acc(2, usage(0, 40, days7=1.25)),
        acc(3, usage(0, 0, days7=6.0)),
    ), MX, MaximizeState(plans={"2": "5x", "3": "20x"})
    # The active account is draining at 7d 92%: no soft 7d move.
    yield "draining active", accounts(
        acc(1, usage(20, 92, days7=0.75), active=True),
        acc(2, usage(0, 10)),
        acc(3, usage(0, 89, days7=0.5)),
    ), MX, plain


FIXTURES = list(_fixtures())


@pytest.mark.parametrize(("name", "snap", "mx", "state"), FIXTURES,
                         ids=[f[0] for f in FIXTURES])
def test_fleet_numbers_exactly_the_engines_landing_order(name, snap, mx, state):
    engine = _engine_snapshot(snap, mx, state)
    landing = [v.number for v in policy.landing_candidates(engine)]
    escape = {v.number for v in policy.escape_candidates(engine)} - set(landing)

    # Fleet, as tui/fleet.py lays the table out.
    rows = fx.fleet_rows(snap, mx, PRIME, state, now=NOW)
    msnap = fx.fleet_snapshot(snap, mx, state, now=NOW)
    picks, forced = home.engine_lists(msnap)
    ordered = home.ordered_rows(rows, picks, now=NOW, forced=forced)
    marks = home.order_marks(ordered, picks, forced)

    numbered = [r.number for r in ordered if marks[r.number].isdigit()]
    assert numbered == landing, name
    assert [marks[n] for n in numbered] == [str(i) for i in range(1, len(numbered) + 1)]
    assert {n for n, m in marks.items() if m == home.ORDER_FORCED} == escape, name
    by = {r.number: r for r in rows}
    for n in landing:
        assert by[n].trusted
    for a in snap.accounts:
        assert by[a.number].trusted is (a.usage.decision_value() is not None)


@pytest.mark.parametrize(("name", "snap", "mx", "state"), FIXTURES,
                         ids=[f[0] for f in FIXTURES])
def test_fleets_next_is_the_engines_target(name, snap, mx, state):
    engine = _engine_snapshot(snap, mx, state)
    decision = policy.decide(engine)
    landing = policy.landing_candidates(engine)
    if isinstance(decision, Switch):
        target = decision.target
    elif isinstance(decision, Hold) and decision.pending:
        target = landing[0].number if landing else None
    else:
        target = None

    rows = fx.fleet_rows(snap, mx, PRIME, state, now=NOW)
    msnap = fx.fleet_snapshot(snap, mx, state, now=NOW)
    picks, _forced = home.engine_lists(msnap)
    dv = fx.preview_decision(msnap, mx)
    never = home.never_next(rows)
    nxt = home.next_number(dv, picks, "live", never)
    if target is not None:
        assert nxt == target, (name, decision)
    assert nxt is None or nxt not in never
    if nxt is not None:
        row = {r.number: r for r in rows}[nxt]
        assert home.tag_for(row, is_next=True, now=NOW)[0] == "next"


def test_the_drain_fixtures_put_draining_accounts_first():
    by_name = {f[0]: f for f in FIXTURES}
    # (order, draining slots). #3 in "draining" and in "draining active"
    # drains with under a quarter 5h window of 7d room: it is landable but
    # competes on its score.
    expected = {
        "draining": (["2", "3", "4", "6"], {"2", "3", "5"}),
        "draining by room": (["2", "3"], {"2"}),
        "draining active": (["2", "3"], {"1", "3"}),
    }
    for name, (order, draining) in expected.items():
        _, snap, mx, state = by_name[name]
        engine = _engine_snapshot(snap, mx, state)
        assert [v.number for v in policy.landing_candidates(engine)] == order, name
        rows = {r.number: r for r in fx.fleet_rows(snap, mx, PRIME, state, now=NOW)}
        assert {n for n, r in rows.items() if r.drain} == draining, name
    _, snap, mx, state = by_name["draining active"]
    decision = policy.decide(_engine_snapshot(snap, mx, state))
    assert isinstance(decision, Hold) and "draining it first" in decision.reason


def test_an_untrusted_account_is_never_tagged_next_even_when_published():
    """A target the engine published while its reading was fresh, read
    after the reading aged out: no ``next`` on it."""
    snap = accounts(acc(1, usage(62, 40), active=True), acc(2, _old(0, 5, age_s=2 * H)))
    rows = fx.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)
    dv = fx.DecisionView("pending", "1", "2", None, "", window="5h", at=NOW, source="engine")
    assert home.next_number(dv, [], "live", {r.number for r in rows if not r.trusted}) is None
    row = {r.number: r for r in rows}["2"]
    assert home.tag_for(row, is_next=True, now=NOW) == ("reading 2h old", "warn")


def test_the_summary_counts_only_trusted_readings():
    snap = accounts(
        acc(1, usage(10, 40), active=True),
        acc(2, _old(0, 5, age_s=2 * H)),            # untrusted: not counted
        acc(3, _old(0, 5, age_s=40 * 60, trusted=True)),
        acc(4, usage(70, 20, reset5=NOW + H)),
    )
    rows = fx.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)
    cap = home.capacity(rows, MX, NOW)
    assert cap.usable == 3 and cap.free5 == 2       # #1 and #3
    assert cap.back5 == (NOW + H, "4")
    assert cap.left7 == pytest.approx((60 + 95 + 80) / 100)


def test_a_shared_login_is_never_numbered_next_or_counted():
    snap = accounts(
        acc(1, usage(62, 40), active=True),
        acc(2, replace(usage(0, 5), last_error=SHARED_LOGIN)),
        acc(3, usage(20, 30)),
    )
    rows = fx.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)
    msnap = fx.fleet_snapshot(snap, MX, MaximizeState(), now=NOW)
    # The engine's own snapshot sets #2 aside: never a target, never forced.
    engine = _engine_snapshot(snap, MX, MaximizeState())
    assert "2" not in {v.number for v in policy.escape_candidates(engine)}
    assert [v.number for v in policy.landing_candidates(engine)] == ["3"]
    picks, forced = home.engine_lists(msnap)
    assert picks == ["3"] and "2" not in forced
    marks = home.order_marks(home.ordered_rows(rows, picks, now=NOW, forced=forced),
                             picks, forced)
    assert marks["2"] == home.ORDER_NONE and marks["3"] == "1"
    row = {r.number: r for r in rows}["2"]
    assert row.shared and home.tag_for(row, is_next=True, now=NOW) != ("next", "accent")
    assert home.capacity(rows, MX, NOW).usable == 2  # #1 and #3
