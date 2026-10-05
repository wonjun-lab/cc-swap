"""Which login each slot's learned ride belongs to (cc-swap fork, maximize).

The learned ride keeps per slot what it learned about that slot's account:
its own q and t (``rideLearning["accounts"][slot]``, maximize/ride.py), its
pace (``rideSteps[slot]``) and the usage-history points its k is read from
(``usage_history.jsonl``, maximize/history.py, ``drain.ride_k``). All of it
describes one login on one plan. When the slot comes to hold another login
(``cc-swap login --new`` into a re-used slot number, a remove and re-add) or
its plan changes, a previous login's k, t and q must not carry over: a 5x
login's low k read for a 20x one under-reads the last point by a third and
rides into 100%.

So the state file keeps, per slot, the login and plan the learning belongs
to (``rideIdentity``: ``{slot: {"who": fingerprint, "plan": tier}}``; a
fingerprint, never an email). Each engine tick reconciles it with the
roster (:func:`reconcile`): a slot whose login or plan differs from the one
stored, or that a store operation marked (:func:`forget_slots`), loses its
learning; a slot no longer in the roster loses its learning and its entry.
A slot seen for the first time (a state file from before this) is taken as
it is: nothing says its learning is someone else's.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path

from claude_swap.maximize import history
from claude_swap.maximize import ride as learned_ride

_logger = logging.getLogger(__name__)

#: State-file key (``autoswitch_state.json``): ``{slot: {"who", "plan"}}``.
IDS_KEY = "rideIdentity"
#: ``"who"`` of a slot a store operation forgot: matches no login, so the
#: engine also drops what it holds in memory for it.
FORGOTTEN = ""


def fingerprint(record: Mapping) -> str:
    """A short, one-way id of the login in ``sequence.json`` record
    ``record`` (email and organization); :data:`FORGOTTEN` without an email."""
    email = str(record.get("email") or "").strip().lower()
    if not email:
        return FORGOTTEN
    org = str(record.get("organizationUuid") or "")
    return hashlib.sha256(f"{email}\0{org}".encode()).hexdigest()[:16]


def _ids(state: Mapping) -> dict[str, dict]:
    raw = state.get(IDS_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, Mapping)}


def drop_learning(state: dict, numbers: Iterable[str]) -> None:
    """Remove slots ``numbers``' own ride learning (their
    ``rideLearning["accounts"]`` records and ``rideSteps``) from ``state``.
    The per-window records are shared by every account and stay."""
    numbers = {str(n) for n in numbers}
    data = state.get(learned_ride.LEARN_KEY)
    if isinstance(data, Mapping) and isinstance(data.get(learned_ride.ACCOUNTS_KEY), Mapping):
        accounts = {
            str(k): v for k, v in data[learned_ride.ACCOUNTS_KEY].items()
            if str(k) not in numbers
        }
        data = dict(data)
        if accounts:
            data[learned_ride.ACCOUNTS_KEY] = accounts
        else:
            data.pop(learned_ride.ACCOUNTS_KEY, None)
        state[learned_ride.LEARN_KEY] = data
    steps = state.get(learned_ride.STEPS_KEY)
    if isinstance(steps, Mapping) and any(str(k) in numbers for k in steps):
        state[learned_ride.STEPS_KEY] = {
            k: v for k, v in steps.items() if str(k) not in numbers
        }


def reconcile(
    state: dict,
    slots: Mapping[str, tuple[str, str | None]],
) -> tuple[set[str], set[str]]:
    """Bring ``state``'s ride learning in line with the roster ``slots``
    (``{slot: (fingerprint, plan tier or None)}``, every slot that exists).
    Returns ``(changed, gone)``: the slots whose login or plan changed (or
    were marked forgotten) and those no longer in the roster. Both lose their
    learning (:func:`drop_learning`); the caller drops their history points.
    Learning of a slot that is in no record (``rideLearning["accounts"]``)
    and not in the roster is pruned too. A plan compares only when both
    sides know it (an unreadable tier is no change). Mutates ``state``."""
    ids = _ids(state)
    changed: set[str] = set()
    out: dict[str, dict] = {}
    for number, (who, plan) in slots.items():
        number = str(number)
        old = ids.get(number)
        if old is not None:
            old_plan = old.get("plan")
            if old.get("who") != who or (
                old_plan is not None and plan is not None and old_plan != plan
            ):
                changed.add(number)
            elif plan is None:
                plan = old_plan
        out[number] = {"who": who, "plan": plan}
    gone = set(ids) - set(out)
    data = state.get(learned_ride.LEARN_KEY)
    accounts = data.get(learned_ride.ACCOUNTS_KEY) if isinstance(data, Mapping) else None
    if isinstance(accounts, Mapping):
        gone |= {str(k) for k in accounts if str(k) not in out}
    steps = state.get(learned_ride.STEPS_KEY)
    if isinstance(steps, Mapping):
        gone |= {str(k) for k in steps if str(k) not in out}
    if changed or gone:
        drop_learning(state, changed | gone)
    if out != ids or IDS_KEY not in state:
        state[IDS_KEY] = out
    return changed, gone


def drop_points(h: history.History, numbers: Iterable[str]) -> history.History:
    """``h`` without slots ``numbers``' usage points (slot observations stay:
    they are the active account's, whoever it was)."""
    numbers = {str(n) for n in numbers}
    return history.History(
        points=tuple(p for p in h.points if p.number not in numbers), slots=h.slots
    )


def forget_points(root: Path, numbers: Iterable[str]) -> None:
    """Rewrite ``root``'s usage history without slots ``numbers``' points.
    Nothing is written when it holds none."""
    numbers = {str(n) for n in numbers}
    path = history.path_for(root)
    if not numbers or not path.exists():
        return
    h = history.read(root)
    if not any(p.number in numbers for p in h.points):
        return
    history._rewrite(path, drop_points(h, numbers))


def forget_slots(root: Path, numbers: Iterable[str]) -> None:
    """Forget what the learned ride knows about slots ``numbers``: their
    own q/t/pace (state file, under its lock) and their usage-history
    points. Marks them :data:`FORGOTTEN` so a running engine drops what it
    holds in memory on its next tick (:func:`reconcile`). For store
    operations (``cc-swap login --new`` into a slot, ``remove``). Never
    raises: losing the learning is the safe side, failing it only logs."""
    from claude_swap.autoswitch import STATE_FILENAME
    from claude_swap.locking import FileLock
    from claude_swap.settings import atomic_write_json

    numbers = {str(n) for n in numbers}
    if not numbers:
        return
    root = Path(root)
    try:
        path = root / STATE_FILENAME
        if path.exists():
            with FileLock(root / ".autoswitch_state.lock"):
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = None
                if isinstance(state, dict):
                    drop_learning(state, numbers)
                    ids = _ids(state)
                    for number in numbers:
                        ids[number] = {"who": FORGOTTEN, "plan": None}
                    state[IDS_KEY] = ids
                    atomic_write_json(path, state)
        forget_points(root, numbers)
    except Exception as e:
        _logger.warning(
            "could not forget the learned ride of slot(s) %s: %s",
            ", ".join(sorted(numbers)), type(e).__name__,
        )
