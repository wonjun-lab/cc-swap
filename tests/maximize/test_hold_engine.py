"""An account hold (``cc-swap hold``) through the real engine (EngineHarness):
soft and preempt moves are set aside, hard and at-limit still switch, and
the hold ends at its end time or with any change of the active account."""

from __future__ import annotations

from claude_swap.autoswitch import ConfigWarningEvent, SwitchEvent, TickOutcome
from claude_swap.maximize import hold
from claude_swap.maximize.engine_hook import DECISION_KEY
from tests.maximize.test_engine_maximize import EMAILS, make, no_switch_reasons, of, win
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
    return [e.message for e in of(h, ConfigWarningEvent) if e.message.startswith("hold on #")]


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
    assert record["reason"].startswith("#1 held until ")
    assert "otherwise: #1 5h 62% >= soft 50%" in record["reason"]
    assert marker(h) is not None and not lifted(h)


def test_preempt_is_set_aside_while_the_hold_lasts(temp_home):
    h = make(temp_home, maximize={"tieEpsilon": 2})
    seed(h)
    pin(h, hours=4, now=FRIDAY_NOON)
    assert two_idle_ticks(h) is TickOutcome.NO_ACTION
    assert no_switch_reasons(h) == ["hold", "hold"]
    assert "otherwise: #1 7d 84% would pass 90%" in h.state()[DECISION_KEY]["reason"]
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
    assert lifted(h) == ["hold on #1 lifted: the engine switched to #2"]


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
    assert lifted(h) == ["hold on #1 lifted: #2 is the active account now"]
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
    assert marker(h) is None and lifted(h) == ["hold on #2 lifted: #1 is the active account now"]


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


def test_a_damaged_hold_file_is_no_hold(temp_home):
    h = make(temp_home)
    (h.switcher.backup_dir / hold.HOLD_FILENAME).write_text("{damaged")
    for _ in range(3):
        h.tick_with_usage(SOFT)
        h.clock.advance(300)
    assert [e.trigger for e in of(h, SwitchEvent)] == ["soft"]
