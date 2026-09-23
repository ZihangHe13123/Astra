"""Failure paths in the live CU runner must not submit or replay an unverified URL."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from scripts.cu_live_acceptance import Runner


def runner_for_omnibox(typed, field_value="old address"):
    runner = Runner.__new__(Runner)
    runner.args = SimpleNamespace(port=8771)
    runner.input_source = lambda source: source
    runner.type_into = AsyncMock(return_value=typed)
    runner.suggestion_popup = AsyncMock(return_value=None)
    runner.fixture_window = AsyncMock(return_value=("app", "window"))
    runner.observe = AsyncMock(return_value={"ax_tree": {"role": "AXTextField", "label": "Address",
                                                       "element_ref": "field", "value": field_value}})
    runner.act = AsyncMock()
    return runner


def test_omnibox_does_not_submit_when_typing_was_refused():
    runner = runner_for_omnibox({"attempts": [{"code": "background_action_unsupported",
                                               "dispatch": "not_dispatched"}],
                                 "last_code": "background_action_unsupported", "unknown": False})
    result = asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "abc"))
    assert result["passed"] is False
    runner.suggestion_popup.assert_not_awaited()
    runner.act.assert_not_awaited()


def test_omnibox_does_not_submit_when_address_does_not_match_this_run():
    runner = runner_for_omnibox({"attempts": [{"code": "", "dispatch": "acknowledged"}],
                                 "last_code": "", "unknown": False})
    result = asyncio.run(runner.omnibox(SimpleNamespace(), "nonce123", "com.apple.keylayout.ABC", "abc"))
    assert result["passed"] is False
    assert result["omnibox_has_url"] is False
    runner.act.assert_not_awaited()


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
