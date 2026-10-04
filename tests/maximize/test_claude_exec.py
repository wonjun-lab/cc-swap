"""The guard around every ``claude`` cc-swap runs (maximize/claude_exec.py):
the audit record, ``DISABLE_AUTOUPDATER`` at every call site, the binary
watcher, the settle delay and the SIGKILL path. Fake ``claude`` scripts
only; ``codesign``/``xattr``/``log`` go through a patched ``_tool``."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import paths
from claude_swap.maximize import claude_exec as cx
from claude_swap.maximize import doctor as dr
from claude_swap.maximize import notify
from claude_swap.maximize import prime_verify as pv
from claude_swap.maximize import relogin as rl
from claude_swap.maximize.primer import PrimeRunResult, build_prime_argv, build_prime_env, run_prime
from tests.maximize.primer_support import Rig, StubRunner

#: The real reader, bound before conftest's stand-in replaces it per test.
REAL_READER = pv.read_claude_version

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake claude is a POSIX shell script"
)

SECRET = "sk-ant-oat01-must-never-reach-the-log-0123456789"


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


#: Writes what it saw (DISABLE_AUTOUPDATER, its args) next to itself.
ENV_DUMP = (
    'd=$(dirname "$0")\n'
    'printf "%s" "${DISABLE_AUTOUPDATER-<unset>}" > "$d/updater.txt"\n'
    'echo "2.1.9 (Claude Code)"\n'
    'echo "  --email <email>"\n'
)

#: SIGKILLs itself unless ``ok`` exists next to it.
KILLED = (
    'd=$(dirname "$0")\n'
    'if [ -f "$d/ok" ]; then echo "2.1.9 (Claude Code)"; exit 0; fi\n'
    "kill -9 $$\n"
)


def _records(root: Path) -> list[dict]:
    path = Path(root) / cx.EXEC_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _execs(root: Path) -> list[dict]:
    return [r for r in _records(root) if r.get("kind") == "exec"]


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def settle(monkeypatch):
    """Turn the settle delay on (conftest turns it off for every test)."""

    def set_to(seconds: float) -> None:
        monkeypatch.setattr(cx, "settle_seconds", lambda _root: float(seconds))

    return set_to


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    backend = notify.Backend("fake", lambda t, b: out.append((t, b)) or True)
    monkeypatch.setattr(notify, "system_backend", lambda *a, **k: backend)
    return out


@pytest.fixture
def tools(monkeypatch) -> list[list[str]]:
    """macOS diagnostics through a fake ``_tool``: what ran, canned answers."""
    calls: list[list[str]] = []

    def fake(argv, timeout):
        calls.append(list(argv))
        if argv[0] == cx.CODESIGN and "-dv" in argv:
            return 0, "", (
                "Executable=/x\nIdentifier=com.anthropic.claude-code\nCDHash=abc123\n"
                "TeamIdentifier=Q6L2SF6YDW\nTimestamp=Oct 4, 2026\nOther=dropped\n"
            )
        if argv[0] == cx.CODESIGN:
            return 0, "", ""
        if argv[0] == cx.XATTR:
            return 0, "com.apple.provenance\n", ""
        if argv[0] == cx.LOG:
            return 0, "kernel: AMFI: killed claude (CODE SIGNING)\n", ""
        return None, "", "unexpected"

    monkeypatch.setattr(cx, "_tool", fake)
    monkeypatch.setattr(cx, "_is_macos", lambda: True)
    return calls


# -- the record ------------------------------------------------------------------------------


def test_a_run_records_the_binary_and_how_it_ended(tmp_path, root):
    real = _script(tmp_path / "versions" / "2.1.9", ENV_DUMP)
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(real)
    done = cx.run([str(link), "--version"], caller="test", timeout=10, root=root)
    assert done.returncode == 0 and "2.1.9" in done.stdout
    [rec] = _execs(root)
    st = os.stat(real)
    assert rec["caller"] == "test" and rec["args"] == ["--version"]
    assert rec["path"] == str(link) and rec["real"] == os.path.realpath(real)
    assert (rec["inode"], rec["size"], rec["mtimeNs"], rec["ctimeNs"]) == (
        st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )
    assert rec["linkMtimeNs"] == os.lstat(link).st_mtime_ns
    assert "birthtimeNs" in rec and rec["ageS"] >= 0
    assert isinstance(rec["pid"], int) and rec["exit"] == 0 and rec["signal"] is None
    assert rec["durationS"] >= 0 and rec["timedOut"] is False and rec["manual"] is None
    # ... and the same line in the cc-swap log's structured form
    assert rec["identity"] == [rec["real"], st.st_ino, st.st_mtime_ns, st.st_size]


def test_the_record_never_holds_the_environment_tokens_or_emails(tmp_path, root, monkeypatch):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    monkeypatch.setenv("CC_SWAP_TEST_SECRET_VALUE", "hunter2-value")
    cx.run(
        [str(claude), "auth", "login", "--email", "someone@example.com", SECRET],
        caller="test", timeout=10, root=root, env={"CLAUDE_CODE_OAUTH_TOKEN": SECRET},
    )
    text = (root / cx.EXEC_LOG_FILENAME).read_text()
    assert SECRET not in text and "someone@example.com" not in text
    assert "hunter2-value" not in text
    [rec] = _execs(root)
    assert rec["args"] == ["auth", "login", "--email", "<email>", "<redacted>"]


def test_the_jsonl_moves_aside_past_its_cap(root, monkeypatch):
    monkeypatch.setattr(cx, "EXEC_LOG_MAX_BYTES", 2000)
    for i in range(40):
        cx.append_record(root, {"kind": "exec", "n": i, "pad": "x" * 100})
    log = root / cx.EXEC_LOG_FILENAME
    assert log.stat().st_size <= 2000
    assert (root / (cx.EXEC_LOG_FILENAME + ".1")).stat().st_size <= 2000
    assert _records(root)[-1]["n"] == 39
    assert oct(log.stat().st_mode & 0o777) == oct(0o600)


# -- DISABLE_AUTOUPDATER at every call site ---------------------------------------------------


def _updater(claude: Path) -> str:
    return (claude.parent / "updater.txt").read_text()


def test_the_priming_guards_version_read_disables_the_updater(tmp_path, monkeypatch):
    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    claude = _script(tmp_path / "claude", ENV_DUMP)
    assert REAL_READER(str(claude)) == "2.1.9"
    assert _updater(claude) == "1"
    [rec] = _execs(paths.get_backup_root())
    assert rec["caller"] == "claude --version (priming guard)"


def test_a_priming_launch_and_the_verify_probe_disable_the_updater(tmp_path):
    from tests.maximize.fake_claude import FakeClaude

    fake = FakeClaude.install(tmp_path / "bin")
    env = build_prime_env({"PATH": os.environ.get("PATH", "")}, tmp_path, "sk-ant-oat01-abcdefgh")
    run_prime(build_prime_argv(str(fake.path), "m"), env, tmp_path)
    pv._default_runner(build_prime_argv(str(fake.path), "m"), env, tmp_path, 30.0)
    calls = fake.calls()
    assert [c["env"].get("DISABLE_AUTOUPDATER") for c in calls] == ["1", "1"]
    assert all(c["env"]["CLAUDE_CODE_OAUTH_TOKEN"] for c in calls)  # still the token
    callers = [r["caller"] for r in _execs(paths.get_backup_root())]
    assert callers == ["prime", "prime verify probe"]
    assert SECRET not in (paths.get_backup_root() / cx.EXEC_LOG_FILENAME).read_text()


def test_relogin_and_its_help_probe_disable_the_updater(tmp_path):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    assert rl.login_supported(str(claude))
    assert _updater(claude) == "1"
    (claude.parent / "updater.txt").unlink()
    code = rl.run_interactive(rl.login_argv(str(claude), "a@example.com"), {}, tmp_path)
    assert code == 0 and _updater(claude) == "1"
    recs = _execs(paths.get_backup_root())
    assert [r["caller"] for r in recs] == ["claude auth login --help", "claude auth login"]
    assert all(r["manual"] == "Fleet: re-login" for r in recs)
    assert recs[1]["args"][-1] == "<email>"


def test_doctors_version_read_disables_the_updater(tmp_path):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    done = dr._run_claude([str(claude), "--version"], 10.0)
    assert done.rc == 0 and _updater(claude) == "1"


def test_cswap_runs_auth_status_probe_disables_the_updater(tmp_path):
    from claude_swap.session import _probe_env

    assert _probe_env(tmp_path)["DISABLE_AUTOUPDATER"] == "1"


def test_cc_swap_never_runs_claude_update():
    from claude_swap import cli

    assert importlib.util.find_spec("claude_swap.maximize.claude_update") is None
    assert "claude-update" not in cli._FORK_COMMANDS
    assert "update" not in {e.action for e in __import__("claude_swap.tui.menus").tui.menus.MAIN_MENU}
    # Structurally: no argv list anywhere in the package puts "update" right
    # after a claude executable (`[claude, "update"]` and its spellings).
    import ast

    offenders = []
    for path in Path(cx.__file__).parents[1].rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
                continue
            first, second = node.elts[0], node.elts[1]
            name = getattr(first, "id", None) or getattr(first, "attr", None) or ""
            if (
                "claude" in name.lower()
                and isinstance(second, ast.Constant) and second.value == "update"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_the_cswap_run_session_keeps_claudes_own_updater(tmp_path, monkeypatch):
    """`cswap run` hands the terminal to the user's own claude: its updater
    stays as the user has it (only cc-swap's own children turn it off)."""
    from claude_swap.session import SessionManager

    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    seen = {}

    def fake_exec(path, argv, env):
        seen["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvpe", fake_exec)
    manager = SessionManager.__new__(SessionManager)
    with pytest.raises(SystemExit):
        manager._exec(str(_script(tmp_path / "claude", ENV_DUMP)), [], dict(os.environ))
    assert "DISABLE_AUTOUPDATER" not in seen["env"]
    [rec] = _execs(paths.get_backup_root())
    assert rec["caller"] == "cswap run" and rec["note"].startswith("exec:")


def test_an_interrupted_run_is_not_marked_killed(tmp_path, root, monkeypatch):
    import subprocess

    claude = _script(tmp_path / "claude", "sleep 30\n")
    real_communicate = subprocess.Popen.communicate

    def interrupted(self, *a, **k):
        if k.get("timeout") is not None and k["timeout"] > 1:
            raise KeyboardInterrupt
        return real_communicate(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with cx.manual("t"), pytest.raises(KeyboardInterrupt):
        cx.run([str(claude)], caller="test", timeout=10, root=root)
    [rec] = _execs(root)
    assert rec["error"] == "KeyboardInterrupt"
    assert "killed" not in cx.load_state(root)


def test_auth_status_probe_killed_by_the_os_is_unknown_not_invalid(tmp_path, monkeypatch):
    """A SIGKILLed `claude auth status` says nothing about the session
    profile, which must not be deleted for it."""
    import subprocess

    from claude_swap import session as session_mod

    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(
        session_mod.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], -9, "", ""),
    )
    manager = session_mod.SessionManager.__new__(session_mod.SessionManager)
    assert manager._session_validity(profile, "a@example.com", "") == "unknown"


# -- the watcher --------------------------------------------------------------------------------


def test_a_new_binary_is_logged_once_with_its_signing_facts(tmp_path, root, tools):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    cx.observe(root, cx.stat_binary(str(claude)))
    cx.observe(root, cx.stat_binary(str(claude)))
    [rec] = [r for r in _records(root) if r["kind"] == "binary-changed"]
    assert rec["old"] is None and rec["new"]["real"] == os.path.realpath(claude)
    assert rec["codesign"] == [
        "Identifier=com.anthropic.claude-code", "CDHash=abc123",
        "TeamIdentifier=Q6L2SF6YDW", "Timestamp=Oct 4, 2026",
    ]
    assert rec["codesignVerify"] == "ok" and rec["xattrs"] == ["com.apple.provenance"]


def test_a_binary_rewritten_in_place_after_a_run_is_a_loud_warning(tmp_path, root, caplog):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
    ino = os.stat(claude).st_ino
    with open(claude, "a") as fh:  # same inode, new bytes
        fh.write("# rewritten\n")
    assert os.stat(claude).st_ino == ino
    with caplog.at_level("WARNING", logger="claude-swap"):
        cx.observe(root, cx.stat_binary(str(claude)))
    [msg] = [r.getMessage() for r in caplog.records if "rewritten in place" in r.getMessage()]
    assert f"claude binary at {os.path.realpath(claude)} was rewritten in place after it had been executed (by cc-swap at" in msg
    assert "same inode" in msg
    state = cx.load_state(root)
    assert state["rewritten"]["sameInode"] is True
    assert [r for r in _records(root) if r["kind"] == "rewritten-after-exec"]
    # doctor repeats it as a warning
    ctx = _doctor_ctx(root, tmp_path)
    [f] = dr.check_claude_exec(ctx)
    assert f.severity == "warn" and "rewritten in place (same inode)" in f.detail
    # once: observing again says nothing new
    caplog.clear()
    with caplog.at_level("WARNING", logger="claude-swap"):
        cx.observe(root, cx.stat_binary(str(claude)))
    assert not [r for r in caplog.records if "rewritten" in r.getMessage()]


def test_a_replaced_binary_before_any_run_is_no_rewrite(tmp_path, root, caplog):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    cx.observe(root, cx.stat_binary(str(claude)))
    _script(tmp_path / "claude", ENV_DUMP + "# v2\n")
    with caplog.at_level("WARNING", logger="claude-swap"):
        cx.observe(root, cx.stat_binary(str(claude)))
    assert not [r for r in caplog.records if "rewritten" in r.getMessage()]


# -- the settle delay --------------------------------------------------------------------------


def test_the_engine_never_runs_a_fresh_binary(tmp_path, root, settle):
    settle(600)
    claude = _script(tmp_path / "claude", ENV_DUMP)
    with pytest.raises(cx.ExecRefused, match=r"waiting for the claude update to settle \(\d+s left\)"):
        cx.run([str(claude), "--version"], caller="engine", timeout=10, root=root)
    assert not (claude.parent / "updater.txt").exists()  # never started
    [rec] = _records(root)[-1:]
    assert rec["kind"] == "refused" and "settle" in rec["reason"]


def test_the_symlinks_own_mtime_counts_too(tmp_path, settle):
    real = _script(tmp_path / "versions" / "2.1.9", ENV_DUMP)
    old = time.time() - 3600
    os.utime(real, (old, old))
    link = tmp_path / "claude"
    link.symlink_to(real)
    binary = cx.stat_binary(str(link))
    assert binary.link_mtime_ns is not None
    # ctime and the link are new: still settling
    assert cx.settle_left(binary, 600, time.time()) > 500
    assert cx.settle_left(binary, 600, time.time() + 700) == 0


def test_a_user_command_runs_it_with_a_warning(tmp_path, root, settle):
    settle(600)
    claude = _script(tmp_path / "claude", ENV_DUMP)
    warnings: list[str] = []
    with cx.manual("cc-swap prime verify", warn=warnings.append):
        done = cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
    assert done.returncode == 0
    [w] = warnings
    assert w.startswith("Warning: ") and "running it anyway for cc-swap prime verify" in w
    [rec] = _execs(root)
    assert rec["manual"] == "cc-swap prime verify" and rec["settleOverrideS"] > 500


def test_the_engine_defers_priming_and_says_why(temp_home, tmp_path, monkeypatch, settle):
    from claude_swap.autoswitch import ConfigWarningEvent

    rig = Rig(temp_home, tmp_path, monkeypatch)
    root = rig.switcher.backup_dir
    settle(600)
    runner = StubRunner(rig)
    primer = rig.primer(runner=runner)
    events = primer.run_due(rig.snap())
    [warning] = [e for e in events if isinstance(e, ConfigWarningEvent)]
    assert "waiting for the claude update to settle" in warning.message
    assert runner.calls == [] and rig.primes() == {}
    note = pv.paused_note(root)
    assert note is not None and note.startswith("paused: waiting for the claude update to settle (")
    assert primer.run_due(rig.snap()) == []  # warned once per binary
    # doctor: no --version while settling, and the wait is named
    ctx = _doctor_ctx(root, tmp_path)
    assert any("waiting for the claude update" in f.detail for f in dr.check_claude_exec(ctx))
    settle(0)
    primer.run_due(rig.snap())
    assert len(runner.calls) == 1
    assert pv.paused_note(root) is None and "settling" not in cx.load_state(root)


# -- SIGKILL ------------------------------------------------------------------------------------


def _doctor_ctx(root: Path, home: Path):
    probes = SimpleNamespace(backup_root=root, home=home, platform="darwin", now=time.time())
    return SimpleNamespace(probes=probes)


def test_a_sigkill_is_recorded_notified_once_and_diagnosed(tmp_path, root, sent, tools):
    claude = _script(tmp_path / "claude", KILLED)
    real = os.path.realpath(claude)
    with cx.manual("cc-swap prime verify"):
        done = cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
        assert done.returncode == -9
        cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
    recs = _execs(root)
    assert [r["signal"] for r in recs] == [9, 9] and recs[0]["exit"] == -9
    killed = cx.load_state(root)["killed"]
    assert killed["real"] == real and killed["count"] == 2
    assert killed["identity"] == cx.stat_binary(str(claude)).identity
    # one notification for this binary
    assert len(sent) == 1 and "killed" in sent[0][0]
    # diagnostics: the record, codesign, xattr names, log show
    [diag] = list(root.glob(f"{cx.KILL_PREFIX}*.txt"))
    text = diag.read_text()
    assert killed["diagnostics"] == str(diag)
    assert '"signal": 9' in text and "Identifier=com.anthropic.claude-code" in text
    assert "com.apple.provenance" in text and "AMFI" in text
    [log_show] = [c for c in tools if c[0] == cx.LOG]
    predicate = log_show[log_show.index("--predicate") + 1]
    assert f"processID == {recs[0]['pid']}" in predicate and "AMFI" in predicate
    assert log_show[log_show.index("--last") + 1] == "2m"
    # doctor: an error with the fix (never applied)
    [f] = dr.check_claude_exec(_doctor_ctx(root, tmp_path))
    assert f.severity == "error"
    assert f"macOS is killing {real} at launch (code-signing cache). Fix: cp -p {real} {real}.tmp && mv {real}.tmp {real}" in f.detail
    assert f.fix.startswith(f"cp -p {real} {real}.tmp && mv {real}.tmp {real}")
    assert os.stat(claude).st_ino == killed["identity"][1]  # nothing was moved


def test_a_sigkill_through_a_wrapper_shell_counts_too(tmp_path, root):
    claude = _script(tmp_path / "claude", "sh -c 'kill -9 $$'\nexit $?\n")
    with cx.manual("t"):
        done = cx.run([str(claude)], caller="test", timeout=10, root=root)
    assert done.returncode == 137
    [rec] = _execs(root)
    assert rec["signal"] == 9 and rec["signalViaShell"] is True
    assert "killed" in cx.load_state(root)


def test_our_own_timeout_kill_is_not_the_os(tmp_path, root):
    import subprocess

    claude = _script(tmp_path / "claude", "sleep 30\n")
    with cx.manual("t"), pytest.raises(subprocess.TimeoutExpired):
        cx.run([str(claude)], caller="test", timeout=0.5, root=root)
    [rec] = _execs(root)
    assert rec["timedOut"] is True and "killed" not in cx.load_state(root)


def test_a_killed_binary_pauses_priming_and_the_engine_never_reruns_it(
    temp_home, tmp_path, monkeypatch, sent
):
    rig = Rig(temp_home, tmp_path, monkeypatch)
    root = rig.switcher.backup_dir
    claude = _script(tmp_path / "kbin" / "claude", KILLED)
    with cx.manual("t"):
        cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
    verdict = pv.gate(root, str(claude))
    assert not verdict.ok and verdict.cause == "killed"
    assert "killed by the OS" in verdict.reason
    assert pv.paused_note(root) == f"paused: {os.path.realpath(claude)} is killed by the OS at launch (SIGKILL; see cc-swap doctor)"
    # the engine refuses to run it again (no second kill, no verify)
    with pytest.raises(cx.ExecRefused, match="killed by the OS"):
        cx.run([str(claude), "--version"], caller="engine", timeout=10, root=root)
    runner = StubRunner(rig)
    events = rig.primer(runner=runner, claude_path=str(claude)).run_due(rig.snap())
    assert runner.calls == [] and any("killed by the OS" in getattr(e, "message", "") for e in events)
    assert pv.failed_verify(root) is None and pv.failed_versions(root) == []
    assert len(sent) == 1


def test_the_mark_clears_after_a_successful_run_of_the_same_file(tmp_path, root, sent):
    claude = _script(tmp_path / "claude", KILLED)
    with cx.manual("t"):
        cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
        assert cx.any_killed(root) is not None
        (claude.parent / "ok").write_text("")  # same file, now it runs
        assert cx.run([str(claude), "--version"], caller="test", timeout=10, root=root).returncode == 0
    assert cx.any_killed(root) is None and cx.display_note(root) is None


def test_the_mark_clears_when_the_file_gets_a_new_identity(tmp_path, root, sent):
    claude = _script(tmp_path / "claude", KILLED)
    with cx.manual("t"):
        cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
    # the documented fix: a copy under a new inode moved over the original
    tmp = claude.with_name("claude.tmp")
    tmp.write_bytes(claude.read_bytes())
    tmp.chmod(0o755)
    os.replace(tmp, claude)
    cx.observe(root, cx.stat_binary(str(claude)))
    assert cx.any_killed(root) is None


def test_a_killed_verify_probe_is_not_a_failed_verify(tmp_path, root):
    creds, config = tmp_path / "creds.json", tmp_path / "claude.json"
    deps = pv.VerifyDeps(
        run=lambda argv, env, cwd, timeout: PrimeRunResult(-9, False, ""),
        version=lambda _p: "2.1.9",
        keychain_attrs=lambda s: None,
        keychain_delete=lambda s: None,
        macos=lambda: False,
        active_services=lambda: [],
        credentials_path=lambda: creds,
        config_path=lambda: config,
    )
    report = pv.run_verify(root, str(_script(tmp_path / "claude", ENV_DUMP)), deps=deps)
    assert report.killed and not report.ok and report.transient
    assert pv.failed_verify(root) is None and pv.failed_versions(root) == []


# -- settings ---------------------------------------------------------------------------------


def test_settle_s_is_a_setting_with_a_600s_default(tmp_path):
    from claude_swap import settings as st

    assert st.load_claude_settings(tmp_path).settle_s == 600
    st.set_setting(tmp_path, "claude.settleS", "120")
    assert st.load_claude_settings(tmp_path).settle_s == 120
    assert cx.settle_seconds_real(tmp_path) == 120.0
    with pytest.raises(Exception):
        st.set_setting(tmp_path, "claude.settleS", "-1")


# -- review fixes: a mark follows the launcher path -------------------------------------------


def _versioned(tmp_path: Path, version: str, body: str) -> tuple[Path, Path]:
    """A native-installer layout: ``bin/claude`` -> ``versions/<version>``."""
    real = _script(tmp_path / "versions" / version, body)
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir(exist_ok=True)
    if link.is_symlink():
        link.unlink()
    link.symlink_to(real)
    return link, real


def test_a_new_version_at_a_new_real_path_clears_the_mark_and_the_note(tmp_path, root, sent):
    link, _old = _versioned(tmp_path, "2.1.289", KILLED)
    with cx.manual("t"):
        cx.run([str(link), "--version"], caller="test", timeout=10, root=root)
    assert cx.display_note(root) is not None
    assert dr.check_claude_exec(_doctor_ctx(root, tmp_path))[0].severity == "error"
    # Claude Code updates: the launcher now points at another file.
    _versioned(tmp_path, "2.1.290", ENV_DUMP)
    # Before anything runs it, nothing reports the stale mark ...
    assert cx.display_note(root) is None and pv.paused_note(root) is None
    assert dr.check_claude_exec(_doctor_ctx(root, tmp_path)) == []
    # ... and the next look at the binary clears it.
    cx.observe(root, cx.stat_binary(str(link)))
    assert cx.any_killed(root) is None


def test_the_fix_and_a_new_inode_are_no_rewrite(tmp_path, root, caplog):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
    tmp = claude.with_name("claude.tmp")
    tmp.write_bytes(claude.read_bytes())
    tmp.chmod(0o755)
    os.replace(tmp, claude)  # cp -p claude claude.tmp && mv claude.tmp claude
    with caplog.at_level("WARNING", logger="claude-swap"):
        cx.observe(root, cx.stat_binary(str(claude)))
    assert not [r for r in caplog.records if "rewritten" in r.getMessage()]
    assert "rewritten" not in cx.load_state(root)
    assert dr.check_claude_exec(_doctor_ctx(root, tmp_path)) == []


def test_doctors_run_is_record_only(tmp_path, root, sent, monkeypatch):
    claude = _script(tmp_path / "claude", KILLED)
    monkeypatch.setattr(cx, "_default_root", lambda: root)
    done = dr._run_claude([str(claude), "--version"], 10.0)
    assert done.rc == -9
    [rec] = _execs(root)
    assert rec["caller"] == "claude --version (doctor)" and rec["signal"] == 9
    assert cx.load_state(root) == {}  # no mark, no binaries, no executed
    assert sent == [] and not list(root.glob(f"{cx.KILL_PREFIX}*"))


def test_a_busy_state_lock_skips_the_write(root):
    from claude_swap.locking import FileLock

    lock = FileLock(root / cx.LOCK_FILENAME, timeout=0)
    assert lock.acquire()
    try:
        started = time.monotonic()
        cx._mutate(root, lambda state: state.update(x=1) or True)
        assert time.monotonic() - started >= 4.5
    finally:
        lock.release()
    assert cx.load_state(root) == {}


# -- B: xattrs and ctime before and after each run -----------------------------------------


def test_each_run_records_xattrs_and_ctime_before_and_after(tmp_path, root):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
    [rec] = _execs(root)
    assert rec["xattrsBefore"] == rec["xattrsAfter"]
    assert isinstance(rec["ctimeAfterNs"], int) and rec["ctimeAfterNs"] == rec["ctimeNs"]
    assert "binaryChangedByRun" not in rec


def test_a_run_that_changes_the_binarys_xattrs_is_a_warning(tmp_path, root, monkeypatch, caplog):
    claude = _script(tmp_path / "claude", ENV_DUMP)
    answers = iter([[], ["com.apple.provenance"]])
    monkeypatch.setattr(cx, "xattr_names", lambda path: next(answers))
    with caplog.at_level("WARNING", logger="claude-swap"):
        cx.run([str(claude), "--version"], caller="test", timeout=10, root=root)
    [rec] = _execs(root)
    assert rec["xattrsBefore"] == [] and rec["xattrsAfter"] == ["com.apple.provenance"]
    assert rec["binaryChangedByRun"] is True
    [msg] = [r.getMessage() for r in caplog.records if "changed during a cc-swap run" in r.getMessage()]
    assert "xattrs [] -> ['com.apple.provenance']" in msg


@pytest.mark.skipif(sys.platform != "darwin", reason="listxattr through ctypes is macOS")
def test_xattr_names_reads_names_without_a_subprocess(tmp_path, monkeypatch):
    import subprocess

    path = tmp_path / "f"
    path.write_text("x")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no subprocess"))
    assert isinstance(cx.xattr_names(str(path)), list)
    assert cx.xattr_names(str(tmp_path / "missing")) is None


# -- kills seen outside cc-swap's runs -------------------------------------------------------


def test_a_kill_seen_elsewhere_marks_the_current_file_once(tmp_path, root, sent):
    link, real = _versioned(tmp_path, "2.1.289", ENV_DUMP)
    now = time.time()
    assert cx.mark_killed_by_os(root, str(link), source="crash report", at=now, detail="x.ips")
    assert not cx.mark_killed_by_os(root, str(link), source="crash report", at=now, detail="x.ips")
    killed = cx.current_killed(root)
    assert killed is not None and killed["real"] == os.path.realpath(real)
    assert killed["caller"] == "crash report" and len(sent) == 1
    # a kill older than the file at that path is about an earlier file
    assert not cx.mark_killed_by_os(root, str(link), source="x", at=now - 86400, detail="")


def test_the_login_help_probe_runs_in_a_scrubbed_throwaway_profile(tmp_path, monkeypatch):
    claude = _script(tmp_path / "claude", (
        'd=$(dirname "$0")\n'
        'printf "%s|%s|%s" "${CLAUDE_CONFIG_DIR-}" "${CLAUDE_CODE_OAUTH_TOKEN-none}" '
        '"${ANTHROPIC_API_KEY-none}" > "$d/env.txt"\n'
        'echo "  --email <email>"\n'
    ))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-should-not-pass")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "live-profile"))
    assert rl.login_supported(str(claude))
    config_dir, token, key = (tmp_path / "env.txt").read_text().split("|")
    assert token == "none" and key == "none"
    assert config_dir and "cc-swap-login-probe-" in config_dir
    assert not Path(config_dir).exists()  # removed afterwards
