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
    assert len(FORK_KEYS) == 18  # 13 maximize.* + 5 prime.*


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
])
def test_readme_covers_install_migration_priming_and_the_service(snippet):
    assert snippet in README.read_text(encoding="utf-8")
