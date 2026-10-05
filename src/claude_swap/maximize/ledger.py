"""The switch ledger: one JSON line per account switch (cc-swap fork).

``<backup root>/switches.jsonl`` answers "when did the active account change,
from which slot to which, who did it and why" across service restarts — the
free-text ``claude-swap.log`` and the service's ``auto.log`` do not.

Every switch is captured, whoever makes it, without touching upstream's
``switcher.py``: ``_perform_switch`` logs ``Switched from account X to Y``
(or ``Activated account Y ...``) on the ``claude-swap`` logger, and
:func:`install` puts a :class:`logging.Filter` on that logger that turns the
record into a ledger entry. A filter, not a handler: every new
``ClaudeAccountSwitcher`` runs ``setup_logging``, which clears the logger's
handlers but leaves its filters alone. ``cli.main`` installs it once per
process with the process's surface (``cli``/``tui``/``menubar``/``service``/
``auto``); a caller that knows more (the engine's trigger, Fleet, a re-login)
says so with :func:`switch_context` / :func:`tagged`.

A live login that changed with no recorded switch (a ``/login`` inside a
Claude Code session) is recorded too, as an ``external`` entry: when the
next switch leaves a slot other than the ledger's last destination, and by
the maximize engine when it sees the mismatch on two ticks in a row.

Entries carry slot numbers (``from``/``to``) with the accounts' display
names at the time (``fromName``/``toName``, maximize/names.py), the host
name and versions — never an email or a token (a reason is scrubbed of
anything shaped like one). The file is 0600,
appended with ``O_APPEND`` under a small lock, and rotated at
:data:`MAX_BYTES` into ``.1`` .. ``.3``.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_FILENAME = "switches.jsonl"
LOCK_FILENAME = ".switches.lock"
#: The live file is rotated once it is larger than this ...
MAX_BYTES = 1024 * 1024
#: ... into ``switches.jsonl.1`` .. ``.3`` (the oldest is dropped).
GENERATIONS = 3
SCHEMA_VERSION = 1
LOCK_TIMEOUT_S = 2.0
LOGGER_NAME = "claude-swap"

#: ``actor``: who decided the switch.
ACTOR_ENGINE = "engine"
ACTOR_USER = "user"
ACTOR_EXTERNAL = "external"
TRIGGER_MANUAL = "manual"
TRIGGER_EXTERNAL = "external-login"

_SWITCHED_RE = re.compile(r"^Switched from account (\S+) to (\S+)$")
_ACTIVATED_RE = re.compile(r"^Activated account (\S+) \(")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}")
REASON_MAX = 300

_context: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "cc_swap_switch_context", default=None
)
_defaults: dict[str, Any] = {}
_filter: "_SwitchFilter | None" = None


# -- context ---------------------------------------------------------------------


@contextlib.contextmanager
def switch_context(**fields: Any) -> Iterator[None]:
    """Tag every switch made inside the block (this thread / task only).
    Nested blocks add to (and override) the outer one's fields."""
    outer = _context.get() or {}
    token = _context.set({**outer, **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def tagged(fn: Callable[..., Any], **fields: Any) -> Callable[..., Any]:
    """``fn`` run inside :func:`switch_context` — for work handed to a
    thread worker, where the caller's context would not follow."""

    def run(*args: Any, **kwargs: Any) -> Any:
        with switch_context(**fields):
            return fn(*args, **kwargs)

    return run


#: The ``reason`` of the result ``engine_switch`` returns when it refuses.
AUTO_OFF_REFUSAL = "automatic switching was turned off"


def engine_switch(engine, number: str, trigger: str):
    """``engine.switcher.switch_to(number)`` tagged as the engine's switch
    (``autoswitch._perform`` calls this instead of ``switch_to``, under the
    state lock). A ``cc-swap auto off`` that landed after the tick read the
    state is honoured here: no switch."""
    from claude_swap.maximize.pause import auto_off, effective_auto_off

    try:
        state = engine._read_state()
    except Exception:
        state = {}
    state_path = getattr(engine, "state_path", None)
    off = effective_auto_off(state_path.parent, state) if state_path else auto_off(state)
    if off is not None:
        # `autoOff` marks the refusal so the caller reports `auto-off`, not a
        # no-op switch onto the already-active account.
        return {"switched": False, "reason": AUTO_OFF_REFUSAL, "autoOff": True}
    strategy = getattr(getattr(engine, "settings", None), "strategy", None)
    with switch_context(
        actor=ACTOR_ENGINE,
        trigger=trigger,
        strategy=strategy if isinstance(strategy, str) else None,
    ):
        return engine.switcher.switch_to(number, json_output=True)


def process_source(argv: list[str], env: Mapping[str, str] | None = None) -> str:
    """The surface this process is, from its command line."""
    env = os.environ if env is None else env
    verb = argv[0] if argv else ""
    if verb == "auto":
        return "service" if env.get("CC_SWAP_SERVICE") == "1" else "auto"
    if verb in ("", "tui", "watch", "--tui", "--watch"):
        return "tui"
    if verb in ("menubar", "--menubar"):
        return "menubar"
    return "cli"


# -- the logging hook ----------------------------------------------------------------


class _SwitchFilter(logging.Filter):
    """Records a ledger entry for each switch log line; never drops a record
    and never raises (a ledger problem must not break a switch)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _on_record(record)
        except Exception:
            pass
        return True


def install(*, source: str) -> None:
    """Record every switch this process makes (idempotent)."""
    global _filter
    _defaults.clear()
    _defaults["source"] = source
    if _filter is None:
        _filter = _SwitchFilter()
        logging.getLogger(LOGGER_NAME).addFilter(_filter)


def uninstall() -> None:
    global _filter
    if _filter is not None:
        logging.getLogger(LOGGER_NAME).removeFilter(_filter)
        _filter = None
    _defaults.clear()


def installed() -> bool:
    return _filter is not None


def _logger_root() -> Path | None:
    """The backup root: where the switcher's own ``claude-swap.log`` goes."""
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        name = getattr(handler, "baseFilename", None)
        if isinstance(name, str) and name.endswith("claude-swap.log"):
            return Path(name).parent
    return None


def _on_record(record: logging.LogRecord) -> None:
    if record.name != LOGGER_NAME or _filter is None:
        return
    message = record.getMessage()
    match = _SWITCHED_RE.match(message)
    if match is not None:
        src, dst = match.group(1), match.group(2)
    else:
        match = _ACTIVATED_RE.match(message)
        if match is None:
            return
        src, dst = None, match.group(1)
    ctx = {**_defaults, **(_context.get() or {})}
    root = ctx.pop("root", None) or _logger_root()
    if root is None:
        return
    record_switch(
        Path(root),
        from_slot=src,
        to_slot=dst,
        actor=ctx.pop("actor", ACTOR_USER),
        trigger=ctx.pop("trigger", TRIGGER_MANUAL),
        source=ctx.pop("source", "cli"),
        reason=ctx.pop("reason", None),
        strategy=ctx.pop("strategy", None),
    )


# -- entries ------------------------------------------------------------------------


def host_name() -> str:
    return socket.gethostname().split(".")[0] or "unknown"


def _slot(value: object) -> int | None:
    text = str(value) if value is not None else ""
    return int(text) if text.isdigit() else None


def scrub(text: object) -> str | None:
    """A reason fit for the ledger: no email, nothing token-shaped, bounded."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = _EMAIL_RE.sub("<email>", _TOKEN_RE.sub("<redacted>", text.strip()))
    return text[:REASON_MAX]


def _names(root: Path) -> dict[str, str]:
    """``{slot: display name}`` off ``root``'s sequence.json (``{}`` when
    unreadable)."""
    from claude_swap.maximize.names import record_names

    try:
        data = json.loads((Path(root) / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return record_names(data.get("accounts") if isinstance(data, dict) else None)


def _name(names: Mapping[str, str], slot: int | None) -> str | None:
    return (names.get(str(slot)) or None) if slot is not None else None


def _versions(root: Path) -> dict[str, str | None]:
    from claude_swap import __version__

    claude: str | None = None
    try:
        from claude_swap.maximize.prime_verify import last_seen_version

        claude = last_seen_version(root)
    except Exception:
        claude = None
    return {"ccSwap": __version__, "claude": claude}


def make_entry(
    root: Path,
    *,
    from_slot: object,
    to_slot: object,
    actor: str,
    trigger: str,
    source: str,
    reason: object = None,
    strategy: str | None = None,
    now: float | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    ts = time.time() if now is None else now
    src, dst = _slot(from_slot), _slot(to_slot)
    names = _names(root)
    return {
        "v": SCHEMA_VERSION,
        "ts": round(ts, 3),
        "at": datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "host": host or host_name(),
        "from": src,
        "to": dst,
        "fromName": _name(names, src),
        "toName": _name(names, dst),
        "actor": actor,
        "trigger": trigger,
        "source": source,
        "reason": scrub(reason),
        "strategy": strategy,
        "versions": _versions(root),
        "pid": os.getpid(),
    }


def path_for(root: Path) -> Path:
    return Path(root) / LEDGER_FILENAME


def _generations(root: Path) -> list[Path]:
    """Ledger files newest first: the live file, then ``.1`` .. ``.3``."""
    base = path_for(root)
    return [base] + [base.with_name(f"{base.name}.{n}") for n in range(1, GENERATIONS + 1)]


def _rotate(base: Path) -> None:
    for n in range(GENERATIONS - 1, 0, -1):
        older = base.with_name(f"{base.name}.{n}")
        if older.is_file():
            os.replace(older, base.with_name(f"{base.name}.{n + 1}"))
    os.replace(base, base.with_name(f"{base.name}.1"))


def _write_line(base: Path, entry: Mapping[str, Any]) -> None:
    data = (json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(base, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[bool]:
    """The ledger lock; yields False (unlocked) when it cannot be had in
    :data:`LOCK_TIMEOUT_S` — an append is still atomic, only rotation and
    the drift check are skipped."""
    from claude_swap.locking import FileLock

    lock = FileLock(Path(root) / LOCK_FILENAME, timeout=LOCK_TIMEOUT_S)
    try:
        got = lock.acquire()
    except OSError:
        got = False
    try:
        yield got
    finally:
        if got:
            lock.release()


def append(root: Path, entry: Mapping[str, Any], *, locked: bool | None = None) -> None:
    """Append one entry, rotating first when the live file is too big."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    base = path_for(root)
    if locked is None:
        with _locked(root) as got:
            append(root, entry, locked=got)
        return
    if locked:
        try:
            if base.is_file() and base.stat().st_size > MAX_BYTES:
                _rotate(base)
        except OSError:
            pass
    _write_line(base, entry)


def record_switch(
    root: Path,
    *,
    from_slot: object,
    to_slot: object,
    actor: str,
    trigger: str,
    source: str,
    reason: object = None,
    strategy: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Append a switch; first an ``external`` entry when the slot it leaves
    is not where the ledger last saw this host land (a login changed
    outside cc-swap in between)."""
    root = Path(root)
    entry = make_entry(
        root, from_slot=from_slot, to_slot=to_slot, actor=actor, trigger=trigger,
        source=source, reason=reason, strategy=strategy, now=now,
    )
    with _locked(root) as got:
        previous = last(root, host=entry["host"])
        if (
            previous is not None
            and entry["from"] is not None
            and previous.get("to") != entry["from"]
        ):
            append(root, make_entry(
                root, from_slot=previous.get("to"), to_slot=entry["from"],
                actor=ACTOR_EXTERNAL, trigger=TRIGGER_EXTERNAL, source="external",
                reason="the live login changed outside cc-swap", now=entry["ts"],
            ), locked=got)
        append(root, entry, locked=got)
    return entry


def record_external(
    root: Path, live: str | None, *, reason: str, now: float | None = None
) -> dict[str, Any] | None:
    """Record that the live login is ``live`` (None: unmanaged or logged
    out) although the ledger's last entry for this host landed elsewhere.
    Re-checked under the lock; returns the entry, or None when nothing
    changed (or there is no ledger yet to differ from)."""
    root = Path(root)
    host = host_name()
    with _locked(root) as got:
        if not got:
            return None
        previous = last(root, host=host)
        if previous is None or previous.get("to") == _slot(live):
            return None
        entry = make_entry(
            root, from_slot=previous.get("to"), to_slot=live, actor=ACTOR_EXTERNAL,
            trigger=TRIGGER_EXTERNAL, source="external", reason=reason, now=now, host=host,
        )
        append(root, entry, locked=True)
    return entry


def drift(root: Path, live: str | None) -> dict[str, Any] | None:
    """The ledger's last entry for this host when the live login is not
    where it landed, else None."""
    previous = last(root, host=host_name())
    if previous is None or previous.get("to") == _slot(live):
        return None
    return previous


# -- reading ------------------------------------------------------------------------


def _parse(line: str) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) and "ts" in data else None


def read(root: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Entries oldest first; with ``limit``, only the newest ``limit``
    (reading no more generations than that needs). Bad lines are skipped."""
    chunks: list[list[dict[str, Any]]] = []
    count = 0
    for path in _generations(Path(root)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows = [e for e in (_parse(line) for line in text.splitlines()) if e is not None]
        chunks.append(rows)
        count += len(rows)
        if limit is not None and count >= limit:
            break
    out = [e for rows in reversed(chunks) for e in rows]
    return out[-limit:] if limit is not None and limit > 0 else ([] if limit == 0 else out)


def last(root: Path, *, host: str | None = None) -> dict[str, Any] | None:
    """The newest entry (for ``host`` when given)."""
    for path in _generations(Path(root)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in reversed(text.splitlines()):
            entry = _parse(line)
            if entry is not None and (host is None or entry.get("host") == host):
                return entry
    return None
