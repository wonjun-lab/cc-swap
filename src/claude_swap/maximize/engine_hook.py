"""Runs the maximize policy inside ``AutoSwitchEngine`` ticks (spec §4.1).

The upstream engine keeps everything it already owns — usage collection,
poll scheduling, freshen, quarantine, ``_perform``, events. This module turns
one tick's collected state into a :class:`Snapshot`, asks ``policy.decide``,
and maps the decision back onto those upstream paths:

* ``Switch``        → ``_freshen_target`` + ``_perform``; a target whose
                      freshen fails is set aside and the policy re-decides
                      (a ``Hold`` then takes the hold path below unless the
                      failure was systemic)
* ``Hold``          → ``NoSwitchEvent``; a pending hold also pulls the active
                      account's next poll to ``pending_poll_s``, a reset-aware
                      wait (``reset-wait``) to the urgent 60 s cadence
* ``Indeterminate`` → ``None``: ``_tick_inner`` continues into its own
                      unknown-usage counting and failover
* ``Exhausted``     → ``AllExhaustedEvent`` + reset-aware sleep, or a
                      normal-cadence ``NoSwitchEvent`` when not provable

An account hold (``cc-swap hold``, maximize/hold.py) on the active account
becomes the Snapshot's ``hold_until``; a hold that no longer applies (past
its end, or on a slot that is no longer active) is cleared here, and a
switch the engine makes ends the hold on the account it leaves.

The learned ride (maximize/ride.py) keeps three state-file records: the
whole-point steps of each account while active (``rideSteps``, T1), the
rides (``maximizeRide``: each account's arm time and T1 per window at its
mark, kept until that window resets, and which windows the active
account's last acted-on decision rode), and what was learned
(``rideLearning``, q per window). A ride the engine ended with its
hard switch before 100% raises q; 100% read while it rode halves q; an
idle switch, a dry run, ``auto off`` and an unreadable tick teach nothing.
A ride polls the active account at the urgent 60 s cadence over its last
``RESET_WAIT_URGENT_S``, at ``pendingPollS`` before that.

Per-engine state lives on a :class:`MaximizeRuntime` attached to the engine
as ``_maximize_runtime`` (``attach_maximize`` / ``runtime_for``).
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from claude_swap import autoswitch as aw
from claude_swap import oauth, poll_policy, shared_login
from claude_swap.exceptions import ConfigError
from claude_swap.maximize import history, idle, ledger, notify, pause, policy
from claude_swap.maximize import hold as account_hold
from claude_swap.maximize import ride as learned_ride
from claude_swap.maximize.model import (
    AccountView,
    Decision,
    Exhausted,
    Forecast,
    Hold,
    Indeterminate,
    Sample,
    Snapshot,
    Switch,
)
from claude_swap.maximize.plan import plan_label, rate_limit_tier_from_credentials
from claude_swap.maximize.report import decision_rows
from claude_swap.maximize.score import score
from claude_swap.maximize.snapshot import build_snapshot, usage_windows
from claude_swap.settings import (
    MaximizeSettings,
    PrimeSettings,
    load_maximize_settings,
    load_prime_settings,
    merge_maximize_cli,
    settings_path,
)

_logger = logging.getLogger("claude-swap")

RUNTIME_ATTR = "_maximize_runtime"
# ``AutoSwitchEngine._emit`` calls this attribute with every event (the
# engine's desktop notifications, maximize/notify.py).
NOTIFY_ATTR = "_event_tap"
SAMPLES_KEY = "maximizeSamples"
# Inside the SAMPLES_KEY record: when its account became the active one.
CHANGED_KEY = "activeChangedAt"
# rateLimitTier only changes with a plan change; re-read the stored
# credential (a Keychain read on macOS) at most hourly per slot.
TIER_CACHE_TTL_S = 3600.0
# A login's deadline (``refreshTokenExpiresAt``) only moves with a re-login:
# re-read the stored credentials at most this often.
LOGIN_DEADLINE_TTL_S = 600.0
# Warn in the engine log from this long before a login's deadline ...
LOGIN_WARN_S = 7 * 86400.0
# ... once per account per this long.
LOGIN_WARN_EVERY_S = 86400.0
# The decision this engine last made, for TUI viewers (``_publish_decision``).
DECISION_KEY = "maximizeDecision"
# An unchanged decision is rewritten this often, so a reader can tell a
# steady engine from a stopped one by the record's age.
PUBLISH_REFRESH_S = 300.0
# A reset-aware wait polls the active account every URGENT_INTERVAL_S only
# over the last this-many seconds before the reset: at most 15 polls a wait,
# the planner's own bound on an urgent episode, whatever resetWaitMin says.
RESET_WAIT_URGENT_S = 900.0
# The active account's learned ride (see the module docstring).
RIDE_KEY = "maximizeRide"

# Numeric ``maximize`` keys. The settings loader is lenient per key (wrong
# type → default, out of range → clamped) and reports only pair repairs in
# ``problems``; a per-key repair shows up as a loaded value that differs from
# the file's raw value (see rejected_keys).
_NUMERIC_KEYS: tuple[tuple[str, str], ...] = (
    ("soft5h", "soft_5h"),
    ("hard5h", "hard_5h"),
    ("soft7d", "soft_7d"),
    ("hard7d", "hard_7d"),
    ("landingMargin", "landing_margin"),
    ("idleWindowMin", "idle_window_min"),
    ("idleMaxDeltaPct", "idle_max_delta_pct"),
    ("forceEtaMin", "force_eta_min"),
    ("pendingPollS", "pending_poll_s"),
    ("rebalanceCooldownMin", "rebalance_cooldown_min"),
    ("tieEpsilon", "tie_epsilon"),
    ("loginExpiryGuardMin", "login_expiry_guard_min"),
    ("resetWaitMin", "reset_wait_min"),
    ("preemptHorizonMaxH", "preempt_horizon_max_h"),
    ("busyRebalanceGap", "busy_rebalance_gap"),
    ("rideMaxMin", "ride_max_min"),
)


class PrimerLike(Protocol):
    def run_due(self, snap: Snapshot) -> list[aw.AutoSwitchEvent]: ...


@dataclass
class MaximizeRuntime:
    settings: MaximizeSettings
    prime_settings: PrimeSettings
    cli_args: Any = None              # the `auto --soft5h ...` namespace, or None
    settings_mtime: int | None = None
    primer: PrimerLike | None = None
    # Dry runs never write autoswitch_state.json; their samples live here.
    dry_samples: dict = field(default_factory=dict)
    tier_cache: dict[str, tuple[str, str | None, float]] = field(default_factory=dict)
    last_snapshot: Snapshot | None = None
    last_decision: Decision | None = None
    # Login deadlines (epoch s) by slot, read at ``login_deadlines_at``.
    login_deadlines: dict[str, float] = field(default_factory=dict)
    login_deadlines_at: float | None = None
    login_warned: dict[str, float] = field(default_factory=dict)
    # (ledger's last destination, live slot) seen on the previous tick while
    # they differed: a login changed outside cc-swap once it repeats.
    drift_seen: tuple[object, str] | None = None
    # The usage history writer (maximize/history.py), loaded on first use.
    history: history.Recorder | None = None
    # Dry runs keep the learned ride's records here (never learning).
    dry_ride: dict = field(default_factory=dict)


# -- settings ------------------------------------------------------------------


def poll_threshold(s: MaximizeSettings) -> float:
    """The threshold both poll inputs key on: the hard caps (spec §4.1).

    It feeds ``_collect_scheduled_usage``'s candidate escalation and the
    planner's 60 s urgent mode. Keying either on soft would re-poll every
    candidate from "binding >= 35%" all week, and hold the active account
    at 60 s polls while a soft switch waits for idle, past the ~30/hour
    per-account budget. The pending wait has its own cadence
    (``pending_poll_s``, see ``_pull_active_poll``).
    """
    return min(s.hard_5h, s.hard_7d)


def _apply_poll_inputs(engine: aw.AutoSwitchEngine, s: MaximizeSettings) -> None:
    # apply_threshold sets settings.threshold (escalation, PollEvent label)
    # and switcher.set_poll_policy_inputs (urgent mode) in one call.
    engine.apply_threshold(poll_threshold(s))


def _settings_mtime(engine: aw.AutoSwitchEngine) -> int | None:
    try:
        return settings_path(engine.switcher.backup_dir).stat().st_mtime_ns
    except OSError:
        return None


def _raw_settings(engine: aw.AutoSwitchEngine) -> dict | None:
    """settings.json as a dict: ``{}`` when missing, None when unreadable."""
    path = settings_path(engine.switcher.backup_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _primer_class():
    """``maximize.primer.Primer`` once that module defines it, else None.

    ``ImportError`` (not only ``ModuleNotFoundError``): between Task 10 and
    Task 11 ``primer.py`` exists without ``Primer``. Only a failure naming
    the primer module itself means "not there yet"; any other import error
    inside it is a real bug and propagates.
    """
    try:
        from claude_swap.maximize.primer import Primer
    except ImportError as e:  # ModuleNotFoundError is a subclass
        if e.name != "claude_swap.maximize.primer":
            raise
        return None
    return Primer


def _build_primer(
    engine: aw.AutoSwitchEngine, prime: PrimeSettings
) -> PrimerLike | None:
    if not prime.enabled:
        return None
    cls = _primer_class()
    return None if cls is None else cls(engine, prime, clock=engine.clock)


def _load(engine: aw.AutoSwitchEngine, cli_args: Any) -> tuple[
    MaximizeSettings, MaximizeSettings, PrimeSettings, list[str], bool
]:
    """``(file_values, effective, prime, problems, maximize_ok)``.

    ``file_values`` is the lenient load of the ``maximize`` section;
    ``effective`` has the CLI flags merged on top. ``maximize_ok`` is False
    when the loader had to repair a soft > hard pair or the flags now
    contradict the file (``merge_maximize_cli`` raised ``ConfigError``);
    ``effective`` is then ``file_values``.
    """
    root = engine.switcher.backup_dir
    problems: list[str] = []
    file_values = load_maximize_settings(root, problems=problems)
    maximize_ok = not problems
    effective = file_values
    try:
        effective = merge_maximize_cli(file_values, cli_args)
    except ConfigError as e:
        problems.append(str(e))
        maximize_ok = False
    prime_problems: list[str] = []
    prime = load_prime_settings(root, problems=prime_problems)
    return file_values, effective, prime, problems + prime_problems, maximize_ok


def attach_maximize(
    engine: aw.AutoSwitchEngine, *, cli_args: Any = None
) -> MaximizeRuntime:
    """Load maximize/prime settings (plus CLI flags) onto ``engine``.

    ``cli_args`` defaults to ``engine.maximize_cli`` (the ``auto --soft5h``
    namespace Task 4 hands the engine). Runs lazily on the first maximize
    tick; a host that wants the first tick's escalation threshold right too
    calls it right after building the engine.
    """
    if cli_args is None:
        cli_args = getattr(engine, "maximize_cli", None)
    _file, settings, prime, problems, _ok = _load(engine, cli_args)
    rt = MaximizeRuntime(
        settings=settings,
        prime_settings=prime,
        cli_args=cli_args,
        settings_mtime=_settings_mtime(engine),
    )
    rt.primer = _build_primer(engine, prime)
    setattr(engine, RUNTIME_ATTR, rt)
    if not isinstance(getattr(engine, NOTIFY_ATTR, None), notify.EngineNotifier):
        # AutoSwitchEngine._emit hands it every event (desktop notifications).
        setattr(engine, NOTIFY_ATTR, notify.EngineNotifier(engine))
    _apply_poll_inputs(engine, settings)
    if problems:
        engine._emit(aw.ConfigWarningEvent(
            message="settings.json: " + "; ".join(problems)
        ))
    return rt


def runtime_for(engine: aw.AutoSwitchEngine) -> MaximizeRuntime:
    rt = getattr(engine, RUNTIME_ATTR, None)
    return rt if isinstance(rt, MaximizeRuntime) else attach_maximize(engine)


def marks_label(s: MaximizeSettings) -> str:
    """``5h soft 50/hard 95 · 7d soft 90/hard 98``: when maximize switches."""
    return (
        f"5h soft {aw.pct_label(s.soft_5h)}/hard {aw.pct_label(s.hard_5h)} · "
        f"7d soft {aw.pct_label(s.soft_7d)}/hard {aw.pct_label(s.hard_7d)}"
    )


def poll_marks(engine: aw.AutoSwitchEngine) -> str:
    """The poll line's label: the marks this engine's maximize policy uses
    (session overrides and ``auto --soft5h`` flags included). Loads the
    runtime if the first maximize tick has not yet."""
    return marks_label(runtime_for(engine).settings)


def apply_maximize_settings(
    engine: aw.AutoSwitchEngine, settings: MaximizeSettings
) -> None:
    """Session override (TUI): retarget the policy and poll cadence now."""
    rt = runtime_for(engine)
    rt.settings = settings
    _apply_poll_inputs(engine, settings)


def rejected_keys(section: object, loaded: MaximizeSettings) -> list[str]:
    """Numeric ``maximize.*`` keys whose raw value the lenient loader had to
    change (wrong type, or clamped into range). Whole-number truncation of an
    int key is the loader's normal reading, not a rejection."""
    if not isinstance(section, Mapping):
        return []
    bad: list[str] = []
    for json_key, attr in _NUMERIC_KEYS:
        if json_key not in section:
            continue
        value = section[json_key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            bad.append(f"maximize.{json_key}")
            continue
        current = getattr(loaded, attr)
        expected = int(value) if isinstance(current, int) else float(value)
        if expected != current:
            bad.append(f"maximize.{json_key}")
    return bad


def reload_if_changed(engine: aw.AutoSwitchEngine, rt: MaximizeRuntime) -> bool:
    """Hot reload on a settings.json mtime change (spec §8.1).

    Returns True when new maximize settings were applied. An unreadable
    file, a repaired soft > hard pair, an out-of-range or mistyped numeric
    ``maximize`` value, or flags that now contradict the file keep the
    previous maximize settings. Prime settings always take the fresh load
    (its loader fails toward "off"). One ``ConfigWarningEvent`` lists every
    problem.
    """
    mtime = _settings_mtime(engine)
    if mtime == rt.settings_mtime:
        return False
    rt.settings_mtime = mtime
    raw = _raw_settings(engine)
    if raw is None:
        engine._emit(aw.ConfigWarningEvent(
            message="settings.json is unreadable; keeping the previous maximize settings"
        ))
        return False
    file_values, effective, prime, problems, maximize_ok = _load(engine, rt.cli_args)
    problems = [p.removesuffix("; using defaults for both") for p in problems]
    if maximize_ok:
        bad = rejected_keys(raw.get("maximize"), file_values)
        if bad:
            problems.append(f"invalid {', '.join(bad)}")
            maximize_ok = False
    if prime != rt.prime_settings:
        rt.prime_settings = prime
        rt.primer = _build_primer(engine, prime)
    if problems:
        tail = "" if maximize_ok else "; keeping the previous maximize settings"
        engine._emit(aw.ConfigWarningEvent(
            message="settings.json: " + "; ".join(problems) + tail
        ))
    if not maximize_ok:
        return False
    rt.settings = effective
    _apply_poll_inputs(engine, effective)
    return True


# -- snapshot inputs -------------------------------------------------------------


def _records(engine: aw.AutoSwitchEngine, current: str) -> dict[str, dict]:
    """``sequence.json`` account records in sequence order (disabled included)."""
    data = engine.switcher._get_sequence_data() or {}
    accounts = data.get("accounts") or {}
    out: dict[str, dict] = {}
    for num in data.get("sequence") or []:
        record = accounts.get(str(num))
        if isinstance(record, dict):
            out[str(num)] = record
    if current not in out and isinstance(accounts.get(current), dict):
        out[current] = accounts[current]
    return out


def _unavailable(
    engine: aw.AutoSwitchEngine, records: Mapping[str, Mapping], current: str
) -> set[str]:
    """Enabled slots without usable stored backups (cannot be activated)."""
    switchable = set(engine.switcher.switchable_account_numbers())
    return {
        n
        for n, r in records.items()
        if n != current and n not in switchable and not r.get("disabled")
    }


def _rate_limit_tiers(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    records: Mapping[str, Mapping],
    now: float,
) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for number, record in records.items():
        email = str(record.get("email") or "")
        cached = rt.tier_cache.get(number)
        if cached is not None and cached[0] == email and now - cached[2] < TIER_CACHE_TTL_S:
            out[number] = cached[1]
            continue
        tier: str | None = None
        if record.get("kind") != "api_key":
            try:
                tier = rate_limit_tier_from_credentials(
                    engine.switcher.read_account_credentials(number, email)
                )
            except Exception:
                _logger.debug("rateLimitTier unreadable for account %s", number)
        rt.tier_cache[number] = (email, tier, now)
        out[number] = tier
    return out


def read_login_deadlines(
    switcher, records: Mapping[str, Mapping], current: str | None
) -> dict[str, float]:
    """Each slot's login deadline (epoch s): the live login for the active
    slot, the stored backup for the rest. Slots without one are absent.
    Only the deadline leaves this function — never a token."""
    out: dict[str, float] = {}
    for number, record in records.items():
        if record.get("kind") == "api_key":
            continue
        try:
            if number == current:
                creds = switcher._read_credentials()
            else:
                creds = switcher.read_account_credentials(
                    number, str(record.get("email") or "")
                )
        except Exception:
            _logger.debug("login deadline unreadable for account %s", number)
            continue
        deadline_ms = oauth.login_expires_at_ms(creds or "")
        if deadline_ms is not None:
            out[number] = deadline_ms / 1000.0
    return out


def _login_deadlines(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    records: Mapping[str, Mapping],
    current: str,
    now: float,
) -> dict[str, float]:
    """:func:`read_login_deadlines`, re-read at most every
    :data:`LOGIN_DEADLINE_TTL_S` (Keychain reads on macOS)."""
    if (
        rt.login_deadlines_at is not None
        and 0 <= now - rt.login_deadlines_at < LOGIN_DEADLINE_TTL_S
    ):
        return rt.login_deadlines
    out = read_login_deadlines(engine.switcher, records, current)
    rt.login_deadlines, rt.login_deadlines_at = out, now
    return out


def _warn_login_expiry(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    deadlines: Mapping[str, float],
    now: float,
) -> None:
    """One ``ConfigWarningEvent`` per account per day from a week before its
    login deadline: a parked slot has no Claude Code session to warn in."""
    for number, deadline in deadlines.items():
        if deadline - now >= LOGIN_WARN_S:
            continue
        last = rt.login_warned.get(number)
        if last is not None and 0 <= now - last < LOGIN_WARN_EVERY_S:
            continue
        rt.login_warned[number] = now
        note = oauth.login_expiry_note_ms(deadline * 1000.0, int(now * 1000))
        then = "" if now >= deadline else "before then, "
        engine._emit(aw.ConfigWarningEvent(
            message=f"Account-{number} {note} — {then}{oauth.relogin_fix(number)}"
        ))


def _notify_tick(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    records: Mapping[str, Mapping],
    usage: Mapping[str, dict | str | None],
    state: Mapping,
    deadlines: Mapping[str, float],
    now: float,
) -> None:
    """This tick's desktop notifications about logins and priming
    (maximize/notify.py); its events reach the notifier through ``_emit``.
    Never on dry runs; never raises."""
    tap = getattr(engine, NOTIFY_ATTR, None)
    if engine.dry_run or not isinstance(tap, notify.EngineNotifier):
        return
    prime_note: str | None = None
    if rt.prime_settings.enabled:
        try:
            from claude_swap.maximize.prime_verify import paused_state

            prime_note, auto = paused_state(
                engine.switcher.backup_dir, auto_verify=rt.prime_settings.auto_verify
            )
            if auto:
                # The primer verifies at the end of this tick (or retries
                # later) and notifies the outcome itself.
                prime_note = None
        except Exception:
            prime_note = None
    tap.tick(
        records=records, usage=usage, state=state, deadlines=deadlines,
        prime_note=prime_note, now=now,
    )


def _stored_samples(source: Mapping, current: str) -> list[Sample]:
    raw = source.get(SAMPLES_KEY)
    if not isinstance(raw, Mapping) or str(raw.get("account")) != current:
        return []
    out: list[Sample] = []
    for item in raw.get("samples") or ():
        if (
            isinstance(item, (list, tuple))
            and len(item) == 3
            and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) for x in item
            )
        ):
            out.append(Sample(float(item[0]), float(item[1]), float(item[2])))
    return out


def _fresh_sample(entry, value: object, now: float) -> Sample | None:
    """A sample from the active account's reading while it is fresh."""
    fetched_at = getattr(entry, "fetched_at", None)
    if fetched_at is None or now - fetched_at > idle.FRESH_SAMPLE_S:
        return None
    pct5, _, pct7, _ = usage_windows(value, fetched_at)
    if pct5 is None or pct7 is None:
        return None
    return Sample(ts=float(fetched_at), pct5=pct5, pct7=pct7)


def _active_changed_at(source: Mapping, current: str, now: float) -> float | None:
    """When the active account last changed, from the samples record.

    A record naming another account means the active account changed since
    the last tick (a manual login, or another surface switched): ``now`` is
    the first moment we know of it. No record at all is a first run, not a
    change (None).
    """
    raw = source.get(SAMPLES_KEY)
    if not isinstance(raw, Mapping):
        return None
    if str(raw.get("account")) != current:
        return now
    ts = raw.get(CHANGED_KEY)
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return min(float(ts), now)


def _update_samples(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    state: dict,
    current: str,
    entry,
    value: object,
    now: float,
) -> tuple[tuple[Sample, ...], float | None]:
    """Append this tick's fresh reading; persist ``maximizeSamples``.

    Returns ``(samples, active_changed_at)``. The buffer belongs to one
    account: another active account (a switch, a manual login) starts it
    empty and stamps the change time, which restarts the rebalance cooldown.
    """
    source = rt.dry_samples if engine.dry_run else state
    samples = _stored_samples(source, current)
    changed_at = _active_changed_at(source, current, now)
    new = _fresh_sample(entry, value, now)
    if new is not None and (not samples or new.ts > samples[-1].ts):
        samples.append(new)
    trimmed = idle.trim_samples(samples, now)
    record: dict[str, Any] = {
        "account": current,
        "samples": [[x.ts, x.pct5, x.pct7] for x in trimmed],
    }
    if changed_at is not None:
        record[CHANGED_KEY] = changed_at
    if engine.dry_run:
        rt.dry_samples = {SAMPLES_KEY: record}
    elif state.get(SAMPLES_KEY) != record:
        engine._mutate_state(lambda s: s.__setitem__(SAMPLES_KEY, record))
    return trimmed, changed_at


def _history_inputs(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    entries: Mapping,
    usage: Mapping[str, dict | str | None],
    current: str,
    samples: tuple[Sample, ...],
    now: float,
) -> tuple[Forecast | None, dict[str, float]]:
    """Record this tick in the usage history; ``(forecast, rates7)`` for the
    Snapshot.

    Only readings the tick already has are recorded (no poll, no refresh):
    usage points while ``preempt`` is on, slot observations while
    ``learnIdlePattern`` is. Dry runs record in memory only. History is a
    planning aid: any failure here is logged and decides as if it had none.
    """
    s = rt.settings
    if not (s.preempt or s.learn_idle_pattern):
        return None, {}
    try:
        root = engine.switcher.backup_dir
        if rt.history is None or rt.history.root != root:
            rt.history = history.Recorder(root)
        readings: dict[str, tuple[float, float, float]] = {}
        for number, value in usage.items():
            fetched_at = getattr(entries.get(number), "fetched_at", None)
            if fetched_at is None:
                continue
            pct5, _, pct7, _ = usage_windows(value, fetched_at)
            if pct5 is not None and pct7 is not None:
                readings[str(number)] = (float(fetched_at), pct5, pct7)
        rt.history.observe(
            now, current, readings, samples,
            points=s.preempt, slots=s.learn_idle_pattern, write=not engine.dry_run,
        )
        kept = rt.history.history
        forecast = history.forecast(kept.slots, now) if s.learn_idle_pattern else None
        rates = history.burn_rates(kept.points, now) if s.preempt else {}
        return forecast, rates
    except Exception as e:  # a planning aid must never break a tick
        _logger.debug("usage history unavailable: %s", type(e).__name__)
        return None, {}


def _recent_429(entry, now: float) -> bool:
    """Whether the active account's usage entry 429'd recently enough to
    keep the post-429 cadence (``_pull_active_poll`` skips it then): a
    reset-aware wait past the hard cap cannot count on 60 s polls."""
    check = getattr(entry, "recent_429", None)
    try:
        return bool(check(now)) if callable(check) else False
    except Exception:
        return False


def _reset_samples(engine: aw.AutoSwitchEngine, number: str) -> None:
    record = {"account": number, "samples": [], CHANGED_KEY: engine.clock()}
    engine._mutate_state(lambda s: s.__setitem__(SAMPLES_KEY, record))


# -- the learned ride ---------------------------------------------------------------


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _new_sample(
    before: list[Sample], samples: tuple[Sample, ...]
) -> tuple[Sample | None, float | None]:
    """``(sample, previous ts)``: the reading this tick added to the active
    account's samples, and when the one before it was read (None when it
    is the first since the account became active)."""
    if not samples or (before and samples[-1].ts <= before[-1].ts):
        return None, None
    return samples[-1], (before[-1].ts if before else None)


def _armed_windows(raw: object) -> dict:
    """One account's armed windows, leniently: ``{window: {"at", "pointS",
    "reset"}}``."""
    out: dict = {}
    for w in learned_ride.WINDOWS:
        item = raw.get(w) if isinstance(raw, Mapping) else None
        at = _finite(item.get("at")) if isinstance(item, Mapping) else None
        if at is None:
            continue
        point = _finite(item.get("pointS"))
        out[w] = {
            "at": at,
            "pointS": point if point and point > 0 else None,
            "reset": _finite(item.get("reset")),
        }
    return out


def _ride_record(raw: object, current: str) -> dict:
    """The ``maximizeRide`` record, leniently: ``{"account": current,
    "riding": [...], "accounts": {slot: {window: {...}}}}``.

    Arm times are kept per account, so a switch away mid-ride and back to
    the same window at its mark does not start the ride over (each
    window's entry ends with that window's reset, :func:`_ride_track`).
    ``riding`` belongs to the account the record names: another active
    account starts it empty (a switch ends the ride under way)."""
    out: dict = {"account": current, "riding": [], "accounts": {}}
    if not isinstance(raw, Mapping):
        return out
    accounts = raw.get("accounts")
    for number, windows in (accounts.items() if isinstance(accounts, Mapping) else ()):
        parsed = _armed_windows(windows)
        if parsed:
            out["accounts"][str(number)] = parsed
    riding = raw.get("riding")
    mine = out["accounts"].get(current, {})
    if str(raw.get("account")) == current and isinstance(riding, list):
        out["riding"] = [w for w in learned_ride.WINDOWS if w in riding and w in mine]
    return out


def _disarm_parked(
    record: dict, usage: Mapping[str, object], s: MaximizeSettings, current: str, now: float
) -> None:
    """Drop each parked account's armed window once that window reset: its
    recorded reset time passed, or it reads under its mark (or at 100%)."""
    for number in list(record["accounts"]):
        if number == current:
            continue
        windows = record["accounts"][number]
        pct5, _, pct7, _ = usage_windows(usage.get(number), now)
        for w, pct, cap in (("5h", pct5, s.hard_5h), ("7d", pct7, s.hard_7d)):
            item = windows.get(w)
            if item is None:
                continue
            reset = item.get("reset")
            if (reset is not None and now >= reset) or (
                pct is not None and not cap <= pct < policy.LIMIT_PCT
            ):
                del windows[w]
        if not windows:
            del record["accounts"][number]


def _velocity_point_s(
    samples: tuple[Sample, ...], s: MaximizeSettings, now: float, window: str
) -> float | None:
    """Seconds per point at the recent velocity (fresh samples only)."""
    if not samples or now - samples[-1].ts > s.idle_window_min * 60.0:
        return None
    v5, v7 = idle.velocity(samples, s)
    rate = v5 if window == "5h" else v7
    return 60.0 / rate if rate is not None and rate > 0 else None


@dataclass
class RideTick:
    """This tick's learned-ride bookkeeping, written by :func:`_ride_commit`."""

    steps: dict
    record: dict
    hits: tuple[str, ...]
    stored_steps: object
    stored_record: object
    q: dict[str, float]

    @property
    def _mine(self) -> dict:
        return self.record["accounts"].get(self.record["account"], {})

    @property
    def armed_at(self) -> dict[str, float]:
        return {w: item["at"] for w, item in self._mine.items()}

    @property
    def point_s(self) -> dict[str, float]:
        return {
            w: item["pointS"] for w, item in self._mine.items() if item["pointS"] is not None
        }


def _ride_track(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    state: Mapping,
    current: str,
    entry,
    usage: Mapping[str, object],
    samples: tuple[Sample, ...],
    new: Sample | None,
    prev_ts: float | None,
    now: float,
) -> RideTick:
    """Record this tick's whole-point steps, arm or disarm each window that
    may ride, and note a hit: a window that reads 100% while the last
    acted-on decision rode it. Writes nothing (:func:`_ride_commit` does).

    A window is armed when it first reads its hard mark under 100% (at the
    reading's fetch time) with its T1 frozen then: the shorter of the timed
    steps' and the recent velocity's, else unknown until one is known. Under the mark again
    (a reset) or at 100% disarms it."""
    s = rt.settings
    source = rt.dry_ride if engine.dry_run else state
    stored_steps = source.get(learned_ride.STEPS_KEY)
    stored_record = source.get(RIDE_KEY)
    steps = (
        {str(k): v for k, v in stored_steps.items()}
        if isinstance(stored_steps, Mapping) else {}
    )
    if new is not None:
        steps = learned_ride.observe(
            steps, current, new.pct5, new.pct7, new.ts, prev_ts,
            quiet_s=s.idle_window_min * 60.0,
        )
    record = _ride_record(stored_record, current)
    _disarm_parked(record, usage, s, current, now)
    armed = record["accounts"].setdefault(current, {})
    rides = policy.ride_windows(s)
    pct5, reset5, pct7, reset7 = usage_windows(usage.get(current), now)
    fetched_at = getattr(entry, "fetched_at", None)
    hits: list[str] = []
    for w, pct, cap, reset in (
        ("5h", pct5, s.hard_5h, reset5), ("7d", pct7, s.hard_7d, reset7)
    ):
        if pct is None:
            continue  # unreadable this tick: keep what we had
        if pct >= policy.LIMIT_PCT:
            if w in record["riding"]:
                hits.append(w)
            armed.pop(w, None)
        elif w in rides and cap >= policy.RIDE_FLOOR_PCT and pct >= cap:
            item = armed.get(w)
            if item is None:
                read_at = _finite(fetched_at)
                read_at = read_at if read_at is not None and read_at <= now else now
                # From the reading before this one (or a slow poll back):
                # the window may have crossed its mark right after it.
                previous = max((x.ts for x in samples if x.ts < read_at), default=None)
                at = learned_ride.arm_time(read_at, previous)
                item = armed[w] = {"at": at, "pointS": None, "reset": None}
            # When this window resets (it drops the arm time even while
            # the account is parked and unread).
            item["reset"] = reset if reset is not None and reset > now else None
            # An arm time ahead of now (the clock stepped back) is pulled to
            # now and kept there: clamped only when deciding, the ride
            # would count from "now" on every tick and never end.
            item["at"] = min(item["at"], now)
            if item["pointS"] is None:
                # The shorter of the measured steps and the recent velocity:
                # a T1 too long rides into 100%.
                known = [
                    x for x in (
                        learned_ride.point_seconds(steps, current, w, now),
                        _velocity_point_s(samples, s, now, w),
                    ) if x is not None
                ]
                item["pointS"] = min(known) if known else None
        else:
            armed.pop(w, None)
    record["riding"] = [w for w in record["riding"] if w in armed]
    if not armed:
        del record["accounts"][current]
    return RideTick(
        steps=steps,
        record=record,
        hits=tuple(hits),
        stored_steps=stored_steps,
        stored_record=stored_record,
        q=learned_ride.q_values(source.get(learned_ride.LEARN_KEY)),
    )


def _ride_commit(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    tick: RideTick,
    now: float,
    *,
    riding: tuple[str, ...] | None = None,
    ok: tuple[str, ...] = (),
) -> None:
    """Persist this tick's ride records and learn from how a ride ended.

    ``riding``: the windows the decision the engine acted on rides (None
    keeps the previous ones: nothing was acted on this tick). ``ok``: the
    windows whose ride the engine ended with its hard switch, before 100%.
    A hit (:func:`_ride_track`) halves q, an ok raises it, both under the
    state lock. A dry run keeps its records in memory and learns nothing.
    Never raises: bookkeeping must not break a tick."""
    record = dict(tick.record)
    if riding is not None:
        record["riding"] = [w for w in learned_ride.WINDOWS if w in riding]
    if engine.dry_run:
        rt.dry_ride = {
            **rt.dry_ride, learned_ride.STEPS_KEY: tick.steps, RIDE_KEY: record,
        }
        return
    outcomes = [(w, "hit") for w in tick.hits] + [(w, "ok") for w in ok]
    if tick.steps == tick.stored_steps and record == tick.stored_record and not outcomes:
        return

    def mutate(st: dict) -> None:
        st[learned_ride.STEPS_KEY] = tick.steps
        st[RIDE_KEY] = record
        if outcomes:
            data: object = st.get(learned_ride.LEARN_KEY)
            for w, outcome in outcomes:
                data = learned_ride.learn(data, w, outcome, now)  # type: ignore[arg-type]
            st[learned_ride.LEARN_KEY] = data

    try:
        engine._mutate_state(mutate)
    except Exception as e:
        _logger.debug("could not record the learned ride: %s", type(e).__name__)
        return
    for w, outcome in outcomes:
        _logger.info(
            "learned ride: %s %s", w,
            "switched before 100%" if outcome == "ok" else "reached 100% while riding",
        )


# -- decision → engine ------------------------------------------------------------


def _decision_event(
    snap: Snapshot, decision: Decision, dry_run: bool
) -> aw.MaximizeDecisionEvent:
    if isinstance(decision, Switch):
        name, trigger, pending = "switch", decision.trigger, False
    elif isinstance(decision, Hold):
        name, trigger, pending = "hold", None, decision.pending
    elif isinstance(decision, Exhausted):
        name, trigger, pending = "exhausted", None, False
    else:
        name, trigger, pending = "indeterminate", None, False
    scores: dict[str, float] = {}
    for v in snap.accounts:
        value = score(v, snap.now)
        if math.isfinite(value):
            scores[v.number] = round(value, 3)
    return aw.MaximizeDecisionEvent(
        active=snap.active,
        decision=name,
        trigger=trigger,
        reason=decision.reason,
        scores=scores,
        pending=pending,
        rows=decision_rows(snap),
        dry_run=dry_run,
        # The hold's own code, as published (``_publish_decision``): a TUI
        # hosting this engine words it like a viewer reading the state file.
        code=decision.code if isinstance(decision, Hold) else None,
        ride_until=decision.ride_until if isinstance(decision, Hold) else None,
    )


def _decision_fields(decision: Decision) -> tuple[str, str | None, bool]:
    if isinstance(decision, Switch):
        return "switch", decision.trigger, False
    if isinstance(decision, Hold):
        return "hold", None, decision.pending
    if isinstance(decision, Exhausted):
        return "exhausted", None, False
    return "indeterminate", None, False


def _publish_decision(
    engine: aw.AutoSwitchEngine,
    snap: Snapshot,
    decision: Decision,
    state: Mapping,
    tiers: Mapping[str, str | None],
    *,
    shared: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Write this tick's decision to the state file for TUI viewers.

    Slot numbers and the policy's own reason only — no emails, no raw
    ``rateLimitTier`` strings; a hold with its own code (``reset-wait``,
    ``preempt``, ``rebalance-deferred``, ``hold``) adds ``code`` so
    ``cc-swap why`` can name it, and ``shared`` the slots set aside because
    their login is also held elsewhere. Rewritten when the decision changes, or when
    the stored one is :data:`PUBLISH_REFRESH_S` old (the TUI's freshness
    clock); never on dry runs, which write nothing."""
    if engine.dry_run:
        return
    name, trigger, pending = _decision_fields(decision)
    target: str | None = decision.target if isinstance(decision, Switch) else None
    if pending:
        landing = policy.landing_candidates(snap)
        target = landing[0].number if landing else None
    record = {
        "at": snap.now,
        "pid": os.getpid(),
        "active": snap.active,
        "decision": name,
        "trigger": trigger,
        "target": target,
        "reason": decision.reason,
        "pending": pending,
        "plans": {num: plan_label(tier) for num, tier in tiers.items()},
    }
    if isinstance(decision, Hold) and decision.code is not None:
        record["code"] = decision.code
    if shared:
        record["shared"] = sorted(shared, key=lambda n: (len(n), n))
    if isinstance(decision, Hold) and decision.ride_until is not None:
        # A ride's switch time, so a viewer counts its minutes down live.
        record["rideUntil"] = decision.ride_until
    previous = state.get(DECISION_KEY)
    if isinstance(previous, Mapping):
        at = previous.get("at")
        same = all(previous.get(k) == record[k] for k in record if k not in ("at", "pid"))
        if (
            same
            and isinstance(at, (int, float))
            and not isinstance(at, bool)
            and 0 <= snap.now - at < PUBLISH_REFRESH_S
        ):
            return
    try:
        engine._mutate_state(lambda s: s.__setitem__(DECISION_KEY, record))
    except Exception as e:  # a display aid must never break a tick
        _logger.debug("could not publish the maximize decision: %s", type(e).__name__)


def _without(snap: Snapshot, failed: set[str]) -> Snapshot:
    return replace(
        snap,
        accounts=tuple(
            replace(v, quarantined=True) if v.number in failed else v
            for v in snap.accounts
        ),
    )


def _switch(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    snap: Snapshot,
    decision: Switch,
    usage: Mapping[str, dict | str | None],
    headroom: Mapping[str, float | None],
    current: str,
    entry,
) -> tuple[aw.TickOutcome, str | None]:
    """Freshen + perform, re-deciding without each target that fails.

    A re-decision that holds (nothing else worth moving to) is an ordinary
    hold — the policy is content to stay — unless the failure was systemic,
    which keeps upstream's error so its cause gets named.
    """
    left = (
        headroom.get(current),
        aw._binding_recovery_ts(usage.get(current), engine._models, snap.now),
    )
    failed: set[str] = set()
    set_aside: list[str] = []
    transient = False
    systemic = ""
    pick: Decision = decision
    while isinstance(pick, Switch):
        number = pick.target
        email = engine.switcher.account_email(number)
        if engine.dry_run:
            # Dry-run stops at the decision: freshening is a mutation.
            return engine._perform(number, email, pick.trigger, left), None
        status = engine._freshen_target(number, email)
        if status == "ok":
            try:
                with ledger.switch_context(reason=pick.reason):
                    outcome = engine._perform(number, email, pick.trigger, left)
            except aw.TargetLoginDead:
                # switch_to refused the target (its login is dead): set it
                # aside and decide again, as for a target that failed to freshen.
                status = "login-dead"
            else:
                if outcome is aw.TickOutcome.SWITCHED:
                    _reset_samples(engine, number)
                    return outcome, number
                return outcome, None
        if status in ("identity-conflict", "invalid_grant"):
            engine._quarantine(number, email, status)
        elif status == "transient":
            transient = True
        elif status in aw._SYSTEMIC_STATUSES:
            if not systemic or aw._SYSTEMIC_STATUSES.index(
                status
            ) < aw._SYSTEMIC_STATUSES.index(systemic):
                systemic = status
        # "skip-live-session" and every failure: set aside, decide again.
        failed.add(number)
        set_aside.append(f"#{number} ({status})")
        pick = policy.decide(_without(snap, failed))
    if isinstance(pick, Hold) and not systemic:
        held = replace(pick, reason=f"{pick.reason}; set aside {', '.join(set_aside)}")
        return _hold(engine, rt, held, current, entry, snap.now), None
    if systemic or transient:
        engine._emit(aw.ErrorEvent(
            message=(
                "could not freshen: " + aw._SYSTEMIC_MESSAGES[systemic]
                if systemic
                else "could not freshen any candidate (network?)"
            ),
            transient=True,
        ))
        return aw.TickOutcome.ERROR, None
    engine._emit(aw.NoSwitchEvent(reason="no-viable-target", detail=decision.reason))
    return aw.TickOutcome.BLOCKED, None


def _pull_active_poll(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    current: str,
    entry,
    now: float,
    *,
    urgent: bool = False,
) -> None:
    """Poll the active account sooner while a switch waits.

    A pending soft switch polls every ``pending_poll_s``, never sooner than
    ``fetchedAt + poll_policy.MIN_INTERVAL_S`` whatever the settings say (a
    session override skips the loader's clamp): the per-account poll budget
    is shared by every machine. A reset-aware wait (``urgent``) polls at the
    planner's urgent cadence, ``poll_policy.URGENT_INTERVAL_S``, so a climb
    to 100% is caught quickly (``_hold`` bounds how long). Only ever pulls
    the next poll earlier. A token that 429'd recently keeps the planner's
    post-429 cadence (spec §5.6), and the collector still enforces any live
    backoff.
    """
    fetched_at = getattr(entry, "fetched_at", None)
    if fetched_at is None or entry.recent_429(now):
        return
    if urgent:
        interval = poll_policy.URGENT_INTERVAL_S
    else:
        interval = max(float(rt.settings.pending_poll_s), poll_policy.MIN_INTERVAL_S)
    deadline = max(now, fetched_at + interval)
    if entry.next_poll_at is not None and entry.next_poll_at <= deadline:
        return
    identity = engine.switcher.account_identity(current)
    if not identity.get("email"):
        return
    try:
        engine.switcher._usage_store.set_poll_plan(
            {current: (deadline, entry.poll_interval_s)},
            {current: (identity["email"], identity["organizationUuid"])},
        )
    except Exception as e:
        _logger.debug("pending poll re-plan failed for account %s: %s", current, type(e).__name__)


def _hold(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    decision: Hold,
    current: str,
    entry,
    now: float,
) -> aw.TickOutcome:
    until = decision.reset_wait_until
    if decision.pending:
        _pull_active_poll(engine, rt, current, entry, now)
    elif until is not None and until - now <= RESET_WAIT_URGENT_S:
        _pull_active_poll(engine, rt, current, entry, now, urgent=True)
    elif decision.code == "ride" and decision.ride_until is not None:
        # The urgent cadence over the ride's last RESET_WAIT_URGENT_S (the
        # planner's bound on an urgent episode), pendingPollS before that.
        _pull_active_poll(
            engine, rt, current, entry, now,
            urgent=decision.ride_until - now <= RESET_WAIT_URGENT_S,
        )
    engine._emit(_hold_event(decision))
    return aw.TickOutcome.NO_ACTION


def _hold_event(decision: Hold) -> aw.NoSwitchEvent:
    """A hold's ``NoSwitchEvent``: ``maximize-pending``, the hold's own code,
    else ``maximize-hold``. One literal per call — tests/maximize/test_why.py
    reads the codes off these calls."""
    detail = decision.reason
    if decision.pending:
        return aw.NoSwitchEvent(reason="maximize-pending", detail=detail)
    if decision.code == "reset-wait":
        return aw.NoSwitchEvent(reason="reset-wait", detail=detail)
    if decision.code == "preempt":
        return aw.NoSwitchEvent(reason="preempt", detail=detail)
    if decision.code == "rebalance-deferred":
        return aw.NoSwitchEvent(reason="rebalance-deferred", detail=detail)
    if decision.code == "hold":
        return aw.NoSwitchEvent(reason="hold", detail=detail)
    if decision.code == "hard-stay":
        return aw.NoSwitchEvent(reason="hard-stay", detail=detail)
    if decision.code == "ride":
        return aw.NoSwitchEvent(reason="ride", detail=detail)
    return aw.NoSwitchEvent(reason="maximize-hold", detail=detail)


def _blocking_resets(v: AccountView) -> list[float | None]:
    """Resets of the windows at the limit. ``Exhausted`` comes from the
    at-limit trigger only after its last fallback found no account under
    100% on both windows, so 100% (not the hard caps) is what blocks."""
    out: list[float | None] = []
    if v.pct5 is not None and v.pct5 >= policy.LIMIT_PCT:
        out.append(v.reset5)
    if v.pct7 is not None and v.pct7 >= policy.LIMIT_PCT:
        out.append(v.reset7)
    return out


def _earliest_usable(snap: Snapshot, views: list[AccountView]) -> float | None:
    """Earliest moment any of ``views`` drops under the limit on both
    windows, or None when some blocking window has no future reset (not
    provable)."""
    earliest: float | None = None
    for v in views:
        resets = _blocking_resets(v)
        if not resets:
            return None
        if any(r is None or r <= snap.now for r in resets):
            return None
        usable_at = max(r for r in resets if r is not None)
        if earliest is None or usable_at < earliest:
            earliest = usable_at
    return earliest


def _exhausted(
    engine: aw.AutoSwitchEngine, snap: Snapshot, decision: Exhausted, current: str
) -> aw.TickOutcome:
    peers = [
        v
        for v in snap.accounts
        if v.number != current
        and v.tier != "excluded"
        and not v.quarantined
        and not v.api_key
    ]
    if not peers:
        engine._blocked_wait_long = True
        engine._emit(aw.NoSwitchEvent(reason="no-candidates", detail=decision.reason))
        return aw.TickOutcome.BLOCKED
    active = snap.view(current)
    active_blocked = active is not None and bool(_blocking_resets(active))
    if not active_blocked or any(v.pct5 is None or v.pct7 is None for v in peers):
        # An active account with quota left, or a peer we cannot read this
        # tick: either can change any moment, so keep the normal cadence
        # (upstream's rule).
        engine._emit(aw.NoSwitchEvent(
            reason="no-qualifying-candidate", detail=decision.reason
        ))
        return aw.TickOutcome.BLOCKED
    engine._blocked_wait_long = True
    earliest = _earliest_usable(snap, [active, *peers])
    if earliest is not None:
        engine._sleep_until_ts = earliest + aw.RESET_SLACK_S
    engine._emit(aw.AllExhaustedEvent(
        earliest_reset_at=(
            datetime.fromtimestamp(earliest, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if earliest is not None
            else None
        )
    ))
    return aw.TickOutcome.BLOCKED


def _run_primer(
    engine: aw.AutoSwitchEngine, rt: MaximizeRuntime, snap: Snapshot
) -> None:
    """End-of-tick priming (spec §4.1). Never on dry runs."""
    if engine.dry_run or rt.primer is None or not rt.prime_settings.enabled:
        return
    try:
        events = rt.primer.run_due(snap)
    except Exception as e:
        engine._emit(aw.ErrorEvent(
            message=f"prime: {type(e).__name__}", transient=True
        ))
        return
    for event in events or ():
        engine._emit(event)


def _account_hold(
    engine: aw.AutoSwitchEngine, state: Mapping, current: str, now: float
) -> float | None:
    """When the account hold (maximize/hold.py) on ``current`` ends — the
    Snapshot's ``hold_until`` — or None.

    A marker that no longer applies is cleared here (never on dry runs):
    one past its end, or one on a slot that is not the active account any
    more (a manual switch, an external ``/login``, a forced switch all end
    a hold). A hold on the active account is never touched.

    ``current`` was read before the tick's usage fetch, which can take
    seconds: the user may have switched and held the new account
    meanwhile. So a marker on another slot is cleared only once the live
    login, read again now, is not its slot either, and never when it was
    written after this tick began (the next tick decides about it)."""
    root = engine.switcher.backup_dir
    try:
        found = account_hold.marker(root, state)
    except Exception:  # a convenience must never break a tick
        return None
    if found is None:
        return None
    pinned = account_hold.holding(found, current, now)
    # The switch ledger may have seen the account leave and come back
    # between two ticks: that ends the hold too.
    moved = pinned is not None and account_hold.moved_away(root, pinned)
    if pinned is not None and not moved:
        return pinned.until
    if engine.dry_run:
        return None
    started = getattr(engine, "_tick_started_at", None)
    if found.since is not None and started is not None and found.since > started:
        return None
    live = current
    if not moved and account_hold.current(found, now) is not None:
        try:
            live = engine.switcher.current_account_number() or current
        except Exception:
            live = current
        if live == found.slot:
            return None
    try:
        account_hold.clear_hold(root)
    except Exception as e:
        _logger.debug("could not clear the account hold: %s", type(e).__name__)
        return None
    if moved:
        engine._emit(aw.ConfigWarningEvent(
            message=f"hold on #{found.slot} lifted: the active account changed since it was set"
        ))
    elif account_hold.current(found, now) is not None:
        engine._emit(aw.ConfigWarningEvent(
            message=f"hold on #{found.slot} lifted: #{live} is the active account now"
        ))
    return None


def _end_hold_after_switch(
    engine: aw.AutoSwitchEngine, held_until: float | None, current: str, landed: str
) -> None:
    """A switch the engine made (hard, at-limit, …) ends the hold on the
    account it left — that hold only: a marker on another slot, or one
    written after this tick began, is the user's newer word."""
    if held_until is None or engine.dry_run:
        return
    root = engine.switcher.backup_dir
    try:
        found = account_hold.marker(root, account_hold.read_state(root))
    except Exception:
        return
    started = getattr(engine, "_tick_started_at", None)
    if found is None or found.slot != current or (
        found.since is not None and started is not None and found.since > started
    ):
        return
    try:
        account_hold.clear_hold(root)
    except Exception as e:
        _logger.debug("could not clear the account hold: %s", type(e).__name__)
        return
    engine._emit(aw.ConfigWarningEvent(
        message=f"hold on #{current} lifted: the engine switched to #{landed}"
    ))


def _note_drift(engine: aw.AutoSwitchEngine, rt: MaximizeRuntime, current: str) -> None:
    """Record a live login that changed with no switch in the ledger (a
    ``/login`` inside a Claude Code session). Only once the mismatch shows
    on two ticks in a row: a switch another process is making right now
    lands in the ledger a moment after the live login changes."""
    if engine.dry_run or not ledger.installed():
        return
    root = engine.switcher.backup_dir
    try:
        previous = ledger.drift(root, current)
        if previous is None:
            rt.drift_seen = None
            return
        seen = (previous.get("ts"), current)
        if rt.drift_seen != seen:
            rt.drift_seen = seen
            return
        rt.drift_seen = None
        entry = ledger.record_external(
            root, current, reason="the live login changed outside cc-swap"
        )
        if entry is not None:
            engine._emit(aw.ConfigWarningEvent(
                message=(
                    f"the live login changed outside cc-swap: "
                    f"#{entry.get('from') or '?'} -> #{current} "
                    "(a /login in a Claude Code session?)"
                )
            ))
    except Exception as e:  # bookkeeping must never break a tick
        _logger.debug("switch ledger drift check failed: %s", type(e).__name__)


def run_maximize_tick(
    engine: aw.AutoSwitchEngine,
    entries: Mapping,
    usage: Mapping[str, dict | str | None],
    headroom: Mapping[str, float | None],
    *,
    current: str,
    quarantined: set[str],
    state: dict,
) -> aw.TickOutcome | None:
    """One maximize tick after usage collection. ``None`` = Indeterminate:
    the caller continues into upstream's unknown-usage / failover path."""
    rt = runtime_for(engine)
    reload_if_changed(engine, rt)
    now = engine.clock()
    paused = pause.active_pause(state, now)
    if paused is not None:
        # A TUI re-login owns the live login for now: no switch, no prime,
        # and no samples (the login it shows is not a manual switch).
        until, why = paused
        engine._emit(aw.NoSwitchEvent(
            reason="maximize-paused",
            detail=f"switching paused ({why}) for {until - now:.0f}s more",
        ))
        return aw.TickOutcome.NO_ACTION
    _note_drift(engine, rt, current)
    held_until = _account_hold(engine, state, current, now)
    records = _records(engine, current)
    deadlines = _login_deadlines(engine, rt, records, current, now)
    _warn_login_expiry(engine, rt, deadlines, now)
    _notify_tick(engine, rt, records, usage, state, deadlines, now)
    before = _stored_samples(rt.dry_samples if engine.dry_run else state, current)
    samples, active_changed_at = _update_samples(
        engine, rt, state, current, entries.get(current), usage.get(current), now
    )
    new_sample, prev_ts = _new_sample(before, samples)
    ride_tick = _ride_track(
        engine, rt, state, current, entries.get(current), usage,
        samples, new_sample, prev_ts, now,
    )
    forecast, rates7 = _history_inputs(
        engine, rt, entries, usage, current, samples, now
    )
    last = state.get("lastSwitchAt")
    tiers = _rate_limit_tiers(engine, rt, records, now)
    # A login also held elsewhere (shared_login.py): a switch onto it is
    # refused, so the policy never targets it (from the entries in hand).
    shared = shared_login.shared_slots(entries, current) & set(records)
    snap = build_snapshot(
        now=now,
        active=current,
        usage=usage,
        records=records,
        quarantined=set(quarantined) | _unavailable(engine, records, current) | shared,
        api_key_accounts={n for n, r in records.items() if r.get("kind") == "api_key"},
        rate_limit_tiers=tiers,
        samples=samples,
        last_switch_at=(
            float(last)
            if isinstance(last, (int, float)) and not isinstance(last, bool)
            else None
        ),
        settings=rt.settings,
        active_changed_at=active_changed_at,
        login_deadlines=deadlines,
        forecast=forecast,
        rates7=rates7,
        active_recent_429=_recent_429(entries.get(current), now),
        hold_until=held_until,
        ride_armed_at=ride_tick.armed_at,
        ride_point_s=ride_tick.point_s,
        ride_q=ride_tick.q,
    )
    decision = policy.decide(snap)
    rt.last_snapshot, rt.last_decision = snap, decision
    engine._emit(_decision_event(snap, decision, engine.dry_run))
    _publish_decision(engine, snap, decision, state, tiers, shared=shared)
    # `cc-swap auto off`: the decision is shown and published, but nothing
    # acts on it — no switch, no failover, no prime.
    held = pause.auto_off_hold(engine, state)
    if held is not None:
        # Nothing acts on the decision: no ride is under way to learn from.
        _ride_commit(engine, rt, ride_tick, now, riding=())
        return held
    if isinstance(decision, Indeterminate):
        _ride_commit(engine, rt, ride_tick, now)
        _run_primer(engine, rt, snap)
        return None
    # Readable active usage: clear upstream's unhealthy/idle-hold counters,
    # exactly as _tick_inner does on its own readable-usage branch.
    engine._unhealthy_ticks = 0
    engine._idle_hold_since = None
    landed: str | None = None
    riding: tuple[str, ...] = ()
    ok: tuple[str, ...] = ()
    if isinstance(decision, Switch):
        outcome, landed = _switch(
            engine, rt, snap, decision, usage, headroom, current, entries.get(current)
        )
        if landed:
            _end_hold_after_switch(engine, held_until, current, landed)
            if decision.ride == "due" and not decision.ride_capped:
                ok = decision.ride_windows
    elif isinstance(decision, Hold):
        outcome = _hold(engine, rt, decision, current, entries.get(current), now)
        if decision.code == "ride":
            riding = decision.ride_windows
    else:
        outcome = _exhausted(engine, snap, decision, current)
    _ride_commit(engine, rt, ride_tick, now, riding=riding, ok=ok)
    _run_primer(engine, rt, replace(snap, active=landed) if landed else snap)
    return outcome
