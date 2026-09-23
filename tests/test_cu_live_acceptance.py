"""Failure paths in the live CU runner must not submit or replay an unverified URL."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from scripts.cu_live_acceptance import (
    SCENARIOS, Fixture, Runner, exact_fixture_address, observed_fixture_navigation,
)


def runner_for_omnibox(typed, field_value="old address"):
    runner = Runner.__new__(Runner)
    runner.args = SimpleNamespace(port=8771)
    runner.input_source = lambda source: source
    runner.open_fixture_tab = AsyncMock(return_value="127.0.0.1:8771/text?step=setup-abc&nonce=nonce123")
    runner.type_omnibox = AsyncMock(return_value=typed)
    runner.suggestion_popup = AsyncMock(return_value=None)
    runner.fixture_window = AsyncMock(return_value=("app", "window"))
    runner.observe = AsyncMock(return_value={"ax_tree": {"role": "AXTextField", "label": "Address",
                                                       "element_ref": "field", "value": field_value}})
    runner.act = AsyncMock()
    return runner


def test_omnibox_does_not_submit_when_typing_was_refused():
    runner = runner_for_omnibox({"attempts": [{"code": "background_action_unsupported",
                                               "dispatch": "not_dispatched"}],
                                 "last_code": "background_action_unsupported", "delivered": False,
                                 "unknown": False})
    result = asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "abc"))
    assert result["passed"] is False
    runner.suggestion_popup.assert_not_awaited()
    runner.act.assert_not_awaited()


def test_omnibox_does_not_submit_when_address_does_not_match_this_run():
    runner = runner_for_omnibox({"attempts": [{"code": "", "dispatch": "acknowledged"}],
                                 "last_code": "", "delivered": True, "unknown": False},
                                "127.0.0.1:8771/text?nonce=old"
                                "127.0.0.1:8771/text?step=omnibox-abc&nonce=nonce123")
    result = asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "abc"))
    assert result["passed"] is False
    assert result["omnibox_has_url"] is False
    runner.act.assert_not_awaited()


def test_type_omnibox_selects_and_types_into_the_same_verified_field():
    runner = Runner.__new__(Runner)
    runner.fixture_window = AsyncMock(return_value=("app", "window"))
    runner.observe = AsyncMock(return_value={"snapshot_id": "snapshot", "ax_tree": {
        "role": "AXTextField", "label": "Address", "element_ref": "field", "value": "http://127.0.0.1:8771/text?step=setup-abc&nonce=nonce123"}})
    runner.act = AsyncMock(return_value=({"code": "post_action_observation_pending",
                                          "computer_receipt": {"dispatch_state": "acknowledged", "acknowledged": 2}}, None))
    current = "127.0.0.1:8771/text?step=setup-abc&nonce=nonce123"
    target = "127.0.0.1:8771/text?step=omnibox-abc&nonce=nonce123"
    result = asyncio.run(runner.type_omnibox(current, target))
    assert result["delivered"] is True
    assert runner.act.await_args.args[1] == [
        {"type": "keypress", "key": "a", "modifiers": ["command"], "element_ref": "field"},
        {"type": "type", "element_ref": "field", "text": target},
    ]


def test_type_omnibox_shortcut_variant_leaves_text_routing_to_the_helper():
    """Cmd+L carries no element_ref, so the bound select-all rule does not apply; the
    helper's application-level keyboard routing alone must carry the typed address."""
    runner = Runner.__new__(Runner)
    runner.fixture_window = AsyncMock(return_value=("app", "window"))
    runner.observe = AsyncMock(return_value={"snapshot_id": "snapshot", "ax_tree": {
        "role": "AXTextField", "label": "Address", "element_ref": "field", "value": "http://127.0.0.1:8771/text?step=setup-shortcut&nonce=nonce123"}})
    runner.act = AsyncMock(return_value=({"code": "post_action_observation_pending",
                                          "computer_receipt": {"dispatch_state": "acknowledged", "acknowledged": 2}}, None))
    current = "127.0.0.1:8771/text?step=setup-shortcut&nonce=nonce123"
    target = "127.0.0.1:8771/text?step=omnibox-shortcut&nonce=nonce123"
    result = asyncio.run(runner.type_omnibox(current, target, select="shortcut"))
    assert result["delivered"] is True
    assert runner.act.await_args.args[1] == [
        {"type": "keypress", "key": "l", "modifiers": ["command"]},
        {"type": "type", "element_ref": "field", "text": target},
    ]


def test_omnibox_shortcut_scenario_is_selectable_and_passes_its_variant():
    assert "omnibox-shortcut" in SCENARIOS
    runner = runner_for_omnibox({"attempts": [{"code": "background_action_unsupported",
                                               "dispatch": "not_dispatched"}],
                                 "last_code": "background_action_unsupported", "delivered": False,
                                 "unknown": False})
    asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "shortcut",
                               select="shortcut"))
    assert runner.type_omnibox.await_args.kwargs == {"select": "shortcut"}


def test_omnibox_uses_only_a_unique_exact_popup_row():
    target = "127.0.0.1:8771/text?step=omnibox-abc&nonce=nonce123"
    runner = runner_for_omnibox({"attempts": [{"code": "post_action_observation_pending",
                                               "dispatch": "acknowledged", "acknowledged": 2}],
                                 "last_code": "post_action_observation_pending", "delivered": True,
                                 "unknown": False})
    runner.suggestion_popup = AsyncMock(return_value=("app", "popup"))
    runner.observe = AsyncMock(return_value={"ax_tree": {"role": "AXWindow", "children": [
        {"role": "AXStaticText", "value": target, "element_ref": "exact", "actions": ["AXPress"]},
        {"role": "AXStaticText", "value": target + "，Bing 搜索", "element_ref": "search",
         "actions": ["AXPress"]}]}})
    runner.act = AsyncMock(return_value=({"code": "post_action_observation_pending"}, None))
    runner.navigated = AsyncMock(return_value=True)
    result = asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "abc"))
    assert result["passed"] is True
    assert result["typed_through_popup"] is True
    assert runner.act.await_args.args[1] == [{"type": "click", "element_ref": "exact"}]


def test_omnibox_stops_on_appended_popup_url():
    target = "127.0.0.1:8771/text?step=omnibox-abc&nonce=nonce123"
    runner = runner_for_omnibox({"attempts": [{"code": "post_action_observation_pending",
                                               "dispatch": "acknowledged", "acknowledged": 2}],
                                 "last_code": "post_action_observation_pending", "delivered": True,
                                 "unknown": False})
    runner.suggestion_popup = AsyncMock(return_value=("app", "popup"))
    runner.observe = AsyncMock(return_value={"ax_tree": {"role": "AXStaticText",
                                                       "value": "old-address" + target,
                                                       "element_ref": "wrong", "actions": ["AXPress"]}})
    result = asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "abc"))
    assert result["passed"] is False
    assert result["stop"] is True
    runner.act.assert_not_awaited()


def test_omnibox_stops_on_duplicate_exact_popup_rows():
    target = "127.0.0.1:8771/text?step=omnibox-abc&nonce=nonce123"
    runner = runner_for_omnibox({"attempts": [{"code": "post_action_observation_pending",
                                               "dispatch": "acknowledged", "acknowledged": 2}],
                                 "last_code": "post_action_observation_pending", "delivered": True,
                                 "unknown": False})
    runner.suggestion_popup = AsyncMock(return_value=("app", "popup"))
    runner.observe = AsyncMock(return_value={"ax_tree": {"role": "AXWindow", "children": [
        {"role": "AXStaticText", "value": target, "element_ref": "first", "actions": ["AXPress"]},
        {"role": "AXStaticText", "value": target, "element_ref": "second", "actions": ["AXPress"]}]}})
    result = asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "abc"))
    assert result["passed"] is False
    assert result["stop"] is True
    runner.act.assert_not_awaited()


def test_exact_fixture_address_rejects_extra_prefix_or_search_suffix():
    target = "127.0.0.1:8771/text?step=omnibox-abc&nonce=nonce123"
    assert exact_fixture_address(target, target)
    assert exact_fixture_address("http://" + target, target)
    assert not exact_fixture_address("old" + target, target)
    assert not exact_fixture_address(target + "，Bing 搜索", target)


def test_navigation_requires_the_loaded_page_marker_and_exact_active_address():
    path = "/text?step=omnibox-abc&nonce=nonce123"
    url = "127.0.0.1:8771" + path
    tree = {"role": "AXWindow", "children": [
        {"role": "AXStaticText", "value": path},
        {"role": "AXTextField", "label": "Address", "value": url}]}
    assert observed_fixture_navigation(tree, "omnibox-abc", "nonce123", 8771)
    tree["children"][0]["value"] = "/text?step=setup-abc&nonce=nonce123"
    assert not observed_fixture_navigation(tree, "omnibox-abc", "nonce123", 8771)
    tree["children"][0]["value"] = path
    tree["children"][1]["value"] = "old" + url
    assert not observed_fixture_navigation(tree, "omnibox-abc", "nonce123", 8771)


def test_server_request_evidence_requires_the_text_route():
    fixture = Fixture.__new__(Fixture)
    fixture.lock = threading.Lock()
    fixture.requests = ["/state?step=omnibox-abc&nonce=nonce123"]
    assert not fixture.saw("omnibox-abc", "nonce123")
    fixture.requests.append("/text?step=omnibox-abc&nonce=nonce123")
    assert fixture.saw("omnibox-abc", "nonce123")


def test_type_into_does_not_replay_an_ax_write_with_unchanged_readback():
    runner = Runner.__new__(Runner)
    runner.fixture_window = AsyncMock(return_value=("app", "window"))
    runner.observe = AsyncMock(return_value={"snapshot_id": "snapshot", "ax_tree": {
        "role": "AXTextField", "label": "Address", "element_ref": "field"}})
    runner.act = AsyncMock(return_value=({"error": "focus", "code": "input_focus_required",
                                          "computer_receipt": {"dispatch_state": "not_dispatched"}}, None))
    result = asyncio.run(runner.type_into("AXTextField", ("Address",), "example"))
    assert result["last_code"] == "input_focus_required"
    assert runner.act.await_count == 1
