"""Menu bar under the engine lease: the toggle's label names the other engine."""

from claude_swap import menubar


def test_autoswitch_item_title_is_upstreams_when_the_menu_bar_can_run_an_engine():
    assert menubar.autoswitch_item_title(False) == "Auto-switch accounts"


def test_autoswitch_item_title_names_the_other_engine_when_the_lease_is_taken():
    title = menubar.autoswitch_item_title(True)
    assert title.startswith("Auto-switch accounts")
    assert "another cc-swap engine" in title
