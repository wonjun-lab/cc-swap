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


def test_pause_is_skipped_when_no_longer_wanted_checked_under_the_lock(tmp_path):
    # A renewal racing a resume: the "still wanted" check runs inside the
    # state lock, so a resume that already ran is never undone.
    path = tmp_path / "autoswitch_state.json"
    path.write_text(json.dumps({"schemaVersion": 1}))
    assert pause.pause(tmp_path, "relogin", now=NOW, wanted=lambda: False) is None
    assert "pausedUntil" not in json.loads(path.read_text())
    assert pause.pause(tmp_path, "relogin", now=NOW, wanted=lambda: True) == NOW + 600


def test_renewal_extends_to_ten_minutes_past_the_last_renewal(tmp_path):
    pause.pause(tmp_path, "relogin", now=NOW)
    until = pause.pause(tmp_path, "relogin", now=NOW + 300)
    assert until == NOW + 300 + pause.MAX_PAUSE_S
    raw = json.loads((tmp_path / "autoswitch_state.json").read_text())
    assert raw["pausedUntil"] == until


def test_resume_without_a_marker_does_not_rewrite_the_file(tmp_path):
    path = tmp_path / "autoswitch_state.json"
    path.write_text('{"schemaVersion": 1, "x": 1}')
    before = path.stat().st_mtime_ns
    pause.resume(tmp_path)
    assert path.read_text() == '{"schemaVersion": 1, "x": 1}'
    assert path.stat().st_mtime_ns == before


def test_a_pause_never_shortens_another_owners_longer_one(tmp_path):
    long_until = pause.pause(tmp_path, "relogin", now=NOW, owner="modal")
    assert pause.pause(tmp_path, "relogin", now=NOW, seconds=60, owner="cli") == long_until
    raw = json.loads((tmp_path / "autoswitch_state.json").read_text())
    assert raw["pausedUntil"] == long_until and raw["pausedBy"] == "modal"


def test_resume_with_an_owner_lifts_only_its_own_pause(tmp_path):
    until = pause.pause(tmp_path, "relogin", now=NOW, owner="modal")
    pause.resume(tmp_path, owner="cli")  # not ours: kept
    raw = json.loads((tmp_path / "autoswitch_state.json").read_text())
    assert raw["pausedUntil"] == until
    pause.resume(tmp_path, owner="modal")
    raw = json.loads((tmp_path / "autoswitch_state.json").read_text())
    assert "pausedUntil" not in raw and "pausedBy" not in raw
