"""Desktop notifications (maximize/notify.py): what the engine sends, the
dedupe and rate limit, the settings, the delivery commands, privacy, and
that a failing notifier never affects a tick. Every test hands in a fake
backend: nothing here runs ``osascript`` or ``notify-send``."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from claude_swap.autoswitch import TickOutcome
from claude_swap.maximize import notify
from claude_swap.maximize.notify import system_backend as REAL_SYSTEM_BACKEND
from claude_swap.settings import NotifySettings, load_notify_settings, set_setting
from tests.maximize.test_engine_maximize import EMAILS, make, win

NOW = 1_000_000.0
H = 3600.0


class Fake:
    """A notifier that records instead of showing anything."""

    def __init__(self, ok: bool = True, boom: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.ok, self.boom = ok, boom

    def __call__(self, title: str, body: str) -> bool:
        if self.boom:
            raise RuntimeError("notifier exploded")
        self.sent.append((title, body))
        return self.ok

    @property
    def backend(self) -> notify.Backend:
        return notify.Backend("fake", self)


@pytest.fixture
def fake(monkeypatch) -> Fake:
    """The system backend, faked for code that looks it up itself."""
    f = Fake()
    monkeypatch.setattr(notify, "system_backend", lambda *a, **k: f.backend)
    return f


def note(key: str = "relogin:3", event: str = "relogin") -> notify.Note:
    return notify.Note(event, key, "cc-swap: #3 old needs a re-login", "its refresh token is dead")


# -- delivery commands --------------------------------------------------------------------


def test_osascript_gets_the_text_as_arguments_never_in_the_script():
    argv = notify.osascript_argv("/usr/bin/osascript", 'a "quoted" title', "it's; done")
    assert argv[:7] == [
        "/usr/bin/osascript", "-e", "on run argv",
        "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e", "end run",
    ]
    assert argv[7:] == ['a "quoted" title', "it's; done"]


def test_the_backend_follows_the_platform():
    which = {"osascript": "/usr/bin/osascript", "notify-send": "/usr/bin/notify-send"}.get
    assert REAL_SYSTEM_BACKEND({}, "darwin", which).name == "osascript"
    assert REAL_SYSTEM_BACKEND({}, "linux", which).name == "notify-send"
    assert REAL_SYSTEM_BACKEND({}, "linux", lambda _n: None) is None  # no notify-send
    assert REAL_SYSTEM_BACKEND({}, "win32", which) is None
    assert REAL_SYSTEM_BACKEND({"CC_SWAP_NOTIFY": "0"}, "darwin", which) is None


def test_delivery_is_time_bounded_and_never_raises(monkeypatch):
    seen = {}

    def slow(argv, **kw):
        seen.update(kw, argv=argv)
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(notify.subprocess, "run", slow)
    backend = REAL_SYSTEM_BACKEND({}, "linux", lambda _n: "/usr/bin/notify-send")
    assert backend.send("t", "b") is False
    assert seen["timeout"] == notify.TIMEOUT_S == 2.0
    assert seen["argv"] == ["/usr/bin/notify-send", "t", "b"]
    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert backend.send("t", "b") is False


# -- privacy ------------------------------------------------------------------------------------


def test_control_characters_never_reach_the_notifier(monkeypatch):
    def run(argv, **kw):  # what the real one does with a NUL in an argument
        if any("\x00" in a for a in argv):
            raise ValueError("embedded null byte")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(notify.subprocess, "run", run)
    assert notify.scrub("a\x00b\x07c") == "a b c"
    # Even unscrubbed, an embedded NUL is a failed send, never an exception.
    backend = REAL_SYSTEM_BACKEND({}, "linux", lambda _n: "/usr/bin/notify-send")
    assert backend.send("t", "a\x00b") is False


@pytest.mark.parametrize(("text", "expected"), [
    ("switched to dev.shared@example.com", "switched to …"),
    ("switched to #4 jordan.lee@uni", "switched to #4 jordan.lee@uni"),  # a short name
    ("token sk-ant-oat01-ABCDEFGHIJ leaked", "token … leaked"),
    ("line one\nline two", "line one line two"),
    ("x" * 300, "x" * 199 + "…"),
])
def test_scrub(text, expected):
    assert notify.scrub(text) == expected


def test_deliver_scrubs_what_it_sends(tmp_path):
    f = Fake()
    bad = notify.Note("relogin", "relogin:1", "cc-swap: a@example.com", "rt sk-ant-ort01-SECRET1")
    assert notify.deliver(tmp_path, bad, now=NOW, backend=f.backend, settings=NotifySettings())
    [(title, body)] = f.sent
    assert "@" not in title + body and "SECRET" not in body


# -- dedupe, rate limit, settings ------------------------------------------------------------------


def test_a_key_is_not_repeated_within_its_interval(tmp_path):
    f = Fake()
    kw = dict(backend=f.backend, settings=NotifySettings())
    assert notify.deliver(tmp_path, note(), now=NOW, **kw)
    assert not notify.deliver(tmp_path, note(), now=NOW + H, **kw)          # same day
    assert notify.deliver(tmp_path, note("relogin:4"), now=NOW + H, **kw)   # another key
    assert notify.deliver(tmp_path, note(), now=NOW + notify.DAY_S, **kw)   # a day later
    assert len(f.sent) == 3
    state = json.loads((tmp_path / notify.STATE_FILENAME).read_text())
    assert state["sent"]["relogin:3"] == NOW + notify.DAY_S


def test_at_most_six_in_ten_minutes(tmp_path):
    f = Fake()
    kw = dict(backend=f.backend, settings=NotifySettings())
    sent = [notify.deliver(tmp_path, note(f"relogin:{n}"), now=NOW + n, **kw) for n in range(8)]
    assert sent == [True] * 6 + [False] * 2
    assert notify.deliver(tmp_path, note("relogin:9"), now=NOW + 601, **kw)


def test_reminders_never_crowd_out_a_switch_or_a_stuck_keychain(tmp_path):
    f = Fake()
    kw = dict(backend=f.backend, settings=NotifySettings())
    for n in range(6):  # a burst of reminders fills their own room
        assert notify.deliver(tmp_path, note(f"relogin:{n}"), now=NOW + n, **kw)
    assert not notify.deliver(tmp_path, note("login-expiring:9", "login-expiring"),
                              now=NOW + 10, **kw)
    assert notify.deliver(tmp_path, note("switch:1>2", "switch"), now=NOW + 11, **kw)
    assert notify.deliver(tmp_path, note("keychain", "keychain"), now=NOW + 12, **kw)
    # Alerts have a room of their own, so a flapping engine is still bounded.
    sent = [notify.deliver(tmp_path, note(f"switch:{n}>x", "switch"), now=NOW + 20 + n, **kw)
            for n in range(6)]
    assert sent == [True] * 4 + [False] * 2


def test_an_old_state_file_with_one_shared_list_still_reads(tmp_path):
    (tmp_path / notify.STATE_FILENAME).write_text(json.dumps(
        {"sent": {}, "recent": [NOW - 10] * 6}))
    f = Fake()
    kw = dict(backend=f.backend, settings=NotifySettings())
    assert not notify.deliver(tmp_path, note(), now=NOW, **kw)          # reminders: full
    assert notify.deliver(tmp_path, note("switch:1>2", "switch"), now=NOW, **kw)


@pytest.mark.parametrize(("settings", "event", "sent"), [
    (NotifySettings(enabled=False), "relogin", False),
    (NotifySettings(relogin=False), "relogin", False),
    (NotifySettings(relogin=False), "switch", True),
    (NotifySettings(switch=False), "switch", False),
    (NotifySettings(login_expiring=False), "login-expiring", False),
    (NotifySettings(prime_paused=False), "prime-paused", False),
    (NotifySettings(keychain=False), "keychain", False),
])
def test_settings_switch_events_off(tmp_path, settings, event, sent):
    f = Fake()
    got = notify.deliver(tmp_path, note(f"{event}:1", event), now=NOW, backend=f.backend,
                         settings=settings)
    assert got is sent and bool(f.sent) is sent


def test_no_backend_sends_nothing_and_writes_nothing(tmp_path):
    assert not notify.deliver(tmp_path, note(), now=NOW, settings=NotifySettings())
    assert not (tmp_path / notify.STATE_FILENAME).exists()


def test_a_failing_backend_never_raises_and_is_not_retried_at_once(tmp_path):
    boom = Fake(boom=True)
    assert notify.deliver(tmp_path, note(), now=NOW, backend=boom.backend,
                          settings=NotifySettings()) is False
    failing = Fake(ok=False)
    kw = dict(backend=failing.backend, settings=NotifySettings())
    assert notify.deliver(tmp_path, note("relogin:4"), now=NOW, **kw) is False
    assert notify.deliver(tmp_path, note("relogin:4"), now=NOW + 60, **kw) is False
    assert len(failing.sent) == 1


def test_notify_settings_load_and_config_set(tmp_path):
    assert load_notify_settings(tmp_path) == NotifySettings()
    set_setting(tmp_path, "notify.switch", "false")
    set_setting(tmp_path, "notify.loginExpiring", "no")
    loaded = load_notify_settings(tmp_path)
    assert (loaded.enabled, loaded.switch, loaded.login_expiring) == (True, False, False)
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw["notify"] == {"switch": False, "loginExpiring": False}


# -- the engine ----------------------------------------------------------------------------------------


HARD = {"1": win(96, 40), "2": win(0, 10), "3": win(0, 50)}


def _alias(h, number: int, alias: str) -> None:
    data = h.switcher._get_sequence_data()
    data["accounts"][str(number)]["alias"] = alias
    h.switcher._write_json(h.switcher.sequence_file, data)


def test_a_switch_is_notified_with_its_trigger_and_no_email(temp_home, fake):
    h = make(temp_home)
    _alias(h, 2, "side")
    assert h.tick_with_usage(HARD) is TickOutcome.SWITCHED
    [(title, body)] = fake.sent
    assert title == "cc-swap: switched to #2 side"
    assert body == "from #1 a · hard: #1 5h 96% >= hard 95%"
    assert "@" not in title + body
    state = json.loads((h.switcher.backup_dir / notify.STATE_FILENAME).read_text())
    assert "switch:1>2" in state["sent"]


def test_a_dry_run_notifies_nothing(temp_home, fake):
    h = make(temp_home)
    h.engine = h._make_engine(dry_run=True)
    assert h.tick_with_usage(HARD) is TickOutcome.SWITCHED
    assert fake.sent == []


def test_notifications_off_send_nothing(temp_home, fake):
    h = make(temp_home)
    set_setting(h.switcher.backup_dir, "notify.enabled", "false")
    assert h.tick_with_usage(HARD) is TickOutcome.SWITCHED
    assert fake.sent == []


def test_a_failing_notifier_never_affects_the_tick(temp_home, monkeypatch):
    boom = Fake(boom=True)
    monkeypatch.setattr(notify, "system_backend", lambda *a, **k: boom.backend)
    h = make(temp_home)
    assert h.tick_with_usage(HARD) is TickOutcome.SWITCHED
    assert h.active_number() == 2


def test_relogin_and_expiring_logins_once_a_day(temp_home, fake):
    from claude_swap.maximize.engine_hook import runtime_for

    h = make(temp_home)
    rt = runtime_for(h.engine)
    rt.login_deadlines = {"2": NOW + 5 * H, "3": NOW + 3 * 86400}
    rt.login_deadlines_at = NOW
    h.clock.now = NOW
    usage = {"1": win(10, 10), "2": win(0, 10), "3": "re-login needed"}
    h.tick_with_usage(usage)
    titles = sorted(t for t, _b in fake.sent)
    assert titles == ["cc-swap: #2 b login ends in 5h 0m", "cc-swap: #3 c needs a re-login"]
    bodies = " ".join(b for _t, b in fake.sent)
    assert "its refresh token is dead" in bodies and "press r" in bodies and "@" not in bodies
    for _ in range(5):  # the next ticks of the day: nothing new
        h.clock.advance(1200)
        rt.login_deadlines_at = h.clock.now
        h.tick_with_usage(usage)
    assert len(fake.sent) == 2
    h.clock.now = NOW + 86400 + 60
    rt.login_deadlines = {"2": NOW + 86400 + 4 * H}
    rt.login_deadlines_at = h.clock.now
    h.tick_with_usage(usage)
    assert len(fake.sent) == 4


def test_a_login_past_its_deadline_needs_a_relogin(temp_home, fake):
    from claude_swap.maximize.engine_hook import runtime_for

    h = make(temp_home)
    rt = runtime_for(h.engine)
    rt.login_deadlines, rt.login_deadlines_at = {"3": NOW - 60}, NOW
    h.tick_with_usage({"1": win(10, 10), "2": win(0, 10), "3": win(0, 10)})
    [(title, body)] = fake.sent
    assert title == "cc-swap: #3 c needs a re-login" and body.startswith("its login expired")


def test_priming_paused_is_notified(temp_home, fake, monkeypatch):
    from claude_swap.maximize import prime_verify
    from claude_swap.maximize.engine_hook import runtime_for
    from claude_swap.settings import PrimeSettings

    h = make(temp_home)
    rt = runtime_for(h.engine)
    rt.prime_settings, rt.primer = PrimeSettings(enabled=True), None
    monkeypatch.setattr(prime_verify, "paused_state",
                        lambda _root, **_kw: ("paused: claude 2.1.0 -> 2.1.1 (cc-swap prime verify)", False))
    h.tick_with_usage({"1": win(10, 10), "2": win(0, 10), "3": win(0, 10)})
    [(title, body)] = fake.sent
    assert title == "cc-swap: priming paused"
    assert body.startswith("priming paused: claude 2.1.0 -> 2.1.1")


def test_a_keychain_hold_over_15_minutes_is_notified_once(temp_home, fake, monkeypatch):
    h = make(temp_home)
    monkeypatch.setattr(h.engine, "_active_read_unhealthy", lambda: True)
    for _ in range(3):  # 0, 5, 10 min: not yet
        h.tick_with_usage(HARD)
        h.clock.advance(300)
    assert fake.sent == []
    for _ in range(3):  # 15, 20, 25 min: once
        h.tick_with_usage(HARD)
        h.clock.advance(300)
    [(title, body)] = fake.sent
    assert title == "cc-swap: Keychain unreadable for 15 min"
    assert body.startswith("#1's live login cannot be read, so nothing switches")
    assert h.active_number() == 1


def test_attach_is_idempotent(temp_home):
    from claude_swap.maximize.engine_hook import NOTIFY_ATTR, attach_maximize

    h = make(temp_home)
    attach_maximize(h.engine)
    first = getattr(h.engine, NOTIFY_ATTR)
    attach_maximize(h.engine)
    assert getattr(h.engine, NOTIFY_ATTR) is first
    assert isinstance(first, notify.EngineNotifier)


def test_no_email_in_any_engine_notification(temp_home, fake):
    from claude_swap.maximize.engine_hook import runtime_for

    h = make(temp_home, n=4)
    rt = runtime_for(h.engine)
    rt.login_deadlines, rt.login_deadlines_at = {"2": NOW + H}, NOW
    h.tick_with_usage({"1": win(96, 40), "2": win(0, 10), "3": "re-login needed",
                       "4": win(0, 50)})
    assert fake.sent
    for title, body in fake.sent:
        for email in EMAILS.values():
            assert email not in title + body
        assert "@" not in title + body


# -- `cc-swap notify` ------------------------------------------------------------------------------------


def _main(monkeypatch, capsys, *argv) -> tuple[int, str]:
    from claude_swap import cli

    monkeypatch.setattr(sys, "argv", ["cc-swap", *argv])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    return exit_.value.code, capsys.readouterr().out


def test_notify_test_sends_through_the_system_notifier(temp_home, fake, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "notify", "test")
    assert code == 0 and out.startswith("Sent a test notification (via fake).")
    [(title, _body)] = fake.sent
    assert title == "cc-swap: test notification"


def test_notify_test_without_a_notifier_fails(temp_home, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, "notify", "test")
    assert code == 1 and "Nothing is sent from this process: CC_SWAP_NOTIFY=0." in out
    code, out = _main(monkeypatch, capsys, "notify", "test", "--json")
    assert code == 1 and json.loads(out)["sent"] is False


def test_notify_test_says_when_the_engine_sends_none(temp_home, fake, monkeypatch, capsys):
    from claude_swap import paths

    set_setting(paths.get_backup_root(), "notify.enabled", "false")
    code, out = _main(monkeypatch, capsys, "notify", "test")
    assert code == 0 and "notify.enabled is false" in out


def test_notify_status(temp_home, fake, monkeypatch, capsys):
    from claude_swap import paths

    set_setting(paths.get_backup_root(), "notify.keychain", "false")
    code, out = _main(monkeypatch, capsys, "notify")
    assert code == 0 and out.startswith("Notifications are ON (notify.enabled), sent via fake.")
    assert "keychain off" in out and "switch on" in out
    code, out = _main(monkeypatch, capsys, "notify", "status", "--json")
    payload = json.loads(out)
    assert payload["enabled"] is True and payload["backend"] == "fake"
    assert payload["events"]["keychain"] is False and payload["events"]["switch"] is True
