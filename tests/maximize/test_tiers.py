"""Tiers and plan weights (spec §5.1, §8)."""

from __future__ import annotations

import json

import pytest

from claude_swap.maximize.plan import (
    parse_plan_override,
    plan_weight,
    rate_limit_tier_from_credentials,
)
from claude_swap.exceptions import ConfigError
from claude_swap.maximize.tiers import (
    last_resort_entry,
    last_resort_matches,
    parse_account_list,
    tier_for,
    toggle_last_resort,
)


class TestTiers:
    def test_parse_account_list(self):
        assert parse_account_list(None) == ()
        assert parse_account_list("") == ()
        assert parse_account_list(" A@x.com, team ,a@x.com,,") == ("a@x.com", "team")

    def test_disabled_is_excluded_even_when_listed_last_resort(self):
        rec = {"email": "a@x.com", "disabled": True}
        assert tier_for(rec, "a@x.com", ("a@x.com",)) == "excluded"

    def test_email_match_is_case_insensitive(self):
        assert tier_for({}, "A@X.com", ("a@x.com",)) == "last_resort"

    def test_alias_match(self):
        assert tier_for({"alias": "Team"}, "t@x.com", ("team",)) == "last_resort"

    def test_default_is_normal(self):
        assert tier_for({"alias": "work"}, "w@x.com", ("team",)) == "normal"


class TestPlanWeight:
    @pytest.mark.parametrize(
        ("tier", "expected"),
        [
            ("default_claude_max_20x", 4),
            ("default_claude_max_5x", 1),
            ("default_claude_ai", 1),
            (None, 1),
        ],
    )
    def test_from_rate_limit_tier(self, tier, expected):
        assert plan_weight(tier, "a@x.com", None) == expected

    def test_override_wins_over_stored_tier(self):
        assert plan_weight("default_claude_max_20x", "A@x.com", "a@x.com:5x") == 1
        assert plan_weight(None, "b@x.com", "a@x.com:5x, b@x.com:20x") == 4

    def test_override_ignores_malformed_items(self):
        assert parse_plan_override("nocolon,:20x,a@x.com:7x,b@x.com:20X") == {
            "b@x.com": 4
        }

    def test_rate_limit_tier_from_credentials(self):
        blob = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-secret",
                "rateLimitTier": "default_claude_max_20x",
            }
        })
        assert rate_limit_tier_from_credentials(blob) == "default_claude_max_20x"
        assert rate_limit_tier_from_credentials("") is None
        assert rate_limit_tier_from_credentials("not json") is None
        assert rate_limit_tier_from_credentials("[1, 2]") is None
        assert rate_limit_tier_from_credentials(
            json.dumps({"claudeAiOauth": {"rateLimitTier": None}})
        ) is None


# -- last-resort entries (moved from cli.py for the TUI's `l` key) ---------------

ACCOUNTS = {
    "1": {"email": "a@x.com"},
    "2": {"email": "team@x.com", "alias": "work"},
    "3": {"email": "Team@x.com"},
    "4": {"email": "d@x.com", "alias": "dee"},
}


def test_toggle_last_resort_adds_email_or_alias_and_removes_all_matches():
    # Unique email: the email is the entry, appended after what is there.
    assert toggle_last_resort(ACCOUNTS, None, "1") == "a@x.com"
    assert toggle_last_resort(ACCOUNTS, "dee", "1") == "dee,a@x.com"
    # Shared email: the alias names exactly this account.
    assert toggle_last_resort(ACCOUNTS, "", "2") == "work"
    # Already marked (by email or alias, any case): every matching entry goes.
    assert toggle_last_resort(ACCOUNTS, "A@X.com,dee,a@x.com", "1") == "dee"
    assert toggle_last_resort(ACCOUNTS, "D@x.com, Dee", "4") == ""
    assert last_resort_matches(ACCOUNTS, "team@x.com") == ["2", "3"]
    assert last_resort_entry(ACCOUNTS, "4", "d@x.com") == "d@x.com"


def test_toggle_last_resort_shared_email_without_alias_raises():
    with pytest.raises(
        ConfigError,
        match=r"shared by Team and work; give Team an alias first \(cc-swap alias Team NAME\)",
    ):
        toggle_last_resort(ACCOUNTS, None, "3")


def test_cli_keeps_the_private_names():
    from claude_swap import cli

    assert cli._last_resort_entry is last_resort_entry
    assert cli._last_resort_matches is last_resort_matches
