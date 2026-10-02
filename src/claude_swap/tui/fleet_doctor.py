"""Fleet's *Inspect all logins* modal: ``cc-swap doctor`` inside the TUI.

Account settings → ``i`` runs the same read-only checks as ``cc-swap doctor``
(``maximize/doctor.py``) in a thread worker and lists the findings — slot
numbers and fingerprint prefixes only — with one fix line each. Nothing is
refreshed or written; ``r`` reruns, ``esc`` closes.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, RichLog, Static

from claude_swap.maximize import doctor as dr


def run_doctor() -> list[dr.Finding]:
    """The checks against this machine (tests patch this)."""
    return dr.run_checks()


_STYLES = {"error": "bold red", "warn": "yellow", "info": "dim", "ok": ""}


def finding_lines(findings: list[dr.Finding]) -> list[Text]:
    """Problems first (errors, warnings), then info, then ok; then a summary."""
    order = {"error": 0, "warn": 1, "info": 2, "ok": 3}
    out: list[Text] = []
    for f in sorted(findings, key=lambda f: order[f.severity]):
        where = f.check if f.scope in ("env", "accounts") else f"{f.scope} {f.check}"
        tag = {"error": "ERROR", "warn": "WARN", "info": "info", "ok": "ok"}[f.severity]
        line = Text(f"{tag:<5}  ", style=_STYLES[f.severity])
        line.append(f"{where}: {f.detail}")
        out.append(line)
        if f.fix and f.severity != "ok":
            out.append(Text(f"       fix: {f.fix}", style="dim"))
    n = dr.counts(findings)
    out.append(Text(
        f"{n['error']} error(s) · {n['warn']} warning(s) · same as cc-swap doctor",
        style="bold",
    ))
    return out


class DoctorModal(ModalScreen[None]):
    DEFAULT_CSS = """
    DoctorModal { align: center middle; background: $background 60%; }
    DoctorModal .fx-modal { width: 110; max-width: 95%; }
    DoctorModal #fx-doctor-log { height: 24; background: $surface; }
    """
    BINDINGS = [
        Binding("r", "rerun", "Run again", show=False),
        Binding("escape,b,q", "close", "Close", show=False),
    ]

    def __init__(self, runner: Callable[[], list[dr.Finding]] | None = None) -> None:
        super().__init__()
        self._runner = runner or run_doctor
        self._busy = False
        self.findings: list[dr.Finding] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide fx-modal"):
            yield Label("Inspect all logins (doctor)", classes="modal-title")
            yield RichLog(id="fx-doctor-log", wrap=True, markup=False, highlight=False)
            yield Static(
                "read-only: no refresh, no writes · r run again · esc close",
                classes="modal-hint",
            )

    def on_mount(self) -> None:
        self.action_rerun()

    def action_rerun(self) -> None:
        if self._busy:
            return
        self._busy = True
        log = self.query_one("#fx-doctor-log", RichLog)
        log.clear()
        log.write("checking the Keychain, the live login, the service and every slot…")
        self.run_worker(
            self._run_blocking, thread=True, group="fleet-doctor",
            exit_on_error=False, name="fleet-doctor",
        )

    def _run_blocking(self) -> None:
        try:
            findings = self._runner()
        except Exception as e:  # report, never crash the UI
            findings = [dr.Finding("doctor", "error", f"doctor failed ({type(e).__name__})")]
        self.app.call_from_thread(self._done, findings)

    def _done(self, findings: list[dr.Finding]) -> None:
        self._busy = False
        self.findings = findings
        if not self.is_attached:
            return
        log = self.query_one("#fx-doctor-log", RichLog)
        log.clear()
        for line in finding_lines(findings):
            log.write(line)

    def action_close(self) -> None:
        self.dismiss(None)
