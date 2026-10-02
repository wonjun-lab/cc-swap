"""cc-swap settings sections: ``maximize`` and ``prime`` (spec §8)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    MaximizeSettings,
    PrimeSettings,
    effective_settings,
    load_maximize_settings,
    load_prime_settings,
    load_settings,
    merge_maximize_cli,
    parse_jitter_range,
    parse_setting_value,
    set_setting,
    settings_path,
    unset_setting,
)


def _write(tmp_path: Path, payload: dict) -> None:
    settings_path(tmp_path).write_text(json.dumps(payload))


def _flags(**kwargs) -> argparse.Namespace:
    values = {"soft5h": None, "hard5h": None, "soft7d": None, "hard7d": None}
    values.update(kwargs)
    return argparse.Namespace(**values)


class TestRegistry:
    def test_maximize_and_prime_fields_are_all_registered(self):
        by_section: dict[str, set[str]] = {}
        for spec in SETTING_SPECS.values():
            by_section.setdefault(spec.section, set()).add(spec.field)
        assert by_section["maximize"] == set(MaximizeSettings.__dataclass_fields__)
        assert by_section["prime"] == set(PrimeSettings.__dataclass_fields__)

    def test_json_keys_match_the_contract(self):
        keys = {spec.dotted for spec in SETTING_SPECS.values()}
        assert {
            "maximize.soft5h", "maximize.hard5h", "maximize.soft7d",
            "maximize.hard7d", "maximize.landingMargin", "maximize.idleWindowMin",
            "maximize.idleMaxDeltaPct", "maximize.forceEtaMin",
            "maximize.pendingPollS", "maximize.rebalanceCooldownMin",
            "maximize.tieEpsilon", "maximize.lastResort", "maximize.planOverride",
            "maximize.loginExpiryGuardMin",
            "prime.enabled", "prime.model", "prime.jitterS", "prime.maxAttempts",
            "prime.claudePath",
        } <= keys

    def test_ranges_match_the_spec(self):
        bounds = {
            spec.dotted: (spec.lo, spec.hi)
            for spec in SETTING_SPECS.values()
            if spec.section in ("maximize", "prime") and spec.lo is not None
        }
        assert bounds == {
            "maximize.soft5h": (1.0, 99.9),
            "maximize.hard5h": (1.0, 99.9),
            "maximize.soft7d": (1.0, 99.9),
            "maximize.hard7d": (1.0, 99.9),
            "maximize.landingMargin": (0.0, 30.0),
            "maximize.idleWindowMin": (3, 60),
            "maximize.idleMaxDeltaPct": (0.0, 10.0),
            "maximize.forceEtaMin": (0, 60),
            "maximize.pendingPollS": (180, 600),
            "maximize.rebalanceCooldownMin": (0, 240),
            "maximize.tieEpsilon": (0.0, 2.0),
            "maximize.loginExpiryGuardMin": (0, 1440),
            "prime.maxAttempts": (1, 5),
        }

    def test_maximize_is_an_autoswitch_strategy(self, tmp_path: Path):
        assert "maximize" in SETTING_SPECS["autoswitch.strategy"].choices
        set_setting(tmp_path, "autoswitch.strategy", "maximize")
        assert load_settings(tmp_path).strategy == "maximize"


class TestLoadMaximize:
    def test_missing_file_gives_defaults(self, tmp_path: Path):
        assert load_maximize_settings(tmp_path) == MaximizeSettings()

    def test_partial_section_fills_defaults(self, tmp_path: Path):
        _write(tmp_path, {"maximize": {"soft5h": 40, "lastResort": "a@x.com"}})
        loaded = load_maximize_settings(tmp_path)
        assert loaded.soft_5h == 40.0
        assert loaded.last_resort == "a@x.com"
        assert loaded.hard_5h == MaximizeSettings().hard_5h

    def test_values_are_clamped(self, tmp_path: Path):
        _write(tmp_path, {"maximize": {
            "soft5h": 0, "hard5h": 150, "idleWindowMin": 1,
            "pendingPollS": 9999, "tieEpsilon": -1,
        }})
        loaded = load_maximize_settings(tmp_path)
        assert loaded.soft_5h == 1.0
        assert loaded.hard_5h == 99.9
        assert loaded.idle_window_min == 3
        assert loaded.pending_poll_s == 600
        assert loaded.tie_epsilon == 0.0

    def test_pending_poll_never_below_the_poll_floor(self, tmp_path: Path):
        # Upstream poll_policy.MIN_INTERVAL_S: the per-account budget floor.
        assert MaximizeSettings().pending_poll_s == 180
        _write(tmp_path, {"maximize": {"pendingPollS": 60}})
        assert load_maximize_settings(tmp_path).pending_poll_s == 180

    def test_int_keys_truncate_floats(self, tmp_path: Path):
        _write(tmp_path, {"maximize": {"idleWindowMin": 12.7}})
        assert load_maximize_settings(tmp_path).idle_window_min == 12

    def test_bad_types_fall_back_to_defaults(self, tmp_path: Path):
        _write(tmp_path, {"maximize": {
            "soft7d": "high", "hard7d": True, "lastResort": 5,
        }})
        loaded = load_maximize_settings(tmp_path)
        assert loaded.soft_7d == MaximizeSettings().soft_7d
        assert loaded.hard_7d == MaximizeSettings().hard_7d
        assert loaded.last_resort is None

    def test_soft_above_hard_resets_that_window_only(self, tmp_path: Path):
        _write(tmp_path, {"maximize": {
            "soft5h": 96, "hard5h": 90, "soft7d": 80,
        }})
        problems: list[str] = []
        loaded = load_maximize_settings(tmp_path, problems=problems)
        assert (loaded.soft_5h, loaded.hard_5h) == (50.0, 95.0)
        assert loaded.soft_7d == 80.0  # the valid window keeps its value
        assert len(problems) == 1
        assert "maximize.soft5h (96) must not exceed maximize.hard5h (90)" in problems[0]

    def test_both_windows_broken_reports_both(self, tmp_path: Path):
        _write(tmp_path, {"maximize": {
            "soft5h": 96, "hard5h": 90, "soft7d": 99, "hard7d": 91,
        }})
        problems: list[str] = []
        loaded = load_maximize_settings(tmp_path, problems=problems)
        assert loaded == MaximizeSettings()
        assert len(problems) == 2

    def test_soft_equal_to_hard_is_valid(self, tmp_path: Path):
        _write(tmp_path, {"maximize": {"soft5h": 80, "hard5h": 80}})
        problems: list[str] = []
        loaded = load_maximize_settings(tmp_path, problems=problems)
        assert (loaded.soft_5h, loaded.hard_5h) == (80.0, 80.0)
        assert problems == []

    def test_load_soft_above_hard_reverts_pair(self, tmp_path: Path, caplog):
        # Header Review Focus 5: a hand-edited file with soft > hard, a string
        # in a numeric key and an unknown strategy still loads, pair by pair,
        # with a warning instead of an exception.
        _write(tmp_path, {
            "autoswitch": {"strategy": "fastest"},
            "maximize": {"soft7d": 99, "hard7d": 95, "hard5h": "high"},
        })
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            loaded = load_maximize_settings(tmp_path)
        assert (loaded.soft_7d, loaded.hard_7d) == (90.0, 98.0)  # pair reverted
        assert (loaded.soft_5h, loaded.hard_5h) == (50.0, 95.0)  # "high" -> default
        assert "maximize.soft7d (99) must not exceed maximize.hard7d (95)" in caplog.text
        assert load_settings(tmp_path).strategy == "best"


NON_FINITE_LITERALS = ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"]


class TestNonFiniteNumbers:
    """JSON allows NaN/Infinity (Python's parser accepts them), and a number
    that is not finite is not a threshold: it reads as a bad type -> default,
    like a string would. ``int(nan)`` used to raise out of the loader."""

    @pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
    @pytest.mark.parametrize("key, field", [
        ("idleWindowMin", "idle_window_min"),  # int kind: int(nan) raised
        ("pendingPollS", "pending_poll_s"),
        ("soft5h", "soft_5h"),  # float kind: NaN passed straight through
        ("tieEpsilon", "tie_epsilon"),
    ])
    def test_maximize_key_falls_back_to_its_default(
        self, tmp_path: Path, key, field, literal
    ):
        settings_path(tmp_path).write_text(
            '{"maximize": {"%s": %s}}' % (key, literal)
        )
        loaded = load_maximize_settings(tmp_path)
        assert getattr(loaded, field) == getattr(MaximizeSettings(), field)

    @pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
    def test_prime_max_attempts_falls_back_to_its_default(self, tmp_path, literal):
        settings_path(tmp_path).write_text('{"prime": {"maxAttempts": %s}}' % literal)
        assert load_prime_settings(tmp_path).max_attempts == PrimeSettings().max_attempts

    def test_other_keys_in_the_section_survive(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            '{"maximize": {"idleWindowMin": NaN, "forceEtaMin": 20}}'
        )
        loaded = load_maximize_settings(tmp_path)
        assert loaded.idle_window_min == MaximizeSettings().idle_window_min
        assert loaded.force_eta_min == 20

    def test_an_int_too_large_for_a_float_is_clamped_not_a_crash(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            '{"maximize": {"idleWindowMin": %s}}' % ("9" * 400)
        )
        assert load_maximize_settings(tmp_path).idle_window_min == 60

    def test_effective_settings_survives_non_finite_values(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            '{"maximize": {"idleWindowMin": NaN, "soft5h": Infinity}}'
        )
        rows = {spec.dotted: (value, is_set) for spec, value, is_set in effective_settings(tmp_path)}
        assert rows["maximize.idleWindowMin"] == (10, True)
        assert rows["maximize.soft5h"] == (50.0, True)

    @pytest.mark.parametrize("raw_value", ["nan", "NaN", "inf", "-inf", "Infinity", "1e400"])
    @pytest.mark.parametrize("dotted", [
        "maximize.soft5h",  # float kind
        "maximize.idleWindowMin",  # int kind
        "prime.maxAttempts",
    ])
    def test_strict_parse_rejects_non_finite(self, dotted, raw_value):
        with pytest.raises(ConfigError, match="finite"):
            parse_setting_value(SETTING_SPECS[dotted], raw_value)

    @pytest.mark.parametrize("raw_value", ["nan", "inf", "-inf"])
    def test_config_set_rejects_non_finite_without_writing(self, tmp_path, raw_value):
        with pytest.raises(ConfigError, match="finite"):
            set_setting(tmp_path, "maximize.tieEpsilon", raw_value)
        assert not settings_path(tmp_path).exists()


class TestProblemsReportEveryRepair:
    """``problems=`` lists each raw value the loader had to change, not only
    soft > hard pairs: the engine's hot reload keeps its previous values when
    the list is non-empty, so a silently repaired key must still show up."""

    def _load(self, tmp_path: Path, section: dict, caplog=None):
        _write(tmp_path, {"maximize": section})
        problems: list[str] = []
        loaded = load_maximize_settings(tmp_path, problems=problems)
        return loaded, problems

    def test_a_wrong_type_is_reported_with_its_default(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"hard5h": "high"})
        assert loaded.hard_5h == 95.0
        assert problems == [
            "maximize.hard5h must be a number, got 'high'; using default 95"
        ]

    @pytest.mark.parametrize("raw", [True, None, [5], {"a": 1}])
    def test_other_non_numbers_are_reported_too(self, tmp_path: Path, raw):
        _, problems = self._load(tmp_path, {"landingMargin": raw})
        assert len(problems) == 1
        assert problems[0].startswith("maximize.landingMargin must be a number, got ")
        assert problems[0].endswith("; using default 5")

    def test_bool_is_a_wrong_type_even_when_it_equals_the_default(self, tmp_path: Path):
        # idleMaxDeltaPct's default is 1.0 and True == 1.0: only a type check
        # (not comparing the result to the input) catches this one.
        loaded, problems = self._load(tmp_path, {"idleMaxDeltaPct": True})
        assert loaded.idle_max_delta_pct == 1.0
        assert len(problems) == 1

    @pytest.mark.parametrize("literal, shown", [
        ("NaN", "nan"), ("Infinity", "inf"), ("-Infinity", "-inf"),
    ])
    def test_non_finite_is_reported(self, tmp_path: Path, literal, shown):
        settings_path(tmp_path).write_text('{"maximize": {"idleWindowMin": %s}}' % literal)
        problems: list[str] = []
        loaded = load_maximize_settings(tmp_path, problems=problems)
        assert loaded.idle_window_min == 10
        assert problems == [
            f"maximize.idleWindowMin must be a finite number, got {shown}; "
            "using default 10"
        ]

    def test_clamped_above_is_reported(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"hard5h": 150})
        assert loaded.hard_5h == 99.9
        assert problems == ["maximize.hard5h is 150, outside 1-99.9; clamped to 99.9"]

    def test_clamped_below_is_reported_for_an_int_key(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"idleWindowMin": 1})
        assert loaded.idle_window_min == 3
        assert problems == ["maximize.idleWindowMin is 1, outside 3-60; clamped to 3"]

    def test_huge_int_is_reported_without_dumping_its_digits(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            '{"maximize": {"idleWindowMin": %s}}' % ("9" * 400)
        )
        problems: list[str] = []
        load_maximize_settings(tmp_path, problems=problems)
        assert len(problems) == 1
        assert "clamped to 60" in problems[0]
        assert len(problems[0]) < 200

    def test_a_fractional_value_for_an_int_key_is_reported(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"idleWindowMin": 12.7})
        assert loaded.idle_window_min == 12
        assert problems == [
            "maximize.idleWindowMin must be a whole number, got 12.7; truncated to 12"
        ]

    def test_an_integral_float_for_an_int_key_is_not_a_repair(self, tmp_path: Path):
        # Pure int truncation: 12.0 -> 12 changes nothing the user wrote.
        loaded, problems = self._load(tmp_path, {"idleWindowMin": 12.0, "pendingPollS": 240.0})
        assert (loaded.idle_window_min, loaded.pending_poll_s) == (12, 240)
        assert problems == []

    def test_an_int_for_a_float_key_is_not_a_repair(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"soft5h": 40, "tieEpsilon": 1})
        assert (loaded.soft_5h, loaded.tie_epsilon) == (40.0, 1.0)
        assert problems == []

    def test_values_on_the_range_edges_are_not_repairs(self, tmp_path: Path):
        _, problems = self._load(tmp_path, {"soft5h": 1, "hard5h": 99.9, "idleWindowMin": 60})
        assert problems == []

    def test_a_non_string_for_a_string_key_is_reported(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"lastResort": 5})
        assert loaded.last_resort is None
        assert problems == [
            "maximize.lastResort must be a non-empty string, got 5; "
            "using default (none)"
        ]

    @pytest.mark.parametrize("raw", [None, ""])
    def test_unsetting_a_string_key_with_null_or_empty_is_not_a_repair(
        self, tmp_path: Path, raw
    ):
        # The default is "unset", so null / "" already mean what they say.
        loaded, problems = self._load(tmp_path, {"lastResort": raw, "planOverride": raw})
        assert (loaded.last_resort, loaded.plan_override) == (None, None)
        assert problems == []

    def test_one_message_per_repaired_key_in_registry_order(self, tmp_path: Path):
        _, problems = self._load(tmp_path, {
            "idleWindowMin": 1, "soft5h": "x", "hard7d": 1000, "soft7d": 85,
        })
        assert [p.split()[0] for p in problems] == [
            "maximize.soft5h", "maximize.hard7d", "maximize.idleWindowMin",
        ]

    def test_a_pair_repair_adds_to_the_key_repairs(self, tmp_path: Path):
        # soft5h clamps to 99.9 (one message), which then exceeds hard5h 90
        # (a second message, for the pair).
        loaded, problems = self._load(tmp_path, {"soft5h": 150, "hard5h": 90})
        assert (loaded.soft_5h, loaded.hard_5h) == (50.0, 95.0)
        assert len(problems) == 2
        assert "maximize.soft5h is 150" in problems[0]
        assert "must not exceed" in problems[1]

    def test_clean_files_report_nothing(self, tmp_path: Path):
        for payload in (
            {},
            {"maximize": {}},
            {"maximize": {"soft5h": 40, "idleWindowMin": 15, "lastResort": "a@x.com"}},
            {"maximize": "not a section"},
        ):
            _write(tmp_path, payload)
            problems: list[str] = []
            load_maximize_settings(tmp_path, problems=problems)
            assert problems == [], payload

    def test_every_repair_is_also_logged_as_a_warning(self, tmp_path: Path, caplog):
        _write(tmp_path, {"maximize": {"hard5h": "high", "idleWindowMin": 1}})
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            load_maximize_settings(tmp_path)  # no problems= list: still logged
        warnings = [r.getMessage() for r in caplog.records]
        assert len(warnings) == 2
        assert any("maximize.hard5h must be a number" in w for w in warnings)
        assert any("maximize.idleWindowMin is 1" in w for w in warnings)

    def test_each_repair_is_logged_once_when_problems_is_given(self, tmp_path: Path, caplog):
        _write(tmp_path, {"maximize": {"hard5h": "high"}})
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            load_maximize_settings(tmp_path, problems=[])
        assert len(caplog.records) == 1

    def test_the_strict_path_stays_quiet_about_other_keys(self, tmp_path: Path, caplog):
        # `config set` reads the raw section to judge a soft/hard pair; it
        # must not log the lenient loader's repairs for unrelated keys.
        _write(tmp_path, {"maximize": {"idleWindowMin": "x"}})
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            set_setting(tmp_path, "maximize.soft5h", "40")
        assert caplog.records == []


class TestPrimeProblems:
    def _load(self, tmp_path: Path, section: dict):
        _write(tmp_path, {"prime": section})
        problems: list[str] = []
        return load_prime_settings(tmp_path, problems=problems), problems

    def test_clamped_max_attempts_is_reported(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"maxAttempts": 9})
        assert loaded.max_attempts == 5
        assert problems == ["prime.maxAttempts is 9, outside 1-5; clamped to 5"]

    def test_wrong_type_max_attempts_is_reported(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"maxAttempts": "many"})
        assert loaded.max_attempts == 2
        assert problems == ["prime.maxAttempts must be a number, got 'many'; using default 2"]

    def test_non_finite_max_attempts_is_reported(self, tmp_path: Path):
        settings_path(tmp_path).write_text('{"prime": {"maxAttempts": NaN}}')
        problems: list[str] = []
        loaded = load_prime_settings(tmp_path, problems=problems)
        assert loaded.max_attempts == 2
        assert problems == [
            "prime.maxAttempts must be a finite number, got nan; using default 2"
        ]

    @pytest.mark.parametrize("raw", ["", None, 5])
    def test_empty_or_wrong_type_model_is_reported(self, tmp_path: Path, raw):
        # Unlike lastResort, model's default is a value: replacing the user's
        # entry with it is a repair.
        loaded, problems = self._load(tmp_path, {"model": raw})
        assert loaded.model == "claude-haiku-4-5"
        assert len(problems) == 1
        assert problems[0].startswith("prime.model must be a non-empty string, got ")
        assert problems[0].endswith("; using default claude-haiku-4-5")

    def test_a_non_string_jitter_is_reported_once(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"jitterS": 5})
        assert loaded.jitter_s == "45-300"
        assert len(problems) == 1
        assert problems[0].startswith("prime.jitterS must be a non-empty string")

    def test_malformed_jitter_is_still_reported_exactly_once(self, tmp_path: Path):
        _, problems = self._load(tmp_path, {"jitterS": "300-45"})
        assert len(problems) == 1
        assert problems[0].startswith("prime.jitterS ")

    def test_a_non_bool_enabled_is_still_reported_exactly_once(self, tmp_path: Path):
        loaded, problems = self._load(tmp_path, {"enabled": "false"})
        assert loaded.enabled is False
        assert len(problems) == 1
        assert "prime.enabled" in problems[0]

    def test_clean_prime_section_reports_nothing(self, tmp_path: Path):
        _, problems = self._load(tmp_path, {
            "enabled": True, "model": "haiku", "jitterS": "60-120",
            "maxAttempts": 3, "claudePath": "/opt/claude",
        })
        assert problems == []
        _, problems = self._load(tmp_path, {"claudePath": None})
        assert problems == []

    def test_every_repair_is_logged(self, tmp_path: Path, caplog):
        _write(tmp_path, {"prime": {"maxAttempts": 9}})
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            load_prime_settings(tmp_path)
        assert [r.getMessage() for r in caplog.records] == [
            "settings.json: prime.maxAttempts is 9, outside 1-5; clamped to 5"
        ]


class TestLoadPrime:
    def test_missing_file_gives_defaults(self, tmp_path: Path):
        assert load_prime_settings(tmp_path) == PrimeSettings()
        assert PrimeSettings().enabled is False

    def test_reads_values(self, tmp_path: Path):
        _write(tmp_path, {"prime": {
            "enabled": True, "model": "haiku", "jitterS": "60-120",
            "maxAttempts": 3, "claudePath": "/opt/claude",
        }})
        assert load_prime_settings(tmp_path) == PrimeSettings(
            enabled=True, model="haiku", jitter_s="60-120",
            max_attempts=3, claude_path="/opt/claude",
        )

    def test_only_json_true_enables_priming(self, tmp_path: Path):
        _write(tmp_path, {"prime": {"enabled": "false"}})
        problems: list[str] = []
        assert load_prime_settings(tmp_path, problems=problems).enabled is False
        assert "prime.enabled" in problems[0]

    def test_malformed_jitter_reverts_to_default(self, tmp_path: Path):
        _write(tmp_path, {"prime": {"jitterS": "300-45"}})
        problems: list[str] = []
        assert load_prime_settings(tmp_path, problems=problems).jitter_s == "45-300"
        assert "prime.jitterS" in problems[0]

    def test_empty_model_reverts_to_default(self, tmp_path: Path):
        _write(tmp_path, {"prime": {"model": ""}})
        assert load_prime_settings(tmp_path).model == "claude-haiku-4-5"

    def test_max_attempts_clamped(self, tmp_path: Path):
        _write(tmp_path, {"prime": {"maxAttempts": 9}})
        assert load_prime_settings(tmp_path).max_attempts == 5


class TestParseJitterRange:
    @pytest.mark.parametrize("value,expected", [
        ("45-300", (45, 300)),
        (" 0 - 599 ", (0, 599)),
        ("120-120", (120, 120)),
    ])
    def test_valid(self, value, expected):
        assert parse_jitter_range(value) == expected

    @pytest.mark.parametrize("value", ["300-45", "45-600", "abc", "45", "-5-10", "1.5-3"])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            parse_jitter_range(value)


class TestStrictSet:
    def test_soft_above_default_hard_is_rejected_without_writing(self, tmp_path: Path):
        with pytest.raises(
            ConfigError,
            match=r"maximize\.soft5h \(96\) must not exceed maximize\.hard5h \(95\); "
                  r"change maximize\.hard5h first",
        ):
            set_setting(tmp_path, "maximize.soft5h", "96")
        assert not settings_path(tmp_path).exists()

    def test_hard_below_soft_is_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="change maximize.soft5h first"):
            set_setting(tmp_path, "maximize.hard5h", "40")

    def test_raising_hard_first_then_soft_is_accepted(self, tmp_path: Path):
        assert set_setting(tmp_path, "maximize.hard5h", "99") == 99.0
        assert set_setting(tmp_path, "maximize.soft5h", "96") == 96.0
        loaded = load_maximize_settings(tmp_path)
        assert (loaded.soft_5h, loaded.hard_5h) == (96.0, 99.0)

    def test_broken_pair_in_file_is_judged_raw_not_masked(self, tmp_path: Path):
        # Lenient load shows 50/95 for this pair; the check must use 55.
        _write(tmp_path, {"maximize": {"soft5h": 60, "hard5h": 55}})
        with pytest.raises(ConfigError, match=r"\(58\) must not exceed maximize\.hard5h \(55\)"):
            set_setting(tmp_path, "maximize.soft5h", "58")
        assert set_setting(tmp_path, "maximize.hard5h", "70") == 70.0

    def test_other_window_broken_does_not_block_an_edit(self, tmp_path: Path):
        _write(tmp_path, {"maximize": {"soft7d": 99, "hard7d": 90}})
        assert set_setting(tmp_path, "maximize.soft5h", "40") == 40.0

    def test_unset_that_would_break_the_pair_is_rejected(self, tmp_path: Path):
        set_setting(tmp_path, "maximize.hard5h", "98")
        set_setting(tmp_path, "maximize.soft5h", "96")
        before = settings_path(tmp_path).read_text()
        with pytest.raises(ConfigError, match="change maximize.soft5h first"):
            unset_setting(tmp_path, "maximize.hard5h")
        assert settings_path(tmp_path).read_text() == before

    def test_range_is_enforced(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="between 3 and 60"):
            set_setting(tmp_path, "maximize.idleWindowMin", "2")

    def test_jitter_format_is_enforced(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="prime.jitterS"):
            set_setting(tmp_path, "prime.jitterS", "abc")
        assert set_setting(tmp_path, "prime.jitterS", "30-200") == "30-200"

    def test_plan_override_format_is_enforced(self, tmp_path: Path):
        assert set_setting(
            tmp_path, "maximize.planOverride", "a@x.com:20x,b@y.com:5x"
        ) == "a@x.com:20x,b@y.com:5x"
        with pytest.raises(ConfigError, match="a@x.com:10x"):
            set_setting(tmp_path, "maximize.planOverride", "a@x.com:10x")

    def test_prime_enabled_bool_words(self, tmp_path: Path):
        assert set_setting(tmp_path, "prime.enabled", "yes") is True
        assert load_prime_settings(tmp_path).enabled is True

    def test_set_writes_only_that_key(self, tmp_path: Path):
        set_setting(tmp_path, "maximize.soft5h", "40")
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "maximize": {"soft5h": 40.0}}


class TestMergeMaximizeCli:
    def test_no_flags_returns_settings_unchanged(self):
        base = MaximizeSettings(soft_5h=40.0)
        assert merge_maximize_cli(base, _flags()) is base
        assert merge_maximize_cli(base, None) is base

    def test_flags_beat_settings(self):
        merged = merge_maximize_cli(
            MaximizeSettings(soft_5h=40.0, hard_7d=97.0), _flags(hard5h=90.0, soft7d=85.0)
        )
        assert (merged.soft_5h, merged.hard_5h) == (40.0, 90.0)
        assert (merged.soft_7d, merged.hard_7d) == (85.0, 97.0)

    def test_flags_are_clamped(self):
        merged = merge_maximize_cli(MaximizeSettings(), _flags(soft5h=0.5))
        assert merged.soft_5h == 1.0

    def test_soft_above_hard_after_merge_raises(self):
        with pytest.raises(ConfigError, match="maximize.soft5h"):
            merge_maximize_cli(MaximizeSettings(), _flags(soft5h=97.0))

    def test_accepts_objects_missing_some_attributes(self):
        merged = merge_maximize_cli(MaximizeSettings(), argparse.Namespace(soft7d=80.0))
        assert merged.soft_7d == 80.0


class TestConfigCli:
    def _run(self, argv, capsys):
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["cc-swap", "config", *argv]):
            code = 0
            try:
                cli.main()
            except SystemExit as e:
                code = e.code or 0
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def test_list_includes_new_sections(self, temp_home, capsys):
        code, out, _ = self._run([], capsys)
        assert code == 0
        assert "maximize.soft5h" in out
        assert "prime.enabled" in out

    def test_set_get_unset_round_trip(self, temp_home, capsys):
        assert self._run(["set", "maximize.soft5h", "40"], capsys)[0] == 0
        code, out, _ = self._run(["get", "maximize.soft5h"], capsys)
        assert (code, out.strip()) == (0, "40")
        code, out, _ = self._run(["unset", "maximize.soft5h"], capsys)
        assert code == 0
        assert "default: 50" in out

    def test_set_soft_above_hard_exits_1(self, temp_home, capsys):
        code, _, err = self._run(["set", "maximize.soft7d", "99"], capsys)
        assert code == 1
        assert "must not exceed maximize.hard7d (98)" in err

    def test_effective_settings_rows_cover_new_sections(self, tmp_path: Path):
        rows = {spec.dotted: (value, is_set) for spec, value, is_set in effective_settings(tmp_path)}
        assert rows["maximize.hard7d"] == (98.0, False)
        assert rows["prime.jitterS"] == ("45-300", False)


def test_login_expiry_guard_defaults_to_two_hours_and_loads(tmp_path: Path):
    assert MaximizeSettings().login_expiry_guard_min == 120
    _write(tmp_path, {"maximize": {"loginExpiryGuardMin": 30}})
    assert load_maximize_settings(tmp_path).login_expiry_guard_min == 30
    set_setting(tmp_path, "maximize.loginExpiryGuardMin", "0")
    assert load_maximize_settings(tmp_path).login_expiry_guard_min == 0
