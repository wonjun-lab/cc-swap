"""cc-swap CLI surface: `auto --strategy maximize` flags and `last-resort`."""

from __future__ import annotations

import inspect
import json
import sys
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome
from claude_swap.settings import (
    load_maximize_settings,
    set_setting,
    settings_path,
)
from claude_swap.switcher import ClaudeAccountSwitcher


class FakeEngine:
    """Stands in for AutoSwitchEngine; records what the CLI hands it."""

    instances: list = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False,
                 state_path=None, clock=None, maximize_cli=None):
        self.settings = settings
        self.dry_run = dry_run
        self.maximize_cli = maximize_cli
        type(self).instances.append(self)

    def tick(self):
        return TickOutcome.NO_ACTION

    def run_loop(self):
        return 0

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_fake():
    FakeEngine.instances = []


def _auto(argv: list[str]) -> int:
    with patch("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["cc-swap", "auto", *argv]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
    return excinfo.value.code


def _backup_root():
    return ClaudeAccountSwitcher().backup_dir


class TestAutoMaximize:
    def test_strategy_maximize_is_accepted_and_reaches_the_engine(self, temp_home):
        assert _auto(["--once", "--strategy", "maximize"]) == 2
        engine = FakeEngine.instances[-1]
        assert engine.settings.strategy == "maximize"
        assert engine.maximize_cli is not None

    def test_threshold_flags_are_handed_to_the_engine(self, temp_home):
        _auto(["--once", "--strategy", "maximize", "--soft5h", "40", "--hard7d", "97"])
        flags = FakeEngine.instances[-1].maximize_cli
        assert (flags.soft5h, flags.hard5h, flags.soft7d, flags.hard7d) == (
            40.0, None, None, 97.0,
        )

    def test_strategy_from_settings_json_also_takes_the_flags(self, temp_home):
        root = _backup_root()
        root.mkdir(parents=True, exist_ok=True)
        set_setting(root, "autoswitch.strategy", "maximize")
        assert _auto(["--once", "--soft7d", "85"]) == 2
        assert FakeEngine.instances[-1].maximize_cli.soft7d == 85.0

    def test_threshold_flags_without_maximize_are_rejected(self, temp_home, capsys):
        assert _auto(["--once", "--soft5h", "40"]) == 2
        assert "--soft5h only apply to the maximize strategy" in capsys.readouterr().err
        assert FakeEngine.instances == []

    def test_soft_above_hard_exits_1(self, temp_home, capsys):
        assert _auto(["--once", "--strategy", "maximize", "--soft5h", "97"]) == 1
        assert "maximize.soft5h (97) must not exceed maximize.hard5h (95)" in (
            capsys.readouterr().err
        )
        assert FakeEngine.instances == []

    def test_banner_shows_maximize_marks(self, temp_home, capsys):
        root = _backup_root()
        root.mkdir(parents=True, exist_ok=True)
        set_setting(root, "maximize.soft7d", "85")
        assert _auto(["--strategy", "maximize", "--soft5h", "40"]) == 0
        out = capsys.readouterr().out
        assert (
            "Auto-switch running: strategy maximize, 5h soft 40% / hard 95%, "
            "7d soft 85% / hard 98%, every 60s"
        ) in out

    def test_banner_for_other_strategies_is_unchanged(self, temp_home, capsys):
        assert _auto([]) == 0
        assert "Auto-switch running: threshold 90%, every 60s" in capsys.readouterr().out
        assert FakeEngine.instances[-1].maximize_cli is None

    def test_help_lists_the_flags(self, capsys):
        with patch.object(sys, "argv", ["cc-swap", "auto", "--help"]):
            with pytest.raises(SystemExit):
                cli.main()
        out = capsys.readouterr().out
        for flag in ("--soft5h", "--hard5h", "--soft7d", "--hard7d", "maximize"):
            assert flag in out

    def test_real_engine_accepts_the_maximize_cli_keyword(self):
        param = inspect.signature(AutoSwitchEngine.__init__).parameters["maximize_cli"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None
