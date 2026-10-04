"""The code-signing kill watcher (maximize/codesign_watch.py): ``log stream``
events, crash-report scans, the doctor finding, and the engine's start/stop.
A fake ``log stream`` and fake ``.ips`` files only — never the real ones."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap.maximize import claude_exec as cx
from claude_swap.maximize import codesign_watch as cw
from claude_swap.maximize import doctor as dr
from claude_swap.maximize import notify

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths and symlinks")


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def sent(monkeypatch) -> list:
    out: list = []
    backend = notify.Backend("fake", lambda t, b: out.append((t, b)) or True)
    monkeypatch.setattr(notify, "system_backend", lambda *a, **k: backend)
    return out


def _install(home: Path, version: str) -> tuple[Path, Path]:
    real = home / ".local" / "share" / "claude" / "versions" / version
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("#!/bin/sh\necho ok\n")
    real.chmod(0o755)
    link = home / ".local" / "bin" / "claude"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    link.symlink_to(real)
    return link, real


def _ips(home: Path, name: str, proc_path: str, at: float, *, signal="SIGKILL (Code Signature Invalid)",
         mtime: float | None = None) -> Path:
    directory = cw.reports_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-4] + " +0000"
    header = {"app_name": os.path.basename(proc_path), "timestamp": stamp, "bug_type": "309"}
    body = {
        "procPath": proc_path, "pid": 4242, "captureTime": stamp,
        "exception": {"type": "EXC_CRASH", "signal": signal},
        "termination": {"namespace": "CODESIGNING", "indicator": "Invalid Page"},
    }
    path = directory / name
    path.write_text(json.dumps(header) + "\n" + json.dumps(body, indent=2))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _events(root: Path) -> list[dict]:
    return cx.read_jsonl(root, cw.EVENTS_FILENAME)


# -- log stream lines ------------------------------------------------------------------------

KERNEL = {
    "timestamp": "2026-10-04 20:52:46.123456+0900", "processID": 0,
    "processImagePath": "/kernel", "subsystem": "", "category": "",
    "eventMessage": 'load code signature error 2 for file "2.1.289"',
}


def test_the_banner_and_noise_are_not_events():
    assert cw.parse_event("Filtering the log data using \"…\"\n") is None
    assert cw.parse_event("[]") is None


def test_a_kernel_refusal_of_the_current_claude_is_recorded_and_marks_it(home, root, sent):
    link, real = _install(home, "2.1.289")
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps(KERNEL) + "\n")
    [event] = _events(root)
    assert event["kind"] == "log-event" and event["pid"] == 0 and event["process"] == "kernel"
    assert event["message"] == KERNEL["eventMessage"] and event["ts"] == KERNEL["timestamp"]
    killed = cx.current_killed(root)
    assert killed is not None and killed["real"] == os.path.realpath(real)
    assert killed["caller"] == "log stream" and len(sent) == 1
    # the user's own claude, killed again: still one notification
    w.handle_line(json.dumps(KERNEL) + "\n")
    assert len(sent) == 1 and cx.current_killed(root)["count"] == 2


def test_another_files_refusal_is_recorded_but_marks_nothing(home, root, sent):
    link, _ = _install(home, "2.1.290")
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps(KERNEL) + "\n")  # names 2.1.289
    w.handle_line(json.dumps({**KERNEL, "eventMessage": "AMFI: code signature validated"}) + "\n")
    assert len(_events(root)) == 2
    assert cx.any_killed(root) is None and sent == []


# -- the child -------------------------------------------------------------------------------


class FakeStream:
    def __init__(self, lines=()):
        self.stdout = io.StringIO("".join(lines))
        self.rc = None
        self.terminated = False

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = -15

    def kill(self):
        self.rc = -9

    def wait(self, timeout=None):
        return self.rc


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_the_stream_restarts_with_a_backoff_and_stops_cleanly(home, root):
    clock = Clock()
    spawned: list[FakeStream] = []

    def spawn(argv):
        assert argv[:4] == [cw.LOG, "stream", "--style", "ndjson"]
        assert "load code signature" not in argv[-1]  # the predicate is general …
        assert '"code signature"' in argv[-1] and "CODESIGNING" in argv[-1]
        spawned.append(FakeStream())
        return spawned[-1]

    w = cw.Watcher(root, lambda: None, home=home, clock=clock, spawn=spawn)
    w.tick()
    assert len(spawned) == 1
    spawned[0].rc = 1  # it died
    clock.t += 1
    w.tick()
    assert len(spawned) == 1  # waits BACKOFF_MIN_S
    clock.t += cw.BACKOFF_MIN_S
    w.tick()
    assert len(spawned) == 2
    spawned[1].rc = 1
    clock.t += 1
    w.tick()
    clock.t += cw.BACKOFF_MIN_S
    w.tick()
    assert len(spawned) == 2  # the backoff doubled
    clock.t += cw.BACKOFF_MIN_S
    w.tick()
    assert len(spawned) == 3
    w.stop()
    assert spawned[2].terminated


def test_a_stream_that_cannot_start_never_blocks_the_tick(home, root):
    clock = Clock()
    w = cw.Watcher(root, lambda: None, home=home, clock=clock, spawn=lambda argv: None)
    started = time.monotonic()
    for _ in range(5):
        w.tick()
        clock.t += 10
    assert time.monotonic() - started < 1.0 and w.starts == 0


def test_lines_from_the_child_are_read_off_the_tick(home, root):
    w = cw.Watcher(
        root, lambda: None, home=home,
        spawn=lambda argv: FakeStream([json.dumps(KERNEL) + "\n"]),
    )
    w.tick()
    w._reader.join(timeout=5)
    assert len(_events(root)) == 1
    w.stop()


# -- crash reports ---------------------------------------------------------------------------


def test_scan_finds_code_signing_kills_of_native_claude_only(home):
    now = time.time()
    versions = home / ".local" / "share" / "claude" / "versions"
    _ips(home, "claude-1.ips", str(versions / "2.1.288"), now - 3 * 86400)
    _ips(home, "claude-2.ips", str(versions / "2.1.289"), now - 600)
    _ips(home, "claude-3.ips", str(versions / "2.1.289"), now - 300)
    _ips(home, "other.ips", "/Applications/Other.app/Contents/MacOS/Other", now - 60)
    _ips(home, "crash.ips", str(versions / "2.1.289"), now - 60, signal="SIGSEGV")
    # (a plain crash: no CODESIGNING termination either)
    path = cw.reports_dir(home) / "crash.ips"
    head, _, body = path.read_text().partition("\n")
    data = json.loads(body)
    data["termination"] = {"namespace": "SIGNAL"}
    path.write_text(head + "\n" + json.dumps(data))
    _ips(home, "old.ips", str(versions / "2.1.200"), now - 30 * 86400, mtime=now - 30 * 86400)
    kills = cw.scan_crash_reports(home, now)
    assert [k.file for k in kills] == ["claude-1.ips", "claude-2.ips", "claude-3.ips"]
    assert kills[0].version == "2.1.288" and kills[0].pid == 4242
    assert abs(kills[0].at - (now - 3 * 86400)) < 1
    [e288, e289] = cw.episodes(kills)
    assert (e288["version"], e288["count"]) == ("2.1.288", 1)
    assert (e289["version"], e289["count"]) == ("2.1.289", 2)


def test_the_engine_scan_marks_the_current_file_and_logs_new_reports(home, root, sent):
    link, real = _install(home, "2.1.289")
    now = time.time() + 5  # after the file was written
    _ips(home, "claude-a.ips", str(real), now)
    w = cw.Watcher(root, lambda: str(link), home=home, clock=lambda: now)
    assert [k.file for k in w.scan(now)] == ["claude-a.ips"]
    assert [e["kind"] for e in _events(root)] == ["crash-report"]
    killed = cx.current_killed(root)
    assert killed is not None and killed["caller"] == "crash report" and len(sent) == 1
    assert w.scan(now) == [] and len(_events(root)) == 1  # nothing new


def test_a_report_of_an_older_file_at_the_path_marks_nothing(home, root, sent):
    link, real = _install(home, "2.1.289")
    _ips(home, "claude-old.ips", str(real), time.time() - 3600)  # before this file
    cw.Watcher(root, lambda: str(link), home=home).scan(time.time())
    assert cx.any_killed(root) is None and sent == []


def test_doctor_shows_each_episode_with_the_launch_before_it(home, root):
    now = time.time()
    versions = home / ".local" / "share" / "claude" / "versions"
    first = now - 600
    _ips(home, "claude-1.ips", str(versions / "2.1.289"), first)
    _ips(home, "claude-2.ips", str(versions / "2.1.289"), now - 300)
    cx.append_record(root, {"kind": "exec", "at": first - 20, "caller": "prime", "args": ["-p"]})
    cx.append_record(root, {"kind": "exec", "at": first + 5, "caller": "later"})
    ctx = SimpleNamespace(probes=SimpleNamespace(
        backup_root=root, home=home, platform="darwin", now=now,
    ))
    [f] = dr.check_codesign_kills(ctx)
    assert f.severity == "warn" and f.check == "codesign-kills"
    assert "macOS killed claude 2.1.289 2 times at launch" in f.detail
    assert f"first {dr._when(first)}" in f.detail and f"last {dr._when(now - 300)}" in f.detail
    assert "latest cc-swap claude launch before the first: prime at" in f.detail
    assert "(20s before)" in f.detail
    assert "cp -p ~/.local/share/claude/versions/2.1.289" in f.fix
    linux = SimpleNamespace(probes=SimpleNamespace(
        backup_root=root, home=home, platform="linux", now=now,
    ))
    assert dr.check_codesign_kills(linux) == []


# -- the engine ------------------------------------------------------------------------------


def test_a_live_engine_runs_the_watcher_and_stops_it(temp_home, monkeypatch):
    from tests.test_autoswitch import EngineHarness

    harness = EngineHarness(temp_home)
    engine = harness.engine
    streams: list[FakeStream] = []
    monkeypatch.setattr(cw, "_is_macos", lambda: True)
    monkeypatch.setattr(cw, "_spawn", lambda argv: streams.append(FakeStream()) or streams[-1])
    monkeypatch.setattr(engine, "tick", lambda: engine.stop() or __import__(
        "claude_swap.autoswitch", fromlist=["TickOutcome"]).TickOutcome.NO_ACTION)
    engine.dry_run = False
    assert engine.run_loop() == 0
    assert len(streams) == 1 and streams[0].terminated


def test_a_dry_run_engine_watches_nothing(temp_home, monkeypatch):
    from tests.test_autoswitch import EngineHarness

    harness = EngineHarness(temp_home)
    harness.engine.dry_run = True
    monkeypatch.setattr(cw, "_is_macos", lambda: True)
    assert cw.for_engine(harness.engine) is None
