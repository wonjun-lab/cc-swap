"""Check the cc-swap fork's GitHub Releases for newer versions.

cc-swap is installed from git (``uv tool install git+...``), never from PyPI:
the PyPI ``claude-swap`` project is upstream, and following it would offer to
replace the fork with upstream.

Release tags are ``cc-vX.Y.Z``. The fork inherited upstream's ``v0.3.0`` ...
``v0.26.0`` tags, so a fork release named ``v0.4.0`` would sit on upstream's
old commit; only ``cc-v`` releases are considered (plus the four pre-``cc-v``
fork tags in :data:`LEGACY_TAGS`, and only while no ``cc-v`` release exists).
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

from claude_swap import __version__
from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache

# Not upstream's ``update_check.json``: the backup root (and so CACHE_DIR) is
# shared with an upstream cswap install, whose cached PyPI version would read
# here as the fork's latest release.
CACHE_PATH = CACHE_DIR / "cc_swap_update_check.json"
CACHE_TTL = 24 * 3600  # 24 hours
_API_URL = "https://api.github.com/repos/wonjun-lab/cc-swap"
# The releases list, not ``/releases/latest``: "latest" is whatever GitHub
# flags (by default the newest by tag date) and can be a tag outside the
# ``cc-v`` scheme. Listed newest first, so 100 is far more than needed.
RELEASES_URL = f"{_API_URL}/releases?per_page=100"
RELEASE_TAG_PREFIX = "cc-v"
# The fork's releases before the ``cc-v`` scheme. An explicit allowlist, never
# a ``v*`` pattern: every other ``v*`` tag is upstream's.
LEGACY_TAGS = ("v0.1.0", "v0.1.1", "v0.2.0", "v0.3.0")
INSTALL_URL = "git+https://github.com/wonjun-lab/cc-swap"
# Seconds `cc-swap upgrade` waits for GitHub to name the latest release; the
# passive update notice keeps its 2s so that it never slows a command down.
UPGRADE_LOOKUP_TIMEOUT = 10
# `upgrade --check` exit code when a newer release exists (0 = up to date,
# 1 = could not tell), so a timer or script can branch on it.
EXIT_UPDATE_AVAILABLE = 10
# How much of the change list `upgrade --check` prints.
_NOTES_MAX_LINES = 15
_COMMITS_MAX = 30
_NO_RELEASE_NOTE = (
    "No published cc-swap release could be found (there may be none yet, or "
    "GitHub could not be reached), so this installs the default branch "
    "instead of a release."
)

_VERSION_RE = re.compile(
    r"(\d+(?:\.\d+)*)(?:[-_.]?(alpha|beta|preview|pre|rc|a|b|c)[-_.]?(\d+)?)?",
    re.IGNORECASE,
)
# Tag names we are willing to splice into an install URL.
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
# What follows ``cc-v``: a final release, or one with a PEP 440 a/b/rc suffix.
_RELEASE_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")
_PRE_RANKS = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "preview": 2, "pre": 2, "rc": 2, "c": 2}
_FINAL_RANK = 3


class _Version(NamedTuple):
    """A version as a sort key. NamedTuple so that comparing two of these
    compares the release first and only then the pre-release fields, which is
    what puts 0.27.0b1 below 0.27.0 and both below 0.28.0."""

    release: tuple[int, ...]
    pre_rank: int  # _FINAL_RANK when this is not a pre-release.
    pre_number: int


def release_tag(version: str) -> str:
    """The git tag the release of ``version`` is published under.

    ``0.3.1`` -> ``cc-v0.3.1``. Raises ValueError for anything that is not a
    bare ``X.Y.Z`` (optionally ``aN``/``bN``/``rcN``) version, so a typo such
    as ``v0.3.1`` cannot produce a tag the update check would then ignore.
    """
    if not _RELEASE_VERSION_RE.fullmatch(version):
        raise ValueError(f"not a release version (expected X.Y.Z, e.g. 0.3.1): {version!r}")
    return f"{RELEASE_TAG_PREFIX}{version}"


def _is_prefixed_tag(tag: str) -> bool:
    return tag.startswith(RELEASE_TAG_PREFIX) and bool(
        _RELEASE_VERSION_RE.fullmatch(tag[len(RELEASE_TAG_PREFIX):])
    )


def is_fork_tag(tag: str) -> bool:
    """Whether ``tag`` names one of the fork's own releases: ``cc-vX.Y.Z`` or
    one of the :data:`LEGACY_TAGS`. Upstream's inherited ``v*`` tags are not."""
    return _is_prefixed_tag(tag) or tag in LEGACY_TAGS


def _tag_for_version(version: str) -> str:
    """The tag an already-released ``version`` of the fork lives at: the legacy
    ``v`` tag for 0.1.0 - 0.3.0, ``cc-v`` for everything after."""
    legacy = f"v{version}"
    return legacy if legacy in LEGACY_TAGS else f"{RELEASE_TAG_PREFIX}{version}"


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


def _install_url(tag: str | None = None) -> str:
    """The git URL to install from: the ``tag`` release, or the default branch."""
    return f"{INSTALL_URL}@{tag}" if tag else INSTALL_URL


def _install_spec(tag: str | None = None, menubar: bool | None = None) -> str:
    """What to hand ``uv tool install`` / ``pipx install``.

    ``menubar`` defaults to whether this install already carries the extra, so
    a reinstall keeps it; the menu bar's own install hints pass ``True``.
    """
    url = _install_url(tag)
    if _has_menubar_extra() if menubar is None else menubar:
        return f"cc-swap[menubar] @ {url}"
    return url


def _upgrade_command(method: str | None, tag: str | None = None) -> list[str] | None:
    """The reinstall-from-git command for ``method``, or None if unknown.

    ``install --force`` rather than ``upgrade``: the tool came from a git URL,
    and a forced install re-resolves the ref on every run. ``tag`` pins that
    ref to a release; without it the default branch is installed, which can be
    ahead of (or behind) the release the update notice announced.
    """
    spec = _install_spec(tag)
    return {
        "uv": ["uv", "tool", "install", "--force", spec],
        "pipx": ["pipx", "install", "--force", spec],
    }.get(method or "")


def _get_json(url: str, timeout: float) -> object | None:
    """GET ``url`` from the GitHub API and decode it; None on any failure."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "cc-swap-update-check",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _fork_releases(data: object) -> list[tuple[_Version, str, dict]]:
    """``(version, tag, release)`` for every published fork release in a
    releases-list payload, highest version first.

    Drafts and pre-releases are out, and so is any tag that is not the fork's
    (see :func:`is_fork_tag`). The legacy tags only count while no ``cc-v``
    release is published. The tag comes off the network and ends up in a URL
    handed to the package manager, hence the strict shape check.
    """
    if not isinstance(data, list):
        return []
    prefixed: list[tuple[_Version, str, dict]] = []
    legacy: list[tuple[_Version, str, dict]] = []
    for item in data:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag) or not is_fork_tag(tag):
            continue
        try:
            version = _parse_version(_tag_version(tag))
        except ValueError:
            continue
        (prefixed if _is_prefixed_tag(tag) else legacy).append((version, tag, item))
    chosen = prefixed or legacy
    chosen.sort(key=lambda entry: entry[0], reverse=True)
    return chosen


def _pick_latest_tag(data: object) -> str | None:
    """The tag of the highest published fork release in ``data``, or None."""
    releases = _fork_releases(data)
    return releases[0][1] if releases else None


def _fetch_latest_tag(timeout: float = 2) -> str | None:
    """The fork's latest published release tag, as published (``cc-v0.4.0``).

    None on any failure. That includes HTTP 404 and an empty list, which is
    what GitHub answers while the fork has no published (non-draft,
    non-prerelease) release yet: a normal state, not one to report. A 403 rate
    limit or a 5xx is just as unactionable for a passive check.
    """
    return _pick_latest_tag(_get_json(RELEASES_URL, timeout))


def _tag_version(tag: str) -> str:
    """The version a release tag names: ``cc-v0.4.0`` -> ``0.4.0`` (and the
    legacy ``v0.3.0`` -> ``0.3.0``)."""
    if tag.startswith(RELEASE_TAG_PREFIX):
        return tag[len(RELEASE_TAG_PREFIX):]
    return tag[1:] if tag[:1] in ("v", "V") else tag


def _cached_tag() -> str | None:
    """The tag in the update-check cache however old it is, or None.

    Only a last resort for ``upgrade`` when GitHub is unreachable, so the
    24 h TTL is ignored; the value is re-validated because the file is not
    something we trust to hold a safe ref.
    """
    cached = read_cache(CACHE_PATH, float("inf"))
    if isinstance(cached, str) and _TAG_RE.fullmatch(cached) and is_fork_tag(cached):
        return cached
    return None


def _latest_tag_for_upgrade() -> tuple[str | None, bool]:
    """``(tag, from_cache)`` for ``cc-swap upgrade``.

    Unlike the passive notice this never trusts the cache while GitHub
    answers: a release published after the last check would otherwise be
    skipped for up to a day. A live answer refreshes the cache so the notice
    agrees with what was just installed. Only when the live lookup fails does
    the cached tag stand in.
    """
    tag = _fetch_latest_tag(timeout=UPGRADE_LOOKUP_TIMEOUT)
    if tag is not None:
        try:
            write_cache(CACHE_PATH, tag)
        except OSError:
            pass
        return tag, False
    cached = _cached_tag()
    return cached, cached is not None


def _is_installed(tag: str) -> bool:
    """Whether the running version is exactly the release ``tag`` names."""
    try:
        return _parse_version(_tag_version(tag)) == _parse_version(__version__)
    except ValueError:
        return False


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    try:
        cached_data = read_cache(CACHE_PATH, CACHE_TTL)
        if cached_data is not MISSING:
            latest_tag = cached_data
        else:
            latest_tag = _fetch_latest_tag()
            # Cache failures too (as upstream does): offline, the next
            # command must not pay the 2s timeout again. The tag is cached as
            # published, not as a bare version: the Windows hint below has to
            # name the exact git ref the release lives at.
            write_cache(CACHE_PATH, latest_tag)

        # The cache file is not something we trust to hold one of our tags.
        if not (isinstance(latest_tag, str) and is_fork_tag(latest_tag)):
            latest_tag = None

        latest_version = _tag_version(latest_tag) if latest_tag else None
        if latest_version and _is_newer(latest_version, current_version):
            direct = _upgrade_command(_detect_install_method(), latest_tag)
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


def run_self_upgrade(force: bool = False) -> int:
    """Run the appropriate upgrade command for the current install method.

    Installs the latest published release (the one the update notice
    announces), pinned by its tag, looked up live rather than from the 24 h
    cache. If GitHub cannot be reached the cached tag is used, with a
    warning. When no release can be determined at all it falls back to the
    default branch and says so. Already being on the latest release is a
    no-op unless ``force``.

    Returns the subprocess exit code, or 1 if detection failed or the package
    manager is missing from PATH.
    """
    from claude_swap.printer import accent, error, warning

    method = _detect_install_method()
    # Unlike the passive check, the user asked for this, so wait for GitHub.
    tag, from_cache = _latest_tag_for_upgrade()
    if tag is not None and not force and _is_installed(tag):
        print(f"cc-swap is already on {tag}; nothing to do (use --force to reinstall).")
        return 0
    cmd = _upgrade_command(method, tag)
    url = _install_url(tag)
    if cmd is None:
        error(
            "Could not detect install method (looked for uv tool / pipx).\n"
            f"  sys.prefix:     {sys.prefix}\n"
            f"  sys.executable: {sys.executable}\n"
            "To upgrade manually, run one of:\n"
            f"  uv tool install --force {url}\n"
            f"  pipx install --force {url}\n"
            f"  {sys.executable} -m pip install --upgrade {url}\n"
            "If you installed with `pip install -e .`, use `git pull` instead."
            + ("" if tag else f"\n{_NO_RELEASE_NOTE}")
        )
        return 1

    if tag is None:
        warning(_NO_RELEASE_NOTE)
    elif from_cache:
        warning(
            "Could not reach GitHub to look up the latest release; "
            f"installing the cached tag {tag}, which may be out of date."
        )

    # Windows: the running cc-swap.exe launcher is locked, so an in-process
    # uv/pipx reinstall fails when it tries to replace the executable even
    # though the package itself updates. cc-swap exits right after this,
    # which releases the lock, so the user can just run the command.
    if sys.platform == "win32":
        print(f"To upgrade cc-swap on Windows, run:\n  {accent(shlex.join(cmd))}")
        return 1

    if tag is not None:
        print(f"Installing cc-swap release {tag} ...")
    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            _refresh_service()
        return result.returncode
    except FileNotFoundError:
        error(
            f"Detected {method} install but `{cmd[0]}` is not on PATH. "
            "Run the upgrade manually from a shell where it is available."
        )
        return 1


def _refresh_service() -> None:
    """Re-run ``cc-swap service install`` when the service is installed.

    The service keeps running the build it was started with, so without this
    an upgrade leaves a stale engine behind. It shells out to the console
    script rather than calling :func:`service.install` because this process
    still holds the old code. Never fails the upgrade: the reinstall already
    happened, so a problem here is a warning with the command to run.
    """
    from claude_swap.printer import warning

    try:
        from claude_swap.maximize import service

        if not service.status().get("installed"):
            return
        cmd = service.reinstall_command()
    except Exception:
        # No service for this platform, or launchctl/systemctl is absent.
        return
    print("Refreshing the cc-swap service so it runs the new build ...")
    try:
        rc = subprocess.run(cmd, check=False).returncode
    except OSError:
        rc = 1
    if rc != 0:
        warning(
            "Could not restart the cc-swap service on the new build; run "
            "`cc-swap service install` yourself.",
            file=sys.stderr,
        )


def _release_notes_between(installed: str, latest: str) -> list[tuple[str, str]]:
    """``(tag, notes)`` of each published release after ``installed`` up to
    ``latest``, newest first. Empty on any failure (best effort)."""
    data = _get_json(RELEASES_URL, UPGRADE_LOOKUP_TIMEOUT)
    if not isinstance(data, list):
        return []
    try:
        low, high = _parse_version(installed), _parse_version(latest)
    except ValueError:
        return []
    found: list[tuple[str, str]] = []
    for version, tag, item in _fork_releases(data):
        if not low < version <= high:
            continue
        body = item.get("body")
        found.append((tag, body.strip() if isinstance(body, str) else ""))
    return found


def _commit_subjects_between(installed: str, tag: str) -> list[str]:
    """First lines of the commits between the installed release and ``tag``
    (GitHub compare API), oldest first. Empty on any failure."""
    base = _tag_for_version(installed)
    if not (_TAG_RE.fullmatch(base) and _TAG_RE.fullmatch(tag)):
        return []
    data = _get_json(f"{_API_URL}/compare/{base}...{tag}", UPGRADE_LOOKUP_TIMEOUT)
    commits = data.get("commits") if isinstance(data, dict) else None
    subjects: list[str] = []
    for item in commits if isinstance(commits, list) else []:
        message = (item.get("commit") or {}).get("message") if isinstance(item, dict) else None
        if isinstance(message, str) and message.strip():
            subjects.append(message.strip().splitlines()[0])
    return subjects


def run_upgrade_check() -> int:
    """``cc-swap upgrade --check``: report, never install.

    Prints the installed and the latest release, and when the latter is newer
    the release notes and commit subjects in between. Exit codes: 0 up to
    date, 10 update available, 1 the latest release could not be determined.
    """
    from claude_swap.printer import accent, dimmed, error, warning

    tag, from_cache = _latest_tag_for_upgrade()
    if tag is None:
        error(
            "Could not determine the latest cc-swap release (GitHub could not "
            "be reached, or no release is published yet)."
        )
        return 1
    if from_cache:
        warning(
            "Could not reach GitHub; using the cached release tag "
            f"{tag}, which may be out of date.",
            file=sys.stderr,
        )
    latest = _tag_version(tag)
    print(f"Installed: {__version__}")
    print(f"Latest:    {latest}")
    try:
        newer = _is_newer(latest, __version__)
    except ValueError:
        newer = not _is_installed(tag)
    if not newer:
        print(f"cc-swap is up to date ({__version__}).")
        return 0

    for note_tag, body in _release_notes_between(__version__, latest):
        print(f"\n{accent(note_tag)}")
        lines = body.splitlines()
        for line in lines[:_NOTES_MAX_LINES]:
            print(f"  {line}")
        if len(lines) > _NOTES_MAX_LINES:
            print(dimmed(f"  ... ({len(lines) - _NOTES_MAX_LINES} more lines)"))
    subjects = _commit_subjects_between(__version__, tag)
    if subjects:
        print(f"\nChanges since {__version__}:")
        for subject in subjects[:_COMMITS_MAX]:
            print(f"  - {subject}")
        if len(subjects) > _COMMITS_MAX:
            print(dimmed(f"  ... and {len(subjects) - _COMMITS_MAX} more commits"))

    print()
    direct = _upgrade_command(_detect_install_method(), tag)
    if direct and sys.platform != "win32":
        print(f"Update with: {accent('cc-swap upgrade')}")
    elif direct:
        print(f"Update with: {accent(shlex.join(direct))}")
    else:
        url = _install_url(tag)
        print(
            "Could not detect a uv tool / pipx install. Update manually with one of:\n"
            f"  uv tool install --force {url}\n"
            f"  pipx install --force {url}\n"
            f"  {sys.executable} -m pip install --upgrade {url}\n"
            "If you installed with `pip install -e .`, use `git pull` instead."
        )
    return EXIT_UPDATE_AVAILABLE
