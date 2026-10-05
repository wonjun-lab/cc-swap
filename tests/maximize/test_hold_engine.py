"""An account hold (``cc-swap hold``) through the real engine (EngineHarness):
soft and preempt moves are set aside, hard and at-limit still switch, and
the hold ends at its end time or with any change of the active account."""

from __future__ import annotations

from unittest.mock import patch

from claude_swap.autoswitch import ConfigWarningEvent, SwitchEvent, TickOutcome
from claude_swap.maximize import hold
from claude_swap.maximize.engine_hook import DECISION_KEY
from tests.maximize.test_engine_maximize import EMAILS, make, no_switch_reasons, of, win
from tests.test_autoswitch import _entry_for
from tests.maximize.test_history_engine import (  # noqa: F401 (utc is an autouse fixture)
    FRIDAY_NOON,
    USAGE,
    seed,
    two_idle_ticks,
    utc,
)

H = 3600.0
SOFT = {"1": win(62, 40), "2": win(0, 10), "3": win(0, 50)}
HARD = {"1": win(96, 40), "2": win(0, 10), "3": win(0, 50)}
LIMIT = {"1": win(100, 40), "2": win(0, 10), "3": win(0, 50)}


def pin(h, slot: str = "1", *, hours: float = 2.0, now: float | None = None) -> hold.AccountHold:
    now = h.clock.now if now is None else now
    return hold.set_hold(h.switcher.backup_dir, slot, now + hours * H, by="test", now=now)


def marker(h) -> hold.AccountHold | None:
    return hold.marker(h.switcher.backup_dir, h.state())


def lifted(h) -> list[str]:
    return [e.message for e in of(h, ConfigWarningEvent) if e.message.startswith("hold on ")]


def test_soft_is_set_aside_while_the_hold_lasts(temp_home):
    h = make(temp_home)
    pin(h)
    for _ in range(4):  # idle for 15 minutes: without the hold, a soft switch
        assert h.tick_with_usage(SOFT) is TickOutcome.NO_ACTION
        h.clock.advance(300)
    assert h.active_number() == 1 and not of(h, SwitchEvent)
    assert no_switch_reasons(h) == ["hold"] * 4
    record = h.state()[DECISION_KEY]
    assert (record["decision"], record["code"], record["pending"]) == ("hold", "hold", False)
    assert record["reason"].startswith("a held until ")
    assert "otherwise: a 5h 62% >= soft 50%" in record["reason"]
    assert marker(h) is not None and not lifted(h)


def test_preempt_is_set_aside_while_the_hold_lasts(temp_home):
    h = make(temp_home, maximize={"tieEpsilon": 2})
    seed(h)
    pin(h, hours=4, now=FRIDAY_NOON)
    assert two_idle_ticks(h) is TickOutcome.NO_ACTION
    assert no_switch_reasons(h) == ["hold", "hold"]
    assert "otherwise: a 7d 84% would pass 90%" in h.state()[DECISION_KEY]["reason"]
    assert h.active_number() == 1


def test_preempt_moves_once_the_hold_is_lifted(temp_home):
    h = make(temp_home, maximize={"tieEpsilon": 2})
    seed(h)
    pin(h, hours=4, now=FRIDAY_NOON)
    hold.clear_hold(h.switcher.backup_dir)
    assert two_idle_ticks(h) is TickOutcome.SWITCHED
    assert [e.trigger for e in of(h, SwitchEvent)] == ["preempt"]


def test_hard_still_switches_and_ends_the_hold(temp_home):
    h = make(temp_home)
    pin(h)
    assert h.tick_with_usage(HARD) is TickOutcome.SWITCHED
    assert [e.trigger for e in of(h, SwitchEvent)] == ["hard"] and h.active_number() == 2
    assert marker(h) is None
    assert not (h.switcher.backup_dir / hold.HOLD_FILENAME).exists()
    assert hold.STATE_KEY not in h.state()
    assert lifted(h) == ["hold on a lifted: the engine switched to b"]


def test_at_limit_still_switches(temp_home):
    h = make(temp_home)
    pin(h)
    assert h.tick_with_usage(LIMIT) is TickOutcome.SWITCHED
    assert [e.trigger for e in of(h, SwitchEvent)] == ["at-limit"]
    assert marker(h) is None


def test_an_external_switch_ends_the_hold(temp_home):
    h = make(temp_home)
    pin(h)
    assert h.tick_with_usage(SOFT) is TickOutcome.NO_ACTION
    h.make_live(EMAILS[2], 2)  # a manual switch / a /login outside cc-swap
    h.clock.advance(120)
    h.tick_with_usage({"1": win(62, 40), "2": win(10, 10), "3": win(0, 50)})
    assert marker(h) is None
    assert lifted(h) == ["hold on a lifted: b is the active account now"]
    assert no_switch_reasons(h)[-1] != "hold"


def test_the_hold_ends_at_its_end_time(temp_home):
    h = make(temp_home)
    pin(h, hours=600 / H)  # ten minutes
    assert h.tick_with_usage(SOFT) is TickOutcome.NO_ACTION
    h.clock.advance(300)
    assert h.tick_with_usage(SOFT) is TickOutcome.NO_ACTION
    assert no_switch_reasons(h) == ["hold", "hold"]
    h.clock.advance(400)  # past the end, still idle: the soft switch goes ahead
    assert h.tick_with_usage(SOFT) is TickOutcome.SWITCHED
    assert [e.trigger for e in of(h, SwitchEvent)] == ["soft"]
    assert marker(h) is None and not lifted(h)  # ended by itself: no "lifted" note


def test_a_hold_on_another_slot_never_applies(temp_home):
    h = make(temp_home)
    pin(h, "2")
    h.tick_with_usage(SOFT)
    assert "hold" not in no_switch_reasons(h)
    assert marker(h) is None and lifted(h) == ["hold on b lifted: a is the active account now"]


def test_a_dry_run_honours_the_hold_but_never_clears_it(temp_home):
    h = make(temp_home)
    h.engine = h._make_engine(dry_run=True)
    pin(h)
    assert h.tick_with_usage(SOFT) is TickOutcome.NO_ACTION
    assert no_switch_reasons(h) == ["hold"]
    assert h.tick_with_usage(HARD) is TickOutcome.SWITCHED  # dry: decided, not done
    assert marker(h) is not None and h.active_number() == 1
    pin(h, "3")
    h.tick_with_usage(SOFT)
    assert marker(h) is not None and marker(h).slot == "3"  # not ours to clear


# -- a hold set while a tick runs (its usage fetch takes seconds) ------------------------------


def tick_during(h, usage: dict, during) -> TickOutcome:
    """One tick whose usage fetch first runs ``during()``: the user acting
    while the engine waits on the network. The tick read its active
    account before that."""
    entries = {num: _entry_for(value, h.clock.now) for num, value in usage.items()}
    done: list[bool] = []

    def fetch(*_a, **_k):
        if not done:
            done.append(True)
            during()
        return entries

    with patch.object(h.switcher, "usage_entries_by_account", side_effect=fetch):
        return h.engine.tick()


def test_a_hold_set_during_the_tick_is_never_wiped(temp_home):
    # Mid-fetch the user switches to #2 and holds it: the tick still thinks
    # #1 is active, but the hold is the user's newest word.
    h = make(temp_home)

    def switch_and_hold():
        h.clock.advance(5)
        h.make_live(EMAILS[2], 2)
        pin(h, "2")

    tick_during(h, SOFT, switch_and_hold)
    assert marker(h) is not None and marker(h).slot == "2"
    assert not lifted(h)
    h.clock.advance(60)
    h.tick_with_usage({"1": win(10, 10), "2": win(62, 40), "3": win(0, 50)})
    assert no_switch_reasons(h)[-1] == "hold"  # the next tick honours it


def test_the_live_login_is_read_again_before_a_hold_is_cleared(temp_home):
    # An older hold on #2; mid-fetch #2 becomes the live login again.
    h = make(temp_home)
    pin(h, "2", now=h.clock.now - 120)
    tick_during(h, SOFT, lambda: h.make_live(EMAILS[2], 2))
    assert marker(h) is not None and marker(h).slot == "2" and not lifted(h)


def test_a_marker_newer_than_the_tick_is_left_for_the_next_one(temp_home):
    h = make(temp_home)

    def hold_three():
        h.clock.advance(5)
        pin(h, "3")  # never the live login: the next tick lifts it

    tick_during(h, SOFT, hold_three)
    assert marker(h) is not None and not lifted(h)
    h.clock.advance(60)
    h.tick_with_usage(SOFT)
    assert marker(h) is None
    assert lifted(h) == ["hold on c lifted: a is the active account now"]


def test_the_lifted_note_names_the_live_account_not_the_ticks(temp_home):
    h = make(temp_home)
    pin(h, "3", now=h.clock.now - 120)
    tick_during(h, SOFT, lambda: h.make_live(EMAILS[2], 2))
    assert marker(h) is None
    assert lifted(h) == ["hold on c lifted: b is the active account now"]


def test_a_forced_switch_leaves_a_hold_renewed_during_its_tick_to_the_next_tick(temp_home):
    h = make(temp_home)
    pin(h, now=h.clock.now - 120)

    def renew():
        h.clock.advance(5)
        pin(h, hours=3)

    assert tick_during(h, HARD, renew) is TickOutcome.SWITCHED
    assert marker(h) is not None and not lifted(h)   # the user's newer word, for now
    h.clock.advance(60)
    h.tick_with_usage({"1": win(96, 40), "2": win(10, 10), "3": win(0, 50)})
    assert marker(h) is None
    assert lifted(h) == ["hold on a lifted: b is the active account now"]


def test_a_switch_away_and_back_between_ticks_ends_the_hold(temp_home):
    from claude_swap.maximize import ledger

    h = make(temp_home)
    pin(h)
    assert h.tick_with_usage(SOFT) is TickOutcome.NO_ACTION
    root = h.switcher.backup_dir
    for src, dst in ((1, 2), (2, 1)):  # no engine tick sees #2
        h.clock.advance(30)
        ledger.record_switch(root, from_slot=src, to_slot=dst, actor="user",
                             trigger="manual", source="cli", now=h.clock.now)
    h.clock.advance(240)
    h.tick_with_usage(SOFT)
    assert no_switch_reasons(h)[-1] != "hold"
    assert marker(h) is None
    assert lifted(h) == ["hold on a lifted: the active account changed since it was set"]


def test_a_damaged_hold_file_is_no_hold(temp_home):
    h = make(temp_home)
    (h.switcher.backup_dir / hold.HOLD_FILENAME).write_text("{damaged")
    for _ in range(3):
        h.tick_with_usage(SOFT)
        h.clock.advance(300)
    assert [e.trigger for e in of(h, SwitchEvent)] == ["soft"]
