"""The ``cc-swap auto off`` flag file: ``<backup root>/auto_off.json``.

``cc-swap auto off`` is a standing choice of the user, so it must not depend
on ``autoswitch_state.json`` being parseable: the engine reads that file as
``{}`` when it is damaged and rewrites it a tick later, which used to switch
automatic switching back on without anyone asking. The flag is therefore also
kept in this small file, written only by ``cc-swap auto off`` / ``auto on``
(``pause.set_auto_off``)::

    {"schemaVersion": 1, "autoOff": {"since": 1.7e9, "by": "cli", "host": "mbp"}}

Semantics, read here and nowhere else:

* No file: this file says nothing (the ``autoOff`` key in the state file, which
  older builds wrote alone, still counts).
* A file that exists means OFF, whatever it holds: an unreadable or damaged
  flag file reads as off, because failing open would switch against the
  user's wish. The marker's details (``since``/``by``/``host``) are used when
  they can be read.

Dependency-free on purpose: the engine, the CLI and the Fleet read model all
import it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

FLAG_FILENAME = "auto_off.json"
FLAG_KEY = "autoOff"


def flag_path(root: Path) -> Path:
    return Path(root) / FLAG_FILENAME


def read_flag(root: Path) -> Mapping | None:
    """None when there is no flag file (this file says nothing); else the
    marker mapping, empty when the file exists but cannot be read."""
    path = flag_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):  # unreadable, not JSON, not UTF-8
        return {}
    marker = raw.get(FLAG_KEY) if isinstance(raw, dict) else None
    return marker if isinstance(marker, Mapping) else {}
