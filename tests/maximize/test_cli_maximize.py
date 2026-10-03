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
        assert "--soft5h only applies to the maximize strategy" in capsys.readouterr().err
        assert FakeEngine.instances == []

    def test_several_flags_without_maximize_keep_the_plural(self, temp_home, capsys):
        assert _auto(["--once", "--soft5h", "40", "--hard5h", "90"]) == 2
        assert "--soft5h, --hard5h only apply to the maximize strategy" in (
            capsys.readouterr().err
        )

    def test_soft_above_hard_exits_1(self, temp_home, capsys):
        assert _auto(["--once", "--strategy", "maximize", "--soft5h", "97"]) == 1
        assert "maximize.soft5h (97) must not exceed maximize.hard5h (95)" in (
            capsys.readouterr().err
        )
        assert FakeEngine.instances == []

    @pytest.mark.parametrize("flag", ["--soft5h", "--hard5h", "--soft7d", "--hard7d"])
    @pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity", "1e400"])
    def test_non_finite_flag_values_are_rejected_by_argparse(
        self, temp_home, capsys, flag, value
    ):
        # Rejected before merge_maximize_cli ever sees them: argparse exits 2
        # with a usage error, no engine is built.
        assert _auto(["--once", "--strategy", "maximize", f"{flag}={value}"]) == 2
        err = capsys.readouterr().err
        assert flag in err
        assert "finite number" in err
        assert FakeEngine.instances == []

    @pytest.mark.parametrize("flag", ["--soft5h", "--hard5h", "--soft7d", "--hard7d"])
    def test_non_numeric_flag_values_are_still_rejected(self, temp_home, capsys, flag):
        assert _auto(["--once", "--strategy", "maximize", flag, "high"]) == 2
        assert flag in capsys.readouterr().err
        assert FakeEngine.instances == []

    @pytest.mark.parametrize("flag", ["--soft5h", "--hard5h", "--soft7d", "--hard7d"])
    @pytest.mark.parametrize("value", ["150", "100", "99.95", "0", "0.5", "-5"])
    def test_out_of_range_flag_values_are_rejected_at_parse_time(
        self, temp_home, capsys, flag, value
    ):
        # Not clamped and then reported as a soft/hard conflict: the message
        # names the flag, the value and the accepted range.
        assert _auto(["--once", "--strategy", "maximize", f"{flag}={value}"]) == 2
        err = capsys.readouterr().err
        assert flag in err
        assert "between 1 and 99.9" in err
        assert value in err
        assert "must not exceed" not in err
        assert FakeEngine.instances == []

    @pytest.mark.parametrize("value", ["1", "99.9", "50"])
    def test_range_ends_are_accepted_by_the_type(self, value):
        from claude_swap import cli

        assert cli._mark_pct("--soft5h")(value) == float(value)

    def test_finite_flag_values_still_parse_as_floats(self, temp_home):
        _auto(["--once", "--strategy", "maximize", "--soft5h", "40", "--hard7d", "97.5"])
        flags = FakeEngine.instances[-1].maximize_cli
        assert flags.soft5h == 40.0
        assert flags.hard7d == 97.5

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


class TestLastResort:
    def _seed(self, *, shared_email: bool = False):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        team_email = "work@co.com" if shared_email else "team@co.com"
        data["accounts"]["2"] = {
            "email": "work@co.com", "uuid": "u2", "organizationUuid": "",
            "organizationName": "", "added": "2024-01-01T00:00:00Z",
        }
        data["accounts"]["3"] = {
            "email": team_email, "uuid": "u3", "organizationUuid": "org-3",
            "organizationName": "Team", "added": "2024-01-01T00:00:00Z",
            "alias": "team",
        }
        data["sequence"] = [2, 3]
        switcher._write_json(switcher.sequence_file, data)
        return switcher

    def _cmd(self, argv: list[str]) -> int:
        with patch("os.geteuid", return_value=1000, create=True):
            try:
                cli._last_resort_command(argv)
            except SystemExit as e:
                return e.code or 0
        return 0

    def _stored(self):
        return load_maximize_settings(_backup_root()).last_resort

    def test_add_by_number_stores_the_email(self, temp_home, capsys):
        self._seed()
        assert self._cmd(["add", "3"]) == 0
        assert self._stored() == "team@co.com"
        assert "Marked" in capsys.readouterr().out

    def test_add_by_alias_stores_the_email(self, temp_home):
        self._seed()
        self._cmd(["add", "team"])
        assert self._stored() == "team@co.com"

    def test_add_appends_and_skips_duplicates(self, temp_home, capsys):
        self._seed()
        self._cmd(["add", "3"])
        self._cmd(["add", "work@co.com"])
        assert self._stored() == "team@co.com,work@co.com"
        capsys.readouterr()
        assert self._cmd(["add", "team"]) == 0
        assert "already last-resort" in capsys.readouterr().out
        assert self._stored() == "team@co.com,work@co.com"

    def test_add_hints_when_strategy_is_not_maximize(self, temp_home, capsys):
        self._seed()
        self._cmd(["add", "3"])
        assert "Takes effect with the maximize strategy (now best)" in (
            capsys.readouterr().out
        )

    def test_no_hint_under_maximize(self, temp_home, capsys):
        self._seed()
        set_setting(_backup_root(), "autoswitch.strategy", "maximize")
        self._cmd(["add", "3"])
        assert "Takes effect" not in capsys.readouterr().out

    def test_shared_email_stores_the_alias(self, temp_home):
        self._seed(shared_email=True)
        self._cmd(["add", "3"])
        assert self._stored() == "team"

    def test_shared_email_without_alias_is_refused(self, temp_home, capsys):
        switcher = self._seed(shared_email=True)
        data = switcher._get_sequence_data()
        del data["accounts"]["3"]["alias"]
        switcher._write_json(switcher.sequence_file, data)
        assert self._cmd(["add", "3"]) == 1
        assert "give Account-3 an alias first" in capsys.readouterr().err
        assert self._stored() is None

    def test_remove_last_entry_unsets_the_key(self, temp_home):
        self._seed()
        self._cmd(["add", "3"])
        assert self._cmd(["remove", "team"]) == 0
        raw = json.loads(settings_path(_backup_root()).read_text())
        assert "maximize" not in raw

    def test_remove_keeps_other_entries(self, temp_home):
        self._seed()
        self._cmd(["add", "3"])
        self._cmd(["add", "2"])
        self._cmd(["remove", "3"])
        assert self._stored() == "work@co.com"

    def test_remove_drops_a_hand_written_alias_entry(self, temp_home):
        self._seed()
        set_setting(_backup_root(), "maximize.lastResort", "TEAM")
        self._cmd(["remove", "3"])
        assert self._stored() is None

    def test_remove_shared_email_entry_warns_about_the_other_account(
        self, temp_home, capsys
    ):
        self._seed(shared_email=True)
        set_setting(_backup_root(), "maximize.lastResort", "work@co.com")
        self._cmd(["remove", "2"])
        assert self._stored() is None
        assert "Also returned Account-3" in capsys.readouterr().out

    def test_remove_clears_an_entry_that_names_no_account(self, temp_home, capsys):
        # Left by an older `remove N`: resolving the email failed, so it
        # could never be cleared from the command line.
        self._seed()
        set_setting(_backup_root(), "maximize.lastResort", "team@co.com,ghost@x.com")
        assert self._cmd(["remove", "ghost@x.com"]) == 0
        assert self._stored() == "team@co.com"
        assert "named no account" in capsys.readouterr().out

    def test_remove_unmarked_account_is_a_noop(self, temp_home, capsys):
        self._seed()
        assert self._cmd(["remove", "2"]) == 0
        assert "is not last-resort" in capsys.readouterr().out

    def test_list_shows_entries_and_their_accounts(self, temp_home, capsys):
        self._seed()
        set_setting(_backup_root(), "maximize.lastResort", "team@co.com,ghost@x.com")
        assert self._cmd(["list"]) == 0
        out = capsys.readouterr().out
        assert "team@co.com → Account-3" in out
        assert "ghost@x.com → (no matching account)" in out

    def test_bare_command_lists(self, temp_home, capsys):
        self._seed()
        assert self._cmd([]) == 0
        assert "No last-resort accounts" in capsys.readouterr().out

    def test_unknown_account_exits_1(self, temp_home, capsys):
        self._seed()
        assert self._cmd(["add", "9"]) == 1
        assert "Error" in capsys.readouterr().err

    def test_missing_account_argument_is_a_usage_error(self, temp_home):
        self._seed()
        assert self._cmd(["add"]) == 2

    def test_dispatched_from_main(self, temp_home):
        with patch("claude_swap.cli._last_resort_command") as fn, \
             patch.object(sys, "argv", ["cc-swap", "last-resort", "add", "3"]):
            cli.main()
        fn.assert_called_once_with(["add", "3"])

    def test_main_help_lists_the_fork_commands(self, capsys):
        with patch.object(sys, "argv", ["cc-swap", "--help"]):
            with pytest.raises(SystemExit):
                cli.main()
        out = capsys.readouterr().out
        assert "last-resort add|remove <a>" in out
        assert "auto --strategy maximize" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="root guard is POSIX-only")
    def test_refuses_root(self, temp_home, capsys):
        self._seed()
        with patch("os.geteuid", return_value=0, create=True), \
             patch.object(ClaudeAccountSwitcher, "_is_running_in_container",
                          return_value=False):
            with pytest.raises(SystemExit) as exc:
                cli._last_resort_command(["add", "3"])
        assert exc.value.code == 1
