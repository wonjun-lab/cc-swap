"""Fleet modals: Prime now, Re-login (guided), and a one-line text input.

Blocking work runs in thread workers; the modals only lay out the steps and
the results. Results name accounts by slot number.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, RichLog, SelectionList, Static
from textual.widgets.selection_list import Selection

from claude_swap.maximize import fleet as fx
from claude_swap.maximize import pause
from claude_swap.maximize.prime_cli import manual_prime
from claude_swap.tui.data import ActionResult
from claude_swap.tui.theme import Palette


def prime_lines(switcher, numbers: set[str]) -> list[str]:
    """Run one manual priming pass (blocking) and return its report lines."""
    engine_events: list = []
    report = manual_prime(switcher, numbers, dry_run=False, emit=engine_events.append)
    return [event.human() for event in engine_events] + report.lines()


@dataclass(frozen=True)
class PrimeChoice:
    number: str
    label: str
    eligible: bool


class PrimeModal(ModalScreen[None]):
    """Pick accounts and prime them now. Accounts the primer would skip are
    listed with the reason and cannot be picked."""

    DEFAULT_CSS = """
    PrimeModal { align: center middle; background: $background 60%; }
    PrimeModal .fx-modal { width: 96; max-width: 95%; }
    PrimeModal #fx-prime-list { height: auto; max-height: 12; background: $surface; border: none; }
    PrimeModal #fx-prime-log { height: 8; background: $surface; margin-top: 1; }
    """
    BINDINGS = [
        Binding("enter", "run", "Prime", priority=True, show=False),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(
        self,
        choices: list[PrimeChoice],
        preselect: set[str],
        runner: Callable[[set[str]], list[str]],
    ) -> None:
        super().__init__()
        self._choices = choices
        self._preselect = preselect
        self._runner = runner
        self._priming = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide fx-modal"):
            yield Label("Prime now", classes="modal-title")
            yield SelectionList[str](
                *(
                    Selection(
                        c.label, c.number,
                        c.eligible and c.number in self._preselect,
                        disabled=not c.eligible,
                    )
                    for c in self._choices
                ),
                id="fx-prime-list",
            )
            yield RichLog(id="fx-prime-log", wrap=True, markup=False, highlight=False)
            yield Static(
                "space pick · enter prime the picked accounts · esc close",
                classes="modal-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#fx-prime-list", SelectionList).focus()

    def action_run(self) -> None:
        if self._priming:
            return
        numbers = set(self.query_one("#fx-prime-list", SelectionList).selected)
        log = self.query_one("#fx-prime-log", RichLog)
        if not numbers:
            log.write("Pick an account first (space).")
            return
        self._priming = True
        order = sorted(numbers, key=lambda n: (len(n), n))
        log.write(
            f"priming {', '.join('#' + n for n in order)} — a launch is verified "
            "about 35 s later…"
        )
        self.run_worker(
            partial(self._run_blocking, numbers), thread=True, group="fleet-prime",
            exit_on_error=False, name="fleet-prime",
        )

    def _run_blocking(self, numbers: set[str]) -> None:
        try:
            lines = self._runner(numbers)
        except Exception as e:  # report, never crash the UI
            lines = [f"prime failed: {type(e).__name__}: {e}"]
        self.app.call_from_thread(self._done, lines)

    def _done(self, lines: list[str]) -> None:
        self._priming = False
        if not self.is_attached:
            return
        log = self.query_one("#fx-prime-log", RichLog)
        for line in lines:
            log.write(line)
        self.app.request_refresh()

    def action_close(self) -> None:
        self.dismiss(None)


class ReloginModal(ModalScreen["ActionResult | None"]):
    """Guided re-login. cc-swap launches nothing: the steps say what to run;
    enter then stores the live login (identity-checked) and switches back.
    The engine is paused (``maximize/pause.py``) while this is open."""

    DEFAULT_CSS = """
    ReloginModal { align: center middle; background: $background 60%; }
    ReloginModal .fx-modal { width: 100; max-width: 95%; }
    ReloginModal #fx-relogin-steps { margin-bottom: 1; }
    """
    BINDINGS = [
        Binding("enter", "store", "Store", priority=True, show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        lines: list[str],
        *,
        backup_root: Path,
        store: Callable[[], ActionResult],
    ) -> None:
        super().__init__()
        self._lines = lines
        self._root = backup_root
        self._store = store
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide fx-modal"):
            yield Label("Re-login", classes="modal-title")
            yield Static("\n".join(self._lines), id="fx-relogin-steps", markup=False)
            yield Static("", id="fx-relogin-status", markup=False)
            yield Static(
                "enter I logged in — store it · esc cancel",
                classes="modal-hint",
            )

    def on_mount(self) -> None:
        self.run_worker(
            self._pause_blocking, thread=True, group="fleet-pause",
            exit_on_error=False, name="fleet-pause",
        )

    def _pause_blocking(self) -> None:
        try:
            until = pause.pause(self._root, "relogin", now=time.time())
            note = f"switching paused until {fx.hhmm(until)}"
        except Exception as e:  # the guidance still works; say the pause did not
            note = f"could not pause the engine ({type(e).__name__}); it may react to the login"
        self.app.call_from_thread(self._status, note)

    def _status(self, note: str) -> None:
        if self.is_attached:
            palette = Palette.from_theme(self.app.current_theme)
            self.query_one("#fx-relogin-status", Static).update(Text(note, style=palette.muted))

    def action_store(self) -> None:
        if self._busy:
            return
        if self.app.busy:
            self.notify("Another action is still running", severity="warning")
            return
        self._busy = True
        self.app.busy = True
        self._status("checking the live login…")
        self.run_worker(
            self._store_blocking, thread=True, group="fleet-relogin",
            exit_on_error=False, name="fleet-relogin",
        )

    def _store_blocking(self) -> None:
        try:
            result = self._store()
        except Exception as e:
            result = ActionResult(False, f"Error: {type(e).__name__}: {e}")
        finally:
            _resume(self._root)
        self.app.call_from_thread(self._stored, result)

    def _stored(self, result: ActionResult) -> None:
        self.app.busy = False
        self.dismiss(result)

    def action_cancel(self) -> None:
        if self._busy:
            return
        self.app.run_worker(
            partial(_resume, self._root), thread=True, group="fleet-pause",
            exit_on_error=False, name="fleet-resume",
        )
        self.dismiss(None)


def _resume(root: Path) -> None:
    try:
        pause.resume(root)
    except Exception:
        pass  # the marker expires on its own within 10 minutes


class TextInputModal(ModalScreen["str | None"]):
    """One line of text (alias, a typed setting value). Enter submits, Esc
    cancels."""

    DEFAULT_CSS = """
    TextInputModal { align: center middle; background: $background 60%; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, prompt: str, value: str = "") -> None:
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide fx-modal"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._prompt, classes="modal-body", markup=False)
            yield Input(value=self._value, id="fx-text-input")
            yield Static("enter save · esc cancel", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#fx-text-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)
