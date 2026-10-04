"""``cc-swap doctor`` · ``cc-swap init`` · ``cc-swap why``.

Three presentations of the same read-only checks (``maximize/doctor.py``):

* ``doctor [--json]`` — every finding, environment first, with one fix line
  each. Exit 0 all good, 1 warnings, 2 errors.
* ``init [--apply] [--json]`` — the onboarding / migration checklist as
  ``ok`` / ``FIX`` / ``TODO`` steps; exit 1 until every step is ok. Only
  ``--apply`` writes, and only the two idempotent steps (set the strategy to
  ``maximize``, install the service).
* ``why [--json] [--no-fallback]`` — the engine's last published decision
  with its reason code explained (:data:`REASONS`, mirrored by docs/reference.md
  table "Why didn't it switch?"); without a fresh one, a dry-run tick. An
  account hold on the active account (``cc-swap hold``) is named too.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from claude_swap import paths
from claude_swap.maximize import doctor as dr
from claude_swap.printer import bolded, dimmed, muted, reddened, yellowed

SCHEMA_VERSION = 1

# -- doctor ---------------------------------------------------------------------------

_TAGS = {"ok": "ok", "info": "info", "warn": "WARN", "error": "ERROR"}


def _tag(severity: str) -> str:
    text = f"{_TAGS[severity]:<5}"
    if severity == "error":
        return reddened(text)
    if severity == "warn":
        return yellowed(text)
    return dimmed(text) if severity == "info" else text


def doctor_lines(findings: list[dr.Finding], *, color: bool = True) -> list[str]:
    """The human report, one or two lines per finding (fix indented below)."""
    out: list[str] = []
    sections = (
        ("Environment", [f for f in findings if f.scope == "env"]),
        ("Accounts", [f for f in findings if f.scope != "env"]),
    )
    for title, rows in sections:
        if not rows:
            continue
        out.append(bolded(title) if color else title)
        for f in rows:
            label = f.check if f.scope in ("env", "accounts") else f"{f.scope} {f.check}"
            tag = _tag(f.severity) if color else f"{_TAGS[f.severity]:<5}"
            out.append(f"  {tag}  {label:<18} {f.detail}")
            if f.fix and f.severity != "ok":
                fix = f"fix: {f.fix}"
                out.append(" " * 28 + (muted(fix) if color else fix))
    return out


def summary_line(findings: list[dr.Finding]) -> str:
    n = dr.counts(findings)
    parts = []
    if n["error"]:
        parts.append(f"{n['error']} error{'s' if n['error'] != 1 else ''}")
    if n["warn"]:
        parts.append(f"{n['warn']} warning{'s' if n['warn'] != 1 else ''}")
    if not parts:
        parts.append("no problems found")
    return " · ".join(parts) + f" · exit {dr.exit_code(findings)}"


def _probes() -> dr.Probes:
    """Indirection so tests hand the commands fake probes."""
    return dr.Probes.system()


def doctor_payload(probes: dr.Probes, findings: list[dr.Finding]) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": probes.version,
        "platform": probes.platform,
        "exitCode": dr.exit_code(findings),
        "counts": dr.counts(findings),
        "findings": [f.to_json() for f in findings],
    }


def doctor_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cc-swap doctor",
        description=(
            "Check this machine and every stored login, read-only: Keychain, "
            "live login, upstream claude-swap, service, engine lease, login "
            "deadlines, quarantine, priming and settings. Each problem comes "
            "with one line saying what to do. Never refreshes a token, never "
            "writes anything, runs no claude but `claude --version`."
        ),
        epilog="Exit status: 0 all good, 1 warnings, 2 errors.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the findings as JSON")
    args = parser.parse_args(argv)
    probes = _probes()
    findings = dr.run_checks(probes)
    if args.json:
        print(json.dumps(doctor_payload(probes, findings), indent=2))
    else:
        root = dr._tilde(probes.backup_root, probes.home)
        print(dimmed(
            f"cc-swap doctor · cc-swap {probes.version} · {probes.platform} · {root}"
        ))
        for line in doctor_lines(findings):
            print(line)
        print()
        print(summary_line(findings))
    sys.exit(dr.exit_code(findings))


# -- init -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    key: str
    status: str  # "ok" | "FIX" | "TODO"
    title: str
    detail: str = ""
    fix: str = ""
    applicable: bool = False  # `init --apply` can do it

    def to_json(self) -> dict:
        return {
            "step": self.key,
            "status": self.status,
            "title": self.title,
            "detail": self.detail or None,
            "fix": self.fix or None,
            "applicable": self.applicable,
        }


def _worst(findings: list[dr.Finding]) -> list[dr.Finding]:
    return [f for f in findings if f.severity in ("warn", "error")]


def init_steps(probes: dr.Probes) -> list[Step]:
    """The onboarding checklist, in the order a new user walks it."""
    ctx = dr.gather(probes)
    steps: list[Step] = []

    path, problem = dr.resolve_claude(ctx)
    if path:
        steps.append(Step("claude", "ok", "Claude Code installed", dr._tilde(path, probes.home)))
    else:
        steps.append(Step(
            "claude", "FIX", "Claude Code installed", problem or "claude not found",
            "install Claude Code, or cc-swap config set prime.claudePath <path>",
        ))

    login_problems = _worst(dr.check_keychain(ctx)) or [
        f for f in dr.check_plaintext(ctx) if f.severity == "error"
    ]
    if ctx.live.identity is not None and ctx.live.value and not login_problems:
        steps.append(Step("login", "ok", "Logged in to Claude Code"))
    elif login_problems and ctx.live.identity is not None:
        f = login_problems[0]
        steps.append(Step("login", "FIX", "Logged in to Claude Code", f.detail, f.fix))
    else:
        steps.append(Step(
            "login", "TODO", "Logged in to Claude Code", "no live login",
            "run claude and /login",
        ))

    number = dr.live_slot(ctx)
    if number:
        steps.append(Step("adopted", "ok", "Live login saved in a slot", f"#{number}"))
    else:
        steps.append(Step(
            "adopted", "TODO", "Live login saved in a slot",
            "the live login belongs to no slot" if ctx.live.identity else "",
            "cc-swap add",
        ))

    count = len(ctx.slots)
    if count >= 2:
        steps.append(Step("accounts", "ok", "Two or more accounts", f"{count} accounts"))
    else:
        steps.append(Step(
            "accounts", "TODO", "Two or more accounts", f"{count} account{'s' if count != 1 else ''}",
            "log in as each other account (claude → /login) and run cc-swap add after each",
        ))

    upstream = _worst(dr.check_upstream(ctx))
    if upstream:
        steps.append(Step(
            "upstream", "FIX", "Upstream claude-swap gone",
            upstream[0].detail, "; ".join(dict.fromkeys(f.fix for f in upstream)),
        ))
    else:
        steps.append(Step("upstream", "ok", "Upstream claude-swap gone"))

    if ctx.strategy == "maximize":
        steps.append(Step("strategy", "ok", "Strategy is maximize"))
    else:
        steps.append(Step(
            "strategy", "TODO", "Strategy is maximize", f"autoswitch.strategy is {ctx.strategy}",
            "cc-swap config set autoswitch.strategy maximize", applicable=True,
        ))

    if probes.platform not in ("darwin", "linux"):
        steps.append(Step(
            "service", "ok", "Service running on this build",
            "no service on this platform; run cc-swap auto in a terminal",
        ))
    else:
        status = ctx.service_status() or {}
        service_problems = _worst(dr.check_service(ctx))
        if not status.get("installed"):
            steps.append(Step(
                "service", "TODO", "Service running on this build", "not installed",
                "cc-swap service install", applicable=True,
            ))
        elif service_problems:
            steps.append(Step(
                "service", "FIX", "Service running on this build",
                service_problems[0].detail, "cc-swap service install", applicable=True,
            ))
        else:
            steps.append(Step(
                "service", "ok", "Service running on this build", f"pid {status.get('pid') or '?'}",
            ))

    if not ctx.prime_enabled:
        steps.append(Step("priming", "ok", "Priming off unless verified", "priming off (opt-in)"))
    elif path is None or problem:
        steps.append(Step(
            "priming", "FIX", "Priming off unless verified",
            "priming is on but its claude is missing",
            "cc-swap config set prime.claudePath <path>, or cc-swap config set prime.enabled false",
        ))
    elif (note := dr.priming_guard(
        ctx.probes.backup_root, auto_verify=ctx.prime_auto_verify
    )[0]) is not None:
        steps.append(Step(
            "priming", "FIX", "Priming off unless verified",
            f"priming is {note}", "cc-swap prime verify",
        ))
    else:
        steps.append(Step(
            "priming", "ok", "Priming off unless verified",
            f"priming on: {dr.update_pause_text(ctx.prime_auto_verify)}",
        ))
    return steps


def _set_strategy(backup_root) -> None:
    from claude_swap.settings import set_setting

    set_setting(backup_root, "autoswitch.strategy", "maximize")


def _install_service() -> dict:
    from claude_swap.maximize import service

    return service.install()


def apply_steps(probes: dr.Probes, steps: list[Step]) -> list[str]:
    """Do the idempotent steps ``--apply`` may do; returns what was done.

    The service is installed only once Claude Code is logged in, a slot
    holds the live login and upstream claude-swap is gone: a service next to
    a running upstream engine would fight it over the active login."""
    from claude_swap.exceptions import ClaudeSwitchError

    by_key = {s.key: s for s in steps}
    done: list[str] = []
    if by_key.get("strategy") and by_key["strategy"].status != "ok":
        _set_strategy(probes.backup_root)
        done.append("set autoswitch.strategy to maximize")
    service = by_key.get("service")
    ready = all(by_key[k].status == "ok" for k in ("login", "adopted", "upstream") if k in by_key)
    if service and service.status != "ok" and service.applicable:
        if not ready:
            done.append("service not installed: finish the steps above first")
        else:
            try:
                _install_service()
                done.append("installed the cc-swap service")
            except ClaudeSwitchError as e:
                done.append(f"service install failed: {e}")
    return done


def _step_tag(status: str) -> str:
    text = f"{status:<4}"
    if status == "FIX":
        return reddened(text)
    if status == "TODO":
        return yellowed(text)
    return text


def init_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cc-swap init",
        description=(
            "Onboarding and migration checklist: Claude Code installed, logged "
            "in, accounts added, upstream claude-swap gone, strategy maximize, "
            "service running on this build, priming. Re-run it until every "
            "step is ok. Writes nothing without --apply."
        ),
        epilog="Exit status: 0 when every step is ok, else 1.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Do the idempotent steps: set autoswitch.strategy to maximize and "
            "(once the steps before it are ok) cc-swap service install"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit the steps as JSON")
    args = parser.parse_args(argv)
    probes = _probes()
    steps = init_steps(probes)
    applied: list[str] = []
    if args.apply:
        applied = apply_steps(probes, steps)
        if applied:
            probes = _probes()
            steps = init_steps(probes)
    code = 0 if all(s.status == "ok" for s in steps) else 1
    # The checklist covers onboarding; doctor checks everything else (a
    # plaintext copy of the login, a service that holds no lease, ...). Its
    # verdict is part of ours so "All set" never stands next to a doctor
    # that exits 1.
    findings = dr.run_checks(probes)
    doctor_problems = _worst(findings)
    if args.json:
        print(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "exitCode": code,
            "applied": applied,
            "steps": [s.to_json() for s in steps],
            "doctor": {
                "exitCode": dr.exit_code(findings),
                "counts": dr.counts(findings),
                "problems": [f.to_json() for f in doctor_problems],
            },
        }, indent=2))
        sys.exit(code)
    for line in applied:
        print(dimmed(f"applied: {line}"))
    for i, step in enumerate(steps, 1):
        detail = f" — {step.detail}" if step.detail else ""
        print(f"{_step_tag(step.status)}  {i}. {step.title}{detail}")
        if step.status != "ok" and step.fix:
            print(" " * 9 + muted(f"→ {step.fix}"))
    print()
    if code == 0 and doctor_problems:
        n = len(doctor_problems)
        print(
            f"Set up, but cc-swap doctor reports {n} problem{'s' if n != 1 else ''} "
            f"({summary_line(findings)}):"
        )
        for f in doctor_problems:
            label = f.check if f.scope in ("env", "accounts") else f"{f.scope} {f.check}"
            print(f"  {_tag(f.severity)}  {label}: {f.detail}")
            if f.fix:
                print(" " * 9 + muted(f"→ {f.fix}"))
    elif code == 0:
        print("All set. cc-swap doctor checks the details any time.")
    else:
        left = sum(1 for s in steps if s.status != "ok")
        hint = " (--apply does the ones marked applicable)" if any(
            s.applicable and s.status != "ok" for s in steps
        ) and not args.apply else ""
        print(f"{left} step{'s' if left != 1 else ''} to go{hint}; run cc-swap init again after each.")
    sys.exit(code)


# -- why ------------------------------------------------------------------------------

#: Reason code → (what it means, what to do). Every ``NoSwitchEvent`` reason the
#: engine can emit is here and in the docs/reference.md table "Why didn't it switch?"
#: (``tests/maximize/test_why.py`` checks both against the source).
REASONS: dict[str, tuple[str, str]] = {
    "below-threshold": (
        "The active account is below autoswitch.threshold (strategies best and consume-first).",
        "Nothing; lower autoswitch.threshold to switch earlier.",
    ),
    "cooldown": (
        "A proactive switch happened less than autoswitch.cooldownSeconds ago.",
        "Wait, or lower autoswitch.cooldownSeconds.",
    ),
    "no-candidates": (
        "No other account can take you: every other one is disabled, excluded, quarantined or an API key.",
        "cc-swap add another account, cc-swap enable one, or re-login a quarantined one (cc-swap doctor).",
    ),
    "no-qualifying-candidate": (
        "Other accounts exist, but none is far enough below the thresholds, or their usage is unreadable this tick.",
        "Wait for a reset (cc-swap list shows when), or loosen the thresholds.",
    ),
    "no-comparison": (
        "No candidate's usage could be read this tick.",
        "Check the network and cc-swap list; if it persists, cc-swap doctor.",
    ),
    "no-viable-target": (
        "Every candidate failed the last-moment check (dead token, another account's login, live session).",
        "cc-swap doctor names the broken slots; re-login them.",
    ),
    "reset-unknown": (
        "consume-first: the active account's weekly reset time is unknown, so it cannot compare.",
        "Nothing; it resumes once usage reports the reset.",
    ),
    "already-consuming-soonest": (
        "consume-first: no account with room resets sooner than the active one.",
        "Nothing; this is the strategy working.",
    ),
    "stale-usage": (
        "consume-first: the target's usage could not be refreshed this tick (backoff or another poller).",
        "Nothing; it retries next tick.",
    ),
    "active-usage-unknown": (
        "The active account's usage could not be read; failover follows after autoswitch.unhealthyTicks misses in a row.",
        "Check the network and cc-swap list; if it persists, cc-swap doctor.",
    ),
    "active-idle": (
        "The active access token expired while Claude Code is idle; it refreshes on next use.",
        "Nothing.",
    ),
    "active-api-key": (
        "The live login is a managed API key, which has no quota to watch.",
        "cc-swap switch to a subscription account.",
    ),
    "active-credential-unreadable": (
        "The live login could not be read cleanly (Keychain rc=36/51, or only a stale plaintext copy), so switching would overwrite a login cc-swap cannot see.",
        "cc-swap doctor; unlock the login keychain. Switching resumes by itself once a read succeeds.",
    ),
    "unmanaged-active-account": (
        "The live login belongs to no slot, or to a different account than the slot it claims.",
        "cc-swap add (after checking which account you are logged in as).",
    ),
    "new-login-not-backed-up": (
        "A new /login on the active slot is not backed up yet; the engine waits until it is.",
        "Wait a tick; if it persists, cc-swap add --slot N.",
    ),
    "no-active-account": (
        "Nobody is logged in to Claude Code.",
        "Run claude and /login, then cc-swap add.",
    ),
    "already-active": (
        "The chosen target was already the live login when the switch ran.",
        "Nothing.",
    ),
    "maximize-paused": (
        "A Fleet re-login paused switching (pausedUntil, at most 10 minutes).",
        "Finish or cancel the re-login; the pause also ends by itself.",
    ),
    "auto-off": (
        "Automatic switching is off (cc-swap auto off, or Fleet: m → o): the engine keeps deciding but never switches or primes.",
        "cc-swap auto on (or Fleet: m → o); cc-swap auto status shows who turned it off and when.",
    ),
    "maximize-pending": (
        "A soft mark is crossed; maximize waits for an idle moment (idleWindowMin) before switching.",
        "Nothing; a hard ceiling switches at once. Lower maximize.idleWindowMin to switch sooner.",
    ),
    "maximize-hold": (
        "maximize sees no reason to move: below every soft mark and no better-scored account (or within rebalanceCooldownMin, or the better-scored one's 7d would pass soft7d before your next quiet time, so preempt would only move you off it again).",
        "Nothing.",
    ),
    "reset-wait": (
        "A hard or soft mark is crossed, but that window resets within maximize.resetWaitMin minutes and the recent pace will not reach 100% before then, so maximize waits for the reset instead of switching (a switch makes Claude Code re-read the whole context on the new account).",
        "Nothing; it switches at once if the window hits 100%. Set maximize.resetWaitMin to 0 to switch without waiting.",
    ),
    "preempt": (
        "The active account's 7d is on pace to pass soft7d before your next quiet time, and another account would not; maximize moves at the next idle moment (after rebalanceCooldownMin).",
        "Nothing; set maximize.preempt to false to wait for the soft mark instead.",
    ),
    "rebalance-deferred": (
        "A better-scored account exists, but this is usually a busy time and the gain is under maximize.busyRebalanceGap, so the move waits for your next quiet window (at most 6 hours away).",
        "Nothing; lower maximize.busyRebalanceGap, or set maximize.learnIdlePattern to false, to rebalance at any idle moment.",
    ),
    "hard-stay": (
        "A hard mark is reached (or close at the recent pace), but no account under the hard caps has more room on that window than the active one, so maximize stays on it rather than move somewhere that would force a move straight back.",
        "Nothing; at 100% the at-limit switch moves you at once to whatever has quota left. cc-swap add another account for more room.",
    ),
    "ride": (
        "A window listed in maximize.rideWindows reached a hard mark of 99% or more but is under 100%. Usage is reported in whole percents, so up to one point is left: maximize keeps using it for a learned share of the time one point takes, then switches (at once if the account goes idle first, or if it hits 100%).",
        "Nothing; cc-swap doctor shows what has been learned. Set maximize.learnedRide to false (or maximize.rideWindows to \"\") to switch at the hard mark, or lower maximize.rideMaxMin to cap the ride.",
    ),
    "hold": (
        "You asked to stay on the active account (cc-swap hold, or Fleet: h) so a long task keeps its context: until the hold ends, maximize skips its soft, preempt and rebalance moves. A hard mark, 100% and a reset-wait still switch.",
        "Nothing; cc-swap hold off (or Fleet: h → o) lifts it. It ends by itself at its end time (at most 24h), or when the active account changes.",
    ),
}

#: Switch triggers (docs/reference.md's "When it switches" table plus upstream's).
TRIGGERS: dict[str, str] = {
    "at-limit": "the active 5h or 7d window is at 100%",
    "hard": "a hard ceiling is reached (or the recent pace reaches one within forceEtaMin)",
    "soft": "a soft mark is crossed and the account went idle",
    "preempt": "the active 7d is on pace to pass soft7d before your next quiet time; moved while idle",
    "rebalance": "a better-scored account exists, or the active one is excluded / last resort",
    "failover": "the active account's usage could not be read several times in a row",
    "proactive": "the active account reached autoswitch.threshold",
    "consume-first": "another account's weekly window resets sooner",
}


def _decision_code(kind: str, pending: bool) -> str | None:
    if kind == "hold":
        return "maximize-pending" if pending else "maximize-hold"
    if kind == "exhausted":
        return "no-qualifying-candidate"
    if kind == "indeterminate":
        return "active-usage-unknown"
    return None


def _poll_s(backup_root) -> float:
    from claude_swap.settings import load_settings

    try:
        return float(load_settings(backup_root).interval_seconds)
    except Exception:
        return 60.0


def published_why(backup_root, *, now: float) -> dict | None:
    """The engine's fresh published decision, explained; None when there is none.

    Fresh as Fleet's ``now`` line counts it (``fleet.fresh_s``), and for the
    account that is live now: a decision about another active account is
    history. A re-login pause wins over everything (it is what the engine is
    honouring), then ``cc-swap auto off`` (the engine still decides, but
    never acts). A switch that just landed (published on the old account,
    its target now live) still counts."""
    from claude_swap.maximize import pause
    from claude_swap.maximize import view as mxview

    try:
        state = json.loads((backup_root / mxview.STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    paused = pause.active_pause(state, now)
    if paused is not None:
        until, why = paused
        meaning, action = REASONS["maximize-paused"]
        return {
            "source": "paused",
            "decision": "paused",
            "code": "maximize-paused",
            "reason": f"switching paused ({why}) for {dr.duration(until - now)} more",
            "meaning": meaning,
            "action": action,
            "ageS": 0,
        }
    engine = _engine_why(backup_root, now=now, state=state)
    off = pause.effective_auto_off(backup_root, state)
    if off is not None:
        # The engine keeps deciding (and publishing "switch") while auto is
        # off, but refuses to act on it: that refusal is the answer.
        meaning, action = REASONS["auto-off"]
        return {
            "source": "auto-off",
            "decision": "auto-off",
            "code": "auto-off",
            "reason": pause.auto_off_detail(off),
            "meaning": meaning,
            "action": action,
            "ageS": 0,
            "wouldDecide": engine,
        }
    return engine


def idle_pattern(backup_root, *, now: float) -> dict:
    """The learned idle pattern (``history.summary``) for ``why``; reads
    the usage history and settings, writes nothing."""
    from claude_swap.maximize import history
    from claude_swap.settings import load_maximize_settings

    try:
        enabled = load_maximize_settings(backup_root).learn_idle_pattern
    except Exception:
        enabled = True
    return history.summary(history.read(backup_root, now).slots, now, enabled=enabled)


def _pattern_line(pattern: dict) -> str:
    text = str(pattern.get("text") or "").removeprefix("idle pattern: ")
    return dimmed(f"  pattern  {text}")


def ride_learning(backup_root) -> dict:
    """What the learned ride has learned (the state file's
    ``rideLearning``) for ``why``: ``{"text", "windows", "learned"}``.
    Reads the state file and settings, writes nothing."""
    from claude_swap.maximize import policy
    from claude_swap.maximize import ride as learned_ride
    from claude_swap.maximize import view as mxview
    from claude_swap.settings import MaximizeSettings, load_maximize_settings

    try:
        s = load_maximize_settings(backup_root)
    except Exception:
        s = MaximizeSettings()
    try:
        state = json.loads((backup_root / mxview.STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    raw = state.get(learned_ride.LEARN_KEY) if isinstance(state, dict) else None
    return {
        "text": policy.ride_text(raw, s),
        "windows": list(policy.ride_windows(s)),
        "learned": learned_ride.learned(raw),
    }


def _ride_line(ride: dict) -> str:
    text = str(ride.get("text") or "").removeprefix("learned ride: ")
    return dimmed(f"  ride     {text}")


def _published_code(state: Mapping, published) -> str | None:
    """The ``code`` a published hold carries (a reset-aware wait), when it
    is the same record ``published`` was read from."""
    from claude_swap.maximize import view as mxview

    raw = state.get(mxview.DECISION_KEY)
    if not isinstance(raw, Mapping) or raw.get("at") != published.at:
        return None
    code = raw.get("code")
    return code if published.decision == "hold" and code in REASONS else None


def _engine_why(backup_root, *, now: float, state: Mapping | None = None) -> dict | None:
    """The engine's fresh published decision about the live account, explained."""
    from claude_swap.maximize import view as mxview
    from claude_swap.maximize.fleet import fresh_s

    published = mxview.read_state(backup_root).decision
    if published is None:
        return None
    age = now - published.at
    if not 0 <= age <= fresh_s(_poll_s(backup_root)):
        return None
    try:
        sequence = json.loads((backup_root / "sequence.json").read_text(encoding="utf-8"))
        live = sequence.get("activeAccountNumber") if isinstance(sequence, dict) else None
    except (OSError, ValueError):
        live = None
    landed = (
        published.decision == "switch"
        and published.target is not None
        and live is not None
        and str(live) == published.target
    )
    if (
        live is not None
        and published.active is not None
        and str(live) != published.active
        and not landed
    ):
        return None
    code = _published_code(state or {}, published) or _decision_code(
        published.decision, published.pending
    )
    if published.decision == "switch":
        meaning = f"Switching: {TRIGGERS.get(published.trigger or '', published.trigger or 'switch')}."
        action = (
            f"Nothing; the engine switched (now on #{published.target})."
            if landed
            else "Nothing; the engine is switching (or just switched)."
        )
    else:
        meaning, action = REASONS[code] if code in REASONS else ("", "")
    return {
        "source": "engine",
        "decision": "pending" if published.pending else published.decision,
        "pid": published.pid,
        "active": published.active,
        "target": published.target,
        "trigger": published.trigger,
        "code": code,
        "reason": published.reason,
        "meaning": meaning,
        "action": action,
        "ageS": round(age),
    }


def _why_lines(why: dict) -> list[str]:
    who = f"engine pid {why['pid']}" if why.get("pid") else "engine"
    if why["source"] == "paused":
        head = "PAUSED"
    elif why["source"] == "auto-off":
        head = "OFF — auto-switching is OFF"
    else:
        head = why["decision"].upper()
        if why.get("target"):
            head += f" → #{why['target']}"
        if why.get("trigger"):
            head += f" ({why['trigger']})"
    active = f" on #{why['active']}" if why.get("active") else ""
    when = "" if why["source"] in ("paused", "auto-off") else f" · {dr.duration(why['ageS'])} ago"
    if why["source"] == "auto-off":
        lines = [bolded(head)]
    else:
        lines = [f"{bolded(head)}{active}  " + dimmed(f"{who}{when}")]
    lines.append(f"  reason   {why['reason']}")
    if why.get("code"):
        lines.append(f"  code     {why['code']}")
    if why.get("meaning"):
        lines.append(f"  meaning  {why['meaning']}")
    if why.get("action"):
        lines.append(f"  do       {why['action']}")
    would = why.get("wouldDecide")
    if would:
        verdict = would["decision"]
        if would.get("target") and would["decision"] in ("switch", "pending"):
            verdict += f" → #{would['target']}"
        lines.append(dimmed(f"  if on    the engine would {verdict}: {would['reason']}"))
    if why.get("holdLine"):
        lines.append(f"  hold     {why['holdLine']}")
    pattern = why.get("idlePattern")
    if pattern:
        lines.append(_pattern_line(pattern))
    ride = why.get("learnedRide")
    if ride:
        lines.append(_ride_line(ride))
    return lines


def account_hold_now(backup_root, *, now: float) -> tuple[dict | None, str | None]:
    """``(json, line)`` for an account hold pinning the active account
    (``cc-swap hold``), else ``(None, None)``. Read-only."""
    from claude_swap.maximize import hold as account_hold

    try:
        pinned = account_hold.holding(
            account_hold.read_hold(backup_root, now=now),
            account_hold.live_slot(backup_root), now, root=backup_root,
        )
        line = account_hold.status_line(backup_root, now)
    except Exception:
        return None, None
    if pinned is None or line is None:
        return None, None
    return account_hold.status_payload(pinned, now), line


def _dry_run_tick() -> int:
    """``cc-swap auto --once --dry-run`` in this process; its exit code."""
    from claude_swap import cli

    try:
        cli._auto_command(["--once", "--dry-run"])
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    return 0


def why_command(argv: list[str], *, clock: Callable[[], float] = time.time) -> None:
    parser = argparse.ArgumentParser(
        prog="cc-swap why",
        description=(
            "Why did (or didn't) the engine switch? Prints the decision the "
            "running engine last published, with its reason code explained "
            "(docs/reference.md: Why didn't it switch?). Without a fresh one it runs "
            "`cc-swap auto --once --dry-run` instead."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit the explanation as JSON")
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Do not run a dry-run tick when no engine published a fresh decision",
    )
    args = parser.parse_args(argv)
    root = paths.get_backup_root()
    now = clock()
    why = published_why(root, now=now)
    if why is not None:
        why["idlePattern"] = idle_pattern(root, now=now)
        why["learnedRide"] = ride_learning(root)
    held, hold_line = account_hold_now(root, now=now)
    if args.json:
        payload = {"schemaVersion": SCHEMA_VERSION, **(why or {"source": "none"})}
        payload.pop("holdLine", None)
        if why is None:
            payload["fallback"] = "cc-swap auto --once --dry-run --json"
        if held is not None:
            payload["hold"] = held
        print(json.dumps(payload, indent=2))
        sys.exit(0)
    if why is not None:
        if hold_line:
            why["holdLine"] = (
                f"{hold_line.removeprefix('Holding ')} (cc-swap hold off lifts it)"
            )
        for line in _why_lines(why):
            print(line)
        sys.exit(0)
    if hold_line:
        print(f"{hold_line} (cc-swap hold off lifts it)")
    print(
        "No engine published a fresh decision (no engine running, the strategy "
        "is not maximize, or the login changed since)."
    )
    print(_pattern_line(idle_pattern(root, now=now)))
    print(_ride_line(ride_learning(root)))
    if args.no_fallback:
        print(dimmed("Run cc-swap auto --once --dry-run to see what one would decide now."))
        sys.exit(0)
    print(dimmed("What a tick would decide now (cc-swap auto --once --dry-run):"))
    sys.stdout.flush()
    _dry_run_tick()
    sys.exit(0)
