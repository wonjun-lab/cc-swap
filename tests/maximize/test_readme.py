"""The README's fork section must track SETTING_SPECS and the install story."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from claude_swap import cli
from claude_swap.settings import SETTING_SPECS, format_setting_value
from claude_swap.tui.app import CswapApp
from claude_swap.tui.fleet import FleetScreen

README = Path(__file__).resolve().parents[2] / "README.md"
FORK_KEYS = sorted(k for k, s in SETTING_SPECS.items() if s.section in ("maximize", "prime"))


def _table_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        cells = [c.strip().strip("`") for c in line.split("|")]
        if len(cells) >= 6 and (
            cells[1].startswith(("maximize.", "prime.")) or cells[1] == "autoswitch.strategy"
        ):
            rows[cells[1]] = cells
    return rows


def test_every_fork_key_is_registered():
    assert len(FORK_KEYS) == 19  # 14 maximize.* + 5 prime.*


@pytest.mark.parametrize("key", FORK_KEYS)
def test_readme_documents_each_fork_key_with_its_default(key):
    rows = _table_rows()
    assert key in rows, f"README settings table is missing {key}"
    default = SETTING_SPECS[key].default
    documented = rows[key][3]
    if default is None:
        assert documented in ("—", "auto")
    else:
        assert documented == format_setting_value(default)


def test_readme_strategy_row_mentions_maximize():
    assert "maximize" in _table_rows()["autoswitch.strategy"][4]


@pytest.mark.parametrize("snippet", [
    "uv tool install git+https://github.com/wonjun-lab/cc-swap",
    "uv tool uninstall claude-swap",
    "cc-swap config set autoswitch.strategy maximize",
    "cc-swap service install",
    "loginctl enable-linger",
    "cc-swap config set prime.enabled true",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "Terms of service",
    "cc-swap auto --once --dry-run",
    "### Logins expire",
    "refreshTokenExpiresAt",
    "maximize.loginExpiryGuardMin",
])
def test_readme_covers_install_migration_priming_and_the_service(snippet):
    assert snippet in README.read_text(encoding="utf-8")


def test_readme_describes_the_service_takeover_as_a_retry_not_a_wait_for_terminals():
    text = README.read_text(encoding="utf-8")
    # The service holds the lease while it runs, so a terminal engine is refused
    # (exit 4); it never "waits and takes over after you stop it".
    assert "the service waits and takes over" not in text
    assert "retries every minute and takes over once that engine stops" in text
    assert "run `cc-swap service uninstall` first" in text


@pytest.mark.parametrize("snippet", [
    "## Fleet: the TUI home for maximize",
    "`ctrl+f`",
    "cc-swap launches nothing itself",
    "it refuses a login that belongs to another slot",
    "`pausedUntil`",
    "CC_SWAP_FETCH_ON_OPEN=0",
])
def test_readme_documents_the_fleet_screen_and_relogin(snippet):
    assert snippet in README.read_text(encoding="utf-8")


# --- README <-> code contracts ----------------------------------------------
#
# These read the registries (``_FORK_COMMANDS``, the parsers, the Fleet
# bindings), so a command or key added later is held to the README as soon as
# it is registered. Since 0.3.0 this covers doctor, init, why, history and
# claude-update; `auto on|off|status`, `prime verify` and `upgrade --check`
# are not in ``_FORK_COMMANDS`` and are listed explicitly below.


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _snippets() -> list[str]:
    """Every inline-code ``cc-swap …`` snippet in the README."""
    return [m.group(1) for m in re.finditer(r"`(cc-swap [^`\n]+)`", _readme_text())]


def _help_output(handler, argv, capsys) -> str | None:
    """``--help`` text of ``handler(argv)``, or None if it does not print help."""
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        handler([*argv, "--help"])
    out = capsys.readouterr().out
    return out if excinfo.value.code in (0, None) and out else None


def _known_verbs() -> set[str]:
    main_source = inspect.getsource(cli.main)
    dispatched = set(re.findall(r'argv\[0\] == "([a-z][a-z-]*)"', main_source))
    dispatched |= set(re.findall(r'sys\.argv\[1\] == "([a-z][a-z-]*)"', main_source))
    return {*cli._FORK_COMMANDS, *cli._SUBCOMMAND_FLAGS, *dispatched, "switch", "help"}


@pytest.mark.parametrize("command", sorted(cli._FORK_COMMANDS))
def test_readme_documents_every_registered_fork_command(command):
    assert any(s.split()[1] == command for s in _snippets()), (
        f"README has no `cc-swap {command} …` snippet; document the command"
    )


def test_every_cc_swap_verb_in_the_readme_is_a_real_command():
    known = _known_verbs()
    unknown = sorted({s.split()[1] for s in _snippets() if not s.split()[1].startswith("-")} - known)
    assert not unknown, f"README names commands the CLI does not have: {unknown}"


def test_every_flag_on_a_documented_fork_command_exists_in_its_parser(capsys):
    handlers = {**{c: getattr(cli, n) for c, n in cli._FORK_COMMANDS.items()},
                "auto": cli._auto_command}
    problems: list[str] = []
    for snippet in _snippets():
        words = snippet.split()
        verb = words[1]
        flags = re.findall(r"--[a-z0-9][a-z0-9-]*", snippet)
        if verb not in handlers or not flags:
            continue
        helps = [_help_output(handlers[verb], [], capsys)]
        if len(words) > 2 and re.fullmatch(r"[a-z][a-z-]*", words[2]):
            helps.append(_help_output(handlers[verb], [words[2]], capsys))
        text = "\n".join(h for h in helps if h)
        problems += [f"`{snippet}`: {f} is not in `{verb}`'s help" for f in flags if f not in text]
    assert not problems, "\n".join(problems)


def test_every_config_key_the_readme_sets_exists():
    keys = set(re.findall(r"cc-swap config (?:set|get|unset) ([A-Za-z0-9.]+)", _readme_text()))
    assert keys, "expected README config examples"
    assert sorted(keys - set(SETTING_SPECS)) == []


def test_every_fleet_key_the_readme_names_is_bound():
    text = _readme_text()
    start = text.index("## Fleet: the TUI home for maximize")
    section = text[start : text.index("\n## ", start + 5)]
    named = set(re.findall(r"`((?:ctrl\+)?[a-z?])`", section))
    assert {"s", "m", "r", "ctrl+f"} <= named, "section parse found too little"
    from claude_swap.maximize import fleet as fx
    from claude_swap.tui.fleet_accounts import AccountsScreen

    bound = {"?" if k == "question_mark" else k for b in FleetScreen.BINDINGS for k in b.key.split(",")}
    bound |= {k for b in CswapApp.BINDINGS for k in b.key.split(",")}
    # Sub-screens the section documents: Account settings and the Mode modal.
    bound |= {k for b in AccountsScreen.BINDINGS for k in b.key.split(",")}
    bound |= {
        a.key
        for holder in ("none", "here-dry", "here-live", "service")
        for off in (False, True)
        for a in fx.mode_transitions(holder, auto_off=off)
    }
    assert sorted(named - bound) == [], "README names Fleet keys that are not bound"


def test_every_cc_swap_env_var_matches_between_readme_and_code():
    src = Path(__file__).resolve().parents[2] / "src" / "claude_swap"
    in_code = {
        name
        for path in src.rglob("*.py")
        for name in re.findall(r"CC_SWAP_[A-Z0-9_]+", path.read_text(encoding="utf-8"))
    }
    in_readme = set(re.findall(r"CC_SWAP_[A-Z0-9_]+", _readme_text()))
    # Markers the generated service file sets for itself; not user settings.
    internal = {"CC_SWAP_SERVICE"}
    assert sorted(in_readme - in_code) == [], "README documents env vars the code never reads"
    assert sorted(in_code - in_readme - internal) == [], (
        "code reads env vars the README never mentions"
    )


def test_readme_documents_upgrade_check_and_its_exit_codes():
    text = _readme_text()
    assert "cc-swap upgrade --check" in text
    assert "`10`" in text and "`0`" in text
    # The exit code the README promises is the real one.
    from claude_swap.update_check import EXIT_UPDATE_AVAILABLE

    assert EXIT_UPDATE_AVAILABLE == 10


def test_readme_says_the_service_rotates_its_macos_logs():
    text = README.read_text(encoding="utf-8")
    assert "auto.err.log" in text
    assert "is not rotated" not in text
    assert "370 KiB" in text
    assert "10 MiB" in text and "three generations" in text


@pytest.mark.parametrize("command", [
    "cc-swap auto off", "cc-swap auto on", "cc-swap auto status",
    "cc-swap prime verify", "cc-swap prime verify --live",
    "cc-swap history -n 0", "cc-swap upgrade --check", "cc-swap claude-update --check",
])
def test_readme_documents_the_subcommands_outside_fork_commands(command):
    assert command in _readme_text()


def test_readme_replaces_the_manual_isolation_checklist_with_prime_verify():
    text = _readme_text()
    assert "### After every Claude Code update: `cc-swap prime verify`" in text
    assert "It replaces the manual checklist" in text
    assert "Fingerprint the active login" not in text
