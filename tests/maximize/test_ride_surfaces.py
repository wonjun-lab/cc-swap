"""The learned ride where you see it: settings, Fleet's sentence and Swap
strategy, the published decision, ``cc-swap why`` and ``cc-swap doctor``."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.maximize import doctor_cli, home
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import ride as learned_ride
from claude_swap.maximize.model import Sample
from claude_swap.maximize.view import MaximizeState, read_state
from claude_swap.settings import (
    MaximizeSettings,
    load_maximize_settings,
    set_setting,
    settings_path,
    unset_setting,
)
from tests.maximize.test_fleet import MX, NOW, PRIME, acc, accounts, usage

README = Path(__file__).resolve().parents[2] / "README.md"
LIVE_MX = replace(MX, hard_5h=95.0, hard_7d=99.0, force_eta_min=3)
SERVICE = fx.EngineStatus("service", 4121, {"running": True, "pid": 4121})


# -- settings ----------------------------------------------------------------------------


class TestSettings:
    def test_defaults(self):
        s = MaximizeSettings()
        assert (s.learned_ride, s.ride_windows, s.ride_max_min) == (True, "7d", 30)

    def test_lenient_load_reports_each_repair(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"maximize": {
            "learnedRide": "no", "rideWindows": "7d,5h", "rideMaxMin": 500,
        }}))
        problems: list[str] = []
        s = load_maximize_settings(tmp_path, problems=problems)
        assert (s.learned_ride, s.ride_windows, s.ride_max_min) == (False, "7d", 120)
        assert problems == [
            "maximize.learnedRide must be true or false (no quotes), got 'no'; read as false",
            "maximize.rideWindows must be one of: 7d, 5h, 5h,7d, \"\", got '7d,5h'; "
            "using default 7d",
            "maximize.rideMaxMin is 500, outside 0-120; clamped to 120",
        ]

    def test_an_empty_list_loads_as_none(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"maximize": {"rideWindows": ""}}))
        problems: list[str] = []
        assert load_maximize_settings(tmp_path, problems=problems).ride_windows == ""
        assert problems == []

    def test_strict_set(self, tmp_path: Path):
        assert set_setting(tmp_path, "maximize.rideWindows", "5h,7d") == "5h,7d"
        assert set_setting(tmp_path, "maximize.rideWindows", "") == ""
        assert set_setting(tmp_path, "maximize.learnedRide", "false") is False
        assert set_setting(tmp_path, "maximize.rideMaxMin", "0") == 0
        s = load_maximize_settings(tmp_path)
        assert (s.learned_ride, s.ride_windows, s.ride_max_min) == (False, "", 0)
        for key, bad in (
            ("maximize.rideWindows", "7d,5h"), ("maximize.rideWindows", "1d"),
            ("maximize.rideMaxMin", "121"), ("maximize.rideMaxMin", "-1"),
            ("maximize.rideMaxMin", "2.5"), ("maximize.learnedRide", "sometimes"),
        ):
            with pytest.raises(ConfigError):
                set_setting(tmp_path, key, bad)
        with pytest.raises(ConfigError, match=r'one of: 7d, 5h, 5h,7d, ""'):
            set_setting(tmp_path, "maximize.rideWindows", "both")
        assert unset_setting(tmp_path, "maximize.rideWindows")
        assert load_maximize_settings(tmp_path).ride_windows == "7d"


class TestSwapStrategy:
    def test_the_editor_has_the_ride_fields(self):
        from claude_swap.tui import menus

        keys = [f.key for f in menus.STRATEGY_FIELDS]
        for key in ("maximize.learnedRide", "maximize.rideWindows", "maximize.rideMaxMin"):
            assert key in keys, key

    def test_ride_windows_cycles_through_its_choices(self):
        values = fx.strategy_values(MaximizeSettings(), PRIME)
        seen = []
        for _ in range(4):
            values = fx.strategy_step(values, "maximize.rideWindows", 1)
            seen.append(values["maximize.rideWindows"])
        assert seen == ["5h", "5h,7d", "", "7d"]
        assert fx.strategy_step(values, "maximize.rideWindows", -1)["maximize.rideWindows"] == ""

    def test_ride_max_min_stays_in_range(self):
        values = {**fx.strategy_values(MaximizeSettings(), PRIME), "maximize.rideMaxMin": 0}
        assert fx.strategy_step(values, "maximize.rideMaxMin", -5)["maximize.rideMaxMin"] == 0
        values["maximize.rideMaxMin"] = 120
        assert fx.strategy_step(values, "maximize.rideMaxMin", 5)["maximize.rideMaxMin"] == 120

    def test_none_is_saved_as_config_set_reads_it(self, tmp_path: Path):
        saved = fx.strategy_values(MaximizeSettings(), PRIME)
        edited = {**saved, "maximize.rideWindows": "", "maximize.learnedRide": False}
        writes = sorted(fx.strategy_writes(saved, edited))
        assert writes == [("maximize.learnedRide", "false"), ("maximize.rideWindows", "")]
        for key, raw in writes:
            set_setting(tmp_path, key, raw)
        s = load_maximize_settings(tmp_path)
        assert (s.learned_ride, s.ride_windows) == (False, "")

    def test_the_editor_shows_none_for_an_empty_list(self):
        from claude_swap.tui import menus
        from claude_swap.tui.fleet_strategy import _value_text

        field = next(f for f in menus.STRATEGY_FIELDS if f.key == "maximize.rideWindows")
        assert _value_text(field, "") == "none"
        assert _value_text(field, "5h,7d") == "5h,7d"


# -- Fleet -------------------------------------------------------------------------------


def _ride_fleet(*, armed_ago: float = 0.0, q: float = 0.35):
    snap = accounts(
        acc(5, usage(40, 99), active=True, alias="side"),
        acc(2, usage(0, 10), alias="main"),
    )
    state = MaximizeState(
        samples_account="5",
        samples=(Sample(NOW - 660, 37, 99), Sample(NOW - 60, 40, 99)),
        ride_q={"7d": q, "5h": 0.3},
        ride_account="5",
        ride_armed_at={"7d": NOW - armed_ago},
        ride_point_s={"7d": 600.0},
    )
    msnap = fx.fleet_snapshot(snap, LIVE_MX, state, now=NOW)
    dv = replace(fx.preview_decision(msnap, LIVE_MX), source="engine", at=NOW - 20)
    rows = fx.fleet_rows(snap, LIVE_MX, PRIME, state, now=NOW)
    return dv, rows


def _says(dv, rows, now=NOW) -> list[str]:
    return [
        "".join(t for t, _ in v)
        for v in home.status_variants(SERVICE, dv, rows, LIVE_MX, "live", now=now)
    ]


def test_fleet_words_a_ride_and_counts_it_down():
    dv, rows = _ride_fleet()
    assert (dv.kind, dv.code) == ("hold", "ride")
    assert dv.ride_until == pytest.approx(NOW + 0.35 * 600 - 90)
    assert dv.eta_hard_min is None  # past its hard mark already
    said = _says(dv, rows)
    assert said[0] == (
        "Auto ON · using #5 side · 7d 99% — riding to the limit, switching in ~2m "
        "(learned) or at your next pause"
    )
    assert said[1].startswith("Auto ON · #5 7d 99% — riding to the limit")
    assert said[-1] == "Auto ON"
    # A minute later, from the same published decision: ~1m, then now.
    assert "switching in ~1m (learned)" in _says(dv, rows, NOW + 60)[0]
    assert "riding to the limit, switching now (learned)" in _says(dv, rows, NOW + 130)[0]
    assert f"`{said[0]}`" in README.read_text(encoding="utf-8")


def test_a_ride_is_never_worded_as_an_account_hold():
    from claude_swap.maximize.hold import AccountHold

    dv, rows = _ride_fleet()
    held = home.status_variants(
        SERVICE, dv, rows, LIVE_MX, "live", now=NOW,
        hold=AccountHold("5", NOW + 3600), hold_read=True,
    )
    assert "riding to the limit" in "".join(t for t, _ in held[0])


def test_a_ride_without_its_switch_time_quotes_the_reason_minutes():
    dv = fx.DecisionView(
        "hold", "5", None, None,
        "#5 7d 99% — riding to the limit, switching in ~4m (capped) or at your next pause",
        at=NOW, source="engine", code="ride",
    )
    rows = _ride_fleet()[1]
    assert "switching in ~4m (capped) or at your next pause" in _says(dv, rows)[0]


def test_the_published_ride_is_read_back(tmp_path: Path):
    (tmp_path / "autoswitch_state.json").write_text(json.dumps({
        "maximizeDecision": {
            "at": NOW, "pid": 1, "active": "5", "decision": "hold", "trigger": None,
            "target": None, "reason": "#5 7d 99% — riding", "pending": False,
            "code": "ride", "rideUntil": NOW + 120,
        },
        learned_ride.LEARN_KEY: {"7d": {"q": 0.45, "n_ok": 3, "n_hit": 0}},
        "maximizeRide": {
            "account": "5", "armed": {"7d": {"at": NOW - 30, "pointS": 900.0}},
            "riding": ["7d"],
        },
    }))
    state = read_state(tmp_path)
    assert state.decision.code == "ride" and state.decision.ride_until == NOW + 120
    assert state.ride_q == {"5h": 0.3, "7d": 0.45}
    assert (state.ride_account, state.ride_armed_at, state.ride_point_s) == (
        "5", {"7d": NOW - 30}, {"7d": 900.0},
    )


# -- why and doctor ----------------------------------------------------------------------


def test_why_explains_a_ride_and_shows_what_was_learned(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor_cli.paths, "get_backup_root", lambda: tmp_path)
    (tmp_path / "sequence.json").write_text(
        json.dumps({"activeAccountNumber": 5, "accounts": {}})
    )
    reason = "#5 7d 99% — riding to the limit, switching in ~2m (learned) or at your next pause"
    (tmp_path / "autoswitch_state.json").write_text(json.dumps({
        "maximizeDecision": {
            "at": NOW - 30, "pid": 4121, "active": "5", "decision": "hold",
            "trigger": None, "target": None, "reason": reason, "pending": False,
            "plans": {}, "code": "ride", "rideUntil": NOW + 90,
        },
        learned_ride.LEARN_KEY: {"7d": {"q": 0.4, "n_ok": 2, "n_hit": 0}},
    }))
    with pytest.raises(SystemExit):
        doctor_cli.why_command(["--json"], clock=lambda: NOW)
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "ride"
    assert payload["meaning"] == doctor_cli.REASONS["ride"][0]
    assert payload["learnedRide"]["windows"] == ["7d"]
    assert payload["learnedRide"]["learned"]["7d"]["q"] == 0.4
    with pytest.raises(SystemExit):
        doctor_cli.why_command([], clock=lambda: NOW)
    out = capsys.readouterr().out
    assert "code     ride" in out
    assert (
        "ride     5h off (rideWindows; learned 0.30) · "
        "7d rides 0.40 of the last point (2 ok, 0 hit)"
    ) in out


def test_doctor_reports_the_learned_share_per_window(tmp_path):
    from claude_swap.maximize import doctor as dr
    from tests.maximize.doctor_support import World

    world = World(tmp_path)
    world.healthy()
    world.state(**{learned_ride.LEARN_KEY: {"7d": {"q": 0.25, "n_ok": 4, "n_hit": 2}}})
    findings = dr.run_checks(world.probes())
    [f] = [f for f in findings if f.check == "learned-ride"]
    assert f.severity == "info"
    assert f.detail == (
        "learned ride: 5h off (rideWindows; learned 0.30) · "
        "7d rides 0.25 of the last point (4 ok, 2 hit)"
    )
    world.settings(maximize={"learnedRide": False})
    [f] = [f for f in dr.run_checks(world.probes()) if f.check == "learned-ride"]
    assert f.detail == "learned ride: off (maximize.learnedRide is false)"
    world.settings(autoswitch={"strategy": "best"})
    assert [f for f in dr.run_checks(world.probes()) if f.check == "learned-ride"] == []
