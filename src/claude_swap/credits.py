"""Prepaid credit balance and credit grants, per account (display only).

Two read-only organization endpoints Claude Code itself calls (2.1.289,
``auth: "teleport-org"`` — bearer token plus ``x-organization-uuid``):

- ``GET /api/oauth/organizations/{org}/prepaid/credits`` — the prepaid
  balance in minor units (``amount``, Claude Code's ``balanceCents``) with
  ``currency``, and ``promo_tranches``: promotional parts of that balance,
  each with ``remaining_amount_minor_units`` and ``expires_at`` (Claude Code
  words one as "$10 of your balance expires in 3 days").
- ``GET /api/oauth/organizations/{org}/overage_credit_grant?campaign=
  feature_of_the_week`` — a one-off extra-usage credit grant: ``available``,
  ``eligible``, ``granted``, ``amount_minor_units``, ``currency``,
  ``expires_at``. Claiming it is a POST; cc-swap never claims.

Neither is the usage endpoint, so none of this spends the 5h/7d polling
budget, and nothing here feeds a switch decision. Fetches are paced
separately (``REFRESH_S`` per account, ``FORCE_MIN_S`` for an explicit
refresh) with their own failure backoff, in ``cache/credits.json``. An
expired access token is skipped, never refreshed here: the usage collector
owns token refresh.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import oauth
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

SCHEMA_VERSION = 1
API_BASE = "https://api.anthropic.com"
PREPAID_PATH = "/api/oauth/organizations/{org}/prepaid/credits"
GRANT_PATH = "/api/oauth/organizations/{org}/overage_credit_grant?campaign=feature_of_the_week"

#: A reading this old is refetched by the background cadence.
REFRESH_S = 3600.0
#: An explicit refresh (Fleet's ``f``) still waits this long between fetches.
FORCE_MIN_S = 60.0
#: In-flight lease: concurrent surfaces skip an account another is fetching.
LEASE_S = 60.0
#: A slot with no usable access token (expired, missing) is looked at again
#: after this long; the usage collector refreshes tokens meanwhile.
SKIP_RETRY_S = 300.0
#: Failure backoff: BACKOFF_BASE_S · 2^(n-1), capped; a Retry-After wins when longer.
BACKOFF_BASE_S = 300.0
BACKOFF_CAP_S = 6 * 3600.0

Identity = tuple[str, str]  # (email, organizationUuid), as in usage_store

# Claude Code 2.1.289's own formatter (the same API's reference consumer):
# these symbols, and only JPY/KRW/VND read as zero-decimal minor units.
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "BRL": "R$",
            "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "SGD": "S$"}
_ZERO_DECIMAL = {"JPY", "KRW", "VND"}
#: Statuses that mean "this org has no such resource" — an empty reading,
#: not a failure to back off from.
_ABSENT = (403, 404)


# -- HTTP ----------------------------------------------------------------------------


def request_org_json(
    access_token: str, org_uuid: str, path: str, timeout: float = 5.0
) -> dict:
    """GET one organization-scoped OAuth endpoint and decode its JSON."""
    url = API_BASE + path.format(org=org_uuid)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": oauth.OAUTH_BETA_HEADER,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "x-organization-uuid": org_uuid,
            "User-Agent": "claude-swap/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data if isinstance(data, dict) else {}


def _int_or_none(value: object) -> int | None:
    number = oauth._finite(value)
    return None if number is None else int(number)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_prepaid(data: object) -> dict | None:
    """``{"balance", "currency", "expiring"}`` from ``prepaid/credits``, or
    None when the org has no prepaid balance (no numeric ``amount``)."""
    if not isinstance(data, dict):
        return None
    balance = _int_or_none(data.get("amount"))
    if balance is None:
        return None
    currency = _str_or_none(data.get("currency"))
    expiring: list[dict] = []
    for tranche in data.get("promo_tranches") or []:
        if not isinstance(tranche, dict):
            continue
        amount = _int_or_none(tranche.get("remaining_amount_minor_units"))
        expires_at = _str_or_none(tranche.get("expires_at"))
        if amount is None or amount <= 0 or expires_at is None:
            continue
        expiring.append({
            "amount": amount,
            "currency": _str_or_none(tranche.get("currency")) or currency,
            "name": _str_or_none(tranche.get("name")),
            "expiresAt": expires_at,
        })
    expiring.sort(key=lambda t: t["expiresAt"])
    return {"balance": balance, "currency": currency, "expiring": expiring}


def parse_grant(data: object) -> dict | None:
    """A granted or claimable credit grant, else None (nothing to show)."""
    if not isinstance(data, dict):
        return None
    granted = data.get("granted") is True
    claimable = data.get("available") is True and data.get("eligible") is True
    if not (granted or claimable):
        return None
    return {
        "state": "granted" if granted else "available",
        "amount": _int_or_none(data.get("amount_minor_units")),
        "currency": _str_or_none(data.get("currency")),
        "expiresAt": _str_or_none(data.get("expires_at")),
    }


@dataclass(frozen=True)
class CreditsOutcome:
    """One account's fetch. ``credits`` is set on success (``error`` None);
    ``grant_failed`` keeps the previous grant reading when only the grant
    request failed."""

    credits: dict | None = None
    error: str | None = None
    retry_after_s: float | None = None
    grant_failed: bool = False


def fetch_credits(
    access_token: str, org_uuid: str, timeout: float = 5.0
) -> CreditsOutcome:
    """Both reads for one account, concurrently. The balance is the primary
    read: its failure fails the fetch. A 403/404 means the org has no such
    resource — an empty reading, not an error. A failed grant read only
    leaves the grant as it was. A body that does not parse reads as empty."""

    def get(path: str) -> dict:
        return request_org_json(access_token, org_uuid, path, timeout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        prepaid_f = pool.submit(get, PREPAID_PATH)
        grant_f = pool.submit(get, GRANT_PATH)
        try:
            prepaid = _parse_safely(parse_prepaid, prepaid_f.result())
        except urllib.error.HTTPError as e:
            if e.code not in _ABSENT:
                kind, retry_after = oauth._classify_usage_error(e)
                return CreditsOutcome(error=kind, retry_after_s=retry_after)
            prepaid = None
        except Exception as e:
            kind, retry_after = oauth._classify_usage_error(e)
            return CreditsOutcome(error=kind, retry_after_s=retry_after)
        credits: dict = dict(prepaid) if prepaid else {}
        grant_failed = False
        try:
            grant = _parse_safely(parse_grant, grant_f.result())
            if grant is not None:
                credits["grant"] = grant
        except urllib.error.HTTPError as e:
            if e.code not in _ABSENT:
                grant_failed = True
                _logger.debug("Credit grant fetch failed: http-%s", e.code)
        except Exception as e:
            grant_failed = True
            _logger.debug("Credit grant fetch failed: %r", e)
    return CreditsOutcome(credits=credits, grant_failed=grant_failed)


def _parse_safely(parse: Callable[[object], dict | None], data: object) -> dict | None:
    try:
        return parse(data)
    except Exception as e:  # display-only: a body we cannot read is "nothing"
        _logger.debug("Credits response parse failed: %r", e)
        return None


# -- store ---------------------------------------------------------------------------


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class CreditsStore:
    """``cache/credits.json``: last-good credits per slot plus fetch state.

    Rows are identity-guarded like ``usage.json`` (a slot re-used by another
    account never shows the previous one's balance), writes go through
    ``cache/.credits.lock`` and never hold it across network I/O.
    """

    def __init__(self, cache_dir: Path, clock: Callable[[], float] = time.time):
        self.path = cache_dir / "credits.json"
        self._lock_path = cache_dir / ".credits.lock"
        self.clock = clock

    def _read_rows(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA_VERSION:
            return {}
        rows = raw.get("accounts")
        return rows if isinstance(rows, dict) else {}

    def _write_rows(self, rows: dict[str, dict]) -> None:
        atomic_write_json(self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows})

    @staticmethod
    def _matches(row: object, identity: Identity) -> bool:
        return (
            isinstance(row, dict)
            and row.get("email") == identity[0]
            and row.get("organizationUuid", "") == identity[1]
        )

    def _mutate(
        self,
        identities: dict[str, Identity],
        nums: Iterable[str],
        fn: Callable[[str, dict, float], None],
    ) -> None:
        with FileLock(self._lock_path):
            rows = self._read_rows()
            now = self.clock()
            for num in nums:
                identity = identities[num]
                if not self._matches(rows.get(num), identity):
                    rows[num] = {"email": identity[0], "organizationUuid": identity[1]}
                fn(num, rows[num], now)
            self._write_rows(rows)

    def readings(self, identities: dict[str, Identity]) -> dict[str, dict]:
        """Slot → ``{**credits, "fetchedAt": epoch}`` for slots with a reading."""
        rows = self._read_rows()
        out: dict[str, dict] = {}
        for num, identity in identities.items():
            row = rows.get(num)
            if not self._matches(row, identity):
                continue
            credits = row.get("credits")
            fetched_at = _num(row.get("fetchedAt"))
            if isinstance(credits, dict) and fetched_at is not None:
                out[num] = {**credits, "fetchedAt": fetched_at}
        return out

    @staticmethod
    def _due(row: dict, now: float, force: bool) -> bool:
        lease = _num(row.get("leaseUntil"))
        if lease is not None and now < lease:
            return False
        backoff = _num(row.get("backoffUntil"))
        if backoff is not None and now < backoff:
            return False
        fetched_at = _num(row.get("fetchedAt"))
        if fetched_at is None:
            return True
        return now - fetched_at >= (FORCE_MIN_S if force else REFRESH_S)

    def reserve(
        self, identities: dict[str, Identity], *, force: bool = False
    ) -> list[str]:
        """Lease and return the slots due for a fetch."""
        due: list[str] = []

        def take(num: str, row: dict, now: float) -> None:
            if self._due(row, now, force):
                row["leaseUntil"] = now + LEASE_S
                due.append(num)

        self._mutate(identities, list(identities), take)
        return due

    def defer(self, identities: dict[str, Identity], nums: Iterable[str]) -> None:
        """Slots reserved but not fetchable now (no usable token): look again
        in ``SKIP_RETRY_S``, without counting a failure."""
        nums = [n for n in nums if n in identities]
        if nums:
            def push(_num: str, row: dict, now: float) -> None:
                row["leaseUntil"] = now + SKIP_RETRY_S

            self._mutate(identities, nums, push)

    def record(
        self, identities: dict[str, Identity], outcomes: dict[str, CreditsOutcome]
    ) -> dict[str, int]:
        """Merge outcomes; returns each failed slot's consecutive-failure count."""
        failed: dict[str, int] = {}

        def apply(num: str, row: dict, now: float) -> None:
            outcome = outcomes[num]
            row.pop("leaseUntil", None)
            row["lastAttemptAt"] = now
            if outcome.error is None:
                credits = dict(outcome.credits or {})
                previous = row.get("credits")
                if (
                    outcome.grant_failed
                    and isinstance(previous, dict)
                    and "grant" in previous
                ):
                    credits["grant"] = previous["grant"]
                row["credits"] = credits
                row["fetchedAt"] = now
                row["consecutiveFailures"] = 0
                row.pop("lastError", None)
                row.pop("backoffUntil", None)
                return
            failures = int(row.get("consecutiveFailures") or 0) + 1
            row["consecutiveFailures"] = failures
            failed[num] = failures
            row["lastError"] = outcome.error
            wait = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** min(failures - 1, 16))
            if outcome.retry_after_s is not None:
                wait = max(wait, min(outcome.retry_after_s, BACKOFF_CAP_S))
            row["backoffUntil"] = now + wait

        nums = [n for n in outcomes if n in identities]
        if nums:
            self._mutate(identities, nums, apply)
        return failed


def refresh(
    store: CreditsStore,
    identities: dict[str, Identity],
    read_credentials: Callable[[str], str],
    *,
    force: bool = False,
    fetcher: Callable[[str, str], CreditsOutcome] | None = None,
    timeout: float = 5.0,
    names: Mapping[str, str] | None = None,
) -> dict[str, dict]:
    """Fetch the due slots, record the outcomes, return every slot's reading.

    Log lines call a slot by its display name (``names``, maximize/names.py;
    never an email).

    Slots without an organization are never fetched (nor their credentials
    read). ``read_credentials(num)`` is called only for due slots, at fetch
    time — so a token the usage pass just rotated is the one used. A slot
    without a usable (present, unexpired) access token is skipped and
    deferred (``CreditsStore.defer``) — no failure is recorded for it.
    ``timeout`` bounds each request (``cswap list`` passes a short one).
    """
    from claude_swap.maximize.names import name_of

    fetcher = fetcher or partial(fetch_credits, timeout=timeout)
    names = names or {}
    with_org = {num: ident for num, ident in identities.items() if ident[1]}
    due = store.reserve(with_org, force=force) if with_org else []
    jobs: dict[str, tuple[str, str]] = {}
    skipped: list[str] = []
    for num in due:
        try:
            creds = read_credentials(num)
        except Exception as e:
            _logger.debug("Credits: credential read failed for %s: %r", name_of(names, num), e)
            creds = ""
        data = oauth.extract_oauth_data(creds) if creds else None
        token = data.get("accessToken") if data else None
        org = identities[num][1]
        if not token or oauth.is_oauth_token_expired(data.get("expiresAt")):
            skipped.append(num)
            continue
        jobs[num] = (token, org)
    outcomes: dict[str, CreditsOutcome] = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = {num: pool.submit(fetcher, *job) for num, job in jobs.items()}
            for num, future in futures.items():
                try:
                    outcomes[num] = future.result()
                except Exception as e:  # a fetcher must not take the pass down
                    outcomes[num] = CreditsOutcome(error=type(e).__name__)
        failed = store.record(identities, outcomes)
        for num, failures in failed.items():
            # First failure of a streak at WARNING, the repeats at DEBUG. The
            # display name, never the email: the line is paste-safe.
            level = logging.WARNING if failures == 1 else logging.DEBUG
            _logger.log(level, "Credits fetch failed for %s: %s (%d in a row)",
                        name_of(names, num), outcomes[num].error, failures)
    store.defer(identities, skipped)
    return store.readings(identities)


# -- display -------------------------------------------------------------------------


def money(minor: int, currency: str | None) -> str:
    """``$12.30`` / ``EUR 5.00`` / ``JPY 500`` from minor units."""
    code = (currency or "USD").upper()
    prefix = _SYMBOLS.get(code, f"{code} ")
    if code in _ZERO_DECIMAL:
        return f"{prefix}{minor:,}"
    return f"{prefix}{minor / 100:,.2f}"


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _day(ts: float) -> str:
    local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return f"{local:%b} {local.day}"


def summary(credits: dict | None, now: float) -> str | None:
    """One compact line, or None when the account has nothing to show:
    ``balance $20.00 · $10.00 expires Nov 1 · grant $25.00 expires Oct 20``."""
    if not isinstance(credits, dict):
        return None
    parts: list[str] = []
    currency = credits.get("currency")
    balance = credits.get("balance")
    if isinstance(balance, int) and balance > 0:
        parts.append(f"balance {money(balance, currency)}")
    live = [
        t for t in credits.get("expiring") or []
        if isinstance(t, dict) and (ts := _parse_ts(t.get("expiresAt"))) is not None and ts > now
    ]
    if live:
        first = live[0]
        parts.append(
            f"{money(first['amount'], first.get('currency'))} expires "
            f"{_day(_parse_ts(first['expiresAt']))}"  # type: ignore[arg-type]
            + (f" (+{len(live) - 1} more)" if len(live) > 1 else "")
        )
    grant = credits.get("grant")
    if isinstance(grant, dict):
        expires = _parse_ts(grant.get("expiresAt"))
        if expires is None or expires > now:
            amount = grant.get("amount")
            words = "grant"
            if isinstance(amount, int):
                words += f" {money(amount, grant.get('currency'))}"
            if grant.get("state") == "available":
                words += " available"
            elif expires is not None:
                words += f" expires {_day(expires)}"
            parts.append(words)
    return " · ".join(parts) or None


def _dollars(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def cloud_credit_summary(usage: dict | None, now: float) -> str | None:
    """``$0 / $250 · expires Nov 5`` for the cloud-session credit the usage
    poll carries (``oauth.parse_cloud_credit``); None when the account has
    none or it has expired. Everything shown comes from the block itself."""
    cloud = usage.get("cloud_credit") if isinstance(usage, dict) else None
    if not isinstance(cloud, dict) or not isinstance(cloud.get("limit"), (int, float)):
        return None
    expires = _parse_ts(cloud.get("resets_at"))
    if expires is not None and expires <= now:
        return None
    used = cloud.get("used")
    text = (
        f"{_dollars(used)} / {_dollars(cloud['limit'])}"
        if isinstance(used, (int, float)) else _dollars(cloud["limit"])
    )
    if expires is not None:
        text += f" · expires {_day(expires)}"
    if cloud.get("locked_reason"):
        text += f" · locked: {cloud['locked_reason']}"
    return text


def reset_coupons_summary(usage: dict | None, now: float) -> str | None:
    """``2 left (5h/7d) · expires Nov 20`` for the usage-reset coupons the
    usage poll carries (``oauth.parse_reset_coupons``); None unless the
    account is eligible and holds a coupon that is still usable."""
    coupons = usage.get("reset_coupons") if isinstance(usage, dict) else None
    if not isinstance(coupons, dict) or coupons.get("eligible") is not True:
        return None
    live = []
    for grant in coupons.get("grants") or []:
        if not isinstance(grant, dict) or not isinstance(grant.get("left"), int):
            continue
        ends = _parse_ts(grant.get("ends_at"))
        if grant["left"] > 0 and (ends is None or ends > now):
            live.append((grant, ends))
    if not live:
        return None
    labels: list[str] = []
    for grant, _ends in live:
        for limit in grant.get("clears") or []:
            label = oauth.RESET_LIMIT_LABELS.get(limit)
            if label and label not in labels:
                labels.append(label)
    text = f"{sum(g['left'] for g, _ in live)} left"
    if labels:
        text += f" ({'/'.join(labels)})"
    ends = [e for _, e in live if e is not None]
    if ends:
        text += f" · expires {_day(min(ends))}"
    if all(g.get("paused") for g, _ in live):
        text += " · paused"
    cooldown = _parse_ts(coupons.get("cooldown_until"))
    if cooldown is not None and cooldown > now:
        text += " · cooldown until " + oauth.reset_clock_string(
            datetime.fromtimestamp(cooldown, tz=timezone.utc),
            datetime.fromtimestamp(now, tz=timezone.utc),
        )
    return text


def _major(minor: int, currency: str | None) -> float:
    return float(minor) if (currency or "USD").upper() in _ZERO_DECIMAL else minor / 100


def _iso(epoch_s: float) -> str:
    return (
        datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def to_json(credits: dict | None) -> dict | None:
    """``--json`` projection (major units, like ``spend``); None without a reading."""
    if not isinstance(credits, dict):
        return None
    currency = credits.get("currency")
    out: dict = {
        "balance": (
            _major(credits["balance"], currency)
            if isinstance(credits.get("balance"), int) else None
        ),
        "currency": currency,
        "expiring": [
            {
                "amount": _major(t["amount"], t.get("currency")),
                "currency": t.get("currency"),
                "name": t.get("name"),
                "expiresAt": t["expiresAt"],
            }
            for t in credits.get("expiring") or []
            if isinstance(t, dict)
        ],
        "grant": None,
    }
    grant = credits.get("grant")
    if isinstance(grant, dict):
        amount = grant.get("amount")
        out["grant"] = {
            "state": grant.get("state"),
            "amount": _major(amount, grant.get("currency")) if isinstance(amount, int) else None,
            "currency": grant.get("currency"),
            "expiresAt": grant.get("expiresAt"),
        }
    fetched_at = credits.get("fetchedAt")
    if isinstance(fetched_at, (int, float)):
        out["fetchedAt"] = _iso(fetched_at)
    return out
