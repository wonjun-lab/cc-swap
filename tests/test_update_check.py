"""Tests for update_check module."""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import claude_swap.update_check as uc
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
def _installed_version(monkeypatch):
    """The installed build the upgrade tests start from: older than the
    ``cc-v0.4.0`` release they mock, whatever pyproject's version is."""
    monkeypatch.setattr("claude_swap.update_check.__version__", "0.3.2", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing here may reach GitHub. Tests that exercise the release lookup
    patch ``urlopen`` themselves, which replaces this for their duration."""

    def _offline(*args, **kwargs):
        raise OSError("network disabled in tests")

    monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _offline)


@pytest.fixture(autouse=True)
def _no_github_credentials(monkeypatch):
    """The lookup authenticates from $GITHUB_TOKEN / $GH_TOKEN or `gh auth
    token`. Nothing here may pick up the developer's token or run the real
    `gh`; the tests of that path stage both themselves."""
    import claude_swap.update_check as uc

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(uc, "shutil", SimpleNamespace(which=lambda *a, **k: None))
    uc._gh_cli_token.cache_clear()
    yield
    uc._gh_cli_token.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """`upgrade` now refreshes the update-check cache; keep it off the real one."""
    monkeypatch.setattr(
        "claude_swap.update_check.CACHE_PATH", tmp_path / "isolated_cache.json"
    )


@pytest.fixture(autouse=True)
def _no_real_service(monkeypatch):
    """After a successful upgrade cc-swap probes the service with launchctl or
    systemctl. Nothing here may run those for real: the service is simply not
    installed (tests/test_upgrade_check.py covers the installed case)."""
    from claude_swap.maximize import service

    monkeypatch.setattr(service, "status", lambda **kw: {"installed": False})


def _cache_path() -> Path:
    """The (monkeypatched) cache path currently in effect."""
    import claude_swap.update_check as uc

    return uc.CACHE_PATH


def _make_release_response(version: str) -> MagicMock:
    """A GitHub releases-list payload holding one release, tagged
    ``cc-v<version>`` (the fork's release tag scheme)."""
    data = json.dumps(
        [{"tag_name": f"cc-v{version}", "draft": False, "prerelease": False}]
    ).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_tag_response(tag: str) -> MagicMock:
    """A releases-list payload whose only release has tag_name exactly ``tag``."""
    resp = _make_release_response("0.0.0")
    resp.read.return_value = json.dumps([{"tag_name": tag}]).encode()
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
        _write_cache(cache_path, "cc-v0.5.0")
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
    def test_requests_the_fork_releases_list_not_releases_latest(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        check_for_update("0.3.2")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == RELEASES_URL
        # /releases/latest can point at a tag outside the cc-v scheme.
        assert RELEASES_URL.startswith(
            "https://api.github.com/repos/wonjun-lab/cc-swap/releases?"
        )
        assert "/latest" not in RELEASES_URL
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

    @pytest.mark.parametrize("tag", ["0.4.0", "v0.4.0", "v0.26.0", "V0.4.0"])
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_tag_outside_the_cc_v_scheme_is_ignored(
        self, mock_urlopen, tmp_path, monkeypatch, tag
    ):
        # v0.4.0 .. v0.26.0 are upstream's tags, inherited by the fork.
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_tag_response(tag)

        assert check_for_update("0.3.1") is None

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


def _http_error(code: int, **headers: str) -> urllib.error.HTTPError:
    """An HTTPError with response headers; ``X_RateLimit_Reset`` -> ``X-RateLimit-Reset``."""
    message = http.client.HTTPMessage()
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    return urllib.error.HTTPError(RELEASES_URL, code, "error", message, None)


def _rate_limit_error(reset: int) -> urllib.error.HTTPError:
    """GitHub's answer once the quota is spent: 403 with the window's end."""
    return _http_error(403, X_RateLimit_Remaining="0", X_RateLimit_Reset=str(reset))


def _hhmm(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def _raise_on_request(monkeypatch, exc: BaseException) -> None:
    def _urlopen(req, timeout=None):
        raise exc

    monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _urlopen)


def _capture_requests(monkeypatch) -> list[urllib.request.Request]:
    """Answer every request with a 0.4.0 release; return the requests seen."""
    seen: list[urllib.request.Request] = []

    def _urlopen(req, timeout=None):
        seen.append(req)
        return _make_release_response("0.4.0")

    monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _urlopen)
    return seen


def _stage_gh(monkeypatch, *, stdout="gho_FROMGH\n", returncode=0, raises=None) -> list:
    """Put a fake `gh` on PATH. Returns the ``(argv, kwargs)`` of each run."""
    calls: list = []
    monkeypatch.setattr(
        "claude_swap.update_check.shutil.which",
        lambda name, *a, **k: "/opt/bin/gh" if name == "gh" else None,
    )

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr("claude_swap.update_check.subprocess.run", _run)
    return calls


class TestGitHubAuthentication:
    """The API allows 60 anonymous requests an hour per IP; a token raises that."""

    def test_anonymous_when_there_is_no_token_anywhere(self, monkeypatch):
        seen = _capture_requests(monkeypatch)

        assert uc._fetch_latest_tag() == "cc-v0.4.0"

        assert seen[0].get_header("Authorization") is None

    def test_github_token_is_sent_as_a_bearer_token(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken")
        seen = _capture_requests(monkeypatch)

        uc._fetch_latest_tag()

        assert seen[0].get_header("Authorization") == "Bearer ghp_envtoken"

    def test_gh_token_is_used_when_github_token_is_unset(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "ghp_other")
        seen = _capture_requests(monkeypatch)

        uc._fetch_latest_tag()

        assert seen[0].get_header("Authorization") == "Bearer ghp_other"

    def test_github_token_wins_over_gh_token(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_first")
        monkeypatch.setenv("GH_TOKEN", "ghp_second")
        seen = _capture_requests(monkeypatch)

        uc._fetch_latest_tag()

        assert seen[0].get_header("Authorization") == "Bearer ghp_first"

    def test_the_gh_cli_supplies_a_token_when_the_environment_has_none(self, monkeypatch):
        calls = _stage_gh(monkeypatch)
        seen = _capture_requests(monkeypatch)

        uc._fetch_latest_tag()

        assert seen[0].get_header("Authorization") == "Bearer gho_FROMGH"
        argv, kwargs = calls[0]
        assert argv == ["/opt/bin/gh", "auth", "token"]
        assert kwargs["timeout"] == 3

    def test_the_environment_wins_over_the_gh_cli(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken")
        calls = _stage_gh(monkeypatch)
        seen = _capture_requests(monkeypatch)

        uc._fetch_latest_tag()

        assert calls == []
        assert seen[0].get_header("Authorization") == "Bearer ghp_envtoken"

    def test_a_blank_environment_token_is_ignored(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "   ")
        seen = _capture_requests(monkeypatch)

        uc._fetch_latest_tag()

        assert seen[0].get_header("Authorization") is None

    @pytest.mark.parametrize(
        "staging",
        [
            {"returncode": 1, "stdout": ""},  # not logged in
            {"stdout": "\n"},
            {"stdout": "two words\n"},  # not a token; never goes into a header
            {"raises": subprocess.TimeoutExpired(["gh"], 3)},
            {"raises": OSError("exec format error")},
        ],
        ids=["not-logged-in", "blank", "malformed", "timeout", "oserror"],
    )
    def test_a_gh_that_cannot_help_means_anonymous(self, monkeypatch, staging):
        _stage_gh(monkeypatch, **staging)
        seen = _capture_requests(monkeypatch)

        assert uc._fetch_latest_tag() == "cc-v0.4.0"

        assert seen[0].get_header("Authorization") is None

    def test_gh_is_asked_once_per_process(self, monkeypatch):
        calls = _stage_gh(monkeypatch)
        _capture_requests(monkeypatch)

        uc._fetch_latest_tag()
        uc._fetch_latest_tag()
        uc._get_json(f"{uc._API_URL}/compare/a...b", 2)

        assert len(calls) == 1

    def test_the_token_goes_only_to_api_github_com(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken")
        seen = _capture_requests(monkeypatch)

        uc._get_json("https://example.com/releases", 2)
        uc._get_json("https://api.github.com.evil.example/releases", 2)
        uc._get_json(RELEASES_URL, 2)

        assert [r.get_header("Authorization") for r in seen] == [
            None, None, "Bearer ghp_envtoken",
        ]

    def test_the_token_does_not_follow_a_redirect_to_another_host(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken")
        seen = _capture_requests(monkeypatch)
        uc._fetch_latest_tag()
        request = seen[0]

        redirected = urllib.request.HTTPRedirectHandler().redirect_request(
            request, None, 302, "Found", request.headers, "https://elsewhere.example/x"
        )

        assert redirected.get_header("User-agent")  # other headers do carry over
        assert redirected.get_header("Authorization") is None

    def test_a_rejected_token_falls_back_to_an_anonymous_request(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_stale")
        seen: list = []

        def _urlopen(req, timeout=None):
            seen.append(req)
            if req.get_header("Authorization"):
                raise _http_error(401)
            return _make_release_response("0.4.0")

        monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _urlopen)

        assert uc._fetch_latest_tag() == "cc-v0.4.0"
        assert [r.get_header("Authorization") for r in seen] == ["Bearer ghp_stale", None]

    def test_a_forbidden_token_falls_back_to_an_anonymous_request(self, monkeypatch):
        # e.g. a token with no SSO grant: 403, but not a spent quota.
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_nosso")
        seen: list = []

        def _urlopen(req, timeout=None):
            seen.append(req)
            if req.get_header("Authorization"):
                raise _http_error(403)
            return _make_release_response("0.4.0")

        monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _urlopen)

        assert uc._fetch_latest_tag() == "cc-v0.4.0"
        assert len(seen) == 2

    def test_a_spent_token_quota_is_reported_not_retried_anonymously(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken")
        seen: list = []

        def _urlopen(req, timeout=None):
            seen.append(req)
            raise _rate_limit_error(int(time.time()) + 600)

        monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _urlopen)

        with pytest.raises(uc._LookupFailed) as excinfo:
            uc._fetch_json(RELEASES_URL, 2)

        assert len(seen) == 1
        assert excinfo.value.rate_limited

    def test_a_token_that_works_costs_no_second_request(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken")
        seen = _capture_requests(monkeypatch)

        uc._fetch_latest_tag()

        assert len(seen) == 1

    def test_the_token_is_never_printed_or_logged(self, monkeypatch, capsys, caplog):
        secret = "ghp_TOPSECRETVALUE"
        monkeypatch.setenv("GITHUB_TOKEN", secret)
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: None)
        caplog.set_level("DEBUG")
        reset = int(time.time()) + 600

        for answer in (_rate_limit_error(reset), OSError("network error"), None):
            if answer is None:
                _capture_requests(monkeypatch)
            else:
                _raise_on_request(monkeypatch, answer)
            run_self_upgrade()
            uc.run_upgrade_check()
            check_for_update("0.0.1")

        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err + caplog.text

    def test_a_rate_limited_passive_check_stays_silent(self, monkeypatch, capsys):
        _raise_on_request(monkeypatch, _rate_limit_error(int(time.time()) + 600))

        assert check_for_update("0.3.1") is None

        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""


class TestWhyTheLookupFailed:
    """``_fetch_json`` says in words why GitHub did not answer, so that
    ``upgrade`` can tell the user instead of quietly using the cache."""

    def _failure(self, monkeypatch, exc: BaseException) -> uc._LookupFailed:
        _raise_on_request(monkeypatch, exc)
        with pytest.raises(uc._LookupFailed) as excinfo:
            uc._fetch_json(RELEASES_URL, 2)
        return excinfo.value

    def test_a_spent_quota_names_when_it_resets(self, monkeypatch):
        reset = int(time.time()) + 1800

        failure = self._failure(monkeypatch, _rate_limit_error(reset))

        assert failure.reason == f"rate limited until {_hhmm(reset)}"

    def test_a_secondary_limit_counts_down_from_retry_after(self, monkeypatch):
        failure = self._failure(monkeypatch, _http_error(429, Retry_After="120"))

        assert re.fullmatch(r"rate limited until \d\d:\d\d", failure.reason)

    def test_a_spent_quota_without_a_reset_header_is_still_a_rate_limit(self, monkeypatch):
        failure = self._failure(monkeypatch, _http_error(403, X_RateLimit_Remaining="0"))

        assert failure.reason == "rate limited"

    @pytest.mark.parametrize("code", [403, 404, 500, 503])
    def test_other_http_errors_name_the_status(self, monkeypatch, code):
        failure = self._failure(monkeypatch, _http_error(code))

        assert failure.reason == f"HTTP {code}"

    @pytest.mark.parametrize(
        "exc", [TimeoutError(), urllib.error.URLError(TimeoutError("timed out"))],
        ids=["bare", "wrapped"],
    )
    def test_a_timeout_says_so(self, monkeypatch, exc):
        assert self._failure(monkeypatch, exc).reason == "timed out"

    @pytest.mark.parametrize(
        "exc",
        [OSError("boom"), urllib.error.URLError(OSError("name resolution failed"))],
        ids=["oserror", "urlerror"],
    )
    def test_a_network_error_says_so(self, monkeypatch, exc):
        assert self._failure(monkeypatch, exc).reason.startswith("network error")

    def test_a_reply_that_is_not_json_says_so(self, monkeypatch):
        resp = _make_release_response("0.4.0")
        resp.read.return_value = b"<html>captive portal</html>"
        monkeypatch.setattr(
            "claude_swap.update_check.urllib.request.urlopen", lambda req, timeout=None: resp
        )

        with pytest.raises(uc._LookupFailed) as excinfo:
            uc._fetch_json(RELEASES_URL, 2)

        assert excinfo.value.reason == "unexpected response"

    def test_a_rate_limit_without_a_token_suggests_one(self, monkeypatch):
        failure = self._failure(monkeypatch, _rate_limit_error(int(time.time()) + 60))

        assert "GITHUB_TOKEN" in failure.hint and "gh auth login" in failure.hint

    def test_a_rate_limit_with_a_token_does_not(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken")

        failure = self._failure(monkeypatch, _rate_limit_error(int(time.time()) + 60))

        assert failure.hint is None

    def test_other_failures_have_no_hint(self, monkeypatch):
        assert self._failure(monkeypatch, OSError("boom")).hint is None


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
PINNED = f"{FORK}@cc-v0.4.0"


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
    def test_upstream_style_tag_is_never_installed(
        self, mock_detect, mock_urlopen, mock_run
    ):
        # upstream's inherited v0.26.0 must not be picked up, however high.
        mock_urlopen.return_value = _make_tag_response("v0.26.0")
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", INSTALL_URL], check=False
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

        assert "cc-v0.4.0" in capsys.readouterr().out

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

    @pytest.mark.parametrize("tag", ["cc-v1.0 beta", "cc-v1.0#frag", "cc-v1.0@evil", "-v1", "cc-v1/../x"])
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


@patch("claude_swap.update_check.sys.platform", "linux")
@patch("claude_swap.update_check._detect_install_method", return_value="uv")
@patch("claude_swap.update_check.subprocess.run")
class TestUpgradeIgnoresTheCache:
    """`upgrade` asks GitHub every time; the 24 h notice cache may be stale."""

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_fresh_stale_cache_is_bypassed_and_refreshed(
        self, mock_urlopen, mock_run, mock_detect, monkeypatch
    ):
        # v0.1.1 is out, but the fresh cache still says v0.1.0.
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.1.0", raising=False)
        _write_cache(_cache_path(), "cc-v0.1.0")
        mock_urlopen.return_value = _make_release_response("0.1.1")
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0

        mock_urlopen.assert_called_once()
        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", f"{FORK}@cc-v0.1.1"], check=False
        )
        assert json.loads(_cache_path().read_text())["data"] == "cc-v0.1.1"

    def test_live_failure_falls_back_to_cached_tag_with_warning(
        self, mock_run, mock_detect, monkeypatch, capsys
    ):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.0.1", raising=False)
        # Even an expired cache entry beats guessing the default branch.
        _write_cache(_cache_path(), "cc-v0.1.0", timestamp=time.time() - 10 * CACHE_TTL)
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade() == 0

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", f"{FORK}@cc-v0.1.0"], check=False
        )
        err = capsys.readouterr().err
        assert "cc-v0.1.0" in err
        assert "cached" in err.lower()

    def test_live_failure_and_no_cache_uses_default_branch(
        self, mock_run, mock_detect, capsys
    ):
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", INSTALL_URL], check=False
        )
        assert "default branch" in capsys.readouterr().out

    def test_cached_failure_marker_is_not_a_tag(
        self, mock_run, mock_detect
    ):
        # check_for_update caches failures as null.
        _write_cache(_cache_path(), None)
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", INSTALL_URL], check=False
        )

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_unsafe_cached_tag_is_not_used(
        self, mock_urlopen, mock_run, mock_detect
    ):
        _write_cache(_cache_path(), "cc-v1.0@evil")
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", INSTALL_URL], check=False
        )


@patch("claude_swap.update_check.sys.platform", "linux")
@patch("claude_swap.update_check._detect_install_method", return_value="uv")
@patch("claude_swap.update_check.subprocess.run")
@patch("claude_swap.update_check.urllib.request.urlopen")
class TestUpgradeAlreadyCurrent:
    def test_skips_reinstall_when_on_latest(
        self, mock_urlopen, mock_run, mock_detect, monkeypatch, capsys
    ):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.1.1", raising=False)
        mock_urlopen.return_value = _make_release_response("0.1.1")

        assert run_self_upgrade() == 0

        mock_run.assert_not_called()
        assert "already on cc-v0.1.1" in capsys.readouterr().out

    def test_force_reinstalls_anyway(
        self, mock_urlopen, mock_run, mock_detect, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.1.1", raising=False)
        mock_urlopen.return_value = _make_release_response("0.1.1")
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade(force=True) == 0

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", f"{FORK}@cc-v0.1.1"], check=False
        )

    def test_never_downgrades_to_an_older_release(
        self, mock_urlopen, mock_run, mock_detect, monkeypatch, capsys
    ):
        # `upgrade --check` calls 0.2.0 up to date against a 0.1.1 release;
        # `upgrade` must not then install that older release.
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.2.0", raising=False)
        mock_urlopen.return_value = _make_release_response("0.1.1")

        assert run_self_upgrade() == 0

        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "0.2.0" in out and "cc-v0.1.1" in out
        assert "--force" in out

    def test_a_prerelease_build_is_not_moved_back_to_the_last_final(
        self, mock_urlopen, mock_run, mock_detect, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.4.0rc1", raising=False)
        mock_urlopen.return_value = _make_release_response("0.3.1")

        assert run_self_upgrade() == 0

        mock_run.assert_not_called()

    def test_force_installs_the_older_release(
        self, mock_urlopen, mock_run, mock_detect, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.2.0", raising=False)
        mock_urlopen.return_value = _make_release_response("0.1.1")
        mock_run.return_value = MagicMock(returncode=0)

        assert run_self_upgrade(force=True) == 0

        mock_run.assert_called_once_with(
            ["uv", "tool", "install", "--force", f"{FORK}@cc-v0.1.1"], check=False
        )

    def test_unparseable_installed_version_reinstalls(
        self, mock_urlopen, mock_run, mock_detect, monkeypatch
    ):
        monkeypatch.setattr(
            "claude_swap.update_check.__version__", "0+unknown", raising=False
        )
        mock_urlopen.return_value = _make_release_response("0.1.1")
        mock_run.return_value = MagicMock(returncode=0)

        run_self_upgrade()

        mock_run.assert_called_once()


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

        assert json.loads(cache_path.read_text())["data"] == "cc-v0.4.0"

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
        _write_cache(cache_path, "cc-v0.5.0")
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "pipx")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "(0.5.0)" in result
        assert f"pipx install --force {FORK}@cc-v0.5.0" in result


class TestInstallHintsNameTheFork:
    """The PyPI ``claude-swap`` project is upstream: any hint that tells the
    user to install it would replace the fork with upstream."""

    SRC = Path(__file__).resolve().parents[1] / "src" / "claude_swap"

    # `claude-swap` as a requirement name, not as part of a path or log name
    # (``~/.claude-swap-backup``, ``claude-swap.log``).
    _UPSTREAM_DIST = r"(?<![\w.-])claude-swap(?![\w./-])"
    _FORBIDDEN = {
        "upstream extra": re.compile(r"claude-swap\["),
        "pypi url": re.compile(r"pypi\.(?:org|python\.org)", re.IGNORECASE),
        "installer line": re.compile(
            rf"\b(?:pip3?|pipx|uv)\b[^\n]*\b(?:install|upgrade)\b[^\n]*{_UPSTREAM_DIST}"
        ),
    }

    def test_no_source_line_installs_upstream(self):
        offenders = []
        for path in sorted(self.SRC.rglob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for name, pattern in self._FORBIDDEN.items():
                    if pattern.search(line):
                        offenders.append(f"{path.name}:{lineno} ({name}): {line.strip()}")
        assert not offenders, "\n".join(offenders)

    def test_scan_would_catch_the_old_hints(self):
        # Guard the guard: the exact lines this test replaced must trip it.
        old = [
            "uv tool install --managed-python --force 'claude-swap[menubar]'",
            "pipx install --force --python <that python> 'claude-swap[menubar]'",
            "Install with: pip install 'claude-swap[menubar]'",
            "pip install claude-swap",
            "uv tool upgrade claude-swap",
        ]
        for line in old:
            assert any(p.search(line) for p in self._FORBIDDEN.values()), line
        for fine in ["~/.claude-swap-backup", "claude-swap.log", "uv tool upgrade rebuilds"]:
            assert not any(p.search(fine) for p in self._FORBIDDEN.values()), fine

    def test_menubar_install_spec_is_the_fork_from_git(self):
        from claude_swap.update_check import _install_spec

        assert _install_spec(menubar=True) == (
            "cc-swap[menubar] @ git+https://github.com/wonjun-lab/cc-swap"
        )
        assert _install_spec(menubar=False) == INSTALL_URL
