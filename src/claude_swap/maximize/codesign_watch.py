"""Code-signing kills of ``claude``: seen live, and found afterwards
(cc-swap fork, macOS only).

The 2026-10-04 incident left 59 crash reports, all ``SIGKILL (Code
Signature Invalid)`` with the kernel's ``load code signature error 2 for
file "2.1.289"``, in two episodes that each began 20–40 s after a ``claude``
cc-swap had launched (a prime, a re-login). Claude Code's updater writes by
tmp + rename and ``-p`` runs no updater, so the mechanism is unknown, and
cc-swap's launches may be the trigger or a coincidence. This module
collects the evidence for next time:

* **Live** (:class:`Watcher`, while an engine runs): a ``log stream --style
  ndjson`` child filtered to code-signature messages; every matching line
  (time, pid, process, message) goes to ``<backup root>/codesign-events.jsonl``
  (rotated like ``claude-exec.jsonl``). A kernel message naming the current
  ``claude`` file marks it killed by the OS (``claude_exec.mark_killed_by_os``:
  priming pauses, one notification per binary) — even when the process it
  killed was the user's own ``claude``, not one of cc-swap's. The child is
  restarted with a backoff when it dies; the tick only polls it.
* **Afterwards** (:func:`scan_crash_reports`, on engine start, hourly, and
  in ``cc-swap doctor``): ``~/Library/Logs/DiagnosticReports/*.ips`` of the
  last week whose process lives under ``~/.local/share/claude/versions`` and
  was terminated for an invalid code signature. Doctor groups them per
  version with the latest ``claude`` cc-swap launched before the first kill
  (from ``claude-exec.jsonl``), so the correlation is on the screen.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_swap.maximize import claude_exec

_logger = logging.getLogger("claude-swap")

EVENTS_FILENAME = "codesign-events.jsonl"
LOG = "/usr/bin/log"
#: Catches the kernel's ``load code signature error N for file "…"``, AMFI's
#: and taskgated's code-signature lines, and CODESIGNING terminations.
PREDICATE = (
    'eventMessage CONTAINS[c] "code signature" '
    'OR (process == "kernel" AND eventMessage CONTAINS "CODESIGNING") '
    'OR eventMessage CONTAINS "Code Signature Invalid"'
)
#: A dead ``log stream`` is restarted after this long, doubling up to the
#: maximum; one that stayed up :data:`HEALTHY_S` resets it.
BACKOFF_MIN_S = 30.0
BACKOFF_MAX_S = 3600.0
HEALTHY_S = 600.0
STOP_GRACE_S = 2.0
#: Crash reports are looked at on the first tick and then this often.
SCAN_EVERY_S = 3600.0
REPORT_MAX_AGE_S = 7 * 86400.0
REPORT_MAX_FILES = 500
REPORT_MAX_BYTES = 4 * 1024 * 1024
MESSAGE_MAX = 1000
VERSIONS_DIR = Path(".local") / "share" / "claude" / "versions"
INVALID = "Code Signature Invalid"

_KERNEL_FILE_RE = re.compile(r'load code signature error \d+ for file "([^"]+)"')
_VERSION_PATH_RE = re.compile(r"(/[^\s\"']*/\.local/share/claude/versions/[^\s\"'),]+)")


def _is_macos() -> bool:
    return sys.platform == "darwin"


def stream_argv() -> list[str]:
    return [LOG, "stream", "--style", "ndjson", "--predicate", PREDICATE]


def _spawn(argv: list[str]) -> subprocess.Popen | None:
    """Start ``log stream`` (tests replace this; none ever runs the real one)."""
    try:
        return subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, errors="replace", bufsize=1,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return None


# -- live events --------------------------------------------------------------------------


def parse_event(line: str) -> dict[str, Any] | None:
    """One ``log stream --style ndjson`` line as an event record, or None
    (the ``Filtering the log data …`` banner, anything that is not one)."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or "eventMessage" not in data:
        return None
    image = str(data.get("processImagePath") or "")
    return {
        "kind": "log-event",
        "ts": data.get("timestamp"),
        "at": time.time(),
        "pid": data.get("processID"),
        "process": os.path.basename(image) or data.get("process"),
        "processImagePath": image or None,
        "subsystem": data.get("subsystem") or None,
        "category": data.get("category") or None,
        "message": str(data.get("eventMessage") or "")[:MESSAGE_MAX],
    }


def killed_file(message: str) -> str | None:
    """The file a code-signing message says was refused: the kernel's
    ``for file "2.1.289"``, or a ``…/.local/share/claude/versions/…`` path."""
    match = _KERNEL_FILE_RE.search(message or "")
    if match:
        return match.group(1)
    match = _VERSION_PATH_RE.search(message or "")
    if match and ("invalid" in message.lower() or "error" in message.lower()):
        return match.group(1)
    return None


def names_binary(token: str, binary: claude_exec.Binary) -> bool:
    """Whether a message's file names ``binary`` (the kernel gives only the
    file name, ``2.1.289``)."""
    if binary.real is None or not token:
        return False
    return token == binary.real or os.path.basename(token) == os.path.basename(binary.real)


# -- crash reports ------------------------------------------------------------------------


@dataclass(frozen=True)
class CrashKill:
    file: str
    proc_path: str
    version: str
    pid: int | None
    at: float


def reports_dir(home: Path) -> Path:
    return Path(home) / "Library" / "Logs" / "DiagnosticReports"


def _report_time(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(value.strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def parse_report(path: Path) -> CrashKill | None:
    """A ``.ips`` crash report (a JSON header line, then a JSON body) of a
    process killed for an invalid code signature, or None."""
    try:
        if path.stat().st_size > REPORT_MAX_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    head, _, rest = text.partition("\n")
    try:
        header = json.loads(head)
        body = json.loads(rest) if rest.strip() else {}
    except ValueError:
        return None
    if not isinstance(header, dict) or not isinstance(body, dict):
        return None
    exception = body.get("exception") if isinstance(body.get("exception"), dict) else {}
    termination = body.get("termination") if isinstance(body.get("termination"), dict) else {}
    signal_text = str(exception.get("signal") or "")
    indicator = str(termination.get("indicator") or "")
    if not (
        INVALID in signal_text or INVALID in indicator
        or termination.get("namespace") == "CODESIGNING"
    ):
        return None
    proc_path = str(body.get("procPath") or "")
    if not proc_path:
        return None
    at = (
        _report_time(header.get("timestamp")) or _report_time(body.get("captureTime"))
        or mtime
    )
    pid = body.get("pid")
    return CrashKill(
        path.name, proc_path, os.path.basename(proc_path),
        pid if isinstance(pid, int) and not isinstance(pid, bool) else None, at,
    )


def scan_crash_reports(
    home: Path, now: float, *, max_age_s: float = REPORT_MAX_AGE_S
) -> list[CrashKill]:
    """Code-signing kills of a native ``claude`` (a process under
    ``~/.local/share/claude/versions``) in the last ``max_age_s``, oldest
    first. Never raises."""
    prefix = str(Path(home) / VERSIONS_DIR) + os.sep
    try:
        files = [
            (p, p.stat().st_mtime) for p in reports_dir(home).glob("*.ips") if p.is_file()
        ]
    except OSError:
        return []
    files = [(p, m) for p, m in files if 0 <= now - m <= max_age_s or m > now]
    files.sort(key=lambda pm: pm[1], reverse=True)
    out = []
    for path, _m in files[:REPORT_MAX_FILES]:
        kill = parse_report(path)
        if kill is not None and kill.proc_path.startswith(prefix):
            out.append(kill)
    return sorted(out, key=lambda k: k.at)


def episodes(kills: Iterable[CrashKill]) -> list[dict[str, Any]]:
    """Per version: ``{"version", "procPath", "count", "first", "last"}``,
    oldest first."""
    by: dict[str, dict[str, Any]] = {}
    for k in kills:
        e = by.setdefault(k.version, {
            "version": k.version, "procPath": k.proc_path, "count": 0,
            "first": k.at, "last": k.at,
        })
        e["count"] += 1
        e["first"] = min(e["first"], k.at)
        e["last"] = max(e["last"], k.at)
    return sorted(by.values(), key=lambda e: e["first"])


# -- the engine's watcher ------------------------------------------------------------------


class Watcher:
    """``log stream`` + crash-report scans for one engine. :meth:`tick` is
    cheap and never raises or blocks; :meth:`stop` ends the child."""

    def __init__(
        self,
        root: Path,
        claude_path: Callable[[], str | None],
        *,
        home: Path | None = None,
        clock: Callable[[], float] = time.time,
        spawn: Callable[[list[str]], Any] | None = None,
    ):
        self.root = Path(root)
        self._claude_path = claude_path
        self.home = Path.home() if home is None else Path(home)
        self._clock = clock
        self._spawn = spawn
        self._proc: Any = None
        self._reader: threading.Thread | None = None
        self._started_at: float | None = None
        self._backoff = BACKOFF_MIN_S
        self._next_start = 0.0
        self._next_scan = 0.0
        self._seen: set[str] = set()
        self.starts = 0

    # -- the child --------------------------------------------------------------------

    def tick(self) -> None:
        try:
            now = self._clock()
            self._keep_streaming(now)
            if now >= self._next_scan:
                self._next_scan = now + SCAN_EVERY_S
                self.scan(now)
        except Exception as e:  # evidence gathering never breaks a tick
            _logger.debug("codesign watch: %s", type(e).__name__)

    def _keep_streaming(self, now: float) -> None:
        proc = self._proc
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                if self._started_at is not None and now - self._started_at >= HEALTHY_S:
                    self._backoff = BACKOFF_MIN_S
                return
            _logger.warning(
                "codesign watch: log stream exited (%s); restarting in %.0fs", rc, self._backoff,
            )
            self._proc = None
            self._schedule_retry(now)
        if now < self._next_start:
            return
        spawn = self._spawn or _spawn
        proc = spawn(stream_argv())
        if proc is None:
            _logger.warning("codesign watch: could not start log stream")
            self._schedule_retry(now)
            return
        self.starts += 1
        self._proc = proc
        self._started_at = now
        self._reader = threading.Thread(
            target=self._read, args=(proc,), name="cc-swap-codesign-watch", daemon=True,
        )
        self._reader.start()

    def _schedule_retry(self, now: float) -> None:
        self._next_start = now + self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)

    def _read(self, proc) -> None:
        try:
            for line in proc.stdout:
                self.handle_line(line)
        except Exception as e:
            _logger.debug("codesign watch: reader stopped: %s", type(e).__name__)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=STOP_GRACE_S)
        except Exception:
            pass
        if self._reader is not None:
            self._reader.join(timeout=STOP_GRACE_S)

    # -- what it sees -------------------------------------------------------------------

    def _claude(self) -> str | None:
        try:
            return self._claude_path()
        except Exception:
            return None

    def handle_line(self, line: str) -> None:
        event = parse_event(line)
        if event is None:
            return
        claude_exec.append_jsonl(self.root, EVENTS_FILENAME, event)
        token = killed_file(event["message"])
        claude = self._claude() if token else None
        if not claude or not names_binary(token, claude_exec.stat_binary(claude)):
            return
        pid = event.get("pid")
        claude_exec.mark_killed_by_os(
            self.root, claude, source="log stream", at=event["at"], detail=event["message"],
            pid=pid if isinstance(pid, int) else None,
        )

    def scan(self, now: float) -> list[CrashKill]:
        """New crash reports into ``codesign-events.jsonl``; the latest kill
        of the current ``claude`` file marks it."""
        kills = scan_crash_reports(self.home, now)
        new = [k for k in kills if k.file not in self._seen]
        self._seen.update(k.file for k in kills)
        for k in new:
            claude_exec.append_jsonl(self.root, EVENTS_FILENAME, {
                "kind": "crash-report", "at": k.at, "ts": claude_exec._iso(k.at),
                "pid": k.pid, "process": k.version, "processImagePath": k.proc_path,
                "message": f"{INVALID} ({k.file})",
            })
        if new:
            _logger.warning(
                "codesign watch: %d new code-signing kill report(s) of claude (%s)",
                len(new), ", ".join(sorted({k.version for k in new})),
            )
        claude = self._claude()
        if claude and kills:
            real = claude_exec.stat_binary(claude).real
            mine = [k for k in kills if k.proc_path == real]
            if mine:
                k = mine[-1]
                claude_exec.mark_killed_by_os(
                    self.root, claude, source="crash report", at=k.at, detail=k.file, pid=k.pid,
                )
        return new


def for_engine(engine) -> Watcher | None:
    """The watcher of a live engine on macOS, or None."""
    if getattr(engine, "dry_run", False) or not _is_macos():
        return None
    root = Path(engine.switcher.backup_dir)

    def claude_path() -> str | None:
        from claude_swap.maximize.primer import resolve_claude_path
        from claude_swap.settings import load_prime_settings

        return resolve_claude_path(load_prime_settings(root).claude_path)

    return Watcher(root, claude_path)
