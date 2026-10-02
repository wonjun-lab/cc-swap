"""maximize/view.py: the TUI's maximize read model (pure functions)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from claude_swap.json_output import USAGE_API_KEY
from claude_swap.maximize import view
from claude_swap.maximize.model import AccountView, Sample, Snapshot
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.settings import MaximizeSettings
from claude_swap.usage_store import UsageEntry

NOW = 1_800_000_000.0
DAY = 86400.0


def av(number, *, tier="normal", pct5=10.0, reset5=NOW + 3600, pct7=20.0, reset7=NOW + 3 * DAY):
    return AccountView(
        number=str(number), email=f"u{number}@x.com", tier=tier, plan_weight=1,
        pct5=pct5, reset5=reset5, pct7=pct7, reset7=reset7,
        quarantined=False, api_key=False,
    )


def snap(accounts, *, active="1", samples=(), settings=None):
    return Snapshot(
        now=NOW, active=active, accounts=tuple(accounts), samples=tuple(samples),
        last_switch_at=None, settings=settings or MaximizeSettings(),
    )


def samples(*rows):
    return [Sample(NOW + dt, p5, p7) for dt, p5, p7 in rows]


# -- 5h window state -------------------------------------------------------------


def test_state5_is_cold_when_the_window_is_off_or_past():
    assert view.state5(av(1, reset5=None), None, NOW) == "cold"
    assert view.state5(av(1, reset5=NOW - 1), None, NOW) == "cold"


def test_state5_running_without_a_matching_prime():
    assert view.state5(av(1), None, NOW) == "running"
    failed = {"lastOutcome": "failed", "lastAttemptAt": NOW + 3600 - view.FIVE_HOUR_S + 30}
    assert view.state5(av(1), failed, NOW) == "running"


def test_state5_primed_when_this_window_was_opened_by_priming():
    opened = NOW + 3600 - view.FIVE_HOUR_S  # av's reset5 is NOW + 1h
    for at in (opened - 60, opened + 30, opened + 599):
        assert view.state5(av(1), {"lastOutcome": "primed", "lastAttemptAt": at}, NOW) == "primed"


def test_state5_an_earlier_windows_prime_is_just_running():
    opened = NOW + 3600 - view.FIVE_HOUR_S
    prime = {"lastOutcome": "primed", "lastAttemptAt": opened - view.FIVE_HOUR_S}
    assert view.state5(av(1), prime, NOW) == "running"


# -- rows --------------------------------------------------------------------------


def test_rows_rank_normal_then_last_resort_then_excluded():
    accounts = [
        av(1, pct7=40.0),  # active
        av(2, pct7=20.0),  # more weekly quota left at the same reset: higher score
        av(3, tier="last_resort", pct7=0.0),  # best score, but last resort
        av(4, tier="excluded", pct7=0.0),
    ]
    rows = view.rows(snap(accounts), {})
    assert [r.number for r in rows] == ["2", "1", "3", "4"]
    by = {r.number: r for r in rows}
    assert by["1"].active and not by["2"].active
    assert by["2"].score == pytest.approx(80 / (3 * 100 / 7))
    assert by["2"].landable and by["3"].landable
    assert by["4"].landable is False and by["4"].tier == "excluded"


def test_rows_unknown_weekly_usage_has_no_score_and_cannot_land():
    rows = view.rows(snap([av(1), av(2, pct7=None)]), {})
    row = next(r for r in rows if r.number == "2")
    assert row.score is None and row.landable is False


def test_rows_read_primes_by_email():
    opened = NOW + 3600 - view.FIVE_HOUR_S
    primes = {"u2@x.com": {"lastOutcome": "primed", "lastAttemptAt": opened + 30}}
    rows = view.rows(snap([av(1), av(2)]), primes)
    assert {r.number: r.state5 for r in rows} == {"1": "running", "2": "primed"}


def test_format_score_and_state5():
    assert view.format_score(None) == "—"
    assert view.format_score(1.8666) == "1.87"
    cold = view.RowView("3", "u3@x.com", "normal", False, 1.0, True, "cold", None)
    assert view.format_state5(cold) == "5h cold"
    primed = replace(cold, state5="primed", reset5=NOW + 3600)
    assert view.format_state5(primed).startswith("5h primed · resets ")


# -- pending (soft threshold crossed, waiting for idle) -----------------------------------


def test_no_pending_below_the_soft_thresholds():
    assert view.pending(snap([av(1, pct5=49.0, pct7=89.0)])) is None


def test_pending_on_5h_reports_growth_over_the_idle_window():
    s = snap([av(1, pct5=72.0, pct7=40.0)], samples=samples((-660, 69.0, 40.0), (-60, 72.0, 40.0)))
    p = view.pending(s)
    assert p == view.PendingView(window="5h", pct=72.0, growth=3.0, window_min=10)
    assert view.format_pending(p) == "waiting for idle: 5h 72%, +3%p/10min"


def test_pending_growth_is_normalised_to_the_idle_window():
    s = snap([av(1, pct5=72.0)], samples=samples((-1260, 66.0, 20.0), (-60, 72.0, 20.0)))
    assert view.pending(s).growth == pytest.approx(3.0)


def test_pending_without_a_full_window_of_samples_is_measuring():
    s = snap([av(1, pct5=72.0)], samples=samples((-300, 70.0, 20.0), (-60, 72.0, 20.0)))
    p = view.pending(s)
    assert p.growth is None
    assert view.format_pending(p) == "waiting for idle: 5h 72%, measuring pace"


def test_pending_on_7d_when_only_the_weekly_soft_is_crossed():
    p = view.pending(snap([av(1, pct5=10.0, pct7=92.0)]))
    assert (p.window, p.pct) == ("7d", 92.0)


@pytest.mark.parametrize("pct5,pct7", [(95.0, 40.0), (40.0, 98.0), (100.0, 40.0)])
def test_no_pending_at_a_hard_ceiling_because_it_switches_at_once(pct5, pct7):
    assert view.pending(snap([av(1, pct5=pct5, pct7=pct7)])) is None


def test_no_pending_without_an_active_account_or_its_usage():
    assert view.pending(snap([av(1, pct5=72.0)], active=None)) is None
    assert view.pending(snap([av(1, pct5=None)])) is None


# -- thresholds ------------------------------------------------------------------------


def test_window_ticks_and_threshold_label():
    s = MaximizeSettings()
    assert view.window_ticks(s) == {"5h": (50.0, 95.0), "7d": (90.0, 98.0)}
    assert view.format_thresholds(s) == "5h 50/95% · 7d 90/98%"


def test_step_knob_keeps_soft_at_or_below_hard_and_inside_the_range():
    s = replace(MaximizeSettings(), soft_5h=94.0, hard_5h=95.0)
    assert view.step_knob(s, "soft_5h", 3).soft_5h == 95.0
    assert view.step_knob(s, "hard_5h", -3).hard_5h == 94.0
    assert view.step_knob(s, "hard_5h", 10).hard_5h == 99.9
    assert view.step_knob(MaximizeSettings(), "soft_7d", -200).soft_7d == 1.0
    assert view.step_knob(MaximizeSettings(), "soft_5h", 1).soft_5h == 51.0


def test_threshold_writes_lists_only_changes_in_a_valid_order():
    base = MaximizeSettings()
    assert view.threshold_writes(base, base) == []
    lower = replace(base, soft_5h=52.0, hard_5h=94.0, hard_7d=99.0)
    assert view.threshold_writes(base, lower) == [
        ("maximize.soft5h", 52.0), ("maximize.hard5h", 94.0), ("maximize.hard7d", 99.0),
    ]
    raised = replace(base, soft_5h=97.0, hard_5h=99.0)
    assert view.threshold_writes(base, raised) == [
        ("maximize.hard5h", 99.0), ("maximize.soft5h", 97.0),
    ]


@pytest.mark.parametrize("old5,new5", [
    ((50, 95), (52, 94)), ((50, 95), (97, 99)), ((50, 95), (30, 40)),
    ((60, 70), (10, 20)), ((10, 20), (80, 90)), ((50, 95), (50, 95)),
])
def test_threshold_writes_never_pass_through_soft_above_hard(old5, new5):
    old = replace(MaximizeSettings(), soft_5h=float(old5[0]), hard_5h=float(old5[1]))
    new = replace(MaximizeSettings(), soft_5h=float(new5[0]), hard_5h=float(new5[1]))
    current = {"maximize.soft5h": old.soft_5h, "maximize.hard5h": old.hard_5h}
    for key, value in view.threshold_writes(old, new):
        current[key] = value
        assert current["maximize.soft5h"] <= current["maximize.hard5h"]
    assert current == {"maximize.soft5h": new.soft_5h, "maximize.hard5h": new.hard_5h}


# -- state file ---------------------------------------------------------------------------


def test_read_state_is_empty_without_a_readable_state_file(tmp_path):
    assert view.read_state(tmp_path) == view.MaximizeState()
    (tmp_path / "autoswitch_state.json").write_text("{not json")
    assert view.read_state(tmp_path) == view.MaximizeState()
    (tmp_path / "autoswitch_state.json").write_text("[]")
    assert view.read_state(tmp_path) == view.MaximizeState()


def test_read_state_parses_the_engine_keys(tmp_path):
    (tmp_path / "autoswitch_state.json").write_text(json.dumps({
        "schemaVersion": 1,
        "maximizeSamples": {"account": "2", "samples": [
            [NOW, 72, 40], [NOW - 600, 69, 40], ["bad", 1, 2], [NOW],
        ]},
        "primes": {"u3@x.com": {"lastOutcome": "primed", "lastAttemptAt": NOW}},
        "quarantine": {"5": {"email": "u5@x.com"}},
        "lastSwitchAt": NOW - 120,
    }))
    state = view.read_state(tmp_path)
    assert state.samples_account == "2"
    assert state.samples == (Sample(NOW - 600, 69.0, 40.0), Sample(NOW, 72.0, 40.0))
    assert state.primes["u3@x.com"]["lastOutcome"] == "primed"
    assert state.quarantined == frozenset({"5"})
    assert state.last_switch_at == NOW - 120


def test_read_state_parses_published_decision_and_plans(tmp_path):
    (tmp_path / "autoswitch_state.json").write_text(json.dumps({
        "maximizeDecision": {
            "at": NOW - 30, "pid": 4121, "active": "1", "decision": "hold",
            "trigger": None, "target": "2", "reason": "#1 5h 62% >= soft 50; waiting",
            "pending": True, "plans": {"1": "20x", "2": "5x", "3": None, "4": 7},
        },
        "pausedUntil": NOW + 300, "pausedReason": "relogin",
    }))
    state = view.read_state(tmp_path)
    assert state.decision == view.PublishedDecision(
        at=NOW - 30, pid=4121, active="1", decision="hold", trigger=None,
        target="2", reason="#1 5h 62% >= soft 50; waiting", pending=True,
    )
    assert state.plans == {"1": "20x", "2": "5x", "3": None}
    assert (state.paused_until, state.paused_reason) == (NOW + 300, "relogin")


@pytest.mark.parametrize("record", [
    "hold",
    {"decision": "hold", "reason": "x"},                   # no timestamp
    {"at": "soon", "decision": "hold", "reason": "x"},
    {"at": NOW, "decision": "dance", "reason": "x"},       # unknown kind
    {"at": NOW, "decision": "switch", "reason": 3},
])
def test_read_state_ignores_malformed_published_decision(tmp_path, record):
    (tmp_path / "autoswitch_state.json").write_text(json.dumps({
        "maximizeDecision": record, "lastSwitchAt": NOW, "pausedUntil": "later",
    }))
    state = view.read_state(tmp_path)
    assert state.decision is None and state.plans == {}
    assert state.paused_until is None
    assert state.last_switch_at == NOW  # the rest still reads


def test_snapshot_from_accounts_weighs_published_plans():
    s = view.snapshot_from_accounts(
        _accounts(), MaximizeSettings(), view.MaximizeState(), now=NOW, plans={"1": "20x"},
    )
    assert [v.plan_weight for v in s.accounts] == [4, 1, 1]


def test_state_filename_matches_the_engine():
    from claude_swap.autoswitch import STATE_FILENAME
    from claude_swap.maximize.engine_hook import DECISION_KEY

    assert view.STATE_FILENAME == STATE_FILENAME
    assert view.DECISION_KEY == DECISION_KEY


# -- from the TUI's AccountsSnapshot ----------------------------------------------------------

WINDOWS = {
    # NOW is 2027-01-15T08:00:00Z: the 5h reset must lie in the future, or
    # build_snapshot reads the window as rolled over (pct5 0).
    "five_hour": {"pct": 72.0, "resets_at": "2027-01-15T10:00:00Z"},
    "seven_day": {"pct": 40.0, "resets_at": "2027-01-18T08:00:00Z"},
}


def _acc(number, *, active=False, disabled=False, kind="oauth", last_good=None, sentinel=None):
    return AccountSnapshot(
        number=str(number), email=f"u{number}@x.com", org_name="", org_uuid="",
        is_active=active, kind=kind, switchable=True,
        usage=UsageEntry(sentinel=sentinel, last_good=last_good), disabled=disabled,
    )


def _accounts():
    return AccountsSnapshot(active_number="1", accounts=(
        _acc(1, active=True, last_good=WINDOWS),
        _acc(2, disabled=True, last_good=WINDOWS),
        _acc(3, kind="api_key", sentinel=USAGE_API_KEY),
    ), taken_at=NOW)


def test_snapshot_from_accounts_maps_the_tui_snapshot():
    state = view.MaximizeState(
        samples_account="1", samples=(Sample(NOW - 60, 72.0, 40.0),), quarantined=frozenset({"2"}),
    )
    settings = replace(MaximizeSettings(), last_resort="u1@x.com")
    s = view.snapshot_from_accounts(_accounts(), settings, state, now=NOW)
    by = {v.number: v for v in s.accounts}
    assert (s.active, s.now) == ("1", NOW)
    assert by["1"].tier == "last_resort" and by["1"].pct5 == 72.0 and by["1"].reset5 is not None
    assert by["2"].tier == "excluded" and by["2"].quarantined
    assert by["3"].api_key and by["3"].pct5 is None
    assert s.samples == state.samples


def test_snapshot_from_accounts_drops_another_accounts_samples():
    state = view.MaximizeState(samples_account="2", samples=(Sample(NOW - 60, 72.0, 40.0),))
    s = view.snapshot_from_accounts(_accounts(), MaximizeSettings(), state, now=NOW)
    assert s.samples == ()
