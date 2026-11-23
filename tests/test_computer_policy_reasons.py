"""Risk explanations attached to the policy decision for a native action batch."""

import pytest

from agent.runtime.computer_policy import ComputerRisk, classify_computer_batch


def classify(actions, **metadata):
    context = {
        "metadata_trusted": True, "session_id": "session", "bundle_id": "com.kingsoft.wpsoffice.mac",
        "application": "Editor", "window_ref": "document", "window_title": "Notes",
        "window_role": "AXWindow", "elements": {"field": {"role": "AXTextField", "label": "Name"}},
        "focused_element_ref": "field",
    }
    return classify_computer_batch({**context, **metadata}, actions)


TYPE = {"type": "type", "element_ref": "field", "text": "value"}
CLICK = {"type": "click", "element_ref": "field"}
ENTER = {"type": "keypress", "key": "return"}
SCROLL = {"type": "scroll"}


@pytest.mark.parametrize(("reason", "action", "metadata", "risk"), [
    ("prohibited", CLICK, {"window_title": "Password"}, "prohibited"),
    ("untrusted_metadata", SCROLL, {"metadata_trusted": False}, "high_impact"),
    ("save_new_copy_to", {**ENTER, "save_new_copy_to": "/fixture/report.docx"}, {}, "high_impact"),
    ("missing_bundle", SCROLL, {"bundle_id": ""}, "high_impact"),
    ("unprofiled_bundle", SCROLL, {"bundle_id": "com.example.unprofiled"}, "high_impact"),
    ("element_identity_incomplete", CLICK, {"elements": {"field": {"label": "Name"}}}, "high_impact"),
    ("target_metadata_incomplete", CLICK, {"window_role": ""}, "high_impact"),
    ("system_bundle", SCROLL, {"bundle_id": "com.apple.systemsettings"}, "high_impact"),
    ("keyword_window_title", TYPE, {"window_title": "Save As"}, "high_impact"),
    ("keyword_element", TYPE, {"application": "Export utility"}, "high_impact"),
    ("terminal_text", TYPE, {"bundle_id": "com.apple.Terminal"}, "high_impact"),
    ("unreferenced_enter", ENTER, {}, "high_impact"),
    ("command_side_effect", {"type": "keypress", "key": "s", "modifiers": ["command"]}, {}, "high_impact"),
    ("finder_text_or_drag", TYPE, {"bundle_id": "com.apple.finder"}, "high_impact"),
    ("unreferenced_pointer", {"type": "click", "x": 12, "y": 16}, {"bundle_id": "com.apple.finder"}, "high_impact"),
    ("unknown_action", {"type": "unknown"}, {}, "high_impact"),
    ("risk_hint", {**CLICK, "risk_hint": "high"}, {}, "high_impact"),
])
def test_reports_each_risk_reason(reason, action, metadata, risk):
    decision = classify([action], **metadata)
    assert decision.risk.value == risk
    assert reason in decision.reasons


def test_collects_all_batch_reasons_even_after_prohibited_and_high_matches():
    decision = classify(
        [{**TYPE, "risk_hint": "high"}, {**ENTER, "save_new_copy_to": "/fixture/report.docx"}, ENTER],
        window_title="Save", elements={"field": {"label": "Password", "role": "AXTextField"}},
    )
    assert decision.risk is ComputerRisk.PROHIBITED
    assert set(decision.reasons) == {
        "prohibited", "keyword_window_title", "risk_hint", "unreferenced_enter", "save_new_copy_to",
    }


@pytest.mark.parametrize("field", ["label", "help", "actions"])
def test_non_title_keyword_excludes_title_only_reason(field):
    element = {"role": "AXTextField", field: ["submit"] if field == "actions" else "Submit"}
    decision = classify([TYPE], window_title="Save", elements={"field": element})
    assert set(decision.reasons) == {"keyword_element"}


def test_disjoint_title_and_element_matches_both_survive_batch():
    decision = classify([TYPE, {**CLICK, "element_ref": "confirm"}], window_title="Save", elements={
        "field": {"role": "AXTextField", "label": "Name"},
        "confirm": {"role": "AXButton", "label": "Overwrite"},
    })
    assert set(decision.reasons) == {"keyword_element", "keyword_window_title"}


def test_api_does_not_accept_model_authored_reasons_or_typed_content_as_evidence():
    decision = classify([{**TYPE, "text": "Save", "reasons": ["prohibited"]}])
    assert decision.risk is ComputerRisk.ORDINARY
    assert not decision.reasons
    assert not classify([{"type": "wait"}], metadata_trusted=False).reasons


def test_wait_can_raise_its_own_risk_without_inheriting_target_semantics():
    decision = classify([{"type": "wait", "risk_hint": "high_impact"}], window_title="Password")
    assert decision.risk is ComputerRisk.HIGH_IMPACT
    assert set(decision.reasons) == {"risk_hint"}
