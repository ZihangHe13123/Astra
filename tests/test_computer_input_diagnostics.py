"""Input execution evidence remains bounded and distinct from app effects."""

import pytest

from agent.runtime.computer_protocol import (
    ComputerAction, ComputerActionOutcome, KeyboardFailureDiagnostics,
)
from agent.runtime.tools.computer import _model_action_metadata


DIAGNOSTICS = {
    "stage": "before_key_up", "cause": "stale_snapshot",
    "input_may_have_started": True, "cleanup_failed": False,
}


def test_keyboard_failure_diagnostics_round_trip_without_claiming_effect():
    raw = {"index": 0, "ok": False, "error_code": "unknown_outcome", "input_diagnostics": DIAGNOSTICS}
    parsed = ComputerActionOutcome.from_mapping(raw, expected_index=0)
    assert parsed.to_mapping() == raw
    public = _model_action_metadata({"outcomes": [parsed.to_mapping()], "last_acknowledged_action": -1})
    assert public["outcomes"] == [raw]
    assert public.get("effect_verification") != "verified"
    assert public.get("status") != "action_acknowledged"


@pytest.mark.parametrize("payload", [
    None, [], {}, {**DIAGNOSTICS, "stage": "arbitrary text"},
    {**DIAGNOSTICS, "cause": "AX secret exception"},
    {**DIAGNOSTICS, "cause": []}, {**DIAGNOSTICS, "stage": []},
    {**DIAGNOSTICS, "input_may_have_started": 1},
    {**DIAGNOSTICS, "cleanup_failed": "false"},
    {**DIAGNOSTICS, "focus_text": "secret"},
    {**DIAGNOSTICS, "input_may_have_started": False},
    {**DIAGNOSTICS, "stage": "before_key_down", "input_may_have_started": False, "cleanup_failed": True},
])
def test_keyboard_failure_diagnostics_reject_and_do_not_expose_malformed_data(payload):
    with pytest.raises(ValueError):
        KeyboardFailureDiagnostics.from_mapping(payload)
    public = _model_action_metadata({"outcomes": [
        {"index": 0, "ok": False, "error_code": "unknown_outcome", "input_diagnostics": payload},
    ]})
    assert "input_diagnostics" not in public["outcomes"][0]


def test_keyboard_failure_diagnostics_are_not_allowed_on_success():
    with pytest.raises(ValueError):
        ComputerActionOutcome.from_mapping({
            "index": 0, "ok": True, "input_diagnostics": DIAGNOSTICS,
        }, expected_index=0)


@pytest.mark.parametrize(("key", "canonical"), [
    ("ArrowLeft", "left"), ("ArrowRight", "right"), ("ArrowUp", "up"),
    ("ArrowDown", "down"), ("Esc", "escape"), ("Enter", "return"),
    ("Backspace", "delete"),
])
def test_common_key_aliases_use_native_names_without_changing_target_or_modifiers(key, canonical):
    request = {"type": "keypress", "key": key, "element_ref": "ax_field", "modifiers": ["shift"]}
    action = ComputerAction.from_mapping(request)
    assert action.to_mapping() == {**request, "key": canonical}


@pytest.mark.parametrize("key", ["ArrowDiagonal", "Right+Enter", "Control+K", "F100"])
def test_unknown_key_names_are_argument_errors_before_dispatch(key):
    with pytest.raises(ValueError, match="unsupported key name"):
        ComputerAction.from_mapping({"type": "keypress", "key": key})
