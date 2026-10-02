"""5h-window priming (spec §6): open an idle account's 5-hour window right
after it resets, so its countdown runs while nobody is using the account.

One tiny request through the official ``claude`` CLI opens a window; success
is judged by the usage endpoint's ``five_hour.resets_at``, never by the
percentage (spec §3.1: small requests stay at 0%).

Safety rules this module carries:

- The child gets the account's ACCESS token only (``CLAUDE_CODE_OAUTH_TOKEN``),
  never a refresh token, so the one-time-use refresh chain cannot fork.
- The child runs in one fixed isolated profile, ``<backup_root>/prime-profile``,
  so the active login's Keychain item and ``~/.claude.json`` are not touched.
- Every environment variable that could reroute auth or the API endpoint is
  removed before the two that matter are set.
- Logs and events name accounts by slot number only.

This first half is pure (no I/O): target selection, reset math, env/argv
building, output classification. ``Primer`` (Task 11) is the engine half.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import oauth
from claude_swap.autoswitch import AutoSwitchEvent, ConfigWarningEvent, PrimeEvent
from claude_swap.maximize.model import AccountView, Snapshot
from claude_swap.maximize.snapshot import build_snapshot
from claude_swap.poll_policy import parse_reset_ts
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS, delete_macos_keychain_entry
from claude_swap.settings import PrimeSettings, load_maximize_settings, parse_jitter_range

_logger = logging.getLogger("claude-swap")

WINDOW_S = 5 * 3600.0          # a 5h window's length
BUCKET_S = 600.0               # resets land on 10-minute boundaries (spec §3.1)
VERIFY_DELAY_S = 30.0          # read usage no sooner than this after a prime
VERIFY_TOLERANCE_S = 120.0     # spec §6.2 step 5: ± 2 minutes
PRIME_TIMEOUT_S = 90.0
LIVE_RECHECK_S = 600.0         # re-check a live-session skip after 10 minutes
RATE_LIMIT_FALLBACK_S = 3600.0 # 429 with no known 7d reset: wait an hour
DEFAULT_JITTER: tuple[int, int] = (45, 300)  # used if prime.jitterS is unparseable
COLD_KEY = "cold"
PROFILE_DIRNAME = "prime-profile"
PROMPT = "Reply OK"
FALLBACK_MODEL = "haiku"       # spec §6.3: model 404 → retry once with the alias
TAIL_CHARS = 500

# ``primes[email].lastOutcome`` vocabulary (autoswitch_state.json).
PENDING_OUTCOMES = frozenset({"launched", "timeout", "exit-error"})  # verify first
PERIOD_END_OUTCOMES = frozenset({"primed", "already-on"})  # this off-period is over
RETRY_NOW_OUTCOMES = frozenset({"unverified", "auth-failed"})  # retry without jitter

# Third-party provider routing (Bedrock/Vertex/Foundry/...). session.py's
# AUTH_OVERRIDE_ENV_VARS covers credentials only; these reroute the endpoint.
PROVIDER_ENV_VARS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLOUD_ML_REGION",
)
# Set when cc-swap itself runs inside a Claude Code session; a nested child
# must not believe it is a sub-process of that session.
NESTING_ENV_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
SCRUBBED_ENV_VARS = frozenset(
    {
        *AUTH_OVERRIDE_ENV_VARS,
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "ANTHROPIC_BASE_URL",
        *PROVIDER_ENV_VARS,
        *NESTING_ENV_VARS,
    }
)
SCRUBBED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_")


@dataclass(frozen=True)
class PrimeTarget:
    number: str
    email: str
    window_key: str  # the off-period's identity: minute-ISO of the elapsed reset, or "cold"
    due_at: float    # jittered launch time; callers launch when due_at <= now


@dataclass(frozen=True)
class PrimeRunResult:
    returncode: int | None  # None: timed out, or the process never started
    timed_out: bool
    stderr_tail: str        # secret-masked, last TAIL_CHARS characters
    stdout_tail: str = ""   # secret-masked, last TAIL_CHARS characters
    is_error: bool | None = None  # `--output-format json` "is_error", when parseable


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _slot_order(number: str) -> tuple[int, str]:
    return (int(number), "") if number.isdigit() else (1 << 30, number)


def floor10(ts: float) -> float:
    return float(math.floor(ts / BUCKET_S) * BUCKET_S)


def expected_reset(prime_at: float) -> float:
    """Where a window opened at ``prime_at`` resets: 10-minute floor + 5h."""
    return floor10(prime_at) + WINDOW_S


def verified(
    prime_at: float,
    resets_at_epoch: float | None,
    tolerance_s: float = VERIFY_TOLERANCE_S,
) -> bool:
    if resets_at_epoch is None:
        return False
    return abs(resets_at_epoch - expected_reset(prime_at)) <= tolerance_s


def iso_minute(ts: float) -> str:
    """Epoch → ``YYYY-MM-DDTHH:MM:00Z`` rounded to the nearest minute, so a
    server's ``14:19:59.63`` and ``14:20:00.8`` name the same window."""
    return (
        datetime.fromtimestamp(round(ts / 60.0) * 60.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _continues(entry: Mapping, now: float) -> bool:
    """Whether a stored record still belongs to the current off-period."""
    last = _num(entry.get("lastAttemptAt"))
    return (
        entry.get("lastOutcome") not in PERIOD_END_OUTCOMES
        and last is not None
        and now - last < WINDOW_S
    )


def window_key(view: AccountView, entry: Mapping | None, now: float) -> str:
    """Identity of the off-period an account is in.

    A reading that still shows the elapsed reset names it. A cold reading
    (``resets_at`` null) cannot, so it inherits the key of an attempt made in
    the same off-period — otherwise the key would flip from the reset's ISO to
    ``"cold"`` as soon as the first post-reset reading lands, and the attempt
    count would silently start over.
    """
    if view.reset5 is not None:
        return iso_minute(view.reset5)
    if entry is not None and _continues(entry, now):
        key = entry.get("windowKey")
        if isinstance(key, str) and key:
            return key
    return COLD_KEY


def attempts_used(entry: Mapping | None, key: str, now: float) -> int:
    if entry is None or entry.get("windowKey") != key or not _continues(entry, now):
        return 0
    attempts = entry.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        return 0
    return max(attempts, 0)


def skip_reason(
    view: AccountView,
    active: str | None,
    entry: Mapping | None,
    now: float,
    max_attempts: int,
) -> str | None:
    """Why ``view`` is not a priming target right now (``None`` = it is)."""
    if view.number == active:
        return "active"
    if view.tier == "excluded":
        return "excluded"
    if view.api_key:
        return "api-key"
    if view.quarantined:
        return "quarantined"
    if view.pct5 is None or view.pct7 is None:
        return "usage-unknown"
    if view.pct7 >= 100.0:
        return "7d-exhausted"
    if view.reset5 is not None and view.reset5 > now:
        return "window-on"
    if entry is None:
        return None
    outcome = entry.get("lastOutcome")
    if outcome in PERIOD_END_OUTCOMES:
        # Our own fresh reading saw the window open; a snapshot built before
        # that reading (same tick, or a CLI run) must not re-prime it.
        open_until = _num(entry.get("resetsAt"))
        if open_until is not None and now < open_until:
            return "window-on"
    last = _num(entry.get("lastAttemptAt"))
    if last is not None:
        if outcome in PENDING_OUTCOMES and now - last < WINDOW_S:
            return "pending-verify"
        if outcome == "skipped-live" and now - last < LIVE_RECHECK_S:
            return "live-session"
        if outcome == "rate-limited":
            reset7 = view.reset7
            until = (
                reset7
                if reset7 is not None and reset7 > last
                else last + RATE_LIMIT_FALLBACK_S
            )
            if now < until:
                return "rate-limited"
    key = window_key(view, entry, now)
    if attempts_used(entry, key, now) >= max_attempts:
        return "attempts-exhausted"
    return None


def _anchor(view: AccountView, now: float) -> float:
    """The reset instant the jitter counts from: the reading's own reset while
    we are still in its 10-minute bucket, else the current bucket's start —
    so after a long sleep every account spreads over [B+lo, B+hi] instead of
    all firing at once."""
    if view.reset5 is not None and now - view.reset5 < BUCKET_S:
        return view.reset5
    return floor10(now)


def due_targets(
    snap: Snapshot,
    prime_state: Mapping[str, Mapping],
    settings: PrimeSettings,
    now: float,
    rng: random.Random,
) -> list[PrimeTarget]:
    """Every account whose 5h window should be primed, earliest ``due_at``
    first (ties by slot). Callers launch only those with ``due_at <= now``.

    The jitter is re-drawn on every call: a target that is not due yet on
    this tick gets a fresh draw on the next, which still lands inside
    ``[anchor + lo, anchor + hi]``.
    """
    try:
        lo, hi = parse_jitter_range(settings.jitter_s)  # Task 3's validator
    except ValueError:
        lo, hi = DEFAULT_JITTER
    targets: list[PrimeTarget] = []
    for view in snap.accounts:
        raw = prime_state.get(view.email)
        entry = raw if isinstance(raw, Mapping) else None
        if skip_reason(view, snap.active, entry, now, settings.max_attempts):
            continue
        key = window_key(view, entry, now)
        retry_now = (
            entry is not None
            and entry.get("lastOutcome") in RETRY_NOW_OUTCOMES
            and attempts_used(entry, key, now) > 0
        )
        due_at = now if retry_now else _anchor(view, now) + rng.uniform(lo, hi)
        targets.append(PrimeTarget(view.number, view.email, key, due_at))
    targets.sort(key=lambda t: (t.due_at, _slot_order(t.number)))
    return targets


def _scrubbed(name: str) -> bool:
    upper = name.upper()
    return (
        upper in SCRUBBED_ENV_VARS
        or upper.startswith(SCRUBBED_PREFIXES)
        or "REFRESH_TOKEN" in upper
    )


def build_prime_env(
    base_env: Mapping[str, str], config_dir: Path, access_token: str
) -> dict[str, str]:
    """The child's environment: ``base_env`` minus every auth/endpoint
    override, plus exactly ``CLAUDE_CONFIG_DIR`` and ``CLAUDE_CODE_OAUTH_TOKEN``.

    Refuses an empty token and anything shaped like a refresh token
    (``sk-ant-ort…``): handing one to a child is the one mistake that forks
    the account's token chain.
    """
    token = access_token.strip() if isinstance(access_token, str) else ""
    if not token:
        raise ValueError("priming needs an access token")
    if token.startswith("sk-ant-ort"):
        raise ValueError("refusing to hand a refresh token to the priming child")
    env = {key: value for key, value in base_env.items() if not _scrubbed(key)}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def build_prime_argv(claude_path: str, model: str) -> list[str]:
    return [
        claude_path,
        "-p",
        "--model",
        model,
        "--safe-mode",
        "--tools",
        "",
        "--no-session-persistence",
        "--max-turns",
        "1",
        "--output-format",
        "json",
        PROMPT,
    ]


def resolve_claude_path(configured: str | None, *, home: Path | None = None) -> str | None:
    """``prime.claudePath`` (``cc-swap service install`` saves the path it
    detects there) → ``~/.local/bin/claude``; first executable file wins.

    Deliberately not ``shutil.which``: a shell alias (``claude`` aliased to a
    wrapper) is invisible to it, and launchd/systemd PATHs are minimal.
    """
    home = Path.home() if home is None else home
    for candidate in (configured, str(home / ".local" / "bin" / "claude")):
        if not candidate:
            continue
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def mask_secrets(text: str | None, secret: str | None = None, limit: int = TAIL_CHARS) -> str:
    """Redact ``secret``, anything token-shaped, and email addresses; keep the
    last ``limit`` characters. Masking runs before the cut so a token split
    by the cut can never survive half-visible."""
    if not text:
        return ""
    if secret:
        text = text.replace(secret, "<redacted>")
    text = _EMAIL_RE.sub("<email>", _TOKEN_RE.sub("<redacted>", text))
    return text[-limit:] if limit > 0 else text


_AUTH_RE = re.compile(
    r"\b401\b|authentication_error|invalid api key|invalid bearer token"
    r"|oauth token (?:has expired|is invalid|revoked)|please run /login|not logged in",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"\b429\b|rate_limit_error|usage limit|limit reached|hit your limit|rate limit",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(
    r"not_found_error|issue with the selected model"
    r"|model[^\n]{0,80}(?:not found|not available|does not exist|may not exist)"
    r"|invalid model",
    re.IGNORECASE,
)


def classify_failure(result: PrimeRunResult) -> str:
    """``"auth"`` | ``"rate-limited"`` | ``"model-not-found"`` | ``"other"`` for
    a run that did not succeed. Patterns are checked against the masked
    stdout+stderr tails; Task 9 step 7 records the real CLI wording."""
    text = f"{result.stdout_tail}\n{result.stderr_tail}"
    if _AUTH_RE.search(text):
        return "auth"
    if _RATE_RE.search(text):
        return "rate-limited"
    if _MODEL_RE.search(text):
        return "model-not-found"
    return "other"


# ---------------------------------------------------------------------------
# I/O half (Task 11): run the child, talk to the engine, keep state.
# ---------------------------------------------------------------------------

def _parse_is_error(stdout: str) -> bool | None:
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("is_error"), bool):
        return data["is_error"]
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass  # already gone


def run_prime(
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: Path,
    timeout_s: float = PRIME_TIMEOUT_S,
) -> PrimeRunResult:
    """Run the priming child once: no shell, stdin closed, bounded.

    POSIX children get their own session so a timeout kills the whole tree
    (node may fork helpers that would otherwise hold the pipes open).
    """
    secret = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    extra: dict = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = subprocess.Popen(
            list(argv),
            env=dict(env),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            **extra,
        )
    except OSError as exc:
        return PrimeRunResult(None, False, mask_secrets(f"{type(exc).__name__}: {exc}", secret))
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        return PrimeRunResult(None, True, mask_secrets(err, secret), mask_secrets(out, secret))
    return PrimeRunResult(
        proc.returncode,
        False,
        mask_secrets(err, secret),
        mask_secrets(out, secret),
        _parse_is_error(out),
    )


PRECHECK_MAX_AGE_S = 60.0      # the pre-launch reading must be at most this old
BOUNDARY_GUARD_S = 15.0        # never launch in a bucket's last seconds
TOKEN_MIN_LIFETIME_S = 120.0   # an access token must outlive the run by this much
Runner = Callable[[Sequence[str], Mapping[str, str], Path, float], PrimeRunResult]


def request_fetch(switcher, number: str, at: float) -> None:
    """Make ``number`` poll-due at ``at`` so the next collect fetches it even
    inside the store's serve TTL. Only ever pulls a plan earlier; the store
    still enforces backoff, claims and holds. Best-effort."""
    try:
        ident = switcher.account_identity(number)
        identities = {number: (ident["email"], ident["organizationUuid"])}
        store = switcher._usage_store
        entry = store.entries(identities).get(number)
        if entry is not None and entry.next_poll_at is not None and entry.next_poll_at <= at:
            return
        interval = entry.poll_interval_s if entry is not None else None
        store.set_poll_plan({number: (at, interval)}, identities)
    except Exception:
        _logger.debug("prime: could not pull account %s's poll plan", number, exc_info=True)


def _five_hour(usage: Mapping) -> tuple[float | None, str | None]:
    window = usage.get("five_hour")
    raw = window.get("resets_at") if isinstance(window, dict) else None
    return parse_reset_ts(raw), raw


def _seven_day_pct(usage: Mapping) -> float | None:
    window = usage.get("seven_day")
    return _num(window.get("pct")) if isinstance(window, dict) else None


class Primer:
    """Engine-facing primer: verifies earlier launches, then launches at most
    one due target per call.

    Events are RETURNED, not emitted: the engine's primer slot (Task 8,
    ``engine_hook._run_primer``) emits what ``run_due`` returns, and the CLI
    prints what ``prime_now`` returns. The one exception is upstream's
    ``engine._quarantine``, which emits its own ``QuarantineEvent``.
    The engine builds a new Primer whenever ``prime.*`` changes."""

    def __init__(
        self,
        engine,
        settings: PrimeSettings,
        *,
        runner: Runner = run_prime,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.engine = engine
        self.settings = settings
        self._runner = runner
        self._rng = rng if rng is not None else random.Random()
        self._clock = clock
        self._disabled: str | None = None

    @property
    def profile_dir(self) -> Path:
        return self.engine.switcher.backup_dir / PROFILE_DIRNAME

    # -- entry points ---------------------------------------------------------

    def run_due(self, snap: Snapshot) -> list[AutoSwitchEvent]:
        if not self.settings.enabled or self.engine.dry_run:
            return []
        claude, events = self._claude_or_disable()
        if claude is None:
            return events
        events.extend(self._verify_pending(snap))
        now = self._clock()
        if BUCKET_S - (now % BUCKET_S) < BOUNDARY_GUARD_S:
            return events  # a launch now could land in the next bucket
        state = self._prime_state()
        for target in due_targets(snap, state, self.settings, now, self._rng):
            if target.due_at > now:
                break
            new_events, launched = self._attempt(target, state.get(target.email), claude)
            events.extend(new_events)
            if launched:
                break  # one launch per tick bounds how long a tick can block
        return events

    def prime_now(
        self,
        snap: Snapshot,
        numbers: set[str] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> list[AutoSwitchEvent]:
        """Manual priming (``cc-swap prime``): every eligible target now, no
        jitter, then one verification pass. Same safety checks as ``run_due``;
        ignores ``prime.enabled``."""
        claude, events = self._claude_or_disable()
        if claude is None:
            return events
        events.extend(self._verify_pending(snap))
        now = self._clock()
        state = self._prime_state()
        launched = False
        for target in due_targets(snap, state, self.settings, now, self._rng):
            if numbers is not None and target.number not in numbers:
                continue
            new_events, did = self._attempt(target, state.get(target.email), claude)
            events.extend(new_events)
            launched = launched or did
        if launched:
            sleep(VERIFY_DELAY_S + 5.0)
            events.extend(self._verify_pending(snap))
        return events

    def plan_lines(self, snap: Snapshot, numbers: set[str] | None = None) -> list[str]:
        """``cc-swap prime --dry-run`` rows: slot numbers and reasons, no emails."""
        now = self._clock()
        state = self._prime_state()
        lines: list[str] = []
        for view in sorted(snap.accounts, key=lambda v: _slot_order(v.number)):
            if numbers is not None and view.number not in numbers:
                continue
            raw = state.get(view.email)
            entry = raw if isinstance(raw, Mapping) else None
            reason = skip_reason(view, snap.active, entry, now, self.settings.max_attempts)
            if reason is None and self.engine.switcher.live_session_pids_for(view.number, view.email):
                reason = "live-session"
            if reason is not None:
                lines.append(f"#{view.number}  skip ({reason})")
                continue
            key = window_key(view, entry, now)
            attempt = attempts_used(entry, key, now) + 1
            lines.append(
                f"#{view.number}  would prime now "
                f"(window {key}, attempt {attempt}/{self.settings.max_attempts})"
            )
        return lines

    def pending_accounts(self, snap: Snapshot) -> list[str]:
        state = self._prime_state()
        return [
            view.number
            for view in snap.accounts
            if isinstance(state.get(view.email), Mapping)
            and state[view.email].get("lastOutcome") in PENDING_OUTCOMES
        ]

    # -- steps ----------------------------------------------------------------

    def _claude_or_disable(self) -> tuple[str | None, list[AutoSwitchEvent]]:
        """The ``claude`` path, or — once per Primer — the disable events."""
        if self._disabled is not None:
            return None, []
        claude = resolve_claude_path(self.settings.claude_path)
        if claude is not None:
            return claude, []
        self._disabled = "claude executable not found"
        warning = ConfigWarningEvent(
            message=(
                "prime: no `claude` executable at prime.claudePath or "
                "~/.local/bin/claude — priming is off until prime.claudePath "
                "changes or the engine restarts"
            )
        )
        return None, [warning, PrimeEvent("", "disabled", None, self._disabled)]

    def _verify_pending(self, snap: Snapshot) -> list[PrimeEvent]:
        events: list[PrimeEvent] = []
        now = self._clock()
        by_email = {view.email: view for view in snap.accounts}
        for email, entry in self._prime_state().items():
            if not isinstance(entry, Mapping):
                continue
            pending = entry.get("lastOutcome")
            prime_at = _num(entry.get("lastAttemptAt"))
            view = by_email.get(email)
            if (
                pending not in PENDING_OUTCOMES
                or view is None
                or prime_at is None
                or now < prime_at + VERIFY_DELAY_S
            ):
                continue
            usage = self._fresh_usage(view.number, since=prime_at + VERIFY_DELAY_S)
            if usage is None:
                continue  # no post-launch reading yet; a later tick verifies
            reset, raw = _five_hour(usage)
            on = reset is not None and reset > now
            if verified(prime_at, reset):
                outcome, stored, detail = "primed", "primed", ""
            elif on:
                outcome, stored, detail = "already-on", "already-on", "window open, but not at this prime's reset"
            else:
                outcome = "failed" if pending == "exit-error" else "unverified"
                stored = "unverified"
                detail = "window still off" + (" after a timeout" if pending == "timeout" else "")
            if on:
                self._record(email, lastOutcome=stored, resetsAt=reset)
            else:
                self._record(email, lastOutcome=stored)
            events.append(PrimeEvent(view.number, outcome, raw if on else None, detail))
        return events

    def _attempt(
        self, target: PrimeTarget, entry: Mapping | None, claude: str
    ) -> tuple[list[PrimeEvent], bool]:
        switcher = self.engine.switcher
        num, email = target.number, target.email
        now = self._clock()
        if switcher.current_account_number() == num:
            return [], False  # became active this tick; never prime the active login
        if switcher.live_session_pids_for(num, email):
            return self._skip_live(target, entry, now), False
        usage = self._fresh_usage(num, since=now - PRECHECK_MAX_AGE_S)
        if usage is None:
            _logger.info("prime: account %s has no fresh usage reading; retrying next tick", num)
            return [], False
        reset, raw = _five_hour(usage)
        if reset is not None and reset > now:
            self._mark(target, entry, now, "already-on", resetsAt=reset)
            return [PrimeEvent(num, "already-on", raw, "window opened elsewhere")], False
        pct7 = _seven_day_pct(usage)
        if pct7 is not None and pct7 >= 100.0:
            self._mark(target, entry, now, "rate-limited")
            return [PrimeEvent(num, "failed", None, "7d window exhausted")], False
        force = (
            isinstance(entry, Mapping)
            and entry.get("lastOutcome") == "auth-failed"
            and entry.get("windowKey") == target.window_key
        )
        token, status = self._access_token(num, email, force_refresh=force)
        if status in ("invalid_grant", "identity-conflict"):
            self.engine._quarantine(num, email, status)
            self._mark(target, entry, now, status)
            return [PrimeEvent(num, "failed", None, status)], False
        if status == "skip-live-session":
            return self._skip_live(target, entry, now), False
        if token is None:
            _logger.info("prime: account %s token not ready (%s); retrying next tick", num, status)
            return [], False
        attempts = attempts_used(entry, target.window_key, now) + 1
        if not self._claim(email, target.window_key, attempts, now, entry):
            _logger.info("prime: account %s was claimed by another cc-swap process", num)
            return [], False
        return self._launch(target, claude, token), True

    def _access_token(
        self, num: str, email: str, *, force_refresh: bool
    ) -> tuple[str | None, str]:
        """The slot's current access token, freshened through upstream's
        consume gate when it expires within 10 minutes (``_freshen_target``)
        or when the last launch was rejected with 401 (``force_refresh``).
        Only the access token leaves this method."""
        status = self.engine._freshen_target(num, email)
        if status != "ok":
            return None, status
        switcher = self.engine.switcher
        creds = switcher.read_account_credentials(num, email)
        if force_refresh and creds:
            outcome = switcher.consume_backup_grant(num, email, creds)
            if outcome.error in ("invalid_grant", "no_refresh_token"):
                return None, "invalid_grant"
            if outcome.error is not None or not outcome.credentials:
                return None, outcome.error or "transient"
            creds = outcome.credentials
        data = oauth.extract_oauth_data(creds) if creds else None
        token = data.get("accessToken") if data else None
        expires = _num(data.get("expiresAt")) if data else None
        if not isinstance(token, str) or not token:
            return None, "transient"
        if expires is not None and expires <= (self._clock() + TOKEN_MIN_LIFETIME_S) * 1000:
            return None, "transient"
        return token, "ok"

    def _launch(self, target: PrimeTarget, claude: str, token: str) -> list[PrimeEvent]:
        profile = self._prepare_profile()
        env = build_prime_env(os.environ, profile, token)
        result = self._runner(
            build_prime_argv(claude, self.settings.model), env, profile, PRIME_TIMEOUT_S
        )
        if (
            not result.timed_out
            and result.returncode not in (0, None)
            and classify_failure(result) == "model-not-found"
            and self.settings.model != FALLBACK_MODEL
        ):
            _logger.warning(
                "prime: model %s not found; retrying account %s with %s",
                self.settings.model, target.number, FALLBACK_MODEL,
            )
            result = self._runner(
                build_prime_argv(claude, FALLBACK_MODEL), env, profile, PRIME_TIMEOUT_S
            )
        self._scrub_profile(profile)
        return self._settle(target, result)

    def _settle(self, target: PrimeTarget, result: PrimeRunResult) -> list[PrimeEvent]:
        num, email = target.number, target.email
        if result.timed_out:
            self._record(email, lastOutcome="timeout")
            return [PrimeEvent(
                num, "timeout", None,
                f"no answer in {PRIME_TIMEOUT_S:.0f}s; verifying before any retry",
            )]
        if result.returncode is None:
            self._record(email, lastOutcome="spawn-failed")
            return [PrimeEvent(
                num, "failed", None, "could not start claude: " + result.stderr_tail[-200:]
            )]
        if result.returncode == 0 and result.is_error is not True:
            _logger.info("prime: account %s request sent; verifying after %.0fs", num, VERIFY_DELAY_S)
            return []  # stays "launched" until a later reading verifies it
        kind = classify_failure(result)
        if kind == "auth":
            self._record(email, lastOutcome="auth-failed")
            detail = "token rejected (401); refreshing it next tick"
        elif kind == "rate-limited":
            self._record(email, lastOutcome="rate-limited")
            detail = "rate limited; skipping until the 7d reset"
        elif kind == "model-not-found":
            self._record(email, lastOutcome="model-not-found")
            detail = f"model {self.settings.model} not found (also tried {FALLBACK_MODEL})"
        else:
            # The request may still have reached the API: verify like a timeout.
            self._record(email, lastOutcome="exit-error")
            _logger.warning(
                "prime: account %s claude exited %s: %s",
                num, result.returncode, result.stderr_tail[-200:],
            )
            return []
        return [PrimeEvent(num, "failed", None, detail)]

    # -- helpers --------------------------------------------------------------

    def _fresh_usage(self, number: str, *, since: float) -> dict | None:
        """A usage reading for ``number`` fetched at or after ``since``."""
        switcher = self.engine.switcher
        entry = switcher.usage_entries_by_account(fetch=set()).get(number)
        if entry is None or entry.fetched_at is None or entry.fetched_at < since:
            request_fetch(switcher, number, self._clock())
            entry = switcher.usage_entries_by_account(fetch={number}).get(number)
        if entry is None or entry.fetched_at is None or entry.fetched_at < since:
            return None
        value = entry.decision_value()
        return value if isinstance(value, dict) else None

    def _skip_live(
        self, target: PrimeTarget, entry: Mapping | None, now: float
    ) -> list[PrimeEvent]:
        self._mark(target, entry, now, "skipped-live")
        return [PrimeEvent(
            target.number, "skipped-live", None, "a cswap run session owns this account"
        )]

    def _prepare_profile(self) -> Path:
        profile = self.profile_dir
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(profile, 0o700)
        self._scrub_profile(profile)
        return profile

    def _scrub_profile(self, profile: Path) -> None:
        """Drop any credential the child left behind: the hashed Keychain item
        (macOS) and a plaintext ``.credentials.json``. Run before and after."""
        delete_macos_keychain_entry(profile)
        try:
            (profile / ".credentials.json").unlink(missing_ok=True)
        except OSError:
            _logger.warning("prime: could not remove the prime profile's .credentials.json")

    def _prime_state(self) -> dict:
        primes = self.engine._read_state().get("primes")
        return primes if isinstance(primes, dict) else {}

    def _record(self, email: str, **fields) -> None:
        def mutate(state: dict) -> None:
            primes = state.get("primes")
            if not isinstance(primes, dict):
                primes = state["primes"] = {}
            entry = primes.get(email)
            if not isinstance(entry, dict):
                entry = primes[email] = {}
            entry.update(fields)

        self.engine._mutate_state(mutate)

    def _mark(
        self,
        target: PrimeTarget,
        entry: Mapping | None,
        now: float,
        outcome: str,
        **extra,
    ) -> None:
        """Record a non-launch outcome without disturbing the attempt count."""
        self._record(
            target.email,
            windowKey=target.window_key,
            attempts=attempts_used(entry, target.window_key, now),
            lastAttemptAt=now,
            lastOutcome=outcome,
            **extra,
        )

    def _claim(
        self, email: str, key: str, attempts: int, now: float, seen: Mapping | None
    ) -> bool:
        """Record the attempt before launching, under the state lock, and only
        if nobody else recorded one since we read the state (double-spend
        guard against a concurrent ``cc-swap prime`` or second engine)."""
        seen_at = _num(seen.get("lastAttemptAt")) if isinstance(seen, Mapping) else None
        won = False

        def mutate(state: dict) -> None:
            nonlocal won
            primes = state.get("primes")
            if not isinstance(primes, dict):
                primes = state["primes"] = {}
            current = primes.get(email)
            current_at = _num(current.get("lastAttemptAt")) if isinstance(current, dict) else None
            if current_at != seen_at:
                return
            primes[email] = {
                "windowKey": key,
                "attempts": attempts,
                "lastAttemptAt": now,
                "lastOutcome": "launched",
            }
            won = True

        self.engine._mutate_state(mutate)
        return won


def prime_snapshot(engine, usage: Mapping[str, dict | str | None], now: float) -> Snapshot:
    """A Snapshot for priming, from the engine's state and this tick's usage.
    The active account is re-read here, after any switch the tick made."""
    switcher = engine.switcher
    state = engine._read_state()
    quarantine = state.get("quarantine")
    records = (switcher._get_sequence_data() or {}).get("accounts", {})
    if not isinstance(records, dict):
        records = {}
    return build_snapshot(
        now=now,
        active=switcher.current_account_number(),
        usage=usage,
        records=records,
        quarantined=set(quarantine) if isinstance(quarantine, dict) else set(),
        api_key_accounts={n for n in records if switcher.account_kind_for(n) == "api_key"},
        rate_limit_tiers={},
        samples=(),
        last_switch_at=None,
        settings=load_maximize_settings(switcher.backup_dir),
    )
