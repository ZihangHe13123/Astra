"""Provider-neutral model tools for guarded desktop Computer Use."""

from __future__ import annotations

from agent.runtime.paths import state_path

from ..computer_forms import form_elements, image_actions, observation_capabilities, truncated_form_region, verify_choice_goals
from ..computer_feedback import PendingClickGuard, build_action_receipt

import asyncio
import base64
import io
import json
import logging
import math
import os
import re
import secrets
import sys
import weakref
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import Any, cast

from ..computer_backend import (
    ComputerActionPlan,
    ComputerBackend,
    ComputerSessionError,
    ComputerSessionManager,
    ComputerSnapshotTargetBinding,
    ComputerTarget,
    ComputerVerifiedAppState,
)
from ..computer_observation import observe_action_effect, window_transition
from ..computer_policy import (
    ComputerApprovalRequest,
    ComputerPolicyDecision,
    app_state_approval_scope,
    app_state_permission_request,
    classify_computer_batch,
    permission_grant,
    permission_request,
    snapshot_approval_scope,
    snapshot_permission_request,
)
from ..computer_protocol import (
    ALLOWED_MODIFIERS,
    MAX_ACTIONS,
    MAX_COORDINATE_MAGNITUDE,
    MAX_DURATION_MS,
    MAX_ELEMENT_REF_SCALARS,
    MAX_KEY_SCALARS,
    MAX_NATIVE_AX_DEPTH,
    MAX_PUBLIC_AX_DEPTH,
    MAX_PUBLIC_AX_MAPPING_FIELDS,
    MAX_PUBLIC_AX_NODES,
    MAX_PUBLIC_AX_STRING_CHARACTERS,
    MAX_TEXT_CHARACTERS,
    ComputerAction,
    ComputerError,
    ComputerErrorCode,
    ComputerInteractionMode,
    KeyboardFailureDiagnostics,
    ComputerSnapshot,
    ComputerSnapshotTextDetailMode,
    decode_text_detail_envelope,
    validate_snapshot_payload,
)
from ..computer_routing import computer_route_advice
from ..computer_text import contains_credentials
from ..macos_computer import HelperApplicationError, HelperTransportError
from ..macos_computer_compatibility import (
    CompatibilityRegistryError,
    enabled_pid_actions,
)
from ..tool_failure import ToolFailure
from .approval import ScopedApprovalStore
from .registry import ToolDef, ToolPrivateResult, ToolRegistry

logger = logging.getLogger(__name__)

# Reserved durable-history marker; ``chars`` counts Python Unicode code points,
# not UTF-8 bytes, UTF-16 units, or rendered graphemes.
_REDACTED_ACT_TEXT = re.compile(r"\[redacted [0-9]+ chars\]")

# Generic JSON and per-element metadata retain the existing depth limit.
_MAX_AX_DEPTH = MAX_PUBLIC_AX_DEPTH
# Native ordinary serialization counts the root as depth 0 and stops before
# MAX_NATIVE_AX_DEPTH. Do not discard its valid lower levels in the public tree.
_MAX_AX_ELEMENT_DEPTH = MAX_NATIVE_AX_DEPTH - 1
_MAX_AX_NODES = MAX_PUBLIC_AX_NODES
_MAX_AX_MAPPING_FIELDS = MAX_PUBLIC_AX_MAPPING_FIELDS
_MAX_AX_STRING = MAX_PUBLIC_AX_STRING_CHARACTERS
_MAX_AX_JSON_BYTES = 512 * 1024
# AX 深度按父子元素之间的边计数，children 列表和属性不额外占层。锚点展开从新根
# 使用独立的 24 层额度；元素数、总值数和 512 KiB 上限仍生效。
_SUBTREE_AX_DEPTH = 2 * _MAX_AX_DEPTH
# 剪枝树保留祖先链，因此保留独立的深度额度；元素数、总值数和 512 KiB 上限照旧生效。
_FILTERED_AX_DEPTH = 64
# 上一次**整窗口**投影所见的原始 AX 树，按会话保存，仅用于把已公开的 element_ref 按稳定
# 身份映射到新快照（element_ref 每次快照重新生成）。这是呈现层缓存，不参与任何输入授权
# 判定：manager 何时清理自己的快照都不影响它，反之它也绝不放宽授权。
_LAST_PUBLISHED_AX_TREES: dict[str, Any] = {}
_MAX_TEXT_SUMMARY_ENTRIES = 200
_MAX_TEXT_SUMMARY_BYTES = 32 * 1024
_TEXT_SUMMARY_TRUNCATION_MARKER = {
    "role": "AXSummaryTruncated",
    "text": "[summary truncated]",
}
_WPS_DESTINATION_MODAL_TITLES = frozenset({
    "另存为",
    "save as",
    "另存为新格式",
    "save as new format",
    "输出为pdf",
    "export as pdf",
    "修改输出名称",
    "change output name",
    "选择路径",
    "choose path",
    "select path",
    "前往文件夹",
    "go to folder",
})
_SAFE_ACTION_ERRORS = {
    ComputerErrorCode.UNSUPPORTED_PLATFORM: "Computer Use is unavailable on this platform.",
    ComputerErrorCode.PERMISSION_DENIED: "The native helper lacks a required permission.",
    ComputerErrorCode.PROTOCOL_MISMATCH: "The native helper returned an invalid action response.",
    ComputerErrorCode.TARGET_GONE: "The selected application or window is no longer available.",
    ComputerErrorCode.OVERLAY_BLOCKED: "An overlay or uncertain occluding window blocks exact target binding. This is not evidence of expired references.",
    ComputerErrorCode.AX_WINDOW_UNMATCHED: "The window is present, but its Accessibility identity could not be matched uniquely. This is not evidence of expired references or a system prohibition on interaction.",
    ComputerErrorCode.TARGET_NOT_FRONTMOST: "The selected window is no longer frontmost.",
    ComputerErrorCode.STALE_SNAPSHOT: "The action snapshot is stale.",
    ComputerErrorCode.OUT_OF_BOUNDS: "The action target is outside the current window.",
    ComputerErrorCode.SECURE_TARGET: "The action targets a protected control and requires user handoff.",
    ComputerErrorCode.INPUT_FOCUS_REQUIRED: "The intended text input element is not focused.",
    ComputerErrorCode.ACTION_TIMEOUT: "The native action timed out.",
    ComputerErrorCode.OBSERVATION_TIMEOUT: "The native observation exceeded its time budget.",
    ComputerErrorCode.WINDOW_CONTENT_UNAVAILABLE: (
        "The exact target-window capture returned no readable pixels (fully transparent). "
        "This does not prove that the window is occluded, unfocused, or that a prior action failed. "
        "The application may be withholding its window content from capture."
    ),
    ComputerErrorCode.HELPER_FAILED: "The native helper could not complete the action.",
    ComputerErrorCode.UNKNOWN_OUTCOME: (
        "The action outcome is unknown; input may already have been sent. "
        "This is not evidence that the requested key or action is prohibited."
    ),
    ComputerErrorCode.OBSERVATION_REQUIRED: (
        "Input delivery was acknowledged. Remaining actions were not sent; "
        "inspect fresh state before choosing the next batch. Do not replay the acknowledged prefix."
    ),
    ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED: "This action requires foreground takeover.",
    ComputerErrorCode.REQUIRES_ACTIVE_FOREGROUND_TAKEOVER: (
        "The application must be activated (foreground) before this input can be delivered."
    ),
    ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED: "This action cannot be performed safely in the background.",
    ComputerErrorCode.USER_ACTIVITY_PAUSED: "Computer input paused because user activity was detected.",
    ComputerErrorCode.SIDECAR_FAILED: "The native cooperative input service failed.",
}


def _object_schema(properties: dict[str, Any], *, required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


_NUMBER = {
    "type": "number",
    "minimum": -MAX_COORDINATE_MAGNITUDE,
    "maximum": MAX_COORDINATE_MAGNITUDE,
}
_SCROLL_DELTA_X = {**_NUMBER, "description": "Content offset: positive scrolls right, negative left; pixels for pointer scrolling."}
_SCROLL_DELTA_Y = {**_NUMBER, "description": "Content offset: positive scrolls down, negative up; pixels for pointer scrolling."}
_ELEMENT_REF = {"type": "string", "minLength": 1, "maxLength": MAX_ELEMENT_REF_SCALARS}
_MODIFIERS = {
    "type": "array",
    "items": {"type": "string", "enum": sorted(ALLOWED_MODIFIERS)},
    "maxItems": 8,
    "uniqueItems": True,
}


def _action_schema(action_type: str, properties: dict[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return _object_schema(
        {"type": {"type": "string", "const": action_type}, **properties},
        required=("type", *required),
    )


def _with_any_required(base: dict[str, Any], variants: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
    return {
        "allOf": [
            base,
            {"anyOf": [{"type": "object", "required": list(required)} for required in variants]},
        ]
    }


ACTION_SCHEMA = {
    "oneOf": [
        _action_schema("click", {"element_ref": _ELEMENT_REF, "checked": {"type": "boolean", "description": "Optional checkbox/radio state goal: skip if satisfied, otherwise click once and verify"}}, ("element_ref",)),
        _action_schema(
            "click",
            {"element_index": {"type": "integer", "minimum": 1}, "checked": {"type": "boolean"}},
            ("element_index",),
        ),
        _action_schema(
            "click",
            {"x": _NUMBER, "y": _NUMBER, "target_element_ref": _ELEMENT_REF},
            ("x", "y"),
        ),
        _action_schema("right_click", {"element_ref": _ELEMENT_REF}, ("element_ref",)),
        _action_schema(
            "right_click",
            {"element_index": {"type": "integer", "minimum": 1}},
            ("element_index",),
        ),
        _action_schema(
            "right_click",
            {"x": _NUMBER, "y": _NUMBER, "target_element_ref": _ELEMENT_REF},
            ("x", "y"),
        ),
        _action_schema("double_click", {"element_ref": _ELEMENT_REF}, ("element_ref",)),
        _action_schema(
            "double_click",
            {"element_index": {"type": "integer", "minimum": 1}},
            ("element_index",),
        ),
        _action_schema(
            "double_click",
            {"x": _NUMBER, "y": _NUMBER, "target_element_ref": _ELEMENT_REF},
            ("x", "y"),
        ),
        _action_schema(
            "type",
            {"text": {"type": "string", "maxLength": MAX_TEXT_CHARACTERS}, "element_ref": _ELEMENT_REF},
            ("text",),
        ),
        _action_schema(
            "type",
            {
                "text": {"type": "string", "maxLength": MAX_TEXT_CHARACTERS},
                "element_ref": _ELEMENT_REF,
                "replace": {"type": "boolean", "const": True,
                            "description": "Replace the complete field value; empty text clears. Requires replace_text_v1 and exact readback."},
            },
            ("text", "element_ref", "replace"),
        ),
        _action_schema(
            "keypress",
            {
                "key": {"type": "string", "minLength": 1, "maxLength": MAX_KEY_SCALARS},
                "modifiers": _MODIFIERS,
                "element_ref": _ELEMENT_REF,
            },
            ("key",),
        ),
        _with_any_required(
            _action_schema(
                "scroll",
                {"delta_x": _SCROLL_DELTA_X, "delta_y": _SCROLL_DELTA_Y, "element_ref": _ELEMENT_REF},
                ("element_ref",),
            ),
            (("delta_x",), ("delta_y",)),
        ),
        _with_any_required(
            _action_schema(
                "scroll",
                {"delta_x": _SCROLL_DELTA_X, "delta_y": _SCROLL_DELTA_Y, "element_index": {"type": "integer", "minimum": 1}},
                ("element_index",),
            ),
            (("delta_x",), ("delta_y",)),
        ),
        _with_any_required(
            _action_schema(
                "scroll",
                {
                    "delta_x": _SCROLL_DELTA_X,
                    "delta_y": _SCROLL_DELTA_Y,
                    "x": _NUMBER,
                    "y": _NUMBER,
                    "target_element_ref": _ELEMENT_REF,
                },
                ("x", "y"),
            ),
            (("delta_x",), ("delta_y",)),
        ),
        _action_schema(
            "drag",
            {
                "x": _NUMBER,
                "y": _NUMBER,
                "end_x": _NUMBER,
                "end_y": _NUMBER,
                "duration_ms": {"type": "integer", "minimum": 0, "maximum": MAX_DURATION_MS},
                "target_element_ref": _ELEMENT_REF,
            },
            ("x", "y", "end_x", "end_y"),
        ),
        _action_schema(
            "wait",
            {"duration_ms": {"type": "integer", "minimum": 0, "maximum": MAX_DURATION_MS}},
            ("duration_ms",),
        ),
    ]
}

STATUS_SCHEMA = _object_schema({})
APPS_SCHEMA = _object_schema({})
FOCUS_SCHEMA = _object_schema(
    {"app_ref": _ELEMENT_REF, "window_ref": _ELEMENT_REF},
    required=("app_ref", "window_ref"),
)
GET_APP_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "app_ref": {"type": "string", "minLength": 1},
        "window_ref": {"type": "string", "minLength": 1},
        "scope": {
            "type": "string",
            "enum": ["target_window", "display"],
            "default": "target_window",
        },
        "text_detail": {
            "type": "string",
            "enum": ["off", "on"],
            "default": "off",
        },
    },
    "required": ["app_ref", "window_ref"],
    "additionalProperties": False,
}
SNAPSHOT_SCHEMA = _object_schema({
    "settle_ms": {
        "type": "integer", "minimum": 0, "maximum": 2000, "default": 0,
        "description": "Optional bounded delay before this read (e.g. 300 after an acknowledged submit). No input is replayed.",
    },
    "scope": {
        "type": "string",
        "enum": ["target_window", "display"],
        "default": "target_window",
    },
    "text_detail": {
        "type": "string",
        "enum": ["off", "on"],
        "default": "off",
    },
    "subtree_ref": {
        "type": "string",
        "description": (
            "Re-render the accessibility tree rooted at this element_ref from the latest "
            "snapshot, with its own depth/node budget, to reach elements that the "
            "whole-window projection truncates."
        ),
    },
    "role_filter": {
        "type": "string",
        "description": (
            "Keep only elements with this AX role plus their ancestors (e.g. AXTextArea). "
            "When subtree_ref is set, filter only within that subtree. "
            "Use it when a huge list of identical siblings — a sidebar of chat rows — "
            "consumes the whole subtree budget."
        ),
    },
})
ACT_SCHEMA = _object_schema(
    {
        "snapshot_id": _ELEMENT_REF,
        "coordinate_space": {"type": "string", "enum": ["window_logical", "image_pixels"], "default": "window_logical",
            "description": "For screenshot pointing use image_pixels; coordinates are converted from this snapshot's published image only. Legacy coordinates remain window_logical."},
        "actions": {
            "type": "array",
            "items": ACTION_SCHEMA,
            "minItems": 1,
            "maxItems": MAX_ACTIONS,
        },
        "interaction_mode": {
            "type": "string",
            "enum": ["auto", "background", "foreground_takeover"],
            "default": "auto",
            "description": "auto plans the supported input path and handles takeover within the existing approval scope; explicit legacy modes remain available",
        },
        "opens_dialog": {
            "type": "boolean",
            "default": False,
            "description": "Set true on the FIRST action that opens a native file picker, save panel or modal dialog. With auto, activate the target before input even if AX exposes an ordinary button. Never replay an already-dispatched click to add this flag.",
        },
    },
    required=("snapshot_id", "actions"),
)
# Both refs resume a suspended target; neither ends a targetless handoff.
RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "app_ref": {"type": "string", "minLength": 1},
        "window_ref": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}
TAKEOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "snapshot_id": {"type": "string"},
        "plan_ref": {"type": "string"},
    },
    "required": ["snapshot_id", "plan_ref"],
    "additionalProperties": False,
}
END_TAKEOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "takeover_ref": {"type": "string"},
    },
    "required": ["takeover_ref"],
    "additionalProperties": False,
}
EMPTY_SCHEMA = _object_schema({})


@dataclass
class _PreparedComputerAct:
    mode: ComputerInteractionMode
    snapshot_id: str
    actions: tuple[ComputerAction, ...]
    raw_actions: tuple[dict[str, Any], ...]
    decision: ComputerPolicyDecision | None = None
    # 授权判定（decision，原形态）与 hash 基准（hash_basis，BACKGROUND 形态）必须
    # 分开：permission_request 按 takeover 形态才发授权，而三处 batch_hash 复核
    # 必须同 BG 形态（2026-09-04 WPS 八连 stale 与 test_default_local_runtime
    # 回归两头踩证的职责分裂）。
    hash_basis: ComputerPolicyDecision | None = None
    plan: ComputerActionPlan | None = None
    failure: ToolFailure | None = None
    approved: bool = False
    restore_previous_focus: bool = True
    # decision.batch_hash 的计算输入。plan_actions 成功后 backend 会更新
    # trusted_snapshot，二次复核若重取 context 会因信任分支形状漂移而必失配
    # （2026-09-04 WPS 六连 stale 的根因）；复核必须复用同一 context。
    policy_context: dict[str, Any] | None = None
    dispatch_attempted: bool = False
    dispatch_result: dict[str, Any] | None = None


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _failure(code: str, message: str, *, retryable: bool = False, recovery_hint: str = "", details=None) -> ToolFailure:
    return ToolFailure(
        code=code,
        message=message,
        retryable=retryable,
        recovery_hint=recovery_hint,
        details=dict(details or {}),
    )


def _session_failure(exc: ComputerSessionError) -> ToolFailure:
    retryable = exc.code in {"stale_snapshot", "target_required", "handoff_active"}
    recovery_hint = ""
    if exc.code in {"catalog_required", "stale_target", "target_gone"}:
        recovery_hint = (
            "Call computer_apps, then choose the exact app_ref and window_ref from "
            "its fresh catalog before calling computer_get_app_state."
        )
    return _failure(exc.code, str(exc), retryable=retryable, recovery_hint=recovery_hint)


def _app_state_identity_failure(error: ComputerError, *, target_bound: bool = True) -> ToolFailure:
    if error.code is ComputerErrorCode.OBSERVATION_TIMEOUT and not target_bound:
        return replace(
            _application_failure(error),
            recovery_hint=(
                "Call computer_apps, then choose the exact app_ref and window_ref from "
                "its fresh catalog before calling computer_get_app_state."
            ),
        )
    if error.code not in {
        ComputerErrorCode.STALE_TARGET,
        ComputerErrorCode.TARGET_GONE,
    }:
        return _application_failure(error)
    return _failure(
        error.code.value,
        "The selected application or window is no longer the exact catalog target.",
        retryable=True,
        recovery_hint=(
            "Call computer_apps, then choose the exact app_ref and window_ref from "
            "its fresh catalog before calling computer_get_app_state."
        ),
    )


def _plan_rejection_failure(
    exc: BaseException,
    *,
    requested_takeover: bool,
) -> ToolFailure | None:
    """把 helper 的"后台直投不可行"翻译成**当下真能执行**的指引。

    升级为显式接管的建议只对后台请求成立。调用方本来就在请求 foreground takeover 时，再让它
    "改用 interaction_mode='foreground_takeover' 重试"是一条无法执行的指引（实机 2026-09-03
    被它白烧数轮，每次都朝同一个方向重试），必须改为如实回传 helper 原文里的未满足前置条件。
    see docs/macos-computer-use.md#input-delivery-contracts
    """

    if not isinstance(exc, HelperApplicationError):
        return None
    if exc.error.code is not ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED:
        return None
    if requested_takeover:
        return _failure(
            ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED.value,
            _SAFE_ACTION_ERRORS[ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED],
            retryable=False,
            recovery_hint=(
                "Foreground takeover was already requested, so switching modes cannot fix this. "
                "The unmet precondition is in details.helper_message; satisfy it or hand this step to the user."
            ),
            details={
                "requested_interaction_mode": ComputerInteractionMode.FOREGROUND_TAKEOVER.value,
                "helper_message": exc.error.message,
            },
        )
    return _failure(
        ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED.value,
        _SAFE_ACTION_ERRORS[ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED],
        retryable=True,
        recovery_hint=(
            "Retry this exact snapshot and action batch with "
            "interaction_mode='foreground_takeover'."
        ),
        details={
            "interaction_mode": ComputerInteractionMode.FOREGROUND_TAKEOVER.value,
        },
    )


def _application_failure(error) -> ToolFailure:
    code = error.code
    message = _SAFE_ACTION_ERRORS.get(code, "The native Computer Use request was rejected.")
    if code is ComputerErrorCode.PROTOCOL_MISMATCH and error.message:
        # 安全文案不能吃掉 helper 的原文：未知码的名字若在这一步被替换掉，协议层的
        # 保留就白做了 —— 调用方依旧只看到一句 protocol_mismatch。
        message = f"{message} ({error.message})"
    if code is ComputerErrorCode.OVERLAY_BLOCKED:
        return _failure(
            code.value, message, retryable=False,
            recovery_hint=(
                "Stop repeating catalog/bind calls while the overlay is unchanged. "
                "Observe the visible popup as its own catalog target and use an observed dismissal "
                "control if authorized by the task; otherwise ask the user to dismiss it. Do not guess keys, use "
                "resume without a suspended target, or treat this as a reference TTL failure. "
                "After the overlay changes, refresh computer_apps and observe the intended window."
            ),
            details={"retry_requires": "overlay_state_changed"},
        )
    if code is ComputerErrorCode.AX_WINDOW_UNMATCHED:
        return _failure(
            code.value, message, retryable=False,
            recovery_hint=(
                "Stop repeating catalog/bind calls while the window is unchanged. "
                "Do not target its obscured parent or bypass identity checks with guessed input. "
                "After the panel changes, refresh computer_apps and choose only bindable=true windows; "
                "if it remains unmatched, report a compatibility failure and hand this step to the user."
            ),
            details={"retry_requires": "window_identity_or_state_changed"},
        )
    if code is ComputerErrorCode.WINDOW_CONTENT_UNAVAILABLE:
        return _failure(
            code.value,
            message,
            retryable=False,
            recovery_hint=(
                "Stop visual actions on this target. Do not repeat a prior click or send, "
                "switch to desktop-region capture, or repeatedly activate/move the window. "
                "Ask the user to check the application's capture/privacy state. After that state "
                "changes, refresh computer_apps and bind the current window with computer_get_app_state."
            ),
        )
    return _failure(
        code.value,
        message,
        retryable=code in {
            ComputerErrorCode.TARGET_GONE,
            ComputerErrorCode.OBSERVATION_TIMEOUT,
            ComputerErrorCode.TARGET_NOT_FRONTMOST,
            ComputerErrorCode.STALE_SNAPSHOT,
            ComputerErrorCode.INPUT_FOCUS_REQUIRED,
            ComputerErrorCode.REQUIRES_ACTIVE_FOREGROUND_TAKEOVER,
        },
        recovery_hint=(
            "Focus the intended input element, capture a fresh snapshot, and retry the explicit text action."
            if code is ComputerErrorCode.INPUT_FOCUS_REQUIRED
            else (
                "Retry this exact snapshot and action batch with interaction_mode="
                "'foreground_takeover'; the helper needs the app activated first."
                if code is ComputerErrorCode.REQUIRES_ACTIVE_FOREGROUND_TAKEOVER
                else (
                "Capture a fresh snapshot before choosing another action."
                if code is not ComputerErrorCode.UNKNOWN_OUTCOME
                else "Do not repeat this action batch. Capture a fresh snapshot and inspect the current state."
            )
            )
        ),
    )


def _action_failure(error, result: Mapping[str, object] | None) -> ToolFailure:
    details = _model_action_metadata(result)
    acknowledgement = details.get("last_acknowledged_action")
    outcomes = details.get("outcomes")
    do_not_repeat = (
        error.code is ComputerErrorCode.UNKNOWN_OUTCOME
        or (isinstance(acknowledgement, int) and acknowledgement >= 0)
        or (
            isinstance(outcomes, list)
            and any(
                outcome.get("ok") is True
                or outcome.get("error_code") == ComputerErrorCode.UNKNOWN_OUTCOME.value
                for outcome in outcomes
            )
        )
    )
    base = _application_failure(error)
    return _failure(
        base.code,
        base.message,
        retryable=base.retryable and not do_not_repeat,
        recovery_hint=(
            "Do not repeat this action batch. Capture a fresh snapshot and inspect the current state."
            if do_not_repeat and error.code is not ComputerErrorCode.WINDOW_CONTENT_UNAVAILABLE
            else base.recovery_hint
        ),
        details=details,
    )


def _capabilities_value(
    configured: Collection[str] | Callable[[], Collection[str]],
) -> frozenset[str]:
    value = configured() if callable(configured) else configured
    return frozenset(str(item) for item in value)


def _new_macos_computer_manager(
    *,
    helper_path: Path | str | None,
    cache_root: Path | str,
) -> ComputerSessionManager:
    """Construct the native manager only after an explicit Computer Use call."""
    from ..macos_computer import HelperTransport, MacComputerBackend

    transport = HelperTransport(helper_path)
    backend = cast(ComputerBackend, MacComputerBackend(transport))
    return ComputerSessionManager(backend, cache_root=cache_root)


class LocalComputerRuntime:
    """Lazy, session-rotating owner shared by tools and diagnostics."""

    def __init__(
        self,
        *,
        helper_path: Path | str | None,
        cache_root: Path | str,
    ) -> None:
        self._helper_path = helper_path
        self._cache_root = cache_root
        self._manager: ComputerSessionManager | None = None
        self._retired_manager: ComputerSessionManager | None = None
        self._teardown_task: asyncio.Task[None] | None = None
        self._teardown_error = ""
        self._teardown_failure_unreported = False
        self._rotation_lock = asyncio.Lock()
        self._generation = 0
        self._permanently_shutting_down = False
        self._grants: set[str] = set()
        self._helper_started = False
        self._capability = self._configured_capability()
        self._cached_status: dict[str, object] | None = None

    @staticmethod
    def _configured_capability() -> tuple[str, str]:
        return "configured", "Native helper is configured but has not been probed."

    @property
    def manager_started(self) -> bool:
        return self._manager is not None

    @property
    def helper_started(self) -> bool:
        return self._helper_started

    @property
    def session_id(self) -> str:
        manager = self._manager
        return manager.session_id if manager is not None else "unstarted"

    @property
    def session_dir(self) -> Path:
        manager = self._manager
        if manager is None:
            raise ComputerSessionError("session_closed", "session_closed: Computer Use session ended")
        return manager.session_dir

    @property
    def target(self):
        manager = self._manager
        return manager.target if manager is not None else None

    @property
    def snapshot_target_binding(self) -> ComputerSnapshotTargetBinding | None:
        manager = self._manager
        return manager.snapshot_target_binding if manager is not None else None

    def validate_snapshot_publication(
        self,
        snapshot: ComputerSnapshot,
        binding: ComputerSnapshotTargetBinding | None,
        resume_publication_id: str | None = None,
    ) -> None:
        if self._manager is None:
            raise ComputerSessionError("session_closed")
        self._manager.validate_snapshot_publication(snapshot, binding, resume_publication_id)

    @property
    def handed_off(self) -> bool:
        manager = self._manager
        return bool(manager is not None and manager.handed_off)

    @property
    def suspended_target(self) -> ComputerTarget | None:
        manager = self._manager
        return manager.suspended_target if manager is not None else None

    @property
    def catalog_generation(self) -> int:
        manager = self._manager
        return manager.catalog_generation if manager is not None else 0

    @property
    def closed(self) -> bool:
        manager = self._manager
        return bool(manager is not None and manager.closed)

    @property
    def grants(self) -> set[str]:
        return self._grants

    def capability_status(self) -> tuple[str, str]:
        return self._capability

    def cached_status(self) -> dict[str, object] | None:
        """Return only a previous safe status result without activating the helper."""
        return dict(self._cached_status) if self._cached_status is not None else None

    async def _ensure_manager(self) -> ComputerSessionManager:
        requested_generation = self._generation
        self._check_activation_generation(requested_generation)
        async with self._rotation_lock:
            self._check_activation_generation(requested_generation)
            await self._await_prior_teardown_locked()
            self._check_activation_generation(requested_generation)
            manager = self._manager
            if manager is None:
                manager = _new_macos_computer_manager(
                    helper_path=self._helper_path,
                    cache_root=self._cache_root,
                )
                manager.grants = self._grants
                self._manager = manager
            return manager

    def _check_activation_generation(self, requested_generation: int) -> None:
        if self._permanently_shutting_down:
            raise ComputerSessionError(
                "computer_runtime_shutdown",
                "computer_runtime_shutdown: local Computer Use is shutting down",
            )
        if requested_generation != self._generation:
            raise ComputerSessionError(
                "computer_session_changed",
                "computer_session_changed: retry in the current Astra session",
            )

    def _schedule_teardown(self) -> asyncio.Task[None] | None:
        manager = self._retired_manager
        if manager is None:
            return None
        task = self._teardown_task
        if task is not None and not task.done():
            return task
        task = asyncio.get_running_loop().create_task(self._teardown_manager(manager))
        self._teardown_task = task
        return task

    async def _teardown_manager(self, manager: ComputerSessionManager) -> None:
        errors: list[BaseException] = []
        try:
            await manager.close_backend()
        except Exception as exc:  # noqa: BLE001 - local cleanup is unsafe while helper liveness is uncertain
            errors.append(exc)
        if not errors:
            try:
                manager.close_local_state()
            except Exception as exc:  # noqa: BLE001 - preserve a bounded retryable teardown state
                errors.append(exc)
        if errors:
            self._teardown_error = ",".join(type(exc).__name__ for exc in errors)
            self._teardown_failure_unreported = True
            self._capability = (
                "degraded",
                "Previous Computer Use helper teardown failed; activation is blocked until cleanup succeeds.",
            )
            logger.warning(
                "Computer Use teardown failed error_types=%s",
                ",".join(type(exc).__name__ for exc in errors),
            )
            return
        if self._retired_manager is manager:
            self._retired_manager = None
        self._teardown_error = ""
        self._teardown_failure_unreported = False
        self._capability = self._configured_capability()

    async def _await_prior_teardown_locked(self) -> None:
        task = self._teardown_task
        if task is not None and not task.done():
            await asyncio.shield(task)
        if self._retired_manager is None:
            self._teardown_task = None
            return
        if self._teardown_failure_unreported:
            self._teardown_failure_unreported = False
            raise ComputerSessionError(
                "computer_teardown_failed",
                "computer_teardown_failed: previous helper cleanup failed; retry the operation",
            )
        task = self._schedule_teardown()
        assert task is not None
        await asyncio.shield(task)
        if self._retired_manager is not None:
            self._teardown_failure_unreported = False
            raise ComputerSessionError(
                "computer_teardown_failed",
                "computer_teardown_failed: previous helper cleanup failed; retry the operation",
            )
        self._teardown_task = None

    @staticmethod
    def _status_capability(status: Mapping[str, object]) -> tuple[str, str]:
        permissions = status.get("permissions")
        if status.get("supported") is not True or not isinstance(permissions, Mapping):
            return "degraded", "Native helper protocol status is invalid."
        accessibility = permissions.get("accessibility")
        screen_recording = permissions.get("screen_recording")
        if not isinstance(accessibility, bool) or not isinstance(screen_recording, bool):
            return "degraded", "Native helper protocol permission status is invalid."
        missing = []
        if not accessibility:
            missing.append("Accessibility")
        if not screen_recording:
            missing.append("Screen Recording")
        if missing:
            return "degraded", "Missing macOS permission: " + " and ".join(missing) + "."
        return "available", "Native helper protocol and required macOS permissions are available."

    @staticmethod
    def _error_capability(exc: BaseException) -> tuple[str, str]:
        message = str(exc).lower()
        if any(marker in message for marker in ("unavailable", "executable", "ownership", "symlink")):
            return "degraded", "Native Computer Use helper is unavailable or unsafe."
        if any(marker in message for marker in ("protocol", "malformed", "request_id", "response")):
            return "degraded", "Native Computer Use helper protocol check failed."
        return "degraded", "Native Computer Use helper probe failed."

    async def status(self) -> dict[str, object]:
        try:
            manager = await self._ensure_manager()
            self._helper_started = True
            status = await manager.status()
        except ComputerSessionError as exc:
            if exc.code not in {
                "computer_teardown_failed",
                "computer_runtime_shutdown",
                "computer_session_changed",
            }:
                self._capability = self._error_capability(exc)
            raise
        except Exception as exc:
            self._capability = self._error_capability(exc)
            raise
        self._capability = self._status_capability(status)
        self._cached_status = dict(status)
        return status

    async def probe(self) -> tuple[str, str]:
        try:
            await self.status()
        except Exception:  # noqa: BLE001 - probe records a bounded state for every helper failure
            return self._capability
        return self._capability

    async def apps(self):
        self._helper_started = True
        return await (await self._ensure_manager()).apps()

    async def select(self, app_ref: str, window_ref: str):
        self._helper_started = True
        return await (await self._ensure_manager()).select(app_ref, window_ref)

    async def focus(self, app_ref: str, window_ref: str):
        """Compatibility alias that preserves the v2 background-select wire shape."""
        return await self.select(app_ref, window_ref)

    async def get_app_state(
        self,
        app_ref: str,
        window_ref: str,
        scope: str = "target_window",
        *,
        text_detail: ComputerSnapshotTextDetailMode = ComputerSnapshotTextDetailMode.OFF,
        recovery_catalog_generation: int | None = None,
        recovery_window_identity_ref: str | None = None,
    ) -> ComputerVerifiedAppState:
        self._helper_started = True
        return await (await self._ensure_manager()).get_app_state(
            app_ref,
            window_ref,
            scope,
            text_detail=text_detail,
            recovery_catalog_generation=recovery_catalog_generation,
            recovery_window_identity_ref=recovery_window_identity_ref,
        )

    async def invalidate_app_state_attempt_authority(self) -> None:
        manager = self._manager
        if manager is not None:
            await manager.invalidate_app_state_attempt_authority()

    async def snapshot(
        self,
        scope: str = "target_window",
        *,
        text_detail: ComputerSnapshotTextDetailMode = ComputerSnapshotTextDetailMode.OFF,
        expected_target_binding: ComputerSnapshotTargetBinding | None = None,
        resume_publication_id: str | None = None,
        subtree_ref: str = "",
    ):
        self._helper_started = True
        return await (await self._ensure_manager()).snapshot(
            scope,
            text_detail=text_detail,
            expected_target_binding=expected_target_binding,
            resume_publication_id=resume_publication_id,
            **({"subtree_ref": subtree_ref} if subtree_ref else {}),
        )

    async def plan_actions(
        self,
        snapshot_id: str,
        actions,
        *,
        interaction_mode: ComputerInteractionMode,
    ):
        self._helper_started = True
        return await (await self._ensure_manager()).plan_actions(
            snapshot_id,
            actions,
            interaction_mode=interaction_mode,
        )

    async def begin_takeover(self, snapshot_id: str, plan_ref: str):
        self._helper_started = True
        return await (await self._ensure_manager()).begin_takeover(snapshot_id, plan_ref)

    async def act(
        self,
        snapshot_id: str,
        actions,
        *,
        interaction_mode: ComputerInteractionMode,
        plan_ref: str,
        takeover_ref: str | None = None,
    ):
        self._helper_started = True
        return await (await self._ensure_manager()).act(
            snapshot_id,
            actions,
            interaction_mode=interaction_mode,
            plan_ref=plan_ref,
            takeover_ref=takeover_ref,
        )

    async def end_takeover(self, takeover_ref: str, *, restore_previous_focus: bool = True):
        self._helper_started = True
        return await (await self._ensure_manager()).end_takeover(
            takeover_ref, restore_previous_focus=restore_previous_focus,
        )

    async def handoff(self) -> None:
        await (await self._ensure_manager()).handoff()

    async def pause_for_user_activity(self) -> None:
        manager = self._manager
        if manager is None:
            raise ComputerSessionError("session_closed")
        await manager.pause_for_user_activity()

    async def resume(self, publication_id: str):
        self._helper_started = True
        return await (await self._ensure_manager()).resume(publication_id)

    async def resume_unbound(self, publication_id: str) -> None:
        # A targetless release performs no native I/O, so it never starts the helper.
        manager = self._manager
        if manager is None:
            raise ComputerSessionError("handoff_inactive")
        await manager.resume_unbound(publication_id)

    async def commit_resume_publication(self, publication_id: str) -> None:
        await (await self._ensure_manager()).commit_resume_publication(publication_id)

    def validate_unbound_resume_publication(self, publication_id: str) -> None:
        if self._manager is None:
            raise ComputerSessionError("stale_resume_publication")
        self._manager.validate_unbound_resume_publication(publication_id)

    def validate_targetless_resume_publication(self, publication_id: str) -> None:
        if self._manager is None:
            raise ComputerSessionError("stale_resume_publication")
        self._manager.validate_targetless_resume_publication(publication_id)

    async def abort_resume_publication(self, publication_id: str) -> None:
        manager = self._manager
        if manager is not None:
            await manager.abort_resume_publication(publication_id)

    async def close(self) -> bool:
        async with self._rotation_lock:
            self._generation += 1
            self._detach_current("computer_close")
            closed = await self._finish_teardown_locked(max_attempts=2)
            if not closed:
                raise ComputerSessionError(
                    "computer_teardown_failed",
                    "computer_teardown_failed: Computer Use helper cleanup remains incomplete",
                )
            return True

    def close_local_state(self) -> None:
        self.end_session("session_end")

    async def close_backend(self) -> None:
        await self.wait_for_teardown()

    def read_verified_png_artifact(self, *args, **kwargs):
        manager = self._manager
        if manager is None:
            raise ComputerSessionError("session_closed", "session_closed: Computer Use session ended")
        return manager.read_verified_png_artifact(*args, **kwargs)

    def read_verified_snapshot_pair(self, image_filename: str, detail_filename: str):
        manager = self._manager
        if manager is None:
            raise ComputerSessionError("session_closed", "session_closed: Computer Use session ended")
        return manager._read_verified_snapshot_pair(image_filename, detail_filename)

    def preflight_current_snapshot_artifacts(self) -> None:
        manager = self._manager
        if manager is None:
            raise ComputerSessionError("session_closed", "session_closed: Computer Use session ended")
        manager._preflight_current_snapshot_artifacts()

    def poison_artifact_session(self, *, clear_target: bool = True) -> None:
        manager = self._manager
        if manager is None:
            raise ComputerSessionError("session_closed", "session_closed: Computer Use session ended")
        manager.poison_artifact_session(clear_target=clear_target)

    def end_session(self, reason: str) -> None:
        """Detach one session synchronously and start tracked helper teardown."""
        self._generation += 1
        self._detach_current(reason)

    def _detach_current(self, reason: str) -> None:
        del reason
        self._grants.clear()
        self._helper_started = False
        if self._retired_manager is None and not self._teardown_error:
            self._capability = self._configured_capability()
        manager = self._manager
        self._manager = None
        if manager is None:
            return
        self._capability = self._configured_capability()
        self._retired_manager = manager
        try:
            self._schedule_teardown()
        except RuntimeError:
            try:
                asyncio.run(self.wait_for_teardown())
            except Exception as exc:  # noqa: BLE001 - hook records the degraded retryable state
                logger.warning("Computer Use synchronous teardown failed error_type=%s", type(exc).__name__)
            finally:
                task = self._teardown_task
                if task is not None and task.done():
                    self._teardown_task = None

    async def wait_for_teardown(self) -> None:
        """Await the tracked teardown; one later call retries a recorded failure."""
        async with self._rotation_lock:
            await self._await_prior_teardown_locked()

    async def _finish_teardown_locked(self, *, max_attempts: int) -> bool:
        for _attempt in range(max_attempts):
            try:
                await self._await_prior_teardown_locked()
                return True
            except ComputerSessionError as exc:
                if exc.code != "computer_teardown_failed":
                    raise
        logger.warning("Computer Use teardown remains incomplete after %d attempts", max_attempts)
        return False

    async def shutdown(self) -> bool:
        """Detach the current session and await teardown before its loop exits."""
        async with self._rotation_lock:
            if not self._permanently_shutting_down:
                self._permanently_shutting_down = True
                self._generation += 1
                self._detach_current("shutdown")
            return await self._finish_teardown_locked(max_attempts=2)


_LOCAL_COMPUTER_RUNTIMES: weakref.WeakKeyDictionary[ToolRegistry, LocalComputerRuntime] = (
    weakref.WeakKeyDictionary()
)


def register_local_computer_runtime(
    registry: ToolRegistry,
    *,
    helper_path: Path | str | None = None,
    cache_root: Path | str | None = None,
    model_capabilities: Collection[str] | Callable[[], Collection[str]] = frozenset(),
) -> LocalComputerRuntime | None:
    """Register Computer Use on a local macOS surface without starting it."""
    if sys.platform != "darwin":
        return None
    existing = _LOCAL_COMPUTER_RUNTIMES.get(registry)
    if existing is not None:
        return existing
    configured_cache = cache_root or os.getenv("ASTRA_COMPUTER_CACHE_ROOT", "").strip()
    if not configured_cache:
        configured_cache = state_path("computer-cache")
    runtime = LocalComputerRuntime(
        helper_path=helper_path,
        cache_root=configured_cache,
    )
    register_computer_tools(registry, runtime, model_capabilities=model_capabilities)
    _LOCAL_COMPUTER_RUNTIMES[registry] = runtime
    return runtime


def _bound_string(value: Any) -> str:
    text = str(value)
    return text[:_MAX_AX_STRING]


def _bounded_public_value(
    value: Any,
    *,
    depth: int = 0,
    state: dict[str, int] | None = None,
    max_depth: int = _MAX_AX_DEPTH,
    ax_tree: bool = False,
    collapse_static_menus: bool = False,
    menu_entry: bool = False,
) -> Any:
    state = state if state is not None else {"nodes": 0}
    value_limit = state.get("value_limit", _MAX_AX_NODES)
    if depth > max_depth or state["nodes"] >= value_limit:
        return "<truncated>"
    state["nodes"] += 1
    if isinstance(value, Mapping):
        mapping_output: dict[str, Any] = {}
        is_ax_node = ax_tree and isinstance(value.get("role"), str)
        if is_ax_node:
            if state.get("ax_nodes", 0) >= _MAX_AX_NODES:
                return "<truncated>"
            state["ax_nodes"] = state.get("ax_nodes", 0) + 1
        secure = any("secure" in str(value.get(field) or "").lower() for field in ("role", "subrole"))
        for raw_key, item in islice(value.items(), _MAX_AX_MAPPING_FIELDS):
            if state["nodes"] >= value_limit:
                break
            key = _bound_string(raw_key)
            if secure and key.lower() in {"value", "value_summary", "text"}:
                mapping_output[key] = "<redacted>"
            elif is_ax_node and menu_entry and key == "children" and item:
                mapping_output["children_omitted"] = True
                mapping_output["expansion_hint"] = (
                    "Use computer_snapshot subtree_ref with this element_ref to expand menu contents."
                )
            else:
                # AX attributes do not add element levels. Bound each attribute
                # with the generic metadata rules; only children extend the tree.
                child_tree = ax_tree and (not is_ax_node or key == "children")
                child_depth = (depth if key == "children" else 0) if is_ax_node else depth + 1
                mapping_output[key] = _bounded_public_value(
                    item, depth=child_depth, state=state,
                    max_depth=_MAX_AX_DEPTH if is_ax_node and key != "children" else max_depth,
                    ax_tree=child_tree,
                    collapse_static_menus=collapse_static_menus,
                    menu_entry=(collapse_static_menus and is_ax_node
                                and value.get("role") == "AXMenuBar" and key == "children"),
                )
        return mapping_output
    if isinstance(value, (list, tuple)):
        list_output: list[Any] = []
        for item in value[:_MAX_AX_NODES]:
            if state["nodes"] >= value_limit:
                break
            list_output.append(_bounded_public_value(
                item, depth=depth + 1, state=state, max_depth=max_depth, ax_tree=ax_tree,
                collapse_static_menus=collapse_static_menus, menu_entry=menu_entry,
            ))
        return list_output
    if isinstance(value, str):
        return value[:_MAX_AX_STRING]
    if isinstance(value, float) and not math.isfinite(value):
        return "<invalid-number>"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _bound_string(value)


_AX_SHELL_FIELDS = ("role", "subrole", "label", "title", "element_ref", "bounds", "index")


def _prune_ax_tree_by_role(value: Any, role: str) -> tuple[Any | None, int]:
    """只保留 role 命中的元素及其祖先链，返回 (剪枝树, 命中数)。

    存在的理由：展开预算是**按分支分摊**的，SPA 侧栏几百条同名兄弟会把整份预算吃光，
    右栏唯一的输入框 ref 永远挤不进来（oMLX 聊天页实机如此）。剪枝发生在预算约束**之前**，
    所以同名墙不能再挤掉目标；祖先只保留结构性字段，命中节点保留全部字段（含
    element_ref / actions，才能直接交给 computer_act）。
    """

    if not isinstance(value, Mapping):
        return None, 0
    children = value.get("children")
    kept: list[Any] = []
    matched = 0
    if isinstance(children, list):
        for child in children:
            pruned_child, count = _prune_ax_tree_by_role(child, role)
            matched += count
            if pruned_child is not None:
                kept.append(pruned_child)
    if str(value.get("role") or "") == role:
        node = dict(value)
        if isinstance(children, list):
            node["children"] = kept
        return node, matched + 1
    if kept:
        shell = {key: value[key] for key in _AX_SHELL_FIELDS if key in value}
        shell["children"] = kept
        return shell, matched
    return None, matched


def _find_ax_node_by_element_ref(value: Any, reference: str) -> Any | None:
    """在**未经截断的**原始快照树上按 element_ref 定位锚点节点。

    锚点必然来自上一次已公开的投影，因此它在原始树里一定可达；这里用原始 payload
    而不是截断后的树，避免"锚点自己就被深度剪掉了"的假失败。
    """

    stack: list[Any] = [value]
    remaining = _MAX_AX_NODES * 4
    while stack and remaining > 0:
        node = stack.pop()
        remaining -= 1
        if not isinstance(node, Mapping):
            continue
        if node.get("element_ref") == reference:
            return node
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(reversed(children))
    return None


def _iter_ax_nodes(value: Any, limit: int):
    stack: list[Any] = [value]
    remaining = limit
    while stack and remaining > 0:
        node = stack.pop()
        remaining -= 1
        if isinstance(node, Mapping):
            yield node
            children = node.get("children")
            if isinstance(children, list):
                stack.extend(reversed(children))


def _anchor_identity_key(node: Any) -> str | None:
    """元素的稳定身份 = role + bounds。element_ref 每次快照都重新生成，不能跨快照字面比对。"""

    if not isinstance(node, Mapping):
        return None
    bounds = node.get("bounds")
    if not isinstance(bounds, Mapping):
        return None
    try:
        shape = json.dumps(
            {key: bounds[key] for key in ("x", "y", "width", "height")},
            sort_keys=True,
            separators=(",", ":"),
        )
    except KeyError:
        return None
    return f"{node.get('role')}|{shape}"


def _find_anchor_by_stable_identity(tree: Any, prior_tree: Any, reference: str) -> Any | None:
    """在上一棵已公开的树里查出该 ref 的身份，再按同一身份定位新快照中的节点。

    同名身份可能对应多个节点（例如并列的标签项），此处取先序第一个；展开失败会如实报
    stale_target 而不是静默挑一个错的子树。
    """

    wanted = next(
        (
            _anchor_identity_key(node)
            for node in _iter_ax_nodes(prior_tree, _MAX_AX_NODES * 4)
            if node.get("element_ref") == reference
        ),
        None,
    )
    if not wanted:
        return None
    return next(
        (
            node
            for node in _iter_ax_nodes(tree, _MAX_AX_NODES * 4)
            if _anchor_identity_key(node) == wanted
        ),
        None,
    )


def _default_ax_depth(value: Any) -> int:
    if isinstance(value, Mapping) and isinstance(value.get("role"), str):
        return _MAX_AX_ELEMENT_DEPTH
    return _MAX_AX_DEPTH


def _bounded_ax_tree(
    value: Any, max_depth: int | None = None, *, collapse_static_menus: bool = True,
) -> Any:
    if max_depth is None:
        max_depth = _default_ax_depth(value)
    bounded = _bounded_public_value(value, max_depth=max_depth, ax_tree=True,
                                    state=_ax_projection_state(value),
                                    collapse_static_menus=collapse_static_menus)
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= _MAX_AX_JSON_BYTES:
        return bounded
    return {"truncated": True, "reason": "accessibility payload exceeded 512 KiB"}


def _ax_projection_state(value: Any) -> dict[str, int] | None:
    if isinstance(value, Mapping) and isinstance(value.get("role"), str):
        # AX elements have refs, flags, bounds and other scalar attributes. A
        # separate finite value budget keeps these from consuming the element
        # allowance. Generic JSON retains its original, smaller value budget.
        return {"nodes": 0, "ax_nodes": 0, "value_limit": _MAX_AX_NODES * 32}
    return None


def _resolve_ax_subtree(value: Any, subtree_ref: str, *, prior_tree: Any = None) -> Any:
    anchor = _find_ax_node_by_element_ref(value, subtree_ref)
    if anchor is None and prior_tree is not None:
        # 实机暴露：ref 每快照重生成，锚点必然来自上一轮公开的那棵树。
        anchor = _find_anchor_by_stable_identity(value, prior_tree, subtree_ref)
    if anchor is None:
        # 三事实必须放在**错误消息里**：Python 的 logger 不落盘（.astra/logs 为空），写成
        # 日志等于没写；而 message 会原样透传给调用方，一眼定位断在哪一环。
        prior_present = prior_tree is not None
        prior_hit = (
            _find_ax_node_by_element_ref(prior_tree, subtree_ref) is not None
            if prior_present
            else False
        )
        raise ComputerSessionError(
            "stale_target",
            "stale_target: subtree_ref could not be resolved "
            f"(ref={subtree_ref} prior_tree_present={prior_present} "
            f"ref_in_prior_tree={prior_hit} identity_matched={prior_present and prior_hit})",
        )
    return anchor


def _bounded_ax_subtree(value: Any, subtree_ref: str, *, prior_tree: Any | None = None) -> Any:
    """以 subtree_ref 指向的节点为新根，重新计数深度与节点预算渲染一次。

    helper 建树限深 20 层（Snapshot.swift maximumAXDepth），公开投影按 AX 元素边
    限深 _MAX_AX_ELEMENT_DEPTH，children 列表不额外计层。锚点子树展开只重新渲染 payload 里
    已有的数据，不向 helper 多要任何权限，也不改变授权链。
    """

    anchor = _resolve_ax_subtree(value, subtree_ref, prior_tree=prior_tree)
    bounded = _bounded_public_value(anchor, max_depth=_SUBTREE_AX_DEPTH, ax_tree=True,
                                    state=_ax_projection_state(anchor))
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= _MAX_AX_JSON_BYTES:
        return bounded
    # 定向展开时不学整窗口的"整树替换"降级：那会把已取得的深层数据全部丢掉，比不展开
    # 更瞎。这里明确失败并给出可执行提示。
    raise ComputerSessionError(
        "snapshot_failed",
        "snapshot_failed: the expanded subtree exceeds the accessibility payload budget; "
        "anchor subtree_ref on a narrower element",
    )


def _ax_tree_contains_element_ref(tree: Any, reference: str) -> bool:
    stack = [tree] if isinstance(tree, Mapping) else []
    remaining = _MAX_AX_NODES
    while stack and remaining > 0:
        node = stack.pop()
        remaining -= 1
        if node.get("element_ref") == reference:
            return True
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(
                child for child in reversed(children)
                if isinstance(child, Mapping)
            )
    return False


def _normalized_summary_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _encoded_summary_size(entries: Sequence[Mapping[str, str | int]]) -> int:
    return len(json.dumps(
        entries,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8"))


def _text_detail_summary(root: Mapping[str, Any]) -> list[dict[str, str | int]]:
    """Create a deterministic bounded pre-order summary of a validated AX tree."""

    entries: list[dict[str, str | int]] = []
    truncated = False
    stack: list[Mapping[str, Any]] = [root] if root else []
    while stack:
        node = stack.pop()
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(
                child for child in reversed(children)
                if isinstance(child, Mapping)
            )
        role = _normalized_summary_text(node.get("role"))
        parts = [
            text
            for key in ("label", "title")
            if (text := _normalized_summary_text(node.get(key)))
        ]
        if node.get("redacted") is not True:
            value = _normalized_summary_text(node.get("value"))
            if value:
                parts.append(value)
        text = " ".join(parts)
        if not role or not text:
            continue
        entry: dict[str, str | int] = {"role": role, "text": text}
        index = node.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 1:
            entry["index"] = index
        if entries and entries[-1] == entry:
            continue
        if (
            len(entries) >= _MAX_TEXT_SUMMARY_ENTRIES
            or _encoded_summary_size([*entries, entry]) > _MAX_TEXT_SUMMARY_BYTES
        ):
            truncated = True
            break
        entries.append(entry)
    if truncated:
        while entries and (
            len(entries) >= _MAX_TEXT_SUMMARY_ENTRIES
            or _encoded_summary_size([*entries, _TEXT_SUMMARY_TRUNCATION_MARKER])
            > _MAX_TEXT_SUMMARY_BYTES
        ):
            entries.pop()
        entries.append(dict(_TEXT_SUMMARY_TRUNCATION_MARKER))
    return entries


def _read_verified_snapshot_pair(
    manager: ComputerSessionManager | LocalComputerRuntime,
    image_filename: str,
    detail_filename: str,
):
    if isinstance(manager, LocalComputerRuntime):
        return manager.read_verified_snapshot_pair(image_filename, detail_filename)
    return manager._read_verified_snapshot_pair(image_filename, detail_filename)


def _preflight_current_snapshot_artifacts(
    manager: ComputerSessionManager | LocalComputerRuntime,
) -> None:
    if isinstance(manager, LocalComputerRuntime):
        manager.preflight_current_snapshot_artifacts()
        return
    manager._preflight_current_snapshot_artifacts()


def _safe_action_metadata(value: Mapping[str, object] | None) -> dict[str, Any]:
    """Keep only protocol acknowledgement metadata, never helper diagnostics."""
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    acknowledgement = value.get("last_acknowledged_action")
    if (
        isinstance(acknowledgement, int)
        and not isinstance(acknowledgement, bool)
        and -1 <= acknowledgement < MAX_ACTIONS
    ):
        safe["last_acknowledged_action"] = acknowledgement
    raw_outcomes = value.get("outcomes")
    if isinstance(raw_outcomes, list):
        outcomes: list[dict[str, Any]] = []
        known_codes = {code.value for code in ComputerErrorCode}
        known_effects = {"verified", "unverified", "noop"}
        for raw in raw_outcomes[:MAX_ACTIONS]:
            if not isinstance(raw, Mapping):
                continue
            item: dict[str, Any] = {}
            index = raw.get("index")
            ok = raw.get("ok")
            error_code = raw.get("error_code")
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < MAX_ACTIONS:
                item["index"] = index
            if isinstance(ok, bool):
                item["ok"] = ok
            if isinstance(error_code, str) and error_code in known_codes:
                item["error_code"] = error_code
            effect = raw.get("effect_verification")
            if ok is True and isinstance(effect, str) and effect in known_effects:
                item["effect_verification"] = effect
            if ok is False and "input_diagnostics" in raw:
                try:
                    diagnostics = KeyboardFailureDiagnostics.from_mapping(raw["input_diagnostics"])
                except (TypeError, ValueError):
                    pass
                else:
                    item["input_diagnostics"] = diagnostics.to_mapping()
            if ok is True and raw.get("observation_required") is True:
                item["observation_required"] = True
            if item:
                outcomes.append(item)
        safe["outcomes"] = outcomes
    return safe


def _model_action_metadata(value: Mapping[str, object] | None) -> dict[str, Any]:
    safe = _safe_action_metadata(value)
    acknowledgement = safe.get("last_acknowledged_action")
    outcomes = safe.get("outcomes")
    acknowledged = (
        isinstance(acknowledgement, int) and acknowledgement >= 0
    ) or (
        isinstance(outcomes, list)
        and any(outcome.get("ok") is True for outcome in outcomes)
    )
    if acknowledged:
        safe["status"] = "action_acknowledged"
        if isinstance(outcomes, list) and outcomes and outcomes[-1].get("observation_required") is True:
            safe["observation_required"] = True
            if isinstance(acknowledgement, int) and acknowledgement >= 0:
                safe["next_action_index"] = acknowledgement + 1
            safe["continuation_hint"] = (
                "Inspect the fresh state before sending more input. Only the listed prefix was sent; "
                "do not replay it or automatically resume the remaining actions."
            )
        successful = (
            [outcome for outcome in outcomes if outcome.get("ok") is True]
            if isinstance(outcomes, list)
            else []
        )
        effects = [outcome.get("effect_verification") for outcome in successful]
        if "noop" in effects:
            safe["effect_verification"] = "noop"
            safe["verification_hint"] = (
                "The typed state stayed unchanged through the bounded settle window. "
                "Inspect a fresh snapshot and choose a different explicit action; do not "
                "automatically replay this action."
            )
        elif (
            isinstance(outcomes, list)
            and outcomes
            and len(successful) == len(outcomes)
            and acknowledgement == len(outcomes) - 1
            and effects
            and all(effect == "verified" for effect in effects)
        ):
            safe["effect_verification"] = "verified"
        else:
            safe["effect_verification"] = "unverified"
    return safe


def _fully_acknowledged_action_batch(value: Mapping[str, object] | None, count: int) -> bool:
    """Require complete dispatch receipts, never infer delivery from screen changes."""
    if not isinstance(value, Mapping) or count <= 0:
        return False
    acknowledged = value.get("last_acknowledged_action")
    outcomes = value.get("outcomes")
    return (
        type(acknowledged) is int
        and acknowledged == count - 1
        and isinstance(outcomes, list)
        and len(outcomes) == count
        and all(
            isinstance(outcome, Mapping)
            and type(outcome.get("index")) is int
            and outcome["index"] == index
            and outcome.get("ok") is True
            and outcome.get("error_code") is None
            for index, outcome in enumerate(outcomes)
        )
    )


_POST_OBSERVATION_ERROR_CODES = frozenset({
    "helper_failed", "snapshot_failed", "observation_timeout", "target_gone",
    "stale_target", "stale_snapshot", "overlay_blocked", "window_content_unavailable",
    "target_not_frontmost", "permission_denied", "secure_target", "protected_context",
    "unsafe_artifact", "user_activity_paused", "handoff_active", "session_changed",
    "target_required",
})


def _post_observation_error_code(error: Exception) -> str | None:
    """Disclose a bounded reason, never a helper/transport exception's contents."""
    if isinstance(error, TimeoutError):
        return "observation_timeout"
    code = (error.error.code.value if isinstance(error, HelperApplicationError)
            else error.code if isinstance(error, ComputerSessionError) else None)
    return code if isinstance(code, str) and code in _POST_OBSERVATION_ERROR_CODES else None


# Screenshots leave this module as request-local data URLs. Several remote gateways stop
# draining request bodies somewhere around 1 MiB and let the client block until it reports
# a write timeout, which surfaces as a provider timeout instead of a clean rejection. An
# oversized capture is therefore recompressed here, at full resolution first: only the byte
# cost changes, never the pixel dimensions, until the quality ladder alone cannot fit.
_POST_ACTION_OBSERVATION_TIMEOUT_SECONDS = 15.0
_SCREENSHOT_DATA_URL_BUDGET_BYTES = 320_000
_SCREENSHOT_WEBP_QUALITIES = (85, 78, 70, 62)
_SCREENSHOT_JPEG_QUALITY = 70
_SCREENSHOT_MIN_LONG_EDGE = 1600
_SCREENSHOT_SCALES = (1.0, 0.75, 0.5)


def _screenshot_data_url_budget() -> int:
    raw = str(os.getenv("CU_IMAGE_MAX_DATA_URL_BYTES") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return _SCREENSHOT_DATA_URL_BUDGET_BYTES
    return value if value > 0 else _SCREENSHOT_DATA_URL_BUDGET_BYTES


def _flatten_screenshot(image: Any) -> Any:
    """Drop the alpha channel onto white; providers disagree about transparency."""
    from PIL import Image

    if image.mode == "RGB":
        return image
    rgba = image.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.split()[-1])
    return flat


# Per-loop slots remain occupied until the thread actually finishes, even when
# its caller is cancelled. No mutable session state crosses this boundary.
_ENCODING_SLOTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_ENCODING_TASKS: set[asyncio.Task] = set()


async def _encode_screenshot_async(png: bytes) -> tuple[str, dict[str, Any]]:
    loop = asyncio.get_running_loop()
    slots = _ENCODING_SLOTS.setdefault(loop, asyncio.Semaphore(2))
    queued_at = loop.time()
    await slots.acquire()
    started_at = loop.time()
    task = asyncio.create_task(asyncio.to_thread(_encode_screenshot_data_url, png))
    _ENCODING_TASKS.add(task)

    def finished(worker: asyncio.Task) -> None:
        slots.release()
        logger.debug(
            "computer image encoding queue_ms=%.3f worker_ms=%.3f",
            (started_at - queued_at) * 1000, (loop.time() - started_at) * 1000,
        )
        _ENCODING_TASKS.discard(worker)
        if not worker.cancelled():
            worker.exception()

    task.add_done_callback(finished)
    return await asyncio.shield(task)


def _encode_screenshot_data_url(png: bytes) -> tuple[str, dict[str, Any]]:
    """Return one screenshot data URL inside the body budget, plus bounded metadata.

    Small captures keep their original PNG bytes untouched. Only an oversized capture is
    recompressed, and the original bytes are never replaced by a larger candidate.
    """
    budget = _screenshot_data_url_budget()

    def encoded_size(data: bytes, fmt: str) -> int:
        return len(f"data:image/{fmt};base64,") + 4 * ((len(data) + 2) // 3)

    def publish(data: bytes, meta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return f"data:image/{meta['format']};base64," + base64.b64encode(data).decode("ascii"), meta

    png_size = encoded_size(png, "png")
    fallback_meta: dict[str, Any] = {
        "source_bytes": len(png),
        "data_url_bytes": png_size,
        "budget_bytes": budget,
        "format": "png",
        "quality": None,
        "scale": 1.0,
        "recompressed": False,
        "fits_budget": png_size <= budget,
        "reason": "within_budget" if png_size <= budget else "png_only",
    }
    if png_size <= budget:
        return publish(png, fallback_meta)

    try:
        from PIL import Image
    except ImportError:
        fallback_meta["reason"] = "pillow_unavailable"
        return publish(png, fallback_meta)
    try:
        source = Image.open(io.BytesIO(png))
        source.load()
        source = _flatten_screenshot(source)
    except (OSError, ValueError, RuntimeError):
        fallback_meta["reason"] = "undecodable"
        return publish(png, fallback_meta)

    best_data = png
    best_size = png_size
    best_meta = fallback_meta
    long_edge = max(source.size)
    ladder = [("webp", quality) for quality in _SCREENSHOT_WEBP_QUALITIES]
    ladder.append(("jpeg", _SCREENSHOT_JPEG_QUALITY))
    for scale in _SCREENSHOT_SCALES:
        image = source
        if scale != 1.0:
            if int(long_edge * scale) < _SCREENSHOT_MIN_LONG_EDGE:
                break
            image = source.resize(
                (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
                Image.Resampling.LANCZOS,
            )
        for fmt, quality in ladder:
            kwargs: dict[str, Any] = {"quality": quality}
            if fmt == "jpeg":
                kwargs["optimize"] = True
            buffer = io.BytesIO()
            image.save(buffer, fmt.upper(), **kwargs)
            data = buffer.getvalue()
            size = encoded_size(data, fmt)
            meta: dict[str, Any] = {
                "source_bytes": len(png),
                "data_url_bytes": size,
                "budget_bytes": budget,
                "format": fmt,
                "quality": quality,
                "scale": scale,
                "recompressed": True,
                "fits_budget": size <= budget,
                "reason": "fits_budget" if size <= budget else "searching",
            }
            if size < best_size:
                best_data, best_size, best_meta = data, size, meta
            if size <= budget:
                return publish(data, meta)
    return publish(best_data, best_meta)


def _public_snapshot_payload(
    manager: ComputerSessionManager | LocalComputerRuntime,
    snapshot: ComputerSnapshot,
    *,
    scope: str,
    action_result: Mapping[str, object] | None = None,
    verified_state: ComputerVerifiedAppState | None = None,
    subtree_ref: str | None = None,
    prior_ax_tree: Any = None,
    role_filter: str | None = None,
) -> tuple[dict[str, Any], bytes, tuple[int, int], str]:
    try:
        wire_payload = dict(snapshot.payload)
        wire_payload.pop("text_detail_path", None)
        validate_snapshot_payload(wire_payload)
    except ValueError as exc:
        raise ComputerSessionError(
            "protocol_mismatch",
            f"protocol_mismatch: invalid native snapshot metadata ({exc})",
        ) from exc
    artifact = snapshot.payload.get("image_artifact")
    if not isinstance(artifact, str):
        raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: snapshot omitted image artifact")
    if verified_state is None:
        path, png, identity, digest = manager.read_verified_png_artifact(artifact)
        target = manager.target
        target_generation = None
    else:
        if (
            verified_state.snapshot.snapshot_id != snapshot.snapshot_id
            or verified_state.snapshot.payload.get("image_artifact") != artifact
        ):
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: verified app state crossed snapshot identity",
            )
        path = verified_state.image_path
        png = verified_state.image_data
        identity = verified_state.image_identity
        digest = verified_state.image_sha256
        target = verified_state.target
        target_generation = verified_state.target_generation
    if target is None:
        raise ComputerSessionError("target_required", "target_required: call focus first")
    raw_ax_tree = snapshot.payload.get("ax_tree", {})
    capabilities = observation_capabilities(raw_ax_tree)
    if isinstance(raw_ax_tree, Mapping) and raw_ax_tree.get("observation_scope") == "native_subtree":
        subtree_ref = None  # The helper has already re-read and re-anchored it.
    if role_filter:
        filter_root = (
            _resolve_ax_subtree(raw_ax_tree, subtree_ref, prior_tree=prior_ax_tree)
            if subtree_ref else raw_ax_tree
        )
        pruned, matches = _prune_ax_tree_by_role(filter_root, role_filter)
        if matches == 0:
            raise ComputerSessionError(
                "invalid_arguments",
                f"invalid_arguments: role_filter {role_filter} matched no element "
                "in the current snapshot",
            )
        public_ax_tree = _bounded_ax_tree(
            pruned, max_depth=_FILTERED_AX_DEPTH, collapse_static_menus=False,
        )
    else:
        public_ax_tree = (
            _bounded_ax_subtree(raw_ax_tree, subtree_ref, prior_tree=prior_ax_tree)
            if subtree_ref
            else _bounded_ax_tree(
                raw_ax_tree,
                collapse_static_menus=not (
                    isinstance(raw_ax_tree, Mapping) and (
                        raw_ax_tree.get("role") in {"AXMenu", "AXMenuBar"}
                        or raw_ax_tree.get("observation_scope") == "native_subtree"
                    )
                ),
            )
        )
    payload: dict[str, Any] = {
        "success": True,
        "type": "image_attachment",
        "detail": "original",
        "image_paths": [str(path)],
        "image_artifact": artifact,
        "session_id": manager.session_id,
        "app_ref": target.app_ref,
        "window_id": target.window_ref,
        "snapshot_id": snapshot.snapshot_id,
        "scope": scope,
        "logical_size": _bounded_public_value(snapshot.payload.get("logical_size", {})),
        "pixel_size": _bounded_public_value(snapshot.payload.get("pixel_size", {})),
        "backing_scale": _bounded_public_value(snapshot.payload.get("backing_scale")),
        "capture_bounds": _bounded_public_value(snapshot.payload.get("capture_bounds", {})),
        "ax_tree": public_ax_tree,
        "message": "Inspect the attached current macOS target image before choosing the next action.",
    }
    if capabilities:
        payload["capabilities"] = capabilities
        payload["form_controls"] = form_elements(raw_ax_tree)
        if "checked_click_v1" in capabilities:
            payload["choice_action_contract"] = {
                "tool": "computer_act", "type": "click", "state_field": "checked",
                "target_fields": ["element_ref", "element_index"], "interaction_mode": "auto",
                "max_actions": MAX_ACTIONS,
            }
        payload["coordinate_space"] = "window_logical" if scope == "target_window" else "display_logical_observation_only"
        payload["message"] = "Use current form_controls element_index/ref for fields, including deep web content. Batch independent choices with click + checked; verify receipts. Coordinates are window-local logical units, not screenshot pixels or old Appshot coordinates."
    if "has_default_button" in snapshot.payload:
        payload["has_default_button"] = snapshot.payload["has_default_button"]
    default_button_ref = snapshot.payload.get("default_button_element_ref")
    if (
        isinstance(default_button_ref, str)
        and _ax_tree_contains_element_ref(public_ax_tree, default_button_ref)
    ):
        payload["default_button_element_ref"] = default_button_ref
    if target_generation is not None:
        payload["window_ref"] = target.window_ref
        payload["target_generation"] = target_generation
    if scope == "display":
        if "display_id" not in snapshot.payload or "target_window_bounds" not in snapshot.payload:
            raise ComputerSessionError(
                "protocol_mismatch",
                "protocol_mismatch: display snapshot omitted display identity or target bounds",
            )
        payload["display_id"] = _bounded_public_value(snapshot.payload["display_id"])
        payload["target_window_bounds"] = _bounded_public_value(
            snapshot.payload["target_window_bounds"]
        )
    if action_result is not None:
        safe_action_result = _model_action_metadata(action_result)
        payload["action_result"] = safe_action_result
        if safe_action_result.get("status") == "action_acknowledged":
            payload["message"] = (
                "The action was acknowledged, but this does not prove the intended "
                "application effect. Inspect this fresh observation before continuing "
                "or claiming completion."
            )
    return payload, png, identity, digest


def register_computer_tools(
    registry: ToolRegistry,
    manager: ComputerSessionManager | LocalComputerRuntime,
    *,
    model_capabilities: Collection[str] | Callable[[], Collection[str]] = frozenset({"tools", "vision"}),
) -> ComputerSessionManager | LocalComputerRuntime:
    """Register the nine provider-neutral Computer Use tools."""

    verified_artifacts: dict[str, tuple[tuple[int, int], str]] = {}
    verified_detail_artifacts: dict[str, tuple[str, Mapping[str, object]]] = {}
    approvals = ScopedApprovalStore(approved_scopes=registry.approved_permission_scopes)
    trusted_catalog: dict[str, dict[str, Any]] = {}
    trusted_catalog_generation = 0
    observation_authority_generation = 0
    trusted_target: dict[str, Any] = {}
    trusted_snapshot: dict[str, Any] = {}
    effect_snapshot: tuple[str, Any] = ("", None)
    published_image_geometry: tuple[str, tuple[float, ...]] = ("", ())
    trusted_wps_directory = ""
    trusted_wps_destination = ""
    trusted_wps_filename = ""
    trusted_wps_document_title = ""
    pending_wps_modal_target: tuple[str, str, str] | None = None
    approval_scope_secret = secrets.token_bytes(32)
    prepared_acts: dict[str, _PreparedComputerAct] = {}
    # Diagnostics survive grant invalidation, but never authorize execution.
    act_receipt_states: dict[str, _PreparedComputerAct] = {}
    pending_clicks = PendingClickGuard()
    pending_foreground_digest = ""
    pending_foreground_snapshot = ""
    catalog_target_operation_lock = asyncio.Lock()
    resume_publication_owner: str | None = None

    def clear_trusted_wps_destination() -> None:
        nonlocal trusted_wps_destination, trusted_wps_directory
        nonlocal trusted_wps_filename, trusted_wps_document_title
        nonlocal pending_wps_modal_target
        trusted_wps_directory = ""
        trusted_wps_destination = ""
        trusted_wps_filename = ""
        trusted_wps_document_title = ""
        pending_wps_modal_target = None

    def clear_computer_grants() -> None:
        nonlocal pending_foreground_digest
        nonlocal pending_foreground_snapshot
        prefixes = (
            f"computer-write:{manager.session_id}:",
            f"computer-write-batch:{manager.session_id}:",
            f"computer-foreground:{manager.session_id}:",
            f"computer-observe-display:{manager.session_id}",
            "computer-observe-snapshot:",
            "computer-observe-app-state:",
        )
        approvals.approved_scopes.difference_update({
            scope for scope in approvals.approved_scopes
            if scope.startswith(prefixes)
        })
        manager.grants.clear()
        prepared_acts.clear()
        pending_foreground_digest = ""
        pending_foreground_snapshot = ""

    def clear_snapshot_grants() -> None:
        approvals.approved_scopes.difference_update({
            scope for scope in approvals.approved_scopes
            if scope.startswith((
                "computer-observe-snapshot:",
                "computer-observe-app-state:",
            ))
        })

    def clear_write_grants(*, preserve_foreground_session: bool = False) -> None:
        nonlocal pending_foreground_digest
        nonlocal pending_foreground_snapshot
        prefixes = (
            f"computer-write:{manager.session_id}:",
            f"computer-write-batch:{manager.session_id}:",
        ) + (() if preserve_foreground_session else (f"computer-foreground:{manager.session_id}:",))
        approvals.approved_scopes.difference_update({
            scope for scope in approvals.approved_scopes
            if scope.startswith(prefixes)
        })
        prepared_acts.clear()
        pending_foreground_digest = ""
        pending_foreground_snapshot = ""

    def _trusted_text(value: Any, maximum: int = 1_000) -> str:
        if not isinstance(value, str):
            return ""
        return value[:maximum]

    def _approval_safe_path(value: Any) -> str:
        path = _trusted_text(value, 4_000)
        private_root = str(manager.session_dir).rstrip("/")
        if (
            manager.session_id in path
            or path == private_root
            or path.startswith(private_root + "/")
        ):
            return ""
        return path

    def remember_catalog(apps: list[dict[str, object]]) -> None:
        nonlocal trusted_catalog_generation, observation_authority_generation
        trusted_catalog_generation = manager.catalog_generation
        observation_authority_generation += 1
        trusted_catalog.clear()
        for raw_app in apps[:100]:
            if not isinstance(raw_app, Mapping):
                continue
            raw_app_ref = raw_app.get("app_ref")
            app_ref = raw_app_ref if isinstance(raw_app_ref, str) else ""
            bundle_id = _trusted_text(raw_app.get("bundle_id"), 512)
            app_version = _trusted_text(raw_app.get("app_version"), 64)
            if not app_ref or not bundle_id:
                continue
            windows: dict[str, dict[str, str]] = {}
            raw_windows = raw_app.get("windows")
            if isinstance(raw_windows, list):
                for raw_window in raw_windows[:100]:
                    if not isinstance(raw_window, Mapping):
                        continue
                    raw_window_ref = raw_window.get("window_ref")
                    window_ref = raw_window_ref if isinstance(raw_window_ref, str) else ""
                    if not window_ref:
                        continue
                    windows[window_ref] = {
                        "window_identity_ref": _trusted_text(raw_window.get("window_identity_ref"), 256),
                        "window_title": _trusted_text(raw_window.get("title"), 1_000),
                        "window_role": _trusted_text(raw_window.get("role"), 256),
                        "known_path": _approval_safe_path(
                            raw_window.get("document_path")
                            or raw_window.get("file_path")
                            or raw_window.get("path"),
                        ),
                    }
            trusted_catalog[app_ref] = {
                "application": _trusted_text(raw_app.get("name"), 512) or bundle_id,
                "bundle_id": bundle_id,
                "app_version": app_version,
                "windows": windows,
            }

    def remember_target(app_ref: str, window_ref: str) -> None:
        nonlocal observation_authority_generation
        nonlocal trusted_wps_destination, trusted_wps_directory
        nonlocal trusted_wps_filename, trusted_wps_document_title
        nonlocal pending_wps_modal_target
        observation_authority_generation += 1
        clear_snapshot_grants()
        previous = (trusted_target.get("app_ref"), trusted_target.get("window_ref"))
        selected = trusted_catalog.get(app_ref)
        window = selected.get("windows", {}).get(window_ref) if selected is not None else None
        catalog_title = (
            str(window.get("window_title") or "").strip().casefold()
            if isinstance(window, Mapping)
            else ""
        )
        pending = pending_wps_modal_target
        same_target_modal = (
            pending is not None
            and isinstance(selected, Mapping)
            and selected.get("bundle_id") == pending[0]
            and (
                pending[1] == catalog_title
                or catalog_title in _WPS_DESTINATION_MODAL_TITLES
                or bool(pending[2]) and pending[2] == catalog_title
            )
        ) or (
            previous == (app_ref, window_ref)
            and str(trusted_snapshot.get("window_title") or "").strip().casefold()
            in _WPS_DESTINATION_MODAL_TITLES
        )
        pending_wps_modal_target = None
        trusted_target.clear()
        trusted_snapshot.clear()
        if isinstance(selected, Mapping) and isinstance(window, Mapping):
            trusted_target.update({
                "metadata_trusted": True,
                "app_ref": app_ref,
                "window_ref": window_ref,
                "application": selected.get("application", ""),
                "bundle_id": selected.get("bundle_id", ""),
                "app_version": selected.get("app_version", ""),
                "window_title": window.get("window_title", ""),
                "window_role": window.get("window_role", ""),
                "known_path": window.get("known_path", ""),
            })
            if selected.get("bundle_id") == "com.kingsoft.wpsoffice.mac":
                document_path = _approval_safe_path(window.get("known_path"))
                if same_target_modal:
                    # WPS commonly exposes a sheet as a contained AXWindow
                    # while the app catalog still reports the underlying
                    # document.  A defensive refocus of that same target must
                    # not erase the destination proven inside the modal.
                    pass
                elif document_path and os.path.isabs(document_path):
                    trusted_wps_directory = str(Path(document_path).parent.resolve())
                    trusted_wps_destination = ""
                    trusted_wps_filename = ""
                elif (
                    str(window.get("window_title") or "").strip().casefold()
                    not in _WPS_DESTINATION_MODAL_TITLES
                ):
                    clear_trusted_wps_destination()
                if not same_target_modal and catalog_title not in _WPS_DESTINATION_MODAL_TITLES:
                    trusted_wps_document_title = catalog_title
            else:
                clear_trusted_wps_destination()
        else:
            trusted_target.update({
                "metadata_trusted": False,
                "app_ref": app_ref,
                "window_ref": window_ref,
            })
        if previous != (app_ref, window_ref):
            clear_write_grants(preserve_foreground_session=True)

    def remember_snapshot(snapshot: ComputerSnapshot) -> None:
        nonlocal effect_snapshot
        effect_snapshot = (snapshot.snapshot_id, snapshot.payload.get("ax_tree"))
        nonlocal trusted_wps_destination, trusted_wps_directory, trusted_wps_filename
        elements: dict[str, dict[str, Any]] = {}
        known_path = str(trusted_target.get("known_path") or "")
        if (
            not known_path
            and trusted_snapshot.get("metadata_trusted") is True
            and trusted_snapshot.get("session_id") == manager.session_id
            and trusted_snapshot.get("app_ref") == trusted_target.get("app_ref")
            and trusted_snapshot.get("window_ref") == trusted_target.get("window_ref")
        ):
            known_path = str(trusted_snapshot.get("known_path") or "")
        focused_element_ref = ""
        focused_role = ""
        focused_value = ""
        remaining = _MAX_AX_NODES
        raw_tree = snapshot.payload.get("ax_tree", {})
        max_depth = 64 if observation_capabilities(raw_tree) else _default_ax_depth(raw_tree)

        def visit(value: Any, depth: int = 0) -> None:
            nonlocal focused_element_ref, focused_role, focused_value, known_path, remaining
            if remaining <= 0 or depth > max_depth:
                return
            remaining -= 1
            if isinstance(value, Mapping):
                reference = _trusted_text(value.get("element_ref"), MAX_ELEMENT_REF_SCALARS)
                if reference:
                    metadata: dict[str, Any] = {}
                    for key in ("role", "subrole", "label", "title", "help"):
                        text = _trusted_text(value.get(key), _MAX_AX_STRING)
                        if text:
                            metadata[key] = text
                    raw_actions = value.get("actions")
                    if isinstance(raw_actions, list):
                        metadata["actions"] = [
                            _trusted_text(item, 128) for item in raw_actions[:32]
                            if _trusted_text(item, 128)
                        ]
                    raw_bounds = value.get("bounds")
                    if isinstance(raw_bounds, Mapping):
                        bounds: dict[str, float] = {}
                        for key in ("x", "y", "width", "height"):
                            item = raw_bounds.get(key)
                            if (
                                isinstance(item, (int, float))
                                and not isinstance(item, bool)
                                and math.isfinite(item)
                            ):
                                bounds[key] = float(item)
                        if len(bounds) == 4 and bounds["width"] >= 0 and bounds["height"] >= 0:
                            metadata["bounds"] = bounds
                    path = _approval_safe_path(
                        value.get("document_path")
                        or value.get("file_path")
                        or value.get("path"),
                    )
                    if path:
                        metadata["path"] = path
                        known_path = path
                    elements[reference] = metadata
                    if value.get("focused") is True:
                        focused_element_ref = reference
                        focused_role = _trusted_text(value.get("role"), 128)
                        focused_value = _trusted_text(value.get("value"), 4_000)
                if isinstance(value.get("role"), str):
                    children = value.get("children")
                    if isinstance(children, list):
                        for child in children:
                            visit(child, depth + 1)
                else:
                    for child in value.values():
                        visit(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    visit(child, depth + 1)

        visit(raw_tree)
        if (
            trusted_target.get("bundle_id") == "com.kingsoft.wpsoffice.mac"
            and focused_role == "AXTextField"
            and focused_value not in {"", "<redacted>"}
        ):
            if os.path.isabs(focused_value) and os.path.isdir(focused_value):
                observed_directory = str(Path(focused_value).resolve(strict=True))
                if _approval_safe_path(observed_directory):
                    trusted_wps_directory = observed_directory
                    if trusted_wps_filename:
                        trusted_wps_destination = str(
                            Path(observed_directory, trusted_wps_filename)
                        )
                        known_path = trusted_wps_destination
                    else:
                        trusted_wps_destination = ""
            elif (
                trusted_wps_directory
                and str(trusted_target.get("window_title") or "").strip().casefold()
                in {"输出为pdf", "export as pdf"}
                and Path(focused_value).name == focused_value
                and not Path(focused_value).suffix
                and focused_value not in {".", ".."}
                and not focused_value.startswith(".")
                and "\x00" not in focused_value
            ):
                destination = str(Path(trusted_wps_directory, focused_value + ".pdf"))
                if _approval_safe_path(destination):
                    trusted_wps_filename = focused_value + ".pdf"
                    known_path = destination
                    trusted_wps_destination = destination
            elif (
                trusted_wps_directory
                and Path(focused_value).name == focused_value
                and focused_value not in {".", ".."}
                and "\x00" not in focused_value
                and Path(focused_value).suffix.casefold() in {".docx", ".pdf"}
            ):
                destination = str(Path(trusted_wps_directory, focused_value))
                if _approval_safe_path(destination):
                    trusted_wps_filename = focused_value
                    known_path = destination
                    trusted_wps_destination = destination
            elif trusted_wps_destination:
                known_path = trusted_wps_destination
        elif (
            trusted_target.get("bundle_id") == "com.kingsoft.wpsoffice.mac"
            and trusted_wps_destination
        ):
            known_path = trusted_wps_destination
        if trusted_target.get("bundle_id") == "com.apple.finder":
            def finder_path(value: Any) -> str:
                if isinstance(value, list):
                    for child in value:
                        candidate = finder_path(child)
                        if candidate:
                            return candidate
                    return ""
                if not isinstance(value, Mapping):
                    return ""
                role = _trusted_text(value.get("role"), 128).casefold()
                label = _trusted_text(value.get("label") or value.get("title"), 128).casefold()
                if role == "axlist" and label in {"path", "路径"}:
                    components: list[str] = []

                    def collect_components(item: Any) -> None:
                        if isinstance(item, Mapping):
                            component = _trusted_text(item.get("value"), 512)
                            if component:
                                components.append(component)
                            for child in item.get("children", []):
                                collect_components(child)
                        elif isinstance(item, list):
                            for child in item:
                                collect_components(child)

                    collect_components(value.get("children", []))
                    if components and components[0].casefold() in {
                        "macintosh hd", "macintosh 硬盘",
                    }:
                        components = components[1:]
                    if components and all(
                        component not in {".", ".."}
                        and "/" not in component
                        and "\\" not in component
                        and "\x00" not in component
                        for component in components
                    ):
                        return "/" + "/".join(components)
                for child in value.values():
                    candidate = finder_path(child)
                    if candidate:
                        return candidate
                return ""

            observed_path = finder_path(raw_tree)
            if observed_path:
                known_path = observed_path
        snapshot_target = dict(trusted_target)
        if isinstance(raw_tree, Mapping):
            root_role = _trusted_text(raw_tree.get("role"), 256)
            if root_role.casefold() == "axwindow":
                snapshot_target["window_role"] = root_role
                observed_title = _trusted_text(
                    raw_tree.get("title") or raw_tree.get("label"),
                    1_000,
                )
                if (
                    observed_title
                    and trusted_target.get("bundle_id") == "com.kingsoft.wpsoffice.mac"
                    and observed_title.strip().casefold() in _WPS_DESTINATION_MODAL_TITLES
                ):
                    # The selected catalog window can host a contained WPS
                    # sheet or dialog.  Bind policy and approval copy to the
                    # helper-observed AXWindow that actions actually target.
                    snapshot_target["window_title"] = observed_title
        trusted_snapshot.clear()
        trusted_snapshot.update({
            **snapshot_target,
            "session_id": manager.session_id,
            "snapshot_id": snapshot.snapshot_id,
            "elements": elements,
            "focused_element_ref": focused_element_ref,
            "focused_role": focused_role,
            "known_path": known_path,
        })

    def current_policy_context(snapshot_id: str = "") -> dict[str, Any]:
        if snapshot_id and trusted_snapshot.get("snapshot_id") != snapshot_id:
            return {
                "metadata_trusted": False,
                "session_id": manager.session_id,
                "window_ref": getattr(manager.target, "window_ref", ""),
                "snapshot_id": snapshot_id,
                "_scope_secret": approval_scope_secret,
            }
        return {
            **(trusted_snapshot or trusted_target),
            "session_id": manager.session_id,
            "_scope_secret": approval_scope_secret,
        }

    def app_state_policy_context(app_ref: str, window_ref: str) -> dict[str, Any]:
        selected = trusted_catalog.get(app_ref)
        window = selected.get("windows", {}).get(window_ref) if selected is not None else None
        if not isinstance(selected, Mapping) or not isinstance(window, Mapping):
            return {
                "metadata_trusted": False,
                "session_id": manager.session_id,
                "app_ref": app_ref,
                "window_ref": window_ref,
                "_scope_secret": approval_scope_secret,
            }
        return {
            "metadata_trusted": True,
            "session_id": manager.session_id,
            "app_ref": app_ref,
            "window_ref": window_ref,
            "application": selected.get("application", ""),
            "bundle_id": selected.get("bundle_id", ""),
            "app_version": selected.get("app_version", ""),
            **window,
            "_scope_secret": approval_scope_secret,
        }

    def current_target_binding() -> str:
        binding = manager.snapshot_target_binding
        if binding is None:
            return ""
        return (
            f"{binding.session_id}:{binding.app_ref}:{binding.window_ref}:"
            f"{binding.generation}"
        )

    def preflight() -> ToolFailure | None:
        capabilities = _capabilities_value(model_capabilities)
        missing = sorted({"tools", "vision"} - capabilities)
        if missing:
            return _failure(
                "computer_capability_unavailable",
                "Computer Use requires a model with both tool calling and image input.",
                details={"missing_capabilities": missing},
            )
        if sys.platform != "darwin":
            return _failure(
                ComputerErrorCode.UNSUPPORTED_PLATFORM.value,
                "Computer Use is currently available only on macOS.",
            )
        return None

    async def snapshot_result(
        snapshot: ComputerSnapshot,
        *,
        scope: str,
        action_result: Mapping[str, object] | None = None,
        action_observation: Mapping[str, str] | None = None,
        observed_window_transition: Mapping[str, object] | None = None,
        verified_state: ComputerVerifiedAppState | None = None,
        subtree_ref: str | None = None,
        prior_ax_tree: Any = None,
        role_filter: str | None = None,
        resume_publication_id: str | None = None,
        before_encoding: Callable[[], Awaitable[None]] | None = None,
        choice_verification: Mapping[str, Any] | None = None,
    ) -> ToolPrivateResult:
        nonlocal published_image_geometry
        try:
            payload, png, identity, digest = _public_snapshot_payload(
                manager,
                snapshot,
                scope=scope,
                action_result=action_result,
                verified_state=verified_state,
                subtree_ref=subtree_ref,
                prior_ax_tree=prior_ax_tree,
                role_filter=role_filter,
            )
        except ComputerSessionError as exc:
            if exc.code != "unsafe_artifact":
                raise
            if verified_state is not None:
                raise
            # Return only bounded identifiers so the registry's independent
            # postcondition produces the authoritative failure. No image bytes
            # cross the private side channel when verification failed.
            artifact = snapshot.payload.get("image_artifact")
            payload = {
                "success": True,
                "type": "image_attachment",
                "detail": "original",
                "image_paths": [],
                "image_artifact": artifact,
                "session_id": manager.session_id,
                "snapshot_id": snapshot.snapshot_id,
                "scope": scope,
            }
            return ToolPrivateResult(output=_json(payload), private={})
        binding = manager.snapshot_target_binding
        if action_observation is not None:
            payload["action_observation"] = dict(action_observation)
        if choice_verification is not None:
            payload["choice_verification"] = dict(choice_verification)
            payload["message"] = "Inspect choice_verification: verified means every requested choice still matches in this fresh observation. Do not repeat completed choices."
        if observed_window_transition is not None:
            payload["window_transition"] = dict(observed_window_transition)
        authority_generation = observation_authority_generation
        if before_encoding is not None:
            await before_encoding()
        data_url, screenshot_encoding = await _encode_screenshot_async(png)
        manager.validate_snapshot_publication(snapshot, binding, resume_publication_id)
        if authority_generation != observation_authority_generation:
            raise ComputerSessionError("stale_snapshot")
        artifact = payload["image_artifact"]
        assert isinstance(artifact, str)
        detail_artifact = snapshot.payload.get("text_detail_artifact")
        detail_metadata = snapshot.payload.get("text_detail_metadata")
        if isinstance(detail_artifact, str) and isinstance(detail_metadata, Mapping):
            try:
                if verified_state is None:
                    detail_path, _image_data, detail_data, detail_digest, _authorities = (
                        _read_verified_snapshot_pair(manager, artifact, detail_artifact)
                    )
                else:
                    detail_path = verified_state.detail_path
                    detail_data = verified_state.detail_data
                    detail_digest = verified_state.detail_sha256
                    if (
                        detail_path is None
                        or detail_data is None
                        or detail_digest is None
                    ):
                        raise ComputerSessionError(
                            "unsafe_artifact",
                            "unsafe_artifact: verified app state omitted detail bytes",
                        )
                detail = decode_text_detail_envelope(
                    detail_data,
                    snapshot_id=snapshot.snapshot_id,
                    metadata=detail_metadata,
                    sha256=detail_digest,
                )
            except (TypeError, ValueError) as exc:
                raise ComputerSessionError(
                    "unsafe_artifact",
                    f"unsafe_artifact: AX detail {exc}",
                ) from exc
            payload["text_detail_path"] = str(detail_path)
            payload["text_detail_metadata"] = _bounded_public_value(detail_metadata)
            payload["text_summary"] = _text_detail_summary(detail["root"])
            verified_detail_artifacts[artifact] = (
                detail_artifact,
                dict(detail_metadata),
            )
        remember_snapshot(snapshot)
        verified_artifacts[artifact] = (identity, digest)
        if isinstance(snapshot.payload.get("ax_tree"), Mapping):
            _LAST_PUBLISHED_AX_TREES[str(manager.session_id or "")] = snapshot.payload["ax_tree"]
        payload["screenshot_encoding"] = screenshot_encoding
        if scope == "target_window":
            pixels, logical = payload["pixel_size"], payload["logical_size"]
            scale = screenshot_encoding["scale"]
            geometry = (max(1, round(pixels["width"] * scale)), max(1, round(pixels["height"] * scale)), logical["width"], logical["height"])
            published_image_geometry = (snapshot.snapshot_id, geometry)
            payload["published_image_size"] = {"width": geometry[0], "height": geometry[1]}
        else:
            published_image_geometry = ("", ())
        return ToolPrivateResult(
            output=_json(payload),
            private={"image_data_urls": [data_url], "detail": "original"},
        )

    def resume_recovery() -> dict[str, Any]:
        """Opaque suspended refs survive catalog refresh; never authorize input."""
        target = manager.suspended_target
        if target is None:
            # A targetless stop resumes into an unbound session; there are no refs to reuse.
            return {"next_observation": {"tool": "computer_resume", "arguments": {}}} if manager.handed_off else {}
        return {"next_observation": {
            "tool": "computer_resume",
            "arguments": {"app_ref": target.app_ref, "window_ref": target.window_ref},
        }}

    async def computer_status():
        blocked = preflight()
        if blocked:
            return blocked
        try:
            backend = await manager.status()
        except HelperApplicationError as exc:
            return _application_failure(exc.error)
        except HelperTransportError as exc:
            logger.warning("computer status helper failed error_type=%s", type(exc).__name__)
            return _failure("helper_failed", "Unable to query the native Computer Use helper.", retryable=True)
        except ComputerSessionError as exc:
            return _session_failure(exc)
        except Exception as exc:  # noqa: BLE001 - backend failures cross a sanitized tool boundary
            logger.warning("computer status helper failed error_type=%s", type(exc).__name__)
            return _failure("helper_failed", "Unable to query the native Computer Use helper.", retryable=True)
        target = manager.target
        return _json({
            **_bounded_public_value(backend),
            "session_id": manager.session_id,
            "target": (
                {"app_ref": target.app_ref, "window_ref": target.window_ref}
                if target is not None else None
            ),
            "handoff_active": manager.handed_off,
            **(resume_recovery() if manager.handed_off else {}),
        })

    async def _computer_apps():
        nonlocal pending_wps_modal_target
        blocked = preflight()
        if blocked:
            return blocked
        try:
            apps = await manager.apps()
            clear_write_grants(preserve_foreground_session=True)
            clear_snapshot_grants()
            if (
                trusted_target.get("bundle_id") == "com.kingsoft.wpsoffice.mac"
                and (
                    str(trusted_snapshot.get("window_title") or "").strip().casefold()
                    in _WPS_DESTINATION_MODAL_TITLES
                    or (
                        trusted_snapshot.get("focused_role") == "AXTextField"
                        and bool(trusted_wps_directory)
                    )
                )
            ):
                pending_wps_modal_target = (
                    str(trusted_target.get("bundle_id") or ""),
                    str(trusted_target.get("window_title") or "").strip().casefold(),
                    trusted_wps_document_title,
                )
            else:
                pending_wps_modal_target = None
            trusted_target.clear()
            trusted_snapshot.clear()
            remember_catalog(apps)
            public_apps = [
                {**app, "routing_advice": computer_route_advice(
                    str(app.get("bundle_id") or ""), str(app.get("app_version") or ""),
                )}
                for app in apps[:100]
            ]
            return _json({"apps": _bounded_public_value(public_apps),
                          "binding_note": "bindable/ready means catalog identity matched; binding also checks current overlays and window state. It is not a guarantee that the window is unobstructed."})
        except HelperApplicationError as exc:
            return _application_failure(exc.error)
        except HelperTransportError as exc:
            logger.warning("computer app enumeration failed error_type=%s", type(exc).__name__)
            return _failure("helper_failed", "Unable to enumerate macOS applications.", retryable=True)
        except ComputerSessionError as exc:
            return _session_failure(exc)
        except Exception as exc:  # noqa: BLE001 - backend failures cross a sanitized tool boundary
            logger.warning("computer app enumeration failed error_type=%s", type(exc).__name__)
            return _failure("helper_failed", "Unable to enumerate macOS applications.", retryable=True)

    async def computer_apps():
        async with catalog_target_operation_lock:
            return await _computer_apps()

    async def computer_get_app_state(
        app_ref: str,
        window_ref: str,
        scope: str = "target_window",
        text_detail: str = "off",
    ):
        blocked = preflight()
        if blocked:
            return blocked
        if manager.handed_off:
            return _failure(
                "handoff_active",
                "User handoff is active; after the user yields control, use the supplied computer_resume arguments.",
                retryable=True, details=resume_recovery(),
            )
        detail_mode = ComputerSnapshotTextDetailMode(text_detail)
        selected = trusted_catalog.get(app_ref)
        exact_window = (
            selected.get("windows", {}).get(window_ref)
            if isinstance(selected, Mapping)
            else None
        )
        request_catalog_generation = manager.catalog_generation
        request_authority_generation = observation_authority_generation
        approval_scope = ""
        if isinstance(exact_window, Mapping) and request_catalog_generation > 0:
            approval_scope = app_state_approval_scope(
                app_state_policy_context(app_ref, window_ref),
                catalog_generation=request_catalog_generation,
                app_ref=app_ref,
                window_ref=window_ref,
                capture_scope=scope,
                text_detail_mode=detail_mode.value,
            )
        if (
            request_catalog_generation <= 0
            or request_catalog_generation != trusted_catalog_generation
        ):
            return _failure(
                "catalog_required",
                "The application catalog is no longer current for this Computer Use session.",
                retryable=True,
                recovery_hint=(
                    "Call computer_apps, then choose the exact app_ref and window_ref from "
                    "its fresh catalog before calling computer_get_app_state."
                ),
            )
        clear_write_grants(preserve_foreground_session=True)
        approvals.approved_scopes.difference_update({
            approved
            for approved in approvals.approved_scopes
            if approved.startswith((
                "computer-observe-snapshot:",
                "computer-observe-app-state:",
            ))
            and approved != approval_scope
        })
        session_observe_granted = "observe-app-state" in manager.grants
        if (
            approval_scope
            and not session_observe_granted
            and approval_scope not in approvals.approved_scopes
        ):
            return _failure(
                "approval_required",
                "This app state observation requires explicit temporary approval.",
                retryable=True,
            )
        if approval_scope:
            if not session_observe_granted and not registry.yolo:
                # Session-level observe grant (Plan B): one explicit approval
                # covers this session's app-state observations until a
                # handoff/session event clears the grants.
                manager.grants.add("observe-app-state")
            # Consume the exact one-shot scope only; the session grant above
            # keeps follow-up observations approval-free until cleared.
            approvals.approved_scopes.discard(approval_scope)
        await catalog_target_operation_lock.acquire()
        try:
            if approval_scope and (
                request_catalog_generation != trusted_catalog_generation
                or request_authority_generation != observation_authority_generation
            ):
                return _failure(
                    "session_state_changed",
                    "The approved Computer Use catalog or target changed before dispatch.",
                    retryable=True,
                )
            state = await manager.get_app_state(
                app_ref,
                window_ref,
                scope,
                text_detail=detail_mode,
            )
            if approval_scope and (
                request_catalog_generation != trusted_catalog_generation
                or request_authority_generation != observation_authority_generation
            ):
                trusted_target.clear()
                trusted_snapshot.clear()
                clear_write_grants()
                return _failure(
                    "session_state_changed",
                    "The approved Computer Use catalog or target changed before publication.",
                    retryable=True,
                )
            remember_target(state.target.app_ref, state.target.window_ref)
            form_region = truncated_form_region(state.snapshot.payload.get("ax_tree"))
            if scope == "target_window" and detail_mode is ComputerSnapshotTextDetailMode.OFF and form_region:
                # Same authorized window, one anchored native read. Never loop,
                # rebind by title, or expand this into another observation scope.
                focused = await manager.snapshot(scope, subtree_ref=form_region)
                return await snapshot_result(focused, scope=scope)
            return await snapshot_result(
                state.snapshot,
                scope=scope,
                verified_state=state,
            )
        except HelperApplicationError as exc:
            return _app_state_identity_failure(exc.error, target_bound=manager.target is not None)
        except HelperTransportError as exc:
            logger.warning("computer app state helper failed error_type=%s", type(exc).__name__)
            return _failure(
                "helper_failed",
                "Unable to capture the exact macOS application state.",
                retryable=True,
            )
        except ComputerSessionError as exc:
            if exc.code == "unsafe_artifact":
                with suppress(Exception):
                    manager.poison_artifact_session(clear_target=True)
                trusted_target.clear()
                trusted_snapshot.clear()
                clear_write_grants()
                return _failure(
                    "unsafe_artifact",
                    "unsafe_artifact: verified app state publication could not be trusted",
                    retryable=False,
                    recovery_hint=(
                        "Call computer_close, then start a fresh computer_apps -> "
                        "computer_get_app_state sequence."
                    ),
                )
            return _session_failure(exc)
        except Exception as exc:  # noqa: BLE001 - backend failures cross a sanitized tool boundary
            logger.warning("computer app state failed error_type=%s", type(exc).__name__)
            return _failure(
                "helper_failed",
                "Unable to capture the exact macOS application state.",
                retryable=True,
            )
        finally:
            catalog_target_operation_lock.release()

    async def _computer_focus(app_ref: str, window_ref: str):
        blocked = preflight()
        if blocked:
            return blocked
        if manager.handed_off:
            return _failure(
                "handoff_active",
                "User handoff is active; after the user yields control, use the supplied computer_resume arguments.",
                retryable=True, details=resume_recovery(),
            )
        try:
            target = await manager.select(app_ref, window_ref)
            remember_target(target.app_ref, target.window_ref)
            return _json({
                "success": True,
                "session_id": manager.session_id,
                "app_ref": target.app_ref,
                "window_ref": target.window_ref,
                "handoff_active": False,
            })
        except HelperApplicationError as exc:
            return _application_failure(exc.error)
        except HelperTransportError as exc:
            logger.warning("computer selection helper failed error_type=%s", type(exc).__name__)
            return _failure("helper_failed", "Unable to select the macOS target.", retryable=True)
        except ComputerSessionError as exc:
            return _session_failure(exc)
        except Exception as exc:  # noqa: BLE001 - backend failures cross a sanitized tool boundary
            logger.warning("computer selection failed error_type=%s", type(exc).__name__)
            return _failure("helper_failed", "Unable to select the macOS target.", retryable=True)

    async def computer_focus(app_ref: str, window_ref: str):
        async with catalog_target_operation_lock:
            return await _computer_focus(app_ref, window_ref)

    async def computer_snapshot(
        scope: str = "target_window",
        text_detail: str = "off",
        subtree_ref: str = "",
        role_filter: str = "",
        settle_ms: int = 0,
    ):
        blocked = preflight()
        if blocked:
            return blocked
        if type(settle_ms) is not int or not 0 <= settle_ms <= 2000:
            return _failure("invalid_arguments", "settle_ms must be an integer from 0 to 2000.", retryable=False)
        if manager.handed_off:
            return _failure("handoff_active", "User controls the target; resume only after the user yields control.",
                            retryable=True, details=resume_recovery())
        detail_mode = ComputerSnapshotTextDetailMode(text_detail)
        # 锚点身份必须在**本次抓取之前**取：抓取会换掉上一次公开的投影。
        previous_ax_tree = _LAST_PUBLISHED_AX_TREES.get(str(manager.session_id or ""))
        request_target_binding = manager.snapshot_target_binding
        target_binding = (
            ""
            if request_target_binding is None
            else (
                f"{request_target_binding.session_id}:"
                f"{request_target_binding.app_ref}:"
                f"{request_target_binding.window_ref}:"
                f"{request_target_binding.generation}"
            )
        )
        approval_scope = snapshot_approval_scope(
            current_policy_context(),
            capture_scope=scope,
            text_detail_mode=detail_mode.value,
            target_binding=target_binding,
        )
        expected_target_binding = request_target_binding if approval_scope or settle_ms else None
        if approval_scope and approval_scope not in approvals.approved_scopes:
            return _failure(
                "approval_required",
                "This snapshot observation requires explicit temporary approval.",
                retryable=True,
            )
        if approval_scope:
            # Consume before the first await. A concurrent snapshot cannot reuse
            # the exact once-only observation grant while capture is in flight.
            approvals.approved_scopes.discard(approval_scope)
        try:
            if settle_ms:
                await asyncio.sleep(settle_ms / 1000)
            snapshot = await manager.snapshot(
                scope,
                text_detail=detail_mode,
                expected_target_binding=expected_target_binding,
                **({"subtree_ref": subtree_ref} if subtree_ref and "subtree_v1" in observation_capabilities(previous_ax_tree) else {}),
            )
            if (
                expected_target_binding is not None
                and manager.snapshot_target_binding != expected_target_binding
            ):
                raise ComputerSessionError(
                    "snapshot_target_changed",
                    "snapshot_target_changed: approved Computer Use target changed",
                )
            return await snapshot_result(
                snapshot,
                scope=scope,
                subtree_ref=subtree_ref or None,
                prior_ax_tree=previous_ax_tree,
                role_filter=role_filter or None,
            )
        except HelperApplicationError as exc:
            return _application_failure(exc.error)
        except HelperTransportError as exc:
            logger.warning("computer snapshot helper failed error_type=%s", type(exc).__name__)
            return _failure("helper_failed", "Unable to capture the current macOS target.", retryable=True)
        except ComputerSessionError as exc:
            return _session_failure(exc)
        except Exception as exc:  # noqa: BLE001 - backend failures cross a sanitized tool boundary
            logger.warning("computer snapshot failed error_type=%s", type(exc).__name__)
            return _failure("helper_failed", "Unable to capture the current macOS target.", retryable=True)

    def _runtime_takeover_event(
        event_type: str,
        decision: ComputerPolicyDecision,
    ) -> None:
        registry.hooks.dispatch_runtime_event({
            "type": event_type,
            "application": decision.application,
            "action_classes": list(decision.action_classes),
        })

    def _planning_failure(exc: BaseException, *, requested_takeover: bool = False) -> ToolFailure:
        # manifest 只管 background 直投许可；background 不可行 ≠ 必须用户手点，而是可升级为
        # 显式授权的 foreground takeover（独立授权路径，不该被 manifest 卡住）。但已经请求接管
        # 时不许再建议切模式 —— 见 _plan_rejection_failure。
        rejection = _plan_rejection_failure(exc, requested_takeover=requested_takeover)
        if rejection is not None:
            return rejection
        if isinstance(exc, HelperApplicationError):
            return _application_failure(exc.error)
        if isinstance(exc, ComputerSessionError):
            return _session_failure(exc)
        if isinstance(exc, (HelperTransportError, OSError)):
            return _failure(
                ComputerErrorCode.HELPER_FAILED.value,
                _SAFE_ACTION_ERRORS[ComputerErrorCode.HELPER_FAILED],
                retryable=True,
            )
        logger.warning("computer action planning failed error_type=%s", type(exc).__name__)
        return _failure(
            ComputerErrorCode.HELPER_FAILED.value,
            _SAFE_ACTION_ERRORS[ComputerErrorCode.HELPER_FAILED],
            retryable=True,
        )

    async def computer_act(
        snapshot_id: str,
        actions: list[dict[str, Any]],
        interaction_mode: str = "auto",
        coordinate_space: str = "window_logical",
        opens_dialog: bool = False,
        *,
        _permission_call_id: str = "",
    ):
        nonlocal pending_foreground_digest, pending_foreground_snapshot
        blocked = preflight()
        if blocked:
            return blocked
        prepared = prepared_acts.get(_permission_call_id)
        if prepared is None:
            return _failure(
                ComputerErrorCode.STALE_SNAPSHOT.value,
                "The exact Computer Use action preparation is unavailable.",
                retryable=True,
                recovery_hint="Capture a fresh snapshot and retry the background batch.",
            )
        act_receipt_states[_permission_call_id] = prepared
        if prepared.failure is not None:
            return prepared.failure
        normalized: list[ComputerAction | Mapping[str, object]] = list(prepared.actions)
        before_tree = effect_snapshot[1] if effect_snapshot[0] == snapshot_id else None
        before_target = dict(trusted_target)
        before_windows = trusted_catalog.get(str(before_target.get("app_ref") or ""), {}).get("windows", {})
        before_identity = before_windows.get(before_target.get("window_ref"), {}).get("window_identity_ref", "")
        before_identities = {w.get("window_identity_ref") for w in before_windows.values() if w.get("window_identity_ref")}
        before_authority = observation_authority_generation
        before_binding = manager.snapshot_target_binding
        if manager.handed_off:
            return _failure(
                "handoff_active",
                "handoff_active: user controls the target; call computer_resume before generating input.",
                retryable=True,
            )
        decision = prepared.decision
        plan = prepared.plan
        if decision is None or plan is None:
            return _failure(
                ComputerErrorCode.STALE_SNAPSHOT.value,
                "The exact Computer Use action preparation is incomplete.",
                retryable=True,
            )
        if decision.handoff_required:
            return _failure(
                "computer_handoff_required",
                "This protected authentication, secret, or payment step requires user handoff.",
                retryable=False,
                recovery_hint="Call computer_handoff and ask the user to complete this step.",
            )
        if prepared.mode is ComputerInteractionMode.FOREGROUND_TAKEOVER and not prepared.approved:
            return _failure(
                "approval_required",
                "Foreground Computer Use requires an exact grant or an approved application scope.",
                retryable=True,
            )
        if prepared.mode is ComputerInteractionMode.BACKGROUND and plan.requires_takeover:
            pending_foreground_digest = decision.batch_hash
            pending_foreground_snapshot = str(prepared.snapshot_id)
            # requires_active 的 app(见 docs/macos-computer-use.md#input-delivery-contracts):
            # 计划阶段已把 pointer 动作路由到激活路径, 这里给出明确提示,
            # 与普通 foreground takeover 的通用文案区分开。
            activation_required = plan.reason == "requires_active_foreground_takeover"
            return _failure(
                ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED.value,
                (
                    "The application requires activation (foreground) to process "
                    "synthesized pointer events; use interaction_mode="
                    "'foreground_takeover'."
                    if activation_required
                    else _SAFE_ACTION_ERRORS[ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED]
                ),
                retryable=True,
                recovery_hint=(
                    "Retry this exact snapshot and action batch with "
                    "interaction_mode='foreground_takeover'."
                ),
                details={
                    "interaction_mode": ComputerInteractionMode.FOREGROUND_TAKEOVER.value,
                    "action_classes": list(decision.action_classes),
                    **({"plan_ref": plan.plan_ref} if not observation_capabilities(before_tree) else {}),
                    "next_tool": "computer_act",
                    "do_not_call": "computer_begin_takeover",
                    **({"reason": plan.reason} if activation_required else {}),
                },
            )
        takeover_ref: str | None = None
        recovery_catalog_attempted = False
        input_succeeded = False

        async def finish_takeover() -> None:
            nonlocal takeover_ref
            if takeover_ref is not None:
                ending_ref = takeover_ref
                takeover_ref = None
                restoration = "restore_failed"
                try:
                    if input_succeeded and not prepared.restore_previous_focus:
                        outcome = await manager.end_takeover(ending_ref, restore_previous_focus=False)
                    else:
                        outcome = await manager.end_takeover(ending_ref)
                    raw_restoration = str(outcome.get("restoration") or "")
                    restoration = {
                        "restored": "focus_restored",
                        "preserved_user_focus": "focus_preserved",
                    }.get(raw_restoration, "restore_failed")
                except Exception as exc:  # noqa: BLE001 - cleanup failure remains an audit event
                    logger.warning(
                        "computer takeover cleanup failed error_type=%s",
                        type(exc).__name__,
                    )
                _runtime_takeover_event(
                    f"foreground_takeover_{restoration}",
                    decision,
                )
                _runtime_takeover_event("foreground_takeover_end", decision)

        async def recover_window_transition(deadline: float, *, action_result=None):
            nonlocal recovery_catalog_attempted
            if not before_identity:
                return None
            try:
                async with asyncio.timeout_at(deadline):
                    await finish_takeover()
                    async with catalog_target_operation_lock:
                        if before_authority != observation_authority_generation:
                            return None
                        may_reobserve = (
                            before_binding is not None
                            and manager.snapshot_target_binding == before_binding
                            and _fully_acknowledged_action_batch(action_result, len(normalized))
                        )
                        # Catalog refresh consumes old authority. Suggested refs do
                        # not grant observation or replay an uncertain input.
                        recovered = None
                        for attempt in range(2):
                            if manager.handed_off:
                                return None
                            recovery_catalog_attempted = True
                            catalog_result = await _computer_apps()
                            if not isinstance(catalog_result, str):
                                return None
                            recovered = window_transition(
                                json.loads(catalog_result).get("apps", []),
                                before_target.get("bundle_id"), before_identity, before_identities,
                            )
                            # A closing modal can outlive its AX object briefly.
                            # Wait once for catalog matching, never repeat input.
                            unmatched = any(w.get("bindable") is False for w in recovered.get("windows", []))
                            if not unmatched or attempt == 1:
                                break
                            await asyncio.sleep(0.1)
                        if recovered is None:
                            return None
                        if not may_reobserve or recovered.get("observation_reason") != "exact_previous_target":
                            return recovered
                        arguments = recovered["next_observation"]["arguments"]
                        catalog_generation = manager.catalog_generation
                        authority_generation = observation_authority_generation
                        try:
                            state = await manager.get_app_state(
                                arguments["app_ref"], arguments["window_ref"],
                                recovery_catalog_generation=catalog_generation,
                                recovery_window_identity_ref=before_identity,
                            )
                            if authority_generation != observation_authority_generation:
                                return None
                            remember_target(state.target.app_ref, state.target.window_ref)
                            observed = {k: v for k, v in recovered.items() if k != "next_observation"}
                            observed["observation_status"] = "observed"
                            return await snapshot_result(
                                state.snapshot, scope="target_window", verified_state=state,
                                action_result=action_result,
                                action_observation=observe_action_effect(
                                    before_tree, state.snapshot.payload.get("ax_tree"), normalized,
                                ),
                                observed_window_transition=observed,
                            )
                        except Exception as exc:  # noqa: BLE001 - recovery never replays input
                            logger.debug("same-window observation unavailable error_type=%s", type(exc).__name__)
                            if (
                                manager.catalog_generation != catalog_generation
                                or observation_authority_generation != authority_generation
                                or manager.handed_off
                                or manager.target is not None
                            ):
                                return None
                            return recovered
            except Exception as exc:  # noqa: BLE001 - observations cannot upgrade unknown input
                logger.debug("post-action catalog recovery unavailable error_type=%s", type(exc).__name__)
            return None

        def recovery_hint(recovery):
            if manager.handed_off:
                return (
                    "Do not repeat this action batch. After the user yields control, "
                    "read computer_status and follow its current computer_resume recovery; "
                    "do not enumerate replacement refs while handoff is active."
                )
            if recovery is not None and recovery.get("status") == "unmatched_window_observed":
                return (
                    "Do not repeat this action batch. An unmatched window may obscure the target. "
                    "Do not bind its parent or repeatedly refresh the unchanged catalog. "
                    "Wait for the panel state to change or hand this step to the user; "
                    "then obtain fresh bindable refs through normal observation approval."
                )
            if recovery is not None:
                return (
                    "Do not repeat this action batch. Use the fresh window_transition refs "
                    "with computer_get_app_state through normal observation approval."
                )
            # A concurrent operation can revoke or replace the original target
            # before this call gets to attempt its own catalog recovery.
            if (
                recovery_catalog_attempted
                or manager.target is None
                or manager.snapshot_target_binding != before_binding
            ):
                return (
                    "Do not repeat this action batch. Old refs were revoked. Call computer_apps, "
                    "then computer_get_app_state with fresh refs through normal observation approval."
                )
            return "Do not repeat this action batch. Capture a fresh snapshot and inspect the current state."

        async def report_action_failure(error, receipt):
            failure = _action_failure(error, receipt)
            if manager.handed_off:
                return replace(failure, details={**failure.details, **resume_recovery()},
                               recovery_hint="Do not repeat this action batch. After the user yields control, use the supplied computer_resume arguments; do not enumerate replacement refs.")
            if error.code is ComputerErrorCode.UNKNOWN_OUTCOME:
                recovery = await recover_window_transition(
                    asyncio.get_running_loop().time() + _POST_ACTION_OBSERVATION_TIMEOUT_SECONDS
                )
                if recovery is not None or recovery_catalog_attempted:
                    return replace(
                        failure,
                        details={**failure.details, **({"window_transition": recovery} if recovery is not None else {})},
                        recovery_hint=recovery_hint(recovery),
                    )
            return failure

        try:
            if prepared.mode is ComputerInteractionMode.FOREGROUND_TAKEOVER:
                current = classify_computer_batch(
                    prepared.policy_context
                    or current_policy_context(snapshot_id),
                    prepared.raw_actions,
                    interaction_plan=replace(
                        plan,
                        interaction_mode=ComputerInteractionMode.BACKGROUND,
                    ),
                )
                basis = prepared.hash_basis or decision
                if basis is None or current.batch_hash != basis.batch_hash:
                    logger.warning(
                        "computer_act takeover batch_hash mismatch decision=%s current=%s "
                        "decision_intent=%s decision_ctx_keys=%s current_ctx_keys=%s "
                        "actions_type=%s raw_type=%s snapshot_id=%s",
                        basis.batch_hash if basis else "none",
                        current.batch_hash,
                        getattr(decision, "intent", "?"),
                        sorted((prepared.policy_context or {}).keys()),
                        sorted(current_policy_context(snapshot_id).keys()),
                        type(actions).__name__,
                        type(prepared.raw_actions).__name__,
                        snapshot_id,
                    )
                    return _failure(
                        ComputerErrorCode.STALE_SNAPSHOT.value,
                        _SAFE_ACTION_ERRORS[ComputerErrorCode.STALE_SNAPSHOT],
                        retryable=True,
                    )
                enabled = enabled_pid_actions(
                    str(current_policy_context(snapshot_id).get("bundle_id") or ""),
                    str(current_policy_context(snapshot_id).get("app_version") or ""),
                )
                if not set(plan.pid_action_classes).issubset(enabled):
                    return _failure(
                        ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED.value,
                        "This foreground action is not enabled for the exact application version.",
                        retryable=False,
                    )
                takeover_ref = await manager.begin_takeover(snapshot_id, plan.plan_ref)
                _runtime_takeover_event("foreground_takeover_begin", decision)
            prepared.dispatch_attempted = True
            result = await manager.act(
                snapshot_id,
                normalized,
                interaction_mode=prepared.mode,
                plan_ref=plan.plan_ref,
                takeover_ref=takeover_ref,
            )
            prepared.dispatch_result = _safe_action_metadata(result.result)
        except HelperApplicationError as exc:
            prepared.dispatch_result = _safe_action_metadata(exc.result)
            if exc.error.code is ComputerErrorCode.USER_ACTIVITY_PAUSED:
                _runtime_takeover_event(
                    "foreground_takeover_user_activity_paused",
                    decision,
                )
                with suppress(ComputerSessionError):
                    await manager.pause_for_user_activity()
                clear_computer_grants()
                trusted_snapshot.clear()
            return await report_action_failure(exc.error, exc.result)
        except HelperTransportError as exc:
            logger.warning("computer action helper transport failed error_type=%s", type(exc).__name__)
            return _failure(
                ComputerErrorCode.HELPER_FAILED.value,
                _SAFE_ACTION_ERRORS[ComputerErrorCode.HELPER_FAILED],
                retryable=True,
            )
        except ComputerSessionError as exc:
            return _session_failure(exc)
        except Exception as exc:  # noqa: BLE001 - dispatch started, so outcome is conservatively unknown
            logger.warning("computer action helper failed error_type=%s", type(exc).__name__)
            return _failure(
                ComputerErrorCode.UNKNOWN_OUTCOME.value,
                _SAFE_ACTION_ERRORS[ComputerErrorCode.UNKNOWN_OUTCOME],
                retryable=False,
                recovery_hint=(
                    "Do not repeat this action batch. Capture a fresh snapshot and inspect the current state."
                ),
            )
        else:
            checkpoint = result.error is not None and result.error.code is ComputerErrorCode.OBSERVATION_REQUIRED
            if not result.ok and not checkpoint:
                assert result.error is not None
                if result.error.code is ComputerErrorCode.USER_ACTIVITY_PAUSED:
                    _runtime_takeover_event(
                        "foreground_takeover_user_activity_paused",
                        decision,
                    )
                    clear_computer_grants()
                    trusted_snapshot.clear()
                return await report_action_failure(result.error, result.result)
            input_succeeded = True
            observed_actions = normalized
            if checkpoint:
                # The protocol decoder requires an acknowledged successful prefix.
                assert result.result is not None
                acknowledged = result.result["last_acknowledged_action"]
                assert isinstance(acknowledged, int) and not isinstance(acknowledged, bool)
                observed_actions = normalized[:acknowledged + 1]
            observation_error: Exception | None = None
            pending_clicks.remember(before_binding, before_tree, normalized, result.result)
            observation_deadline = asyncio.get_running_loop().time() + _POST_ACTION_OBSERVATION_TIMEOUT_SECONDS
            for attempt in range(3):
                if asyncio.get_running_loop().time() >= observation_deadline:
                    observation_error = TimeoutError()
                    break
                try:
                    async with asyncio.timeout_at(observation_deadline):
                        fresh = await manager.snapshot("target_window", expected_target_binding=before_binding)
                        form_region = truncated_form_region(fresh.payload.get("ax_tree")) if any(
                            getattr(action, "checked", None) is not None for action in observed_actions
                        ) else None
                        if form_region:
                            fresh = await manager.snapshot("target_window", expected_target_binding=before_binding,
                                subtree_ref=form_region)
                        return await snapshot_result(
                            fresh, scope="target_window", action_result=result.result,
                            action_observation=observe_action_effect(before_tree, fresh.payload.get("ax_tree"), observed_actions),
                            before_encoding=finish_takeover,
                            choice_verification=verify_choice_goals(before_tree, fresh.payload.get("ax_tree"), observed_actions),
                        )
                except Exception as exc:  # noqa: BLE001 - observation-only retry is safe after acknowledged input
                    observation_error = exc
                    observation_code = (
                        exc.error.code.value if isinstance(exc, HelperApplicationError)
                        else exc.code if isinstance(exc, ComputerSessionError) else None
                    )
                    if observation_code not in {"helper_failed", "snapshot_failed", "observation_timeout"}:
                        break
                    if attempt < 2:
                        remaining = observation_deadline - asyncio.get_running_loop().time()
                        await asyncio.sleep(min(0.05, max(0, remaining)))
            assert observation_error is not None
            logger.warning(
                "post-action computer snapshot failed error_type=%s",
                type(observation_error).__name__,
            )
            observation_code = _post_observation_error_code(observation_error)
            # A checkpoint's validated prefix is the complete observed batch,
            # not proof that its unexecuted suffix was delivered. Missing or
            # malformed receipts must remain genuinely unknown.
            acknowledged = _fully_acknowledged_action_batch(result.result, len(observed_actions))
            failure_code = (
                "post_action_observation_pending" if acknowledged
                else ComputerErrorCode.UNKNOWN_OUTCOME.value
            )
            cause = {"observation_error_code": observation_code} if observation_code else {}
            if observation_code in {
                ComputerErrorCode.WINDOW_CONTENT_UNAVAILABLE.value,
                ComputerErrorCode.OVERLAY_BLOCKED.value,
            }:
                unavailable = _application_failure(ComputerError(
                    ComputerErrorCode(observation_code), "",
                ))
                return _failure(
                    failure_code,
                    ("Input dispatch is acknowledged. " if acknowledged else "") + unavailable.message,
                    retryable=False,
                    recovery_hint="Do not repeat this action batch. " + (unavailable.recovery_hint or ""),
                    details={
                        "observation_status": "post_action_observation_pending",
                        **cause,
                        "action_result": _model_action_metadata(result.result),
                    },
                )
            recovery = (
                await recover_window_transition(observation_deadline, action_result=result.result)
                if observation_code in {"target_gone", "stale_target"} else None
            )
            if isinstance(recovery, ToolPrivateResult):
                return recovery
            return _failure(
                failure_code,
                ("Input dispatch is acknowledged. " if acknowledged else "")
                + "The post-action window state was not observed; the effect remains unverified. "
                + ("Only the acknowledged prefix was delivered; the remaining actions were not dispatched. "
                   if acknowledged and checkpoint else "")
                + "Check the action receipt before deciding the result.",
                retryable=False,
                recovery_hint=recovery_hint(recovery),
                details={
                    "observation_status": "post_action_observation_pending",
                    **cause,
                    "action_result": _model_action_metadata(result.result),
                    **({"window_transition": recovery} if recovery is not None else {}),
                },
            )
        finally:
            await finish_takeover()

    async def computer_handoff():
        blocked = preflight()
        if blocked:
            return blocked
        try:
            await manager.handoff()
            clear_computer_grants()
            trusted_snapshot.clear()
            return _json({
                "success": True,
                "session_id": manager.session_id,
                "handoff_active": True,
                "message": (
                    "Automation input is blocked until computer_resume revalidates the target."
                    if manager.suspended_target is not None else
                    "Automation input is blocked. No window is bound; after the user yields control, "
                    "call computer_resume without refs, then bind a fresh window."
                ),
                **resume_recovery(),
            })
        except ComputerSessionError as exc:
            return _session_failure(exc)

    def targetless_resume_receipt() -> dict[str, Any]:
        return {
            "success": True,
            "status": "resumed_unbound",
            "session_id": manager.session_id,
            "message": "User control ended. No window was bound during this handoff; no input was sent and no earlier authority was restored.",
            "recovery_hint": "Call computer_apps, then choose an exact app_ref and window_ref for computer_get_app_state. New observation and input authorization are required.",
        }

    def unbound_resume_receipt() -> dict[str, Any]:
        return {
            "success": True,
            "status": "target_gone_unbound",
            "session_id": manager.session_id,
            "message": "The exact suspended target vanished. Old input authority is revoked; no replacement was bound and no input was sent.",
            "recovery_hint": "Call computer_apps, then choose an exact app_ref and window_ref for computer_get_app_state. New observation and input authorization are required.",
        }

    async def computer_resume(
        app_ref: str | None = None,
        window_ref: str | None = None,
        *,
        _permission_call_id: str = "",
    ):
        nonlocal resume_publication_owner
        blocked = preflight()
        if blocked:
            return blocked
        await catalog_target_operation_lock.acquire()
        publication_started = False
        try:
            suspended = manager.suspended_target
            if suspended is None and not manager.handed_off:
                return _failure(
                    "no_suspended_target", "There is no suspended Computer Use target to resume.",
                    retryable=False, recovery_hint="Do not call handoff to manufacture a suspended target. Use computer_apps and computer_get_app_state for normal observation; resolve any prior overlay failure first.",
                )
            if suspended is None:
                if app_ref is not None or window_ref is not None:
                    return _failure(
                        "invalid_arguments",
                        "This handoff has no suspended target. Call computer_resume without app_ref or window_ref.",
                        retryable=False, details=resume_recovery(),
                    )
                try:
                    await manager.resume_unbound(_permission_call_id)
                except ComputerSessionError as exc:
                    return _session_failure(exc)
                resume_publication_owner = _permission_call_id
                publication_started = True
                trusted_target.clear()
                trusted_snapshot.clear()
                clear_computer_grants()
                return json.dumps(targetless_resume_receipt())
            if app_ref is None or window_ref is None:
                return _failure(
                    "invalid_arguments",
                    "This handoff has a suspended target. Pass both supplied computer_resume refs.",
                    retryable=False, details=resume_recovery(),
                )
            if (
                suspended.app_ref != app_ref
                or suspended.window_ref != window_ref
            ):
                return _failure(
                    ComputerErrorCode.TARGET_GONE.value,
                    "The suspended Computer Use target does not match these exact references. Use the supplied suspended references, not a refreshed catalog.",
                    retryable=True, details=resume_recovery(),
                )
            try:
                # Refresh internally while retaining the user's suspended target
                # identity. New catalog refs are not the resume authority.
                catalog = await _computer_apps()
                if not isinstance(catalog, str):
                    return catalog
                target = await manager.resume(_permission_call_id)
                resume_publication_owner = _permission_call_id
                publication_started = True
                if target is None:
                    trusted_target.clear()
                    trusted_snapshot.clear()
                    clear_computer_grants()
                    return json.dumps(unbound_resume_receipt())
                resume_target_binding = manager.snapshot_target_binding
                if resume_target_binding is None:
                    raise ComputerSessionError(
                        "target_gone",
                        "target_gone: resumed target binding is unavailable",
                    )
                remember_target(target.app_ref, target.window_ref)
                return await snapshot_result(
                    await manager.snapshot(
                        "target_window",
                        expected_target_binding=resume_target_binding,
                        resume_publication_id=_permission_call_id,
                    ),
                    scope="target_window",
                    resume_publication_id=_permission_call_id,
                )
            except HelperApplicationError as exc:
                return _application_failure(exc.error)
            except HelperTransportError as exc:
                logger.warning("computer resume helper failed error_type=%s", type(exc).__name__)
                return _failure("helper_failed", "Unable to revalidate the macOS target.", retryable=True)
            except ComputerSessionError as exc:
                return _session_failure(exc)
            except Exception as exc:  # noqa: BLE001 - backend failures cross a sanitized tool boundary
                logger.warning("computer resume failed error_type=%s", type(exc).__name__)
                return _failure("helper_failed", "Unable to revalidate the macOS target.", retryable=True)
        finally:
            if not publication_started:
                catalog_target_operation_lock.release()

    async def finalize_resume_publication(
        _args: dict[str, Any],
        result: dict[str, Any],
        permission_call_id: str,
    ) -> None:
        nonlocal resume_publication_owner, observation_authority_generation
        if resume_publication_owner != permission_call_id:
            return
        try:
            if not result.get("error") and result.get("verified") is True:
                try:
                    await manager.commit_resume_publication(permission_call_id)
                    return
                except BaseException as exc:  # noqa: BLE001 - publication must fail closed
                    logger.warning(
                        "computer resume publication commit failed error_type=%s",
                        type(exc).__name__,
                    )
                    result.pop("fresh_output", None)
                    result["verified"] = False
                    result["verification_detail"] = "resume publication commit failed"
                    result["error"] = (
                        "[ToolPostconditionFailed] computer_resume: "
                        "resume publication commit failed"
                    )
                    result["code"] = "postcondition_failed"
                    result["error_type"] = "postcondition_failed"
                    result["recoverable"] = False
            with suppress(BaseException):
                await manager.abort_resume_publication(permission_call_id)
            observation_authority_generation += 1
            trusted_target.clear()
            trusted_snapshot.clear()
            clear_computer_grants()
        finally:
            resume_publication_owner = None
            catalog_target_operation_lock.release()

    async def computer_begin_takeover(snapshot_id: str, plan_ref: str):
        blocked = preflight()
        if blocked:
            return blocked
        try:
            takeover_ref = await manager.begin_takeover(snapshot_id, plan_ref)
        except ComputerSessionError as exc:
            return _session_failure(exc)
        return _json({"takeover_ref": takeover_ref})

    async def computer_end_takeover(takeover_ref: str):
        blocked = preflight()
        if blocked:
            return blocked
        try:
            await manager.end_takeover(takeover_ref)
        except ComputerSessionError as exc:
            return _session_failure(exc)
        return _json({"ended": True})

    async def computer_close():
        nonlocal trusted_catalog_generation, observation_authority_generation
        clear_computer_grants()
        clear_trusted_wps_destination()
        trusted_catalog.clear()
        trusted_catalog_generation = 0
        observation_authority_generation += 1
        trusted_target.clear()
        trusted_snapshot.clear()
        pending_clicks.pending = None
        closed_session_id = manager.session_id
        try:
            await manager.close()
            return _json({"success": True, "closed": True, "session_id": closed_session_id})
        except (ComputerSessionError, OSError, RuntimeError) as exc:
            # ComputerSessionManager still clears grants and cache when only
            # helper shutdown fails. Keep the failure visible and bounded.
            return _failure(
                "helper_failed",
                f"Computer Use closed with helper error ({type(exc).__name__}).",
                retryable=False,
            )

    def verify_snapshot(_args: dict, result: dict) -> tuple[bool, str]:
        try:
            payload = json.loads(str(result.get("fresh_output") or result.get("output") or ""))
            artifact = payload.get("image_artifact") if isinstance(payload, dict) else None
            if payload.get("type") != "image_attachment" or not isinstance(artifact, str):
                return False, "tool did not return a request-local image attachment"
            expected = verified_artifacts.pop(artifact, None)
            detail_expected = verified_detail_artifacts.pop(artifact, None)
            if expected is None:
                manager.read_verified_png_artifact(artifact)
            else:
                expected_identity, expected_sha256 = expected
                manager.read_verified_png_artifact(
                    artifact,
                    expected_identity=expected_identity,
                    expected_sha256=expected_sha256,
                )
            if detail_expected is not None:
                detail_artifact, detail_metadata = detail_expected
                _preflight_current_snapshot_artifacts(manager)
                _path, _png, detail_data, detail_digest, _authorities = (
                    _read_verified_snapshot_pair(manager, artifact, detail_artifact)
                )
                decode_text_detail_envelope(
                    detail_data,
                    snapshot_id=str(detail_metadata.get("snapshot_id") or ""),
                    metadata=detail_metadata,
                    sha256=detail_digest,
                )
            return True, "verified private mode-0600 PNG through the held session directory"
        except (ComputerSessionError, OSError, ValueError, json.JSONDecodeError) as exc:
            return False, str(exc)

    def verify_resume(args: dict, result: dict) -> tuple[bool, str]:
        if manager.target is not None:
            return verify_snapshot(args, result)
        targetless = manager.suspended_target is None
        try:
            if targetless:
                manager.validate_targetless_resume_publication(resume_publication_owner or "")
            else:
                manager.validate_unbound_resume_publication(resume_publication_owner or "")
            payload = json.loads(str(result.get("fresh_output") or result.get("output") or ""))
            if payload != (targetless_resume_receipt() if targetless else unbound_resume_receipt()):
                return False, "unbound resume receipt changed before publication"
            if targetless:
                return True, "verified targetless stop release; no window bound and no input sent"
            return True, "verified exact-target absence and revoked authority; no replacement bound"
        except (ComputerSessionError, ValueError, TypeError) as exc:
            return False, str(exc)

    def verify_app_state(args: dict, result: dict) -> tuple[bool, str]:
        try:
            passed, detail = verify_snapshot(args, result)
        except BaseException:  # noqa: BLE001 - fail closed after manager commit
            passed = False
            detail = ""
        if passed:
            return True, detail
        with suppress(BaseException):
            manager.poison_artifact_session(clear_target=True)
        trusted_target.clear()
        trusted_snapshot.clear()
        clear_write_grants()
        return False, "unsafe_artifact: verified app state publication changed"

    def bound_app_state_postcondition_failure(
        name: str,
        args: dict,
        result: dict,
        tool_def: ToolDef,
    ) -> dict | None:
        if (
            name != "computer_get_app_state"
            or result.get("code") != "postcondition_failed"
            or not str(result.get("verification_detail") or "").startswith(
                "unsafe_artifact:"
            )
        ):
            return None
        return {
            "output": "",
            "error": (
                "unsafe_artifact: verified app state publication changed; "
                "close this Computer Use session before retrying"
            ),
            "code": "unsafe_artifact",
            "error_type": "unsafe_artifact",
            "recoverable": False,
            "retryable": False,
            "recovery_hint": (
                "Call computer_close, then start a fresh computer_apps -> "
                "computer_get_app_state sequence."
            ),
            "duration_ms": result.get("duration_ms", 0),
            "risk": result.get("risk", "read"),
            "verified": False,
        }

    registry.hooks.on_after_tool(bound_app_state_postcondition_failure)

    def display_permission_check(args: dict) -> dict[str, Any] | None:
        return snapshot_permission_request(
            approvals,
            current_policy_context(),
            capture_scope=str(args.get("scope") or "target_window"),
            text_detail_mode=str(args.get("text_detail") or "off"),
            target_binding=current_target_binding(),
        )

    def display_permission_grant(args: dict, request: dict[str, Any], decision: str):
        capture_scope = str(args.get("scope") or "target_window")
        text_detail_mode = str(args.get("text_detail") or "off")
        requested_binding = str(getattr(request, "private_target_binding", ""))
        if (
            not requested_binding
            or requested_binding != current_target_binding()
            or getattr(request, "private_capture_scope", "") != capture_scope
            or getattr(request, "private_text_detail_mode", "") != text_detail_mode
        ):
            raise ValueError("Computer Use target changed while approval was pending")
        expected_scope = snapshot_approval_scope(
            current_policy_context(),
            capture_scope=capture_scope,
            text_detail_mode=text_detail_mode,
            target_binding=requested_binding,
        )
        if not expected_scope or expected_scope != getattr(request, "private_scope", ""):
            raise ValueError("Computer Use snapshot approval binding changed")
        return permission_grant(approvals, args, request, decision)

    async def app_state_permission_check(args: dict) -> ComputerApprovalRequest | None:
        clear_write_grants(preserve_foreground_session=True)
        clear_snapshot_grants()
        trusted_snapshot.clear()
        verified_artifacts.clear()
        verified_detail_artifacts.clear()
        await manager.invalidate_app_state_attempt_authority()

        app_ref = str(args.get("app_ref") or "")
        window_ref = str(args.get("window_ref") or "")
        selected = trusted_catalog.get(app_ref)
        window = (
            selected.get("windows", {}).get(window_ref)
            if isinstance(selected, Mapping)
            else None
        )
        if (
            not isinstance(window, Mapping)
            or trusted_catalog_generation <= 0
            or manager.catalog_generation != trusted_catalog_generation
        ):
            return None
        request = app_state_permission_request(
            approvals,
            app_state_policy_context(app_ref, window_ref),
            catalog_generation=trusted_catalog_generation,
            app_ref=app_ref,
            window_ref=window_ref,
            capture_scope=str(args.get("scope") or "target_window"),
            text_detail_mode=str(args.get("text_detail") or "off"),
        )
        if request is not None:
            request.private_authority_generation = observation_authority_generation
        return request

    def app_state_permission_grant(
        args: dict,
        request: dict[str, Any],
        decision: str,
    ):
        app_ref = str(args.get("app_ref") or "")
        window_ref = str(args.get("window_ref") or "")
        capture_scope = str(args.get("scope") or "target_window")
        text_detail_mode = str(args.get("text_detail") or "off")
        selected = trusted_catalog.get(app_ref)
        window = (
            selected.get("windows", {}).get(window_ref)
            if isinstance(selected, Mapping)
            else None
        )
        if (
            not isinstance(window, Mapping)
            or getattr(request, "private_catalog_generation", 0)
            != trusted_catalog_generation
            or manager.catalog_generation != trusted_catalog_generation
            or getattr(request, "private_app_ref", "") != app_ref
            or getattr(request, "private_window_ref", "") != window_ref
            or getattr(request, "private_capture_scope", "") != capture_scope
            or getattr(request, "private_text_detail_mode", "") != text_detail_mode
            or getattr(request, "private_authority_generation", -1)
            != observation_authority_generation
        ):
            raise ValueError("Computer Use catalog or target changed while approval was pending")
        expected_scope = app_state_approval_scope(
            app_state_policy_context(app_ref, window_ref),
            catalog_generation=trusted_catalog_generation,
            app_ref=app_ref,
            window_ref=window_ref,
            capture_scope=capture_scope,
            text_detail_mode=text_detail_mode,
        )
        if not expected_scope or expected_scope != getattr(request, "private_scope", ""):
            raise ValueError("Computer Use app state approval binding changed")
        return permission_grant(approvals, args, request, decision)

    async def act_permission_check(args: dict) -> dict[str, Any] | None:
        nonlocal pending_foreground_digest, pending_foreground_snapshot
        call_id = str(args.get("__permission_call_id") or "")
        if not call_id:
            return None
        raw_actions = args.get("actions", [])
        if args.get("coordinate_space", "window_logical") == "image_pixels":
            if published_image_geometry[0] != args.get("snapshot_id"):
                raise ValueError("Pixel coordinates require the latest published target-window snapshot")
            raw_actions = image_actions(raw_actions, published_image_geometry[1])
        requested_mode = str(args.get("interaction_mode") or "auto")
        opens_dialog = args.get("opens_dialog", False)
        raw_tree = _LAST_PUBLISHED_AX_TREES.get(str(manager.session_id or ""))
        automatic = requested_mode == "auto" and not opens_dialog and "auto_takeover_v1" in observation_capabilities(raw_tree)
        mode = (
            ComputerInteractionMode.FOREGROUND_TAKEOVER if opens_dialog else ComputerInteractionMode.BACKGROUND
        ) if requested_mode == "auto" else ComputerInteractionMode(requested_mode)
        prepared = _PreparedComputerAct(
            mode=mode,
            snapshot_id=str(args.get("snapshot_id") or ""),
            actions=tuple(ComputerAction.from_mapping(action) for action in raw_actions),
            raw_actions=tuple(dict(action) for action in raw_actions),
        )
        prepared_acts[call_id] = prepared
        if opens_dialog and requested_mode == "background":
            prepared.failure = _failure(
                "invalid_arguments", "opens_dialog requires auto or foreground_takeover; no input was dispatched.",
                retryable=False,
            )
            return None
        if manager.handed_off:
            return None
        snapshot_id = prepared.snapshot_id
        context = current_policy_context(snapshot_id)
        # decision.batch_hash 基于此刻的 context；plan_actions 之后 backend 会更新
        # trusted_snapshot，二次复核必须复用同一 context（见 _PreparedComputerAct 注释）。
        prepared.policy_context = context
        if pending_clicks.blocks(manager.snapshot_target_binding, raw_tree, prepared.actions):
            prepared.failure = _failure(
                "effect_pending", "The previous click on this same button was acknowledged; its result is still unconfirmed. No new input was dispatched.",
                retryable=False,
                recovery_hint="Call computer_snapshot(settle_ms=300) and inspect the result. Ref churn or an unchanged dialog does not authorize another click. After two inconclusive reads, report unconfirmed and hand off.",
            )
            return None
        if any(action.replace is True for action in prepared.actions):
            if "replace_text_v1" not in observation_capabilities(raw_tree):
                prepared.failure = _failure(
                    "unsupported_operation",
                    "Field replacement requires replace_text_v1 from an updated helper. No input was dispatched; do not substitute typing or keyboard shortcuts.",
                    retryable=False,
                )
                return None
        if any(action.checked is not None for action in prepared.actions):
            raw_tree = _LAST_PUBLISHED_AX_TREES.get(str(manager.session_id or ""))
            if "checked_click_v1" not in observation_capabilities(raw_tree):
                prepared.failure = _failure("unsupported_operation", "Checked state goals require an updated helper. Refresh/restart the helper before retrying; ordinary clicks remain available.", retryable=False)
                return None
        expected_background_digest = ""
        if mode is ComputerInteractionMode.FOREGROUND_TAKEOVER:
            expected_background_digest = (
                pending_foreground_digest
                if pending_foreground_snapshot == snapshot_id
                else ""
            )
            pending_foreground_digest = ""
            pending_foreground_snapshot = ""
            # 新语义(2026-09-02, requires_active): foreground takeover 是独立显式
            # 授权路径, 不再要求先以 background 提交同一批动作; pending digest
            # 仅作可选的批次一致性校验(为空即跳过)。
        try:
            try:
                plan = await manager.plan_actions(
                    snapshot_id,
                    list(prepared.actions),
                    interaction_mode=mode,
                )
            except HelperApplicationError as exc:
                # Only a typed, pre-input rejection can preserve this exact
                # snapshot for one foreground plan. Transport loss never does.
                if not automatic or exc.error.code not in {
                    ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED,
                    ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED,
                }:
                    raise
                plan = None
            if automatic and (plan is None or plan.requires_takeover):
                mode = ComputerInteractionMode.FOREGROUND_TAKEOVER
                prepared.mode = mode
                plan = await manager.plan_actions(snapshot_id, list(prepared.actions), interaction_mode=mode)
            assert plan is not None
        except asyncio.CancelledError:
            prepared_acts.pop(call_id, None)
            raise
        except Exception as exc:  # noqa: BLE001 - planning is pre-input and safely typed
            prepared.failure = _planning_failure(
                exc,
                requested_takeover=mode is ComputerInteractionMode.FOREGROUND_TAKEOVER,
            )
            return None
        background = classify_computer_batch(
            context,
            prepared.raw_actions,
            interaction_plan=replace(
                plan,
                interaction_mode=ComputerInteractionMode.BACKGROUND,
            ),
        )
        prepared.plan = plan
        if background.handoff_required:
            prepared.decision = background
            return None
        # takeover 合法路径同样要落 decision：主函数的二次复核用它比对
        # batch_hash，漏赋会让复核撞 None 崩溃并被包装成 stale_snapshot
        # （2026-09-04 WPS 六连 stale 的另一半根因）。
        prepared.decision = background
        if mode is ComputerInteractionMode.BACKGROUND:
            prepared.decision = background
            if plan.requires_takeover:
                return None
            request = permission_request(approvals, background)
            if isinstance(request, ComputerApprovalRequest):
                request.private_call_id = call_id
            return request
        if (
            not plan.requires_takeover
            or (
                expected_background_digest
                and background.batch_hash != expected_background_digest
            )
        ):
            prepared.failure = _failure(
                ComputerErrorCode.STALE_SNAPSHOT.value,
                _SAFE_ACTION_ERRORS[ComputerErrorCode.STALE_SNAPSHOT],
                retryable=True,
            )
            return None
        try:
            enabled = enabled_pid_actions(
                str(context.get("bundle_id") or ""),
                str(context.get("app_version") or ""),
            )
        except CompatibilityRegistryError:
            enabled = frozenset()
        if not set(plan.pid_action_classes).issubset(enabled):
            prepared.failure = _failure(
                ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED.value,
                "This foreground action is not enabled for the exact application version.",
                retryable=False,
            )
            return None
        # 授权判定用原形态（takeover 的显式授权语义不能因 hash 对齐被降级——
        # permission_request 按 interaction_mode 判 foreground，BG 形态会让
        # 低风险批次免批）。
        decision = classify_computer_batch(
            context,
            prepared.raw_actions,
            interaction_plan=plan,
        )
        prepared.decision = decision
        # hash 基准单独存 BG 形态：主函数与 grant 的 batch_hash 复核都以它为准
        #（2026-09-04 WPS 八连 stale 根因——interaction_mode 进 canonical hash，
        # 各复核点形态必须一致）。
        prepared.hash_basis = classify_computer_batch(
            context,
            prepared.raw_actions,
            interaction_plan=replace(
                plan,
                interaction_mode=ComputerInteractionMode.BACKGROUND,
            ),
        )
        request = permission_request(approvals, decision)
        if request is None and decision.scope in approvals.approved_scopes:
            prepared.approved = True
            authorized_acts.add(call_id)
            prepared.restore_previous_focus = False
        if isinstance(request, ComputerApprovalRequest):
            request.private_call_id = call_id
        return request

    def act_permission_grant(args: dict, request: dict[str, Any], decision: str):
        if manager.handed_off:
            raise ValueError("Computer Use target changed while approval was pending")
        call_id = str(getattr(request, "private_call_id", ""))
        prepared = prepared_acts.get(call_id)
        if prepared is None or prepared.decision is None or prepared.plan is None:
            raise ValueError("Computer Use target changed while approval was pending")
        current = classify_computer_batch(
            current_policy_context(prepared.snapshot_id),
            prepared.raw_actions,
            # 与 decision/主函数复核三方同形态（BACKGROUND）——同 4ac5002 根因，
            # grant 复核漏改会让授权路径自己撞 hash 失配。
            interaction_plan=replace(
                prepared.plan,
                interaction_mode=ComputerInteractionMode.BACKGROUND,
            ),
        )
        # takeover 批次比 hash_basis（BG 形态）；background 批次的 decision 本就是
        # BG 形态，直接当基准——两者皆缺才算准备不完整。
        basis = prepared.hash_basis or prepared.decision
        if basis is None or current.batch_hash != basis.batch_hash:
            raise ValueError("Computer Use target changed while approval was pending")
        cleanup = permission_grant(approvals, args, request, decision)
        if prepared.mode is ComputerInteractionMode.FOREGROUND_TAKEOVER:
            prepared.approved = True
            authorized_acts.add(call_id)
            prepared.restore_previous_focus = not (
                registry.yolo or decision == "session" and "session" in request.get("choices", [])
            )
        return cleanup

    completed_observations: set[str] = set()
    completed_acts: set[str] = set()
    # Lifecycle bookkeeping must survive catalog refresh clearing prepared plans.
    authorized_acts: set[str] = set()

    def complete_observation(_args: dict, result: dict, call_id: str) -> None:
        if result.get("error"):
            clear_write_grants()
        else:
            completed_observations.add(call_id)

    def finalize_observation(args: dict) -> None:
        call_id = str(args.get("__permission_call_id") or "")
        if call_id not in completed_observations:
            # Also runs when cancellation occurs before the tool body, e.g.
            # while waiting for observation approval or preparing the target.
            clear_write_grants()
        completed_observations.discard(call_id)

    def complete_act(_args: dict, result: dict, call_id: str) -> None:
        prepared = prepared_acts.get(call_id) or act_receipt_states.get(call_id)
        if prepared is not None:
            payload = {}
            with suppress(ValueError, TypeError):
                payload = json.loads(result.get("fresh_output", "{}"))
            if not isinstance(payload, dict):
                payload = {}
            if not payload and isinstance(result.get("details"), dict):
                payload = result["details"]
            if "action_result" not in payload and prepared.dispatch_result is not None:
                payload = {**payload, "action_result": prepared.dispatch_result}
            result["computer_receipt"] = build_action_receipt(
                payload, mode=prepared.mode.value, action_count=len(prepared.actions),
                dispatch_attempted=prepared.dispatch_attempted, error_code=result.get("code", ""),
            )
        if result.get("error") and call_id in authorized_acts:
            clear_write_grants()
        completed_acts.add(call_id)

    def finalize_act_permission(args: dict) -> None:
        call_id = str(args.get("__permission_call_id") or "")
        prepared_acts.pop(call_id, None)
        act_receipt_states.pop(call_id, None)
        if call_id not in completed_acts and call_id in authorized_acts:
            clear_write_grants()
        completed_acts.discard(call_id)
        authorized_acts.discard(call_id)

    def validate_act_arguments(args: dict[str, Any]) -> None:
        for action in args.get("actions", []):
            ComputerAction.from_mapping(action)
            if action.get("type") == "type" and _REDACTED_ACT_TEXT.fullmatch(action["text"]):
                raise ValueError(
                    "text contains an internal redaction marker, not the original input. "
                    "Use the original user-authorized text from current evidence; "
                    "never type the marker from tool history."
                )

    def redact_act_arguments(args: dict[str, Any]) -> dict[str, Any]:
        """Preserve ordinary input; mask credential-bearing text in stores.

        Keep complete markers idempotent and non-executable. The execution
        path still receives the original validated arguments.
        """
        redacted: dict[str, Any] = {}
        for key, value in args.items():
            if key == "actions" and isinstance(value, list):
                redacted_actions = []
                for action in value:
                    if isinstance(action, dict) and action.get("type") == "type":
                        text = action.get("text")
                        if (
                            isinstance(text, str)
                            and not _REDACTED_ACT_TEXT.fullmatch(text)
                            and contains_credentials(text)
                        ):
                            action = {
                                **action,
                                "text": f"[redacted {len(text)} chars]",
                            }
                    redacted_actions.append(action)
                redacted[key] = redacted_actions
            else:
                redacted[key] = value
        return redacted

    def cleanup_session(session_id: str, reason: str) -> None:
        nonlocal trusted_catalog_generation, observation_authority_generation
        del session_id
        clear_computer_grants()
        verified_artifacts.clear()
        verified_detail_artifacts.clear()
        trusted_catalog.clear()
        trusted_catalog_generation = 0
        observation_authority_generation += 1
        trusted_target.clear()
        trusted_snapshot.clear()
        if isinstance(manager, LocalComputerRuntime):
            manager.end_session(reason)
            return
        async def close_safely() -> None:
            try:
                await manager.close()
            except Exception as exc:  # noqa: BLE001 - lifecycle hook cannot leak helper failures
                logger.warning("Computer Use session cleanup failed error_type=%s", type(exc).__name__)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(manager.close())
            except Exception as exc:  # noqa: BLE001 - lifecycle hook cannot leak helper failures
                logger.warning("Computer Use session cleanup failed error_type=%s", type(exc).__name__)
        else:
            task = loop.create_task(close_safely())
            task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)

    registry.hooks.on_session_end(cleanup_session)

    common = {"group": "computer", "cache_results": False, "strict_schema": True}
    registry.register(ToolDef("computer_status", "Report macOS Computer Use support and session state.", STATUS_SCHEMA, computer_status, risk="read", replay="safe", **common))
    registry.register(ToolDef(
        "computer_apps",
        "List bounded running GUI applications and eligible windows.",
        APPS_SCHEMA,
        computer_apps,
        completion_finalizer=complete_observation, permission_finalizer=finalize_observation,
        risk="read",
        replay="safe",
        repeat_guard=False,
        max_calls_per_turn=8,
        **common,
    ))
    registry.register(ToolDef(
        "computer_get_app_state",
        "Bind one exact target from the latest application catalog without activating it. Its returned target-window snapshot_id and element refs can feed the next computer_act directly while current; no extra focus or snapshot is required.",
        GET_APP_STATE_SCHEMA,
        computer_get_app_state,
        risk="read",
        approval="never",
        replay="safe",
        result_persistence="request_local",
        completion_finalizer=complete_observation, permission_finalizer=finalize_observation,
        permission_check=app_state_permission_check,
        permission_grant=app_state_permission_grant,
        permission_authoritative=True, permission_yolo_auto_grant=True,
        postcondition=verify_app_state,
        repeat_guard=False, max_calls_per_turn=32,
        **common,
    ))
    registry.register(ToolDef("computer_focus", "Lock Computer Use to one opaque application and window reference.", FOCUS_SCHEMA, computer_focus, risk="write", approval="on_risk", replay="safe", **common))
    registry.register(ToolDef(
        "computer_snapshot", "Observe the locked macOS target. After an acknowledged click with pending effect, use settle_ms=300 for a bounded read; do not click again to wait. Two inconclusive reads are enough to report unconfirmed.", SNAPSHOT_SCHEMA, computer_snapshot,
        risk="read", approval="never", replay="safe", result_persistence="request_local",
        completion_finalizer=complete_observation, permission_finalizer=finalize_observation,
        permission_check=display_permission_check, permission_grant=display_permission_grant,
        permission_authoritative=True, permission_yolo_auto_grant=True,
        postcondition=verify_snapshot, repeat_guard=False, max_calls_per_turn=32, **common,
    ))
    registry.register(ToolDef(
        "computer_act", "Execute a guarded batch. For short forms, put independent radio/checkbox choices in one actions array using click + checked:true and fresh form_controls refs/indexes. With replace_text_v1, type + replace:true + exact element_ref replaces the complete field value (empty text clears); inspect effect verification, never replace it with keyboard shortcuts after failure. Already satisfied goals skip input; inspect final choice_verification. An input acknowledgement alone does not prove the desired effect or page saving. Default auto plans takeover before input; set opens_dialog:true on the FIRST click opening a native file/save/modal panel, including ordinary or custom buttons. Coordinates default to window_logical; use image_pixels only with the latest published target image. Return fresh refs for continued work. Never replay unknown outcomes or click Submit without task authorization.", ACT_SCHEMA, computer_act,
        risk="write", approval="on_risk", replay="never", max_retries=0,
        result_persistence="request_local", permission_check=act_permission_check,
        permission_grant=act_permission_grant, permission_finalizer=finalize_act_permission,
        completion_finalizer=complete_act,
        postcondition=verify_snapshot,
        permission_authoritative=True, permission_yolo_auto_grant=True,
        argument_persistence="request_local", argument_validator=validate_act_arguments,
        argument_redactor=redact_act_arguments, **common,
    ))
    registry.register(ToolDef(
        "computer_begin_takeover", "Authorize a planned foreground takeover and return its takeover reference.",
        TAKEOVER_SCHEMA, computer_begin_takeover,
        risk="write", approval="on_risk", replay="safe", result_persistence="request_local", **common,
    ))
    registry.register(ToolDef(
        "computer_end_takeover", "End an authorized foreground takeover and release the foreground authority.",
        END_TAKEOVER_SCHEMA, computer_end_takeover,
        risk="write", approval="never", replay="safe", **common,
    ))
    registry.register(ToolDef("computer_handoff", "Block model input and transfer control to the user.", EMPTY_SCHEMA, computer_handoff, risk="write", approval="never", replay="safe", **common))
    registry.register(ToolDef(
        "computer_resume", "Revalidate the handed-off target and return a fresh observation. After a targetless handoff, call it without refs to end user control unbound.", RESUME_SCHEMA, computer_resume,
        risk="write", approval="on_risk", replay="safe", result_persistence="request_local",
        postcondition=verify_resume, completion_finalizer=finalize_resume_publication, **common,
    ))
    registry.register(ToolDef("computer_close", "Close Computer Use and erase session-local captures and grants.", EMPTY_SCHEMA, computer_close, risk="write", approval="never", replay="safe", **common))
    return manager


__all__ = [
    "ACTION_SCHEMA",
    "ACT_SCHEMA",
    "GET_APP_STATE_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "LocalComputerRuntime",
    "register_computer_tools",
    "register_local_computer_runtime",
]
