"""Screenshots of the Fleet home screen, from a fake fleet (README images).

    uv run python tools/fleet_screenshots.py [--out DIR] [NAME ...]

Runs the real ``CswapApp`` against six fake accounts in a temporary HOME
(``tests.test_tui.FakeSwitcher``, a temporary backup root, the service
probe stubbed, the engine lease held by this process standing in for the
service, so the TUI is a viewer). Nothing touches the real ``~/.claude*``,
the Keychain, ``claude`` or launchd/systemd.

Writes ``fleet-<size>.svg`` for each shot, plus a PNG (macOS Quick Look,
cropped to the window) when ``qlmanage`` and ``sips`` exist. NAMEs pick
shots (``160x45``, ``menu``, ``autooff`` …); none means all.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="ccswap-shots-home-"))
os.environ["HOME"] = str(HOME)
os.environ["CC_SWAP_FETCH_ON_OPEN"] = "0"
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED  # noqa: E402
from claude_swap.maximize.lease import EngineLease  # noqa: E402
from claude_swap.models import AccountSnapshot  # noqa: E402
from claude_swap.usage_store import UsageEntry  # noqa: E402

NOW = time.time()
H, D = 3600.0, 86400.0
SIZES = [(160, 45), (120, 36), (90, 28), (80, 24)]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _entry(p5, r5, p7, r7_days, age=40.0) -> UsageEntry:
    last_good = {
        "five_hour": {"pct": p5, "resets_at": _iso(NOW + r5) if r5 else None},
        "seven_day": {"pct": p7, "resets_at": _iso(NOW + r7_days * D)},
    }
    return UsageEntry(last_good=last_good, fetched_at=NOW - age, age_s=age)


def _account(n, alias, entry, *, active=False, org="", login_days=None, disabled=False):
    return AccountSnapshot(
        number=str(n), email=f"{alias}@acme.dev", org_name=org, org_uuid="org-1" if org else "",
        is_active=active, kind="oauth", switchable=True, usage=entry, alias=alias,
        disabled=disabled,
        login_expires_at=(NOW + login_days * D) * 1000.0 if login_days is not None else None,
    )


# #1 active past its 5h soft mark; #2 cold, next; #3 dead login; #4 primed,
# login ends in about a day; #5 excluded; #6 a Team account, last resort.
ACCOUNTS = [
    _account(1, "main", _entry(62.0, 1.8 * H, 41.0, 3.8), active=True, login_days=21),
    _account(2, "side", _entry(0.0, None, 35.0, 2.2), login_days=20),
    _account(3, "old", UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED)),
    _account(4, "work", _entry(3.0, 3.3 * H, 22.0, 5.1), login_days=1.2),
    _account(5, "alt", _entry(48.0, 0.5 * H, 71.0, 1.5), login_days=14, disabled=True),
    _account(6, "team", _entry(0.0, None, 30.0, 4.0), org="Acme Team", login_days=29),
]


def seed(root: Path, *, auto_off: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps({
        "schemaVersion": 1,
        "autoswitch": {"strategy": "maximize"},
        "maximize": {"lastResort": "team@acme.dev", "soft5h": 50, "hard5h": 98,
                     "soft7d": 90, "hard7d": 98},
        "prime": {"enabled": True, "jitterS": "45-300"},
    }))
    reset4 = NOW + 3.3 * H
    state = {
        "schemaVersion": 1,
        "quarantine": {"3": {"email": "old@acme.dev", "reason": "invalid_grant"}},
        "maximizeSamples": {"account": "1", "samples": [
            [NOW - 660, 59.0, 40.8], [NOW - 60, 62.0, 41.0]]},
        "primes": {"work@acme.dev": {
            "windowKey": "w", "attempts": 1,
            "lastAttemptAt": reset4 - 5 * H + 60, "lastOutcome": "primed"}},
        "lastSwitchAt": NOW - 2 * H,
        "maximizeDecision": {
            "at": NOW - 50, "pid": os.getpid(), "active": "1", "decision": "hold",
            "trigger": None, "target": "2", "pending": True,
            "reason": "#1 5h 62% >= soft 50%; waiting for idle to move to #2 "
                      "(5h +3 / 7d +0.2 pts over 10 min)",
            "plans": {"1": "20x", "2": "5x", "3": "5x", "4": "20x", "5": "5x", "6": "team"},
        },
    }
    flag = root / "auto_off.json"
    if auto_off:
        marker = {"since": NOW - 25 * 60, "by": "cli", "host": "mbp"}
        state["autoOff"] = marker
        flag.write_text(json.dumps({"schemaVersion": 1, "autoOff": marker}))
    elif flag.exists():
        flag.unlink()
    (root / "autoswitch_state.json").write_text(json.dumps(state))


def to_png(svg: Path) -> Path | None:
    """SVG -> PNG via Quick Look, cropped to the window (macOS only)."""
    if not (shutil.which("qlmanage") and shutil.which("sips")):
        return None
    size = 1800
    tmp = Path(tempfile.mkdtemp(prefix="ql-"))
    subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(tmp), str(svg)],
                   check=True, capture_output=True)
    png = svg.with_suffix(".png")
    (tmp / (svg.name + ".png")).replace(png)
    head = svg.read_text()[:2000]
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head)
    if box:
        w, h = float(box.group(1)), float(box.group(2))
        if w > h:
            subprocess.run(["sips", "-c", str(int(size * h / w) + 2), str(size), str(png)],
                           check=True, capture_output=True)
    return png


async def settle(app, pilot) -> None:
    for _ in range(6):
        await app.workers.wait_for_complete([w for w in app.workers if w.group != "engine"])
        await pilot.pause()


async def shoot(out: Path, root: Path, w: int, h: int, name: str, *, keys=()) -> None:
    import claude_swap.tui.fleet as fleet_mod
    from claude_swap.tui.app import CswapApp
    from claude_swap.tui.fleet import FleetScreen
    from tests.test_tui import FakeSwitcher

    fleet_mod.service_status = lambda: {
        "platform": "darwin", "installed": True, "loaded": True, "running": True,
        "state": "running", "pid": os.getpid(), "logs": [],
    }
    app = CswapApp(FakeSwitcher(ACCOUNTS, root))
    async with app.run_test(size=(w, h)) as pilot:
        await settle(app, pilot)
        assert isinstance(app.screen, FleetScreen), type(app.screen)
        app.screen._probe_service()
        await settle(app, pilot)
        for key in keys:
            await pilot.press(key)
            await settle(app, pilot)
        app.save_screenshot(filename=f"{name}.svg", path=str(out))
    png = to_png(out / f"{name}.svg")
    print(png or out / f"{name}.svg")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("/tmp/cc-swap-tui-poc/final"))
    parser.add_argument("only", nargs="*")
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    root = HOME / ".claude-swap-backup"

    def wanted(name: str) -> bool:
        return not args.only or any(o in name for o in args.only)

    seed(root)
    lease = EngineLease(root)
    assert lease.acquire()
    try:
        for w, h in SIZES:
            if wanted(f"fleet-{w}x{h}"):
                await shoot(out, root, w, h, f"fleet-{w}x{h}")
        if wanted("fleet-menu-120x36"):
            await shoot(out, root, 120, 36, "fleet-menu-120x36", keys=("m",))
        if wanted("fleet-help-120x36"):
            await shoot(out, root, 120, 36, "fleet-help-120x36", keys=("question_mark",))
        if wanted("fleet-autooff"):
            seed(root, auto_off=True)
            await shoot(out, root, 120, 36, "fleet-autooff-120x36")
            await shoot(out, root, 80, 24, "fleet-autooff-80x24")
            seed(root)
    finally:
        lease.release()
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
