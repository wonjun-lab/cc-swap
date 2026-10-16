"""Accounts in use on another machine: detection (maximize/elsewhere.py).

Fakes only (example.com). A reading is (ts, 5h %, 5h reset)."""

from __future__ import annotations

from claude_swap.maximize import elsewhere as ew
from claude_swap.maximize.elsewhere import Reading

T0 = 1_000_000.0
M = 60.0


def tick(state, readings, *, now, active="1", busy_at=None, known=True, roster=None):
    return {ew.STATE_KEY: ew.update(
        state, readings, active=active, local_busy_at=busy_at, local_known=known,
        now=now, roster=roster,
    )}


def r(ts, pct5, reset5=None):
    return Reading(T0 + ts * M, pct5, None if reset5 is None else T0 + reset5 * M)


def two(first, second, **kw):
    """Slot 2 (not active) read twice."""
    state = tick(None, {"2": first}, now=first.ts, **kw)
    return tick(state, {"2": second}, now=second.ts, **kw)


def marked(state, now):
    return ew.marks(state, now)


class TestNonActive:
    def test_a_rise_of_two_points_within_30_minutes_marks_it(self):
        later = r(10, 12)
        state = two(r(0, 10), later)
        assert marked(state, later.ts) == {"2": later.ts}

    def test_a_rise_under_two_points_does_not(self):
        later = r(10, 11)
        assert marked(two(r(0, 10), later), later.ts) == {}

    def test_readings_more_than_30_minutes_apart_do_not(self):
        later = r(31, 30)
        assert marked(two(r(0, 10), later), later.ts) == {}

    def test_exactly_30_minutes_apart_counts(self):
        later = r(30, 14)
        assert marked(two(r(0, 10), later), later.ts) == {"2": later.ts}

    def test_it_expires_30_minutes_after_the_last_rise(self):
        later = r(10, 12)
        state = two(r(0, 10), later)
        assert marked(state, later.ts + 29 * M) == {"2": later.ts}
        assert marked(state, later.ts + 30 * M) == {}

    def test_a_further_rise_extends_it_and_keeps_since(self):
        state = two(r(0, 10), r(10, 12))
        again = r(25, 15)
        state = tick(state, {"2": again}, now=again.ts)
        assert marked(state, again.ts + 29 * M) == {"2": r(10, 12).ts}
        assert marked(state, r(10, 12).ts + 31 * M) == {"2": r(10, 12).ts}

    def test_a_flat_reading_does_not_extend_it(self):
        state = two(r(0, 10), r(10, 12))
        flat = r(25, 12)
        state = tick(state, {"2": flat}, now=flat.ts)
        assert marked(state, r(10, 12).ts + 31 * M) == {}

    def test_the_5h_reset_clears_it(self):
        later = r(10, 12, reset5=20)
        state = two(r(0, 10, reset5=20), later)
        assert marked(state, later.ts) == {"2": later.ts}
        assert marked(state, T0 + 20 * M) == {}  # at the reset itself
        rolled = r(25, 0)
        state = tick(state, {"2": rolled}, now=rolled.ts)
        assert marked(state, rolled.ts) == {}

    def test_a_drop_in_usage_clears_it_and_is_no_rise(self):
        state = two(r(0, 10), r(10, 12))
        down = r(12, 3)
        state = tick(state, {"2": down}, now=down.ts)
        assert marked(state, down.ts) == {}

    def test_an_account_that_was_active_since_the_earlier_reading_is_ours(self):
        state = tick(None, {"2": r(0, 10)}, now=T0, active="2", busy_at=T0 + 1)
        later = r(10, 14)
        state = tick(state, {"2": later}, now=later.ts, active="1")
        assert marked(state, later.ts) == {}

    def test_it_survives_a_restart(self):
        import json

        later = r(10, 12)
        state = json.loads(json.dumps(two(r(0, 10), later)))
        assert marked(state, later.ts + M) == {"2": later.ts}

    def test_an_older_reading_than_the_one_held_is_ignored(self):
        state = two(r(0, 10), r(10, 12))
        state = tick(state, {"2": r(5, 5)}, now=T0 + 11 * M)
        assert marked(state, T0 + 11 * M) == {"2": r(10, 12).ts}

    def test_a_slot_without_a_reading_keeps_its_record_and_gone_slots_drop(self):
        state = two(r(0, 10), r(10, 12))
        state = tick(state, {}, now=T0 + 11 * M)
        assert marked(state, T0 + 11 * M) == {"2": r(10, 12).ts}
        state = tick(state, {}, now=T0 + 12 * M, roster={"1"})
        assert marked(state, T0 + 12 * M) == {}

    def test_garbage_state_is_no_mark(self):
        assert ew.marks(None, T0) == {}
        assert ew.marks({ew.STATE_KEY: "x"}, T0) == {}
        assert ew.marks({ew.STATE_KEY: {"slots": {"2": 5}}}, T0) == {}


class TestActive:
    def run(self, *, busy_at, known=True, gap=10, rise=3):
        state = tick(None, {"1": r(0, 20)}, now=T0, busy_at=None, known=known)
        later = r(gap, 20 + rise)
        state = tick(state, {"1": later}, now=later.ts, busy_at=busy_at, known=known)
        return state, later.ts

    def test_a_rise_while_locally_idle_the_whole_interval_marks_it(self):
        state, now = self.run(busy_at=None)
        assert marked(state, now) == {"1": now}

    def test_busy_before_the_interval_still_counts_as_idle_in_it(self):
        state, now = self.run(busy_at=T0 - 5 * M)
        assert marked(state, now) == {"1": now}

    def test_a_rise_while_claude_code_was_busy_here_is_ours(self):
        state, now = self.run(busy_at=T0 + 4 * M)
        assert marked(state, now) == {}

    def test_busy_now_is_ours(self):
        state, now = self.run(busy_at=T0 + 10 * M)
        assert marked(state, now) == {}

    def test_unknown_local_activity_is_never_idle(self):
        state, now = self.run(busy_at=None, known=False)
        assert marked(state, now) == {}

    def test_a_small_rise_does_not(self):
        state, now = self.run(busy_at=None, rise=1)
        assert marked(state, now) == {}

    def test_the_busy_moment_is_remembered_across_ticks(self):
        state = tick(None, {"1": r(0, 20)}, now=T0)
        mid = r(5, 20)
        state = tick(state, {"1": mid}, now=mid.ts, busy_at=None)
        busy = T0 + 7 * M
        state = tick(state, {}, now=busy, busy_at=busy)  # a tick with no new reading
        later = r(10, 24)
        state = tick(state, {"1": later}, now=later.ts, busy_at=None)
        assert marked(state, later.ts) == {}


class TestEngineTracking:
    """``engine_hook._track_elsewhere``: readings in, state out."""

    class Engine:
        dry_run = False

        def __init__(self):
            self.saved = {}

        def _mutate_state(self, fn):
            fn(self.saved)

    def run(self, engine, rt, entries, usage, now, monkeypatch, local=(True, None)):
        from claude_swap.maximize import active_watch, engine_hook

        monkeypatch.setattr(active_watch, "local_activity", lambda e, n: local)
        return engine_hook._track_elsewhere(
            engine, rt, engine.saved, entries, usage, {"1": {}, "2": {}}, "1", now
        )

    def reading(self, ts, pct5):
        from types import SimpleNamespace

        entry = SimpleNamespace(fetched_at=T0 + ts * M)
        value = {"five_hour": {"pct": pct5, "resets_at": None}}
        return entry, value

    def rt(self, **kw):
        from types import SimpleNamespace

        return SimpleNamespace(settings=SimpleNamespace(avoid_elsewhere=True, **kw),
                               dry_elsewhere={})

    def test_a_rise_on_a_non_active_account_is_persisted_and_returned(self, monkeypatch):
        engine, rt = self.Engine(), self.rt()
        e0, u0 = self.reading(0, 10)
        assert self.run(engine, rt, {"2": e0}, {"2": u0}, T0, monkeypatch) == {}
        e1, u1 = self.reading(10, 13)
        got = self.run(engine, rt, {"2": e1}, {"2": u1}, T0 + 10 * M, monkeypatch)
        assert got == {"2": T0 + 10 * M}
        assert ew.marks(engine.saved, T0 + 11 * M) == got  # a restart reads the same

    def test_the_active_account_needs_local_idleness(self, monkeypatch):
        for local, expected in (((True, None), True), ((True, T0 + 5 * M), False),
                                ((False, None), False)):
            engine, rt = self.Engine(), self.rt()
            e0, u0 = self.reading(0, 10)
            self.run(engine, rt, {"1": e0}, {"1": u0}, T0, monkeypatch, local)
            e1, u1 = self.reading(10, 13)
            got = self.run(engine, rt, {"1": e1}, {"1": u1}, T0 + 10 * M, monkeypatch, local)
            assert ("1" in got) is expected, local

    def test_nothing_is_tracked_with_the_setting_off(self, monkeypatch):
        engine, rt = self.Engine(), self.rt()
        rt.settings.avoid_elsewhere = False
        e0, u0 = self.reading(0, 10)
        e1, u1 = self.reading(10, 13)
        assert self.run(engine, rt, {"2": e0}, {"2": u0}, T0, monkeypatch) == {}
        assert self.run(engine, rt, {"2": e1}, {"2": u1}, T0 + 10 * M, monkeypatch) == {}
        assert engine.saved == {}
