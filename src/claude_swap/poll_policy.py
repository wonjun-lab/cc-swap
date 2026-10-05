"""Cadence policy for the ``/api/oauth/usage`` endpoint — every number in one place.

The endpoint enforces a budget on non-first-party clients: a **~60-minute
window of ~28-30 requests per identity × UA-class** (measured 2026-07-11,
probe3, two runs: a rested identity admitted 30 requests before the first
429; the post-drain 429 oscillation ended exactly when the drain burst aged
60 minutes; steady 1/180 s polling then ran 96 minutes from a rested window
with zero 429s). It is NOT a bucket with a refill rate: capacity returns
only as old requests age out of the trailing hour, so a burst saturates the
identity for up to a full hour — pausing does not restore headroom early,
and earlier "refill rate" estimates were artifacts of measuring while
saturated.

What that identity is depends on which 429 regime the org is on — the two
regimes coexist across orgs (see the Retry-After discussion in
``usage_store``). Under the fixed-deadline regime it is the **account/org**
(measured 2026-07-28: a freshly minted token was blocked 135 s after issue,
which a per-token counter cannot produce). Under the saturated-edge
(``Retry-After: 0``) regime it is the **access token** (measured 2026-07-29,
probe4: at saturation, a freshly minted token of the same lineage was
admitted while the old token stayed blocked, requests interleaved). Plan for
the account-scoped case — it is the conservative one: re-authenticating
cannot be relied on to clear a block, and two machines holding different
tokens for one account may share one budget, which is what
``POST_429_BACKOFF_MULT`` below exists to converge.

Error bars: the horizon is bracketed to ~55-64 minutes from a single
transition event, the exact edge algorithm (likely a Cloudflare
sliding-window approximation) is undocumented, and Anthropic can retune it
any day — so the constants below lean only on the robust parts: a sustained
rate safely under the cap, and an ~hour recovery horizon. The budget target
is an **average of at most ~1 request / 3 minutes** (20/hour vs the ~28-30/
hour cap), leaving ~8-10 requests/hour of headroom for manual commands,
wake-from-sleep catch-up, and the bounded urgent mode below.

Health invariant to watch in the logs: steady state shows zero http-429.
A post-burst 429 does NOT reliably clear at its stated horizon — measured
over one machine's full log (re-measured 2026-08-03; re-derive rather than
trust this verbatim, it ages as the log grows — method recorded next to
``usage_store.RETRY_AFTER_MARGIN_S``), 20 of 35 lapsed blocks re-blocked
within 900s of their own deadline (+2s..+887s), each for a fresh full hour
("of 35", not 38 raw gaps: 3 are negative — NOT a uniform mechanism (one has
no within-block revision at all, one is revised BACKWARD mid-block, one is
unchanged — see usage_store.RETRY_AFTER_MARGIN_S's comment for the per-gap
detail) — excluded from both numerator and denominator; round 8 switched
from the prior "21 of 36"/"2 of 38" figures here to these, on the OTHER of
two equally-reproducing readings — see usage_store.RETRY_AFTER_MARGIN_S's
comment for which reading and why).
The prior "10 of 23" figure here did not reproduce under any of 40 method
variants swept and is superseded. That is why the wait is Retry-After plus
``usage_store.RETRY_AFTER_MARGIN_S`` and not Retry-After alone. What would
mean this model needs revisiting is a 429 episode at modest rates that
outlasts an hour *past* that margin.

Plans computed here are persisted per account in the usage store
(``nextPollAt``/``pollIntervalS``) by whichever collector fetched, so every
surface — ``cswap list``, the TUI, the menu bar, the auto engine — inherits
the same cadence no matter how often it repaints.

If a future probe revises the measured shape, adjust the constants in this
module only.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from claude_swap import oauth

# Freshness floor shared by every collector: an entry younger than this is
# served from the store without any fetch, so the maximum sustained rate on
# one token is 1/SERVE_TTL_S regardless of how many surfaces are open.
SERVE_TTL_S = 180.0

# Normal cadence floor — movement can halve an interval down to this, never
# below.
MIN_INTERVAL_S = 180.0

# Urgent mode: the ACTIVE account, within ESCALATION_MARGIN_PCT of the
# switch threshold, with movement observed this poll (i.e. actually burning
# toward the limit). Bounded by construction: either the threshold is crossed
# (the engine switches away) or the movement stops (the next poll decays back
# to MIN_INTERVAL_S) — worst case margin/movement-delta ≈ 15 polls per
# episode, inside the measured ~28-30 request rolling-hour window; overshoot
# on top of steady traffic is absorbed by the post-429 floor below.
URGENT_INTERVAL_S = 60.0

# Per-window urgency (cc-swap maximize: ``window_caps``). Each window keys
# on its OWN marks (the next switch-at-once mark above it: urgent, then
# hard) and on the pace between the last two readings, not on one threshold
# for the binding window: a 7d in the 80s is hours from a 98% cap, and
# polling the active account every minute for it is what spent its login's
# budget on 2026-10-06 (ml-main: 60 s polls from 23:44, the first 429 at
# 00:10 — ~26 reads, the measured ~28-30/hour — then 90 minutes of 429s).
# URGENT_INTERVAL_S while a moving window reaches its mark within
# URGENT_ETA_S (at most ~8 polls: the switch then happens), NEAR_INTERVAL_S
# within NEAR_ETA_S (~6 more). With the 180 s far cadence that is ~24
# reads in the hour before a switch, under the ~30/hour budget with room
# for the other surfaces (Fleet, menu bar, `list`) sharing the login.
NEAR_INTERVAL_S = 120.0
URGENT_ETA_S = 480.0
NEAR_ETA_S = 1200.0

# Decay ceilings for an account whose usage is not moving: the active account
# stays reasonably fresh, an idle alternate drifts out to ten minutes.
ACTIVE_MAX_INTERVAL_S = 300.0
CANDIDATE_DEFAULT_INTERVAL_S = 300.0
CANDIDATE_MAX_INTERVAL_S = 600.0

# Exhaustion is stable enough to poll slowly, but not to stop polling until a
# reported reset. Quota grants and provider-side corrections can make an
# account usable before that timestamp, and decision-grade status must not age
# into "unavailable" while the scheduler is deliberately waiting. Ten-minute
# polling (six requests/hour) stays below the measured budget and
# detects recovery promptly; a nearer reported reset still pulls the next poll
# forward.
EXHAUSTED_INTERVAL_S = 600.0

# A window whose binding pct moved at least this much between polls is being
# consumed somewhere (this machine, another PC, session mode) → tighten; an
# unmoved one backs off toward its ceiling.
MOVEMENT_DELTA_PCT = 1.0

# ±fraction applied to each scheduled interval so independent processes
# (watch + menu bar + auto) drift apart instead of fetching in lockstep.
JITTER_FRAC = 0.1

# Reaction to a 429 with ``Retry-After: 0`` (the saturated-window edge):
# probe at most every 5 minutes (≤12/hour) so aging-out — up to ~30/hour —
# outpaces the probing (used by the usage store's failure backoff)...
EDGE_BACKOFF_S = 300.0
# ...and while any 429 was seen on the token within this window, floor the
# planned cadence here so freed capacity accumulates instead of being
# re-spent. The window matches the saturation horizon: a full trailing hour
# takes up to 60 minutes to age out.
POST_429_MIN_INTERVAL_S = 360.0
RECENT_429_WINDOW_S = 3600.0

# AIMD on a contended budget. The budget is shared across every machine
# polling the same account (under the account-scoped regime a machine with its
# own token is no less a competitor — see the module docstring on scope),
# none of them can see the others, and the endpoint
# exposes no remaining-request count — only a Retry-After once already
# blocked. So while 429s recur, each successful poll multiplicatively grows the
# interval (×POST_429_BACKOFF_MULT) toward POST_429_MAX_INTERVAL_S — wider than
# the normal candidate ceiling so several machines can each back off far enough
# that their combined rate fits under the budget. Movement (a real success run
# with no recent 429) decays it back down. This is TCP-style congestion control:
# the budget gets fair-shared by reaction alone, with no machine count or
# shared state to configure.
POST_429_BACKOFF_MULT = 1.5
POST_429_MAX_INTERVAL_S = 1800.0

# The engine escalates to a full candidate refresh when the active account is
# within this margin of the threshold (decision policy, but the urgent-mode
# cadence keys on the same band, so it lives with the cadence numbers).
ESCALATION_MARGIN_PCT = 15.0

# Never schedule a poll later than a known window reset (+ slack): stored
# usage is obsolete the moment the window rolls over.
RESET_SLACK_S = 60.0


def binding_pct(usage: dict | None, models: tuple[str, ...] = ()) -> float | None:
    """Utilization of the binding (worst) relevant window, or None."""
    headroom = oauth.account_headroom(usage, models)
    return None if headroom is None else 100.0 - headroom


def limiting_reset_ts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch when the last of the ≥100% relevant windows resets (account
    usable again)."""
    latest: float | None = None
    for _, pct, resets_at in oauth.relevant_windows(usage, models):
        if pct < 100.0:
            continue
        ts = parse_reset_ts(resets_at)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def earliest_future_reset_ts(
    usage: dict | None, now: float, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch of the next relevant-window reset ahead of ``now``, any
    utilization."""
    earliest: float | None = None
    for _, _, resets_at in oauth.relevant_windows(usage, models):
        ts = parse_reset_ts(resets_at)
        if ts is not None and ts > now and (earliest is None or ts < earliest):
            earliest = ts
    return earliest


def parse_reset_ts(resets_at: str | None) -> float | None:
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def _window_map(
    usage: dict | None, models: tuple[str, ...]
) -> dict[str, float]:
    return {label: pct for label, pct, _ in oauth.relevant_windows(usage, models)}


def _moved_windows(
    prev_usage: dict | None,
    new_usage: dict | None,
    models: tuple[str, ...],
) -> list[str]:
    """The windows (any relevant one) that moved at least MOVEMENT_DELTA_PCT."""
    before = _window_map(prev_usage, models)
    after = _window_map(new_usage, models)
    return [
        w for w, pct in after.items()
        if w in before and abs(pct - before[w]) >= MOVEMENT_DELTA_PCT
    ]


def _next_mark(marks: float | Sequence[float], pct: float) -> float:
    """The lowest of a window's marks above ``pct`` (100 past them all)."""
    listed = [float(marks)] if isinstance(marks, (int, float)) else [float(m) for m in marks]
    above = [m for m in listed if m > pct]
    return min(above) if above else 100.0


def _capped_interval(
    prev_usage: dict | None,
    new_usage: dict | None,
    models: tuple[str, ...],
    caps: Mapping[str, float | Sequence[float]],
    prev_fetched_at: float | None,
    now: float,
    threshold: float,
) -> float:
    """The active account's cadence by its fastest-approaching window
    (``window_caps``; see ``plan_after_fetch``). A window without a cap of
    its own keys on ``threshold``."""
    before = _window_map(prev_usage, models)
    after = _window_map(new_usage, models)
    elapsed = (now - prev_fetched_at) if prev_fetched_at is not None else None
    best = float("inf")
    for w, pct in after.items():
        cap = _next_mark(caps.get(w, threshold), pct)
        rise = pct - before[w] if w in before else 0.0
        if elapsed is None or elapsed <= 0 or w not in before:
            # No pace: the threshold band, on this window's own cap.
            if rise >= MOVEMENT_DELTA_PCT and pct >= cap - ESCALATION_MARGIN_PCT:
                best = min(best, URGENT_INTERVAL_S)
            continue
        if rise < MOVEMENT_DELTA_PCT:
            continue
        eta = max(cap - pct, 0.0) / (rise / elapsed)
        if eta <= URGENT_ETA_S:
            best = min(best, URGENT_INTERVAL_S)
        elif eta <= NEAR_ETA_S:
            best = min(best, NEAR_INTERVAL_S)
    return best


def plan_after_fetch(
    *,
    prev_interval_s: float | None,
    prev_usage: dict | None,
    new_usage: dict | None,
    is_active: bool,
    threshold: float,
    models: tuple[str, ...],
    recent_429: bool,
    now: float,
    rng: Callable[[], float] = random.random,
    window_caps: Mapping[str, float | Sequence[float]] | None = None,
    prev_fetched_at: float | None = None,
) -> tuple[float, float]:
    """``(next_poll_at, interval_s)`` for an account just fetched successfully.

    Movement (binding pct changed ≥ ``MOVEMENT_DELTA_PCT`` since the previous
    poll) halves the interval, floored at ``MIN_INTERVAL_S`` — or drops to
    ``URGENT_INTERVAL_S`` when the active account is moving inside the
    escalation band. No movement backs off ×1.5 toward the account's ceiling;
    unknown utilization uses the default. A recent 429 on this token floors
    the cadence at ``POST_429_MIN_INTERVAL_S`` (and suppresses urgent mode)
    until ``RECENT_429_WINDOW_S`` has passed. The scheduled time gets
    ``JITTER_FRAC`` noise, is never later than the account's next window
    reset (+ ``RESET_SLACK_S``). An at-limit account keeps a bounded slow
    poll instead of sleeping until that reset, so an early provider-side
    quota grant is observed and its decision-grade status stays current.

    ``window_caps`` (window label → its own cap; cc-swap maximize passes the
    hard caps) replaces the threshold test for the active account: urgent
    (``URGENT_INTERVAL_S``) while a moving window's pace since the previous
    reading (``prev_fetched_at``) reaches its cap within ``URGENT_ETA_S``,
    ``NEAR_INTERVAL_S`` within ``NEAR_ETA_S``; with no pace to measure, the
    threshold band on each window's own cap.
    """
    default = MIN_INTERVAL_S if is_active else CANDIDATE_DEFAULT_INTERVAL_S
    ceiling = ACTIVE_MAX_INTERVAL_S if is_active else CANDIDATE_MAX_INTERVAL_S
    base = prev_interval_s or default
    prev_pct = binding_pct(prev_usage, models)
    new_pct = binding_pct(new_usage, models)
    moved = (
        _moved_windows(prev_usage, new_usage, models)
        if window_caps
        else None
    )
    if prev_pct is None or new_pct is None:
        moving = False
        interval = default
    elif (
        abs(new_pct - prev_pct) >= MOVEMENT_DELTA_PCT
        if moved is None
        else bool(moved)
    ):
        moving = True
        interval = max(MIN_INTERVAL_S, base / 2)
    else:
        # Floored so a sub-floor base (urgent mode's 60s) snaps straight back
        # to the normal cadence once movement stops, instead of decaying
        # through 90s/135s polls that the budget never intended.
        moving = False
        interval = min(ceiling, max(MIN_INTERVAL_S, base * 1.5))
    if is_active and moving and not recent_429 and new_pct is not None:
        if window_caps:
            interval = min(
                interval,
                _capped_interval(
                    prev_usage, new_usage, models, window_caps,
                    prev_fetched_at, now, threshold,
                ),
            )
        elif new_pct >= threshold - ESCALATION_MARGIN_PCT:
            interval = URGENT_INTERVAL_S
    if recent_429:
        # AIMD additive-increase: grow the interval multiplicatively from the
        # last one toward the wider 429 ceiling, so machines sharing a
        # contended token each retreat until their combined rate fits the
        # budget. Floored at POST_429_MIN_INTERVAL_S for the first 429.
        increased = max(base * POST_429_BACKOFF_MULT, POST_429_MIN_INTERVAL_S)
        interval = min(POST_429_MAX_INTERVAL_S, max(interval, increased))

    headroom = oauth.account_headroom(new_usage, models)
    if headroom is not None and headroom <= 0:
        # Keep probing exhausted accounts: Anthropic can grant/reset quota
        # before the previously advertised timestamp. Preserve a wider
        # post-429 interval if congestion control already selected one.
        interval = max(interval, EXHAUSTED_INTERVAL_S)

    next_poll = now + interval * (1.0 + JITTER_FRAC * (2.0 * rng() - 1.0))
    if headroom is not None and headroom <= 0:
        reset_ts = limiting_reset_ts(new_usage, models)
        if reset_ts is not None and reset_ts > now:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
    else:
        reset_ts = earliest_future_reset_ts(new_usage, now, models)
        if reset_ts is not None:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
    return next_poll, interval
