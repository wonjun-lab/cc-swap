"""Size-based rotation of the launchd service's own log files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_swap.maximize import logrotate, service
from claude_swap.maximize.logrotate import (
    CHECK_INTERVAL_S,
    MAX_BYTES,
    LogRotator,
    check_due,
    rotate_file,
    rotate_names,
    service_log_files,
    should_rotate,
)

MIB = 1024 * 1024


def test_limits_match_the_spec():
    assert MAX_BYTES == 10 * MIB
    assert CHECK_INTERVAL_S == 3600


class TestShouldRotate:
    def test_only_above_ten_mib(self):
        assert not should_rotate(0)
        assert not should_rotate(10 * MIB)
        assert should_rotate(10 * MIB + 1)

    def test_check_due_first_time_and_after_an_hour(self):
        assert check_due(None, 1000.0)
        assert not check_due(1000.0, 1000.0 + 3599)
        assert check_due(1000.0, 1000.0 + 3600)

    def test_clock_going_backwards_is_due(self):
        assert check_due(5000.0, 1000.0)


class TestRotateNames:
    def test_oldest_first_three_generations(self):
        p = Path("/l/auto.log")
        assert rotate_names(p) == [
            (Path("/l/auto.log.2"), Path("/l/auto.log.3")),
            (Path("/l/auto.log.1"), Path("/l/auto.log.2")),
            (Path("/l/auto.log"), Path("/l/auto.log.1")),
        ]

    def test_generation_count(self):
        assert len(rotate_names(Path("x.log"), generations=5)) == 5


class TestRotateFile:
    def test_copies_to_dot_one_and_truncates_in_place(self, tmp_path):
        log = tmp_path / "auto.log"
        log.write_bytes(b"abc" * 10)
        inode = log.stat().st_ino

        rotate_file(log)

        assert (tmp_path / "auto.log.1").read_bytes() == b"abc" * 10
        assert log.stat().st_size == 0
        assert log.stat().st_ino == inode  # launchd's descriptor stays valid

    def test_append_writer_keeps_working_after_truncate(self, tmp_path):
        log = tmp_path / "auto.log"
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        os.write(fd, b"x" * 100)
        rotate_file(log)
        os.write(fd, b"new")
        os.close(fd)
        assert log.read_bytes() == b"new"

    def test_shifts_generations_and_drops_the_oldest(self, tmp_path):
        log = tmp_path / "auto.log"
        for name, body in [("", b"cur"), (".1", b"g1"), (".2", b"g2"), (".3", b"g3")]:
            (tmp_path / f"auto.log{name}").write_bytes(body)

        rotate_file(log)

        assert (tmp_path / "auto.log.1").read_bytes() == b"cur"
        assert (tmp_path / "auto.log.2").read_bytes() == b"g1"
        assert (tmp_path / "auto.log.3").read_bytes() == b"g2"
        assert not (tmp_path / "auto.log.4").exists()

    def test_missing_file_is_a_no_op(self, tmp_path):
        rotate_file(tmp_path / "nope.log")
        assert list(tmp_path.iterdir()) == []


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    out, err = service.log_paths(home)
    out.parent.mkdir(parents=True)
    monkeypatch.setattr(logrotate.sys, "platform", "darwin")
    return home, out, err


class TestServiceLogFiles:
    def test_empty_unless_the_service_marker_is_set(self, mac_home):
        home, out, err = mac_home
        assert service_log_files(home=home, env={}) == []
        assert service_log_files(home=home, env={"CC_SWAP_SERVICE": "0"}) == []

    def test_the_two_service_logs_on_macos(self, mac_home):
        home, out, err = mac_home
        assert service_log_files(home=home, env={"CC_SWAP_SERVICE": "1"}) == [out, err]

    def test_linux_journal_means_nothing_to_rotate(self, mac_home, monkeypatch):
        home, *_ = mac_home
        monkeypatch.setattr(logrotate.sys, "platform", "linux")
        assert service_log_files(home=home, env={"CC_SWAP_SERVICE": "1"}) == []

    def test_symlinked_log_pointing_elsewhere_is_refused(self, mac_home, tmp_path):
        home, out, err = mac_home
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret").write_bytes(b"x" * (MAX_BYTES + 1))
        out.symlink_to(elsewhere / "secret")
        assert out not in service_log_files(home=home, env={"CC_SWAP_SERVICE": "1"})


class TestLogRotator:
    ENV = {"CC_SWAP_SERVICE": "1"}

    def test_rotates_oversized_logs_at_startup(self, mac_home):
        home, out, err = mac_home
        out.write_bytes(b"a" * (MAX_BYTES + 1))
        err.write_bytes(b"small")

        LogRotator(home=home, env=self.ENV, clock=lambda: 0.0).maybe_rotate()

        assert out.stat().st_size == 0
        assert out.with_name("auto.log.1").stat().st_size == MAX_BYTES + 1
        assert err.read_bytes() == b"small"
        assert not err.with_name("auto.err.log.1").exists()

    def test_at_most_once_per_hour(self, mac_home):
        home, out, _ = mac_home
        now = [0.0]
        r = LogRotator(home=home, env=self.ENV, clock=lambda: now[0])
        r.maybe_rotate()
        out.write_bytes(b"a" * (MAX_BYTES + 1))

        now[0] = 1800.0
        r.maybe_rotate()
        assert out.stat().st_size == MAX_BYTES + 1  # not yet

        now[0] = 3600.0
        r.maybe_rotate()
        assert out.stat().st_size == 0

    def test_does_nothing_outside_the_service(self, mac_home):
        home, out, _ = mac_home
        out.write_bytes(b"a" * (MAX_BYTES + 1))
        LogRotator(home=home, env={}, clock=lambda: 0.0).maybe_rotate()
        assert out.stat().st_size == MAX_BYTES + 1

    def test_errors_never_escape(self, mac_home, monkeypatch):
        home, out, _ = mac_home
        out.write_bytes(b"a" * (MAX_BYTES + 1))

        def boom(path):
            raise OSError("disk full")

        monkeypatch.setattr(logrotate, "rotate_file", boom)
        LogRotator(home=home, env=self.ENV, clock=lambda: 0.0).maybe_rotate()
