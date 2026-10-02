"""Menu bar under the engine lease: the toggle's label names the other engine."""

from claude_swap import menubar


def test_autoswitch_item_title_is_upstreams_when_the_menu_bar_can_run_an_engine():
    assert menubar.autoswitch_item_title(False) == "Auto-switch accounts"


def test_autoswitch_item_title_names_the_other_engine_when_the_lease_is_taken():
    title = menubar.autoswitch_item_title(True)
    assert title.startswith("Auto-switch accounts")
    assert "another cc-swap engine" in title


def test_a_viewer_with_auto_switch_on_retries_the_claim_on_the_refresh_tick():
    assert menubar.should_retry_engine_claim(
        auto_switch_enabled=True, engine_running=False, engine_elsewhere=True
    ) is True


def test_nothing_to_retry_when_the_menu_bar_already_runs_its_engine():
    assert menubar.should_retry_engine_claim(
        auto_switch_enabled=True, engine_running=True, engine_elsewhere=False
    ) is False


def test_nothing_to_retry_when_auto_switch_is_off():
    # The user switched it off: leaving viewer mode would start an engine they
    # declined.
    assert menubar.should_retry_engine_claim(
        auto_switch_enabled=False, engine_running=False, engine_elsewhere=True
    ) is False


def test_nothing_to_retry_when_the_menu_bar_never_saw_another_engine():
    # The failed-to-start path (engine_elsewhere stays False) is not contention.
    assert menubar.should_retry_engine_claim(
        auto_switch_enabled=True, engine_running=False, engine_elsewhere=False
    ) is False
