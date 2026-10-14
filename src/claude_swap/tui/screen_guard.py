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
    ``dismiss`` and ``pop_self``."""

    @property
    def is_top(self) -> bool:
        try:
            stack = self.app.screen_stack
        except Exception:  # no active app (already torn down)
            return False
        return bool(stack) and stack[-1] is self and len(stack) > 1

    def dismiss(self, result=None):
        if not self.is_top:
            log.debug("ignored dismiss of %s: not the active screen", type(self).__name__)
            return AwaitComplete.nothing()
        return super().dismiss(result)

    def pop_self(self) -> None:
        """Pop this screen — only when it is the active one."""
        if self.is_top:
            self.app.pop_screen()
        else:
            log.debug("ignored pop of %s: not the active screen", type(self).__name__)
