"""cc-swap fork identity: distribution name, console scripts, project URLs.

Reads the installed (editable) distribution's metadata, the same source
``claude_swap.__version__`` reads, so a pyproject edit that forgets
``uv sync`` or the ``__init__`` lookup fails here first.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from importlib.metadata import PackageNotFoundError, distribution, version

import claude_swap

FORK_URL = "https://github.com/wonjun-lab/cc-swap"


def _console_scripts() -> dict[str, str]:
    dist = distribution("cc-swap")
    return {
        ep.name: ep.value
        for ep in dist.entry_points
        if ep.group == "console_scripts"
    }


def _project_urls() -> dict[str, str]:
    entries = distribution("cc-swap").metadata.get_all("Project-URL") or []
    return dict(entry.split(", ", 1) for entry in entries)


def test_distribution_is_named_cc_swap():
    assert distribution("cc-swap").metadata["Name"] == "cc-swap"


def test_version_comes_from_the_cc_swap_distribution():
    assert claude_swap.__version__ == version("cc-swap")


def test_import_survives_missing_distribution_metadata(monkeypatch):
    """Running from a source tree nobody installed (PYTHONPATH=src, a vendored
    copy, a stale uninstall) has no cc-swap metadata: `import claude_swap`
    must not die on PackageNotFoundError before any command can run."""

    def not_installed(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_installed)
    try:
        reloaded = importlib.reload(claude_swap)
        assert reloaded.__version__ == "0+unknown"
        # The rest of the package surface is intact.
        assert reloaded.ClaudeAccountSwitcher is not None
    finally:
        monkeypatch.undo()
        importlib.reload(claude_swap)
    assert claude_swap.__version__ == version("cc-swap")


def test_console_scripts_are_cc_swap_and_transitional_cswap():
    # `cswap` stays until spec §11 stage 6 so upstream's `cswap ...` hints
    # keep working; `claude-swap` is gone so it can't shadow an upstream install.
    assert _console_scripts() == {
        "cc-swap": "claude_swap.cli:main",
        "cswap": "claude_swap.cli:main",
    }


def test_project_urls_point_at_the_fork():
    urls = _project_urls()
    assert urls["Homepage"] == FORK_URL
    assert urls["Repository"] == FORK_URL
    assert urls["Issues"] == f"{FORK_URL}/issues"
    assert urls["Upstream"] == "https://github.com/realiti4/claude-swap"
