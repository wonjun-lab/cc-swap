"""Check the cc-swap fork's GitHub Releases for newer versions.

cc-swap is installed from git (``uv tool install git+...``), never from PyPI:
the PyPI ``claude-swap`` project is upstream, and following it would offer to
replace the fork with upstream.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import NamedTuple

from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache

# Not upstream's ``update_check.json``: the backup root (and so CACHE_DIR) is
# shared with an upstream cswap install, whose cached PyPI version would read
# here as the fork's latest release.
CACHE_PATH = CACHE_DIR / "cc_swap_update_check.json"
CACHE_TTL = 24 * 3600  # 24 hours
RELEASES_URL = "https://api.github.com/repos/wonjun-lab/cc-swap/releases/latest"
INSTALL_URL = "git+https://github.com/wonjun-lab/cc-swap"

_VERSION_RE = re.compile(
    r"(\d+(?:\.\d+)*)(?:[-_.]?(alpha|beta|preview|pre|rc|a|b|c)[-_.]?(\d+)?)?",
    re.IGNORECASE,
)
_PRE_RANKS = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "preview": 2, "pre": 2, "rc": 2, "c": 2}
_FINAL_RANK = 3


class _Version(NamedTuple):
    """A version as a sort key. NamedTuple so that comparing two of these
    compares the release first and only then the pre-release fields, which is
    what puts 0.27.0b1 below 0.27.0 and both below 0.28.0."""

    release: tuple[int, ...]
    pre_rank: int  # _FINAL_RANK when this is not a pre-release.
    pre_number: int


def _parse_version(v: str) -> _Version:
    """Parse a release number with an optional PEP 440 pre-release suffix.

    Every cycle of claude-swap ships as a pre-release first (0.27.0b1,
    0.26.0b1, ...), and a plain int() over the dotted parts raised ValueError
    on those, which check_for_update swallowed as "no update available".
    Development and post releases are not modeled — the project publishes
    none — so they collapse onto the release they belong to.
    """
    m = _VERSION_RE.match(v)
    if m is None:
        raise ValueError(f"unrecognized version: {v!r}")
    release = tuple(int(x) for x in m.group(1).split("."))
    # PEP 440 makes 0.27 and 0.27.0 the same release, and the pre-release rank
    # only means anything once the release segments line up.
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    if m.group(2) is None:
        return _Version(release, _FINAL_RANK, 0)
    return _Version(release, _PRE_RANKS[m.group(2).lower()], int(m.group(3) or 0))


def _is_newer(latest: str, current: str) -> bool:
    """Whether the latest release is an upgrade worth telling the user about."""
    latest_version = _parse_version(latest)
    current_version = _parse_version(current)
    # Nobody on a final release asked to be moved onto a pre-release, so we
    # stay quiet for them; people already running one still hear about later
    # pre-releases.
    if latest_version.pre_rank != _FINAL_RANK and current_version.pre_rank == _FINAL_RANK:
        return False
    return latest_version > current_version


def _detect_install_method() -> str | None:
    """Return 'uv', 'pipx', or None if we can't tell."""
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))

    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"

    # Env-var override: only trust if sys.prefix is actually under it.
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


def _has_menubar_extra() -> bool:
    """Whether this install carries the ``menubar`` extra (rumps importable).

    A reinstall from a bare git URL drops extras, so the upgrade command has
    to name the extra to keep the menu bar working afterwards.
    """
    return importlib.util.find_spec("rumps") is not None


def _install_spec() -> str:
    if _has_menubar_extra():
        return f"cc-swap[menubar] @ {INSTALL_URL}"
    return INSTALL_URL


def _upgrade_command(method: str | None) -> list[str] | None:
    """The reinstall-from-git command for ``method``, or None if unknown.

    ``install --force`` rather than ``upgrade``: the tool came from a git URL,
    and a forced install re-resolves the default branch on every run.
    """
    spec = _install_spec()
    return {
        "uv": ["uv", "tool", "install", "--force", spec],
        "pipx": ["pipx", "install", "--force", spec],
    }.get(method or "")


def _fetch_latest_release() -> str | None:
    """The fork's latest published release version (tag without ``v``).

    None on any failure. That includes HTTP 404, which is what GitHub answers
    while the fork has no published (non-draft, non-prerelease) release yet:
    a normal state, not one to report. A 403 rate limit or a 5xx is just as
    unactionable for a passive check.
    """
    req = urllib.request.Request(
        RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "cc-swap-update-check",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        return None
    tag = tag.strip()
    return tag[1:] if tag[:1] in ("v", "V") else tag


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    try:
        cached_data = read_cache(CACHE_PATH, CACHE_TTL)
        if cached_data is not MISSING:
            latest_version = cached_data
        else:
            latest_version = _fetch_latest_release()
            # Cache failures too (as upstream does): offline, the next
            # command must not pay the 2s timeout again.
            write_cache(CACHE_PATH, latest_version)

        if latest_version and _is_newer(latest_version, current_version):
            direct = _upgrade_command(_detect_install_method())
            if direct and sys.platform != "win32":
                # cc-swap upgrade actually performs the reinstall here.
                hint = "Run `cc-swap upgrade` to update."
            elif direct:
                # Windows: cc-swap upgrade only prints, so point at the command.
                hint = f"Run `{shlex.join(direct)}` to update."
            else:
                # Unknown install method: cc-swap upgrade shows instructions.
                hint = "Run `cc-swap upgrade` for upgrade instructions."
            return (
                f"A newer version of cc-swap is available ({latest_version}). "
                f"You are using {current_version}. {hint}"
            )
        return None
    except Exception:
        return None


def run_self_upgrade() -> int:
    """Run the appropriate upgrade command for the current install method.

    Returns the subprocess exit code, or 1 if detection failed or the package
    manager is missing from PATH.
    """
    from claude_swap.printer import accent, error

    method = _detect_install_method()
    cmd = _upgrade_command(method)
    if cmd is None:
        error(
            "Could not detect install method (looked for uv tool / pipx).\n"
            f"  sys.prefix:     {sys.prefix}\n"
            f"  sys.executable: {sys.executable}\n"
            "To upgrade manually, run one of:\n"
            f"  uv tool install --force {INSTALL_URL}\n"
            f"  pipx install --force {INSTALL_URL}\n"
            f"  {sys.executable} -m pip install --upgrade {INSTALL_URL}\n"
            "If you installed with `pip install -e .`, use `git pull` instead."
        )
        return 1

    # Windows: the running cc-swap.exe launcher is locked, so an in-process
    # uv/pipx reinstall fails when it tries to replace the executable even
    # though the package itself updates. cc-swap exits right after this,
    # which releases the lock, so the user can just run the command.
    if sys.platform == "win32":
        print(f"To upgrade cc-swap on Windows, run:\n  {accent(shlex.join(cmd))}")
        return 1

    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        error(
            f"Detected {method} install but `{cmd[0]}` is not on PATH. "
            "Run the upgrade manually from a shell where it is available."
        )
        return 1
