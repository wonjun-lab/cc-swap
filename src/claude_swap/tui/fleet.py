"""Fleet: the maximize home screen (cc-swap fork).

Pushed over the untouched upstream ``DashboardScreen`` when
``autoswitch.strategy == "maximize"`` (``c`` pops back to it, ``ctrl+f``
returns). One screen answers "what is maximize doing, and why": a status
block (engine holder, the engine's last decision, priming), every account
as one table row in slot order with maximize's rank, plan, tier, landing
verdict, 5h window and next prime, the highlighted account's card, and a
first-letter menu (codex-swap's convention, ``tui/menus.py``).

Viewer by default: the screen never takes the engine lease on its own. It
probes the lease every few seconds (a free lease is taken and dropped at
once; a service starting inside that microsecond window exits 4 and is
restarted by launchd/systemd a minute later — acceptable) and reads what
the engine publishes to its state file. Every computation lives in
``maximize/fleet.py``; this module only lays the cells out. Blocking work
(the lease probe, ``service.status()``) runs in thread workers.
"""

from __future__ import annotations

import os
import socket
import time
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, ListItem, ListView, Static

from claude_swap.maximize import fleet as fx
from claude_swap.maximize import view as mxview
from claude_swap.maximize.primer import plan_text as mxprimer_plan_text
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import (
    MaximizeSettings,
    PrimeSettings,
    load_maximize_settings,
    load_prime_settings,
    load_settings,
)
from claude_swap.tui import menus
from claude_swap.tui.engine_host import EngineHost  # noqa: F401 (app.py imports it here)
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import account_card_text

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

FLASH_S = 1.5             # a just-refreshed row stays highlighted this long
LEASE_PROBE_S = 5.0
SERVICE_PROBE_S = 30.0
FETCH_ON_OPEN_ENV = "CC_SWAP_FETCH_ON_OPEN"


def fork_home(backup_dir: Path) -> bool:
    """Whether the app opens on Fleet: the maximize strategy is configured."""
    try:
        return load_settings(backup_dir).strategy == "maximize"
    except Exception:
        return False


def service_status() -> dict | None:
    """``maximize.service.status()`` plus linger on Linux; None when it
    cannot be read. Spawns launchctl/systemctl: thread workers only."""
    from claude_swap.maximize import service

    try:
        status = dict(service.status())
        if status.get("platform") == "linux":
            status["linger"] = service.linger_enabled()
        return status
    except Exception:
        return None


def host_name() -> str:
    return socket.gethostname().split(".")[0] or "this host"


def tone_style(tone: str, palette: Palette) -> str:
    return {
        "ok": palette.sev_ok,
        "warn": palette.sev_warn,
        "crit": palette.sev_crit,
        "dim": palette.muted,
        "accent": palette.accent,
        "bold": f"bold {palette.foreground}",
    }.get(tone, palette.foreground)


def menu_text(title: str, key: str, palette: Palette, *, tone: str = "plain") -> Text:
    """A menu title with its shortcut letter in bold."""
    text = Text(title, style=tone_style(tone, palette))
    span = menus.bold_spans(title, key)
    if span is not None:
        text.stylize(f"bold {palette.accent}", *span)
    return text


def request_quit(app: "CswapApp") -> None:
    """``q`` anywhere in the Fleet screens: asks first only while this TUI
    runs a LIVE engine (quitting stops it)."""
    host = getattr(app, "engine_host", None)
    if host is None or not host.running or host.dry_run:
        _quit(app)
        return
    from claude_swap.tui.modals import ConfirmModal

    app.push_screen(
        ConfirmModal(
            "Quit? The LIVE engine running in this TUI stops with it; nothing "
            "switches accounts until the service or another engine runs.",
            title="Quit",
            yes_label="Quit",
        ),
        lambda confirmed: _quit(app) if confirmed else None,
    )


def _quit(app: "CswapApp") -> None:
    host = getattr(app, "engine_host", None)
    if host is not None:
        host.stop()
    app.exit()


def open_fleet(app: "CswapApp") -> None:
    """``ctrl+f``: back to Fleet from any screen — pop down to it when it is
    in the stack, else push it. A modal keeps the keyboard (answer it first);
    without maximize there is no Fleet and the key does nothing."""
    if not app.is_screen_installed("fleet") or isinstance(app.screen, ModalScreen):
        return
    fleet = app.get_screen("fleet")
    if fleet in app.screen_stack:
        while app.screen is not fleet:
            app.pop_screen()
    else:
        app.push_screen(fleet)


def fleet_rows_now(app: "CswapApp", snap: AccountsSnapshot | None = None) -> list[fx.FleetRow]:
    """``fleet_rows`` for the app's current snapshot, settings and state file."""
    snap = snap if snap is not None else app.snapshot
    if snap is None:
        return []
    root = app.switcher.backup_dir
    try:
        mx, prime = load_maximize_settings(root), load_prime_settings(root)
    except Exception:
        mx, prime = MaximizeSettings(), PrimeSettings()
    try:
        state = mxview.read_state(root)
    except Exception:
        state = mxview.MaximizeState()
    return fx.fleet_rows(snap, mx, prime, state, now=time.time())


def open_relogin(app: "CswapApp", number: str) -> None:
    """The guided re-login for slot ``number`` (cc-swap launches nothing).

    The steps name the real ``claude`` (``prime.claudePath``, else
    ``~/.local/bin/claude``) and the account's email; enter then stores the
    live login only if it is this slot's account, and switches back to the
    account active now."""
    from claude_swap.maximize.fleet_actions import live_login_fingerprint, relogin_store
    from claude_swap.maximize.primer import resolve_claude_path
    from claude_swap.tui.data import run_action
    from claude_swap.tui.fleet_modals import ReloginModal

    rows = {r.number: r for r in fleet_rows_now(app)}
    row = rows.get(number)
    if row is None:
        return
    root = app.switcher.backup_dir
    previous = app.snapshot.active_number if app.snapshot is not None else None
    try:
        claude = resolve_claude_path(load_prime_settings(root).claude_path)
    except Exception:
        claude = None
    lines = fx.relogin_steps(
        row,
        ssh=fx.over_ssh(),
        host=host_name(),
        claude_path=claude,
        return_to=rows.get(previous) if previous else None,
    )
    def store(before: str | None):
        return run_action(
            partial(relogin_store, app.switcher, number, return_to=previous, before=before)
        )

    app.push_screen(
        ReloginModal(
            lines, backup_root=root, store=store,
            fingerprint=partial(live_login_fingerprint, app.switcher),
        ),
        partial(_relogin_done, app, number),
    )


def _relogin_done(app: "CswapApp", number: str, result) -> None:
    from claude_swap.tui.modals import OutputModal

    app.request_refresh(full=True)
    if result is None:
        app.notify(f"Re-login #{number} cancelled; switching resumed", timeout=3)
        return
    if not result.ok:
        app.push_screen(OutputModal(f"Re-login #{number} — failed", result.output))
        return
    payload = result.payload or {}
    if not payload.get("stored"):
        app.notify(
            str(payload.get("reason") or "nothing stored"), title=f"Re-login #{number}",
            severity="error", timeout=10,
        )
        return
    back = payload.get("returned_to")
    tail = f"; back on #{back}" if back else ""
    app.notify(f"#{number} login stored{tail}", title="Re-login")


class FleetTable(DataTable):
    """The account table; ↓ past the last row moves focus to the menu."""

    def action_cursor_down(self) -> None:
        if self.row_count and self.cursor_row >= self.row_count - 1:
            screen = self.screen
            if isinstance(screen, FleetScreen):
                screen.focus_menu()
                return
        super().action_cursor_down()


class FleetMenu(ListView):
    """The vertical menu; ↑ on its first item returns to the table."""

    def action_cursor_up(self) -> None:
        if (self.index or 0) <= 0:
            screen = self.screen
            if isinstance(screen, FleetScreen):
                screen.focus_table()
                return
        super().action_cursor_up()


class FleetMenuItem(ListItem):
    def __init__(self, entry: menus.MenuEntry) -> None:
        super().__init__(Static(entry.title, markup=False))
        self.action_id = entry.action
        self.key = entry.key

    def set_title(self, text: Text) -> None:
        self.query_one(Static).update(text)


class FleetScreen(Screen):
    CSS_PATH = "fleet.tcss"
    BINDINGS = [
        Binding("s", "menu('strategy')", "Swap strategy", show=False),
        Binding("m", "menu('mode')", "Mode", show=False),
        Binding("p", "menu('prime')", "Prime now", show=False),
        Binding("a", "menu('accounts')", "Account settings", show=False),
        Binding("l", "last_resort", "Last resort", show=False),
        Binding("x", "exclude", "Exclude", show=False),
        Binding("r", "relogin", "Re-login", show=False),
        Binding("f", "fetch", "Fetch", show=False),
        Binding("e,g", "app.open_auto", "Engine log", show=False),
        Binding("c", "classic", "Classic dashboard", show=False),
        Binding("w", "app.open_watch", "Watch", show=False),
        Binding("question_mark,h", "help", "Help", show=False),
        Binding("q", "quit", "Quit", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        # The home screen: Esc never leaves it (c does).
        Binding("escape", "noop", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._mx = MaximizeSettings()
        self._prime = PrimeSettings()
        self._poll_s = 60.0
        self._state = mxview.MaximizeState()
        self._rows: list[fx.FleetRow] = []
        self._accounts: dict = {}
        self._numbers: list[str] = []
        self._columns: tuple[str, ...] = ()
        self._held_elsewhere: bool | None = None
        self._holder_pid: int | None = None
        self._service: dict | None = None
        self._fetched: dict[str, float | None] = {}
        self._flash_until: dict[str, float] = {}
        self._fetched_on_open = False
        self._layout: fx.LayoutPlan | None = None
        self._hostname = host_name()
        self._ssh = fx.over_ssh()
        self._fx_timers: list = []

    # -- composition ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="fx-head", markup=False)
        yield Static("", id="fx-engine", markup=False)
        yield Static("", id="fx-now", markup=False)
        yield Static("", id="fx-prime", markup=False)
        yield Static("", id="fx-attention", markup=False)
        yield FleetTable(id="fx-table", cursor_type="row", zebra_stripes=False)
        yield Static("", id="fx-detail", markup=False)
        yield FleetMenu(*(FleetMenuItem(e) for e in menus.MAIN_MENU), id="fx-menu")
        yield Static("", id="fx-menu-folded", markup=False)
        yield Static("", id="fx-keys", markup=False)

    def on_mount(self) -> None:
        self._load_settings()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", lambda _t: self._render_all())
        self._fx_timers = [
            self.set_interval(LEASE_PROBE_S, self._probe_lease),
            self.set_interval(SERVICE_PROBE_S, self._probe_service),
        ]
        self._probe_lease()
        self._probe_service()
        if self._host is not None:
            self._host.subscribe(self._on_host_event)
        self.focus_table()

    def on_unmount(self) -> None:
        # The app is closing: an engine run here stops with it.
        if self._host is not None:
            self._host.unsubscribe(self._on_host_event)
            self._host.stop()

    def on_screen_resume(self) -> None:
        for timer in self._fx_timers:
            timer.resume()
        self._load_settings()
        self.app.window_ticks = mxview.window_ticks(self._mx)
        self._apply_store_only()
        self._probe_lease()
        self._on_snapshot(self.app.snapshot)

    def on_screen_suspend(self) -> None:
        # Another screen is on top (engine log, watch, a sub-screen): it owns
        # the store-only lane meanwhile, and nobody needs our probes.
        for timer in self._fx_timers:
            timer.pause()

    def on_resize(self) -> None:
        self._render_all()

    # -- data -------------------------------------------------------------------------

    @property
    def _root(self) -> Path:
        return self.app.switcher.backup_dir

    def _load_settings(self) -> None:
        try:
            self._mx = load_maximize_settings(self._root)
            self._prime = load_prime_settings(self._root)
            self._poll_s = float(load_settings(self._root).interval_seconds)
        except Exception:
            pass

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        self._load_settings()
        try:
            self._state = mxview.read_state(self._root)
        except Exception:
            self._state = mxview.MaximizeState()
        now = time.time()
        self._rows = fx.fleet_rows(snap, self._mx, self._prime, self._state, now=now)
        self._accounts = {a.number: a for a in snap.accounts}
        for row in self._rows:
            before = self._fetched.get(row.number)
            if before is not None and row.fetched_at is not None and row.fetched_at > before:
                self._flash_until[row.number] = now + FLASH_S
                self.set_timer(FLASH_S + 0.05, self._render_table)
            self._fetched[row.number] = row.fetched_at
        self._maybe_fetch_on_open()
        self._render_all()

    @property
    def _host(self) -> EngineHost | None:
        return getattr(self.app, "engine_host", None)

    def _own_mode(self) -> str | None:
        """``"live"``/``"dry"`` while this TUI runs the engine."""
        host = self._host
        if host is None or not host.running:
            return None
        return "dry" if host.dry_run else "live"

    def _engine_status(self) -> fx.EngineStatus:
        own = self._own_mode()
        return fx.engine_status(
            held_elsewhere=bool(self._held_elsewhere),
            holder_pid=os.getpid() if own else self._holder_pid,
            own=own,
            service=self._service,
        )

    def _decision(self) -> fx.DecisionView:
        snap = self.app.snapshot
        now = time.time()
        if snap is None:
            return fx.DecisionView("none", None, None, None, "")
        msnap = fx.fleet_snapshot(snap, self._mx, self._state, now=now)
        host = self._host
        own = host.last_decision if host is not None and host.running else None
        return fx.decision_view(
            self._state, msnap, now=now, poll_s=self._poll_s,
            own=own, own_at=host.last_decision_at if own is not None else None,
        )

    def _on_host_event(self, event) -> None:
        """The engine this TUI runs started, stopped, or decided."""
        if event is None:
            self._apply_store_only()
            self._render_all()
        elif event.kind == "maximize" and self.is_attached:
            self._render_status()

    def current_row(self) -> fx.FleetRow | None:
        table = self.query_one("#fx-table", FleetTable)
        if not self._numbers or table.row_count == 0:
            return None
        index = min(max(table.cursor_row, 0), len(self._numbers) - 1)
        number = self._numbers[index]
        return next((r for r in self._rows if r.number == number), None)

    # -- lease and service probes (thread workers) ---------------------------------------

    def _probe_lease(self) -> None:
        self.run_worker(
            self._lease_blocking, thread=True, group="fleet-lease",
            exclusive=True, exit_on_error=False, name="fleet-lease-probe",
        )

    def _lease_blocking(self) -> None:
        lease = self.app.engine_keeper.lease
        try:
            held = lease.held_elsewhere()
        except OSError:
            held = False
        pid = lease.holder_pid() if held else None
        self.app.call_from_thread(self._on_lease, held, pid)

    def _on_lease(self, held: bool, pid: int | None) -> None:
        changed = (held, pid) != (self._held_elsewhere, self._holder_pid)
        self._held_elsewhere, self._holder_pid = held, pid
        self._apply_store_only()
        self._maybe_fetch_on_open()
        if changed:
            self._render_all()

    def _probe_service(self) -> None:
        self.run_worker(
            self._service_blocking, thread=True, group="fleet-service",
            exclusive=True, exit_on_error=False, name="fleet-service-probe",
        )

    def _service_blocking(self) -> None:
        status = service_status()
        self.app.call_from_thread(self._on_service, status)

    def _on_service(self, status: dict | None) -> None:
        if status != self._service:
            self._service = status
            self._render_all()

    def _apply_store_only(self) -> None:
        """Another engine (or ours) fetches: the poller only reads the store."""
        if not self.is_current or self._held_elsewhere is None:
            return
        store_only = bool(self._held_elsewhere) or self._own_mode() is not None
        if store_only != self.app._store_only:
            self.app.set_store_only(store_only)

    def _maybe_fetch_on_open(self) -> None:
        """A viewer reads what the engine fetched; rows it has not refreshed
        for a while get one full fetch when the screen opens."""
        if self._fetched_on_open or not self._held_elsewhere or not self._rows:
            return
        self._fetched_on_open = True
        if os.environ.get(FETCH_ON_OPEN_ENV, "1") == "0":
            return
        if any(r.stale for r in self._rows):
            self.app._start_normal_refresh(full=True)

    # -- rendering ---------------------------------------------------------------------

    def _palette(self) -> Palette:
        return Palette.from_theme(self.app.current_theme)

    def _width(self) -> int:
        return max((self.size.width or 112) - 2, 20)

    def _render_all(self) -> None:
        if not self.is_attached:
            return
        size = self.size
        self._layout = fx.fit_layout(
            size.height or 32, size.width or 112, len(self._rows),
            attention=fx.attention(self._rows) is not None,
        )
        self._render_status()
        self._render_table()
        self._render_detail()
        self._render_menu()
        self._render_keys()
        self._apply_layout()

    def _apply_layout(self) -> None:
        plan = self._layout
        if plan is None:
            return
        self.query_one("#fx-prime").display = plan.prime_line
        self.query_one("#fx-detail").display = plan.detail
        self.query_one("#fx-menu").display = plan.menu == "full"
        self.query_one("#fx-menu-folded").display = plan.menu == "folded"
        self.query_one("#fx-attention").display = fx.attention(self._rows) is not None
        self.set_class(not plan.blanks, "-compact")
        if plan.menu == "folded" and self.focused is self.query_one("#fx-menu"):
            self.focus_table()

    def _render_status(self) -> None:
        palette = self._palette()
        width = self._width()
        now = time.time()
        head = fx.header_line(
            self._mx, self._prime, self._rows, host=self._hostname, ssh=self._ssh,
            now=now, width=width,
        )
        self.query_one("#fx-head", Static).update(Text(head, style=f"bold {palette.foreground}"))
        lines = fx.status_lines(
            self._engine_status(), self._decision(), self._rows, self._mx, self._prime,
            now=now, width=width,
        )
        for widget_id, (text, tone) in zip(("#fx-engine", "#fx-now", "#fx-prime"), lines):
            line = Text(text[:8], style=palette.muted)
            line.append(text[8:], style=tone_style(tone, palette))
            self.query_one(widget_id, Static).update(line)
        warning = fx.attention(self._rows) or ""
        self.query_one("#fx-attention", Static).update(
            Text(warning, style=f"bold {palette.sev_crit}")
        )

    def _cells(self, row: fx.FleetRow, palette: Palette) -> list[Text]:
        now = time.time()
        flash = self._flash_until.get(row.number, 0.0) > now
        out: list[Text] = []
        name_width = fx.account_width(self._width())
        cells = fx.row_cells(row, self._columns, now=now, mx=self._mx)
        for col, (text, tone) in zip(self._columns, cells):
            if col == "account":
                text = fx.clip(text, name_width)
            if row.login == "relogin" and text.strip():
                tone = "crit"
            style = tone_style(tone, palette)
            if flash:
                style += f" on {palette.track}"
            out.append(Text(text, style=style, no_wrap=True))
        return out

    def _render_table(self) -> None:
        if not self.is_attached or self._layout is None:
            return
        table = self.query_one("#fx-table", FleetTable)
        palette = self._palette()
        columns = self._layout.columns
        numbers = [r.number for r in self._rows]
        keep = self._numbers[table.cursor_row] if (
            self._numbers and 0 <= table.cursor_row < len(self._numbers)
        ) else None
        if columns != self._columns or numbers != self._numbers:
            self._columns = columns
            table.clear(columns=True)
            for col in columns:
                table.add_column(fx.COLUMN_LABELS[col], key=col)
            for row in self._rows:
                table.add_row(*self._cells(row, palette), key=row.number)
            self._numbers = numbers
            if keep in numbers:
                table.move_cursor(row=numbers.index(keep))
            elif keep is None and self.app.snapshot is not None:
                active = next((i for i, r in enumerate(self._rows) if r.active), 0)
                table.move_cursor(row=active)
            return
        for row in self._rows:
            for col, cell in zip(columns, self._cells(row, palette)):
                table.update_cell(row.number, col, cell, update_width=True)

    def _render_detail(self) -> None:
        if self._layout is not None and not self._layout.detail:
            return
        row = self.current_row()
        widget = self.query_one("#fx-detail", Static)
        if row is None:
            widget.update("")
            return
        palette = self._palette()
        acc = self._accounts.get(row.number)
        text = Text()
        if acc is not None:
            text.append(account_card_text(
                acc, self._width() - 2, threshold=self.app.threshold_pct, now=time.time(),
                palette=palette, window_ticks=getattr(self.app, "window_ticks", None),
            ))
        text.append("\n    ")
        text.append(fx.detail_line(row, self._mx), style=palette.muted)
        widget.update(text)

    def _menu_title(self, entry: menus.MenuEntry) -> tuple[str, str]:
        es = self._engine_status()
        relogin = fx.relogin_count(self._rows)
        title = menus.menu_title(
            entry.action,
            mode_label=menus.mode_label(es.holder, es.pid),
            relogin=relogin,
            fetching=self.app._normal_refreshing,
        )
        tone = "plain"
        if entry.action == "mode" and es.holder == "none":
            tone = "warn"
        if entry.action == "accounts" and relogin:
            tone = "warn"
        return title, tone

    def _render_menu(self) -> None:
        palette = self._palette()
        for item in self.query(FleetMenuItem):
            entry = menus.BY_ACTION[item.action_id]
            title, tone = self._menu_title(entry)
            item.set_title(menu_text(title, entry.key, palette, tone=tone))
        es = self._engine_status()
        folded = Text()
        for i, line in enumerate(
            menus.folded_menu(self._width(), mode_label=menus.mode_label(es.holder, es.pid))
        ):
            if i:
                folded.append("\n")
            for j, (title, key) in enumerate(line):
                if j:
                    folded.append(menus.SEP)
                folded.append(menu_text(title, key, palette))
        self.query_one("#fx-menu-folded", Static).update(folded)

    def _render_keys(self) -> None:
        palette = self._palette()
        minimal = self._layout is not None and self._layout.keys == "minimal"
        hints = menus.key_hints(self._width() - 2, minimal=minimal)
        self.query_one("#fx-keys", Static).update(Text(hints, style=palette.muted))

    # -- focus ---------------------------------------------------------------------------

    def focus_table(self) -> None:
        self.query_one("#fx-table", FleetTable).focus()

    def focus_menu(self) -> None:
        if self._layout is not None and self._layout.menu == "folded":
            return  # the folded menu is keys only
        menu = self.query_one("#fx-menu", FleetMenu)
        menu.index = 0
        menu.focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._render_detail()

    # -- menu ----------------------------------------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, FleetMenuItem):
            self.dispatch_menu(item.action_id)

    def dispatch_menu(self, action: str) -> None:
        handler = {
            "mode": self.open_mode,
            "strategy": self.open_strategy,
            "prime": self.open_prime,
            "accounts": self.open_accounts,
            "fetch": self.action_fetch,
            "engine": self.app.action_open_auto,
            "classic": self.action_classic,
            "quit": self.action_quit,
        }.get(action)
        if handler is None:
            self.notify(f"{menus.BY_ACTION[action].title}: not available yet", timeout=3)
            return
        handler()

    def action_menu(self, action: str) -> None:
        self.dispatch_menu(action)

    def open_mode(self) -> None:
        from claude_swap.tui.fleet_modals import ModeModal

        self.app.push_screen(ModeModal(self._engine_status()), self._on_mode)

    def _on_mode(self, action: str | None) -> None:
        """Carry out a Mode choice. Going live always asks first (the auto
        screen's wording); Fleet never takes the lease without a choice."""
        host = self._host
        if action is None or host is None:
            return
        if action == "start-dry":
            if not host.start(dry_run=True, by="fleet"):
                self.notify("Another engine holds the lease — this TUI stays a viewer",
                            severity="warning")
        elif action in ("start-live", "go-live"):
            from claude_swap.tui.modals import ConfirmModal

            self.app.push_screen(
                ConfirmModal(
                    "Go live? cc-swap will switch your active account by the maximize "
                    "strategy (and prime idle accounts when priming is on).\n\n"
                    "(Same behavior as running `cc-swap auto` in a terminal; quitting "
                    "this TUI stops it.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                partial(self._on_go_live, action),
            )
        elif action == "go-dry":
            host.set_dry_run(True)
        elif action == "stop":
            host.stop()
        self._apply_store_only()
        self._render_all()

    def _on_go_live(self, action: str, confirmed: bool | None) -> None:
        host = self._host
        if not confirmed or host is None:
            return
        if action == "go-live":
            host.set_dry_run(False)
        elif not host.start(dry_run=False, by="fleet"):
            self.notify("Another engine holds the lease — this TUI stays a viewer",
                        severity="warning")
        self._apply_store_only()
        self._render_all()

    def open_strategy(self) -> None:
        from claude_swap.tui.fleet_strategy import StrategyScreen

        self.app.push_screen(StrategyScreen())

    def open_accounts(self) -> None:
        from claude_swap.tui.fleet_accounts import AccountsScreen

        self.app.push_screen(AccountsScreen())

    def open_prime(self) -> None:
        """Pick accounts to prime now; the highlighted one is preselected."""
        from claude_swap.maximize.primer import plan_rows
        from claude_swap.tui.fleet_modals import PrimeChoice, PrimeModal, prime_lines

        snap = self.app.snapshot
        if snap is None:
            return
        now = time.time()
        msnap = fx.fleet_snapshot(snap, self._mx, self._state, now=now)
        names = {r.number: r.name for r in self._rows}
        choices = []
        for plan in plan_rows(msnap, self._state.primes, self._prime, now):
            text = mxprimer_plan_text(plan, self._prime.max_attempts)
            choices.append(PrimeChoice(
                plan.number, f"#{plan.number} {names.get(plan.number, '')}  {text}",
                plan.reason is None,
            ))
        row = self.current_row()
        preselect = {row.number} if row is not None else set()
        self.app.push_screen(
            PrimeModal(choices, preselect, partial(prime_lines, self.app.switcher))
        )

    # -- row keys ------------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """enter on a row: switch to it — asking first only when maximize
        would not land there (switching is reversible)."""
        number = str(event.row_key.value)
        row = next((r for r in self._rows if r.number == number), None)
        if row is None:
            return
        if row.active:
            self.notify(f"#{number} is already the active account", timeout=2)
            return
        warning = fx.switch_warning(row, self._mx)
        if warning is None:
            self.app.do_switch(number)
            return
        from claude_swap.tui.modals import ConfirmModal

        self.app.push_screen(
            ConfirmModal(warning + "\n\nSwitch anyway?", title=f"Switch to #{number}",
                         yes_label="Switch"),
            lambda confirmed: self.app.do_switch(number) if confirmed else None,
        )

    def action_last_resort(self) -> None:
        row = self.current_row()
        if row is None:
            return
        snap = self.app.snapshot
        accounts = {
            a.number: {"email": a.email, "alias": a.alias}
            for a in (snap.accounts if snap else ())
        }
        self.run_worker(
            partial(self._last_resort_blocking, accounts, row.number), thread=True,
            group="fleet-action", exit_on_error=False, name="fleet-last-resort",
        )

    def _last_resort_blocking(self, accounts: dict, number: str) -> None:
        from claude_swap.exceptions import ClaudeSwitchError
        from claude_swap.maximize.fleet_actions import toggle_last_resort_setting

        try:
            marked = toggle_last_resort_setting(self._root, accounts, number)
        except ClaudeSwitchError as e:
            self.app.call_from_thread(self.notify, str(e), severity="error", timeout=8)
            return
        message = f"#{number} is last resort" if marked else f"#{number} is back to normal"
        self.app.call_from_thread(self._after_setting, message)

    def _after_setting(self, message: str) -> None:
        self.notify(message, timeout=3)
        self._on_snapshot(self.app.snapshot)

    def action_exclude(self) -> None:
        row = self.current_row()
        if row is not None:
            self.app.do_toggle_disabled(row.number)

    def action_relogin(self) -> None:
        row = self.current_row()
        if row is None:
            return
        if row.login != "relogin":
            self.notify(f"#{row.number} login works — nothing to fix", timeout=3)
            return
        open_relogin(self.app, row.number)

    # -- actions -------------------------------------------------------------------------

    def action_noop(self) -> None:
        pass

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, (FleetTable, FleetMenu)):
            focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, (FleetTable, FleetMenu)):
            focused.action_cursor_up()

    def action_fetch(self) -> None:
        """One full fetch now — also as a viewer (store-only lane)."""
        self.app._start_normal_refresh(full=True)
        self.notify("Fetching latest usage…", timeout=2)
        self._render_menu()

    def action_classic(self) -> None:
        # The upstream dashboard has no notion of a viewer lane: hand it the
        # fetch-enabled lane so its ``f`` performs a real fetch. Fleet's
        # ``on_screen_resume`` re-applies store-only when it comes back.
        self.app.set_store_only(False)
        self.app.pop_screen()

    def action_help(self) -> None:
        from claude_swap.tui.fleet_help import HelpScreen

        self.app.push_screen(HelpScreen())

    def action_quit(self) -> None:
        request_quit(self.app)
