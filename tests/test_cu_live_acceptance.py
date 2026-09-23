"""Failure paths in the live CU runner must not submit or replay an unverified URL."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from scripts.cu_live_acceptance import (
    SCENARIOS, Fixture, Runner, exact_fixture_address, hover_point, observed_fixture_navigation,
    pick_suggestion_window, status_strip_seen,
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


def test_hover_point_is_the_fixture_link_center_on_screen():
    tree = {"role": "AXWindow", "children": [
        {"role": "AXLink", "label": "Fixture hover link", "bounds": {"x": 40, "y": 200, "width": 120, "height": 20}},
    ]}
    assert hover_point(tree, {"x": 25, "y": 30, "width": 1319, "height": 768}) == (125, 240)
    assert hover_point({"role": "AXWindow"}, {"x": 0, "y": 0, "width": 10, "height": 10}) is None


def test_status_strip_evidence_is_only_the_helper_waiver_marker():
    assert status_strip_seen(["[action_rejected] 2026 OVERLAY-PASSIVE status_strip [dx=3 dy=741 w=437 h=24]"])
    # Live Edge exposes the strip as a help tag window; the tooltip waiver covers it.
    assert status_strip_seen(["[action_rejected] 2026 OVERLAY-PASSIVE help_tag [dx=3 dy=741 w=249 h=24]"])
    assert not status_strip_seen(["[action_rejected] 2026 OVERLAY-PASSIVE help_tag [dx=3 dy=100 w=249 h=120]"])
    assert not status_strip_seen(["[action_rejected] 2026 OVERLAY-DETAIL ax=false visible=true"])


def test_omnibox_hover_types_by_shortcut_and_passes_only_with_strip_evidence():
    assert "omnibox-hover" in SCENARIOS
    typed = {"attempts": [{"code": "", "dispatch": "acknowledged", "acknowledged": 2}], "last_code": "",
             "delivered": True, "unknown": False}
    for strip in (False, True):
        runner = runner_for_omnibox(typed, "127.0.0.1:8771/text?step=omnibox-hover&nonce=nonce123")
        moves = []
        runner.locate_pointer = lambda: (700.0, 5.0)
        runner.move_pointer = lambda x, y: moves.append((x, y))
        runner.hover_fixture_link = AsyncMock(return_value=(125, 240))
        runner.diagnostics_offset = lambda: 0
        runner.diagnostics_since = lambda _offset, seen=strip: (
            ["OVERLAY-PASSIVE status_strip [dx=3 dy=741 w=437 h=24]"] if seen else [])
        runner.navigated = AsyncMock(return_value=True)
        runner.act = AsyncMock(return_value=({"code": "", "computer_receipt": {}}, None))
        result = asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "hover",
                                            select="hover"))
        runner.hover_fixture_link.assert_awaited_once()
        assert runner.type_omnibox.await_args.kwargs == {"select": "shortcut"}
        assert result["status_strip_seen"] is strip
        assert result["passed"] is strip
        # The pointer returns to where the user left it.
        assert moves == [(700.0, 5.0)]


def test_suggestion_window_is_never_a_bottom_edge_status_strip():
    """Live 20:48: a pointer left on the fixture link kept Edge's status strip open, and the runner
    took that untitled window for the suggestion list."""
    main = {"window_ref": "main", "title": "Astra CU Text Fixture", "bindable": True,
            "bounds": {"x": 25, "y": 30, "width": 1319, "height": 768}}
    strip = {"window_ref": "strip", "title": "", "bindable": True,
             "bounds": {"x": 28, "y": 771, "width": 249, "height": 24}}
    dropdown = {"window_ref": "dropdown", "title": "", "bindable": True,
                "bounds": {"x": 89, "y": 70, "width": 1038, "height": 199}}
    assert pick_suggestion_window(main, [main, strip, dropdown]) is dropdown
    assert pick_suggestion_window(main, [main, strip]) is None


def runner_for_omnibox_click(observations, results, *, overlay_wait_on=None):
    runner = Runner.__new__(Runner)
    runner.args = SimpleNamespace(port=8771)
    runner.report = {"overlay_waits": []}
    runner.input_source = lambda source: source
    runner.open_fixture_tab = AsyncMock(return_value="127.0.0.1:8771/text?step=setup-click&nonce=nonce123")
    runner.fixture_window = AsyncMock(return_value=("app", "window"))
    calls = iter(observations)

    async def observe(*_):
        index, observation = next(calls)
        if index == overlay_wait_on:
            runner.report["overlay_waits"].append(0.5)
        return observation

    runner.observe = observe
    runner.act = AsyncMock(side_effect=[(result, None) for result in results])
    runner.navigated = AsyncMock(return_value=True)
    return runner


def page(value, **extra):
    return {"snapshot_id": "s", "ax_tree": {"role": "AXTextField", "label": "Address", "element_ref": "field",
                                            "value": value}, **extra}


CLICK_URL = "127.0.0.1:8771/text?step=omnibox-click&nonce=nonce123"
ACKED = {"code": "", "computer_receipt": {"dispatch_state": "acknowledged"}}


def test_omnibox_click_types_and_submits_through_the_page_window():
    observations = list(enumerate([
        page("http://127.0.0.1:8771/text?step=setup-click&nonce=nonce123"),
        page("http://127.0.0.1:8771/text?step=setup-click&nonce=nonce123",
             suggestion_popups=[{"x": 64, "y": 40, "width": 1038, "height": 471}]),
        page(CLICK_URL),
    ]))
    runner = runner_for_omnibox_click(observations, [ACKED, ACKED, ACKED])
    result = asyncio.run(runner.omnibox_click(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "click"))
    assert result["passed"] is True and result["popup_reported"] is True
    sent = [call.args[1] for call in runner.act.await_args_list]
    assert sent[0] == [{"type": "click", "element_ref": "field"}]
    assert [action["type"] for action in sent[1]] == ["keypress", "type"]
    assert sent[1][1]["text"] == CLICK_URL
    assert sent[2] == [{"type": "keypress", "key": "return", "element_ref": "field"}]


def test_omnibox_click_fails_when_the_page_was_observable_only_after_the_list_closed():
    observations = list(enumerate([
        page("http://127.0.0.1:8771/text?step=setup-click&nonce=nonce123"),
        page("http://127.0.0.1:8771/text?step=setup-click&nonce=nonce123"),
        page(CLICK_URL),
    ]))
    runner = runner_for_omnibox_click(observations, [ACKED, ACKED, ACKED], overlay_wait_on=1)
    result = asyncio.run(runner.omnibox_click(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "click"))
    assert result["page_observable"] is False
    assert result["passed"] is False


def test_omnibox_click_does_not_submit_when_the_typing_observation_was_pending():
    observations = list(enumerate([
        page("http://127.0.0.1:8771/text?step=setup-click&nonce=nonce123"),
        page("http://127.0.0.1:8771/text?step=setup-click&nonce=nonce123"),
    ]))
    pending = {"code": "post_action_observation_pending", "computer_receipt": {"dispatch_state": "acknowledged"}}
    runner = runner_for_omnibox_click(observations, [ACKED, pending])
    result = asyncio.run(runner.omnibox_click(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "click"))
    assert result["passed"] is False and result["stop"] is True
    assert runner.act.await_count == 2


def test_omnibox_click_fails_when_the_observation_after_return_was_pending():
    observations = list(enumerate([
        page("http://127.0.0.1:8771/text?step=setup-click&nonce=nonce123"),
        page("http://127.0.0.1:8771/text?step=setup-click&nonce=nonce123"),
        page(CLICK_URL),
    ]))
    pending = {"code": "post_action_observation_pending", "computer_receipt": {"dispatch_state": "acknowledged"}}
    runner = runner_for_omnibox_click(observations, [ACKED, ACKED, pending])
    result = asyncio.run(runner.omnibox_click(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "click"))
    assert result["navigated"] is True
    assert result["return_code"] == "post_action_observation_pending"
    assert result["passed"] is False


def test_the_click_route_submits_to_a_page_that_loads_like_a_real_site():
    from scripts.cu_live_acceptance import SLOW_STEPS
    assert SLOW_STEPS.get("omnibox-click", 0) >= 1

