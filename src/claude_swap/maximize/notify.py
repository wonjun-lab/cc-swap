"""Desktop notifications from the engine (cc-swap fork; ``cc-swap notify``).

What matters while you are not looking at the TUI:

* ``switch`` — the engine switched accounts (with its trigger);
* ``relogin`` — an account needs a re-login (its refresh token is dead, or
  its login passed its deadline);
* ``login-expiring`` — a login ends within 24 hours (once per account per
  day);
* ``prime-paused`` — priming paused after a Claude Code update;
* ``keychain`` — the live login has been unreadable (the Keychain hold) for
  over 15 minutes, so nothing switches.

**Who sends them.** The engine: the service, a terminal ``cc-swap auto``, a
TUI or menu bar engine — never a dry run. ``AutoSwitchEngine._emit`` hands
every event to :class:`EngineNotifier` (attached by
``engine_hook.attach_maximize``), and each maximize tick hands it the
logins and the priming guard (:meth:`EngineNotifier.tick`).

**How.** macOS runs ``osascript -e 'display notification …'`` (the text goes
in as arguments, never spliced into the script); Linux runs ``notify-send``
when it is installed; anything else sends nothing. ``CC_SWAP_NOTIFY=0`` turns
delivery off for a process. ``notify.enabled`` (default true) and one
``notify.*`` switch per event turn them off for good.

**Dedupe and rate limit.** ``<backup root>/notify_state.json`` remembers when
each key was last sent (the event plus its account, e.g.
``login-expiring:3``) and the recent sends: a key is not repeated within its
own interval, and at most :data:`RATE_MAX` go out per :data:`RATE_WINDOW_S`
in each of two rooms — alerts (switch, keychain) and reminders — so a burst
of reminders never crowds out a switch.

**Privacy.** A notification names accounts by slot number and short name
(the alias, else the part of the address before the ``@``) — never an
email, never a token; every text is scrubbed of anything shaped like either
before it leaves (:func:`scrub`).

**Never in the way.** Every call is wrapped (a failure is logged at debug
level and dropped) and each delivery is time-bounded (:data:`TIMEOUT_S`), so
a notification never breaks a tick.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from claude_swap.json_output import (
    USAGE_LOGIN_EXPIRED,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
)
from claude_swap.maximize.hold import record_names

_logger = logging.getLogger("claude-swap")

#: ``CC_SWAP_NOTIFY=0`` sends nothing from this process.
ENV = "CC_SWAP_NOTIFY"
STATE_FILENAME = "notify_state.json"
#: One delivery never takes longer than this.
TIMEOUT_S = 2.0
#: At most this many notifications per room …
RATE_MAX = 6
#: … per this many seconds.
RATE_WINDOW_S = 600.0
#: Rate-limit rooms: a switch or a stuck Keychain is never crowded out by a
#: burst of reminders (re-logins, expiring logins, paused priming).
ALERTS = frozenset({"switch", "keychain"})


def room(event: str) -> str:
    return "alerts" if event in ALERTS else "reminders"
#: Keys not sent for this long are forgotten.
KEEP_S = 8 * 86400.0
DAY_S = 86400.0
#: ``login-expiring``: a login this close to its deadline.
EXPIRING_S = 86400.0
#: ``keychain``: the unreadable-login hold has lasted this long
#: (``autoswitch.READ_HOLD_NAG_S``).
KEYCHAIN_HOLD_S = 15 * 60.0
#: How often each kind may repeat for the same key.
EVERY_S: dict[str, float] = {
    "switch": 30.0,
    "relogin": DAY_S,
    "login-expiring": DAY_S,
    "prime-paused": DAY_S,
    "keychain": 2 * 3600.0,
}
#: Event → its ``NotifySettings`` switch.
TOGGLES: dict[str, str] = {
    "switch": "switch",
    "relogin": "relogin",
    "login-expiring": "login_expiring",
    "prime-paused": "prime_paused",
    "keychain": "keychain",
}
#: A process weighs the same key again at most this often (the state file
#: is read only then).
_RECHECK_S = 600.0

#: Usage sentinels that mean a re-login (``fleet._SENTINEL_LOGIN``'s
#: "relogin" ones), and why.
_DEAD_SENTINELS: dict[str, str] = {
    USAGE_RELOGIN_REQUIRED: "its refresh token is dead",
    USAGE_LOGIN_EXPIRED: "its login expired",
    USAGE_NO_CREDENTIALS: "it has no stored login",
}
_DEAD_QUARANTINE: dict[str, str] = {
    "invalid_grant": "its refresh token is dead",
    "login_expired": "its login expired",
}


@dataclass(frozen=True)
class Note:
    """One notification: ``key`` dedupes it, ``event`` picks its switch."""

    event: str
    key: str
    title: str
    body: str

    @property
    def every_s(self) -> float:
        return EVERY_S.get(self.event, DAY_S)


# -- text ---------------------------------------------------------------------------

#: A whole address (a dot after the @). A short name that tells two
#: accounts apart (``jordan.lee@uni``, maximize/names.py) is not one.
_EMAIL_RE = re.compile(r"[^\s@]+@[\w-]+(?:\.[\w-]+)+")
_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}|\b(?:sk|rt)-[A-Za-z0-9_\-]{12,}")
TEXT_MAX = 200


def scrub(text: str) -> str:
    """A text fit for a notification: nothing shaped like an email or a
    token, one line, bounded."""
    text = _TOKEN_RE.sub("…", str(text))
    text = _EMAIL_RE.sub("…", text)
    text = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text)  # NUL, BEL …
    text = " ".join(text.split())
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"


def label(number: object, names: Mapping[str, str]) -> str:
    """``#3 old`` (the short name), or ``#3``."""
    slot = str(number)
    name = names.get(slot, "")
    return f"#{slot} {name}" if name else f"#{slot}"


def switch_note(src: object, dst: object, trigger: str, why: str | None,
                names: Mapping[str, str]) -> Note:
    body = f"from {label(src, names)} · {trigger}" if src is not None else f"({trigger})"
    if why:
        body += f": {why}"
    return Note("switch", f"switch:{src}>{dst}", f"cc-swap: switched to {label(dst, names)}", body)


def relogin_note(slot: str, cause: str, names: Mapping[str, str]) -> Note:
    return Note(
        "relogin", f"relogin:{slot}",
        f"cc-swap: {label(slot, names)} needs a re-login",
        f"{cause} — Fleet: select it, press r (or claude → /login → cc-swap add)",
    )


def expiring_note(slot: str, deadline: float, now: float, names: Mapping[str, str]) -> Note:
    from claude_swap import oauth

    return Note(
        "login-expiring", f"login-expiring:{slot}",
        f"cc-swap: {label(slot, names)} login ends in {oauth.login_countdown(deadline - now)}",
        f"at {oauth.local_clock(deadline)}; a fresh login now starts a new deadline — "
        "Fleet: select it, press r",
    )


def prime_paused_note(note: str) -> Note:
    return Note(
        "prime-paused", f"prime-paused:{note}", "cc-swap: priming paused",
        f"priming {note} — after a Claude Code update; run cc-swap prime verify",
    )


def keychain_note(slot: str | None, minutes: int) -> Note:
    who = f"#{slot}'s live login" if slot else "the live login"
    return Note(
        "keychain", "keychain", f"cc-swap: Keychain unreadable for {minutes} min",
        f"{who} cannot be read, so nothing switches. Unlock the login keychain, "
        "or restart cc-swap",
    )


# -- delivery --------------------------------------------------------------------------


@dataclass(frozen=True)
class Backend:
    """How this machine shows a notification: ``send(title, body)``."""

    name: str
    send: Callable[[str, str], bool]


def _run(argv: list[str]) -> bool:
    try:
        result = subprocess.run(
            argv, capture_output=True, timeout=TIMEOUT_S, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError):  # ValueError: a NUL byte
        return False
    return result.returncode == 0


def osascript_argv(path: str, title: str, body: str) -> list[str]:
    """``osascript -e 'display notification …'`` with the text as
    arguments (never spliced into the script)."""
    return [
        path,
        "-e", "on run argv",
        "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e", "end run",
        title, body,
    ]


def system_backend(
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Backend | None:
    """macOS ``osascript``, Linux ``notify-send`` when installed, else None
    (and None under ``CC_SWAP_NOTIFY=0``)."""
    env = os.environ if environ is None else environ
    if env.get(ENV, "").strip() == "0":
        return None
    plat = sys.platform if platform is None else platform
    if plat == "darwin":
        path = which("osascript")
        if path:
            return Backend("osascript", lambda t, b: _run(osascript_argv(path, t, b)))
        return None
    if plat.startswith("linux"):
        path = which("notify-send")
        if path:
            return Backend("notify-send", lambda t, b: _run([path, t, b]))
    return None


def state_path(root: Path) -> Path:
    return Path(root) / STATE_FILENAME


def _stamps(value: object) -> list[float]:
    return [
        float(t) for t in (value if isinstance(value, list) else ())
        if isinstance(t, (int, float)) and not isinstance(t, bool)
    ]


def read_state(root: Path) -> dict:
    """``{"sent": {key: ts}, "recent": {room: [ts, …]}}`` (empty when
    unreadable; an older file's single ``recent`` list is the reminders')."""
    try:
        raw = json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    sent = raw.get("sent")
    recent = raw.get("recent")
    rooms = recent if isinstance(recent, dict) else {"reminders": recent}
    return {
        "sent": {
            str(k): float(v) for k, v in (sent.items() if isinstance(sent, dict) else ())
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        },
        "recent": {name: _stamps(rooms.get(name)) for name in ("alerts", "reminders")},
    }


def _write_state(root: Path, sent: dict[str, float], recent: dict[str, list[float]]) -> None:
    from claude_swap.settings import atomic_write_json

    atomic_write_json(state_path(root), {"schemaVersion": 2, "sent": sent, "recent": recent})


def allowed(note: Note, settings) -> bool:
    """``notify.enabled`` and the note's own switch."""
    return bool(settings.enabled) and bool(getattr(settings, TOGGLES.get(note.event, ""), True))


def deliver(
    root: Path,
    note: Note,
    *,
    now: float,
    backend: Backend | None = None,
    settings=None,
) -> bool:
    """Send ``note`` unless it is switched off, was sent within its
    interval, or the rate limit is reached; whether it went out. The
    attempt is recorded either way it was tried. Never raises."""
    try:
        if settings is None:
            from claude_swap.settings import load_notify_settings

            settings = load_notify_settings(Path(root))
        if not allowed(note, settings):
            return False
        backend = backend if backend is not None else system_backend()
        if backend is None:
            return False
        state = read_state(Path(root))
        sent = {k: t for k, t in state["sent"].items() if 0 <= now - t < KEEP_S}
        recent = {
            name: [t for t in stamps if 0 <= now - t < RATE_WINDOW_S]
            for name, stamps in state["recent"].items()
        }
        last = sent.get(note.key)
        if last is not None and now - last < note.every_s:
            return False
        mine = recent[room(note.event)]
        if len(mine) >= RATE_MAX:
            _logger.debug("notification rate-limited: %s", note.event)
            return False
        sent[note.key] = now
        mine.append(now)
        _write_state(Path(root), sent, recent)
        return bool(backend.send(scrub(note.title), scrub(note.body)))
    except Exception as e:  # a notification must never break a tick
        _logger.debug("notification failed: %s", type(e).__name__)
        return False


# -- the engine ----------------------------------------------------------------------------


def relogin_slots(
    records: Mapping[str, Mapping],
    usage: Mapping[str, object],
    quarantine: Mapping[str, object],
    deadlines: Mapping[str, float],
    now: float,
) -> dict[str, str]:
    """``{slot: why}`` for every account that needs a re-login: a dead-login
    usage sentinel, a quarantine for a dead or expired login, or a login
    past its deadline. API keys never do."""
    out: dict[str, str] = {}
    for number, record in records.items():
        slot = str(number)
        if isinstance(record, Mapping) and record.get("kind") == "api_key":
            continue
        value = usage.get(slot)
        q = quarantine.get(slot)
        reason = q.get("reason") if isinstance(q, Mapping) else None
        if isinstance(value, str) and value in _DEAD_SENTINELS:
            out[slot] = _DEAD_SENTINELS[value]
        elif isinstance(reason, str) and reason in _DEAD_QUARANTINE:
            out[slot] = _DEAD_QUARANTINE[reason]
        elif slot in deadlines and deadlines[slot] <= now:
            out[slot] = "its login expired"
    return out


class EngineNotifier:
    """One engine's notifications. The engine calls it with every event it
    emits (``AutoSwitchEngine._emit``) and once per maximize tick
    (:meth:`tick`). Never raises; sends nothing for a dry run."""

    def __init__(self, engine, *, backend: Backend | None = None) -> None:
        self.engine = engine
        self.backend = backend
        self._weighed: dict[str, float] = {}

    @property
    def root(self) -> Path:
        return Path(self.engine.switcher.backup_dir)

    def _names(self) -> dict[str, str]:
        try:
            data = self.engine.switcher._get_sequence_data() or {}
        except Exception:
            return {}
        return record_names(data.get("accounts"))

    def send(self, note: Note, now: float) -> bool:
        last = self._weighed.get(note.key)
        if last is not None and 0 <= now - last < min(note.every_s, _RECHECK_S):
            return False
        self._weighed[note.key] = now
        return deliver(self.root, note, now=now, backend=self.backend)

    # -- events --------------------------------------------------------------------------

    def __call__(self, event) -> None:
        if getattr(self.engine, "dry_run", False):
            return
        try:
            for note in self._from_event(event):
                self.send(note, self.engine.clock())
        except Exception as e:
            _logger.debug("notification skipped: %s", type(e).__name__)

    def _from_event(self, event) -> list[Note]:
        kind = getattr(event, "kind", None)
        if kind == "switch" and not getattr(event, "dry_run", False):
            src = (event.from_ref or {}).get("number")
            dst = (event.to_ref or {}).get("number")
            return [switch_note(src, dst, event.trigger, self._switch_why(dst), self._names())]
        if kind == "no-switch" and getattr(event, "reason", "") == "active-credential-unreadable":
            since = getattr(self.engine, "_read_hold_since", None)
            now = self.engine.clock()
            if since is not None and now - since >= KEYCHAIN_HOLD_S:
                try:
                    slot = self.engine.switcher.current_account_number()
                except Exception:
                    slot = None
                return [keychain_note(slot, int((now - since) // 60))]
        return []

    def _switch_why(self, dst: object) -> str | None:
        """The head of the maximize reason behind a switch to ``dst``."""
        rt = getattr(self.engine, "_maximize_runtime", None)
        decision = getattr(rt, "last_decision", None)
        target = getattr(decision, "target", None)
        reason = getattr(decision, "reason", None)
        if target is None or str(target) != str(dst) or not isinstance(reason, str):
            return None
        return reason.split(";", 1)[0].strip() or None

    # -- ticks ---------------------------------------------------------------------------

    def tick(
        self,
        *,
        records: Mapping[str, Mapping],
        usage: Mapping[str, object],
        state: Mapping,
        deadlines: Mapping[str, float],
        prime_note: str | None,
        now: float,
    ) -> None:
        """Logins that need a re-login or end within a day, and priming
        paused after a Claude Code update."""
        if getattr(self.engine, "dry_run", False):
            return
        try:
            names = self._names()
            quarantine = state.get("quarantine") if isinstance(state, Mapping) else None
            dead = relogin_slots(
                records, usage, quarantine if isinstance(quarantine, Mapping) else {},
                deadlines, now,
            )
            for slot, why in sorted(dead.items()):
                self.send(relogin_note(slot, why, names), now)
            for slot, deadline in sorted(deadlines.items()):
                if slot not in dead and 0 < deadline - now < EXPIRING_S:
                    self.send(expiring_note(slot, deadline, now, names), now)
            if prime_note:
                self.send(prime_paused_note(prime_note), now)
        except Exception as e:
            _logger.debug("notification skipped: %s", type(e).__name__)


# -- `cc-swap notify` -----------------------------------------------------------------------


def notify_command(argv: list[str]) -> None:
    """``cc-swap notify [status|test] [--json]``."""
    import argparse

    from claude_swap.paths import get_backup_root
    from claude_swap.settings import load_notify_settings

    parser = argparse.ArgumentParser(
        prog="cc-swap notify",
        description=(
            "Desktop notifications from the engine: an account switch, a re-login "
            "needed, a login ending within 24 hours, priming paused after a Claude "
            "Code update, and a Keychain that stays unreadable. macOS uses osascript, "
            "Linux notify-send. Turn them off with cc-swap config set notify.enabled "
            "false, or one kind with notify.switch / notify.relogin / "
            "notify.loginExpiring / notify.primePaused / notify.keychain."
        ),
    )
    parser.add_argument("action", nargs="?", choices=("status", "test"), default="status")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)
    root = get_backup_root()
    settings = load_notify_settings(root)
    backend = system_backend()
    toggles = {event: bool(getattr(settings, field)) for event, field in TOGGLES.items()}
    if args.action == "test":
        sent = bool(backend is not None and backend.send(
            "cc-swap: test notification",
            "Notifications work: switches, re-logins, expiring logins, paused "
            "priming and a stuck Keychain show up here.",
        ))
        if args.json:
            print(json.dumps({
                "schemaVersion": 1, "sent": sent, "backend": backend.name if backend else None,
                "enabled": settings.enabled,
            }))
            sys.exit(0 if sent else 1)
        if sent:
            print(f"Sent a test notification (via {backend.name}).")
        elif backend is None:
            print(_no_backend_text())
        else:
            print(f"Could not send a test notification via {backend.name}.")
        if not settings.enabled:
            print("notify.enabled is false, so the engine sends none: "
                  "cc-swap config set notify.enabled true")
        sys.exit(0 if sent else 1)
    state = read_state(root)
    if args.json:
        print(json.dumps({
            "schemaVersion": 1, "enabled": settings.enabled, "events": toggles,
            "backend": backend.name if backend else None,
            "lastSent": max(state["sent"].values(), default=None),
        }))
        sys.exit(0)
    print(f"Notifications are {'ON' if settings.enabled else 'OFF'} (notify.enabled)"
          + (f", sent via {backend.name}." if backend else "."))
    if backend is None:
        print(_no_backend_text())
    print("  " + " · ".join(f"{event} {'on' if on else 'off'}" for event, on in toggles.items()))
    last = max(state["sent"].values(), default=None)
    if last is not None:
        print(f"  last sent {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}")
    sys.exit(0)


def _no_backend_text() -> str:
    if os.environ.get(ENV, "").strip() == "0":
        return f"Nothing is sent from this process: {ENV}=0."
    if sys.platform.startswith("linux"):
        return "No notifier on this machine: install notify-send (libnotify-bin / libnotify)."
    if sys.platform == "darwin":
        return "No notifier on this machine: osascript was not found."
    return "No notifier on this platform: cc-swap notifies on macOS and Linux only."
