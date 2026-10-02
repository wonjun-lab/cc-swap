"""`cc-swap service …`: routing, output, and refusals (runs on every OS)."""

from __future__ import annotations

import os
import sys

import pytest

from claude_swap import cli
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.maximize import service

LINUX_INSTALL = {
    "platform": "linux",
    "name": "cc-swap.service",
    "path": "/home/u/.config/systemd/user/cc-swap.service",
    "program": ["/home/u/.local/bin/cc-swap", "auto"],
    "logs": ["journalctl --user -u cc-swap.service -f"],
    "linger": False,
    "claude_path": "/home/u/.local/bin/claude",
    "claude_path_saved": True,
}


@pytest.fixture(autouse=True)
def _not_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)


def _service(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as excinfo:
        cli._service_command(argv)
    return excinfo.value.code


def test_main_routes_the_service_subcommand(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_service_command", lambda argv: seen.append(argv))
    monkeypatch.setattr(sys, "argv", ["cc-swap", "service", "status"])
    cli.main()
    assert seen == [["status"]]


def test_install_prints_paths_claude_and_the_linger_hint(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(service, "install", lambda **kw: calls.append(kw) or dict(LINUX_INSTALL))
    assert _service(["install", "--claude-path", "/home/u/.local/bin/claude"]) == 0
    assert calls == [{"claude_path": "/home/u/.local/bin/claude"}]
    out = capsys.readouterr().out
    assert "cc-swap service installed (cc-swap.service)." in out
    assert "/home/u/.local/bin/cc-swap auto" in out
    assert "claude: /home/u/.local/bin/claude (saved as prime.claudePath)" in out
    assert "loginctl enable-linger" in out


def test_install_with_linger_on_prints_no_hint(monkeypatch, capsys):
    monkeypatch.setattr(service, "install", lambda **kw: {**LINUX_INSTALL, "linger": True})
    _service(["install"])
    assert "enable-linger" not in capsys.readouterr().out


def test_install_without_claude_warns_about_priming(monkeypatch, capsys):
    monkeypatch.setattr(service, "install", lambda **kw: {**LINUX_INSTALL, "claude_path": None, "claude_path_saved": False})
    assert _service(["install"]) == 0
    assert "prime.claudePath" in capsys.readouterr().err


def test_status_not_installed(monkeypatch, capsys):
    monkeypatch.setattr(service, "status", lambda **kw: {
        "platform": "linux", "name": "cc-swap.service", "installed": False, "loaded": False,
        "running": False, "state": "inactive", "pid": None, "path": "/u/cc-swap.service",
        "logs": ["journalctl --user -u cc-swap.service -f"],
    })
    assert _service(["status"]) == 0
    out = capsys.readouterr().out
    assert "cc-swap service is not installed." in out
    assert "cc-swap service install" in out


def test_status_running(monkeypatch, capsys):
    monkeypatch.setattr(service, "status", lambda **kw: {
        "platform": "linux", "name": "cc-swap.service", "installed": True, "loaded": True,
        "running": True, "state": "active", "pid": 4242, "path": "/u/cc-swap.service",
        "logs": ["journalctl --user -u cc-swap.service -f"],
    })
    _service(["status"])
    assert "cc-swap service: active (pid 4242)" in capsys.readouterr().out


@pytest.mark.parametrize("result,message", [
    ({"was_running": True, "removed": True}, "cc-swap service removed."),
    ({"was_running": False, "removed": False}, "cc-swap service was not installed."),
])
def test_uninstall_messages(monkeypatch, capsys, result, message):
    monkeypatch.setattr(service, "uninstall", lambda **kw: {"platform": "linux", "name": "cc-swap.service", **result})
    assert _service(["uninstall"]) == 0
    assert message in capsys.readouterr().out


def test_no_action_prints_help_and_exits_2(capsys):
    assert _service([]) == 2
    assert "install" in capsys.readouterr().out


def test_errors_exit_1(monkeypatch, capsys):
    def boom(**kw):
        raise ClaudeSwitchError("launchctl bootstrap failed (exit 5)")

    monkeypatch.setattr(service, "install", boom)
    assert _service(["install"]) == 1
    assert "launchctl bootstrap failed" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["install", "uninstall", "status"])
def test_windows_is_refused(monkeypatch, capsys, action):
    monkeypatch.setattr(sys, "platform", "win32")
    assert _service([action]) == 1
    assert "supports macOS (launchd) and Linux" in capsys.readouterr().err


@pytest.mark.parametrize("call", [service.install, service.uninstall, service.status])
def test_the_api_refuses_windows_before_touching_anything(monkeypatch, tmp_path, call):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ClaudeSwitchError, match="supports macOS"):
        call(home=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="root check is POSIX-only")
def test_root_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert _service(["install"]) == 1
    assert "not root" in capsys.readouterr().err
