import json
from types import SimpleNamespace

import pytest

from agent.runtime.computer_feedback import PendingClickGuard, build_action_receipt, receipt_instruction
from agent.runtime.react import ReActAgent


def receipt(payload, **kwargs):
    return build_action_receipt(payload, mode="background", action_count=1, dispatch_attempted=True, **kwargs)


def ack(effect=None):
    return {"last_acknowledged_action": 0, "outcomes": [{"index": 0, "ok": True, **({"effect_verification": effect} if effect else {})}]}


def test_acknowledged_submit_and_observation_failure_remain_observe_only():
    value = receipt({"action_result": ack()}, error_code="unknown_outcome")
    assert value["dispatch_state"] == "acknowledged"
    assert value["verification_state"] == "unverified"
    assert value["next_step"] == "observe_result"
    assert value["replay"] == "forbidden"
    assert receipt({}, error_code="stale_snapshot")["next_step"] == "handoff"


@pytest.mark.parametrize("cause", [
    "overlay_blocked", "window_content_unavailable", "target_gone", "snapshot_failed", "observation_timeout", None,
])
def test_acknowledged_missing_observation_defers_to_recovery(cause):
    payload = {"action_result": ack(), **({"observation_error_code": cause} if cause else {})}
    value = receipt(payload, error_code="post_action_observation_pending")
    assert value["dispatch_state"] == "acknowledged"
    assert value["verification_state"] == "unverified"
    assert value["next_step"] == "follow_recovery"
    assert value["replay"] == "forbidden"
    guidance = receipt_instruction(value)
    assert "Recovery" in guidance
    assert "Do not replay" in guidance
    # Nothing was observed and recovery may already have revoked the target, so the
    # receipt must neither prescribe its own snapshot nor contradict Recovery.
    for contradiction in ("computer_snapshot", "returned fresh observation", "input outcome is uncertain",
                          "stop automatic input"):
        assert contradiction not in guidance.lower()


def test_observation_checkpoint_prefix_is_not_reported_as_uncertain_input():
    value = build_action_receipt({"action_result": ack()}, mode="foreground_takeover", action_count=2,
                                 dispatch_attempted=True, error_code="post_action_observation_pending")
    assert value["dispatch_state"] == "partial"
    assert value["next_step"] == "follow_recovery"
    guidance = receipt_instruction(value)
    assert "none after them were sent" in guidance
    assert "input outcome is uncertain" not in guidance.lower()


def refused(code, outcome_code=None):
    return {"action_result": {"last_acknowledged_action": -1,
                              "outcomes": [{"index": 0, "ok": False, "error_code": outcome_code or code}]}}


@pytest.mark.parametrize("code", [
    "input_focus_required", "stale_snapshot", "target_not_frontmost", "target_gone", "secure_target", "out_of_bounds",
])
def test_structured_pre_input_refusal_is_not_reported_as_uncertain_input(code):
    # The helper reports unknown_outcome whenever input may have started, so a specific
    # code with nothing acknowledged means nothing was sent.
    value = receipt(refused(code), error_code=code)
    assert value["dispatch_state"] == "not_dispatched"
    assert value["next_step"] == ("refresh_snapshot" if code == "stale_snapshot" else "inspect_error")
    assert "did not dispatch input" in receipt_instruction(value)


@pytest.mark.parametrize("code", ["stale_snapshot", "input_focus_required"])
def test_structured_empty_pre_input_refusal_is_not_reported_as_uncertain_input(code):
    value = receipt({"action_result": {"last_acknowledged_action": -1, "outcomes": []}}, error_code=code)
    assert value["dispatch_state"] == "not_dispatched"
    assert value["next_step"] == ("refresh_snapshot" if code == "stale_snapshot" else "inspect_error")


def test_structured_empty_background_unsupported_act_was_not_dispatched():
    value = receipt({"action_result": {"last_acknowledged_action": -1, "outcomes": []}},
                    error_code="background_action_unsupported")
    assert value["dispatch_state"] == "not_dispatched"
    assert value["next_step"] == "inspect_error"
    assert "did not dispatch input" in receipt_instruction(value)


@pytest.mark.parametrize("native,code", [
    (None, "background_action_unsupported"),
    ({"outcomes": []}, "background_action_unsupported"),
    ({"last_acknowledged_action": -1}, "background_action_unsupported"),
    ({"last_acknowledged_action": 0, "outcomes": []}, "background_action_unsupported"),
    ({"last_acknowledged_action": -1, "outcomes": [{"index": 0, "ok": False,
                                                     "error_code": "unknown_outcome"}]},
     "background_action_unsupported"),
    ({"last_acknowledged_action": -1, "outcomes": []}, "unknown_outcome"),
])
def test_background_unsupported_without_exact_empty_act_evidence_stays_unknown(native, code):
    payload = {"action_result": native} if native is not None else {}
    assert receipt(payload, error_code=code)["dispatch_state"] == "unknown"


@pytest.mark.parametrize("payload,code", [
    ({}, "stale_snapshot"),
    (refused("input_focus_required", "unknown_outcome"), "input_focus_required"),
    (refused("helper_failed"), "helper_failed"),
    (refused("unknown_outcome"), "unknown_outcome"),
    ({"action_result": {"last_acknowledged_action": 0, "outcomes": [{"index": 0, "ok": True}]}}, "stale_snapshot"),
])
def test_missing_or_uncertain_refusal_evidence_stays_conservative(payload, code):
    assert receipt(payload, error_code=code)["dispatch_state"] != "not_dispatched"


def test_observation_pending_code_is_not_delivery_proof():
    value = receipt({}, error_code="post_action_observation_pending")
    assert value["dispatch_state"] == "unknown"
    assert value["next_step"] == "handoff"


def test_checked_goals_need_final_matching_states():
    native = {"outcomes": [{"index": i, "ok": True, "effect_verification": "verified"} for i in range(10)], "last_acknowledged_action": 9}
    payload = {"action_result": native, "choice_verification": {"status": "verified", "results": [{"index": i, "status": "verified"} for i in range(10)]}}
    value = build_action_receipt(payload, mode="background", action_count=10, dispatch_attempted=True)
    assert value["next_step"] == "continue_from_fresh_observation"
    assert value["goal_verified_count"] == value["acknowledged"] == 10
    assert build_action_receipt(payload, mode="background", action_count=10, dispatch_attempted=True, error_code="postcondition_failed")["verification_state"] == "unverified"
    payload["choice_verification"]["results"][-1]["status"] = "not_satisfied"
    assert build_action_receipt(payload, mode="background", action_count=10, dispatch_attempted=True)["verification_state"] == "unverified"


@pytest.mark.parametrize("native", [{"last_acknowledged_action": True, "outcomes": []}, {"last_acknowledged_action": 0, "outcomes": [{"index": 7, "ok": True}]}, {"outcomes": [{"index": 0, "ok": True}]}])
def test_malformed_prefix_does_not_become_delivery_proof(native):
    assert receipt({"action_result": native})["acknowledged"] == 0


def tree(ref="old-ref", identity="same-button", label="Submit"):
    return {"role": "AXWindow", "children": [{"role": "AXButton", "element_ref": ref, "observation_identity": identity, "label": label}]}


def test_ref_churn_and_observation_do_not_authorize_duplicate_submit():
    guard = PendingClickGuard()
    action = SimpleNamespace(type="click", element_ref="old-ref", checked=None)
    guard.remember("window", tree(), [action], ack())
    new_action = {"type": "click", "element_ref": "new-ref"}
    assert guard.blocks("window", tree(ref="new-ref"), [new_action])
    guard.remember("window", tree(), [{"type": "wait"}], ack())
    assert guard.blocks("window", tree(ref="new-ref"), [new_action])
    assert not guard.blocks("window", tree(ref="new-ref", identity="confirmation-dialog-button"), [new_action])
    assert not guard.blocks("other-window", tree(ref="new-ref"), [new_action])
    assert not guard.blocks("window", tree(ref="new-ref"), [{**new_action, "checked": True}])
    # An actual edit starts a new action, making another save meaningful.
    guard.remember("window", tree(), [{"type": "type"}], ack())
    assert not guard.blocks("window", tree(ref="new-ref"), [new_action])


def test_noncommit_buttons_and_legacy_observation_do_not_get_blanket_blocked():
    guard = PendingClickGuard()
    action = {"type": "click", "element_ref": "old-ref"}
    for node in [tree(label="Next month"), tree(identity=None)]:
        guard.remember("window", node, [action], ack())
        assert not guard.blocks("window", node, [action])


def test_saved_context_keeps_fixed_receipt_without_private_payload():
    value = receipt({"action_result": ack()})
    value.update({"text": "secret-value", "snapshot_id": "secret-ref", "path": "/private/secret"})
    output = ReActAgent._tool_result_context({"name": "computer_act", "computer_receipt": value, "request_local_placeholder": True, "tool_output": "[omitted]"})
    assert '"dispatch_state":"acknowledged"' in output
    assert "computer_snapshot(settle_ms=300)" in output
    assert "secret" not in output
    assert "not click the same button" in output
    assert json.loads(output.split("Computer receipt: ")[1].splitlines()[0])["replay"] == "forbidden"
