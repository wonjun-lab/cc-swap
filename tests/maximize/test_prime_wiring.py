"""Priming inside the maximize tick (Task 11, cycle C): Task 8's primer slot
builds this section's Primer and emits what it returns; ``prime_snapshot``
gives the CLI the same view without a tick."""

from __future__ import annotations

import pytest

from claude_swap.autoswitch import PrimeEvent, TickOutcome
from claude_swap.maximize.engine_hook import runtime_for
from claude_swap.maximize.primer import Primer, expected_reset, prime_snapshot
from claude_swap.settings import atomic_write_json, settings_path
from tests.maximize.fake_claude import FakeClaude
from tests.maximize.primer_support import H, _usage, needs_posix
from tests.test_autoswitch import EngineHarness

WEEK = 3 * 24 * H


def _harness(temp_home, tmp_path, *, slots=3, maximize=None, prime=None):
    harness = EngineHarness(temp_home, strategy="maximize")
    for num, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com"))[:slots]:
        harness.seed(num, email)
    harness.make_live("a@example.com", 1)
    fake = FakeClaude.install(tmp_path / "fakebin")
    atomic_write_json(
        settings_path(harness.switcher.backup_dir),
        {
            "schemaVersion": 1,
            "autoswitch": {"strategy": "maximize"},
            "maximize": maximize or {},
            "prime": {"claudePath": str(fake.path), **(prime or {})},
        },
    )
    return harness, fake


def test_prime_snapshot_reads_engine_state(temp_home, tmp_path):
    harness, _ = _harness(temp_home, tmp_path, maximize={"lastResort": "c@example.com"})
    harness.make_live("b@example.com", 2)  # the active login moved to slot 2
    harness.engine._mutate_state(lambda s: s.update({"quarantine": {"3": {"email": "c@example.com"}}}))
    now = harness.clock()
    snap = prime_snapshot(
        harness.engine,
        {"1": _usage(pct5=30.0, reset5=now + H), "2": _usage(), "3": _usage()},
        now,
    )
    assert snap.active == "2"
    views = {v.number: v for v in snap.accounts}
    assert set(views) == {"1", "2", "3"}
    assert views["3"].quarantined and views["3"].tier == "last_resort"
    assert views["1"].reset5 == pytest.approx(now + H, abs=1)
    assert not any(v.api_key for v in snap.accounts)


def test_disabled_by_default_builds_no_primer(temp_home, tmp_path):
    harness, fake = _harness(temp_home, tmp_path)
    now = harness.clock()
    harness.tick_with_usage({
        "1": _usage(pct5=10.0, reset5=now + 2 * H, pct7=20.0, reset7=now + WEEK),
        "2": _usage(pct7=10.0, reset7=now + WEEK),
        "3": _usage(pct7=10.0, reset7=now + WEEK),
    })
    assert runtime_for(harness.engine).primer is None
    assert fake.calls() == []


@needs_posix
class TestPrimingTick:
    def test_tick_builds_primer_and_primes_one_account(self, temp_home, tmp_path):
        harness, fake = _harness(temp_home, tmp_path, prime={"enabled": True})
        now = harness.clock()
        outcome = harness.tick_with_usage({
            "1": _usage(pct5=10.0, reset5=now + 2 * H, pct7=20.0, reset7=now + WEEK),
            "2": _usage(pct7=10.0, reset7=now + WEEK),
            "3": _usage(pct7=10.0, reset7=now + WEEK),
        })
        assert outcome is not TickOutcome.ERROR
        assert isinstance(runtime_for(harness.engine).primer, Primer)
        assert harness.active_number() == 1
        [call] = fake.calls()  # one launch per tick
        assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] in {"sk-2", "sk-3"}

    def test_verification_event_reaches_the_stream_once(self, temp_home, tmp_path):
        harness, fake = _harness(temp_home, tmp_path, slots=2, prime={"enabled": True})
        now = harness.clock()
        active = _usage(pct5=10.0, reset5=now + 2 * H, pct7=20.0, reset7=now + WEEK)
        harness.tick_with_usage({"1": active, "2": _usage(pct7=10.0, reset7=now + WEEK)})
        prime_at = harness.state()["primes"]["b@example.com"]["lastAttemptAt"]
        harness.clock.advance(31)
        opened = expected_reset(prime_at)
        harness.tick_with_usage({"1": active, "2": _usage(reset5=opened, pct7=10.0, reset7=now + WEEK)})
        primes = [e for e in harness.events if isinstance(e, PrimeEvent)]
        assert [(e.account, e.outcome) for e in primes] == [("2", "primed")]
        assert len(fake.calls()) == 1

    def test_prime_idle_switch_and_last_resort_together(self, temp_home, tmp_path):
        """Spec §10 combination scenario: a soft-threshold idle switch, a
        last_resort account and priming in ONE tick. The switch must land on
        the normal account (2), and the primer must then prime only the
        last_resort account (3) — never the account that just became active."""
        harness, fake = _harness(
            temp_home, tmp_path,
            maximize={"lastResort": "c@example.com"}, prime={"enabled": True},
        )
        now = harness.clock()
        harness.engine._mutate_state(lambda s: s.update({
            "maximizeSamples": {
                "account": "1",
                "samples": [[now - 900, 60.0, 20.0], [now - 300, 60.0, 20.0]],
            },
        }))
        harness.tick_with_usage({
            "1": _usage(pct5=60.0, reset5=now + H, pct7=20.0, reset7=now + WEEK),
            "2": _usage(pct7=10.0, reset7=now + WEEK),
            "3": _usage(pct7=10.0, reset7=now + WEEK),
        })
        switches = [e for e in harness.events if e.kind == "switch"]
        assert [(s.to_ref or {}).get("number") for s in switches] == [2]
        assert harness.active_number() == 2
        tokens = [c["env"]["CLAUDE_CODE_OAUTH_TOKEN"] for c in fake.calls()]
        assert tokens == ["sk-3"]
        assert harness.state()["primes"]["c@example.com"]["lastOutcome"] == "launched"
