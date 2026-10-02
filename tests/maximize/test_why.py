"""``cc-swap why`` and the README table "Why didn't it switch?".

The published decision is read the way Fleet reads it (fresh, about the live
account); without one, ``why`` falls back to ``auto --once --dry-run``. The
README test pins every ``NoSwitchEvent`` reason literal in the engine source
to a documented row and to :data:`doctor_cli.REASONS`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claude_swap.maximize import doctor_cli
from claude_swap.maximize.fleet import fresh_s

NOW = 1_790_000_000.0
SRC = Path(__file__).resolve().parents[2] / "src" / "claude_swap"
README = Path(__file__).resolve().parents[2] / "README.md"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_cli.paths, "get_backup_root", lambda: tmp_path)
    (tmp_path / "sequence.json").write_text(json.dumps({"activeAccountNumber": 1, "accounts": {}}))
    return tmp_path


def publish(root: Path, *, at: float = NOW - 30, **record) -> None:
    decision = {
        "at": at, "pid": 4121, "active": "1", "decision": "hold", "trigger": None,
        "target": "2", "reason": "#1 5h 62% >= soft 50%", "pending": True, "plans": {},
    }
    decision.update(record)
    state = {"maximizeDecision": decision}
    (root / "autoswitch_state.json").write_text(json.dumps(state))


def why(monkeypatch, capsys, *argv) -> tuple[int, str]:
    with pytest.raises(SystemExit) as exc:
        doctor_cli.why_command(list(argv), clock=lambda: NOW)
    return exc.value.code, capsys.readouterr().out


@pytest.fixture
def dry_runs(monkeypatch):
    from claude_swap import cli

    calls: list[list[str]] = []

    def fake(argv):
        calls.append(argv)
        print("[dry-run] decision: hold")
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_auto_command", fake)
    return calls


def test_fresh_pending_decision_is_explained(root, monkeypatch, capsys, dry_runs):
    publish(root)
    code, out = why(monkeypatch, capsys)
    assert code == 0 and dry_runs == []
    assert "PENDING → #2" in out and "on #1" in out and "engine pid 4121" in out
    assert "code     maximize-pending" in out
    assert doctor_cli.REASONS["maximize-pending"][0] in out
    assert "#1 5h 62% >= soft 50%" in out


def test_hold_and_exhausted_and_indeterminate_map_to_codes(root, monkeypatch, capsys):
    for decision, pending, code in [
        ("hold", False, "maximize-hold"),
        ("exhausted", False, "no-qualifying-candidate"),
        ("indeterminate", False, "active-usage-unknown"),
    ]:
        publish(root, decision=decision, pending=pending, target=None)
        payload = json.loads(why(monkeypatch, capsys, "--json")[1])
        assert payload["code"] == code and payload["source"] == "engine"
        assert payload["meaning"] == doctor_cli.REASONS[code][0]


def test_switch_names_its_trigger(root, monkeypatch, capsys):
    publish(root, decision="switch", trigger="soft", pending=False)
    _, out = why(monkeypatch, capsys)
    assert "SWITCH → #2 (soft)" in out
    assert doctor_cli.TRIGGERS["soft"] in out


def test_stale_decision_falls_back_to_a_dry_run(root, monkeypatch, capsys, dry_runs):
    publish(root, at=NOW - fresh_s(60.0) - 1)
    code, out = why(monkeypatch, capsys)
    assert code == 0
    assert dry_runs == [["--once", "--dry-run"]]
    assert "No engine published a fresh decision" in out
    assert "[dry-run] decision: hold" in out


def test_decision_about_another_active_account_is_history(root, monkeypatch, capsys, dry_runs):
    publish(root, active="3")
    why(monkeypatch, capsys)
    assert dry_runs == [["--once", "--dry-run"]]


def test_no_fallback(root, monkeypatch, capsys, dry_runs):
    code, out = why(monkeypatch, capsys, "--no-fallback")
    assert code == 0 and dry_runs == []
    assert "cc-swap auto --once --dry-run" in out


def test_json_without_a_decision_names_the_fallback(root, monkeypatch, capsys, dry_runs):
    payload = json.loads(why(monkeypatch, capsys, "--json")[1])
    assert payload == {
        "schemaVersion": 1, "source": "none",
        "fallback": "cc-swap auto --once --dry-run --json",
    }
    assert dry_runs == []


def test_a_relogin_pause_wins(root, monkeypatch, capsys, dry_runs):
    publish(root)
    state = json.loads((root / "autoswitch_state.json").read_text())
    state.update(pausedUntil=NOW + 240, pausedReason="re-login #2")
    (root / "autoswitch_state.json").write_text(json.dumps(state))
    _, out = why(monkeypatch, capsys)
    assert "PAUSED" in out and "code     maximize-paused" in out and "re-login #2" in out


def test_why_writes_nothing(root, monkeypatch, capsys, dry_runs):
    publish(root)
    before = {p: p.read_bytes() for p in root.iterdir()}
    why(monkeypatch, capsys)
    assert {p: p.read_bytes() for p in root.iterdir()} == before


# -- README: every reason code documented ------------------------------------------------


def _source_reason_codes() -> set[str]:
    """Every literal reason a ``NoSwitchEvent(...)`` call can carry."""
    codes: set[str] = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"NoSwitchEvent\(", text):
            call = text[match.end(): match.end() + 400]
            m = re.match(r"\s*reason=((?:\"[a-z0-9-]+\"|\s|if|else|[\w.]+)+)", call)
            if m:
                codes.update(re.findall(r"\"([a-z0-9-]+)\"", m.group(1)))
    return codes


def _readme_codes() -> dict[str, list[str]]:
    text = README.read_text(encoding="utf-8")
    section = text.split("### Why didn't it switch?", 1)[1].split("\n#", 1)[0]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) == 5 and cells[1].startswith("`") and cells[1].endswith("`"):
            rows[cells[1].strip("`")] = cells[2:4]
    return rows


def test_the_scan_finds_the_known_codes():
    codes = _source_reason_codes()
    assert {"below-threshold", "active-credential-unreadable", "maximize-pending",
            "maximize-hold", "no-viable-target", "unmanaged-active-account"} <= codes
    assert len(codes) >= 20


def test_every_reason_code_in_the_engine_is_explained_and_documented():
    codes = _source_reason_codes()
    readme = _readme_codes()
    assert codes - set(doctor_cli.REASONS) == set(), "add these to doctor_cli.REASONS"
    assert codes - set(readme) == set(), "add these to the README table"


def test_readme_table_matches_reasons_and_lists_no_dead_codes():
    readme = _readme_codes()
    codes = _source_reason_codes()
    assert set(readme) == set(doctor_cli.REASONS)
    assert set(readme) <= codes, "README documents a code the engine no longer emits"
    for code, (meaning, action) in doctor_cli.REASONS.items():
        assert readme[code] == [meaning, action], code


@pytest.mark.parametrize("snippet", [
    "## Diagnostics: `doctor`, `init`, `why`",
    "### Why didn't it switch?",
    "errSecInteractionNotAllowed",
    "errSecAuthFailed",
    "cc-swap init --apply",
    "Account settings → `v`",
])
def test_readme_documents_doctor_init_and_why(snippet):
    assert snippet in README.read_text(encoding="utf-8")
