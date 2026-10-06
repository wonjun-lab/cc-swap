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
from claude_swap.maximize import hold as account_hold
from claude_swap.maximize import home, policy
from claude_swap.maximize.model import Forecast, QuietWindow, Sample
from claude_swap.maximize.prime_verify import PausedView
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

#: What the screenshots' six accounts need: 26-character names (as wide as
#: the 23-character names plus the ``#4`` slot they used to carry), a ``team`` plan, ``1h47m · 07:10`` / ``3d19h · Oct 7
#: 02:18`` resets (``not started`` without the clock), ``login 1d left``.
NEEDS = home.TableNeeds(rows=6, name=26, plan=4, reset5=13, reset5_short=11,
                        reset7=19, reset7_short=5, status=13, detail=6)
ALWAYS = ("order", "account", "5h", "7d", "status")
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
    # 4. then the bars go, the percentages stay; the name is never cut …
    (90, (0, False, False, 1, 26)),
    (81, (0, False, False, 1, 26)),
    # … then the 7d resets go, then the 5h ones (the status never does).
    (80, (0, False, False, 1, 26)),
    (66, (0, False, False, 1, 26)),
])
def test_table_plan_gives_way_in_order(width, shape):
    plan = home.table_plan(width, 40, NEEDS)
    assert _shape(plan) == shape
    assert plan.room == width - home.MARGIN
    assert plan.total <= plan.room
    assert plan.keys == tuple(k for k in ORDER if k in plan.keys)
    assert (plan.width("5h"), plan.width("7d")) == ((plan.bar + 5 if plan.bar else 4),) * 2
    assert ("reset7" in plan.keys) is (width >= 81) and ("reset5" in plan.keys) is (width >= 71)
    if "reset5" in plan.keys:
        assert plan.width("reset5") == max(
            NEEDS.reset5 if plan.clock else NEEDS.reset5_short, 9)


@pytest.mark.parametrize("width", range(60, 221, 3))
@pytest.mark.parametrize("height", [8, 16, 24, 36, 45])
def test_table_plan_never_drops_order_names_or_status(width, height):
    plan = home.table_plan(width, height, NEEDS, attention=True)
    assert set(ALWAYS) <= set(plan.keys)
    assert plan.keys == tuple(k for k in ORDER if k in plan.keys)
    # The status column is the last, right after the column before it.
    before = plan.keys[plan.keys.index("status") - 1]
    assert plan.x("status") == plan.x(before) + plan.width(before) + plan.gap
    assert plan.x("status") + plan.width("status") == plan.total
    # The name is never cut, and the status is never what a clipped row
    # loses: the resets go first (7d, then 5h).
    assert plan.width("account") == NEEDS.name
    assert plan.total <= plan.room
    if "reset7" not in plan.keys:
        assert plan.bar == 0 and not plan.plan and not plan.clock
    if "reset5" not in plan.keys:
        assert "reset7" not in plan.keys
    assert plan.bar == 0 or home.MIN_BAR <= plan.bar <= home.MAX_BAR
    # The order the details give way in.
    if plan.gap == home.GAP:
        assert plan.clock and plan.plan
    if not plan.plan:
        assert not plan.clock
    # Wider never shows less.
    wider = home.table_plan(width + 1, height, NEEDS, attention=True)
    assert (wider.clock, wider.plan) >= (plan.clock, plan.plan)
    assert set(plan.keys) <= set(wider.keys)


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


def test_the_account_column_fits_the_longest_name_whole():
    def account(name: int, width: int = 220) -> int:
        return home.table_plan(width, 40, replace(NEEDS, name=name)).width("account")

    assert account(23) == 23
    assert account(32) == 32
    assert account(45) == 45              # never capped: a name is never cut
    assert account(2) == len("account")   # never narrower than its header
    assert account(26, 60) == 26          # not even when nothing else gives


def test_table_needs_measures_the_rows():
    rows = _fleet()[3]
    statuses = {r.number: home.status_for(r, is_next=False, now=NOW) for r in rows}
    long = replace(rows[1], name="team.shared@example.com", number="12")
    wide = replace(rows[2], name="업무 계정")  # two cells per Hangul syllable
    needs = home.table_needs([*rows, long, wide], statuses, now=NOW, detail=5)
    assert needs.rows == 8 and needs.detail == 5
    assert needs.name == len("team.shared@example.com")
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
    assert home.countdown(59) == "0h01m"
    assert home.countdown(47 * 60 + 30) == "0h47m"
    assert home.countdown(H + 47 * 60) == "1h47m"
    assert home.countdown(H + 5 * 60) == "1h05m"
    assert home.countdown(3 * DAY + 19 * H + 600) == "3d19h"
    assert home.countdown(3 * DAY + 2 * H) == "3d02h"
    assert len({len(home.countdown(x)) for x in (59, H, 5 * H, DAY, 6 * DAY + 23 * H)}) == 1
    assert home.countdown(2 * DAY) == "2d00h"


def test_resets_with_and_without_the_clock():
    now = _local(2026, 10, 3, 5, 23)
    soon = now + H + 47 * 60
    week = _local(2026, 10, 7, 2, 18)
    assert home.resets_text(soon, now, clock=True) == "1h47m · 07:10"
    assert home.resets_text(soon, now, clock=False) == "1h47m"
    assert home.resets_text(week, now, clock=True) == f"{home.countdown(week - now)} · Oct  7 02:18"
    assert home.resets_text(week, now, clock=False) == home.countdown(week - now)
    # A 5h window is never more than five hours away: no date.
    late = _local(2026, 10, 3, 23, 0)
    assert home.resets_text(late + 2 * H, late, clock=True, date=False).endswith(" · 01:00")
    assert home.resets_text(late + 2 * H, late, clock=True).endswith(" · Oct  4 01:00")
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
        # 5h: the countdown alone at every width, never the clock time.
        assert cells["1"][0] == home.countdown(2 * H)
        assert all(" · " not in a for a, _b in cells.values())
        assert cells["2"][0] == "not started"  # a cold 5h window
        assert cells["6"][0] == "not started"
        assert cells["3"] == ("—", "—")        # a dead login with no last reading
        assert (" · " in cells["1"][1]) is clock  # 7d keeps its clock while it fits


def test_the_5h_resets_column_is_as_wide_as_its_countdowns():
    rows = _fleet()[3]
    needs = home.table_needs(rows, {}, now=NOW)
    assert needs.reset5 == needs.reset5_short
    plan = home.table_plan(220, 40, needs)
    assert plan.clock and plan.width("reset5") == max(needs.reset5_short,
                                                      len(home.HEADERS["reset5"]))


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
    assert home.row_resets(rows["2"], "7d", NOW, clock=False) == "2d00h"
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
    """● the active account, 1 2 3 … exactly the engine's landing
    candidates in its order, · only when forced, – the rest."""
    _snap, _mx, _state, rows, msnap, picks = _fleet()
    by = _by(rows)
    # #5 excluded (as the engine's lists would then leave it out).
    rows = [replace(by["5"], tier="excluded", rank=None) if r.number == "5" else r for r in rows]
    picks = [n for n in picks if n != "5"]
    forced = {v.number for v in policy.escape_candidates(msnap)} - set(picks) - {"5"}
    ordered = home.ordered_rows(rows, picks, now=NOW, forced=forced)
    marks = home.order_marks(ordered, picks, forced)
    assert ordered[0].number == "1" and marks["1"] == "●"
    assert marks["3"] == "–" and marks["5"] == "–"  # a dead login, an excluded account
    numbered = [r.number for r in ordered if marks[r.number].isdigit()]
    assert numbered == picks
    assert [marks[n] for n in numbered] == [str(i) for i in range(1, len(numbered) + 1)]
    assert [r.number for r in ordered[1:1 + len(picks)]] == picks
    # Forced-only next, then the never-goes accounts.
    seq = [marks[r.number] for r in ordered]
    assert all(m in ("●", "·", "–") or m.isdigit() for m in seq)
    if "–" in seq:
        assert seq.index("–") > max(i for i, m in enumerate(seq) if m != "–")


def test_only_a_forced_move_gets_a_dot():
    row = _by(_fleet()[3])["2"]
    marks = home.order_marks([replace(row, number="7"), row], ["7"], {"2"})
    assert marks == {"7": "1", "2": "·"}
    assert home.order_marks([row], [], ()) == {"2": "–"}


def test_a_login_past_its_deadline_is_never_numbered():
    row = _by(_fleet()[3])["4"]
    assert not home.unusable(row, NOW)
    lapsed = replace(row, login_deadline=NOW - 60)
    assert home.unusable(lapsed, NOW) and not home.unusable(lapsed)  # needs now to tell
    assert home.order_marks([lapsed], [], ()) == {"4": "–"}
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
    assert home.TAG_PRIORITY[0] == "active" and home.TAG_PRIORITY[-1] == "prime"


def test_cold_tag_shows_the_prime_time_only_while_priming_runs():
    row = _by(_fleet()[3])["2"]  # cold, due now
    assert row.prime.kind == "due"
    text, tone = home.tag_for(row, is_next=False, now=NOW)
    assert text.startswith("prime ") and tone == "dim"
    assert home.tag_for(row, is_next=False, now=NOW, priming=False) == ("5h off", "dim")
    late = replace(row, prime=fx.PrimeCell("due", NOW - 120, NOW - 60, ""))
    assert home.tag_for(late, is_next=False, now=NOW) == ("prime now", "dim")
    window = replace(row, prime=fx.PrimeCell("window", NOW + H, NOW + H + 60, ""))
    assert home.tag_for(window, is_next=False, now=NOW)[0] == f"prime {fx.hhmm(NOW + H)}"
    # No window to speak of: an API key, a login cc-swap cannot read.
    for login in ("api", "foreign", "expired"):
        assert home.tag_for(replace(row, login=login), is_next=False, now=NOW) is None


def test_a_locked_keychain_and_an_old_reading_get_a_tag():
    row = replace(_by(_fleet()[3])["2"], state5="running")
    assert home.tag_for(replace(row, login="keychain"), is_next=True, now=NOW) == (
        "keychain locked (f)", "warn",
    )
    # Still trusted by the engine (its own cadence, a failure streak): dim,
    # from 15 minutes on; a few minutes behind is the usual cadence.
    old = replace(row, stale=True, fetched_at=NOW - 25 * 60)
    assert home.tag_for(old, is_next=False, now=NOW) == ("reading 25m old", "dim")
    assert home.tag_for(replace(row, stale=True, fetched_at=NOW - 6 * 60),
                        is_next=False, now=NOW) is None
    assert home.tag_for(old, is_next=True, now=NOW) == ("next", "accent")
    # No longer trusted: amber at any age, and never next.
    untrusted = replace(row, stale=True, fetched_at=NOW - 7 * 60, trusted=False)
    assert home.tag_for(untrusted, is_next=False, now=NOW) == ("reading 7m old", "warn")
    assert home.tag_for(untrusted, is_next=True, now=NOW) == ("reading 7m old", "warn")
    older = replace(row, stale=True, fetched_at=NOW - 2 * H, trusted=False)
    assert home.tag_for(older, is_next=True, now=NOW) == ("reading 2h old", "warn")


def test_the_status_column_gives_way_before_a_name_is_cut():
    """80 columns, the audit's six names and a locked keychain: the status
    column takes the shorter wording of the tags that are too wide, so no
    name is cut."""
    needs = replace(NEEDS, name=21, status=19, status_short=12)
    plan = home.table_plan(80, 24, needs)
    assert plan.width("account") == 21 and plan.short_status
    assert plan.width("status") < 19
    assert plan.status_text("keychain locked (f)") == "keychain (f)"
    assert plan.status_text("login 1d left") == "login 1d left"   # it fits as it is
    assert plan.status_text("reading 2h old") == "reading 2h old"
    # Room for everything: the full wording.
    wide = home.table_plan(160, 40, needs)
    assert not wide.short_status and wide.status_text("keychain locked (f)") == (
        "keychain locked (f)"
    )
    assert home.short_status("login 1d left") == "login 1d"
    assert home.short_status("reading 2h old") == "2h old"
    assert home.short_status("prime 19:30") == "prime 19:30"


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
        "Auto ON · using main · 5h 62% past soft 50 — will switch to side when you "
        "pause (forced at 98%, ~2h)"
    )
    assert variants[0][0] == ("Auto ON", "okb")
    lengths = [len(_plain(v)) for v in variants]
    assert lengths == sorted(lengths, reverse=True)
    assert _plain(variants[-1]) == "Auto ON"


def test_dry_run_says_would():
    text = _plain(_sentence(HERE_DRY, _pending(source="here"), "live")[0])
    assert text.startswith("Dry run · ") and "would switch to side" in text


@pytest.mark.parametrize(("sit", "es", "first"), [
    ("auto-off", replace(SERVICE, auto_off=True),
     "Auto OFF — nothing switches automatically (m to turn on)"),
    # No service status (unreadable, or a platform without the service):
    # the menu's Mode is all there is.
    ("no-engine", NONE, "Not switching — no engine is running (m to start one)"),
    ("no-engine", replace(NONE, service={"installed": False}),
     "Not switching — no engine is running (cc-swap service install starts one)"),
    ("no-engine", replace(NONE, service={"installed": True, "running": True}),
     "Not switching — the service is not switching (cc-swap service install restarts it)"),
    ("no-engine", replace(NONE, service={"installed": True, "running": False}),
     "Not switching — the service is stopped (cc-swap service install starts it)"),
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


def test_a_silent_service_says_what_restarts_it():
    dv = replace(_pending(), source="computed")
    variants = _sentence(SERVICE, dv, "stale", published_at=NOW - 40 * 60)
    texts = [_plain(v) for v in variants]
    assert texts[0].endswith(" · cc-swap service install restarts it")
    # 80 columns keep the remedy.
    at80 = _plain(home.fit_variant(variants, home.text_width(80)))
    assert at80 == f"Auto ON · engine silent since {fx.hhmm(NOW - 2400)} · cc-swap service " \
        "install restarts it"
    # Another process's engine: the service would not restart it.
    other = [_plain(v) for v in _sentence(OTHER, dv, "stale", published_at=NOW - 40 * 60)]
    assert not any("service install" in t for t in other)


def test_no_engine_points_at_the_service_not_at_an_engine_in_this_tui():
    stopped = replace(NONE, service={"installed": True, "running": False})
    variants = _sentence(stopped, _pending(), "no-engine")
    assert all("m to start" not in _plain(v) for v in variants)
    assert _plain(home.fit_variant(variants, home.text_width(80))) == (
        "Not switching — the service is stopped (cc-swap service install starts it)"
    )
    facts = "\n".join(fx.mode_facts(stopped))
    assert "To start it: cc-swap service install" in facts


def test_with_no_account_the_sentence_and_footer_say_how_to_add_one():
    first = _plain(home.empty_variants()[0])
    assert first == "No accounts yet — log in with claude, then press a, or run cc-swap add"
    assert len(first) <= home.text_width(80)
    assert home.key_hints(120, empty=True) == [("a", "add"), ("m", "menu"), ("?", "help"),
                                               ("q", "quit")]


def test_waiting_sentence_claims_no_decision():
    variants = _sentence(OTHER, replace(_pending(), source="computed"), "waiting")
    assert _plain(variants[0]) == "Auto ON · using main · waiting for the engine's next check"
    assert all("switch" not in _plain(v) for v in variants)


def test_paused_sentence():
    dv = fx.DecisionView("paused", "1", None, None, "relogin", at=NOW + 300)
    first = _plain(_sentence(SERVICE, dv, "paused")[0])
    assert first == "Paused · re-login in progress — nothing switches for 5m"


def test_hold_sentences_tell_all_fine_from_stuck_past_soft():
    calm = fx.DecisionView("hold", "4", None, None, "under soft", at=NOW - 10, source="engine")
    assert _plain(_sentence(SERVICE, calm, "live")[0]).startswith(
        "Auto ON · using work · all fine (5h 3%, moves on past 50%)"
    )
    stuck = fx.DecisionView("hold", "1", None, None, "nothing landable", at=NOW - 10,
                            source="engine")
    text = _plain(_sentence(SERVICE, stuck, "live")[0])
    assert "5h 62% past soft 50 — nowhere better to go yet" in text


def test_switch_exhausted_and_indeterminate_sentences():
    switch = fx.DecisionView("switch", "1", "2", "soft", "", at=NOW - 5, source="engine")
    assert _plain(_sentence(SERVICE, switch, "live")[0]) == (
        "Auto ON · switching main → side now (soft)"
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
        "Auto ON · using main · 5h 96% — resets in 8m, waiting it out "
        "(switches at once if it hits 100%)"
    )
    assert said[1] == (
        "Auto ON · main 5h 96% — resets in 8m, waiting it out (switches at once if it hits 100%)"
    )
    assert all("nowhere better" not in s and "forced" not in s for s in said)
    # The minutes count down from now, not from when the engine decided.
    later = home.status_variants(SERVICE, dv, rows, MX, "live", now=NOW + 5 * 60)
    assert "resets in 3m" in _plain(later[0])
    # The window reset since: the engine's own words, never a negative count.
    gone = replace(dv, waits=())
    assert _says(SERVICE, gone, rows)[0] == (
        "Auto ON · using main · 5h 96% — resets in 8m, waiting it out "
        "(switches at once if it hits 100%)"
    )


def test_a_preempt_hold_says_why_and_where_it_moves_at_the_next_pause():
    snap, state = _code_fleet(30, 84, 20)
    dv, rows = _decided(snap, state, forecast=QUIET_23, rates7={"1": 1.5})
    assert (dv.kind, dv.code, dv.target) == ("hold", "preempt", "2")
    said = _says(SERVICE, dv, rows)
    assert said[0] == (
        "Auto ON · using main · 7d 84% would pass 90% in ~4h, before your usual quiet "
        "time (23:00) — will move to side when you pause"
    )
    assert said[2] == "Auto ON · main 7d 84% would pass 90% in ~4h — to side on pause"
    assert _says(HERE_DRY, replace(dv, source="here"), rows)[0].startswith("Dry run · ")
    assert "would move to side" in _says(HERE_DRY, dv, rows)[0]
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
        "Auto ON · switching main → side now while you're idle — 7d 84% would pass "
        "90% in ~4h, before your usual quiet time (23:00)"
    )
    assert said[1] == "Auto ON · switching main → side now while you're idle (preempt)"
    assert _says(HERE_DRY, dv, rows)[0].startswith("Dry run · would switch main → side")


def test_a_deferred_rebalance_says_it_waits_for_your_quiet_time():
    snap, state = _code_fleet(30, 50, 45, idle=True)
    soon = replace(QUIET_23, next=replace(QUIET_23.next, start=NOW + 2 * H))
    dv, rows = _decided(snap, state, forecast=soon)
    assert (dv.kind, dv.code, dv.target) == ("hold", "rebalance-deferred", "2")
    said = _says(SERVICE, dv, rows)
    assert said[0] == (
        "Auto ON · using main · all fine — rebalance deferred to your quiet time (23:00) "
        "(side scores better; this is usually a busy time)"
    )
    assert "Auto ON · rebalance deferred to your quiet time (23:00)" in said
    assert home.next_number(dv, ["2"], "live") == "2"


def _tier_decided(state, mx, *, p7=30, other7=20):
    snap = accounts(
        acc(1, usage(30, p7, days7=3), active=True, alias="main"),
        acc(2, usage(10, other7, days7=3), alias="side"),
    )
    msnap = fx.fleet_snapshot(snap, mx, state, now=NOW)
    dv = replace(fx.preview_decision(msnap, mx), source="engine", at=NOW - 20)
    rows = fx.fleet_rows(snap, mx, PRIME, state, now=NOW)
    return dv, rows, [_plain(v) for v in home.status_variants(
        SERVICE, dv, rows, mx, "live", now=NOW)]


def test_a_pending_tier_move_names_where_it_goes():
    busy = _code_fleet(30, 30, 20)[1]
    mx = replace(MX, preferred="side")
    dv, rows, said = _tier_decided(busy, mx)
    assert (dv.kind, dv.code, dv.target) == ("hold", None, "2")
    assert said[0] == (
        "Auto ON · using main (normal) — will move up to side (preferred) when you pause"
    )
    assert "Auto ON · up to side on pause" in said
    assert not any("all fine" in s for s in said)
    # In the cooldown: it says it waits for that too.
    cool = replace(busy, last_switch_at=NOW - 600)
    dv, rows, said = _tier_decided(cool, mx)
    assert dv.target == "2"
    assert said[0].endswith(
        "will move up to side (preferred) when you pause after the cooldown (20m left)"
    )
    # A draining active account: not "draining it first" either.
    dv, rows, said = _tier_decided(busy, replace(mx, drain_hours=24 * 4), p7=88)
    assert rows[0].drain and dv.target == "2"
    assert said[0].startswith("Auto ON · using main (normal) — will move up to side")
    # Off a last-resort account to a normal one: the same words.
    dv, rows, said = _tier_decided(busy, replace(MX, last_resort="main"))
    assert said[0] == (
        "Auto ON · using main (last resort) — will move up to side (normal) when you pause"
    )
    # Never a slot number.
    assert not any("#" in s for s in said)


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


# -- the attention lines ----------------------------------------------------------------------------

KILLED = PausedView("killed", "paused: claude 2.1.4 is killed by the OS at launch (SIGKILL; see "
                    "cc-swap doctor)", True, version="2.1.4", system="macOS")
#: 80 columns lay text out in 77.
W80 = home.text_width(80)


def _attention(rows, width, lines=home.MAX_ATTENTION, **kw):
    return home.attention_lines(home.attention_notices(rows, now=NOW, **kw), width, lines)


def test_attention_names_dead_logins_first_then_expiring_ones():
    rows = _fleet()[3]
    by = _by(rows)
    rows = [replace(by["4"], login_deadline=NOW + 28 * H) if r.number == "4" else r
            for r in rows]
    dead = "! old needs re-login — select it, press r"
    # Wide: both on one line, red (a dead login).
    assert _attention(rows, 200) == [(f"{dead} · work login ends in 1d 4h", "crit")]
    # Narrower: a line each, every one saying what to do, each in its own colour.
    assert _attention(rows, 60) == [
        (dead, "crit"), ("! work login ends in 1d 4h — select it, press r", "warn"),
    ]
    # One line only: the dead login, the other when it fits after it, else
    # how many did not (unless saying so would cut the first note).
    assert _attention(rows, 60, lines=1) == [(f"{dead} (+1 more)", "crit")]
    assert _attention(rows, 33, lines=1) == [("! old needs re-login (+1 more)", "crit")]
    assert _attention(rows, 29, lines=1) == [("! old needs re-login", "crit")]
    assert _attention(rows, 20, lines=1) == [("! " + fx.clip("old needs re-login", 18),
                                              "crit")]


def test_attention_tone_and_extra_parts():
    rows = [r for r in _fleet()[3] if r.login != "relogin"]
    assert home.attention_notices(rows, now=NOW) == []
    soon = [replace(rows[1], login_deadline=NOW + 3 * DAY)]
    assert _attention(soon, 200) == [("! side login ends in 3d 0h — select it, press r",
                                      "warn")]
    guard = "paused: claude 2.1.3 -> 2.1.4 (cc-swap prime verify)"
    assert home.attention_notices(rows, now=NOW, prime_guard=guard, priming=False) == []
    assert _attention(rows, 200, prime_guard=guard, priming=True) == [
        (f"! priming {guard}", "warn"),
    ]
    (line, _tone), = _attention(rows, 200, linger_off=True)
    assert "loginctl enable-linger" in line


def test_a_killed_claude_comes_right_after_a_dead_login_and_keeps_its_remedy():
    """The note used to follow every login item on one line and be dropped:
    it now comes second, and every wording names cc-swap doctor."""
    rows = _fleet()[3]
    by = _by(rows)
    rows = [replace(by["4"], login_deadline=NOW + 28 * H) if r.number == "4" else r
            for r in rows]
    lines = _attention(rows, W80, prime_guard=KILLED, priming=True)
    assert [t for t, _ in lines] == [
        "! old needs re-login — select it, press r",
        "! claude 2.1.4 killed by macOS — priming paused (fix: cc-swap doctor)",
        "! work login ends in 1d 4h — select it, press r",
    ]
    assert _attention(rows, 117, prime_guard=KILLED, priming=True)[1][0] == (
        "! claude 2.1.4 killed by macOS at launch — priming paused (cc-swap doctor shows the fix)"
    )
    notice = home.guard_notice(KILLED, NOW)
    assert notice.alarm and all("cc-swap doctor" in v for v in notice.variants)
    # Two lines: the rest share the second, as much as fits.
    two = _attention(rows, W80, lines=2, prime_guard=KILLED, priming=True)
    assert len(two) == 2 and "killed by macOS" in two[1][0]


@pytest.mark.parametrize(("view", "width", "line", "alarm"), [
    # The engine re-verifies it: amber, no "!".
    (PausedView("changed", "n", True, version="2.1.4", previous="2.1.3"), W80,
     "priming paused: claude 2.1.3→2.1.4, re-verifying on its own", False),
    # Only you can: "!" and the command.
    (PausedView("changed", "n", False, version="2.1.4", previous="2.1.3"), 117,
     "! priming paused: claude 2.1.3→2.1.4 not verified yet — run cc-swap prime verify", True),
    (PausedView("changed", "n", False, version="2.1.4", previous="2.1.3"), W80,
     "! priming paused: claude 2.1.3→2.1.4 — run cc-swap prime verify", True),
    (PausedView("changed", "n", False, version="2.1.4", previous="2.1.3"), 45,
     "! priming paused: run cc-swap prime verify", True),
    (PausedView("failed", "n", False, version="2.1.4"), 60,
     "! priming paused: verify failed — run cc-swap prime verify", True),
    (PausedView("failed", "n", True, version="2.1.4"), W80,
     "priming paused: verify of claude 2.1.4 failed, retrying on its own", False),
    (PausedView("failed", "n", False), W80,
     "! priming paused: prime verify failed — run cc-swap prime verify", True),
    (PausedView("failed", "n", True), W80,
     "priming paused: prime verify failed, retrying on its own", False),
    # Settling: minutes, not seconds, and it resumes by itself.
    (PausedView("settle", "n", True, until=NOW + 412), W80,
     "priming paused: claude update settling, ~7m left, resumes by itself", False),
])
def test_priming_paused_is_worded_for_the_width(view, width, line, alarm):
    (text, tone), = _attention([], width, prime_guard=view, priming=True)
    assert text == line and tone == "warn"
    assert home.guard_notice(view, NOW).alarm is alarm


def test_a_relogin_pause_does_not_ask_for_a_relogin():
    rows = _fleet()[3]
    (line, _tone), = _attention(rows, 200, relogin_paused=True)
    assert line == "! old needs re-login"


def test_a_locked_keychain_says_how_to_unlock_it():
    rows = [replace(r, login="keychain") if r.number == "2" else r
            for r in _fleet()[3] if r.login != "relogin"]
    assert _attention(rows, 200) == [("! side keychain locked — unlock it, press f", "warn")]
    assert _attention(rows, 42) == [("! side keychain locked (f)", "warn")]


def test_attention_lines_take_only_rows_the_table_leaves():
    # 80x24 with six accounts and a six-line panel leaves five rows over.
    plan = home.table_plan(80, 24, NEEDS, attention=3, summary=True)
    assert plan.attention == 3 and plan.detail and plan.summary
    # Short: the rows and the panel keep theirs; one line only.
    plan = home.table_plan(80, 16, NEEDS, attention=3, summary=True)
    assert plan.detail and (plan.attention, plan.summary) == (1, False)
    # One row over: a second line, which comes before the summary.
    plan = home.table_plan(80, 17, NEEDS, attention=3, summary=True)
    assert plan.detail and (plan.attention, plan.summary) == (2, False)
    assert home.table_plan(80, 24, NEEDS, attention=False).attention == 0
    assert home.table_plan(80, 24, NEEDS, attention=True).attention == 1


@pytest.mark.parametrize("rows", range(13, 22))  # too many for the panel too
@pytest.mark.parametrize("want", [1, 2, 3])
def test_neither_attention_lines_nor_the_summary_make_a_fitting_table_scroll(rows, want):
    """17 accounts at 80x24 (no panel) fit the 18 rows under one attention
    line; a second line and the summary must not leave the table 16."""
    plan = home.table_plan(80, 24, replace(NEEDS, rows=rows), attention=want, summary=True)
    assert not plan.detail
    rest = 24 - (3 + 2) - plan.attention - int(plan.summary)  # what the table gets
    if rows <= 24 - (3 + 2) - 1:  # it fits with one attention line
        assert rest >= rows, (plan.attention, plan.summary)
    else:  # it scrolls anyway: the summary keeps its place, no extra lines
        assert plan.attention == 1 and plan.summary
    plan17 = home.table_plan(80, 24, replace(NEEDS, rows=17), attention=2, summary=True)
    assert (plan17.attention, plan17.summary) == (2, False)


# -- footer --------------------------------------------------------------------------------------------


def test_footer_is_eight_keys():
    full = home.key_hints(120)
    assert " · ".join(f"{k} {w}" for k, w in full) == (
        "enter switch · r login · l last · u first · h hold · m menu · ? help · q quit"
    )
    short = home.key_hints(50)
    assert [k for k, _ in short] == ["enter", "r", "l", "u", "h", "m", "?", "q"]
    assert render.keys_text(50, P).plain == "enter · r · l · u · h · m menu · ? help · q quit"
    assert render.keys_text(40, P).plain == "enter · r · l · u · h · m · ? · q"
    for width in (40, 50, 77):
        assert render.keys_text(width, P).cell_len <= width
    # The whole footer, both tier toggles labelled, fits an 80-column terminal.
    assert home.key_hints(home.text_width(80)) == list(home.KEY_HINTS)
    assert render.keys_text(home.text_width(80), P).plain == (
        "enter switch · r login · l last · u first · h hold · m menu · ? help · q quit"
    )


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
    snap, mock_mx, _state, rows, msnap, picks = _fleet()
    forced = frozenset(v.number for v in policy.escape_candidates(msnap)) - set(picks)
    ctx = render.Ctx(P, window_ticks(mx or mock_mx), NOW, next_no=next_no,
                     picks=tuple(picks), forced=forced)
    ordered = home.ordered_rows(rows, picks, now=NOW, forced=forced)
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
    marks = home.order_marks(ordered, ctx.picks, ctx.forced)
    for row, line in zip(ordered, lines):
        assert _cell(plan, line, "order") == marks[row.number]
        cell = _cell(plan, line, "account")
        assert "#" not in cell and row.name.startswith(cell.rstrip("…")), cell
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


def test_the_account_cell_never_cuts_the_name_and_shows_no_slot():
    long = "team.shared@example.com"
    snap = accounts(acc(1, active=True), acc(12, usage(5, 5)))
    rows = [replace(r, name=long) for r in fx.fleet_rows(snap, MX, PRIME, MaximizeState(),
                                                         now=NOW)]
    ctx = render.Ctx(P, window_ticks(MX), NOW)
    statuses = {r.number: ctx.status(r) for r in rows}
    for width in (160, 75, 50):
        plan = home.table_plan(width, 40, home.table_needs(rows, statuses, now=NOW))
        cells = [_cell(plan, line, "account")
                 for line in render.render_table(rows, plan, ctx, selected=None,
                                                 selected_bg="").text.plain.splitlines()]
        assert "#" not in "".join(cells) and "…" not in "".join(cells)
        assert cells[1] == long, width


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
    assert lines[1].startswith("main (u1@x.com)  personal · 20x  ● active")
    labels = [line.split()[0] for line in lines[2:-1]]
    assert labels == ["5h", "7d", "Fable"]
    assert lines[2].rstrip().endswith(home.exact_reset(row1.reset5, NOW))
    assert "resets " in lines[4] and "(in 3d00h)" in lines[4]
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


# -- the account hold sentence (cc-swap hold, h) ---------------------------------------------

HOLD =account_hold.AccountHold("1", NOW + 2 * H, NOW - 60, "fleet")


def _held_sentence(dv, *, hold=HOLD, sit="live", es=SERVICE, mx=None, hold_read=True):
    rows = _fleet()[3]
    mx = mx or replace(MX, hard_5h=98.0)
    return home.status_variants(es, dv, rows, mx, sit, now=NOW, hold=hold, hold_read=hold_read)


def test_a_hold_on_the_active_account_is_the_sentence():
    clock = account_hold.clock_text(NOW + 2 * H, NOW)
    variants = _held_sentence(_pending())
    assert _plain(variants[0]) == (
        f"Holding main until {clock} (2h left) — only hard 98%/100% will move you "
        "(h to change)"
    )
    assert variants[0][0] == ("Holding", "okb") and variants[0][-1] == ("(h to change)", "dim")
    # The wording the user asked for, at a narrower width.
    assert _plain(variants[1]) == (
        f"Holding main until {clock} (2h left) — only hard 98%/100% will move you"
    )
    lengths = [home.seg_len(v) for v in variants]
    assert lengths == sorted(lengths, reverse=True)
    assert _plain(variants[-1]) == "Holding main"
    different = _held_sentence(_pending(), mx=MX)
    assert "only a hard mark (5h 95%, 7d 98%) or 100% will move you" in _plain(different[0])


@pytest.mark.parametrize("dv", [
    fx.DecisionView("hold", "1", None, None, "under soft", at=NOW - 10, source="engine"),
    fx.DecisionView("hold", "1", "2", None, "#1 7d …", at=NOW - 10, source="engine",
                    code="preempt"),
    fx.DecisionView("hold", "1", None, None, "#1 x", at=NOW - 10, source="engine", code="hold"),
    replace(_pending(), source="computed"),
])
def test_the_hold_is_worded_for_every_decision_it_sets_aside(dv):
    sit = "waiting" if dv.source == "computed" else "live"
    assert _plain(_held_sentence(dv, sit=sit)[0]).startswith("Holding main until ")


@pytest.mark.parametrize("dv", [
    fx.DecisionView("switch", "1", "2", "hard", "#1 5h 99%", at=NOW - 5, source="engine"),
    fx.DecisionView("hold", "1", None, None, "#1 5h 96% — resets in 8m, waiting it out",
                    at=NOW - 5, source="engine", code="reset-wait"),
    fx.DecisionView("exhausted", "1", None, None, "", at=NOW - 5, source="engine"),
    fx.DecisionView("indeterminate", "1", None, None, "#1 usage unknown", at=NOW - 5,
                    source="engine"),
])
def test_safety_decisions_are_never_hidden_by_a_hold(dv):
    assert not _plain(_held_sentence(dv)[0]).startswith("Holding")


HARD_STAY = fx.DecisionView(
    "hold", "1", None, None,
    "#1 5h 99% >= hard 98%; nothing landable and no account under the hard caps has more "
    "5h room than #1; staying",
    at=NOW - 5, source="engine", code="hard-stay",
)


def test_past_hard_with_nowhere_roomier_is_worded_and_never_a_hold():
    for hold in (HOLD, None):  # held or not: the hard mark is what it is
        variants = _held_sentence(HARD_STAY, hold=hold)
        assert _plain(variants[0]) == (
            "Auto ON · using main · 5h 99% >= hard 98% — no account has more room, "
            "it stays (switches at once at 100%)"
        )
        assert not any("Holding" in _plain(v) or "only hard" in _plain(v) for v in variants)
        lengths = [home.seg_len(v) for v in variants]
        assert lengths == sorted(lengths, reverse=True)


@pytest.mark.parametrize(("hold", "sit", "es"), [
    (account_hold.AccountHold("2", NOW + H), "live", SERVICE),         # another slot
    (account_hold.AccountHold("1", NOW - 1), "live", SERVICE),         # ended
    (None, "live", SERVICE),
    (HOLD, "auto-off", replace(SERVICE, auto_off=True)),               # off wins
    (HOLD, "no-engine", NONE),
    (HOLD, "stale", SERVICE),
])
def test_no_hold_sentence_when_the_hold_does_not_apply(hold, sit, es):
    dv = replace(_pending(), kind="off") if sit == "auto-off" else _pending()
    assert not _plain(_held_sentence(dv, hold=hold, sit=sit, es=es)[0]).startswith("Holding")


def test_a_lifted_hold_is_not_worded_from_a_stale_engine_word():
    coded = fx.DecisionView("hold", "1", None, None, "#1 held until 15:30 (2h left) — x",
                            at=NOW - 10, source="engine", code="hold")
    assert not _plain(_held_sentence(coded, hold=None)[0]).startswith("Holding")
    # A caller that never read the marker words the engine's own code.
    said = _plain(_held_sentence(coded, hold=None, hold_read=False)[1])
    assert said == "Holding main until 15:30 (2h left) — only hard 98%/100% will move you"


def test_a_dry_run_hold_says_dry_run():
    first = _held_sentence(_pending(source="here"), es=HERE_DRY)[0]
    assert first[0] == ("Dry run", "warnb")
    assert _plain(first).startswith("Dry run · holding main until ")


@pytest.mark.parametrize("width", [157, 117, 77, 60, 40, 20])
def test_the_hold_sentence_never_exceeds_the_width(width):
    variants = _held_sentence(_pending())
    sentence, note = home.status_line(variants, home.holder_variants(SERVICE, "live"), width)
    assert home.seg_len(sentence) <= width
    assert render.status_text(sentence, note, width, P).cell_len <= width


# -- the capacity summary -------------------------------------------------------------------------


def _cap_row(n, pct5, pct7, *, reset5=None, reset7=None, login="ok", tier="normal",
             active=False, deadline=None) -> fx.FleetRow:
    return fx.FleetRow(
        number=str(n), name=f"acct{n}", email=f"acct{n}@example.com", org="personal",
        active=active, rank=1, plan="20x", tier=tier, pct5=pct5, pct7=pct7, days7=3.0,
        score=1.0, landable=True, land="yes", state5="running", reset5=reset5,
        prime=fx.PrimeCell("active", None, None, "—"), login=login, stale=False,
        login_deadline=deadline, reset7=reset7,
    )


CAP_ROWS = [
    _cap_row(1, 62, 40, reset5=NOW + 2 * H, reset7=NOW + 3 * DAY, active=True),  # past soft
    _cap_row(2, 10, 20, reset7=NOW + 2 * DAY),
    _cap_row(3, 0, 30, reset7=NOW + 5 * DAY),
    _cap_row(4, 70, 50, reset5=NOW + 1 * H, reset7=NOW + 4 * DAY),            # back first
    _cap_row(5, None, None, login="relogin", reset7=NOW + 0.5 * DAY),          # not counted
    _cap_row(6, 0, 0, tier="excluded", reset7=NOW + 0.2 * DAY),                # not counted
    _cap_row(7, None, None, login="api"),                                      # not counted
    _cap_row(8, 10, 99, reset5=NOW + 0.5 * H, reset7=NOW + 6 * DAY),           # week spent
    _cap_row(9, 0, 10, deadline=NOW - 60, reset7=NOW + 0.1 * DAY),             # login lapsed
]


def test_capacity_counts_the_accounts_switching_can_use():
    cap = home.capacity(CAP_ROWS, MX, NOW)
    assert cap.usable == 5                         # 1, 2, 3, 4, 8
    assert cap.free5 == 2                          # 2 and 3 (8's week is spent)
    assert cap.back5 == (NOW + 1 * H, "acct4")         # 1 is back later, 8's week is spent
    assert cap.left7 == pytest.approx((60 + 80 + 70 + 50 + 1) / 100)
    assert cap.next7 == (NOW + 2 * DAY, "acct2")       # the dead/excluded/lapsed ones skipped


def test_the_account_named_back_is_one_switching_could_land_on():
    """``next back`` follows the landing rule (both windows under soft less
    the margin), not ``7d under hard``: an account whose week is past its
    soft mark never comes back as a place to land."""
    mx = replace(MX, soft_7d=80.0, hard_7d=98.0, landing_margin=5.0)
    rows = [
        _cap_row(1, 20, 10, active=True),
        _cap_row(2, 70, 85, reset5=NOW + 0.5 * H, reset7=NOW + 3 * DAY),  # 7d past soft-5
        _cap_row(3, 48, 30, reset5=NOW + 1.5 * H, reset7=NOW + 4 * DAY),  # 5h past soft-5
    ]
    cap = home.capacity(rows, mx, NOW)
    assert cap.back5 == (NOW + 1.5 * H, "acct3")


def test_free_and_back_follow_one_rule():
    """Free and back never name the same account: 5h 47% is under soft 50
    but not under soft less the margin, so it is not free — it is back.
    The active account counts free under its soft marks and is never back;
    a login inside the expiry guard is neither."""
    mx = replace(MX, soft_5h=50.0, soft_7d=80.0, hard_7d=98.0, landing_margin=5.0,
                 login_expiry_guard_min=120)
    rows = [
        _cap_row(1, 47, 10, reset5=NOW + 0.2 * H, active=True),            # free (stays)
        _cap_row(2, 47, 10, reset5=NOW + 0.5 * H),                          # back, not free
        _cap_row(3, 10, 10, deadline=NOW + 1 * H, reset5=NOW + 0.1 * H),    # guarded
        _cap_row(4, 10, 10),                                                # free
        _cap_row(5, 60, 10, reset5=NOW + 0.3 * H, deadline=NOW + 1.5 * H),  # guarded at reset
    ]
    cap = home.capacity(rows, mx, NOW)
    assert cap.free5 == 2                       # #1 and #4
    assert cap.back5 == (NOW + 0.5 * H, "acct2")    # not #1 (active), #3/#5 (login guard)
    active_past = [replace(rows[0], pct5=55.0), rows[3]]
    cap = home.capacity(active_past, mx, NOW)
    assert cap.free5 == 1 and cap.back5 is None  # the active one is never named back


def test_capacity_with_nothing_to_count_is_none():
    assert home.capacity([_cap_row(5, None, None, login="relogin")], MX, NOW) is None
    assert home.capacity([], MX, NOW) is None


def test_the_summary_line_and_how_it_gives_way():
    cap = home.capacity(CAP_ROWS, MX, NOW)
    variants = [_plain(v) for v in home.summary_variants(cap, NOW)]
    # Countdowns, as the table's resets count; the 7d room goes first.
    assert variants == [
        "5h free: 2 accounts · next back in 1h00m (acct4) · 7d left this week ≈ 2.6 accounts"
        " · next 7d in 2d00h (acct2)",
        "5h free: 2 accounts · next back in 1h00m (acct4) · next 7d in 2d00h (acct2)",
        "5h free: 2 accounts · next back in 1h00m (acct4)",
        "5h free: 2 accounts",
    ]
    for width in (200, 90, 70, 40, 15):
        line = render.summary_text(cap, width, NOW, P)
        assert line.cell_len <= width or width < len(variants[-1])
    # 80 columns (77 to lay out in) keep both countdowns.
    assert render.summary_text(cap, 77, NOW, P).plain == variants[1]
    assert render.summary_text(cap, 60, NOW, P).plain == variants[2]
    assert render.summary_text(cap, 40, NOW, P).plain == variants[3]


def test_the_summary_says_none_free_in_amber_and_one_account_in_the_singular():
    spent = [_cap_row(1, 70, 40, reset5=NOW + H, active=True), _cap_row(2, 80, 20)]
    first = home.summary_variants(home.capacity(spent, MX, NOW), NOW)[0]
    assert first[1] == ("none", "warn")
    one = home.summary_variants(home.capacity([_cap_row(1, 10, 40, active=True)], MX, NOW),
                                NOW)
    assert _plain(one[0]).startswith("5h free: 1 account · 7d left this week ≈ 0.6 accounts")


@pytest.mark.parametrize(("size", "attention", "rows", "detail", "summary"), [
    ((160, 45), True, 6, True, True),
    ((80, 24), True, 6, True, True),
    ((200, 16), True, 6, True, False),    # the panel fits, the summary too would not: it goes
    ((200, 16), False, 6, True, True),    # no attention line: room for both
    ((200, 16), True, 7, False, True),    # no room for the panel anyway: the summary stays
    ((80, 12), True, 8, False, False),    # the table just fits: the summary would scroll it
    ((80, 12), True, 9, False, True),     # it scrolls anyway: the summary stays over it
    ((80, 11), True, 9, False, False),    # very short: never
    ((120, 8), True, 6, False, False),
])
def test_the_summary_goes_before_the_panel(size, attention, rows, detail, summary):
    plan = home.table_plan(*size, replace(NEEDS, rows=rows), attention=attention, summary=True)
    assert (plan.detail, plan.summary) == (detail, summary)
    # The panel is exactly what it would be without a summary.
    assert plan.detail == home.table_plan(*size, replace(NEEDS, rows=rows),
                                          attention=attention).detail
    assert not home.table_plan(*size, replace(NEEDS, rows=rows), attention=attention).summary
