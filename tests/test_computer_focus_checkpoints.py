"""Focus checkpoints acknowledge only the sent prefix and never authorize replay."""

import pytest

from agent.runtime.computer_protocol import (
    ComputerActResult, ComputerActionOutcome, ComputerError, ComputerErrorCode,
    KeyboardFailureDiagnostics,
)
from agent.runtime.tools.computer import _model_action_metadata


CHECKPOINT = {"index": 0, "ok": True, "observation_required": True}
RECEIPT = {"outcomes": [CHECKPOINT], "last_acknowledged_action": 0}
ERROR = ComputerError(ComputerErrorCode.OBSERVATION_REQUIRED, "observe")


def test_checkpoint_error_copy_does_not_invent_a_keyboard_focus_cause():
    from agent.runtime.tools.computer import _SAFE_ACTION_ERRORS

    message = _SAFE_ACTION_ERRORS[ComputerErrorCode.OBSERVATION_REQUIRED].lower()
    assert "keyboard focus" not in message
    assert "acknowledged" in message
    assert "remaining actions were not sent" in message
    assert "do not replay" in message


@pytest.mark.parametrize("partial", [False, True])
def test_checkpoint_receipt_preserves_ack_without_claiming_effect_or_full_batch(partial):
    result = ComputerActResult.from_mapping(
        RECEIPT, action_count=2 if partial else 1, response_ok=not partial,
        response_error=ERROR if partial else None,
    )
    assert result.to_mapping() == RECEIPT
    public = _model_action_metadata(result.to_mapping())
    assert public["outcomes"] == [CHECKPOINT]
    assert public["last_acknowledged_action"] == 0
    assert public["next_action_index"] == 1
    assert public["observation_required"] is True
    assert public["effect_verification"] == "unverified"
    assert "do not replay" in public["continuation_hint"]


@pytest.mark.parametrize("receipt,count,ok,error", [
    ({"outcomes": [], "last_acknowledged_action": -1}, 2, False, ERROR),
    ({"outcomes": [{"index": 0, "ok": True}], "last_acknowledged_action": 0}, 2, False, ERROR),
    (RECEIPT, 1, False, ERROR),
    (RECEIPT, 2, True, None),
    (RECEIPT, 2, False, ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown")),
    ({"outcomes": [CHECKPOINT, {"index": 1, "ok": True}], "last_acknowledged_action": 1}, 2, True, None),
    ({"outcomes": [CHECKPOINT], "last_acknowledged_action": -1}, 2, False, ERROR),
    ({"outcomes": [{"index": 0, "ok": False, "error_code": "observation_required"}],
      "last_acknowledged_action": -1}, 2, False, ERROR),
])
def test_checkpoint_rejects_missing_evidence_or_execution_past_boundary(receipt, count, ok, error):
    with pytest.raises(ValueError):
        ComputerActResult.from_mapping(receipt, action_count=count, response_ok=ok, response_error=error)


@pytest.mark.parametrize("marker", [False, None, 1, "true", {}])
def test_checkpoint_marker_is_literal_true_only(marker):
    with pytest.raises(ValueError):
        ComputerActionOutcome.from_mapping({**CHECKPOINT, "observation_required": marker}, expected_index=0)


def test_failed_action_cannot_be_a_successful_checkpoint():
    with pytest.raises(ValueError):
        ComputerActionOutcome.from_mapping({**CHECKPOINT, "ok": False, "error_code": "unknown_outcome"},
                                          expected_index=0)


def test_after_release_failure_requires_possible_input_and_keeps_original_cause():
    data = {"stage": "after_key_up", "cause": "secure_target",
            "input_may_have_started": True, "cleanup_failed": False}
    assert KeyboardFailureDiagnostics.from_mapping(data).to_mapping() == data
    with pytest.raises(ValueError):
        KeyboardFailureDiagnostics.from_mapping({**data, "input_may_have_started": False})


def test_resolved_checkpoint_continues_as_a_plain_acknowledged_prefix():
    """Live Edge 2026-09-23: after Cmd+L the helper proved the bound address field and typed on.
    The resolved checkpoint leaves the wire; only the final outcome may still carry one."""
    receipt = {"outcomes": [{"index": 0, "ok": True},
                            {"index": 1, "ok": True, "observation_required": True}],
               "last_acknowledged_action": 1}
    result = ComputerActResult.from_mapping(receipt, action_count=2, response_ok=True, response_error=None)
    assert result.to_mapping() == receipt
    public = _model_action_metadata(result.to_mapping())
    assert public["last_acknowledged_action"] == 1
    assert public["observation_required"] is True
