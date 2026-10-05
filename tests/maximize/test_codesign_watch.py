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


def _stamp(epoch: float) -> str:
    """``epoch`` the way ``log stream --style ndjson`` writes it (+0900)."""
    return datetime.fromtimestamp(epoch, tz=timezone(timedelta(hours=9))).strftime(
        "%Y-%m-%d %H:%M:%S.%f%z"
    )


def _kernel(at: float | None = None) -> dict:
    """:data:`KERNEL`, happening at ``at`` (default: now)."""
    return {**KERNEL, "timestamp": _stamp(time.time() if at is None else at)}


def test_the_ndjson_timestamp_is_the_events_time():
    # the exact shape `log stream --style ndjson` (and `log show`) writes
    line = json.dumps({**KERNEL, "timestamp": "2026-10-04 22:24:19.003000+0900"})
    expected = datetime(2026, 10, 4, 13, 24, 19, 3000, tzinfo=timezone.utc).timestamp()
    assert cw.parse_event(line)["at"] == expected == 1791120259.003
    for text in (
        "2026-10-04T22:24:19.003+09:00", "2026-10-04T13:24:19.003Z",
        "2026-10-04 22:24:19.003000 +0900", "2026-10-04 06:24:19.003-0700",
    ):
        assert cw.parse_log_time(text) == pytest.approx(expected), text
    assert cw.parse_log_time("2026-10-04 22:24:19+0900") == pytest.approx(expected - 0.003)
    for bad in (None, "", "yesterday", "2026-13-04 22:24:19+0900", 5):
        assert cw.parse_log_time(bad) is None
    # unparsable: the time it was read
    before = time.time()
    assert cw.parse_event(json.dumps({**KERNEL, "timestamp": "?"}))["at"] >= before


def test_old_jsonl_records_are_dated_by_their_ts():
    # 0.5.3 stored the time a line was READ as `at`
    record = {"ts": "2026-10-04 22:24:19.003000+0900", "at": 1791300000.0}
    assert cw.event_time(record) == pytest.approx(1791120259.003)
    assert cw.event_time({"ts": None, "at": 5.0}) == 5.0 and cw.event_time({}) is None


def test_a_line_read_late_is_dated_by_when_it_happened(home, root):
    link, _real = _install(home, "2.1.289")
    happened = time.time() + 5
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps(_kernel(happened)) + "\n")
    ext = cx.current_external(root)
    assert ext["lastAt"] == pytest.approx(happened, abs=1e-5)
    assert ext["probeDueAt"] == pytest.approx(happened + cx.PROBE_DELAY_S, abs=1e-5)
    # a line about a kill before this file was written is about an earlier file
    w.handle_line(json.dumps(_kernel(time.time() - 3600)) + "\n")
    assert cx.external_count(cx.current_external(root)) == 1
    # an ASP line older than PROVENANCE_WINDOW_S is not the kill's
    w.handle_line(json.dumps({
        **_asp(_real), "timestamp": _stamp(happened + 1),
    }) + "\n")
    w.handle_line(json.dumps(_kernel(happened + 2 + cw.PROVENANCE_WINDOW_S)) + "\n")
    assert cx.current_external(root)["provenance"] is None


def test_the_banner_and_noise_are_not_events():
    assert cw.parse_event("Filtering the log data using \"…\"\n") is None
    assert cw.parse_event("[]") is None


def test_a_kernel_refusal_of_the_current_claude_is_evidence_not_a_pause(home, root, sent):
    link, real = _install(home, "2.1.289")
    w = cw.Watcher(root, lambda: str(link), home=home)
    before = time.time() - 1  # the line's stamp has microseconds only
    line = _kernel()
    w.handle_line(json.dumps(line) + "\n")
    [event] = _events(root)
    assert event["kind"] == "log-event" and event["pid"] == 0 and event["process"] == "kernel"
    assert event["message"] == KERNEL["eventMessage"] and event["ts"] == line["timestamp"]
    # not a launch of cc-swap's: no mark, no pause, no notification
    assert cx.any_killed(root) is None and sent == []
    binary = cx.stat_binary(str(link))
    assert cx.engine_hold(root, binary, now=time.time()) is None
    ext = cx.current_external(root)
    assert ext is not None and ext["real"] == os.path.realpath(real)
    assert ext["sources"] == {"log stream": 1}
    # the engine's own probe is due a minute later, not during the episode
    assert ext["probeDueAt"] >= before + cx.PROBE_DELAY_S
    assert not cx.external_probe_due(root, binary, time.time())
    w.handle_line(json.dumps(_kernel()) + "\n")
    assert cx.external_count(cx.current_external(root)) == 2 and sent == []
    [rec, _] = [r for r in cx.read_jsonl(root) if r.get("kind") == "external-kill"]
    assert rec["caller"] == "log stream" and rec["real"] == os.path.realpath(real)


def test_another_files_refusal_is_recorded_but_marks_nothing(home, root, sent):
    link, _ = _install(home, "2.1.290")
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps(_kernel()) + "\n")  # names 2.1.289
    # another app's AMFI chatter is not kept at all; a line naming claude is
    amfid = {**_kernel(), "processImagePath": "/usr/libexec/amfid", "processID": 312}
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
        spawn=lambda argv: FakeStream([json.dumps(_kernel()) + "\n"]),
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


def test_the_engine_scan_records_a_report_of_the_current_file_as_evidence(home, root, sent):
    link, real = _install(home, "2.1.289")
    now = time.time() + 5  # after the file was written
    _ips(home, "claude-a.ips", str(real), now)
    w = cw.Watcher(root, lambda: str(link), home=home, clock=lambda: now)
    assert [k.file for k in w.scan(now)] == ["claude-a.ips"]
    assert [e["kind"] for e in _events(root)] == ["crash-report"]
    assert cx.any_killed(root) is None and sent == []
    ext = cx.current_external(root)
    assert ext["sources"] == {"crash report": 1} and ext["reports"] == ["claude-a.ips"]
    assert w.scan(now) == [] and len(_events(root)) == 1  # nothing new
    # an engine restart reads the same report again: counted once
    cw.Watcher(root, lambda: str(link), home=home).scan(now)
    assert cx.external_count(cx.current_external(root)) == 1


def test_a_report_of_a_claude_cc_swap_launched_still_marks_it(home, root, sent):
    link, real = _install(home, "2.1.289")
    now = time.time() + 5
    _ips(home, "claude-a.ips", str(real), now)  # pid 4242
    cx.append_record(root, {"kind": "exec", "at": now - 1, "pid": 4242, "caller": "prime"})
    cw.Watcher(root, lambda: str(link), home=home).scan(now)
    killed = cx.current_killed(root)
    assert killed is not None and killed["caller"] == "crash report" and len(sent) == 1
    assert killed["origin"] == cx.ORIGIN_CC_SWAP
    assert cx.current_external(root) is None
    # an old run with a reused pid is not it
    assert cx.cc_swap_launch(root, 4242, now + 2 * 3600) is None


def test_a_report_of_an_older_file_at_the_path_marks_nothing(home, root, sent):
    link, real = _install(home, "2.1.289")
    _ips(home, "claude-old.ips", str(real), time.time() - 3600)  # before this file
    cw.Watcher(root, lambda: str(link), home=home).scan(time.time())
    assert cx.any_killed(root) is None and sent == []
    assert cx.current_external(root) is None


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


def test_an_anonymized_report_of_the_current_file_names_its_launcher(home, root, sent):
    link, _real = _install(home, "2.1.289")
    later = time.time() + 120
    _real_shape(home, "2.1.289-y.ips", "2.1.289", _local_stamp(later))
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.scan(later + 1)
    assert cx.any_killed(root) is None and sent == []
    ext = cx.current_external(root)
    assert ext["launchers"] == {"T3 Code (Alpha)": 1}
    [event] = _events(root)
    assert event["parentProc"] == "T3 Code (Alpha)" and event["path"].endswith("2.1.289")
    [rec] = [r for r in cx.read_jsonl(root) if r.get("kind") == "external-kill"]
    assert rec["launcher"] == "T3 Code (Alpha)" and rec["pid"] == 56959


# -- re-review: old evidence never re-marks; only kernel lines count -----------------------------


def test_a_successful_run_outlives_the_old_reports_across_a_restart(home, root, sent):
    link, real = _install(home, "2.1.289")
    killed_at = os.stat(real).st_ctime + 0.05  # killed just after it was written
    _ips(home, "2.1.289-a.ips", str(real), killed_at)
    while time.time() <= killed_at + 0.02:
        time.sleep(0.01)
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.scan(time.time())
    assert cx.current_external(root)["probeDueAt"] is not None
    with cx.manual("t"):
        assert cx.run([str(link), "--version"], caller="test", timeout=10, root=root).returncode == 0
    # the run is the answer: the due probe records it without running claude
    assert cx.probe_external(root, str(link), now=time.time() + cx.PROBE_DELAY_S) == "ok"
    assert cx.current_external(root)["probe"]["by"] == "a cc-swap run"
    runs = len([r for r in cx.read_jsonl(root) if r.get("kind") == "exec"])
    assert runs == 1
    w.scan(time.time())  # the hourly scan: same old reports
    restarted = cw.Watcher(root, lambda: str(link), home=home)  # engine restart
    restarted.scan(time.time())
    ext = cx.current_external(root)
    assert ext["probeDueAt"] is None and cx.external_count(ext) == 1
    assert cx.any_killed(root) is None and sent == []
    # new evidence schedules a new probe (not before PROBE_EVERY_S after the last)
    restarted.handle_line(json.dumps(_kernel()) + "\n")
    ext = cx.current_external(root)
    assert ext["probeDueAt"] >= ext["probe"]["at"] + cx.PROBE_EVERY_S


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
        **_kernel(), "processImagePath": "/usr/libexec/syspolicyd", "processID": 99,
        "eventMessage": f"code signature error for {real}: invalid",
    }) + "\n")
    w.handle_line(json.dumps({
        **_kernel(), "processImagePath": "/usr/libexec/amfid", "processID": 98,
        "eventMessage": 'load code signature error 2 for file "2.1.289"',
    }) + "\n")
    assert len(_events(root)) == 2  # kept as evidence
    assert cx.any_killed(root) is None and sent == []
    assert cx.current_external(root) is None


def test_a_bare_name_that_is_not_a_version_never_matches(tmp_path, root, sent):
    brew = tmp_path / "homebrew" / "bin" / "claude"
    brew.parent.mkdir(parents=True)
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    w = cw.Watcher(root, lambda: str(brew), home=tmp_path)
    w.handle_line(json.dumps({
        **_kernel(), "eventMessage": 'load code signature error 2 for file "claude"',
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


# -- kills cc-swap did not launch: evidence, the engine's own probe ------------------------------


def _install_body(home: Path, version: str, body: str) -> tuple[Path, Path]:
    link, real = _install(home, version)
    real.write_text("#!/bin/sh\n" + body)
    return link, real


def _asp(real: Path | str, pid: int = 56959) -> dict:
    return {
        **_kernel(),
        "eventMessage": (
            f"(AppleSystemPolicy) ASP: Unable to apply provenance sandbox: 268451845, "
            f"{pid}, {real}"
        ),
    }


def _doctor(root: Path, home: Path, now: float | None = None):
    return SimpleNamespace(probes=SimpleNamespace(
        backup_root=root, home=home, platform="darwin", now=time.time() if now is None else now,
    ))


def _exec_records(root: Path) -> list[dict]:
    return [r for r in cx.read_jsonl(root) if r.get("kind") == "exec"]


def test_the_predicate_asks_for_provenance_sandbox_lines():
    assert '"provenance sandbox"' in cw.stream_argv()[-1]


def test_provenance_lines_are_kept_only_for_the_claude_versions_dir(home, root):
    link, real = _install(home, "2.1.289")
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps(_asp("/Users/x/.local/share/codex/bin/codex")) + "\n")
    w.handle_line(json.dumps(_asp("/opt/homebrew/bin/codex", pid=7)) + "\n")
    assert _events(root) == []
    w.handle_line(json.dumps(_asp(real)) + "\n")
    [event] = _events(root)
    assert "provenance sandbox" in event["message"]
    # evidence only: a provenance line is no kill
    assert cx.current_external(root) is None and cx.any_killed(root) is None
    prov = cw.provenance_of(cw.parse_event(json.dumps(_asp(real))))
    assert prov["pid"] == 56959 and prov["error"] == 268451845 and prov["path"] == str(real)


def test_the_provenance_line_goes_with_the_kill_after_it(home, root, sent):
    link, real = _install(home, "2.1.289")
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps(_asp(real)) + "\n")
    w.handle_line(json.dumps(_kernel()) + "\n")
    ext = cx.current_external(root)
    assert ext["provenance"]["pid"] == 56959 and "provenance sandbox" in ext["provenance"]["message"]
    [rec] = [r for r in cx.read_jsonl(root) if r.get("kind") == "external-kill"]
    assert rec["pid"] == 56959 and rec["provenance"]["error"] == 268451845
    assert sent == [] and cx.any_killed(root) is None
    # doctor: the hint names ASP and the launching app (from a crash report)
    _real_shape(home, "2.1.289-z.ips", "2.1.289", _local_stamp(time.time() + 1))
    w.scan(time.time() + 2)
    [f] = dr.check_claude_exec(_doctor(root, home))
    assert f.severity == "warn"
    assert "macOS killed claude" in f.detail and "launched by T3 Code (Alpha)" in f.detail
    assert "1 time " in f.detail  # a kernel line and its report: one kill
    assert "provenance sandbox" in f.detail
    assert "the engine checks claude --version itself shortly" in f.detail
    assert "AppleSystemPolicy" in f.fix and "quit and reopen T3 Code (Alpha)" in f.fix


def test_a_kill_line_whose_pid_is_a_cc_swap_run_is_left_to_that_run(home, root, sent):
    link, real = _install(home, "2.1.289")
    cx.append_record(root, {"kind": "exec", "at": time.time() - 1, "pid": 56959, "caller": "prime"})
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps(_asp(real)) + "\n")
    w.handle_line(json.dumps(_kernel()) + "\n")
    assert cx.current_external(root) is None and cx.any_killed(root) is None


def test_the_engine_probes_after_the_episode_and_a_working_claude_pauses_nothing(home, root, sent):
    link, _real = _install(home, "2.1.289")
    clock = Clock(time.time())
    w = cw.Watcher(root, lambda: str(link), home=home, clock=clock, spawn=lambda argv: None)
    w.handle_line(json.dumps(_kernel()) + "\n")
    w.tick()
    w._scanner.join(5)
    assert w._prober is None  # not during the episode
    clock.t += cx.PROBE_DELAY_S + 1
    w.tick()
    w._prober.join(10)
    [probe] = _exec_records(root)
    assert probe["caller"] == cx.PROBE_CALLER and probe["args"] == ["--version"]
    assert probe["exit"] == 0 and probe["manual"] is None
    ext = cx.current_external(root)
    assert ext["probe"]["result"] == "ok" and ext["probeDueAt"] is None
    assert cx.ran_ok_since(root, cx.stat_binary(str(link)), ext["lastAt"])
    assert cx.any_killed(root) is None and sent == []
    [f] = dr.check_claude_exec(_doctor(root, home))
    assert f.severity == "warn" and "not launched by cc-swap" in f.detail
    assert "cc-swap's own launches work (claude --version ran fine at" in f.detail
    # once only: the next tick has nothing due
    w.tick()
    w._prober.join(5)
    assert len(_exec_records(root)) == 1
    # a day later the finding is information only; after a week it is gone
    [f] = dr.check_claude_exec(_doctor(root, home, time.time() + cx.EXTERNAL_WARN_S + 1))
    assert f.severity == "info"
    assert dr.check_claude_exec(_doctor(root, home, time.time() + cx.EXTERNAL_RECENT_S + 1)) == []


def test_a_probe_the_os_kills_takes_the_killed_path(home, root, sent):
    from claude_swap.maximize import prime_verify as pv

    link, _real = _install_body(home, "2.1.289", "kill -9 $$\n")
    w = cw.Watcher(root, lambda: str(link), home=home)
    w.handle_line(json.dumps(_kernel()) + "\n")
    assert cx.probe_external(root, str(link), now=time.time() + cx.PROBE_DELAY_S) == "killed"
    killed = cx.current_killed(root)
    assert killed is not None and killed["caller"] == cx.PROBE_CALLER and len(sent) == 1
    verdict = pv.gate(root, str(link))
    assert not verdict.ok and verdict.cause == "killed"
    findings = dr.check_claude_exec(_doctor(root, home))
    assert [f.severity for f in findings] == ["error"]
    assert "priming is paused" in findings[0].detail


def test_probes_are_rate_limited_per_identity(home, root):
    link, _real = _install(home, "2.1.289")
    now = time.time()
    assert cx.note_external_kill(root, str(link), source="log stream", at=now, detail="")
    probed_at = now + cx.PROBE_DELAY_S
    assert cx.probe_external(root, str(link), now=probed_at) == "ok"
    assert cx.note_external_kill(root, str(link), source="log stream", at=probed_at + 5, detail="")
    due = cx.current_external(root)["probeDueAt"]
    assert due == probed_at + cx.PROBE_EVERY_S
    assert cx.probe_external(root, str(link), now=due - 1) is None
    assert len(_exec_records(root)) == 1


def test_a_storm_defers_the_probe_only_so_long(home, root):
    link, _real = _install(home, "2.1.289")
    now = time.time()
    for i in range(0, 600, 30):
        cx.note_external_kill(root, str(link), source="log stream", at=now + i, detail="")
    assert cx.current_external(root)["probeDueAt"] == now + cx.PROBE_MAX_DEFER_S


def test_a_settling_binary_is_probed_once_it_has_settled(home, root, monkeypatch):
    link, _real = _install(home, "2.1.289")
    monkeypatch.setattr(cx, "settle_seconds", lambda _root: 600.0)
    now = time.time()
    cx.note_external_kill(root, str(link), source="log stream", at=now, detail="")
    result = cx.probe_external(root, str(link), now=now + cx.PROBE_DELAY_S)
    assert result.startswith("not run: waiting for the claude update to settle")
    assert cx.current_external(root)["probeDueAt"] >= now + 500
    kinds = [r["kind"] for r in cx.read_jsonl(root) if r["kind"] != "binary-changed"]
    assert kinds == ["external-kill", "refused"]  # nothing ran


def _legacy_mark(root: Path, link: Path, caller: str) -> None:
    binary = cx.stat_binary(str(link))
    at = time.time()
    (root / cx.STATE_FILENAME).write_text(json.dumps({"killed": {
        "path": binary.path, "real": binary.real, "identity": binary.identity,
        "version": "2.1.289", "at": at, "lastAt": at, "caller": caller, "pid": 0,
        "exit": None, "count": 37, "diagnostics": None,
    }}))


def test_a_0_5_3_external_mark_no_longer_pauses_and_is_probed(home, root, sent):
    from claude_swap.maximize import prime_verify as pv

    link, _real = _install(home, "2.1.289")
    _legacy_mark(root, link, "log stream")
    # every reader: no pause, before any engine ran
    assert cx.any_killed(root) is None and cx.current_killed(root) is None
    assert cx.display_note(root) is None
    assert cx.engine_hold(root, cx.stat_binary(str(link)), now=time.time()) is None
    assert pv.gate(root, str(link)).cause != "killed"
    ext = cx.current_external(root)
    assert ext["upgraded"] is True and cx.external_count(ext) == 37
    [f] = dr.check_claude_exec(_doctor(root, home))
    assert f.severity == "warn" and "37 times" in f.detail
    # the engine probes it at once; success clears it for good
    assert cx.probe_pending(root, time.time())
    assert cx.probe_external(root, str(link), now=time.time()) == "ok"
    state = json.loads((root / cx.STATE_FILENAME).read_text())
    assert "killed" not in state and state["external"]["probe"]["result"] == "ok"
    assert sent == []


def test_a_0_5_3_mark_of_a_cc_swap_launch_still_pauses(home, root):
    link, _real = _install(home, "2.1.289")
    _legacy_mark(root, link, "prime")
    assert cx.current_killed(root) is not None
    assert cx.current_external(root) is None


def test_a_new_file_at_the_path_drops_the_old_evidence(home, root):
    link, _real = _install(home, "2.1.289")
    cx.note_external_kill(root, str(link), source="log stream", at=time.time(), detail="")
    _install(home, "2.1.290")
    cx.observe(root, cx.stat_binary(str(link)))
    assert "external" not in cx.load_state(root)
