"""build_snapshot / usage_windows / report rows (no I/O)."""

from __future__ import annotations

import pytest

from claude_swap.maximize.model import Sample
from claude_swap.maximize.report import decision_rows, render_rows
from claude_swap.maximize.snapshot import build_snapshot, usage_windows
from claude_swap.settings import MaximizeSettings
from tests.test_autoswitch import _iso_at

NOW = 1_000_000.0
H = 3600.0


def win(p5, p7, r5=None, r7=None) -> dict:
    five = {"pct": p5}
    seven = {"pct": p7}
    if r5 is not None:
        five["resets_at"] = _iso_at(r5)
    if r7 is not None:
        seven["resets_at"] = _iso_at(r7)
    return {"five_hour": five, "seven_day": seven}


class TestUsageWindows:
    def test_reads_both_windows(self):
        assert usage_windows(win(12, 34, NOW + H, NOW + 50 * H), NOW) == (
            12.0, NOW + H, 34.0, NOW + 50 * H,
        )

    @pytest.mark.parametrize("value", [None, "token-expired", {}, {"spend": {"pct": 5}}])
    def test_unknown(self, value):
        assert usage_windows(value, NOW) == (None, None, None, None)

    def test_missing_window_reads_as_zero(self):
        assert usage_windows({"seven_day": {"pct": 20.0}}, NOW) == (0.0, None, 20.0, None)
        assert usage_windows({"five_hour": {"pct": 7, "resets_at": None}}, NOW) == (
            7.0, None, 0.0, None,
        )

    def test_rolled_over_5h_is_zero_but_keeps_its_reset(self):
        assert usage_windows(win(80, 30, NOW - 60), NOW) == (0.0, NOW - 60, 30.0, None)

    def test_rolled_over_7d_is_zero_without_reset(self):
        assert usage_windows(win(10, 90, None, NOW - 60), NOW) == (10.0, None, 0.0, None)


def test_build_snapshot_maps_records_in_order():
    records = {
        "2": {"email": "b@x.com", "alias": "Team"},
        "1": {"email": "a@x.com"},
        "3": {"email": "c@x.com", "disabled": True},
        "4": {"email": "d@x.com", "kind": "api_key"},
    }
    snap = build_snapshot(
        now=NOW,
        active="1",
        usage={"1": win(10, 20), "2": win(0, 0), "3": None},
        records=records,
        quarantined={"2"},
        api_key_accounts={"4"},
        rate_limit_tiers={"1": "default_claude_max_20x", "2": None},
        samples=[Sample(NOW - 2000, 1, 1), Sample(NOW - 60, 10, 20)],
        last_switch_at=NOW - 100,
        settings=MaximizeSettings(last_resort="team", plan_override="b@x.com:20x"),
        active_changed_at=NOW - 50,
    )
    assert [v.number for v in snap.accounts] == ["2", "1", "3", "4"]
    by = {v.number: v for v in snap.accounts}
    assert by["2"].tier == "last_resort" and by["2"].quarantined and by["2"].plan_weight == 4
    assert by["1"].tier == "normal" and by["1"].plan_weight == 4 and by["1"].pct7 == 20.0
    assert by["3"].tier == "excluded" and by["3"].pct5 is None
    assert by["4"].api_key and by["4"].plan_weight == 1
    assert [s.ts for s in snap.samples] == [NOW - 60]      # 30-minute trim
    assert snap.view("1") is by["1"] and snap.view("9") is None
    assert (snap.last_switch_at, snap.active_changed_at) == (NOW - 100, NOW - 50)


def test_rows_and_table_carry_no_email():
    snap = build_snapshot(
        now=NOW,
        active="1",
        usage={"1": win(62, 40, NOW + H, NOW + 72 * H), "2": win(0, 10)},
        records={"1": {"email": "a@x.com"}, "2": {"email": "b@x.com"}},
        quarantined=set(),
        api_key_accounts=set(),
        rate_limit_tiers={},
        samples=[Sample(NOW - 600, 62, 40), Sample(NOW, 62, 40)],
        last_switch_at=None,
        settings=MaximizeSettings(),
    )
    rows = decision_rows(snap)
    assert rows[0]["active"] and rows[0]["idle"] == "idle" and not rows[0]["landable"]
    assert rows[1]["landable"] and rows[1]["score"] == pytest.approx(0.9)
    table = render_rows(rows)
    assert table[0].split()[:3] == ["#", "tier", "plan"]
    assert table[1].split()[:3] == ["*", "1", "normal"]
    assert table[2].split()[:2] == ["2", "normal"]
    assert "@" not in "\n".join(table)
