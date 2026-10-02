"""Swap strategy: every ``maximize.*`` threshold and the ``prime.*`` knobs,
with a live preview of the decision the fleet would get under the edits.

←/→ step a field (soft never passes hard), ``e`` types a value, ``s``
saves through ``set_setting`` (thresholds in a valid order), ``b``/esc with
unsaved edits asks once. The running engine — the service, or one here —
re-reads settings.json on its next tick, so a viewer may save too.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.maximize import fleet as fx
from claude_swap.maximize import view as mxview
from claude_swap.settings import (
    SETTING_SPECS,
    load_maximize_settings,
    load_prime_settings,
    parse_setting_value,
    set_setting,
    settings_path,
)
from claude_swap.tui import menus
from claude_swap.tui.fleet_modals import TextInputModal
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp


def _value_text(field: menus.StrategyField, value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if field.key == "prime.jitterS":
        return f"{value} {field.unit}".strip()
    if isinstance(value, float):
        text = f"{value:.2f}" if field.key == "maximize.tieEpsilon" else f"{value:g}"
    else:
        text = "" if value is None else str(value)
    return f"{text} {field.unit}".rstrip()


class StrategyScreen(Screen):
    CSS_PATH = "fleet.tcss"
    BINDINGS = [
        Binding("up,k", "move(-1)", show=False),
        Binding("down,j", "move(1)", show=False),
        Binding("left", "step(-1)", show=False),
        Binding("right", "step(1)", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("s", "save", "Save", show=False),
        Binding("b,escape", "back", "Back", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._saved: dict[str, object] = {}
        self._edited: dict[str, object] = {}
        self._cursor = 0
        self._discard_armed = False

    def compose(self) -> ComposeResult:
        yield Static("", id="fx-st-head", markup=False)
        yield Static("", id="fx-st-body", markup=False)
        yield Static("", id="fx-st-preview", markup=False)
        yield Static("", id="fx-st-keys", markup=False)

    def on_mount(self) -> None:
        self._reload()
        self.watch(self.app, "snapshot", lambda _s: self._render_preview())

    @property
    def _root(self):
        return self.app.switcher.backup_dir

    def _reload(self) -> None:
        values = fx.strategy_values(
            load_maximize_settings(self._root), load_prime_settings(self._root)
        )
        self._saved = dict(values)
        self._edited = dict(values)
        self._discard_armed = False
        self._redraw()

    @property
    def dirty(self) -> bool:
        return self._edited != self._saved

    # -- rendering -----------------------------------------------------------------

    def _redraw(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        head = Text("cc-swap · swap strategy", style=f"bold {palette.foreground}")
        head.append(f"    {settings_path(self._root)}", style=palette.muted)
        self.query_one("#fx-st-head", Static).update(head)
        body = Text()
        group = None
        for i, field in enumerate(menus.STRATEGY_FIELDS):
            if field.group != group:
                group = field.group
                if i:
                    body.append("\n")
                body.append(f"  {group}\n", style=palette.muted)
            selected = i == self._cursor
            changed = self._edited.get(field.key) != self._saved.get(field.key)
            body.append(" > " if selected else "   ", style=palette.accent)
            body.append(
                f"{field.label:<19}",
                style=f"bold {palette.foreground}" if selected else palette.foreground,
            )
            body.append(
                f"{_value_text(field, self._edited.get(field.key)):>18}",
                style=palette.accent if changed else palette.foreground,
            )
            body.append("  *" if changed else "   ", style=palette.accent)
            if field.why:
                body.append(f"  {field.why}", style=palette.muted)
            body.append("\n")
        self.query_one("#fx-st-body", Static).update(body)
        self.query_one("#fx-st-keys", Static).update(
            Text(menus.STRATEGY_KEYS, style=palette.muted)
        )
        self._render_preview()

    def _decision_text(self, settings) -> str | None:
        snap = self.app.snapshot
        if snap is None:
            return None
        now = time.time()
        state = mxview.read_state(self._root)
        msnap = fx.fleet_snapshot(snap, settings, state, now=now)
        line = fx.now_line(fx.preview_decision(msnap, settings), now=now)
        return line.removesuffix(" · computed here")

    def preview_text(self) -> str:
        edited = self._decision_text(fx.strategy_settings(self._edited))
        if edited is None:
            return "with these values: (waiting for usage)"
        saved = self._decision_text(fx.strategy_settings(self._saved))
        tail = "same" if saved == edited else saved
        return f"with these values: {edited}\nsaved values: {tail}"

    def _render_preview(self) -> None:
        if not self.is_attached:
            return
        palette = Palette.from_theme(self.app.current_theme)
        try:
            text = self.preview_text()
        except Exception as e:  # a display aid must never take the screen down
            text = f"preview unavailable: {type(e).__name__}"
        self.query_one("#fx-st-preview", Static).update(Text(text, style=palette.foreground))

    # -- actions -----------------------------------------------------------------------

    @property
    def _field(self) -> menus.StrategyField:
        return menus.STRATEGY_FIELDS[self._cursor]

    def action_move(self, delta: int) -> None:
        self._cursor = (self._cursor + delta) % len(menus.STRATEGY_FIELDS)
        self._redraw()

    def action_step(self, direction: int) -> None:
        field = self._field
        if not field.step:
            self.notify(f"{field.label}: press e to type a value", timeout=3)
            return
        self._edited = fx.strategy_step(self._edited, field.key, direction * field.step)
        self._discard_armed = False
        self._redraw()

    def action_edit(self) -> None:
        field = self._field
        current = fx.setting_text(self._edited.get(field.key))
        self.app.push_screen(
            TextInputModal(f"Swap strategy · {field.label}", SETTING_SPECS[field.key].help, current),
            self._on_edit,
        )

    def _on_edit(self, raw: str | None) -> None:
        if raw is None:
            return
        field = self._field
        try:
            value = parse_setting_value(SETTING_SPECS[field.key], raw)
        except ClaudeSwitchError as e:
            self.notify(str(e), severity="error")
            return
        edited = {**self._edited, field.key: value}
        s = fx.strategy_settings(edited)
        if s.soft_5h > s.hard_5h or s.soft_7d > s.hard_7d:
            self.notify("a soft mark cannot be above its hard cap", severity="error")
            return
        self._edited = edited
        self._discard_armed = False
        self._redraw()

    def action_save(self) -> None:
        writes = fx.strategy_writes(self._saved, self._edited)
        if not writes:
            self.notify("Nothing to save", timeout=2)
            return
        failure: Exception | None = None
        try:
            for key, raw in writes:
                set_setting(self._root, key, raw)
        except Exception as e:  # validation or I/O: report, never crash the UI
            failure = e
        self._reload()  # the file is the truth either way
        self.app.window_ticks = mxview.window_ticks(load_maximize_settings(self._root))
        if failure is not None:
            self.notify(f"Could not save: {failure}", severity="error")
            return
        host = getattr(self.app, "engine_host", None)
        if host is not None:
            host.wake()  # an engine run here re-reads settings.json at once
        self.notify(
            f"Saved {len(writes)} setting{'s' if len(writes) != 1 else ''} — the engine "
            "re-reads settings.json on its next tick",
            timeout=4,
        )

    def action_back(self) -> None:
        if self.dirty and not self._discard_armed:
            self._discard_armed = True
            self.notify(
                "Unsaved changes — b again discards them, s saves", severity="warning"
            )
            return
        self.app.pop_screen()

    def action_quit(self) -> None:
        from claude_swap.tui.fleet import request_quit

        request_quit(self.app)
