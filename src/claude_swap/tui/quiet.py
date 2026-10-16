"""Quiet repaints: a widget is redrawn only when what it shows changed.

The poll loop re-renders every surface each tick. Hosts that persist
terminal scrollback turn each repaint into disk writes, so an idle window
must emit no output at all between polls. ``Static.update`` is the one
choke point nearly every surface goes through; ``install`` makes it a no-op
for content equal to what the widget already shows. Widgets that render in
``render()`` use ``changed`` to decide whether to ``refresh``.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

_installed = False


def same_content(old: object, new: object) -> bool:
    """Whether two pieces of widget content draw identically."""
    if isinstance(old, Text) and isinstance(new, Text):
        return (
            old.plain == new.plain
            and old.style == new.style
            and old.spans == new.spans
            and old.justify == new.justify
            and old.no_wrap == new.no_wrap
            and old.overflow == new.overflow
        )
    if isinstance(old, str) and isinstance(new, str):
        return old == new
    return False


def changed(cache: dict, key: str, value: object) -> bool:
    """Record ``value`` under ``key``; True when it differs from the last."""
    old = cache.get(key)
    cache[key] = value
    return old is None or not same_content(old, value)


def install() -> None:
    """Make ``Static.update`` skip identical content (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True
    original = Static.update

    def update(self, content="", *, layout=True):
        if same_content(self.content, content):
            return
        original(self, content, layout=layout)

    Static.update = update  # type: ignore[method-assign]
