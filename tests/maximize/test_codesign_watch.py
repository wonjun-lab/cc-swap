"""The code-signing kill watcher (maximize/codesign_watch.py): ``log stream``
events, crash-report scans, the doctor finding, and the engine's start/stop.
A fake ``log stream`` and fake ``.ips`` files only — never the real ones."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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
    # another app's AMFI chatter is not kept at all; a line naming claude is
    amfid = {**KERNEL, "processImagePath": "/usr/libexec/amfid", "processID": 312}
    w.handle_line(json.dumps({**amfid, "eventMessage": "Some.app: code signature validated"}) + "\n")
    w.handle_line(json.dumps({
        **amfid,
        "eventMessage": "/Users/x/.local/share/claude/versions/2.1.290: code signature ok",
    }) + "\n")
    assert [e["pid"] for e in _events(root)] == [0, 312]
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


# -- the real report shape (macOS anonymizes procPath) ---------------------------------------


def _real_shape(home: Path, name: str, version: str, stamp: str, *,
                parent: str = "T3 Code (Alpha)") -> Path:
    """An .ips built from the incident's reports (no personal data): procPath
    anonymized to /Users/USER/*/<file>, procName the file, a CODESIGNING
    termination, slashes escaped as the real files have them."""
    directory = cw.reports_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    header = {
        "app_name": version, "timestamp": f"{stamp}.00 +0900", "app_version": "",
        "bug_type": "309", "os_version": "macOS 26.6.2 (25G83)", "name": version,
    }
    body = {
        "uptime": 470000, "procRole": "Unspecified", "version": 2, "userID": 501,
        "captureTime": f"{stamp}.8550 +0900", "pid": 56959,
        "procLaunch": f"{stamp}.8545 +0900", "procName": version,
        "procPath": f"/Users/USER/*/{version}",
        "parentProc": parent, "parentPid": 26737,
        "responsiblePid": 26691, "responsibleProc": parent,
        "codeSigningID": "", "codeSigningTeamID": "", "codeSigningFlags": 16777728,
        "exception": {
            "codes": "0x0, 0x0", "type": "EXC_CRASH",
            "signal": "SIGKILL (Code Signature Invalid)",
        },
        "termination": {
            "flags": 0, "code": 2, "namespace": "CODESIGNING",
            "indicator": "Taskgated Invalid Signature",
        },
    }
    path = directory / name
    escaped = json.dumps(body, indent=2).replace("/", "\\/")
    path.write_text(json.dumps(header) + "\n" + escaped)
    return path


def _local_stamp(epoch: float) -> str:
    """``YYYY-MM-DD HH:MM:SS`` in +0900, the reports' zone."""
    return datetime.fromtimestamp(epoch, tz=timezone(timedelta(hours=9))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_the_anonymized_real_shape_is_recognised(home):
    _install(home, "2.1.288")
    _install(home, "2.1.289")
    _real_shape(home, "2.1.288-2026-10-04-012924.ips", "2.1.288", "2026-10-04 01:29:24")
    _real_shape(home, "2.1.288-2026-10-04-015501.ips", "2.1.288", "2026-10-04 01:55:01")
    _real_shape(home, "2.1.289-2026-10-04-205246.ips", "2.1.289", "2026-10-04 20:52:46")
    # anonymized and no longer installed: indistinguishable from another file
    _real_shape(home, "2.1.100-2026-10-04-100000.ips", "2.1.100", "2026-10-04 10:00:00")
    _real_shape(home, "tool-2026-10-04-100000.ips", "tool", "2026-10-04 10:00:00")
    now = datetime(2026, 10, 4, 21, 0, tzinfo=timezone(timedelta(hours=9))).timestamp()
    for p in cw.reports_dir(home).iterdir():
        os.utime(p, (now - 3600, now - 3600))
    kills = cw.scan_crash_reports(home, now)
    assert [k.file for k in kills] == [
        "2.1.288-2026-10-04-012924.ips", "2.1.288-2026-10-04-015501.ips",
        "2.1.289-2026-10-04-205246.ips",
    ]
    k = kills[0]
    assert k.proc_path == "/Users/USER/*/2.1.288" and k.version == "2.1.288"
    assert k.path == str(home / ".local" / "share" / "claude" / "versions" / "2.1.288")
    assert (k.parent, k.responsible, k.pid) == ("T3 Code (Alpha)", "T3 Code (Alpha)", 56959)
    assert k.at == datetime(2026, 10, 4, 1, 29, 24, tzinfo=timezone(timedelta(hours=9))).timestamp()
    # the current binary's name counts even when its file is gone
    named = cw.scan_crash_reports(home, now, names={"2.1.100"})
    assert [k.version for k in named].count("2.1.100") == 1
    [e288, e289] = cw.episodes(kills)
    assert e288["count"] == 2 and e288["parents"] == ["T3 Code (Alpha)"]
    assert e289["path"].endswith("versions/2.1.289")


def test_doctor_names_the_launching_app(home, root):
    _install(home, "2.1.289")
    _real_shape(home, "2.1.289-x.ips", "2.1.289", _local_stamp(time.time() - 120))
    ctx = SimpleNamespace(probes=SimpleNamespace(
        backup_root=root, home=home, platform="darwin", now=time.time(),
    ))
    [f] = dr.check_codesign_kills(ctx)
    assert "launched by T3 Code (Alpha)" in f.detail
    assert "/Users/USER" not in f.fix and "versions/2.1.289" in f.fix


def test_the_engine_marks_the_current_file_from_an_anonymized_report(home, root, sent):
    link, _real = _install(home, "2.1.289")
    later = time.time() + 120
    _real_shape(home, "2.1.289-y.ips", "2.1.289", _local_stamp(later))
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.scan(later + 1)
    killed = cx.current_killed(root)
    assert killed is not None and killed["caller"] == "crash report" and len(sent) == 1
    [event] = _events(root)
    assert event["parentProc"] == "T3 Code (Alpha)" and event["path"].endswith("2.1.289")


# -- re-review: old evidence never re-marks; only kernel lines mark -----------------------------


def test_a_successful_run_outlives_the_old_reports_across_a_restart(home, root, sent):
    link, real = _install(home, "2.1.289")
    killed_at = os.stat(real).st_ctime + 0.05  # killed just after it was written
    _ips(home, "2.1.289-a.ips", str(real), killed_at)
    while time.time() <= killed_at + 0.02:
        time.sleep(0.01)
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.scan(time.time())
    assert cx.current_killed(root) is not None
    with cx.manual("t"):
        assert cx.run([str(link), "--version"], caller="test", timeout=10, root=root).returncode == 0
    assert cx.current_killed(root) is None
    w.scan(time.time())  # the hourly scan: same old reports
    assert cx.current_killed(root) is None
    restarted = cw.Watcher(root, lambda: str(link), home=home)  # engine restart
    restarted.scan(time.time())
    assert cx.current_killed(root) is None and len(sent) == 1
    # new evidence still marks
    restarted.handle_line(json.dumps(KERNEL) + "\n")
    assert cx.current_killed(root) is not None


def test_evidence_before_a_successful_run_never_marks(home, root):
    link, _real = _install(home, "2.1.289")
    with cx.manual("t"):
        cx.run([str(link), "--version"], caller="test", timeout=10, root=root)
    assert not cx.mark_killed_by_os(root, str(link), source="x", at=time.time() - 1, detail="")
    assert cx.mark_killed_by_os(root, str(link), source="x", at=time.time() + 1, detail="")


def test_only_the_kernel_marks(home, root, sent):
    link, real = _install(home, "2.1.289")
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps({
        **KERNEL, "processImagePath": "/usr/libexec/syspolicyd", "processID": 99,
        "eventMessage": f"code signature error for {real}: invalid",
    }) + "\n")
    w.handle_line(json.dumps({
        **KERNEL, "processImagePath": "/usr/libexec/amfid", "processID": 98,
        "eventMessage": 'load code signature error 2 for file "2.1.289"',
    }) + "\n")
    assert len(_events(root)) == 2  # kept as evidence
    assert cx.any_killed(root) is None and sent == []


def test_a_bare_name_that_is_not_a_version_never_matches(tmp_path, root, sent):
    brew = tmp_path / "homebrew" / "bin" / "claude"
    brew.parent.mkdir(parents=True)
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    w = cw.Watcher(root, lambda: str(brew), home=tmp_path)
    w.handle_line(json.dumps({
        **KERNEL, "eventMessage": 'load code signature error 2 for file "claude"',
    }) + "\n")
    assert cx.any_killed(root) is None
    assert not cw.names_binary("claude", cx.stat_binary(str(brew)))
    assert cw.names_binary(os.path.realpath(brew), cx.stat_binary(str(brew)))


def test_the_stream_ends_itself_hourly_and_restarts_at_once(home, root):
    clock = Clock()
    spawned: list[FakeStream] = []

    def spawn(argv):
        assert argv[argv.index("--timeout") + 1] == cw.STREAM_TIMEOUT
        spawned.append(FakeStream())
        return spawned[-1]

    w = cw.Watcher(root, lambda: None, home=home, clock=clock, spawn=spawn)
    w.tick()
    clock.t += cw.STREAM_TIMEOUT_S
    spawned[0].rc = 0  # its own --timeout
    w.tick()
    assert len(spawned) == 2  # no backoff
    w.stop()


def test_the_scan_runs_off_the_tick(home, root, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow_scan(self, now):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr(cw.Watcher, "scan", slow_scan)
    w = cw.Watcher(root, lambda: None, home=home, spawn=lambda argv: None)
    t0 = time.monotonic()
    w.tick()
    assert started.wait(5) and time.monotonic() - t0 < 1.0
    w.tick()  # still scanning: no second scan
    release.set()
    w._scanner.join(5)


def test_reports_of_other_programs_are_skipped_on_their_header(home, monkeypatch):
    _install(home, "2.1.289")
    _real_shape(home, "Other-1.ips", "Other", _local_stamp(time.time() - 60))
    _real_shape(home, "2.1.289-1.ips", "2.1.289", _local_stamp(time.time() - 60))
    parsed: list[str] = []
    real_parse = cw.parse_report
    monkeypatch.setattr(cw, "parse_report", lambda p: parsed.append(p.name) or real_parse(p))
    kills = cw.scan_crash_reports(home, time.time())
    assert parsed == ["2.1.289-1.ips"] and len(kills) == 1
