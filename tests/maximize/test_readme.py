"""The README's fork section must track SETTING_SPECS and the install story."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap.settings import SETTING_SPECS, format_setting_value

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


def test_readme_says_the_service_rotates_its_macos_logs():
    text = README.read_text(encoding="utf-8")
    assert "auto.err.log" in text
    assert "is not rotated" not in text
    assert "370 KiB" in text
    assert "10 MiB" in text and "three generations" in text
