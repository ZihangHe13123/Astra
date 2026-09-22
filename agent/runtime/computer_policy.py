"""Backend-authoritative risk policy for provider-neutral Computer Use.

Model-authored action fields are untrusted.  They identify the requested
protocol action, but application, window, element, and path semantics come
only from the current helper observation assembled by the tool layer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .computer_protocol import DISPATCH_ACTION_CLASSES, ComputerInteractionMode
from .tools.approval import ScopedApprovalStore
from .tools.registry import PermissionCleanup


class ComputerRisk(str, Enum):
    """Monotonic Computer Use risk levels with stable wire labels."""

    OBSERVE = "observe"
    ORDINARY = "ordinary"
    HIGH_IMPACT = "high_impact"
    PROHIBITED = "prohibited"


_RISK_RANK = {
    ComputerRisk.OBSERVE: 0,
    ComputerRisk.ORDINARY: 1,
    ComputerRisk.HIGH_IMPACT: 2,
    ComputerRisk.PROHIBITED: 3,
}

_LOCAL_EDITING_BUNDLES = frozenset({
    "com.kingsoft.wpsoffice.mac",
    "com.kingsoft.wpsoffice.mac.writer",
    "com.kingsoft.wpsoffice.mac.presentation",
    "com.kingsoft.wpsoffice.mac.spreadsheets",
    "com.astra.computer-fixture",
    "dev.astra.fixture",
})
_FINDER_BUNDLES = frozenset({"com.apple.finder"})
_TERMINAL_BUNDLES = frozenset({"com.apple.Terminal", "com.apple.terminal"})
_PROFILED_BUNDLES = _LOCAL_EDITING_BUNDLES | _FINDER_BUNDLES | _TERMINAL_BUNDLES
_SYSTEM_BUNDLE_MARKERS = (
    "com.apple.systempreferences",
    "com.apple.systemsettings",
    "com.apple.loginwindow",
    "com.apple.securityagent",
)

_PROHIBITED_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bpassword\b",
    r"\bpasscode\b",
    r"\bpin\b",
    r"\b(?:security code|cvv|cvc)\b",
    r"\bone[- ]?time(?: password| code)?\b",
    r"\botp\b",
    r"\b(?:2fa|mfa)\b",
    r"\bverification code\b",
    r"\bcaptcha\b",
    r"\btouch\s*id\b",
    r"\bface\s*id\b",
    r"\bauth(?:enticate|entication|orize|orization)?\b",
    r"\b(?:log\s*in|login|sign\s*in)\b",
    r"\bconfirm(?:ation)? payment\b",
    r"\bpay(?:ment)?\b",
    r"\bapple\s*pay\b",
    r"\b(?:checkout|purchase|buy)\b",
    r"\bcredit card\b",
    r"\bdebit card\b",
    r"\bcard\b",
    r"\bbank(?:ing)?\b",
    r"密码|口令|验证码|动态码|短信码|安全码|一次性密码|一次性代码|触控\s*id|指纹",
    r"支付|付款|确认付款|银行|银行卡|信用卡|借记卡|卡号|身份验证|认证|登录",
))

_HIGH_IMPACT_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bsend\b",
    r"\bsubmit\b",
    r"\bupload\b",
    r"\bexport\b",
    r"\bshare\b",
    r"\bdelete\b",
    r"\btrash\b",
    r"\bremove\b",
    r"\boverwrite\b",
    r"\breplace\b",
    r"\bsave\b",
    r"\bsave\s+(?:as|a copy)\b",
    r"\brename\b",
    r"\b(?:run|execute)\b",
    r"\binstall\b",
    r"\buninstall\b",
    r"\bmove\b",
    r"\bcopy\b",
    r"发送|提交|上传|导出|分享|共享|删除|废纸篓|移到废纸篓|覆盖|另存为|保存副本|安装|卸载|移动|复制|重命名",
))

_PROTOCOL_ACTION_KEYS = frozenset({
    "type",
    "x",
    "y",
    "end_x",
    "end_y",
    "text",
    "key",
    "keys",
    "delta_x",
    "delta_y",
    "duration_ms",
    "element_ref",
    "target_element_ref",
    "modifiers",
    "replace",
})


@dataclass(frozen=True)
class ComputerPolicyDecision:
    risk: ComputerRisk
    scope: str
    choices: tuple[str, ...]
    operation: str
    target: str
    reason: str
    effect: str
    boundary: str
    question: str
    application: str
    window: str
    known_path: str = ""
    batch_hash: str = ""
    handoff_required: bool = False
    interaction_mode: ComputerInteractionMode = ComputerInteractionMode.BACKGROUND
    requires_takeover: bool = False
    interaction_reason: str = "background_ax_only"
    action_classes: tuple[str, ...] = ()


class ComputerApprovalRequest(dict[str, Any]):
    """Public approval copy with its exact scope kept off the wire."""

    private_authority_generation: int
    private_call_id: str

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        private_scope: str,
        private_target_binding: str = "",
        private_capture_scope: str = "",
        private_text_detail_mode: str = "",
        private_catalog_generation: int = 0,
        private_app_ref: str = "",
        private_window_ref: str = "",
    ) -> None:
        super().__init__(payload)
        self.private_scope = private_scope
        self.private_target_binding = private_target_binding
        self.private_capture_scope = private_capture_scope
        self.private_text_detail_mode = private_text_detail_mode
        self.private_catalog_generation = private_catalog_generation
        self.private_app_ref = private_app_ref
        self.private_window_ref = private_window_ref


@dataclass(frozen=True)
class _ActionAssessment:
    risk: ComputerRisk
    summary: str


def _bounded(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _safe_ui_label(value: Any, *, fallback: str = "") -> str:
    label = _bounded(value, 160)
    if not label or any(marker in label for marker in ("/", "\\", "\x00")):
        return fallback
    return label


def _maximum(left: ComputerRisk, right: ComputerRisk) -> ComputerRisk:
    return left if _RISK_RANK[left] >= _RISK_RANK[right] else right


def snapshot_approval_scope(
    context: Mapping[str, Any],
    *,
    capture_scope: str,
    text_detail_mode: str,
    target_binding: str,
) -> str:
    """Return an opaque scope bound to one exact snapshot observation request."""

    if capture_scope not in {"target_window", "display"}:
        raise ValueError("snapshot capture scope is invalid")
    if text_detail_mode not in {"off", "on"}:
        raise ValueError("snapshot text detail mode is invalid")
    if capture_scope == "target_window" and text_detail_mode == "off":
        return ""
    authority = {
        "session_id": _bounded(context.get("session_id"), 128),
        "app_ref": _bounded(context.get("app_ref"), 256),
        "window_ref": _bounded(context.get("window_ref"), 256),
        "target_binding": _bounded(target_binding, 1_024),
        "capture_scope": capture_scope,
        "text_detail_mode": text_detail_mode,
    }
    encoded = json.dumps(
        authority,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"computer-observe-snapshot:{hashlib.sha256(encoded).hexdigest()}"


def app_state_approval_scope(
    context: Mapping[str, Any],
    *,
    catalog_generation: int,
    app_ref: str,
    window_ref: str,
    capture_scope: str,
    text_detail_mode: str,
) -> str:
    """Bind one app-state observation grant to the exact current catalog record."""

    if capture_scope not in {"target_window", "display"}:
        raise ValueError("app state capture scope is invalid")
    if text_detail_mode not in {"off", "on"}:
        raise ValueError("app state text detail mode is invalid")
    if capture_scope == "target_window" and text_detail_mode == "off":
        return ""
    if (
        isinstance(catalog_generation, bool)
        or not isinstance(catalog_generation, int)
        or catalog_generation <= 0
    ):
        raise ValueError("app state catalog generation is invalid")
    session_id = context.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("app state session is invalid")
    for name, value in (("app_ref", app_ref), ("window_ref", window_ref)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"app state {name} is invalid")
    exact = (
        f"{session_id}:{catalog_generation}:{app_ref}:{window_ref}:"
        f"{capture_scope}:{text_detail_mode}"
    )
    encoded = json.dumps(
        {
            "session_id": session_id,
            "catalog_generation": catalog_generation,
            "app_ref": app_ref,
            "window_ref": window_ref,
            "capture_scope": capture_scope,
            "text_detail_mode": text_detail_mode,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return (
        f"computer-observe-app-state:{exact}:"
        f"{hashlib.sha256(encoded).hexdigest()}"
    )


def snapshot_permission_request(
    approvals: ScopedApprovalStore,
    context: Mapping[str, Any],
    *,
    capture_scope: str,
    text_detail_mode: str,
    target_binding: str,
) -> ComputerApprovalRequest | None:
    """Build one frontend-safe approval for display and/or AX text capture."""

    private_scope = snapshot_approval_scope(
        context,
        capture_scope=capture_scope,
        text_detail_mode=text_detail_mode,
        target_binding=target_binding,
    )
    if not private_scope or private_scope in approvals.approved_scopes:
        return None
    application = _safe_ui_label(
        context.get("application"),
        fallback="Selected application",
    )
    window = _safe_ui_label(
        context.get("window_title"),
        fallback="Selected window",
    )
    arguments = {
        "application": application,
        "window": window,
        "scope": capture_scope,
        "text_detail": text_detail_mode,
    }
    if capture_scope == "display" and text_detail_mode == "on":
        kind = "computer_display_and_text_detail_capture"
        title = "Capture the display and accessibility text"
        summary = "Observe the full display and accessibility text for the selected target"
        effect = (
            "The screenshot may include other applications, windows, or notifications, "
            "and accessibility text may include content outside the visible viewport."
        )
        reason = "This request combines full-display pixels with a detailed accessibility observation."
        question = "Allow one combined display and accessibility text capture?"
    elif capture_scope == "display":
        kind = "computer_display_capture"
        title = "Capture the full macOS display"
        summary = "Observe content outside the selected target window"
        effect = "The screenshot may include other applications, windows, or notifications."
        reason = "Full-display capture may include content outside the selected target window."
        question = "Allow one full-display capture?"
    else:
        kind = "computer_text_detail_capture"
        title = "Capture accessibility text"
        summary = "Observe detailed accessibility text for the selected target window"
        effect = (
            "The accessibility artifact may include text outside the visible viewport when "
            "the application reports it in the current accessibility subtree."
        )
        reason = "Detailed accessibility text is more sensitive than the ordinary window snapshot."
        question = "Allow one accessibility text capture?"
    boundary = (
        "Only this exact Computer Use session, application, window, snapshot scope, and detail mode; "
        "no later capture or desktop input is included."
    )
    return ComputerApprovalRequest(
        {
            "kind": kind,
            "operation": summary,
            "target": f"{application} — {window}",
            "reason": reason,
            "detail": boundary,
            "arguments": arguments,
            "approval_title": title,
            "approval_summary": summary,
            "approval_effect": effect,
            "approval_boundary": boundary,
            "approval_question": question,
            "choices": ["once", "deny"],
        },
        private_scope=private_scope,
        private_target_binding=target_binding,
        private_capture_scope=capture_scope,
        private_text_detail_mode=text_detail_mode,
    )


def app_state_permission_request(
    approvals: ScopedApprovalStore,
    context: Mapping[str, Any],
    *,
    catalog_generation: int,
    app_ref: str,
    window_ref: str,
    capture_scope: str,
    text_detail_mode: str,
) -> ComputerApprovalRequest | None:
    """Build the existing observation prompt with catalog-bound private authority."""

    private_scope = app_state_approval_scope(
        context,
        catalog_generation=catalog_generation,
        app_ref=app_ref,
        window_ref=window_ref,
        capture_scope=capture_scope,
        text_detail_mode=text_detail_mode,
    )
    if not private_scope or private_scope in approvals.approved_scopes:
        return None
    request = snapshot_permission_request(
        ScopedApprovalStore(),
        {
            **context,
            "app_ref": app_ref,
            "window_ref": window_ref,
        },
        capture_scope=capture_scope,
        text_detail_mode=text_detail_mode,
        target_binding=(
            f"{context.get('session_id')}:{catalog_generation}:"
            f"{app_ref}:{window_ref}"
        ),
    )
    if request is None:
        raise ValueError("app state approval request is missing")
    request.private_scope = private_scope
    request.private_catalog_generation = catalog_generation
    request.private_app_ref = app_ref
    request.private_window_ref = window_ref
    return request


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _trusted_elements(
    context: Mapping[str, Any],
    action: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    if context.get("metadata_trusted") is not True:
        return ()
    reference = action.get("target_element_ref")
    if not isinstance(reference, str):
        reference = action.get("element_ref")
    if not isinstance(reference, str) and str(action.get("type") or "").lower() in {"type", "keypress"}:
        reference = context.get("focused_element_ref")
    elements = context.get("elements")
    if not isinstance(elements, Mapping):
        return ()
    declared: tuple[Mapping[str, Any], ...] = ()
    if isinstance(reference, str):
        element = elements.get(reference)
        if not isinstance(element, Mapping):
            return ()
        declared = (element,)

    raw_x = action.get("x")
    raw_y = action.get("y")
    if (
        isinstance(raw_x, bool)
        or isinstance(raw_y, bool)
        or not isinstance(raw_x, (int, float))
        or not isinstance(raw_y, (int, float))
        or not math.isfinite(raw_x)
        or not math.isfinite(raw_y)
    ):
        return declared
    hits: list[tuple[float, Mapping[str, Any]]] = []
    for raw_element in elements.values():
        if not isinstance(raw_element, Mapping):
            continue
        bounds = raw_element.get("bounds")
        if not isinstance(bounds, Mapping):
            continue
        numeric_values: list[float] = []
        for key in ("x", "y", "width", "height"):
            value = bounds.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                break
            numeric_values.append(float(value))
        if len(numeric_values) != 4:
            continue
        x, y, width, height = numeric_values
        if width < 0 or height < 0:
            continue
        if x <= raw_x <= x + width and y <= raw_y <= y + height:
            hits.append((width * height, raw_element))
    ordered = list(declared)
    for _area, element in sorted(hits, key=lambda item: item[0]):
        if all(element is not existing for existing in ordered):
            ordered.append(element)
    return tuple(ordered)


def _trusted_semantics(
    context: Mapping[str, Any],
    elements: Sequence[Mapping[str, Any]],
) -> str:
    if context.get("metadata_trusted") is not True:
        return ""
    fields: list[str] = []
    for source in (context, *elements):
        for key in ("application", "window_title", "window_role", "role", "subrole", "label", "title", "help"):
            value = source.get(key)
            if isinstance(value, str):
                fields.append(_bounded(value, 512))
        actions = source.get("actions")
        if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes, bytearray)):
            fields.extend(_bounded(value, 128) for value in actions[:32])
    return " ".join(fields)


def _hint_risk(action: Mapping[str, Any]) -> ComputerRisk:
    raw = str(action.get("risk_hint") or "").strip().lower().replace("-", "_")
    aliases = {
        "observe": ComputerRisk.OBSERVE,
        "ordinary": ComputerRisk.ORDINARY,
        "high": ComputerRisk.HIGH_IMPACT,
        "high_impact": ComputerRisk.HIGH_IMPACT,
        "prohibited": ComputerRisk.PROHIBITED,
    }
    return aliases.get(raw, ComputerRisk.OBSERVE)


def _action_key(action: Mapping[str, Any]) -> str:
    raw = action.get("key", action.get("keys", ""))
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return "+".join(str(value) for value in raw).casefold()
    return str(raw).casefold()


def _action_modifiers(action: Mapping[str, Any]) -> frozenset[str]:
    raw = action.get("modifiers")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return frozenset()
    return frozenset(str(value).casefold() for value in raw)


def _classify_action(context: Mapping[str, Any], action: Mapping[str, Any]) -> _ActionAssessment:
    action_type = str(action.get("type") or "").strip().lower()
    if action_type == "wait":
        return _ActionAssessment(_hint_risk(action), "wait")

    trusted = context.get("metadata_trusted") is True
    bundle = str(context.get("bundle_id") or "").strip().casefold() if trusted else ""
    elements = _trusted_elements(context, action)
    semantics = _trusted_semantics(context, elements)
    reference_requested = isinstance(action.get("element_ref"), str) or isinstance(
        action.get("target_element_ref"), str
    )
    element_identity_incomplete = bool(elements) and any(
        not str(element.get("role") or "").strip()
        or str(element.get("role") or "").strip().casefold()
        in {"<truncated>", "<invalid>", "<redacted>"}
        for element in elements
    )
    target_metadata_incomplete = trusted and (
        not str(context.get("window_ref") or "").strip()
        or not str(context.get("window_title") or "").strip()
        or str(context.get("window_role") or "").strip().casefold() != "axwindow"
    )
    modifiers = _action_modifiers(action)
    key = _action_key(action)
    command_side_effect = (
        action_type == "keypress"
        and "command" in modifiers
        and (
            key in {"delete", "backspace", "s", "p", "q", "w"}
            or bundle in _FINDER_BUNDLES and key in {"c", "d", "v", "x"}
        )
    )

    if _matches(_PROHIBITED_PATTERNS, semantics) or "secure" in semantics.casefold():
        risk = ComputerRisk.PROHIBITED
    elif (
        not trusted
        or not bundle
        or bundle not in _PROFILED_BUNDLES
        or element_identity_incomplete
        or target_metadata_incomplete
        or any(marker in bundle for marker in _SYSTEM_BUNDLE_MARKERS)
        or _matches(_HIGH_IMPACT_PATTERNS, semantics)
        or bundle in _TERMINAL_BUNDLES and action_type in {"type", "keypress"}
        or action_type == "keypress" and _action_key(action) in {"enter", "return", "\r", "\n"} and not reference_requested
        or command_side_effect
        or bundle in _FINDER_BUNDLES and action_type in {"type", "drag"}
    ):
        risk = ComputerRisk.HIGH_IMPACT
    elif action_type in {"click", "right_click", "double_click", "drag"} and not reference_requested:
        risk = (
            ComputerRisk.ORDINARY
            if bundle in _LOCAL_EDITING_BUNDLES
            else ComputerRisk.HIGH_IMPACT
        )
    elif (
        reference_requested and not elements
        or action_type in {"type", "keypress"} and not elements
        or action_type not in {"click", "right_click", "double_click", "type", "keypress", "scroll", "drag"}
    ):
        risk = ComputerRisk.HIGH_IMPACT
    else:
        risk = ComputerRisk.ORDINARY

    risk = _maximum(risk, _hint_risk(action))
    first_element = elements[0] if elements else {}
    label = _bounded(first_element.get("label") or first_element.get("title"), 80)
    summary = action_type.replace("_", " ") or "unknown action"
    if action_type == "type":
        text = action.get("text")
        count = len(text) if isinstance(text, str) else 0
        summary = f"type text ({count} characters)"
    elif label:
        summary = f"{summary} {label}"
    return _ActionAssessment(risk, summary)


def _canonical_batch_hash(
    context: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    *,
    interaction_mode: ComputerInteractionMode,
    requires_takeover: bool,
    action_classes: tuple[str, ...],
) -> str:
    normalized_actions: list[dict[str, Any]] = []
    for action in actions:
        normalized: dict[str, Any] = {}
        for key in sorted(_PROTOCOL_ACTION_KEYS):
            if key not in action:
                continue
            value = action[key]
            if isinstance(value, tuple):
                value = list(value)
            normalized[key] = value
        normalized_actions.append(normalized)
    canonical = {
        "session_id": str(context.get("session_id") or ""),
        "bundle_id": str(context.get("bundle_id") or ""),
        "window_ref": str(context.get("window_ref") or ""),
        "snapshot_id": str(context.get("snapshot_id") or ""),
        "interaction_mode": interaction_mode.value,
        "requires_takeover": requires_takeover,
        "action_classes": list(action_classes),
        "actions": normalized_actions,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    secret = context.get("_scope_secret")
    if isinstance(secret, bytes) and secret:
        return hmac.new(secret, encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def classify_computer_batch(
    context: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    *,
    interaction_plan: Any | None = None,
) -> ComputerPolicyDecision:
    """Classify an ordered batch using only backend-provided target metadata."""

    if not isinstance(context, Mapping):
        raise TypeError("context must be an object")
    if not actions:
        raise ValueError("computer action batch requires at least one action")
    if any(not isinstance(action, Mapping) for action in actions):
        raise TypeError("each computer action must be an object")

    assessments = [_classify_action(context, action) for action in actions]
    inferred_classes = {
        "keypress": "press",
        "type": "text",
    }
    action_classes = tuple(dict.fromkeys(
        inferred_classes.get(str(action.get("type") or ""), str(action.get("type") or ""))
        for action in actions
        if str(action.get("type") or "") != "wait"
    ))
    interaction_mode = ComputerInteractionMode.BACKGROUND
    requires_takeover = False
    interaction_reason = "background_ax_only"
    if interaction_plan is not None:
        raw_mode = getattr(interaction_plan, "interaction_mode", None)
        raw_requires_takeover = getattr(interaction_plan, "requires_takeover", None)
        raw_reason = getattr(interaction_plan, "reason", None)
        raw_action_classes = getattr(interaction_plan, "action_classes", None)
        try:
            interaction_mode = (
                raw_mode
                if isinstance(raw_mode, ComputerInteractionMode)
                else ComputerInteractionMode(raw_mode)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("interaction plan mode is invalid") from exc
        if not isinstance(raw_requires_takeover, bool):
            raise ValueError("interaction plan takeover requirement is invalid")
        if (
            not isinstance(raw_action_classes, (tuple, list))
            or len(raw_action_classes) > len(DISPATCH_ACTION_CLASSES)
            or not all(
                isinstance(label, str) and label in DISPATCH_ACTION_CLASSES
                for label in raw_action_classes
            )
            or len(set(raw_action_classes)) != len(raw_action_classes)
        ):
            raise ValueError("interaction plan action classes are invalid")
        if not isinstance(raw_reason, str) or not raw_reason or len(raw_reason) > 240:
            raise ValueError("interaction plan reason is invalid")
        requires_takeover = raw_requires_takeover
        interaction_reason = _bounded(raw_reason, 240)
        action_classes = tuple(raw_action_classes)
    risk = max((item.risk for item in assessments), key=_RISK_RANK.__getitem__)
    session = _bounded(context.get("session_id"), 128) or "unknown-session"
    bundle = _bounded(context.get("bundle_id"), 256) or "unknown-bundle"
    application = _safe_ui_label(
        context.get("application"),
        fallback="Selected application",
    )
    window = _safe_ui_label(
        context.get("window_title"),
        fallback="Selected window",
    )
    known_path = _bounded(context.get("known_path"), 1_000)
    summaries = "; ".join(item.summary for item in assessments[:20])
    batch_hash = _canonical_batch_hash(
        context,
        actions,
        interaction_mode=interaction_mode,
        requires_takeover=requires_takeover,
        action_classes=action_classes,
    )
    target = f"{application} — {window}"
    if known_path:
        target += f" — {known_path}"

    if risk is ComputerRisk.OBSERVE:
        return ComputerPolicyDecision(
            risk=risk,
            scope=(
                f"computer-write-batch:{session}:{batch_hash}"
                if interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER else ""
            ),
            choices=(("once", "deny") if interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER else ()),
            operation=summaries,
            target=target,
            reason="The batch only waits and does not generate desktop input.",
            effect="No desktop input will be generated.",
            boundary="No approval is required for this observation-only batch.",
            question="",
            application=application,
            window=window,
            known_path=known_path,
            batch_hash=batch_hash,
            interaction_mode=interaction_mode,
            requires_takeover=requires_takeover,
            interaction_reason=interaction_reason,
            action_classes=action_classes,
        )
    if risk is ComputerRisk.PROHIBITED:
        return ComputerPolicyDecision(
            risk=risk,
            scope="",
            choices=(),
            operation=summaries,
            target=target,
            reason="This target involves authentication, a protected secret, or payment.",
            effect="A person must complete this protected step directly.",
            boundary="Automation remains blocked until explicit user handoff and resume.",
            question="",
            application=application,
            window=window,
            known_path=known_path,
            batch_hash=batch_hash,
            handoff_required=True,
            interaction_mode=interaction_mode,
            requires_takeover=requires_takeover,
            interaction_reason=interaction_reason,
            action_classes=action_classes,
        )
    if risk is ComputerRisk.ORDINARY:
        return ComputerPolicyDecision(
            risk=risk,
            scope=(
                f"computer-foreground:{session}:{bundle}"
                if interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
                else f"computer-write:{session}:{bundle}"
            ),
            choices=("once", "session", "deny"),
            operation=summaries,
            target=target,
            reason="This batch will interact with the selected macOS application.",
            effect="The selected window may be edited or navigated.",
            boundary=(
                "Ordinary foreground actions in this application during this Computer Use session. "
                "High-impact actions require separate approval. The application may remain in front."
                if interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
                else f"Only this Computer Use session, application, and current window ({window})."
            ),
            question=f"Allow these ordinary actions in {application}?",
            application=application,
            window=window,
            known_path=known_path,
            batch_hash=batch_hash,
            interaction_mode=interaction_mode,
            requires_takeover=requires_takeover,
            interaction_reason=interaction_reason,
            action_classes=action_classes,
        )
    return ComputerPolicyDecision(
        risk=risk,
        scope=f"computer-write-batch:{session}:{batch_hash}",
        choices=("once", "deny"),
        operation=summaries,
        target=target,
        reason="This batch may cause an external, destructive, or otherwise high-impact effect.",
        effect="The selected application may send, save, export, delete, execute, or change protected state.",
        boundary="Only this exact action batch against the current trusted application, window, and snapshot.",
        question=f"Allow this high-impact action batch in {application}?",
        application=application,
        window=window,
        known_path=known_path,
        batch_hash=batch_hash,
        interaction_mode=interaction_mode,
        requires_takeover=requires_takeover,
        interaction_reason=interaction_reason,
        action_classes=action_classes,
    )


def build_computer_approval(decision: ComputerPolicyDecision) -> dict[str, Any]:
    """Build bounded semantic approval copy without runtime identifiers."""

    foreground = decision.interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
    application = _safe_ui_label(decision.application, fallback="Selected application")
    window = _safe_ui_label(decision.window, fallback="Selected window")
    takeover_effect = (
        "The selected application may be temporarily brought to the foreground and control the real pointer and keyboard."
    )
    effect = f"{decision.effect} {takeover_effect}" if foreground else decision.effect
    boundary = decision.boundary
    kind = "computer_foreground_takeover" if foreground else "computer_write"
    title = "Temporarily control a macOS application" if foreground else "Control a macOS application"
    risk_question = decision.question or f"Allow this action fragment in {application}?"
    question = (
        f"{risk_question.rstrip('?')} with temporary foreground takeover?"
        if foreground
        else risk_question
    )
    reason = decision.reason
    fallback_operation = (
        "Foreground fragment: "
        + (", ".join(decision.action_classes) or "application interaction")
    )
    operation = (
        _safe_ui_label(decision.operation, fallback=fallback_operation)
        if foreground
        else decision.operation
    )
    choices = list(decision.choices) or (["once", "deny"] if foreground else [])
    request = {
        "kind": kind,
        "operation": operation,
        "target": " — ".join(value for value in (application, window) if value),
        "reason": reason,
        "detail": boundary,
        "arguments": {
            "application": application,
            "reason": reason,
            "action_classes": list(decision.action_classes),
            "expected_effect": effect,
        },
        "approval_title": title,
        "approval_summary": operation,
        "approval_effect": effect,
        "approval_boundary": boundary,
        "approval_question": question,
        "choices": choices,
    }
    if window:
        request["arguments"]["window"] = window
    return request


def permission_request(
    approvals: ScopedApprovalStore,
    decision: ComputerPolicyDecision,
) -> dict[str, Any] | None:
    """Build a frontend-safe request without raw text or AX payloads."""

    foreground = decision.interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
    if decision.risk is ComputerRisk.PROHIBITED or (
        decision.risk is ComputerRisk.OBSERVE and not foreground
    ):
        return None
    if decision.scope and decision.scope in approvals.approved_scopes:
        return None
    return ComputerApprovalRequest(
        build_computer_approval(decision),
        private_scope=decision.scope,
    )


def permission_grant(
    approvals: ScopedApprovalStore,
    args: dict,
    request: dict[str, Any],
    decision: str,
) -> PermissionCleanup | None:
    """Apply a grant without letting a frontend broaden the declared choices."""

    allowed = {
        str(choice) for choice in request.get("choices", [])
        if str(choice) in {"once", "session", "deny"}
    }
    if decision in allowed:
        effective = decision
    elif decision == "session" and "session" not in allowed and "once" in allowed:
        # High-impact batches deliberately collapse an over-broad frontend
        # session response to their declared exact-call boundary.
        effective = "once"
    else:
        raise ValueError("approval decision is not permitted for this Computer Use request")
    if effective == "deny":
        raise ValueError("a denied Computer Use request cannot be granted")
    scope = getattr(request, "private_scope", "")
    if not scope:
        raise ValueError("approval request is missing its private Computer Use scope")
    if request.get("kind") == "computer_foreground_takeover" and effective == "once":
        # The sealed prepared call owns this authorization. Never publish a
        # temporary app scope that a concurrent call could reuse as a session.
        return None
    return approvals.grant(args, {"scope": scope}, effective)


__all__ = [
    "ComputerApprovalRequest",
    "ComputerPolicyDecision",
    "ComputerRisk",
    "app_state_approval_scope",
    "app_state_permission_request",
    "build_computer_approval",
    "classify_computer_batch",
    "permission_grant",
    "permission_request",
    "snapshot_approval_scope",
    "snapshot_permission_request",
]
