"""The one parser for ``claude --version`` text.

``prime_verify`` (the priming version guard) and ``claude_update`` (``cc-swap
claude-update``) both turn ``claude --version`` output into a version string
and later compare the two strings for equality: the version ``claude-update``
records must be the version ``prime verify`` verifies. Two parsers that
disagree on ``2.2.0-beta-1 (Claude Code)`` or ``1.0.0+abc`` would leave the
guard paused for good, so there is exactly this one.

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


def sort_key(version: str) -> tuple[int, int, int, int]:
    """Ordering key; build metadata is ignored and a release outranks its
    pre-release (``2.2.0-beta.1 < 2.2.0``). Raises ValueError on non-versions."""
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(version)
    major, minor, patch, pre, _build = match.groups()
    return int(major), int(minor), int(patch), 0 if pre else 1
