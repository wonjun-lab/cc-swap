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
  ndjson`` child filtered to code-signature messages and AppleSystemPolicy's
  ``provenance sandbox`` lines; every kernel line and every line naming
  claude (:func:`relevant`; time, pid, process, message — a provenance line
  only when it names the claude versions directory) goes to ``<backup
  root>/codesign-events.jsonl`` (rotated like ``claude-exec.jsonl``). A
  kernel message naming the current ``claude`` file is a kill cc-swap did
  not launch (its own launches see their SIGKILL themselves): evidence,
  with the provenance line that came just before it
  (``claude_exec.note_external_kill``), and the engine probes ``claude
  --version`` itself a little later (``claude_exec.probe_external``, run off
  the tick). Only a probe the OS kills pauses priming. The child is
  restarted with a backoff when it dies; the tick only polls it.
* **Afterwards** (:func:`scan_crash_reports`, on engine start, hourly, and
  in ``cc-swap doctor``): ``~/Library/Logs/DiagnosticReports/*.ips`` of the
  last week whose process lives under ``~/.local/share/claude/versions``
  (macOS anonymizes it to ``/Users/USER/*/<file>``; the file name then has
  to be one there) and was terminated for an invalid code signature. A
  report of the current file is evidence with its launching app, or — when
  its pid is a ``claude`` cc-swap ran — that run's kill. Doctor groups them
  per version with the app that launched them
  (``parentProc``/``responsibleProc``) and the latest ``claude`` cc-swap
  launched before the first kill (from ``claude-exec.jsonl``), so the
  correlation is on the screen.

2026-10-04 22:24–22:26: 37 kills of ``claude`` launched by T3 Code (a GUI
app), each after ``ASP: Unable to apply provenance sandbox``, while cc-swap
had launched nothing for 20 minutes and ``claude --version`` ran fine from a
shell. 0.5.3 paused priming on them; now they are evidence until a probe of
cc-swap's own says otherwise.
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
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_swap.maximize import claude_exec

_logger = logging.getLogger("claude-swap")

EVENTS_FILENAME = "codesign-events.jsonl"
LOG = "/usr/bin/log"
#: Catches the kernel's ``load code signature error N for file "…"``, AMFI's
#: and taskgated's code-signature lines, CODESIGNING terminations, and
#: AppleSystemPolicy's ``ASP: Unable to apply provenance sandbox: <err>,
#: <pid>, <path>`` (:func:`relevant` keeps only those naming claude).
PREDICATE = (
    'eventMessage CONTAINS[c] "code signature" '
    'OR (process == "kernel" AND eventMessage CONTAINS "CODESIGNING") '
    'OR eventMessage CONTAINS "Code Signature Invalid" '
    'OR eventMessage CONTAINS "provenance sandbox"'
)
PROVENANCE = "provenance sandbox"
#: A provenance line this recent goes with the kill line after it.
PROVENANCE_WINDOW_S = 10.0
#: A dead ``log stream`` is restarted after this long, doubling up to the
#: maximum; one that stayed up :data:`HEALTHY_S` resets it.
BACKOFF_MIN_S = 30.0
BACKOFF_MAX_S = 3600.0
HEALTHY_S = 600.0
STOP_GRACE_S = 2.0
#: ``log stream --timeout``: the child ends itself this often (and is
#: restarted at once), so one orphaned by an engine killed outright never
#: outlives it by more than this.
STREAM_TIMEOUT = "1h"
STREAM_TIMEOUT_S = 3600.0
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
_PROVENANCE_RE = re.compile(r"provenance sandbox:\s*(-?\d+),\s*(\d+),")
CLAUDE_VERSIONS = "/.local/share/claude/versions/"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def stream_argv() -> list[str]:
    return [
        LOG, "stream", "--style", "ndjson", "--timeout", STREAM_TIMEOUT,
        "--predicate", PREDICATE,
    ]


def _spawn(argv: list[str]) -> subprocess.Popen | None:
    """Start ``log stream`` (tests replace this; none ever runs the real one).
    In the engine's own process group, so a Ctrl-C or a service stop that
    signals the group ends it too; ``--timeout`` bounds an orphan."""
    try:
        return subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, errors="replace", bufsize=1,
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
    """The file a code-signing message names: the kernel's ``for file
    "2.1.289"``, or a ``…/.local/share/claude/versions/…`` path. Evidence
    for the jsonl; only :func:`kill_target` decides a kill."""
    match = _KERNEL_FILE_RE.search(message or "")
    if match:
        return match.group(1)
    match = _VERSION_PATH_RE.search(message or "")
    return match.group(1) if match else None


def kill_target(event: dict[str, Any]) -> str | None:
    """The file a line says the KERNEL refused to run, or None: only the
    kernel's ``load code signature error … for file "…"`` and its
    CODESIGNING terminations naming a claude path count. AMFI, taskgated
    and syspolicyd diagnostics stay evidence only."""
    if event.get("process") != "kernel":
        return None
    message = str(event.get("message") or "")
    match = _KERNEL_FILE_RE.search(message)
    if match:
        return match.group(1)
    if "CODESIGNING" in message:
        match = _VERSION_PATH_RE.search(message)
        if match:
            return match.group(1)
    return None


def relevant(event: dict[str, Any]) -> bool:
    """Whether a code-signature line is worth keeping: the kernel's (its
    refusals name the file), or one that names a file or a path that could
    be claude. The rest — every app's AMFI chatter, hundreds a day — is
    dropped. AppleSystemPolicy's provenance lines (the kernel's too) are
    kept only when they name the claude versions directory: other CLIs
    (codex …) get them as well."""
    message = str(event.get("message") or "")
    if PROVENANCE in message:
        return CLAUDE_VERSIONS in message
    if event.get("process") == "kernel" or killed_file(message):
        return True
    lowered = message.lower() + " " + str(event.get("processImagePath") or "").lower()
    return "claude" in lowered or "/.local/share/claude/" in lowered


def provenance_of(event: dict[str, Any]) -> dict[str, Any] | None:
    """AppleSystemPolicy's ``ASP: Unable to apply provenance sandbox: <err>,
    <pid>, <path>`` as ``{"at", "ts", "error", "pid", "path", "message"}``,
    or None for any other line."""
    message = str(event.get("message") or "")
    if PROVENANCE not in message:
        return None
    match = _PROVENANCE_RE.search(message)
    path = _VERSION_PATH_RE.search(message)
    return {
        "at": event.get("at"), "ts": event.get("ts"),
        "error": int(match.group(1)) if match else None,
        "pid": int(match.group(2)) if match else None,
        "path": path.group(1) if path else None,
        "message": message[:MESSAGE_MAX],
    }


def names_binary(token: str, binary: claude_exec.Binary) -> bool:
    """Whether a message's file names ``binary``. The kernel gives only the
    file name (``2.1.289``), so a bare name counts when it is a version
    number (a native install's file); anything else (a Homebrew ``claude``)
    has to be the full real path."""
    from claude_swap.maximize.claude_version import VERSION_RE

    if binary.real is None or not token:
        return False
    if token == binary.real:
        return True
    base = os.path.basename(token)
    return (
        "/" not in token and VERSION_RE.fullmatch(base) is not None
        and base == os.path.basename(binary.real)
    )


# -- crash reports ------------------------------------------------------------------------


@dataclass(frozen=True)
class CrashKill:
    file: str
    proc_path: str        # as the report has it (macOS anonymizes: /Users/USER/*/2.1.289)
    version: str          # procName, else the path's file name
    pid: int | None
    at: float
    path: str = ""        # the file it was: the real path, or versions/<version>
    parent: str | None = None       # parentProc: who started the killed process
    responsible: str | None = None  # responsibleProc: the app macOS charges it to


#: macOS writes a report's ``procPath`` under the home directory anonymized.
_ANONYMIZED_RE = re.compile(r"/Users/USER/\*/(?P<name>[^/]+)")


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
    name = str(body.get("procName") or "") or os.path.basename(proc_path)

    def text(key: str) -> str | None:
        value = body.get(key)
        return value if isinstance(value, str) and value else None

    return CrashKill(
        path.name, proc_path, name,
        pid if isinstance(pid, int) and not isinstance(pid, bool) else None, at,
        parent=text("parentProc"), responsible=text("responsibleProc"),
    )


def _claude_kill(kill: CrashKill, versions: Path, names: set[str]) -> CrashKill | None:
    """``kill`` with :attr:`CrashKill.path` set when it was a native
    ``claude``: its ``procPath`` is under ``versions`` — or is the anonymized
    ``/Users/USER/*/<name>`` and ``<name>`` is a file in ``versions`` (or one
    of ``names``, the current binary's) — else None."""
    prefix = str(versions) + os.sep
    if kill.proc_path.startswith(prefix):
        return replace(kill, path=kill.proc_path)
    match = _ANONYMIZED_RE.fullmatch(kill.proc_path)
    if match is None or match.group("name") != kill.version:
        return None
    candidate = versions / kill.version
    if candidate.is_file() or kill.version in names:
        return replace(kill, path=str(candidate))
    return None


def _header_may_be_claude(path: Path, names: set[str]) -> bool:
    """A cheap look at the report's first line (its JSON header) before the
    body is read: a crash report (bug type 309) of a process named like a
    native claude file (a version number), ``claude``, or one of ``names``."""
    from claude_swap.maximize.claude_version import VERSION_RE

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            header = json.loads(fh.readline(64 * 1024))
    except (OSError, ValueError):
        return False
    if not isinstance(header, dict) or str(header.get("bug_type") or "309") != "309":
        return False
    name = str(header.get("name") or header.get("app_name") or "")
    return bool(name) and (
        VERSION_RE.fullmatch(name) is not None or name == "claude" or name in names
    )


def scan_crash_reports(
    home: Path, now: float, *, max_age_s: float = REPORT_MAX_AGE_S,
    names: Iterable[str] = (),
) -> list[CrashKill]:
    """Code-signing kills of a native ``claude`` (a process under
    ``~/.local/share/claude/versions``, or macOS's anonymized form of one:
    :func:`_claude_kill`) in the last ``max_age_s``, oldest first. ``names``:
    file names that count as claude even when no longer in ``versions``.
    Never raises."""
    versions = Path(home) / VERSIONS_DIR
    known = set(names)
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
        if not _header_may_be_claude(path, known):
            continue
        kill = parse_report(path)
        kill = _claude_kill(kill, versions, known) if kill is not None else None
        if kill is not None:
            out.append(kill)
    return sorted(out, key=lambda k: k.at)


def launcher_of(kill: CrashKill) -> str | None:
    """The app that launched the killed process: ``parentProc``, with
    ``responsibleProc`` when that is another one."""
    who = kill.parent or kill.responsible
    if kill.responsible and kill.parent and kill.responsible != kill.parent:
        who = f"{kill.parent} (responsible: {kill.responsible})"
    return who


def episodes(kills: Iterable[CrashKill]) -> list[dict[str, Any]]:
    """Per version: ``{"version", "path", "count", "first", "last",
    "parents"}`` (``parents``: the launching apps, most frequent first),
    oldest first."""
    by: dict[str, dict[str, Any]] = {}
    counts: dict[str, dict[str, int]] = {}
    for k in kills:
        e = by.setdefault(k.version, {
            "version": k.version, "path": k.path or k.proc_path, "count": 0,
            "first": k.at, "last": k.at,
        })
        e["count"] += 1
        e["first"] = min(e["first"], k.at)
        e["last"] = max(e["last"], k.at)
        who = launcher_of(k)
        if who:
            c = counts.setdefault(k.version, {})
            c[who] = c.get(who, 0) + 1
    for version, e in by.items():
        c = counts.get(version, {})
        e["parents"] = sorted(c, key=lambda w: (-c[w], w))
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
        self._scanner: threading.Thread | None = None
        self._prober: threading.Thread | None = None
        self._provenance: dict[str, Any] | None = None
        self.starts = 0

    # -- the child --------------------------------------------------------------------

    def tick(self) -> None:
        try:
            now = self._clock()
            self._keep_streaming(now)
            scanning = self._scanner is not None and self._scanner.is_alive()
            if now >= self._next_scan and not scanning:
                self._next_scan = now + SCAN_EVERY_S
                # Off the tick: a directory of reports takes a while to read.
                self._scanner = threading.Thread(
                    target=self._scan_quietly, args=(now,), name="cc-swap-codesign-scan",
                    daemon=True,
                )
                self._scanner.start()
            self._maybe_probe(now)
        except Exception as e:  # evidence gathering never breaks a tick
            _logger.debug("codesign watch: %s", type(e).__name__)

    def _scan_quietly(self, now: float) -> None:
        try:
            self.scan(now)
        except Exception as e:
            _logger.debug("codesign watch: scan failed: %s", type(e).__name__)

    def _maybe_probe(self, now: float) -> None:
        """A probe due after kills cc-swap did not launch
        (``claude_exec.probe_external``), on its own thread: ``claude
        --version`` may take seconds."""
        if self._prober is not None and self._prober.is_alive():
            return
        if not claude_exec.probe_pending(self.root, now):
            return
        claude = self._claude()
        if not claude or not claude_exec.external_probe_due(
            self.root, claude_exec.stat_binary(claude), now,
        ):
            return
        self._prober = threading.Thread(
            target=self._probe_quietly, args=(claude, now), name="cc-swap-claude-probe",
            daemon=True,
        )
        self._prober.start()

    def _probe_quietly(self, claude: str, now: float) -> None:
        try:
            claude_exec.probe_external(self.root, claude, now=now)
        except Exception as e:
            _logger.debug("codesign watch: probe failed: %s", type(e).__name__)

    def _keep_streaming(self, now: float) -> None:
        proc = self._proc
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                if self._started_at is not None and now - self._started_at >= HEALTHY_S:
                    self._backoff = BACKOFF_MIN_S
                return
            self._proc = None
            lived = now - self._started_at if self._started_at is not None else 0.0
            if rc == 0 and lived >= STREAM_TIMEOUT_S * 0.9:
                self._backoff = BACKOFF_MIN_S  # its own --timeout: restart now
            else:
                _logger.warning(
                    "codesign watch: log stream exited (%s); restarting in %.0fs",
                    rc, self._backoff,
                )
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
        if event is None or not relevant(event):
            return
        claude_exec.append_jsonl(self.root, EVENTS_FILENAME, event)
        provenance = provenance_of(event)
        if provenance is not None:
            # Comes just before the kernel's kill line, with the killed pid.
            claude = self._claude() if provenance.get("path") else None
            if claude and names_binary(provenance["path"], claude_exec.stat_binary(claude)):
                self._provenance = provenance
            return
        token = kill_target(event)
        claude = self._claude() if token else None
        if not claude or not names_binary(token, claude_exec.stat_binary(claude)):
            return
        provenance, self._provenance = self._provenance, None
        at = event["at"]
        if provenance is not None and not (
            isinstance(provenance.get("at"), (int, float))
            and 0 <= at - provenance["at"] <= PROVENANCE_WINDOW_S
        ):
            provenance = None
        pid = provenance.get("pid") if provenance else None
        if pid is not None and claude_exec.cc_swap_launch(self.root, pid, at) is not None:
            return  # a run of cc-swap's: its own SIGKILL already took that path
        claude_exec.note_external_kill(
            self.root, claude, source="log stream", at=at, detail=event["message"],
            pid=pid, provenance=provenance,
        )

    def scan(self, now: float) -> list[CrashKill]:
        """New crash reports into ``codesign-events.jsonl``. Each new kill
        of the current ``claude`` file is evidence with its launching app
        (``claude_exec.note_external_kill``) — or, when its pid is a
        ``claude`` cc-swap ran, that run's kill
        (``claude_exec.mark_killed_by_os``)."""
        claude = self._claude()
        binary = claude_exec.stat_binary(claude) if claude else None
        names = {os.path.basename(binary.real)} if binary and binary.real else set()
        kills = scan_crash_reports(self.home, now, names=names)
        new = [k for k in kills if k.file not in self._seen]
        self._seen.update(k.file for k in kills)
        for k in new:
            claude_exec.append_jsonl(self.root, EVENTS_FILENAME, {
                "kind": "crash-report", "at": k.at, "ts": claude_exec._iso(k.at),
                "pid": k.pid, "process": k.version, "processImagePath": k.proc_path,
                "path": k.path, "parentProc": k.parent, "responsibleProc": k.responsible,
                "message": f"{INVALID} ({k.file})",
            })
        if new:
            _logger.warning(
                "codesign watch: %d new code-signing kill report(s) of claude (%s)",
                len(new), ", ".join(sorted({k.version for k in new})),
            )
        if claude and new and binary is not None and binary.real:
            real = binary.real
            mine = [k for k in new if k.path and os.path.realpath(k.path) == real]
            launches = claude_exec.read_jsonl(self.root) if mine else []
            for k in mine:
                if claude_exec.cc_swap_launch(self.root, k.pid, k.at, records=launches):
                    claude_exec.mark_killed_by_os(
                        self.root, claude, source="crash report", at=k.at, detail=k.file,
                        pid=k.pid,
                    )
                else:
                    claude_exec.note_external_kill(
                        self.root, claude, source="crash report", at=k.at, detail=k.file,
                        pid=k.pid, launcher=launcher_of(k), report=k.file,
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
