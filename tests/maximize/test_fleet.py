"""maximize/fleet.py: the Fleet screen's view model (pure functions)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from claude_swap.autoswitch import MaximizeDecisionEvent
from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.maximize import fleet
from claude_swap.maximize.model import AccountView, Sample
from claude_swap.maximize.view import MaximizeState, PublishedDecision
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.oauth import local_clock
from claude_swap.settings import MaximizeSettings, PrimeSettings
from claude_swap.usage_store import UsageEntry

# 2027-01-15T08:00:00Z sits on a 10-minute boundary; NOW is 3 minutes in.
BUCKET = 1_800_000_000.0 - (1_800_000_000.0 % 600)
NOW = BUCKET + 180.0
H = 3600.0
DAY = 86400.0
MX = MaximizeSettings()
PRIME = PrimeSettings(enabled=True, jitter_s="45-300")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def usage(pct5, pct7, *, reset5=None, days7=3.0, age_s=5.0) -> UsageEntry:
    return UsageEntry(
        last_good={
            "five_hour": {"pct": pct5, "resets_at": _iso(reset5) if reset5 else None},
            "seven_day": {"pct": pct7, "resets_at": _iso(NOW + days7 * DAY)},
        },
        fetched_at=NOW - age_s,
        age_s=age_s,
    )


def acc(number, entry=None, *, active=False, alias="", disabled=False, kind="oauth",
        sentinel=None, org="", switchable=True) -> AccountSnapshot:
    if sentinel is not None:
        entry = UsageEntry(sentinel=sentinel)
    return AccountSnapshot(
        number=str(number), email=f"u{number}@x.com", org_name=org, org_uuid="o" if org else "",
        is_active=active, kind=kind, switchable=switchable,
        usage=entry if entry is not None else usage(10.0, 20.0), alias=alias,
        disabled=disabled,
    )


def accounts(*items) -> AccountsSnapshot:
    active = next((a.number for a in items if a.is_active), None)
    return AccountsSnapshot(active_number=active, accounts=tuple(items), taken_at=NOW)


def mockup():
    """The design mockup's fleet (§2.3): #1 active past 5h soft, #3 needs a
    re-login, #2/#5 tie within epsilon, #6 is a last-resort Team account."""
    snap = accounts(
        acc(1, usage(62, 41, reset5=NOW + 2 * H, days7=3.8), active=True, alias="main"),
        acc(2, usage(0, 55, days7=2.2), alias="side"),
        acc(3, sentinel=USAGE_RELOGIN_REQUIRED, alias="old"),
        acc(4, usage(3, 22, reset5=NOW + 3 * H, days7=5.1), alias="work"),
        acc(5, usage(48, 71, reset5=NOW + 0.5 * H, days7=1.5), alias="alt"),
        acc(6, usage(0, 30, days7=4.0), alias="team", org="Acme"),
    )
    state = MaximizeState(
        samples_account="1",
        samples=(Sample(NOW - 660, 59.0, 41.0), Sample(NOW - 60, 62.0, 41.0)),
        primes={"u4@x.com": {"lastOutcome": "primed", "lastAttemptAt": NOW + 3 * H - 5 * H + 30,
                             "windowKey": "cold", "attempts": 1}},
        quarantined=frozenset({"3"}),
        plans={"1": "20x", "2": "5x", "4": "20x", "5": "5x", "6": "team"},
    )
    mx = replace(MX, last_resort="team")
    return snap, mx, state


def av(number, **kw) -> AccountView:
    base = dict(number=str(number), email=f"u{number}@x.com", tier="normal", plan_weight=1,
                pct5=10.0, reset5=None, pct7=20.0, reset7=NOW + 3 * DAY,
                quarantined=False, api_key=False)
    base.update(kw)
    return AccountView(**base)


# -- login state, plan, land -----------------------------------------------------------


@pytest.mark.parametrize(("account", "expected"), [
    (acc(1), "ok"),
    (acc(1, sentinel=USAGE_RELOGIN_REQUIRED), "relogin"),
    (acc(1, sentinel=USAGE_TOKEN_EXPIRED), "expired"),
    (acc(1, sentinel=USAGE_FOREIGN_CREDENTIAL), "foreign"),
    (acc(1, sentinel=USAGE_KEYCHAIN_UNAVAILABLE), "keychain"),
    (acc(1, sentinel=USAGE_API_KEY, kind="api_key"), "api"),
])
def test_login_state_maps_every_sentinel(account, expected):
    assert fleet.login_state(account) == expected


def test_plan_label_prefers_published_then_override_then_org():
    personal = acc(1)
    team = acc(2, org="Acme")
    assert fleet.plan_label(personal, "20x", "u1@x.com:5x") == "20x"
    assert fleet.plan_label(personal, None, "U1@x.com:5x") == "5x"
    assert fleet.plan_label(personal, None, "u1@x.com:20x") == "20x"
    assert fleet.plan_label(team, None, None) == "team"
    assert fleet.plan_label(team, "team", None) == "team"
    assert fleet.plan_label(personal, None, None) == "?"
    # Claude names a personal account's org "<email>'s Organization".
    assert fleet.plan_label(acc(3, org="u3@x.com's Organization"), None, None) == "?"
    assert fleet.plan_label(acc(4, kind="api_key"), "20x", None) == "api"


@pytest.mark.parametrize(("view", "active", "login", "expected"), [
    (av(2, pct5=10.0, pct7=20.0), False, "ok", "yes"),
    (av(2, pct5=46.0, pct7=20.0), False, "ok", "5h≥45"),
    (av(2, pct5=10.0, pct7=86.0), False, "ok", "7d≥85"),
    (av(2, tier="excluded"), False, "ok", "excluded"),
    (av(2, quarantined=True), False, "ok", "quarant."),
    (av(2, quarantined=True, pct5=None, pct7=None), False, "relogin", "re-login"),
    (av(2, pct5=None, pct7=None), False, "ok", "usage ?"),
    (av(2, api_key=True), False, "api", "api key"),
    (av(1), True, "ok", "active"),
])
def test_land_note_names_the_failing_window_with_margin(view, active, login, expected):
    assert fleet.land_note(view, MX, active=active, login=login) == expected


# -- next prime ----------------------------------------------------------------------------


def test_prime_cell_cold_is_due_in_the_current_bucket():
    cell = fleet.prime_cell(av(2), None, "1", PRIME, NOW)
    assert (cell.kind, cell.lo, cell.hi) == ("due", BUCKET + 45, BUCKET + 300)
    assert fleet.prime_text(cell) == f"≤{fleet.hhmm(BUCKET + 300)}"


def test_prime_cell_running_window_primes_after_its_reset():
    cell = fleet.prime_cell(av(2, reset5=NOW + H), None, "1", PRIME, NOW)
    assert (cell.kind, cell.lo, cell.hi) == ("window", NOW + H + 45, NOW + H + 300)
    assert fleet.prime_text(cell) == f"{fleet.hhmm(NOW + H + 45)}–{fleet.hhmm(NOW + H + 300)}"


@pytest.mark.parametrize(("view", "entry", "prime", "note"), [
    (av(2), {"lastOutcome": "rate-limited", "lastAttemptAt": NOW - 60}, PRIME,
     "rate-ltd → " + fleet.day_clock(NOW + 3 * DAY, NOW)),
    (av(2), {"lastOutcome": "unverified", "lastAttemptAt": NOW - 60, "windowKey": "cold",
             "attempts": 2}, PRIME, "2/2 tries"),
    (av(2), {"lastOutcome": "launched", "lastAttemptAt": NOW - 60}, PRIME, "verifying"),
    (av(2), {"lastOutcome": "skipped-live", "lastAttemptAt": NOW - 60}, PRIME, "live session"),
    (av(2, pct7=100.0), None, PRIME, "7d spent"),
    (av(2, quarantined=True), None, PRIME, "re-login"),
    (av(2, pct5=None, pct7=None), None, PRIME, "usage ?"),
    (av(2), None, replace(PRIME, enabled=False), "off"),
    (av(2, tier="excluded"), None, PRIME, "—"),
    (av(1), None, PRIME, "—"),
])
def test_prime_cell_skip_reasons(view, entry, prime, note):
    assert fleet.prime_text(fleet.prime_cell(view, entry, "1", prime, NOW)) == note


# -- rows ---------------------------------------------------------------------------------


def test_fleet_rows_keep_slot_order_and_carry_maximize_rank():
    snap, mx, state = mockup()
    rows = fleet.fleet_rows(snap, mx, PRIME, state, now=NOW)
    assert [r.number for r in rows] == ["1", "2", "3", "4", "5", "6"]
    by = {r.number: r for r in rows}
    # #2 (1.43) and #5 (1.35) tie within 0.1: the sooner running 5h reset wins.
    assert by["2"].score > by["5"].score
    assert (by["5"].rank, by["2"].rank, by["1"].rank, by["4"].rank) == (1, 2, 3, 4)
    assert by["3"].rank is None and by["6"].rank == 6
    assert [by[n].plan for n in "123456"] == ["20x", "5x", "?", "20x", "5x", "team"]
    assert by["6"].tier == "last_resort"
    assert by["1"].active and by["1"].land == "active"
    assert by["2"].land == "yes" and by["5"].land == "5h≥45"
    assert by["3"].login == "relogin" and by["3"].land == "re-login"
    assert by["4"].state5 == "primed" and by["1"].state5 == "running" and by["2"].state5 == "cold"
    assert by["2"].prime.kind == "due" and by["1"].prime.kind == "active"
    assert by["3"].prime.note == "re-login"
    assert by["1"].name == "main" and by["1"].email == "u1@x.com"
    assert round(by["2"].days7, 1) == 2.2


def test_fleet_rows_mark_stale_readings_and_unswitchable_slots():
    snap = accounts(
        acc(1, active=True),
        acc(2, usage(10, 20, age_s=900)),
        acc(3, switchable=False),
    )
    rows = {r.number: r for r in fleet.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)}
    assert rows["2"].stale and not rows["1"].stale
    assert rows["3"].land == "quarant."


# -- decision -----------------------------------------------------------------------------


def _msnap():
    snap, mx, state = mockup()
    return fleet.fleet_snapshot(snap, mx, state, now=NOW), state


def test_decision_view_prefers_fresh_published():
    msnap, state = _msnap()
    published = PublishedDecision(
        at=NOW - 30, pid=4121, active="1", decision="hold", trigger=None, target="2",
        reason="#1 5h 62% >= soft 50; waiting for idle to move to #2 (x)", pending=True,
    )
    dv = fleet.decision_view(replace(state, decision=published), msnap, now=NOW, poll_s=60)
    assert (dv.kind, dv.target, dv.source, dv.at) == ("pending", "2", "engine", NOW - 30)
    # This TUI's own engine wins over anything published.
    own = MaximizeDecisionEvent(active="1", decision="switch", trigger="soft",
                                reason="#1 5h 62% >= soft 50; idle; -> #2 (normal, score 1.43)")
    dv = fleet.decision_view(replace(state, decision=published), msnap, now=NOW, poll_s=60,
                             own=own, own_at=NOW - 5)
    assert (dv.kind, dv.trigger, dv.target, dv.source) == ("switch", "soft", "2", "here")


def test_decision_view_recomputes_when_published_is_stale_or_missing():
    msnap, state = _msnap()
    old = PublishedDecision(
        at=NOW - 3600, pid=1, active="1", decision="switch", trigger="hard", target="4",
        reason="old", pending=False,
    )
    for st in (state, replace(state, decision=old),
               replace(state, decision=replace(old, at=NOW - 10, active="2"))):
        dv = fleet.decision_view(st, msnap, now=NOW, poll_s=60)
        assert dv.source == "computed" and dv.kind == "pending" and dv.target == "2"


def test_decision_view_pending_reports_growth_and_hard_eta():
    msnap, state = _msnap()
    dv = fleet.decision_view(state, msnap, now=NOW, poll_s=60)
    assert dv.window == "5h" and dv.growth == pytest.approx(3.0)
    # +3 points in 10 minutes from 62% to hard 95: 110 minutes.
    assert dv.eta_hard_min == pytest.approx(110.0)
    line = fleet.now_line(dv, now=NOW)
    assert line.startswith("HOLD — waiting for idle → #2 · #1 5h 62% >= soft 50% · +3%p/10m")
    assert "hard in ~1h50m" in line and line.endswith("computed here")


def test_decision_view_shows_a_pause_first():
    msnap, state = _msnap()
    dv = fleet.decision_view(
        replace(state, paused_until=NOW + 300, paused_reason="relogin"), msnap, now=NOW, poll_s=60
    )
    assert dv.kind == "paused"
    assert fleet.now_line(dv, now=NOW).startswith("PAUSED · relogin")


def test_preview_decision_uses_edited_thresholds():
    msnap, _state = _msnap()
    assert fleet.preview_decision(msnap, msnap.settings).kind == "pending"
    relaxed = replace(msnap.settings, soft_5h=70.0, hard_5h=96.0)
    assert fleet.preview_decision(msnap, relaxed).kind == "hold"
    strict = replace(msnap.settings, soft_5h=40.0, hard_5h=60.0)
    dv = fleet.preview_decision(msnap, strict)
    assert (dv.kind, dv.trigger, dv.target) == ("switch", "hard", "2")


# -- hold codes, the reset-aware wait and the usage history (feat/reset-wait hooks) ----------------


#: #1 at 5h 96% (past hard 95), the window resetting in 8 minutes, at a pace
#: that needs 80 minutes to reach 100%: the policy waits the reset out.
def _reset_wait_fleet():
    snap = accounts(
        acc(1, usage(96, 40, reset5=NOW + 8 * 60 + 20), active=True, alias="main"),
        acc(2, usage(10, 20), alias="side"),
    )
    state = MaximizeState(samples_account="1", samples=(
        Sample(NOW - 660, 95.5, 40.0), Sample(NOW - 60, 96.0, 40.0),
    ))
    return snap, state


RESET_WAIT_REASON = (
    "#1 5h 96% — resets in 8m, waiting it out (switches at once if it hits 100%)"
)


def test_a_published_code_is_read_for_a_hold_only():
    from claude_swap.maximize.view import _published

    base = {"at": NOW, "pid": 1, "active": "1", "decision": "hold", "trigger": None,
            "target": None, "reason": RESET_WAIT_REASON, "pending": False}
    assert _published({**base, "code": "reset-wait"})[0].code == "reset-wait"
    assert _published({**base, "code": "rebalance-deferred"})[0].code == "rebalance-deferred"
    assert _published(base)[0].code is None
    assert _published({**base, "code": "from-a-newer-engine"})[0].code is None
    assert _published({**base, "code": ["reset-wait"]})[0].code is None
    switch = {**base, "decision": "switch", "trigger": "hard", "code": "reset-wait"}
    assert _published(switch)[0].code is None


def test_a_reset_wait_hold_names_its_window_and_has_no_hard_eta():
    snap, state = _reset_wait_fleet()
    msnap = fleet.fleet_snapshot(snap, MX, state, now=NOW)
    dv = fleet.decision_view(state, msnap, now=NOW, poll_s=60)
    assert (dv.kind, dv.code, dv.source) == ("hold", "reset-wait", "computed")
    assert dv.reason == RESET_WAIT_REASON
    assert dv.waits == (("5h", 96.0, pytest.approx(NOW + 8 * 60 + 20)),)
    # Past the hard cap the ETA to it is 0: "hard in ~0m" said nothing true.
    assert dv.eta_hard_min is None
    assert "hard in" not in fleet.now_line(dv, now=NOW)
    # The same hold, published by the engine.
    published = PublishedDecision(
        at=NOW - 30, pid=4121, active="1", decision="hold", trigger=None, target=None,
        reason=RESET_WAIT_REASON, pending=False, code="reset-wait",
    )
    dv = fleet.decision_view(replace(state, decision=published), msnap, now=NOW, poll_s=60)
    assert (dv.kind, dv.code, dv.source, dv.target) == ("hold", "reset-wait", "engine", None)
    assert dv.eta_hard_min is None and dv.waits[0][0] == "5h"
    # A plain hold over the hard cap keeps its ETA (it is not waiting a reset out).
    plain = fleet.decision_view(
        replace(state, decision=replace(published, code=None)), msnap, now=NOW, poll_s=60
    )
    assert plain.code is None and plain.eta_hard_min == 0.0


def test_a_tui_hosted_engine_carries_the_code_too():
    snap, state = _reset_wait_fleet()
    msnap = fleet.fleet_snapshot(snap, MX, state, now=NOW)
    own = MaximizeDecisionEvent(active="1", decision="hold", trigger=None,
                                reason=RESET_WAIT_REASON, code="reset-wait")
    dv = fleet.decision_view(state, msnap, now=NOW, poll_s=60, own=own, own_at=NOW - 5)
    assert (dv.kind, dv.code, dv.source) == ("hold", "reset-wait", "here")
    assert dv.eta_hard_min is None
    # A code on anything but a hold, or one this build does not know, is ignored.
    odd = MaximizeDecisionEvent(active="1", decision="hold", trigger=None,
                                reason=RESET_WAIT_REASON, code="from-a-newer-engine")
    assert fleet.decision_view(state, msnap, now=NOW, poll_s=60, own=odd).code is None


def test_the_engine_event_carries_the_hold_code():
    from claude_swap.maximize import engine_hook
    from claude_swap.maximize.model import Hold, Switch

    snap, state = _reset_wait_fleet()
    msnap = fleet.fleet_snapshot(snap, MX, state, now=NOW)
    held = engine_hook._decision_event(
        msnap, Hold(RESET_WAIT_REASON, pending=False, code="reset-wait"), False
    )
    assert held.code == "reset-wait" and held._fields()["code"] == "reset-wait"
    moved = engine_hook._decision_event(msnap, Switch("2", "preempt", "why"), False)
    assert moved.code is None and "code" not in moved._fields()


def test_auto_off_says_which_hold_it_would_be():
    snap, state = _reset_wait_fleet()
    msnap = fleet.fleet_snapshot(snap, MX, state, now=NOW)
    dv = fleet.decision_view(replace(state, auto_off=True), msnap, now=NOW, poll_s=60)
    assert (dv.kind, dv.would, dv.code) == ("off", "hold (reset-wait)", None)


def _hourly_points(number: str, pct7_now: float, per_hour: float, hours: int):
    from claude_swap.maximize.history import UsagePoint

    return [
        UsagePoint(NOW - k * H, number, 20.0, pct7_now - k * per_hour, True)
        for k in range(hours, -1, -1)
    ]


def _preempt_fleet():
    """#1 at 7d 84%, climbing 1.5 points an hour while active; #2 has room.
    The 5h rose 3 points in the last 10 minutes: not idle."""
    snap = accounts(
        acc(1, usage(30, 84, days7=3), active=True, alias="main"),
        acc(2, usage(10, 20, days7=3), alias="side"),
    )
    state = MaximizeState(samples_account="1", samples=(
        Sample(NOW - 660, 27.0, 84.0), Sample(NOW - 60, 30.0, 84.0),
    ))
    return snap, state


def test_fleet_decisions_use_the_usage_history():
    from claude_swap.maximize import history

    snap, state = _preempt_fleet()
    h = history.History(points=tuple(_hourly_points("1", 84.0, 1.5, 6)))
    with_history = fleet.fleet_snapshot(snap, MX, state, now=NOW, history=h)
    assert with_history.rates7 == history.burn_rates(h.points, NOW)
    assert with_history.rates7["1"] == pytest.approx(1.5)
    assert with_history.forecast is None  # fewer than 3 days of slots: no pattern
    dv = fleet.decision_view(state, with_history, now=NOW, poll_s=60)
    assert (dv.kind, dv.code, dv.target, dv.source) == ("hold", "preempt", "2", "computed")
    assert "would pass 90% in ~4h, within the next 4h" in dv.reason
    # Without history (none, unreadable, or learning off) it decides as the
    # engine does on a cold start.
    bare = fleet.fleet_snapshot(snap, MX, state, now=NOW)
    assert bare.rates7 == {} and bare.forecast is None
    assert fleet.decision_view(state, bare, now=NOW, poll_s=60).code is None
    off = replace(MX, preempt=False, learn_idle_pattern=False)
    assert fleet.fleet_snapshot(snap, off, state, now=NOW, history=h).rates7 == {}


def test_history_inputs_follow_the_settings_like_the_engine():
    from claude_swap.maximize import history
    from claude_swap.maximize import view as mxview

    slots = tuple(
        history.SlotObs(NOW - k * history.SLOT_S - (NOW % history.SLOT_S), k % 8 != 0)
        for k in range(1, 4 * 96)
    )
    h = history.History(points=tuple(_hourly_points("1", 84.0, 1.5, 6)), slots=slots)
    forecast, rates = mxview.history_inputs(h, MX, NOW)
    assert forecast == history.forecast(h.slots, NOW) and forecast is not None
    assert rates == history.burn_rates(h.points, NOW)
    assert mxview.history_inputs(h, replace(MX, learn_idle_pattern=False), NOW)[0] is None
    assert mxview.history_inputs(h, replace(MX, preempt=False), NOW)[1] == {}
    assert mxview.history_inputs(None, MX, NOW) == (None, {})


def test_a_history_error_falls_back_to_no_history(tmp_path, monkeypatch):
    from claude_swap.maximize import history
    from claude_swap.maximize import view as mxview

    assert mxview.read_history(tmp_path, NOW) == history.History()  # no file yet
    (tmp_path / history.HISTORY_FILENAME).mkdir()  # a directory: unreadable as a file
    assert mxview.read_history(tmp_path, NOW) in (None, history.History())

    def boom(*_a, **_k):
        raise RuntimeError("corrupt")

    monkeypatch.setattr(history, "forecast", boom)
    h = history.History(points=tuple(_hourly_points("1", 84.0, 1.5, 6)))
    assert mxview.history_inputs(h, MX, NOW) == (None, {})
    snap, state = _preempt_fleet()
    assert fleet.fleet_snapshot(snap, MX, state, now=NOW, history=h).rates7 == {}
    monkeypatch.setattr(history, "read", boom)
    (tmp_path / history.HISTORY_FILENAME).rmdir()
    (tmp_path / history.HISTORY_FILENAME).write_text("{}\n")
    assert mxview.read_history(tmp_path, NOW) is None


def test_read_history_parses_once_while_the_file_is_unchanged(tmp_path, monkeypatch):
    import os

    from claude_swap.maximize import history
    from claude_swap.maximize import view as mxview

    path = tmp_path / history.HISTORY_FILENAME
    path.write_text('{"k":"u","t":%r,"n":"1","p5":1,"p7":2,"a":1}\n' % (NOW - 60))
    calls: list[int] = []
    real = history.read
    monkeypatch.setattr(history, "read", lambda root, now=None: calls.append(1) or real(root))
    first = mxview.read_history(tmp_path, NOW)
    assert len(first.points) == 1 and calls == [1]
    assert mxview.read_history(tmp_path, NOW) == first and calls == [1]
    with open(path, "a") as f:
        f.write('{"k":"u","t":%r,"n":"2","p5":1,"p7":2,"a":0}\n' % (NOW - 30))
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    assert len(mxview.read_history(tmp_path, NOW).points) == 2 and calls == [1, 1]


def test_the_idle_pattern_line_for_help_and_swap_strategy():
    from claude_swap.maximize import history
    from claude_swap.maximize import view as mxview

    assert mxview.idle_pattern_text(history.History(), MX, NOW) == (
        "idle pattern: learning (0 of 3 days observed)"
    )
    assert mxview.idle_pattern_text(None, MX, NOW) == "idle pattern: usage history unreadable"
    assert mxview.idle_pattern_text(None, replace(MX, learn_idle_pattern=False), NOW) == (
        "idle pattern: off (maximize.learnIdlePattern)"
    )
    slots = tuple(
        history.SlotObs(NOW - k * history.SLOT_S - (NOW % history.SLOT_S), False)
        for k in range(1, 10 * 96)
    )
    days = history.learned_days(slots, NOW)
    text = mxview.idle_pattern_text(history.History(slots=slots), MX, NOW)
    assert text.startswith(f"idle pattern: {days} days learned · quiet now until ")
    assert ", " not in text  # Fleet's separator; doctor and why keep the comma
    assert history.describe(slots, NOW).startswith(f"idle pattern: {days} days learned, ")


# -- engine status and status lines ---------------------------------------------------------------


def test_engine_status_identifies_the_service_by_pid():
    service = {"platform": "darwin", "installed": True, "running": True, "pid": 4121}
    es = fleet.engine_status(held_elsewhere=True, holder_pid=4121, own=None, service=service)
    assert (es.holder, es.pid) == ("service", 4121)
    es = fleet.engine_status(held_elsewhere=True, holder_pid=5521, own=None, service=service)
    assert (es.holder, es.pid) == ("other", 5521)
    assert fleet.engine_status(held_elsewhere=False, holder_pid=None, own="live",
                               service=service).holder == "here-live"
    assert fleet.engine_status(held_elsewhere=False, holder_pid=None, own="dry",
                               service=None).holder == "here-dry"


def test_engine_status_none_when_free():
    es = fleet.engine_status(held_elsewhere=False, holder_pid=4121, own=None, service=None)
    assert (es.holder, es.pid) == ("none", None)
    stopped = {"platform": "linux", "installed": True, "running": False, "pid": None,
               "state": "inactive", "linger": False}
    es = fleet.engine_status(held_elsewhere=False, holder_pid=None, own=None, service=stopped)
    assert es.holder == "none" and es.service is stopped


def test_login_expired_is_a_relogin_named_by_its_cause():
    from claude_swap.json_output import USAGE_LOGIN_EXPIRED

    snap = accounts(
        acc(1, active=True),
        acc(2, sentinel=USAGE_LOGIN_EXPIRED, alias="lapsed"),
        acc(3, sentinel=USAGE_RELOGIN_REQUIRED, alias="dead"),
    )
    rows = {r.number: r for r in fleet.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)}
    assert rows["2"].login == "relogin" and rows["2"].login_expired is True
    assert rows["3"].login == "relogin" and rows["3"].login_expired is False
    assert fleet.relogin_count(list(rows.values())) == 2
    steps = "\n".join(fleet.relogin_steps(rows["2"], ssh=False, host="h",
                                          claude_path=None, return_to=rows["1"]))
    assert "its login expired" in steps and "refresh token is dead" not in steps
    assert fleet.login_text(rows["2"]) == ("re-login needed (login expired)", "crit")
    assert fleet.login_text(rows["3"]) == ("re-login needed (refresh token dead)", "crit")


def _expiring(number, seconds_left, **kw):
    return replace(acc(number, **kw), login_expires_at=(NOW + seconds_left) * 1000)


def test_login_deadline_tags_count_down_amber_in_the_last_week_red_in_the_last_day():
    from claude_swap.maximize import home

    snap = accounts(
        acc(1, active=True),
        _expiring(2, DAY + 9 * H, alias="side"),
        _expiring(3, 20 * H, alias="soon"),
        _expiring(4, -60, alias="gone"),
        _expiring(5, 20 * DAY, alias="fine"),
    )
    rows = {r.number: r for r in fleet.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)}
    assert rows["2"].login_deadline == NOW + DAY + 9 * H
    assert rows["1"].login_deadline is None

    def tag(n):
        return home.tag_for(rows[n], is_next=False, now=NOW, priming=False)

    assert tag("2") == ("login 1d left", "warn")
    assert tag("3") == ("login 20h left", "crit")
    assert tag("4") == ("login expired", "crit")
    assert not fleet.login_due(rows["5"], NOW) and not fleet.login_due(rows["1"], NOW)


def test_land_note_names_the_login_guard():
    snap = accounts(acc(1, active=True), _expiring(2, 30 * 60, alias="soon"),
                    _expiring(3, 3 * H, alias="ok"))
    rows = {r.number: r for r in fleet.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)}
    assert rows["2"].land == "login<2h" and rows["2"].landable is False
    assert rows["3"].land == "yes"


def test_attention_warns_of_logins_expiring_within_a_week():
    from claude_swap.maximize import home

    snap = accounts(
        acc(1, active=True),
        _expiring(2, DAY + 9 * H, alias="side"),
        _expiring(5, 20 * DAY, alias="fine"),
    )
    rows = fleet.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)

    def lines(rows, width=200):
        return home.attention_lines(home.attention_notices(rows, now=NOW), width)

    assert lines(rows) == [("! #2 side login ends in 1d 9h — select it, press r", "warn")]
    soon = fleet.fleet_rows(
        accounts(acc(1, active=True), _expiring(3, 20 * H, alias="soon"),
                 _expiring(4, 2 * DAY, alias="next")),
        MX, PRIME, MaximizeState(), now=NOW,
    )
    assert lines(soon) == [(
        "! #3 soon login ends in 20h 0m — select it, press r · #4 next login ends in 2d 0h",
        "crit",
    )]
    assert fleet.login_due(soon[1], NOW) and not fleet.login_due(rows[2], NOW)
    # A dead login leads; an expiring one rides along.
    mixed = fleet.fleet_rows(
        accounts(acc(1, active=True), _expiring(2, DAY + 9 * H, alias="side"),
                 acc(3, sentinel=USAGE_RELOGIN_REQUIRED, alias="old")),
        MX, PRIME, MaximizeState(), now=NOW,
    )
    assert lines(mixed) == [(
        "! #3 old needs re-login — select it, press r · #2 side login ends in 1d 9h", "crit",
    )]
    steps = "\n".join(fleet.relogin_steps(rows[1], ssh=False, host="h", claude_path=None,
                                          return_to=rows[0], now=NOW))
    assert f"login expires {local_clock(NOW + DAY + 9 * H)} (in 1d 9h)" in steps


# -- actions ------------------------------------------------------------------------------


def test_switch_warning_only_for_non_landable_relogin_excluded():
    snap, mx, state = mockup()
    rows = {r.number: r for r in fleet.fleet_rows(snap, mx, PRIME, state, now=NOW)}
    assert fleet.switch_warning(rows["2"], mx) is None
    assert "may move you again" in fleet.switch_warning(rows["5"], mx)
    assert "log in" in fleet.switch_warning(rows["3"], mx)
    excluded = replace(rows["2"], tier="excluded", land="excluded", landable=False)
    assert "excluded" in fleet.switch_warning(excluded, mx)
    assert fleet.switch_warning(rows["1"], mx) is None  # already active


def test_relogin_steps_mention_ssh_code_flow_only_over_ssh():
    snap, mx, state = mockup()
    rows = {r.number: r for r in fleet.fleet_rows(snap, mx, PRIME, state, now=NOW)}
    local = "\n".join(fleet.relogin_steps(rows["3"], ssh=False, host="studio",
                                          claude_path="/opt/bin/claude", return_to=rows["1"]))
    remote = "\n".join(fleet.relogin_steps(rows["3"], ssh=True, host="studio",
                                           claude_path=None, return_to=rows["1"]))
    assert "/opt/bin/claude" in local and "/login" in local and "u3@x.com" in local
    assert "paste" not in local and "paste" in remote
    assert "claude" in remote
    assert "#1 main" in local and "slot 3" in local
    assert "paused" in local  # the engine pauses while this runs


def test_strategy_step_and_writes_list_only_changes_in_a_valid_order():
    saved = fleet.strategy_values(MX, PrimeSettings())
    edited = fleet.strategy_step(saved, "maximize.soft5h", 1)
    edited = fleet.strategy_step(edited, "maximize.soft5h", 1)
    edited = fleet.strategy_step(edited, "maximize.landingMargin", 1)
    edited = fleet.strategy_step(edited, "prime.enabled", 1)
    edited = fleet.strategy_step(edited, "prime.maxAttempts", 10)   # clamped to 5
    assert fleet.strategy_writes(saved, edited) == [
        ("maximize.soft5h", "52"), ("maximize.landingMargin", "6"),
        ("prime.enabled", "true"), ("prime.maxAttempts", "5"),
    ]
    # soft never steps past hard
    capped = fleet.strategy_step({**saved, "maximize.soft5h": 95.0}, "maximize.soft5h", 1)
    assert capped["maximize.soft5h"] == 95.0
    # raising both above the old hard: hard is written first
    raised = {**saved, "maximize.soft5h": 97.0, "maximize.hard5h": 98.0}
    assert fleet.strategy_writes(saved, raised) == [
        ("maximize.hard5h", "98"), ("maximize.soft5h", "97"),
    ]
    assert fleet.strategy_writes(saved, saved) == []


def test_publish_refresh_matches_the_engine():
    from claude_swap.maximize import engine_hook

    assert fleet.PUBLISH_REFRESH_S == engine_hook.PUBLISH_REFRESH_S


def test_over_ssh():
    assert fleet.over_ssh({"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"})
    assert fleet.over_ssh({"SSH_TTY": "/dev/ttys001"})
    assert not fleet.over_ssh({})
