#!/usr/bin/env python3
"""Publish a cc-swap release: ``uv run python tools/release.py VERSION``.

Why this exists: the fork inherited upstream's tags (v0.3.0 ... v0.26.0), so
``gh release create v0.3.0 --target main`` attached the fork's release to
upstream's old ``v0.3.0`` tag and ``cc-swap upgrade`` installed upstream.
Fork releases are therefore tagged ``cc-vX.Y.Z``, and this script is the one
way to cut them. It refuses unless

* ``pyproject.toml``'s version is VERSION,
* the working tree is clean and HEAD is ``main``, equal to the fork's ``main``
  (fetched just now),
* the tag ``cc-vVERSION`` exists neither locally nor on the fork's remote,
* the whole test suite passes,

then creates an annotated tag at HEAD, pushes it, and runs ``gh release create
--verify-tag`` so gh can neither reuse nor invent a tag anywhere else.

``--dry-run`` runs every check (tests included) and stops before any write.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_swap.update_check import release_tag  # noqa: E402

REPO = "wonjun-lab/cc-swap"
BRANCH = "main"

Shell = Callable[..., "tuple[int, str]"]


class ReleaseError(Exception):
    """A check failed; the message says what to fix."""


def tag_for(version: str) -> str:
    """``0.3.1`` -> ``cc-v0.3.1``, via the same function the update check uses."""
    try:
        return release_tag(version)
    except ValueError:
        raise ReleaseError(
            f"VERSION must look like 0.3.1 (no 'v' or 'cc-v' prefix), got {version!r}."
        ) from None


def pyproject_version(path: Path) -> str:
    with open(path, "rb") as f:
        return tomllib.load(f)["project"]["version"]


_REMOTE_URL_RE = re.compile(
    rf"(?:^|[/:@]){re.escape(REPO)}(?:\.git)?/?$", re.IGNORECASE
)


def find_fork_remote(remote_v_output: str) -> str | None:
    """The name of the git remote that points at the fork, from ``git remote -v``.

    Looked up by URL because ``origin`` is upstream in a checkout cloned from
    it; pushing a tag there would be the incident all over again.
    """
    for line in remote_v_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _REMOTE_URL_RE.search(parts[1]):
            return parts[0]
    return None


def sh(*argv: str) -> tuple[int, str]:
    """Run ``argv`` in the repo root; return ``(returncode, stdout)``."""
    result = subprocess.run(
        argv, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if result.returncode != 0:
        # Surface why (a failing suite prints its report on stdout).
        shown = "\n".join(result.stdout.strip().splitlines()[-20:])
        for text in (shown, result.stderr.strip()):
            if text:
                print(text, file=sys.stderr)
    return result.returncode, result.stdout


def preflight(version: str, run: Shell, pyproject: Path) -> str:
    """Every check except the test suite. Returns the fork's remote name."""
    tag = tag_for(version)

    declared = pyproject_version(pyproject)
    if declared != version:
        raise ReleaseError(
            f"pyproject.toml says version {declared}, not {version}. Bump it "
            "(and run `uv lock`) in a commit on main first."
        )

    rc, out = run("git", "remote", "-v")
    remote = find_fork_remote(out) if rc == 0 else None
    if remote is None:
        raise ReleaseError(f"No git remote points at github.com/{REPO}; add one first.")

    rc, out = run("git", "status", "--porcelain")
    if rc != 0 or out.strip():
        raise ReleaseError("The working tree is not clean; commit or stash your changes.")

    rc, out = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or out.strip() != BRANCH:
        raise ReleaseError(f"Releases are cut from {BRANCH}; you are on {out.strip() or '?'}.")

    rc, _ = run("git", "fetch", remote, BRANCH)
    if rc != 0:
        raise ReleaseError(f"Could not fetch {remote}/{BRANCH}.")
    rc_head, head = run("git", "rev-parse", "HEAD")
    rc_remote, remote_head = run("git", "rev-parse", "FETCH_HEAD")
    if rc_head != 0 or rc_remote != 0 or head.strip() != remote_head.strip():
        raise ReleaseError(
            f"{BRANCH} is not up to date with {remote}/{BRANCH} (behind, ahead or "
            "diverged); pull or push first so the tag lands on a published commit."
        )

    rc, _ = run("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}")
    if rc == 0:
        raise ReleaseError(f"Tag {tag} already exists locally; pick a new version.")
    rc, out = run("git", "ls-remote", "--tags", remote, f"refs/tags/{tag}")
    if rc != 0:
        raise ReleaseError(f"`git ls-remote` against {remote} failed; cannot rule out {tag}.")
    if out.strip():
        raise ReleaseError(f"Tag {tag} already exists on {remote}; pick a new version.")
    return remote


def publish(version: str, remote: str, run: Shell) -> None:
    """Tag HEAD, push the tag, create the release on it."""
    tag = tag_for(version)
    rc, _ = run("git", "tag", "-a", tag, "-m", f"cc-swap {version}", "HEAD")
    if rc != 0:
        raise ReleaseError(f"Could not create tag {tag}.")
    rc, _ = run("git", "push", remote, f"refs/tags/{tag}")
    if rc != 0:
        raise ReleaseError(
            f"Could not push tag {tag} to {remote}. The local tag remains; "
            f"delete it with `git tag -d {tag}` before retrying."
        )
    rc, out = run(
        "gh", "release", "create", tag,
        "--verify-tag", "--repo", REPO,
        "--title", f"cc-swap {version}", "--generate-notes",
    )  # fmt: skip
    if rc != 0:
        raise ReleaseError(
            f"Tag {tag} is pushed but `gh release create` failed; re-run "
            f"`gh release create {tag} --verify-tag --repo {REPO}` yourself."
        )
    print(out.strip())


def main(argv: Sequence[str] | None = None, run: Shell = sh) -> int:
    parser = argparse.ArgumentParser(description="Publish a cc-swap release (tag cc-vVERSION).")
    parser.add_argument("version", metavar="VERSION", help="e.g. 0.3.1")
    parser.add_argument("--dry-run", action="store_true", help="check everything, change nothing")
    args = parser.parse_args(argv)
    try:
        remote = preflight(args.version, run, ROOT / "pyproject.toml")
        print("Running the test suite ...")
        if run("uv", "run", "pytest", "-q")[0] != 0:
            raise ReleaseError("The test suite fails; fix it before releasing.")
        if args.dry_run:
            print(f"Dry run: all checks pass; would release {tag_for(args.version)} from {remote}.")
            return 0
        publish(args.version, remote, run)
    except ReleaseError as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
