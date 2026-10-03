"""Fleet: the maximize home screen (cc-swap fork).

Pushed over the untouched upstream ``DashboardScreen`` when
``autoswitch.strategy == "maximize"`` (the menu's *Classic dashboard* pops
back to it, ``ctrl+f`` returns). One screen answers "what is automatic
switching doing, and is anything wrong":

* one plain-English sentence about the engine, with who runs it on the
  right, and at most one attention line (``maximize/home.py``); ``h`` holds
  the active account (stay on it for 1/2/4 hours or until a time) and the
  sentence then says ``Holding #1 until 15:30 (2h left) — …``;
* a capacity summary over the column headers (``home.capacity``): how many
  accounts have 5h room, when the next 5h comes back, about how many
  accounts' worth of 7d is left this week, and the next 7d reset;
* every account as one row of a table under dim column headers — ``order ·
  account · plan · 5h · 5h resets · 7d · 7d resets · status``: the active
  account first, then the engine's pick order numbered 1, 2, 3 …; 5h/7d
  bars carrying the soft and hard marks; when both windows reset, for every
  account; and one tag each, right after the resets;
* under the table, when there are rows to spare, the selected account in
  full (organization, plan, login deadline, priming, every usage window
  with its exact reset);
* a footer of seven keys; everything else is in the ``m`` menu popup.

The table is used at every terminal size; ``home.table_plan`` fits its
columns to the width (shorter bars, then no reset clocks, no plan column,
shorter names) and, when the terminal is short, drops the capacity summary
first, then the detail panel. The sentence, the attention line, the column
headers and the footer never scroll away.

Viewer by default: the screen never takes the engine lease on its own. It
probes the lease every few seconds (a free lease is taken and dropped at
once; a service starting inside that microsecond window exits 4 and is
restarted by launchd/systemd a minute later — acceptable) and reads what
the engine publishes to its state file, plus its usage history (the idle
pattern and burn rates a decision computed here uses, and ``?`` names).
Every computation lives in
``maximize/fleet.py`` and ``maximize/home.py``; ``tui/fleet_render.py``
draws it. Blocking work (the lease probe, ``service.status()``) runs in
thread workers.
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
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from claude_swap.maximize import fleet as fx
from claude_swap.maximize import hold as account_hold
from claude_swap.maximize import home
from claude_swap.maximize import policy
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
from claude_swap.tui import fleet_render as render
from claude_swap.tui import menus
from claude_swap.tui.engine_host import EngineHost  # noqa: F401 (app.py imports it here)
from claude_swap.tui.fleet_render import tone_style  # noqa: F401 (fleet_accounts imports it here)
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

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


def prime_guard(root: Path) -> str | None:
    """Priming paused after a Claude Code update (no subprocess: what the
    engine last saw, from ``prime_verify.json``)."""
    from claude_swap.maximize.prime_verify import paused_note

    try:
        return paused_note(root)
    except Exception:
        return None


HISTORY_COUNT = 50


def history_text(root: Path, live: str | None) -> str:
    """Fleet's switch-history view: the ledger's newest entries, newest
    first, plus a note when the live login moved outside cc-swap."""
    from claude_swap.maximize import ledger
    from claude_swap.maximize.history_cli import drift_line, history_lines

    entries = ledger.read(root, HISTORY_COUNT)
    lines = history_lines(list(reversed(entries)))
    note = drift_line(root, live)
    if note:
        lines = [note, ""] + lines
    lines += ["", f"(newest first; all of it: cc-swap history -n 0 · {ledger.path_for(root)})"]
    return "\n".join(lines)


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
    from claude_swap.maximize import ledger
    from claude_swap.maximize.fleet_actions import (
        backup_active_before_relogin,
        live_login_fingerprint,
        relogin_store,
    )
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
        now=time.time(),
    )

    def store(before: str | None):
        # The only switch a re-login store makes is the one back to `previous`.
        return run_action(ledger.tagged(
            partial(relogin_store, app.switcher, number, return_to=previous, before=before),
            source="fleet", trigger="relogin-return",
        ))

    app.push_screen(
        ReloginModal(
            lines, backup_root=root, store=store,
            fingerprint=partial(live_login_fingerprint, app.switcher),
            prepare=partial(backup_active_before_relogin, app.switcher, number),
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
    if payload.get("switch_back_error"):
        app.notify(
            f"#{number} login stored; not switched back: {payload['switch_back_error']}",
            title="Re-login", severity="warning", timeout=10,
        )
        return
    tail = f"; back on #{back}" if back else ""
    app.notify(f"#{number} login stored{tail}", title="Re-login")




class FleetBody(Static):
    """The account area. A click selects the account under the pointer."""

    def __init__(self, **kw) -> None:
        super().__init__("", markup=False, **kw)
        self.layout_map: render.Body = render.Body()

    def on_click(self, event) -> None:
        number = self.layout_map.number_at(event.x, event.y)
        screen = self.screen
        if number is not None and isinstance(screen, FleetScreen):
            screen.select(number)


class FleetScreen(Screen):
    CSS_PATH = "fleet.tcss"
    BINDINGS = [
        # The footer: menus.HOME_KEYS.
        Binding("enter", "switch_selected", "Switch", show=False),
        Binding("r", "relogin", "Re-login", show=False),
        Binding("l", "last_resort", "Last resort", show=False),
        Binding("h", "hold", "Hold", show=False),
        Binding("n", "rename", "Name", show=False),  # a row key, not in the footer
        Binding("m", "open_menu", "Menu", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("q", "quit", "Quit", show=False),
        Binding("down,j", "move('down')", show=False),
        Binding("up,k", "move('up')", show=False),
        # The menu's letters, straight from here (menus.SHORTCUT_KEYS).
        Binding("s", "menu('strategy')", "Swap strategy", show=False),
        Binding("p", "menu('prime')", "Prime now", show=False),
        Binding("f", "menu('fetch')", "Fetch", show=False),
        Binding("x", "menu('exclude')", "Exclude", show=False),
        Binding("a", "menu('accounts')", "Account settings", show=False),
        Binding("e,g", "menu('engine')", "Engine log", show=False),
        Binding("v", "menu('history')", "Switch history", show=False),
        Binding("u", "menu('update')", "Update Claude Code", show=False),
        Binding("c", "menu('classic')", "Classic dashboard", show=False),
        Binding("w", "app.open_watch", "Watch", show=False),
        # The home screen: Esc never leaves it (the menu's c does).
        Binding("escape", "noop", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._mx = MaximizeSettings()
        self._prime = PrimeSettings()
        self._poll_s = 60.0
        self._state = mxview.MaximizeState()
        # The engine's usage history (view.read_history; None = unreadable):
        # the idle pattern and burn rates Fleet's own decisions use.
        self._history = None
        self._rows: list[fx.FleetRow] = []
        self._accounts: dict = {}
        self._order: list[str] = []
        self._sel: str | None = None
        self._scroll_to_sel = True
        self._held_elsewhere: bool | None = None
        self._holder_pid: int | None = None
        self._service: dict | None = None
        self._fetched_on_open = False
        self._plan: home.TablePlan | None = None
        self._situation: home.Situation | None = None
        self._hostname = host_name()
        self._fx_timers: list = []
        self._prime_guard: str | None = None

    # -- composition ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="fx-status", markup=False)
        yield Static("", id="fx-attention", markup=False)
        yield Static("", id="fx-summary", markup=False)
        yield Static("", id="fx-head", markup=False)
        with VerticalScroll(id="fx-scroll", can_focus=False):
            yield FleetBody(id="fx-body")
        yield Static("", id="fx-detail", markup=False)
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
        self._scroll_to_sel = True
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
        now = time.time()
        try:
            self._state = mxview.read_state(self._root)
        except Exception:
            self._state = mxview.MaximizeState()
        self._history = mxview.read_history(self._root, now)  # never raises
        self._prime_guard = prime_guard(self._root)
        self._rows = fx.fleet_rows(snap, self._mx, self._prime, self._state, now=now)
        self._accounts = {a.number: a for a in snap.accounts}
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
            auto_off=self._state.auto_off,
        )

    def _msnap(self, now: float):
        """The policy Snapshot Fleet decides on when no engine word is
        fresh: the store, the state file and the usage history, so its own
        decisions see the idle pattern and burn rates the engine sees."""
        snap = self.app.snapshot
        if snap is None:
            return None
        return fx.fleet_snapshot(snap, self._mx, self._state, now=now, history=self._history)

    def _decision(self, msnap=None, now: float | None = None) -> fx.DecisionView:
        now = time.time() if now is None else now
        msnap = msnap if msnap is not None else self._msnap(now)
        if msnap is None:
            return fx.DecisionView("none", None, None, None, "")
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
            self._render_all()

    def current_row(self) -> fx.FleetRow | None:
        return next((r for r in self._rows if r.number == self._sel), None)

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
        """The text width every line is laid out in (the screen minus one
        column of padding each side and one for the accounts' scrollbar)."""
        return home.text_width(self.size.width or 120)

    def _selected_bg(self, palette: Palette) -> str:
        panel = getattr(self.app.current_theme, "panel", None)
        return f"on {panel or palette.track}"

    def _render_all(self) -> None:
        if not self.is_attached:
            return
        size = self.size
        self.set_class((size.height or 36) < home.BLANKS_MIN_ROWS, "-compact")
        palette = self._palette()
        width = self._width()
        now = time.time()
        es = self._engine_status()
        msnap = self._msnap(now)
        dv = self._decision(msnap, now)
        picks = [v.number for v in policy.landing_candidates(msnap)] if msnap else []
        snap = self.app.snapshot
        published = self._state.decision
        sit = home.situation(
            es, dv, active=snap.active_number if snap else None,
            published_at=published.at if published else None, now=now, poll_s=self._poll_s,
        )
        self._situation = sit
        attention = self._render_top(es, dv, sit, now, width, palette)
        ctx = render.Ctx(
            palette=palette,
            ticks=mxview.window_ticks(self._mx),
            now=now,
            next_no=home.next_number(dv, picks, sit),
            priming=self._priming(es, sit),
        )
        rows = home.ordered_rows(self._rows, picks, now=now)
        self._order = [r.number for r in rows]
        if self._sel not in self._order:
            self._sel = next((r.number for r in rows if r.active), None) or (
                self._order[0] if self._order else None
            )
            self._scroll_to_sel = True
        self._render_accounts(rows, ctx, attention, palette)
        self.query_one("#fx-keys", Static).update(render.keys_text(width, palette))

    def _priming(self, es: fx.EngineStatus, sit: home.Situation) -> bool:
        """Whether priming runs now (the next prime time is worth showing)."""
        return home.priming_runs(self._prime.enabled, es, sit, self._prime_guard)

    def _render_top(
        self, es: fx.EngineStatus, dv: fx.DecisionView, sit: home.Situation,
        now: float, width: int, palette: Palette,
    ) -> bool:
        """The status sentence and the attention line; whether the
        attention line shows."""
        published = self._state.decision
        variants = home.status_variants(
            es, dv, self._rows, self._mx, sit, now=now,
            published_at=published.at if published else None,
            hold=self._state.hold, hold_read=True,
        )
        sentence, note = home.status_line(variants, home.holder_variants(es, sit), width)
        self.query_one("#fx-status", Static).update(
            render.status_text(sentence, note, width, palette)
        )
        service = es.service or {}
        attention = home.attention_parts(
            self._rows, now=now, prime_guard=self._prime_guard,
            priming=self._prime.enabled and sit != "auto-off",
            linger_off=service.get("linger") is False,
        )
        widget = self.query_one("#fx-attention", Static)
        widget.display = attention is not None
        if attention is not None:
            parts, tone = attention
            widget.update(render.attention_text(parts, tone, width, palette))
        return attention is not None

    def _render_accounts(
        self, rows: list[fx.FleetRow], ctx: render.Ctx, attention: bool, palette: Palette,
    ) -> None:
        """The capacity summary, the column headers, the table and the
        selected account's panel, laid out by ``home.table_plan`` for this
        terminal."""
        body = self.query_one("#fx-body", FleetBody)
        head = self.query_one("#fx-head", Static)
        summary = self.query_one("#fx-summary", Static)
        detail = self.query_one("#fx-detail", Static)
        size = self.size
        width, height = size.width or 120, size.height or 36
        if not rows:
            self._plan = None
            body.layout_map = render.Body()
            body.update(Text(
                "loading…" if self.app.snapshot is None
                else "No managed accounts yet: m → a (Account settings) adds one.",
                style=palette.muted,
            ))
            head.display = detail.display = summary.display = False
            self.set_class(False, "-summary")
            self._fit_scroll(height, attention, 0)
            return
        row = self.current_row()
        acc = self._accounts.get(row.number) if row is not None else None
        statuses = {r.number: ctx.status(r) for r in rows}
        needs = home.table_needs(
            rows, statuses, now=ctx.now, detail=render.detail_height(row, acc, ctx),
        )
        cap = home.capacity(rows, self._mx, ctx.now)
        plan = home.table_plan(width, height, needs, attention=attention, summary=cap is not None)
        self._plan = plan
        summary.display = plan.summary
        self.set_class(plan.summary, "-summary")
        if plan.summary and cap is not None:
            summary.update(render.summary_text(cap, plan.room, ctx.now, palette))
        head.update(render.table_header(plan, palette))
        head.display = True
        layout_map = render.render_table(
            rows, plan, ctx, selected=self._sel, selected_bg=self._selected_bg(palette),
        )
        body.layout_map = layout_map
        body.update(layout_map.text)
        detail.display = plan.detail
        if plan.detail:
            detail.update(render.render_detail(row, acc, plan.room, ctx))
        self._fit_scroll(height, attention, needs.detail if plan.detail else 0,
                         summary=plan.summary)
        if self._scroll_to_sel:
            self._scroll_to_sel = False
            self.call_after_refresh(self._scroll_selected_into_view)

    def _fit_scroll(
        self, height: int, attention: bool, detail_lines: int, *, summary: bool = False
    ) -> None:
        """Cap the table at the rows the fixed lines leave: the status line,
        the attention line, the blank lines, the capacity summary, the
        column headers, the selected account's panel (``table_plan`` shows
        it only when every row fits above it) and the footer always stay on
        screen."""
        fixed = 1 + 1 + 1  # status line, column headers, footer
        if attention:
            fixed += 1
        if summary:
            fixed += 1
        if height >= home.BLANKS_MIN_ROWS:
            fixed += 2  # above the status line and above the table
        rest = height - fixed - detail_lines
        self.query_one("#fx-scroll", VerticalScroll).styles.max_height = max(rest, 2)

    def _scroll_selected_into_view(self) -> None:
        if not self.is_attached:
            return
        span = self.query_one("#fx-body", FleetBody).layout_map.spans.get(self._sel or "")
        if span is None:
            return
        first, count = span
        self.query_one("#fx-scroll", VerticalScroll).scroll_to_region(
            Region(0, first, 1, count), animate=False, immediate=True,
        )

    # -- selection ----------------------------------------------------------------------

    def select(self, number: str) -> None:
        """Select account ``number`` (a click, or a test)."""
        if number in self._order and number != self._sel:
            self._sel = number
            self._scroll_to_sel = True
            self._render_all()

    def action_move(self, direction: str) -> None:
        target = home.step_selection(self._order, self._sel, direction)
        if target is not None and target != self._sel:
            self._sel = target
            self._scroll_to_sel = True
            self._render_all()

    # -- the menu ------------------------------------------------------------------------

    def action_open_menu(self) -> None:
        from claude_swap.tui.fleet_modals import MenuModal

        self.app.push_screen(MenuModal(self._menu_rows()), self._on_menu)

    def _menu_rows(self) -> list[menus.MenuRow]:
        es = self._engine_status()
        row = self.current_row()
        selected = (
            menus.Selected(row.number, row.name, row.tier == "excluded")
            if row is not None else None
        )
        mx = self._mx
        return menus.menu_rows(
            auto_off=es.auto_off,
            holder=es.holder,
            mode_label=menus.mode_label(es.holder, es.pid),
            thresholds=f"5h {mx.soft_5h:g}/{mx.hard_5h:g} · 7d {mx.soft_7d:g}/{mx.hard_7d:g}",
            relogin=fx.relogin_count(self._rows),
            fetching=self.app._normal_refreshing,
            selected=selected,
        )

    def _on_menu(self, action: str | None) -> None:
        if action is not None:
            self.dispatch_menu(action)

    def dispatch_menu(self, action: str) -> None:
        handler = {
            "auto": self.toggle_auto,
            "mode": self.open_mode,
            "strategy": self.open_strategy,
            "prime": self.open_prime,
            "fetch": self.action_fetch,
            "exclude": self.action_exclude,
            "accounts": self.open_accounts,
            "engine": self.app.action_open_auto,
            "history": self.open_history,
            "update": self.open_update,
            "classic": self.action_classic,
            "quit": self.action_quit,
        }.get(action)
        if handler is None:
            self.notify(f"{menus.BY_ACTION[action].title}: not available yet", timeout=3)
            return
        handler()

    def action_menu(self, action: str) -> None:
        self.dispatch_menu(action)

    def toggle_auto(self) -> None:
        """Menu → o: automatic switching off (after a confirmation), or
        back on at once (``cc-swap auto``)."""
        if self._state.auto_off:
            self._set_auto(False)
        else:
            self.confirm_auto_off()

    def confirm_auto_off(self) -> None:
        """Ask before turning automatic switching OFF (``m`` then a stray
        ``o`` must not stop switching on every engine until someone notices);
        y or enter turns it off, n or esc leaves it on. Turning it back on
        never asks."""
        from claude_swap.tui.modals import ConfirmModal

        self.app.push_screen(
            ConfirmModal(
                "Turn automatic switching OFF? Nothing switches or primes "
                "automatically — on any engine, including the service — until "
                "you turn it back on (m → o, or cc-swap auto on). Manual "
                "switches still work.",
                title="Automatic switching",
                yes_label="Turn off",
            ),
            lambda confirmed: self._set_auto(True) if confirmed else None,
        )

    def open_mode(self) -> None:
        from claude_swap.tui.fleet_modals import ModeModal

        self.app.push_screen(ModeModal(self._engine_status()), self._on_mode)

    def _on_mode(self, action: str | None) -> None:
        """Carry out a Mode choice. Going live always asks first (the auto
        screen's wording); Fleet never takes the lease without a choice."""
        host = self._host
        if action == "auto-off":
            self.confirm_auto_off()
            return
        if action == "auto-on":
            self._set_auto(False)
            return
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

    def _set_auto(self, off: bool) -> None:
        """The persistent automatic-switching switch (``cc-swap auto
        off|on``), honoured by whichever engine runs."""
        self.run_worker(
            partial(self._set_auto_blocking, off), thread=True,
            group="fleet-action", exit_on_error=False, name="fleet-auto-toggle",
        )

    def _set_auto_blocking(self, off: bool) -> None:
        from claude_swap.maximize import pause

        try:
            pause.set_auto_off(self._root, off, by="fleet", now=time.time(), host=self._hostname)
        except Exception as e:
            self.app.call_from_thread(
                self.notify, f"Could not change automatic switching: {e}",
                severity="error", timeout=8,
            )
            return
        message = (
            "Automatic switching OFF — nothing switches or primes until you turn it on"
            if off else "Automatic switching ON"
        )
        self.app.call_from_thread(self._after_setting, message)

    def open_update(self) -> None:
        """Update Claude Code (``cc-swap claude-update``): check, then run on
        a confirmation, with the output in the modal."""
        from claude_swap.tui.fleet_update import ClaudeUpdateModal

        self.app.push_screen(ClaudeUpdateModal(self._root), lambda _r: self._reload_soon())

    def _reload_soon(self) -> None:
        """After the update modal: re-read the prime guard (a changed claude
        pauses priming) and redraw."""
        self._prime_guard = prime_guard(self._root)
        self._render_all()

    def open_history(self) -> None:
        """The switch ledger, newest first (``cc-swap history``)."""
        from claude_swap.tui.modals import OutputModal

        snap = self.app.snapshot
        live = snap.active_number if snap is not None else None
        try:
            text = history_text(self._root, live)
        except Exception as e:
            text = f"Could not read the switch history: {e}"
        self.app.push_screen(OutputModal("Switch history", text))

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
        """Pick accounts to prime now; the selected one is preselected."""
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

    # -- account keys ----------------------------------------------------------------------

    def action_switch_selected(self) -> None:
        """enter: switch to the selected account — asking first only when
        maximize would not land there (switching is reversible)."""
        row = self.current_row()
        if row is None:
            return
        number = row.number
        if row.active:
            self.notify(f"#{number} is already the active account", timeout=2)
            return
        warning = fx.switch_warning(row, self._mx)
        if warning is None:
            self._switch(number)
            return
        from claude_swap.tui.modals import ConfirmModal

        self.app.push_screen(
            ConfirmModal(warning + "\n\nSwitch anyway?", title=f"Switch to #{number}",
                         yes_label="Switch"),
            lambda confirmed: self._switch(number) if confirmed else None,
        )

    def _switch(self, number: str) -> None:
        """The app's switch action, recorded in the switch ledger as Fleet's."""
        from claude_swap.maximize import ledger

        self.app._start_action(
            f"Switch to account {number}",
            ledger.tagged(
                partial(self.app.switcher.switch_to, number, json_output=True), source="fleet"
            ),
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

    # -- name (n) -----------------------------------------------------------------------

    def action_rename(self) -> None:
        """n: name the selected account (``cc-swap alias``): a small input
        prefilled with the name the table shows. Enter saves (the switcher
        checks it as the CLI does), empty clears the alias, esc cancels."""
        from claude_swap.tui.fleet_modals import TextInputModal

        row = self.current_row()
        if row is None:
            return
        self.app.push_screen(
            TextInputModal(
                f"Name #{row.number}",
                "Letters, digits, - _ . (no @ or comma, not taken). Empty: back to the "
                "part of the address before the @.",
                row.name,
            ),
            partial(self._on_rename, row.number, row.name),
        )

    def _on_rename(self, number: str, shown: str, typed: str | None) -> None:
        acc = self._accounts.get(number)
        request = fx.name_request(acc.alias if acc is not None else "", shown, typed)
        if request is None:
            return
        switcher = self.app.switcher
        verb, name = request
        if verb == "set":
            self.app._start_action(f"Name #{number}", partial(switcher.set_alias, number, name))
        else:
            self.app._start_action(f"Name #{number}", partial(switcher.unset_alias, number))

    # -- hold (h) -----------------------------------------------------------------------

    def action_hold(self) -> None:
        """h: hold the ACTIVE account (whatever row is selected) — stay on
        it for 1, 2 or 4 hours (h, t, f) or until a time (u, or a digit
        starts typing one), or lift the hold
        (``cc-swap hold``). Any engine honours it on its next tick."""
        from claude_swap.tui.fleet_modals import MenuModal

        snap = self.app.snapshot
        active = snap.active_number if snap is not None else None
        row = next((r for r in self._rows if r.number == active), None)
        if row is None:
            self.notify("No active account to hold", timeout=3)
            return
        now = time.time()
        current = account_hold.holding(self._state.hold, row.number, now)
        self.app.push_screen(
            MenuModal(
                menus.hold_rows(current.until if current is not None else None, now),
                title=menus.HOLD_TITLE.format(number=row.number, name=row.name),
                note=menus.HOLD_NOTE,
                digits=menus.HOLD_TYPED,
            ),
            partial(self._on_hold_choice, row.number),
        )

    def _on_hold_choice(self, slot: str, action: str | None) -> None:
        if action is None:
            return
        if action == menus.HOLD_OFF:
            self._hold_write(slot, None)
        elif action == menus.HOLD_UNTIL or action.startswith(menus.HOLD_TYPED):
            from claude_swap.tui.fleet_modals import TextInputModal

            self.app.push_screen(
                TextInputModal(
                    f"Hold #{slot} until", "A local time, HH:MM (the next one; at most 24h "
                    "ahead). Enter holds, esc cancels.",
                    value=action.removeprefix(menus.HOLD_TYPED)
                    if action.startswith(menus.HOLD_TYPED) else "",
                    select=False,
                ),
                partial(self._on_hold_until, slot),
            )
        elif action.startswith("hold:"):
            self._hold_write(slot, time.time() + float(action.removeprefix("hold:")))

    def _on_hold_until(self, slot: str, text: str | None) -> None:
        if not text:
            return
        try:
            until = account_hold.parse_until(text, time.time())
        except ValueError as e:
            self.notify(str(e), severity="error", timeout=6)
            return
        self._hold_write(slot, until)

    def _hold_write(self, slot: str, until: float | None) -> None:
        """Set (``until``) or lift (None) the hold, in a thread worker."""
        self.run_worker(
            partial(self._hold_blocking, slot, until), thread=True,
            group="fleet-action", exit_on_error=False, name="fleet-hold",
        )

    def _hold_blocking(self, slot: str, until: float | None) -> None:
        now = time.time()
        try:
            if until is None:
                lifted = account_hold.clear_hold(self._root)
                message = "Hold lifted" if lifted else "No hold to lift"
            else:
                hold = account_hold.set_hold(
                    self._root, slot, until, by="fleet", now=now, host=self._hostname,
                )
                message = account_hold.held_message(hold, now, asked=until)
        except Exception as e:
            self.app.call_from_thread(
                self.notify, f"Could not change the hold: {e}", severity="error", timeout=8,
            )
            return
        self.app.call_from_thread(self._after_setting, message)

    def action_exclude(self) -> None:
        row = self.current_row()
        if row is not None:
            self.app.do_toggle_disabled(row.number)

    def action_relogin(self) -> None:
        row = self.current_row()
        if row is None:
            return
        if row.login != "relogin" and not fx.login_due(row, time.time()):
            self.notify(f"#{row.number} login works — nothing to fix", timeout=3)
            return
        open_relogin(self.app, row.number)

    # -- actions -------------------------------------------------------------------------

    def action_noop(self) -> None:
        pass

    def action_fetch(self) -> None:
        """One full fetch now — also as a viewer (store-only lane)."""
        self.app._start_normal_refresh(full=True)
        self.notify("Fetching latest usage…", timeout=2)

    def action_classic(self) -> None:
        # The upstream dashboard has no notion of a viewer lane: hand it the
        # fetch-enabled lane so its ``f`` performs a real fetch. Fleet's
        # ``on_screen_resume`` re-applies store-only when it comes back.
        self.app.set_store_only(False)
        self.app.pop_screen()

    def idle_pattern(self) -> str:
        """``idle pattern: 9 days learned · next quiet window 23:00–07:30``:
        what the engine has learned of your busy and quiet times, for the
        help screen (the home screen itself stays quiet about it)."""
        now = time.time()
        return mxview.idle_pattern_text(mxview.read_history(self._root, now), self._mx, now)

    def action_help(self) -> None:
        from claude_swap.tui.fleet_help import HelpScreen

        self.app.push_screen(HelpScreen(idle_pattern=self.idle_pattern()))

    def action_quit(self) -> None:
        request_quit(self.app)
