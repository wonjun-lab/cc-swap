"""maximize/ledger.py: the structured switch ledger and ``cc-swap history``."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from claude_swap.autoswitch import SwitchEvent, TickOutcome
from claude_swap.maximize import history_cli, ledger
from tests.maximize.test_engine_maximize import EMAILS, make, of, win
from tests.test_autoswitch import EngineHarness

NOW = 1_800_000_000.0


@pytest.fixture
def installed():
    ledger.install(source="cli")
    yield
    ledger.uninstall()


def entries(root: Path) -> list[dict]:
    return ledger.read(root)


def _record(root: Path, frm, to, **kw) -> dict:
    kw.setdefault("actor", ledger.ACTOR_USER)
    kw.setdefault("trigger", ledger.TRIGGER_MANUAL)
    kw.setdefault("source", "cli")
    return ledger.record_switch(root, from_slot=frm, to_slot=to, **kw)


# -- the file -----------------------------------------------------------------------


class TestFile:
    def test_entry_shape_has_slots_host_versions_and_no_identity(self, tmp_path):
        entry = _record(
            tmp_path, "1", "2", reason="#1 at 5h 96% (a@example.com, sk-ant-oat01-SECRETSECRET)",
            strategy="maximize", now=NOW,
        )
        [stored] = entries(tmp_path)
        assert stored == entry
        assert stored["from"] == 1 and stored["to"] == 2
        assert stored["host"] == ledger.host_name()
        assert stored["actor"] == "user" and stored["trigger"] == "manual"
        assert stored["source"] == "cli" and stored["strategy"] == "maximize"
        assert stored["at"] == "2027-01-15T08:00:00Z" and stored["ts"] == NOW
        assert set(stored["versions"]) == {"ccSwap", "claude"}
        raw = ledger.path_for(tmp_path).read_text()
        assert "@" not in raw and "SECRET" not in raw
        assert "<email>" in stored["reason"] and "<redacted>" in stored["reason"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_file_is_0600_even_when_it_existed_wider(self, tmp_path):
        path = ledger.path_for(tmp_path)
        path.write_text("")
        os.chmod(path, 0o644)
        _record(tmp_path, "1", "2")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_rotation_keeps_three_generations_and_reads_across_them(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ledger, "MAX_BYTES", 400)
        for i in range(40):
            _record(tmp_path, str(i % 2 + 1), str((i + 1) % 2 + 1), now=NOW + i)
        base = ledger.path_for(tmp_path)
        names = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(base.name))
        assert names == ["switches.jsonl", "switches.jsonl.1", "switches.jsonl.2", "switches.jsonl.3"]
        assert all(p.stat().st_size <= 400 + 400 for p in tmp_path.glob("switches.jsonl*"))
        rows = entries(tmp_path)
        assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
        assert rows[-1]["ts"] == NOW + 39
        assert len(rows) < 40  # the oldest generation was dropped
        last3 = ledger.read(tmp_path, 3)
        assert [r["ts"] for r in last3] == [NOW + 37, NOW + 38, NOW + 39]

    def test_bad_lines_are_skipped(self, tmp_path):
        _record(tmp_path, "1", "2", now=NOW)
        with ledger.path_for(tmp_path).open("a") as fh:
            fh.write("not json\n{\"no\": \"ts\"}\n")
        _record(tmp_path, "2", "1", now=NOW + 1)
        assert [r["to"] for r in entries(tmp_path)] == [2, 1]

    def test_concurrent_appenders_never_interleave_lines(self, tmp_path):
        def worker(k):
            for i in range(25):
                ledger.append(tmp_path, ledger.make_entry(
                    tmp_path, from_slot=k, to_slot=i, actor="user", trigger="manual",
                    source="cli", reason="x" * 200,
                ))

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(1, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = ledger.path_for(tmp_path).read_text().splitlines()
        assert len(lines) == 100
        assert all(json.loads(line)["reason"] == "x" * 200 for line in lines)


# -- drift: a login changed outside cc-swap ------------------------------------------------


class TestDrift:
    def test_a_switch_leaving_another_slot_records_the_external_change_first(self, tmp_path):
        _record(tmp_path, "1", "2", now=NOW)
        _record(tmp_path, "3", "1", now=NOW + 60)  # live was #3, not #2
        rows = entries(tmp_path)
        assert [(r["from"], r["to"], r["actor"], r["trigger"]) for r in rows] == [
            (1, 2, "user", "manual"),
            (2, 3, "external", "external-login"),
            (3, 1, "user", "manual"),
        ]

    def test_record_external_needs_a_ledger_and_a_difference(self, tmp_path):
        assert ledger.record_external(tmp_path, "2", reason="r") is None  # no ledger yet
        _record(tmp_path, "1", "2", now=NOW)
        assert ledger.record_external(tmp_path, "2", reason="r") is None  # no change
        entry = ledger.record_external(tmp_path, None, reason="unmanaged login")
        assert entry["from"] == 2 and entry["to"] is None and entry["source"] == "external"
        assert ledger.drift(tmp_path, None) is None

    def test_history_drift_note(self, tmp_path):
        _record(tmp_path, "1", "2", now=NOW)
        assert history_cli.drift_line(tmp_path, "2") is None
        note = history_cli.drift_line(tmp_path, "3")
        assert "#3" in note and "#2" in note and "outside cc-swap" in note


# -- capture: the logging hook --------------------------------------------------------------


class TestCapture:
    def test_manual_switch_is_recorded_through_the_log_filter(self, temp_home, installed):
        h = EngineHarness(temp_home)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)
        h.switcher.switch_to("2", json_output=True)
        [entry] = entries(h.switcher.backup_dir)
        assert (entry["from"], entry["to"]) == (1, 2)
        assert (entry["actor"], entry["trigger"], entry["source"]) == ("user", "manual", "cli")
        assert "@" not in ledger.path_for(h.switcher.backup_dir).read_text()

    def test_a_new_switcher_does_not_drop_the_hook(self, temp_home, installed):
        # setup_logging clears the logger's handlers; the filter survives.
        h = EngineHarness(temp_home)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)
        logging.getLogger("claude-swap").handlers.clear()
        h2 = EngineHarness(temp_home)
        h2.switcher.switch_to("2", json_output=True)
        assert [e["to"] for e in entries(h2.switcher.backup_dir)] == [2]

    def test_context_tags_the_entry_and_tagged_survives_a_thread(self, temp_home, installed):
        h = EngineHarness(temp_home)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)
        fn = ledger.tagged(lambda: h.switcher.switch_to("2", json_output=True), source="fleet")
        t = threading.Thread(target=fn)
        t.start()
        t.join()
        [entry] = entries(h.switcher.backup_dir)
        assert entry["source"] == "fleet" and entry["trigger"] == "manual"

    def test_nothing_is_recorded_when_not_installed(self, temp_home):
        h = EngineHarness(temp_home)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)
        h.switcher.switch_to("2", json_output=True)
        assert not ledger.path_for(h.switcher.backup_dir).exists()

    def test_a_broken_ledger_never_breaks_the_switch(self, temp_home, installed, monkeypatch):
        h = EngineHarness(temp_home)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)

        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(ledger, "record_switch", boom)
        result = h.switcher.switch_to("2", json_output=True)
        assert result["switched"] is True

    def test_engine_switch_records_trigger_reason_and_strategy(self, temp_home, installed):
        h = make(temp_home)
        assert h.tick_with_usage(
            {"1": win(96, 40), "2": win(0, 10), "3": win(0, 50)}
        ) is TickOutcome.SWITCHED
        [switch] = of(h, SwitchEvent)
        [entry] = entries(h.switcher.backup_dir)
        assert (entry["from"], entry["to"]) == (1, int(switch.to_ref["number"]))
        assert entry["actor"] == "engine" and entry["trigger"] == switch.trigger
        assert entry["strategy"] == "maximize" and entry["reason"]

    def test_best_strategy_engine_switch_is_tagged_too(self, temp_home, installed):
        h = EngineHarness(temp_home, threshold=90)
        for i in (1, 2):
            h.seed(i, EMAILS[i])
        h.make_live(EMAILS[1], 1)
        usage = {"1": {"five_hour": {"pct": 95.0}, "seven_day": {"pct": 10.0}},
                 "2": {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 0.0}}}
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        [entry] = entries(h.switcher.backup_dir)
        assert entry["actor"] == "engine" and entry["strategy"] == "best"
        assert entry["trigger"] == of(h, SwitchEvent)[0].trigger

    def test_maximize_engine_records_an_outside_login_after_two_ticks(self, temp_home, installed):
        h = make(temp_home)
        root = h.switcher.backup_dir
        _record(root, "2", "1", now=NOW)  # the ledger last landed on #1
        h.make_live(EMAILS[3], 3)         # a /login as #3 inside a session
        usage = {"1": win(10, 10), "2": win(0, 10), "3": win(10, 20)}
        h.tick_with_usage(usage)
        assert len(entries(root)) == 1   # seen once: maybe a switch in flight
        h.clock.advance(60)
        h.tick_with_usage(usage)
        rows = entries(root)
        assert [(r["from"], r["to"], r["source"]) for r in rows[1:]] == [(1, 3, "external")]
        assert any("outside cc-swap" in getattr(e, "message", "") for e in h.events)
        h.clock.advance(60)
        h.tick_with_usage(usage)
        assert len(entries(root)) == 2   # recorded once


# -- the command ----------------------------------------------------------------------------


class TestHistoryCommand:
    def test_process_source(self):
        assert ledger.process_source(["auto"], {"CC_SWAP_SERVICE": "1"}) == "service"
        assert ledger.process_source(["auto", "--once"], {}) == "auto"
        assert ledger.process_source([], {}) == "tui"
        assert ledger.process_source(["tui"], {}) == "tui"
        assert ledger.process_source(["menubar"], {}) == "menubar"
        assert ledger.process_source(["switch", "2"], {}) == "cli"

    def test_lines_name_slots_trigger_and_who(self, tmp_path):
        _record(tmp_path, "1", "2", now=NOW, actor="engine", trigger="soft",
                source="service", strategy="maximize", reason="#1 at 5h 62%")
        [line] = history_cli.history_lines(entries(tmp_path))
        assert "#1 -> #2" in line and "soft (engine, service, maximize)" in line
        assert line.endswith("#1 at 5h 62%")
        assert "No switches recorded" in history_cli.history_lines([])[0]

    def test_cli_json_and_count(self, temp_home, capsys):
        from claude_swap import paths

        root = paths.get_backup_root()
        for i in range(5):
            _record(root, "1" if i % 2 == 0 else "2", "2" if i % 2 == 0 else "1", now=NOW + i)
        with pytest.raises(SystemExit) as exit_:
            history_cli.history_command(["-n", "2", "--json"])
        assert exit_.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["schemaVersion"] == 1 and len(data["entries"]) == 2
        assert data["entries"][-1]["ts"] == NOW + 4

    def test_cli_dispatch_registers_history(self, monkeypatch):
        from claude_swap import cli

        assert cli._FORK_COMMANDS["history"] == "_history_command"
        seen = []
        monkeypatch.setattr(cli, "_history_command", seen.append)
        monkeypatch.setattr(sys, "argv", ["cc-swap", "history", "-n", "3"])
        cli.main()
        assert seen == [["-n", "3"]]
        assert ledger.installed()  # main installs the hook for every command


# -- Fleet ------------------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFleet:
    async def test_v_opens_the_history_newest_first(self, tmp_path):
        from claude_swap.tui.modals import OutputModal
        from tests.maximize.test_tui_fleet import _fleet, _open, _settings
        from tests.test_tui import make_app

        _settings(tmp_path)
        _record(tmp_path, "2", "1", now=NOW, actor="engine", trigger="hard", source="service")
        _record(tmp_path, "1", "3", now=NOW + 60)
        app = make_app(_fleet(tmp_path))  # live: #1 — the ledger says #3
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, OutputModal)
            lines = app.screen._output.splitlines()
            assert lines[0].startswith("Note: the live login is #1")
            body = [line for line in lines if " -> " in line and not line.startswith("Note")]
            assert "#1 -> #3" in body[0] and "#2 -> #1" in body[1]
            assert "hard (engine, service)" in body[1]

    async def test_enter_switch_is_tagged_as_fleet(self, tmp_path):
        from tests.maximize.test_tui_fleet import _fleet, _open, _settings
        from tests.maximize.test_tui_fleet_actions import _to_row
        from tests.test_tui import make_app

        _settings(tmp_path)
        fake = _fleet(tmp_path)
        seen = []
        real = fake.switch_to

        def switch_to(identifier, json_output=False, force=False):
            seen.append(dict(ledger._context.get() or {}))
            return real(identifier, json_output=json_output, force=force)

        fake.switch_to = switch_to
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(pilot)
            await _to_row(pilot, "2")
            await pilot.press("enter")
            await _open(pilot)
        assert seen == [{"source": "fleet"}]


def test_menu_has_the_history_entry():
    from claude_swap.tui import menus

    entry = menus.BY_ACTION["history"]
    assert entry.key == "v" and entry.title.lower().startswith("v")
    assert "v" not in menus.ROW_KEYS and "v" not in menus.RESERVED_KEYS
