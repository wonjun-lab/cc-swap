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
                      account's next poll to ``pending_poll_s``
* ``Indeterminate`` → ``None``: ``_tick_inner`` continues into its own
                      unknown-usage counting and failover
* ``Exhausted``     → ``AllExhaustedEvent`` + reset-aware sleep, or a
                      normal-cadence ``NoSwitchEvent`` when not provable

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
from claude_swap import oauth, poll_policy
from claude_swap.exceptions import ConfigError
from claude_swap.maximize import idle, pause, policy
from claude_swap.maximize.model import (
    AccountView,
    Decision,
    Exhausted,
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
    _apply_poll_inputs(engine, settings)
    if problems:
        engine._emit(aw.ConfigWarningEvent(
            message="settings.json: " + "; ".join(problems)
        ))
    return rt


def runtime_for(engine: aw.AutoSwitchEngine) -> MaximizeRuntime:
    rt = getattr(engine, RUNTIME_ATTR, None)
    return rt if isinstance(rt, MaximizeRuntime) else attach_maximize(engine)


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


def _login_deadlines(
    engine: aw.AutoSwitchEngine,
    rt: MaximizeRuntime,
    records: Mapping[str, Mapping],
    current: str,
    now: float,
) -> dict[str, float]:
    """Each slot's login deadline (epoch s): the live login for the active
    slot, the stored backup for the rest. Slots without one are absent.
    Only the deadline leaves this function — never a token."""
    if (
        rt.login_deadlines_at is not None
        and 0 <= now - rt.login_deadlines_at < LOGIN_DEADLINE_TTL_S
    ):
        return rt.login_deadlines
    out: dict[str, float] = {}
    for number, record in records.items():
        if record.get("kind") == "api_key":
            continue
        try:
            if number == current:
                creds = engine.switcher._read_credentials()
            else:
                creds = engine.switcher.read_account_credentials(
                    number, str(record.get("email") or "")
                )
        except Exception:
            _logger.debug("login deadline unreadable for account %s", number)
            continue
        deadline_ms = oauth.login_expires_at_ms(creds or "")
        if deadline_ms is not None:
            out[number] = deadline_ms / 1000.0
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
        then = "re-login needed" if now >= deadline else "re-login before then"
        engine._emit(aw.ConfigWarningEvent(
            message=(
                f"Account-{number} {note} — {then}: log in with Claude Code as "
                f"that account, then run: cc-swap add (or Fleet → r)"
            )
        ))


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


def _reset_samples(engine: aw.AutoSwitchEngine, number: str) -> None:
    record = {"account": number, "samples": [], CHANGED_KEY: engine.clock()}
    engine._mutate_state(lambda s: s.__setitem__(SAMPLES_KEY, record))


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
) -> None:
    """Write this tick's decision to the state file for TUI viewers.

    Slot numbers and the policy's own reason only — no emails, no raw
    ``rateLimitTier`` strings. Rewritten when the decision changes, or when
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
            outcome = engine._perform(number, email, pick.trigger, left)
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
) -> None:
    """Pending soft switch: poll the active account every ``pending_poll_s``.

    Only ever pulls the next poll earlier, and never sooner than
    ``fetchedAt + poll_policy.MIN_INTERVAL_S`` whatever the settings say (a
    session override skips the loader's clamp): the per-account poll budget
    is shared by every machine. A token that 429'd recently keeps the
    planner's post-429 cadence (spec §5.6), and the collector still enforces
    any live backoff.
    """
    fetched_at = getattr(entry, "fetched_at", None)
    if fetched_at is None or entry.recent_429(now):
        return
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
    if decision.pending:
        _pull_active_poll(engine, rt, current, entry, now)
    engine._emit(aw.NoSwitchEvent(
        reason="maximize-pending" if decision.pending else "maximize-hold",
        detail=decision.reason,
    ))
    return aw.TickOutcome.NO_ACTION


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
    records = _records(engine, current)
    deadlines = _login_deadlines(engine, rt, records, current, now)
    _warn_login_expiry(engine, rt, deadlines, now)
    samples, active_changed_at = _update_samples(
        engine, rt, state, current, entries.get(current), usage.get(current), now
    )
    last = state.get("lastSwitchAt")
    tiers = _rate_limit_tiers(engine, rt, records, now)
    snap = build_snapshot(
        now=now,
        active=current,
        usage=usage,
        records=records,
        quarantined=set(quarantined) | _unavailable(engine, records, current),
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
    )
    decision = policy.decide(snap)
    rt.last_snapshot, rt.last_decision = snap, decision
    engine._emit(_decision_event(snap, decision, engine.dry_run))
    _publish_decision(engine, snap, decision, state, tiers)
    if isinstance(decision, Indeterminate):
        _run_primer(engine, rt, snap)
        return None
    # Readable active usage: clear upstream's unhealthy/idle-hold counters,
    # exactly as _tick_inner does on its own readable-usage branch.
    engine._unhealthy_ticks = 0
    engine._idle_hold_since = None
    landed: str | None = None
    if isinstance(decision, Switch):
        outcome, landed = _switch(
            engine, rt, snap, decision, usage, headroom, current, entries.get(current)
        )
    elif isinstance(decision, Hold):
        outcome = _hold(engine, rt, decision, current, entries.get(current), now)
    else:
        outcome = _exhausted(engine, snap, decision, current)
    _run_primer(engine, rt, replace(snap, active=landed) if landed else snap)
    return outcome
