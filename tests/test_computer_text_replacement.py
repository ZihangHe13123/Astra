"""Explicit native field replacement wire contract (not GUI verification)."""

import pytest

from agent.runtime.computer_forms import observation_capabilities
from agent.runtime.computer_protocol import ComputerAction
from agent.runtime.tools.computer import ACTION_SCHEMA
from agent.runtime.tools.registry import ToolRegistry


@pytest.mark.parametrize("text", ["", "A中文🙂", "x" * 20_000], ids=["clear", "unicode", "limit"])
def test_replacement_wire_and_schema_require_explicit_field(text):
    payload = {"type": "type", "text": text, "element_ref": "fresh:field", "replace": True}
    assert ComputerAction.from_mapping(payload).to_mapping() == payload
    assert not ToolRegistry._schema_errors(ACTION_SCHEMA, payload, strict=True)


@pytest.mark.parametrize("changes", [
    {"element_ref": None}, {"replace": False}, {"replace": 1},
    {"replace": "true"}, {"replace": None}, {"x": 1}, {"key": "a"}, {"end_y": 2},
    {"modifiers": []}, {"key": None},
    {"delta_y": 1}, {"duration_ms": 1}, {"element_index": 1},
    {"modifiers": ["command"]}, {"text": "x" * 20_001},
    {"text": "\ud800"}, {"type": "keypress", "key": "a"},
])
def test_replacement_rejects_ambiguous_or_invalid_shape(changes):
    payload = {"type": "type", "text": "", "element_ref": "fresh:field", "replace": True, **changes}
    with pytest.raises(ValueError):
        ComputerAction.from_mapping(payload)


def test_ordinary_type_stays_wire_compatible_with_legacy_helpers():
    for text in ("", "abc中文"):
        payload = {"type": "type", "text": text}
        assert ComputerAction.from_mapping(payload).to_mapping() == payload


def test_replacement_capability_is_explicit_and_not_inferred():
    assert observation_capabilities({"observation_capabilities": ["replace_text_v1"]}) == ["replace_text_v1"]
    assert "replace_text_v1" not in observation_capabilities({})
    assert "replace_text_v1" not in observation_capabilities({"observation_capabilities": "replace_text_v1"})
