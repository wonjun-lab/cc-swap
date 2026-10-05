"""The engine's human event lines name an account by its display name (alias,
else the part before the ``@``, maximize/names.py) — never its slot number
(an internal id that differs per machine) nor its address (``auto.log`` is
read over shoulders and pasted into issues). ``cc-swap auto`` installs the
name hook under every strategy. JSON output keeps the number and address."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from claude_swap import autoswitch as aw
from claude_swap import cli
from claude_swap.autoswitch import (
    LoginAdoptedEvent,
    MaximizeDecisionEvent,
    PollEvent,
    PrimeEvent,
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


def test_without_a_hook_a_line_says_the_local_part():
    line = _poll().human()
    assert line.startswith("side.user: 13% used")
    assert "Account-" not in line and "@" not in line
    assert "Switched main.user -> side.user (soft5h)" in _switch().human()


def test_hook_names_every_line():
    with aw.account_names(_name):
        poll = _poll().human()
        assert poll.startswith("work: 13% used")
        assert poll.endswith("| others: main 20%")
        assert "Switched main -> work (soft5h)" in _switch().human()
        quarantined = QuarantineEvent(
            number="3", email="carol@example.com", reason="dead"
        ).human()
        assert quarantined.startswith("carol quarantined: dead.")
        assert "re-login carol: cc-swap login carol" in quarantined
        assert "carol back in rotation" in UnquarantineEvent(
            number="3", email="carol@example.com"
        ).human()
        assert LoginAdoptedEvent(number="1").human() == "adopted new login for main"
        assert PrimeEvent("2", "primed", None).human() == "work: 5h window primed"
    # The hook is scoped: afterwards the local part is back.
    assert _poll().human().startswith("side.user:")


def test_no_line_names_a_slot_number():
    with aw.account_names(_name):
        lines = [
            _poll().human(),
            _switch().human(),
            LoginAdoptedEvent(number="2").human(),
            MaximizeDecisionEvent(
                active="2", decision="hold", trigger=None, reason="work under soft",
            ).human(),
        ]
    for line in lines:
        assert "Account-" not in line and "#1" not in line and "#2" not in line


def test_a_decision_line_names_the_active_account_once():
    with aw.account_names(_name):
        line = MaximizeDecisionEvent(
            active="2", decision="switch", trigger="soft",
            reason="work 5h 91% >= soft 90%; idle; -> main (normal, score 1.20)",
        ).human()
        assert line == (
            "maximize: switch (soft): work 5h 91% >= soft 90%; idle; "
            "-> main (normal, score 1.20)"
        )
        other = MaximizeDecisionEvent(
            active="2", decision="hold", trigger=None, reason="switching paused",
        ).human()
        assert other == "maximize: hold on work: switching paused"


def test_an_account_with_neither_alias_nor_address_is_its_slot():
    assert LoginAdoptedEvent(number="7").human() == "adopted new login for #7"


def test_hook_leaves_json_alone():
    plain = [_poll().to_json(), _switch().to_json()]
    with aw.account_names(_name):
        hooked = [_poll().to_json(), _switch().to_json()]
    for a, b in zip(plain, hooked):
        a.pop("ts"), b.pop("ts")
        assert a == b
    assert hooked[0]["active"]["email"] == SIDE
    assert hooked[0]["active"]["number"] == 2


def test_a_failing_hook_falls_back_to_the_local_part():
    def boom(number, email):
        raise RuntimeError("no")

    with aw.account_names(boom):
        assert _poll().human().startswith("side.user: 13% used")


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


def test_cli_maximize_prints_display_names(temp_home, capsys):
    root = ClaudeAccountSwitcher().backup_dir
    _fleet(root, side_alias="work")
    set_setting(root, "autoswitch.strategy", "maximize")
    assert _auto(["--once"]) == 2
    out = capsys.readouterr().out
    assert "work: 13% used" in out
    assert "Switched main.user -> work" in out
    assert "@example.com" not in out
    assert "Account-" not in out


def test_cli_maximize_falls_back_to_the_local_part(temp_home, capsys):
    root = ClaudeAccountSwitcher().backup_dir
    _fleet(root)
    set_setting(root, "autoswitch.strategy", "maximize")
    assert _auto(["--once"]) == 2
    assert "side.user: 13% used" in capsys.readouterr().out


def test_cli_other_strategies_name_accounts_too(temp_home, capsys):
    root = ClaudeAccountSwitcher().backup_dir
    _fleet(root, side_alias="work")
    assert _auto(["--once"]) == 2
    out = capsys.readouterr().out
    assert "work: 13% used" in out
    assert "@example.com" not in out
    assert "Account-" not in out


def test_cli_json_keeps_the_address_under_maximize(temp_home, capsys):
    root = ClaudeAccountSwitcher().backup_dir
    _fleet(root, side_alias="work")
    set_setting(root, "autoswitch.strategy", "maximize")
    assert _auto(["--once", "--json"]) == 2
    first = json.loads(capsys.readouterr().out.splitlines()[0])
    assert first["active"]["email"] == SIDE
