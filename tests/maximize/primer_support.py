"""Shared fakes for the primer's engine-facing tests (Task 11).

Not a test module: pytest collects ``test_*.py`` only.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from claude_swap.maximize.model import AccountView, Snapshot
from claude_swap.maximize.primer import (
    PrimeRunResult,
    Primer,
    expected_reset,
    run_prime,
)
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.settings import MaximizeSettings, PrimeSettings
from claude_swap.usage_store import UsageEntry
from tests.maximize.fake_claude import FakeClaude
from tests.test_autoswitch import EngineHarness, _iso_at

needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake claude is a POSIX shebang script"
)
H = 3600.0


def _usage(pct5=0.0, reset5=None, pct7=10.0, reset7=None) -> dict:
    five: dict = {"pct": pct5}
    if reset5 is not None:
        five["resets_at"] = _iso_at(reset5)
    seven: dict = {"pct": pct7}
    if reset7 is not None:
        seven["resets_at"] = _iso_at(reset7)
    return {"five_hour": five, "seven_day": seven}


class FakeUsage:
    """Stands in for ``switcher.usage_entries_by_account``. ``server[num]`` is
    what a fetch returns now; only accounts in the fetch set get a new
    ``fetched_at`` (``fetch=None`` fetches everything, like on-demand callers)."""

    def __init__(self, clock, on_fetch=None):
        self.clock = clock
        self.server: dict[str, dict] = {}
        self.stored: dict[str, UsageEntry] = {}
        self.on_fetch = on_fetch

    def reading(self, num: str, **kw) -> None:
        self.server[num] = _usage(**kw)

    def __call__(self, fetch=None, *, scheduled=False):
        now = self.clock()
        wanted = list(self.server) if fetch is None else list(fetch)
        for num in wanted:
            if num in self.server:
                if self.on_fetch is not None:
                    self.on_fetch(num)
                self.stored[num] = UsageEntry(
                    last_good=self.server[num], fetched_at=now, age_s=0.0
                )
        return dict(self.stored)


class StubRunner:
    """In-process runner: records each call and returns scripted results
    (default: success). ``opens=True`` makes the fake server open the
    window of whichever account's token was used, as the real API would."""

    def __init__(self, rig, results=(), *, opens=True):
        self.rig = rig
        self.results = list(results)
        self.opens = opens
        self.calls: list[dict] = []

    def __call__(self, argv, env, cwd, timeout_s=90.0):
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd})
        result = (
            self.results.pop(0)
            if self.results
            else PrimeRunResult(0, False, "", '{"is_error":false}', False)
        )
        if self.opens and result.returncode == 0 and not result.timed_out:
            num = env["CLAUDE_CODE_OAUTH_TOKEN"].removeprefix("sk-")
            self.rig.usage.reading(num, reset5=expected_reset(self.rig.clock()))
        return result

    def tokens(self) -> list[str]:
        return [c["env"]["CLAUDE_CODE_OAUTH_TOKEN"] for c in self.calls]


class Rig:
    """Seeded harness (slot 1 active; 2 and 3 idle) + fake usage + fake claude."""

    def __init__(self, temp_home: Path, tmp_path: Path, monkeypatch):
        self.harness = EngineHarness(temp_home)
        for num, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
            self.harness.seed(num, email)
        self.harness.make_live("a@example.com", 1)
        self.clock = self.harness.clock
        self.engine = self.harness.engine
        self.switcher = self.harness.switcher
        self.usage = FakeUsage(self.clock)
        monkeypatch.setattr(self.switcher, "usage_entries_by_account", self.usage)
        self.fake = FakeClaude.install(tmp_path / "fakebin")
        self.usage.reading("1", pct5=40.0, reset5=self.clock() + 2 * H)
        self.usage.reading("2")
        self.usage.reading("3")

    def view(self, num: str, **kw) -> AccountView:
        value = self.usage.server[num]
        five, seven = value["five_hour"], value["seven_day"]
        defaults = dict(
            number=num,
            email=self.switcher.account_email(num),
            tier="normal",
            plan_weight=1,
            pct5=five["pct"],
            reset5=parse_reset_ts(five.get("resets_at")),
            pct7=seven["pct"],
            reset7=parse_reset_ts(seven.get("resets_at")),
            quarantined=False,
            api_key=False,
        )
        defaults.update(kw)
        return AccountView(**defaults)

    def snap(self, active: str | None = "1", nums=("1", "2", "3")) -> Snapshot:
        return Snapshot(
            now=self.clock(),
            active=active,
            accounts=tuple(self.view(n) for n in nums),
            samples=(),
            last_switch_at=None,
            settings=MaximizeSettings(),
        )

    def primer(self, runner=None, **settings) -> Primer:
        kw = dict(enabled=True, claude_path=str(self.fake.path))
        kw.update(settings)
        return Primer(
            self.engine,
            PrimeSettings(**kw),
            runner=runner if runner is not None else run_prime,
            rng=random.Random(0),
            clock=self.clock,
            sleep=self.clock.advance,
        )

    def primes(self) -> dict:
        return self.harness.state().get("primes", {})
