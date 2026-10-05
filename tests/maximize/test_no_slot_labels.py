"""No user-facing line names an account by its slot number (``#3``,
``Account-3``) when the account has a name (maximize/names.py): the render
functions of Fleet, the policy's reasons, the engine's log lines, the
notifications and the CLI helpers, driven with fakes."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from claude_swap import autoswitch as aw
from claude_swap import oauth, shared_login
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import hold as account_hold
from claude_swap.maximize import home, notify
from claude_swap.maximize.history_cli import entry_line
from claude_swap.maximize.model import Forecast, QuietWindow, Sample
from claude_swap.maximize.view import MaximizeState, window_ticks
from claude_swap.tui import fleet_render as render
from claude_swap.tui.theme import Palette
from tests.maximize.test_fleet import DAY, H, MX, NOW, PRIME, acc, accounts, mockup, usage

#: A slot label: ``#3`` / ``Account-3`` / ``account 3`` (not ``#fx-…`` ids).
SLOT_LABEL = re.compile(r"(?<![\w&])#\d+\b|Account-\d|\baccount \d")
SERVICE = fx.EngineStatus("service", 4121, {"running": True, "pid": 4121})


def _clean(texts) -> None:
    for text in texts:
        assert not SLOT_LABEL.search(text), text


def _plain(segs) -> str:
    return "".join(t for t, _ in segs)


def _scenarios():
    """Fleets whose decisions cover switch, pending, preempt, reset-wait,
    rebalance-deferred, hard-stay, exhausted."""
    quiet = Forecast(days=9, p_busy_now=0.8, current=None,
                     next=QuietWindow(NOW + 5 * H, NOW + 13 * H, "23:00", "07:30"))
    soon = replace(quiet, next=replace(quiet.next, start=NOW + 2 * H))
    out = []
    for p5, p7, other7, reset5, idle, forecast, rates in (
        (62, 40, 20, None, False, None, None),          # past soft: pending
        (62, 40, 20, None, True, None, None),           # idle: switch
        (96, 40, 20, NOW + 8 * 60 + 20, False, None, None),   # reset-wait
        (30, 84, 20, None, False, quiet, {"1": 1.5}),   # preempt hold
        (30, 84, 20, None, True, quiet, {"1": 1.5}),    # preempt switch
        (30, 50, 45, None, True, soon, None),           # rebalance deferred
        (99, 40, 99, None, False, None, None),          # hard, nowhere roomier
        (100, 100, 100, None, False, None, None),       # exhausted
    ):
        snap = accounts(
            acc(1, usage(p5, p7, reset5=reset5, days7=3), active=True, alias="main"),
            acc(2, usage(10 if other7 < 99 else 99, other7, days7=3), alias="side"),
        )
        a5 = p5 if idle else p5 - 0.5 if reset5 else p5 - 3
        state = MaximizeState(samples_account="1", samples=(
            Sample(NOW - 660, a5, p7), Sample(NOW - 60, p5, p7)))
        msnap = fx.fleet_snapshot(snap, MX, state, now=NOW)
        msnap = replace(msnap, forecast=forecast, rates7=rates or {})
        dv = replace(fx.preview_decision(msnap, MX), source="engine", at=NOW - 20)
        out.append((snap, state, msnap, dv, fx.fleet_rows(snap, MX, PRIME, state, now=NOW)))
    return out


SCENARIOS = _scenarios()


@pytest.mark.parametrize("case", range(len(SCENARIOS)))
def test_the_policy_reason_and_every_home_sentence_use_names(case):
    _snap, _state, _msnap, dv, rows = SCENARIOS[case]
    assert "main" in dv.reason or dv.kind == "exhausted", dv.reason
    _clean([dv.reason])
    for dry in (SERVICE, fx.EngineStatus("here-dry", 7310, None)):
        _clean(_plain(v) for v in home.status_variants(dry, dv, rows, MX, "live", now=NOW))
    parts, _tone = fx.decision_parts(dv, now=NOW)
    _clean(text for text, _p in parts)
    hold = account_hold.AccountHold("1", NOW + 2 * H)
    _clean(_plain(v) for v in home.status_variants(
        SERVICE, dv, rows, MX, "live", now=NOW, hold=hold, hold_read=True))


def test_the_table_attention_summary_and_panel_use_names():
    snap, mx, state = mockup()
    rows = fx.fleet_rows(snap, mx, PRIME, state, now=NOW)
    rows = [replace(r, login="keychain") if r.number == "2" else r for r in rows]
    rows = [replace(r, login_deadline=NOW + DAY) if r.number == "4" else r for r in rows]
    ctx = render.Ctx(Palette.DARK, window_ticks(mx), NOW)
    statuses = {r.number: ctx.status(r) for r in rows}
    for width in (220, 120, 80, 60):
        plan = home.table_plan(width, 40, home.table_needs(rows, statuses, now=NOW))
        table = render.render_table(rows, plan, ctx, selected=None, selected_bg="")
        _clean(table.text.plain.splitlines())
        notes = home.attention_notices(rows, now=NOW)
        _clean(t for t, _ in home.attention_lines(notes, width, home.MAX_ATTENTION))
        for n in notes:
            _clean(n.variants)
    cap = home.capacity(rows, mx, NOW)
    if cap is not None:
        _clean(_plain(v) for v in home.summary_variants(cap, NOW))
    by_n = {a.number: a for a in snap.accounts}
    for row in rows:
        _clean(render.render_detail(row, by_n[row.number], 117, ctx).plain.splitlines())
        _clean(filter(None, [fx.switch_warning(row, mx)]))
        _clean(fx.relogin_steps(row, ssh=True, host="h", claude_path=None,
                                return_to=rows[0], now=NOW))


@pytest.mark.parametrize("width", [100, 80, 70])
def test_a_long_name_at_80_columns_keeps_every_status_and_the_name_whole(width):
    long = "wonjun.chois-Organization-account-x"  # 37 cells with the order mark's gap
    snap, mx, state = mockup()
    rows = fx.fleet_rows(snap, mx, PRIME, state, now=NOW)
    rows = [replace(r, name=long) if r.number == "4" else r for r in rows]
    ctx = render.Ctx(Palette.DARK, window_ticks(mx), NOW)
    statuses = {r.number: ctx.status(r) for r in rows}
    plan = home.table_plan(width, 24, home.table_needs(rows, statuses, now=NOW))
    lines = render.render_table(rows, plan, ctx, selected=None, selected_bg="").text.plain
    lines = lines.splitlines()
    for row, line in zip(rows, lines):
        status = statuses[row.number]
        if status:
            assert line.rstrip().endswith(plan.status_text(status[0])), (width, line)
        assert row.name in line and "…" not in line.split(row.name)[0]
    header = render.table_header(plan, Palette.DARK).plain
    assert header.rstrip().endswith("status") and "…" not in header


def test_the_sentence_and_attention_lines_never_cut_a_name():
    snap, mx, state = mockup()
    rows = fx.fleet_rows(snap, mx, PRIME, state, now=NOW)
    rows = [replace(r, name="a-rather-long-account-name") if r.number in ("1", "3") else r
            for r in rows]
    dv = fx.DecisionView("hold", "1", None, None, "x", at=NOW - 5, source="engine", code="hold")
    hold = account_hold.AccountHold("1", NOW + 2 * H)
    variants = home.status_variants(SERVICE, dv, rows, MX, "live", now=NOW, hold=hold,
                                    hold_read=True)
    notes = home.attention_notices(rows, now=NOW)
    for width in range(8, 120):
        text = _plain(home.fit_variant(variants, width))
        assert "a-rather-long-account-name" in text or "a-rather" not in text, (width, text)
        for line, _tone in home.attention_lines(notes, width, 1):
            assert "a-rather-long-account-name" in line or "a-rath" not in line, (width, line)


def test_engine_log_lines_use_names():
    names = {"1": "main", "2": "side"}

    def hook(number, email):
        return names.get(str(number), "")

    events = [
        aw.PollEvent(active={"number": 1, "email": "main@example.com"},
                     headroom={"1": 30.0, "2": 80.0}, threshold=90.0),
        aw.SwitchEvent(trigger="soft", from_ref={"number": 1, "email": "main@example.com"},
                       to_ref={"number": 2, "email": "side@example.com"}),
        aw.QuarantineEvent(number="2", email="side@example.com", reason="dead"),
        aw.UnquarantineEvent(number="2", email="side@example.com"),
        aw.LoginAdoptedEvent(number="1"),
        aw.MaximizeDecisionEvent(active="1", decision="hold", trigger=None,
                                 reason=SCENARIOS[0][3].reason),
        aw.PrimeEvent(account="2", outcome="primed", resets_at=None),
    ]
    with aw.account_names(hook):
        lines = [e.human() for e in events]
    _clean(lines)
    assert all("@" not in line for line in lines)
    # Without a hook and without a roster: never the address.
    assert all("example.com" not in e.human() for e in events)


def test_without_a_hook_lines_read_the_roster(temp_home):
    import json

    from claude_swap import paths

    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": 1, "sequence": [1, 2],
        "accounts": {"1": {"email": "main@example.com", "alias": "main"},
                     "2": {"email": "side.user@example.com"}},
    }))
    lines = [
        aw.LoginAdoptedEvent(number="2").human(),
        aw.PrimeEvent(account="2", outcome="primed", resets_at=None).human(),
        aw.MaximizeDecisionEvent(active="1", decision="hold", trigger=None, reason="x").human(),
    ]
    _clean(lines)
    assert "side.user" in lines[0] and "side.user" in lines[1] and "main" in lines[2]


def test_notifications_and_cli_helpers_use_names():
    names = {"1": "main", "2": "side", "3": "old"}
    notes = [
        notify.switch_note("1", "2", "soft", "main 5h 62% >= soft 50%", names),
        notify.relogin_note("3", "refresh token dead", names),
        notify.expiring_note("3", NOW + DAY, NOW, names),
    ]
    _clean(text for n in notes for text in (n.title, n.body))
    _clean([
        oauth.relogin_fix("old"),
        shared_login.fix(["2", "3"], names),
        shared_login.skip_text(["2"], {"2": [("old", "3")]}, names),
        shared_login.profile_label("3", names),
        account_hold.held_message(account_hold.AccountHold("1", NOW + H), NOW, name="main"),
        entry_line({"ts": NOW, "host": "h", "from": 1, "to": 2, "trigger": "soft",
                    "source": "engine"}, names),
    ])


def test_a_relogin_row_names_the_account_in_its_attention_line():
    snap = accounts(acc(1, active=True, alias="main"),
                    acc(3, sentinel=USAGE_RELOGIN_REQUIRED, alias="old"))
    rows = fx.fleet_rows(snap, MX, PRIME, MaximizeState(), now=NOW)
    [note] = home.attention_notices(rows, now=NOW)
    assert note.variants[0].startswith("old needs re-login")
