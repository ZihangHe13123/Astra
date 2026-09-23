"""helper 错误码必须原样抵达调用方。

起因（2026-09-03 实机）：takeover 之后的每次 computer_snapshot 都报
`protocol_mismatch: The native helper returned an invalid action response.`，而 helper 日志里
对那两次调用**没有任何记录** —— 因为 `_decode_error` 把 Python 枚举里没有的码**静默改名**成
protocol_mismatch，连 helper 原文 message 一起丢进工具层的安全文案，故障因此不可诊断。
本文件锁两件事：①已知但漏登记的码要有真身份；②真正未知的码至少要把原名留在 message 里。
"""

from __future__ import annotations

import json

import pytest

from agent.runtime.computer_protocol import (
    ComputerError,
    ComputerErrorCode,
    decode_response,
    validate_snapshot_payload,
)


def _failed(code: str):
    response = decode_response(json.dumps({
        "protocol_version": 4,
        "request_id": "req-1",
        "ok": False,
        "error": {"code": code, "message": "helper said so"},
    }))
    assert response.error is not None
    return response.error


def test_requires_active_foreground_takeover_is_a_real_error_code() -> None:
    """Swift CooperativeErrorCode 会把它当**错误码**发回，而 Python 只在 plan.reason 里
    认这个字符串 ⇒ 解码即被洗。它是已存在的语义，必须有真枚举身份。"""

    error = _failed("requires_active_foreground_takeover")
    assert error.code is ComputerErrorCode.REQUIRES_ACTIVE_FOREGROUND_TAKEOVER
    assert error.message == "helper said so"


def test_unknown_helper_error_code_keeps_its_name_visible() -> None:
    """真·未知码可以归到 protocol_mismatch，但**不许把名字洗掉** —— 否则下一个漂移又是
    一整晚的猜测。"""

    error = _failed("brand_new_future_code")
    assert error.code is ComputerErrorCode.PROTOCOL_MISMATCH
    assert "brand_new_future_code" in error.message


def test_tool_layer_surfaces_the_unknown_code_name_to_the_caller() -> None:
    """协议层保住了名字还不够：工具层会用安全文案**覆盖** message，那一步同样能把名字吃掉。
    两层都得验，否则修了个寂寞。"""

    from agent.runtime.tools.computer import _application_failure

    failure = _application_failure(ComputerError(
        ComputerErrorCode.PROTOCOL_MISMATCH,
        "unknown helper error code 'brand_new_future_code': helper said so",
    ))
    assert "brand_new_future_code" in str(failure)


def _snapshot_payload(**extra):
    payload = {
        "image_artifact": "snapshot-0123456789abcdef0123456789abcdef.png",
        "logical_size": {"width": 1.0, "height": 1.0},
        "pixel_size": {"width": 2.0, "height": 2.0},
        "backing_scale": 2.0,
        "capture_bounds": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
        "ax_tree": {"role": "AXWindow"},
    }
    payload.update(extra)
    return payload


def test_snapshot_payload_accepts_the_virtual_cursor_pointer() -> None:
    """Python 侧同样漏登记：Swift 修好后若这里不认，故障只是换一层继续存在。"""

    validate_snapshot_payload(
        _snapshot_payload(virtual_pointer={"x": 640.0, "y": 390.0})
    )


def test_snapshot_payload_accepts_the_focused_fields_suggestion_popups() -> None:
    validate_snapshot_payload(_snapshot_payload(
        suggestion_popups=[{"x": 64.0, "y": 40.0, "width": 1038.0, "height": 199.0}],
    ))


@pytest.mark.parametrize(
    "bad",
    [[], [{"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}] * 5, [{"x": 1.0, "y": 2.0}],
     [{"x": 1.0, "y": 2.0, "width": 0.0, "height": 4.0}], {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0},
     [{"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0, "layer": 0}]],
)
def test_suggestion_popups_shape_is_validated(bad) -> None:
    with pytest.raises(ValueError):
        validate_snapshot_payload(_snapshot_payload(suggestion_popups=bad))


@pytest.mark.parametrize(
    "bad",
    [{"x": 1.0}, {"x": 1.0, "y": "2"}, {"x": 1.0, "y": 2.0, "z": 3.0}, True, {"x": 1.0, "y": float("nan")}],
)
def test_virtual_pointer_shape_is_still_validated(bad) -> None:
    with pytest.raises(ValueError):
        validate_snapshot_payload(_snapshot_payload(virtual_pointer=bad))


def test_takeover_upgrade_hint_only_applies_to_background_requests() -> None:
    """既有契约不许破：后台批次不可行时，应当建议升级为显式接管。"""

    from agent.runtime.macos_computer import HelperApplicationError
    from agent.runtime.tools.computer import _plan_rejection_failure

    exc = HelperApplicationError(
        ComputerError(ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED, "plan reason=foreground_keyboard")
    )
    failure = _plan_rejection_failure(exc, requested_takeover=False)
    assert failure is not None
    assert "foreground_takeover" in str(failure)


def test_takeover_request_is_never_told_to_switch_to_takeover() -> None:
    """今晚被这句话白烧好几轮：我**已经**在请求 foreground_takeover，却仍被告知
    "retry with interaction_mode='foreground_takeover'" —— 一条不可能执行的指引。
    真实前置条件（helper 原文）必须同时抵达调用方。"""

    from agent.runtime.macos_computer import HelperApplicationError
    from agent.runtime.tools.computer import _plan_rejection_failure

    exc = HelperApplicationError(
        ComputerError(ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED, "requires keyboardFocus")
    )
    failure = _plan_rejection_failure(exc, requested_takeover=True)
    assert failure is not None
    text = str(failure)
    assert "foreground_takeover_required" not in text
    assert "Retry this exact snapshot and action batch" not in text
    assert "requires keyboardFocus" in text


def test_refused_replacement_keeps_plain_batches_on_precondition_guidance() -> None:
    from agent.runtime.macos_computer import HelperApplicationError
    from agent.runtime.tools.computer import _plan_rejection_failure

    exc = HelperApplicationError(
        ComputerError(ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED, "the action batch is unsafe")
    )
    replaced = _plan_rejection_failure(exc, requested_takeover=True, replace_requested=True)
    assert replaced is not None and "command+a" in replaced.recovery_hint
    plain = _plan_rejection_failure(exc, requested_takeover=True)
    assert plain is not None and "hand this step to the user" in plain.recovery_hint
    # A background request is still told to escalate first.
    background = _plan_rejection_failure(exc, requested_takeover=False, replace_requested=True)
    assert background is not None and background.code == "foreground_takeover_required"


def test_refused_accessibility_action_says_nothing_was_sent_and_to_change_route() -> None:
    """Live Finder 2026-09-23: AXPress on a search suggestion was refused by the app. The model
    must learn that nothing happened and that repeating the same press cannot help."""
    from agent.runtime.computer_protocol import ComputerActResult
    from agent.runtime.tools.computer import _action_failure

    error = ComputerError(ComputerErrorCode.ACCESSIBILITY_ACTION_REFUSED, "refused")
    result = {"outcomes": [{"index": 0, "ok": False, "error_code": "accessibility_action_refused"}],
              "last_acknowledged_action": -1}
    parsed = ComputerActResult.from_mapping(result, action_count=1, response_ok=False, response_error=error)
    failure = _action_failure(error, parsed.to_mapping())
    assert failure.code == "accessibility_action_refused"
    assert failure.retryable is True
    assert "No input was dispatched" in failure.recovery_hint
    assert "another way" in failure.recovery_hint
    assert "Do not repeat this action batch" not in failure.recovery_hint


def test_native_observation_timeout_preserves_error_identity() -> None:
    error = _failed("observation_timeout")
    assert error.code.value == "observation_timeout"


def test_unavailable_window_pixels_survive_wire_and_stop_retry_loop() -> None:
    from agent.runtime.tools.computer import _application_failure

    error = _failed("window_content_unavailable")
    assert error.code.value == "window_content_unavailable"
    failure = _application_failure(error)
    assert failure.retryable is False
    rendered = str(failure).lower()
    assert "no readable pixels" in rendered
    assert "does not prove" in rendered
    assert "do not repeat" in rendered
    assert "capture a fresh snapshot before choosing another action" not in rendered


def test_unavailable_pixels_after_ack_keep_specific_recovery() -> None:
    from agent.runtime.tools.computer import _action_failure

    failure = _action_failure(_failed("window_content_unavailable"), {
        "last_acknowledged_action": 0,
        "outcomes": [{"ok": True}],
    })
    assert failure.retryable is False
    assert "capture/privacy state" in failure.recovery_hint
    assert "Do not repeat" in failure.recovery_hint
