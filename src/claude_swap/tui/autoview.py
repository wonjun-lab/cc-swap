"""Live auto-switch screen: the real engine, visualized.

Runs :class:`AutoSwitchEngine` in a thread worker and renders its typed
events. Opens in **dry-run** — opening a view must never start switching
accounts on its own; going live is an explicit, confirmed action. The
engine runs only while the app holds the machine's engine lease
(``maximize/lease.py``); when another process holds it — the cc-swap
service, a terminal ``cc-swap auto`` — the screen is a read-only VIEWER:
no engine, store-only snapshots, no go-live.

The active account's full card sits on top (same widget as the dashboard's
panel, with the threshold tick); this screen adds the engine badge, the
ranked switch candidates, and the decision log. While it is up, the app's
snapshot poller runs store-only: the engine is the only fetcher.
"""

from __future__ import annotations

import time
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    pct_label,
)
from claude_swap.maximize import fleet as mxfleet
from claude_swap.maximize import view as mxview
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import (
    SETTING_SPECS,
    load_maximize_settings,
    load_settings,
    parse_model_names,
    set_setting,
)
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel

if TYPE_CHECKING:
    from claude_swap.maximize.lease import LeaseKeeper
    from claude_swap.tui.app import CswapApp

_EVENT_ROLES = {
    "switch": "accent",
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
_QUIET_KINDS = {"poll", "no-switch", "sleep", "account-unquarantined"}


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer."""
    role = _EVENT_ROLES.get(event.kind)
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in _QUIET_KINDS else palette.foreground
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


def _run_engine_holding(engine: AutoSwitchEngine, keeper: "LeaseKeeper") -> int:
    """Engine worker body: count this thread against the lease until its last
    tick returns (``stop()`` only asks the loop to end)."""
    try:
        return engine.run_loop()
    finally:
        keeper.engine_exited()


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "adjust_done", "Done"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        # Session-only threshold adjustment (t, then arrows). Never written
        # to settings.json — same memory-only precedent as the dry-run
        # toggle. ``_configured_threshold`` is the mount-time file value the
        # screen reverts to on exit; ``_entry_threshold`` is the value when
        # adjust mode was entered (wake/log only on a net change).
        self._adjusting = False
        self._configured_threshold: float | None = None
        self._entry_threshold: float | None = None
        # Another process holds the engine lease: show, never run, an engine.
        self._viewer = False
        # maximize: the four soft/hard thresholds, stepped with t/←/→ and
        # written to settings.json on enter. ``_mx_entry`` is what escape
        # restores and what the save diff is computed against.
        self._mx = None
        self._mx_entry = None
        self._knob = 0

    def compose(self) -> ComposeResult:
        yield AccountsPanel(show_minis=False, id="auto-active-panel")
        with Vertical(id="auto-top"):
            with Horizontal(id="auto-title-row"):
                yield Static(" DRY-RUN ", id="mode-badge", classes="dry")
                yield Static("", id="auto-summary")
            yield Static("", id="candidates")
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.app.set_store_only(True)
        self._settings = load_settings(self.app.switcher.backup_dir)
        if self._maximize:
            self._mx = load_maximize_settings(self.app.switcher.backup_dir)
            self.app.window_ticks = mxview.window_ticks(self._mx)
        else:
            self.app.window_ticks = None
        # The bar tick everywhere reads app.threshold_pct, loaded once at app
        # startup — sync it to the fresh file value so bars and engine agree,
        # and remember that value: unmount restores it (only the session
        # adjustment reverts, not this correction).
        self._configured_threshold = self._settings.threshold
        self.app.threshold_pct = self._settings.threshold
        self._update_summary()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", self._on_theme_change)
        host = getattr(self.app, "engine_host", None)
        if host is not None and host.running:
            self._attach_host(host)  # cc-swap: one engine per app (tui/engine_host.py)
        elif self.app.engine_keeper.claim():
            self._start_engine(dry_run=True)
        else:
            self._enter_viewer()

    def _attach_host(self, host) -> None:
        """cc-swap: show the engine Fleet runs here; never start a second."""
        self._host = host
        self._engine = host.engine
        log = self.query_one("#event-log", RichLog)
        palette = Palette.from_theme(self.app.current_theme)
        for event in list(host.events):
            log.write(event_text(event, palette=palette))
        log.write(Text("— attached to the engine this TUI runs —", style=palette.muted))
        host.subscribe(self._on_host_event)
        self._update_badge()

    def _on_host_event(self, event) -> None:
        self._engine = self._host.engine if self._host is not None else None
        if event is None:
            self._update_badge()
        else:
            self._on_engine_event(event)

    def on_unmount(self) -> None:
        if getattr(self, "_host", None) is not None:
            # cc-swap: the host's engine keeps running; Fleet owns it.
            self._host.unsubscribe(self._on_host_event)
            if self._configured_threshold is not None:
                self.app.threshold_pct = self._configured_threshold
            return
        if self._engine is not None:
            self._engine.stop()
        # Released now, or by the engine thread that finishes its tick last.
        self.app.engine_keeper.close()
        # A session threshold must not outlive the engine it steered: unpin
        # the poll planner and put the bar tick back on the file value.
        self.app.switcher.clear_poll_policy_inputs()
        if self._configured_threshold is not None:
            self.app.threshold_pct = self._configured_threshold
        self.app.set_store_only(False)

    def _on_theme_change(self, _theme: str) -> None:
        self._update_summary()
        self._update_badge()
        snap = self.app.snapshot
        if snap is not None:
            self._on_snapshot(snap)

    def action_back(self) -> None:
        if self._adjusting:
            if self._maximize:
                self._mx_cancel()
            else:
                self._end_adjust()
            return
        self.app.pop_screen()

    # -- threshold adjust mode ------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("threshold_step", "adjust_done") and not self._adjusting:
            return False  # hidden and inert until adjust mode is armed
        if self._viewer and action == "toggle_live":
            return False  # no engine here to take live
        if self._viewer and action == "adjust_threshold" and not self._maximize:
            # A session threshold would steer no engine. maximize thresholds
            # persist, so a viewer may still steer the engine that runs.
            return False
        return True

    def action_adjust_threshold(self) -> None:
        if self._maximize:
            self._mx_next_knob()
            return
        if self._adjusting:
            self._end_adjust()
            return
        self._adjusting = True
        self._entry_threshold = self._settings.threshold
        self._update_summary()
        self.refresh_bindings()

    def action_adjust_done(self) -> None:
        if not self._adjusting:
            return
        if self._maximize:
            self._mx_save()
        else:
            self._end_adjust()

    def action_threshold_step(self, delta: float) -> None:
        if not self._adjusting:
            return
        if self._maximize:
            self._mx_step(delta)
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        value = min(spec.hi, max(spec.lo, self._settings.threshold + delta))
        self._set_threshold(value)

    def _end_adjust(self) -> None:
        self._adjusting = False
        self._update_summary()
        self.refresh_bindings()
        if self._settings.threshold == self._entry_threshold:
            return  # no net change: nothing to announce, no tick to force
        if self._engine is not None:
            self._engine.wake()  # show a decision at the new value now
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— threshold set to {pct_label(self._settings.threshold)}% "
                "for this session —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _set_threshold(self, value: float) -> None:
        if value == self._settings.threshold:
            return
        self._settings = replace(self._settings, threshold=value)
        if self._engine is not None:
            self._engine.apply_threshold(value)
        self.app.threshold_pct = value
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        if self._maximize and self._mx is not None:
            self.query_one("#auto-summary", Static).update(self._mx_summary(palette))
            return
        text = Text()
        text.append("auto-switch · ")
        text.append(
            f"threshold {pct_label(self._settings.threshold)}%",
            style=palette.accent if self._adjusting else "",
        )
        if self._settings.threshold != self._configured_threshold:
            text.append(" (session)", style=palette.muted)
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one("#auto-summary", Static).update(text)

    # -- engine -------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        engine = AutoSwitchEngine(
            self.app.switcher,
            self._settings,
            self._emit_from_thread,
            dry_run=dry_run,
        )
        self._engine = engine
        keeper = self.app.engine_keeper
        keeper.engine_started()
        self.run_worker(
            partial(_run_engine_holding, engine, keeper),
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"auto-engine-{'dry' if dry_run else 'live'}",
        )
        self._update_badge()
        log = self.query_one("#event-log", RichLog)
        mode = "DRY-RUN (watching only)" if dry_run else "LIVE (will switch accounts)"
        log.write(
            Text(
                f"— engine started: {mode} —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            # App/screen tearing down mid-tick; the event has nowhere to go.
            pass

    def _enter_viewer(self) -> None:
        """Another process owns auto-switching: watch it, never run one."""
        self._viewer = True
        self._update_badge()
        self.refresh_bindings()
        pid = self.app.engine_keeper.lease.holder_pid()
        who = f" (pid {pid})" if pid else ""
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— another cc-swap engine is running{who}: this screen only "
                "watches. Stop that engine and reopen this screen to run one "
                "here —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        if not self.is_attached:
            return
        palette = Palette.from_theme(self.app.current_theme)
        self.query_one("#event-log", RichLog).write(event_text(event, palette=palette))
        if event.kind == "switch":
            self.app.request_refresh()

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    "Go live? claude-swap will switch your active account "
                    "automatically when the threshold is reached.\n\n"
                    "(Same behavior as running `cswap auto` in a terminal.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)

    def _restart_engine(self, *, dry_run: bool) -> None:
        if getattr(self, "_host", None) is not None:  # cc-swap: the host's engine
            self._host.set_dry_run(dry_run)
            self._engine = self._host.engine
            self._update_badge()
            return
        if self._engine is not None:
            self._engine.stop()
        self._start_engine(dry_run=dry_run)

    def _update_badge(self) -> None:
        badge = self.query_one("#mode-badge", Static)
        if self._viewer:
            badge.update(" VIEWER ")
            badge.set_classes("viewer")
        elif self._engine is not None and not self._engine.dry_run:
            badge.update(" LIVE ")
            badge.set_classes("live")
        else:
            badge.update(" DRY-RUN ")
            badge.set_classes("dry")

    # -- candidates -----------------------------------------------------------

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        if self._maximize and self._mx is not None:
            body = self._maximize_text(snap)
        else:
            body = self._candidates_text(snap, active_number=snap.active_number)
        self.query_one("#candidates", Static).update(body)

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets ranked by remaining headroom (best first)."""
        # Same window set as the engine (autoswitch.model included), so the
        # displayed ranking can never disagree with the account it picks.
        palette = Palette.from_theme(self.app.current_theme)
        models = parse_model_names(self._settings.model) if self._settings else ()
        ranked: list[tuple[float, str]] = []  # (sort key: pct used, number)
        lines: dict[str, Text] = {}
        for acc in snap.accounts:
            if acc.number == active_number or not acc.switchable:
                continue
            pct = binding_pct(acc.usage.last_good, models)
            entry = Text()
            entry.append("\n  ", style=palette.foreground)
            entry.append(acc.email, style=palette.foreground)
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=palette.muted
                )
                ranked.append((998.0, acc.number))
            elif pct is None:
                entry.append("  usage unknown", style=palette.muted)
                ranked.append((999.0, acc.number))
            else:
                entry.append(f"  {pct:3.0f}% used", style=palette.severity(pct))
                ranked.append((pct, acc.number))
            lines[acc.number] = entry

        text = Text()
        text.append("Next best", style=palette.muted)
        if not ranked:
            text.append("\n  no other switchable accounts", style=palette.muted)
            return text
        for _pct, number in sorted(ranked):
            text.append(lines[number])
        return text

    # -- maximize ----------------------------------------------------------------

    @property
    def _maximize(self) -> bool:
        return self._settings is not None and self._settings.strategy == "maximize"

    def _mx_next_knob(self) -> None:
        """t: arm adjust mode on 5h soft, then cycle 5h hard → 7d soft → 7d hard."""
        if not self._adjusting:
            # Diff against the file as it is now, not as it was at mount: a
            # `cc-swap config set` made meanwhile must survive our save.
            self._mx = load_maximize_settings(self.app.switcher.backup_dir)
            self._mx_entry = self._mx
            self._knob = 0
            self._adjusting = True
        else:
            self._knob = (self._knob + 1) % len(mxview.KNOBS)
        self._mx_refresh()
        self.refresh_bindings()

    def _mx_step(self, delta: float) -> None:
        stepped = mxview.step_knob(self._mx, mxview.KNOBS[self._knob], delta)
        if stepped != self._mx:
            self._mx = stepped
            self._mx_refresh()

    def _mx_cancel(self) -> None:
        self._adjusting = False
        self._mx = self._mx_entry
        self._mx_refresh()
        self.refresh_bindings()

    def _mx_save(self) -> None:
        root = self.app.switcher.backup_dir
        writes = mxview.threshold_writes(self._mx_entry, self._mx)
        self._adjusting = False
        failure: Exception | None = None
        try:
            for key, value in writes:
                set_setting(root, key, str(value))
        except Exception as exc:  # validation or I/O: report, never crash the UI
            failure = exc
        # The file is the truth either way; a partial save shows as one.
        self._mx = load_maximize_settings(root)
        self._mx_refresh()
        self.refresh_bindings()
        if failure is not None:
            self.notify(f"Could not save thresholds: {failure}", severity="error")
            return
        if not writes:
            return
        if self._engine is not None:
            self._engine.wake()  # it re-reads settings.json at the top of the tick
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— thresholds saved: {mxview.format_thresholds(self._mx)} —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _mx_refresh(self) -> None:
        """Push the current (maybe unsaved) thresholds to every surface."""
        self.app.window_ticks = mxview.window_ticks(self._mx)
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()
        snap = self.app.snapshot
        if snap is not None:
            self._on_snapshot(snap)

    def _mx_summary(self, palette: Palette) -> Text:
        text = Text("auto-switch · maximize")
        for window, pair in (("5h", ("soft_5h", "hard_5h")), ("7d", ("soft_7d", "hard_7d"))):
            text.append(f" · {window} ")
            for i, knob in enumerate(pair):
                if i:
                    text.append("/")
                selected = self._adjusting and mxview.KNOBS[self._knob] == knob
                text.append(
                    pct_label(getattr(self._mx, knob)),
                    style=palette.accent if selected else "",
                )
            text.append("%")
        if self._adjusting and self._mx != self._mx_entry:
            text.append(" (unsaved)", style=palette.muted)
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        if self._adjusting:
            label = mxview.KNOB_LABELS[mxview.KNOBS[self._knob]]
            text.append(
                f"   {label}: ← → adjust · t next · enter save · esc cancel",
                style=palette.muted,
            )
        return text

    def _maximize_text(self, snap: AccountsSnapshot) -> Text:
        """Every account as maximize ranks it, plus the decision line (the coded
        hold's reason, else the pending-switch line).

        Built from the store snapshot and the state file the engine itself
        reads, so it reads the same whether the engine runs here or elsewhere:
        Fleet's snapshot (``fleet.fleet_snapshot``) — the readings the engine
        trusts, slots without a usable backup and shared logins set aside.
        """
        palette = Palette.from_theme(self.app.current_theme)
        now = time.time()
        text = Text()
        text.append("Maximize — tier · score · landable · 5h window", style=palette.muted)
        try:
            root = self.app.switcher.backup_dir
            state = mxview.read_state(root)
            msnap = mxfleet.fleet_snapshot(
                snap, self._mx, state, now=now, history=mxview.read_history(root, now)
            )
            ranked = mxview.rows(msnap, state.primes)
            waiting = mxview.pending(msnap)
            decided = mxfleet.decision_view(
                state, msnap, now=now, poll_s=self._settings.interval_seconds
            )
        except Exception as exc:  # a display aid must never take the screen down
            text.append(f"\n  unavailable: {exc}", style=palette.muted)
            return text
        from claude_swap.maximize.names import display_names

        names = display_names(
            (a.number, a.email, a.alias, a.org_name if a.org_uuid else "") for a in snap.accounts
        )
        for row in ranked:
            line = Text()
            line.append(
                f"\n{'●' if row.active else ' '}  ",
                style=palette.accent if row.active else palette.foreground,
            )
            line.append(f"{names.get(row.number, row.email):<24.24}  ", style=palette.foreground)
            line.append(
                f"{mxview.TIER_LABELS.get(row.tier, row.tier):<11}  ",
                style=palette.foreground if row.tier == "normal" else palette.muted,
            )
            line.append(f"{mxview.format_score(row.score):>5}  ", style=palette.foreground)
            line.append(
                f"{'yes' if row.landable else 'no':<3}  ",
                style=palette.sev_ok if row.landable else palette.muted,
            )
            line.append(mxview.format_state5(row), style=palette.muted)
            text.append(line)
        if decided.code:
            # A coded hold (reset-wait, preempt, rebalance-deferred, account
            # hold) says why in its own words: the decision's reason, not a
            # "waiting for idle" line derived from the soft mark alone.
            text.append("\n")
            text.append(decided.reason, style=palette.sev_warn)
        elif waiting is not None:
            text.append("\n")
            text.append(mxview.format_pending(waiting), style=palette.sev_warn)
        return text
