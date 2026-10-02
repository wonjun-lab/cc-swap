"""One parser for ``claude --version`` text, shared by the priming version
guard (``prime_verify``) and ``cc-swap claude-update``: the version
``claude-update`` records must equal the one ``prime verify`` verifies, or
the guard stays paused for good."""

from __future__ import annotations

import pytest

from claude_swap.maximize import claude_update as cu
from claude_swap.maximize import claude_version as cv
from claude_swap.maximize import prime_verify as pv

CASES = [
    ("2.1.287 (Claude Code)\n", "2.1.287"),
    ("2.2.0-beta-1 (Claude Code)", "2.2.0-beta-1"),
    ("1.0.0+abc", "1.0.0+abc"),
    ("1.0.0+abc (Claude Code)", "1.0.0+abc"),
    ("claude 10.0.12-beta.1", "10.0.12-beta.1"),
    ("2.3.0-rc.1+build.5 (Claude Code)", "2.3.0-rc.1+build.5"),
    ("no version here", None),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize(("text", "version"), CASES)
def test_both_modules_parse_identically(text, version):
    assert cv.parse_version(text) == version
    assert pv.parse_version(text) == version
    assert cu.parse_version(text) == version


def test_the_parser_is_literally_shared():
    assert pv.parse_version is cv.parse_version
    assert cu.parse_version is cv.parse_version


@pytest.mark.parametrize("text", ["2.2.0-beta-1 (Claude Code)", "1.0.0+abc"])
def test_a_version_claude_update_records_is_covered_by_the_same_verification(tmp_path, text):
    version = cu.parse_version(text)
    cu.record_version(tmp_path, version, "2.1.0")
    pv.record_verified(tmp_path, pv.parse_version(text), by=pv.VERIFIED_BY_CLI)
    assert pv.pending_update(tmp_path) is None


def test_ordering_ignores_build_metadata_and_ranks_a_release_over_its_prerelease():
    assert cu.is_newer("2.2.0", "2.2.0-beta-1")
    assert not cu.is_newer("2.2.0+build.2", "2.2.0+build.1")
    assert cu.is_newer("2.3.0+x", "2.2.0")
