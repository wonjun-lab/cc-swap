"""The active account's usage between readings (maximize/estimate.py,
maximize/active_watch.py, maximize/limit_watch.py), the fallback pace
(maximize/policy.py), risk-tied polling (poll_policy) and the settings
reload, replayed against the 2026-10-06 incident
(tests/maximize/incident_replay.py)."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from claude_swap import poll_policy
from claude_swap.autoswitch import (
    ConfigWarningEvent,
    MaximizeDecisionEvent,
    PollEvent,
    SettingsChangedEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.maximize import estimate as est
from claude_swap.maximize import limit_watch, policy
from claude_swap.maximize.engine_hook import DECISION_KEY, runtime_for
from claude_swap.maximize.model import (
    AccountView,
    Hold,
    Sample,
    Snapshot,
    Switch,
    UsageEstimate,
)
from claude_swap.settings import MaximizeSettings
from claude_swap.usage_store import UsageEntry
from tests.maximize import incident_replay as ir
from tests.maximize.test_engine_maximize import make, of, win, write_settings

H = 3600.0


# -- the incident replay -----------------------------------------------------------------


@dataclass
class Replay:
    switched_min: float | None
    trigger: str | None
    real_pct: float | None
    reason: str | None


def replay(temp_home, settings: dict, reads: ir.ActiveReads | None = None,
           *, until_min: float = 125.0) -> tuple[Replay, object]:
    """Tick once a minute from 23:44 until the engine switches or the real
    5h reaches 100% (work stopped). #2 and #3 are fresh landing targets."""
    h = make(temp_home, maximize=settings)
    t0 = h.clock.now
    reads = reads or ir.ActiveReads(t0=t0)
    reads.t0 = t0
    minute = 0.0
    while minute <= until_min and ir.real_5h(minute) < 100.0:
        now = h.clock.now
        entries = {
            "1": reads.entry(now),
            "2": UsageEntry(last_good=win(0, 76), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 72), fetched_at=now, age_s=0.0),
        }
        if h.tick_with_entries(entries) is TickOutcome.SWITCHED:
            [switch] = of(h, SwitchEvent)
            reason = of(h, MaximizeDecisionEvent)[-1].reason
            return Replay(minute, switch.trigger, ir.real_5h(minute), reason), h
        h.clock.advance(60.0)
        minute += 1.0
    return Replay(None, None, None, None), h


def fresh_reads() -> ir.ActiveReads:
    """No 429s: the active account read every 180 s, as ml-a6000 read it."""
    return ir.ActiveReads(t0=0.0, fail_from_min=None, fail_until_min=None, poll_s=180.0)


#: ml-main's settings.json on 2026-10-06 (hard5h 97, forceEtaMin 3) and the
#: user's change after it (hard5h 90).
ML_MAIN = {"soft5h": 50, "hard5h": 97, "hard7d": 99, "forceEtaMin": 3}


class TestIncidentReplay:
    """23:44 /login, 429s on every read of the active account from 00:10 to
    01:36 while two machines used it; the real 5h hit 100% at ~01:44 and
    ml-main switched only at 01:45. Every replay must switch before 100%."""

    @pytest.mark.parametrize(("hard5h", "eta", "minute", "real"), [
        # (switch minute after 23:44, real 5h % then) — see the module notes
        (97, 3, 96, 70.2),    # 01:20
        (97, 10, 89, 65.7),   # 01:13
        (90, 3, 89, 65.7),    # 01:13
        (90, 10, 82, 61.3),   # 01:06
    ])
    def test_429s_switch_on_the_projection_before_100(self, temp_home, hard5h, eta, minute, real):
        r, h = replay(temp_home, {**ML_MAIN, "hard5h": hard5h, "forceEtaMin": eta})
        assert r.switched_min == minute and r.trigger == "hard"
        assert r.real_pct == pytest.approx(real, abs=0.1)
        assert r.real_pct < 100.0
        # Why it switched says the usage was projected, and why.
        assert "projected — usage reads rate-limited for" in r.reason
        poll = of(h, PollEvent)[-1]
        assert "projected" in poll.estimates["1"] and "~" in poll.human()

    @pytest.mark.parametrize(("hard5h", "eta", "minute", "real"), [
        (97, 3, 117, 94.0),   # 01:41 — 3 minutes of margin at the two-machine pace
        (97, 10, 111, 86.0),  # 01:35
        (90, 3, 111, 86.0),   # 01:35
        (90, 10, 108, 83.0),  # 01:32
    ])
    def test_fresh_readings_switch_on_the_pace_before_100(self, temp_home, hard5h, eta, minute, real):
        r, _h = replay(temp_home, {**ML_MAIN, "hard5h": hard5h, "forceEtaMin": eta}, fresh_reads())
        assert (r.switched_min, r.trigger) == (minute, "hard")
        assert r.real_pct == pytest.approx(real, abs=0.1)
        assert "projected" not in r.reason

    def test_the_projection_runs_at_the_learned_pace(self, temp_home):
        # Up to 00:10 the account was read every minute climbing ~55 %/h:
        # the learned steps carry that pace through the 429s.
        r, h = replay(temp_home, ML_MAIN, until_min=40)
        assert r.switched_min is None
        record = h.state()[DECISION_KEY]
        estimate = record["estimate"]
        assert estimate["kind"] == "projected" and estimate["cause"] == "usage reads rate-limited"
        assert estimate["rateSources"]["5h"] in ("learned", "recent pace")
        assert 30.0 <= estimate["ratesPctPerHour"]["5h"] <= 90.0
        assert estimate["projectedPct"]["5h"] > estimate["lastPct"]["5h"]


# -- the fallback pace (B) ----------------------------------------------------------------


def view(n: str, p5, p7, *, age_s=None, reset5=None) -> AccountView:
    return AccountView(
        number=n, email=f"{n}@example.com", tier="normal", plan_weight=4,
        pct5=p5, reset5=reset5, pct7=p7, reset7=None, quarantined=False, api_key=False,
        age_s=age_s,
    )


def snap(active: AccountView, *others: AccountView, **kw) -> Snapshot:
    settings = kw.pop("settings", MaximizeSettings(soft_5h=50, hard_5h=97, hard_7d=99,
                                                   force_eta_min=10))
    return Snapshot(
        now=kw.pop("now", 10_000_000.0), active=active.number, accounts=(active, *others),
        samples=kw.pop("samples", ()), last_switch_at=None, settings=settings, **kw,
    )


PROJECTED = UsageEstimate(kind="projected", note="5h ~88% projected — test",
                          rates={"5h": 60.0, "7d": 8.0})


class TestPace:
    def test_fresh_readings_without_a_velocity_keep_todays_behaviour(self):
        # First tick after a start or a switch: a fresh 88% and no samples
        # yet. No ETA is guessed from a default (S1): soft waits for idle.
        d = policy.decide(snap(view("1", 88, 90), view("2", 0, 70)))
        assert isinstance(d, Hold) and d.pending

    def test_a_projection_always_has_a_pace_for_the_eta(self):
        # The 01:36 situation: 88% (projected), no fresh samples.
        d = policy.decide(snap(view("1", 88, 90), view("2", 0, 70), estimate=PROJECTED))
        assert isinstance(d, Switch) and d.trigger == "hard"
        assert "reaches a hard cap in ~9.0 min" in d.reason
        assert "(5h ~88% projected — test)" in d.reason

    def test_measured_samples_win_over_the_projection_rates(self):
        now = 10_000_000.0
        flat = (Sample(now - 600, 88, 90), Sample(now - 300, 88, 90), Sample(now, 88, 90))
        d = policy.decide(snap(view("1", 88, 90), view("2", 0, 70), now=now, samples=flat,
                               estimate=PROJECTED))
        assert isinstance(d, Switch) and d.trigger == "soft"  # flat = idle, not forced

    def test_far_below_the_hard_cap_a_projection_changes_nothing(self):
        d = policy.decide(snap(view("1", 30, 40), view("2", 0, 70), estimate=PROJECTED))
        assert isinstance(d, Hold) and "under soft" in d.reason

    def test_first_tick_after_a_switch_on_a_5x_is_not_eta_forced(self, temp_home):
        # S1 regression: fresh 5h 80% on the first tick, idle-looking.
        h = make(temp_home, maximize={"hard5h": 90, "forceEtaMin": 10,
                                      "planOverride": "a@example.com:5x"})
        assert h.tick_with_usage({"1": win(80, 40), "2": win(0, 10), "3": win(0, 50)}) \
            is TickOutcome.NO_ACTION
        assert not of(h, SwitchEvent)


# -- the projection (unit) ----------------------------------------------------------------


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class TestProject:
    NOW = 2_000_000_000.0

    def entry(self, age: float, *, failures: int = 1, error: str | None = "http-429") -> UsageEntry:
        return UsageEntry(fetched_at=self.NOW - age, age_s=age,
                          consecutive_failures=failures, last_error=error if failures else None)

    def test_fresh_readings_are_never_projected(self):
        value = {"five_hour": {"pct": 20.0}, "seven_day": {"pct": 88.0}}
        assert est.project(number="1", value=value, entry=self.entry(120), now=self.NOW,
                           rates={"5h": 60.0}) is None
        assert est.project(number="1", value=value, entry=self.entry(300, failures=0),
                           now=self.NOW, rates={"5h": 60.0}) is None

    def test_last_reading_plus_rate_times_elapsed(self):
        value = {"five_hour": {"pct": 20.0, "resets_at": iso(self.NOW + 3 * H)},
                 "seven_day": {"pct": 88.0}}
        e = est.project(number="1", value=value, entry=self.entry(52 * 60), now=self.NOW,
                        rates={"5h": 60.0, "7d": 6.0})
        assert e.value["five_hour"]["pct"] == pytest.approx(72.0)
        assert e.value["seven_day"]["pct"] == pytest.approx(93.2)
        assert e.value["five_hour"]["resets_at"] == iso(self.NOW + 3 * H)
        assert e.note == "5h ~72% / 7d ~93% projected — usage reads rate-limited for 52m"
        assert value["five_hour"]["pct"] == 20.0  # the stored reading is untouched

    def test_capped_at_100(self):
        value = {"five_hour": {"pct": 90.0}, "seven_day": {"pct": 50.0}}
        e = est.project(number="1", value=value, entry=self.entry(2 * H), now=self.NOW,
                        rates={"5h": 60.0, "7d": 1.0})
        assert e.value["five_hour"]["pct"] == 100.0

    def test_a_reset_since_the_reading_restarts_from_zero(self):
        value = {"five_hour": {"pct": 95.0, "resets_at": iso(self.NOW - 30 * 60)},
                 "seven_day": {"pct": 50.0}}
        e = est.project(number="1", value=value, entry=self.entry(H), now=self.NOW,
                        rates={"5h": 40.0, "7d": 1.0})
        assert e.value["five_hour"]["pct"] == pytest.approx(20.0)
        assert e.value["five_hour"]["resets_at"] == iso(self.NOW - 30 * 60 + 5 * H)

    def test_a_network_failure_and_an_unpolled_reading_say_so(self):
        value = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}
        e = est.project(number="1", value=value, entry=self.entry(600, error="timeout"),
                        now=self.NOW, rates={"5h": 6.0})
        assert "usage reads failing (timeout) for 10m" in e.note
        e = est.project(number="1", value=value, entry=self.entry(900, failures=0),
                        now=self.NOW, rates={"5h": 6.0})
        assert "no usage read for 15m" in e.note

    def test_rates_prefer_the_faster_learned_pace_then_the_plan_default(self):
        now = self.NOW
        steps = {"1": {"5h": {"pct": 5, "stepAt": now, "intervals": [[now, 60.0]]}}}
        rates, sources = est.burn_rates(number="1", steps=steps, samples=(), plan="20x",
                                        idle_window_min=10, now=now)
        assert rates["5h"] == 60.0 and sources["5h"] == "learned"
        assert rates["7d"] == pytest.approx(60.0 * 0.165) and sources["7d"] == "5h × 0.165"
        rates, sources = est.burn_rates(number="1", steps=None, samples=(), plan="5x",
                                        idle_window_min=10, now=now)
        assert rates == {"5h": 120.0, "7d": pytest.approx(120.0 * 0.105)}
        assert sources == {"5h": "default 5x", "7d": "default 5x"}
        samples = (Sample(now - 600, 10, 50), Sample(now, 20, 51))
        rates, sources = est.burn_rates(number="1", steps=steps, samples=samples, plan=None,
                                        idle_window_min=10, now=now)
        assert rates["5h"] == 60.0 and rates["7d"] == pytest.approx(6.0)
        assert sources["7d"] == "recent pace"


class TestProjectionInTheEngine:
    def test_projection_moves_at_idle_once_it_crosses_soft(self, temp_home):
        """Soft on a projection waits for idle; idle is then this machine's
        transcripts going quiet (no usage samples come in)."""
        h = make(temp_home)
        h.clock.now = time.time()
        now = h.clock.now
        projects = temp_home / ".claude" / "projects" / "p"
        projects.mkdir(parents=True)
        transcript = projects / "s.jsonl"
        transcript.write_text('{"type":"user"}\n')
        os.utime(transcript, (now - 3600, now - 3600))   # quiet for an hour
        entries = {
            "1": UsageEntry(last_good=win(30, 40), fetched_at=now - 3600, age_s=3600.0,
                            consecutive_failures=5, last_error="http-429",
                            last_429_at=now, trust_extended=True),
            "2": UsageEntry(last_good=win(0, 10), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 50), fetched_at=now, age_s=0.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
        [switch] = of(h, SwitchEvent)
        assert switch.trigger == "soft"
        reason = of(h, MaximizeDecisionEvent)[-1].reason
        assert "projected — usage reads rate-limited for 1h00m" in reason
        assert "idle" in reason

    def test_busy_here_the_projection_waits_until_hard(self, temp_home):
        h = make(temp_home)
        h.clock.now = time.time()
        now = h.clock.now
        projects = temp_home / ".claude" / "projects" / "p"
        projects.mkdir(parents=True)
        (projects / "s.jsonl").write_text('{"type":"user"}\n')   # written just now
        entries = {
            "1": UsageEntry(last_good=win(30, 40), fetched_at=now - 3600, age_s=3600.0,
                            consecutive_failures=5, last_error="http-429",
                            last_429_at=now, trust_extended=True),
            "2": UsageEntry(last_good=win(0, 10), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 50), fetched_at=now, age_s=0.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        decision = of(h, MaximizeDecisionEvent)[-1]
        assert decision.pending and "Claude Code active here" in decision.reason

    def test_a_busy_claude_code_turn_is_not_idle(self, temp_home):
        """A long tool call writes no transcript, but Claude Code keeps its
        session ``busy`` for the whole turn (S3)."""
        h = make(temp_home)
        h.clock.now = time.time()
        now = h.clock.now
        projects = temp_home / ".claude" / "projects" / "p"
        projects.mkdir(parents=True)
        transcript = projects / "s.jsonl"
        transcript.write_text('{"type":"user"}\n')
        os.utime(transcript, (now - 3600, now - 3600))
        sessions = temp_home / ".claude" / "sessions"
        sessions.mkdir()
        (sessions / f"{os.getpid()}.json").write_text(
            json.dumps({"pid": os.getpid(), "status": "busy", "sessionId": "x"})
        )
        entries = {
            "1": UsageEntry(last_good=win(30, 40), fetched_at=now - 3600, age_s=3600.0,
                            consecutive_failures=5, last_error="http-429",
                            last_429_at=now, trust_extended=True),
            "2": UsageEntry(last_good=win(0, 10), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 50), fetched_at=now, age_s=0.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        assert "Claude Code active here" in of(h, MaximizeDecisionEvent)[-1].reason
        # The turn ends: idle again (a dead pid or "idle" status counts as idle).
        (sessions / f"{os.getpid()}.json").write_text(
            json.dumps({"pid": os.getpid(), "status": "idle", "sessionId": "x"})
        )
        h.clock.advance(60)
        entries["1"] = replace(entries["1"], age_s=3660.0)
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED

    def test_fresh_readings_change_nothing(self, temp_home):
        """Regression: far below soft with fresh readings, the decision, its
        reason and the poll line are exactly what they were."""
        h = make(temp_home)
        for _ in range(3):
            h.tick_with_usage({"1": win(20, 40), "2": win(0, 10), "3": win(0, 50)})
            h.clock.advance(180)
        decision = of(h, MaximizeDecisionEvent)[-1]
        assert decision.decision == "hold" and "projected" not in decision.reason
        poll = of(h, PollEvent)[-1]
        assert poll.estimates == {} and "~" not in poll.human()
        assert "estimate" not in h.state()[DECISION_KEY]

    def test_why_names_the_projection(self, temp_home):
        from claude_swap.maximize.doctor_cli import _why_lines

        lines = _why_lines({
            "source": "engine", "decision": "hold", "reason": "r", "active": "1",
            "ageS": 5, "estimate": {"note": "5h ~74% projected — usage reads rate-limited for 52m"},
        })
        assert "  usage    5h ~74% projected — usage reads rate-limited for 52m" in lines


# -- stale non-active readings ------------------------------------------------------------


class TestStaleLanding:
    def test_a_stale_candidate_is_no_landing_target(self):
        s = snap(view("1", 60, 40), view("2", 0, 10, age_s=40 * 60), view("3", 0, 60, age_s=60))
        assert [v.number for v in policy.landing_candidates(s)] == ["3"]

    def test_only_stale_candidates_still_take_a_forced_move(self):
        s = snap(view("1", 98, 40), view("2", 0, 10, age_s=40 * 60))
        assert policy.landing_candidates(s) == []
        d = policy.decide(s)
        assert isinstance(d, Switch) and d.target == "2" and d.trigger == "hard"

    def test_fresher_ones_go_first_in_a_forced_move(self):
        s = snap(view("1", 100, 40), view("2", 0, 10, age_s=40 * 60), view("3", 20, 95, age_s=60))
        d = policy.decide(s)
        assert isinstance(d, Switch) and d.target == "3" and d.trigger == "at-limit"

    def test_a_reading_on_the_post_429_cadence_still_lands(self):
        s = snap(view("1", 60, 40), view("2", 0, 10, age_s=30 * 60))
        assert [v.number for v in policy.landing_candidates(s)] == ["2"]


# -- the transcript watcher ---------------------------------------------------------------


def limit_record(ts: float, window: str = "five_hour", resets: float | None = None,
                 text: str = "You've hit your session limit · resets 3am (Asia/Seoul)") -> str:
    """The shape Claude Code 2.1.2xx writes (values redacted / invented)."""
    return json.dumps({
        "parentUuid": "00000000-0000-0000-0000-000000000000", "isSidechain": False,
        "userType": "external", "cwd": "/redacted", "sessionId": "redacted",
        "version": "2.1.289", "gitBranch": "main", "type": "assistant",
        "uuid": "redacted", "timestamp": iso(ts), "requestId": "req_redacted",
        "error": "rate_limit", "apiErrorStatus": 429, "isApiErrorMessage": True,
        "quotaLimits": {
            "status": "rejected", "rateLimitType": window,
            "resetsAt": int(resets if resets is not None else ts + 3 * H),
            "isUsingOverage": False, "overageStatus": "rejected",
            "overageDisabledReason": "redacted", "unifiedRateLimitFallbackAvailable": False,
        },
        "message": {"id": "redacted", "model": "<synthetic>", "role": "assistant",
                    "type": "message", "stop_reason": "stop_sequence",
                    "content": [{"type": "text", "text": text}]},
    })


class TestParseOk:
    def test_a_real_answer(self):
        line = json.dumps({"type": "assistant", "sessionId": "S", "timestamp": iso(1000.0),
                           "message": {"role": "assistant", "content": []}}).encode()
        assert limit_watch.parse_ok(line) == ("S", 1000.0)

    def test_an_api_error_or_another_record_is_not(self):
        assert limit_watch.parse_ok(limit_record(1000.0).encode()) is None
        user = json.dumps({"type": "user", "sessionId": "S", "timestamp": iso(1.0),
                           "message": {"role": "user", "content": "assistant"}}).encode()
        assert limit_watch.parse_ok(user) is None


class TestParseLine:
    def test_session_and_weekly_limits(self):
        hit = limit_watch.parse_line(limit_record(1000.0, resets=5000.0).encode())
        assert hit == limit_watch.LimitHit(
            ts=1000.0, window="5h", resets_at=5000.0, session_id="redacted"
        )
        hit = limit_watch.parse_line(limit_record(1000.0, "seven_day").encode())
        assert hit is not None and hit.window == "7d"

    def test_a_request_rate_throttle_or_a_model_limit_is_not_the_quota(self):
        burst = json.loads(limit_record(1000.0))
        del burst["quotaLimits"]
        burst["message"]["content"][0]["text"] = (
            "API Error: Request rejected (429) · This request would exceed your "
            "account's rate limit. Please try again later.")
        assert limit_watch.parse_line(json.dumps(burst).encode()) is None
        model = json.loads(limit_record(1000.0))
        del model["quotaLimits"]
        model["message"]["content"][0]["text"] = "You've reached your Fable limit."
        assert limit_watch.parse_line(json.dumps(model).encode()) is None

    def test_older_text_only_records(self):
        old = json.loads(limit_record(1000.0))
        del old["quotaLimits"]
        hit = limit_watch.parse_line(json.dumps(old).encode())
        assert hit is not None and hit.window == "5h" and hit.resets_at is None

    def test_anything_else_is_ignored(self):
        for line in (b"", b"{", b'{"type":"user","message":"rate_limit isApiErrorMessage"}',
                     json.dumps({**json.loads(limit_record(1.0)), "error": "server_error"}).encode()):
            assert limit_watch.parse_line(line) is None


class TestWatcher:
    def watcher(self, root) -> limit_watch.TranscriptWatcher:
        return limit_watch.TranscriptWatcher(root=lambda: root)

    def test_reads_only_what_was_appended(self, tmp_path):
        f = tmp_path / "p" / "s.jsonl"
        f.parent.mkdir()
        f.write_text('{"type":"user"}\n')
        w = self.watcher(tmp_path)
        now = time.time()
        assert w.poll(now) == []
        with f.open("a") as fh:
            fh.write(limit_record(now) + "\n")
        [hit] = w.poll(now)
        assert hit.window == "5h"
        assert w.poll(now) == []          # not again
        assert w.last_write is not None and w.available

    def test_a_partial_line_waits_for_its_end(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text("")
        w = self.watcher(tmp_path)
        now = time.time()
        w.poll(now)
        line = limit_record(now)
        with f.open("a") as fh:
            fh.write(line[:50])
        assert w.poll(now) == []
        with f.open("a") as fh:
            fh.write(line[50:] + "\n")
        assert len(w.poll(now)) == 1

    def test_a_replaced_or_truncated_file_is_read_from_its_tail(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text('{"type":"user"}\n' * 100)
        w = self.watcher(tmp_path)
        now = time.time()
        w.poll(now)
        f.write_text(limit_record(now) + "\n")     # shorter: truncated / rotated
        assert len(w.poll(now)) == 1

    def test_never_reads_a_whole_large_transcript(self, tmp_path):
        f = tmp_path / "s.jsonl"
        old = limit_record(time.time() - 60)
        filler = '{"type":"user","message":"' + "x" * 1000 + '"}\n'
        f.write_text(old + "\n" + filler * 1000)     # ~1 MB, the refusal at the top
        w = self.watcher(tmp_path)
        reads: list[int] = []
        real_open = open

        def spy(path, mode="r", *a, **k):
            fh = real_open(path, mode, *a, **k)
            if "b" in mode:
                read = fh.read
                fh.read = lambda n=-1: (reads.append(n), read(n))[1]  # type: ignore[method-assign]
            return fh

        with patch("builtins.open", spy):
            assert w.poll(time.time()) == []
        assert reads and max(reads) <= limit_watch.INITIAL_TAIL_BYTES

    def test_a_huge_single_line_is_skipped(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text("")
        w = self.watcher(tmp_path)
        now = time.time()
        w.poll(now)
        with f.open("a") as fh:
            fh.write("y" * (limit_watch.MAX_READ_BYTES + 10))
        assert w.poll(now) == []
        with f.open("a") as fh:
            fh.write("\n" + limit_record(now) + "\n")
        assert len(w.poll(now)) == 1

    def test_old_files_are_not_read(self, tmp_path):
        f = tmp_path / "s.jsonl"
        now = time.time()
        f.write_text(limit_record(now - 7200) + "\n")
        os.utime(f, (now - 7200, now - 7200))
        assert self.watcher(tmp_path).poll(now) == []

    def test_no_projects_directory(self, tmp_path):
        w = self.watcher(tmp_path / "missing")
        assert w.poll(time.time()) == [] and not w.available

    def test_a_capped_walk_says_it_is_incomplete(self, tmp_path, monkeypatch):
        for i in range(5):
            (tmp_path / f"s{i}.jsonl").write_text("{}\n")
        monkeypatch.setattr(limit_watch, "MAX_ENTRIES", 3)
        w = self.watcher(tmp_path)
        w.poll(time.time())
        assert w.available and not w.complete
        monkeypatch.setattr(limit_watch, "MAX_ENTRIES", 100)
        w.poll(time.time() + limit_watch.FULL_WALK_S)
        assert w.complete

    def test_between_full_walks_only_hot_files_and_their_directories(self, tmp_path):
        project = tmp_path / "p"
        project.mkdir()
        (project / "a.jsonl").write_text("{}\n")
        for i in range(30):
            cold = tmp_path / f"cold{i}"
            cold.mkdir()
            old = cold / "x.jsonl"
            old.write_text("{}\n")
            os.utime(old, (1, 1))
        w = self.watcher(tmp_path)
        now = time.time()
        w.poll(now)
        stats: list[str] = []
        real_stat = os.stat

        def spy(path, *a, **k):
            stats.append(str(path))
            return real_stat(path, *a, **k)

        time.sleep(0.01)
        (project / "b.jsonl").write_text(limit_record(now) + "\n")   # a new session
        os.utime(project, None)
        with patch("os.stat", spy):
            [hit] = w.poll(now + 60)
        assert hit.window == "5h"
        assert not any("cold" in p for p in stats)

    def test_a_new_project_directory_is_found_before_the_next_walk(self, tmp_path):
        w = self.watcher(tmp_path)
        now = time.time()
        w.poll(now)
        project = tmp_path / "new"
        project.mkdir()
        (project / "s.jsonl").write_text(limit_record(now) + "\n")
        os.utime(tmp_path, (now + 1, now + 1))
        assert len(w.poll(now + 60)) == 1


def ten_min(epoch: float) -> float:
    """On a 10-minute mark, as both reset fields are."""
    return float(int(epoch // 600) * 600)


class TestReportedLimit:
    """A refusal counts for the live account only when it is provably its
    own: its resetsAt is the live reading's reset for that window, or (no
    future reset in the reading, or no reset in the refusal) it came after a
    known moment the account went live."""

    def setup(self, temp_home, *, record_ts=None, record_reset="own", own_reset="future",
              session_id="redacted", fetched_after=False, text_only=False, fetched_ago=120.0):
        h = make(temp_home)
        h.clock.now = time.time()
        now = h.clock.now
        own = {"future": ten_min(now + 2 * H), "past": ten_min(now - 600), None: None}[own_reset]
        reset = own if record_reset == "own" else record_reset
        projects = temp_home / ".claude" / "projects" / "-redacted"
        projects.mkdir(parents=True, exist_ok=True)
        secret = "TOP SECRET PROMPT TEXT"
        record = json.loads(limit_record(
            record_ts if record_ts is not None else now - 30,
            resets=reset if reset is not None else now + H,
        ))
        record["sessionId"] = session_id
        if text_only:
            del record["quotaLimits"]
        (projects / "s.jsonl").write_text(
            json.dumps({"type": "user", "message": {"content": secret}}) + "\n"
            + json.dumps(record) + "\n"
        )
        fetched = now if fetched_after else now - fetched_ago
        value = win(40, 40, r5=own) if own is not None else win(40, 40)
        entries = {
            "1": UsageEntry(last_good=value, fetched_at=fetched, age_s=now - fetched),
            "2": UsageEntry(last_good=win(0, 10), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 50), fetched_at=now, age_s=0.0),
        }
        return h, entries, secret

    def went_live(self, h, at: float) -> None:
        from claude_swap.maximize import ledger

        ledger.append(h.switcher.backup_dir, ledger.make_entry(
            h.switcher.backup_dir, from_slot=2, to_slot=1, actor="engine",
            trigger="hard", source="test", now=at,
        ))

    def test_its_own_window_switches_at_once_busy_or_not(self, temp_home, caplog):
        h, entries, secret = self.setup(temp_home)
        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
        [switch] = of(h, SwitchEvent)
        assert switch.trigger == "at-limit"
        reason = of(h, MaximizeDecisionEvent)[-1].reason
        assert "Claude Code reported the 5h usage limit at" in reason
        assert "treating it as at its limit" in caplog.text
        everything = caplog.text + json.dumps([e.to_json() for e in h.events]) + json.dumps(h.state())
        assert secret not in everything and "hit your session limit" not in everything

    def test_a_reading_taken_after_the_refusal_wins(self, temp_home):
        h, entries, _ = self.setup(temp_home, fetched_after=True, record_ts=time.time() - 120)
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_a_session_still_on_the_previous_token_after_a_switch(self, temp_home):
        # Blocker case 2: switched onto #1 5 min ago; a Claude Code still on
        # the old account's token keeps getting refused after the grace.
        h, entries, _ = self.setup(temp_home, record_reset=ten_min(time.time() + H))
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now - 300,
                                                  lastSwitchTo="1"))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_a_cswap_run_share_history_session_is_not_the_live_login(self, temp_home):
        # Blocker case 1: the refusal's session is a `cswap run` profile's,
        # even if its reset happens to coincide with the live one's.
        h, entries, _ = self.setup(temp_home, session_id="run-session")
        sessions = h.switcher.backup_dir / "sessions" / "2-b_example.com" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "run-session"}))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_a_restart_after_a_switch_made_while_down(self, temp_home):
        # Blocker case 3: the engine was down while the user moved #2 -> #1;
        # the state still names #2. A refusal from minutes ago is #2's.
        h, entries, _ = self.setup(temp_home, record_reset=ten_min(time.time() + H),
                                   record_ts=time.time() - 300)
        h.engine._mutate_state(lambda s: s.update(
            lastSwitchAt=h.clock.now - 3 * H, lastSwitchTo="2",
            maximizeSamples={"account": "2", "samples": []},
        ))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_a_restart_after_a_switch_made_while_down_window_off(self, temp_home):
        # ... and with no reset in its own reading either (window off): the
        # engine only knows the account is live since its first look, now.
        h, entries, _ = self.setup(temp_home, own_reset=None, record_ts=time.time() - 300)
        h.engine._mutate_state(lambda s: s.update(
            lastSwitchTo="2", maximizeSamples={"account": "2", "samples": []},
        ))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_with_nothing_known_only_a_reset_match_counts(self, temp_home):
        # Blocker case 4: since unknown. A foreign reset is not taken ...
        h, entries, _ = self.setup(temp_home, record_reset=ten_min(time.time() + H))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_with_nothing_known_a_text_only_refusal_is_ignored(self, temp_home):
        h, entries, _ = self.setup(temp_home, text_only=True)
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_text_only_after_a_known_switch_counts(self, temp_home):
        h, entries, _ = self.setup(temp_home, text_only=True)
        self.went_live(h, h.clock.now - 3600)
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED

    def test_a_window_that_rolled_over_counts_after_a_known_switch(self, temp_home):
        # The reading's 5h reset passed; the refusal names the new window.
        h, entries, _ = self.setup(temp_home, own_reset="past",
                                   record_reset=ten_min(time.time() + 4 * H))
        self.went_live(h, h.clock.now - 3600)                          # the ledger says
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED

    def test_a_window_that_rolled_over_needs_a_known_switch(self, temp_home):
        h, entries, _ = self.setup(temp_home, own_reset="past",
                                   record_reset=ten_min(time.time() + 4 * H))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION   # since unknown

    def test_a_reset_match_before_the_switch_is_the_previous_accounts(self, temp_home):
        # Re-review 1: after a priming pass two accounts' 5h windows often
        # reset on the same 10-minute mark. #2's refusal from before the
        # switch onto #1 matches #1's reset but is not #1's. (No sessionId:
        # the old-token rule cannot help, the since rule must.)
        # #1's reading predates the refusal, so it cannot overrule it.
        h, entries, _ = self.setup(temp_home, record_ts=time.time() - 900, session_id=None,
                                   fetched_ago=1000.0)
        entries["1"] = replace(entries["1"], trust_extended=True)   # on its own plan
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now - 600,
                                                  lastSwitchTo="1"))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_a_reset_match_after_the_switch_counts(self, temp_home):
        h, entries, _ = self.setup(temp_home, record_ts=time.time() - 60)
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now - 600,
                                                  lastSwitchTo="1"))
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED

    def old_token_setup(self, temp_home, *, answered_after_switch: bool):
        """Session S was refused on #2 (reset X) before the switch onto #1
        10 min ago, and is refused again 1 min ago, still on #2's token
        (reset X). #1's 5h window has not started (no reset in its
        reading), so only the since rule could take the refusal."""
        h, entries, _ = self.setup(temp_home, own_reset=None, record_ts=time.time() - 900,
                                   record_reset=ten_min(time.time() + H), session_id="S")
        now = h.clock.now
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=now - 600, lastSwitchTo="1"))
        lines = []
        if answered_after_switch:
            lines.append(json.dumps({
                "type": "assistant", "sessionId": "S", "timestamp": iso(now - 300),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            }))
        again = json.loads(limit_record(now - 60, resets=ten_min(now + H)))
        again["sessionId"] = "S"
        lines.append(json.dumps(again))
        transcript = temp_home / ".claude" / "projects" / "-redacted" / "s.jsonl"
        with transcript.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
        return h, entries

    def test_a_session_still_on_the_old_token_is_not_the_live_account(self, temp_home):
        # Re-review 2: its new refusals are the previous account's.
        h, entries = self.old_token_setup(temp_home, answered_after_switch=False)
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        h.clock.advance(60)
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_once_that_session_is_answered_its_refusals_count_again(self, temp_home):
        h, entries = self.old_token_setup(temp_home, answered_after_switch=True)
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED

    def test_a_run_session_stays_set_aside_after_it_exits(self, temp_home):
        # Re-review 3: the exclusion is sticky; the pid file goes on exit
        # while the refusal stays in memory until its reset.
        h, entries, _ = self.setup(temp_home, session_id="run-session")
        sessions = h.switcher.backup_dir / "sessions" / "2-b_example.com" / "sessions"
        sessions.mkdir(parents=True)
        pid_file = sessions / "4242.json"
        pid_file.write_text(json.dumps({"pid": 4242, "sessionId": "run-session"}))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        pid_file.unlink()
        h.clock.advance(60)
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION

    def test_the_ledger_entry_must_land_on_the_live_account(self, temp_home):
        from claude_swap.maximize import ledger

        h, entries, _ = self.setup(temp_home, text_only=True)
        ledger.append(h.switcher.backup_dir, ledger.make_entry(
            h.switcher.backup_dir, from_slot=1, to_slot=2, actor="engine",
            trigger="hard", source="test", now=h.clock.now - 3600,
        ))
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION


# -- active-account polling at >= 80% -------------------------------------------------------


def plan(prev: dict | None, new: dict | None, *, recent_429=False,
         prev_interval=180.0, threshold=95.0, active=True) -> float:
    _at, interval = poll_policy.plan_after_fetch(
        prev_interval_s=prev_interval, prev_usage=prev, new_usage=new, is_active=active,
        threshold=threshold, models=(), recent_429=recent_429, now=10_000.0,
        rng=lambda: 0.5,
    )
    return interval


class TestActivePollingAtEighty:
    def test_far_below_and_moving_keeps_the_normal_cadence(self):
        assert plan(win(20, 10), win(23, 10)) == 180.0

    def test_not_moving_backs_off(self):
        assert plan(win(20, 79), win(20, 79)) == 270.0
        assert plan(win(20, 79), win(20, 79), prev_interval=300) == 300.0

    def test_79_is_the_normal_cadence_and_80_is_120_s(self):
        assert plan(win(75, 10), win(79, 10)) == 180.0
        assert plan(win(75, 10), win(80, 10)) == 120.0

    def test_a_7d_at_80_is_120_s_and_at_79_is_not(self):
        assert plan(win(5, 78), win(6, 79)) == 180.0
        assert plan(win(5, 79), win(6, 80)) == 120.0
        assert plan(win(5, 88), win(5, 88)) == 120.0  # not moving: still 120 s

    def test_a_recent_429_keeps_the_aimd_floor(self):
        assert plan(win(80, 88), win(84, 88), recent_429=True) >= \
            poll_policy.POST_429_MIN_INTERVAL_S

    def test_an_alternate_is_unchanged(self):
        assert plan(win(80, 88), win(84, 88), active=False) >= 180.0

    def test_retry_after_zero_backs_off_at_least_two_minutes(self):
        from claude_swap.usage_store import _failure_backoff_s

        assert _failure_backoff_s(1, 0.0) >= 120.0


class TestEscalation:
    def fetches(self, h, entries) -> list[set]:
        with patch.object(h.switcher, "usage_entries_by_account", return_value=entries) as m:
            h.engine.tick()
        return [c.kwargs.get("fetch") for c in m.call_args_list]

    def test_a_7d_in_the_80s_no_longer_refetches_every_candidate(self, temp_home):
        h = make(temp_home, maximize={"hard5h": 90, "hard7d": 99})
        now = h.clock.now
        entries = {
            "1": UsageEntry(last_good=win(20, 88), fetched_at=now, age_s=0.0),
            "2": UsageEntry(last_good=win(0, 76), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 72), fetched_at=now, age_s=0.0),
        }
        calls = self.fetches(h, entries)
        assert not any(c and {"2", "3"} <= c for c in calls)

    def test_near_the_cap_it_still_escalates(self, temp_home):
        h = make(temp_home, maximize={"hard5h": 90, "hard7d": 99, "forceEtaMin": 0})
        now = h.clock.now
        entries = {
            "1": UsageEntry(last_good=win(80, 40), fetched_at=now, age_s=0.0),
            "2": UsageEntry(last_good=win(0, 76), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 72), fetched_at=now, age_s=0.0),
        }
        calls = self.fetches(h, entries)   # default 40 %/h: 90% is 15 min away
        assert any(c and {"1", "2", "3"} <= c for c in calls)

    def test_the_active_accounts_post_429_plan_is_kept(self, temp_home):
        # S4: three machines on one account exceed ~30 reads/h at 180-300 s
        # each; after a 429 the AIMD plan (here 9 min) must hold on the
        # active account too, not be overridden as a "leftover candidate
        # plan" once the reading is 5 minutes old.
        h = make(temp_home, maximize={"hard5h": 90})
        now = h.clock.now
        entries = {
            "1": UsageEntry(last_good=win(20, 40), fetched_at=now - 400, age_s=400.0,
                            last_429_at=now - 900, poll_interval_s=540.0,
                            next_poll_at=now + 140, trust_extended=True),
            "2": UsageEntry(last_good=win(0, 76), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=win(0, 72), fetched_at=now, age_s=0.0),
        }
        calls = self.fetches(h, entries)
        assert not any(c and "1" in c for c in calls)

    def test_escalation_keeps_a_recent_429_plan(self, temp_home):
        h = make(temp_home, maximize={"hard5h": 90, "forceEtaMin": 0})
        now = h.clock.now
        entries = {
            "1": UsageEntry(last_good=win(80, 40), fetched_at=now, age_s=0.0),
            "2": UsageEntry(last_good=win(0, 76), fetched_at=now - 300, age_s=300.0,
                            last_429_at=now - 600, next_poll_at=now + 600),
            "3": UsageEntry(last_good=win(0, 72), fetched_at=now, age_s=0.0),
        }
        calls = self.fetches(h, entries)
        escalated = [c for c in calls if c and "3" in c]
        assert escalated and all("2" not in c for c in escalated)


# -- settings reload ------------------------------------------------------------------------


class TestSettingsReload:
    def test_config_set_takes_effect_on_the_next_tick(self, temp_home):
        h = make(temp_home, maximize={"hard5h": 97, "forceEtaMin": 3})
        usage = {"1": win(91, 40), "2": win(0, 10), "3": win(0, 50)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        write_settings(h, {"maximize": {"hard5h": 90, "forceEtaMin": 3}})
        h.clock.advance(60)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        [changed] = of(h, SettingsChangedEvent)
        assert changed.human() == "settings changed: hard5h 97 → 90"
        # The poll line of that same tick already shows the new mark.
        assert "hard 90" in of(h, PollEvent)[-1].human()
        assert runtime_for(h.engine).settings.hard_5h == 90.0

    def test_an_invalid_file_keeps_the_last_good_settings_and_says_so_once(self, temp_home):
        h = make(temp_home, maximize={"hard5h": 90})
        usage = {"1": win(20, 40), "2": win(0, 10), "3": win(0, 50)}
        h.tick_with_usage(usage)
        path = h.switcher.backup_dir / "settings.json"
        path.write_text("{not json")
        os.utime(path, ns=(1, 1))
        for _ in range(3):
            h.clock.advance(60)
            h.tick_with_usage(usage)
        warnings = [e for e in of(h, ConfigWarningEvent) if "unreadable" in e.message]
        assert len(warnings) == 1
        assert runtime_for(h.engine).settings.hard_5h == 90.0
        assert not of(h, SettingsChangedEvent)
