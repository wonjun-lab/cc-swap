"""``cc-swap auto on|off``: a persistent switch every engine honours without
a restart — off keeps polling and publishing, never switches, never primes."""

from __future__ import annotations

import json
import sys
import time

import pytest
from textual.widgets import Static

from claude_swap.autoswitch import (
    MaximizeDecisionEvent,
    NoSwitchEvent,
    PollEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import pause
from claude_swap.maximize import view as mxview
from claude_swap.maximize.engine_hook import DECISION_KEY, runtime_for
from claude_swap.settings import PrimeSettings
from tests.maximize.test_engine_maximize import EMAILS, FakePrimer, make, of, win
from tests.test_autoswitch import EngineHarness

NOW = 1_800_000_000.0
HARD = {"1": win(96, 40), "2": win(0, 10), "3": win(0, 50)}


def _off(h, **marker) -> None:
    path = h.switcher.backup_dir / "autoswitch_state.json"
    state = json.loads(path.read_text()) if path.exists() else {"schemaVersion": 1}
    state["autoOff"] = marker or {"since": NOW, "by": "cli", "host": "mbp"}
    path.write_text(json.dumps(state))


def auto_off_events(h) -> list[NoSwitchEvent]:
    return [e for e in of(h, NoSwitchEvent) if e.reason == "auto-off"]


@pytest.mark.parametrize(("raw", "off"), [
    ({}, False),
    ({"autoOff": None}, False),
    ({"autoOff": False}, False),
    ({"autoOff": {"since": NOW, "by": "cli"}}, True),
    # Set on purpose but damaged: still off (failing open would switch
    # against the user's wish).
    ({"autoOff": True}, True),
    ({"autoOff": "yes"}, True),
    ({"autoOff": {}}, True),
])
def test_marker_semantics(raw, off):
    assert (pause.auto_off(raw) is not None) is off
    # The TUI's read model agrees with the engine's.
    assert pause.AUTO_OFF_KEY == mxview.AUTO_OFF_KEY


def test_set_auto_off_round_trip_keeps_other_keys(tmp_path):
    path = tmp_path / "autoswitch_state.json"
    path.write_text(json.dumps({"schemaVersion": 1, "lastSwitchAt": NOW - 60}))
    assert pause.set_auto_off(tmp_path, True, by="cli", now=NOW, host="mbp") is True
    assert pause.set_auto_off(tmp_path, True, by="fleet", now=NOW + 5) is False  # keeps since
    off = pause.read_auto_off(tmp_path)
    assert off == pause.AutoOff(NOW, "cli", "mbp")
    assert json.loads(path.read_text())["lastSwitchAt"] == NOW - 60
    assert pause.set_auto_off(tmp_path, False, by="cli", now=NOW) is True
    assert pause.read_auto_off(tmp_path) is None
    assert pause.set_auto_off(tmp_path, False, by="cli", now=NOW) is False
    assert pause.set_auto_off(tmp_path / "missing", False, by="cli", now=NOW) is False
    assert not (tmp_path / "missing").exists()


class TestMaximizeEngine:
    def test_off_holds_a_hard_switch_but_still_decides_and_publishes(self, temp_home):
        h = make(temp_home)
        _off(h)
        assert h.tick_with_usage(HARD) is TickOutcome.NO_ACTION
        assert h.active_number() == 1 and not of(h, SwitchEvent)
        assert of(h, PollEvent)                                  # still polls
        [decision] = of(h, MaximizeDecisionEvent)                # still decides
        assert decision.decision == "switch"
        assert h.state()[DECISION_KEY]["decision"] == "switch"   # still publishes
        [event] = auto_off_events(h)
        assert "cc-swap auto on" in event.detail and "by cli" in event.detail

    def test_off_stops_priming(self, temp_home):
        h = make(temp_home)
        rt = runtime_for(h.engine)
        rt.primer, rt.prime_settings = FakePrimer(), PrimeSettings(enabled=True)
        _off(h)
        h.tick_with_usage({"1": win(10, 10), "2": win(0, 10), "3": win(0, 50)})
        assert rt.primer.calls == []

    def test_auto_off_event_once_an_hour(self, temp_home):
        h = make(temp_home)
        _off(h)
        for _ in range(3):
            h.tick_with_usage(HARD)
            h.clock.advance(600)
        assert len(auto_off_events(h)) == 1
        h.clock.advance(3600)
        h.tick_with_usage(HARD)
        assert len(auto_off_events(h)) == 2

    def test_on_again_without_restart(self, temp_home):
        h = make(temp_home)
        _off(h)
        assert h.tick_with_usage(HARD) is TickOutcome.NO_ACTION
        pause.set_auto_off(h.switcher.backup_dir, False, by="cli", now=NOW)
        assert h.tick_with_usage(HARD) is TickOutcome.SWITCHED

    def test_marker_survives_an_engine_restart(self, temp_home):
        h = make(temp_home)
        pause.set_auto_off(h.switcher.backup_dir, True, by="cli", now=NOW)
        h.engine = h._make_engine()  # a restarted service
        assert h.tick_with_usage(HARD) is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_unreadable_state_file_means_on(self, temp_home):
        h = make(temp_home)
        (h.switcher.backup_dir / "autoswitch_state.json").write_text("{not json")
        assert h.tick_with_usage(HARD) is TickOutcome.SWITCHED

    def test_off_landing_mid_tick_is_honoured_before_the_switch(self, temp_home):
        # The tick read the state while auto was on; `cc-swap auto off`
        # lands before the switch: engine_switch re-reads it under the lock.
        h = make(temp_home)
        real = h.engine._freshen_target

        def freshen(number, email):
            pause.set_auto_off(h.switcher.backup_dir, True, by="cli", now=NOW)
            return real(number, email)

        h.engine._freshen_target = freshen
        assert h.tick_with_usage(HARD) is not TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_the_refusal_at_switch_time_says_auto_off_not_already_active(self, temp_home):
        h = make(temp_home)
        real = h.engine._freshen_target

        def freshen(number, email):
            pause.set_auto_off(h.switcher.backup_dir, True, by="cli", now=NOW, host="mbp")
            return real(number, email)

        h.engine._freshen_target = freshen
        assert h.tick_with_usage(HARD) is TickOutcome.NO_ACTION
        reasons = [e.reason for e in of(h, NoSwitchEvent)]
        assert "auto-off" in reasons and "already-active" not in reasons
        [event] = auto_off_events(h)
        assert "cc-swap auto on" in event.detail and "by cli" in event.detail
        assert h.active_number() == 1

    def test_the_documented_reasons_include_auto_off(self):
        from claude_swap.maximize.doctor_cli import REASONS

        assert "auto-off" in REASONS


class TestFlagFile:
    """The flag lives in its own small file too, so a state file that cannot
    be parsed cannot silently turn automatic switching back on."""

    def _flag(self, root):
        return root / "auto_off.json"

    def test_turning_off_writes_the_flag_and_keeps_the_state_key(self, tmp_path):
        assert pause.set_auto_off(tmp_path, True, by="cli", now=NOW, host="mbp") is True
        assert json.loads(self._flag(tmp_path).read_text())["autoOff"] == {
            "since": NOW, "by": "cli", "host": "mbp"
        }
        assert json.loads((tmp_path / "autoswitch_state.json").read_text())["autoOff"] == {
            "since": NOW, "by": "cli", "host": "mbp"
        }

    def test_turning_on_removes_both(self, tmp_path):
        pause.set_auto_off(tmp_path, True, by="cli", now=NOW)
        assert pause.set_auto_off(tmp_path, False, by="cli", now=NOW) is True
        assert not self._flag(tmp_path).exists()
        assert "autoOff" not in json.loads((tmp_path / "autoswitch_state.json").read_text())
        assert pause.read_auto_off(tmp_path) is None
        assert pause.set_auto_off(tmp_path, False, by="cli", now=NOW) is False

    def test_a_corrupt_state_file_keeps_it_off(self, tmp_path):
        pause.set_auto_off(tmp_path, True, by="cli", now=NOW, host="mbp")
        (tmp_path / "autoswitch_state.json").write_text("{not json")
        assert pause.read_auto_off(tmp_path) == pause.AutoOff(NOW, "cli", "mbp")
        assert mxview.read_state(tmp_path).auto_off is True
        assert mxview.read_state(tmp_path).auto_off_by == "cli"

    def test_a_state_file_replaced_without_the_key_keeps_it_off(self, tmp_path):
        pause.set_auto_off(tmp_path, True, by="cli", now=NOW)
        (tmp_path / "autoswitch_state.json").write_text(json.dumps({"schemaVersion": 1}))
        assert pause.read_auto_off(tmp_path) is not None
        assert mxview.read_state(tmp_path).auto_off is True

    @pytest.mark.parametrize("content", ["", "{not json", "[]", "{}", '{"autoOff": 5}', "\xff\xfe"])
    def test_a_damaged_flag_file_reads_as_off(self, tmp_path, content):
        self._flag(tmp_path).write_bytes(content.encode("latin-1"))
        assert pause.read_auto_off(tmp_path) is not None
        assert mxview.read_state(tmp_path).auto_off is True

    def test_an_unreadable_flag_file_reads_as_off(self, tmp_path):
        self._flag(tmp_path).mkdir()  # exists, but cannot be read as a file
        assert pause.read_auto_off(tmp_path) is not None
        assert mxview.read_state(tmp_path).auto_off is True

    def test_state_key_alone_still_counts_for_older_builds(self, tmp_path):
        (tmp_path / "autoswitch_state.json").write_text(
            json.dumps({"autoOff": {"since": NOW, "by": "cli"}})
        )
        assert pause.read_auto_off(tmp_path) is not None

    def test_turning_off_does_not_overwrite_a_corrupt_state_file(self, tmp_path):
        (tmp_path / "autoswitch_state.json").write_text("{not json")
        assert pause.set_auto_off(tmp_path, True, by="cli", now=NOW) is True
        assert (tmp_path / "autoswitch_state.json").read_text() == "{not json"
        assert pause.read_auto_off(tmp_path) is not None

    def test_turning_on_works_while_the_state_file_is_corrupt(self, tmp_path):
        pause.set_auto_off(tmp_path, True, by="cli", now=NOW)
        (tmp_path / "autoswitch_state.json").write_text("{not json")
        assert pause.set_auto_off(tmp_path, False, by="cli", now=NOW) is True
        assert pause.read_auto_off(tmp_path) is None

    def test_off_again_keeps_the_first_since_and_heals_a_missing_flag(self, tmp_path):
        pause.set_auto_off(tmp_path, True, by="cli", now=NOW, host="mbp")
        self._flag(tmp_path).unlink()
        assert pause.set_auto_off(tmp_path, True, by="fleet", now=NOW + 5) is False
        assert pause.read_auto_off(tmp_path) == pause.AutoOff(NOW, "cli", "mbp")
        assert self._flag(tmp_path).exists()

    def test_engine_stays_off_through_a_corrupt_state_file(self, temp_home):
        h = make(temp_home)
        pause.set_auto_off(h.switcher.backup_dir, True, by="cli", now=NOW)
        (h.switcher.backup_dir / "autoswitch_state.json").write_text("{not json")
        assert h.tick_with_usage(HARD) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        # The tick rewrote the state without the key; the flag still holds.
        assert h.tick_with_usage(HARD) is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_engine_stays_off_with_a_damaged_flag_file(self, temp_home):
        h = make(temp_home)
        (h.switcher.backup_dir / "auto_off.json").write_text("garbage")
        assert h.tick_with_usage(HARD) is TickOutcome.NO_ACTION
        assert h.active_number() == 1


class TestUpstreamStrategies:
    USAGE = {
        "1": {"five_hour": {"pct": 95.0}, "seven_day": {"pct": 10.0}},
        "2": {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 0.0}},
    }

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    def test_off_holds(self, temp_home, strategy):
        h = EngineHarness(temp_home, threshold=90, strategy=strategy)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)
        _off(h)
        assert h.tick_with_usage(self.USAGE) is TickOutcome.NO_ACTION
        assert h.active_number() == 1 and len(auto_off_events(h)) == 1

    def test_on_is_unchanged(self, temp_home):
        h = EngineHarness(temp_home, threshold=90)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)
        assert h.tick_with_usage(self.USAGE) is TickOutcome.SWITCHED
        assert not auto_off_events(h)


class TestCli:
    def _main(self, monkeypatch, *argv):
        from claude_swap import cli

        monkeypatch.setattr(sys, "argv", ["cc-swap", *argv])
        with pytest.raises(SystemExit) as exit_:
            cli.main()
        return exit_.value.code

    def test_off_on_status(self, temp_home, monkeypatch, capsys):
        from claude_swap import paths

        root = paths.get_backup_root()
        assert self._main(monkeypatch, "auto", "off") == 0
        assert "Automatic switching is OFF" in capsys.readouterr().out
        off = pause.read_auto_off(root)
        assert off is not None and off.by == "cli"
        assert self._main(monkeypatch, "auto", "status", "--json") == 0
        data = json.loads(capsys.readouterr().out)
        assert data["autoSwitch"] == "off" and data["by"] == "cli"
        assert self._main(monkeypatch, "auto", "on") == 0
        assert "Automatic switching is ON." in capsys.readouterr().out
        assert pause.read_auto_off(root) is None

    def test_engine_flags_still_reach_the_engine_parser(self, monkeypatch):
        from claude_swap import cli

        seen = []
        monkeypatch.setattr(pause, "auto_command", seen.append)
        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **k: (_ for _ in ()).throw(
            KeyboardInterrupt()))
        monkeypatch.setattr(sys, "argv", ["cc-swap", "auto", "--once"])
        with pytest.raises(SystemExit):
            cli.main()
        assert seen == []


class TestFleetReadModel:
    def _state(self, **extra) -> mxview.MaximizeState:
        return mxview.MaximizeState(**extra)

    def test_read_state(self, tmp_path):
        (tmp_path / "autoswitch_state.json").write_text(json.dumps(
            {"autoOff": {"since": NOW, "by": "fleet"}}
        ))
        state = mxview.read_state(tmp_path)
        assert state.auto_off and state.auto_off_since == NOW and state.auto_off_by == "fleet"
        (tmp_path / "autoswitch_state.json").write_text(json.dumps({"autoOff": False}))
        assert not mxview.read_state(tmp_path).auto_off

    def test_the_home_sentence_says_auto_off_and_priming_stops(self):
        from claude_swap.maximize import home
        from claude_swap.settings import MaximizeSettings

        es = fx.engine_status(held_elsewhere=True, holder_pid=4121, own=None,
                              service={"running": True, "pid": 4121}, auto_off=True)
        dv = fx.DecisionView("off", "1", None, None, "cli", at=NOW, would="hold")
        sit = home.situation(es, dv, active="1", published_at=NOW, now=NOW, poll_s=60)
        assert sit == "auto-off" and not home.switching_live(sit)
        variants = home.status_variants(es, dv, [], MaximizeSettings(), sit, now=NOW)
        assert "".join(t for t, _ in variants[0]).startswith("Auto OFF — nothing switches")
        assert home.holder_variants(es, sit)[0] == "viewer · service pid 4121 is idle"
        # A paused priming is not news while nothing primes anyway.
        guard = "paused: claude 2.1.3 -> 2.1.4 (cc-swap prime verify)"
        assert home.attention_parts([], now=NOW, prime_guard=guard, priming=False) is None

    def test_now_line_shows_what_it_would_do(self):
        dv = fx.DecisionView("off", "1", None, None, "fleet", at=NOW,
                             would="switch (hard) → #2")
        line = fx.now_line(dv, now=NOW)
        assert line.startswith("AUTO OFF · no switch, no prime · would switch (hard) → #2")
        assert "cc-swap auto on" in line

    def test_mode_offers_the_toggle_for_every_holder(self):
        for holder in ("none", "here-dry", "here-live", "service", "other"):
            on = fx.mode_transitions(holder, auto_off=False)
            off = fx.mode_transitions(holder, auto_off=True)
            assert on[-1].action == "auto-off" and off[-1].action == "auto-on"
            keys = [a.key for a in on]
            assert len(set(keys)) == len(keys)
        es = fx.EngineStatus("service", 4121, {"running": True}, auto_off=True)
        assert "Automatic switching is OFF" in "\n".join(fx.mode_facts(es))


@pytest.mark.asyncio
class TestFleetScreen:
    async def test_status_sentence_menu_and_toggle(self, tmp_path):
        from tests.maximize.test_tui_fleet import _fleet, _open, _settings, _state
        from tests.test_tui import make_app, settle

        _settings(tmp_path)
        _state(tmp_path, autoOff={"since": time.time() - 60, "by": "cli"})
        app = make_app(_fleet(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            status = app.screen.query_one("#fx-status", Static).render().plain
            assert status.startswith("Auto OFF — nothing switches automatically")
            await pilot.press("m")  # the menu; its o is automatic switching on/off
            await pilot.pause()
            await pilot.press("o")
            await settle(pilot)
            await settle(pilot)
            assert pause.read_auto_off(tmp_path) is None
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("o")
            await settle(pilot)
            assert pause.read_auto_off(tmp_path) is None  # turning it off asks first
            await pilot.press("y")
            await settle(pilot)
            off = pause.read_auto_off(tmp_path)
            assert off is not None and off.by == "fleet"
