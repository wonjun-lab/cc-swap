"""`cc-swap claude-update [--check] [--json]` (maximize/claude_update.py).

Everything runs against a fake ``claude`` script in a temp dir and a mocked
``urllib.request.urlopen``; the real ``claude`` and the network are never
touched.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, paths
from claude_swap.locking import FileLock
from claude_swap.maximize import claude_update as cu
from claude_swap.settings import atomic_write_json, settings_path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake claude is a POSIX shebang script"
)

FAKE_BODY = r'''
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "behavior.json")
with open(CONFIG, encoding="utf-8") as f:
    cfg = json.load(f)
argv = sys.argv[1:]
with open(os.path.join(HERE, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv}) + "\n")

if argv[:1] == ["--version"]:
    if cfg.get("versionFails"):
        print("boom", file=sys.stderr)
        sys.exit(1)
    print(cfg["version"] + " (Claude Code)")
    sys.exit(0)
if argv[:1] == ["update"]:
    print(cfg.get("updateOutput", "Checking for updates..."), flush=True)
    time.sleep(cfg.get("updateSleep", 0))
    code = cfg.get("updateExit", 0)
    if cfg.get("versionAfter") and (code == 0 or cfg.get("applyOnFailure")):
        cfg["version"] = cfg["versionAfter"]
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    sys.exit(code)
sys.exit(2)
'''


class FakeClaude:
    def __init__(self, directory: Path, **behavior):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "claude"
        self.path.write_text(f"#!{sys.executable}\n{FAKE_BODY}")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self.config = directory / "behavior.json"
        self.calls_file = directory / "calls.jsonl"
        self.set(**behavior)

    def set(self, **behavior):
        self.config.write_text(json.dumps(behavior))

    def calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line)["argv"] for line in self.calls_file.read_text().splitlines()]

    def update_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:1] == ["update"]]


class FakeHttp:
    """Stands in for ``urllib.request.urlopen``."""

    def __init__(self):
        self.urls: list[str] = []
        self.body: bytes | Exception = json.dumps(
            {"stable": "2.1.285", "latest": "2.1.287", "next": "2.1.287"}
        ).encode()

    def tags(self, **tags):
        self.body = json.dumps(tags).encode()

    def __call__(self, request, timeout=None):
        self.urls.append(getattr(request, "full_url", request))
        if isinstance(self.body, Exception):
            raise self.body
        return io.BytesIO(self.body)


@pytest.fixture
def root(temp_home):
    r = paths.get_backup_root()
    r.mkdir(parents=True, exist_ok=True)
    return r


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(cu.urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def fake(root, tmp_path):
    f = FakeClaude(tmp_path / "bin", version="2.1.280")
    atomic_write_json(
        settings_path(root), {"schemaVersion": 1, "prime": {"claudePath": str(f.path)}}
    )
    return f


def state_of(root: Path) -> dict:
    p = root / "autoswitch_state.json"
    return json.loads(p.read_text()) if p.exists() else {}


# -- pure helpers ----------------------------------------------------------


def test_parse_version_reads_the_number_out_of_claude_version_output():
    assert cu.parse_version("2.1.287 (Claude Code)\n") == "2.1.287"
    assert cu.parse_version("garbage") is None
    assert cu.parse_version(None) is None


def test_is_newer_orders_numerically_not_lexically():
    assert cu.is_newer("2.1.287", "2.1.99")
    assert not cu.is_newer("2.1.285", "2.1.287")
    assert not cu.is_newer("2.1.287", "2.1.287")


def test_a_prerelease_build_is_older_than_its_release():
    assert cu.is_newer("2.2.0", "2.2.0-beta.1")
    assert not cu.is_newer("2.2.0-beta.1", "2.2.0")


# -- finding claude ----------------------------------------------------------


def test_configured_claude_path_wins(root, tmp_path):
    f = FakeClaude(tmp_path / "x", version="1.0.0")
    atomic_write_json(settings_path(root), {"schemaVersion": 1, "prime": {"claudePath": str(f.path)}})
    assert cu.find_claude(root) == str(f.path)


def test_falls_back_to_the_native_install_location(root, temp_home):
    native = FakeClaude(temp_home / ".local" / "bin", version="1.0.0")
    assert cu.find_claude(root) == str(native.path)


def test_shutil_which_is_only_the_last_fallback(root, tmp_path):
    f = FakeClaude(tmp_path / "onpath", version="1.0.0")
    with patch.object(cu.shutil, "which", return_value=str(f.path)) as which:
        assert cu.find_claude(root) == str(f.path)
    which.assert_called_once_with("claude")


def test_configured_path_beats_which(root, fake):
    with patch.object(cu.shutil, "which", return_value="/somewhere/else/claude") as which:
        assert cu.find_claude(root) == str(fake.path)
    which.assert_not_called()


def test_no_claude_anywhere(root):
    with patch.object(cu.shutil, "which", return_value=None):
        assert cu.find_claude(root) is None


def test_installed_version_runs_claude_version(fake):
    assert cu.installed_version(str(fake.path)) == "2.1.280"
    assert fake.calls() == [["--version"]]


def test_installed_version_is_none_when_claude_is_broken(fake):
    fake.set(version="2.1.280", versionFails=True)
    assert cu.installed_version(str(fake.path)) is None


# -- the latest version ------------------------------------------------------


def test_latest_comes_from_the_npm_dist_tags(http):
    assert cu.latest_version("latest") == "2.1.287"
    assert http.urls == ["https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"]


def test_stable_channel_reads_the_stable_tag(http):
    assert cu.latest_version("stable") == "2.1.285"


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"latest": 5}', b'{"latest": "rm -rf"}', b"{}"],
)
def test_unusable_registry_answers_are_none(http, body):
    http.body = body
    assert cu.latest_version("latest") is None


def test_network_failure_is_none(http):
    http.body = urllib.error.URLError("offline")
    assert cu.latest_version("latest") is None


def test_channel_defaults_to_latest(temp_home):
    assert cu.update_channel() == "latest"


def test_channel_follows_claude_codes_auto_updates_channel_setting(temp_home):
    (temp_home / ".claude" / "settings.json").write_text('{"autoUpdatesChannel": "stable"}')
    assert cu.update_channel() == "stable"


def test_a_corrupt_claude_settings_file_means_latest(temp_home):
    (temp_home / ".claude" / "settings.json").write_text("{nope")
    assert cu.update_channel() == "latest"


# -- --check -----------------------------------------------------------------


def test_check_up_to_date_exits_0(fake, http, capsys):
    fake.set(version="2.1.287")
    assert cu.run(["--check"]) == 0
    out = capsys.readouterr().out
    assert "2.1.287" in out and "up to date" in out.lower()


def test_check_update_available_exits_10(fake, http, capsys):
    assert cu.run(["--check"]) == 10
    out = capsys.readouterr().out
    assert "2.1.280" in out and "2.1.287" in out and "claude-update" in out


def test_check_changes_nothing(fake, http, root):
    cu.run(["--check"])
    assert fake.update_calls() == []
    assert state_of(root) == {}


def test_check_never_downgrades_a_newer_install(fake, http):
    fake.set(version="2.2.0")
    assert cu.run(["--check"]) == 0


def test_check_honours_the_stable_channel(fake, http, temp_home, capsys):
    (temp_home / ".claude" / "settings.json").write_text('{"autoUpdatesChannel": "stable"}')
    fake.set(version="2.1.285")
    assert cu.run(["--check"]) == 0  # latest is 2.1.287, but stable is 2.1.285


def test_check_exits_1_when_claude_is_missing(root, http, capsys):
    with patch.object(cu.shutil, "which", return_value=None):
        assert cu.run(["--check"]) == 1
    assert "prime.claudePath" in capsys.readouterr().err


def test_check_exits_1_when_the_version_is_unreadable(fake, http):
    fake.set(version="2.1.280", versionFails=True)
    assert cu.run(["--check"]) == 1


def test_check_exits_1_when_the_registry_is_unreachable(fake, http, capsys):
    http.body = urllib.error.URLError("offline")
    assert cu.run(["--check"]) == 1
    assert "registry" in capsys.readouterr().err.lower()


def test_check_json(fake, http, capsys):
    assert cu.run(["--check", "--json"]) == 10
    data = json.loads(capsys.readouterr().out)
    assert data["schemaVersion"] == 1
    assert data["installed"] == "2.1.280"
    assert data["latest"] == "2.1.287"
    assert data["channel"] == "latest"
    assert data["updateAvailable"] is True
    assert data["claudePath"] == str(fake.path)
    assert data["source"].startswith("https://registry.npmjs.org/")


def test_check_json_error_keeps_stdout_parseable(root, http, capsys):
    with patch.object(cu.shutil, "which", return_value=None):
        assert cu.run(["--check", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["installed"] is None and "error" in data


# -- the update itself -------------------------------------------------------


def test_update_runs_claude_update_and_reports_before_and_after(fake, root, capsys):
    fake.set(version="2.1.280", versionAfter="2.1.287", updateOutput="Successfully updated")
    assert cu.run([]) == 0
    out = capsys.readouterr().out
    assert fake.update_calls() == [["update"]]
    assert "Successfully updated" in out  # claude's own output is shown
    assert "2.1.280 -> 2.1.287" in out


def test_update_makes_no_http_request(fake, root, http):
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    assert http.urls == []


def test_update_records_the_version_change(fake, root):
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    state = state_of(root)
    assert state["claudeVersion"] == "2.1.287"
    assert state["claudeVersionPrevious"] == "2.1.280"
    assert state["claudeVersionChangedAt"].endswith("Z")
    assert state["schemaVersion"] == 1


def test_recording_keeps_the_engines_other_state(fake, root):
    atomic_write_json(
        root / "autoswitch_state.json",
        {"schemaVersion": 1, "quarantine": {"2": {"reason": "x"}}, "cooldownUntil": 5},
    )
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    state = state_of(root)
    assert state["quarantine"] == {"2": {"reason": "x"}}
    assert state["cooldownUntil"] == 5
    assert state["claudeVersion"] == "2.1.287"


def test_the_public_reader_returns_the_recorded_version(fake, root):
    assert cu.recorded_claude_version(root) == (None, None)
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    version, changed_at = cu.recorded_claude_version(root)
    assert version == "2.1.287" and changed_at == state_of(root)["claudeVersionChangedAt"]


def test_an_unchanged_version_leaves_the_record_alone(fake, root, capsys):
    fake.set(version="2.1.287")
    cu.run([])
    first = state_of(root)
    assert first["claudeVersion"] == "2.1.287"
    capsys.readouterr()
    cu.run([])
    assert state_of(root) == first
    assert "already" in capsys.readouterr().out.lower()


def test_update_failure_exits_1_and_records_nothing(fake, root, capsys):
    fake.set(version="2.1.280", versionAfter="2.1.287", updateExit=3, updateOutput="npm ERR")
    assert cu.run([]) == 1
    captured = capsys.readouterr()
    assert "npm ERR" in captured.out
    assert "exit" in captured.err.lower() and "3" in captured.err
    assert "claudeVersion" not in state_of(root)


def test_a_failed_update_that_still_changed_the_version_is_recorded(fake, root):
    # claude can swap the binary and then fail on a later step; the change is real.
    fake.set(version="2.1.280", updateExit=1, versionAfter="2.1.287", applyOnFailure=True)
    assert cu.run([]) == 1
    assert state_of(root)["claudeVersion"] == "2.1.287"


def test_update_timeout_kills_the_child_and_exits_1(fake, root, capsys):
    fake.set(version="2.1.280", versionAfter="2.1.287", updateSleep=30)
    assert cu.run(["--timeout", "1"]) == 1
    assert "timed out" in capsys.readouterr().err.lower()
    assert "claudeVersion" not in state_of(root)
    # the lock is released again
    with FileLock(root / cu.LOCK_FILENAME, timeout=0):
        pass


def _unrunnable_claude(root, tmp_path) -> Path:
    """An executable file the kernel cannot exec (no shebang, not a binary):
    Popen raises OSError (ENOEXEC) for it."""
    bad = tmp_path / "badbin" / "claude"
    bad.parent.mkdir(parents=True)
    bad.write_text("this is not a program\n")
    bad.chmod(0o755)
    atomic_write_json(
        settings_path(root), {"schemaVersion": 1, "prime": {"claudePath": str(bad)}}
    )
    return bad


def test_a_claude_that_cannot_be_started_is_a_clean_error(root, tmp_path, capsys):
    _unrunnable_claude(root, tmp_path)
    assert cu.run([]) == 1
    err = capsys.readouterr().err
    assert "could not run" in err and "claude update" in err
    assert "Traceback" not in err
    assert "claudeVersion" not in state_of(root)


def test_a_claude_that_cannot_be_started_releases_the_lock(root, tmp_path):
    _unrunnable_claude(root, tmp_path)
    cu.run([])
    with FileLock(root / cu.LOCK_FILENAME, timeout=0):
        pass


def test_a_claude_that_cannot_be_started_through_the_command_entry_exits_1(root, tmp_path, capsys):
    _unrunnable_claude(root, tmp_path)
    with pytest.raises(SystemExit) as exit_:
        cu.claude_update_command([])
    assert exit_.value.code == 1


def test_a_claude_that_cannot_be_started_keeps_json_parseable(root, tmp_path, capsys):
    _unrunnable_claude(root, tmp_path)
    assert cu.run(["--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and "claude update" in data["error"]


def test_update_refuses_while_another_update_runs(fake, root, capsys):
    with FileLock(root / cu.LOCK_FILENAME, timeout=0):
        assert cu.run([]) == 1
    assert "in progress" in capsys.readouterr().err
    assert fake.calls() == []  # not even --version


def test_the_lock_is_released_after_a_normal_run(fake, root):
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    with FileLock(root / cu.LOCK_FILENAME, timeout=0):
        pass


def test_update_exits_1_when_claude_is_missing(root, capsys):
    with patch.object(cu.shutil, "which", return_value=None):
        assert cu.run([]) == 1
    assert "prime.claudePath" in capsys.readouterr().err


def test_update_json_keeps_stdout_a_single_document(fake, root, capsys):
    fake.set(version="2.1.280", versionAfter="2.1.287", updateOutput="Successfully updated")
    assert cu.run(["--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["before"] == "2.1.280" and data["after"] == "2.1.287"
    assert data["changed"] is True and data["ok"] is True
    assert "Successfully updated" in captured.err  # claude's output goes to stderr


# -- the priming hint --------------------------------------------------------


def _enable_priming(root, fake):
    atomic_write_json(
        settings_path(root),
        {"schemaVersion": 1, "prime": {"claudePath": str(fake.path), "enabled": True}},
    )


def test_hint_to_verify_priming_after_a_version_change(fake, root, capsys):
    _enable_priming(root, fake)
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    assert "cc-swap prime verify" in capsys.readouterr().out


def test_no_hint_when_priming_is_off(fake, root, capsys):
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    assert "prime verify" not in capsys.readouterr().out


def test_no_hint_when_the_version_did_not_change(fake, root, capsys):
    _enable_priming(root, fake)
    fake.set(version="2.1.287")
    cu.run([])
    assert "prime verify" not in capsys.readouterr().out


def test_the_hint_names_the_exact_command(fake, root, capsys):
    _enable_priming(root, fake)
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == (
        "Priming is paused for Claude Code 2.1.287 until its isolation is verified "
        "again. Run: cc-swap prime verify"
    )


def test_json_names_the_prime_verify_command(fake, root, capsys):
    _enable_priming(root, fake)
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["primeVerifyAdvised"] is True
    assert data["primeVerifyCommand"] == "cc-swap prime verify"


# -- the priming version guard sees the update ---------------------------------
#
# One verified version (prime_verify.json); claude-update's change and the
# guard's own `claude --version` cache are two views of the installed one.


def _no_version_reads(monkeypatch):
    from claude_swap.maximize import prime_verify as pv

    def boom(_path):
        raise AssertionError("the guard ran claude --version; claude-update had cached it")

    monkeypatch.setattr(pv, "read_claude_version", boom)
    return pv


def test_an_update_pauses_priming_that_was_verified_for_the_old_version(
    fake, root, monkeypatch
):
    pv = _no_version_reads(monkeypatch)
    pv.record_verified(root, "2.1.280", by=pv.VERIFIED_BY_CLI, now=1.0)
    fake.set(version="2.1.280", versionAfter="2.1.287")
    assert cu.run([]) == 0
    verdict = pv.gate(root, str(fake.path))  # from the cache claude-update filled
    assert not verdict.ok and verdict.current == "2.1.287"
    assert "`cc-swap prime verify`" in verdict.reason
    assert pv.paused_note(root) == "paused: claude 2.1.280 -> 2.1.287 (cc-swap prime verify)"
    pv.record_verified(root, "2.1.287", by=pv.VERIFIED_BY_CLI)  # prime verify passed
    assert pv.gate(root, str(fake.path)).ok
    assert pv.paused_note(root) is None


def test_an_update_pauses_priming_even_with_nothing_verified_yet(fake, root, monkeypatch):
    pv = _no_version_reads(monkeypatch)
    assert pv.verified_version(root) is None
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    verdict = pv.gate(root, str(fake.path))
    assert not verdict.ok and verdict.verified is None
    assert "2.1.280 -> 2.1.287" in verdict.reason
    assert pv.paused_note(root) == "paused: claude 2.1.280 -> 2.1.287 (cc-swap prime verify)"
    # A confirmed prime cannot adopt the new build as the baseline: none runs.
    pv.record_verified(root, "2.1.287", by=pv.VERIFIED_BY_CLI)
    assert pv.gate(root, str(fake.path)).ok


def test_a_first_observation_without_a_change_pauses_nothing(fake, root, monkeypatch):
    pv = _no_version_reads(monkeypatch)
    fake.set(version="2.1.287")  # already current: recorded, but not a change
    cu.run([])
    assert state_of(root)[cu.KEY_VERSION] == "2.1.287"
    assert cu.recorded_claude_change(root) is None
    assert pv.gate(root, str(fake.path)).ok
    assert pv.paused_note(root) is None


def test_check_never_touches_the_guard(fake, http, root):
    from claude_swap.maximize import prime_verify as pv

    cu.run(["--check"])
    assert pv.load(root) == {}


def test_a_verification_after_a_rollback_covers_the_recorded_change(fake, root, monkeypatch):
    pv = _no_version_reads(monkeypatch)
    fake.set(version="2.1.280", versionAfter="2.1.287")
    cu.run([])
    # Rolled back by hand to 2.1.280, then `prime verify` passed on it.
    pv.note_seen(root, str(fake.path), "2.1.280")
    pv.record_verified(root, "2.1.280", by=pv.VERIFIED_BY_CLI, now=4_000_000_000.0)
    assert pv.pending_update(root) is None
    assert pv.gate(root, str(fake.path)).ok


def test_a_recorded_change_newer_than_the_verification_pauses_on_its_own(root):
    """Even when the cached `claude --version` still matches (another path
    updated), the claude-update record alone pauses priming."""
    from claude_swap.maximize import prime_verify as pv

    pv.record_verified(root, "2.1.280", by=pv.VERIFIED_BY_CLI, now=1.0)
    cu.record_version(root, "2.1.280")
    cu.record_version(root, "2.1.287", "2.1.280")
    claude = root / "claude"
    claude.write_text("#!/bin/sh\n")
    verdict = pv.gate(root, str(claude), reader=lambda _p: "2.1.280")
    assert not verdict.ok
    assert "recorded claude 2.1.287" in verdict.reason


# -- CLI wiring --------------------------------------------------------------


def test_dispatched_from_main(temp_home):
    with patch("claude_swap.cli._claude_update_command") as fn, \
         patch.object(sys, "argv", ["cc-swap", "claude-update", "--check"]):
        cli.main()
    fn.assert_called_once_with(["--check"])


def test_end_to_end_through_main_exits_with_the_check_code(fake, http):
    with patch.object(sys, "argv", ["cc-swap", "claude-update", "--check"]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 10


def test_unknown_flag_is_a_usage_error(fake):
    with pytest.raises(SystemExit) as exc:
        cu.run(["--nope"])
    assert exc.value.code == 2


def test_readme_documents_the_command_and_its_version_source():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    assert "cc-swap claude-update --check" in readme
    assert cu.DIST_TAGS_URL in readme
    for key in (cu.KEY_VERSION, cu.KEY_CHANGED_AT):
        assert key in readme
