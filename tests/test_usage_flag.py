"""The usage request's ``cedar_ember=1`` flag never costs a reading: a
refusal or a flag-only 5xx falls back to the plain URL once, and the flag
stays off for ``COUPON_FLAG_REPROBE_S``. Display-only blocks that do not
parse (NaN, Infinity, oversized numbers) are dropped, never the reading."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from claude_swap import oauth

FLAGGED = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"
PLAIN = "https://api.anthropic.com/api/oauth/usage"
BODY = {"five_hour": {"utilization": 10.0, "resets_at": "2030-01-01T00:00:00+00:00"}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def server(monkeypatch):
    """Answers by URL: an int is an HTTP error status, else a JSON body."""
    answers: dict[str, object] = {FLAGGED: BODY, PLAIN: BODY}
    seen: list[str] = []
    clock = [1000.0]

    def urlopen(req, timeout):
        seen.append(req.full_url)
        answer = answers[req.full_url]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "x", {}, io.BytesIO(b"{}"))
        return _Resp(json.dumps(answer).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("claude_swap.oauth.time.time", lambda: clock[0])
    return answers, seen, clock


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_refused_flag_falls_back_and_stays_off(server, status):
    answers, seen, clock = server
    answers[FLAGGED] = status
    assert oauth.request_usage_data("t") == BODY
    assert seen == [FLAGGED, PLAIN]
    oauth.request_usage_data("t")
    assert seen[2:] == [PLAIN]                      # off for this process
    clock[0] += oauth.COUPON_FLAG_REPROBE_S
    oauth.request_usage_data("t")
    assert seen[3:] == [FLAGGED, PLAIN]             # re-probed, still refused


def test_a_flag_only_5xx_falls_back_and_drops_the_flag(server):
    answers, seen, _clock = server
    answers[FLAGGED] = 502
    assert oauth.request_usage_data("t") == BODY
    oauth.request_usage_data("t")
    assert seen == [FLAGGED, PLAIN, PLAIN]


def test_a_real_5xx_keeps_the_flag_and_raises_the_original(server):
    answers, seen, _clock = server
    answers[FLAGGED], answers[PLAIN] = 503, 500
    with pytest.raises(urllib.error.HTTPError) as err:
        oauth.request_usage_data("t")
    assert err.value.code == 503
    answers[FLAGGED] = BODY
    oauth.request_usage_data("t")
    assert seen == [FLAGGED, PLAIN, FLAGGED]


@pytest.mark.parametrize("status", [401, 429])
def test_auth_and_rate_limit_errors_are_not_retried(server, status):
    answers, seen, _clock = server
    answers[FLAGGED] = status
    with pytest.raises(urllib.error.HTTPError):
        oauth.request_usage_data("t")
    assert seen == [FLAGGED]


def test_unparseable_display_values_are_dropped_not_the_reading():
    raw = json.loads(
        '{"five_hour": {"utilization": 10.0}, "seven_day": {"utilization": 20.0},'
        ' "iguana_necktie": {"limit_dollars": NaN, "used_dollars": Infinity},'
        ' "cedar_ember": {"eligible": true, "grants": [{"resets_left": NaN},'
        ' {"resets_left": 1e400}, {"resets_left": Infinity}, {"resets_left": 2}]}}'
    )
    usage = oauth.build_usage_result(raw)
    assert usage["five_hour"]["pct"] == 10.0 and usage["seven_day"]["pct"] == 20.0
    assert "cloud_credit" not in usage
    assert [g["left"] for g in usage["reset_coupons"]["grants"]] == [2]
    raw["iguana_necktie"] = {"limit_dollars": 250, "used_dollars": float("nan")}
    assert oauth.build_usage_result(raw)["cloud_credit"]["used"] is None
    raw["iguana_necktie"] = {"limit_dollars": 10**400}
    assert "cloud_credit" not in oauth.build_usage_result(raw)


def test_a_parser_that_raises_drops_only_its_block(monkeypatch):
    def boom(_block):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(oauth, "parse_reset_coupons", boom)
    monkeypatch.setattr(oauth, "parse_cloud_credit", boom)
    usage = oauth.build_usage_result({"five_hour": {"utilization": 1.0},
                                      "iguana_necktie": {}, "cedar_ember": {}})
    assert usage == {"five_hour": {"pct": 1.0}}
