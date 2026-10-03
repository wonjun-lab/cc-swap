"""Screenshots of the Fleet home screen, from a fake fleet (README images).

    uv run python tools/fleet_screenshots.py [--out DIR ...] [--assets DIR]
        [--emails A,B,C,D,E,F] [NAME ...]

Runs the real ``CswapApp`` against six fake accounts in a temporary HOME
(``tests.test_tui.FakeSwitcher``, a temporary backup root, the service
probe stubbed, the engine lease held by this process standing in for the
service, so the TUI is a viewer). Nothing touches the real ``~/.claude*``,
the Keychain, ``claude`` or launchd/systemd. The fake backup root also gets
nine days of usage history, so help and Swap strategy show a learned idle
pattern.

The six accounts have no aliases, so the table shows their full emails
(20-22 characters, like a real fleet's), and cover every row state: the
active account (with a per-model ``Fable`` window), the next one with its
5h window not started, a login one day from its deadline, a Team account
marked last resort, a dead login and a primed account. ``--emails`` swaps
in other addresses (six, comma-separated) for a private preview; keep real
addresses out of the README images.

The ``hold`` scenario holds #1 (``cc-swap hold``, Fleet ``h``) for two hours
with every login healthy (no attention line), so even 200x16 has the rows
for the capacity summary next to the selected account's panel; its shots
are ``fleet-hold-200x16``, ``fleet-hold-120x36`` and the ``h`` picker
(``fleet-holdpicker-120x36``).

Writes ``fleet-<size>.svg`` for each shot, plus a PNG (macOS Quick Look,
cropped to the window) when ``qlmanage`` and ``sips`` exist, into the first
``--out`` (default ``/tmp/cc-swap-tui-poc/table``) and copies them into any
further ``--out``. ``--assets DIR`` also copies the README's images there
(:data:`README_ASSETS`). NAMEs pick shots (``160x45``, ``menu``,
``autooff``, ``resetwait``, ``preempt``, ``hold``, ``strategy`` …); none
means all. Desktop notifications are off for the run (``CC_SWAP_NOTIFY=0``).
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
os.environ["CC_SWAP_NOTIFY"] = "0"
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from claude_swap.json_output import USAGE_RELOGIN_REQUIRED  # noqa: E402
from claude_swap.maximize.lease import EngineLease  # noqa: E402
from claude_swap.models import AccountSnapshot  # noqa: E402
from claude_swap.usage_store import UsageEntry  # noqa: E402

NOW = time.time()
H, D = 3600.0, 86400.0
SIZES = [(160, 45), (120, 36), (100, 30), (80, 24), (200, 16)]

#: The six accounts' emails (no aliases). ``--emails`` replaces them.
EMAILS = [
    "dev.shared@example.com", "dev.master@example.com", "jordan.lee@example.com",
    "jordan.lee@uni.example", "dev.llm0@example.com", "nightowl@example.com",
]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _window(pct, reset_in) -> dict:
    return {"pct": pct, "resets_at": _iso(NOW + reset_in) if reset_in else None}


def _entry(p5, r5, p7, r7_days, *, age=40.0, fable=None, sentinel=None) -> UsageEntry:
    last_good = {"five_hour": _window(p5, r5), "seven_day": _window(p7, r7_days * D)}
    if fable is not None:
        last_good["scoped"] = [{"name": "Fable", **_window(fable, r7_days * D)}]
    return UsageEntry(sentinel=sentinel, last_good=last_good, fetched_at=NOW - age, age_s=age)


def _account(n, email, entry, *, active=False, org="", login_days=None, disabled=False):
    return AccountSnapshot(
        number=str(n), email=email, org_name=org, org_uuid="org-1" if org else "",
        is_active=active, kind="oauth", switchable=True, usage=entry, alias="",
        disabled=disabled,
        login_expires_at=(NOW + login_days * D) * 1000.0 if login_days is not None else None,
    )


def accounts_for(
    emails: list[str], first: UsageEntry | None = None, *, healthy: bool = False
) -> list[AccountSnapshot]:
    """#1 active, past its 5h soft mark, with a Fable window (``first``
    replaces its usage); #2 next, its 5h not started; #3 login ends in
    about a day; #4 a Team account, last resort, 5h not started; #5 a dead
    login (its last reading still says when its windows reset); #6 primed.
    ``healthy``: #3's login has 12 days left and #5's login works (its 7d
    nearly spent, its 5h past soft), so nothing needs you."""
    e = emails
    if healthy:
        fifth = _account(5, e[4], _entry(71.0, 0.7 * H, 92.0, 1.6), login_days=17)
    else:
        fifth = _account(5, e[4], _entry(0.0, None, 57.0, 1.6, age=9 * H,
                                         sentinel=USAGE_RELOGIN_REQUIRED))
    return [
        _account(1, e[0], first or _entry(62.0, 1.8 * H, 41.0, 3.8, fable=38.0),
                 active=True, login_days=21),
        _account(2, e[1], _entry(0.0, None, 35.0, 2.2), login_days=20),
        _account(3, e[2], _entry(22.0, 2.6 * H, 18.0, 5.4), login_days=12 if healthy else 1.2),
        _account(4, e[3], _entry(0.0, None, 30.0, 4.0), org="Acme Team", login_days=29),
        fifth,
        _account(6, e[5], _entry(3.0, 3.3 * H, 22.0, 5.1), login_days=14),
    ]


ACCOUNTS = accounts_for(EMAILS)
PLANS = {"1": "20x", "2": "20x", "3": "5x", "4": "team", "5": "5x", "6": "20x"}

# The README's images: shot name -> file name in --assets.
README_ASSETS = {
    "fleet-160x45": "fleet-wide.png",
    "fleet-80x24": "fleet-narrow.png",
    "fleet-resetwait-160x45": "fleet-reset-wait.png",
}


def seed_history(root: Path) -> str:
    """Nine days of 15-minute slot observations (``usage_history.jsonl``):
    busy for 14 hours from 4 hours ago, quiet for the 10 after, every day.
    Returns the next quiet window's start (``HH:MM``)."""
    from claude_swap.maximize import history

    slot = history.SLOT_S
    busy_from = NOW - NOW % 1800 - 4 * H
    slots = []
    t = NOW - NOW % slot - 9 * D
    while t + slot + history.SLOT_SETTLE_S <= NOW:
        slots.append(history.SlotObs(t, (t - busy_from) % D < 14 * H))
        t += slot
    (root / history.HISTORY_FILENAME).write_text("".join(history._line(s) for s in slots))
    forecast = history.forecast(slots, NOW)
    return forecast.next.start_label if forecast and forecast.next else "23:00"


def _scenario(name: str, quiet: str, emails: list[str]) -> tuple[list, dict, list]:
    """``(accounts, published decision, #1's samples)`` for a scenario:
    ``pending`` (past the 5h soft mark, waiting for a pause), ``reset-wait``
    (past it too, but the 5h window resets in 8 minutes), ``preempt`` (the
    7d would pass its soft mark before the quiet time) or ``hold`` (past the
    5h soft mark, but held for two hours: the engine sets the move aside)."""
    base = {"at": NOW - 50, "pid": os.getpid(), "active": "1", "plans": PLANS}
    if name == "hold":
        from claude_swap.maximize.hold import AccountHold, safety_text, until_text

        held = until_text(AccountHold("1", NOW + 2 * H), NOW - 50)
        return accounts_for(emails, healthy=True), {
            **base, "decision": "hold", "trigger": None, "target": None, "pending": False,
            "code": "hold",
            "reason": f"#1 held {held} — {safety_text(98, 98)}; otherwise: #1 5h 62% >= soft "
                      "50%; waiting for idle to move to #2 (5h +3 / 7d +0.2 pts over 10 min)",
        }, [[NOW - 660, 59.0, 40.8], [NOW - 60, 62.0, 41.0]]
    if name == "reset-wait":
        accounts = accounts_for(emails, _entry(96.0, 8 * 60 + 20, 41.0, 3.8, fable=38.0))
        return accounts, {
            **base, "decision": "hold", "trigger": None, "target": None, "pending": False,
            "code": "reset-wait",
            "reason": "#1 5h 96% — resets in 8m, waiting it out "
                      "(switches at once if it hits 100%)",
        }, [[NOW - 660, 95.5, 40.8], [NOW - 60, 96.0, 41.0]]
    if name == "preempt":
        accounts = accounts_for(emails, _entry(31.0, 1.8 * H, 84.0, 3.8, fable=60.0))
        return accounts, {
            **base, "decision": "hold", "trigger": None, "target": None, "pending": False,
            "code": "preempt",
            "reason": f"#1 7d 84% would pass 90% in ~3h, before your usual quiet time "
                      f"({quiet}) — will move to #2 at the next idle moment "
                      "(5h +3 / 7d +0.2 pts over 10 min)",
        }, [[NOW - 660, 28.0, 83.8], [NOW - 60, 31.0, 84.0]]
    return accounts_for(emails), {
        **base, "decision": "hold", "trigger": None, "target": "2", "pending": True,
        "reason": "#1 5h 62% >= soft 50%; waiting for idle to move to #2 "
                  "(5h +3 / 7d +0.2 pts over 10 min)",
    }, [[NOW - 660, 59.0, 40.8], [NOW - 60, 62.0, 41.0]]


def seed(
    root: Path, *, emails: list[str] = EMAILS, auto_off: bool = False,
    scenario: str = "pending",
) -> list:
    """Settings, state and usage history for a scenario; returns its accounts."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps({
        "schemaVersion": 1,
        "autoswitch": {"strategy": "maximize"},
        "maximize": {"lastResort": emails[3], "soft5h": 50, "hard5h": 98,
                     "soft7d": 90, "hard7d": 98},
        "prime": {"enabled": True, "jitterS": "45-300"},
    }))
    quiet = seed_history(root)
    accounts, decision, samples = _scenario(scenario, quiet, emails)
    reset6 = NOW + 3.3 * H
    dead = scenario != "hold"  # the hold shots have every login healthy
    state = {
        "schemaVersion": 1,
        "quarantine": {"5": {"email": emails[4], "reason": "invalid_grant"}} if dead else {},
        "maximizeSamples": {"account": "1", "samples": samples},
        "primes": {emails[5]: {
            "windowKey": "w", "attempts": 1,
            "lastAttemptAt": reset6 - 5 * H + 60, "lastOutcome": "primed"}},
        "lastSwitchAt": NOW - 2 * H,
        "maximizeDecision": decision,
    }
    flag = root / "auto_off.json"
    if auto_off:
        marker = {"since": NOW - 25 * 60, "by": "cli", "host": "mbp"}
        state["autoOff"] = marker
        flag.write_text(json.dumps({"schemaVersion": 1, "autoOff": marker}))
    elif flag.exists():
        flag.unlink()
    (root / "autoswitch_state.json").write_text(json.dumps(state))
    from claude_swap.maximize import hold

    hold.clear_hold(root)
    if scenario == "hold":
        hold.set_hold(root, "1", NOW + 2 * H, by="fleet", now=NOW - 50, host="mbp")
    return accounts


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


async def shoot(
    out: Path, root: Path, w: int, h: int, name: str, *, accounts, keys=()
) -> list[Path]:
    import claude_swap.tui.fleet as fleet_mod
    from claude_swap.tui.app import CswapApp
    from claude_swap.tui.fleet import FleetScreen
    from tests.test_tui import FakeSwitcher

    fleet_mod.service_status = lambda: {
        "platform": "darwin", "installed": True, "loaded": True, "running": True,
        "state": "running", "pid": os.getpid(), "logs": [],
    }
    app = CswapApp(FakeSwitcher(accounts, root))
    async with app.run_test(size=(w, h)) as pilot:
        await settle(app, pilot)
        assert isinstance(app.screen, FleetScreen), type(app.screen)
        app.screen._probe_service()
        await settle(app, pilot)
        for key in keys:
            await pilot.press(key)
            await settle(app, pilot)
        app.save_screenshot(filename=f"{name}.svg", path=str(out))
    svg = out / f"{name}.svg"
    png = to_png(svg)
    print(png or svg)
    return [svg] + ([png] if png else [])


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, action="append",
                        help="output directory (repeat to copy into more)")
    parser.add_argument("--assets", type=Path,
                        help="also copy the README's images here (e.g. assets/)")
    parser.add_argument("--emails", help="six comma-separated emails for the accounts")
    parser.add_argument("only", nargs="*")
    args = parser.parse_args()
    emails = [e.strip() for e in args.emails.split(",")] if args.emails else EMAILS
    if len(emails) != len(EMAILS) or not all(emails):
        parser.error(f"--emails takes {len(EMAILS)} comma-separated addresses")
    outs: list[Path] = args.out or [Path("/tmp/cc-swap-tui-poc/table")]
    for directory in outs:
        directory.mkdir(parents=True, exist_ok=True)
    out = outs[0]
    root = HOME / ".claude-swap-backup"
    written: dict[str, list[Path]] = {}

    def wanted(name: str) -> bool:
        return not args.only or any(o in name for o in args.only)

    async def take(w: int, h: int, name: str, *, accounts, **kw) -> None:
        if wanted(name):
            written[name] = await shoot(out, root, w, h, name, accounts=accounts, **kw)

    accounts = seed(root, emails=emails)
    lease = EngineLease(root)
    assert lease.acquire()
    try:
        for w, h in SIZES:
            await take(w, h, f"fleet-{w}x{h}", accounts=accounts)
        await take(120, 36, "fleet-menu-120x36", keys=("m",), accounts=accounts)
        await take(120, 36, "fleet-help-120x36", keys=("question_mark",), accounts=accounts)
        # Help scrolled to its end: the words and what has been learned.
        await take(120, 36, "fleet-help-learned-120x36",
                   keys=("question_mark", *("j",) * 40), accounts=accounts)
        await take(120, 36, "fleet-strategy-120x36", keys=("m", "s"), accounts=accounts)
        if any(wanted(n) for n in ("fleet-autooff-120x36", "fleet-autooff-80x24")):
            seed(root, emails=emails, auto_off=True)
            await take(120, 36, "fleet-autooff-120x36", accounts=accounts)
            await take(80, 24, "fleet-autooff-80x24", accounts=accounts)
        for scenario, shots in (
            ("reset-wait", ((160, 45), (80, 24))),
            ("preempt", ((120, 36),)),
            ("hold", ((200, 16), (120, 36))),
        ):
            names = [f"fleet-{scenario.replace('-', '')}-{w}x{h}" for w, h in shots]
            if any(wanted(n) for n in names):
                scenario_accounts = seed(root, emails=emails, scenario=scenario)
                for (w, h), name in zip(shots, names):
                    await take(w, h, name, accounts=scenario_accounts)
        if wanted("fleet-holdpicker-120x36"):
            hold_accounts = seed(root, emails=emails, scenario="hold")
            await take(120, 36, "fleet-holdpicker-120x36", keys=("h",), accounts=hold_accounts)
        seed(root, emails=emails)
    finally:
        lease.release()
        shutil.rmtree(HOME, ignore_errors=True)
    for directory in outs[1:]:
        for files in written.values():
            for path in files:
                shutil.copy2(path, directory / path.name)
    if args.assets is not None:
        args.assets.mkdir(parents=True, exist_ok=True)
        for name, target in README_ASSETS.items():
            png = next((p for p in written.get(name, []) if p.suffix == ".png"), None)
            if png is not None:
                shutil.copy2(png, args.assets / target)
                print(f"{args.assets / target} <- {png.name}")


if __name__ == "__main__":
    asyncio.run(main())
