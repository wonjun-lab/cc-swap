"""Multi-account switcher for Claude Code."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cc-swap")
except PackageNotFoundError:
    # Imported from a tree that was never installed (PYTHONPATH=src, a vendored
    # copy): no distribution metadata to read. Importing must still work.
    __version__ = "0+unknown"

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
