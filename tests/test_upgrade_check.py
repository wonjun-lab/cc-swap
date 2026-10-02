"""`cc-swap upgrade --check` and the service refresh after `cc-swap upgrade`.

Nothing here reaches GitHub, runs a package manager, or calls launchctl /
systemctl: urlopen and subprocess.run are replaced, and so is the service
module's status/resolve_program.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import cli
from claude_swap.maximize import service
from claude_swap.update_check import (
    RELEASES_URL,
    run_self_upgrade,
    run_upgrade_check,
)

FORK = "git+https://github.com/wonjun-lab/cc-swap"
PINNED = f"{FORK}@cc-v0.4.0"
API = "https://api.github.com/repos/wonjun-lab/cc-swap"


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    def _offline(*args, **kwargs):
        raise OSError("network disabled in tests")

    monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _offline)
    # The installed build: older than the cc-v0.4.0 release the tests mock,
    # whatever pyproject's version is (tests that need another one set it).
    monkeypatch.setattr("claude_swap.update_check.__version__", "0.3.2", raising=False)
    monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("claude_swap.update_check._has_menubar_extra", lambda: False)
    monkeypatch.setattr(service, "status", lambda **kw: {"installed": False})
    monkeypatch.setattr(
        "claude_swap.update_check.subprocess.run",
        lambda *a, **k: pytest.fail(f"subprocess.run called: {a}"),
    )


def _json_response(payload) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _github(monkeypatch, routes: dict) -> list[str]:
    """Answer GitHub API urls from ``routes`` (url -> payload); anything else
    is a network error. Returns the urls requested."""
    seen: list[str] = []

    def _urlopen(req, timeout=None):
        seen.append(req.full_url)
        if req.full_url not in routes:
            raise OSError("not routed")
        return _json_response(routes[req.full_url])

    monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _urlopen)
    return seen


def _release(tag: str, body: str = "", **extra) -> dict:
    return {"tag_name": tag, "body": body, "draft": False, "prerelease": False, **extra}


def _commit(subject: str, body: str = "") -> dict:
    return {"sha": "a" * 40, "commit": {"message": subject + (f"\n\n{body}" if body else "")}}


@pytest.fixture
def v020(monkeypatch):
    monkeypatch.setattr("claude_swap.update_check.__version__", "0.2.0", raising=False)
    monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
    monkeypatch.setattr("claude_swap.update_check.sys.platform", "linux")


@pytest.fixture
def v031(monkeypatch):
    """Installed 0.3.1, the first release tagged cc-v."""
    monkeypatch.setattr("claude_swap.update_check.__version__", "0.3.1", raising=False)
    monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
    monkeypatch.setattr("claude_swap.update_check.sys.platform", "linux")


class TestUpgradeCheck:
    def test_update_available_exits_10_and_names_both_versions(
        self, v020, monkeypatch, capsys
    ):
        _github(monkeypatch, {RELEASES_URL: [_release("cc-v0.3.1")]})

        assert run_upgrade_check() == 10

        out = capsys.readouterr().out
        assert "Installed: 0.2.0" in out
        assert "Latest:    0.3.1" in out
        assert "cc-swap upgrade" in out

    def test_up_to_date_exits_0(self, v020, monkeypatch, capsys):
        _github(monkeypatch, {RELEASES_URL: [_release("cc-v0.2.0")]})

        assert run_upgrade_check() == 0

        out = capsys.readouterr().out
        assert "up to date" in out
        assert "cc-swap upgrade" not in out

    def test_running_ahead_of_the_latest_release_is_up_to_date(self, v020, monkeypatch):
        _github(monkeypatch, {RELEASES_URL: [_release("cc-v0.1.9")]})

        assert run_upgrade_check() == 0

    def test_a_prerelease_is_not_pushed_onto_a_final_install(self, v020, monkeypatch):
        _github(monkeypatch, {RELEASES_URL: [_release("cc-v0.3.0b1")]})

        assert run_upgrade_check() == 0

    def test_offline_without_a_cache_exits_1_and_says_so(self, v020, capsys):
        assert run_upgrade_check() == 1

        assert "could not" in capsys.readouterr().err.lower()

    def test_offline_falls_back_to_the_cached_tag_with_a_warning(self, v020, capsys):
        import claude_swap.update_check as uc

        uc.write_cache(uc.CACHE_PATH, "cc-v0.3.1")

        assert run_upgrade_check() == 10

        captured = capsys.readouterr()
        assert "cached" in captured.err
        assert "Latest:    0.3.1" in captured.out

    def test_check_never_installs_anything(self, v020, monkeypatch):
        # subprocess.run fails the test (see _isolation).
        _github(monkeypatch, {RELEASES_URL: [_release("cc-v0.3.1")]})

        assert run_upgrade_check() == 10

    def test_lists_the_release_notes_between_the_versions(self, v031, monkeypatch, capsys):
        _github(
            monkeypatch,
            {
                RELEASES_URL: [
                    _release("cc-v0.4.0", "Fourth: auto on|off"),
                    _release("cc-v0.3.2", "Third: doctor"),
                    _release("cc-v0.3.1", "Second: already installed"),
                    _release("cc-v0.3.3b1", "beta, not for finals", prerelease=True),
                    _release("cc-v0.3.5", "unfinished notes", draft=True),
                    _release("v0.26.0", "upstream notes"),
                    _release("v0.3.0", "legacy notes"),
                ],
            },
        )

        assert run_upgrade_check() == 10

        out = capsys.readouterr().out
        assert "cc-v0.4.0" in out and "Fourth: auto on|off" in out
        assert "cc-v0.3.2" in out and "Third: doctor" in out
        assert "already installed" not in out
        assert "upstream notes" not in out
        assert "legacy notes" not in out
        assert "beta, not for finals" not in out
        assert "unfinished notes" not in out
        assert out.index("Fourth") < out.index("Third")

    def test_lists_the_commit_subjects_between_the_versions(self, v020, monkeypatch, capsys):
        seen = _github(
            monkeypatch,
            {
                RELEASES_URL: [_release("cc-v0.3.1")],
                f"{API}/compare/v0.2.0...cc-v0.3.1": {
                    "commits": [
                        _commit("Add doctor", "long body that must not be printed"),
                        _commit("Add init"),
                    ]
                },
            },
        )

        assert run_upgrade_check() == 10

        out = capsys.readouterr().out
        assert "Add doctor" in out and "Add init" in out
        assert "long body" not in out
        assert f"{API}/compare/v0.2.0...cc-v0.3.1" in seen

    def test_a_long_commit_list_is_capped(self, v020, monkeypatch, capsys):
        _github(
            monkeypatch,
            {
                RELEASES_URL: [_release("cc-v0.3.1")],
                f"{API}/compare/v0.2.0...cc-v0.3.1": {
                    "commits": [_commit(f"change {i}") for i in range(80)]
                },
            },
        )

        run_upgrade_check()

        out = capsys.readouterr().out
        assert "change 0" in out
        assert "change 79" not in out
        assert "more" in out

    def test_missing_change_lists_do_not_change_the_verdict(self, v020, monkeypatch, capsys):
        _github(monkeypatch, {RELEASES_URL: [_release("cc-v0.3.1")]})

        assert run_upgrade_check() == 10

        assert "Latest:    0.3.1" in capsys.readouterr().out

    def test_windows_prints_the_command_instead_of_the_upgrade_verb(
        self, v020, monkeypatch, capsys
    ):
        monkeypatch.setattr("claude_swap.update_check.sys.platform", "win32")
        _github(monkeypatch, {RELEASES_URL: [_release("cc-v0.3.1")]})

        assert run_upgrade_check() == 10

        assert f"uv tool install --force {FORK}@cc-v0.3.1" in capsys.readouterr().out

    def test_unknown_install_method_prints_manual_and_editable_hints(
        self, v020, monkeypatch, capsys
    ):
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: None)
        _github(monkeypatch, {RELEASES_URL: [_release("cc-v0.3.1")]})

        assert run_upgrade_check() == 10

        out = capsys.readouterr().out
        assert f"pip install --upgrade {FORK}@cc-v0.3.1" in out
        assert "pip install -e" in out and "git pull" in out
        assert "cc-swap upgrade" not in out


class TestCheckFlag:
    def _main(self, argv):
        with patch.object(sys, "argv", ["cc-swap", *argv]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        return excinfo.value.code

    def test_upgrade_check_dispatches_and_exits_with_its_code(self):
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, patch(
            "claude_swap.update_check.run_upgrade_check", return_value=10
        ) as check, patch("claude_swap.update_check.run_self_upgrade") as upgrade:
            assert self._main(["upgrade", "--check"]) == 10

        check.assert_called_once_with()
        upgrade.assert_not_called()
        switcher_cls.assert_not_called()

    def test_check_is_refused_without_upgrade(self, capsys):
        assert self._main(["list", "--check"]) == 2

        assert "--check can only be used with 'upgrade'" in capsys.readouterr().err

    def test_check_does_not_combine_with_force(self, capsys):
        assert self._main(["upgrade", "--check", "--force"]) == 2

        assert "--check" in capsys.readouterr().err

    def test_help_mentions_it(self, capsys):
        self._main(["help"])

        assert "upgrade --check" in capsys.readouterr().out


@patch("claude_swap.update_check.sys.platform", "linux")
@patch("claude_swap.update_check._detect_install_method", return_value="uv")
class TestUpgradeRefreshesTheService:
    def _setup(self, monkeypatch, *, installed=True, upgrade_rc=0, service_rc=0):
        monkeypatch.setattr(service, "status", lambda **kw: {"installed": installed})
        monkeypatch.setattr(service, "resolve_program", lambda: ["/bin/cc-swap"])
        calls: list[list[str]] = []

        def _run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=upgrade_rc if len(calls) == 1 else service_rc)

        monkeypatch.setattr("claude_swap.update_check.subprocess.run", _run)
        monkeypatch.setattr(
            "claude_swap.update_check.urllib.request.urlopen",
            lambda *a, **k: _json_response([_release("cc-v0.4.0")]),
        )
        return calls

    def test_reinstalls_the_service_with_the_new_build_after_success(
        self, mock_detect, monkeypatch, capsys
    ):
        calls = self._setup(monkeypatch)

        assert run_self_upgrade() == 0

        assert calls == [
            ["uv", "tool", "install", "--force", PINNED],
            ["/bin/cc-swap", "service", "install", "--reuse-installed-env"],
        ]
        assert "service" in capsys.readouterr().out.lower()

    def test_does_not_touch_a_service_that_is_not_installed(self, mock_detect, monkeypatch):
        calls = self._setup(monkeypatch, installed=False)

        assert run_self_upgrade() == 0

        assert len(calls) == 1

    def test_does_not_reinstall_the_service_when_the_upgrade_failed(
        self, mock_detect, monkeypatch
    ):
        calls = self._setup(monkeypatch, upgrade_rc=2)

        assert run_self_upgrade() == 2

        assert len(calls) == 1

    def test_does_nothing_to_the_service_when_already_current(self, mock_detect, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.4.0", raising=False)
        calls = self._setup(monkeypatch)

        assert run_self_upgrade() == 0

        assert calls == []

    def test_a_failed_service_reinstall_warns_but_the_upgrade_still_succeeds(
        self, mock_detect, monkeypatch, capsys
    ):
        self._setup(monkeypatch, service_rc=1)

        assert run_self_upgrade() == 0

        assert "cc-swap service install" in capsys.readouterr().err

    def test_a_status_probe_that_raises_does_not_fail_the_upgrade(
        self, mock_detect, monkeypatch
    ):
        from claude_swap.exceptions import ClaudeSwitchError

        calls = self._setup(monkeypatch)

        def _boom(**kw):
            raise ClaudeSwitchError("systemctl not found")

        monkeypatch.setattr(service, "status", _boom)

        assert run_self_upgrade() == 0

        assert len(calls) == 1

    def test_the_command_is_the_resolved_console_script(self, mock_detect):
        with patch.object(service, "resolve_program", return_value=["/x/cc-swap"]):
            assert service.reinstall_command() == [
                "/x/cc-swap", "service", "install", "--reuse-installed-env",
            ]
