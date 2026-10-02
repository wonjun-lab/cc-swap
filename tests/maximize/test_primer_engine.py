"""The engine-facing Primer (Task 11, cycle B): verification, retries,
failure table (spec §6.3), safety checks, state bookkeeping."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from claude_swap import oauth
from claude_swap.autoswitch import ConfigWarningEvent, PrimeEvent
from claude_swap.maximize import primer as primer_mod
from claude_swap.maximize.model import Snapshot
from claude_swap.maximize.primer import BUCKET_S, PrimeRunResult, Primer, expected_reset
from claude_swap.settings import MaximizeSettings, PrimeSettings
from tests.maximize.primer_support import H, Rig, StubRunner, needs_posix
from tests.test_autoswitch import _iso_at


@pytest.fixture
def rig(temp_home, tmp_path, monkeypatch) -> Rig:
    return Rig(temp_home, tmp_path, monkeypatch)


class TestPrimer:
    @needs_posix
    def test_primes_cold_account_with_access_token_only(self, rig):
        rig.fake.behave({"writeCredentials": True})
        events = rig.primer().run_due(rig.snap(nums=("1", "2")))
        assert events == []  # launched; verification comes later
        [call] = rig.fake.calls()
        profile = rig.switcher.backup_dir / "prime-profile"
        env = call["env"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-2"
        assert env["CLAUDE_CONFIG_DIR"] == str(profile)
        assert not any("rt-2" in v for v in env.values())
        assert not any("rt-2" in a for a in call["argv"])
        assert call["argv"][call["argv"].index("--model") + 1] == "claude-haiku-4-5"
        assert call["stdinIsDevnull"] is True
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700
        assert not (profile / ".credentials.json").exists()  # scrubbed after the run
        entry = rig.primes()["b@example.com"]
        assert entry["attempts"] == 1
        assert entry["lastOutcome"] == "launched"
        assert entry["lastAttemptAt"] == rig.clock()
        assert entry["windowKey"] == "cold"

    def test_scrubs_prime_profile_keychain_item_before_and_after(self, rig, monkeypatch):
        scrubbed: list[Path] = []
        monkeypatch.setattr(primer_mod, "delete_macos_keychain_entry", scrubbed.append)
        rig.primer(runner=StubRunner(rig)).run_due(rig.snap())
        profile = rig.switcher.backup_dir / "prime-profile"
        assert scrubbed == [profile, profile]

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    def test_interrupted_run_still_scrubs_the_profile(self, rig, monkeypatch, interrupt):
        scrubbed: list[Path] = []
        monkeypatch.setattr(primer_mod, "delete_macos_keychain_entry", scrubbed.append)
        profile = rig.switcher.backup_dir / "prime-profile"

        def runner(argv, env, cwd, timeout_s=90.0):
            (profile / ".credentials.json").write_text("{}")  # the child saved a login
            raise interrupt()

        with pytest.raises(interrupt):
            rig.primer(runner=runner).run_due(rig.snap(nums=("1", "2")))
        assert scrubbed == [profile, profile]
        assert not (profile / ".credentials.json").exists()

    def test_verification_success(self, rig):
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))  # the engine's view: still cold
        primer.run_due(snap)
        prime_at = rig.clock()
        rig.clock.advance(31)
        events = primer.run_due(snap)
        assert [(e.account, e.outcome, e.resets_at) for e in events] == [
            ("2", "primed", _iso_at(expected_reset(prime_at)))
        ]
        entry = rig.primes()["b@example.com"]
        assert entry["lastOutcome"] == "primed"
        assert entry["resetsAt"] == expected_reset(prime_at)
        assert len(runner.calls) == 1  # the stale cold snapshot does not re-prime

    def test_unverified_retries_once_then_gives_up(self, rig):
        runner = StubRunner(rig, opens=False)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        primer.run_due(snap)                       # attempt 1
        rig.clock.advance(31)
        events = primer.run_due(snap)              # verify: off → retry now (attempt 2)
        assert [e.outcome for e in events] == ["unverified"]
        assert runner.tokens() == ["sk-2", "sk-2"]
        rig.clock.advance(31)
        events = primer.run_due(snap)              # verify: off → attempts exhausted
        assert [e.outcome for e in events] == ["unverified"]
        assert len(runner.calls) == 2
        assert rig.primes()["b@example.com"]["attempts"] == 2

    def test_timeout_is_verified_before_any_retry(self, rig):
        runner = StubRunner(rig, [PrimeRunResult(None, True, "", "")], opens=False)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        events = primer.run_due(snap)
        assert [e.outcome for e in events] == ["timeout"]
        rig.clock.advance(10)
        assert primer.run_due(snap) == []          # still inside the verify delay
        assert len(runner.calls) == 1
        rig.usage.reading("2", reset5=expected_reset(rig.clock() - 10))  # it did land
        rig.clock.advance(25)
        events = primer.run_due(snap)
        assert [e.outcome for e in events] == ["primed"]
        assert len(runner.calls) == 1

    def test_precheck_skips_window_opened_elsewhere(self, rig):
        runner = StubRunner(rig)
        snap = Snapshot(  # the engine still believes slot 2 is cold
            now=rig.clock(), active="1",
            accounts=(rig.view("1"), rig.view("2")),
            samples=(), last_switch_at=None, settings=MaximizeSettings(),
        )
        opened = rig.clock() + 4 * H
        rig.usage.reading("2", reset5=opened)  # another machine primed it meanwhile
        events = rig.primer(runner=runner).run_due(snap)
        assert [(e.outcome, e.resets_at) for e in events] == [("already-on", _iso_at(opened))]
        assert runner.calls == []
        entry = rig.primes()["b@example.com"]
        assert (entry["lastOutcome"], entry["resetsAt"]) == ("already-on", opened)

    def test_auth_failure_refreshes_token_next_tick(self, rig, monkeypatch):
        rejected = PrimeRunResult.from_output(
            1, '{"is_error":true,"api_error_status":401,"result":"Invalid API key · Please run /login"}', ""
        )
        runner = StubRunner(rig, [rejected], opens=False)
        refreshed = json.dumps({"claudeAiOauth": {"accessToken": "sk-2b", "refreshToken": "rt-2b"}})
        grants: list[str] = []

        def consume(num, email, snapshot):
            grants.append(num)
            return oauth.RefreshOutcome(refreshed, None)

        monkeypatch.setattr(rig.switcher, "consume_backup_grant", consume)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        events = primer.run_due(snap)
        assert [(e.outcome, e.detail) for e in events] == [
            ("failed", "token rejected (401); refreshing it next tick")
        ]
        primer.run_due(snap)
        assert grants == ["2"]
        assert runner.tokens() == ["sk-2", "sk-2b"]
        assert not any("rt-2b" in v for v in runner.calls[1]["env"].values())

    def test_invalid_grant_quarantines_without_launching(self, rig, monkeypatch):
        monkeypatch.setattr(rig.engine, "_freshen_target", lambda num, email: "invalid_grant")
        runner = StubRunner(rig)
        events = rig.primer(runner=runner).run_due(rig.snap(nums=("1", "2")))
        assert [(e.outcome, e.detail) for e in events] == [("failed", "invalid_grant")]
        assert runner.calls == []
        assert "2" in rig.harness.state()["quarantine"]
        assert "account-quarantined" in rig.harness.kinds()

    def test_rate_limit_waits_for_weekly_reset(self, rig):
        limited = PrimeRunResult(1, False, "API Error: 429 rate_limit_error", "", True)
        runner = StubRunner(rig, [limited])
        primer = rig.primer(runner=runner)
        rig.usage.reading("2", reset7=rig.clock() + 2 * H)
        snap = rig.snap(nums=("1", "2"))
        events = primer.run_due(snap)
        assert [e.outcome for e in events] == ["failed"]
        rig.clock.advance(H)
        assert primer.run_due(snap) == []
        assert len(runner.calls) == 1

    def test_model_404_retries_once_with_alias(self, rig):
        missing = PrimeRunResult(1, False, 'API Error: 404 {"error":{"type":"not_found_error"}}', "", True)
        runner = StubRunner(rig, [missing])
        events = rig.primer(runner=runner).run_due(rig.snap(nums=("1", "2")))
        assert events == []
        models = [c["argv"][c["argv"].index("--model") + 1] for c in runner.calls]
        assert models == ["claude-haiku-4-5", "haiku"]
        assert rig.primes()["b@example.com"]["attempts"] == 1

    def test_missing_claude_disables_once(self, rig, tmp_path):
        primer = rig.primer(claude_path=str(tmp_path / "missing" / "claude"))
        first = primer.run_due(rig.snap())
        assert [type(e) for e in first] == [ConfigWarningEvent, PrimeEvent]
        assert first[1].outcome == "disabled"
        assert primer.run_due(rig.snap()) == []  # warned once per Primer

    def test_events_are_returned_not_emitted(self, rig):
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        primer.run_due(rig.snap(nums=("1", "2")))
        rig.clock.advance(31)
        events = primer.run_due(rig.snap(nums=("1", "2")))
        assert [e.outcome for e in events] == ["primed"]
        assert not any(isinstance(e, PrimeEvent) for e in rig.harness.events)

    def test_live_session_is_skipped_and_rechecked_later(self, rig, monkeypatch):
        monkeypatch.setattr(
            rig.switcher, "live_session_pids_for",
            lambda num, email: [4242] if num == "2" else [],
        )
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        events = primer.run_due(snap)
        assert [e.outcome for e in events] == ["skipped-live"]
        assert primer.run_due(snap) == []  # no event spam on the next tick
        assert runner.calls == []

    def test_never_primes_the_account_that_just_became_active(self, rig):
        rig.harness.make_live("b@example.com", 2)  # a switch landed this tick
        runner = StubRunner(rig)
        events = rig.primer(runner=runner).run_due(rig.snap(active="1", nums=("1", "2")))
        assert runner.calls == []
        assert [(e.account, e.outcome) for e in events] == [("2", "skipped-active")]

    @pytest.mark.parametrize("during", ["usage-fetch", "token"])
    def test_switch_during_the_checks_skips_without_spending_an_attempt(
        self, rig, monkeypatch, during
    ):
        switched: list[bool] = []

        def switch_to_2():
            if not switched:
                switched.append(True)
                rig.harness.make_live("b@example.com", 2)

        freshened: list[str] = []
        original = rig.engine._freshen_target

        def freshen(num, email):
            freshened.append(num)
            if during == "token":
                switch_to_2()
            return original(num, email)

        monkeypatch.setattr(rig.engine, "_freshen_target", freshen)
        if during == "usage-fetch":
            rig.usage.on_fetch = lambda num: switch_to_2() if num == "2" else None
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        events = primer.run_due(snap)
        assert runner.calls == []
        assert [(e.account, e.outcome) for e in events] == [("2", "skipped-active")]
        if during == "usage-fetch":
            assert freshened == []  # re-checked before the token step
        entry = rig.primes()["b@example.com"]
        assert (entry["lastOutcome"], entry["attempts"]) == ("skipped-active", 0)
        rig.harness.make_live("a@example.com", 1)  # switched back: eligible again
        primer.run_due(snap)
        assert runner.tokens() == ["sk-2"]
        assert rig.primes()["b@example.com"]["attempts"] == 1

    def test_one_launch_per_tick(self, rig):
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        primer.run_due(rig.snap())
        assert len(runner.calls) == 1
        primer.run_due(rig.snap())
        assert sorted(runner.tokens()) == ["sk-2", "sk-3"]

    def test_dry_run_engine_never_primes(self, rig):
        runner = StubRunner(rig)
        dry = Primer(
            rig.harness._make_engine(dry_run=True),
            PrimeSettings(enabled=True, claude_path=str(rig.fake.path)),
            runner=runner, clock=rig.clock,
        )
        assert dry.run_due(rig.snap()) == []
        assert runner.calls == []
        assert rig.primes() == {}

    def test_concurrent_claim_prevents_double_launch(self, rig):
        def other_process_launches(num):
            if num == "2":
                rig.engine._mutate_state(lambda s: s.setdefault("primes", {}).update({
                    "b@example.com": {"windowKey": "cold", "attempts": 1,
                                      "lastAttemptAt": rig.clock() - 1, "lastOutcome": "launched"},
                }))

        rig.usage.on_fetch = other_process_launches
        runner = StubRunner(rig)
        rig.primer(runner=runner).run_due(rig.snap(nums=("1", "2")))
        assert runner.calls == []

    def test_no_launch_in_the_last_seconds_of_a_bucket(self, rig):
        rig.clock.now = (rig.clock.now // BUCKET_S + 1) * BUCKET_S - 5
        runner = StubRunner(rig)
        rig.primer(runner=runner).run_due(rig.snap())
        assert runner.calls == []


class TestForcedRefresh:
    """The retry after a 401 spends exactly one refresh grant, and that grant
    gets the same identity check as the engine's ``_freshen_target``."""

    REJECTED = PrimeRunResult.from_output(
        1, '{"is_error":true,"api_error_status":401,"result":"Invalid API key"}', ""
    )

    def test_one_grant_per_attempt_even_near_expiry(self, rig, monkeypatch):
        # Inside _freshen_target's 10-minute buffer, so it would refresh too.
        rig.harness.seed(2, "b@example.com", expires_at=int((rig.clock() + 300) * 1000))
        refreshed = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2b", "refreshToken": "rt-2b",
            "expiresAt": int((rig.clock() + 8 * H) * 1000),
        }})
        grants: list[str] = []

        def consume(num, email, snapshot):
            grants.append(num)
            return oauth.RefreshOutcome(refreshed, None)

        monkeypatch.setattr(rig.switcher, "consume_backup_grant", consume)
        runner = StubRunner(rig, [self.REJECTED], opens=False)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        primer.run_due(snap)  # near-expiry freshen, launch, 401
        assert grants == ["2"]
        grants.clear()
        primer.run_due(snap)  # the forced refresh, and nothing else
        assert grants == ["2"]
        assert runner.tokens() == ["sk-2", "sk-2b"]

    def test_forced_refresh_checks_the_token_identity(self, rig, monkeypatch):
        refreshed = json.dumps({"claudeAiOauth": {"accessToken": "sk-2b", "refreshToken": "rt-2b"}})
        someone_else = {"uuid": "uuid-someone-else", "email": None, "organizationUuid": None}
        monkeypatch.setattr(
            rig.switcher, "consume_backup_grant",
            lambda num, email, snapshot: oauth.RefreshOutcome(refreshed, None, someone_else),
        )
        runner = StubRunner(rig, [self.REJECTED], opens=False)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        primer.run_due(snap)
        events = primer.run_due(snap)
        assert [(e.outcome, e.detail) for e in events] == [("failed", "identity-conflict")]
        assert runner.tokens() == ["sk-2"]  # the other account's token never ran
        assert rig.harness.state()["quarantine"]["2"]["reason"] == "identity-conflict"
        assert rig.primes()["b@example.com"]["lastOutcome"] == "identity-conflict"


def _bucket_end(rig) -> float:
    return (rig.clock.now // BUCKET_S + 1) * BUCKET_S


class TestLaunchTiming:
    """Verification compares against the launch instant (spec §6.2 step 5),
    so that instant is read right before the claim — after every slow step —
    and the bucket-boundary guard is applied to it, not to the tick's start."""

    @pytest.fixture(params=["usage-fetch", "token"])
    def slow_step(self, request, rig, monkeypatch):
        """``slow_step(s)``: account 2's next pre-launch step takes ``s`` seconds."""

        def make(seconds: float) -> None:
            if request.param == "usage-fetch":
                pending = [seconds]

                def on_fetch(num):
                    if num == "2" and pending:
                        rig.clock.advance(pending.pop())

                rig.usage.on_fetch = on_fetch
            else:
                original = rig.engine._freshen_target

                def slow(num, email):
                    rig.clock.advance(seconds)
                    return original(num, email)

                monkeypatch.setattr(rig.engine, "_freshen_target", slow)

        return make

    def test_slow_step_records_the_actual_launch_time(self, rig, slow_step):
        end = _bucket_end(rig)
        rig.clock.now = end - 20      # the tick-start guard lets this through
        slow_step(25)                 # ...but the launch lands in the next bucket
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        primer.run_due(snap)
        assert len(runner.calls) == 1
        assert rig.primes()["b@example.com"]["lastAttemptAt"] == end + 5
        rig.clock.advance(31)
        events = primer.run_due(snap)
        assert [(e.outcome, e.resets_at) for e in events] == [
            ("primed", _iso_at(expected_reset(end + 5)))
        ]

    def test_slow_step_into_the_guard_defers_without_spending_an_attempt(self, rig, slow_step):
        end = _bucket_end(rig)
        rig.clock.now = end - 40
        slow_step(30)                 # now 10 s before the boundary
        runner = StubRunner(rig)
        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        assert primer.run_due(snap) == []
        assert runner.calls == []
        assert "b@example.com" not in rig.primes()
        rig.clock.now = end + 301     # next bucket, past the widest jitter
        primer.run_due(snap)
        assert len(runner.calls) == 1
        entry = rig.primes()["b@example.com"]
        assert entry["attempts"] == 1
        assert entry["lastAttemptAt"] >= end + 301

    def test_prime_now_waits_out_the_guard_instead_of_launching(self, rig):
        end = _bucket_end(rig)
        rig.clock.now = end - 5
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            rig.clock.advance(seconds)

        runner = StubRunner(rig)
        events = rig.primer(runner=runner).prime_now(rig.snap(nums=("1", "2")), {"2"}, sleep=sleep)
        assert sleeps[0] == 6.0       # until 1 s past the boundary
        assert len(runner.calls) == 1
        assert rig.primes()["b@example.com"]["lastAttemptAt"] == end + 1
        assert [e.outcome for e in events] == ["primed"]

    def test_model_fallback_relaunch_is_guarded_and_timed(self, rig):
        end = _bucket_end(rig)
        rig.clock.now = end - 20
        missing = PrimeRunResult(1, False, 'API Error: 404 {"error":{"type":"not_found_error"}}', "", True)
        stub = StubRunner(rig, [missing])

        def runner(argv, env, cwd, timeout_s=90.0):
            result = stub(argv, env, cwd, timeout_s)
            if len(stub.calls) == 1:
                rig.clock.advance(10)  # the 404 run took 10 s: now inside the guard
            return result

        primer = rig.primer(runner=runner)
        snap = rig.snap(nums=("1", "2"))
        primer.run_due(snap)
        assert len(stub.calls) == 2
        entry = rig.primes()["b@example.com"]
        assert (entry["attempts"], entry["lastAttemptAt"]) == (1, end + 1)
        rig.clock.advance(31)
        assert [e.outcome for e in primer.run_due(snap)] == ["primed"]
