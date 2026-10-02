"""``cc-swap init``: the ok / FIX / TODO checklist, its exit codes, idempotence
and ``--apply`` (the only path that writes), against doctor_support.World."""

from __future__ import annotations

import json

import pytest

from claude_swap.maximize import doctor_cli
from tests.maximize.doctor_support import World, creds, files


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def statuses(world: World) -> dict[str, str]:
    return {s.key: s.status for s in doctor_cli.init_steps(world.probes())}


def run_init(world, monkeypatch, capsys, *argv) -> tuple[int, str]:
    monkeypatch.setattr(doctor_cli, "_probes", world.probes)
    with pytest.raises(SystemExit) as exc:
        doctor_cli.init_command(list(argv))
    return exc.value.code, capsys.readouterr().out


ORDER = ["claude", "login", "adopted", "accounts", "upstream", "strategy", "service", "priming"]


def test_fresh_machine_walks_the_steps_in_order(world, monkeypatch, capsys):
    world.claude_path = None
    (world.home / "bin" / "claude").unlink()
    steps = doctor_cli.init_steps(world.probes())
    assert [s.key for s in steps] == ORDER
    assert {s.key: s.status for s in steps} == {
        "claude": "FIX", "login": "TODO", "adopted": "TODO", "accounts": "TODO",
        "upstream": "ok", "strategy": "TODO", "service": "TODO", "priming": "ok",
    }
    code, out = run_init(world, monkeypatch, capsys)
    assert code == 1
    assert "FIX   1. Claude Code installed" in out
    assert "→ cc-swap add" in out
    assert "→ cc-swap config set autoswitch.strategy maximize" in out
    assert "--apply does the ones marked applicable" in out


def test_logged_in_with_one_account(world):
    world.accounts(1, active=1)
    world.login(1)
    got = statuses(world)
    assert got["login"] == got["adopted"] == "ok"
    assert got["accounts"] == "TODO"


def test_unmanaged_login_is_a_todo_to_add_it(world):
    world.accounts(1, 2, active=1)
    world.login(9)
    step = next(s for s in doctor_cli.init_steps(world.probes()) if s.key == "adopted")
    assert (step.status, step.fix) == ("TODO", "cc-swap add")


def test_locked_keychain_is_a_fix(world):
    world.accounts(1, 2, active=1)
    world.login(1, keychain_rc=36)
    step = next(s for s in doctor_cli.init_steps(world.probes()) if s.key == "login")
    assert step.status == "FIX" and "rc=36" in step.detail


def test_upstream_still_installed_is_a_fix(world):
    world.healthy()
    (world.home / ".local" / "share" / "uv" / "tools" / "claude-swap").mkdir(parents=True)
    step = next(s for s in doctor_cli.init_steps(world.probes()) if s.key == "upstream")
    assert step.status == "FIX" and step.fix == "uv tool uninstall claude-swap"


def test_stale_service_is_a_fix(world):
    world.healthy()
    world.service_installed(env={"PATH": "/usr/bin"})
    step = next(s for s in doctor_cli.init_steps(world.probes()) if s.key == "service")
    assert step.status == "FIX" and step.fix == "cc-swap service install" and step.applicable


def test_priming_on_with_missing_claude_is_a_fix(world):
    world.healthy()
    world.settings(prime={"enabled": True, "claudePath": "/nope/claude"})
    assert statuses(world)["priming"] == "FIX"


def test_everything_done_exits_0_and_is_idempotent(world, monkeypatch, capsys):
    world.healthy()
    before = files(world.home, world.root)
    first = run_init(world, monkeypatch, capsys)
    second = run_init(world, monkeypatch, capsys)
    assert first == second
    assert first[0] == 0 and "All set." in first[1]
    assert files(world.home, world.root) == before, "init without --apply wrote a file"
    assert "SECRET" not in first[1] and "@example.com" not in first[1]


def test_all_steps_ok_but_doctor_warns_is_not_all_set(world, monkeypatch, capsys):
    """init said "All set" (exit 0) while doctor exited 1 over a plaintext
    copy of the Keychain login: init only looked at plaintext *errors*."""
    from claude_swap.maximize import doctor as dr

    world.healthy()
    world.login(1, plaintext=creds(1))  # the same login, also in plaintext
    doctor_findings = dr.run_checks(world.probes())
    problems = [f for f in doctor_findings if f.severity in ("warn", "error")]
    assert problems, "the setup must make doctor complain"
    code, out = run_init(world, monkeypatch, capsys)
    assert code == 0  # every step is still ok
    assert "All set." not in out
    n = len(problems)
    assert f"Set up, but cc-swap doctor reports {n} " in out
    assert problems[0].detail in out

    payload = json.loads(run_init(world, monkeypatch, capsys, "--json")[1])
    assert payload["doctor"]["exitCode"] == dr.exit_code(doctor_findings)
    assert payload["doctor"]["counts"] == dr.counts(doctor_findings)


def test_json(world, monkeypatch, capsys):
    world.accounts(1, active=1)
    world.login(1)
    code, out = run_init(world, monkeypatch, capsys, "--json")
    payload = json.loads(out)
    assert code == payload["exitCode"] == 1
    assert [s["step"] for s in payload["steps"]] == ORDER
    assert payload["applied"] == []
    strategy = next(s for s in payload["steps"] if s["step"] == "strategy")
    assert strategy["applicable"] is True and strategy["status"] == "TODO"


def test_apply_sets_the_strategy_and_installs_the_service(world, monkeypatch, capsys):
    world.accounts(1, 2, active=1)
    world.login(1)
    installs = []

    def install():
        installs.append(1)
        world.service_installed()
        return {}

    monkeypatch.setattr(doctor_cli, "_install_service", install)
    code, out = run_init(world, monkeypatch, capsys, "--apply")
    assert installs == [1]
    settings = json.loads((world.root / "settings.json").read_text())
    assert settings["autoswitch"]["strategy"] == "maximize"
    assert "applied: set autoswitch.strategy to maximize" in out
    assert "applied: installed the cc-swap service" in out
    assert code == 0
    # Idempotent: a second --apply has nothing left to do.
    code, out = run_init(world, monkeypatch, capsys, "--apply")
    assert code == 0 and installs == [1] and "applied:" not in out


def test_apply_never_installs_the_service_next_to_upstream(world, monkeypatch, capsys):
    world.accounts(1, 2, active=1)
    world.login(1)
    world.settings()
    (world.home / ".local" / "share" / "uv" / "tools" / "claude-swap").mkdir(parents=True)
    monkeypatch.setattr(
        doctor_cli, "_install_service",
        lambda: pytest.fail("installed the service while upstream claude-swap is present"),
    )
    code, out = run_init(world, monkeypatch, capsys, "--apply")
    assert code == 1
    assert "service not installed: finish the steps above first" in out


def test_apply_reports_a_failed_install(world, monkeypatch, capsys):
    from claude_swap.exceptions import ClaudeSwitchError

    world.accounts(1, 2, active=1)
    world.login(1)
    world.settings()

    def install():
        raise ClaudeSwitchError("launchctl bootstrap failed (exit 5)")

    monkeypatch.setattr(doctor_cli, "_install_service", install)
    code, out = run_init(world, monkeypatch, capsys, "--apply")
    assert code == 1 and "service install failed: launchctl bootstrap failed" in out


def test_no_service_on_windows(tmp_path):
    world = World(tmp_path, platform="win32")
    world.accounts(1, 2, active=1)
    step = next(s for s in doctor_cli.init_steps(world.probes()) if s.key == "service")
    assert step.status == "ok" and "no service" in step.detail
