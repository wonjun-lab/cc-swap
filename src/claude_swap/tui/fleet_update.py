"""Fleet's *Update Claude Code* modal: ``cc-swap claude-update`` in the TUI.

Main menu ``u``. Opening it runs the read-only check (``claude-update
--check``: ``claude --version`` and the npm dist-tags) in a thread worker;
nothing is installed until ``y`` confirms. Then ``claude update`` runs in a
worker with its output streamed into the modal, followed by the same
before -> after line, error and ``cc-swap prime verify`` hint the CLI
prints. Both steps go through ``maximize/claude_update.py`` (the CLI's own
functions), so the version record and the priming pause are the same.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, RichLog, Static

from claude_swap.maximize import claude_update as cu


def run_check(root: Path) -> cu.CheckResult:
    """The version check (tests patch this)."""
    return cu.check_versions(root)


def run_update(root: Path, sink) -> cu.UpdateResult:
    """``claude update`` with its output sent to ``sink`` (tests patch this)."""
    return cu.perform_update(root, timeout=cu.DEFAULT_UPDATE_TIMEOUT, sink=sink)


class _LogSink:
    """A file-like ``sink`` for :func:`run_update`: each line goes to the
    modal's log on the UI thread."""

    def __init__(self, write_line: Callable[[str], None]) -> None:
        self._write_line = write_line
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._write_line(line)
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self._buffer:
            self._write_line(self._buffer)
            self._buffer = ""


HINT_CONFIRM = "y update now · esc close"
HINT_CHECKING = "checking… · esc close"
HINT_UPDATING = "updating… (esc closes once it is done)"
HINT_DONE = "r check again · esc close"


class ClaudeUpdateModal(ModalScreen[None]):
    DEFAULT_CSS = """
    ClaudeUpdateModal { align: center middle; background: $background 60%; }
    ClaudeUpdateModal .fx-modal { width: 110; max-width: 95%; }
    ClaudeUpdateModal #fx-update-log { height: 20; background: $surface; }
    """
    BINDINGS = [
        Binding("y", "update", "Update", show=False),
        Binding("r", "check", "Check again", show=False),
        Binding("escape,b,q", "close", "Close", show=False),
    ]

    def __init__(
        self,
        root: Path,
        *,
        check: Callable[[Path], cu.CheckResult] | None = None,
        update: Callable[[Path, object], cu.UpdateResult] | None = None,
    ) -> None:
        super().__init__()
        self._root = Path(root)
        self._check = check or run_check
        self._update = update or run_update
        self._busy = False
        self._updating = False
        self.checked: cu.CheckResult | None = None
        self.result: cu.UpdateResult | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide fx-modal"):
            yield Label("Update Claude Code", classes="modal-title")
            yield RichLog(id="fx-update-log", wrap=True, markup=False, highlight=False)
            yield Static(HINT_CHECKING, id="fx-update-hint", classes="modal-hint", markup=False)

    def on_mount(self) -> None:
        self.action_check()

    # -- helpers ------------------------------------------------------------------------

    def _log(self) -> RichLog:
        return self.query_one("#fx-update-log", RichLog)

    def _hint(self, text: str) -> None:
        self.query_one("#fx-update-hint", Static).update(text)

    def _write(self, line: str | Text) -> None:
        if self.is_attached:
            self._log().write(line)

    def log_text(self) -> str:
        return "\n".join(strip.text for strip in self._log().lines)

    @property
    def can_update(self) -> bool:
        return not self._busy and self.checked is not None and self.checked.available

    # -- check ------------------------------------------------------------------------------

    def action_check(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.checked = None
        self._log().clear()
        self._write("checking the installed Claude Code and the latest release…")
        self._hint(HINT_CHECKING)
        self.run_worker(
            self._check_blocking, thread=True, group="fleet-claude-update",
            exit_on_error=False, name="fleet-claude-check",
        )

    def _check_blocking(self) -> None:
        try:
            checked = self._check(self._root)
        except Exception as e:  # report, never crash the UI
            checked = cu.CheckResult(error=f"check failed ({type(e).__name__}: {e})")
        self.app.call_from_thread(self._checked, checked)

    def _checked(self, c: cu.CheckResult) -> None:
        self._busy = False
        self.checked = c
        if not self.is_attached:
            return
        self._log().clear()
        if c.claude:
            self._write(f"claude:    {c.claude}")
        if c.installed:
            self._write(f"installed: {c.installed}")
        if c.latest:
            self._write(f"latest:    {c.latest} ({c.channel})")
        if c.error:
            self._write(Text(f"Error: {c.error}", style="bold red"))
            self._hint(HINT_DONE)
        elif c.available:
            self._write(Text(
                f"Update {c.installed} -> {c.latest}? This runs `claude update` "
                "(Claude Code's own updater).",
                style="bold",
            ))
            self._hint(HINT_CONFIRM)
        else:
            self._write(f"Claude Code {c.installed} is up to date.")
            self._hint(HINT_DONE)

    # -- update -----------------------------------------------------------------------------

    def action_update(self) -> None:
        if not self.can_update:
            return
        self._busy = self._updating = True
        self._write("")
        self._write("running claude update…")
        self._hint(HINT_UPDATING)
        self.run_worker(
            self._update_blocking, thread=True, group="fleet-claude-update",
            exit_on_error=False, name="fleet-claude-update",
        )

    def _update_blocking(self) -> None:
        sink = _LogSink(lambda line: self.app.call_from_thread(self._write, line))
        try:
            result = self._update(self._root, sink)
        except Exception as e:  # report, never crash the UI
            result = cu.UpdateResult(error=f"update failed ({type(e).__name__}: {e})")
        sink.close()
        self.app.call_from_thread(self._updated, result)

    def _updated(self, result: cu.UpdateResult) -> None:
        self._busy = self._updating = False
        self.result = result
        self.checked = None  # a second `y` needs a fresh check
        if not self.is_attached:
            return
        if result.error:
            self._write(Text(f"Error: {result.error}", style="bold red"))
        if result.run is not None:
            self._write(Text(cu.summary_line(result), style="bold"))
        hint = result.prime_hint
        if hint:
            self._write(Text(hint, style="yellow"))
        self._hint(HINT_DONE)

    def action_close(self) -> None:
        if self._updating:  # never leave a running `claude update` unseen
            return
        self.dismiss(None)
