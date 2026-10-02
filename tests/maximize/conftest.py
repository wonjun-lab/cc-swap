"""Fork-test safety net: the Fleet screen probes the cc-swap service with
``launchctl``/``systemctl``. No test may run those against the real
machine; tests that need a service state patch ``service_status`` again."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_service_probe(monkeypatch):
    monkeypatch.setattr("claude_swap.tui.fleet.service_status", lambda: None)
