"""Fleet modals: the menu, Prime now, Re-login (guided), Mode, and a
one-line text input.

Blocking work runs in thread workers; the modals only lay out the steps and
the results. Results name accounts by slot number.
"""

from __future__ import annotations

import threading
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
from textual.widgets import Input, Label, ListItem, ListView, RichLog, SelectionList, Static
from textual.widgets.selection_list import Selection

from claude_swap.maximize import fleet as fx
from claude_swap.maximize import pause
from claude_swap.maximize.fleet_actions import NO_NEW_LOGIN
from claude_swap.maximize.prime_cli import manual_prime
from claude_swap.tui.data import ActionResult
from claude_swap.tui.menus import MENU_KEYS, MenuRow
from claude_swap.tui.theme import Palette

# How often an open re-login modal renews its engine pause.
RENEW_S = 60.0
_now = time.time  # the pause clock (tests move it)


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
        store: Callable[[str | None], ActionResult],
        fingerprint: Callable[[], str | None] | None = None,
        prepare: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        """``store(before)`` stores the live login; ``before`` is the live
        login's fingerprint when this opened (None = unknown). ``fingerprint``
        reads that fingerprint (a hash, never a token). ``prepare`` runs
        first, off the UI thread — backing up the active account's current
        login — and ``(False, reason)`` refuses to start the re-login."""
        super().__init__()
        self._prepare = prepare
        self._preparing = prepare is not None
        self._refused: str | None = None
        self._started = False
        self._lines = lines
        self._root = backup_root
        self._store = store
        self._fingerprint = fingerprint
        self._baseline: str | None = None
        self._baseline_ready = threading.Event()
        self._busy = False
        self._lifted = False  # the pause was lifted (cancel, store, unmount)

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
        if self._prepare is None:
            self._start()
            return
        self.query_one("#fx-relogin-steps", Static).update(
            "Backing up the active account's current login first…"
        )
        self.run_worker(
            self._prepare_blocking, thread=True, group="fleet-relogin-prepare",
            exit_on_error=False, name="fleet-relogin-prepare",
        )

    def _prepare_blocking(self) -> None:
        try:
            ok, reason = self._prepare()
        except Exception as e:
            ok, reason = False, f"{type(e).__name__}: {e}"
        self.app.call_from_thread(self._prepared, ok, reason)

    def _prepared(self, ok: bool, reason: str) -> None:
        self._preparing = False
        if not self.is_attached:
            return
        if not ok:
            self._refused = reason
            palette = Palette.from_theme(self.app.current_theme)
            self.query_one("#fx-relogin-steps", Static).update(
                Text(f"Not starting the re-login: {reason}", style=palette.sev_crit)
            )
            self._status("nothing was changed · esc close")
            return
        self.query_one("#fx-relogin-steps", Static).update("\n".join(self._lines))
        self._start()

    def _start(self) -> None:
        self._started = True
        self._run_pause_op(self._pause_blocking, "fleet-pause")
        # Renew while the modal stays open: the marker never outlives the
        # last renewal by more than MAX_PAUSE_S, so a crash still lifts it.
        self._renew_timer = self.set_interval(RENEW_S, self._renew)
        self.run_worker(
            self._baseline_blocking, thread=True, group="fleet-relogin-baseline",
            exit_on_error=False, name="fleet-relogin-baseline",
        )

    def _run_pause_op(self, work: Callable[[], None], name: str) -> None:
        """Pause, renewal and resume share one exclusive group on the app
        (a resume survives this modal's unmount and supersedes a pause that
        has not started); ``_still_paused`` settles the ones already running."""
        self.app.run_worker(
            work, thread=True, group="fleet-pause", exclusive=True,
            exit_on_error=False, name=name,
        )

    def _still_paused(self) -> bool:
        return not self._lifted

    def _renew(self) -> None:
        if not self._lifted:
            self._run_pause_op(self._pause_blocking, "fleet-pause-renew")

    def _pause_blocking(self) -> None:
        try:
            until = pause.pause(
                self._root, "relogin", now=_now(), wanted=self._still_paused
            )
            if until is None:
                return  # lifted meanwhile: nothing written
            note = f"switching paused until {fx.hhmm(until)}"
        except Exception as e:  # the guidance still works; say the pause did not
            note = f"could not pause the engine ({type(e).__name__}); it may react to the login"
        if not self._busy:
            self.app.call_from_thread(self._status, note)

    def _lift(self, *, wait: bool) -> None:
        """Resume switching, once. ``wait`` resumes on this thread (the app
        is going away and a worker might never run)."""
        if self._lifted:
            return
        self._lifted = True
        if not self._started:
            return  # never paused: nothing of ours to lift
        timer = getattr(self, "_renew_timer", None)
        if timer is not None:
            timer.stop()
        if wait:
            _resume(self._root)
        else:
            self._run_pause_op(partial(_resume, self._root), "fleet-resume")

    def on_unmount(self) -> None:
        # Quit (or anything else) while the modal is open: lift the pause now
        # instead of leaving the engine paused for up to 10 minutes.
        self._lift(wait=True)

    def _status(self, note: str) -> None:
        if self.is_attached:
            palette = Palette.from_theme(self.app.current_theme)
            self.query_one("#fx-relogin-status", Static).update(Text(note, style=palette.muted))

    def action_store(self) -> None:
        if self._busy or self._preparing or self._refused is not None:
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

    def _baseline_blocking(self) -> None:
        """The live login's fingerprint as the modal opens: enter must find
        a different one (a new ``/login``), not the login already there."""
        try:
            self._baseline = self._fingerprint() if self._fingerprint else None
        except Exception:
            self._baseline = None
        finally:
            self._baseline_ready.set()

    def _store_blocking(self) -> None:
        self._baseline_ready.wait(timeout=10.0)
        before = self._baseline if self._baseline_ready.is_set() else None
        if before is not None and self._fingerprint is not None:
            try:
                unchanged = self._fingerprint() == before
            except Exception:
                unchanged = False
            if unchanged:
                self.app.call_from_thread(self._not_yet)
                return
        try:
            result = self._store(before)
        except Exception as e:
            result = ActionResult(False, f"Error: {type(e).__name__}: {e}")
        finally:
            self._lifted = True  # no renewal from here on
            _resume(self._root)
        self.app.call_from_thread(self._stored, result)

    def _not_yet(self) -> None:
        """Same login as when this opened: stay open (still paused)."""
        self._busy = False
        self.app.busy = False
        self._status(NO_NEW_LOGIN)

    def _stored(self, result: ActionResult) -> None:
        self.app.busy = False
        timer = getattr(self, "_renew_timer", None)
        if timer is not None:
            timer.stop()
        self.dismiss(result)

    def action_cancel(self) -> None:
        if self._busy:
            return
        self._lift(wait=False)
        self.dismiss(None)


def _resume(root: Path) -> None:
    try:
        pause.resume(root)
    except Exception:
        pass  # the marker expires on its own within 10 minutes


class ModeModal(ModalScreen["str | None"]):
    """Who switches accounts on this machine, and the choices this TUI has:
    run an engine here (dry-run or live), go live / back, stop it. A viewer
    gets the facts only. Dismisses with a ``ModeAction.action`` or None."""

    DEFAULT_CSS = """
    ModeModal { align: center middle; background: $background 60%; }
    ModeModal #fx-mode-actions { margin-top: 1; }
    """
    BINDINGS = [Binding("escape,b", "close", "Close", show=False)]

    def __init__(self, status: fx.EngineStatus) -> None:
        super().__init__()
        self._status = status
        self._actions = fx.mode_transitions(status.holder, auto_off=status.auto_off)

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide fx-modal"):
            yield Label("Mode", classes="modal-title")
            yield Static("\n".join(fx.mode_facts(self._status)), id="fx-mode-facts", markup=False)
            yield Static("", id="fx-mode-actions", markup=False)
            keys = " · ".join(a.key for a in self._actions)
            yield Static(f"{keys + ' · ' if keys else ''}esc close", classes="modal-hint")

    def on_mount(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        for i, action in enumerate(self._actions):
            if i:
                text.append("\n")
            text.append(f"  {action.key}  ", style=f"bold {palette.accent}")
            text.append(action.label, style=palette.foreground)
        self.query_one("#fx-mode-actions", Static).update(text)

    def on_key(self, event) -> None:
        for action in self._actions:
            if event.key == action.key:
                event.stop()
                self.dismiss(action.action)
                return

    def action_close(self) -> None:
        self.dismiss(None)


class MenuItem(ListItem):
    def __init__(self, row: MenuRow, text: Text) -> None:
        super().__init__(Static(text, markup=False))
        self.row = row


class MenuModal(ModalScreen["str | None"]):
    """The home screen's ``m`` menu: every item with its key, its state and
    a short note. A letter picks its item at once; ↑↓ and enter work too.
    Dismisses with the item's action, or None (esc / b)."""

    DEFAULT_CSS = """
    MenuModal { align: center middle; background: $background 60%; }
    MenuModal #fx-menu-box {
        width: 100; max-width: 95%; height: auto; max-height: 95%;
        background: $surface; border: round $primary; padding: 1 2;
    }
    MenuModal #fx-menu-list { height: auto; background: transparent; }
    MenuModal #fx-menu-list:focus { background-tint: $foreground 0%; }
    MenuModal #fx-menu-list ListItem {
        background: transparent; padding: 0 1 0 0; border-left: thick $surface;
    }
    MenuModal #fx-menu-list ListItem.-highlight,
    MenuModal #fx-menu-list:focus ListItem.-highlight {
        background: $panel; color: $foreground; text-style: none;
        border-left: thick $primary;
    }
    MenuModal #fx-menu-list Static { text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS = [Binding("escape,b", "close", "Close", show=False)]

    def __init__(self, rows: list[MenuRow]) -> None:
        super().__init__()
        self._rows = rows
        self._by_key = {r.key: r.action for r in rows}

    def compose(self) -> ComposeResult:
        palette = Palette.DARK
        try:
            palette = Palette.from_theme(self.app.current_theme)
        except Exception:
            pass
        title_w = min(max((len(r.title) for r in self._rows), default=0) + 3, 40)
        with Vertical(id="fx-menu-box"):
            yield Label("Menu", classes="modal-title")
            yield ListView(
                *(MenuItem(r, menu_row_text(r, title_w, palette)) for r in self._rows),
                id="fx-menu-list",
            )
            yield Static(MENU_KEYS, classes="modal-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#fx-menu-list", ListView).focus()

    def on_key(self, event) -> None:
        action = self._by_key.get(event.character or "")
        if action is not None and event.key not in ("enter", "escape"):
            event.stop()
            self.dismiss(action)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, MenuItem):
            event.stop()
            self.dismiss(event.item.row.action)

    def action_close(self) -> None:
        self.dismiss(None)


def menu_row_text(row: MenuRow, title_w: int, palette: Palette) -> Text:
    """`` s  Swap strategy…         soft/hard 5h 50/95 · 7d 90/98 · priming``."""
    from claude_swap.tui.fleet_render import tone_style

    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f" {row.key}  ", style=f"bold {palette.accent}")
    title_style = palette.foreground if row.tone == "plain" else tone_style(row.tone, palette)
    text.append(row.title.ljust(title_w) if row.note else row.title, style=title_style)
    if row.note:
        text.append(row.note, style=palette.muted)
    return text


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
