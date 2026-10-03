"""``cc-swap auto status`` says so while a re-login pause is in force."""

from __future__ import annotations

import json
import sys
import time

import pytest

from claude_swap.maximize import pause


def _main(monkeypatch, capsys, *argv) -> tuple[int, str]:
    from claude_swap import cli

    monkeypatch.setattr(sys, "argv", ["cc-swap", *argv])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    return exit_.value.code, capsys.readouterr().out


def _root():
    from claude_swap import paths

    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_status_line_names_the_pause_and_its_end():
    now = 1_800_000_000.0
    state = {"pausedUntil": now + 300, "pausedReason": "relogin"}
    line = pause.status_line(state, now)
    assert line is not None
    assert line.startswith("paused for a re-login until ")
    assert time.strftime("%H:%M", time.localtime(now + 300)) in line


def test_status_line_is_none_without_an_active_pause():
    now = 1_800_000_000.0
    assert pause.status_line({}, now) is None
    assert pause.status_line({"pausedUntil": now - 1}, now) is None


def test_other_reasons_are_quoted_not_called_a_relogin():
    now = 1_800_000_000.0
    line = pause.status_line({"pausedUntil": now + 60, "pausedReason": "maintenance"}, now)
    assert line is not None and "maintenance" in line and "re-login" not in line


def test_auto_status_mentions_an_active_pause(temp_home, monkeypatch, capsys):
    root = _root()
    until = pause.pause(root, "relogin", now=time.time())
    code, out = _main(monkeypatch, capsys, "auto", "status")
    assert code == 0
    assert out.startswith("Automatic switching is ON, but paused for a re-login until ")
    assert time.strftime("%H:%M", time.localtime(until)) in out


def test_auto_status_is_plain_without_a_pause(temp_home, monkeypatch, capsys):
    _root()
    code, out = _main(monkeypatch, capsys, "auto", "status")
    assert code == 0 and out.strip() == "Automatic switching is ON."


def test_auto_off_and_paused_mentions_both(temp_home, monkeypatch, capsys):
    root = _root()
    pause.set_auto_off(root, True, by="cli", now=time.time(), host=None)
    pause.pause(root, "relogin", now=time.time())
    code, out = _main(monkeypatch, capsys, "auto", "status")
    assert "Automatic switching is OFF" in out
    assert "paused for a re-login until " in out


def test_auto_status_json_carries_the_pause(temp_home, monkeypatch, capsys):
    root = _root()
    until = pause.pause(root, "relogin", now=time.time())
    code, out = _main(monkeypatch, capsys, "auto", "status", "--json")
    data = json.loads(out)
    assert data["pause"] == {"until": until, "reason": "relogin"}
    pause.resume(root)
    code, out = _main(monkeypatch, capsys, "auto", "status", "--json")
    assert json.loads(out)["pause"] is None
