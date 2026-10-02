"""The one in-process auto-switch engine a CswapApp may run (cc-swap fork).

Fleet's ``Mode`` runs an engine here only on request; the auto screen
(``g``/``e``) attaches to it instead of starting a second engine — the
lease alone cannot stop that, because ``should_run_engine`` is true again
for a lease this process already holds. The host owns the engine object,
its worker, the dry-run flag and a ring buffer of recent events, and it
wraps the app's existing ``LeaseKeeper`` and the auto screen's
``_run_engine_holding`` worker body.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from claude_swap.autoswitch import AutoSwitchEvent, MaximizeDecisionEvent
from claude_swap.settings import load_settings
from claude_swap.tui import autoview

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

RING_SIZE = 500

Subscriber = Callable[[AutoSwitchEvent], None]


class EngineHost:
    def __init__(self, app: "CswapApp") -> None:
        self.app = app
        self.engine = None
        self.started_by: str | None = None
        self.events: deque[AutoSwitchEvent] = deque(maxlen=RING_SIZE)
        self.last_decision: MaximizeDecisionEvent | None = None
        self.last_decision_at: float | None = None
        self._subscribers: list[Subscriber] = []

    @property
    def running(self) -> bool:
        return self.engine is not None

    @property
    def dry_run(self) -> bool:
        return self.engine is None or bool(self.engine.dry_run)

    # -- lifecycle --------------------------------------------------------------

    def start(self, *, dry_run: bool, by: str) -> bool:
        """Run the engine here; False when another process holds the lease.
        A running engine is kept (use :meth:`set_dry_run` to change it)."""
        if self.engine is not None:
            return True
        keeper = self.app.engine_keeper
        if not keeper.claim():
            return False
        self._launch(dry_run=dry_run, by=by)
        return True

    def _launch(self, *, dry_run: bool, by: str) -> None:
        keeper = self.app.engine_keeper
        # Through the autoview module attribute, so the TUI tests' fake engine
        # (patched there) stands in here too.
        engine = autoview.AutoSwitchEngine(
            self.app.switcher,
            load_settings(self.app.switcher.backup_dir),
            self._emit_from_thread,
            dry_run=dry_run,
        )
        self.engine = engine
        self.started_by = by
        self.last_decision = None
        keeper.engine_started()
        self.app.run_worker(
            partial(autoview._run_engine_holding, engine, keeper),
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"host-engine-{'dry' if dry_run else 'live'}",
        )
        self._notify(None)

    def set_dry_run(self, dry_run: bool) -> None:
        """Restart the running engine in the other mode; the lease stays held
        (the old engine thread counts against it until its tick returns)."""
        if self.engine is None or bool(self.engine.dry_run) == dry_run:
            return
        by = self.started_by or "fleet"
        self.engine.stop()
        self.engine = None
        self._launch(dry_run=dry_run, by=by)

    def stop(self, by: str | None = None) -> None:
        """Stop the engine (only the starter's, when ``by`` is given) and
        hand the lease release to whichever engine thread finishes last."""
        if self.engine is None or (by is not None and by != self.started_by):
            return
        self.engine.stop()
        self.engine = None
        self.started_by = None
        self.app.engine_keeper.close()
        try:
            self.app.switcher.clear_poll_policy_inputs()
        except Exception:
            pass
        self._notify(None)

    def wake(self) -> None:
        if self.engine is not None:
            self.engine.wake()

    # -- events -------------------------------------------------------------------

    def subscribe(self, callback: Subscriber) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Subscriber) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_event, event)
        except Exception:
            pass  # app tearing down mid-tick; the event has nowhere to go

    def _on_event(self, event: AutoSwitchEvent) -> None:
        self.events.append(event)
        if isinstance(event, MaximizeDecisionEvent):
            self.last_decision, self.last_decision_at = event, time.time()
        if event.kind == "switch":
            self.app.request_refresh()
        self._notify(event)

    def _notify(self, event: AutoSwitchEvent | None) -> None:
        """Subscribers get every event; ``None`` means started/stopped."""
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:
                pass
