"""A slot's learned ride belongs to one login on one plan (maximize/ride_slots.py):
another login or plan in the slot, or no slot at all, drops its own q/t/pace and
the usage points its k is read from."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from claude_swap.maximize import drain, engine_hook, history
from claude_swap.maximize import ride as learned_ride
from claude_swap.maximize import ride_slots
from claude_swap.maximize.history import UsagePoint
from tests.maximize.test_relogin import _creds, _switcher

A = {"email": "a@example.com", "organizationUuid": "org-a"}
B = {"email": "b@example.com", "organizationUuid": "org-b"}


def _learned(*slots: str) -> dict:
    data: dict = {}
    for i, number in enumerate(slots):
        data = learned_ride.learn(data, "7d", "ok", float(i), by_5h=True, account=number)
        data = learned_ride.learn(data, "7d", "ok", float(i), account=number)
    return data


def _state(*slots: str) -> dict:
    return {
        learned_ride.LEARN_KEY: _learned(*slots),
        learned_ride.STEPS_KEY: {n: {"activeAt": 1.0} for n in slots},
    }


def _own(state: dict) -> set[str]:
    return set(learned_ride.learned_accounts(state.get(learned_ride.LEARN_KEY)))


def _points(*numbers: str, windows: int = 6) -> list[UsagePoint]:
    """Enough agreeing 5h windows per slot for ``drain.ride_k`` (k 0.11)."""
    out: list[UsagePoint] = []
    for number in numbers:
        ts, p7 = 0.0, 10.0
        for _ in range(windows):
            out.append(UsagePoint(ts, number, 0.0, p7, True))
            p7 += 0.11 * 60.0
            out.append(UsagePoint(ts + 4 * 3600.0, number, 60.0, p7, True))
            ts += 5 * 3600.0 + 60.0
    return out


def _write_history(root: Path, points: list[UsagePoint]) -> None:
    h = history.from_items([*points, history.SlotObs(0.0, True)])
    history._rewrite(history.path_for(root), h)


# -- reconcile ----------------------------------------------------------------------------


def test_a_slot_seen_first_keeps_its_learning():
    state = _state("1", "2")
    fp = ride_slots.fingerprint(A)
    assert ride_slots.reconcile(state, {"1": (fp, "20x"), "2": (fp, None)}) == (set(), set())
    assert _own(state) == {"1", "2"}
    assert state[ride_slots.IDS_KEY]["1"] == {"who": fp, "plan": "20x"}


def test_another_login_in_the_slot_drops_its_own_k_t_q():
    state = _state("1", "2")
    ride_slots.reconcile(state, {"1": (ride_slots.fingerprint(A), "20x"), "2": ("x", "5x")})
    window_t = learned_ride.t_values(state[learned_ride.LEARN_KEY])["7d"]
    changed, gone = ride_slots.reconcile(
        state, {"1": (ride_slots.fingerprint(B), "20x"), "2": ("x", "5x")}
    )
    assert (changed, gone) == ({"1"}, set())
    assert _own(state) == {"2"}
    assert set(state[learned_ride.STEPS_KEY]) == {"2"}
    # Slot 1 rides by the window's values now; the window's record stays.
    assert learned_ride.t_values(state[learned_ride.LEARN_KEY], "1")["7d"] == window_t
    assert state[ride_slots.IDS_KEY]["1"]["who"] == ride_slots.fingerprint(B)
    # Settled: nothing more is dropped on the next tick.
    state[learned_ride.LEARN_KEY] = learned_ride.learn(
        state[learned_ride.LEARN_KEY], "7d", "ok", 9.0, account="1"
    )
    assert ride_slots.reconcile(
        state, {"1": (ride_slots.fingerprint(B), "20x"), "2": ("x", "5x")}
    ) == (set(), set())
    assert _own(state) == {"1", "2"}


def test_a_plan_change_drops_it_an_unreadable_plan_does_not():
    state = _state("1")
    ride_slots.reconcile(state, {"1": ("a", "5x")})
    assert ride_slots.reconcile(state, {"1": ("a", None)}) == (set(), set())
    assert _own(state) == {"1"}
    assert state[ride_slots.IDS_KEY]["1"]["plan"] == "5x"  # kept, not forgotten
    assert ride_slots.reconcile(state, {"1": ("a", "20x")}) == ({"1"}, set())
    assert _own(state) == set()


def test_slots_that_no_longer_exist_are_pruned():
    state = _state("1", "2", "7")  # 7: learning from before rideIdentity
    ride_slots.reconcile(state, {"1": ("a", None), "2": ("b", None)})
    assert _own(state) == {"1", "2"}  # 7 is not in the roster
    changed, gone = ride_slots.reconcile(state, {"1": ("a", None)})
    assert (changed, gone) == (set(), {"2"})
    assert _own(state) == {"1"} and set(state[ride_slots.IDS_KEY]) == {"1"}
    assert set(state[learned_ride.STEPS_KEY]) == {"1"}


def test_the_state_survives_json_and_a_learn_after_it():
    state = json.loads(json.dumps(_state("1", "2")))
    ride_slots.reconcile(state, {"1": ("a", None), "2": ("b", None)})
    ride_slots.reconcile(state, {"1": ("c", None), "2": ("b", None)})
    state = json.loads(json.dumps(state))
    data = learned_ride.learn(state[learned_ride.LEARN_KEY], "7d", "hit", 5.0, account="2")
    assert set(learned_ride.learned_accounts(data)) == {"2"}


def test_the_fingerprint_is_no_email():
    fp = ride_slots.fingerprint(A)
    assert "@" not in fp and "example" not in fp and len(fp) == 16
    assert fp == ride_slots.fingerprint({**A, "email": "A@Example.com "})
    assert fp != ride_slots.fingerprint({**A, "organizationUuid": "other"})
    assert ride_slots.fingerprint({}) == ride_slots.FORGOTTEN


# -- forget_slots (store operations) -------------------------------------------------------


def test_forget_slots_drops_learning_and_points_and_marks_the_slot(tmp_path):
    state = _state("1", "2")
    ride_slots.reconcile(state, {"1": ("a", "20x"), "2": ("b", "5x")})
    (tmp_path / "autoswitch_state.json").write_text(json.dumps(state))
    _write_history(tmp_path, _points("1", "2"))
    assert set(drain.ride_k(history.read(tmp_path).points)) == {"1", "2"}

    ride_slots.forget_slots(tmp_path, ["1"])

    after = json.loads((tmp_path / "autoswitch_state.json").read_text())
    assert _own(after) == {"2"}
    assert after[ride_slots.IDS_KEY]["1"]["who"] == ride_slots.FORGOTTEN
    kept = history.read(tmp_path)
    assert {p.number for p in kept.points} == {"2"} and kept.slots
    assert set(drain.ride_k(kept.points)) == {"2"}
    # The same login comes back: the mark still makes the engine forget
    # what it holds in memory.
    assert ride_slots.reconcile(after, {"1": ("a", "20x"), "2": ("b", "5x")})[0] == {"1"}


def test_forget_slots_without_a_state_file_or_history_is_a_no_op(tmp_path):
    ride_slots.forget_slots(tmp_path, ["1"])
    assert list(tmp_path.iterdir()) == []


# -- the engine ---------------------------------------------------------------------------


def _engine(root: Path, *, dry_run: bool = False):
    path = root / "autoswitch_state.json"

    def mutate(fn):
        st = json.loads(path.read_text()) if path.exists() else {}
        fn(st)
        path.write_text(json.dumps(st))
        return st

    return SimpleNamespace(
        dry_run=dry_run, _mutate_state=mutate, switcher=SimpleNamespace(backup_dir=root),
        _name=str,
    )


def test_the_engine_forgets_a_slot_whose_login_changed_on_disk_and_in_memory(tmp_path):
    engine = _engine(tmp_path)
    rt = engine_hook.MaximizeRuntime.__new__(engine_hook.MaximizeRuntime)
    _write_history(tmp_path, _points("1", "2"))
    rt.history = history.Recorder(tmp_path)
    rt.history.load(1e9)
    rt.history.history = history.from_items(_points("1", "2"))  # in memory
    state = _state("1", "2")
    (tmp_path / "autoswitch_state.json").write_text(json.dumps(state))
    records = {"1": A, "2": B}
    tiers = {"1": "20x", "2": "20x"}

    engine_hook._reconcile_ride_slots(engine, rt, state, records, tiers)
    assert _own(state) == {"1", "2"}  # first sight

    engine_hook._reconcile_ride_slots(engine, rt, state, {**records, "1": B}, tiers)
    assert _own(state) == {"2"}
    on_disk = json.loads((tmp_path / "autoswitch_state.json").read_text())
    assert _own(on_disk) == {"2"}
    assert {p.number for p in history.read(tmp_path).points} == {"2"}
    assert {p.number for p in rt.history.history.points} == {"2"}
    assert set(drain.ride_k(rt.history.history.points)) == {"2"}


def test_the_engine_forgets_on_a_plan_change_and_prunes_a_removed_slot(tmp_path):
    engine = _engine(tmp_path)
    rt = SimpleNamespace(history=None)
    _write_history(tmp_path, _points("1", "2"))
    state = _state("1", "2")
    (tmp_path / "autoswitch_state.json").write_text(json.dumps(state))
    engine_hook._reconcile_ride_slots(engine, rt, state, {"1": A, "2": B}, {"1": "5x"})
    engine_hook._reconcile_ride_slots(engine, rt, state, {"1": A, "2": B}, {"1": "20x"})
    assert _own(state) == {"2"}
    engine_hook._reconcile_ride_slots(engine, rt, state, {"1": A}, {"1": "20x"})
    assert _own(state) == set()
    assert set(state[ride_slots.IDS_KEY]) == {"1"}
    assert history.read(tmp_path).points == ()


def test_the_engine_writes_nothing_once_settled_nor_in_a_dry_run(tmp_path):
    rt = SimpleNamespace(history=None)
    state = _state("1")
    dry = _engine(tmp_path, dry_run=True)
    engine_hook._reconcile_ride_slots(dry, rt, state, {"1": A}, {})
    assert not (tmp_path / "autoswitch_state.json").exists()
    engine = _engine(tmp_path)
    engine_hook._reconcile_ride_slots(engine, rt, state, {"1": A}, {})
    path = tmp_path / "autoswitch_state.json"
    path.write_text("sentinel")  # any further write would replace it
    engine_hook._reconcile_ride_slots(engine, rt, state, {"1": A}, {})
    assert path.read_text() == "sentinel"
    # No roster read (an unreadable sequence.json): nothing is pruned.
    engine_hook._reconcile_ride_slots(engine, rt, state, {}, {})
    assert path.read_text() == "sentinel"


# -- the switcher's store operations ---------------------------------------------------------


def _seed(s, *slots: str) -> None:
    root = Path(s.backup_dir)
    state = _state(*slots)
    ride_slots.reconcile(state, {n: (f"who-{n}", None) for n in slots})
    (root / "autoswitch_state.json").write_text(json.dumps(state))
    _write_history(root, _points(*slots))


def _after(s) -> tuple[set[str], set[str], dict]:
    root = Path(s.backup_dir)
    state = json.loads((root / "autoswitch_state.json").read_text())
    return _own(state), {p.number for p in history.read(root).points}, state


def test_a_new_login_into_a_re_used_slot_number_inherits_nothing(temp_home):
    s = _switcher(temp_home)  # slots 1, 4, 5
    _seed(s, "1", "4", "6")   # 6: a slot removed before, its learning left
    num = s.store_new_login(_creds("rt-x"), {"emailAddress": "x@example.com"}, slot=6)
    assert num == "6"
    own, points, state = _after(s)
    assert own == {"1", "4"} and points == {"1", "4"}
    assert state[ride_slots.IDS_KEY]["6"]["who"] == ride_slots.FORGOTTEN


def test_removing_a_slot_drops_its_learning(temp_home):
    s = _switcher(temp_home)
    _seed(s, "1", "4", "5")
    s.remove_account("4", assume_yes=True)
    own, points, _ = _after(s)
    assert own == {"1", "5"} and points == {"1", "5"}
