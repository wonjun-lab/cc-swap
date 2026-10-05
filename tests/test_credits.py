"""Prepaid balance / credit grants (credits.py): parsing, the paced store,
``cswap list`` (human and ``--json``) and Fleet's detail panel.

Response shapes are the ones the live endpoints returned on 2026-10-06
(``prepaid/credits`` and ``overage_credit_grant?campaign=feature_of_the_week``,
both 200), with amounts filled in where the live account had none.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import credits
from claude_swap.credentials import ActiveCredentials
from claude_swap.switcher import ClaudeAccountSwitcher

NOW = time.time()
DAY = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _day(ts: float) -> str:
    local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return f"{local:%b} {local.day}"


# The live answers for an account with no credits (field set verbatim).
PREPAID_EMPTY = {
    "amount": 0, "currency": None, "balance": None, "balance_credits": None,
    "amount_without_scoped_credits": None, "auto_reload_settings": None,
    "auto_reload_disabled_reason": None, "auto_reload_stripe_error_code": None,
    "pending_invoice_amount_cents": None, "last_paid_purchase_cents": None,
    "expiry_policy_months": None, "tranches": None, "promo_tranches": None,
    "next_expires_at": None,
}
GRANT_NONE = {
    "available": False, "eligible": False, "granted": False,
    "amount_minor_units": None, "currency": None, "needs_payment_setup": False,
    "feature": None, "expires_at": None,
}


def prepaid(amount=2000, tranches=()):
    return {**PREPAID_EMPTY, "amount": amount, "currency": "USD",
            "promo_tranches": list(tranches) or None}


def tranche(amount, expires_at, name=None):
    return {"remaining_amount_minor_units": amount, "expires_at": expires_at,
            "currency": "USD", "name": name}


# -- parsing and display -------------------------------------------------------------


def test_an_empty_account_parses_but_shows_nothing():
    parsed = credits.parse_prepaid(PREPAID_EMPTY)
    assert parsed == {"balance": 0, "currency": None, "expiring": []}
    assert credits.parse_grant(GRANT_NONE) is None
    assert credits.summary(parsed, NOW) is None


def test_no_numeric_amount_means_no_prepaid_reading():
    assert credits.parse_prepaid({**PREPAID_EMPTY, "amount": None}) is None
    assert credits.parse_prepaid("nope") is None


def test_balance_and_the_soonest_expiring_promo_part():
    soon, later = NOW + 3 * DAY, NOW + 20 * DAY
    parsed = credits.parse_prepaid(prepaid(2000, [
        tranche(500, _iso(later), "Later"),
        tranche(1000, _iso(soon), "Welcome"),
        tranche(0, _iso(soon)),           # spent: dropped
        tranche(300, None),               # no expiry: dropped
    ]))
    assert [t["amount"] for t in parsed["expiring"]] == [1000, 500]
    assert credits.summary(parsed, NOW) == (
        f"balance $20.00 · $10.00 expires {_day(soon)} (+1 more)"
    )
    # An expired part is not shown.
    assert credits.summary(parsed, later + 1) == "balance $20.00"


def test_grants_granted_and_claimable():
    exp = NOW + 10 * DAY
    granted = credits.parse_grant({**GRANT_NONE, "available": True, "eligible": True,
                                   "granted": True, "amount_minor_units": 2500,
                                   "currency": "USD", "expires_at": _iso(exp)})
    assert credits.summary({"grant": granted}, NOW) == f"grant $25.00 expires {_day(exp)}"
    assert credits.summary({"grant": granted}, exp + 1) is None
    offer = credits.parse_grant({**GRANT_NONE, "available": True, "eligible": True,
                                 "amount_minor_units": 2500, "currency": "USD"})
    assert offer["state"] == "available"
    assert credits.summary({"grant": offer}, NOW) == "grant $25.00 available"
    # Available but not eligible is nothing to show.
    assert credits.parse_grant({**GRANT_NONE, "available": True}) is None


def test_money():
    assert credits.money(123456, "USD") == "$1,234.56"
    assert credits.money(500, "EUR") == "€5.00"
    assert credits.money(500, "JPY") == "¥500"
    assert credits.money(500, "KRW") == "KRW 500"
    # Two-decimal minor units for everything else, as Claude Code formats
    # them (HUF/TWD/IDR included).
    assert credits.money(123456, "HUF") == "HUF 1,234.56"
    assert credits.money(500, "TWD") == "TWD 5.00"
    assert credits.money(500, None) == "$5.00"


def test_json_projection_uses_major_units():
    exp = _iso(NOW + DAY)
    reading = {**credits.parse_prepaid(prepaid(2000, [tranche(1000, exp, "Promo")])),
               "grant": {"state": "granted", "amount": 2500, "currency": "USD",
                         "expiresAt": exp},
               "fetchedAt": 1791000000.0}
    out = credits.to_json(reading)
    assert out["balance"] == 20.0 and out["currency"] == "USD"
    assert out["expiring"] == [{"amount": 10.0, "currency": "USD", "name": "Promo",
                                "expiresAt": exp}]
    assert out["grant"] == {"state": "granted", "amount": 25.0, "currency": "USD",
                            "expiresAt": exp}
    assert out["fetchedAt"].endswith("Z")
    assert credits.to_json(None) is None


# -- HTTP (urlopen faked; example.com stands in for the API) -----------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b"{}"))


@pytest.mark.no_credits_fake
def test_fetch_sends_get_with_org_header(monkeypatch):
    monkeypatch.setattr(credits, "API_BASE", "https://example.com")
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        assert timeout == 5.0
        body = prepaid(2000) if "prepaid/credits" in req.full_url else {
            **GRANT_NONE, "available": True, "eligible": True,
            "amount_minor_units": 500, "currency": "USD"}
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    outcome = credits.fetch_credits("tok", "org-1")
    assert outcome.error is None and not outcome.grant_failed
    assert outcome.credits["balance"] == 2000
    assert outcome.credits["grant"]["state"] == "available"
    assert sorted(r.full_url for r in seen) == sorted([
        "https://example.com/api/oauth/organizations/org-1/prepaid/credits",
        "https://example.com/api/oauth/organizations/org-1/overage_credit_grant"
        "?campaign=feature_of_the_week",
    ])
    for req in seen:
        assert req.get_method() == "GET"
        assert req.get_header("X-organization-uuid") == "org-1"
        assert req.get_header("Authorization") == "Bearer tok"
        assert req.get_header("Anthropic-beta") == "oauth-2025-04-20"


@pytest.mark.no_credits_fake
def test_fetch_failures(monkeypatch):
    monkeypatch.setattr(credits, "API_BASE", "https://example.com")
    answers: dict[str, object] = {}

    def fake_urlopen(req, timeout):
        key = "prepaid" if "prepaid" in req.full_url else "grant"
        answer = answers[key]
        if isinstance(answer, int):
            raise _http_error(req.full_url, answer)
        return _Resp(json.dumps(answer).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    answers.update(prepaid=404, grant=GRANT_NONE)
    assert credits.fetch_credits("t", "o") == credits.CreditsOutcome(credits={})
    answers.update(prepaid=401)
    assert credits.fetch_credits("t", "o").error == "http-401"
    answers.update(prepaid=prepaid(100), grant=500)
    outcome = credits.fetch_credits("t", "o")
    assert outcome.error is None and outcome.grant_failed
    assert outcome.credits == {"balance": 100, "currency": "USD", "expiring": []}


# -- the paced store -----------------------------------------------------------------


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _token(expires_ms: float | None = None) -> str:
    oauth: dict = {"accessToken": "tok"}
    if expires_ms is not None:
        oauth["expiresAt"] = expires_ms
    return json.dumps({"claudeAiOauth": oauth})


IDS = {"1": ("a@example.com", "org-a"), "2": ("b@example.com", "org-b")}


def _refresh(store, fetcher, *, force=False, creds=None, ids=IDS):
    creds = creds or {n: _token() for n in ids}
    return credits.refresh(store, ids, creds.__getitem__, force=force, fetcher=fetcher)


def test_hourly_cadence_and_forced_refresh(tmp_path: Path):
    clock = Clock()
    store = credits.CreditsStore(tmp_path, clock=clock)
    calls: list[str] = []

    def fetcher(token, org):
        calls.append(org)
        return credits.CreditsOutcome(credits={"balance": 100, "currency": "USD",
                                               "expiring": []})

    out = _refresh(store, fetcher)
    assert sorted(calls) == ["org-a", "org-b"]
    assert out["1"]["balance"] == 100 and out["1"]["fetchedAt"] == clock.t
    clock.t += 30
    _refresh(store, fetcher, force=True)        # within FORCE_MIN_S
    clock.t += 1800
    _refresh(store, fetcher)                    # within REFRESH_S
    assert len(calls) == 2
    _refresh(store, fetcher, force=True)        # forced, past FORCE_MIN_S
    assert len(calls) == 4
    clock.t += credits.REFRESH_S
    _refresh(store, fetcher)
    assert len(calls) == 6


def test_failure_backs_off_and_keeps_the_last_reading(tmp_path: Path):
    clock = Clock()
    store = credits.CreditsStore(tmp_path, clock=clock)
    good = credits.CreditsOutcome(credits={"balance": 100, "currency": "USD", "expiring": [],
                                           "grant": {"state": "available", "amount": 5,
                                                     "currency": "USD", "expiresAt": None}})
    _refresh(store, lambda t, o: good)
    clock.t += credits.REFRESH_S
    calls = []

    def failing(token, org):
        calls.append(org)
        return credits.CreditsOutcome(error="http-500")

    out = _refresh(store, failing)
    assert out["1"]["balance"] == 100           # stale-on-error
    clock.t += credits.BACKOFF_BASE_S - 1
    _refresh(store, failing, force=True)        # still backing off
    assert len(calls) == 2
    clock.t += 2
    _refresh(store, failing)
    assert len(calls) == 4                      # second failure: backoff doubles
    row = json.loads(store.path.read_text())["accounts"]["1"]
    assert row["consecutiveFailures"] == 2
    assert row["backoffUntil"] == pytest.approx(clock.t + 2 * credits.BACKOFF_BASE_S)
    # A Retry-After longer than the backoff wins.
    clock.t = row["backoffUntil"]
    _refresh(store, lambda t, o: credits.CreditsOutcome(error="http-429", retry_after_s=7200))
    row = json.loads(store.path.read_text())["accounts"]["1"]
    assert row["backoffUntil"] == pytest.approx(clock.t + 7200)
    # A grant-only failure keeps the previous grant.
    clock.t = row["backoffUntil"]
    out = _refresh(store, lambda t, o: credits.CreditsOutcome(
        credits={"balance": 50, "currency": "USD", "expiring": []}, grant_failed=True))
    assert out["1"]["balance"] == 50 and out["1"]["grant"]["state"] == "available"


def test_expired_or_missing_tokens_are_skipped_not_failed(tmp_path: Path):
    clock = Clock()
    store = credits.CreditsStore(tmp_path, clock=clock)
    calls = []

    def fetcher(token, org):
        calls.append(org)
        return credits.CreditsOutcome(credits={})

    expired = _token(expires_ms=(NOW - 3600) * 1000)
    _refresh(store, fetcher, creds={"1": expired, "2": ""})
    assert calls == []
    rows = json.loads(store.path.read_text())["accounts"]
    assert "consecutiveFailures" not in rows["1"]
    clock.t += credits.SKIP_RETRY_S - 1
    _refresh(store, fetcher)
    assert calls == []                          # deferred
    clock.t += 2
    _refresh(store, fetcher)
    assert sorted(calls) == ["org-a", "org-b"]


def test_a_reused_slot_never_shows_the_previous_accounts_reading(tmp_path: Path):
    store = credits.CreditsStore(tmp_path, clock=Clock())
    _refresh(store, lambda t, o: credits.CreditsOutcome(credits={"balance": 100}))
    other = {"1": ("c@example.com", "org-c")}
    assert store.readings(other) == {}


# -- surfaces ------------------------------------------------------------------------


def _seeded_switcher(sample_sequence_data: dict) -> ClaudeAccountSwitcher:
    accounts = sample_sequence_data["accounts"]
    accounts["1"].update(email="user@example.com", organizationUuid="org-uuid-5678",
                         organizationName="Acme Corp")
    accounts["2"].update(organizationUuid="org-2", organizationName="Other")
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, sample_sequence_data)
    return switcher


def _fake_fetch(token, org, timeout=5.0):
    return credits.CreditsOutcome(credits={
        "balance": 2000, "currency": "USD",
        "expiring": [{"amount": 1000, "currency": "USD", "name": None,
                      "expiresAt": _iso(NOW + 3 * DAY)}],
    })


def test_list_shows_a_credits_line_and_json_field(
    temp_home: Path, mock_org_claude_config: Path, sample_sequence_data: dict, capsys,
):
    from claude_swap import oauth

    switcher = _seeded_switcher(sample_sequence_data)
    creds = json.dumps({"claudeAiOauth": {"accessToken": "sk"}})
    with patch.object(switcher, "_read_active_credentials",
                      return_value=ActiveCredentials(creds, False)), \
         patch.object(switcher, "_read_account_credentials", return_value=creds), \
         patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome({"five_hour": {"pct": 1.0}})), \
         patch("claude_swap.credits.fetch_credits", side_effect=_fake_fetch):
        payload = switcher.list_accounts(json_output=True)
        capsys.readouterr()
        switcher.list_accounts()
    out = capsys.readouterr().out
    assert payload["activeAccountNumber"] == 1
    assert [a["credits"]["balance"] for a in payload["accounts"]] == [20.0, 20.0]
    assert f"credits: balance $20.00 · $10.00 expires {_day(NOW + 3 * DAY)}" in out


def test_list_hides_the_line_for_an_account_with_nothing(
    temp_home: Path, mock_org_claude_config: Path, sample_sequence_data: dict, capsys,
):
    from claude_swap import oauth

    switcher = _seeded_switcher(sample_sequence_data)
    creds = json.dumps({"claudeAiOauth": {"accessToken": "sk"}})
    with patch.object(switcher, "_read_active_credentials",
                      return_value=ActiveCredentials(creds, False)), \
         patch.object(switcher, "_read_account_credentials", return_value=creds), \
         patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome({"five_hour": {"pct": 1.0}})), \
         patch("claude_swap.credits.fetch_credits",
               return_value=credits.CreditsOutcome(credits=credits.parse_prepaid(PREPAID_EMPTY))):
        switcher.list_accounts()
    assert "credits:" not in capsys.readouterr().out


def test_list_without_a_reading_has_no_json_field(
    temp_home: Path, mock_org_claude_config: Path, sample_sequence_data: dict,
):
    from claude_swap import oauth

    switcher = _seeded_switcher(sample_sequence_data)
    creds = json.dumps({"claudeAiOauth": {"accessToken": "sk"}})
    with patch.object(switcher, "_read_active_credentials",
                      return_value=ActiveCredentials(creds, False)), \
         patch.object(switcher, "_read_account_credentials", return_value=creds), \
         patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome({"five_hour": {"pct": 1.0}})):
        payload = switcher.list_accounts(json_output=True)  # fetch blocked → failure
    assert all("credits" not in a for a in payload["accounts"])


def test_fleet_detail_panel_shows_the_credits_line():
    from dataclasses import replace

    from tests.maximize.test_fleet_home import NOW as FNOW, P, _by, _fleet
    from claude_swap.maximize.view import window_ticks
    from claude_swap.tui import fleet_render as render

    snap, mx, _state, rows, _msnap, _picks = _fleet()
    by_n = {a.number: a for a in snap.accounts}
    ctx = render.Ctx(P, window_ticks(mx), FNOW)
    plain = render.render_detail(_by(rows)["1"], by_n["1"], 117, ctx).plain
    assert "credits" not in plain
    reading = {"balance": 1230, "currency": "USD", "expiring": []}
    ctx = replace(ctx, credits={"1": reading})
    lines = render.render_detail(_by(rows)["1"], by_n["1"], 117, ctx).plain.splitlines()
    assert any(line.strip() == "credits balance $12.30" for line in lines)
    assert render.detail_height(_by(rows)["1"], by_n["1"], ctx) == len(lines)


# -- review fixes --------------------------------------------------------------------


@pytest.mark.no_credits_fake
def test_403_reads_as_empty_not_as_a_failure(monkeypatch):
    def fake(token, org, path, timeout=5.0):
        raise _http_error(path, 403)

    monkeypatch.setattr(credits, "request_org_json", fake)
    assert credits.fetch_credits("t", "o") == credits.CreditsOutcome(credits={})


def test_nan_and_infinity_never_raise():
    body = json.loads('{"amount": NaN, "promo_tranches": [{"remaining_amount_minor_units": '
                      'Infinity, "expires_at": "2030-01-01T00:00:00Z"}]}')
    assert credits.parse_prepaid(body) is None
    body = json.loads('{"amount": 100, "promo_tranches": [{"remaining_amount_minor_units": '
                      '1e400, "expires_at": "2030-01-01T00:00:00Z"}]}')
    assert credits.parse_prepaid(body)["expiring"] == []
    grant = json.loads('{"granted": true, "amount_minor_units": NaN}')
    assert credits.parse_grant(grant)["amount"] is None


def test_repeat_failures_log_at_debug(tmp_path: Path, caplog):
    import logging

    clock = Clock()
    store = credits.CreditsStore(tmp_path, clock=clock)

    def fail(token, org):
        return credits.CreditsOutcome(error="http-500")

    ids = {"1": IDS["1"]}
    creds = {"1": _token()}
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        for _ in range(2):
            credits.refresh(store, ids, creds.__getitem__, fetcher=fail,
                            names={"1": "main"})
            clock.t += credits.BACKOFF_CAP_S
    records = [r for r in caplog.records if "Credits fetch failed" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]
    # Named, never by slot number or email.
    assert records[0].getMessage() == "Credits fetch failed for main: http-500 (1 in a row)"
    assert all("@" not in r.getMessage() for r in records)


def test_a_slot_without_an_org_is_never_read(tmp_path: Path):
    store = credits.CreditsStore(tmp_path, clock=Clock())
    read: list[str] = []

    def creds(num):
        read.append(num)
        return _token()

    ids = {"1": ("a@example.com", ""), "2": IDS["2"]}
    credits.refresh(store, ids, creds, fetcher=lambda t, o: credits.CreditsOutcome(credits={}))
    assert read == ["2"]


def test_list_rereads_credentials_and_uses_the_short_timeout(
    temp_home: Path, mock_org_claude_config: Path, sample_sequence_data: dict, monkeypatch,
):
    from claude_swap import oauth
    from claude_swap.switcher import LIST_CREDITS_TIMEOUT_S

    switcher = _seeded_switcher(sample_sequence_data)
    stale = json.dumps({"claudeAiOauth": {"accessToken": "old"}})
    fresh = json.dumps({"claudeAiOauth": {"accessToken": "new"}})
    phase = {"credits": False}
    sent: list[tuple[str, float]] = []

    def read(*_a):
        return fresh if phase["credits"] else stale

    def usage(*_a, **_k):
        phase["credits"] = True  # the usage pass rotated the stored tokens
        return oauth.UsageOutcome({"five_hour": {"pct": 1.0}})

    def fake(token, org, path, timeout=5.0):
        sent.append((token, timeout))
        return {}

    monkeypatch.setattr(credits, "request_org_json", fake)
    with patch.object(switcher, "_read_active_credentials",
                      side_effect=lambda: ActiveCredentials(read(), False)), \
         patch.object(switcher, "_read_account_credentials", side_effect=read), \
         patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=usage):
        switcher.list_accounts(json_output=True)
    assert len(sent) == 4 and set(sent) == {("new", LIST_CREDITS_TIMEOUT_S)}
    assert LIST_CREDITS_TIMEOUT_S <= 3.0
