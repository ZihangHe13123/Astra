"""Strict, default-deny compatibility cells for macOS PID-scoped input."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "macos_computer_compatibility.json"
)
_MAX_REGISTRY_BYTES = 64 * 1024
_MAX_APPLICATIONS = 128
_MAX_CAPABILITIES_PER_APPLICATION = 2
_MAX_KEY_CHORDS = 32
_MAX_NESTING_DEPTH = 32
_MAX_BUNDLE_ID = 255
_MAX_VERSION = 64
_MAX_KEY_CHORD = 128
_BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+")
_EXACT_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,7}(?:[-+][A-Za-z0-9][A-Za-z0-9.-]{0,31})?")
_POINTER_ACTIONS = frozenset({"click", "double_click", "scroll", "drag"})
_REGISTRY_BACKENDS = frozenset({"pid_pointer", "foreground_keyboard", "pid_keyboard", "wps_panel_ax"})
_MODIFIER_ORDER = ("command", "control", "option", "shift", "function", "caps_lock")
_KEYS = frozenset({
    *"abcdefghijklmnopqrstuvwxyz0123456789",
    "=", "-", "]", "[", "'", ";", "\\", ",", "/", ".",
    "return", "tab", "space", "delete", "escape", "left", "right", "down", "up",
})


class CompatibilityRegistryError(ValueError):
    """The reviewed compatibility registry is missing, untrusted, or invalid."""


@dataclass(frozen=True)
class MacDispatchCompatibility:
    backend: str
    enabled_actions: frozenset[str]
    allowed_key_chords: frozenset[str] = frozenset()
    allow_text_entry: bool = False
    requires_active: bool = False


@dataclass(frozen=True)
class _MacCompatibilityCell:
    bundle_id: str
    app_version: str
    dispatch: MacDispatchCompatibility


def _no_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CompatibilityRegistryError(f"duplicate compatibility field: {key}")
        result[key] = value
    return result


def _trusted_bytes(path: Path) -> bytes:
    if os.name != "posix":
        raise CompatibilityRegistryError("compatibility registry ownership requires POSIX")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CompatibilityRegistryError("compatibility registry is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CompatibilityRegistryError("compatibility registry must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise CompatibilityRegistryError("compatibility registry must be a regular file")
    if metadata.st_uid not in {0, os.getuid()}:
        raise CompatibilityRegistryError("compatibility registry has an untrusted owner")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CompatibilityRegistryError("compatibility registry is group/world writable")
    if metadata.st_size > _MAX_REGISTRY_BYTES:
        raise CompatibilityRegistryError("compatibility registry is unbounded")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CompatibilityRegistryError("compatibility registry cannot be read safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or opened.st_uid not in {0, os.getuid()}
            or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or opened.st_size > _MAX_REGISTRY_BYTES
        ):
            raise CompatibilityRegistryError("compatibility registry changed while opening")
        payload = os.read(descriptor, _MAX_REGISTRY_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > _MAX_REGISTRY_BYTES:
        raise CompatibilityRegistryError("compatibility registry is unbounded")
    return payload


def _bounded_identifier(value: Any, *, name: str, maximum: int, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CompatibilityRegistryError(f"{name} must be a bounded string")
    if pattern.fullmatch(value) is None:
        raise CompatibilityRegistryError(f"{name} is invalid")
    return value


def _reject_json_constant(_: str) -> None:
    raise CompatibilityRegistryError("compatibility registry is not strict JSON")


def _validate_depth(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_NESTING_DEPTH:
        raise CompatibilityRegistryError("compatibility registry nesting is unbounded")
    if isinstance(value, dict):
        for nested in value.values():
            _validate_depth(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            _validate_depth(nested, depth=depth + 1)


def _canonical_key_chord(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_KEY_CHORD
        or value != value.lower()
    ):
        raise CompatibilityRegistryError("allowed_key_chords must be canonical bounded strings")
    parts = value.split("+")
    key = parts[-1]
    modifiers = parts[:-1]
    if (
        key not in _KEYS
        or len(set(modifiers)) != len(modifiers)
        or any(modifier not in _MODIFIER_ORDER for modifier in modifiers)
        or modifiers != [modifier for modifier in _MODIFIER_ORDER if modifier in modifiers]
        or (key == "v" and "command" in modifiers)
    ):
        raise CompatibilityRegistryError("allowed_key_chords must be canonical bounded strings")
    return value


def _load_cells(path: Path) -> tuple[_MacCompatibilityCell, ...]:
    try:
        payload = _trusted_bytes(path)
        if payload.startswith(b"\xef\xbb\xbf"):
            raise CompatibilityRegistryError("compatibility registry is not strict JSON")
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicate_fields,
            parse_constant=_reject_json_constant,
        )
    except CompatibilityRegistryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CompatibilityRegistryError("compatibility registry is not strict JSON") from exc
    _validate_depth(document)
    if not isinstance(document, dict) or set(document) != {"schema_version", "applications"}:
        raise CompatibilityRegistryError("compatibility registry schema is invalid")
    if type(document["schema_version"]) is not int or document["schema_version"] != 2:
        raise CompatibilityRegistryError("compatibility registry schema version is invalid")
    applications = document["applications"]
    if not isinstance(applications, list) or len(applications) > _MAX_APPLICATIONS:
        raise CompatibilityRegistryError("compatibility applications must be a bounded array")

    cells: list[_MacCompatibilityCell] = []
    identities: set[tuple[str, str]] = set()
    for raw in applications:
        if not isinstance(raw, dict) or set(raw) != {
            "bundle_id", "app_version", "capabilities",
        }:
            raise CompatibilityRegistryError("compatibility application schema is invalid")
        bundle_id = _bounded_identifier(
            raw["bundle_id"], name="bundle_id", maximum=_MAX_BUNDLE_ID, pattern=_BUNDLE_ID
        )
        app_version = _bounded_identifier(
            raw["app_version"], name="app_version", maximum=_MAX_VERSION, pattern=_EXACT_VERSION
        )
        identity = (bundle_id, app_version)
        if identity in identities:
            raise CompatibilityRegistryError("duplicate compatibility application cell")
        identities.add(identity)
        capabilities = raw["capabilities"]
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or len(capabilities) > _MAX_CAPABILITIES_PER_APPLICATION
        ):
            raise CompatibilityRegistryError("compatibility capabilities must be a bounded array")
        backends: set[str] = set()
        for capability in capabilities:
            if not isinstance(capability, dict):
                raise CompatibilityRegistryError("compatibility capability schema is invalid")
            backend = capability.get("backend")
            if backend not in _REGISTRY_BACKENDS:
                raise CompatibilityRegistryError("compatibility backend is invalid")
            if backend in backends:
                raise CompatibilityRegistryError("duplicate compatibility backend cell")
            backends.add(backend)
            if backend == "pid_pointer":
                # requires_active 为可选字段(缺省 false): app 声明仅 active
                # 前台状态才处理合成指针事件, 计划器需在 plan 阶段强制走
                # foreground takeover 激活路径 (see docs/macos-computer-use.md#input-delivery-contracts)
                if not set(capability) <= {"backend", "enabled_actions", "requires_active"}:
                    raise CompatibilityRegistryError("pointer capability schema is invalid")
                raw_actions = capability["enabled_actions"]
                if (
                    not isinstance(raw_actions, list)
                    or not raw_actions
                    or len(raw_actions) > len(_POINTER_ACTIONS)
                    or not all(isinstance(action, str) for action in raw_actions)
                    or len(set(raw_actions)) != len(raw_actions)
                    or any(action not in _POINTER_ACTIONS for action in raw_actions)
                ):
                    raise CompatibilityRegistryError(
                        "enabled_actions must contain unique pointer action labels"
                    )
                requires_active = capability.get("requires_active", False)
                if not isinstance(requires_active, bool):
                    raise CompatibilityRegistryError("requires_active must be a Boolean")
                dispatch = MacDispatchCompatibility(
                    backend=backend,
                    enabled_actions=frozenset(raw_actions),
                    requires_active=requires_active,
                )
            elif backend == "wps_panel_ax":
                if (bundle_id, app_version) != ("com.kingsoft.wpsoffice.mac", "12.1.26026") or (
                    set(capability) != {"backend", "enabled_actions"}
                    or capability["enabled_actions"] != ["text", "press"]
                ):
                    raise CompatibilityRegistryError("WPS panel AX capability schema is invalid")
                dispatch = MacDispatchCompatibility(backend=backend, enabled_actions=frozenset({"text", "press"}))
            else:
                if set(capability) != {
                    "backend", "enabled_actions", "allowed_key_chords", "allow_text_entry",
                }:
                    raise CompatibilityRegistryError("keyboard capability schema is invalid")
                if capability["enabled_actions"] != ["text"]:
                    raise CompatibilityRegistryError("keyboard enabled_actions must be exactly text")
                raw_chords = capability["allowed_key_chords"]
                if (
                    not isinstance(raw_chords, list)
                    or len(raw_chords) > _MAX_KEY_CHORDS
                    or not all(isinstance(chord, str) for chord in raw_chords)
                    or len(set(raw_chords)) != len(raw_chords)
                ):
                    raise CompatibilityRegistryError(
                        "allowed_key_chords must contain unique bounded strings"
                    )
                chords = frozenset(_canonical_key_chord(chord) for chord in raw_chords)
                allow_text_entry = capability["allow_text_entry"]
                if not isinstance(allow_text_entry, bool):
                    raise CompatibilityRegistryError("allow_text_entry must be a Boolean")
                dispatch = MacDispatchCompatibility(
                    backend=backend,
                    enabled_actions=frozenset({"text"}),
                    allowed_key_chords=chords,
                    allow_text_entry=allow_text_entry,
                )
            cells.append(_MacCompatibilityCell(bundle_id, app_version, dispatch))
    return tuple(cells)


class _DispatchRegistry:
    def lookup_exact(
        self,
        bundle_id: str,
        app_version: str,
        backend: str,
    ) -> MacDispatchCompatibility | None:
        if (
            not isinstance(bundle_id, str)
            or not isinstance(app_version, str)
            or not isinstance(backend, str)
            or backend not in _REGISTRY_BACKENDS
        ):
            return None
        for cell in _load_cells(_REGISTRY_PATH):
            if (
                cell.bundle_id == bundle_id
                and cell.app_version == app_version
                and cell.dispatch.backend == backend
            ):
                return cell.dispatch
        return None


_dispatch_registry = _DispatchRegistry()


def exact_dispatch_compatibility(
    bundle_id: str,
    app_version: str,
    backend: str,
) -> MacDispatchCompatibility | None:
    """Return only the reviewed backend cell for an exact application identity."""

    return _dispatch_registry.lookup_exact(bundle_id, app_version, backend)


def enabled_pid_actions(bundle_id: str, app_version: str) -> frozenset[str]:
    """Actions reviewed for this exact bundle/version across **every pid_* backend**.

    原来只查 ``pid_pointer`` ⇒ 表里 ``pid_keyboard`` 的授权（2026-09-03 实机时 Safari 26.6.2、
    WPS、微信三家的 ``text``）对调用方永久不可见，而调用方拿
    ``set(plan.pid_action_classes).issubset(enabled)`` 做判定 —— 键盘类动作因此从来没有过机会。
    函数名承诺的是"pid 动作"，实现却只给了一半后端，属实现与名字不符。

    ``foreground_keyboard`` **不**并入：那是另一条投递路径，由
    ``exact_dispatch_compatibility(..., "foreground_keyboard")`` 单独把关（既有夹具与用例锁着
    这半边语义）。默认拒绝保持不变：任一后端都不匹配的 app/version 拿不到任何许可。
    """

    actions: set[str] = set()
    for backend in ("pid_pointer", "pid_keyboard"):
        compatibility = exact_dispatch_compatibility(bundle_id, app_version, backend)
        if compatibility is not None:
            actions.update(compatibility.enabled_actions)
    return frozenset(actions)


def pointer_requires_active(bundle_id: str, app_version: str) -> bool:
    """True when the exact bundle/version cell declares requires_active."""

    compatibility = exact_dispatch_compatibility(bundle_id, app_version, "pid_pointer")
    return compatibility.requires_active if compatibility is not None else False


__all__ = [
    "CompatibilityRegistryError",
    "MacDispatchCompatibility",
    "enabled_pid_actions",
    "exact_dispatch_compatibility",
    "pointer_requires_active",
]
