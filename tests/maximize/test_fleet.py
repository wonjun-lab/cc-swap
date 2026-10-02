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
    text, tone = fleet.engine_line(es)
    assert "nothing is switching" in text and tone == "warn"
    stopped = {"platform": "linux", "installed": True, "running": False, "pid": None,
               "state": "inactive", "linger": False}
    es = fleet.engine_status(held_elsewhere=False, holder_pid=None, own=None, service=stopped)
    text, _ = fleet.engine_line(es)
    assert "service stopped (systemd: inactive)" in text and "linger off" in text


def test_status_lines_drop_suffixes_by_priority_when_narrow():
    snap, mx, state = mockup()
    rows = fleet.fleet_rows(snap, mx, PRIME, state, now=NOW)
    msnap = fleet.fleet_snapshot(snap, mx, state, now=NOW)
    dv = fleet.decision_view(state, msnap, now=NOW, poll_s=60)
    es = fleet.engine_status(held_elsewhere=True, holder_pid=4121, own=None,
                             service={"platform": "darwin", "running": True, "pid": 4121})
    wide = fleet.status_lines(es, dv, rows, mx, PRIME, now=NOW, width=140)
    assert [t.split()[0] for t, _ in wide] == ["engine", "now", "prime"]
    assert "holds the lease — this TUI is a viewer" in wide[0][0]
    assert "hard in ~1h50m" in wide[1][0] and "computed here" in wide[1][0]
    assert "#2" in wide[2][0] and "#3 needs re-login" in wide[2][0]
    narrow = fleet.status_lines(es, dv, rows, mx, PRIME, now=NOW, width=60)
    assert all(len(t) <= 60 for t, _ in narrow)
    assert narrow[1][0].startswith("now     HOLD — waiting for idle → #2")
    assert "hard in" not in narrow[1][0]  # the lowest-priority part went first
    head = fleet.header_line(mx, PRIME, rows, host="studio", ssh=True, now=NOW, width=112)
    assert head.startswith("cc-swap @ studio (ssh) · maximize · 5h 50/95 · 7d 90/98")
    assert "1 needs re-login" in head and len(head) == 112
    short = fleet.header_line(mx, PRIME, rows, host="studio", ssh=True, now=NOW, width=50)
    assert "1 needs re-login" in short and len(short) <= 50
    off = fleet.status_lines(es, dv, rows, mx, replace(PRIME, enabled=False), now=NOW, width=140)
    assert "priming off (s → Swap strategy)" in off[2][0]


def test_attention_names_every_relogin_account():
    snap, mx, state = mockup()
    rows = fleet.fleet_rows(snap, mx, PRIME, state, now=NOW)
    assert fleet.attention(rows) == (
        "⚠ #3 old needs re-login (refresh token dead) — select it and press r"
    )
    healthy = [r for r in rows if r.login != "relogin"]
    assert fleet.attention(healthy) is None


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
    assert fleet.attention([rows["2"]]) == (
        "⚠ #2 lapsed needs re-login (login expired) — select it and press r"
    )
    assert fleet.attention([rows["2"], rows["3"]]) == (
        "⚠ #2 lapsed (login expired), #3 dead (refresh token dead) need re-login"
        " — select one and press r"
    )
    steps = "\n".join(fleet.relogin_steps(rows["2"], ssh=False, host="h",
                                          claude_path=None, return_to=rows["1"]))
    assert "its login expired" in steps and "refresh token is dead" not in steps
    assert fleet.login_text(rows["2"]) == ("re-login needed (login expired)", "crit")
    assert fleet.login_text(rows["3"]) == ("re-login needed (refresh token dead)", "crit")


# -- layout -------------------------------------------------------------------------------


@pytest.mark.parametrize(("width", "missing"), [
    (140, set()),
    (112, set()),
    (100, {"rank", "7d in"}),
    (80, {"rank", "7d in", "plan", "tier", "login"}),
    (60, {"rank", "7d in", "plan", "tier", "login", "next prime"}),
])
def test_columns_for_widths(width, missing):
    cols = fleet.columns_for(width)
    assert set(fleet.ALL_COLUMNS) - set(cols) - {"5h win"} == missing | (
        {"5h window"} if width < 64 else set()
    )
    assert ("5h win" in cols) == (width < 64)


@pytest.mark.parametrize(("height", "detail", "menu", "prime_line", "keys"), [
    (40, True, "full", True, "full"),
    (32, True, "full", True, "full"),
    (24, False, "full", True, "full"),
    (18, False, "folded", True, "full"),
    (12, False, "folded", False, "minimal"),
])
def test_fit_layout_keeps_attention_rows_and_status_first(height, detail, menu, prime_line, keys):
    plan = fleet.fit_layout(height, 112, 6, attention=True)
    assert (plan.detail, plan.menu, plan.prime_line, plan.keys) == (detail, menu, prime_line, keys)
    assert plan.columns == fleet.columns_for(112)
    # More accounts take their rows from the optional parts first.
    assert fleet.fit_layout(height + 4, 112, 10, attention=True) == plan


def test_row_cells_colour_against_maximize_thresholds():
    snap, mx, state = mockup()
    rows = {r.number: r for r in fleet.fleet_rows(snap, mx, PRIME, state, now=NOW)}
    cols = fleet.columns_for(140)
    cells = dict(zip(cols, fleet.row_cells(rows["1"], cols, now=NOW, mx=mx)))
    assert cells["mark"] == ("*", "bold")
    assert cells["5h"] == ("62%", "warn") and cells["7d"] == ("41%", "ok")
    assert cells["5h window"][0] == f"running → {fleet.hhmm(NOW + 2 * H)}"
    relogin = dict(zip(cols, fleet.row_cells(rows["3"], cols, now=NOW, mx=mx)))
    assert relogin["5h"] == ("re-login", "crit") and relogin["land"] == ("re-login", "crit")
    assert relogin["account"][1] == "crit"
    narrow = fleet.columns_for(80)
    team = dict(zip(narrow, fleet.row_cells(rows["6"], narrow, now=NOW, mx=mx)))
    assert team["account"][0] == "team·LR"


def _expiring(number, seconds_left, **kw):
    return replace(acc(number, **kw), login_expires_at=(NOW + seconds_left) * 1000)


def test_login_cell_counts_down_amber_in_the_last_week_red_in_the_last_day():
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
    assert fleet.login_cell(rows["1"], NOW) == ("—", "dim")
    assert fleet.login_cell(rows["2"], NOW) == ("1d 9h", "warn")
    assert fleet.login_cell(rows["3"], NOW) == ("20h 0m", "crit")
    assert fleet.login_cell(rows["4"], NOW) == ("expired", "crit")
    assert fleet.login_cell(rows["5"], NOW) == ("20d", "dim")
    cols = fleet.columns_for(140)
    assert "login" in cols
    cells = dict(zip(cols, fleet.row_cells(rows["2"], cols, now=NOW, mx=MX)))
    assert cells["login"] == ("1d 9h", "warn")
    assert "login expires in 1d 9h" in fleet.detail_line(rows["2"], MX, now=NOW)
    assert "login" not in fleet.detail_line(rows["1"], MX, now=NOW)


def test_attention_warns_of_logins_expiring_within_a_week():
    snap = accounts(
        acc(1, active=True),
        _expiring(2, DAY + 9 * H, alias="side"),
        _expiring(5, 20 * DAY, alias="fine"),
    )
    rows = fleet.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)
    assert fleet.attention(rows, now=NOW) == (
        "⚠ #2 side login expires in 1d 9h — re-login before then: select it and press r"
    )
    assert fleet.attention_tone(rows, now=NOW) == "warn"
    assert fleet.attention(rows) is None  # no clock: only dead logins
    soon = fleet.fleet_rows(
        accounts(acc(1, active=True), _expiring(3, 20 * H, alias="soon"),
                 _expiring(4, 2 * DAY, alias="next")),
        MX, PRIME, MaximizeState(), now=NOW,
    )
    assert fleet.attention(soon, now=NOW) == (
        "⚠ logins expire: #3 soon in 20h 0m, #4 next in 2d 0h — "
        "re-login before then: select one and press r"
    )
    assert fleet.attention_tone(soon, now=NOW) == "crit"
    assert fleet.login_due(soon[1], NOW) and not fleet.login_due(rows[2], NOW)
    # A dead login leads; an expiring one rides along.
    mixed = fleet.fleet_rows(
        accounts(acc(1, active=True), _expiring(2, DAY + 9 * H, alias="side"),
                 acc(3, sentinel=USAGE_RELOGIN_REQUIRED, alias="old")),
        MX, PRIME, MaximizeState(), now=NOW,
    )
    assert fleet.attention(mixed, now=NOW) == (
        "⚠ #3 old needs re-login (refresh token dead) — select it and press r"
        " · #2 login expires in 1d 9h"
    )
    assert fleet.attention_tone(mixed, now=NOW) == "crit"


def test_detail_line_explains_rank_pace_and_landing():
    snap, mx, state = mockup()
    rows = {r.number: r for r in fleet.fleet_rows(snap, mx, PRIME, state, now=NOW)}
    line = fleet.detail_line(rows["4"], mx)
    assert line.startswith("rank 4 · 20x · pace 1.07 (78% left over 5.1d) · landable")
    assert "primed" in line


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
