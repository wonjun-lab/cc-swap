"""Tests for update_check module."""

from __future__ import annotations

import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.update_check import (
    CACHE_PATH,
    CACHE_TTL,
    INSTALL_URL,
    RELEASES_URL,
    _detect_install_method,
    check_for_update,
    run_self_upgrade,
)


@pytest.fixture(autouse=True)
def _no_menubar_extra(monkeypatch):
    """Pin the install spec to the bare git URL; rumps may or may not be
    importable on the machine running the suite."""
    monkeypatch.setattr("claude_swap.update_check._has_menubar_extra", lambda: False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing here may reach GitHub. Tests that exercise the release lookup
    patch ``urlopen`` themselves, which replaces this for their duration."""

    def _offline(*args, **kwargs):
        raise OSError("network disabled in tests")

    monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _offline)


def _make_release_response(version: str) -> MagicMock:
    """A GitHub ``releases/latest`` payload for tag ``v<version>``."""
    data = json.dumps({"tag_name": f"v{version}", "prerelease": False}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_tag_response(tag: str) -> MagicMock:
    """A ``releases/latest`` payload whose tag_name is exactly ``tag``."""
    resp = _make_release_response("0.0.0")
    resp.read.return_value = json.dumps({"tag_name": tag}).encode()
    return resp


def _write_cache(path, version, timestamp=None):
    """Write a cache file in the shared cache format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "timestamp": timestamp if timestamp is not None else time.time(),
        "data": version,
    }))


class TestCheckForUpdate:
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_newer_version_available(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "0.4.0" in result
        assert "0.3.2" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_already_on_latest(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.3.2")

        result = check_for_update("0.3.2")

        assert result is None

    @patch("claude_swap.update_check.urllib.request.urlopen", side_effect=OSError("network error"))
    def test_network_error_returns_none_and_caches(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        assert result is None
        assert cache_path.exists()
        cache = json.loads(cache_path.read_text())
        assert cache["data"] is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_fresh_error_cache_skips_network(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, None)
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        mock_urlopen.assert_not_called()
        assert result is None

    def test_fresh_cache_no_network(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.5.0")
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        with patch("claude_swap.update_check.urllib.request.urlopen") as mock_urlopen:
            result = check_for_update("0.3.2")
            mock_urlopen.assert_not_called()

        assert result is not None
        assert "0.5.0" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_stale_cache_fetches_latest_release(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.3.0", timestamp=time.time() - CACHE_TTL - 1)
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        mock_urlopen.assert_called_once()
        assert result is not None
        assert "0.4.0" in result


class TestGitHubReleaseSource:
    """cc-swap polls its own GitHub Releases, never upstream's PyPI project."""

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_requests_the_fork_latest_release(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        check_for_update("0.3.2")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == RELEASES_URL
        assert RELEASES_URL == (
            "https://api.github.com/repos/wonjun-lab/cc-swap/releases/latest"
        )
        assert req.get_header("Accept") == "application/vnd.github+json"
        assert req.get_header("User-agent")  # GitHub rejects requests without one

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_no_release_yet_404_is_silent_and_cached(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        mock_urlopen.side_effect = urllib.error.HTTPError(
            RELEASES_URL, 404, "Not Found", hdrs=None, fp=None
        )

        assert check_for_update("0.1.0") is None
        assert json.loads(cache_path.read_text())["data"] is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_tag_without_v_prefix_is_accepted(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        resp = _make_release_response("0.0.0")
        resp.read.return_value = json.dumps({"tag_name": "0.4.0"}).encode()
        mock_urlopen.return_value = resp

        result = check_for_update("0.3.2")

        assert result is not None
        assert "(0.4.0)" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_payload_without_tag_is_silent(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        resp = _make_release_response("0.0.0")
        resp.read.return_value = json.dumps({"message": "Not Found"}).encode()
        mock_urlopen.return_value = resp

        assert check_for_update("0.3.2") is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_message_names_cc_swap(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert result.startswith("A newer version of cc-swap is available (0.4.0).")

    def test_cache_file_is_not_upstreams(self):
        # The backup root (and its cache/) is shared with an upstream cswap
        # install; its update_check.json holds a PyPI claude-swap version.
        assert CACHE_PATH.name == "cc_swap_update_check.json"


class TestCheckForUpdatePrereleases:
    """claude-swap ships every cycle as a pre-release first, so the version
    cli.main hands to check_for_update is routinely something like 0.27.0b1."""

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_later_release(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.28.0")

        result = check_for_update("0.27.0b1")

        assert result is not None
        assert "0.28.0" in result
        assert "0.27.0b1" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_its_own_final_release(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.27.0")

        result = check_for_update("0.27.0b1")

        assert result is not None
        assert "0.27.0" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_later_prerelease(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.27.0rc1")

        result = check_for_update("0.27.0b2")

        assert result is not None
        assert "0.27.0rc1" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_final_release_is_not_pushed_onto_a_prerelease(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.28.0b1")

        assert check_for_update("0.27.0") is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_unparseable_version_stays_silent(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.28.0")

        assert check_for_update("main") is None


class TestDetectInstallMethod:
    def _set_prefix(self, monkeypatch, prefix: str) -> None:
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", prefix)
        # Clear env vars by default so path-based detection runs in isolation.
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)

    def test_uv_tool_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/share/uv/tools/claude-swap")
        assert _detect_install_method() == "uv"

    def test_pipx_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/pipx/venvs/claude-swap")
        assert _detect_install_method() == "pipx"

    def test_non_adjacent_uv_tools_does_not_match(self, monkeypatch):
        # Both segments present but not adjacent — must not false-positive.
        self._set_prefix(monkeypatch, "/home/me/projects/uv/some-tools/.venv")
        assert _detect_install_method() is None

    def test_non_adjacent_pipx_venvs_does_not_match(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/repos/pipx-clone/venvs-of-mine/.venv")
        assert _detect_install_method() is None

    def test_source_checkout_returns_none(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/code/claude-swap/.venv")
        assert _detect_install_method() is None

    def test_mixed_case_path_detected(self, monkeypatch):
        # Lowercasing should make matching case-insensitive (e.g. Windows).
        self._set_prefix(monkeypatch, "/Home/Me/.local/share/UV/Tools/claude-swap")
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        prefix = custom_root / "claude-swap"
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_set_but_prefix_elsewhere(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        # Prefix lives somewhere else entirely — env var alone must not trigger.
        monkeypatch.setattr(
            "claude_swap.update_check.sys.prefix", str(tmp_path / "some-project" / ".venv")
        )
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() is None

    def test_pipx_home_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "pipx-home"
        prefix = custom_root / "venvs" / "claude-swap"
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("PIPX_HOME", str(custom_root))
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        assert _detect_install_method() == "pipx"


class TestCheckForUpdateMessage:
    @patch("claude_swap.update_check.sys.platform", "linux")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_detected_method_non_windows_suggests_cswap_upgrade(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        # uv/pipx on macOS/Linux: cswap upgrade actually upgrades, so advertise it.
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "cc-swap upgrade" in result
        assert "uv tool install" not in result

    @patch("claude_swap.update_check.sys.platform", "win32")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_detected_method_windows_suggests_direct_command(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        # Windows: cswap upgrade only prints, so point at the real command.
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "pipx")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert f"pipx install --force {INSTALL_URL}" in result
        assert "cc-swap upgrade" not in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_unknown_method_suggests_cswap_instructions(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        # Unknown install method: cswap upgrade can only show instructions.
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: None)
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "cc-swap upgrade` for upgrade instructions" in result
        assert "uv tool install" not in result
        assert "pipx install" not in result


FORK = "git+https://github.com/wonjun-lab/cc-swap"
PINNED = f"{FORK}@v0.4.0"


@patch("claude_swap.update_check.sys.platform", "linux")
class TestRunSelfUpgrade:
    """``upgrade`` installs the release the notice announced, not whatever the
    default branch happens to hold."""

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_uv_installs_the_latest_release_tag(
        self, mock_detect, mock_urlopen, mock_run
    ):
        mock_urlopen.return_value = _make_release_response("0.4.0")
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", PINNED], check=False
        )

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_looks_up_the_fork_release_with_a_patient_timeout(
        self, mock_detect, mock_urlopen, mock_run
    ):
        # An explicit `upgrade` can wait longer than the passive notice's 2s.
        mock_urlopen.return_value = _make_release_response("0.4.0")
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        assert mock_urlopen.call_args[0][0].full_url == RELEASES_URL
        assert mock_urlopen.call_args.kwargs["timeout"] > 2

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_tag_keeps_its_published_spelling(
        self, mock_detect, mock_urlopen, mock_run
    ):
        # The git ref must be the tag as published; a tag without the "v"
        # prefix is not rewritten to carry one.
        mock_urlopen.return_value = _make_tag_response("0.4.0")
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", f"{FORK}@0.4.0"], check=False
        )

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_says_which_release_it_is_installing(
        self, mock_detect, mock_urlopen, mock_run, capsys
    ):
        mock_urlopen.return_value = _make_release_response("0.4.0")
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        assert "v0.4.0" in capsys.readouterr().out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_menubar_extra_survives_the_reinstall(
        self, mock_detect, mock_urlopen, mock_run, monkeypatch
    ):
        monkeypatch.setattr(
            "claude_swap.update_check._has_menubar_extra", lambda: True
        )
        mock_urlopen.return_value = _make_release_response("0.4.0")
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", f"cc-swap[menubar] @ {PINNED}"],
            check=False,
        )

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="pipx")
    def test_pipx_installs_the_latest_release_tag(
        self, mock_detect, mock_urlopen, mock_run
    ):
        mock_urlopen.return_value = _make_release_response("0.4.0")
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["pipx", "install", "--force", PINNED], check=False
        )

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_no_release_falls_back_to_default_branch_and_says_so(
        self, mock_detect, mock_run, capsys
    ):
        # The autouse fixture makes every lookup fail, like a 404 before the
        # first release is published.
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0
        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", INSTALL_URL], check=False
        )
        out = capsys.readouterr().out
        assert "default branch" in out
        assert "release" in out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="pipx")
    def test_payload_without_tag_falls_back_to_default_branch(
        self, mock_detect, mock_urlopen, mock_run, capsys
    ):
        resp = _make_release_response("0.0.0")
        resp.read.return_value = json.dumps({"message": "Not Found"}).encode()
        mock_urlopen.return_value = resp
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        mock_run.assert_called_once_with(
            ["pipx", "install", "--force", INSTALL_URL], check=False
        )
        assert "default branch" in capsys.readouterr().out

    @pytest.mark.parametrize("tag", ["v1.0 beta", "v1.0#frag", "v1.0@evil", "-v1", "v1/../x"])
    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_tag_that_cannot_be_a_git_ref_suffix_is_not_used(
        self, mock_detect, mock_urlopen, mock_run, tag
    ):
        # The tag comes off the network and is spliced into a URL.
        mock_urlopen.return_value = _make_tag_response(tag)
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", INSTALL_URL], check=False
        )

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_propagates_nonzero_exit_code(self, mock_detect, mock_urlopen, mock_run):
        mock_urlopen.return_value = _make_release_response("0.4.0")
        mock_run.return_value = MagicMock(returncode=2)

        assert run_self_upgrade() == 2

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value=None)
    def test_unknown_method_returns_1_and_prints_pinned_instructions(
        self, mock_detect, mock_urlopen, mock_run, capsys
    ):
        mock_urlopen.return_value = _make_release_response("0.4.0")

        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert f"uv tool install --force {PINNED}" in err
        assert f"pipx install --force {PINNED}" in err
        assert f"pip install --upgrade {PINNED}" in err

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value=None)
    def test_unknown_method_without_release_prints_default_branch_instructions(
        self, mock_detect, mock_run, capsys
    ):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert f"uv tool install --force {INSTALL_URL}\n" in err
        assert f"pipx install --force {INSTALL_URL}\n" in err
        assert f"pip install --upgrade {INSTALL_URL}\n" in err

    @patch(
        "claude_swap.update_check.subprocess.run", side_effect=FileNotFoundError
    )
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_filenotfound_returns_1(self, mock_detect, mock_run, capsys):
        assert run_self_upgrade() == 1
        err = capsys.readouterr().err
        assert "PATH" in err


@patch("claude_swap.update_check.sys.platform", "win32")
class TestRunSelfUpgradeWindows:
    """On Windows the running .exe is locked, so we never upgrade in place --
    we print the command for the user to run themselves and exit 1."""

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_uv_prints_pinned_command_and_does_not_run(
        self, mock_detect, mock_urlopen, mock_run, capsys
    ):
        mock_urlopen.return_value = _make_release_response("0.4.0")

        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert f"uv tool install --force {PINNED}" in out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value="pipx")
    def test_pipx_prints_pinned_command_and_does_not_run(
        self, mock_detect, mock_urlopen, mock_run, capsys
    ):
        mock_urlopen.return_value = _make_release_response("0.4.0")

        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert f"pipx install --force {PINNED}" in out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check._detect_install_method", return_value="uv")
    def test_without_release_prints_default_branch_command_and_says_so(
        self, mock_detect, mock_run, capsys
    ):
        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert f"uv tool install --force {INSTALL_URL}" in out
        assert "default branch" in out

    @patch("claude_swap.update_check.subprocess.run")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    @patch("claude_swap.update_check._detect_install_method", return_value=None)
    def test_unknown_method_hits_generic_fallback(
        self, mock_detect, mock_urlopen, mock_run, capsys
    ):
        mock_urlopen.return_value = _make_release_response("0.4.0")

        assert run_self_upgrade() == 1
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert f"uv tool install --force {PINNED}" in err
        assert f"pipx install --force {PINNED}" in err
        assert f"pip install --upgrade {PINNED}" in err


class TestNoticeAnnouncesTheTagItInstalls:
    """The notice and the upgrade it points at must agree on the release."""

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_cache_keeps_the_published_tag(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        mock_urlopen.return_value = _make_release_response("0.4.0")

        check_for_update("0.3.2")

        assert json.loads(cache_path.read_text())["data"] == "v0.4.0"

    @patch("claude_swap.update_check.sys.platform", "win32")
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_windows_hint_command_is_pinned_to_the_announced_tag(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "(0.4.0)" in result  # the notice still names the bare version
        assert f"uv tool install --force {PINNED}" in result

    @patch("claude_swap.update_check.sys.platform", "win32")
    def test_windows_hint_from_cache_is_pinned_too(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "v0.5.0")
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "pipx")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "(0.5.0)" in result
        assert f"pipx install --force {FORK}@v0.5.0" in result
