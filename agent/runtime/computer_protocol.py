"""Versioned, strict wire protocol for the native computer-use helper."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from itertools import pairwise
from typing import Any

PROTOCOL_VERSION = 4
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ACTIONS = 20
MAX_TEXT_CHARACTERS = 20_000
MAX_COORDINATE_MAGNITUDE = 1_000_000
MAX_DURATION_MS = 10_000
MAX_KEY_SCALARS = 64
MAX_KEY_CHORD_SCALARS = 128
MAX_ELEMENT_REF_SCALARS = 256
MAX_TEXT_DETAIL_NODES = 4_000
MAX_TEXT_DETAIL_DEPTH = 20
MAX_TEXT_DETAIL_BYTES = 8 * 1024 * 1024
MAX_PUBLIC_AX_DEPTH = 12
MAX_PUBLIC_AX_NODES = 2_000
MAX_PUBLIC_AX_MAPPING_FIELDS = 128
MAX_PUBLIC_AX_STRING_CHARACTERS = 512
MAX_NATIVE_AX_DEPTH = 20
MAX_NATIVE_AX_NODES = 1_000
TEXT_DETAIL_TRUNCATION_REASONS = (
    "depth_limit",
    "node_limit",
    "structural_string_limit",
    "value_limit",
    "aggregate_text_limit",
    "wall_clock_limit",
    "final_byte_limit",
)
ALLOWED_MODIFIERS = frozenset({"command", "control", "option", "shift", "function", "caps_lock"})
KEY_CHORD_MODIFIER_ORDER = ("command", "control", "option", "shift", "function", "caps_lock")
KEY_CHORD_KEYS = frozenset({
    *"abcdefghijklmnopqrstuvwxyz0123456789",
    "=", "-", "]", "[", "'", ";", "\\", ",", "/", ".",
    "return", "tab", "space", "delete", "escape", "left", "right", "down", "up",
})


class ComputerErrorCode(str, Enum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    PERMISSION_DENIED = "permission_denied"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    TARGET_GONE = "target_gone"
    OVERLAY_BLOCKED = "overlay_blocked"
    AX_WINDOW_UNMATCHED = "ax_window_unmatched"
    TARGET_NOT_FRONTMOST = "target_not_frontmost"
    STALE_TARGET = "stale_target"
    STALE_SNAPSHOT = "stale_snapshot"
    SNAPSHOT_FAILED = "snapshot_failed"
    WINDOW_CONTENT_UNAVAILABLE = "window_content_unavailable"
    OBSERVATION_TIMEOUT = "observation_timeout"
    OUT_OF_BOUNDS = "out_of_bounds"
    SECURE_TARGET = "secure_target"
    INPUT_FOCUS_REQUIRED = "input_focus_required"
    ACTION_TIMEOUT = "action_timeout"
    HELPER_FAILED = "helper_failed"
    UNKNOWN_OUTCOME = "unknown_outcome"
    ACCESSIBILITY_ACTION_REFUSED = "accessibility_action_refused"
    FOREGROUND_TAKEOVER_REQUIRED = "foreground_takeover_required"
    REQUIRES_ACTIVE_FOREGROUND_TAKEOVER = "requires_active_foreground_takeover"
    BACKGROUND_ACTION_UNSUPPORTED = "background_action_unsupported"
    USER_ACTIVITY_PAUSED = "user_activity_paused"
    OBSERVATION_REQUIRED = "observation_required"
    SIDECAR_FAILED = "sidecar_failed"


class ComputerInteractionMode(str, Enum):
    BACKGROUND = "background"
    FOREGROUND_TAKEOVER = "foreground_takeover"


class ActionEffectVerification(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    NOOP = "noop"


class ComputerSnapshotTextDetailMode(str, Enum):
    OFF = "off"
    ON = "on"


class TakeoverRestoration(str, Enum):
    NOT_STARTED = "not_started"
    RESTORED = "restored"
    PRESERVED_USER_FOCUS = "preserved_user_focus"
    RESTORE_FAILED = "restore_failed"


DISPATCH_ACTION_CLASSES = frozenset({"press", "text", "click", "double_click", "scroll", "drag"})
COMPUTER_OPERATIONS = frozenset({
    "status", "apps", "select", "snapshot", "snapshot_subtree", "get_app_state", "plan_actions",
    "takeover_begin", "act", "fragment_stage_commit", "takeover_end",
})
FRAGMENT_BACKENDS = frozenset({
    "ax_press", "ax_increment", "ax_decrement", "ax_selected_text", "wait",
    "pid_pointer", "foreground_pointer", "foreground_keyboard",
})
MAX_FRAGMENT_STAGES = 32
MAX_FRAGMENT_ACTIONS = 64
MAX_FRAGMENT_WALL_CLOCK_MS = 120_000


def _validate_canonical_key_chord(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_KEY_CHORD_SCALARS
        or value != value.lower()
    ):
        raise ValueError("key chord must be a canonical bounded string")
    parts = value.split("+")
    key = parts[-1]
    modifiers = parts[:-1]
    if (
        key not in KEY_CHORD_KEYS
        or len(set(modifiers)) != len(modifiers)
        or any(modifier not in KEY_CHORD_MODIFIER_ORDER for modifier in modifiers)
        or modifiers != [modifier for modifier in KEY_CHORD_MODIFIER_ORDER if modifier in modifiers]
        or (key == "v" and "command" in modifiers)
    ):
        raise ValueError("key chord must be a canonical bounded string")
    return value


def _validate_fragment_hash(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character hex hash")


def _exact_integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class ForegroundFragmentActionRequirement:
    backend: str
    action_class: str | None
    intent: str | None

    def __post_init__(self) -> None:
        if self.backend not in FRAGMENT_BACKENDS:
            raise ValueError("fragment requirement backend is invalid")
        valid = (
            (self.backend == "ax_press" and self.action_class == "press" and self.intent is None)
            or (
                self.backend in {"ax_increment", "ax_decrement"}
                and self.action_class == "scroll"
                and self.intent is None
            )
            or (self.backend == "ax_selected_text" and self.action_class == "text" and self.intent is None)
            or (self.backend == "wait" and self.action_class is None and self.intent is None)
            or (
                self.backend in {"pid_pointer", "foreground_pointer"}
                and self.action_class in {"click", "double_click", "scroll", "drag"}
                and self.intent == f"pointer:{self.action_class}"
            )
            or self._valid_keyboard_requirement()
        )
        if not valid:
            raise ValueError("fragment requirement backend/action/intent is inconsistent")

    def _valid_keyboard_requirement(self) -> bool:
        if self.backend != "foreground_keyboard" or self.action_class != "text":
            return False
        if self.intent == "text_entry":
            return True
        if not isinstance(self.intent, str) or not self.intent.startswith("key_chord:"):
            return False
        try:
            _validate_canonical_key_chord(self.intent.removeprefix("key_chord:"))
        except ValueError:
            return False
        return True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ForegroundFragmentActionRequirement:
        _require_object(value, "fragment requirement")
        _require_exact_payload(value, {"backend", "action_class", "intent"}, "fragment requirement")
        return cls(value["backend"], value["action_class"], value["intent"])

    def to_mapping(self) -> dict[str, Any]:
        return {"backend": self.backend, "action_class": self.action_class, "intent": self.intent}


@dataclass(frozen=True)
class ForegroundFragmentStageDeclaration:
    stage_hash: str
    expected_action_count: int
    requirements: tuple[ForegroundFragmentActionRequirement, ...]

    def __post_init__(self) -> None:
        _validate_fragment_hash(self.stage_hash, "stage_hash")
        _exact_integer(
            self.expected_action_count,
            "expected_action_count",
            minimum=1,
            maximum=MAX_FRAGMENT_ACTIONS,
        )
        if (
            not isinstance(self.requirements, tuple)
            or len(self.requirements) != self.expected_action_count
            or not all(isinstance(item, ForegroundFragmentActionRequirement) for item in self.requirements)
        ):
            raise ValueError("fragment stage requirements must match expected action count")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ForegroundFragmentStageDeclaration:
        _require_object(value, "fragment stage")
        _require_exact_payload(
            value,
            {"stage_hash", "expected_action_count", "requirements"},
            "fragment stage",
        )
        raw = value["requirements"]
        if not isinstance(raw, list):
            raise TypeError("fragment stage requirements must be an array")
        return cls(
            value["stage_hash"],
            value["expected_action_count"],
            tuple(ForegroundFragmentActionRequirement.from_mapping(item) for item in raw),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "stage_hash": self.stage_hash,
            "expected_action_count": self.expected_action_count,
            "requirements": [item.to_mapping() for item in self.requirements],
        }


@dataclass(frozen=True)
class ForegroundFragmentDeclaration:
    fragment_hash: str
    stages: tuple[ForegroundFragmentStageDeclaration, ...]
    max_actions: int
    wall_clock_limit_ms: int
    restore_previous_focus: bool

    def __post_init__(self) -> None:
        _validate_fragment_hash(self.fragment_hash, "fragment_hash")
        if (
            not isinstance(self.stages, tuple)
            or not 1 <= len(self.stages) <= MAX_FRAGMENT_STAGES
            or not all(isinstance(item, ForegroundFragmentStageDeclaration) for item in self.stages)
            or len({item.stage_hash for item in self.stages}) != len(self.stages)
        ):
            raise ValueError("fragment stages must be 1..32 unique bounded stages")
        _exact_integer(self.max_actions, "max_actions", minimum=1, maximum=MAX_FRAGMENT_ACTIONS)
        _exact_integer(
            self.wall_clock_limit_ms,
            "wall_clock_limit_ms",
            minimum=1,
            maximum=MAX_FRAGMENT_WALL_CLOCK_MS,
        )
        if sum(item.expected_action_count for item in self.stages) > self.max_actions:
            raise ValueError("fragment stage actions exceed max_actions")
        if not isinstance(self.restore_previous_focus, bool):
            raise TypeError("restore_previous_focus must be a boolean")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ForegroundFragmentDeclaration:
        _require_object(value, "fragment")
        _require_exact_payload(
            value,
            {"fragment_hash", "stages", "max_actions", "wall_clock_limit_ms", "restore_previous_focus"},
            "fragment",
        )
        raw_stages = value["stages"]
        if not isinstance(raw_stages, list):
            raise TypeError("fragment stages must be an array")
        return cls(
            value["fragment_hash"],
            tuple(ForegroundFragmentStageDeclaration.from_mapping(item) for item in raw_stages),
            value["max_actions"],
            value["wall_clock_limit_ms"],
            value["restore_previous_focus"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "fragment_hash": self.fragment_hash,
            "stages": [item.to_mapping() for item in self.stages],
            "max_actions": self.max_actions,
            "wall_clock_limit_ms": self.wall_clock_limit_ms,
            "restore_previous_focus": self.restore_previous_focus,
        }


@dataclass(frozen=True)
class FragmentStageAuthority:
    fragment_hash: str
    stage_index: int
    stage_hash: str
    input_snapshot_id: str

    def __post_init__(self) -> None:
        _validate_fragment_hash(self.fragment_hash, "fragment_hash")
        _exact_integer(self.stage_index, "stage_index", minimum=0, maximum=MAX_FRAGMENT_STAGES - 1)
        _validate_fragment_hash(self.stage_hash, "stage_hash")
        _validate_reference(self.input_snapshot_id, "input_snapshot_id")


@dataclass(frozen=True)
class ForegroundFragmentPlanRequest:
    kind: str
    authority: FragmentStageAuthority
    takeover_ref: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"initial", "continuing"}:
            raise ValueError("fragment plan kind is invalid")
        if not isinstance(self.authority, FragmentStageAuthority):
            raise TypeError("fragment plan authority is invalid")
        if self.kind == "initial":
            if self.authority.stage_index != 0 or self.takeover_ref is not None:
                raise ValueError("initial fragment plan must bind stage zero")
        else:
            _validate_reference(self.takeover_ref, "takeover_ref")
            if self.authority.stage_index == 0:
                raise ValueError("continuing fragment plan must advance stage index")

    @classmethod
    def stage(
        cls,
        authority: FragmentStageAuthority,
        takeover_ref: str | None = None,
    ) -> ForegroundFragmentPlanRequest:
        return cls("initial" if takeover_ref is None else "continuing", authority, takeover_ref)

    def to_wire_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "fragment_hash": self.authority.fragment_hash,
            "stage_index": self.authority.stage_index,
            "stage_hash": self.authority.stage_hash,
        }
        if self.takeover_ref is not None:
            fields["takeover_ref"] = self.takeover_ref
        return fields

    def to_mapping(self) -> dict[str, Any]:
        return {"kind": self.kind, "authority": self.authority, "takeover_ref": self.takeover_ref}


@dataclass(frozen=True)
class FragmentStageCommit:
    takeover_ref: str
    fragment_hash: str
    stage_index: int
    stage_hash: str
    plan_ref: str
    fresh_snapshot_id: str
    postcondition_verified: bool

    def __post_init__(self) -> None:
        _validate_reference(self.takeover_ref, "takeover_ref")
        _validate_fragment_hash(self.fragment_hash, "fragment_hash")
        _exact_integer(self.stage_index, "stage_index", minimum=0, maximum=MAX_FRAGMENT_STAGES - 1)
        _validate_fragment_hash(self.stage_hash, "stage_hash")
        _validate_reference(self.plan_ref, "plan_ref")
        _validate_reference(self.fresh_snapshot_id, "fresh_snapshot_id")
        if not isinstance(self.postcondition_verified, bool):
            raise TypeError("postcondition_verified must be a boolean")

    def to_mapping(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class FragmentStageCommitResult:
    terminal: bool
    restoration: TakeoverRestoration | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.terminal, bool):
            raise TypeError("fragment commit terminal must be a boolean")
        if self.terminal != (self.restoration is not None):
            raise ValueError("terminal fragment commit must report restoration exactly once")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> FragmentStageCommitResult:
        _require_object(value, "fragment commit result")
        terminal = value.get("terminal")
        if not isinstance(terminal, bool):
            raise TypeError("fragment commit terminal must be a boolean")
        expected = {"terminal", "restoration"} if terminal is True else {"terminal"}
        _require_exact_payload(value, expected, "fragment commit result")
        restoration = TakeoverRestoration(value["restoration"]) if terminal is True else None
        return cls(terminal, restoration)


@dataclass(frozen=True)
class TakeoverOutcome:
    started: bool
    restoration: TakeoverRestoration

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TakeoverOutcome:
        _require_object(value, "takeover")
        if set(value) != {"started", "restoration"}:
            raise ValueError("takeover requires started and restoration")
        started = value["started"]
        if not isinstance(started, bool):
            raise ValueError("takeover.started must be a boolean")  # noqa: TRY004 - wire validation
        try:
            restoration = TakeoverRestoration(value["restoration"])
        except (TypeError, ValueError) as exc:
            raise ValueError("takeover.restoration is invalid") from exc
        if not started and restoration is not TakeoverRestoration.NOT_STARTED:
            raise ValueError("takeover not started must be not_started")
        if started and restoration is TakeoverRestoration.NOT_STARTED:
            raise ValueError("started takeover must have a terminal restoration")
        return cls(started=started, restoration=restoration)

    def to_mapping(self) -> dict[str, Any]:
        return {"started": self.started, "restoration": self.restoration.value}


@dataclass(frozen=True)
class DispatchPlanSummary:
    plan_ref: str
    interaction_mode: ComputerInteractionMode
    requires_takeover: bool
    reason: str
    action_classes: tuple[str, ...]
    pid_action_classes: tuple[str, ...]
    last_acknowledged_action: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DispatchPlanSummary:
        _require_object(value, "dispatch plan")
        required = {
            "plan_ref", "interaction_mode", "requires_takeover", "reason",
            "action_classes", "pid_action_classes", "last_acknowledged_action",
        }
        if set(value) != required:
            raise ValueError("dispatch plan has missing or unknown fields")
        _validate_reference(value["plan_ref"], "plan_ref")
        try:
            interaction_mode = ComputerInteractionMode(value["interaction_mode"])
        except (TypeError, ValueError) as exc:
            raise ValueError("interaction_mode is invalid") from exc
        if not isinstance(value["requires_takeover"], bool):
            raise ValueError("requires_takeover must be a boolean")  # noqa: TRY004 - wire validation
        if not isinstance(value["reason"], str) or not value["reason"]:
            raise ValueError("reason must be a non-empty string")
        raw_action_classes = value["action_classes"]
        if (
            not isinstance(raw_action_classes, list)
            or len(raw_action_classes) > len(DISPATCH_ACTION_CLASSES)
            or not all(isinstance(label, str) and label in DISPATCH_ACTION_CLASSES for label in raw_action_classes)
            or len(set(raw_action_classes)) != len(raw_action_classes)
        ):
            raise ValueError("action_classes must contain unique supported capability labels")
        raw_pid_action_classes = value["pid_action_classes"]
        if (
            not isinstance(raw_pid_action_classes, list)
            or len(raw_pid_action_classes) > len(DISPATCH_ACTION_CLASSES)
            or not all(
                isinstance(label, str) and label in DISPATCH_ACTION_CLASSES
                for label in raw_pid_action_classes
            )
            or len(set(raw_pid_action_classes)) != len(raw_pid_action_classes)
            or not set(raw_pid_action_classes).issubset(raw_action_classes)
        ):
            raise ValueError(
                "pid_action_classes must contain a unique supported subset of action_classes"
            )
        acknowledgement = value["last_acknowledged_action"]
        if isinstance(acknowledgement, bool) or not isinstance(acknowledgement, int) or acknowledgement != -1:
            raise ValueError("planning last_acknowledged_action must be -1")
        return cls(
            plan_ref=value["plan_ref"],
            interaction_mode=interaction_mode,
            requires_takeover=value["requires_takeover"],
            reason=value["reason"],
            action_classes=tuple(raw_action_classes),
            pid_action_classes=tuple(raw_pid_action_classes),
            last_acknowledged_action=acknowledgement,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "plan_ref": self.plan_ref,
            "interaction_mode": self.interaction_mode.value,
            "requires_takeover": self.requires_takeover,
            "reason": self.reason,
            "action_classes": list(self.action_classes),
            "pid_action_classes": list(self.pid_action_classes),
            "last_acknowledged_action": self.last_acknowledged_action,
        }


@dataclass(frozen=True)
class ComputerAction:
    type: str
    x: float | None = None
    y: float | None = None
    end_x: float | None = None
    end_y: float | None = None
    text: str | None = None
    key: str | None = None
    delta_x: float | None = None
    delta_y: float | None = None
    duration_ms: int | None = None
    element_ref: str | None = None
    target_element_ref: str | None = None
    element_index: int | None = None
    modifiers: tuple[str, ...] = ()
    checked: bool | None = None
    replace: bool | None = None

    def __post_init__(self) -> None:
        if self.replace is not None and (
            self.replace is not True or self.type != "type" or self.element_ref is None
            or self.modifiers or any(getattr(self, key) is not None for key in (
                "x", "y", "end_x", "end_y", "key", "delta_x", "delta_y", "duration_ms",
                "element_index", "target_element_ref", "checked",
            ))
        ):
            raise ValueError("replace:true requires only type, text and an exact element_ref")
        if self.checked is not None and (
            type(self.checked) is not bool or self.type != "click"
            or (self.element_ref is None and self.element_index is None)
            or self.x is not None or self.y is not None or self.modifiers
        ):
            raise ValueError("checked requires an unmodified click on an exact element_ref or element_index")
        if self.type not in {"click", "right_click", "double_click", "type", "keypress", "scroll", "drag", "wait"}:
            raise ValueError(f"unsupported action type: {self.type}")
        for name in ("x", "y", "end_x", "end_y", "delta_x", "delta_y"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or abs(value) > MAX_COORDINATE_MAGNITUDE
            ):
                raise ValueError(f"{name} must be finite and within +/-1,000,000")
        if self.text is not None and (
            not isinstance(self.text, str)
            or not _has_valid_unicode_scalars(self.text, MAX_TEXT_CHARACTERS)
        ):
            raise ValueError("text must contain at most 20,000 Unicode scalars and no surrogates")
        if self.element_ref is not None and (
            not isinstance(self.element_ref, str)
            or not self.element_ref
            or not _has_valid_unicode_scalars(self.element_ref, MAX_ELEMENT_REF_SCALARS)
        ):
            raise ValueError("element_ref must be a non-empty string of at most 256 Unicode scalars")
        if self.target_element_ref is not None and (
            not isinstance(self.target_element_ref, str)
            or not self.target_element_ref
            or not _has_valid_unicode_scalars(self.target_element_ref, MAX_ELEMENT_REF_SCALARS)
        ):
            raise ValueError(
                "target_element_ref must be a non-empty string of at most 256 Unicode scalars"
            )
        if self.element_ref is not None and self.target_element_ref is not None:
            raise ValueError("element_ref and target_element_ref cannot be mixed")
        if self.element_index is not None and (
            isinstance(self.element_index, bool)
            or not isinstance(self.element_index, int)
            or self.element_index < 1
        ):
            raise ValueError("element_index must be a positive integer")
        if self.element_index is not None and (
            self.element_ref is not None or self.target_element_ref is not None
        ):
            raise ValueError("element_index cannot be mixed with element_ref or target_element_ref")
        if self.element_index is not None and self.type not in {"click", "right_click", "double_click", "scroll"}:
            raise ValueError("element_index is only valid for click, right_click, double_click, and scroll")
        if self.key is not None and (
            not isinstance(self.key, str)
            or not self.key
            or not _has_valid_unicode_scalars(self.key, MAX_KEY_SCALARS)
        ):
            raise ValueError("key must be a non-empty string of at most 64 Unicode scalars")
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or not 0 <= self.duration_ms <= MAX_DURATION_MS
        ):
            raise ValueError("duration_ms must be an integer between 0 and 10,000")
        if (
            not isinstance(self.modifiers, tuple)
            or len(self.modifiers) > 8
            or not all(isinstance(item, str) and item in ALLOWED_MODIFIERS for item in self.modifiers)
            or len(set(self.modifiers)) != len(self.modifiers)
        ):
            raise ValueError("modifiers must be unique supported modifier names")
        if self.type in {"click", "right_click", "double_click", "scroll"} and (self.x is None) != (self.y is None):
            raise ValueError(f"{self.type} x/y coordinates must be provided together")
        has_pointer_coordinates = self.type == "drag" or self.x is not None or self.y is not None
        if self.type in {"click", "right_click", "double_click", "scroll", "drag"}:
            if has_pointer_coordinates:
                if self.element_ref is not None or self.element_index is not None:
                    raise ValueError("raw pointer coordinates cannot be mixed with element_ref or element_index")
            elif (
                (self.element_ref is None and self.element_index is None)
                or self.target_element_ref is not None
            ):
                raise ValueError(
                    f"{self.type} without coordinates requires element_ref or element_index"
                )
        elif self.target_element_ref is not None:
            raise ValueError("target_element_ref is only valid for raw pointer coordinates")
        if self.type == "type" and self.text is None:
            raise ValueError("type requires text")
        if self.type == "keypress" and not self.key:
            raise ValueError("keypress requires key")
        if self.type == "keypress":
            assert self.key is not None
            canonical = {
                "arrowleft": "left", "arrowright": "right", "arrowup": "up",
                "arrowdown": "down", "enter": "return", "esc": "escape",
                "backspace": "delete",
            }.get(self.key.lower(), self.key.lower())
            supported = set("abcdefghijklmnopqrstuvwxyz0123456789=-][';\\,/.") | {
                "return", "tab", "space", "delete", "escape", "left", "right", "down", "up",
            }
            if canonical not in supported:
                raise ValueError(
                    "unsupported key name; supported: a-z, 0-9, = - ] [ ' ; \\ , / ., "
                    "return (enter), tab, space, delete (backspace), escape (esc), "
                    "left/right/up/down (ArrowLeft/ArrowRight/ArrowUp/ArrowDown); "
                    "pass modifiers separately"
                )
            object.__setattr__(self, "key", canonical)
        if (
            self.type == "keypress"
            and self.key is not None
            and self.key.lower() == "v"
            and "command" in self.modifiers
        ):
            raise ValueError("clipboard paste is not supported")
        if self.type == "scroll" and self.delta_x is None and self.delta_y is None:
            raise ValueError("scroll requires delta_x or delta_y")
        if self.type == "drag" and None in (self.x, self.y, self.end_x, self.end_y):
            raise ValueError("drag requires start and end coordinates")
        if self.type == "wait" and self.duration_ms is None:
            raise ValueError("wait requires duration_ms")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ComputerAction:
        _require_object(value, "action")
        allowed = {
            "type", "x", "y", "end_x", "end_y", "text", "key", "delta_x",
            "delta_y", "duration_ms", "element_ref", "target_element_ref",
            "element_index", "modifiers", "checked", "replace",
        }
        _reject_unknown_fields(value, allowed, "action")
        if "replace" in value:
            if value["replace"] is not True:
                raise ValueError("replace must be true when specified")
            _reject_unknown_fields(value, {"type", "text", "element_ref", "replace"}, "replacement action")
        if "type" not in value:
            raise ValueError("action requires type")
        kwargs = dict(value)
        if "modifiers" in kwargs:
            modifiers = kwargs["modifiers"]
            if not isinstance(modifiers, list):
                raise ValueError("modifiers must be an array")
            kwargs["modifiers"] = tuple(modifiers)
        return cls(**kwargs)

    def to_mapping(self) -> dict[str, Any]:
        result = {key: value for key, value in self.__dict__.items() if value not in (None, ())}
        if self.modifiers:
            result["modifiers"] = list(self.modifiers)
        return result


@dataclass(frozen=True)
class KeyboardFailureDiagnostics:
    """Bounded execution evidence; never proof of an application's effect."""

    stage: str
    cause: str
    input_may_have_started: bool
    cleanup_failed: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> KeyboardFailureDiagnostics:
        _require_object(value, "input diagnostics")
        fields = {"stage", "cause", "input_may_have_started", "cleanup_failed"}
        if set(value) != fields:
            raise ValueError("input diagnostics require only the fixed execution fields")
        if not isinstance(value["stage"], str) or value["stage"] not in {
            "before_key_down", "key_down", "before_key_up", "key_up", "after_key_up",
        }:
            raise ValueError("input diagnostics have an unknown stage")
        if not isinstance(value["cause"], str) or value["cause"] not in {
            "protocol_mismatch", "permission_denied", "target_gone", "target_not_frontmost",
            "stale_snapshot", "out_of_bounds", "secure_target", "input_focus_required",
            "action_timeout", "helper_failed", "unknown_outcome", "user_activity_paused",
        }:
            raise ValueError("input diagnostics have an unknown cause")
        if any(type(value[key]) is not bool for key in ("input_may_have_started", "cleanup_failed")):
            raise ValueError("input diagnostics execution flags must be booleans")
        if not value["input_may_have_started"] and (
            value["cleanup_failed"] or value["stage"] in {"before_key_up", "key_up", "after_key_up"}
        ):
            raise ValueError("input diagnostics contradict the input boundary")
        return cls(**value)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "stage": self.stage, "cause": self.cause,
            "input_may_have_started": self.input_may_have_started,
            "cleanup_failed": self.cleanup_failed,
        }


@dataclass(frozen=True)
class ComputerActionOutcome:
    index: int
    ok: bool
    error_code: ComputerErrorCode | None = None
    effect_verification: ActionEffectVerification | None = None
    input_diagnostics: KeyboardFailureDiagnostics | None = None
    observation_required: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, expected_index: int) -> ComputerActionOutcome:
        _require_object(value, "action outcome")
        _reject_unknown_fields(
            value,
            {"index", "ok", "error_code", "effect_verification", "input_diagnostics", "observation_required"},
            "action outcome",
        )
        if "index" not in value or "ok" not in value:
            raise ValueError("action outcome requires index and ok")
        index = value["index"]
        ok = value["ok"]
        if isinstance(index, bool) or not isinstance(index, int) or index != expected_index:
            raise ValueError("action outcome indices must be contiguous from zero")
        if not isinstance(ok, bool):
            raise ValueError("action outcome ok must be a boolean")  # noqa: TRY004 - protocol validation API uses ValueError
        raw_error = value.get("error_code")
        observation_required = value.get("observation_required", False)
        if "observation_required" in value and (observation_required is not True or not ok):
            raise ValueError("observation checkpoint requires a successful outcome and literal true")
        raw_effect = value.get("effect_verification")
        input_diagnostics = None
        if "input_diagnostics" in value:
            if ok:
                raise ValueError("successful action outcome must omit failure diagnostics")
            input_diagnostics = KeyboardFailureDiagnostics.from_mapping(value["input_diagnostics"])
        if ok:
            if "error_code" in value:
                raise ValueError("successful action outcome must omit error_code")
            error_code = None
            if "effect_verification" in value:
                if not isinstance(raw_effect, str):
                    raise ValueError("action outcome effect_verification must be a string")
                try:
                    effect_verification = ActionEffectVerification(raw_effect)
                except ValueError as exc:
                    raise ValueError("action outcome has unknown effect_verification") from exc
            else:
                effect_verification = None
        else:
            if "effect_verification" in value:
                raise ValueError("failed action outcome must omit effect_verification")
            if not isinstance(raw_error, str):
                raise ValueError("failed action outcome requires error_code")
            try:
                error_code = ComputerErrorCode(raw_error)
            except ValueError as exc:
                raise ValueError("failed action outcome has unknown error_code") from exc
            if error_code is ComputerErrorCode.UNSUPPORTED_PLATFORM:
                raise ValueError("failed action outcome has an invalid platform error")
            effect_verification = None
        return cls(
            index=index,
            ok=ok,
            error_code=error_code,
            effect_verification=effect_verification,
            input_diagnostics=input_diagnostics,
            observation_required=observation_required,
        )

    def to_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {"index": self.index, "ok": self.ok}
        if self.error_code is not None:
            value["error_code"] = self.error_code.value
        if self.effect_verification is not None:
            value["effect_verification"] = self.effect_verification.value
        if self.input_diagnostics is not None:
            value["input_diagnostics"] = self.input_diagnostics.to_mapping()
        if self.observation_required:
            value["observation_required"] = True
        return value


@dataclass(frozen=True)
class ComputerActResult:
    outcomes: tuple[ComputerActionOutcome, ...]
    last_acknowledged_action: int

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        action_count: int,
        response_ok: bool,
        response_error: ComputerError | None,
        interaction_mode: ComputerInteractionMode | None = None,
    ) -> ComputerActResult:
        if not isinstance(value, Mapping):
            raise ValueError("act result must be an object")  # noqa: TRY004 - wire validation
        required = {"outcomes", "last_acknowledged_action"}
        _reject_unknown_fields(value, required, "act result")
        if set(value) != required:
            raise ValueError("act result requires its mode-specific fields")
        raw_outcomes = value["outcomes"]
        acknowledgement = value["last_acknowledged_action"]
        if not isinstance(raw_outcomes, list) or len(raw_outcomes) > action_count:
            raise ValueError("act result outcomes must be an action-bounded array")
        if (
            isinstance(acknowledgement, bool)
            or not isinstance(acknowledgement, int)
            or not -1 <= acknowledgement < max(action_count, 1)
        ):
            raise ValueError("act result acknowledgement is out of range")
        outcomes = tuple(
            ComputerActionOutcome.from_mapping(outcome, expected_index=index)
            for index, outcome in enumerate(raw_outcomes)
        )
        failed = [outcome for outcome in outcomes if not outcome.ok]
        if any(outcome.observation_required for outcome in outcomes[:-1]):
            raise ValueError("act result must stop at its observation checkpoint")
        if failed and (len(failed) != 1 or outcomes[-1].ok):
            raise ValueError("act result must stop at its first failed outcome")
        successful_prefix = len(outcomes) - len(failed) - 1
        if acknowledgement != successful_prefix:
            raise ValueError("act result acknowledgement contradicts its successful prefix")
        if response_ok:
            if response_error is not None or failed or len(outcomes) != action_count:
                raise ValueError("successful act response must acknowledge every action")
        elif response_error is not None and response_error.code is ComputerErrorCode.OBSERVATION_REQUIRED:
            if (
                not outcomes or failed or len(outcomes) >= action_count
                or not outcomes[-1].observation_required
            ):
                raise ValueError("observation checkpoint requires an acknowledged partial prefix")
        else:
            if response_error is None:
                raise ValueError("failed act response requires an error")
            if response_error.code is ComputerErrorCode.UNKNOWN_OUTCOME:
                if not outcomes:
                    raise ValueError("unknown act response requires its unacknowledged current outcome")
                current = outcomes[-1]
                if (
                    current.ok
                    or current.error_code is not ComputerErrorCode.UNKNOWN_OUTCOME
                    or current.index != acknowledgement + 1
                    or current.index >= action_count
                ):
                    raise ValueError("unknown act response contradicts its current outcome")
            if outcomes:
                final = outcomes[-1]
                paused_after_input = (
                    response_error.code is ComputerErrorCode.USER_ACTIVITY_PAUSED
                    and final.error_code is ComputerErrorCode.UNKNOWN_OUTCOME
                    and final.index == acknowledgement + 1
                    and final.index < action_count
                )
                if final.ok or (
                    final.error_code is not response_error.code
                    and not paused_after_input
                ):
                    raise ValueError("failed act response contradicts its final outcome")
        # Restoration is deliberately absent here. It can only be determined
        # after the caller captures fresh evidence and sends takeover_end.
        return cls(outcomes=outcomes, last_acknowledged_action=acknowledgement)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "outcomes": [outcome.to_mapping() for outcome in self.outcomes],
            "last_acknowledged_action": self.last_acknowledged_action,
        }


@dataclass(frozen=True)
class ComputerRequest:
    request_id: str
    operation: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(self.operation, str) or not self.operation:
            raise ValueError("operation must be a non-empty string")
        if self.operation not in COMPUTER_OPERATIONS:
            raise ValueError("operation is not supported by protocol v4")
        _require_object(self.payload, "payload")
        if not _is_current_protocol_version(self.protocol_version):
            raise ValueError("protocol_version must match the current protocol")
        _validate_cooperative_request_payload(self.operation, self.payload)
        actions = self.payload.get("actions")
        if actions is not None:
            if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
                raise ValueError("actions must contain at most 20 actions")
            for action in actions:
                if isinstance(action, ComputerAction):
                    continue
                ComputerAction.from_mapping(action)

@dataclass(frozen=True)
class ComputerSnapshot:
    snapshot_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            raise ValueError("snapshot_id must be a non-empty string")
        _require_object(self.payload, "snapshot.payload")


@dataclass(frozen=True)
class ComputerError:
    code: ComputerErrorCode
    message: str


@dataclass(frozen=True)
class ComputerResponse:
    request_id: str
    ok: bool
    result: Mapping[str, Any] | None = None
    snapshot: ComputerSnapshot | None = None
    error: ComputerError | None = None
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(self.ok, bool):
            raise ValueError("ok must be a boolean")  # noqa: TRY004 - protocol validation API uses ValueError
        if self.result is not None:
            _require_object(self.result, "result")
        if not _is_current_protocol_version(self.protocol_version):
            raise ValueError("protocol_version must match the current protocol")
        if self.snapshot is not None and not isinstance(self.snapshot, ComputerSnapshot):
            raise ValueError("snapshot must be a ComputerSnapshot")
        if self.error is not None and not isinstance(self.error, ComputerError):
            raise ValueError("error must be a ComputerError")


def encode_request(request: ComputerRequest) -> str:
    if not isinstance(request, ComputerRequest):
        raise TypeError("request must be a ComputerRequest")
    payload = dict(request.payload)
    fragment = payload.get("fragment")
    if request.operation == "plan_actions" and fragment is not None:
        payload.pop("fragment")
        if not isinstance(fragment, ForegroundFragmentPlanRequest):
            raise TypeError("fragment must be a ForegroundFragmentPlanRequest")
        payload.update(fragment.to_wire_fields())
    return json.dumps(
        {
            "protocol_version": request.protocol_version,
            "request_id": request.request_id,
            "operation": request.operation,
            "payload": _to_json_value(payload),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def decode_response(encoded: str | bytes) -> ComputerResponse:
    raw = encoded.encode("utf-8") if isinstance(encoded, str) else encoded
    if not isinstance(raw, bytes):
        raise ValueError("response must be UTF-8 JSON text")  # noqa: TRY004 - protocol validation API uses ValueError
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response exceeds 4 MiB")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("response must be valid JSON without duplicate fields") from exc
    _require_object(data, "response")
    _reject_unknown_fields(data, {"protocol_version", "request_id", "ok", "result", "snapshot", "error"}, "response")
    for field_name in ("protocol_version", "request_id", "ok"):
        if field_name not in data:
            raise ValueError(f"response requires {field_name}")
    request_id = data["request_id"]
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id must be a non-empty string")
    if not _is_current_protocol_version(data["protocol_version"]):
        return ComputerResponse(
            request_id=request_id,
            ok=False,
            error=ComputerError(ComputerErrorCode.PROTOCOL_MISMATCH, "protocol version mismatch"),
        )
    if not isinstance(data["ok"], bool):
        raise ValueError("ok must be a boolean")  # noqa: TRY004 - protocol validation API uses ValueError
    result = data.get("result")
    if result is not None:
        _require_object(result, "result")
    snapshot = _decode_snapshot(data.get("snapshot")) if "snapshot" in data else None
    error = _decode_error(data.get("error")) if "error" in data else None
    if not data["ok"] and error is None:
        error = ComputerError(ComputerErrorCode.PROTOCOL_MISMATCH, "failed response omitted error")
    if data["ok"] and error is not None and not _is_known_error_code(data["error"]["code"]):
        return ComputerResponse(request_id=request_id, ok=False, result=result, snapshot=snapshot, error=error)
    return ComputerResponse(request_id=request_id, ok=data["ok"], result=result, snapshot=snapshot, error=error)


def _decode_snapshot(value: Any) -> ComputerSnapshot:
    _require_object(value, "snapshot")
    _reject_unknown_fields(value, {"snapshot_id", "payload"}, "snapshot")
    if "snapshot_id" not in value:
        raise ValueError("snapshot requires snapshot_id")
    if "payload" not in value:
        raise ValueError("snapshot requires payload")
    snapshot_id = value["snapshot_id"]
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id
        or not _has_valid_unicode_scalars(snapshot_id, MAX_ELEMENT_REF_SCALARS)
    ):
        raise ValueError("snapshot_id must be a non-empty bounded string")
    payload = value["payload"]
    validate_snapshot_payload(payload, snapshot_id=snapshot_id)
    return ComputerSnapshot(snapshot_id, payload)


def validate_snapshot_payload(value: Any, *, snapshot_id: str | None = None) -> None:
    """Validate the exact default or text-detail native snapshot schema."""
    _require_object(value, "snapshot payload")
    required = {
        "image_artifact",
        "logical_size",
        "pixel_size",
        "backing_scale",
        "capture_bounds",
        "ax_tree",
    }
    detail_fields = {"text_detail_artifact", "text_detail_metadata"}
    allowed = required | {
        "display_id",
        "target_window_bounds",
        "cursor_visible",
        "virtual_pointer",
        "has_default_button",
        "default_button_element_ref",
    } | detail_fields
    _reject_unknown_fields(value, allowed, "snapshot payload")
    missing = required - set(value)
    if missing:
        raise ValueError(
            f"snapshot payload is missing fields: {', '.join(sorted(missing))}"
        )
    _validate_plain_artifact_name(
        value["image_artifact"], "snapshot payload image_artifact"
    )
    _validate_size(value["logical_size"], "snapshot payload logical_size")
    _validate_size(value["pixel_size"], "snapshot payload pixel_size")
    scale = value["backing_scale"]
    if not _positive_finite_number(scale):
        raise ValueError("snapshot payload backing_scale must be positive and finite")
    _validate_bounds(value["capture_bounds"], "snapshot payload capture_bounds")
    if ("display_id" in value) != ("target_window_bounds" in value):
        raise ValueError(
            "snapshot payload display_id and target_window_bounds must appear together"
        )
    if "target_window_bounds" in value:
        _validate_bounds(value["target_window_bounds"], "snapshot payload target_window_bounds")
    if "display_id" in value and (
        isinstance(value["display_id"], bool)
        or not isinstance(value["display_id"], int)
        or value["display_id"] <= 0
    ):
        raise ValueError("snapshot payload display_id must be a positive integer")
    if "cursor_visible" in value and not isinstance(value["cursor_visible"], bool):
        raise ValueError("snapshot payload cursor_visible must be a boolean")
    if "virtual_pointer" in value:
        # 虚拟游标位置只在 foreground takeover 期间存在（VirtualCursorClient 仅在游标有
        # 位置时发这个键）。登记进白名单时必须连带校形状，否则只是把漏登换成了放任。
        point = value["virtual_pointer"]
        _require_object(point, "snapshot payload virtual_pointer")
        _reject_unknown_fields(point, {"x", "y"}, "snapshot payload virtual_pointer")
        if set(point) != {"x", "y"} or not all(_finite_number(point[key]) for key in ("x", "y")):
            raise ValueError(
                "snapshot payload virtual_pointer requires finite x and y"
            )
    if not isinstance(value["ax_tree"], Mapping):
        raise ValueError("snapshot payload ax_tree must be an object")  # noqa: TRY004 - wire validation
    if "has_default_button" in value and not isinstance(value["has_default_button"], bool):
        raise ValueError("snapshot payload default button presence must be a boolean")
    if "default_button_element_ref" in value:
        reference = value["default_button_element_ref"]
        if value.get("has_default_button") is not True:
            raise ValueError("snapshot payload default button ref requires presence")
        if (
            not isinstance(reference, str)
            or not reference
            or not _has_valid_unicode_scalars(reference, MAX_ELEMENT_REF_SCALARS)
            or not _snapshot_tree_contains_native_reference(
                value["ax_tree"], reference
            )
        ):
            raise ValueError("snapshot payload default button ref must be registered")
    if len(detail_fields.intersection(value)) == 1:
        raise ValueError("snapshot text detail artifact and metadata must appear together")
    if detail_fields.issubset(value):
        _validate_text_detail_artifact_pair(
            value["image_artifact"],
            value["text_detail_artifact"],
            value["text_detail_metadata"],
            snapshot_id=snapshot_id,
        )


def _snapshot_tree_contains_native_reference(
    tree: Mapping[str, Any],
    reference: str,
) -> bool:
    stack: list[tuple[Mapping[str, Any], int]] = [(tree, 0)]
    remaining = MAX_NATIVE_AX_NODES
    while stack and remaining > 0:
        node, depth = stack.pop()
        remaining -= 1
        if node.get("element_ref") == reference:
            return True
        children = node.get("children")
        if depth + 1 >= MAX_NATIVE_AX_DEPTH or not isinstance(children, list):
            continue
        stack.extend(
            (child, depth + 1)
            for child in reversed(children)
            if isinstance(child, Mapping)
        )
    return False


def _validate_text_detail_artifact_pair(
    image_artifact: Any,
    detail_artifact: Any,
    metadata: Any,
    *,
    snapshot_id: str | None,
) -> None:
    _validate_text_detail_artifact_name(image_artifact, detail_artifact)
    _validate_text_detail_metadata(metadata, snapshot_id=snapshot_id)


def _validate_text_detail_metadata(
    metadata: Any,
    *,
    snapshot_id: str | None,
) -> Mapping[str, Any]:
    _require_object(metadata, "snapshot text detail metadata")
    required = {
        "schema_version", "snapshot_id", "coverage", "node_count",
        "max_depth_observed", "byte_count", "sha256", "truncated",
        "truncation_reasons",
    }
    if set(metadata) != required:
        raise ValueError("snapshot text detail metadata has missing or unknown fields")
    _exact_integer(metadata["schema_version"], "text detail schema_version", minimum=1, maximum=1)
    metadata_snapshot_id = metadata["snapshot_id"]
    if (
        not isinstance(metadata_snapshot_id, str)
        or not metadata_snapshot_id
        or not _has_valid_unicode_scalars(metadata_snapshot_id, MAX_ELEMENT_REF_SCALARS)
        or (snapshot_id is not None and metadata_snapshot_id != snapshot_id)
    ):
        raise ValueError("snapshot text detail snapshot_id is invalid or mismatched")
    if metadata["coverage"] != "reported_ax_subtree":
        raise ValueError("snapshot text detail coverage is invalid")
    _exact_integer(
        metadata["node_count"], "text detail node_count", minimum=0, maximum=MAX_TEXT_DETAIL_NODES
    )
    _exact_integer(
        metadata["max_depth_observed"],
        "text detail max_depth_observed",
        minimum=0,
        maximum=MAX_TEXT_DETAIL_DEPTH,
    )
    _exact_integer(
        metadata["byte_count"], "text detail byte_count", minimum=1, maximum=MAX_TEXT_DETAIL_BYTES
    )
    _validate_fragment_hash(metadata["sha256"], "text detail sha256")
    truncated = metadata["truncated"]
    if not isinstance(truncated, bool):
        raise ValueError(  # noqa: TRY004 - wire validation API uses ValueError
            "snapshot text detail truncated must be a boolean"
        )
    reasons = metadata["truncation_reasons"]
    reason_indexes = (
        [TEXT_DETAIL_TRUNCATION_REASONS.index(reason) for reason in reasons]
        if isinstance(reasons, list)
        and all(
            isinstance(reason, str) and reason in TEXT_DETAIL_TRUNCATION_REASONS
            for reason in reasons
        )
        else []
    )
    if (
        not isinstance(reasons, list)
        or len(reasons) > len(TEXT_DETAIL_TRUNCATION_REASONS)
        or len(reason_indexes) != len(reasons)
        or any(left >= right for left, right in pairwise(reason_indexes))
        or truncated != bool(reasons)
    ):
        raise ValueError("snapshot text detail truncation_reasons are invalid")
    return metadata


def decode_text_detail_envelope(
    data: bytes,
    *,
    snapshot_id: str,
    metadata: Mapping[str, object],
    sha256: str,
) -> Mapping[str, Any]:
    """Strictly decode and cross-check one verified AX detail artifact.

    The caller must supply the digest computed from the already-open descriptor.
    Pathnames and helper-provided digests are never treated as authority.
    """
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_TEXT_DETAIL_BYTES:
        raise ValueError("snapshot text detail artifact bytes are invalid")
    _validate_fragment_hash(sha256, "text detail verified sha256")
    validated_metadata = _validate_text_detail_metadata(metadata, snapshot_id=snapshot_id)
    if validated_metadata["byte_count"] != len(data):
        raise ValueError("snapshot text detail byte count mismatch")
    if validated_metadata["sha256"] != sha256:
        raise ValueError("snapshot text detail hash mismatch")
    try:
        decoded = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("snapshot text detail artifact is not strict JSON") from exc
    _require_object(decoded, "snapshot text detail artifact")
    required = {"schema_version", "snapshot_id", "coverage", "limits", "stats", "root"}
    if set(decoded) != required:
        raise ValueError("snapshot text detail artifact schema has missing or unknown fields")
    _exact_integer(decoded["schema_version"], "text detail artifact schema_version", minimum=1, maximum=1)
    if decoded["snapshot_id"] != snapshot_id:
        raise ValueError("snapshot text detail artifact snapshot_id is mismatched")
    if decoded["coverage"] != "reported_ax_subtree":
        raise ValueError("snapshot text detail artifact coverage is invalid")
    expected_limits = {
        "maximum_depth": MAX_TEXT_DETAIL_DEPTH,
        "maximum_nodes": MAX_TEXT_DETAIL_NODES,
        "maximum_structural_string_bytes": 4 * 1024,
        "maximum_value_bytes": 256 * 1024,
        "maximum_aggregate_text_bytes": 4 * 1024 * 1024,
        "maximum_final_bytes": MAX_TEXT_DETAIL_BYTES,
        "wall_clock_ms": 5_000,
    }
    limits = decoded["limits"]
    _require_object(limits, "snapshot text detail limits")
    if set(limits) != set(expected_limits):
        raise ValueError("snapshot text detail limits have missing or unknown fields")
    for key, expected in expected_limits.items():
        _exact_integer(limits[key], f"text detail limits {key}", minimum=expected, maximum=expected)

    stats = decoded["stats"]
    _require_object(stats, "snapshot text detail stats")
    stats_fields = {"node_count", "max_depth_observed", "truncated", "truncation_reasons"}
    if set(stats) != stats_fields:
        raise ValueError("snapshot text detail stats have missing or unknown fields")
    node_count = _exact_integer(
        stats["node_count"], "text detail stats node_count", minimum=0, maximum=MAX_TEXT_DETAIL_NODES
    )
    max_depth = _exact_integer(
        stats["max_depth_observed"],
        "text detail stats max_depth_observed",
        minimum=0,
        maximum=MAX_TEXT_DETAIL_DEPTH,
    )
    _validate_text_detail_truncation(stats["truncated"], stats["truncation_reasons"])

    root = decoded["root"]
    _require_object(root, "snapshot text detail root")
    text_budget = [0]
    if node_count == 0:
        if root:
            raise ValueError("snapshot text detail empty tree is inconsistent")
        observed_count, observed_depth = 0, 0
    else:
        observed_count, observed_depth = _validate_text_detail_node(
            root,
            depth=0,
            ordinal=0,
            text_budget=text_budget,
        )
    if node_count != observed_count or max_depth != observed_depth:
        raise ValueError("snapshot text detail stats do not match the tree")
    for key in ("node_count", "max_depth_observed", "truncated", "truncation_reasons"):
        if validated_metadata[key] != stats[key] or (
            key in {"node_count", "max_depth_observed"}
            and type(validated_metadata[key]) is not int
        ):
            raise ValueError("snapshot text detail metadata does not match artifact stats")
    return decoded


def _validate_text_detail_truncation(truncated: Any, reasons: Any) -> None:
    if not isinstance(truncated, bool) or not isinstance(reasons, list):
        raise ValueError(  # noqa: TRY004 - wire validation API uses ValueError
            "snapshot text detail truncation state is invalid"
        )
    indexes = (
        [TEXT_DETAIL_TRUNCATION_REASONS.index(reason) for reason in reasons]
        if all(isinstance(reason, str) and reason in TEXT_DETAIL_TRUNCATION_REASONS for reason in reasons)
        else []
    )
    if (
        len(indexes) != len(reasons)
        or any(left >= right for left, right in pairwise(indexes))
        or truncated != bool(reasons)
    ):
        raise ValueError("snapshot text detail truncation reasons are invalid")


def _validate_text_detail_node(
    value: Mapping[str, Any],
    *,
    depth: int,
    ordinal: int,
    text_budget: list[int],
) -> tuple[int, int]:
    allowed = {
        "node_id", "role", "subrole", "bounds", "label", "title", "help", "value",
        "enabled", "focused", "redacted", "children",
    }
    if set(value) - allowed or "node_id" not in value or "role" not in value:
        raise ValueError("snapshot text detail node schema is invalid")
    if depth > MAX_TEXT_DETAIL_DEPTH or value["node_id"] != f"node_{ordinal}":
        raise ValueError("snapshot text detail node identity or depth is invalid")
    _bounded_text_detail_string(value["role"], 4 * 1024, "role", text_budget)
    for key in ("subrole", "label", "title", "help"):
        if key in value:
            _bounded_text_detail_string(value[key], 4 * 1024, key, text_budget)
    if "value" in value:
        _bounded_text_detail_string(value["value"], 256 * 1024, "value", text_budget)
    for key in ("enabled", "focused", "redacted"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError(f"snapshot text detail {key} must be a boolean")
    if "redacted" in value and value["redacted"] is not True:
        raise ValueError("snapshot text detail redacted marker must be true when present")
    role = value["role"]
    subrole = value.get("subrole")
    secure = (
        not role
        or role == "AXUnknown"
        or not subrole
        or subrole == "AXUnknown"
        or "secure" in role.casefold()
        or "secure" in subrole.casefold()
    )
    if value.get("redacted") is True and "value" in value:
        raise ValueError("snapshot text detail redacted node contains a value")
    if secure and value.get("redacted") is not True:
        raise ValueError("snapshot text detail secure identity is not redacted")
    if not secure and value.get("redacted") is True:
        raise ValueError("snapshot text detail nonsecure identity must not be redacted")
    if "bounds" in value:
        bounds = value["bounds"]
        _require_object(bounds, "snapshot text detail bounds")
        if set(bounds) != {"x", "y", "width", "height"}:
            raise ValueError("snapshot text detail bounds schema is invalid")
        numbers = [bounds[key] for key in ("x", "y", "width", "height")]
        if (
            not all(_finite_number(number) for number in numbers)
            or numbers[2] < 0
            or numbers[3] < 0
        ):
            raise ValueError("snapshot text detail bounds are invalid")
    children = value.get("children", [])
    if not isinstance(children, list):
        raise ValueError(  # noqa: TRY004 - wire validation API uses ValueError
            "snapshot text detail children must be an array"
        )
    if "children" in value and not children:
        raise ValueError("snapshot text detail children must be omitted when empty")
    count = 1
    deepest = depth
    for child in children:
        _require_object(child, "snapshot text detail child")
        child_count, child_depth = _validate_text_detail_node(
            child,
            depth=depth + 1,
            ordinal=ordinal + count,
            text_budget=text_budget,
        )
        count += child_count
        deepest = max(deepest, child_depth)
        if ordinal + count > MAX_TEXT_DETAIL_NODES:
            raise ValueError("snapshot text detail node limit exceeded")
    return count, deepest


def _bounded_text_detail_string(
    value: Any,
    maximum: int,
    name: str,
    text_budget: list[int],
    *,
    nonempty: bool = False,
) -> None:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"snapshot text detail {name} is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"snapshot text detail {name} has invalid Unicode") from exc
    if size > maximum:
        raise ValueError(f"snapshot text detail {name} exceeds its byte limit")
    text_budget[0] += size
    if text_budget[0] > 4 * 1024 * 1024:
        raise ValueError("snapshot text detail aggregate text limit exceeded")


def _validate_size(value: Any, name: str) -> None:
    _require_object(value, name)
    _reject_unknown_fields(value, {"width", "height"}, name)
    if set(value) != {"width", "height"} or not all(
        _positive_finite_number(value[key]) for key in ("width", "height")
    ):
        raise ValueError(f"{name} must contain positive finite width and height")


def _validate_bounds(value: Any, name: str) -> None:
    _require_object(value, name)
    keys = {"x", "y", "width", "height"}
    _reject_unknown_fields(value, keys, name)
    if set(value) != keys:
        raise ValueError(f"{name} requires x, y, width, and height")
    if not all(_finite_number(value[key]) for key in ("x", "y")) or not all(
        _positive_finite_number(value[key]) for key in ("width", "height")
    ):
        raise ValueError(f"{name} must contain finite coordinates and positive dimensions")


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _positive_finite_number(value: Any) -> bool:
    return _finite_number(value) and value > 0


def _decode_error(value: Any) -> ComputerError:
    _require_object(value, "error")
    _reject_unknown_fields(value, {"code", "message"}, "error")
    if "code" not in value or "message" not in value:
        raise ValueError("error requires code and message")
    if not isinstance(value["message"], str):
        raise ValueError("error.message must be a string")  # noqa: TRY004 - protocol validation API uses ValueError
    try:
        code = ComputerErrorCode(value["code"])
    except (TypeError, ValueError):
        # 不许静默改名：保留 helper 原始码名。此前每个 Swift 侧新增语义都会在 Python 里
        # 变成一句无从查起的 protocol_mismatch（实机两次 takeover 后捕获失败就栽在这）。
        return ComputerError(
            ComputerErrorCode.PROTOCOL_MISMATCH,
            f"unknown helper error code {value['code']!r}: {value['message']}",
        )
    return ComputerError(code, value["message"])


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate field: {key}")
        result[key] = value
    return result


def _require_object(value: Any, name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")  # noqa: TRY004 - protocol validation API uses ValueError


def _validate_reference(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or not _has_valid_unicode_scalars(value, MAX_ELEMENT_REF_SCALARS)
    ):
        raise ValueError(f"{name} must be a non-empty bounded reference")


def _validate_cooperative_request_payload(operation: str, payload: Mapping[str, Any]) -> None:
    if operation == "get_app_state":
        base = {"app_ref", "window_ref", "catalog_generation", "scope", "artifact_name"}
        if "text_detail" not in payload and "text_detail_artifact_name" not in payload:
            _require_exact_payload(payload, base, operation)
        else:
            _require_exact_payload(
                payload,
                base | {"text_detail", "text_detail_artifact_name"},
                operation,
            )
            if payload["text_detail"] != ComputerSnapshotTextDetailMode.ON.value:
                raise ValueError("get_app_state text_detail must be on or omitted")
            _validate_get_app_state_text_detail_artifact_name(
                payload["artifact_name"], payload["text_detail_artifact_name"]
            )
        _validate_reference(payload["app_ref"], "app_ref")
        _validate_reference(payload["window_ref"], "window_ref")
        if (
            isinstance(payload["catalog_generation"], bool)
            or not isinstance(payload["catalog_generation"], int)
            or payload["catalog_generation"] <= 0
        ):
            raise ValueError("catalog_generation must be a positive integer")
        if payload["scope"] not in {"target_window", "display"}:
            raise ValueError("get_app_state scope is invalid")
        artifact_name = _validate_plain_artifact_name(
            payload["artifact_name"], "get_app_state artifact"
        )
        if not artifact_name.endswith(".png"):
            raise ValueError("get_app_state artifact must be a PNG filename")
        return
    if operation in {"snapshot", "snapshot_subtree"}:
        base = {"app_ref", "window_ref", "scope", "artifact_name"}
        if operation == "snapshot_subtree":
            base |= {"snapshot_id", "subtree_ref"}
            _validate_reference(payload.get("snapshot_id"), "snapshot_id")
            _validate_reference(payload.get("subtree_ref"), "subtree_ref")
            if payload.get("scope") != "target_window":
                raise ValueError("snapshot_subtree requires target_window scope")
        if "text_detail" not in payload and "text_detail_artifact_name" not in payload:
            _require_exact_payload(payload, base, operation)
        else:
            _require_exact_payload(
                payload,
                base | {"text_detail", "text_detail_artifact_name"},
                operation,
            )
            if payload["text_detail"] != ComputerSnapshotTextDetailMode.ON.value:
                raise ValueError("snapshot text_detail must be on or omitted")
            _validate_text_detail_artifact_name(
                payload["artifact_name"], payload["text_detail_artifact_name"]
            )
        _validate_reference(payload["app_ref"], "app_ref")
        _validate_reference(payload["window_ref"], "window_ref")
        if payload["scope"] not in {"target_window", "display"}:
            raise ValueError("snapshot scope is invalid")
        _validate_plain_artifact_name(payload["artifact_name"], "snapshot artifact")
        return
    if operation == "select":
        _require_exact_payload(payload, {"app_ref", "window_ref"}, operation)
        _validate_reference(payload["app_ref"], "app_ref")
        _validate_reference(payload["window_ref"], "window_ref")
        return
    if operation == "plan_actions":
        mode = _validate_interaction_mode(payload.get("interaction_mode"))
        required = {"interaction_mode", "snapshot_id", "actions"}
        fragment = payload.get("fragment")
        if fragment is not None:
            required.add("fragment")
            if not isinstance(fragment, ForegroundFragmentPlanRequest):
                raise ValueError("plan_actions fragment is invalid")
            if fragment.authority.input_snapshot_id != payload.get("snapshot_id"):
                raise ValueError("fragment input snapshot does not match plan snapshot")
        _require_exact_payload(payload, required, operation)
        _validate_reference(payload["snapshot_id"], "snapshot_id")
        return
    if operation == "takeover_begin":
        required = {"snapshot_id", "plan_ref"}
        if "fragment" in payload:
            required.add("fragment")
        _require_exact_payload(payload, required, operation)
        _validate_reference(payload["snapshot_id"], "snapshot_id")
        _validate_reference(payload["plan_ref"], "plan_ref")
        if "fragment" in payload and not isinstance(payload["fragment"], ForegroundFragmentDeclaration):
            ForegroundFragmentDeclaration.from_mapping(payload["fragment"])
        return
    if operation == "fragment_stage_commit":
        _require_exact_payload(
            payload,
            {
                "takeover_ref", "fragment_hash", "stage_index", "stage_hash", "plan_ref",
                "fresh_snapshot_id", "postcondition_verified",
            },
            operation,
        )
        FragmentStageCommit(**payload)
        return
    if operation == "takeover_end":
        expected = {"takeover_ref"}
        if "restore_previous_focus" in payload:
            expected.add("restore_previous_focus")
            if not isinstance(payload["restore_previous_focus"], bool):
                raise ValueError("restore_previous_focus must be boolean")
        _require_exact_payload(payload, expected, operation)
        _validate_reference(payload["takeover_ref"], "takeover_ref")
        return
    if operation == "act":
        required = {"interaction_mode", "snapshot_id", "plan_ref", "actions"}
        mode = _validate_interaction_mode(payload.get("interaction_mode"))
        if mode is ComputerInteractionMode.FOREGROUND_TAKEOVER:
            required.add("takeover_ref")
            fragment_fields = {"fragment_hash", "stage_index", "stage_hash"}
            present = fragment_fields.intersection(payload)
            if present:
                required.update(fragment_fields)
        _require_exact_payload(payload, required, operation)
        _validate_reference(payload["snapshot_id"], "snapshot_id")
        _validate_reference(payload["plan_ref"], "plan_ref")
        if mode is ComputerInteractionMode.FOREGROUND_TAKEOVER:
            _validate_reference(payload["takeover_ref"], "takeover_ref")
        if "fragment_hash" in payload:
            FragmentStageAuthority(
                payload["fragment_hash"],
                payload["stage_index"],
                payload["stage_hash"],
                payload["snapshot_id"],
            )




def _require_exact_payload(value: Mapping[str, Any], required: set[str], operation: str) -> None:
    if set(value) != required:
        raise ValueError(f"{operation} requires exactly: {', '.join(sorted(required))}")


def _validate_plain_artifact_name(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not _has_valid_unicode_scalars(value, 128)
        or len(value.encode("utf-8")) > 128
        or value in {".", ".."}
        or not all(0x21 <= ord(character) <= 0x7E for character in value)
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"{name} must be a plain filename")
    return value


def _validate_text_detail_artifact_name(image_artifact: Any, detail_artifact: Any) -> str:
    image = _validate_plain_artifact_name(image_artifact, "snapshot artifact")
    detail = _validate_plain_artifact_name(detail_artifact, "snapshot text detail artifact")
    prefix = "snapshot-"
    suffix = ".png"
    token = image[len(prefix):-len(suffix)] if image.startswith(prefix) and image.endswith(suffix) else ""
    if (
        len(token) != 32
        or any(character not in "0123456789abcdef" for character in token)
        or detail != f"{image[:-4]}.ax.json"
    ):
        raise ValueError(
            "snapshot text detail artifact must be snapshot-<id>.ax.json with the PNG stem"
        )
    return detail


def _validate_get_app_state_text_detail_artifact_name(
    image_artifact: Any, detail_artifact: Any
) -> str:
    image = _validate_plain_artifact_name(image_artifact, "get_app_state artifact")
    detail = _validate_plain_artifact_name(
        detail_artifact, "get_app_state text detail artifact"
    )
    if not image.endswith(".png") or detail != f"{image[:-4]}.ax.json":
        raise ValueError(
            "get_app_state text detail artifact must use the PNG stem with .ax.json"
        )
    return detail


def _validate_interaction_mode(value: Any) -> ComputerInteractionMode:
    try:
        return ComputerInteractionMode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("interaction_mode is invalid") from exc


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} contains unknown fields: {', '.join(sorted(unknown))}")


def _is_current_protocol_version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == PROTOCOL_VERSION


def _is_known_error_code(value: Any) -> bool:
    try:
        ComputerErrorCode(value)
    except (TypeError, ValueError):
        return False
    return True


def _has_valid_unicode_scalars(value: str, maximum: int) -> bool:
    if len(value) > maximum:
        return False
    return all(not 0xD800 <= ord(character) <= 0xDFFF for character in value)


def _to_json_value(value: Any) -> Any:
    if isinstance(value, ComputerAction):
        return value.to_mapping()
    if isinstance(value, ForegroundFragmentDeclaration):
        return value.to_mapping()
    if isinstance(value, ForegroundFragmentActionRequirement):
        return value.to_mapping()
    if isinstance(value, ForegroundFragmentStageDeclaration):
        return value.to_mapping()
    if isinstance(value, Mapping):
        return {key: _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    return value
