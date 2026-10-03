"""Under the maximize strategy the engine's human event lines name an account
by its short display name (alias, else the part before the ``@``), not its
address (``auto.log`` is read over shoulders and pasted into issues). JSON
output keeps the address."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from claude_swap import autoswitch as aw
from claude_swap import cli
from claude_swap.autoswitch import (
    PollEvent,
    QuarantineEvent,
    SwitchEvent,
    TickOutcome,
    UnquarantineEvent,
)
from claude_swap.settings import set_setting
from claude_swap.switcher import ClaudeAccountSwitcher

MAIN = "main.user@example.com"
SIDE = "side.user@example.com"


def _poll() -> PollEvent:
    return PollEvent(
        active={"number": 2, "email": SIDE},
        headroom={"1": 80.0, "2": 87.0},
        threshold=90.0,
    )


def _switch() -> SwitchEvent:
    return SwitchEvent(
        trigger="soft5h",
        from_ref={"number": 1, "email": MAIN},
        to_ref={"number": 2, "email": SIDE},
    )


def _name(number: str, email: str) -> str:
    return {"1": "main", "2": "work"}.get(number, email.split("@")[0])


def test_default_human_lines_still_print_the_address():
    assert f"Account-2 ({SIDE}): 13% used" in _poll().human()
    assert f"Account-1 -> Account-2 ({SIDE})" in _switch().human()


def test_hook_names_the_poll_switch_and_quarantine_lines():
    with aw.account_names(_name):
        assert "Account-2 (work): 13% used" in _poll().human()
        assert "Switched Account-1 -> Account-2 (work) (soft5h)" in _switch().human()
        assert "Account-3 (carol) quarantined" in QuarantineEvent(
            number="3", email="carol@example.com", reason="dead"
        ).human()
        assert "Account-3 (carol) back in rotation" in UnquarantineEvent(
            number="3", email="carol@example.com"
        ).human()
    # The hook is scoped: afterwards the address is back.
    assert SIDE in _poll().human()


def test_hook_leaves_json_alone():
    plain = [_poll().to_json(), _switch().to_json()]
    with aw.account_names(_name):
        hooked = [_poll().to_json(), _switch().to_json()]
    for a, b in zip(plain, hooked):
        a.pop("ts"), b.pop("ts")
        assert a == b
    assert hooked[0]["active"]["email"] == SIDE


def test_a_failing_hook_falls_back_to_the_address():
    def boom(number, email):
        raise RuntimeError("no")

    with aw.account_names(boom):
        assert SIDE in _poll().human()


class FakeEngine:
    """Records the CLI's event callback so a test can feed it events."""

    instances: list = []

    def __init__(self, switcher, settings, on_event, *, dry_run=False,
                 state_path=None, clock=None, maximize_cli=None):
        self.settings = settings
        self.on_event = on_event
        type(self).instances.append(self)

    def tick(self):
        for event in (_poll(), _switch()):
            self.on_event(event)
        return TickOutcome.NO_ACTION

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


def _fleet(root, side_alias: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps({"accounts": {
        "1": {"email": MAIN, "alias": ""},
        "2": {"email": SIDE, "alias": side_alias},
    }}))


def test_cli_maximize_prints_short_names(temp_home, capsys):
    root = ClaudeAccountSwitcher().backup_dir
    _fleet(root, side_alias="work")
    set_setting(root, "autoswitch.strategy", "maximize")
    assert _auto(["--once"]) == 2
    out = capsys.readouterr().out
    assert "Account-2 (work): 13% used" in out
    assert "Switched Account-1 -> Account-2 (work)" in out
    assert "@example.com" not in out


def test_cli_maximize_falls_back_to_the_local_part(temp_home, capsys):
    root = ClaudeAccountSwitcher().backup_dir
    _fleet(root)
    set_setting(root, "autoswitch.strategy", "maximize")
    assert _auto(["--once"]) == 2
    assert "Account-2 (side.user): 13% used" in capsys.readouterr().out


def test_cli_other_strategies_keep_the_address(temp_home, capsys):
    root = ClaudeAccountSwitcher().backup_dir
    _fleet(root, side_alias="work")
    assert _auto(["--once"]) == 2
    assert f"Account-2 ({SIDE}): 13% used" in capsys.readouterr().out


def test_cli_json_keeps_the_address_under_maximize(temp_home, capsys):
    root = ClaudeAccountSwitcher().backup_dir
    _fleet(root, side_alias="work")
    set_setting(root, "autoswitch.strategy", "maximize")
    assert _auto(["--once", "--json"]) == 2
    first = json.loads(capsys.readouterr().out.splitlines()[0])
    assert first["active"]["email"] == SIDE
