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

import math
import os
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.maximize.model import AccountView, Snapshot
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS
from claude_swap.settings import PrimeSettings, parse_jitter_range

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
