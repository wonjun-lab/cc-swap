"""Fleet help (``?``): how to read the home screen, what each tag and word
means (soft/hard, next, last resort, pace, priming, viewer/lease, waiting
out a reset, preempt, quiet time, holding, the capacity summary), what the
engine has learned of your busy and quiet times, and every key. A popup
over the home screen; it scrolls when short."""

from __future__ import annotations

import textwrap

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from claude_swap.tui import menus
from claude_swap.tui.theme import Palette

TERM_WIDTH = 20  # the longest term ("rebalance deferred") plus a gap
INDENT = 2 + TERM_WIDTH


def help_text(
    palette: Palette, width: int | None = None, idle_pattern: str | None = None
) -> Text:
    """Every help entry, the explanation wrapped to ``width`` with its
    continuation lines under the explanation (not under the term).
    ``idle_pattern`` (``view.idle_pattern_text``) adds what has been learned."""
    wrap = max(width - INDENT - 1, 20) if width else None  # 1: a scrollbar may appear
    text = Text()
    for i, (term, what) in enumerate(menus.help_entries(idle_pattern)):
        if i:
            text.append("\n")
        if not term:
            text.append(what, style=f"bold {palette.foreground}")
            continue
        text.append(f"  {term:<{TERM_WIDTH}}", style=f"bold {palette.accent}")
        lines = textwrap.wrap(what, wrap) if wrap else [what]
        text.append(f"\n{' ' * INDENT}".join(lines), style=palette.foreground)
    text.append("\n\nesc / b / ? close · ↑↓ scroll · q quit", style=palette.muted)
    return text


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; background: $background 60%; }
    HelpScreen #fx-help-scroll {
        width: 110; max-width: 95%; height: auto; max-height: 95%;
        background: $surface; border: round $panel; padding: 1 2;
    }
    HelpScreen #fx-help { height: auto; }
    """
    BINDINGS = [
        Binding("b,escape,left,question_mark", "back", "Back", show=False),
        Binding("q", "quit", "Quit", show=False),
        Binding("j,down", "scroll_down", show=False),
        Binding("k,up", "scroll_up", show=False),
    ]

    def __init__(self, idle_pattern: str | None = None) -> None:
        super().__init__()
        self._idle_pattern = idle_pattern

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="fx-help-scroll"):
            yield Static("", id="fx-help", markup=False)

    def on_mount(self) -> None:
        self._fill()
        self.call_after_refresh(self._fill)

    def on_resize(self) -> None:
        self.call_after_refresh(self._fill)

    def _fill(self) -> None:
        if not self.is_attached:
            return
        widget = self.query_one("#fx-help", Static)
        width = widget.content_region.width or None
        widget.update(help_text(
            Palette.from_theme(self.app.current_theme), width, self._idle_pattern
        ))

    def action_back(self) -> None:
        self.dismiss(None)

    def action_quit(self) -> None:
        from claude_swap.tui.fleet import request_quit

        self.dismiss(None)
        request_quit(self.app)

    def action_scroll_down(self) -> None:
        self.query_one("#fx-help-scroll", VerticalScroll).scroll_down()

    def action_scroll_up(self) -> None:
        self.query_one("#fx-help-scroll", VerticalScroll).scroll_up()
