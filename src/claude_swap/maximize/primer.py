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
from claude_swap.autoswitch import (
    _SYSTEMIC_STATUSES,
    AutoSwitchEvent,
    ConfigWarningEvent,
    PrimeEvent,
)
from claude_swap.maximize.model import AccountView, Snapshot
from claude_swap.maximize.pause import active_pause
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
KILL_GRACE_S = 5.0             # after a kill: how long to drain the pipes before closing them
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
# Further credential channels of the CLI (gateway, remote bridge, trusted
# device) that session.py's AUTH_OVERRIDE_ENV_VARS does not list.
EXTRA_AUTH_ENV_VARS = (
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
    "CLAUDE_BRIDGE_OAUTH_TOKEN",
    "CLAUDE_TRUSTED_DEVICE_TOKEN",
)
SCRUBBED_ENV_VARS = frozenset(
    {
        *AUTH_OVERRIDE_ENV_VARS,
        *EXTRA_AUTH_ENV_VARS,
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "ANTHROPIC_BASE_URL",
        *PROVIDER_ENV_VARS,
        *NESTING_ENV_VARS,
    }
)
SCRUBBED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_")
# Any CLAUDE* variable shaped like a credential, so channels added to the CLI
# later are covered too (``..._MAX_OUTPUT_TOKENS`` does not match).
SCRUBBED_CLAUDE_SUFFIXES = ("_TOKEN", "_FILE_DESCRIPTOR")


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
    # Fields of the `--output-format json` result, parsed from the WHOLE stdout
    # (the real result is longer than the tail, so the tail is not JSON).
    is_error: bool | None = None
    api_error_status: int | None = None  # the API's HTTP status, when it reports one
    result_text: str | None = None  # masked "result" message; None: stdout was not a JSON object

    @classmethod
    def from_output(
        cls,
        returncode: int | None,
        stdout: str | None,
        stderr: str | None,
        *,
        secret: str | None = None,
        timed_out: bool = False,
    ) -> "PrimeRunResult":
        data = _json_object(stdout)
        is_error = data.get("is_error") if data is not None else None
        text = data.get("result") if data is not None else None
        return cls(
            returncode,
            timed_out,
            mask_secrets(stderr, secret),
            mask_secrets(stdout, secret),
            is_error if isinstance(is_error, bool) else None,
            _http_status(data.get("api_error_status")) if data is not None else None,
            None if data is None else mask_secrets(text if isinstance(text, str) else "", secret),
        )


def _json_object(text: str | None) -> dict | None:
    try:
        data = json.loads(text) if text else None
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _http_status(value: object) -> int | None:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
    if view.login_deadline is not None and now >= view.login_deadline:
        # Past its login deadline: the next refresh is refused, so a launch
        # would spend an access token on a login that is already gone.
        return "login-expired"
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


def _jitter(settings: PrimeSettings) -> tuple[int, int]:
    try:
        return parse_jitter_range(settings.jitter_s)
    except ValueError:
        return DEFAULT_JITTER


def prime_window(
    view: AccountView,
    entry: Mapping | None,
    active: str | None,
    settings: PrimeSettings,
    now: float,
) -> tuple[float, float] | None:
    """When the primer would launch for ``view``: the ``[lo, hi]`` range its
    jittered ``due_at`` is drawn from (:func:`due_targets`). A target due now
    (an unverified/auth-failed retry) is ``(now, now)``. A running window is
    primed right after its reset: ``reset5 + jitter``. None when the primer
    skips the account for any other reason (TUI read model; pure)."""
    lo, hi = _jitter(settings)
    reason = skip_reason(view, active, entry, now, settings.max_attempts)
    if reason == "window-on" and view.reset5 is not None and view.reset5 > now:
        return view.reset5 + lo, view.reset5 + hi
    if reason is not None:
        return None
    key = window_key(view, entry, now)
    if (
        entry is not None
        and entry.get("lastOutcome") in RETRY_NOW_OUTCOMES
        and attempts_used(entry, key, now) > 0
    ):
        return now, now
    anchor = _anchor(view, now)
    return anchor + lo, anchor + hi


@dataclass(frozen=True)
class PlanRow:
    """One account's manual-priming plan: why it is skipped, or which window
    and attempt a launch now would be."""

    number: str
    reason: str | None       # skip_reason, or None when it would prime
    window_key: str | None   # set when it would prime
    attempt: int | None      # 1-based attempt a launch now would make


def plan_rows(
    snap: Snapshot,
    prime_state: Mapping[str, Mapping],
    settings: PrimeSettings,
    now: float,
    numbers: set[str] | None = None,
) -> list[PlanRow]:
    """``cc-swap prime --dry-run``'s plan, in slot order. Pure: the live
    ``cswap run`` session check is the caller's (:meth:`Primer.plan`)."""
    rows: list[PlanRow] = []
    for view in sorted(snap.accounts, key=lambda v: _slot_order(v.number)):
        if numbers is not None and view.number not in numbers:
            continue
        raw = prime_state.get(view.email)
        entry = raw if isinstance(raw, Mapping) else None
        reason = skip_reason(view, snap.active, entry, now, settings.max_attempts)
        if reason is not None:
            rows.append(PlanRow(view.number, reason, None, None))
            continue
        key = window_key(view, entry, now)
        rows.append(PlanRow(view.number, None, key, attempts_used(entry, key, now) + 1))
    return rows


def plan_text(row: PlanRow, max_attempts: int) -> str:
    if row.reason is not None:
        return f"skip ({row.reason})"
    window = row.window_key
    if window != COLD_KEY:
        from claude_swap.autoswitch import local_time_label

        window = f"after the reset at {local_time_label(window)}"  # the key is an ISO minute
    return f"would prime now (window {window}, attempt {row.attempt}/{max_attempts})"


def plan_lines(rows: Sequence[PlanRow], max_attempts: int) -> list[str]:
    """``cc-swap prime --dry-run`` lines: slot numbers and reasons, no emails."""
    return [f"#{row.number}  {plan_text(row, max_attempts)}" for row in rows]


def _scrubbed(name: str) -> bool:
    upper = name.upper()
    return (
        upper in SCRUBBED_ENV_VARS
        or upper.startswith(SCRUBBED_PREFIXES)
        or (upper.startswith("CLAUDE") and upper.endswith(SCRUBBED_CLAUDE_SUFFIXES))
        or "REFRESH_TOKEN" in upper
    )


def isolated_env(base_env: Mapping[str, str], config_dir: Path) -> dict[str, str]:
    """``base_env`` minus every auth/endpoint override, plus exactly
    ``CLAUDE_CONFIG_DIR``: a claude child confined to ``config_dir``'s
    profile. ``cc-swap login`` runs ``claude auth login`` in one."""
    env = {key: value for key, value in base_env.items() if not _scrubbed(key)}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


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
    a run that did not succeed.

    The JSON result's structured fields decide first: ``api_error_status``
    401 / 429 / 404, then the patterns over its ``result`` message. The
    patterns never run over the raw JSON, whose numeric fields
    (``"duration_api_ms":401``) would match. Stdout that is not a JSON object,
    and stderr, are matched as text; Task 9 step 7 records the real wording."""
    status = result.api_error_status
    if status == 401:
        return "auth"
    if status == 429:
        return "rate-limited"
    if status == 404:
        return "model-not-found"
    message = result.stdout_tail if result.result_text is None else result.result_text
    text = f"{message}\n{result.stderr_tail}"
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

def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass  # already gone


def _decoded(data: str | bytes | None) -> str:
    if isinstance(data, bytes):  # TimeoutExpired carries raw bytes
        return data.decode("utf-8", "replace")
    return data or ""


def _close_and_reap(proc: subprocess.Popen) -> None:
    """Close our pipe ends and collect the (killed) child's exit status."""
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def _drain_killed(proc: subprocess.Popen) -> tuple[str, str]:
    """What a killed child wrote, read for at most ``KILL_GRACE_S``. A helper
    that left the process group survives the kill and may hold the pipes
    open; past the grace we stop waiting for EOF and close them."""
    try:
        return proc.communicate(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired as exc:
        _close_and_reap(proc)
        return _decoded(exc.stdout), _decoded(exc.stderr)


def run_prime(
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: Path,
    timeout_s: float = PRIME_TIMEOUT_S,
    *,
    caller: str = "prime",
) -> PrimeRunResult:
    """Run the priming child once: no shell, stdin closed, bounded — returns
    within ``timeout_s + KILL_GRACE_S`` whatever the child's helpers do.

    POSIX children get their own session so a timeout kills the whole tree
    (node may fork helpers that would otherwise hold the pipes open). That
    also keeps a terminal Ctrl-C from reaching the child, so any exception
    while waiting (KeyboardInterrupt, SystemExit, a raising signal handler)
    kills the tree before it propagates: the child holds an access token.

    Launched through ``claude_exec`` (audited, ``DISABLE_AUTOUPDATER=1``,
    held back by its guard: then nothing starts, as if it could not).
    """
    from claude_swap.maximize import claude_exec

    secret = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    extra: dict = {"start_new_session": True} if os.name == "posix" else {}
    try:
        launch = claude_exec.Launch(argv, caller=caller)
    except claude_exec.ExecRefused as exc:
        return PrimeRunResult(None, False, f"not run: {exc.reason}")
    try:
        proc = launch.popen(
            env=claude_exec.child_env(env),
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
        out, err = _drain_killed(proc)
        launch.finish(proc.returncode, timed_out=True)
        return PrimeRunResult.from_output(None, out, err, secret=secret, timed_out=True)
    except BaseException as exc:
        _kill_tree(proc)
        _close_and_reap(proc)
        launch.finish(proc.returncode, error=type(exc).__name__)
        raise
    launch.finish(proc.returncode)
    return PrimeRunResult.from_output(proc.returncode, out, err, secret=secret)


PRECHECK_MAX_AGE_S = 60.0      # the pre-launch reading must be at most this old
BOUNDARY_GUARD_S = 15.0        # never launch in a bucket's last seconds
TOKEN_MIN_LIFETIME_S = 120.0   # an access token must outlive the run by this much
Runner = Callable[[Sequence[str], Mapping[str, str], Path, float], PrimeRunResult]
Sleep = Callable[[float], None]


def guard_wait(now: float) -> float:
    """Seconds to hold a launch at ``now`` so its request cannot slip into
    the next 10-minute bucket (and so miss the reset verification expects):
    0 outside a bucket's last ``BOUNDARY_GUARD_S`` seconds, else until 1 s
    past the next boundary."""
    left = BUCKET_S - (now % BUCKET_S)
    return left + 1.0 if left < BOUNDARY_GUARD_S else 0.0


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


def _no_reading_reason(entry, now: float) -> str:
    """Why the pre-launch check found no recent usage reading, from the
    store entry it was left with. Slot-free: the caller names the account."""
    head = f"no usage reading from the last {PRECHECK_MAX_AGE_S:.0f}s"
    if entry is None:
        return head
    if entry.sentinel is not None:
        return f"{head}: usage {entry.sentinel}"
    if entry.held(now):
        return (
            f"{head}: usage is held for an imported reading for "
            f"{entry.held_until - now:.0f}s more"
        )
    if entry.in_backoff(now):
        return (
            f"{head}: the last usage fetch failed ({entry.last_error or 'error'}); "
            f"fetches back off for {entry.backoff_until - now:.0f}s more"
        )
    if entry.claim_until is not None and now < entry.claim_until:
        return f"{head}: another cc-swap process is fetching it"
    if entry.last_error:
        return f"{head}: the usage fetch failed ({entry.last_error})"
    return f"{head}: the usage fetch did not run"


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
        sleep: Callable[[float], None] = time.sleep,
        version_gate: bool = True,
        version_reader: Callable[[str], str | None] | None = None,
        verify_deps: Callable[[], object] | None = None,
    ):
        self.engine = engine
        self.settings = settings
        self._runner = runner
        self._rng = rng if rng is not None else random.Random()
        self._clock = clock
        self._sleep = sleep
        self._disabled: str | None = None
        self.not_primed: dict[str, str] = {}  # last prime_now: slot → reason
        # The Claude Code version guard (maximize/prime_verify.py): off only
        # for `prime verify --live`, whose prime runs before the record.
        self._version_gate = version_gate
        self._version_reader = version_reader
        self._gate_warned: tuple[str | None, str | None] | None = None
        self._gate_version: str | None = None
        # `(binary identity,)` the version gate last passed under; None when
        # the gate has not passed (or is off). Re-checked just before a launch.
        self._gate_identity: tuple[list | None] | None = None
        # The last closed gate (prime.autoVerify reads its cause), and what
        # builds the zero-cost verify's VerifyDeps (prime_verify.default_deps).
        self._gate_verdict = None
        self._verify_deps = verify_deps
        # `(version, not before)`: set before each automatic verify runs, so
        # one whose outcome could not be recorded (a write error) still
        # waits AUTO_RETRY_S instead of probing on every tick.
        self._auto_backoff: tuple[str, float] | None = None
        # `(binary identity, reason)` the exec guard last warned about.
        self._held_warned: tuple[str, str] | None = None

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
        held = self._exec_held(claude)
        if held is not None:
            return events + held  # nothing runs claude this tick
        blocked = self._version_blocked(claude, manual=False)
        if blocked is not None:
            # Priming resumes on the next tick if this verify passes: one
            # bounded subprocess run per tick, as a launch would be.
            return events + blocked + self._auto_verify(claude)
        events.extend(self._verify_pending(snap))
        now = self._clock()
        if guard_wait(now):
            return events  # a launch now could land in the next bucket
        state = self._prime_state()
        for target in due_targets(snap, state, self.settings, now, self._rng):
            if target.due_at > now or guard_wait(self._clock()):
                break  # the guard: an earlier target's slow checks ran into it
            # A held-back target records nothing, so the next tick retries it.
            new_events, launched, _held = self._attempt(target, state.get(target.email), claude)
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
        ignores ``prime.enabled``. A launch that would fall in a bucket's last
        seconds waits for the next bucket instead of deferring to a tick.

        Targets that were neither launched nor reported by an event land in
        ``self.not_primed`` (slot → reason), so the caller can say why."""
        self.not_primed = {}
        claude, events = self._claude_or_disable()
        if claude is None:
            return events
        blocked = self._version_blocked(claude, manual=True)
        if blocked is not None:
            return events + blocked
        events.extend(self._verify_pending(snap))
        now = self._clock()
        state = self._prime_state()
        launched = False
        for target in due_targets(snap, state, self.settings, now, self._rng):
            if numbers is not None and target.number not in numbers:
                continue
            new_events, did, held_back = self._attempt(
                target, state.get(target.email), claude, wait=sleep
            )
            events.extend(new_events)
            launched = launched or did
            if held_back is not None:
                self.not_primed[target.number] = held_back
        if launched:
            sleep(VERIFY_DELAY_S + 5.0)
            events.extend(self._verify_pending(snap))
        return events

    def plan_lines(self, snap: Snapshot, numbers: set[str] | None = None) -> list[str]:
        """``cc-swap prime --dry-run`` rows: slot numbers and reasons, no emails."""
        return [f"#{num}  {text}" for num, text, _ in self.plan(snap, numbers)]

    def plan_rows(self, snap: Snapshot, numbers: set[str] | None = None) -> list[PlanRow]:
        """:func:`plan_rows` plus the live ``cswap run`` session check."""
        by_number = {view.number: view for view in snap.accounts}
        rows: list[PlanRow] = []
        for row in plan_rows(snap, self._prime_state(), self.settings, self._clock(), numbers):
            view = by_number[row.number]
            if row.reason is None and self.engine.switcher.live_session_pids_for(
                view.number, view.email
            ):
                row = PlanRow(row.number, "live-session", None, None)
            rows.append(row)
        return rows

    def plan(
        self, snap: Snapshot, numbers: set[str] | None = None
    ) -> list[tuple[str, str, bool]]:
        """``(slot, text, would_prime)`` per account, in slot order."""
        return [
            (row.number, plan_text(row, self.settings.max_attempts), row.reason is None)
            for row in self.plan_rows(snap, numbers)
        ]

    def preflight(self) -> tuple[str | None, bool]:
        """What would stop :meth:`prime_now` before any launch, read without
        launching or recording anything (``cc-swap prime --dry-run``):
        ``(reason, whole_run)``. ``whole_run`` is True when the run itself is
        refused (no ``claude``, the version guard) and False when each
        target would be held back (a re-login pause). ``(None, False)`` when nothing stands in the way."""
        from claude_swap.maximize import prime_verify

        claude = resolve_claude_path(self.settings.claude_path)
        if claude is None:
            return "no `claude` executable at prime.claudePath or ~/.local/bin/claude", True
        if self._version_gate:
            try:
                verdict = prime_verify.gate(
                    self.engine.switcher.backup_dir, claude,
                    reader=self._version_reader, clock=self._clock,
                )
            except Exception as e:
                return (
                    f"could not read claude version ({type(e).__name__}); "
                    f"{prime_verify.PAUSED_UNTIL}",
                    True,
                )
            if not verdict.ok:
                return verdict.reason, True
        paused = active_pause(self.engine._read_state(), self._clock())
        if paused is not None:
            return f"switching paused ({paused[1]})", False
        return None, False

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

    def _exec_held(self, claude: str) -> list[AutoSwitchEvent] | None:
        """The engine's exec guard (maximize/claude_exec.py), before anything
        runs ``claude`` this tick: None to go on; else the events (a warning
        once per binary and reason) — the binary changed less than
        ``claude.settleS`` ago, or the OS kills it at launch. Also where the
        engine first notices a new binary (the watcher). Never raises."""
        from claude_swap.maximize import claude_exec

        root = self.engine.switcher.backup_dir
        try:
            binary = claude_exec.stat_binary(claude)
            claude_exec.observe(root, binary, now=time.time())
            reason = claude_exec.engine_hold(root, binary, now=time.time())
        except Exception as e:
            _logger.warning("prime: exec guard failed: %s", type(e).__name__)
            return None
        if reason is None:
            self._held_warned = None
            return None
        key = (str(binary.identity), reason.split(" (", 1)[0])
        if self._held_warned == key:
            return []
        self._held_warned = key
        _logger.info("prime: %s", reason)
        return [ConfigWarningEvent(message=f"prime: paused: {reason}")]

    def _version_blocked(self, claude: str, *, manual: bool) -> list[AutoSwitchEvent] | None:
        """None while the installed ``claude`` is the version priming was
        verified with (or none is recorded yet); else the events saying
        why priming is paused — the warning once per version change for
        the engine, every time for a manual run (which then fails)."""
        if not self._version_gate:
            return None
        from claude_swap.maximize import prime_verify

        self._gate_identity = None
        try:
            # Read before the gate: a binary swapped while it runs then
            # differs from this when the launch re-checks.
            identity = prime_verify.identity(claude)
            verdict = prime_verify.gate(
                self.engine.switcher.backup_dir, claude,
                reader=self._version_reader, clock=self._clock,
            )
        except Exception as e:
            # Fail closed: a check that crashed proves nothing about the
            # installed claude, so priming stays paused until it works again.
            _logger.warning("prime: version gate failed: %s", type(e).__name__)
            verdict = prime_verify.Gate(
                False, None, None,
                f"could not read claude version ({type(e).__name__}); "
                f"{prime_verify.PAUSED_UNTIL}",
            )
        self._gate_version = verdict.current
        self._gate_verdict = verdict
        if verdict.ok:
            self._gate_warned = None
            self._gate_identity = (identity,)
            return None
        pair = (verdict.verified, verdict.current)
        events: list[AutoSwitchEvent] = []
        if manual or self._gate_warned != pair:
            self._gate_warned = pair
            events.append(ConfigWarningEvent(message=f"prime: {verdict.reason}"))
        if manual:
            events.append(PrimeEvent("", "disabled", None, verdict.reason))
        return events

    def _auto_verify(self, claude: str) -> list[AutoSwitchEvent]:
        """``prime.autoVerify``: when the gate is closed only because the
        installed ``claude`` changed, run the zero-cost ``prime verify``
        checks here (never ``--live``) and record the outcome as ``prime
        verify`` would. A transient failure is retried later
        (``prime_verify.AUTO_RETRY_S``, at most ``AUTO_MAX_TRIES`` per
        version); any other failure, or the last try, records a failed
        verify, which waits for a manual one. Skipped while another verify
        holds the lock. Never raises."""
        from claude_swap.maximize import prime_verify as pv

        verdict = self._gate_verdict
        if not self.settings.auto_verify or verdict is None:
            return []
        root = self.engine.switcher.backup_dir
        try:
            now = self._clock()
            backoff = self._auto_backoff
            if backoff is not None and backoff[0] == verdict.current and now < backoff[1]:
                return []
            if pv.auto_verify_due(root, verdict, now) is None:
                return []
            lock = pv.verify_lock(root)
            if not lock.acquire():
                return []  # a `prime verify` (or another engine) is verifying
            try:
                # Re-read under the lock: another verify may have just
                # recorded this version, or failed it.
                verdict = pv.gate(root, claude, reader=self._version_reader, clock=self._clock)
                version = pv.auto_verify_due(root, verdict, now)
                if version is None:
                    return []
                _logger.info("prime: claude %s changed; verifying priming isolation", version)
                self._auto_backoff = (version, now + pv.AUTO_RETRY_S)
                deps = (self._verify_deps or pv.default_deps)()
                try:
                    report = pv.run_verify(
                        root, claude, deps=deps, model=self.settings.model, record=False, now=now,
                    )
                except Exception as e:
                    report = pv.VerifyReport(claude, None, verdict.verified)
                    report.add("verify ran", False, type(e).__name__, transient=True)
                return self._settle_auto_verify(root, version, report, now)
            finally:
                lock.release()
        except Exception as e:
            _logger.warning("prime: automatic verify failed to run: %s", type(e).__name__)
            return []

    def _settle_auto_verify(
        self, root: Path, version: str, report, now: float
    ) -> list[AutoSwitchEvent]:
        from claude_swap.maximize import prime_verify as pv

        found = report.version or version
        if getattr(report, "killed", False):
            # Not an isolation result: the guard pauses priming as "killed by
            # the OS" (maximize/claude_exec.py); no try is spent, nothing failed.
            _logger.warning("prime: claude %s was killed by the OS during the automatic verify", found)
            return []
        if report.ok and report.version is not None:
            pv.record_verified(root, report.version, by=pv.VERIFIED_BY_ENGINE, now=now)
            detail = (
                f"claude {report.version}: priming isolation re-verified automatically "
                "(zero-cost checks); priming resumes"
            )
            _logger.info("prime: %s", detail)
            return [PrimeEvent("", "auto-verified", None, detail)]
        failed = [c for c in report.checks if not c.ok]
        what = "; ".join(
            f"{c.name}: {c.detail}" if c.detail else c.name for c in failed
        ) or "no checks ran"
        if report.transient and pv.auto_tries(root, found) + 1 < pv.AUTO_MAX_TRIES:
            tries = pv.note_auto_retry(root, found, what, now=now)
            detail = (
                f"claude {found}: automatic verify did not finish ({what}); "
                f"retrying in {pv.AUTO_RETRY_S / 60:.0f} min (try {tries}/{pv.AUTO_MAX_TRIES})"
            )
            _logger.warning("prime: %s", detail)
            return [PrimeEvent("", "auto-verify-retry", None, detail)]
        pv.record_failed(root, found, report.failures() or [c.name for c in failed], now=now)
        detail = (
            f"claude {found}: automatic verify failed ({what}); priming stays paused "
            "until `cc-swap prime verify` passes"
        )
        _logger.warning("prime: %s", detail)
        return [PrimeEvent("", "auto-verify-failed", None, detail)]

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
            if pending not in PENDING_OUTCOMES or view is None or prime_at is None:
                continue
            if now - prime_at >= WINDOW_S:
                # A whole window later (the machine slept): no reading can
                # tell our prime from anything since. Drop it unjudged; it
                # does not count against the account, which is eligible again.
                self._record(email, lastOutcome="unverified", attempts=0)
                _logger.info(
                    "prime: account %s's launch went unverified for 5h; dropped", view.number
                )
                continue
            if now < prime_at + VERIFY_DELAY_S:
                continue
            usage = self._fresh_usage(view.number, since=prime_at + VERIFY_DELAY_S)
            if usage is None:
                continue  # no post-launch reading yet; a later tick verifies
            reset, raw = _five_hour(usage)
            on = reset is not None and reset > now
            if verified(prime_at, reset):
                outcome, stored, detail = "primed", "primed", ""
                self._note_verified_prime()
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

    def _note_verified_prime(self) -> None:
        if not self._version_gate or self._gate_version is None:
            return
        from claude_swap.maximize import prime_verify

        try:
            prime_verify.note_verified_prime(self.engine.switcher.backup_dir, self._gate_version)
        except Exception:
            _logger.debug("prime: could not record the verified claude version", exc_info=True)

    def _attempt(
        self,
        target: PrimeTarget,
        entry: Mapping | None,
        claude: str,
        *,
        wait: Sleep | None = None,
    ) -> tuple[list[PrimeEvent], bool, str | None]:
        """Pre-check, claim and launch one target. ``wait`` is how a launch
        that would fall in a bucket's last seconds is handled: ``None``
        defers it to a later tick (the engine), a sleep waits it out (CLI).

        Returns ``(events, launched, held_back)``. ``held_back`` is the reason
        a target was neither launched nor reported by an event — nothing is
        recorded, so the engine simply retries it next tick, but a manual
        ``cc-swap prime`` must tell the user (it is otherwise a silent no-op)."""
        switcher = self.engine.switcher
        num, email = target.number, target.email
        now = self._clock()
        # Never prime the active login. A switch can land at any point while
        # the checks below run, so this is re-read before the token step and
        # right before the claim as well.
        if self._is_active(num):
            return self._skip_active(target, entry, now), False, None
        if switcher.live_session_pids_for(num, email):
            return self._skip_live(target, entry, now), False, None
        usage, why = self._fresh_reading(num, since=now - PRECHECK_MAX_AGE_S)
        if usage is None:
            return self._held_back(num, why)
        reset, raw = _five_hour(usage)
        if reset is not None and reset > now:
            self._mark(target, entry, now, "already-on", resetsAt=reset)
            return [PrimeEvent(num, "already-on", raw, "window opened elsewhere")], False, None
        pct7 = _seven_day_pct(usage)
        if pct7 is not None and pct7 >= 100.0:
            self._mark(target, entry, now, "rate-limited")
            return [PrimeEvent(num, "failed", None, "7d window exhausted")], False, None
        force = (
            isinstance(entry, Mapping)
            and entry.get("lastOutcome") == "auth-failed"
            and entry.get("windowKey") == target.window_key
        )
        if self._is_active(num):
            return self._skip_active(target, entry, self._clock()), False, None
        token, status = self._access_token(num, email, force_refresh=force)
        if status in ("invalid_grant", "identity-conflict"):
            self.engine._quarantine(num, email, status)
            self._mark(target, entry, now, status)
            return [PrimeEvent(num, "failed", None, status)], False, None
        if status == "skip-live-session":
            return self._skip_live(target, entry, now), False, None
        if token is None:
            return self._held_back(num, f"access token not ready ({status})")
        # The checks above can take a while (usage fetch, token refresh). The
        # launch instant is read here, guarded here, and recorded as the
        # prime time verification measures against.
        if self._is_active(num):
            return self._skip_active(target, entry, self._clock()), False, None
        launch_at = self._clock()
        pause = guard_wait(launch_at)
        if pause:
            if wait is None:
                return self._held_back(
                    num, "a launch now could land in the next 10-minute bucket"
                )
            wait(pause)
            if self._is_active(num):
                return self._skip_active(target, entry, self._clock()), False, None
            launch_at = self._clock()
        # The version gate ran at the start of the tick; the checks above can
        # take a while, and Claude Code may have updated itself since.
        stale = self._launch_blocked(claude)
        if stale is not None:
            return self._held_back(num, stale)
        attempts = attempts_used(entry, target.window_key, launch_at) + 1
        refused = self._claim(email, target.window_key, attempts, launch_at, entry)
        if refused is not None:
            return self._held_back(num, refused)
        return self._launch(target, claude, token, wait or self._sleep), True, None

    def _launch_blocked(self, claude: str) -> str | None:
        """Why a launch must wait, from a re-check just before it: the
        ``claude`` binary is no longer the one the version gate passed."""
        from claude_swap.maximize import claude_exec, prime_verify

        try:
            held = claude_exec.engine_hold(
                self.engine.switcher.backup_dir, claude_exec.stat_binary(claude), now=time.time(),
            ) if claude_exec.current_manual() is None else None
        except Exception:
            held = None
        if held is not None:
            return held
        if self._version_gate and self._gate_identity is not None:
            if prime_verify.identity(claude) != self._gate_identity[0]:
                return "claude changed since the version check; re-checked next tick"
        return None

    @staticmethod
    def _held_back(num: str, why: str) -> tuple[list[PrimeEvent], bool, str]:
        _logger.info("prime: account %s not primed this pass: %s", num, why)
        return [], False, why

    def _access_token(
        self, num: str, email: str, *, force_refresh: bool
    ) -> tuple[str | None, str]:
        """The slot's current access token, freshened through upstream's
        consume gate when it expires within 10 minutes (``_freshen_target``)
        or, instead of that, when the last launch was rejected with 401
        (``force_refresh``) — one refresh grant per attempt at most.
        Only the access token leaves this method."""
        if force_refresh:
            status, creds = self._forced_refresh(num, email)
        else:
            status = self.engine._freshen_target(num, email)
            creds = (
                self.engine.switcher.read_account_credentials(num, email)
                if status == "ok"
                else None
            )
        if status != "ok":
            return None, status
        data = oauth.extract_oauth_data(creds) if creds else None
        token = data.get("accessToken") if data else None
        expires = _num(data.get("expiresAt")) if data else None
        if not isinstance(token, str) or not token:
            return None, "transient"
        if expires is not None and expires <= (self._clock() + TOKEN_MIN_LIFETIME_S) * 1000:
            return None, "transient"
        return token, "ok"

    def _forced_refresh(self, num: str, email: str) -> tuple[str, str | None]:
        """``_freshen_target`` with the refresh made unconditional: the same
        live-session gate, the same consume gate, the same status mapping and
        the same identity check on the grant's ``token_account`` — but not
        a second, near-expiry grant in the same attempt."""
        engine = self.engine
        switcher = engine.switcher
        if switcher.live_session_pids_for(num, email):
            return "skip-live-session", None
        creds = switcher.read_account_credentials(num, email)
        if not creds:
            return "transient", None
        if not oauth.extract_oauth_data(creds):
            return "invalid_grant", None
        outcome = switcher.consume_backup_grant(num, email, creds)
        if outcome.error is None and outcome.credentials:
            if engine._note_token_identity(num, outcome.token_account):
                return "identity-conflict", None
            return "ok", outcome.credentials
        if outcome.error in ("invalid_grant", "no_refresh_token"):
            return "invalid_grant", None
        if outcome.error in _SYSTEMIC_STATUSES:
            return outcome.error, None
        return "transient", None

    def _launch(
        self, target: PrimeTarget, claude: str, token: str, sleep: Sleep
    ) -> list[PrimeEvent]:
        profile = self._prepare_profile()
        try:
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
                # Same attempt (already claimed), new launch instant: guard it
                # and make it the prime time verification measures against.
                pause = guard_wait(self._clock())
                if pause:
                    sleep(pause)
                self._record(target.email, lastAttemptAt=self._clock())
                result = self._runner(
                    build_prime_argv(claude, FALLBACK_MODEL), env, profile, PRIME_TIMEOUT_S
                )
        finally:
            # Also on Ctrl-C / SystemExit: never leave a login in the profile.
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
        return self._fresh_reading(number, since=since)[0]

    def _fresh_reading(self, number: str, *, since: float) -> tuple[dict | None, str]:
        """``_fresh_usage`` plus, when there is no such reading, why not.

        The store decides whether the fetch may run (backoff, holds, another
        collector's claim) and the endpoint may refuse it (429): either way
        the stored reading stays as old as it was, and the caller must say so
        instead of treating the account as having nothing to do."""
        switcher = self.engine.switcher
        entry = switcher.usage_entries_by_account(fetch=set()).get(number)
        if entry is None or entry.fetched_at is None or entry.fetched_at < since:
            request_fetch(switcher, number, self._clock())
            entry = switcher.usage_entries_by_account(fetch={number}).get(number)
        if entry is None or entry.fetched_at is None or entry.fetched_at < since:
            return None, _no_reading_reason(entry, self._clock())
        value = entry.decision_value()
        if not isinstance(value, dict):
            return None, f"usage reading unusable ({value or 'unknown'})"
        return value, ""

    def _is_active(self, number: str) -> bool:
        return self.engine.switcher.current_account_number() == number

    def _skip_active(
        self, target: PrimeTarget, entry: Mapping | None, now: float
    ) -> list[PrimeEvent]:
        """The target became the active login after the snapshot: no launch,
        no attempt spent; ``skip_reason`` keeps it out while it stays active."""
        self._mark(target, entry, now, "skipped-active")
        return [PrimeEvent(target.number, "skipped-active", None, "it became the active login")]

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
    ) -> str | None:
        """Record the attempt before launching, under the state lock, and only
        if nobody else recorded one since we read the state (double-spend
        guard against a concurrent ``cc-swap prime`` or second engine) and no
        re-login pause landed meanwhile. ``None`` = claimed; otherwise why not."""
        seen_at = _num(seen.get("lastAttemptAt")) if isinstance(seen, Mapping) else None
        refused: str | None = "another cc-swap process claimed this attempt"

        def mutate(state: dict) -> None:
            nonlocal refused
            paused = active_pause(state, self._clock())
            if paused is not None:
                refused = f"switching paused ({paused[1]})"
                return
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
            refused = None

        self.engine._mutate_state(mutate)
        return refused


def prime_snapshot(engine, usage: Mapping[str, dict | str | None], now: float) -> Snapshot:
    """A Snapshot for priming, from the engine's state and this tick's usage.
    The active account is re-read here, after any switch the tick made."""
    switcher = engine.switcher
    state = engine._read_state()
    quarantine = state.get("quarantine")
    records = (switcher._get_sequence_data() or {}).get("accounts", {})
    if not isinstance(records, dict):
        records = {}
    from claude_swap.maximize.engine_hook import read_login_deadlines

    active = switcher.current_account_number()
    return build_snapshot(
        now=now,
        active=active,
        login_deadlines=read_login_deadlines(switcher, records, active),
        usage=usage,
        records=records,
        quarantined=set(quarantine) if isinstance(quarantine, dict) else set(),
        api_key_accounts={n for n in records if switcher.account_kind_for(n) == "api_key"},
        rate_limit_tiers={},
        samples=(),
        last_switch_at=None,
        settings=load_maximize_settings(switcher.backup_dir),
    )
