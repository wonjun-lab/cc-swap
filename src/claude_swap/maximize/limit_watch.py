"""Notice a usage limit from Claude Code's own transcripts (cc-swap fork).

The usage endpoint is the engine's only window on an account's 5h/7d
utilization, and it is a shared, budgeted one: several machines polling the
same account exhaust it, and from then on every read answers 429 while the
account keeps being used (the 2026-10-06 incident: 90 minutes of 429s, the
5h window climbing unseen from 20% to 100%). Claude Code itself knows the
moment a request is refused for the limit, and writes it into the session
transcript under ``<config home>/projects/**/*.jsonl``. This module reads
those refusals, and nothing else, so the engine can switch at once.

The record Claude Code writes (2.1.2xx; values redacted)::

    {"type": "assistant", "isApiErrorMessage": true, "error": "rate_limit",
     "apiErrorStatus": 429, "timestamp": "<ISO-8601>",
     "quotaLimits": {"status": "rejected", "rateLimitType": "five_hour",
                     "resetsAt": <epoch s>, "isUsingOverage": false, ...},
     "message": {"model": "<synthetic>", "role": "assistant",
                 "content": [{"type": "text",
                              "text": "You've hit your session limit · resets <when>"}]},
     ...}

``rateLimitType`` is ``five_hour`` (the text says "session limit") or
``seven_day`` ("weekly limit"); a "monthly spend limit" text rides on the
same ``quotaLimits``. A record with ``error: "rate_limit"`` but no
``quotaLimits`` ("Request rejected (429) · This request would exceed your
account's rate limit") is a short request-rate throttle, not the quota, and
is ignored, as are per-model limits ("You've reached your Fable limit").

Cost and safety, per :meth:`TranscriptWatcher.poll`:

* a bounded directory walk (``MAX_ENTRIES`` names, ``MAX_DEPTH`` levels,
  ``stat`` only) every ``FULL_WALK_S``; between walks only the recently
  written files and their directories are looked at again (a new session
  file moves its directory's mtime). Files modified within ``RECENT_S`` are
  read, the ``MAX_FILES`` newest of them. A walk cut short by the cap says
  so (``complete``): the engine then treats local idleness as unknown;
* only the bytes appended since the last poll (``MAX_READ_BYTES`` at most per
  file and poll; a file first seen is read from ``INITIAL_TAIL_BYTES`` before
  its end), never a whole transcript;
* a partial last line waits for the next poll; a line longer than the read
  cap is skipped; a file that shrank or was replaced is read from its new
  tail;
* a line is parsed only when it carries both markers; nothing a
  transcript says (prompts, answers, paths) is kept, returned or logged —
  only the hit's time, window and reset.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: A transcript modified longer ago than this is not read.
RECENT_S = 900.0
#: At most this many of the most recently modified transcripts per poll.
MAX_FILES = 32
#: Directory entries the walk looks at per poll, at most.
MAX_ENTRIES = 20000
#: ``projects/<project>/<session>/subagents/workflows/<wf>/agent.jsonl``.
MAX_DEPTH = 6
#: New bytes read per file and poll, at most (the newest are kept).
MAX_READ_BYTES = 256 * 1024
#: A file seen for the first time is read from this far before its end.
INITIAL_TAIL_BYTES = 64 * 1024
#: Files remembered between polls (offsets); the oldest are forgotten.
MAX_TRACKED = 256
#: A full directory walk at most this often; in between only the files
#: written recently (and their directories) are looked at.
FULL_WALK_S = 300.0

_MARKER = b"isApiErrorMessage"
_KIND = b"rate_limit"
_WINDOWS = {"five_hour": "5h", "seven_day": "7d"}
_TEXT_WINDOWS = (
    (re.compile(r"hit your session limit", re.I), "5h"),
    (re.compile(r"hit your weekly limit", re.I), "7d"),
)


@dataclass(frozen=True)
class LimitHit:
    """Claude Code reported a refusal for a usage limit."""

    ts: float                    # when (the record's timestamp, epoch s)
    window: str                  # "5h" | "7d"
    resets_at: float | None      # quotaLimits.resetsAt, epoch s; None unknown
    #: The record's ``sessionId`` (an opaque id, never logged): lets the
    #: engine set aside refusals from ``cswap run`` sessions on other
    #: accounts whose transcripts share this directory (``--share-history``).
    session_id: str | None = None


def _epoch(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        if out > 1e12:  # milliseconds
            out /= 1000.0
        return out if math.isfinite(out) and out > 0 else None
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def parse_line(line: bytes) -> LimitHit | None:
    """The usage-limit refusal one transcript line records, or None."""
    if _MARKER not in line or _KIND not in line:
        return None
    try:
        record = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or record.get("isApiErrorMessage") is not True:
        return None
    if record.get("error") != "rate_limit":
        return None
    ts = _epoch(record.get("timestamp"))
    if ts is None:
        return None
    sid = record.get("sessionId")
    sid = sid if isinstance(sid, str) and sid else None
    quota = record.get("quotaLimits")
    if isinstance(quota, dict):
        window = _WINDOWS.get(str(quota.get("rateLimitType")))
        if window is None or quota.get("status") != "rejected":
            return None
        return LimitHit(
            ts=ts, window=window, resets_at=_epoch(quota.get("resetsAt")), session_id=sid
        )
    # Older Claude Code builds wrote the text only.
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for part in content if isinstance(content, list) else ():
        text = part.get("text") if isinstance(part, dict) else None
        if not isinstance(text, str):
            continue
        for pattern, window in _TEXT_WINDOWS:
            if pattern.search(text):
                return LimitHit(ts=ts, window=window, resets_at=None, session_id=sid)
    return None


@dataclass
class _Cursor:
    ident: tuple[int, int]       # (st_dev, st_ino): a replaced file starts over
    offset: int
    skip_partial: bool = False   # discard up to the next newline first


@dataclass
class TranscriptWatcher:
    """Tails Claude Code transcripts for usage-limit refusals.

    ``root``: a callable returning the ``projects`` directory (resolved on
    every poll, so a changed ``CLAUDE_CONFIG_DIR`` or patched home is
    followed). Keeps per-file offsets in memory only."""

    root: Callable[[], Path]
    cursors: dict[str, _Cursor] = field(default_factory=dict)
    #: The newest transcript modification seen by the last poll (epoch s);
    #: None before any transcript was seen.
    last_write: float | None = None
    #: Whether the last poll found the projects directory at all.
    available: bool = False
    #: Whether the last full walk saw every file (a capped walk cannot say
    #: "nothing was written": idle is then unknown).
    complete: bool = False
    #: path -> last seen mtime, and directory -> mtime, from the walk.
    files: dict[str, float] = field(default_factory=dict)
    dirs: dict[str, float] = field(default_factory=dict)
    walked_at: float | None = None
    top: str | None = None

    def _walk(self, top: Path) -> bool:
        """The full walk: every ``.jsonl`` under ``top`` (bounded), into
        ``files``/``dirs``. True when it saw everything."""
        files: dict[str, float] = {}
        dirs: dict[str, float] = {}
        seen = 0
        stack: list[tuple[str, int]] = [(str(top), 0)]
        complete = True
        while stack:
            path, depth = stack.pop()
            try:
                dirs[path] = os.stat(path).st_mtime
                with os.scandir(path) as it:
                    for entry in it:
                        seen += 1
                        if seen > MAX_ENTRIES:
                            complete = False
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if depth + 1 < MAX_DEPTH:
                                    stack.append((entry.path, depth + 1))
                                continue
                            if entry.name.endswith(".jsonl"):
                                files[entry.path] = entry.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            continue
            except OSError:
                continue
            if not complete:
                break
        self.files, self.dirs = files, dirs
        return complete

    def _rescan_dir(self, path: str) -> None:
        """One directory whose mtime moved since the walk: pick up the
        ``.jsonl`` files (and directories) created in it."""
        try:
            self.dirs[path] = os.stat(path).st_mtime
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            self.dirs.setdefault(entry.path, 0.0)
                        elif entry.name.endswith(".jsonl") and entry.path not in self.files:
                            self.files[entry.path] = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
        except OSError:
            self.dirs.pop(path, None)

    def _recent_files(self, now: float) -> list[tuple[float, str, os.stat_result]]:
        """The ``MAX_FILES`` most recently modified transcripts within
        ``RECENT_S``. A full (bounded) walk every ``FULL_WALK_S``; between
        walks only the files written within ``RECENT_S`` and the
        directories that hold them (a new session file changes its
        directory's mtime) are looked at again."""
        try:
            top = self.root()
        except Exception:
            self.available = self.complete = False
            return []
        self.available = top.is_dir()
        if not self.available:
            self.complete = False
            return []
        if self.walked_at is None or now - self.walked_at >= FULL_WALK_S or self.top != str(top):
            self.complete = self._walk(top)
            self.walked_at, self.top = now, str(top)
        else:
            hot_dirs = {str(top)} | {
                os.path.dirname(f) for f, m in self.files.items() if now - m <= RECENT_S
            }
            for d in hot_dirs:
                try:
                    if os.stat(d).st_mtime != self.dirs.get(d):
                        self._rescan_dir(d)
                except OSError:
                    continue
            for d, m in list(self.dirs.items()):
                if m == 0.0:  # a directory created since the walk
                    self._rescan_dir(d)
        found: list[tuple[float, str, os.stat_result]] = []
        for path, mtime in list(self.files.items()):
            if now - mtime > RECENT_S + FULL_WALK_S:
                continue  # cold: the next full walk looks again
            try:
                st = os.stat(path)
            except OSError:
                self.files.pop(path, None)
                continue
            self.files[path] = st.st_mtime
            if now - st.st_mtime <= RECENT_S:
                found.append((st.st_mtime, path, st))
        newest = max(self.files.values(), default=None)
        if newest is not None:
            self.last_write = newest if self.last_write is None else max(self.last_write, newest)
        found.sort(reverse=True)
        return found[:MAX_FILES]

    def _read_new(self, path: str, st: os.stat_result) -> list[bytes]:
        ident = (st.st_dev, st.st_ino)
        cursor = self.cursors.get(path)
        size = st.st_size
        if cursor is None or cursor.ident != ident or size < cursor.offset:
            start = max(0, size - INITIAL_TAIL_BYTES)
            cursor = _Cursor(ident, start, skip_partial=start > 0)
            self.cursors[path] = cursor
        if size <= cursor.offset:
            return []
        start = cursor.offset
        if size - start > MAX_READ_BYTES:
            # Too much at once: only the newest bytes matter (the refusal
            # that stops work is the last thing written).
            start = size - MAX_READ_BYTES
            cursor.skip_partial = True
        try:
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return []
        if cursor.skip_partial:
            cut = data.find(b"\n")
            if cut < 0:
                # Still inside one long line: skip all of it.
                cursor.offset = start + len(data)
                return []
            data = data[cut + 1:]
            start += cut + 1
            cursor.skip_partial = False
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) >= MAX_READ_BYTES:
                cursor.offset = start + len(data)
                cursor.skip_partial = True
            else:
                cursor.offset = start  # a partial line: wait for the rest
            return []
        cursor.offset = start + end + 1
        return data[: end + 1].splitlines()

    def poll(self, now: float) -> list[LimitHit]:
        """Usage-limit refusals written since the last poll (a file first
        seen: in its last ``INITIAL_TAIL_BYTES``). Never raises."""
        hits: list[LimitHit] = []
        try:
            files = self._recent_files(now)
            for _mtime, path, st in files:
                for line in self._read_new(path, st):
                    hit = parse_line(line)
                    if hit is not None:
                        hits.append(hit)
            if len(self.cursors) > MAX_TRACKED:
                keep = {path for _m, path, _s in files}
                for path in list(self.cursors):
                    if len(self.cursors) <= MAX_TRACKED:
                        break
                    if path not in keep:
                        del self.cursors[path]
        except Exception:
            return hits
        return hits


def default_root() -> Path:
    """``<Claude config home>/projects`` (``CLAUDE_CONFIG_DIR`` or
    ``~/.claude``): the default profile's transcripts, where the live login
    is used."""
    from claude_swap.paths import get_claude_config_home

    return get_claude_config_home() / "projects"


def busy_sessions(config_home: Path) -> bool | None:
    """Whether a running Claude Code of this config home is in a turn:
    ``<config home>/sessions/<pid>.json`` with a live ``pid`` and a
    ``status`` other than ``idle`` (Claude Code keeps it ``busy`` for the
    whole turn, a long tool call included). None when there is no sessions
    directory to ask. Reads only ``pid`` and ``status``."""
    folder = config_home / "sessions"
    if not folder.is_dir():
        return None
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json")]
    except OSError:
        return None
    for name in names[:256]:
        try:
            with open(folder / name, "rb") as f:
                record = json.loads(f.read(64 * 1024))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        pid, status = record.get("pid"), record.get("status")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            continue
        if status == "idle" or not _alive(pid):
            continue
        return True
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def run_session_ids(backup_root: Path) -> set[str]:
    """The ``sessionId`` of every Claude Code session ``cswap run`` started
    in a per-account profile (``<backup>/sessions/<profile>/sessions/
    <pid>.json``): those sessions use their own account, so their refusals
    in a shared transcript directory are not the live login's."""
    out: set[str] = set()
    base = backup_root / "sessions"
    try:
        profiles = [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return out
    for profile in profiles[:64]:
        try:
            names = [n for n in os.listdir(profile / "sessions") if n.endswith(".json")]
        except OSError:
            continue
        for name in names[:256]:
            try:
                with open(profile / "sessions" / name, "rb") as f:
                    record = json.loads(f.read(64 * 1024))
            except (OSError, ValueError):
                continue
            sid = record.get("sessionId") if isinstance(record, dict) else None
            if isinstance(sid, str) and sid:
                out.add(sid)
    return out
