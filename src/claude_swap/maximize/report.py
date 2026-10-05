"""Per-account maximize rows for dry-run output, JSON events and the TUI.

No I/O and no emails: rows identify accounts by slot number only.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from claude_swap.maximize import drain, idle
from claude_swap.maximize.model import Snapshot
from claude_swap.maximize.score import days_left, landable, score


def idle_state(snap: Snapshot) -> str:
    """``"idle"``, ``"busy"``, or ``"unknown"`` (not enough recent samples)."""
    if idle.idle_evidence(snap.samples, snap.now, snap.settings) is None:
        return "unknown"
    return "idle" if idle.is_idle(snap.samples, snap.now, snap.settings) else "busy"


def decision_rows(snap: Snapshot) -> list[dict]:
    state = idle_state(snap)
    rows: list[dict] = []
    for v in snap.accounts:
        value = score(v, snap.now)
        draining = drain.draining(v, snap)
        flags = [
            name
            for name, on in (
                ("quarantined", v.quarantined), ("api-key", v.api_key), ("drain", draining),
            )
            if on
        ]
        rows.append({
            "number": v.number,
            "active": v.number == snap.active,
            "tier": v.tier,
            "plan": "20x" if v.plan_weight >= 4 else "std",
            "pct5": v.pct5,
            "pct7": v.pct7,
            "days7": round(days_left(v, snap.now), 2) if v.pct7 is not None else None,
            "score": round(value, 3) if math.isfinite(value) else None,
            "landable": v.number != snap.active and landable(
                v, snap.settings, draining=draining
            ),
            "idle": state if v.number == snap.active else "",
            "flags": ",".join(flags),
        })
    return rows


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"


def render_rows(rows: Sequence[dict]) -> list[str]:
    """Fixed-width table; ``*`` marks the active account."""
    lines = [
        f"    {'#':>3} {'tier':<11} {'plan':<4} {'5h':>5} {'7d':>5} "
        f"{'7d-in':>6} {'score':>6} {'land':<4} {'idle':<7} flags"
    ]
    for r in rows:
        days = "?" if r["days7"] is None else f"{r['days7']:.1f}d"
        sc = "-" if r["score"] is None else f"{r['score']:.2f}"
        lines.append(
            (
                f"  {'*' if r['active'] else ' '} {r['number']:>3} {r['tier']:<11} "
                f"{r['plan']:<4} {_pct(r['pct5']):>5} {_pct(r['pct7']):>5} "
                f"{days:>6} {sc:>6} {'yes' if r['landable'] else '-':<4} "
                f"{r['idle'] or '-':<7} {r['flags']}"
            ).rstrip()
        )
    return lines
