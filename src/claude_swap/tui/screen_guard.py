"""One guard for every screen: a key, selection or callback that arrives after
its screen has gone (two keys queued in one burst, a stale timer, a buffered
keystroke after an in-app login) must do nothing.

Textual's ``dismiss`` / ``pop_screen`` pop whatever is on top of the stack, so
a second call pops the screen *underneath* — or raises ``ScreenStackError``
with one screen left. ``TopOnly`` makes both no-ops unless the screen is the
active one.
"""

from __future__ import annotations

import logging

from textual.await_complete import AwaitComplete

log = logging.getLogger(__name__)


class TopOnly:
    """Mixin (list it BEFORE ``Screen``/``ModalScreen``) for idempotent
    ``dismiss`` and ``pop_self``.

    "Active" is not "``app.screen`` agrees right now": a legitimate dismiss
    must never be dropped because Textual is mid-transition or the stack
    cannot be read at that instant. Only two things are refused: a second
    dismiss of a screen the first one already dismissed (``_dismissed``, set
    by the first dismiss and cleared if the screen is pushed again — Fleet is
    an installed screen that is re-pushed), and a dismiss of a screen that is
    plainly not the top of a readable stack (gone, or covered by another
    screen — Textual would pop *that* one instead).
    """

    _dismissed = False

    def _stack_state(self) -> str:
        """``top`` / ``covered`` / ``gone``, or ``unknown`` when the stack
        cannot be read (no app yet or any more)."""
        try:
            stack = self.app.screen_stack
        except Exception:
            return "unknown"
        if self not in stack:
            return "gone"
        return "top" if stack[-1] is self and len(stack) > 1 else "covered"

    @property
    def is_top(self) -> bool:
        """May this screen act now? False once it has been dismissed or is
        plainly not the top; an unreadable stack counts as alive."""
        state = self._stack_state()
        if state == "top":
            self._dismissed = False  # pushed again after an earlier dismiss
            return True
        return state == "unknown" and not self._dismissed

    def dismiss(self, result=None):
        if not self.is_top:
            log.debug("ignored dismiss of %s: already dismissed or not on top", type(self).__name__)
            return AwaitComplete.nothing()
        self._dismissed = True
        return super().dismiss(result)

    def pop_self(self) -> None:
        """Pop this screen — only when it is the active one."""
        if self.is_top:
            self._dismissed = True
            self.app.pop_screen()
        else:
            log.debug("ignored pop of %s: already dismissed or not on top", type(self).__name__)
