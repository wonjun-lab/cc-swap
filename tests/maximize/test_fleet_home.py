"""maximize/home.py and tui/fleet_render.py: the Fleet home screen's pure
model (layout, order, tags, the status sentence, the attention line) and
its Rich renderers. No Textual app here."""

from __future__ import annotations

from dataclasses import replace

import pytest
from rich.text import Text

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import home, policy
from claude_swap.maximize.view import window_ticks
from claude_swap.settings import MaximizeSettings
from claude_swap.tui import fleet_render as render
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import bar_cells, bar_color
from tests.maximize.test_fleet import DAY, H, MX, NOW, PRIME, acc, accounts, mockup, usage

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


# -- layout -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("size", "mode", "columns"), [
    ((160, 45), "wide", 2),
    ((140, 30), "wide", 2),
    ((139, 45), "medium", 1),
    ((120, 36), "medium", 1),
    ((100, 30), "medium", 1),
    ((99, 45), "narrow", 1),
    ((90, 28), "narrow", 1),
    ((80, 24), "narrow", 1),
    ((200, 29), "narrow", 1),  # short: one line per account whatever the width
])
def test_layout_follows_the_terminal_size_only(size, mode, columns):
    layout = home.home_layout(*size)
    assert home.layout_mode(*size) == mode
    assert (layout.mode, layout.columns) == (mode, columns)


def test_layout_bars_and_blank_lines():
    assert home.home_layout(160, 45).max_bar == 34
    assert home.home_layout(120, 36).max_bar == 60
    assert home.home_layout(80, 24).blanks is True
    assert home.home_layout(80, 19).blanks is False
    wide = home.home_layout(160, 45)
    assert home.column_width(157, wide) == (157 - 4) // 2
    assert home.column_width(117, home.home_layout(120, 36)) == 117


def test_step_selection_moves_by_rows_and_columns_without_wrapping():
    order = ["1", "2", "4", "6", "3", "5"]
    assert home.step_selection(order, "1", "down") == "2"
    assert home.step_selection(order, "5", "down") == "5"
    assert home.step_selection(order, "1", "up") == "1"
    assert home.step_selection(order, "x", "down") == "1"
    assert home.step_selection([], None, "down") is None
    # Two columns, row by row: 1 2 / 4 6 / 3 5.
    assert home.step_selection(order, "1", "down", 2) == "4"
    assert home.step_selection(order, "4", "right", 2) == "6"
    assert home.step_selection(order, "6", "right", 2) == "6"
    assert home.step_selection(order, "6", "left", 2) == "4"
    assert home.step_selection(order, "3", "down", 2) == "3"
    assert home.step_selection(order, "5", "up", 2) == "6"
    # A shorter last row: down from the right column lands on its last block.
    assert home.step_selection(order[:5], "6", "down", 2) == "3"
    assert home.step_selection(order[:5], "3", "down", 2) == "3"
    assert home.step_selection(order, "2", "left", 1) == "2"  # one column: no sideways


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


def test_every_bar_and_percentage_uses_the_mark_colours():
    filled = bar_cells(62.0, 20, threshold=50.0, hard=98.0, palette=P)
    assert P.sev_warn in str(filled.spans[0].style)
    snap, mx, state, rows, _msnap, _picks = _fleet()
    ctx = render.Ctx(P, window_ticks(replace(mx, hard_5h=98.0)), NOW)
    accounts_by = {a.number: a for a in snap.accounts}
    block = render.block_lines(_by(rows)["1"], accounts_by["1"], 110, ctx, max_bar=40)
    assert P.sev_warn in _styles_of(block[1], "62%")   # 5h, past soft 50
    assert P.sev_ok in _styles_of(block[2], "41%")     # 7d, under soft 90
    widths = render.mini_widths(rows, 80, ctx)
    mini = render.mini_line(_by(rows)["1"], 80, ctx, widths)
    assert P.sev_warn in _styles_of(mini, "62%")


def test_a_selected_block_keeps_its_threshold_colours():
    snap, mx, state, rows, _msnap, picks = _fleet()
    ctx = render.Ctx(P, window_ticks(mx), NOW)
    accounts_by = {a.number: a for a in snap.accounts}
    for layout in (home.home_layout(160, 45), home.home_layout(120, 36)):
        body = render.render_blocks(
            home.ordered_rows(rows, picks), accounts_by, 117, ctx, layout,
            selected="1", selected_bg="on #262626",
        )
        styles = _styles_of(body.text, "62%")
        assert P.sev_warn in styles and "on #262626" in styles
    body = render.render_list(rows, 77, ctx, selected="1", selected_bg="on #262626")
    styles = _styles_of(body.text, "62%")
    assert P.sev_warn in styles and "on #262626" in styles


# -- rendering -------------------------------------------------------------------------------------------


def test_header_drops_the_email_then_the_org_and_keeps_the_tag():
    snap, mx, state, rows, _msnap, _picks = _fleet()
    row, a = _by(rows)["1"], {x.number: x for x in snap.accounts}["1"]
    ctx = render.Ctx(P, window_ticks(mx), NOW)
    wide = render.header_line(row, a, 80, ctx).plain
    assert wide.startswith(" 1  main (u1@x.com)  [personal] [20x]")
    assert wide.endswith("● active") and len(wide) == 80
    assert "(u1@x.com)" not in render.header_line(row, a, 34, ctx).plain
    assert "[personal]" not in render.header_line(row, a, 22, ctx).plain
    unknown_plan = render.header_line(_by(rows)["3"], None, 80, ctx).plain
    assert "[?]" not in unknown_plan


def test_blocks_put_two_accounts_side_by_side_when_wide():
    snap, mx, state, rows, _msnap, picks = _fleet()
    ctx = render.Ctx(P, window_ticks(mx), NOW)
    accounts_by = {a.number: a for a in snap.accounts}
    ordered = home.ordered_rows(rows, picks)
    wide = render.render_blocks(ordered, accounts_by, 157, ctx, home.home_layout(160, 45),
                                selected=None, selected_bg="")
    first = wide.text.plain.splitlines()[0]
    assert first.startswith(" 1  main") and f"{ordered[1].number}  " in first[70:]
    assert wide.spans["1"] == (0, 3) and wide.spans[ordered[1].number][0] == 0
    assert wide.number_at(0, 0) == "1" and wide.number_at(100, 1) == ordered[1].number
    assert wide.number_at(0, 999) is None
    medium = render.render_blocks(ordered, accounts_by, 117, ctx, home.home_layout(120, 36),
                                  selected=None, selected_bg="")
    lines = medium.text.plain.splitlines()
    assert lines[0].startswith(" 1  main") and lines[3] == ""
    assert all(len(line) <= 117 for line in lines)


def test_list_is_one_line_per_account_with_aligned_bars():
    snap, mx, state, rows, _msnap, picks = _fleet()
    ctx = render.Ctx(P, window_ticks(mx), NOW, next_no="2")
    ordered = home.ordered_rows(rows, picks)
    body = render.render_list(ordered, 77, ctx, selected="1", selected_bg="on #262626")
    lines = body.text.plain.splitlines()
    assert len(lines) == len(ordered) and all(len(line) == 77 for line in lines)
    assert lines[0].startswith("● 1 main") and lines[0].endswith("● active")
    assert "needs re-login" in next(line for line in lines if " old " in line)
    with_bars = [line for line in lines if " 5h " in line]
    assert len({line.index(" 7d ") for line in with_bars}) == 1
    assert next(line for line in lines if " side " in line).endswith("next")
    expanded = render.render_expanded(_by(rows)["1"], {a.number: a for a in snap.accounts}["1"],
                                      77, ctx, max_bar=40).plain.splitlines()
    assert expanded[0] == "─" * 77 and expanded[1].startswith(" 1  main")


def test_relogin_block_says_what_to_do():
    snap = accounts(acc(1, active=True), acc(3, sentinel=USAGE_RELOGIN_REQUIRED, alias="old"))
    rows = fx.fleet_rows(snap, MX, PRIME, mockup()[2], now=NOW)
    ctx = render.Ctx(P, window_ticks(MX), NOW)
    lines = render.block_lines(_by(rows)["3"], snap.accounts[1], 100, ctx, max_bar=40)
    assert lines[0].plain.rstrip().endswith("re-login (r)")
    assert lines[1].plain.strip() == "⚠ needs re-login — select it and press r"


def test_stale_reading_dims_the_bars_and_shows_its_age():
    snap = accounts(acc(1, usage(30, 20, age_s=900), active=True, alias="main"))
    rows = fx.fleet_rows(snap, MX, PRIME, mockup()[2], now=NOW)
    ctx = render.Ctx(P, window_ticks(MaximizeSettings()), NOW)
    lines = render.block_lines(rows[0], snap.accounts[0], 110, ctx, max_bar=40)
    assert "15m ago" in lines[0].plain
    assert "dim" in _styles_of(lines[1], "30%")
