"""The one parser for ``claude --version`` text.

``prime_verify`` (the priming version guard) turns ``claude --version``
output into a version string and compares it with the recorded one for
equality; every reader goes through this parser, so ``2.2.0-beta-1 (Claude
Code)`` or ``1.0.0+abc`` always reads the same.

The version is ``MAJOR.MINOR.PATCH`` with an optional ``-pre-release`` (dots
and hyphens allowed) and an optional ``+build`` suffix, kept whole.
"""

from __future__ import annotations

import re

VERSION_RE = re.compile(
    r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?"
)


def parse_version(text: str | None) -> str | None:
    """``2.1.287 (Claude Code)`` -> ``2.1.287``; None when unreadable."""
    match = VERSION_RE.search(text or "")
    return match.group(0) if match else None

