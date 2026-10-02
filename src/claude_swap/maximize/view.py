"""Read model for the TUI's maximize panel: pure functions, no Textual.

Everything is computed from the inputs the engine decides on: the usage
store snapshot (via :func:`snapshot_from_accounts`, which wraps
``maximize.snapshot.build_snapshot``) and the engine's state file
(``maximizeSamples``, ``primes``, ``quarantine``, ``lastSwitchAt``). The panel
therefore reads the same whether the engine runs in this process or in the
cc-swap service, and it never fetches anything itself.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from claude_swap.maximize.model import AccountView, Sample, Snapshot
from claude_swap.maximize.score import landable, rank, score
from claude_swap.maximize.snapshot import build_snapshot
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import SETTING_SPECS, MaximizeSettings

#: ``autoswitch.STATE_FILENAME`` (pinned by a test; not imported, so the TUI
#: can import this module without pulling in the engine).
STATE_FILENAME = "autoswitch_state.json"
FIVE_HOUR_S = 5 * 3600
#: A primed window opens at floor10(prime time), so its reset minus 5h falls
#: in the 10 minutes before the prime; allow the primer's verify tolerance.
PRIME_SLACK_S = 120.0

State5 = Literal["cold", "running", "primed"]

KNOBS: tuple[str, ...] = ("soft_5h", "hard_5h", "soft_7d", "hard_7d")
KNOB_KEYS = {
    "soft_5h": "maximize.soft5h",
    "hard_5h": "maximize.hard5h",
    "soft_7d": "maximize.soft7d",
    "hard_7d": "maximize.hard7d",
}
KNOB_LABELS = {
    "soft_5h": "5h soft", "hard_5h": "5h hard", "soft_7d": "7d soft", "hard_7d": "7d hard",
}
_PAIRS = (("soft_5h", "hard_5h"), ("soft_7d", "hard_7d"))
TIER_LABELS = {"normal": "normal", "last_resort": "last resort", "excluded": "excluded"}


#: ``engine_hook.DECISION_KEY`` (pinned by a test, like STATE_FILENAME).
DECISION_KEY = "maximizeDecision"
DECISION_KINDS = frozenset({"switch", "hold", "indeterminate", "exhausted"})


@dataclass(frozen=True)
class PublishedDecision:
    """The decision a live engine last wrote (``engine_hook._publish_decision``)."""

    at: float
    pid: int | None
    active: str | None
    decision: str  # one of DECISION_KINDS
    trigger: str | None
    target: str | None
    reason: str
    pending: bool


@dataclass(frozen=True)
class MaximizeState:
    """The engine state-file keys the maximize panel reads."""

    samples_account: str | None = None
    samples: tuple[Sample, ...] = ()
    primes: Mapping[str, Mapping] = field(default_factory=dict)
    quarantined: frozenset[str] = frozenset()
    last_switch_at: float | None = None
    decision: PublishedDecision | None = None
    plans: Mapping[str, str | None] = field(default_factory=dict)
    # A TUI re-login's pause marker (maximize/pause.py), as written.
    paused_until: float | None = None
    paused_reason: str | None = None


@dataclass(frozen=True)
class RowView:
    number: str
    email: str
    tier: str
    active: bool
    score: float | None  # None when the 7d usage is unknown
    landable: bool
    state5: State5
    reset5: float | None


@dataclass(frozen=True)
class PendingView:
    window: Literal["5h", "7d"]
    pct: float
    growth: float | None  # pct points per idle window; None until samples span one
    window_min: int


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _text(value) -> str | None:
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _published(raw: object) -> tuple[PublishedDecision | None, dict[str, str | None]]:
    """The ``maximizeDecision`` record, leniently: anything malformed is None."""
    if not isinstance(raw, dict):
        return None, {}
    at = _num(raw.get("at"))
    kind = raw.get("decision")
    reason = raw.get("reason")
    if at is None or kind not in DECISION_KINDS or not isinstance(reason, str):
        return None, {}
    pid = raw.get("pid")
    trigger = raw.get("trigger")
    decision = PublishedDecision(
        at=at,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        active=_text(raw.get("active")),
        decision=kind,
        trigger=trigger if isinstance(trigger, str) else None,
        target=_text(raw.get("target")),
        reason=reason,
        pending=raw.get("pending") is True,
    )
    plans_raw = raw.get("plans")
    plans: dict[str, str | None] = {}
    if isinstance(plans_raw, dict):
        for num, label in plans_raw.items():
            if label is None or isinstance(label, str):
                plans[str(num)] = label
    return decision, plans


def read_state(backup_root: Path) -> MaximizeState:
    """The maximize keys of ``autoswitch_state.json``; empty when unreadable.

    No lock needed: the engine replaces the file atomically, so a reader
    sees the old or the new version, never half of one.
    """
    try:
        raw = json.loads((Path(backup_root) / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MaximizeState()
    if not isinstance(raw, dict):
        return MaximizeState()
    account: str | None = None
    found: list[Sample] = []
    block = raw.get("maximizeSamples")
    if isinstance(block, dict):
        if block.get("account") is not None:
            account = str(block["account"])
        listed = block.get("samples")
        for row in listed if isinstance(listed, list) else []:
            if isinstance(row, (list, tuple)) and len(row) == 3:
                ts, p5, p7 = (_num(x) for x in row)
                if ts is not None and p5 is not None and p7 is not None:
                    found.append(Sample(ts, p5, p7))
    found.sort(key=lambda s: s.ts)
    primes = raw.get("primes")
    quarantine = raw.get("quarantine")
    decision, plans = _published(raw.get(DECISION_KEY))
    reason = raw.get("pausedReason")
    return MaximizeState(
        samples_account=account,
        samples=tuple(found),
        primes=primes if isinstance(primes, dict) else {},
        quarantined=(
            frozenset(str(n) for n in quarantine)
            if isinstance(quarantine, dict)
            else frozenset()
        ),
        last_switch_at=_num(raw.get("lastSwitchAt")),
        decision=decision,
        plans=plans,
        paused_until=_num(raw.get("pausedUntil")),
        paused_reason=reason if isinstance(reason, str) else None,
    )


def snapshot_from_accounts(
    snap: AccountsSnapshot,
    settings: MaximizeSettings,
    state: MaximizeState,
    *,
    now: float,
    plans: Mapping[str, str | None] | None = None,
) -> Snapshot:
    """The policy Snapshot for the TUI's store snapshot.

    Plan tiers are not read here (reading ``rateLimitTier`` costs a Keychain
    read per account): ``plans`` — the labels a live engine published — and
    ``maximize.planOverride`` weigh in. That affects tie-breaks only.
    """
    accounts = snap.accounts
    return build_snapshot(
        now=now,
        active=snap.active_number,
        usage={a.number: (a.usage.sentinel or a.usage.last_good) for a in accounts},
        records={
            a.number: {"email": a.email, "alias": a.alias, "disabled": a.disabled}
            for a in accounts
        },
        quarantined=set(state.quarantined),
        api_key_accounts={a.number for a in accounts if a.kind == "api_key"},
        rate_limit_tiers={a.number: (plans or {}).get(a.number) for a in accounts},
        samples=state.samples if state.samples_account == snap.active_number else (),
        last_switch_at=state.last_switch_at,
        settings=settings,
    )


def state5(account: AccountView, prime: Mapping | None, now: float) -> State5:
    """``cold``: no running 5h window. ``primed``: the running window is the
    one the primer opened. ``running``: any other window with a future reset."""
    if account.reset5 is None or account.reset5 <= now:
        return "cold"
    if isinstance(prime, Mapping) and prime.get("lastOutcome") == "primed":
        at = _num(prime.get("lastAttemptAt"))
        opened = account.reset5 - FIVE_HOUR_S
        if at is not None and opened - PRIME_SLACK_S <= at < opened + 600 + PRIME_SLACK_S:
            return "primed"
    return "running"


def _slot(account: AccountView) -> tuple[int, str]:
    if account.number.isdigit():
        return int(account.number), ""
    return 1 << 30, account.number


def rows(snap: Snapshot, primes: Mapping[str, Mapping]) -> list[RowView]:
    """Every account, in the order maximize would pick them: ``rank()`` over
    the non-excluded accounts, then whatever it left out (excluded included)
    by slot."""
    eligible = [a for a in snap.accounts if a.tier != "excluded"]
    ordered = list(rank(eligible, snap.now, snap.settings.tie_epsilon))
    seen = {a.number for a in ordered}
    ordered += sorted((a for a in snap.accounts if a.number not in seen), key=_slot)
    out: list[RowView] = []
    for account in ordered:
        value = score(account, snap.now)
        out.append(
            RowView(
                number=account.number,
                email=account.email,
                tier=account.tier,
                active=account.number == snap.active,
                score=value if math.isfinite(value) else None,
                landable=landable(account, snap.settings),
                state5=state5(account, primes.get(account.email), snap.now),
                reset5=account.reset5,
            )
        )
    return out


def pending(snap: Snapshot) -> PendingView | None:
    """The soft-threshold wait: the active account crossed a soft threshold
    but no hard one, so maximize holds until usage slows down (spec §5.5–5.6).
    5h is reported when both windows crossed."""
    active = next((a for a in snap.accounts if a.number == snap.active), None)
    if active is None or active.pct5 is None or active.pct7 is None:
        return None
    s = snap.settings
    if active.pct5 >= s.hard_5h or active.pct7 >= s.hard_7d:
        return None  # at-limit / hard switch at once: nothing to wait for
    if active.pct5 >= s.soft_5h:
        window, pct = "5h", active.pct5
    elif active.pct7 >= s.soft_7d:
        window, pct = "7d", active.pct7
    else:
        return None
    return PendingView(
        window=window,
        pct=pct,
        growth=_growth(snap.samples, window, s.idle_window_min),
        window_min=s.idle_window_min,
    )


def _growth(samples: Sequence[Sample], window: str, window_min: int) -> float | None:
    """Growth of ``window``'s pct over the latest idle window, scaled to
    exactly ``window_min`` minutes; None until two samples span one."""
    if len(samples) < 2:
        return None
    needed = window_min * 60.0
    last = samples[-1]
    base = next((s for s in reversed(samples[:-1]) if last.ts - s.ts >= needed), None)
    if base is None:
        return None
    if window == "5h":
        rise = last.pct5 - base.pct5
    else:
        rise = last.pct7 - base.pct7
    return max(rise * needed / (last.ts - base.ts), 0.0)


def _pct(value: float) -> str:
    return f"{value:.10g}"  # same rule as autoswitch.pct_label


def format_score(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def format_state5(row: RowView) -> str:
    if row.state5 == "cold" or row.reset5 is None:
        return "5h cold"
    clock = time.strftime("%H:%M", time.localtime(row.reset5))
    return f"5h {row.state5} · resets {clock}"


def format_pending(p: PendingView) -> str:
    head = f"waiting for idle: {p.window} {p.pct:.0f}%"
    if p.growth is None:
        return f"{head}, measuring pace"
    return f"{head}, {p.growth:+.0f}%p/{p.window_min}min"


def format_thresholds(s: MaximizeSettings) -> str:
    return (
        f"5h {_pct(s.soft_5h)}/{_pct(s.hard_5h)}% · "
        f"7d {_pct(s.soft_7d)}/{_pct(s.hard_7d)}%"
    )


def window_ticks(s: MaximizeSettings) -> dict[str, tuple[float, float]]:
    """``{bar label: (soft, hard)}`` for the 5h and 7d usage bars."""
    return {"5h": (s.soft_5h, s.hard_5h), "7d": (s.soft_7d, s.hard_7d)}


def step_knob(s: MaximizeSettings, knob: str, delta: float) -> MaximizeSettings:
    """Move one threshold by ``delta``, inside its ``SETTING_SPECS`` range,
    never letting a soft threshold pass its hard ceiling (or vice versa)."""
    spec = SETTING_SPECS[KNOB_KEYS[knob]]
    soft, hard = next(pair for pair in _PAIRS if knob in pair)
    value = min(spec.hi, max(spec.lo, getattr(s, knob) + delta))
    if knob == soft:
        value = min(value, getattr(s, hard))
    else:
        value = max(value, getattr(s, soft))
    return replace(s, **{knob: round(value, 6)})


def threshold_writes(old: MaximizeSettings, new: MaximizeSettings) -> list[tuple[str, float]]:
    """``(dotted key, value)`` writes turning ``old`` into ``new``, ordered so
    the file never holds soft > hard between two writes (``set_setting``
    rejects that). Per window: soft first unless the new soft passes the old
    hard; then hard first. One of the two orders is always valid."""
    writes: list[tuple[str, float]] = []
    for soft, hard in _PAIRS:
        pair: list[tuple[str, float]] = []
        if getattr(new, soft) != getattr(old, soft):
            pair.append((KNOB_KEYS[soft], getattr(new, soft)))
        if getattr(new, hard) != getattr(old, hard):
            pair.append((KNOB_KEYS[hard], getattr(new, hard)))
        if len(pair) == 2 and getattr(new, soft) > getattr(old, hard):
            pair.reverse()
        writes.extend(pair)
    return writes
