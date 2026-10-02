"""Tool settings persisted at ``<backup_root>/settings.json``.

One versioned JSON file for user-tunable claude-swap preferences, written
atomically with the backup dir's 0600/0700 modes. v1 carries the
``autoswitch`` and ``ui`` sections; other sections can be added additively.
Unknown keys (future fields, other tools' experiments) survive a round trip.

Reading is forgiving — a missing or corrupt file yields defaults with a logged
warning, never a crash — so a bad hand edit degrades to default behavior.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_swap.exceptions import ConfigError
from claude_swap.fsutil import replace_with_retry

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``cswap auto``).

    ``threshold`` is binding-window utilization (max of the 5h/7d percentages):
    at or above it the engine looks for a better account. 90 rather than 95
    leaves margin for the macOS ~30s Keychain pickup tail and for heavy
    subagent turns burning past the mark before a swap lands. A proactive
    candidate must itself sit below the threshold (never land somewhere that
    re-triggers next tick) and beat the active account's utilization by at
    least ``hysteresis_pct``, so two accounts hovering at the line never
    ping-pong while a strictly better account is always taken.
    """

    threshold: float = 90.0
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    strategy: str = "best"  # "best" (most headroom) or "consume-first" (soonest weekly reset)
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3
    # Comma-separated model display name(s) (e.g. "Fable" or "Fable,Opus"),
    # or "all" for every scoped window an account reports. Each named model's
    # per-model weekly limit is folded into the binding window, so the engine
    # switches off an account whose model quota is exhausted even while its
    # 5h/7d windows still have headroom. None = account-wide 5h/7d only
    # (default).
    model: str | None = None


@dataclass(frozen=True)
class UiSettings:
    """Appearance preferences (``ui`` section). ``theme`` selects the TUI/CLI
    color theme; ``auto`` follows terminal-background detection."""

    theme: str = "auto"


@dataclass(frozen=True)
class MaximizeSettings:
    """Knobs for cc-swap's ``maximize`` strategy (``maximize`` section).

    A section of its own rather than fields on ``AutoSwitchSettings``:
    upstream adds fields there, and fork fields beside them would conflict on
    every merge. Each window has a soft mark (switch at the next idle moment)
    and a hard cap (switch now); ``soft <= hard`` per window is enforced by
    `load_maximize_settings` (lenient) and `set_setting` (strict).
    """

    soft_5h: float = 50.0
    hard_5h: float = 95.0
    soft_7d: float = 90.0
    hard_7d: float = 98.0
    landing_margin: float = 5.0
    idle_window_min: int = 10
    idle_max_delta_pct: float = 1.0
    force_eta_min: int = 10
    pending_poll_s: int = 180
    rebalance_cooldown_min: int = 30
    tie_epsilon: float = 0.1
    last_resort: str | None = None  # comma-separated emails/aliases
    plan_override: str | None = None  # "email:20x,email:5x"


@dataclass(frozen=True)
class PrimeSettings:
    """cc-swap 5h-window priming (``prime`` section); off unless enabled."""

    enabled: bool = False
    model: str = "claude-haiku-4-5"
    jitter_s: str = "45-300"  # "LO-HI" seconds after a reset, see parse_jitter_range
    max_attempts: int = 2
    claude_path: str | None = None


_SECTION_DEFAULT_SOURCES = {
    "autoswitch": AutoSwitchSettings,
    "ui": UiSettings,
    "maximize": MaximizeSettings,
    "prime": PrimeSettings,
}


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for one user-tunable settings.json key.

    Single source of truth for bounds/choices: both the lenient clamp on load
    (`_clamped`) and the strict validation in `cswap config set`
    (`parse_setting_value`) read from here, so the two can't drift.
    """

    section: str  # top-level JSON section ("autoswitch", "ui")
    json_key: str  # camelCase key inside the section
    field: str  # snake_case AutoSwitchSettings field
    kind: str  # "float" | "int" | "bool" | "choice"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.json_key}"

    @property
    def default(self):
        return getattr(_SECTION_DEFAULT_SOURCES[self.section](), self.field)


# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
SETTING_SPECS: dict[str, SettingSpec] = {
    spec.dotted: spec
    for spec in (
        SettingSpec(
            "autoswitch", "threshold", "threshold", "float", 50.0, 99.9,
            help="Switch when the binding 5h/7d window reaches this pct",
        ),
        SettingSpec(
            "autoswitch", "intervalSeconds", "interval_seconds", "float", 15.0, 3600.0,
            help="Poll interval for the cswap auto loop, in seconds",
        ),
        SettingSpec(
            "autoswitch", "cooldownSeconds", "cooldown_seconds", "float", 0.0, 86400.0,
            help="Minimum seconds between proactive switches",
        ),
        SettingSpec(
            "autoswitch", "hysteresisPct", "hysteresis_pct", "float", 0.0, 50.0,
            help="A target must beat the active account by this many pct",
        ),
        SettingSpec(
            "autoswitch", "strategy", "strategy", "choice",
            choices=("best", "consume-first", "maximize"),
            help="How auto-switch picks the target account",
        ),
        SettingSpec(
            "autoswitch", "includeApiKeyAccounts", "include_api_key_accounts", "bool",
            help="Allow rotating onto managed API-key accounts (bill per token)",
        ),
        SettingSpec(
            "autoswitch", "unhealthyTicks", "unhealthy_ticks", "int", 1, 100,
            help="Consecutive failed polls before an account is unhealthy",
        ),
        SettingSpec(
            "autoswitch", "model", "model", "string",
            help="Also switch on these models' weekly limits (e.g. Fable, Fable,Opus, or all)",
        ),
        SettingSpec(
            "ui", "theme", "theme", "choice", choices=("dark", "light", "auto"),
            help="Color theme; auto follows the terminal background",
        ),
        # cc-swap sections (spec §8). Kept after upstream's rows so an
        # upstream key addition lands above without touching these lines.
        SettingSpec(
            "maximize", "soft5h", "soft_5h", "float", 1.0, 99.9,
            help="maximize: 5h soft mark, switch at the next idle moment",
        ),
        SettingSpec(
            "maximize", "hard5h", "hard_5h", "float", 1.0, 99.9,
            help="maximize: 5h hard cap, switch now (>= soft5h)",
        ),
        SettingSpec(
            "maximize", "soft7d", "soft_7d", "float", 1.0, 99.9,
            help="maximize: 7d soft mark, switch at the next idle moment",
        ),
        SettingSpec(
            "maximize", "hard7d", "hard_7d", "float", 1.0, 99.9,
            help="maximize: 7d hard cap, switch now (>= soft7d)",
        ),
        SettingSpec(
            "maximize", "landingMargin", "landing_margin", "float", 0.0, 30.0,
            help="maximize: a target must sit this many pct below both soft marks",
        ),
        SettingSpec(
            "maximize", "idleWindowMin", "idle_window_min", "int", 3, 60,
            help="maximize: minutes of usage samples that decide idle",
        ),
        SettingSpec(
            "maximize", "idleMaxDeltaPct", "idle_max_delta_pct", "float", 0.0, 10.0,
            help="maximize: max pct growth inside the idle window",
        ),
        SettingSpec(
            "maximize", "forceEtaMin", "force_eta_min", "int", 0, 60,
            help="maximize: switch now when a hard cap is this many minutes away (0 = off)",
        ),
        # Floor = poll_policy.MIN_INTERVAL_S: the per-account poll budget is
        # shared by every machine; the engine enforces it again regardless.
        SettingSpec(
            "maximize", "pendingPollS", "pending_poll_s", "int", 180, 600,
            help="maximize: active-account poll seconds while a switch waits",
        ),
        SettingSpec(
            "maximize", "rebalanceCooldownMin", "rebalance_cooldown_min", "int", 0, 240,
            help="maximize: minimum minutes between rebalance switches",
        ),
        SettingSpec(
            "maximize", "tieEpsilon", "tie_epsilon", "float", 0.0, 2.0,
            help="maximize: scores this close count as a tie",
        ),
        SettingSpec(
            "maximize", "lastResort", "last_resort", "string",
            help="maximize: last-resort accounts (emails/aliases, comma-separated)",
        ),
        SettingSpec(
            "maximize", "planOverride", "plan_override", "string",
            help="maximize: plan per account, e.g. a@x.com:20x,b@x.com:5x",
        ),
        SettingSpec(
            "prime", "enabled", "enabled", "bool",
            help="prime: keep idle accounts' 5h windows started",
        ),
        SettingSpec(
            "prime", "model", "model", "string",
            help="prime: model for the priming call",
        ),
        SettingSpec(
            "prime", "jitterS", "jitter_s", "string",
            help="prime: wait LO-HI seconds after a reset before priming",
        ),
        SettingSpec(
            "prime", "maxAttempts", "max_attempts", "int", 1, 5,
            help="prime: attempts per 5h window",
        ),
        SettingSpec(
            "prime", "claudePath", "claude_path", "string",
            help="prime: claude executable (default: auto-detect)",
        ),
    )
}

_AUTOSWITCH_KEYS: dict[str, str] = {
    spec.field: spec.json_key
    for spec in SETTING_SPECS.values()
    if spec.section == "autoswitch"
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def parse_model_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated model list, trimmed and case-insensitively
    deduped (first spelling wins). Shared by the auto engine and the manual
    switch strategies so both read ``autoswitch.model`` identically."""
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


def _describe(value) -> str:
    """A raw settings value as it reads in a message, without dumping a huge one."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _clamp_number(spec: SettingSpec, value) -> tuple[float, str | None]:
    """One numeric key's lenient clamp: ``(number, problem)``.

    ``problem`` says what had to change, phrased to follow the key name, and
    is None when the stored number stands: an int where a float is wanted is
    the same number. Anything that is not a number, and NaN/±inf, reads as a
    bad type and becomes the default; the rest is clamped into the range.
    """
    default = spec.default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default, (
            f"must be a number, got {_describe(value)}; "
            f"using default {format_setting_value(default)}"
        )
    # NaN and ±inf are not thresholds: `json.loads` accepts them, `int(nan)`
    # raises, and NaN would slip through min/max unclamped. (An int never
    # needs the check, and `isfinite` raises OverflowError on one too big for
    # a float.)
    if isinstance(value, float) and not math.isfinite(value):
        return default, (
            f"must be a finite number, got {_describe(value)}; "
            f"using default {format_setting_value(default)}"
        )
    number = float(min(max(value, spec.lo), spec.hi))
    if number != value:
        return number, (
            f"is {_describe(value)}, outside {format_setting_value(spec.lo)}-"
            f"{format_setting_value(spec.hi)}; clamped to {format_setting_value(number)}"
        )
    return number, None


def _clamped(settings, section: str = "autoswitch", repairs: list[str] | None = None):
    """Clamp values into the SETTING_SPECS ranges; bad types and non-finite
    numbers (NaN, ±inf) → the default.

    ``section`` selects the registry rows; the result has ``settings``' type.
    When ``repairs`` is a list, one message per number or string that had to
    be replaced or clamped is appended to it, and an unsupported choice goes
    there too instead of into the log (a bool is only coerced, so a loader
    that cares about one reports it itself). An int-valued float (12.0 for an
    int key) is not a repair: nothing the user wrote changes.
    """

    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.section != section:
            continue
        value = getattr(settings, spec.field)
        problem = None
        if spec.kind in ("float", "int"):
            number, problem = _clamp_number(spec, value)
            if spec.kind == "int":
                whole = int(number)
                if problem is None and whole != number:
                    problem = (
                        f"must be a whole number, got {_describe(value)}; "
                        f"truncated to {whole}"
                    )
                number = whole
            kwargs[spec.field] = number
        elif spec.kind == "bool":
            kwargs[spec.field] = bool(value)
        elif spec.kind == "string":
            # A non-empty string keeps as-is; anything else reverts to default
            # (None) so a null/garbage settings.json value disables the filter.
            if isinstance(value, str) and value:
                kwargs[spec.field] = value
            else:
                kwargs[spec.field] = spec.default
                # null/"" on a key whose default is "unset" already say so.
                if not (spec.default is None and value in (None, "")):
                    problem = (
                        f"must be a non-empty string, got {_describe(value)}; "
                        f"using default {format_setting_value(spec.default)}"
                    )
        else:  # choice
            if value not in spec.choices:
                if repairs is None:
                    _logger.warning(
                        "settings.json: unsupported %s %r; using %r",
                        spec.dotted, value, spec.default,
                    )
                else:
                    problem = (
                        f"must be one of: {', '.join(spec.choices)}, got "
                        f"{_describe(value)}; using default {spec.default}"
                    )
                value = spec.default
            kwargs[spec.field] = value
        if problem is not None and repairs is not None:
            repairs.append(f"{spec.dotted} {problem}")
    return type(settings)(**kwargs)


def _read_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return {}
    return raw


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section; missing/corrupt file or fields → defaults."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        return AutoSwitchSettings()
    kwargs = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        if json_key in section:
            kwargs[field] = section[json_key]
    try:
        settings = AutoSwitchSettings(**kwargs)
    except TypeError:
        settings = AutoSwitchSettings()
    return _clamped(settings)


def load_ui_settings(backup_root: Path) -> UiSettings:
    """Load the ui section; missing/corrupt file or unknown theme → default."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("ui")
    default = UiSettings()
    if not isinstance(section, dict):
        return default
    theme = section.get("theme", default.theme)
    if theme not in SETTING_SPECS["ui.theme"].choices:
        _logger.warning(
            "settings.json: unsupported ui.theme %r; using %r",
            theme, default.theme,
        )
        return default
    return UiSettings(theme=theme)


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    raw = _read_raw(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        section = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        section[json_key] = getattr(settings, field)
    raw["autoswitch"] = section
    atomic_write_json(path, raw)


def setting_spec(dotted_key: str) -> SettingSpec:
    """Look up a spec by dotted key; unknown keys raise with the valid list."""
    spec = SETTING_SPECS.get(dotted_key)
    if spec is None:
        raise ConfigError(
            f"unknown setting '{dotted_key}'\n"
            f"Valid keys: {', '.join(SETTING_SPECS)}"
        )
    return spec


_BOOL_WORDS = {
    "true": True, "1": True, "yes": True,
    "false": False, "0": False, "no": False,
}


def parse_setting_value(spec: SettingSpec, raw_value: str):
    """Strictly parse a CLI-provided string for `cswap config set`.

    Unlike the forgiving clamp on load, out-of-range, mistyped or non-finite
    (nan/inf) values raise ConfigError so the user learns about the problem
    when setting the value, not by silently degraded behavior at `cswap auto`
    time.
    """
    if spec.kind == "bool":
        # Never bool(str): bool("false") is True.
        parsed = _BOOL_WORDS.get(raw_value.strip().lower())
        if parsed is None:
            raise ConfigError(
                f"{spec.dotted} expects true or false (or 1/0, yes/no), "
                f"got '{raw_value}'"
            )
        return parsed
    if spec.kind == "choice":
        if raw_value not in spec.choices:
            raise ConfigError(
                f"{spec.dotted} must be one of: {', '.join(spec.choices)}"
            )
        return raw_value
    if spec.kind == "string":
        value = raw_value.strip()
        if not value:
            raise ConfigError(
                f"{spec.dotted} expects a non-empty value; use "
                f"'cswap config unset {spec.dotted}' to clear it"
            )
        return value
    try:
        finite = math.isfinite(float(raw_value))
    except ValueError:
        finite = True  # not a number at all: the parse below reports that
    if not finite:
        raise ConfigError(f"{spec.dotted} expects a finite number, got '{raw_value}'")
    try:
        value = int(raw_value) if spec.kind == "int" else float(raw_value)
    except ValueError:
        noun = "an integer" if spec.kind == "int" else "a number"
        raise ConfigError(
            f"{spec.dotted} expects {noun}, got '{raw_value}'"
        ) from None
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{spec.dotted} must be between {format_setting_value(spec.lo)} "
            f"and {format_setting_value(spec.hi)}"
        )
    return value


def format_setting_value(value) -> str:
    """Render a settings value the way settings.json writes it."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_raw_for_write(path: Path) -> dict:
    """Raw read for the config write path: a corrupt file errors, never {}.

    ``_read_raw``'s degrade-to-defaults is right for reads, but a
    read-modify-write starting from ``{}`` would replace a malformed (and
    maybe hand-recoverable) file with a near-empty one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); fix or delete it before "
            "changing settings"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} is not a JSON object; fix or delete it before "
            "changing settings"
        )
    return raw


def set_setting(backup_root: Path, dotted_key: str, raw_value: str):
    """Validate and persist one key for `cswap config set`; returns the value.

    Writes only the given key (plus schemaVersion) — deliberately not
    ``save_settings``, which writes every known key and would freeze the
    current defaults into the file, pinning users to them if a later version
    changes a default. Unknown keys and sections in the file survive.
    """
    spec = setting_spec(dotted_key)
    value = parse_setting_value(spec, raw_value)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    _check_fork_setting(raw, spec, value)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get(spec.section)
    if not isinstance(section, dict):
        section = {}
    section[spec.json_key] = value
    raw[spec.section] = section
    atomic_write_json(path, raw)
    return value


def unset_setting(backup_root: Path, dotted_key: str) -> bool:
    """Remove one key from settings.json; False if it wasn't set (no write)."""
    spec = setting_spec(dotted_key)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    section = raw.get(spec.section)
    if not isinstance(section, dict) or spec.json_key not in section:
        return False
    _check_fork_setting(raw, spec, spec.default)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    del section[spec.json_key]
    if not section:
        del raw[spec.section]
    atomic_write_json(path, raw)
    return True


def effective_settings(backup_root: Path) -> list[tuple[SettingSpec, object, bool]]:
    """(spec, effective value, explicitly set?) per key, in registry order.

    "Set" means the key is present in the raw file — an explicit value equal
    to the default still counts — so `cswap config`'s "(default)" marker
    reflects the file, not value equality.
    """
    raw = _read_raw(settings_path(backup_root))
    loaded = {
        "autoswitch": load_settings(backup_root),
        "ui": load_ui_settings(backup_root),
        "maximize": load_maximize_settings(backup_root),
        "prime": load_prime_settings(backup_root),
    }
    rows = []
    for spec in SETTING_SPECS.values():
        section = raw.get(spec.section)
        is_set = isinstance(section, dict) and spec.json_key in section
        rows.append((spec, getattr(loaded[spec.section], spec.field), is_set))
    return rows


def merged_with_cli(settings: AutoSwitchSettings, args) -> AutoSwitchSettings:
    """Overlay non-None CLI overrides (argparse Namespace) onto settings."""
    overrides = {}
    for attr, field in (
        ("threshold", "threshold"),
        ("interval", "interval_seconds"),
        ("cooldown", "cooldown_seconds"),
        ("include_api_key_accounts", "include_api_key_accounts"),
        ("model", "model"),
        ("strategy", "strategy"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


# -- cc-swap: maximize / prime sections -------------------------------------
# Fork-only code lives in this block so upstream merges touch it rarely; the
# upstream functions above gained only one-line hooks (`_clamped`'s section,
# `effective_settings`' loaders, `set_setting`/`unset_setting`'s
# `_check_fork_setting`).

_SPEC_BY_FIELD: dict[tuple[str, str], SettingSpec] = {
    (spec.section, spec.field): spec for spec in SETTING_SPECS.values()
}
_MAXIMIZE_PAIRS = (("soft_5h", "hard_5h"), ("soft_7d", "hard_7d"))
# `auto` flag attribute -> MaximizeSettings field (see merge_maximize_cli).
MAXIMIZE_CLI_FLAGS = (
    ("soft5h", "soft_5h"),
    ("hard5h", "hard_5h"),
    ("soft7d", "soft_7d"),
    ("hard7d", "hard_7d"),
)
JITTER_MAX_S = 599
_JITTER_RE = re.compile(r"\s*(\d+)\s*-\s*(\d+)\s*")
_PLAN_ENTRY_RE = re.compile(r"[^\s:,]+:(?:20x|5x)", re.IGNORECASE)


def _dotted(section: str, field: str) -> str:
    return _SPEC_BY_FIELD[(section, field)].dotted


def parse_jitter_range(value: str) -> tuple[int, int]:
    """Parse ``prime.jitterS`` ("LO-HI", whole seconds) into ``(lo, hi)``.

    HI stays under 600: a 5h window starts at its first request floored to
    10 minutes (spec §3.1), so priming anywhere in [R, R+10min) after a reset
    R lands the same next reset; later would start the window a slot late.

    Raises:
        ValueError: malformed, LO > HI, or HI > JITTER_MAX_S.
    """
    m = _JITTER_RE.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise ValueError(
            f"expects LO-HI in whole seconds (e.g. 45-300), got {value!r}"
        )
    lo, hi = int(m.group(1)), int(m.group(2))
    if not lo <= hi <= JITTER_MAX_S:
        raise ValueError(
            f"needs 0 <= LO <= HI <= {JITTER_MAX_S}, got {value!r}"
        )
    return lo, hi


def _plan_override_error(value: str) -> str | None:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    bad = [part for part in parts if not _PLAN_ENTRY_RE.fullmatch(part)]
    if parts and not bad:
        return None
    return (
        "maximize.planOverride expects comma-separated EMAIL:20x or EMAIL:5x "
        f"entries, got {', '.join(bad) or repr(value)}"
    )


def _maximize_pair_errors(
    settings: MaximizeSettings,
) -> list[tuple[str, str, str]]:
    """``(soft_field, hard_field, message)`` for each window with soft > hard."""
    errors = []
    for soft, hard in _MAXIMIZE_PAIRS:
        lo, hi = getattr(settings, soft), getattr(settings, hard)
        if lo > hi:
            errors.append((
                soft,
                hard,
                f"{_dotted('maximize', soft)} ({format_setting_value(lo)}) must "
                f"not exceed {_dotted('maximize', hard)} ({format_setting_value(hi)})",
            ))
    return errors


def _section_from_raw(section, name: str, cls, repairs: list[str] | None = None):
    """One section's dataclass from its raw JSON dict: per-key lenient
    (missing → default, bad type or non-finite → default, out of range →
    clamped).

    When ``repairs`` is a list, one message is appended for each raw value
    that was replaced by its default or clamped (see `_clamped`); nothing is
    logged here, so the strict `config set` path can read a section quietly.
    """
    if not isinstance(section, dict):
        return cls()
    kwargs = {
        spec.field: section[spec.json_key]
        for spec in SETTING_SPECS.values()
        if spec.section == name and spec.json_key in section
    }
    return _clamped(cls(**kwargs), name, repairs)


def _report(problems: list[str] | None, message: str) -> None:
    _logger.warning("settings.json: %s", message)
    if problems is not None:
        problems.append(message)


def _section_for_load(
    raw: dict, name: str, cls, problems: list[str] | None
):
    """`_section_from_raw` for the lenient loaders: each per-key repair is
    logged and, when ``problems`` is given, appended to it."""
    repairs: list[str] = []
    settings = _section_from_raw(raw.get(name), name, cls, repairs)
    for message in repairs:
        _report(problems, message)
    return settings


def load_maximize_settings(
    backup_root: Path, *, problems: list[str] | None = None
) -> MaximizeSettings:
    """Load the ``maximize`` section; never raises.

    Per key as `load_settings`: a wrong-typed or non-finite value becomes the
    key's default and an out-of-range one is clamped. Per window, a soft mark
    above its hard cap resets BOTH to their defaults: moving one toward the
    other would invent a threshold nobody chose. Every repair — each key
    replaced or clamped, each pair reset — is logged and, when ``problems`` is
    given, appended to it as one message; the engine's hot reload uses that to
    keep its previous values and raise a ConfigWarningEvent instead
    (spec §8.1). A whole number written as a float (``12.0`` for an int key)
    is not a repair.
    """
    raw = _read_raw(settings_path(backup_root))
    settings = _section_for_load(raw, "maximize", MaximizeSettings, problems)
    defaults = MaximizeSettings()
    for soft, hard, message in _maximize_pair_errors(settings):
        _report(problems, f"{message}; using defaults for both")
        settings = dataclasses.replace(
            settings,
            **{soft: getattr(defaults, soft), hard: getattr(defaults, hard)},
        )
    return settings


def load_prime_settings(
    backup_root: Path, *, problems: list[str] | None = None
) -> PrimeSettings:
    """Load the ``prime`` section; never raises.

    Stricter than the shared clamp in two places, both toward "off": only a
    JSON ``true`` enables priming (the shared bool clamp would read the
    string "false" as true), and a malformed ``jitterS`` reverts to the
    default so `parse_jitter_range` on a loaded value cannot fail. Like
    `load_maximize_settings`, every repair (those two and each replaced or
    clamped key) is logged and, when ``problems`` is given, appended to it
    as one message.
    """
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("prime")
    settings = _section_for_load(raw, "prime", PrimeSettings, problems)
    defaults = PrimeSettings()
    if (
        isinstance(section, dict)
        and "enabled" in section
        and not isinstance(section["enabled"], bool)
    ):
        _report(
            problems,
            f"prime.enabled must be true or false, got {section['enabled']!r}; "
            "priming stays off",
        )
        settings = dataclasses.replace(settings, enabled=False)
    try:
        parse_jitter_range(settings.jitter_s)
    except ValueError as e:
        _report(problems, f"prime.jitterS {e}; using {defaults.jitter_s}")
        settings = dataclasses.replace(settings, jitter_s=defaults.jitter_s)
    return settings


def merge_maximize_cli(settings: MaximizeSettings, args) -> MaximizeSettings:
    """Overlay ``auto --soft5h/--hard5h/--soft7d/--hard7d`` onto settings.

    Flags beat settings.json (spec §8.1) and clamp into the registry ranges
    like `merged_with_cli`. A soft mark above its hard cap after the merge
    raises ConfigError: flags are an explicit request, so the contradiction
    is reported rather than silently reset. ``args`` is any object with
    those attributes (None = not given), or None; the engine re-applies the
    same object after each hot reload.
    """
    overrides = {}
    for attr, field in MAXIMIZE_CLI_FLAGS:
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    merged = _clamped(dataclasses.replace(settings, **overrides), "maximize")
    errors = _maximize_pair_errors(merged)
    if errors:
        raise ConfigError(errors[0][2])
    return merged


def _check_fork_setting(raw: dict, spec: SettingSpec, value) -> None:
    """Strict checks `config set`/`unset` add for the cc-swap sections.

    Single-key bounds already passed `parse_setting_value`. This covers the
    two structured strings and what one key cannot know alone: a soft mark
    against its hard cap, judged against the file's other key per-key
    clamped but NOT pair-repaired, so an already-broken pair is reported
    instead of being masked by the defaults the lenient load would show.
    Only the window being edited is checked.
    """
    if spec.dotted == "prime.jitterS":
        try:
            parse_jitter_range(value)
        except ValueError as e:
            raise ConfigError(f"prime.jitterS {e}") from None
    elif spec.dotted == "maximize.planOverride" and value is not None:
        message = _plan_override_error(value)
        if message is not None:
            raise ConfigError(message)
    if spec.section != "maximize":
        return
    current = _section_from_raw(raw.get("maximize"), "maximize", MaximizeSettings)
    candidate = dataclasses.replace(current, **{spec.field: value})
    for soft, hard, message in _maximize_pair_errors(candidate):
        if spec.field in (soft, hard):
            other = hard if spec.field == soft else soft
            raise ConfigError(f"{message}; change {_dotted('maximize', other)} first")


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them).

    **Writes THROUGH a symlink, never over it.** A rename swaps a directory
    ENTRY and does not follow links, so renaming onto a symlinked path
    DETACHES the link: the write succeeds, the content is right, and the
    link target silently stops receiving updates — until something restores
    the link (a dotfiles deploy), taking every change written since with
    it. Same shape as #192/#193, which fixed ``session.py``'s own writer;
    this is the shared JSON writer. Three consequences, each deliberate:

    - A DANGLING link still writes where it points; linking a path is a
      request to write there.
    - The temp file is created beside the RESOLVED target, so the rename
      stays on one filesystem and remains atomic (beside the LINK it would
      hit EXDEV whenever the target lives on another mount).
    - The 0700 hardening stays on the directory cswap owns. Applying it to
      the resolved parent would narrow a directory belonging to something
      else, and raise ``PermissionError`` outright when that parent is not
      ours to chmod. The written file still gets 0600, and ``mkstemp``
      creates it 0600 to begin with, so the secret is never exposed.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        # `path.parent`, NOT the target's: see the docstring.
        os.chmod(path.parent, 0o700)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(target))
        if sys.platform != "win32":
            os.chmod(str(target), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
