"""Release tag scheme: fork releases are tagged ``cc-vX.Y.Z``.

The fork inherited upstream's tags (v0.3.0 ... v0.26.0), so ``gh release create
v0.3.0`` attached the fork's release to upstream's old tag and ``cc-swap
upgrade`` installed upstream 0.3.0. These tests pin the update check to the
``cc-v`` namespace (plus an explicit legacy allowlist) and cover the pure parts
of ``tools/release.py``. Nothing here reaches GitHub.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_swap.update_check as uc

ROOT = Path(__file__).resolve().parents[1]
FORK = "git+https://github.com/wonjun-lab/cc-swap"


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    def _offline(*args, **kwargs):
        raise OSError("network disabled in tests")

    monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _offline)
    monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr("claude_swap.update_check._has_menubar_extra", lambda: False)
    monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
    monkeypatch.setattr("claude_swap.update_check.sys.platform", "linux")
    monkeypatch.setattr(
        "claude_swap.update_check.subprocess.run",
        lambda *a, **k: pytest.fail(f"subprocess.run called: {a}"),
    )
    # The lookup authenticates from $GITHUB_TOKEN / $GH_TOKEN or `gh auth
    # token`; none of that may leak in from the machine running the suite.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(
        "claude_swap.update_check.shutil", SimpleNamespace(which=lambda *a, **k: None)
    )
    uc._gh_cli_token.cache_clear()
    yield
    uc._gh_cli_token.cache_clear()


def _rel(tag: str, **extra) -> dict:
    return {"tag_name": tag, "body": "", "draft": False, "prerelease": False, **extra}


def _serve(monkeypatch, routes: dict) -> list[str]:
    """Answer GitHub API urls from ``routes`` (url -> payload); anything else
    is a network error. Returns the urls requested."""
    seen: list[str] = []

    def _urlopen(req, timeout=None):
        seen.append(req.full_url)
        if req.full_url not in routes:
            raise OSError("not routed")
        resp = MagicMock()
        resp.read.return_value = json.dumps(routes[req.full_url]).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    monkeypatch.setattr("claude_swap.update_check.urllib.request.urlopen", _urlopen)
    return seen


class TestTagNaming:
    def test_release_tag_prefixes_the_version(self):
        assert uc.release_tag("0.3.1") == "cc-v0.3.1"
        assert uc.release_tag("0.4.0b1") == "cc-v0.4.0b1"

    @pytest.mark.parametrize(
        "bad", ["", "v0.3.1", "cc-v0.3.1", "0.3", "0.3.1.", "0.3.1 beta", "0.3.1/../x", "x"]
    )
    def test_release_tag_refuses_a_malformed_version(self, bad):
        with pytest.raises(ValueError):
            uc.release_tag(bad)

    @pytest.mark.parametrize(
        "tag", ["cc-v0.3.1", "cc-v10.20.30", "cc-v0.4.0b1", "cc-v0.4.0rc2"]
    )
    def test_cc_v_tags_are_fork_tags(self, tag):
        assert uc.is_fork_tag(tag)

    @pytest.mark.parametrize("tag", ["v0.1.0", "v0.1.1", "v0.2.0", "v0.3.0"])
    def test_legacy_allowlist_are_fork_tags(self, tag):
        assert uc.is_fork_tag(tag)

    @pytest.mark.parametrize(
        "tag",
        [
            "v0.4.0", "v0.26.0", "v0.3.1", "v0.1.2", "v0.9.0b1", "0.3.0", "V0.3.0",
            "cc-v", "cc-v0", "cc-v0.3", "cc-v0.3.1.", "cc-v0.3.1@evil", "cc-v0.3.1 x",
            "cc-v0.3.1#f", "CC-v0.3.1", "cc-0.3.1", "xcc-v0.3.1", "cc-vv0.3.1", "",
        ],
    )
    def test_everything_else_is_not_a_fork_tag(self, tag):
        assert not uc.is_fork_tag(tag)

    def test_tag_version_strips_the_prefix(self):
        assert uc._tag_version("cc-v0.3.1") == "0.3.1"
        assert uc._tag_version("v0.3.0") == "0.3.0"  # legacy

    def test_tag_for_version_uses_the_legacy_tag_only_for_legacy_versions(self):
        assert uc._tag_for_version("0.2.0") == "v0.2.0"
        assert uc._tag_for_version("0.3.0") == "v0.3.0"
        assert uc._tag_for_version("0.3.1") == "cc-v0.3.1"
        assert uc._tag_for_version("0.4.0") == "cc-v0.4.0"

    def test_install_url_pins_the_cc_v_tag(self):
        assert uc._install_url("cc-v0.3.1") == f"{FORK}@cc-v0.3.1"


class TestLatestTagSelection:
    def test_picks_the_highest_cc_v_version_not_the_first_listed(self):
        data = [_rel("cc-v0.3.2"), _rel("cc-v0.10.0"), _rel("cc-v0.4.0")]
        assert uc._pick_latest_tag(data) == "cc-v0.10.0"

    def test_upstream_tags_never_win_however_high(self):
        data = [_rel("v0.26.0"), _rel("v0.4.0"), _rel("cc-v0.3.1")]
        assert uc._pick_latest_tag(data) == "cc-v0.3.1"

    def test_drafts_and_prereleases_are_skipped(self):
        data = [
            _rel("cc-v0.5.0", draft=True),
            _rel("cc-v0.4.0b1", prerelease=True),
            _rel("cc-v0.3.1"),
        ]
        assert uc._pick_latest_tag(data) == "cc-v0.3.1"

    def test_legacy_fallback_when_no_cc_v_release_exists(self):
        data = [_rel("v0.26.0"), _rel("v0.3.0"), _rel("v0.2.0"), _rel("v0.9.0")]
        assert uc._pick_latest_tag(data) == "v0.3.0"

    def test_legacy_fallback_is_only_the_allowlist(self):
        assert uc._pick_latest_tag([_rel("v0.26.0"), _rel("v0.3.1"), _rel("v0.4.0")]) is None

    def test_a_cc_v_release_hides_the_legacy_ones(self):
        data = [_rel("v0.3.0"), _rel("cc-v0.3.1")]
        assert uc._pick_latest_tag(data) == "cc-v0.3.1"

    def test_only_draft_cc_v_releases_still_fall_back_to_legacy(self):
        data = [_rel("cc-v0.3.1", draft=True), _rel("v0.3.0")]
        assert uc._pick_latest_tag(data) == "v0.3.0"

    @pytest.mark.parametrize("data", [None, {}, {"message": "Not Found"}, [], ["x", 1, None]])
    def test_junk_payloads_give_no_tag(self, data):
        assert uc._pick_latest_tag(data) is None

    def test_a_tag_that_could_not_be_a_git_ref_suffix_is_skipped(self):
        data = [_rel("cc-v9.9.9@evil"), _rel("cc-v0.3.1")]
        assert uc._pick_latest_tag(data) == "cc-v0.3.1"


class TestLookup:
    def test_fetch_uses_the_releases_list_endpoint(self, monkeypatch):
        seen = _serve(monkeypatch, {uc.RELEASES_URL: [_rel("cc-v0.3.1")]})

        assert uc._fetch_latest_tag() == "cc-v0.3.1"
        assert seen == [uc.RELEASES_URL]
        assert not any(u.endswith("/releases/latest") for u in seen)

    def test_upgrade_installs_the_cc_v_tag_not_upstreams_higher_one(self, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.3.0", raising=False)
        _serve(
            monkeypatch,
            {uc.RELEASES_URL: [_rel("v0.26.0"), _rel("v0.3.0"), _rel("cc-v0.3.1")]},
        )
        calls = []
        monkeypatch.setattr(
            "claude_swap.update_check.subprocess.run",
            lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0),
        )
        monkeypatch.setattr(
            "claude_swap.maximize.service.status", lambda **kw: {"installed": False}
        )

        assert uc.run_self_upgrade() == 0
        assert calls == [["uv", "tool", "install", "--force", f"{FORK}@cc-v0.3.1"]]

    def test_legacy_only_fork_reports_up_to_date_on_0_3_0(self, monkeypatch, capsys):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.3.0", raising=False)
        _serve(monkeypatch, {uc.RELEASES_URL: [_rel("v0.26.0"), _rel("v0.3.0")]})

        assert uc.run_upgrade_check() == 0
        assert "Latest:    0.3.0" in capsys.readouterr().out

    def test_check_from_legacy_install_compares_against_the_legacy_tag(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.3.0", raising=False)
        seen = _serve(
            monkeypatch,
            {
                uc.RELEASES_URL: [_rel("cc-v0.3.1", body="Fixes the tag collision")],
                f"{uc._API_URL}/compare/v0.3.0...cc-v0.3.1": {
                    "commits": [{"commit": {"message": "Tag releases cc-v"}}]
                },
            },
        )

        assert uc.run_upgrade_check() == uc.EXIT_UPDATE_AVAILABLE

        out = capsys.readouterr().out
        assert "Fixes the tag collision" in out and "Tag releases cc-v" in out
        assert f"{uc._API_URL}/compare/v0.3.0...cc-v0.3.1" in seen

    def test_check_from_a_cc_v_install_compares_cc_v_to_cc_v(self, monkeypatch, capsys):
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.3.1", raising=False)
        seen = _serve(
            monkeypatch,
            {
                uc.RELEASES_URL: [_rel("cc-v0.4.0"), _rel("cc-v0.3.1")],
                f"{uc._API_URL}/compare/cc-v0.3.1...cc-v0.4.0": {
                    "commits": [{"commit": {"message": "Add a thing"}}]
                },
            },
        )

        assert uc.run_upgrade_check() == uc.EXIT_UPDATE_AVAILABLE

        assert "Add a thing" in capsys.readouterr().out
        assert f"{uc._API_URL}/compare/cc-v0.3.1...cc-v0.4.0" in seen

    def test_a_poisoned_cache_entry_is_not_installed(self, monkeypatch):
        uc.write_cache(uc.CACHE_PATH, "v0.26.0")
        monkeypatch.setattr("claude_swap.update_check.__version__", "0.3.0", raising=False)
        calls = []
        monkeypatch.setattr(
            "claude_swap.update_check.subprocess.run",
            lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0),
        )
        monkeypatch.setattr(
            "claude_swap.maximize.service.status", lambda **kw: {"installed": False}
        )

        uc.run_self_upgrade()  # offline: the cached tag is the only candidate

        assert calls == [["uv", "tool", "install", "--force", uc.INSTALL_URL]]

    def test_a_poisoned_fresh_cache_gives_no_notice(self):
        uc.write_cache(uc.CACHE_PATH, "v0.26.0")

        assert uc.check_for_update("0.3.1") is None

    def test_a_legacy_tag_in_the_cache_is_still_understood(self):
        uc.write_cache(uc.CACHE_PATH, "v0.3.0")

        assert "(0.3.0)" in (uc.check_for_update("0.2.0") or "")


def _load_release_tool():
    spec = importlib.util.spec_from_file_location("release_tool", ROOT / "tools" / "release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_tool"] = module
    spec.loader.exec_module(module)
    return module


rt = _load_release_tool()


class TestReleaseToolPureParts:
    def test_tag_naming_is_the_update_checks(self):
        assert rt.tag_for("0.3.1") == "cc-v0.3.1"

    @pytest.mark.parametrize("bad", ["v0.3.1", "cc-v0.3.1", "0.3", "main", "", "0.3.1;rm"])
    def test_refuses_a_malformed_version(self, bad):
        with pytest.raises(rt.ReleaseError, match="VERSION"):
            rt.tag_for(bad)

    def test_pyproject_version_is_read(self, tmp_path):
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nname = "cc-swap"\nversion = "0.3.1"\n')
        assert rt.pyproject_version(p) == "0.3.1"

    def test_the_repos_pyproject_has_a_version(self):
        assert rt.pyproject_version(ROOT / "pyproject.toml")

    def test_fork_remote_is_found_by_url_not_by_name(self):
        out = (
            "fork\tgit@github.com:wonjun-lab/cc-swap.git (fetch)\n"
            "fork\tgit@github.com:wonjun-lab/cc-swap.git (push)\n"
            "origin\tgit@github.com:realiti4/claude-swap (fetch)\n"
            "origin\tgit@github.com:realiti4/claude-swap (push)\n"
        )
        assert rt.find_fork_remote(out) == "fork"

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/wonjun-lab/cc-swap",
            "https://github.com/wonjun-lab/cc-swap.git",
            "ssh://git@github.com/wonjun-lab/cc-swap.git",
            "git@github.com:wonjun-lab/cc-swap",
        ],
    )
    def test_fork_remote_accepts_the_usual_url_shapes(self, url):
        assert rt.find_fork_remote(f"r\t{url} (fetch)\nr\t{url} (push)\n") == "r"

    def test_no_fork_remote_means_none(self):
        out = "origin\tgit@github.com:realiti4/claude-swap (fetch)\n"
        assert rt.find_fork_remote(out) is None
        assert rt.find_fork_remote("o\thttps://github.com/wonjun-lab/cc-swap-fake (fetch)\n") is None
        assert rt.find_fork_remote("") is None


class FakeGit:
    """Stands in for the shell: maps an argv tuple to ``(rc, stdout)``."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str):
        self.calls.append(argv)
        key = tuple(argv)
        for pattern, answer in self.answers.items():
            if key[: len(pattern)] == pattern:
                return answer
        raise AssertionError(f"unexpected command: {argv}")


GOOD = {
    ("git", "remote", "-v"): (0, "fork\tgit@github.com:wonjun-lab/cc-swap.git (fetch)\n"),
    ("git", "status", "--porcelain"): (0, ""),
    ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
    ("git", "rev-parse", "HEAD"): (0, "a" * 40 + "\n"),
    ("git", "fetch", "fork", "main"): (0, ""),
    ("git", "rev-parse", "FETCH_HEAD"): (0, "a" * 40 + "\n"),
    ("git", "rev-parse", "-q", "--verify", "refs/tags/cc-v0.3.1"): (1, ""),
    ("git", "ls-remote", "--tags", "fork", "refs/tags/cc-v0.3.1"): (0, ""),
}


@pytest.fixture
def pyproject(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('[project]\nversion = "0.3.1"\n')
    return p


class TestPreflight:
    def test_all_green_returns_the_remote(self, pyproject):
        assert rt.preflight("0.3.1", FakeGit(dict(GOOD)), pyproject) == "fork"

    def _refuses(self, pyproject, override: dict, match: str, version="0.3.1"):
        answers = {**GOOD, **override}
        with pytest.raises(rt.ReleaseError, match=match):
            rt.preflight(version, FakeGit(answers), pyproject)

    def test_pyproject_version_mismatch(self, pyproject):
        self._refuses(pyproject, {}, "pyproject", version="0.3.2")

    def test_dirty_tree(self, pyproject):
        self._refuses(
            pyproject, {("git", "status", "--porcelain"): (0, " M README.md\n")}, "clean"
        )

    def test_not_on_main(self, pyproject):
        self._refuses(
            pyproject, {("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "feature\n")}, "main"
        )

    def test_behind_or_ahead_of_the_remote(self, pyproject):
        self._refuses(
            pyproject, {("git", "rev-parse", "FETCH_HEAD"): (0, "b" * 40 + "\n")}, "up to date"
        )

    def test_tag_exists_locally(self, pyproject):
        self._refuses(
            pyproject,
            {("git", "rev-parse", "-q", "--verify", "refs/tags/cc-v0.3.1"): (0, "c" * 40 + "\n")},
            "already exists locally",
        )

    def test_tag_exists_on_the_remote(self, pyproject):
        self._refuses(
            pyproject,
            {
                ("git", "ls-remote", "--tags", "fork", "refs/tags/cc-v0.3.1"): (
                    0,
                    "c" * 40 + "\trefs/tags/cc-v0.3.1\n",
                )
            },
            "already exists on",
        )

    def test_remote_lookup_failure_refuses_rather_than_assumes(self, pyproject):
        self._refuses(
            pyproject,
            {("git", "ls-remote", "--tags", "fork", "refs/tags/cc-v0.3.1"): (128, "")},
            "ls-remote",
        )

    def test_no_fork_remote(self, pyproject):
        self._refuses(
            pyproject,
            {("git", "remote", "-v"): (0, "origin\tgit@github.com:realiti4/claude-swap (fetch)\n")},
            "remote",
        )

    def test_a_malformed_version_never_reaches_git(self, pyproject):
        git = FakeGit({})
        with pytest.raises(rt.ReleaseError):
            rt.preflight("v0.3.1", git, pyproject)
        assert git.calls == []


class TestPublish:
    def test_creates_pushes_and_releases_with_verify_tag(self):
        git = FakeGit(
            {
                ("git", "tag"): (0, ""),
                ("git", "push"): (0, ""),
                ("gh", "release", "create"): (0, "https://github.com/wonjun-lab/cc-swap/releases/tag/cc-v0.3.1\n"),
            }
        )

        rt.publish("0.3.1", "fork", git)

        tag_cmd, push_cmd, gh_cmd = git.calls
        assert tag_cmd[:3] == ("git", "tag", "-a") and "cc-v0.3.1" in tag_cmd
        assert push_cmd == ("git", "push", "fork", "refs/tags/cc-v0.3.1")
        assert gh_cmd[:4] == ("gh", "release", "create", "cc-v0.3.1")
        assert "--verify-tag" in gh_cmd
        assert gh_cmd[gh_cmd.index("--repo") + 1] == "wonjun-lab/cc-swap"
        assert "--target" not in gh_cmd

    def test_a_failed_push_stops_before_gh(self):
        git = FakeGit({("git", "tag"): (0, ""), ("git", "push"): (1, "")})

        with pytest.raises(rt.ReleaseError, match="push"):
            rt.publish("0.3.1", "fork", git)

        assert not any(c[0] == "gh" for c in git.calls)


_WRITES = (("git", "tag"), ("git", "push"), ("gh", "release"))


class TestMain:
    def _git(self, override=None):
        answers = {
            **GOOD,
            ("uv", "run", "pytest"): (0, ""),
            ("git", "tag"): (0, ""),
            ("git", "push"): (0, ""),
            ("gh", "release", "create"): (0, ""),
            **(override or {}),
        }
        return FakeGit(answers)

    @pytest.fixture(autouse=True)
    def _repo_version(self, monkeypatch, pyproject):
        monkeypatch.setattr(rt, "ROOT", pyproject.parent)

    def test_dry_run_checks_and_tests_but_writes_nothing(self):
        git = self._git()

        assert rt.main(["0.3.1", "--dry-run"], run=git) == 0

        assert ("uv", "run", "pytest", "-q") in git.calls
        assert not any(c[:2] in _WRITES for c in git.calls)

    def test_failing_suite_refuses_before_tagging(self, capsys):
        git = self._git({("uv", "run", "pytest"): (1, "")})

        assert rt.main(["0.3.1"], run=git) == 1

        assert "test suite" in capsys.readouterr().err
        assert not any(c[:2] in _WRITES for c in git.calls)

    def test_refusal_is_a_message_and_exit_1(self, capsys):
        git = self._git({("git", "status", "--porcelain"): (0, "?? x\n")})

        assert rt.main(["0.3.1"], run=git) == 1

        assert "release refused" in capsys.readouterr().err

    def test_release_runs_in_order(self):
        git = self._git()

        assert rt.main(["0.3.1"], run=git) == 0

        kinds = [c[:2] for c in git.calls if c[:2] in (*_WRITES, ("uv", "run"))]
        assert kinds == [("uv", "run"), *_WRITES]
