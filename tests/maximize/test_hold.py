"""``cc-swap hold``: the account hold marker (maximize/hold.py), its CLI, and
every reader that names it (the Fleet read model, ``why``, ``doctor``,
``auto status``). The engine and the policy are in test_hold_engine.py and
test_policy.py."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from claude_swap.maximize import doctor as dr
from claude_swap.maximize import doctor_cli
from claude_swap.maximize import hold as h
from claude_swap.maximize import view as mxview
from claude_swap.maximize.fleet import fresh_s
from tests.maximize.doctor_support import World, files

NOW = 1_790_000_000.0
H = 3600.0


@pytest.fixture(autouse=True)
def utc():
    """``until 23:00`` and the clocks are local time: pin it."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _write(root, marker=None, *, state=None) -> None:
    if marker is not None:
        (root / h.HOLD_FILENAME).write_text(json.dumps({"schemaVersion": 1, "hold": marker}))
    if state is not None:
        (root / "autoswitch_state.json").write_text(json.dumps(state))


# -- the marker ------------------------------------------------------------------------------


def test_set_and_clear_round_trip_keeps_other_state_keys(tmp_path):
    _write(tmp_path, state={"schemaVersion": 1, "lastSwitchAt": NOW - 60})
    hold = h.set_hold(tmp_path, "1", NOW + 2 * H, by="cli", now=NOW, host="mbp")
    assert hold == h.AccountHold("1", NOW + 2 * H, NOW, "cli", "mbp")
    on_disk = json.loads((tmp_path / h.HOLD_FILENAME).read_text())
    assert on_disk == {"schemaVersion": 1, "hold": hold.to_json()}
    state = json.loads((tmp_path / "autoswitch_state.json").read_text())
    assert state[h.STATE_KEY] == hold.to_json() and state["lastSwitchAt"] == NOW - 60
    assert h.read_hold(tmp_path, now=NOW) == hold
    assert h.clear_hold(tmp_path) is True
    assert not (tmp_path / h.HOLD_FILENAME).exists()
    state = json.loads((tmp_path / "autoswitch_state.json").read_text())
    assert h.STATE_KEY not in state and state["lastSwitchAt"] == NOW - 60
    assert h.read_hold(tmp_path, now=NOW) is None
    assert h.clear_hold(tmp_path) is False  # nothing to lift: no write either


def test_a_hold_is_capped_at_24h_and_must_end_in_the_future(tmp_path):
    assert h.set_hold(tmp_path, "1", NOW + 30 * H, by="cli", now=NOW).until == NOW + 24 * H
    with pytest.raises(ValueError):
        h.set_hold(tmp_path, "1", NOW - 1, by="cli", now=NOW)


def test_clear_on_a_missing_root_writes_nothing(tmp_path):
    missing = tmp_path / "missing"
    assert h.clear_hold(missing) is False
    assert not missing.exists()


@pytest.mark.parametrize(("raw", "expected"), [
    ({"slot": "1", "until": NOW + H}, h.AccountHold("1", NOW + H)),
    ({"slot": 2, "until": NOW + H, "by": "fleet"}, h.AccountHold("2", NOW + H, by="fleet")),
    ({"slot": "1"}, None),                         # no end: pins nothing
    ({"until": NOW + H}, None),                    # no slot
    ({"slot": True, "until": NOW + H}, None),
    ({"slot": "1", "until": "soon"}, None),
    ({"slot": "1", "until": float("inf")}, None),
    ("1", None),
    (None, None),
])
def test_parse_marker(raw, expected):
    assert h.parse_marker(raw) == expected


def test_the_file_is_authoritative_and_the_state_mirror_counts_without_it(tmp_path):
    mirror = {h.STATE_KEY: {"slot": "2", "until": NOW + H}}
    assert h.marker(tmp_path, mirror) == h.AccountHold("2", NOW + H)
    _write(tmp_path, {"slot": "1", "until": NOW + 2 * H})
    assert h.marker(tmp_path, mirror).slot == "1"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"hold": "yes"}', '{"hold": {}}'])
def test_a_damaged_hold_file_is_no_hold(tmp_path, content):
    (tmp_path / h.HOLD_FILENAME).write_text(content)
    assert h.marker(tmp_path, {h.STATE_KEY: {"slot": "1", "until": NOW + H}}) is None


def test_a_hold_ends_at_its_end_and_a_far_future_one_is_none():
    hold = h.AccountHold("1", NOW + H)
    assert h.current(hold, NOW) == hold
    assert h.current(hold, NOW + H) is None
    assert h.current(h.AccountHold("1", NOW + 3 * 24 * H), NOW) is None


def test_holding_needs_the_slot_to_be_the_active_account():
    hold = h.AccountHold("1", NOW + H)
    assert h.holding(hold, "1", NOW) == hold
    assert h.holding(hold, 1, NOW) == hold
    assert h.holding(hold, "2", NOW) is None
    assert h.holding(hold, None, NOW) is None
    assert h.holding(None, "1", NOW) is None


# -- words ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "seconds"), [
    ("1h", 3600), ("90m", 5400), ("2h30m", 9000), ("2H", 7200), (" 45m ", 2700),
    ("24h", 86400), ("30h", 108000),
])
def test_parse_duration(text, seconds):
    assert h.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "h", "0m", "0h0m", "1.5h", "2 h", "1d", "abc", "-1h", "m30"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        h.parse_duration(text)


def test_parse_until_is_the_next_such_local_time():
    morning = time.mktime((2026, 10, 3, 9, 15, 0, 0, 0, -1))  # 09:15 local (UTC)
    assert h.parse_until("23:00", morning) == morning + 13 * H + 45 * 60   # later today
    assert h.parse_until("9:00", morning) == morning + 24 * H - 15 * 60    # tomorrow
    assert h.parse_until("09:15", morning) == morning + 24 * H             # now: tomorrow
    for bad in ("24:00", "12:60", "noon", "23", "23:0"):
        with pytest.raises(ValueError):
            h.parse_until(bad, morning)


def test_words():
    morning = time.mktime((2026, 10, 3, 13, 30, 0, 0, 0, -1))
    assert [h.left_text(s) for s in (30, 25 * 60, 2 * H, 90 * 60, 2 * H - 20)] == [
        "1m", "25m", "2h", "1h30m", "2h"]
    assert h.clock_text(morning + 2 * H, morning) == "15:30"
    assert h.clock_text(morning + 20 * H, morning) == "Oct 4 09:30"
    hold = h.AccountHold("1", morning + 2 * H)
    assert h.until_text(hold, morning) == "until 15:30 (2h left)"
    assert h.safety_text(98, 98) == "only hard 98%/100% will move you"
    assert h.safety_text(95, 98) == (
        "only a hard mark (5h 95%, 7d 98%) or 100% will move you")


def test_short_name_never_carries_an_address():
    assert h.short_name({"alias": "main", "email": "main@example.com"}) == "main"
    assert h.short_name({"alias": "", "email": "dev.shared@example.com"}) == "dev.shared"
    assert h.short_name({"alias": "me@example.com"}) == "me"
    assert h.short_name(None) == ""


# -- the Fleet read model -----------------------------------------------------------------------


def test_read_state_carries_the_marker_even_with_a_damaged_state_file(tmp_path):
    _write(tmp_path, {"slot": "1", "until": NOW + H})
    assert mxview.read_state(tmp_path).hold == h.AccountHold("1", NOW + H)
    (tmp_path / "autoswitch_state.json").write_text("{damaged")
    assert mxview.read_state(tmp_path).hold == h.AccountHold("1", NOW + H)


def test_the_tui_snapshot_honours_a_hold_on_the_active_account_only():
    from tests.maximize.test_fleet import MX, acc, accounts, usage
    from tests.maximize.test_fleet import NOW as FNOW

    snap = accounts(acc(1, usage(62, 40), active=True), acc(2, usage(0, 10)))
    pinned = mxview.MaximizeState(hold=h.AccountHold("1", FNOW + H))
    other = mxview.MaximizeState(hold=h.AccountHold("2", FNOW + H))
    assert mxview.snapshot_from_accounts(snap, MX, pinned, now=FNOW).hold_until == FNOW + H
    assert mxview.snapshot_from_accounts(snap, MX, other, now=FNOW).hold_until is None
    assert mxview.snapshot_from_accounts(snap, MX, pinned, now=FNOW + H).hold_until is None


# -- `cc-swap hold` ---------------------------------------------------------------------------------


@pytest.fixture
def root(temp_home, monkeypatch):
    from claude_swap import paths

    backup = paths.get_backup_root()
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": 1, "sequence": [1, 2],
        "accounts": {"1": {"email": "main@example.com", "alias": "main"},
                     "2": {"email": "side@example.com"}},
    }))
    monkeypatch.setattr(time, "time", lambda: NOW)
    return backup


def _main(monkeypatch, capsys, *argv) -> tuple[int, str]:
    from claude_swap import cli

    monkeypatch.setattr(sys, "argv", ["cc-swap", *argv])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    captured = capsys.readouterr()
    return exit_.value.code, captured.out + captured.err


def test_hold_a_duration_pins_the_active_account(root, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "hold", "2h")
    assert code == 0
    assert out.startswith("Holding #1 main until ")
    assert "(2h left) — only a hard mark (5h 95%, 7d 98%) or 100% will move you." in out
    assert "cc-swap hold off lifts it" in out
    hold = h.read_hold(root, now=NOW)
    assert (hold.slot, hold.until, hold.by) == ("1", NOW + 2 * H, "cli")


def test_hold_until_a_time(root, monkeypatch, capsys):
    code, _out = _main(monkeypatch, capsys, "hold", "until", "23:00")
    assert code == 0
    assert h.read_hold(root, now=NOW).until == h.parse_until("23:00", NOW)


def test_hold_is_capped_at_24h_and_says_so(root, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "hold", "30h")
    assert code == 0 and "(a hold is at most 24h)" in out
    assert h.read_hold(root, now=NOW).until == NOW + 24 * H


def test_hold_status_json_and_off(root, monkeypatch, capsys):
    assert _main(monkeypatch, capsys, "hold", "90m")[0] == 0
    code, out = _main(monkeypatch, capsys, "hold", "status", "--json")
    payload = json.loads(out)
    assert code == 0 and payload["action"] == "status" and payload["changed"] is False
    assert payload["hold"] == {"slot": "1", "until": NOW + 5400, "leftS": 5400,
                               "since": NOW, "by": "cli"}
    code, out = _main(monkeypatch, capsys, "hold")  # no argument: status
    assert code == 0 and out.startswith("Holding #1 main until")
    code, out = _main(monkeypatch, capsys, "hold", "off")
    assert code == 0 and "Hold lifted" in out
    assert h.read_hold(root, now=NOW) is None
    assert _main(monkeypatch, capsys, "hold", "off")[1].startswith("No hold to lift")
    assert json.loads(_main(monkeypatch, capsys, "hold", "--json")[1])["hold"] is None


def test_a_hold_on_another_slot_is_reported_as_over(root, monkeypatch, capsys):
    h.set_hold(root, "2", NOW + H, by="cli", now=NOW)
    code, out = _main(monkeypatch, capsys, "hold", "status")
    assert code == 0
    assert out.startswith("No hold: the hold on #2 side no longer applies (#1 main is the active")


@pytest.mark.parametrize("argv", [["soon"], ["until"], ["until", "25:00"], ["2h", "3h"],
                                  ["0m"], ["until", "23:00", "x"]])
def test_hold_rejects_bad_words(root, monkeypatch, capsys, argv):
    code, out = _main(monkeypatch, capsys, "hold", *argv)
    assert code == 2 and "cc-swap hold" in out
    assert h.read_hold(root, now=NOW) is None


def test_hold_without_an_active_account_fails(root, monkeypatch, capsys):
    (root / "sequence.json").write_text(json.dumps({"activeAccountNumber": None, "accounts": {}}))
    code, out = _main(monkeypatch, capsys, "hold", "1h")
    assert code == 1 and "No active account to hold" in out
    assert not (root / h.HOLD_FILENAME).exists()


def _live(temp_home, email: str | None, org: str = "") -> None:
    """The live login ``~/.claude.json`` names (None: nobody is logged in)."""
    path = temp_home / ".claude.json"
    if email is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps({"oauthAccount": {
        "emailAddress": email, "organizationUuid": org, "accountUuid": "uuid-x"}}))


def test_hold_pins_the_live_login_not_the_recorded_slot(root, temp_home, monkeypatch, capsys):
    # sequence.json still says #1, but a /login outside cc-swap made #2 live.
    _live(temp_home, "side@example.com")
    code, out = _main(monkeypatch, capsys, "hold", "1h")
    assert code == 0 and out.startswith("Holding #2 side until ")
    assert h.read_hold(root, now=NOW).slot == "2"
    assert "Holding #2 side" in _main(monkeypatch, capsys, "auto", "status")[1]


def test_an_unmanaged_live_login_is_never_held(root, temp_home, monkeypatch, capsys):
    _live(temp_home, "stranger@example.com")
    code, out = _main(monkeypatch, capsys, "hold", "1h")
    assert code == 1 and "No active account to hold" in out
    assert not (root / h.HOLD_FILENAME).exists()


@pytest.mark.parametrize(("email", "org"), [
    ("main@example.com", ""), ("side@example.com", ""), ("stranger@example.com", ""),
    ("side@example.com", "org-other"), (None, ""),
])
def test_live_slot_agrees_with_the_switcher(root, temp_home, email, org):
    from claude_swap.switcher import ClaudeAccountSwitcher

    _live(temp_home, email, org)
    switcher = ClaudeAccountSwitcher()
    expected = switcher.current_account_number()
    if expected is None and not switcher.has_live_login():
        expected = "1"  # nobody logged in: what sequence.json recorded
    assert h.live_slot(root) == expected


def test_why_names_the_hold_on_the_live_login(root, temp_home, monkeypatch, capsys):
    monkeypatch.setattr(doctor_cli.paths, "get_backup_root", lambda: root)
    _live(temp_home, "side@example.com")
    h.set_hold(root, "2", NOW + H, by="cli", now=NOW)
    with pytest.raises(SystemExit):
        doctor_cli.why_command(["--no-fallback", "--json"], clock=lambda: NOW)
    assert json.loads(capsys.readouterr().out)["hold"]["slot"] == "2"


def test_auto_status_names_the_hold(root, monkeypatch, capsys):
    h.set_hold(root, "1", NOW + 2 * H, by="fleet", now=NOW)
    code, out = _main(monkeypatch, capsys, "auto", "status")
    assert code == 0 and "Automatic switching is ON." in out
    assert "Holding #1 main until" in out and "(cc-swap hold off lifts it)." in out
    code, out = _main(monkeypatch, capsys, "auto", "status", "--json")
    assert json.loads(out)["hold"]["slot"] == "1"
    h.clear_hold(root)
    assert json.loads(_main(monkeypatch, capsys, "auto", "status", "--json")[1])["hold"] is None


# -- `cc-swap why` ---------------------------------------------------------------------------------


def _publish(root, **record) -> None:
    decision = {
        "at": NOW - 30, "pid": 4121, "active": "1", "decision": "hold", "trigger": None,
        "target": None, "pending": False, "plans": {},
        "reason": "#1 held until 02:13 (2h left) — only a hard mark (5h 95%, 7d 98%) or 100% "
                  "will move you; otherwise: #1 5h 62% >= soft 50%; idle; -> #2",
        "code": "hold",
    }
    decision.update(record)
    (root / "autoswitch_state.json").write_text(json.dumps({"maximizeDecision": decision}))


def test_why_explains_a_hold_and_names_it(root, monkeypatch, capsys):
    monkeypatch.setattr(doctor_cli.paths, "get_backup_root", lambda: root)
    _publish(root)
    h.set_hold(root, "1", NOW + 2 * H, by="cli", now=NOW)
    with pytest.raises(SystemExit):
        doctor_cli.why_command([], clock=lambda: NOW)
    out = capsys.readouterr().out
    assert "code     hold" in out and doctor_cli.REASONS["hold"][0] in out
    assert "  hold     #1 main until" in out and "(cc-swap hold off lifts it)" in out
    with pytest.raises(SystemExit):
        doctor_cli.why_command(["--json"], clock=lambda: NOW)
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "hold" and payload["hold"]["slot"] == "1"
    assert "holdLine" not in payload


def test_why_names_a_hold_without_a_fresh_decision(root, monkeypatch, capsys):
    monkeypatch.setattr(doctor_cli.paths, "get_backup_root", lambda: root)
    _publish(root, at=NOW - fresh_s(60.0) - 1)
    h.set_hold(root, "1", NOW + H, by="cli", now=NOW)
    with pytest.raises(SystemExit):
        doctor_cli.why_command(["--no-fallback"], clock=lambda: NOW)
    out = capsys.readouterr().out
    assert out.startswith("Holding #1 main until") and "No engine published" in out


# -- `cc-swap doctor` --------------------------------------------------------------------------------


def _doctor(world: World) -> list[dr.Finding]:
    before = files(world.home, world.root)
    findings = dr.run_checks(world.probes())
    assert files(world.home, world.root) == before, "doctor wrote a file"
    return findings


def test_doctor_names_a_hold_as_info(tmp_path):
    from tests.maximize.doctor_support import NOW as DNOW

    world = World(tmp_path).healthy()
    h.set_hold(world.root, "1", DNOW + 2 * H, by="cli", now=DNOW)
    [finding] = [f for f in _doctor(world) if f.check == "hold"]
    assert finding.severity == "info" and finding.fix == ""
    assert finding.detail.startswith("holding #1 until ")
    assert "soft, preempt and rebalance moves wait" in finding.detail
    assert dr.exit_code([finding]) == 0


def test_doctor_names_a_left_over_hold(tmp_path):
    from tests.maximize.doctor_support import NOW as DNOW

    world = World(tmp_path).healthy()
    h.set_hold(world.root, "2", DNOW + H, by="cli", now=DNOW)
    [finding] = [f for f in _doctor(world) if f.check == "hold"]
    assert "left over" in finding.detail and finding.fix == "cc-swap hold off"


def test_doctor_reads_the_live_login_for_the_held_slot(tmp_path):
    from tests.maximize.doctor_support import NOW as DNOW

    world = World(tmp_path).healthy()   # sequence.json says #1 ...
    world.login(2)                      # ... but #2 is the live login
    h.set_hold(world.root, "2", DNOW + H, by="cli", now=DNOW)
    [finding] = [f for f in _doctor(world) if f.check == "hold"]
    assert finding.detail.startswith("holding #2 until ")


def test_doctor_says_nothing_without_a_hold(tmp_path):
    world = World(tmp_path).healthy()
    assert not [f for f in _doctor(world) if f.check == "hold"]
