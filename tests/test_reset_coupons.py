"""Usage-reset coupons (``cedar_ember``, asked for with ``?cedar_ember=1`` on
the normal usage request): defensive parsing, ``cswap list``/``--json``,
Fleet's detail panel and the doctor line. The ineligible block is the live
answer of 2026-10-06; the eligible grant follows Claude Code 2.1.289's
schema (id, label, resets_total, resets_left, starts_at, ends_at, clears,
paused, usable_now, use_requires_limit, percent_used, blocking)."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap import credits, oauth
from claude_swap.credentials import ActiveCredentials
from claude_swap.json_output import usage_from_json, usage_to_json
from tests.test_credits import DAY, NOW, _day, _iso, _seeded_switcher

INELIGIBLE = {
    "eligible": False, "ineligible_reason": "surface", "at_limit": False,
    "exhausted": [], "grants": [], "next_grant_id": None,
    "weekly_resets_at": None, "cooldown_until": None, "event_props": None,
}


def grant(left=2, total=3, ends=None, clears=("five_hour", "seven_day"), **kw):
    return {
        "id": "g_1", "label": "Weekly resets", "resets_total": total,
        "resets_left": left, "starts_at": _iso(NOW - DAY),
        "ends_at": _iso(ends if ends is not None else NOW + 14 * DAY),
        "clears": list(clears), "paused": False, "usable_now": True,
        "use_requires_limit": True, "percent_used": {"five_hour": 40},
        "blocking": [], **kw,
    }


def eligible(*grants):
    return {**INELIGIBLE, "eligible": True, "ineligible_reason": None,
            "grants": list(grants), "next_grant_id": "g_1",
            "weekly_resets_at": _iso(NOW + 3 * DAY)}


def response(block):
    data = {
        "five_hour": {"utilization": 27.0, "resets_at": _iso(NOW + 3600)},
        "seven_day": {"utilization": 75.0, "resets_at": _iso(NOW + 3 * DAY)},
        "extra_usage": None,
    }
    if block is not ...:
        data["cedar_ember"] = block
    return data


# -- request and parsing -------------------------------------------------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_normal_usage_request_carries_the_flag_and_no_skip_spend(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        return _Resp(json.dumps(response(INELIGIBLE)).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    oauth.request_usage_data("tok")
    assert seen == ["https://api.anthropic.com/api/oauth/usage?cedar_ember=1"]
    assert "skip_spend" not in seen[0]


def test_absent_or_null_block_adds_nothing():
    for block in (..., None, "junk", 3):
        usage = oauth.build_usage_result(response(block))
        assert "reset_coupons" not in usage, block
        assert "resetCoupons" not in usage_to_json(usage)


def test_ineligible_block_is_kept_but_never_shown():
    usage = oauth.build_usage_result(response(INELIGIBLE))
    assert usage["reset_coupons"] == {
        "eligible": False, "ineligible_reason": "surface", "at_limit": False,
        "exhausted": [], "grants": [], "next_grant_id": None,
        "weekly_resets_at": None, "cooldown_until": None,
    }
    assert credits.reset_coupons_summary(usage, NOW) is None
    out = usage_to_json(usage)["resetCoupons"]
    assert out["eligible"] is False and out["ineligibleReason"] == "surface"


def test_eligible_grants_parse_and_summarize():
    ends = NOW + 14 * DAY
    usage = oauth.build_usage_result(response(eligible(grant(left=2, ends=ends))))
    g = usage["reset_coupons"]["grants"][0]
    assert g == {"id": "g_1", "label": "Weekly resets", "total": 3, "left": 2,
                 "starts_at": _iso(NOW - DAY), "ends_at": _iso(ends),
                 "clears": ["five_hour", "seven_day"], "paused": False,
                 "usable_now": True}
    assert credits.reset_coupons_summary(usage, NOW) == f"2 left (5h/7d) · expires {_day(ends)}"
    out = usage_to_json(usage)["resetCoupons"]
    assert out["grants"][0]["left"] == 2 and out["grants"][0]["usableNow"] is True
    assert out["nextGrantId"] == "g_1"
    # Import (usage_from_json) does not carry it back.
    assert "reset_coupons" not in usage_from_json(usage_to_json(usage))
    # Decisions never see it.
    assert oauth.relevant_windows(usage, ("all",)) == oauth.relevant_windows(
        oauth.build_usage_result(response(None)), ("all",))


def test_spent_expired_or_paused_coupons():
    soon, later = NOW + 2 * DAY, NOW + 9 * DAY
    two = response(eligible(grant(left=1, ends=later, clears=["seven_day_opus"]),
                            grant(left=1, ends=soon, clears=["five_hour"], id="g_2")))
    usage = oauth.build_usage_result(two)
    assert credits.reset_coupons_summary(usage, NOW) == f"2 left (Opus/5h) · expires {_day(soon)}"
    assert credits.reset_coupons_summary(usage, soon + 1) == f"1 left (Opus) · expires {_day(later)}"
    spent = oauth.build_usage_result(response(eligible(grant(left=0))))
    assert credits.reset_coupons_summary(spent, NOW) is None
    paused = oauth.build_usage_result(response(eligible(grant(paused=True))))
    assert credits.reset_coupons_summary(paused, NOW).endswith(" · paused")


def test_malformed_fields_are_tolerated():
    block = {
        "eligible": "yes",                 # not True → ineligible
        "grants": [None, "x", {"resets_left": "two"}, {"resets_left": 1, "clears": "five_hour",
                                                      "ends_at": 5, "label": 7}],
        "exhausted": "five_hour",
        "cooldown_until": 12,
    }
    parsed = oauth.parse_reset_coupons(block)
    assert parsed["eligible"] is False
    assert parsed["exhausted"] == []
    assert parsed["grants"] == [{"id": None, "label": None, "total": None, "left": 1,
                                 "starts_at": None, "ends_at": None, "clears": [],
                                 "paused": False, "usable_now": False}]
    assert oauth.parse_reset_coupons({}) == {
        "eligible": False, "ineligible_reason": None, "at_limit": False, "exhausted": [],
        "grants": [], "next_grant_id": None, "weekly_resets_at": None, "cooldown_until": None,
    }
    # A grant with nothing to read still summarizes when eligible.
    usage = {"reset_coupons": {"eligible": True, "grants": [{"left": 1}]}}
    assert credits.reset_coupons_summary(usage, NOW) == "1 left"


# -- surfaces ------------------------------------------------------------------------


def _list(switcher, usage, capsys):
    creds = json.dumps({"claudeAiOauth": {"accessToken": "sk"}})
    with patch.object(switcher, "_read_active_credentials",
                      return_value=ActiveCredentials(creds, False)), \
         patch.object(switcher, "_read_account_credentials", return_value=creds), \
         patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome(usage)):
        payload = switcher.list_accounts(json_output=True)
        capsys.readouterr()
        switcher.list_accounts()
    return payload, capsys.readouterr().out


@pytest.mark.parametrize("block,shown", [(..., False), (INELIGIBLE, False), ("eligible", True)])
def test_list(temp_home: Path, mock_org_claude_config: Path, sample_sequence_data: dict,
              capsys, block, shown):
    if block == "eligible":
        block = eligible(grant())
    switcher = _seeded_switcher(sample_sequence_data)
    payload, out = _list(switcher, oauth.build_usage_result(response(block)), capsys)
    assert ("reset coupons:" in out) is shown
    row = payload["accounts"][0]["usage"]
    assert ("resetCoupons" in row) is (block is not ...)
    if shown:
        assert f"reset coupons: 2 left (5h/7d) · expires {_day(NOW + 14 * DAY)}" in out


def test_fleet_detail_panel():
    from tests.maximize.test_fleet_home import NOW as FNOW, P, _by, _fleet
    from claude_swap.maximize.view import window_ticks
    from claude_swap.tui import fleet_render as render

    snap, mx, _state, rows, _msnap, _picks = _fleet()
    a1 = {a.number: a for a in snap.accounts}["1"]
    ctx = render.Ctx(P, window_ticks(mx), FNOW)
    ends = FNOW + 14 * DAY

    def with_block(block):
        coupons = oauth.parse_reset_coupons(block)
        return replace(a1, usage=replace(a1.usage, last_good={**a1.usage.last_good,
                                                              "reset_coupons": coupons}))

    ineligible = render.render_detail(_by(rows)["1"], with_block(INELIGIBLE), 117, ctx).plain
    assert "reset coupons" not in ineligible
    held = with_block({**eligible(), "grants": [{**grant(), "ends_at": _iso(ends)}]})
    lines = render.render_detail(_by(rows)["1"], held, 117, ctx).plain.splitlines()
    assert any(line.strip() == f"reset coupons 2 left (5h/7d) · expires {_day(ends)}"
               for line in lines)
    assert render.detail_height(_by(rows)["1"], held, ctx) == len(lines)


def test_doctor_line(tmp_path: Path):
    from claude_swap.maximize import doctor as dr

    def seed(blocks):
        rows = {
            num: {"email": f"{num}@example.com", "organizationUuid": f"org-{num}",
                  "fetchedAt": NOW - 60,
                  "lastGood": oauth.build_usage_result(response(block))}
            for num, block in blocks.items()
        }
        cache = tmp_path / "cache"
        cache.mkdir(exist_ok=True)
        (cache / "usage.json").write_text(json.dumps({"schemaVersion": 2, "accounts": rows}))
        slots = {num: SimpleNamespace(email=f"{num}@example.com", org=f"org-{num}")
                 for num in blocks}
        ctx = SimpleNamespace(probes=SimpleNamespace(backup_root=tmp_path, now=NOW),
                              slots=slots, name=lambda n: f"acct{n}")
        return [(f.scope, f.severity, f.detail) for f in dr.check_reset_coupons(ctx)]

    assert seed({"1": INELIGIBLE, "2": INELIGIBLE}) == [
        ("accounts", "info", "not offered to this client (surface)")]
    assert seed({"1": INELIGIBLE, "2": eligible(grant())}) == [
        ("#2", "info", f"2 left (5h/7d) · expires {_day(NOW + 14 * DAY)}"),
        ("accounts", "info", "not offered to this client (surface) on acct1"),
    ]
    assert seed({"1": ..., "2": None}) == []
    assert dr.check_reset_coupons in dr.ENV_CHECKS
