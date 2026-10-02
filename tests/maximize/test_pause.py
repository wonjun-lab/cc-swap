"""maximize/pause.py: the engine pause a TUI re-login writes (pure check + I/O)."""

from __future__ import annotations

import json

import pytest

from claude_swap.maximize import pause

NOW = 1_800_000_000.0


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, None),
        ({"pausedUntil": NOW + 300, "pausedReason": "relogin"}, (NOW + 300, "relogin")),
        ({"pausedUntil": NOW + 300}, (NOW + 300, "paused")),
        ({"pausedUntil": NOW - 1, "pausedReason": "relogin"}, None),       # expired
        ({"pausedUntil": NOW, "pausedReason": "relogin"}, None),
        # Never longer than MAX_PAUSE_S from now: a bogus far-future value
        # (clock skew, a hand edit) must not stop the engine for good.
        ({"pausedUntil": NOW + 86400, "pausedReason": "relogin"}, None),
        ({"pausedUntil": "soon"}, None),
        ({"pausedUntil": True}, None),
        ({"pausedUntil": float("nan")}, None),
    ],
)
def test_active_pause(state, expected):
    assert pause.active_pause(state, NOW) == expected


def test_pause_and_resume_round_trip_keeps_other_keys(tmp_path):
    path = tmp_path / "autoswitch_state.json"
    path.write_text(json.dumps({"schemaVersion": 1, "lastSwitchAt": NOW - 60}))
    until = pause.pause(tmp_path, "relogin", now=NOW)
    assert until == NOW + pause.MAX_PAUSE_S
    raw = json.loads(path.read_text())
    assert raw["pausedUntil"] == until and raw["pausedReason"] == "relogin"
    assert raw["lastSwitchAt"] == NOW - 60
    assert pause.active_pause(raw, NOW + 1) == (until, "relogin")
    pause.resume(tmp_path)
    raw = json.loads(path.read_text())
    assert "pausedUntil" not in raw and "pausedReason" not in raw
    assert raw["lastSwitchAt"] == NOW - 60


def test_pause_is_capped_and_resume_without_a_file_is_a_no_op(tmp_path):
    assert pause.pause(tmp_path, "relogin", now=NOW, seconds=10_000) == NOW + pause.MAX_PAUSE_S
    pause.resume(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
