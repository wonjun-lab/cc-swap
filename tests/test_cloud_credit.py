"""The cloud-session credit (``iguana_necktie`` in the normal usage
response): parsed by the usage poll, shown by ``cswap list``, ``--json``
and Fleet's detail panel, invisible to decisions. Shapes are the live
response's (2026-10-06)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from claude_swap import credits, oauth
from claude_swap.credentials import ActiveCredentials
from claude_swap.json_output import usage_from_json, usage_to_json
from tests.test_credits import DAY, NOW, _day, _iso, _seeded_switcher


def _usage_response(cloud):
    return {
        "five_hour": {"utilization": 82.0, "resets_at": _iso(NOW + 3600)},
        "seven_day": {"utilization": 87.0, "resets_at": _iso(NOW + 3 * DAY)},
        "iguana_necktie": cloud,
        "extra_usage": None,
    }


def _cloud(used=0.0, limit=250, locked=None):
    return {"utilization": 0.0, "resets_at": _iso(NOW + 30 * DAY),
            "limit_dollars": limit, "used_dollars": used,
            "remaining_dollars": limit - used, "locked_reason": locked}


def test_the_usage_parse_keeps_the_cloud_credit():
    usage = oauth.build_usage_result(_usage_response(_cloud()))
    assert usage["cloud_credit"] == {
        "limit": 250.0, "used": 0.0, "remaining": 250.0,
        "resets_at": _iso(NOW + 30 * DAY), "locked_reason": None,
    }
    without = oauth.build_usage_result(_usage_response(None))
    assert "cloud_credit" not in without
    # Decisions never see it.
    assert oauth.relevant_windows(usage, ("all",)) == oauth.relevant_windows(without, ("all",))
    # A response with no windows still reads as "no usage".
    assert oauth.build_usage_result({"iguana_necktie": _cloud()}) is None
    assert oauth.parse_cloud_credit({"limit_dollars": None}) is None
    assert oauth.parse_cloud_credit("x") is None


def test_cloud_credit_line():
    exp = NOW + 30 * DAY
    usage = {"cloud_credit": {"limit": 250.0, "used": 0.0, "remaining": 250.0,
                              "resets_at": _iso(exp), "locked_reason": None}}
    assert credits.cloud_credit_summary(usage, NOW) == f"$0 / $250 · expires {_day(exp)}"
    usage["cloud_credit"].update(used=12.5, locked_reason="spend_paused")
    assert credits.cloud_credit_summary(usage, NOW) == (
        f"$12.50 / $250 · expires {_day(exp)} · locked: spend_paused"
    )
    assert credits.cloud_credit_summary(usage, exp + 1) is None
    assert credits.cloud_credit_summary({"five_hour": {"pct": 1.0}}, NOW) is None
    assert credits.cloud_credit_summary(None, NOW) is None


def test_json_is_additive_and_import_ignores_it():
    usage = oauth.build_usage_result(_usage_response(_cloud(used=5)))
    out = usage_to_json(usage)
    assert out["cloudCredit"] == {"limit": 250.0, "used": 5.0, "remaining": 245.0,
                                  "expiresAt": _iso(NOW + 30 * DAY), "lockedReason": None}
    assert "cloud_credit" not in usage_from_json(out)
    assert "cloudCredit" not in usage_to_json(oauth.build_usage_result(_usage_response(None)))


def test_list_shows_the_cloud_credit(
    temp_home: Path, mock_org_claude_config: Path, sample_sequence_data: dict, capsys,
):
    switcher = _seeded_switcher(sample_sequence_data)
    creds = json.dumps({"claudeAiOauth": {"accessToken": "sk"}})
    usage = oauth.build_usage_result(_usage_response(_cloud()))
    with patch.object(switcher, "_read_active_credentials",
                      return_value=ActiveCredentials(creds, False)), \
         patch.object(switcher, "_read_account_credentials", return_value=creds), \
         patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome(usage)):
        payload = switcher.list_accounts(json_output=True)
        capsys.readouterr()
        switcher.list_accounts()
    out = capsys.readouterr().out
    assert out.count(f"cloud credit: $0 / $250 · expires {_day(NOW + 30 * DAY)}") == 2
    assert payload["accounts"][0]["usage"]["cloudCredit"]["limit"] == 250.0


def test_list_hides_it_when_null(
    temp_home: Path, mock_org_claude_config: Path, sample_sequence_data: dict, capsys,
):
    switcher = _seeded_switcher(sample_sequence_data)
    creds = json.dumps({"claudeAiOauth": {"accessToken": "sk"}})
    usage = oauth.build_usage_result(_usage_response(None))
    with patch.object(switcher, "_read_active_credentials",
                      return_value=ActiveCredentials(creds, False)), \
         patch.object(switcher, "_read_account_credentials", return_value=creds), \
         patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome(usage)):
        switcher.list_accounts()
    assert "cloud credit" not in capsys.readouterr().out


def test_fleet_detail_panel_shows_the_cloud_credit():
    from tests.maximize.test_fleet_home import NOW as FNOW, P, _by, _fleet
    from claude_swap.maximize.view import window_ticks
    from claude_swap.tui import fleet_render as render

    snap, mx, _state, rows, _msnap, _picks = _fleet()
    a1 = {a.number: a for a in snap.accounts}["1"]
    ctx = render.Ctx(P, window_ticks(mx), FNOW)
    assert "cloud credit" not in render.render_detail(_by(rows)["1"], a1, 117, ctx).plain
    exp = FNOW + 30 * DAY
    cloud = {"limit": 250.0, "used": 0.0, "remaining": 250.0,
             "resets_at": _iso(exp), "locked_reason": None}
    a1 = replace(a1, usage=replace(a1.usage, last_good={**a1.usage.last_good,
                                                        "cloud_credit": cloud}))
    lines = render.render_detail(_by(rows)["1"], a1, 117, ctx).plain.splitlines()
    assert any(line.strip() == f"cloud credit $0 / $250 · expires {_day(exp)}" for line in lines)
    assert render.detail_height(_by(rows)["1"], a1, ctx) == len(lines)
