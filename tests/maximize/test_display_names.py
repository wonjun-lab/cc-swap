"""The one display name an account goes by (maximize/names.py): alias, else
short name, made unique — deterministically, whatever order the accounts
come in, and never a whole address."""

from __future__ import annotations

import itertools

import pytest

from claude_swap.maximize import names


def test_alias_wins_over_the_short_name():
    got = names.display_names([("3", "dev.master@example.com", "main"),
                               ("1", "dev.shared@example.com", "")])
    assert got == {"3": "main", "1": "dev.shared"}


def test_same_address_in_two_organizations_says_the_org():
    got = names.record_names({
        "1": {"email": "same@example.com"},
        "4": {"email": "same@example.com", "organizationUuid": "o-1",
              "organizationName": "Acme Labs"},
    })
    assert got == {"1": "same·personal", "4": "same·Acme-Labs"}


def test_org_named_after_an_address_shows_no_address():
    got = names.record_names({
        "1": {"email": "same@example.com", "organizationUuid": "o-1",
              "organizationName": "same@example.com's Organization"},
        "2": {"email": "same@example.com"},
    })
    assert got["1"] == "same·sames-Organization"  # typeable: no quote, no space, no @
    assert got["2"] == "same·personal"


def test_org_name_without_uuid_is_personal():
    # An organizationName left behind without an organizationUuid is no org.
    got = names.record_names({
        "1": {"email": "same@example.com", "organizationName": "Stale"},
        "2": {"email": "same@example.com", "organizationUuid": "o-2",
              "organizationName": "Team"},
    })
    assert got == {"1": "same·personal", "2": "same·Team"}


def test_same_local_part_different_domain_and_a_twin_in_an_org():
    got = names.record_names({
        "1": {"email": "jo@uni.example.com"},
        "2": {"email": "jo@example.org"},
        "3": {"email": "jo@example.org", "organizationUuid": "o", "organizationName": "Acme"},
    })
    assert got == {"1": "jo@uni", "2": "jo@example·personal", "3": "jo@example·Acme"}


def test_addresses_differing_only_in_the_top_level_label_fall_back_to_the_slot():
    got = names.display_names([("1", "a@x.com", ""), ("2", "a@x.org", "")])
    assert got == {"1": "a@x·1", "2": "a@x·2"}


def test_two_orgs_with_one_name_fall_back_to_the_slot():
    got = names.display_names([("1", "s@example.com", "", "Team"),
                               ("2", "s@example.com", "", "team")])
    assert got == {"1": "s·1", "2": "s·2"}


ROSTER = [
    ("1", "dev.shared@example.com", "", ""),
    ("2", "jordan.lee@example.com", "", ""),
    ("3", "jordan.lee@uni.example.com", "", ""),
    ("4", "same@example.com", "", ""),
    ("5", "same@example.com", "", "Acme"),
    ("6", "side@example.com", "", ""),
    ("7", "other@example.com", "side", ""),
    ("8", "", "", ""),
]


def test_names_do_not_depend_on_order():
    want = names.display_names(ROSTER)
    for perm in itertools.islice(itertools.permutations(ROSTER), 0, 400, 7):
        assert names.display_names(perm) == want


def test_names_are_unique_and_never_a_whole_address():
    got = names.display_names(ROSTER)
    assert len({v.lower() for v in got.values()}) == len(got)
    for slot, email, _alias, _org in ROSTER:
        assert got[slot] != email
        if email:
            assert email not in got[slot]
    assert got["8"] == "#8"  # neither alias nor address: the slot is its only name


def test_adding_an_unrelated_account_keeps_every_name():
    before = names.display_names(ROSTER)
    after = names.display_names([*ROSTER, ("9", "newcomer@example.com", "", "")])
    assert {k: after[k] for k in before} == before


@pytest.mark.parametrize(("slot", "email", "expected"), [
    ("1", "", "dev"),                     # the name it has
    ("9", "late@example.com", "late"),    # unknown slot: the local part
    ("9", "", "#9"),                      # nothing else names it
    (None, "", "?"),
])
def test_name_of(slot, email, expected):
    assert names.name_of({"1": "dev"}, slot, email) == expected


def test_view_name_prefers_the_carried_name():
    from claude_swap.maximize.model import AccountView

    base = dict(number="2", email="side@example.com", tier="normal", plan_weight=1,
                pct5=None, reset5=None, pct7=None, reset7=None, quarantined=False,
                api_key=False)
    assert names.view_name(AccountView(**base)) == "side"
    assert names.view_name(AccountView(**base, name="work")) == "work"


def test_snapshot_views_carry_the_display_name():
    from claude_swap.maximize.snapshot import build_snapshot
    from claude_swap.settings import MaximizeSettings

    snap = build_snapshot(
        now=0.0, active="1", usage={},
        records={"1": {"email": "dev@example.com", "alias": "main"},
                 "2": {"email": "dev@uni.example.com"},
                 "3": {"email": "dev@example.org"}},
        quarantined=set(), api_key_accounts=set(), rate_limit_tiers={}, samples=(),
        last_switch_at=None, settings=MaximizeSettings(),
    )
    assert [v.name for v in snap.accounts] == ["main", "dev@uni", "dev@example"]


@pytest.mark.parametrize(("typed", "slot"), [
    ("main", "1"), ("MAIN", "1"), ("same·acme", "5"), ("same.Acme", "5"),
    ("same:acme", "5"), ("jordan.lee@uni", "3"), ("nobody", None), ("", None),
])
def test_match_name(typed, slot):
    shown = names.display_names([("1", "dev.shared@example.com", "main", ""), *ROSTER[1:7]])
    assert names.match_name(shown, typed) == slot


def test_cli_arg_quotes_only_when_needed():
    assert names.cli_arg("dev.shared") == "dev.shared"
    assert names.cli_arg("same·Acme Labs") == "'same·Acme Labs'"


def test_labeled_adds_the_address_only_to_an_alias():
    assert names.labeled("dev", "dev@example.com") == "dev"
    assert names.labeled("dev@uni", "dev@uni.example.com") == "dev@uni"
    assert names.labeled("main", "dev@example.com") == "main (dev@example.com)"


def test_cli_resolves_display_names(temp_home):
    import json

    from claude_swap import paths
    from claude_swap.switcher import ClaudeAccountSwitcher

    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": 1, "sequence": [1, 2, 3],
        "accounts": {"1": {"email": "dev.shared@example.com", "uuid": "u1"},
                     "2": {"email": "jo@example.com", "uuid": "u2"},
                     "3": {"email": "jo@uni.example.com", "uuid": "u3", "alias": "school"}},
    }))
    sw = ClaudeAccountSwitcher()
    assert sw._resolve_account_identifier("dev.shared") == "1"
    assert sw._resolve_account_identifier("jo") == "2"
    assert sw._resolve_account_identifier("school") == "3"
    assert sw._resolve_account_identifier("2") == "2"  # numbers still work
    assert sw.account_names() == {"1": "dev.shared", "2": "jo", "3": "school"}


def test_a_digits_only_local_part_never_reads_like_a_slot_number():
    got = names.display_names([("1", "123456789@qq.example.com", ""),
                               ("2", "dev@example.com", "")])
    assert got == {"1": "123456789@qq", "2": "dev"}
    # With no domain label to add, the slot tells (never a bare number).
    assert not names.display_names([("1", "42@x", "")])["1"].isdigit()


def test_an_alias_that_folds_like_a_short_name_takes_it_over():
    # ``same.acme`` (alias) and ``same:acme`` (local part) are one name typed
    # two ways: the short name gives way, as it does to an identical alias.
    got = names.display_names([("1", "x@example.com", "same.acme"),
                               ("2", "same:acme@example.com", "")])
    assert got["1"] == "same.acme" and names.fold(got["2"]) != names.fold(got["1"])
    assert names.match_name(got, "same·acme") == "1"


def test_deep_domains_join_with_a_dash_so_no_name_reads_like_an_address():
    from claude_swap.maximize import ledger, notify

    got = names.display_names([("1", "jo@cs.stanford.example.edu", ""),
                               ("2", "jo@ee.stanford.example.edu", ""),
                               ("3", "a@example.co.uk", ""), ("4", "a@example.com", "")])
    assert got["1"] == "jo@cs" and got["2"] == "jo@ee"
    deep = names.display_names([("1", "jo@mail.cs.example.edu", ""),
                                ("2", "jo@mail.ee.example.edu", "")])
    assert deep == {"1": "jo@mail-cs", "2": "jo@mail-ee"}
    for name in (*got.values(), *deep.values()):
        assert "." not in name.split("@", 1)[1]
        assert name in notify.scrub(f"switched to {name}.")
        assert name in ledger._EMAIL_RE.sub("<email>", f"{name} 5h 96%")


def test_roster_names_rereads_only_a_changed_file(tmp_path):
    import json
    import os

    path = tmp_path / "sequence.json"
    path.write_text(json.dumps({"accounts": {"1": {"email": "a@example.com"}}}))
    assert names.roster_names(tmp_path) == {"1": "a"}
    path.write_text(json.dumps({"accounts": {"1": {"email": "a@example.com", "alias": "work"}}}))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert names.roster_names(tmp_path) == {"1": "work"}
    assert names.roster_names(tmp_path / "missing") == {}


def test_labeled_tells_an_alias_that_starts_like_the_address():
    assert names.labeled("dev.shared2", "dev.shared@example.com") == (
        "dev.shared2 (dev.shared@example.com)"
    )
    assert names.labeled("same·Acme-Labs", "same@example.com") == "same·Acme-Labs"
