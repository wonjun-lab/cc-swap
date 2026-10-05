"""Unit tests for the shared usage-poll cadence policy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_swap import poll_policy

NOW = 1_000_000.0
HALF = lambda: 0.5  # noqa: E731 — rng midpoint: jitter factor exactly 1.0


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 0.0}}


def _plan(**overrides):
    kwargs = dict(
        prev_interval_s=None,
        prev_usage=None,
        new_usage=_usage(10),
        is_active=False,
        threshold=90.0,
        models=(),
        recent_429=False,
        now=NOW,
        rng=HALF,
    )
    kwargs.update(overrides)
    return poll_policy.plan_after_fetch(**kwargs)


class TestIntervalAdaptation:
    def test_first_fetch_uses_defaults(self):
        _, active = _plan(is_active=True)
        _, candidate = _plan(is_active=False)
        assert active == poll_policy.MIN_INTERVAL_S
        assert candidate == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_unmoved_decays_toward_the_ceiling(self):
        _, interval = _plan(prev_interval_s=300.0, prev_usage=_usage(10))
        assert interval == 450.0
        _, capped = _plan(prev_interval_s=500.0, prev_usage=_usage(10))
        assert capped == poll_policy.CANDIDATE_MAX_INTERVAL_S
        _, active_capped = _plan(
            prev_interval_s=250.0, prev_usage=_usage(10), is_active=True
        )
        assert active_capped == poll_policy.ACTIVE_MAX_INTERVAL_S

    def test_movement_halves_floored_at_min(self):
        _, interval = _plan(
            prev_interval_s=600.0, prev_usage=_usage(10), new_usage=_usage(15)
        )
        assert interval == 300.0
        _, floored = _plan(
            prev_interval_s=200.0, prev_usage=_usage(10), new_usage=_usage(15)
        )
        assert floored == poll_policy.MIN_INTERVAL_S

    def test_sub_delta_wiggle_is_not_movement(self):
        _, interval = _plan(
            prev_interval_s=300.0,
            prev_usage=_usage(10),
            new_usage=_usage(10.5),  # below MOVEMENT_DELTA_PCT
        )
        assert interval == 450.0

    def test_unknown_pct_uses_the_default(self):
        _, interval = _plan(prev_interval_s=600.0, new_usage=None)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S


class TestHighUsageCadence:
    """The active account is read every 120 s at >= 80% (5h or 7d), else on
    the normal cadence; a 429 wins."""

    def _kwargs(self, **overrides):
        kwargs = dict(
            prev_interval_s=poll_policy.MIN_INTERVAL_S,
            prev_usage=_usage(70),
            new_usage=_usage(79),
            is_active=True,
            threshold=90.0,
        )
        kwargs.update(overrides)
        return kwargs

    def test_the_constants(self):
        assert poll_policy.ACTIVE_HIGH_USAGE_PCT == 80.0
        assert poll_policy.ACTIVE_HIGH_USAGE_INTERVAL_S == 120.0

    def test_79_percent_keeps_the_normal_cadence(self):
        _, interval = _plan(**self._kwargs())  # moving: halving, floored
        assert interval == poll_policy.MIN_INTERVAL_S
        _, backoff = _plan(**self._kwargs(new_usage=_usage(79), prev_usage=_usage(79)))
        assert backoff == 270.0  # unmoved: the usual x1.5 back-off

    def test_80_percent_is_every_120_s(self):
        _, interval = _plan(**self._kwargs(new_usage=_usage(80)))
        assert interval == 120.0

    def test_it_holds_at_120_s_whether_or_not_it_moves(self):
        _, interval = _plan(
            **self._kwargs(
                prev_interval_s=300.0, prev_usage=_usage(85), new_usage=_usage(85)
            )
        )
        assert interval == 120.0

    def test_unknown_previous_reading_still_uses_it(self):
        _, interval = _plan(**self._kwargs(prev_usage=None, new_usage=_usage(90)))
        assert interval == 120.0

    def test_a_7d_at_or_above_80_is_every_120_s(self):
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 80.0}}
        _, interval = _plan(**self._kwargs(prev_usage=usage, new_usage=usage))
        assert interval == 120.0
        low = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 79.0}}
        _, interval = _plan(**self._kwargs(prev_usage=low, new_usage=low))
        assert interval == 270.0

    def test_candidate_is_unchanged(self):
        _, interval = _plan(**self._kwargs(is_active=False, new_usage=_usage(85)))
        assert interval == poll_policy.MIN_INTERVAL_S  # plain movement halving
        _, still = _plan(
            **self._kwargs(
                is_active=False, prev_interval_s=300.0,
                prev_usage=_usage(85), new_usage=_usage(85),
            )
        )
        assert still == 450.0

    def test_a_429_overrides_the_120_s_cadence(self):
        _, interval = _plan(**self._kwargs(new_usage=_usage(85), recent_429=True))
        assert interval == poll_policy.POST_429_MIN_INTERVAL_S
        _, grown = _plan(
            **self._kwargs(
                new_usage=_usage(85), recent_429=True, prev_interval_s=540.0
            )
        )
        assert grown == 810.0  # AIMD growth

    def test_below_80_after_120_s_snaps_back_to_the_floor(self):
        # A sub-floor base (the 120 s plan) never decays through 135 s polls.
        _, interval = _plan(
            **self._kwargs(
                prev_interval_s=120.0, prev_usage=_usage(79), new_usage=_usage(79)
            )
        )
        assert interval == poll_policy.MIN_INTERVAL_S


class TestPost429Floor:
    def test_recent_429_floors_the_cadence(self):
        _, interval = _plan(recent_429=True, prev_usage=_usage(10))
        assert interval >= poll_policy.POST_429_MIN_INTERVAL_S

    def test_slower_learned_cadence_survives_the_floor(self):
        # A learned interval already above the floor is grown (×1.5), never
        # dropped back to the floor.
        _, interval = _plan(
            recent_429=True, prev_interval_s=590.0, prev_usage=_usage(10)
        )
        assert interval == pytest.approx(590.0 * poll_policy.POST_429_BACKOFF_MULT)
        assert interval > poll_policy.POST_429_MIN_INTERVAL_S


class TestPost429Aimd:
    """AIMD backoff on a contended token: while 429s recur, each successful
    poll multiplicatively increases the interval toward a wider 429 ceiling, so
    independent machines sharing one token each retreat and their combined poll
    rate converges under the endpoint budget (no cross-machine coordination)."""

    def test_recent_429_multiplicatively_increases_from_prev(self):
        # A prior 360s interval, still seeing 429s, is pushed up (×1.5), not
        # held flat at the floor.
        _, interval = _plan(
            recent_429=True,
            prev_interval_s=poll_policy.POST_429_MIN_INTERVAL_S,  # 360
            prev_usage=_usage(10),
        )
        assert interval > poll_policy.POST_429_MIN_INTERVAL_S
        assert interval == pytest.approx(
            poll_policy.POST_429_MIN_INTERVAL_S * poll_policy.POST_429_BACKOFF_MULT
        )

    def test_recent_429_ceiling_exceeds_normal_candidate_max(self):
        # The 429 ceiling is wider than the normal candidate ceiling so a
        # contended token can back off far enough for several machines to fit.
        assert (
            poll_policy.POST_429_MAX_INTERVAL_S
            > poll_policy.CANDIDATE_MAX_INTERVAL_S
        )
        _, interval = _plan(
            recent_429=True,
            prev_interval_s=poll_policy.POST_429_MAX_INTERVAL_S,  # already at ceiling
            prev_usage=_usage(10),
        )
        assert interval == poll_policy.POST_429_MAX_INTERVAL_S

    def test_no_429_uses_normal_ceiling(self):
        # Without recent 429s the wider ceiling never applies (normal cadence).
        _, interval = _plan(
            recent_429=False, prev_interval_s=590.0, prev_usage=_usage(10)
        )
        assert interval == poll_policy.CANDIDATE_MAX_INTERVAL_S

    def _converge_trajectory(self, recent_429: bool, rounds: int = 12):
        # Deterministic evolution of the interval under a sustained contended
        # token: each successful poll re-plans with the same recent_429 flag and
        # unmoved usage (movement decay would only shorten it — the worst case
        # for convergence is an unmoving account that just keeps 429ing). rng at
        # the midpoint so jitter is exactly 1.0 and the trajectory is exact.
        prev = None
        traj = []
        for _ in range(rounds):
            _, interval = _plan(
                recent_429=recent_429,
                prev_interval_s=prev,
                prev_usage=_usage(10),
                new_usage=_usage(10),
            )
            traj.append(interval)
            prev = interval
        return traj

    def test_sustained_429_grows_the_interval_to_the_wide_ceiling(self):
        # THE convergence property: while 429s recur, the interval must keep
        # growing (×MULT) until it reaches POST_429_MAX_INTERVAL_S. This is what
        # lets N machines sharing one token each back off far enough that their
        # combined rate drops under the budget — the deadlock cannot clear
        # without it.
        traj = self._converge_trajectory(recent_429=True)
        # strictly increasing until it saturates at the wide ceiling
        assert traj[-1] == poll_policy.POST_429_MAX_INTERVAL_S
        assert traj == sorted(traj)  # monotonic non-decreasing
        # actually reaches the ceiling within the simulated rounds
        assert max(traj) == poll_policy.POST_429_MAX_INTERVAL_S
        # each pre-ceiling step grew by the multiplier (AIMD multiplicative incr)
        for a, b in zip(traj, traj[1:]):
            if b < poll_policy.POST_429_MAX_INTERVAL_S:
                assert b == pytest.approx(a * poll_policy.POST_429_BACKOFF_MULT)

    def test_without_recency_the_interval_is_capped_at_the_narrow_ceiling(self):
        # The failure mode the recency bug caused: if recent_429 is False on the
        # post-block success (the pre-fix behavior after an honored hour-scale
        # Retry-After), the interval can never exceed CANDIDATE_MAX_INTERVAL_S.
        # N machines then jam at 600s each and their combined rate can sit above
        # the budget forever — a permanent deadlock the AIMD is meant to break.
        traj = self._converge_trajectory(recent_429=False)
        assert max(traj) == poll_policy.CANDIDATE_MAX_INTERVAL_S
        assert (
            poll_policy.CANDIDATE_MAX_INTERVAL_S
            < poll_policy.POST_429_MAX_INTERVAL_S
        )


class TestResetCapping:
    def _iso(self, ts: float) -> str:
        return (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_poll_never_scheduled_past_a_future_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(40, self._iso(reset_ts)))
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_at_limit_keeps_bounded_polling_before_distant_reset(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)
        assert next_poll < reset_ts

    def test_at_limit_poll_is_pulled_to_an_imminent_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)

    @pytest.mark.parametrize("reset_ts", [NOW - 90.0, NOW])
    def test_at_limit_ignores_non_future_reset(self, reset_ts):
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)

    def test_active_at_limit_uses_same_bounded_recovery_probe(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(
            new_usage=_usage(100, self._iso(reset_ts)), is_active=True
        )
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)


class TestJitter:
    def test_jitter_bounds(self, monkeypatch):
        monkeypatch.setattr("claude_swap.poll_policy.JITTER_FRAC", 0.1)
        early, _ = _plan(rng=lambda: 0.0)
        late, _ = _plan(rng=lambda: 1.0)
        interval = poll_policy.CANDIDATE_DEFAULT_INTERVAL_S
        assert early == pytest.approx(NOW + interval * 0.9)
        assert late == pytest.approx(NOW + interval * 1.1)


class TestBudgetInvariants:
    """Relationships the measured rate limit demands of the constants.

    Measured 2026-07-11 (probe3): a rolling ~60-minute window of ~28-30
    requests per token × UA-class — not a refilling bucket. Capacity returns
    only as old requests age out of the trailing hour, so a saturated window
    needs up to 60 minutes to recover. These invariants lean only on the
    robust parts of that measurement (a safe sustained cadence and an
    hour-scale recovery horizon), not on the exact server algorithm.
    """

    def test_sustained_floor_stays_under_the_hourly_cap(self):
        # 3600/180 = 20 requests/hour vs the measured ~28-30/hour window.
        assert poll_policy.MIN_INTERVAL_S >= 180.0
        assert poll_policy.SERVE_TTL_S >= 180.0

    def test_edge_backoff_probes_slower_than_capacity_frees(self):
        # While saturated, capacity returns at up to ~30/hour as the old
        # burst ages out; probing at ≥300 s (≤12/hour) lets recovery win.
        assert poll_policy.EDGE_BACKOFF_S >= 300.0

    def test_post_429_floor_covers_the_saturation_horizon(self):
        # A 429 means the trailing hour is full, and it takes up to 60
        # minutes for the spending burst to age out entirely.
        assert poll_policy.RECENT_429_WINDOW_S >= 3600.0
        assert poll_policy.POST_429_MIN_INTERVAL_S >= poll_policy.MIN_INTERVAL_S

    def test_one_machine_at_the_high_cadence_sits_at_the_budget(self):
        # 3600 / 120 = 30 reads an hour: at the measured ~28-30 budget, which
        # is why a second machine on the account is expected to see 429s and
        # the post-429 plan must keep winning over this cadence.
        assert 3600.0 / poll_policy.ACTIVE_HIGH_USAGE_INTERVAL_S == 30.0
        assert poll_policy.POST_429_MIN_INTERVAL_S > poll_policy.ACTIVE_HIGH_USAGE_INTERVAL_S
