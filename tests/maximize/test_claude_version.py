"""One parser for ``claude --version`` text: the priming version guard
(``prime_verify``) uses ``claude_version``'s, never its own."""

from __future__ import annotations

import pytest

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
def test_the_guard_parses_like_the_shared_parser(text, version):
    assert cv.parse_version(text) == version
    assert pv.parse_version(text) == version


def test_the_parser_is_literally_shared():
    assert pv.parse_version is cv.parse_version
