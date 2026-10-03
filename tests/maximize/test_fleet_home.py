"""maximize/home.py and tui/fleet_render.py: the Fleet home screen's pure
model (the table plan, order, resets, tags, the status sentence, the
attention line) and its Rich renderers. No Textual app here."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from rich.text import Text

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import home, policy
from claude_swap.maximize.model import Forecast, QuietWindow, Sample
from claude_swap.maximize.view import MaximizeState, window_ticks
from claude_swap.settings import MaximizeSettings
from claude_swap.tui import fleet_render as render
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import bar_cells, bar_color
from claude_swap.usage_store import UsageEntry
from tests.maximize.test_fleet import (
    DAY, H, MX, NOW, PRIME, _iso, acc, accounts, mockup, usage,
)

P = Palette.DARK
SERVICE = fx.EngineStatus("service", 4121, {"running": True, "pid": 4121})
OTHER = fx.EngineStatus("other", 5521, None)
NONE = fx.EngineStatus("none", None, None)
HERE_DRY = fx.EngineStatus("here-dry", 7310, None)
HERE_LIVE = fx.EngineStatus("here-live", 7310, None)


def _fleet():
    snap, mx, state = mockup()
    rows = fx.fleet_rows(snap, mx, PRIME, state, now=NOW)
    msnap = fx.fleet_snapshot(snap, mx, state, now=NOW)
    picks = [v.number for v in policy.landing_candidates(msnap)]
    return snap, mx, state, rows, msnap, picks


def _by(rows) -> dict[str, fx.FleetRow]:
    return {r.number: r for r in rows}


def _pending(at: float = NOW - 30, source: str = "engine") -> fx.DecisionView:
    return fx.DecisionView(
        "pending", "1", "2", None, "#1 5h 62% >= soft 50%; waiting for idle to move to #2",
        window="5h", eta_hard_min=118.0, at=at, source=source,
    )


def _plain(segs) -> str:
    return "".join(text for text, _tone in segs)


# -- the table plan -----------------------------------------------------------------------------

#: What the screenshots' six accounts need: full 22-23 character emails and a
#: ``#4`` after each, a ``team`` plan, ``1h47m · 07:10`` / ``3d19h · Oct 7
#: 02:18`` resets (``not started`` without the clock), ``login 1d left``.
NEEDS = home.TableNeeds(rows=6, name=23, slot=3, plan=4, reset5=13, reset5_short=11,
                        reset7=19, reset7_short=5, status=13, detail=6)
ALWAYS = ("order", "account", "5h", "reset5", "7d", "reset7", "status")
ORDER = tuple(k for k, _h in home.COLUMNS)


def _shape(plan: home.TablePlan) -> tuple:
    return plan.bar, plan.clock, plan.plan, plan.gap, plan.width("account")


@pytest.mark.parametrize(("width", "shape"), [
    # Everything, bars at their longest; the table ends well short of the edge.
    (220, (24, True, True, 2, 26)),
    (200, (24, True, True, 2, 26)),
    (160, (24, True, True, 2, 26)),
    # 1. the bars shorten, down to 6 …
    (140, (16, True, True, 2, 26)),
    (120, (6, True, True, 2, 26)),
    # … the columns move closer …
    (116, (8, True, True, 1, 26)),
    # 2. the reset clocks go (the bars grow back into the room) …
    (110, (11, False, True, 1, 26)),
    (100, (6, False, True, 1, 26)),
    # 3. the plan column goes …
    (95, (6, False, False, 1, 26)),
    # 4. the name shortens with … (down to 13 cells) …
    (90, (6, False, False, 1, 21)),
    (85, (6, False, False, 1, 16)),
    # … then the bars go, the percentages stay and the name takes the room.
    (84, (0, False, False, 1, 26)),
    (80, (0, False, False, 1, 25)),
    (66, (0, False, False, 1, 11)),
])
def test_table_plan_gives_way_in_order(width, shape):
    plan = home.table_plan(width, 40, NEEDS)
    assert _shape(plan) == shape
    assert plan.room == width - home.MARGIN
    assert plan.total <= plan.room
    assert plan.keys == tuple(k for k in ORDER if k in plan.keys)
    assert (plan.width("5h"), plan.width("7d")) == ((plan.bar + 5 if plan.bar else 4),) * 2
    assert plan.width("reset5") == max(NEEDS.reset5 if plan.clock else NEEDS.reset5_short, 9)


@pytest.mark.parametrize("width", range(60, 221, 3))
@pytest.mark.parametrize("height", [8, 16, 24, 36, 45])
def test_table_plan_never_drops_order_resets_or_status(width, height):
    plan = home.table_plan(width, height, NEEDS, attention=True)
    assert set(ALWAYS) <= set(plan.keys)
    assert plan.keys == tuple(k for k in ORDER if k in plan.keys)
    # The status column starts right after 7d resets: never pushed to the edge.
    assert plan.x("status") == plan.x("reset7") + plan.width("reset7") + plan.gap
    assert plan.x("status") + plan.width("status") == plan.total
    if width >= 62:
        assert plan.total <= plan.room
    assert plan.bar == 0 or home.MIN_BAR <= plan.bar <= home.MAX_BAR
    # The order the details give way in.
    if plan.gap == home.GAP:
        assert plan.clock and plan.plan and plan.width("account") == NEEDS.name + NEEDS.slot
    if not plan.plan:
        assert not plan.clock
    if plan.width("account") < NEEDS.name + NEEDS.slot:
        assert not plan.plan and plan.gap == home.TIGHT_GAP
    # Wider never shows less.
    wider = home.table_plan(width + 1, height, NEEDS, attention=True)
    assert (wider.clock, wider.plan) >= (plan.clock, plan.plan)
    assert wider.width("account") >= plan.width("account") or plan.bar == 0


def test_table_is_narrower_than_a_wide_terminal():
    for width in (160, 200, 220):
        plan = home.table_plan(width, 40, NEEDS)
        assert plan.total < plan.room  # the status column never meets the edge
    assert home.table_plan(220, 40, NEEDS).total == home.table_plan(160, 40, NEEDS).total


@pytest.mark.parametrize(("size", "attention", "rows", "detail", "blanks"), [
    ((160, 45), True, 6, True, True),
    ((80, 24), True, 6, True, True),
    ((200, 16), True, 6, True, False),   # wide but short: still the table, panel fits
    ((200, 16), True, 7, False, False),  # one more row: the panel goes first
    ((200, 16), False, 7, True, False),  # no attention line: room again
    ((80, 14), False, 6, False, False),
    ((80, 19), False, 6, True, False),
    ((80, 20), True, 6, True, True),
    ((120, 8), True, 6, False, False),
])
def test_the_panel_goes_first_on_a_short_terminal(size, attention, rows, detail, blanks):
    plan = home.table_plan(*size, replace(NEEDS, rows=rows), attention=attention)
    assert (plan.detail, plan.blanks) == (detail, blanks)
    assert set(ALWAYS) <= set(plan.keys)  # never a different layout
    assert not home.table_plan(*size, replace(NEEDS, rows=rows, detail=0)).detail


def test_the_account_column_fits_the_longest_name_up_to_32():
    def account(name: int, width: int = 220) -> int:
        return home.table_plan(width, 40, replace(NEEDS, name=name)).width("account")

    assert account(23) == 23 + 3
    assert account(32) == 32 + 3
    assert account(45) == 32 + 3          # capped
    assert account(2) == len("account")   # never narrower than its header
    assert account(23, 80) < 23 + 3       # cut with … only when nothing else gives


def test_table_needs_measures_the_rows():
    rows = _fleet()[3]
    statuses = {r.number: home.status_for(r, is_next=False, now=NOW) for r in rows}
    long = replace(rows[1], name="team.shared@example.com", number="12")
    wide = replace(rows[2], name="업무 계정")  # two cells per Hangul syllable
    needs = home.table_needs([*rows, long, wide], statuses, now=NOW, detail=5)
    assert needs.rows == 8 and needs.detail == 5
    assert needs.name == len("team.shared@example.com")
    assert needs.slot == len(" #12")
    assert home.cells("업무 계정") == 9
    assert needs.reset5_short == len("not started")
    assert needs.status == max(len(s[0]) for s in statuses.values() if s)


def test_step_selection_moves_one_row_without_wrapping():
    order = ["1", "2", "4", "6", "3", "5"]
    assert home.step_selection(order, "1", "down") == "2"
    assert home.step_selection(order, "2", "up") == "1"
    assert home.step_selection(order, "5", "down") == "5"
    assert home.step_selection(order, "1", "up") == "1"
    assert home.step_selection(order, "x", "down") == "1"
    assert home.step_selection(order, "4", "left") == "4"
    assert home.step_selection([], None, "down") is None


# -- when the windows reset -------------------------------------------------------------------


def _local(*ymdhm: int) -> float:
    return time.mktime((*ymdhm, 0, 0, 0, -1))


def test_countdowns_are_compact():
    assert home.countdown(59) == "1m"
    assert home.countdown(47 * 60 + 30) == "47m"
    assert home.countdown(H + 47 * 60) == "1h47m"
    assert home.countdown(H + 5 * 60) == "1h05m"
    assert home.countdown(3 * DAY + 19 * H + 600) == "3d19h"
    assert home.countdown(2 * DAY) == "2d0h"


def test_resets_with_and_without_the_clock():
    now = _local(2026, 10, 3, 5, 23)
    soon = now + H + 47 * 60
    week = _local(2026, 10, 7, 2, 18)
    assert home.resets_text(soon, now, clock=True) == "1h47m · 07:10"
    assert home.resets_text(soon, now, clock=False) == "1h47m"
    assert home.resets_text(week, now, clock=True) == f"{home.countdown(week - now)} · Oct 7 02:18"
    assert home.resets_text(week, now, clock=False) == home.countdown(week - now)
    # A 5h window is never more than five hours away: no date.
    late = _local(2026, 10, 3, 23, 0)
    assert home.resets_text(late + 2 * H, late, clock=True, date=False).endswith(" · 01:00")
    assert home.resets_text(late + 2 * H, late, clock=True).endswith(" · Oct 4 01:00")
    assert home.resets_text(None, now, clock=True) == "—"
    assert home.resets_text(now - 5, now, clock=True) == "now"
    assert home.exact_reset(soon, now) == "resets 07:10 (in 1h47m)"
    assert home.exact_reset(week, now).startswith("resets Oct 7 02:18 (in 3d")


def test_every_row_says_when_both_windows_reset():
    rows = _by(_fleet()[3])
    for clock in (True, False):
        cells = {n: (home.row_resets(r, "5h", NOW, clock=clock),
                     home.row_resets(r, "7d", NOW, clock=clock)) for n, r in rows.items()}
        assert all(a and b for a, b in cells.values())
        assert cells["1"][0].startswith(home.countdown(2 * H))
        assert cells["2"][0] == "not started"  # a cold 5h window
        assert cells["6"][0] == "not started"
        assert cells["3"] == ("—", "—")        # a dead login with no last reading
        assert (" · " in cells["1"][1]) is clock


def test_a_dead_login_keeps_the_resets_of_its_last_reading():
    dead = UsageEntry(
        sentinel=USAGE_RELOGIN_REQUIRED,
        last_good={"five_hour": {"pct": 30, "resets_at": _iso(NOW + 2 * H)},
                   "seven_day": {"pct": 50, "resets_at": _iso(NOW + 2 * DAY)}},
        fetched_at=NOW - 3 * H, age_s=3 * H,
    )
    gone = replace(dead, last_good={**dead.last_good,
                                    "five_hour": {"pct": 30, "resets_at": _iso(NOW - H)}})
    snap = accounts(acc(1, active=True), acc(2, dead), acc(3, gone))
    rows = _by(fx.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW))
    assert rows["2"].login == "relogin"
    assert home.row_resets(rows["2"], "5h", NOW, clock=False) == "2h00m"
    assert home.row_resets(rows["2"], "7d", NOW, clock=False) == "2d0h"
    assert home.row_resets(rows["3"], "5h", NOW, clock=False) == "—"  # already past


# -- order and tags ---------------------------------------------------------------------------


def test_order_is_active_then_the_engine_picks_then_the_rest_excluded_last():
    _snap, _mx, _state, rows, _msnap, picks = _fleet()
    excluded = replace(_by(rows)["5"], tier="excluded", rank=None)
    rows = [excluded if r.number == "5" else r for r in rows]
    order = [r.number for r in home.ordered_rows(rows, picks)]
    assert order[0] == "1"
    assert order[1:1 + len(picks)] == picks
    assert order[-1] == "5"
    assert sorted(order) == ["1", "2", "3", "4", "5", "6"]


def test_order_rest_is_by_rank_with_unknown_rank_after():
    _snap, _mx, _state, rows, _msnap, _picks = _fleet()
    order = [r.number for r in home.ordered_rows(rows, [])]
    by = _by(rows)
    ranked = [n for n in order[1:] if by[n].rank is not None]
    assert ranked == sorted(ranked, key=lambda n: by[n].rank)
    assert order[-1] == "3"  # re-login: rank unknown


def test_the_order_column_numbers_where_switching_goes():
    """● the active account, 1 2 3 … in the display order (the engine's
    picks first), – where switching never goes."""
    _snap, _mx, _state, rows, _msnap, picks = _fleet()
    by = _by(rows)
    rows = [replace(by["5"], tier="excluded", rank=None) if r.number == "5" else r for r in rows]
    ordered = home.ordered_rows(rows, picks, now=NOW)
    marks = home.order_marks(ordered, NOW)
    assert ordered[0].number == "1" and marks["1"] == "●"
    assert marks["3"] == "–" and marks["5"] == "–"  # a dead login, an excluded account
    numbered = [marks[r.number] for r in ordered if marks[r.number] not in ("●", "–")]
    assert numbered == [str(i) for i in range(1, len(numbered) + 1)]
    assert [r.number for r in ordered[1:1 + len(picks)]] == picks
    assert all(marks[n] == str(i) for i, n in enumerate(picks, 1))
    # The never-goes accounts come after every numbered one.
    seq = [marks[r.number] for r in ordered]
    assert seq.index("–") > max(i for i, m in enumerate(seq) if m not in ("●", "–"))


def test_a_login_past_its_deadline_is_never_numbered():
    row = _by(_fleet()[3])["4"]
    assert not home.unusable(row, NOW)
    lapsed = replace(row, login_deadline=NOW - 60)
    assert home.unusable(lapsed, NOW) and not home.unusable(lapsed)  # needs now to tell
    assert home.order_marks([lapsed], NOW) == {"4": "–"}
    ordered = home.ordered_rows([lapsed, replace(row, number="7", rank=9)], [], now=NOW)
    assert [r.number for r in ordered] == ["7", "4"]


def test_the_status_column_falls_back_to_primed():
    row = _by(_fleet()[3])["4"]  # primed, nothing else to say
    assert row.state5 == "primed"
    assert home.tag_for(row, is_next=False, now=NOW) is None
    assert home.status_for(row, is_next=False, now=NOW) == ("primed", "dim")
    assert home.status_for(row, is_next=True, now=NOW) == ("next", "accent")  # the tag wins
    assert home.status_for(replace(row, state5="running"), is_next=False, now=NOW) is None


def test_tags_follow_their_priority():
    base = _by(_fleet()[3])["2"]
    login_soon = NOW + 3 * DAY
    cases = [
        (replace(base, active=True, login="relogin"), True, ("● active", "active")),
        (replace(base, login="relogin", tier="excluded"), True, ("re-login (r)", "crit")),
        (replace(base, tier="excluded"), True, ("excluded", "dim")),
        (replace(base, login_deadline=login_soon, tier="last_resort"), True, ("next", "accent")),
        (replace(base, login_deadline=login_soon, tier="last_resort"), False,
         ("login 3d left", "warn")),
        (replace(base, login_deadline=NOW + 5 * H), False, ("login 5h left", "crit")),
        (replace(base, login_deadline=NOW - 60), False, ("login expired", "crit")),
        (replace(base, tier="last_resort"), False, ("last resort", "dim")),
    ]
    for row, is_next, expected in cases:
        assert home.tag_for(row, is_next=is_next, now=NOW) == expected, (row, is_next)
    assert home.tag_for(replace(base, state5="running"), is_next=False, now=NOW) is None
    assert home.TAG_PRIORITY[0] == "active" and home.TAG_PRIORITY[-1] == "5h off"


def test_cold_tag_shows_the_prime_time_only_while_priming_runs():
    row = _by(_fleet()[3])["2"]  # cold, due now
    assert row.prime.kind == "due"
    text, tone = home.tag_for(row, is_next=False, now=NOW)
    assert text.startswith("5h off · prime ") and tone == "dim"
    assert home.tag_for(row, is_next=False, now=NOW, priming=False) == ("5h off", "dim")
    late = replace(row, prime=fx.PrimeCell("due", NOW - 120, NOW - 60, ""))
    assert home.tag_for(late, is_next=False, now=NOW) == ("5h off · prime now", "dim")
    window = replace(row, prime=fx.PrimeCell("window", NOW + H, NOW + H + 60, ""))
    assert home.tag_for(window, is_next=False, now=NOW)[0] == f"5h off · prime {fx.hhmm(NOW + H)}"
    # No window to speak of: an API key, a login cc-swap cannot read.
    for login in ("api", "keychain", "foreign", "expired"):
        assert home.tag_for(replace(row, login=login), is_next=False, now=NOW) is None


# -- situation ---------------------------------------------------------------------------------


def _sit(es, dv, *, published_at=None, active="1"):
    return home.situation(es, dv, active=active, published_at=published_at, now=NOW, poll_s=60)


def test_situation_never_calls_an_old_or_foreign_decision_live():
    computed = replace(_pending(), source="computed")
    assert _sit(SERVICE, _pending()) == "live"
    assert _sit(SERVICE, _pending(source="here")) == "live"
    assert _sit(SERVICE, _pending(), active="2") == "waiting"  # about another account
    assert _sit(SERVICE, _pending(at=NOW - 3600, source="here")) == "stale"
    assert _sit(SERVICE, computed, published_at=NOW - 3600) == "stale"
    assert _sit(OTHER, computed, published_at=NOW - 30) == "waiting"
    assert _sit(SERVICE, computed, published_at=None) == "waiting"
    assert _sit(HERE_DRY, computed, published_at=NOW - 3600) == "waiting"  # ours, starting
    assert _sit(NONE, _pending()) == "no-engine"
    assert _sit(replace(SERVICE, auto_off=True), replace(_pending(), kind="off")) == "auto-off"
    paused = fx.DecisionView("paused", "1", None, None, "relogin", at=NOW + 300)
    assert _sit(replace(SERVICE, auto_off=True), paused) == "paused"


def test_prime_times_show_only_while_an_engine_primes():
    assert home.priming_runs(True, SERVICE, "live")
    assert home.priming_runs(True, HERE_LIVE, "waiting")
    assert not home.priming_runs(False, SERVICE, "live")
    assert not home.priming_runs(True, HERE_DRY, "live")  # a dry run never primes
    assert not home.priming_runs(True, SERVICE, "live", guard="paused: claude 2.1.3 -> 2.1.4")
    for sit in ("stale", "no-engine", "auto-off", "paused"):
        assert not home.priming_runs(True, SERVICE, sit)


def test_next_is_only_named_while_switching_is_live():
    dv = _pending()
    assert home.next_number(dv, ["4", "6"], "live") == "2"  # the decision's target
    assert home.next_number(replace(dv, kind="hold", target=None), ["4", "6"], "waiting") == "4"
    for sit in ("stale", "no-engine", "auto-off", "paused"):
        assert home.next_number(dv, ["4"], sit) is None
    assert home.next_number(replace(dv, kind="hold"), [], "live") is None


# -- the status sentence -------------------------------------------------------------------------


def _sentence(es, dv, sit, *, published_at=None, mx=None):
    rows = _fleet()[3]
    mx = mx or replace(MX, hard_5h=98.0)
    return home.status_variants(es, dv, rows, mx, sit, now=NOW, published_at=published_at)


def test_live_pending_sentence_names_both_accounts_the_mark_and_the_force():
    variants = _sentence(SERVICE, _pending(), "live")
    assert _plain(variants[0]) == (
        "Auto ON · using #1 main · 5h 62% past soft 50 — will switch to #2 side when you "
        "pause (forced at 98%, ~2h)"
    )
    assert variants[0][0] == ("Auto ON", "okb")
    lengths = [len(_plain(v)) for v in variants]
    assert lengths == sorted(lengths, reverse=True)
    assert _plain(variants[-1]) == "Auto ON"


def test_dry_run_says_would():
    text = _plain(_sentence(HERE_DRY, _pending(source="here"), "live")[0])
    assert text.startswith("Dry run · ") and "would switch to #2" in text


@pytest.mark.parametrize(("sit", "es", "first"), [
    ("auto-off", replace(SERVICE, auto_off=True),
     "Auto OFF — nothing switches automatically (m to turn on)"),
    ("no-engine", NONE, "Not switching — no engine is running (m to start one)"),
])
def test_off_sentences(sit, es, first):
    dv = replace(_pending(), kind="off") if sit == "auto-off" else _pending()
    variants = _sentence(es, dv, sit)
    assert _plain(variants[0]) == first
    assert all("will switch" not in _plain(v) for v in variants)


def test_stale_sentence_says_since_when_and_claims_nothing():
    dv = replace(_pending(), source="computed")
    variants = _sentence(SERVICE, dv, "stale", published_at=NOW - 25 * 60)
    first = _plain(variants[0])
    assert first.startswith(f"Auto ON · the engine has not reported since {fx.hhmm(NOW - 1500)}")
    assert "(25m ago)" in first and "nothing below is live" in first
    assert variants[0][0] == ("Auto ON", "warnb")
    assert all("switch to" not in _plain(v) for v in variants)


def test_waiting_sentence_claims_no_decision():
    variants = _sentence(OTHER, replace(_pending(), source="computed"), "waiting")
    assert _plain(variants[0]) == "Auto ON · using #1 main · waiting for the engine's next check"
    assert all("switch" not in _plain(v) for v in variants)


def test_paused_sentence():
    dv = fx.DecisionView("paused", "1", None, None, "relogin", at=NOW + 300)
    first = _plain(_sentence(SERVICE, dv, "paused")[0])
    assert first == f"Paused · re-login in progress — nothing switches until {fx.hhmm(NOW + 300)}"


def test_hold_sentences_tell_all_fine_from_stuck_past_soft():
    calm = fx.DecisionView("hold", "4", None, None, "under soft", at=NOW - 10, source="engine")
    assert _plain(_sentence(SERVICE, calm, "live")[0]).startswith(
        "Auto ON · using #4 work · all fine (5h 3%, moves on past 50%)"
    )
    stuck = fx.DecisionView("hold", "1", None, None, "nothing landable", at=NOW - 10,
                            source="engine")
    text = _plain(_sentence(SERVICE, stuck, "live")[0])
    assert "5h 62% past soft 50 — nowhere better to go yet" in text


def test_switch_exhausted_and_indeterminate_sentences():
    switch = fx.DecisionView("switch", "1", "2", "soft", "", at=NOW - 5, source="engine")
    assert _plain(_sentence(SERVICE, switch, "live")[0]) == (
        "Auto ON · switching #1 main → #2 side now (soft)"
    )
    spent = fx.DecisionView("exhausted", "1", None, None, "", at=NOW - 5, source="engine")
    assert "every account is at its limit" in _plain(_sentence(SERVICE, spent, "live")[0])
    lost = fx.DecisionView("indeterminate", "1", None, None, "#1 usage unknown", at=NOW,
                           source="engine")
    assert _plain(_sentence(SERVICE, lost, "live")[0]).startswith("Auto ON · #1 usage unknown")


# -- the hold codes (reset-wait, preempt, rebalance-deferred) and a preempt switch ---------------
#
# The decisions come from the real policy (``fx.preview_decision`` on a
# snapshot with a forecast and burn rates), so the sentence is pinned to the
# policy's own wording: a reason the sentence can no longer read fails here.


def _code_fleet(p5, p7, other7, *, reset5=None, idle=False):
    snap = accounts(
        acc(1, usage(p5, p7, reset5=reset5, days7=3), active=True, alias="main"),
        acc(2, usage(10, other7, days7=3), alias="side"),
    )
    a5 = p5 if idle else p5 - 0.5 if reset5 else p5 - 3
    state = MaximizeState(samples_account="1", samples=(
        Sample(NOW - 660, a5, p7), Sample(NOW - 60, p5, p7),
    ))
    return snap, state


def _decided(snap, state, *, forecast=None, rates7=None, source="engine"):
    msnap = fx.fleet_snapshot(snap, MX, state, now=NOW)
    msnap = replace(msnap, forecast=forecast, rates7=rates7 or {})
    dv = replace(fx.preview_decision(msnap, MX), source=source, at=NOW - 20)
    rows = fx.fleet_rows(snap, MX, PRIME, state, now=NOW)
    return dv, rows


def _says(es, dv, rows, sit="live") -> list[str]:
    return [_plain(v) for v in home.status_variants(es, dv, rows, MX, sit, now=NOW)]


QUIET_23 = Forecast(days=9, p_busy_now=0.8, current=None,
                    next=QuietWindow(NOW + 5 * H, NOW + 13 * H, "23:00", "07:30"))


def test_a_reset_wait_hold_says_it_waits_the_reset_out():
    snap, state = _code_fleet(96, 40, 20, reset5=NOW + 8 * 60 + 20)
    dv, rows = _decided(snap, state)
    assert (dv.kind, dv.code) == ("hold", "reset-wait")
    said = _says(SERVICE, dv, rows)
    assert said[0] == (
        "Auto ON · using #1 main · 5h 96% — resets in 8m, waiting it out "
        "(switches at once if it hits 100%)"
    )
    assert said[1] == (
        "Auto ON · #1 5h 96% — resets in 8m, waiting it out (switches at once if it hits 100%)"
    )
    assert all("nowhere better" not in s and "forced" not in s for s in said)
    # The minutes count down from now, not from when the engine decided.
    later = home.status_variants(SERVICE, dv, rows, MX, "live", now=NOW + 5 * 60)
    assert "resets in 3m" in _plain(later[0])
    # The window reset since: the engine's own words, never a negative count.
    gone = replace(dv, waits=())
    assert _says(SERVICE, gone, rows)[0] == (
        "Auto ON · using #1 main · 5h 96% — resets in 8m, waiting it out "
        "(switches at once if it hits 100%)"
    )


def test_a_preempt_hold_says_why_and_where_it_moves_at_the_next_pause():
    snap, state = _code_fleet(30, 84, 20)
    dv, rows = _decided(snap, state, forecast=QUIET_23, rates7={"1": 1.5})
    assert (dv.kind, dv.code, dv.target) == ("hold", "preempt", "2")
    said = _says(SERVICE, dv, rows)
    assert said[0] == (
        "Auto ON · using #1 main · 7d 84% would pass 90% in ~4h, before your usual quiet "
        "time (23:00) — will move to #2 side when you pause"
    )
    assert said[2] == "Auto ON · #1 7d 84% would pass 90% in ~4h — to #2 on pause"
    assert _says(HERE_DRY, replace(dv, source="here"), rows)[0].startswith("Dry run · ")
    assert "would move to #2" in _says(HERE_DRY, dv, rows)[0]
    # In the rebalance cooldown it names no target and says it waits.
    cool = replace(state, last_switch_at=NOW - 600)
    dv, rows = _decided(snap, cool, forecast=QUIET_23, rates7={"1": 1.5})
    assert (dv.code, dv.target) == ("preempt", None)
    assert _says(SERVICE, dv, rows)[0].endswith(
        "— will move to the next account when you pause after the cooldown (20m left)"
    )


def test_a_preempt_switch_says_it_moves_early_while_you_are_idle():
    snap, state = _code_fleet(30, 84, 20, idle=True)
    dv, rows = _decided(snap, state, forecast=QUIET_23, rates7={"1": 1.5})
    assert (dv.kind, dv.trigger, dv.target) == ("switch", "preempt", "2")
    said = _says(SERVICE, dv, rows)
    assert said[0] == (
        "Auto ON · switching #1 main → #2 side now while you're idle — 7d 84% would pass "
        "90% in ~4h, before your usual quiet time (23:00)"
    )
    assert said[1] == "Auto ON · switching #1 main → #2 side now while you're idle (preempt)"
    assert _says(HERE_DRY, dv, rows)[0].startswith("Dry run · would switch #1 main → #2")


def test_a_deferred_rebalance_says_it_waits_for_your_quiet_time():
    snap, state = _code_fleet(30, 50, 45, idle=True)
    soon = replace(QUIET_23, next=replace(QUIET_23.next, start=NOW + 2 * H))
    dv, rows = _decided(snap, state, forecast=soon)
    assert (dv.kind, dv.code, dv.target) == ("hold", "rebalance-deferred", "2")
    said = _says(SERVICE, dv, rows)
    assert said[0] == (
        "Auto ON · using #1 main · all fine — rebalance deferred to your quiet time (23:00) "
        "(#2 side scores better; this is usually a busy time)"
    )
    assert "Auto ON · rebalance deferred to your quiet time (23:00)" in said
    assert home.next_number(dv, ["2"], "live") == "2"


def test_every_hold_code_has_its_own_words():
    """Parity with ``cc-swap why``: every code a hold can carry
    (``model.HoldCode``, doctor_cli.REASONS) is worded on the home screen,
    never as the generic "nowhere better to go" or "all fine" hold."""
    from typing import get_args

    from claude_swap.maximize.model import HoldCode
    from claude_swap.maximize.view import HOLD_CODES

    assert HOLD_CODES == set(get_args(HoldCode))
    rows = _fleet()[3]
    generic = _says(SERVICE, fx.DecisionView("hold", "1", None, None, "x", at=NOW,
                                             source="engine"), rows)
    for code in sorted(HOLD_CODES):
        dv = fx.DecisionView("hold", "1", "2", None, "#1 x", at=NOW, source="engine", code=code)
        said = _says(SERVICE, dv, rows)
        assert said != generic, code
        assert all("nowhere better" not in s for s in said), code


@pytest.mark.parametrize("width", [157, 117, 87, 77, 60, 40, 20])
def test_hold_code_sentences_never_exceed_the_width(width):
    cases = [
        _decided(*_code_fleet(96, 40, 20, reset5=NOW + 8 * 60 + 20)),
        _decided(*_code_fleet(30, 84, 20), forecast=QUIET_23, rates7={"1": 1.5}),
        _decided(*_code_fleet(30, 84, 20, idle=True), forecast=QUIET_23, rates7={"1": 1.5}),
    ]
    for dv, rows in cases:
        variants = home.status_variants(SERVICE, dv, rows, MX, "live", now=NOW)
        lengths = [home.seg_len(v) for v in variants]
        assert lengths == sorted(lengths, reverse=True), dv.code or dv.trigger
        sentence, note = home.status_line(variants, home.holder_variants(SERVICE, "live"), width)
        assert home.seg_len(sentence) <= width
        assert render.status_text(sentence, note, width, P).cell_len <= width


def test_holder_note_says_who_switches_and_whether_it_is_idle():
    assert home.holder_variants(SERVICE, "live")[0] == "viewer · service pid 4121 is switching"
    assert home.holder_variants(OTHER, "waiting")[0] == "viewer · pid 5521 is switching"
    assert home.holder_variants(SERVICE, "auto-off")[0] == "viewer · service pid 4121 is idle"
    assert home.holder_variants(SERVICE, "paused")[0] == "viewer · service pid 4121 is paused"
    assert home.holder_variants(SERVICE, "stale")[0] == "viewer · service pid 4121 is silent"
    assert home.holder_variants(HERE_LIVE, "live")[0] == "engine runs here · quitting stops it"
    assert home.holder_variants(HERE_DRY, "live")[0] == "engine here · dry run"
    assert home.holder_variants(NONE, "no-engine") == [""]


@pytest.mark.parametrize("width", [157, 117, 87, 77, 60, 40, 20])
def test_status_line_never_exceeds_the_width(width):
    variants = _sentence(SERVICE, _pending(), "live")
    sentence, note = home.status_line(variants, home.holder_variants(SERVICE, "live"), width)
    used = home.seg_len(sentence)
    assert used <= width
    if note:
        assert used + 3 + len(note) <= width
    line = render.status_text(sentence, note, width, P)
    assert line.cell_len <= width


def test_status_line_keeps_the_note_when_there_is_room():
    variants = _sentence(SERVICE, _pending(), "live")
    sentence, note = home.status_line(variants, home.holder_variants(SERVICE, "live"), 157)
    assert _plain(sentence).endswith("(forced at 98%, ~2h)")
    assert note == "viewer · service pid 4121 is switching"


# -- the attention line -----------------------------------------------------------------------------


def test_attention_names_dead_logins_first_then_expiring_ones():
    rows = _fleet()[3]
    by = _by(rows)
    rows = [replace(by["4"], login_deadline=NOW + 28 * H) if r.number == "4" else r
            for r in rows]
    parts, tone = home.attention_parts(rows, now=NOW)
    assert parts[0] == "! #3 old needs re-login — select it, press r"
    assert parts[1] == "#4 work login ends in 1d 4h"
    assert tone == "crit"
    assert home.attention_line(parts, 200) == " · ".join(parts)
    assert home.attention_line(parts, 50) == parts[0]
    assert len(home.attention_line(parts, 30)) == 30


def test_attention_tone_and_extra_parts():
    rows = [r for r in _fleet()[3] if r.login != "relogin"]
    assert home.attention_parts(rows, now=NOW) is None
    soon = [replace(rows[1], login_deadline=NOW + 3 * DAY)]
    parts, tone = home.attention_parts(soon, now=NOW)
    assert parts == ["! #2 side login ends in 3d 0h — select it, press r"] and tone == "warn"
    guard = "paused: claude 2.1.3 -> 2.1.4 (cc-swap prime verify)"
    assert home.attention_parts(rows, now=NOW, prime_guard=guard, priming=False) is None
    parts, tone = home.attention_parts(rows, now=NOW, prime_guard=guard, priming=True)
    assert parts == [f"! priming {guard}"] and tone == "warn"
    parts, _ = home.attention_parts(rows, now=NOW, linger_off=True)
    assert "loginctl enable-linger" in parts[0]


# -- footer --------------------------------------------------------------------------------------------


def test_footer_is_six_keys():
    full = home.key_hints(120)
    assert " · ".join(f"{k} {w}" for k, w in full) == (
        "enter switch · r re-login · l last resort · m menu · ? help · q quit"
    )
    short = home.key_hints(40)
    assert [k for k, _ in short] == ["enter", "r", "l", "m", "?", "q"]
    assert render.keys_text(40, P).cell_len <= 40


# -- colours -------------------------------------------------------------------------------------------


def test_bar_colour_follows_the_soft_and_hard_marks():
    assert bar_color(62.0, threshold=50.0, hard=98.0, palette=P) == P.sev_warn  # was green
    assert bar_color(49.0, threshold=50.0, hard=98.0, palette=P) == P.sev_ok
    assert bar_color(50.0, threshold=50.0, hard=98.0, palette=P) == P.sev_warn
    assert bar_color(98.0, threshold=50.0, hard=98.0, palette=P) == P.sev_crit
    # Without both marks: upstream's fixed 70/90 ramp.
    assert bar_color(62.0, threshold=50.0, palette=P) == P.sev_ok
    assert bar_color(75.0, palette=P) == P.sev_warn
    assert bar_color(None, palette=P) == P.muted


def _styles_of(text: Text, needle: str) -> str:
    start = text.plain.index(needle)
    return " ".join(
        str(span.style) for span in text.spans if span.start <= start < span.end
    )


def _table(width: int, *, height: int = 40, selected: str | None = None, next_no="2",
           mx: MaximizeSettings | None = None):
    """The mockup fleet as the home screen draws it at ``width``: (plan,
    header line, body, the ordered rows, ctx, accounts by number)."""
    snap, mock_mx, _state, rows, _msnap, picks = _fleet()
    ctx = render.Ctx(P, window_ticks(mx or mock_mx), NOW, next_no=next_no)
    ordered = home.ordered_rows(rows, picks, now=NOW)
    statuses = {r.number: ctx.status(r) for r in ordered}
    by = {a.number: a for a in snap.accounts}
    first = ordered[0]
    detail = render.detail_height(first, by[first.number], ctx)
    plan = home.table_plan(width, height, home.table_needs(ordered, statuses, now=NOW,
                                                           detail=detail))
    body = render.render_table(ordered, plan, ctx, selected=selected,
                               selected_bg="on #262626")
    return plan, render.table_header(plan, P), body, ordered, ctx, by


def _cell(plan: home.TablePlan, line: str, key: str) -> str:
    x = plan.x(key)
    return line[x:x + plan.width(key)].strip()


def test_every_bar_and_percentage_uses_the_mark_colours():
    filled = bar_cells(62.0, 20, threshold=50.0, hard=98.0, palette=P)
    assert P.sev_warn in str(filled.spans[0].style)
    for width in (160, 120, 80, 70):  # with bars, short bars, the percentage only
        plan, _head, body, ordered, _ctx, _by_n = _table(width, mx=replace(MX, hard_5h=98.0))
        line = body.text.split("\n")[0]
        assert P.sev_warn in _styles_of(line, "62%")   # 5h, past soft 50
        assert P.sev_ok in _styles_of(line, "41%")     # 7d, under soft 90


def test_a_selected_row_keeps_its_threshold_colours():
    for width in (160, 120, 80):
        _plan, _head, body, _rows, _ctx, _by_n = _table(width, selected="1")
        styles = _styles_of(body.text, "62%")
        assert P.sev_warn in styles and "on #262626" in styles
    _plan, _head, body, _rows, _ctx, _by_n = _table(160, selected="2")
    assert "on #262626" not in _styles_of(body.text, "62%")


# -- rendering -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("width", [220, 160, 120, 100, 80, 70])
def test_the_table_has_a_dim_header_over_every_column(width):
    plan, head, body, ordered, _ctx, _by_n = _table(width)
    text = head.plain
    assert P.muted in str(head.style)
    for key in plan.keys:
        assert _cell(plan, text, key) == home.HEADERS[key], key
    assert text.split()[:2] == ["order", "account"]
    lines = body.text.plain.splitlines()
    assert len(lines) == len(ordered) and all(len(line) == plan.room for line in lines)


@pytest.mark.parametrize("width", [220, 160, 120, 100, 80])
def test_every_row_shows_its_order_both_resets_and_its_status_after_them(width):
    plan, _head, body, ordered, ctx, _by_n = _table(width)
    lines = body.text.plain.splitlines()
    marks = home.order_marks(ordered, NOW)
    for row, line in zip(ordered, lines):
        assert _cell(plan, line, "order") == marks[row.number]
        assert _cell(plan, line, "account").endswith(f"#{row.number}")
        assert _cell(plan, line, "reset5") == home.row_resets(row, "5h", NOW, clock=plan.clock)
        assert _cell(plan, line, "reset7") == home.row_resets(row, "7d", NOW, clock=plan.clock)
        status = ctx.status(row)
        if status:
            # The tag starts right after 7d resets, and is the row's last word.
            assert line[plan.x("status"):].rstrip() == status[0]
            assert line.rstrip().endswith(status[0])
    tags = {r.number: _cell(plan, line, "status") for r, line in zip(ordered, lines)}
    assert tags["1"] == "● active" and tags["2"] == "next" and tags["3"] == "re-login (r)"
    assert tags["4"] == "primed" and tags["6"] == "last resort"
    if plan.total < plan.room:  # a table narrower than the terminal ends before it
        assert all(len(line.rstrip()) < plan.room for line in lines)


def test_the_account_cell_cuts_the_name_but_keeps_the_slot():
    long = "team.shared@example.com"
    snap = accounts(acc(1, active=True), acc(12, usage(5, 5)))
    rows = [replace(r, name=long) for r in fx.fleet_rows(snap, MX, PRIME, MaximizeState(),
                                                         now=NOW)]
    ctx = render.Ctx(P, window_ticks(MX), NOW)
    statuses = {r.number: ctx.status(r) for r in rows}
    for width, whole in ((160, True), (80, False)):
        plan = home.table_plan(width, 40, home.table_needs(rows, statuses, now=NOW))
        cells = [_cell(plan, line, "account")
                 for line in render.render_table(rows, plan, ctx, selected=None,
                                                 selected_bg="").text.plain.splitlines()]
        assert cells[1].endswith(" #12") and cells[0].endswith(" #1")
        assert (cells[1] == f"{long} #12") is whole
        if not whole:
            assert "…" in cells[1] and cells[1].startswith("team.shared")


def test_a_dead_login_spans_its_bars_with_what_to_do():
    plan, _head, body, ordered, _ctx, _by_n = _table(160)
    line = body.text.plain.splitlines()[[r.number for r in ordered].index("3")]
    assert _cell(plan, line, "5h") == "⚠ needs re-login"
    assert _cell(plan, line, "7d") == "select it, press r"
    assert _cell(plan, line, "status") == "re-login (r)"
    assert _cell(plan, line, "order") == "–"
    for width, bar, words in ((80, 6, ("⚠ re-login", "press r")), (70, 0, ("⚠", ""))):
        narrow, _h, body, _o, _c, _b = _table(width)
        line = body.text.plain.splitlines()[[r.number for r in ordered].index("3")]
        assert narrow.bar == bar
        assert (_cell(narrow, line, "5h"), _cell(narrow, line, "7d")) == words, width
        assert "…" not in line


def test_a_click_selects_the_row_under_it():
    _plan, _head, body, ordered, _ctx, _by_n = _table(120)
    assert body.spans[ordered[2].number] == (2, 1)
    assert body.number_at(0, 2) == ordered[2].number
    assert body.number_at(100, 0) == ordered[0].number
    assert body.number_at(0, 999) is None


def test_the_panel_shows_the_selected_account_in_full():
    snap, mx, _state, rows, _msnap, _picks = _fleet()
    by_n = {a.number: a for a in snap.accounts}
    ctx = render.Ctx(P, window_ticks(mx), NOW, priming=True)
    # The active account with a per-model window: it shows here, not in the table.
    fable = UsageEntry(
        last_good={**by_n["1"].usage.last_good,
                   "scoped": [{"name": "Fable", "pct": 38.0,
                               "resets_at": _iso(NOW + 3 * DAY)}]},
        fetched_at=NOW - 5, age_s=5.0,
    )
    a1 = replace(by_n["1"], usage=fable)
    row1 = replace(_by(rows)["1"], login_deadline=NOW + 21 * DAY)
    lines = render.render_detail(row1, a1, 117, ctx).plain.splitlines()
    assert lines[0] == "─" * 117
    assert lines[1].startswith("main (u1@x.com) #1  personal · 20x  ● active")
    labels = [line.split()[0] for line in lines[2:-1]]
    assert labels == ["5h", "7d", "Fable"]
    assert lines[2].rstrip().endswith(home.exact_reset(row1.reset5, NOW))
    assert "resets " in lines[4] and "(in 3d0h)" in lines[4]
    assert lines[-1].strip().startswith("login ends ") and "(in 21d 0h)" in lines[-1]
    assert render.detail_height(row1, a1, ctx) == len(lines)
    statuses = {r.number: ctx.status(r) for r in rows}
    assert "Fable" not in "".join(home.HEADERS.values())
    assert home.table_needs(rows, statuses, now=NOW).rows == len(rows)
    # Primed, and the next prime time of a cold account.
    primed = render.render_detail(_by(rows)["4"], by_n["4"], 117, ctx).plain
    assert "· primed" in primed and "5h opened by priming" in primed
    cold = _by(rows)["2"]
    text = render.render_detail(cold, by_n["2"], 117, ctx).plain
    assert "not started" in text and f"next prime {fx.prime_text(cold.prime)}" in text
    off = render.render_detail(cold, by_n["2"], 117, replace(ctx, priming=False)).plain
    assert "priming not running" in off


def test_the_panel_says_what_a_dead_login_needs():
    snap = accounts(acc(1, active=True), acc(3, sentinel=USAGE_RELOGIN_REQUIRED, alias="old"))
    rows = fx.fleet_rows(snap, MX, PRIME, mockup()[2], now=NOW)
    ctx = render.Ctx(P, window_ticks(MX), NOW)
    lines = render.render_detail(_by(rows)["3"], snap.accounts[1], 100, ctx).plain.splitlines()
    assert lines[1].rstrip().endswith("re-login (r)")
    assert lines[2].strip() == "⚠ needs re-login — select it and press r"
    assert lines[-1].strip() == "re-login needed (refresh token dead)"


def test_stale_reading_dims_the_bars_and_shows_its_age():
    snap = accounts(acc(1, usage(30, 20, age_s=900), active=True, alias="main"))
    rows = fx.fleet_rows(snap, MX, PRIME, mockup()[2], now=NOW)
    ctx = render.Ctx(P, window_ticks(MaximizeSettings()), NOW)
    plan = home.table_plan(120, 40, home.table_needs(rows, {}, now=NOW))
    line = render.table_row(rows[0], "●", plan, ctx)
    assert "dim" in _styles_of(line, "30%")
    detail = render.render_detail(rows[0], snap.accounts[0], 110, ctx).plain
    assert "reading 15m old" in detail
