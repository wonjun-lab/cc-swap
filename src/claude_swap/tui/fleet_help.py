"""Fleet help: every key, plus a legend for each column (``?`` / ``h``)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from claude_swap.tui import menus
from claude_swap.tui.theme import Palette


def help_text(palette: Palette) -> Text:
    text = Text()
    for i, (key, what) in enumerate(menus.help_entries()):
        if i:
            text.append("\n")
        if not key:
            text.append(what, style=f"bold {palette.foreground}")
            continue
        text.append(f"  {key:<12}", style=f"bold {palette.accent}")
        text.append(what, style=palette.foreground)
    text.append("\n\nb / esc / ← back · q quit", style=palette.muted)
    return text


class HelpScreen(Screen):
    CSS_PATH = "fleet.tcss"
    BINDINGS = [
        Binding("b,escape,left", "back", "Back", show=False),
        Binding("q", "quit", "Quit", show=False),
        Binding("j,down", "scroll_down", show=False),
        Binding("k,up", "scroll_up", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="fx-help-scroll"):
            yield Static("", id="fx-help", markup=False)

    def on_mount(self) -> None:
        self.query_one("#fx-help", Static).update(
            help_text(Palette.from_theme(self.app.current_theme))
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        from claude_swap.tui.fleet import request_quit

        request_quit(self.app)

    def action_scroll_down(self) -> None:
        self.query_one("#fx-help-scroll", VerticalScroll).scroll_down()

    def action_scroll_up(self) -> None:
        self.query_one("#fx-help-scroll", VerticalScroll).scroll_up()
