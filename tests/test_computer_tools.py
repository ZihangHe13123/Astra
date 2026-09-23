"""Provider-neutral Computer Use tool contract tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import zlib
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.computer_backend import (
    ComputerActionPlan,
    ComputerActionResult,
    ComputerAppCatalog,
    ComputerArtifact,
    ComputerBackendAppState,
    ComputerSessionError,
    ComputerSessionManager,
    ComputerTarget,
)
from agent.runtime.computer_feedback import receipt_instruction
from agent.runtime.computer_protocol import (
    ComputerError,
    ComputerErrorCode,
    ComputerInteractionMode,
    ComputerResponse,
    ComputerSnapshot,
    ComputerSnapshotTextDetailMode,
    FragmentStageAuthority,
)
from agent.runtime.hooks import HookRegistry
from agent.runtime.macos_computer import HelperApplicationError, MacComputerBackend
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)


def _smart_detail_document(snapshot_id: str, root: dict) -> tuple[dict, bytes]:
    ordinal = 0
    max_depth = 0

    def assign(value: dict, depth: int) -> dict:
        nonlocal ordinal, max_depth
        current = ordinal
        ordinal += 1
        max_depth = max(max_depth, depth)
        output = {key: item for key, item in value.items() if key != "children"}
        output["node_id"] = f"node_{current}"
        children = value.get("children", [])
        if children:
            output["children"] = [assign(child, depth + 1) for child in children]
        return output

    assigned_root = assign(root, 0)
    document = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "coverage": "reported_ax_subtree",
        "limits": {
            "maximum_depth": 20,
            "maximum_nodes": 4_000,
            "maximum_structural_string_bytes": 4 * 1_024,
            "maximum_value_bytes": 256 * 1_024,
            "maximum_aggregate_text_bytes": 4 * 1_024 * 1_024,
            "maximum_final_bytes": 8 * 1_024 * 1_024,
            "wall_clock_ms": 5_000,
        },
        "stats": {
            "node_count": ordinal,
            "max_depth_observed": max_depth,
            "truncated": False,
            "truncation_reasons": [],
        },
        "root": assigned_root,
    }
    return document, json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def png_with_replaced_idat(payload: bytes) -> bytes:
    output = bytearray(PNG[:8])
    offset = 8
    while offset < len(PNG):
        length = struct.unpack(">I", PNG[offset:offset + 4])[0]
        chunk_type = PNG[offset + 4:offset + 8]
        chunk_data = payload if chunk_type == b"IDAT" else PNG[offset + 8:offset + 8 + length]
        output.extend(struct.pack(">I", len(chunk_data)))
        output.extend(chunk_type)
        output.extend(chunk_data)
        output.extend(struct.pack(">I", zlib.crc32(chunk_data, zlib.crc32(chunk_type)) & 0xFFFFFFFF))
        offset += 12 + length
    return bytes(output)


def run(awaitable):
    return asyncio.run(awaitable)


class FakeComputerBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.capture_targets: list[ComputerTarget] = []
        self.artifact_fd = -1
        self.invalid_png = False
        self.wrong_mode = False
        self.ax_known_path = ""
        self.catalog_window_role = ""
        self.catalog_window_title = "Fixture"
        self.catalog_app_ref = "app-1"
        self.catalog_window_ref = "window-1"
        self.catalog_window_identity_ref = "identity-a"
        self.catalog_bundle_id = "dev.astra.fixture"
        self.catalog_app_version = "1.0.0"
        self.finder_path_parts: list[str] = []
        self.focused_role = "AXTextArea"
        self.focused_value = ""
        self.snapshot_root_role = "AXWindow"
        self.snapshot_root_label = "Fixture"
        self.has_default_button = False
        self.default_button_element_ref: str | None = None
        self.snapshot_ax_tree_override: dict[str, object] | None = None
        self.status_error: Exception | None = None
        self.select_error: Exception | None = None
        self.force_takeover = False
        self.plan_action_classes: tuple[str, ...] | None = None
        self.plan_pid_action_classes: tuple[str, ...] | None = None
        self.takeover_end_result = {"ended": True, "restoration": "restored"}
        self.act_result = ComputerActionResult(
            result={"outcomes": [{"index": 0, "ok": True}], "last_acknowledged_action": 0}
        )
        self.close_error: Exception | None = None
        self.text_detail_root = {
            "role": "AXWindow",
            "subrole": "AXStandardWindow",
            "title": "Fixture",
            "children": [{
                "role": "AXStaticText",
                "subrole": "AXText",
                "value": "Smart detail",
            }],
        }
        self.catalog_generation = 0

    async def bind_artifact_directory(self, directory_fd: int) -> None:
        self.artifact_fd = directory_fd

    async def status(self):
        self.calls.append(("status", None))
        if self.status_error is not None:
            raise self.status_error
        return {
            "supported": True,
            "protocol_version": 1,
            "permissions": {"accessibility": True, "screen_recording": True},
        }

    async def apps(self):
        self.calls.append(("apps", None))
        self.catalog_generation += 1
        apps = [
            {
                "app_ref": self.catalog_app_ref,
                "name": "Fixture",
                "bundle_id": self.catalog_bundle_id,
                "app_version": self.catalog_app_version,
                "windows": [
                    {
                        "window_ref": self.catalog_window_ref,
                        "title": self.catalog_window_title,
                        "bindable": True,
                        "binding_status": "ready",
                        "window_identity_ref": self.catalog_window_identity_ref,
                        **({"role": self.catalog_window_role} if self.catalog_window_role else {}),
                        "bounds": {"x": 1, "y": 2, "width": 3, "height": 4},
                    },
                    {"window_ref": "window-2", "title": "Second Fixture", "bounds": {"x": 5, "y": 6, "width": 7, "height": 8}},
                ],
            },
            {
                "app_ref": "app-2",
                "name": "Other Fixture",
                "bundle_id": "com.example.other",
                "windows": [
                    {"window_ref": "window-3", "title": "Other", "bounds": {"x": 1, "y": 2, "width": 3, "height": 4}},
                ],
            },
        ]
        return ComputerAppCatalog(self.catalog_generation, tuple(apps))

    async def get_app_state(
        self,
        target,
        scope,
        artifact,
        *,
        text_detail=ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact=None,
    ):
        self.calls.append((
            "get_app_state",
            (target, scope, self.catalog_generation, text_detail.value),
        ))
        snapshot = await self.snapshot(
            target,
            scope,
            artifact,
            text_detail=text_detail,
            text_detail_artifact=text_detail_artifact,
        )
        return ComputerBackendAppState(
            self.catalog_generation,
            target,
            snapshot,
        )

    async def select(self, app_ref: str, window_ref: str):
        self.calls.append(("select", (app_ref, window_ref)))
        if self.select_error is not None:
            raise self.select_error
        return ComputerTarget(app_ref, window_ref)

    async def snapshot(
        self,
        target,
        scope: str,
        artifact: ComputerArtifact,
        *,
        text_detail=ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact=None,
    ):
        self.capture_targets.append(target)
        self.calls.append(("snapshot", scope))
        payload = b"not a png" if self.invalid_png else PNG
        descriptor = os.open(
            artifact.filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=artifact.directory_fd,
        )
        try:
            os.write(descriptor, payload)
            if self.wrong_mode:
                os.fchmod(descriptor, 0o644)
        finally:
            os.close(descriptor)
        count = sum(name == "snapshot" for name, _ in self.calls)
        snapshot_id = f"snapshot-{count}"
        detail_payload = {}
        if text_detail is ComputerSnapshotTextDetailMode.ON:
            assert text_detail_artifact is not None
            document, detail = _smart_detail_document(snapshot_id, self.text_detail_root)
            detail_descriptor = os.open(
                text_detail_artifact.filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=text_detail_artifact.directory_fd,
            )
            try:
                os.write(detail_descriptor, detail)
            finally:
                os.close(detail_descriptor)
            stats = document["stats"]
            detail_payload = {
                "text_detail_artifact": text_detail_artifact.filename,
                "text_detail_metadata": {
                    "schema_version": 1,
                    "snapshot_id": snapshot_id,
                    "coverage": "reported_ax_subtree",
                    "node_count": stats["node_count"],
                    "max_depth_observed": stats["max_depth_observed"],
                    "byte_count": len(detail),
                    "sha256": hashlib.sha256(detail).hexdigest(),
                    "truncated": False,
                    "truncation_reasons": [],
                },
            }
        return ComputerSnapshot(snapshot_id, {
            "image_artifact": artifact.filename,
            "logical_size": {"width": 800, "height": 600},
            "pixel_size": {"width": 1, "height": 1},
            "backing_scale": 1,
            "capture_bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "has_default_button": self.has_default_button,
            **(
                {"default_button_element_ref": self.default_button_element_ref}
                if self.default_button_element_ref is not None else {}
            ),
            **detail_payload,
            **(
                {
                    "display_id": 7,
                    "target_window_bounds": {"x": 10, "y": 20, "width": 300, "height": 200},
                }
                if scope == "display" else {}
            ),
            "ax_tree": self.snapshot_ax_tree_override or {
                "role": self.snapshot_root_role,
                "label": self.snapshot_root_label,
                "children": [
                    {
                        "role": self.focused_role,
                        "label": "Body",
                        "focused": True,
                        **({"value": self.focused_value} if self.focused_value else {}),
                        "element_ref": f"snapshot-{count}:1",
                        "bounds": {"x": 0, "y": 0, "width": 400, "height": 400},
                        **({"document_path": self.ax_known_path} if self.ax_known_path else {}),
                    },
                    {"role": "AXButton", "label": "Export PDF", "bounds": {"x": 10, "y": 450, "width": 100, "height": 30}, "element_ref": f"snapshot-{count}:export"},
                    {"role": "AXSecureTextField", "label": "Password", "bounds": {"x": 10, "y": 500, "width": 100, "height": 30}, "element_ref": f"snapshot-{count}:secure"},
                    *(
                        [{
                            "role": "AXList",
                            "label": "Path",
                            "children": [
                                {"role": "AXStaticText", "value": part}
                                for part in self.finder_path_parts
                            ],
                        }]
                        if self.finder_path_parts else []
                    ),
                ],
            },
        })

    async def plan_actions(self, target, snapshot_id, actions, interaction_mode):
        self.calls.append(("plan_actions", (snapshot_id, interaction_mode.value)))
        action_class = {"keypress": "press", "type": "text"}
        resolved_action_classes = tuple(dict.fromkeys(
            action_class.get(action.type, action.type)
            for action in actions
            if action.type != "wait"
        ))
        return ComputerActionPlan(
            plan_ref=f"plan-{sum(name == 'plan_actions' for name, _ in self.calls)}",
            interaction_mode=interaction_mode,
            requires_takeover=(
                self.force_takeover
                or interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
            ),
            reason=(
                "foreground_takeover_required"
                if self.force_takeover
                or interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
                else "background_ax_only"
            ),
            action_classes=self.plan_action_classes or resolved_action_classes,
            pid_action_classes=(
                self.plan_pid_action_classes
                if self.plan_pid_action_classes is not None
                else resolved_action_classes
            ),
        )

    async def begin_takeover(self, snapshot_id, plan_ref):
        self.calls.append(("takeover_begin", (snapshot_id, plan_ref)))
        return "takeover-1"

    async def act(
        self,
        target,
        snapshot_id,
        actions,
        *,
        interaction_mode,
        plan_ref,
        takeover_ref=None,
    ):
        self.calls.append((
            "act",
            (
                snapshot_id,
                [action.to_mapping() for action in actions],
                interaction_mode.value,
                plan_ref,
                takeover_ref,
            ),
        ))
        return self.act_result

    async def end_takeover(self, takeover_ref, *, restore_previous_focus=True):
        self.calls.append(("takeover_end", takeover_ref))
        self.restoration_requests = getattr(self, "restoration_requests", []) + [restore_previous_focus]
        return self.takeover_end_result

    async def close(self):
        self.calls.append(("close", None))
        if self.close_error is not None:
            raise self.close_error


class NativeActReceiptTransport:
    def __init__(self, *, outcomes, acknowledgement, error) -> None:
        self.outcomes = outcomes
        self.acknowledgement = acknowledgement
        self.error = error
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        fragment_binding = {
            key: request.payload[key]
            for key in ("fragment_hash", "stage_index", "stage_hash")
            if key in request.payload
        }
        return ComputerResponse(
            request.request_id,
            ok=False,
            result={
                "outcomes": self.outcomes,
                "last_acknowledged_action": self.acknowledgement,
                **fragment_binding,
            },
            error=self.error,
        )


class OrdinaryTakeoverTransport:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        if request.operation == "plan_actions":
            mode = request.payload["interaction_mode"]
            required = mode == "background"
            return ComputerResponse(
                request.request_id,
                ok=not required,
                result={
                    "plan_ref": f"native-plan-{len(self.requests)}",
                    "interaction_mode": mode,
                    "requires_takeover": True,
                    "reason": "foreground_takeover_required",
                    "action_classes": ["click"],
                    "pid_action_classes": ["click"],
                    "last_acknowledged_action": -1,
                },
                error=(
                    ComputerError(
                        ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED,
                        "foreground required",
                    )
                    if required else None
                ),
            )
        if request.operation == "takeover_begin":
            return ComputerResponse(
                request.request_id, True, result={"takeover_ref": "ordinary-takeover"}
            )
        if request.operation == "act":
            return ComputerResponse(request.request_id, True, result={
                "outcomes": [{"index": 0, "ok": True}],
                "last_acknowledged_action": 0,
            })
        if request.operation == "takeover_end":
            return ComputerResponse(request.request_id, True, result={
                "started": True,
                "restoration": "restored",
            })
        raise AssertionError(request.operation)


def route_act_through_native_adapter(backend, monkeypatch, transport):
    adapter = MacComputerBackend(transport)

    async def native_act(
        target,
        snapshot_id,
        actions,
        *,
        interaction_mode,
        plan_ref,
        takeover_ref=None,
    ):
        backend.calls.append(("act", (snapshot_id, interaction_mode.value, plan_ref)))
        fragment_stage = (
            FragmentStageAuthority("a" * 64, 0, "0" * 64, snapshot_id)
            if interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
            else None
        )
        return await adapter.act(
            target,
            snapshot_id,
            actions,
            interaction_mode=interaction_mode,
            plan_ref=plan_ref,
            takeover_ref=takeover_ref,
            fragment_stage=fragment_stage,
        )

    monkeypatch.setattr(backend, "act", native_act)


def test_public_tool_keeps_ordinary_v3_foreground_takeover_through_native_adapter(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    transport = OrdinaryTakeoverTransport()
    adapter = MacComputerBackend(transport)
    monkeypatch.setattr(backend, "plan_actions", adapter.plan_actions)
    monkeypatch.setattr(backend, "begin_takeover", adapter.begin_takeover)
    monkeypatch.setattr(backend, "act", adapter.act)
    monkeypatch.setattr(backend, "end_takeover", adapter.end_takeover)
    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"click"}),
    )
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": f"{snapshot['snapshot_id']}:1"}],
    }

    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"
    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert result["error"] == ""
    assert [request.operation for request in transport.requests] == [
        "plan_actions", "plan_actions", "takeover_begin", "act", "takeover_end",
    ]
    assert transport.requests[1].payload.get("fragment") is None
    assert set(transport.requests[2].payload) == {"snapshot_id", "plan_ref"}
    assert set(transport.requests[3].payload) == {
        "interaction_mode", "snapshot_id", "plan_ref", "takeover_ref", "actions",
    }


@pytest.fixture
def backend():
    return FakeComputerBackend()


@pytest.fixture
def manager(backend, tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("ComputerSessionManager requires POSIX held-directory cache leases")
    # Simulate the public tool's host without changing the real OS file APIs
    # used by the session manager and its held-directory cache.
    monkeypatch.setattr(
        "agent.runtime.tools.computer.sys", SimpleNamespace(platform="darwin")
    )
    return ComputerSessionManager(backend, cache_root=tmp_path / "computer-cache")


@pytest.fixture
def registry():
    return ToolRegistry()


def register(registry, manager, **kwargs):
    from agent.runtime.tools.computer import register_computer_tools

    register_computer_tools(registry, manager, **kwargs)


def register_local(registry, manager, monkeypatch, tmp_path):
    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(computer_tools, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        computer_tools,
        "_new_macos_computer_manager",
        lambda **_kwargs: manager,
    )
    runtime = computer_tools.register_local_computer_runtime(
        registry,
        cache_root=tmp_path / "local-computer-cache",
        model_capabilities={"tools", "vision"},
    )
    assert runtime is not None
    return runtime


def test_robustness_initial_timeout_recovers_through_exact_catalog(registry, manager, backend, monkeypatch):
    register(registry, manager)
    registry.yolo = True
    assert run(registry.execute("computer_apps", {}))["error"] == ""

    async def timeout(*args, **kwargs):
        raise HelperApplicationError(ComputerError(ComputerErrorCode.OBSERVATION_TIMEOUT, "timeout"))

    monkeypatch.setattr(backend, "get_app_state", timeout)
    result = run(registry.execute("computer_get_app_state", {"app_ref": "app-1", "window_ref": "window-1"}))
    assert result["code"] == "observation_timeout"
    assert manager.target is None
    hint = result["recovery_hint"]
    assert "computer_apps" in hint and "computer_get_app_state" in hint
    assert "snapshot" not in hint.lower()


def test_robustness_bad_key_rejected_before_helper(registry, manager, backend):
    register(registry, manager)
    before = list(backend.calls)
    result = run(registry.execute("computer_act", {
        "snapshot_id": "unused", "actions": [{"type": "keypress", "key": "bad-key"}],
    }))
    assert result["error"]
    assert "delete" in str(result) and "escape" in str(result)
    assert backend.calls == before


def _robustness_menu_tree():
    return {"role": "AXWindow", "children": [
        {"role": "AXMenuBar", "element_ref": "bar", "children": [
            {"role": "AXMenuBarItem", "element_ref": "file", "index": 2,
             "label": "File", "actions": ["AXPress"], "children": [
                 {"role": "AXMenu", "children": [
                     {"role": "AXMenuItem", "element_ref": "open", "index": 4, "label": "Open"},
                 ]},
             ]},
        ]},
        {"role": "AXTextField", "element_ref": "field", "index": 5, "value": "current"},
        {"role": "AXTextField", "subrole": "AXSecureTextField", "value": "secret"},
    ]}


def test_robustness_ordinary_ax_collapses_only_static_menu_descendants():
    from copy import deepcopy
    from agent.runtime.tools.computer import _bounded_ax_tree, _bounded_ax_subtree

    raw = _robustness_menu_tree()
    original = deepcopy(raw)
    public = _bounded_ax_tree(raw)
    entry = public["children"][0]["children"][0]
    assert entry["element_ref"] == "file" and entry["index"] == 2
    assert entry["actions"] == ["AXPress"]
    assert "Open" not in json.dumps(public)
    assert "subtree_ref" in json.dumps(entry)
    assert public["children"][1]["value"] == "current"
    assert "secret" not in json.dumps(public)
    assert raw == original
    assert "Open" in json.dumps(_bounded_ax_subtree(raw, "file"))


@pytest.mark.parametrize("explicit", ["active_menu", "subtree", "role_filter", "native_subtree"])
def test_robustness_explicit_menu_content_is_preserved(registry, manager, backend, explicit):
    from agent.runtime.tools.computer import _public_snapshot_payload

    register(registry, manager)
    run(registry.execute("computer_apps", {}))
    run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}))
    raw = _robustness_menu_tree()
    kwargs = {}
    if explicit == "active_menu":
        raw = {"role": "AXMenu", "children": raw["children"][0]["children"]}
    elif explicit == "subtree":
        kwargs["subtree_ref"] = "file"
    elif explicit == "role_filter":
        kwargs["role_filter"] = "AXMenuItem"
    else:
        raw["observation_scope"] = "native_subtree"
    backend.snapshot_ax_tree_override = raw
    snapshot = run(manager.snapshot())
    payload, *_ = _public_snapshot_payload(manager, snapshot, scope="target_window", **kwargs)
    assert "Open" in json.dumps(payload["ax_tree"])


@pytest.mark.parametrize("action_result", [None, {"status": "action_acknowledged", "last_acknowledged_action": 0}])
def test_robustness_public_ordinary_receipt_prioritizes_window_fields(registry, manager, backend, action_result):
    from agent.runtime.tools.computer import _public_snapshot_payload

    register(registry, manager)
    run(registry.execute("computer_apps", {}))
    run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}))
    backend.snapshot_ax_tree_override = _robustness_menu_tree()
    snapshot = run(manager.snapshot())
    payload, *_ = _public_snapshot_payload(manager, snapshot, scope="target_window", action_result=action_result)
    projected = json.dumps(payload["ax_tree"])
    assert "Open" not in projected and "secret" not in projected
    assert "current" in projected and "AXPress" in projected and "subtree_ref" in projected
    assert payload["ax_tree"]["children"][0]["children"][0]["index"] == 2
    assert "Open" in json.dumps(snapshot.payload["ax_tree"])


def _robustness_menu_handoff(registry, manager, backend, monkeypatch):
    original_apps = backend.apps
    vanished = [False]

    async def catalog():
        result = await original_apps()
        app = dict(result.apps[0])
        parent = {"window_ref": "parent", "title": "Parent", "bindable": True,
                  "window_identity_ref": "identity-parent"}
        app["windows"] = [parent] if vanished[0] else [*app["windows"][:1], parent]
        return ComputerAppCatalog(
            result.generation, (app,),
            confirmed_absent_window_identity_refs=("identity-a",) if vanished[0] else (),
        )

    monkeypatch.setattr(backend, "apps", catalog)
    register(registry, manager)
    registry.yolo = True
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    assert run(registry.execute("computer_get_app_state", {"app_ref": "app-1", "window_ref": "window-1"}))["error"] == ""
    assert run(registry.execute("computer_handoff", {}))["error"] == ""
    vanished[0] = True


def test_robustness_vanished_resume_receipt_allows_exact_rebind(registry, manager, backend, monkeypatch):
    _robustness_menu_handoff(registry, manager, backend, monkeypatch)
    before = len(backend.calls)
    result = run(registry.execute("computer_resume", {"app_ref": "app-1", "window_ref": "window-1"}))
    assert not result["error"] and result["verified"] is True
    payload = json.loads(result.get("fresh_output") or result["output"])
    assert payload["status"] == "target_gone_unbound"
    assert "computer_apps" in payload["recovery_hint"] and "computer_get_app_state" in payload["recovery_hint"]
    assert not manager.handed_off and manager.target is None and manager.suspended_target is None
    assert [name for name, _ in backend.calls[before:]] == ["apps"]
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    assert run(registry.execute("computer_get_app_state", {"app_ref": "app-1", "window_ref": "parent"}))["error"] == ""


@pytest.mark.parametrize("failure", ["postcondition", "commit", "cancelled"])
def test_robustness_unbound_receipt_publication_failure_stays_handed_off(registry, manager, backend, monkeypatch, failure):
    _robustness_menu_handoff(registry, manager, backend, monkeypatch)
    if failure == "postcondition":
        registry.get("computer_resume").postcondition = lambda args, result: (False, "publication refused")
    else:
        async def fail_commit(publication_id):
            if failure == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("commit refused")
        monkeypatch.setattr(manager, "commit_resume_publication", fail_commit)
    result = run(registry.execute("computer_resume", {"app_ref": "app-1", "window_ref": "window-1"}))
    assert result["code"] == "postcondition_failed"
    assert manager.handed_off and manager.suspended_target == ComputerTarget("app-1", "window-1")
    assert manager.target is None and not manager.grants


def test_local_runtime_resume_is_lazy_and_does_not_start_manager(
    registry,
    manager,
    monkeypatch,
    tmp_path,
):
    runtime = register_local(registry, manager, monkeypatch, tmp_path)

    assert runtime.manager_started is False
    assert runtime.suspended_target is None
    assert runtime.manager_started is False


def test_local_runtime_resume_returns_fresh_snapshot_after_handoff(
    registry,
    manager,
    backend,
    monkeypatch,
    tmp_path,
):
    runtime = register_local(registry, manager, monkeypatch, tmp_path)

    catalog = run(registry.execute("computer_apps", {}))
    assert catalog["error"] == ""
    selected = run(registry.execute(
        "computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}
    ))
    assert selected["error"] == ""
    handed_off = run(registry.execute("computer_handoff", {}))
    assert json.loads(handed_off["output"])["handoff_active"] is True
    refreshed_catalog = run(registry.execute("computer_apps", {}))
    assert refreshed_catalog["error"] == ""

    resumed = run(registry.execute("computer_resume", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert resumed["error"] == ""
    payload = json.loads(resumed["fresh_output"])
    assert payload["snapshot_id"] == "snapshot-1"
    assert runtime.suspended_target is None
    assert runtime.target == ComputerTarget("app-1", "window-1")
    assert manager.handed_off is False
    assert [name for name, _value in backend.calls] == [
        "apps", "select", "apps", "apps", "select", "snapshot",
    ]


def test_local_runtime_resume_rotates_private_refs_but_preserves_public_arguments(
    registry,
    manager,
    backend,
    monkeypatch,
    tmp_path,
):
    runtime = register_local(registry, manager, monkeypatch, tmp_path)

    run(registry.execute("computer_apps", {}))
    run(registry.execute(
        "computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}
    ))
    run(registry.execute("computer_handoff", {}))
    backend.catalog_app_ref = "fresh-app"
    backend.catalog_window_ref = "fresh-window"
    refreshed = run(registry.execute("computer_apps", {}))

    assert refreshed["error"] == ""
    assert runtime.suspended_target == ComputerTarget("app-1", "window-1")
    resumed = run(registry.execute("computer_resume", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert resumed["error"] == ""
    assert runtime.target == ComputerTarget("fresh-app", "fresh-window")
    assert [value for name, value in backend.calls if name == "select"] == [
        ("app-1", "window-1"),
        ("fresh-app", "fresh-window"),
    ]


def test_local_runtime_resume_target_gone_snapshot_never_uses_title_recovery(
    registry,
    manager,
    backend,
    monkeypatch,
    tmp_path,
):
    register_local(registry, manager, monkeypatch, tmp_path)
    run(registry.execute("computer_apps", {}))
    run(registry.execute(
        "computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}
    ))
    run(registry.execute("computer_handoff", {}))
    backend.catalog_app_ref = "fresh-app"
    backend.catalog_window_ref = "fresh-window"
    run(registry.execute("computer_apps", {}))
    original_snapshot = backend.snapshot
    resume_snapshot_attempts = 0

    async def target_gone_then_success(*args, **kwargs):
        nonlocal resume_snapshot_attempts
        resume_snapshot_attempts += 1
        if resume_snapshot_attempts == 1:
            raise HelperApplicationError(ComputerError(
                ComputerErrorCode.TARGET_GONE,
                "token-matched target disappeared",
            ))
        return await original_snapshot(*args, **kwargs)

    backend.snapshot = target_gone_then_success

    result = run(registry.execute("computer_resume", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert result["code"] == "target_gone"
    assert resume_snapshot_attempts == 1
    assert [name for name, _value in backend.calls].count("apps") == 3
    assert [value for name, value in backend.calls if name == "select"] == [
        ("app-1", "window-1"),
        ("fresh-app", "fresh-window"),
    ]


def test_local_runtime_resume_rejects_mismatched_refs_without_resuming(
    registry,
    manager,
    backend,
    monkeypatch,
    tmp_path,
):
    runtime = register_local(registry, manager, monkeypatch, tmp_path)

    run(registry.execute("computer_apps", {}))
    run(registry.execute(
        "computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}
    ))
    run(registry.execute("computer_handoff", {}))
    run(registry.execute("computer_apps", {}))
    select_calls = sum(name == "select" for name, _value in backend.calls)

    result = run(registry.execute("computer_resume", {
        "app_ref": "app-2",
        "window_ref": "window-3",
    }))

    assert result["code"] == "target_gone"
    assert runtime.suspended_target == ComputerTarget("app-1", "window-1")
    assert manager.handed_off is True
    assert sum(name == "select" for name, _value in backend.calls) == select_calls


def focus(registry):
    catalog = run(registry.execute("computer_apps", {}))
    assert catalog["error"] == ""
    result = run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}))
    assert result["error"] == ""


def test_registers_computer_apps_with_repeat_and_budget_controls(registry, manager):
    register(registry, manager)

    names = {name for name in registry.tool_names if name.startswith("computer_")}
    assert names == {
        "computer_status", "computer_apps", "computer_get_app_state",
        "computer_focus", "computer_snapshot",
        "computer_act", "computer_handoff", "computer_resume", "computer_close",
        "computer_begin_takeover", "computer_end_takeover",
    }
    assert all(registry.get(name).group == "computer" for name in names)
    assert registry.get("computer_act").replay == "never"
    assert registry.get("computer_act").max_retries == 0
    assert registry.get("computer_begin_takeover").approval == "on_risk"
    assert registry.get("computer_end_takeover").approval == "never"
    assert registry.get("computer_apps").repeat_guard is False
    assert registry.get("computer_apps").max_calls_per_turn == 8
    definition = registry.get("computer_act")
    assert "acknowledgement" in definition.description
    assert "effect" in definition.description
    assert registry.get("computer_snapshot").result_persistence == "request_local"
    assert registry.get("computer_get_app_state").result_persistence == "request_local"
    assert registry.get("computer_get_app_state").parameters == {
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
    assert registry.get("computer_snapshot").parameters["properties"]["text_detail"] == {
        "type": "string",
        "enum": ["off", "on"],
        "default": "off",
    }
    mode_schema = registry.get("computer_act").parameters["properties"]["interaction_mode"]
    assert mode_schema["enum"] == ["auto", "background", "foreground_takeover"]
    assert mode_schema["default"] == "auto"
    # Both refs resume a suspended target; neither ends a targetless handoff.
    assert registry.get("computer_resume").parameters == {
        "type": "object",
        "properties": {
            "app_ref": {"type": "string", "minLength": 1},
            "window_ref": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }


def test_react_repeats_computer_apps_catalog_refresh_three_times(
    registry, manager, backend,
):
    register(registry, manager)

    class RepeatingCatalogLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            del messages, tools
            self.calls += 1
            if self.calls <= 3:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": f"catalog-{self.calls}",
                        "name": "computer_apps",
                        "arguments": "{}",
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "catalog refreshed", "usage": None}

    async def scenario():
        llm = RepeatingCatalogLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=4)
        agent.tool_allowlist = {"computer_apps"}
        events = [
            event async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("refresh the application catalog")])
            )
        ]
        return llm, events

    llm, events = run(scenario())

    assert llm.calls == 4
    assert sum(name == "apps" for name, _value in backend.calls) == 3
    assert not any("ToolCircuitOpen" in json.dumps(event) for event in events)
    assert not any("repeated tool call" in event.get("message", "") for event in events)


def test_react_enforces_computer_apps_budget_after_eight_catalog_refreshes(
    registry, manager, backend,
):
    register(registry, manager)

    class RepeatingCatalogLLM:
        def __init__(self):
            self.calls = 0
            self.tool_calls = 0

        async def chat_stream(self, messages, tools):
            del messages, tools
            self.calls += 1
            if self.calls <= 9:
                self.tool_calls += 1
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": f"catalog-{self.calls}",
                        "name": "computer_apps",
                        "arguments": "{}",
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "catalog refresh stopped", "usage": None}

    async def scenario():
        llm = RepeatingCatalogLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=10)
        agent.tool_allowlist = {"computer_apps"}
        # Keep the schema exposed to exercise the ReAct execution budget rather
        # than its earlier schema-availability optimization.
        agent._available_tool_schemas = lambda *_args: [
            schema for schema in registry.to_openai_tools()
            if schema["function"]["name"] == "computer_apps"
        ]
        events = [
            event async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("refresh the application catalog")])
            )
        ]
        return agent, llm, events

    agent, llm, events = run(scenario())

    assert llm.tool_calls == 9
    assert sum(name == "apps" for name, _value in backend.calls) == 8
    assert any("ToolBudgetExhausted" in message["content"] for message in agent.context.messages)
    assert any("per-turn tool budget exhausted" in event.get("message", "") for event in events)


def test_react_rejects_entire_computer_apps_batch_that_exceeds_budget(
    registry, manager, backend,
):
    register(registry, manager)

    class SevenThenOverBudgetBatchLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            del messages, tools
            self.calls += 1
            if self.calls <= 7:
                calls = [{
                    "id": f"catalog-{self.calls}",
                    "name": "computer_apps",
                    "arguments": "{}",
                }]
            elif self.calls == 8:
                calls = [{
                    "id": "catalog-over-budget-1",
                    "name": "computer_apps",
                    "arguments": "{}",
                }, {
                    "id": "catalog-over-budget-2",
                    "name": "computer_apps",
                    "arguments": "{}",
                }]
            else:
                yield {"type": "done", "content": "catalog refresh stopped", "usage": None}
                return
            yield {"type": "tool_calls", "calls": calls, "content": "", "usage": None}

    async def scenario():
        llm = SevenThenOverBudgetBatchLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=9)
        agent.tool_allowlist = {"computer_apps"}
        agent._available_tool_schemas = lambda *_args: [
            schema for schema in registry.to_openai_tools()
            if schema["function"]["name"] == "computer_apps"
        ]
        events = [
            event async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("refresh the application catalog")])
            )
        ]
        return agent, llm, events

    agent, llm, events = run(scenario())

    assert llm.calls >= 8
    assert sum(name == "apps" for name, _value in backend.calls) == 7
    assert sum(
        "ToolBudgetExhausted" in message["content"]
        for message in agent.context.messages
    ) == 2
    assert any("per-turn tool budget exhausted" in event.get("message", "") for event in events)


def test_computer_resume_holds_catalog_authority_through_registry_postcondition(
    registry, manager, backend, monkeypatch,
):
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    assert run(registry.execute("computer_focus", {
        "app_ref": "app-1", "window_ref": "window-1",
    }))["error"] == ""
    assert run(registry.execute("computer_handoff", {}))["error"] == ""
    assert run(registry.execute("computer_apps", {}))["error"] == ""

    catalog_entered = asyncio.Event()
    original_apps = backend.apps
    boundary_observations: list[bool] = []
    catalog_tasks: list[asyncio.Task] = []

    async def tracked_apps():
        catalog_entered.set()
        return await original_apps()

    monkeypatch.setattr(backend, "apps", tracked_apps)

    async def schedule_catalog_at_handler_boundary(name, _args, _tool, call_next):
        result = await call_next()
        if name == "computer_resume":
            # The handler's own catalog refresh is complete. Track only the
            # competing refresh scheduled at the postcondition boundary.
            catalog_entered.clear()
            catalog_tasks.append(asyncio.create_task(
                registry.execute("computer_apps", {})
            ))
            try:
                await asyncio.wait_for(catalog_entered.wait(), timeout=0.05)
            except TimeoutError:
                pass
            boundary_observations.append(catalog_entered.is_set())
        return result

    registry.hooks.on_around_tool(schedule_catalog_at_handler_boundary)

    async def scenario():
        resumed = await registry.execute("computer_resume", {
            "app_ref": "app-1", "window_ref": "window-1",
        })
        assert catalog_tasks
        catalog = await asyncio.wait_for(catalog_tasks[0], timeout=1)
        return resumed, catalog

    resumed, catalog = run(scenario())

    assert resumed["error"] == ""
    assert resumed["verified"] is True
    assert catalog["error"] == ""
    assert catalog_entered.is_set()
    assert boundary_observations == [False]


def test_computer_resume_snapshot_failure_aborts_publication_and_requires_fresh_catalog(
    registry, manager, monkeypatch,
):
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    assert run(registry.execute("computer_focus", {
        "app_ref": "app-1", "window_ref": "window-1",
    }))["error"] == ""
    assert run(registry.execute("computer_handoff", {}))["error"] == ""
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    original_snapshot = manager.snapshot

    async def failed_snapshot(*_args, **_kwargs):
        raise ComputerSessionError("target_gone", "target_gone: mandatory resume snapshot failed")

    monkeypatch.setattr(manager, "snapshot", failed_snapshot)
    failed = run(registry.execute("computer_resume", {
        "app_ref": "app-1", "window_ref": "window-1",
    }))

    assert failed["code"] == "target_gone"
    assert manager.handed_off is True
    assert manager.suspended_target == ComputerTarget("app-1", "window-1")
    assert manager.target is None

    monkeypatch.setattr(manager, "snapshot", original_snapshot)
    stale_retry = run(registry.execute("computer_resume", {
        "app_ref": "app-1", "window_ref": "window-1",
    }))
    assert stale_retry["error"] == ""
    assert stale_retry["verified"] is True
    assert manager.handed_off is False


def test_existing_tools_keep_durable_argument_behavior_by_default():
    tool = ToolDef("echo", "echo", {"type": "object"}, lambda value: value)
    original = {"value": "ordinary", "nested": [1]}

    persisted = ToolRegistry.persistence_safe_args(tool, original)

    assert persisted == original
    assert persisted is not original
    assert persisted["nested"] is not original["nested"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"app_ref": "app-1"},
        {"window_ref": "window-1"},
        {"app_ref": "", "window_ref": "window-1"},
        {"app_ref": "app-1", "window_ref": ""},
        {"app_ref": True, "window_ref": "window-1"},
        {"app_ref": "app-1", "window_ref": False},
        {"app_ref": "app-1", "window_ref": "window-1", "scope": "window"},
        {"app_ref": "app-1", "window_ref": "window-1", "scope": True},
        {"app_ref": "app-1", "window_ref": "window-1", "text_detail": "yes"},
        {"app_ref": "app-1", "window_ref": "window-1", "text_detail": True},
        {"app_ref": "app-1", "window_ref": "window-1", "catalog_generation": 1},
        {"app_ref": "app-1", "window_ref": "window-1", "app_name": "Fixture"},
        {"app_ref": "app-1", "window_ref": "window-1", "window_title": "Fixture"},
        {"app": "Fixture", "window": "Fixture"},
        {"app_ref": "Fixture", "window_ref": "Fixture", "fuzzy": True},
    ],
)
def test_get_app_state_schema_rejects_non_exact_public_selectors_before_dispatch(
    registry,
    manager,
    backend,
    arguments,
):
    approvals = []
    registry.set_approval_handler(
        lambda request: approvals.append(request) or asyncio.sleep(0, result="once")
    )
    register(registry, manager)

    result = run(registry.execute("computer_get_app_state", arguments))

    assert result["code"] == "invalid_arguments"
    assert approvals == []
    assert backend.calls == []


def test_apps_then_get_app_state_binds_and_publishes_without_focus(
    registry,
    manager,
    backend,
):
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert result["error"] == ""
    assert result["verified"] is True
    payload = json.loads(result["fresh_output"])
    assert payload["session_id"] == manager.session_id
    assert payload["app_ref"] == "app-1"
    assert payload["window_ref"] == "window-1"
    assert payload["window_id"] == "window-1"
    assert payload["target_generation"] == manager.snapshot_target_binding.generation
    assert payload["snapshot_id"] == "snapshot-1"
    assert payload["scope"] == "target_window"
    assert len(payload["image_paths"]) == 1
    assert result["_private_result"]["image_data_urls"][0].startswith(
        "data:image/png;base64,"
    )
    assert not any(name == "select" for name, _value in backend.calls)
    assert any(name == "get_app_state" for name, _value in backend.calls)

    refreshed = run(registry.execute("computer_snapshot", {"scope": "target_window"}))
    assert refreshed["error"] == ""
    assert backend.capture_targets[-1] == ComputerTarget("app-1", "window-1")
    refreshed_payload = json.loads(refreshed["fresh_output"])
    acted = run(registry.execute("computer_act", {
        "snapshot_id": refreshed_payload["snapshot_id"],
        "actions": [{
            "type": "click",
            "element_ref": f"{refreshed_payload['snapshot_id']}:1",
        }],
    }))
    assert acted["error"] == ""
    assert backend.capture_targets[-1] == ComputerTarget("app-1", "window-1")


def test_get_app_state_rejects_catalog_names_and_titles_without_dispatch(
    registry,
    manager,
    backend,
):
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    before = sum(name == "get_app_state" for name, _value in backend.calls)

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "Fixture",
        "window_ref": "Fixture",
    }))

    assert result["code"] == "catalog_required"
    assert sum(name == "get_app_state" for name, _value in backend.calls) == before


def test_app_state_approval_scope_uses_the_native_catalog_generation(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    requests = []
    generations = []
    original_scope = computer_tools.app_state_approval_scope

    def observe_scope(*args, **kwargs):
        generations.append(kwargs["catalog_generation"])
        return original_scope(*args, **kwargs)

    async def approve_once(request):
        requests.append(request)
        return "once"

    backend.catalog_generation = 41
    registry.set_approval_handler(approve_once)
    register(registry, manager)
    monkeypatch.setattr(computer_tools, "app_state_approval_scope", observe_scope)
    assert run(registry.execute("computer_apps", {}))["error"] == ""

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))

    assert result["error"] == ""
    assert len(requests) == 1
    assert generations
    assert set(generations) == {42}


def test_app_state_rejects_a_tool_catalog_superseded_by_native_recovery(
    registry,
    manager,
    backend,
):
    approvals = 0

    async def unexpected_approval(_request):
        nonlocal approvals
        approvals += 1
        return "once"

    registry.set_approval_handler(unexpected_approval)
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    assert manager._last_catalog is not None
    manager._last_catalog = ComputerAppCatalog(
        manager._last_catalog.generation + 1,
        manager._last_catalog.apps,
    )
    before = sum(name == "get_app_state" for name, _value in backend.calls)

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))

    assert result["code"] == "catalog_required"
    assert "computer_apps" in result["recovery_hint"]
    assert approvals == 0
    assert sum(name == "get_app_state" for name, _value in backend.calls) == before


@pytest.mark.parametrize("error_code", [
    "catalog_required",
    ComputerErrorCode.STALE_TARGET.value,
    ComputerErrorCode.TARGET_GONE.value,
])
def test_app_state_catalog_identity_failures_require_fresh_exact_apps_refs(
    registry,
    manager,
    backend,
    monkeypatch,
    error_code,
):
    register(registry, manager)
    if error_code == "catalog_required":
        result = run(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
        }))
    else:
        assert run(registry.execute("computer_apps", {}))["error"] == ""

        async def reject(*_args, **_kwargs):
            raise HelperApplicationError(
                ComputerError(ComputerErrorCode(error_code), "private native detail")
            )

        monkeypatch.setattr(backend, "get_app_state", reject)
        result = run(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
        }))

    assert result["code"] == error_code
    assert "computer_apps" in result["recovery_hint"]
    assert "exact app_ref and window_ref" in result["recovery_hint"]


def test_get_app_state_text_detail_reuses_bounded_smart_snapshot_publication(
    registry,
    manager,
):
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))

    assert result["error"] == ""
    payload = json.loads(result["fresh_output"])
    assert payload["text_detail_path"].endswith(".ax.json")
    assert payload["text_detail_metadata"]["coverage"] == "reported_ax_subtree"
    assert payload["text_summary"] == [
        {"role": "AXWindow", "text": "Fixture"},
        {"role": "AXStaticText", "text": "Smart detail"},
    ]
    assert "children" not in payload["text_detail_metadata"]
    assert "Smart detail" not in json.dumps(payload["ax_tree"])


def test_get_app_state_primary_publication_does_not_reread_verified_png(
    registry,
    manager,
    monkeypatch,
):
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    original = manager.read_verified_png_artifact
    reads = 0
    postcondition_kwargs = {}

    def observe(filename, **kwargs):
        nonlocal reads, postcondition_kwargs
        reads += 1
        postcondition_kwargs = dict(kwargs)
        return original(filename, **kwargs)

    monkeypatch.setattr(manager, "read_verified_png_artifact", observe)

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert result["error"] == ""
    assert reads == 1  # the registry postcondition only
    assert postcondition_kwargs["expected_identity"]
    assert len(postcondition_kwargs["expected_sha256"]) == 64


def test_get_app_state_detail_primary_publication_uses_verified_detail_bytes(
    registry,
    manager,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    original = computer_tools._read_verified_snapshot_pair
    reads = 0

    def observe(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(computer_tools, "_read_verified_snapshot_pair", observe)

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))

    assert result["error"] == ""
    # The only mutable-path reread is the registry's independent postcondition.
    assert reads == 1


def test_get_app_state_postcondition_race_poisons_and_returns_only_unsafe_artifact(
    registry,
    manager,
    monkeypatch,
):
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""

    def changed_after_commit(_filename, **_kwargs):
        raise ComputerSessionError(
            "unsafe_artifact",
            "unsafe_artifact: screenshot identity changed after manager commit",
        )

    monkeypatch.setattr(manager, "read_verified_png_artifact", changed_after_commit)

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert result["code"] == "unsafe_artifact"
    assert "fresh_output" not in result
    assert "_private_result" not in result
    assert manager.target is None
    encoded = json.dumps(result, ensure_ascii=False)
    assert "image_paths" not in encoded
    assert "element_ref" not in encoded
    assert "target_generation" not in encoded
    assert "data:image" not in encoded


def test_get_app_state_unexpected_postcondition_exception_fails_closed(
    registry,
    manager,
    backend,
    monkeypatch,
):
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    secret = "/private/session/snapshot.png element_ref target_generation"

    def verifier_crashed(_filename, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(manager, "read_verified_png_artifact", verifier_crashed)

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert result["code"] == "unsafe_artifact"
    assert result["error_type"] == "unsafe_artifact"
    assert result["recoverable"] is False
    assert result["retryable"] is False
    assert result["verified"] is False
    assert "fresh_output" not in result
    assert "_private_result" not in result
    assert manager.target is None
    encoded = json.dumps(result, ensure_ascii=False)
    assert secret not in encoded
    assert "image_paths" not in encoded
    assert "element_ref" not in encoded
    assert "target_generation" not in encoded
    assert "data:image" not in encoded

    app_calls = sum(name == "apps" for name, _value in backend.calls)
    later = run(registry.execute("computer_apps", {}))
    assert later["code"] == "unsafe_cache"
    assert sum(name == "apps" for name, _value in backend.calls) == app_calls


def test_get_app_state_detail_pair_postcondition_race_poisons_without_leaking(
    registry,
    manager,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    original = computer_tools._read_verified_snapshot_pair
    reads = 0

    def changed_pair(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: detail pair changed after manager commit",
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(computer_tools, "_read_verified_snapshot_pair", changed_pair)

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))

    assert result["code"] == "unsafe_artifact"
    assert "fresh_output" not in result
    assert "_private_result" not in result
    assert manager.target is None
    encoded = json.dumps(result, ensure_ascii=False)
    assert "image_paths" not in encoded
    assert "text_detail_path" not in encoded
    assert "text_summary" not in encoded
    assert "target_generation" not in encoded


def test_get_app_state_detail_denial_never_dispatches_or_leaves_a_grant(
    registry,
    manager,
    backend,
):
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="deny"))
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    manager._latest_snapshot = ComputerSnapshot("old")
    manager._pending_plan = object()
    manager.grants.add("old-grant")

    result = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))

    assert result["code"] == "approval_denied"
    assert not any(name == "get_app_state" for name, _value in backend.calls)
    assert not any(
        scope.startswith("computer-observe-app-state:")
        for scope in registry.approved_permission_scopes
    )
    assert manager._latest_snapshot is None
    assert manager._pending_plan is None
    assert manager.grants == set()


def test_denied_app_state_waits_for_an_inflight_snapshot_then_invalidates_it(
    registry,
    manager,
    backend,
    monkeypatch,
):
    snapshot_entered = asyncio.Event()
    release_snapshot = asyncio.Event()
    approval_entered = asyncio.Event()
    release_approval = asyncio.Event()
    original_snapshot = backend.snapshot

    async def blocked_snapshot(*args, **kwargs):
        snapshot_entered.set()
        await release_snapshot.wait()
        return await original_snapshot(*args, **kwargs)

    async def deny_after_snapshot(_request):
        approval_entered.set()
        await release_approval.wait()
        return "deny"

    async def exercise():
        in_flight = asyncio.create_task(registry.execute("computer_snapshot", {
            "scope": "target_window",
        }))
        await snapshot_entered.wait()
        pending = asyncio.create_task(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "text_detail": "on",
        }))
        await asyncio.sleep(0)
        release_snapshot.set()
        snapshot = await in_flight
        await approval_entered.wait()
        release_approval.set()
        return snapshot, await pending

    registry.set_approval_handler(deny_after_snapshot)
    register(registry, manager)
    focus(registry)
    monkeypatch.setattr(backend, "snapshot", blocked_snapshot)

    snapshot, denied = run(exercise())

    # Encoding yields: queued invalidation now wins before image publication.
    assert snapshot["code"] == "stale_snapshot"
    assert denied["code"] == "approval_denied"
    assert manager._latest_snapshot is None
    assert manager._current_snapshot_artifacts == {}


def test_cancelled_app_state_waits_for_inflight_snapshot_invalidation(
    registry,
    manager,
    backend,
    monkeypatch,
):
    snapshot_entered = asyncio.Event()
    release_snapshot = asyncio.Event()
    original_snapshot = backend.snapshot

    async def blocked_snapshot(*args, **kwargs):
        snapshot_entered.set()
        await release_snapshot.wait()
        return await original_snapshot(*args, **kwargs)

    async def exercise():
        in_flight = asyncio.create_task(registry.execute("computer_snapshot", {
            "scope": "target_window",
        }))
        await snapshot_entered.wait()
        app_state = asyncio.create_task(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "text_detail": "on",
        }))
        await asyncio.sleep(0)
        app_state.cancel()
        await asyncio.sleep(0)
        assert not app_state.done()
        release_snapshot.set()
        snapshot = await in_flight
        with pytest.raises(asyncio.CancelledError):
            await app_state
        return snapshot

    register(registry, manager)
    focus(registry)
    manager._latest_snapshot = ComputerSnapshot("old-snapshot")
    manager._pending_plan = object()
    manager._takeover_ref = "old-takeover"
    manager._takeover_cleanup_ref = "old-cleanup"
    manager._fragment_declaration = object()
    manager._fragment_stage = object()
    manager._fragment_stage_input_completed = True
    manager.grants.add("old-grant")
    monkeypatch.setattr(backend, "snapshot", blocked_snapshot)

    snapshot = run(exercise())

    # Encoding yields: queued invalidation now wins before image publication.
    assert snapshot["code"] == "stale_snapshot"
    assert manager._latest_snapshot is None
    assert manager._pending_plan is None
    assert manager._takeover_ref is None
    assert manager._takeover_cleanup_ref is None
    assert manager._fragment_declaration is None
    assert manager._fragment_stage is None
    assert manager._fragment_stage_input_completed is False
    assert manager.grants == set()


def test_repeated_app_state_detail_cleans_the_previous_verified_pair(
    registry,
    manager,
):
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""

    for _ in range(10):
        result = run(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "text_detail": "on",
        }))
        assert result["error"] == ""

    assert len(list(manager.session_dir.glob("snapshot-*.ax.json"))) == 1
    assert len(list(manager.session_dir.glob("snapshot-*.png"))) == 1


def test_get_app_state_detail_cancellation_consumes_once_approval(
    registry,
    manager,
    backend,
    monkeypatch,
):
    approvals = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    original = backend.get_app_state

    async def approve_once(_request):
        nonlocal approvals
        approvals += 1
        return "once" if approvals == 1 else "deny"

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    async def exercise():
        first = asyncio.create_task(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "text_detail": "on",
        }))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "text_detail": "on",
        })

    registry.set_approval_handler(approve_once)
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    monkeypatch.setattr(backend, "get_app_state", blocked)

    second = run(exercise())

    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert not registry.approved_permission_scopes


def test_get_app_state_detail_timeout_consumes_once_approval(
    registry,
    manager,
    backend,
    monkeypatch,
):
    approvals = 0
    original = backend.get_app_state

    async def approve_once(_request):
        nonlocal approvals
        approvals += 1
        return "once" if approvals == 1 else "deny"

    async def too_slow(*args, **kwargs):
        await asyncio.sleep(1)
        return await original(*args, **kwargs)

    registry.set_approval_handler(approve_once)
    register(registry, manager)
    registry.get("computer_get_app_state").timeout = 0.01
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    monkeypatch.setattr(backend, "get_app_state", too_slow)

    first = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))
    second = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))

    assert first["code"] == "overall_timeout"
    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert not registry.approved_permission_scopes


@pytest.mark.parametrize("change", ["catalog", "target"])
def test_get_app_state_detail_pending_approval_cannot_cross_state_change(
    registry,
    manager,
    backend,
    change,
):
    approvals = 0

    async def change_then_approve(_request):
        nonlocal approvals
        approvals += 1
        if approvals != 1:
            return "deny"
        if change == "catalog":
            changed = await registry.execute("computer_apps", {})
        else:
            changed = await registry.execute("computer_focus", {
                "app_ref": "app-1",
                "window_ref": "window-2",
            })
        assert changed["error"] == ""
        return "once"

    registry.set_approval_handler(change_then_approve)
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    before = sum(name == "get_app_state" for name, _value in backend.calls)

    first = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))
    second = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "text_detail": "on",
    }))

    assert first["error"]
    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert sum(name == "get_app_state" for name, _value in backend.calls) == before
    assert not registry.approved_permission_scopes


def test_get_app_state_detail_once_approval_is_consumed_before_concurrent_dispatch(
    registry,
    manager,
    backend,
    monkeypatch,
):
    approvals = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    original = backend.get_app_state

    async def approve_once(_request):
        nonlocal approvals
        approvals += 1
        return "once" if approvals == 1 else "deny"

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    async def exercise():
        first = asyncio.create_task(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "text_detail": "on",
        }))
        await entered.wait()
        second = asyncio.create_task(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "text_detail": "on",
        }))
        await asyncio.sleep(0)
        release.set()
        return await first, await second

    registry.set_approval_handler(approve_once)
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    monkeypatch.setattr(backend, "get_app_state", delayed)

    first, second = run(exercise())

    assert first["code"] == "stale_snapshot"
    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert sum(name == "get_app_state" for name, _value in backend.calls) == 1
    assert not registry.approved_permission_scopes


def test_get_app_state_approved_detail_does_not_dispatch_behind_queued_target_change(
    registry,
    manager,
    backend,
    monkeypatch,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    original_select = backend.select

    async def delayed_select(app_ref, window_ref):
        entered.set()
        await release.wait()
        return await original_select(app_ref, window_ref)

    async def exercise():
        focused = asyncio.create_task(registry.execute("computer_focus", {
            "app_ref": "app-1",
            "window_ref": "window-2",
        }))
        await entered.wait()
        observed = asyncio.create_task(registry.execute("computer_get_app_state", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "text_detail": "on",
        }))
        await asyncio.sleep(0)
        release.set()
        return await focused, await observed

    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    monkeypatch.setattr(backend, "select", delayed_select)

    focused, observed = run(exercise())

    assert focused["error"] == ""
    assert observed["error"] == ""
    assert any(name == "get_app_state" for name, _value in backend.calls)
    assert not registry.approved_permission_scopes


@pytest.mark.parametrize("text_detail", [True, False, "unknown", 1, None])
def test_snapshot_schema_rejects_non_enum_text_detail_before_approval_or_capture(
    registry,
    manager,
    backend,
    text_detail,
):
    approvals = []
    registry.set_approval_handler(
        lambda request: approvals.append(request) or asyncio.sleep(0, result="once")
    )
    register(registry, manager)
    focus(registry)
    before = sum(name == "snapshot" for name, _ in backend.calls)

    result = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "text_detail": text_detail,
    }))

    assert result["code"] == "invalid_arguments"
    assert approvals == []
    assert sum(name == "snapshot" for name, _ in backend.calls) == before


def test_snapshot_omitted_text_detail_preserves_off_mode_without_new_approval(
    registry,
    manager,
    backend,
):
    approvals = []
    registry.set_approval_handler(
        lambda request: approvals.append(request) or asyncio.sleep(0, result="deny")
    )
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    assert result["error"] == ""
    assert approvals == []
    assert [value for name, value in backend.calls if name == "snapshot"][-1] == "target_window"


def test_text_detail_summary_is_document_order_normalized_adjacent_deduplicated_and_redacted():
    import agent.runtime.tools.computer as computer_tools

    summarize = getattr(computer_tools, "_text_detail_summary", None)
    assert callable(summarize)
    root = {
        "role": "  AXWindow ",
        "title": " Main\n Document ",
        "children": [
            {"role": "AXStaticText", "label": " Hello\tworld "},
            {"role": "AXStaticText", "label": "Hello world"},
            {"role": "AXButton", "label": " Save ", "title": "Document", "value": " Ready "},
            {"role": "AXStaticText", "label": "Hello world"},
            {
                "role": "AXSecureTextField",
                "subrole": "AXSecureTextField",
                "label": "Password",
                "value": "NEVER-SUMMARIZE-ME",
                "redacted": True,
            },
        ],
    }

    summary = summarize(root)

    assert summary == [
        {"role": "AXWindow", "text": "Main Document"},
        {"role": "AXStaticText", "text": "Hello world"},
        {"role": "AXButton", "text": "Save Document Ready"},
        {"role": "AXStaticText", "text": "Hello world"},
        {"role": "AXSecureTextField", "text": "Password"},
    ]
    assert "NEVER-SUMMARIZE-ME" not in json.dumps(summary)


def test_text_detail_summary_reserves_explicit_marker_inside_entry_and_byte_limits():
    import agent.runtime.tools.computer as computer_tools

    summarize = getattr(computer_tools, "_text_detail_summary", None)
    assert callable(summarize)
    entry_limited = summarize({
        "role": "AXWindow",
        "children": [
            {"role": "AXStaticText", "value": f"entry {index}"}
            for index in range(205)
        ],
    })

    assert len(entry_limited) == 200
    assert entry_limited[-1] == {
        "role": "AXSummaryTruncated",
        "text": "[summary truncated]",
    }
    assert len(json.dumps(
        entry_limited,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()) <= 32 * 1_024

    byte_limited = summarize({
        "role": "AXWindow",
        "children": [
            {"role": "AXStaticText", "value": f"{index}-" + "界" * 1_000}
            for index in range(100)
        ],
    })
    assert byte_limited[-1] == {
        "role": "AXSummaryTruncated",
        "text": "[summary truncated]",
    }
    assert len(json.dumps(
        byte_limited,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()) <= 32 * 1_024


def test_ax_bounding_counts_nested_list_and_scalar_nodes(monkeypatch):
    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(computer_tools, "_MAX_AX_NODES", 8)
    bounded = computer_tools._bounded_ax_tree([["node"] * 10 for _ in range(10)])

    assert json.dumps(bounded).count("node") < 8


@pytest.mark.parametrize("observe", ["computer_get_app_state", "computer_snapshot"])
@pytest.mark.parametrize("leaf_depth", [10, 13, 14, 19])
def test_default_ax_projection_preserves_deep_element_identity_and_focus(
    registry, manager, backend, monkeypatch, observe, leaf_depth,
):
    import agent.runtime.tools.computer as computer_tools

    leaf = {"role": "AXTextField", "element_ref": "snapshot-1:source",
            "focused": True, "value": "Source", "bounds": {"x": 1, "y": 2, "width": 100, "height": 20}}
    tree = leaf
    for ancestor in range(leaf_depth):
        tree = {"role": "AXWebArea" if ancestor == 1 else "AXGroup", "children": [tree]}
    backend.snapshot_ax_tree_override = tree
    register(registry, manager)
    if observe == "computer_get_app_state":
        assert run(registry.execute("computer_apps", {}))["error"] == ""
        arguments = {"app_ref": "app-1", "window_ref": "window-1"}
    else:
        focus(registry)
        arguments = {"scope": "target_window"}
    result = run(registry.execute(observe, arguments))
    assert result["error"] == ""
    payload = json.loads(result["fresh_output"])
    node = payload["ax_tree"]
    for _ in range(leaf_depth):
        node = node["children"][0]
    assert node == leaf

    # A visible focused ref must also reach the policy metadata on the next
    # observation, without issuing any keyboard or pointer actions.
    contexts = []
    original = computer_tools.snapshot_permission_request

    def record_context(approvals, context, **kwargs):
        contexts.append(context)
        return original(approvals, context, **kwargs)

    monkeypatch.setattr(computer_tools, "snapshot_permission_request", record_context)
    assert run(registry.execute("computer_snapshot", {}))["error"] == ""
    assert contexts[-1]["focused_element_ref"] == leaf["element_ref"]
    assert contexts[-1]["elements"][leaf["element_ref"]]["bounds"] == leaf["bounds"]


def test_default_ax_depth_covers_native_boundary_without_expanding_generic_metadata():
    from agent.runtime.computer_protocol import MAX_NATIVE_AX_DEPTH
    from agent.runtime.tools.computer import _bounded_ax_tree, _bounded_public_value

    leaf = {"role": "AXButton", "element_ref": "last-native-level",
            "children": [{"role": "AXButton", "element_ref": "too-deep"}]}
    tree = leaf
    for ancestor in range(MAX_NATIVE_AX_DEPTH - 1):
        tree = {"role": "AXWebArea" if ancestor % 2 else "AXGroup", "children": [tree]}
    projected = json.dumps(_bounded_ax_tree(tree))
    assert "last-native-level" in projected
    assert "too-deep" not in projected

    metadata = {"leaf": "outside-generic-depth"}
    for _ in range(13):
        metadata = {"nested": metadata}
    bounded = _bounded_public_value(metadata)
    assert "<truncated>" in json.dumps(bounded)
    assert "outside-generic-depth" not in json.dumps(bounded)
    assert _bounded_ax_tree(metadata) == bounded
    assert _bounded_ax_tree({"role": "AXGroup", "metadata": metadata})["metadata"] == bounded


def test_ax_projection_preserves_boundary_attributes_and_redacts_secure_subrole():
    from agent.runtime.tools.computer import _bounded_ax_tree

    tree = {"role": "AXTextField", "subrole": "AXSecureTextField", "value": "PRIVATE",
            "focused": True, "element_ref": "secure-ref", "children": [{"role": "AXButton"}]}
    bounded = _bounded_ax_tree({"role": "AXWindow", "children": [tree]}, max_depth=1)
    child = bounded["children"][0]
    assert child["element_ref"] == "secure-ref"
    assert child["focused"] is True
    assert child["value"] == "<redacted>"
    assert child["children"] == ["<truncated>"]
    assert "PRIVATE" not in json.dumps(bounded)


def test_ax_element_budget_is_not_consumed_by_toolbar_scalar_attributes(monkeypatch):
    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(computer_tools, "_MAX_AX_NODES", 20)
    buttons = [{"role": "AXButton", "title": str(i), "label": "tool", "enabled": True,
                "actions": ["AXPress"], "bounds": {"x": i, "y": 1, "width": 10, "height": 10},
                "element_ref": f"button-{i}"} for i in range(10)]
    field = {"role": "AXTextField", "focused": True, "element_ref": "source"}
    bounded = computer_tools._bounded_ax_tree({"role": "AXWindow", "children": [*buttons, field]})
    assert bounded["children"][-1] == field
    too_many = computer_tools._bounded_ax_tree({"role": "AXWindow", "children": buttons * 3})
    assert json.dumps(too_many).count('"AXButton"') <= 19


def test_snapshot_is_request_local_original_image_with_bounded_ax(registry, manager):
    observed = []
    registry.hooks.on_tool_result(
        lambda name, args, result, _tool: observed.append((name, args, result))
    )
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    payload = json.loads(result["fresh_output"])
    assert result["request_local_placeholder"] is True
    assert result["output"] != result["fresh_output"]
    assert result["verified"] is True
    assert payload["type"] == "image_attachment"
    assert payload["detail"] == "original"
    assert payload["session_id"] == manager.session_id
    assert payload["window_id"] == "window-1"
    assert payload["snapshot_id"] == "snapshot-1"
    assert len(payload["image_paths"]) == 1
    assert payload["ax_tree"]["children"][0]["element_ref"] == "snapshot-1:1"
    assert result["_private_result"]["image_data_urls"][0].startswith("data:image/png;base64,")
    durable = json.dumps({key: value for key, value in result.items() if not key.startswith("_") and key != "fresh_output"})
    assert "image_paths" not in durable
    assert "AXButton" not in durable
    assert "iVBOR" not in durable
    snapshot_observation = next(item for item in observed if item[0] == "computer_snapshot")
    observed_json = json.dumps(snapshot_observation, ensure_ascii=False)
    assert "image_paths" not in observed_json
    assert "AXButton" not in observed_json
    assert "iVBOR" not in observed_json
    assert "fresh_output" not in observed_json


def test_snapshot_public_payload_copies_trusted_default_button_disclosure(
    registry,
    manager,
    backend,
):
    backend.has_default_button = True
    backend.default_button_element_ref = "snapshot-1:export"
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    payload = json.loads(result["fresh_output"])
    assert payload["has_default_button"] is True
    assert payload["default_button_element_ref"] == "snapshot-1:export"


def test_snapshot_public_payload_omits_default_button_ref_when_ax_tree_is_truncated(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    backend.has_default_button = True
    backend.default_button_element_ref = "snapshot-1:export"
    monkeypatch.setattr(computer_tools, "_MAX_AX_JSON_BYTES", 1)
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    payload = json.loads(result["fresh_output"])
    assert payload["has_default_button"] is True
    assert payload["ax_tree"]["truncated"] is True
    assert "default_button_element_ref" not in payload


def test_snapshot_subtree_ref_reanchors_depth_budget_to_reach_deep_web_content(
    registry,
    manager,
    backend,
    monkeypatch,
):
    """A deliberately narrow public budget can still be reanchored on a visible ref."""

    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(computer_tools, "_MAX_AX_ELEMENT_DEPTH", 12)

    anchor_ref = "snapshot-1:anchor"
    deep_ref = "snapshot-1:deep-chat-link"
    node: dict[str, object] = {"role": "AXLink", "element_ref": deep_ref, "title": "聊天"}
    for _ in range(12):  # Beyond the default element-depth limit, within the anchored limit.
        node = {"role": "AXGroup", "children": [node]}
    anchor: dict[str, object] = {"role": "AXScrollArea", "element_ref": anchor_ref, "children": [node]}
    backend.snapshot_ax_tree_override = {
        "role": "AXWindow",
        "children": [{"role": "AXGroup", "children": [anchor]}],
    }
    register(registry, manager)
    focus(registry)

    plain = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    # 该节点超过整窗口的元素深度上限，必须先从已公开的锚点重新计层。
    assert deep_ref not in json.dumps(plain["ax_tree"], ensure_ascii=False)

    expanded = json.loads(
        run(
            registry.execute(
                "computer_snapshot",
                {"scope": "target_window", "subtree_ref": anchor_ref},
            )
        )["fresh_output"]
    )
    assert deep_ref in json.dumps(expanded["ax_tree"], ensure_ascii=False)


def _deep_web_tree(anchor_ref: str, deep_ref: str) -> dict[str, object]:
    leaf: dict[str, object] = {"role": "AXLink", "element_ref": deep_ref, "title": "聊天"}
    for _ in range(12):
        leaf = {"role": "AXGroup", "children": [leaf]}
    return {
        "role": "AXWindow",
        "children": [{
            "role": "AXGroup",
            "children": [{
                "role": "AXScrollArea",
                "element_ref": anchor_ref,
                "bounds": {"x": 256, "y": 90, "width": 1182, "height": 769},
                "children": [leaf],
            }],
        }],
    }


def test_snapshot_subtree_ref_resolves_anchor_after_refs_are_regenerated(
    registry,
    manager,
    backend,
):
    """实机暴露的缺陷：element_ref 每次快照都重新生成（同一稳定 DOM 三次得到三个不同 ref），
    而 subtree_ref 必然来自上一轮公开的投影 —— 按字面在新树里查锚点必定失配。
    锚点必须按稳定身份（role+bounds）映射到本次快照的同一元素。"""

    register(registry, manager)
    focus(registry)

    backend.snapshot_ax_tree_override = _deep_web_tree("snapshot-1:anchor-v1", "snapshot-1:deep-v1")
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    assert "snapshot-1:anchor-v1" in json.dumps(first["ax_tree"])
    assert "snapshot-1:deep-v1" in json.dumps(first["ax_tree"])

    backend.snapshot_ax_tree_override = _deep_web_tree("snapshot-1:anchor-v2", "snapshot-1:deep-v2")
    expanded = json.loads(
        run(registry.execute("computer_snapshot", {
            "scope": "target_window",
            "subtree_ref": "snapshot-1:anchor-v1",
        }))["fresh_output"]
    )
    blob = json.dumps(expanded["ax_tree"], ensure_ascii=False)
    assert "snapshot-1:deep-v2" in blob, f"anchor must map across regenerated refs, got: {blob[:300]}"


def test_subtree_ref_resolves_anchor_published_by_get_app_state(
    registry,
    manager,
    backend,
):
    """实机走的是 get_app_state 发布 → 下一次 computer_snapshot 展开。上一版把锚点来源接在
    manager 内部快照生命周期上，实机直接 prior_tree_present=False，而单元测用 stub 直连同一
    条 payload 键、抓不到这条路径 —— 此用例锁住跨工具的锚点可达。"""

    register(registry, manager)
    focus(registry)
    backend.snapshot_ax_tree_override = _deep_web_tree("snapshot-1:anchor-v1", "snapshot-1:deep-v1")

    published = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))
    assert "fresh_output" in published, json.dumps(published, ensure_ascii=False)[:300]

    backend.snapshot_ax_tree_override = _deep_web_tree("snapshot-1:anchor-v2", "snapshot-1:deep-v2")
    expanded = json.loads(
        run(registry.execute("computer_snapshot", {
            "scope": "target_window",
            "subtree_ref": "snapshot-1:anchor-v1",
        }))["fresh_output"]
    )
    blob = json.dumps(expanded["ax_tree"], ensure_ascii=False)
    assert "snapshot-1:deep-v2" in blob, f"anchor published by get_app_state must resolve: {blob[:300]}"


def test_computer_snapshot_role_filter_reaches_target_role_despite_a_huge_sibling_list(
    registry,
    manager,
    backend,
    monkeypatch,
):
    """实机缺口：oMLX 聊天页侧栏 227 条对话把整棵展开预算吃光，右栏输入框的 ref 永远进不来。

    角色过滤必须在剪枝之后再受预算约束 —— 于是"一堵同名兄弟墙"不能再挤掉唯一的 AXTextArea。
    """

    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(computer_tools, "_MAX_AX_NODES", 60)  # 远小于侧栏条目数
    composer_ref = "snapshot-1:composer"
    sidebar = {
        "role": "AXGroup",
        "children": [
            {"role": "AXButton", "element_ref": f"snapshot-1:row-{i}", "title": f"对话 {i}"}
            for i in range(227)
        ],
    }
    # 关键：实机的输入框藏在 20+ 层里，而剪枝**保留祖先链＝保留原始深度**。把 composer 放浅处
    # 的用例曾经"绿着放过"这个 bug（实机 role_filter 仍然截断），所以这里必须按真实几何摆深。
    composer: dict[str, object] = {
        "role": "AXTextArea",
        "element_ref": composer_ref,
        "title": "输入消息…",
    }
    for _ in range(16):
        composer = {"role": "AXGroup", "children": [composer]}
    backend.snapshot_ax_tree_override = {"role": "AXWindow", "children": [sidebar, composer]}
    register(registry, manager)
    focus(registry)

    plain = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    assert composer_ref not in json.dumps(plain["ax_tree"]), "前提：同名兄弟墙确实挤掉了目标"

    filtered = json.loads(
        run(registry.execute("computer_snapshot", {
            "scope": "target_window",
            "role_filter": "AXTextArea",
        }))["fresh_output"]
    )
    blob = json.dumps(filtered["ax_tree"], ensure_ascii=False)
    assert composer_ref in blob, f"role_filter 应直达目标角色，得到：{blob[:200]}"
    assert "snapshot-1:row-9" not in blob, "过滤后不该再把整堵墙塞回载荷"


def test_computer_snapshot_role_filter_that_matches_nothing_reports_invalid_arguments(
    registry,
    manager,
    backend,
):
    """过滤后什么都没有了，必须明说，不能假装"剪枝成功"或回一棵空树让人继续猜。"""

    backend.snapshot_ax_tree_override = {
        "role": "AXWindow",
        "children": [{"role": "AXButton", "element_ref": "snapshot-1:b", "title": "x"}],
    }
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "role_filter": "AXTextArea",
    }))
    blob = json.dumps(result, ensure_ascii=False)
    assert result["code"] == "invalid_arguments"
    # 必须说明是"过滤后无命中"，否则与"参数被 schema 拒"（未知 role_filter）看起来一模一样 ——
    # 那条区分不开的用例在本轮之前就侥幸绿过一次。
    assert "matched no element" in blob, f"报错要能区分无命中与参数被拒：{blob[:200]}"


def test_snapshot_subtree_ref_on_unknown_anchor_does_not_claim_stale_snapshot(
    registry,
    manager,
    backend,
):
    """锚点不存在时报**可区分**的错，不许混进 stale_snapshot —— 今晚 16 小时的归因灾难
    正是来自一个码被 20+ 处共用（见 docs/macos-computer-use.md#targeted-subtree-observations）。"""

    backend.snapshot_ax_tree_override = {
        "role": "AXWindow",
        "children": [{"role": "AXScrollArea", "element_ref": "snapshot-1:anchor"}],
    }
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "subtree_ref": "snapshot-1:does-not-exist",
    }))
    blob = json.dumps(result, ensure_ascii=False)
    assert "stale_target" in blob, f"unknown anchor must report stale_target, got: {blob[:300]}"
    assert result.get("code") != "stale_snapshot"


def test_snapshot_subtree_ref_over_budget_fails_without_dropping_whole_tree(
    registry,
    manager,
    backend,
    monkeypatch,
):
    """定向展开超预算时明确失败并给出可执行提示；不得学整窗口那样把结果替换成
    {"truncated": true} —— 那等于丢掉已经拿到的深层数据，比不展开更瞎。"""

    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(computer_tools, "_MAX_AX_JSON_BYTES", 1)
    backend.snapshot_ax_tree_override = {
        "role": "AXWindow",
        "children": [{
            "role": "AXScrollArea",
            "element_ref": "snapshot-1:anchor",
            "children": [{"role": "AXLink", "element_ref": "snapshot-1:deep", "title": "聊天"}],
        }],
    }
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "subtree_ref": "snapshot-1:anchor",
    }))
    blob = json.dumps(result, ensure_ascii=False)
    assert result["code"] == "snapshot_failed", f"expected honest failure, got: {blob[:300]}"
    assert result["retryable"] is False
    assert "narrower" in blob  # 提示可执行：换一个更窄的锚点
    assert '"truncated": true' not in blob  # 不得整树丢弃


def test_snapshot_public_payload_retains_native_default_button_at_deep_valid_depth(
    registry,
    manager,
    backend,
):
    reference = "snapshot-1:deep-default"
    tree: dict[str, object] = {"role": "AXButton", "element_ref": reference}
    for _ in range(14):
        tree = {"role": "AXGroup", "children": [tree]}
    backend.snapshot_ax_tree_override = tree
    backend.has_default_button = True
    backend.default_button_element_ref = reference
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    payload = json.loads(result["fresh_output"])
    assert payload["has_default_button"] is True
    assert payload["default_button_element_ref"] == reference


@pytest.mark.parametrize(("attribute", "expected"), [("invalid_png", "PNG"), ("wrong_mode", "0600")])
def test_snapshot_postcondition_rejects_unsafe_artifacts(registry, manager, backend, attribute, expected):
    setattr(backend, attribute, True)
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    assert result["code"] == "postcondition_failed"
    assert expected.lower() in result["verification_detail"].lower()
    assert "fresh_output" not in result
    assert "_private_result" not in result


def test_postcondition_uses_held_directory_identity_after_path_replacement(registry, manager, tmp_path):
    register(registry, manager)
    focus(registry)
    original_root = manager.session_dir.parent
    moved_root = tmp_path / "moved-cache"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_root.rename(moved_root)
    original_root.symlink_to(outside, target_is_directory=True)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    assert result["error"] == ""
    assert result["verified"] is True
    assert list(outside.iterdir()) == []


def test_verified_png_rejects_hardlinked_file_from_outside_session(manager, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    outside.chmod(0o600)
    os.link(outside, "linked.png", dst_dir_fd=manager._session_fd)

    with pytest.raises(ComputerSessionError, match="link"):
        manager.read_verified_png_artifact("linked.png")


def test_verified_png_rejects_crc_valid_but_undecodable_pixels(manager):
    manager.write_artifact("bad-idat.png", png_with_replaced_idat(b"not-zlib-data"))

    with pytest.raises(ComputerSessionError, match="pixel|compressed"):
        manager.read_verified_png_artifact("bad-idat.png")


def test_postcondition_rejects_valid_png_swapped_after_private_read(registry, manager, monkeypatch):
    register(registry, manager)
    focus(registry)
    original_read = manager.read_verified_png_artifact
    reads = 0

    def read_then_swap(filename, **kwargs):
        nonlocal reads
        verified = original_read(filename, **kwargs)
        reads += 1
        if reads == 1:
            os.rename(filename, f"old-{filename}", src_dir_fd=manager._session_fd, dst_dir_fd=manager._session_fd)
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=manager._session_fd,
            )
            try:
                os.write(descriptor, PNG)
            finally:
                os.close(descriptor)
        return verified

    monkeypatch.setattr(manager, "read_verified_png_artifact", read_then_swap)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    assert result["code"] == "postcondition_failed"
    assert "identity" in result["verification_detail"]
    assert "fresh_output" not in result


def test_postcondition_rejects_in_place_png_rewrite_after_private_read(
    registry, manager, monkeypatch,
):
    register(registry, manager)
    focus(registry)
    original_read = manager.read_verified_png_artifact
    reads = 0
    replacement = png_with_replaced_idat(zlib.compress(b"\x00\xff\x00\x00\xff"))

    def read_then_rewrite(filename, **kwargs):
        nonlocal reads
        verified = original_read(filename, **kwargs)
        reads += 1
        if reads == 1:
            descriptor = os.open(filename, os.O_WRONLY | os.O_TRUNC, dir_fd=manager._session_fd)
            try:
                os.write(descriptor, replacement)
            finally:
                os.close(descriptor)
        return verified

    monkeypatch.setattr(manager, "read_verified_png_artifact", read_then_rewrite)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    assert result["code"] == "postcondition_failed"
    assert "content" in result["verification_detail"]
    assert "fresh_output" not in result


def test_snapshot_rejects_helper_reusing_an_older_session_artifact(
    registry, manager, backend, monkeypatch,
):
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    original_snapshot = backend.snapshot

    async def reuse_old_artifact(target, scope, artifact):
        fresh = await original_snapshot(target, scope, artifact)
        return ComputerSnapshot(
            fresh.snapshot_id,
            {**fresh.payload, "image_artifact": first["image_artifact"]},
        )

    monkeypatch.setattr(backend, "snapshot", reuse_old_artifact)

    result = run(registry.execute("computer_snapshot", {"scope": "target_window"}))

    assert result["code"] == "unsafe_artifact"
    assert "fresh_output" not in result


def test_schema_rejects_unknown_and_invalid_actions_before_approval(registry, manager, backend):
    approvals = []

    async def approve(request):
        approvals.append(request)
        return "once"

    registry.set_approval_handler(approve)
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])

    unknown = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1", "path": "/tmp/no"}],
    }))
    invalid = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "type"}],
    }))
    missing_target = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click"}],
    }))
    unsupported = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "launch", "x": 1, "y": 1}],
    }))
    registry.policy.mode = "locked"
    clipboard_paste = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "keypress", "key": "v", "modifiers": ["command"]}],
    }))

    assert unknown["code"] == "invalid_arguments"
    assert invalid["code"] == "invalid_arguments"
    assert missing_target["code"] == "invalid_arguments"
    assert unsupported["code"] == "invalid_arguments"
    assert clipboard_paste["code"] == "invalid_arguments"
    assert approvals == []
    assert not any(name == "act" for name, _ in backend.calls)


def test_act_never_retries_unknown_outcome_and_does_not_persist_credentials(registry, manager, backend):
    observed = []
    approvals = []
    helper_secret = "HELPER-PRIVATE-DIAGNOSTIC"
    registry.hooks.on_tool_result(lambda name, args, result, _tool: observed.append((name, args, result)))

    async def approve(request):
        approvals.append(request)
        return "once"

    registry.set_approval_handler(approve)
    backend.act_result = ComputerActionResult(
        result={
            "outcomes": [{"index": 0, "ok": False, "error_code": "unknown_outcome"}],
            "last_acknowledged_action": -1,
            "debug_value": helper_secret,
        },
        error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "action outcome is unknown"),
    )
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "type", "text": "api_key=TOP-SECRET-TEXT"}],
    }))

    assert result["code"] == "unknown_outcome"
    assert result["retryable"] is False
    assert result["recoverable"] is False
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert sum(name == "snapshot" for name, _ in backend.calls) == 1
    assert "api_key=TOP-SECRET-TEXT" not in json.dumps(observed)
    assert "api_key=TOP-SECRET-TEXT" not in json.dumps(approvals)
    assert helper_secret not in json.dumps(result)


def test_input_focus_required_returns_bounded_fresh_snapshot_recovery(registry, manager, backend):
    backend.act_result = ComputerActionResult(
        result={
            "outcomes": [
                {"index": 0, "ok": False, "error_code": "input_focus_required"},
            ],
            "last_acknowledged_action": -1,
        },
        error=ComputerError(
            ComputerErrorCode.INPUT_FOCUS_REQUIRED,
            "PRIVATE-FOCUS-DIAGNOSTIC",
        ),
    )
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{
            "type": "type",
            "text": "must-not-be-persisted",
            "element_ref": f"{snapshot['snapshot_id']}:1",
        }],
    }))

    assert result["code"] == "input_focus_required"
    assert result["retryable"] is True
    assert "focus" in result["recovery_hint"].lower()
    assert "fresh snapshot" in result["recovery_hint"].lower()
    assert "must-not-be-persisted" not in json.dumps(result)
    assert "PRIVATE-FOCUS-DIAGNOSTIC" not in json.dumps(result)


@pytest.mark.parametrize(
    ("outcomes", "acknowledgement", "expected_retryable", "expect_no_repeat"),
    [
        (
            [
                {"index": 0, "ok": True},
                {"index": 1, "ok": False, "error_code": "stale_snapshot"},
            ],
            0,
            False,
            True,
        ),
        (
            [{"index": 0, "ok": False, "error_code": "stale_snapshot"}],
            -1,
            True,
            False,
        ),
    ],
)
def test_native_failed_act_receipt_survives_adapter_manager_and_tool(
    registry,
    manager,
    backend,
    monkeypatch,
    outcomes,
    acknowledgement,
    expected_retryable,
    expect_no_repeat,
):
    transport = NativeActReceiptTransport(
        outcomes=outcomes,
        acknowledgement=acknowledgement,
        error=ComputerError(ComputerErrorCode.STALE_SNAPSHOT, "PRIVATE-NATIVE-STALE"),
    )
    route_act_through_native_adapter(backend, monkeypatch, transport)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.calls.clear()
    actions = [
        {"type": "click", "element_ref": f"{snapshot['snapshot_id']}:1"}
        for _outcome in outcomes
    ]

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": actions,
    }))

    assert result["code"] == "stale_snapshot"
    assert result["error"] == "The action snapshot is stale."
    assert result["retryable"] is expected_retryable
    assert result["recoverable"] is expected_retryable
    assert ("Do not repeat this action batch" in result["recovery_hint"]) is expect_no_repeat
    expected_details = {
        "outcomes": outcomes,
        "last_acknowledged_action": acknowledgement,
    }
    if acknowledgement >= 0 or any(outcome.get("ok") is True for outcome in outcomes):
        expected_details.update({
            "status": "action_acknowledged",
            "effect_verification": "unverified",
        })
    assert result["details"] == expected_details
    assert "PRIVATE-NATIVE-STALE" not in json.dumps(result)
    assert [request.operation for request in transport.requests] == ["act"]
    assert [name for name, _value in backend.calls] == ["plan_actions", "act"]


def test_ordinary_action_approval_is_bound_to_session_bundle_and_current_window(
    registry,
    manager,
    backend,
):
    requests = []

    async def approve_session(request):
        requests.append(request)
        return "session"

    registry.set_approval_handler(approve_session)
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    acted = run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }))
    second = json.loads(acted["fresh_output"])
    acted_again = run(registry.execute("computer_act", {
        "snapshot_id": second["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-2:1"}],
    }))

    assert acted_again["error"] == ""
    assert len(requests) == 1
    assert "scope" not in requests[0]
    assert requests[0]["arguments"]["application"] == "Fixture"
    assert requests[0]["choices"] == ["once", "session", "deny"]

    run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-2"}))
    third = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    changed_window = run(registry.execute("computer_act", {
        "snapshot_id": third["snapshot_id"],
        "actions": [{"type": "click", "element_ref": f'{third["snapshot_id"]}:1'}],
    }))

    assert changed_window["error"] == ""
    assert len(requests) == 2
    assert "Second Fixture" in requests[1]["target"]


def test_finder_path_bar_is_a_trusted_exact_approval_directory(registry, manager, backend):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    backend.catalog_bundle_id = "com.apple.finder"
    backend.finder_path_parts = ["Macintosh HD", "private", "tmp", "astra-e2e"]
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "keypress", "key": "c", "modifiers": ["command"]}],
    }))

    assert result["error"] == ""
    assert "/private/tmp/astra-e2e" not in json.dumps(requests[0])
    assert "known_path" not in requests[0]["arguments"]


def test_finder_exact_path_survives_a_same_target_transient_snapshot(registry, manager, backend):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    backend.catalog_bundle_id = "com.apple.finder"
    backend.finder_path_parts = ["Macintosh HD", "private", "tmp", "astra-e2e"]
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.finder_path_parts = []
    transient = json.loads(run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "keypress", "key": "return"}],
    }))["fresh_output"])

    result = run(registry.execute("computer_act", {
        "snapshot_id": transient["snapshot_id"],
        "actions": [{"type": "keypress", "key": "a", "modifiers": ["command"]}],
    }))

    assert result["error"] == ""
    assert len(requests) == 2
    assert all("/private/tmp/astra-e2e" not in json.dumps(request) for request in requests)
    assert all("known_path" not in request["arguments"] for request in requests)


def test_wps_confirmed_save_directory_and_filename_bind_exact_approval_path(
    registry, manager, backend, tmp_path,
):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    destination = tmp_path / "private-output"
    destination.mkdir(mode=0o700)
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    backend.focused_role = "AXTextField"
    backend.focused_value = str(destination)
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.focused_value = "saved-copy.docx"
    _named = json.loads(run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "keypress", "key": "a", "modifiers": ["command"]}],
    }))["fresh_output"])
    backend.catalog_window_title = "另存为新格式"
    run(registry.execute("computer_apps", {}))
    run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}))
    backend.focused_role = "AXButton"
    backend.focused_value = ""
    confirmation = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": confirmation["snapshot_id"],
        "actions": [{"type": "click", "element_ref": confirmation["ax_tree"]["children"][0]["element_ref"]}],
    }))

    assert result["error"] == ""
    assert str(destination / "saved-copy.docx") not in json.dumps(requests[-1])
    assert "known_path" not in requests[-1]["arguments"]


def test_wps_save_as_refocus_preserves_exact_destination_path(
    registry, manager, backend, tmp_path,
):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    destination = tmp_path / "private-output"
    destination.mkdir(mode=0o700)
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    backend.catalog_window_title = "document.docx"
    backend.snapshot_root_role = "AXSheet"
    backend.snapshot_root_label = "前往文件夹"
    backend.focused_role = "AXTextField"
    backend.focused_value = str(destination)
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.focused_value = "saved-copy.docx"
    named = json.loads(run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "keypress", "key": "a", "modifiers": ["command"]}],
    }))["fresh_output"])
    probe = run(registry.execute("computer_act", {
        "snapshot_id": named["snapshot_id"],
        "actions": [{"type": "click", "element_ref": named["ax_tree"]["children"][1]["element_ref"]}],
    }))
    assert probe["error"] == ""
    assert str(destination / "saved-copy.docx") not in json.dumps(requests[-1])
    assert "known_path" not in requests[-1]["arguments"]

    run(registry.execute("computer_apps", {}))
    run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}))
    backend.focused_role = "AXButton"
    backend.focused_value = ""
    confirmation = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    result = run(registry.execute("computer_act", {
        "snapshot_id": confirmation["snapshot_id"],
        "actions": [{"type": "click", "element_ref": confirmation["ax_tree"]["children"][1]["element_ref"]}],
    }))

    assert result["error"] == ""
    assert str(destination / "saved-copy.docx") not in json.dumps(requests[-1])
    assert "known_path" not in requests[-1]["arguments"]


def test_wps_export_dialog_basename_binds_exact_pdf_approval_path(
    registry, manager, backend, tmp_path,
):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    destination = tmp_path / "private-output"
    destination.mkdir(mode=0o700)
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    backend.catalog_window_title = "输出为PDF"
    backend.focused_role = "AXTextField"
    backend.focused_value = str(destination)
    register(registry, manager)
    focus(registry)
    directory = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.focused_value = "exported-copy"
    named = json.loads(run(registry.execute("computer_act", {
        "snapshot_id": directory["snapshot_id"],
        "actions": [{"type": "keypress", "key": "a", "modifiers": ["command"]}],
    }))["fresh_output"])
    backend.focused_role = "AXButton"
    backend.focused_value = ""

    result = run(registry.execute("computer_act", {
        "snapshot_id": named["snapshot_id"],
        "actions": [{"type": "click", "element_ref": named["ax_tree"]["children"][1]["element_ref"]}],
    }))

    assert result["error"] == ""
    assert str(destination / "exported-copy.pdf") not in json.dumps(requests[-1])
    assert "known_path" not in requests[-1]["arguments"]


def test_wps_pdf_destination_rebases_after_custom_directory_change(
    registry, manager, backend, tmp_path,
):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    first_directory = tmp_path / "first"
    final_directory = tmp_path / "final"
    first_directory.mkdir(mode=0o700)
    final_directory.mkdir(mode=0o700)
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    backend.catalog_window_title = "输出为PDF"
    backend.focused_role = "AXTextField"
    backend.focused_value = str(first_directory)
    register(registry, manager)
    focus(registry)
    initial = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.focused_value = "exported-copy"
    named = json.loads(run(registry.execute("computer_act", {
        "snapshot_id": initial["snapshot_id"],
        "actions": [{"type": "keypress", "key": "a", "modifiers": ["command"]}],
    }))["fresh_output"])
    del named
    backend.focused_value = str(final_directory)
    directory_changed = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.focused_role = "AXButton"
    backend.focused_value = ""
    ready = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": ready["snapshot_id"],
        "actions": [{"type": "click", "element_ref": ready["ax_tree"]["children"][1]["element_ref"]}],
    }))

    assert directory_changed["snapshot_id"] != initial["snapshot_id"]
    assert result["error"] == ""
    assert str(final_directory / "exported-copy.pdf") not in json.dumps(requests[-1])
    assert "known_path" not in requests[-1]["arguments"]


def test_wps_destination_does_not_cross_into_unrelated_wps_window(
    registry, manager, backend, tmp_path,
):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    destination = tmp_path / "private-output"
    destination.mkdir(mode=0o700)
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    backend.catalog_window_title = "输出为PDF"
    backend.focused_role = "AXTextField"
    backend.focused_value = str(destination)
    register(registry, manager)
    focus(registry)
    directory = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.focused_value = "exported-copy"
    _named = json.loads(run(registry.execute("computer_act", {
        "snapshot_id": directory["snapshot_id"],
        "actions": [{"type": "keypress", "key": "a", "modifiers": ["command"]}],
    }))["fresh_output"])

    backend.catalog_window_title = "Unrelated WPS window"
    run(registry.execute("computer_apps", {}))
    run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}))
    backend.focused_role = "AXButton"
    backend.focused_value = ""
    unrelated = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    result = run(registry.execute("computer_act", {
        "snapshot_id": unrelated["snapshot_id"],
        "actions": [{"type": "click", "element_ref": unrelated["ax_tree"]["children"][1]["element_ref"]}],
    }))

    assert result["error"] == ""
    assert "known_path" not in requests[-1]["arguments"]


def test_wps_destination_is_cleared_when_computer_session_closes(
    registry, manager, backend, tmp_path, monkeypatch,
):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(approve)
    destination = tmp_path / "private-output"
    destination.mkdir(mode=0o700)
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    backend.catalog_window_title = "输出为PDF"
    backend.focused_role = "AXTextField"
    backend.focused_value = str(destination)
    register(registry, manager)
    focus(registry)
    directory = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.focused_value = "exported-copy"
    _named = json.loads(run(registry.execute("computer_act", {
        "snapshot_id": directory["snapshot_id"],
        "actions": [{"type": "keypress", "key": "a", "modifiers": ["command"]}],
    }))["fresh_output"])

    async def close_without_terminating_manager():
        return None

    monkeypatch.setattr(manager, "close", close_without_terminating_manager)
    assert run(registry.execute("computer_close", {}))["error"] == ""

    run(registry.execute("computer_apps", {}))
    run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}))
    backend.focused_role = "AXButton"
    backend.focused_value = ""
    fresh_session = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    result = run(registry.execute("computer_act", {
        "snapshot_id": fresh_session["snapshot_id"],
        "actions": [{"type": "click", "element_ref": fresh_session["ax_tree"]["children"][1]["element_ref"]}],
    }))

    assert result["error"] == ""
    assert "known_path" not in requests[-1]["arguments"]


def test_high_impact_action_is_once_per_exact_batch_and_session_decision_cannot_broaden(
    registry,
    manager,
):
    requests = []

    async def try_session(request):
        requests.append(request)
        return "session"

    registry.set_approval_handler(try_session)
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    first_result = run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:export"}],
    }))
    second = json.loads(first_result["fresh_output"])
    second_result = run(registry.execute("computer_act", {
        "snapshot_id": second["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-2:export"}],
    }))

    assert second_result["error"] == ""
    assert len(requests) == 2
    assert all(request["choices"] == ["once", "deny"] for request in requests)
    assert all("scope" not in request for request in requests)


def test_approval_cannot_race_a_target_change_into_a_new_window_grant(
    registry,
    manager,
    backend,
):
    async def switch_window_then_approve(_request):
        switched = await registry.execute(
            "computer_focus",
            {"app_ref": "app-1", "window_ref": "window-2"},
        )
        assert switched["error"] == ""
        return "session"

    registry.set_approval_handler(switch_window_then_approve)
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    raced = run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }))

    assert raced["error"]
    assert manager.target is not None and manager.target.window_ref == "window-2"
    assert not any(name == "act" for name, _ in backend.calls)
    assert not any(
        scope.startswith(f"computer-write:{manager.session_id}:")
        for scope in registry.approved_permission_scopes
    )


def test_denied_action_and_prohibited_action_never_reach_helper(
    registry,
    manager,
    backend,
):
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    denied = run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }))

    assert denied["code"] == "approval_denied"
    assert not any(name == "act" for name, _ in backend.calls)

    second = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    coordinate_prohibited = run(registry.execute("computer_act", {
        "snapshot_id": second["snapshot_id"],
        "actions": [{"type": "click", "x": 20, "y": 510}],
    }))

    assert coordinate_prohibited["code"] == "computer_handoff_required"
    assert not any(name == "act" for name, _ in backend.calls)

    registry.yolo = True
    registry.policy.allow("computer_act")
    manager.grants.add("computer:interaction-session")
    third = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    prohibited = run(registry.execute("computer_act", {
        "snapshot_id": third["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-3:secure"}],
    }))

    assert prohibited["code"] == "computer_handoff_required"
    assert prohibited["retryable"] is False
    assert manager.handed_off is False
    assert len(requests) == 1
    assert not any(name == "act" for name, _ in backend.calls)


def test_coordinate_pointer_target_element_ref_passes_tool_schema(
    registry,
    manager,
    backend,
):
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="deny"))
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{
            "type": "click",
            "x": 20,
            "y": 20,
            "target_element_ref": "snapshot-1:1",
        }],
    }))

    assert result["code"] == "approval_denied"
    assert not any(name == "act" for name, _ in backend.calls)


@pytest.mark.parametrize(
    "action",
    [
        {"type": "scroll", "delta_y": 120},
        {
            "type": "click",
            "x": 20,
            "y": 20,
            "element_ref": "snapshot-1:1",
        },
        {
            "type": "click",
            "x": 20,
            "y": 20,
            "element_ref": "snapshot-1:1",
            "target_element_ref": "snapshot-1:1",
        },
    ],
)
def test_pointer_schema_rejects_missing_or_mixed_safe_region_authority(
    registry,
    manager,
    backend,
    action,
):
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [action],
    }))

    assert result["code"] == "invalid_arguments"
    assert not any(name == "act" for name, _ in backend.calls)


@pytest.mark.parametrize(
    "action",
    [
        {
            "type": "click",
            "x": 20,
            "y": 20,
            "element_ref": "snapshot-1:1",
        },
        {
            "type": "click",
            "element_ref": "snapshot-1:1",
            "target_element_ref": "snapshot-1:1",
        },
        {
            "type": "click",
            "x": 20,
            "y": 20,
            "element_ref": "snapshot-1:1",
            "target_element_ref": "snapshot-1:1",
        },
    ],
)
def test_action_schema_itself_rejects_semantic_and_pointer_field_mixing(action):
    from agent.runtime.tools.computer import ACTION_SCHEMA

    assert ToolRegistry._schema_errors(ACTION_SCHEMA, action, strict=True)


def test_approval_payload_redacts_typed_text_and_request_local_paths(
    registry,
    manager,
    backend,
):
    requests = []
    secret = "APPROVAL-MUST-NOT-STORE-ME"

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    register(registry, manager)
    focus(registry)
    backend.ax_known_path = f"/var/tmp/{manager.session_id}/snapshot-private.png"
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    result = run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "type", "text": secret, "element_ref": "snapshot-1:1"}],
    }))

    assert result["code"] == "approval_denied"
    encoded = json.dumps(requests, ensure_ascii=False)
    assert secret not in encoded
    assert str(manager.session_dir) not in encoded
    assert backend.ax_known_path not in encoded
    assert requests[0]["approval_effect"]
    assert requests[0]["approval_boundary"]
    assert requests[0]["approval_question"]
    assert requests[0]["arguments"]["application"] == "Fixture"


def test_non_window_ax_root_cannot_fill_missing_catalog_window_title(
    registry,
    manager,
    backend,
):
    requests = []
    backend.catalog_window_role = "AXWindow"
    backend.catalog_window_title = ""
    backend.snapshot_root_role = "AXButton"
    backend.snapshot_root_label = "Untrusted Root Label"

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }))

    assert result["code"] == "approval_denied"
    assert requests[0]["choices"] == ["once", "deny"]
    assert "Untrusted Root Label" not in requests[0]["target"]


def test_window_ax_root_rebinds_policy_title_to_contained_wps_overlay(
    registry,
    manager,
    backend,
):
    requests = []
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    backend.catalog_window_role = "AXWindow"
    backend.catalog_window_title = "document.docx"
    backend.snapshot_root_role = "AXWindow"
    backend.snapshot_root_label = "输出为PDF"
    backend.focused_role = "AXButton"

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    register(registry, manager)
    focus(registry)
    first = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "keypress", "key": "return"}],
    }))

    assert result["code"] == "approval_denied"
    assert requests[0]["arguments"]["window"] == "输出为PDF"


def test_pending_submit_blocks_ref_churn_but_allows_new_confirmation_button(registry, manager, backend):
    node = {"role": "AXButton", "label": "Submit", "element_ref": "submit-first", "observation_identity": "button-first", "enabled": True, "actions": ["AXPress"]}
    backend.snapshot_ax_tree_override = {"role": "AXWindow", "label": "Fixture", "children": [node]}
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    focus(registry)

    def observe():
        return json.loads(run(registry.execute("computer_snapshot", {"settle_ms": 1}))["fresh_output"])

    def click(snapshot, ref):
        return run(registry.execute("computer_act", {"snapshot_id": snapshot["snapshot_id"], "actions": [{"type": "click", "element_ref": ref}]}))

    first = click(observe(), "submit-first")
    assert not first.get("error"), first
    assert first["computer_receipt"]["next_step"] == "observe_result"
    node["element_ref"] = "submit-refreshed"
    second = click(observe(), "submit-refreshed")
    assert second["code"] == "effect_pending", second
    assert second["computer_receipt"]["dispatch_state"] == "not_dispatched"
    assert second["computer_receipt"]["next_step"] == "observe_result"
    assert sum(name == "act" for name, _ in backend.calls) == 1
    node["observation_identity"] = "new-confirmation-button"
    third = click(observe(), "submit-refreshed")
    assert not third.get("error"), third
    assert sum(name == "act" for name, _ in backend.calls) == 2


def test_settle_observation_is_cancellable_before_capture(registry, manager, backend):
    register(registry, manager)
    focus(registry)
    before = sum(name == "snapshot" for name, _ in backend.calls)

    async def scenario():
        task = asyncio.create_task(registry.execute("computer_snapshot", {"settle_ms": 2000}))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert sum(name == "snapshot" for name, _ in backend.calls) == before


def test_successful_act_and_resume_return_fresh_request_local_snapshots(registry, manager):
    async def approve(_request):
        return "session"

    registry.set_approval_handler(approve)
    register(registry, manager)
    focus(registry)
    first = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])

    acted = run(registry.execute("computer_act", {
        "snapshot_id": first["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }))
    acted_payload = json.loads(acted["fresh_output"])
    assert acted_payload["snapshot_id"] == "snapshot-2"
    assert acted_payload["action_result"] == {
        "outcomes": [{"index": 0, "ok": True}],
        "last_acknowledged_action": 0,
        "status": "action_acknowledged",
        "effect_verification": "unverified",
    }
    assert "does not prove" in acted_payload["message"]
    assert "fresh observation" in acted_payload["message"]

    handed_off = run(registry.execute("computer_handoff", {}))
    assert json.loads(handed_off["output"])["handoff_active"] is True
    blocked = run(registry.execute("computer_act", {
        "snapshot_id": acted_payload["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))
    assert "handoff_active" in blocked["error"]
    select_calls = sum(name == "select" for name, _value in manager._backend.calls)
    blocked_focus = run(registry.execute(
        "computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}
    ))
    refreshed_apps = run(registry.execute("computer_apps", {}))
    assert blocked_focus["code"] == "handoff_active"
    assert refreshed_apps["error"] == ""
    assert manager.handed_off is True
    assert sum(name == "select" for name, _value in manager._backend.calls) == select_calls

    resumed = run(registry.execute("computer_resume", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))
    resumed_payload = json.loads(resumed["fresh_output"])
    assert resumed_payload["snapshot_id"] == "snapshot-3"
    assert manager.handed_off is False


@pytest.mark.parametrize(
    ("outcomes", "acknowledgement", "expected"),
    [
        (
            [
                {"index": 0, "ok": True, "effect_verification": "verified"},
                {"index": 1, "ok": True, "effect_verification": "verified"},
            ],
            1,
            "verified",
        ),
        (
            [
                {"index": 0, "ok": True, "effect_verification": "verified"},
                {"index": 1, "ok": True, "effect_verification": "noop"},
            ],
            1,
            "noop",
        ),
        (
            [
                {"index": 0, "ok": True, "effect_verification": "verified"},
                {"index": 1, "ok": True, "effect_verification": "unverified"},
            ],
            1,
            "unverified",
        ),
        (
            [
                {"index": 0, "ok": True, "effect_verification": "verified"},
                {"index": 1, "ok": False, "error_code": "unknown_outcome"},
            ],
            0,
            "unverified",
        ),
    ],
)
def test_action_effect_verification_aggregates_only_native_per_action_records(
    outcomes,
    acknowledgement,
    expected,
):
    import agent.runtime.tools.computer as computer_tools

    metadata = computer_tools._model_action_metadata({
        "outcomes": outcomes,
        "last_acknowledged_action": acknowledgement,
        "effect_verification": "verified",
        "public_snapshot_diff": "changed",
    })

    assert metadata["effect_verification"] == expected
    assert "public_snapshot_diff" not in metadata
    if expected == "noop":
        assert metadata["verification_hint"]
        assert "fresh snapshot" in metadata["verification_hint"].lower()
        assert "different explicit action" in metadata["verification_hint"].lower()
    else:
        assert "verification_hint" not in metadata


def test_action_effect_verification_sanitizer_discards_unknown_values():
    import agent.runtime.tools.computer as computer_tools

    metadata = computer_tools._model_action_metadata({
        "outcomes": [
            {"index": 0, "ok": True, "effect_verification": "verified"},
            {"index": 1, "ok": True, "effect_verification": "changed"},
            {"index": 2, "ok": True, "effect_verification": 1},
        ],
        "last_acknowledged_action": 2,
    })

    assert metadata["outcomes"] == [
        {"index": 0, "ok": True, "effect_verification": "verified"},
        {"index": 1, "ok": True},
        {"index": 2, "ok": True},
    ]
    assert metadata["effect_verification"] == "unverified"


def test_display_scope_yolo_grant_is_temporary_and_normal_mode_still_asks(registry, manager):
    approval_requests = []

    async def approve_once(request):
        approval_requests.append(request)
        return "session" if len(approval_requests) == 1 else "deny"

    register(registry, manager)
    focus(registry)

    denied = run(registry.execute("computer_snapshot", {"scope": "display"}))
    assert denied["code"] == "approval_required"

    registry.yolo = True
    automatic = run(registry.execute("computer_snapshot", {"scope": "display"}))
    assert automatic["error"] == ""
    assert not registry.approved_permission_scopes

    registry.yolo = False
    registry.set_approval_handler(approve_once)
    allowed = run(registry.execute("computer_snapshot", {"scope": "display"}))
    assert allowed["error"] == ""
    public = json.loads(allowed["fresh_output"])
    assert public["display_id"] == 7
    assert public["target_window_bounds"] == {
        "x": 10,
        "y": 20,
        "width": 300,
        "height": 200,
    }
    assert "computer:display-once" not in manager.grants
    returned = run(registry.execute("computer_snapshot", {}))
    assert returned["error"] == ""
    denied_again = run(registry.execute("computer_snapshot", {"scope": "display"}))
    assert denied_again["code"] == "approval_denied"
    assert [value for name, value in manager._backend.calls if name == "snapshot"][-2:] == [
        "display",
        "target_window",
    ]
    assert len(approval_requests) == 2


def test_text_detail_on_returns_only_request_local_verified_path_metadata_and_summary(
    registry,
    manager,
    backend,
):
    approval_requests = []
    observed = []

    async def approve_once(request):
        approval_requests.append(request)
        return "once"

    registry.set_approval_handler(approve_once)
    registry.hooks.on_tool_result(
        lambda name, args, result, _tool: observed.append((name, args, result))
    )
    backend.text_detail_root = {
        "role": "AXWindow",
        "subrole": "AXStandardWindow",
        "title": "Private document",
        "children": [{
            "role": "AXStaticText",
            "subrole": "AXText",
            "value": "REQUEST-LOCAL-SMART-TEXT",
        }],
    }
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "text_detail": "on",
    }))

    assert result["error"] == ""
    assert result["request_local_placeholder"] is True
    payload = json.loads(result["fresh_output"])
    assert payload["app_ref"] == "app-1"
    assert payload["window_id"] == "window-1"
    assert Path(payload["text_detail_path"]).is_absolute()
    assert payload["text_detail_metadata"] == {
        "schema_version": 1,
        "snapshot_id": payload["snapshot_id"],
        "coverage": "reported_ax_subtree",
        "node_count": 2,
        "max_depth_observed": 1,
        "byte_count": payload["text_detail_metadata"]["byte_count"],
        "sha256": payload["text_detail_metadata"]["sha256"],
        "truncated": False,
        "truncation_reasons": [],
    }
    assert payload["text_summary"] == [
        {"role": "AXWindow", "text": "Private document"},
        {"role": "AXStaticText", "text": "REQUEST-LOCAL-SMART-TEXT"},
    ]
    assert "root" not in payload
    assert "text_detail_artifact" not in payload
    assert approval_requests[0]["kind"] == "computer_text_detail_capture"
    assert approval_requests[0]["choices"] == ["once", "deny"]
    assert not manager.grants

    durable = json.dumps({
        key: value
        for key, value in result.items()
        if not key.startswith("_") and key != "fresh_output"
    }, ensure_ascii=False)
    observed_json = json.dumps(observed, ensure_ascii=False)
    assert "REQUEST-LOCAL-SMART-TEXT" not in durable
    assert "REQUEST-LOCAL-SMART-TEXT" not in observed_json
    assert str(manager.session_dir) not in durable
    assert str(manager.session_dir) not in observed_json


def test_display_text_detail_uses_one_combined_once_only_approval(registry, manager):
    requests = []

    async def approve_once(request):
        requests.append(request)
        return "session"

    registry.set_approval_handler(approve_once)
    register(registry, manager)
    focus(registry)

    result = run(registry.execute("computer_snapshot", {
        "scope": "display",
        "text_detail": "on",
    }))

    assert result["error"] == ""
    assert len(requests) == 1
    assert requests[0]["kind"] == "computer_display_and_text_detail_capture"
    assert requests[0]["choices"] == ["once", "deny"]
    assert json.loads(result["fresh_output"])["scope"] == "display"


def test_denied_text_detail_never_reaches_helper(registry, manager, backend):
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    register(registry, manager)
    focus(registry)
    before = sum(name == "snapshot" for name, _ in backend.calls)

    result = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "text_detail": "on",
    }))

    assert result["code"] == "approval_denied"
    assert len(requests) == 1
    assert sum(name == "snapshot" for name, _ in backend.calls) == before


def test_text_detail_approval_is_invalidated_by_refocus_even_to_the_same_refs(
    registry,
    manager,
    backend,
):
    async def refocus_then_approve(_request):
        refocused = await registry.execute(
            "computer_focus",
            {"app_ref": "app-1", "window_ref": "window-1"},
        )
        assert refocused["error"] == ""
        return "once"

    registry.set_approval_handler(refocus_then_approve)
    register(registry, manager)
    focus(registry)
    before = sum(name == "snapshot" for name, _ in backend.calls)

    result = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "text_detail": "on",
    }))

    assert result["error"]
    assert sum(name == "snapshot" for name, _ in backend.calls) == before
    assert not any(
        scope.startswith("computer-observe-snapshot:")
        for scope in registry.approved_permission_scopes
    )


def test_text_detail_once_approval_is_consumed_before_await_and_cannot_be_reused(
    registry,
    manager,
    backend,
    monkeypatch,
):
    approvals = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    original_snapshot = backend.snapshot

    async def approve_first_only(_request):
        nonlocal approvals
        approvals += 1
        return "once" if approvals == 1 else "deny"

    async def delayed_snapshot(target, scope, artifact, **kwargs):
        if kwargs.get("text_detail") is ComputerSnapshotTextDetailMode.ON:
            entered.set()
            await release.wait()
        return await original_snapshot(target, scope, artifact, **kwargs)

    async def exercise():
        first = asyncio.create_task(registry.execute("computer_snapshot", {
            "scope": "target_window",
            "text_detail": "on",
        }))
        await entered.wait()
        second = asyncio.create_task(registry.execute("computer_snapshot", {
            "scope": "target_window",
            "text_detail": "on",
        }))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    registry.set_approval_handler(approve_first_only)
    register(registry, manager)
    focus(registry)
    monkeypatch.setattr(backend, "snapshot", delayed_snapshot)

    first, second = run(exercise())

    assert first["error"] == ""
    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert sum(name == "snapshot" for name, _ in backend.calls) == 1


def test_text_detail_approval_cannot_capture_target_changed_while_focus_holds_manager_lock(
    registry,
    manager,
    backend,
    monkeypatch,
):
    approvals = 0
    select_entered = asyncio.Event()
    release_select = asyncio.Event()
    focus_task = None
    original_select = backend.select

    async def blocking_select(app_ref, window_ref):
        if window_ref == "window-2":
            select_entered.set()
            await release_select.wait()
        return await original_select(app_ref, window_ref)

    async def approve_once_then_deny(_request):
        nonlocal approvals, focus_task
        approvals += 1
        if approvals != 1:
            return "deny"
        focus_task = asyncio.create_task(registry.execute(
            "computer_focus",
            {"app_ref": "app-1", "window_ref": "window-2"},
        ))
        await select_entered.wait()

        async def release_after_approval_returns():
            await asyncio.sleep(0)
            release_select.set()

        asyncio.create_task(release_after_approval_returns())
        return "once"

    async def exercise():
        first = await registry.execute("computer_snapshot", {
            "scope": "target_window",
            "text_detail": "on",
        })
        assert focus_task is not None
        focused = await focus_task
        second = await registry.execute("computer_snapshot", {
            "scope": "target_window",
            "text_detail": "on",
        })
        return first, focused, second

    registry.set_approval_handler(approve_once_then_deny)
    register(registry, manager)
    focus(registry)
    monkeypatch.setattr(backend, "select", blocking_select)

    first, focused, second = run(exercise())

    assert first["error"]
    assert focused["error"] == ""
    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert ComputerTarget("app-1", "window-2") not in backend.capture_targets
    assert not any(
        scope.startswith("computer-observe-snapshot:")
        for scope in registry.approved_permission_scopes
    )


def test_display_approval_queues_exact_binding_behind_concurrent_focus(
    registry,
    manager,
    backend,
    monkeypatch,
):
    approvals = 0
    status_entered = asyncio.Event()
    release_status = asyncio.Event()
    approval_returning = asyncio.Event()
    focus_task = None
    original_status = backend.status

    async def blocking_status():
        status_entered.set()
        await release_status.wait()
        return await original_status()

    async def approve_once_then_deny(_request):
        nonlocal approvals, focus_task
        approvals += 1
        if approvals != 1:
            return "deny"
        focus_task = asyncio.create_task(registry.execute(
            "computer_focus",
            {"app_ref": "app-1", "window_ref": "window-2"},
        ))
        # Let focus queue on the manager lock before the approved snapshot.
        await asyncio.sleep(0)
        approval_returning.set()
        return "once"

    async def exercise():
        status_task = asyncio.create_task(registry.execute("computer_status", {}))
        await status_entered.wait()
        snapshot_task = asyncio.create_task(registry.execute(
            "computer_snapshot",
            {"scope": "display", "text_detail": "off"},
        ))
        await approval_returning.wait()
        # The permission grant and tool body run synchronously until snapshot
        # queues behind the already-waiting focus task.
        await asyncio.sleep(0)
        release_status.set()
        status_result = await status_task
        first = await snapshot_task
        assert focus_task is not None
        focused = await focus_task
        second = await registry.execute(
            "computer_snapshot",
            {"scope": "display", "text_detail": "off"},
        )
        return status_result, first, focused, second

    registry.set_approval_handler(approve_once_then_deny)
    register(registry, manager)
    focus(registry)
    monkeypatch.setattr(backend, "status", blocking_status)

    status_result, first, focused, second = run(exercise())

    assert status_result["error"] == ""
    assert focused["error"] == ""
    assert first["code"] == "snapshot_target_changed"
    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert backend.capture_targets == []
    assert not any(
        scope.startswith("computer-observe-snapshot:")
        for scope in registry.approved_permission_scopes
    )


def test_text_detail_target_gone_does_not_recover_to_new_refs_or_reuse_approval(
    registry,
    manager,
    backend,
    monkeypatch,
):
    from agent.runtime.macos_computer import HelperApplicationError

    approvals = 0
    failed = False
    original_apps = backend.apps
    original_snapshot = backend.snapshot

    async def approve_once_then_deny(_request):
        nonlocal approvals
        approvals += 1
        return "once" if approvals == 1 else "deny"

    async def recovered_catalog():
        catalog = await original_apps()
        catalog.apps[0]["app_ref"] = "app-recovered"
        catalog.apps[0]["windows"][0]["window_ref"] = "window-recovered"
        return catalog

    async def target_gone_once(target, scope, artifact, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            backend.capture_targets.append(target)
            backend.calls.append(("snapshot", scope))
            raise HelperApplicationError(ComputerError(
                ComputerErrorCode.TARGET_GONE,
                "old opaque references expired",
            ))
        return await original_snapshot(target, scope, artifact, **kwargs)

    registry.set_approval_handler(approve_once_then_deny)
    register(registry, manager)
    focus(registry)
    monkeypatch.setattr(backend, "apps", recovered_catalog)
    monkeypatch.setattr(backend, "snapshot", target_gone_once)

    first = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "text_detail": "on",
    }))
    second = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "text_detail": "on",
    }))

    assert first["error"]
    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert backend.capture_targets == [ComputerTarget("app-1", "window-1")]
    assert ("select", ("app-recovered", "window-recovered")) not in backend.calls
    assert not any(
        scope.startswith("computer-observe-snapshot:")
        for scope in registry.approved_permission_scopes
    )


def test_text_detail_postcondition_clears_request_state_when_png_verification_raises(
    registry,
    manager,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    focus(registry)
    tool = registry.get("computer_snapshot")
    original_postcondition = tool.postcondition
    assert original_postcondition is not None
    captured = {}

    def capture_postcondition(args, result):
        captured["args"] = dict(args)
        captured["result"] = dict(result)
        return original_postcondition(args, result)

    tool.postcondition = capture_postcondition
    original_read = manager.read_verified_png_artifact
    reads = 0

    def raise_during_postcondition(filename, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise ComputerSessionError("unsafe_artifact", "PNG verification crashed")
        return original_read(filename, **kwargs)

    monkeypatch.setattr(manager, "read_verified_png_artifact", raise_during_postcondition)
    result = run(registry.execute("computer_snapshot", {
        "scope": "target_window",
        "text_detail": "on",
    }))
    assert result["code"] == "postcondition_failed"
    assert "fresh_output" not in result

    preflights = 0

    def observe_stale_detail_state(_manager):
        nonlocal preflights
        preflights += 1

    monkeypatch.setattr(manager, "read_verified_png_artifact", original_read)
    monkeypatch.setattr(
        computer_tools,
        "_preflight_current_snapshot_artifacts",
        observe_stale_detail_state,
    )

    original_postcondition(captured["args"], captured["result"])

    assert preflights == 0


@pytest.mark.parametrize("mode", ["permissive", "safe", "locked"])
@pytest.mark.parametrize("yolo", [False, True])
@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "element_ref": "snapshot-1:1"},
        {"type": "click", "element_ref": "snapshot-1:export"},
    ],
    ids=["ordinary", "high-impact"],
)
def test_computer_exact_action_approval_is_authoritative_in_every_policy_mode(
    registry,
    manager,
    backend,
    mode,
    yolo,
    action,
):
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    registry.policy.mode = mode
    registry.policy.allow("computer_act")
    registry.policy.allow_call("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [action],
    })
    registry.yolo = yolo
    registry.set_approval_handler(deny)

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [action],
    }))

    if yolo:
        assert result["error"] == ""
        assert requests == []
        assert sum(name == "act" for name, _ in backend.calls) == 1
    else:
        assert result["code"] == "approval_denied"
        assert len(requests) == 1
        assert requests[0]["kind"] == "computer_write"
        assert "scope" not in requests[0]
        assert not any(name == "act" for name, _ in backend.calls)


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "element_ref": "snapshot-1:1"},
        {"type": "click", "element_ref": "snapshot-1:export"},
    ],
    ids=["ordinary", "high-impact"],
)
def test_yolo_executes_with_automatic_exact_once_grant(registry, manager, backend, action):
    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    registry.yolo = True
    registry.policy.mode = "permissive"
    registry.set_approval_handler(approve)

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [action],
    }))

    assert result["error"] == ""
    assert requests == []
    assert not registry.approved_permission_scopes
    assert sum(name == "act" for name, _ in backend.calls) == 1


def test_computer_generic_tool_approval_cannot_substitute_for_exact_scope(registry, manager, backend):
    requests = []

    async def decide(request):
        requests.append(request)
        return "session" if request.get("kind") == "tool_policy" else "deny"

    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    registry.policy.mode = "locked"
    registry.set_approval_handler(decide)

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }))

    assert result["code"] == "approval_denied"
    assert [request["kind"] for request in requests] == ["computer_write"]
    assert not any(name == "act" for name, _ in backend.calls)


def test_display_approval_cannot_be_reused_after_target_changes(registry, manager, backend):
    async def switch_window_then_approve(_request):
        switched = await registry.execute(
            "computer_focus",
            {"app_ref": "app-1", "window_ref": "window-2"},
        )
        assert switched["error"] == ""
        return "once"

    registry.set_approval_handler(switch_window_then_approve)
    register(registry, manager)
    focus(registry)
    before = sum(name == "snapshot" for name, _ in backend.calls)

    result = run(registry.execute("computer_snapshot", {"scope": "display"}))

    assert result["error"]
    assert sum(name == "snapshot" for name, _ in backend.calls) == before
    assert f"computer-observe-display:{manager.session_id}" not in (
        registry.approved_permission_scopes
    )


def test_display_once_approval_is_atomically_consumed_across_concurrent_calls(
    registry,
    manager,
    backend,
    monkeypatch,
):
    approvals = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    original_snapshot = backend.snapshot

    async def approve_first_only(_request):
        nonlocal approvals
        approvals += 1
        return "once" if approvals == 1 else "deny"

    async def delayed_snapshot(target, scope, artifact):
        if scope == "display":
            entered.set()
            await release.wait()
        return await original_snapshot(target, scope, artifact)

    async def exercise():
        first = asyncio.create_task(
            registry.execute("computer_snapshot", {"scope": "display"})
        )
        await entered.wait()
        second = asyncio.create_task(
            registry.execute("computer_snapshot", {"scope": "display"})
        )
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    registry.set_approval_handler(approve_first_only)
    register(registry, manager)
    focus(registry)
    monkeypatch.setattr(backend, "snapshot", delayed_snapshot)

    first, second = run(exercise())

    assert first["error"] == ""
    assert second["code"] == "approval_denied"
    assert approvals == 2
    assert sum(
        name == "snapshot" and value == "display"
        for name, value in backend.calls
    ) == 1


def test_missing_model_capabilities_and_unsupported_platform_fail_before_manager_side_effects(
    registry, monkeypatch, tmp_path,
):
    from agent.runtime.tools.computer import LocalComputerRuntime

    manager = LocalComputerRuntime(helper_path=None, cache_root=tmp_path / "cache")
    register(registry, manager, model_capabilities=frozenset({"tools"}))
    no_vision = run(registry.execute("computer_status", {}))
    assert no_vision["code"] == "computer_capability_unavailable"
    assert manager.manager_started is False
    assert not (tmp_path / "cache").exists()

    second = ToolRegistry()
    register(second, manager, model_capabilities=frozenset({"tools", "vision"}))
    monkeypatch.setattr(
        "agent.runtime.tools.computer.sys", SimpleNamespace(platform="linux")
    )
    unsupported = run(second.execute("computer_status", {}))
    assert unsupported["code"] == "unsupported_platform"
    assert manager.manager_started is False
    assert not (tmp_path / "cache").exists()


def test_helper_failure_is_typed_and_stale_snapshot_never_reaches_input(
    registry, manager, backend, caplog,
):
    backend.status_error = RuntimeError("private helper detail")
    register(registry, manager)
    failed = run(registry.execute("computer_status", {}))
    assert failed["code"] == "helper_failed"
    assert "private helper detail" not in failed["error"]
    assert "private helper detail" not in caplog.text

    backend.status_error = None
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    stale = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    focus(registry)
    rejected = run(registry.execute("computer_act", {
        "snapshot_id": stale["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))
    assert rejected["code"] == "stale_snapshot"
    assert not any(name == "act" for name, _value in backend.calls)


def test_computer_focus_selects_in_background_without_legacy_activation(
    registry,
    manager,
    backend,
):
    register(registry, manager)
    run(registry.execute("computer_apps", {}))

    result = run(registry.execute("computer_focus", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert result["error"] == ""
    assert ("select", ("app-1", "window-1")) in backend.calls
    assert all(name != "focus" for name, _value in backend.calls)
    assert "target_not_frontmost" not in json.dumps(result)


def test_default_local_runtime_forwards_background_v2_requests_exactly(
    registry,
    manager,
    backend,
    monkeypatch,
    tmp_path,
):
    register_local(registry, manager, monkeypatch, tmp_path)

    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["error"] == ""
    assert ("select", ("app-1", "window-1")) in backend.calls
    assert ("plan_actions", ("snapshot-1", "background")) in backend.calls
    assert (
        "act",
        (
            "snapshot-1",
            [{"type": "wait", "duration_ms": 0}],
            "background",
            "plan-1",
            None,
        ),
    ) in backend.calls
    assert all(name != "focus" for name, _value in backend.calls)


def test_default_local_runtime_forwards_foreground_interface_without_fake_token(
    registry,
    manager,
    backend,
    monkeypatch,
    tmp_path,
):
    from agent.runtime.macos_computer import HelperApplicationError

    register_local(registry, manager, monkeypatch, tmp_path)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True

    async def reject_takeover(snapshot_id, plan_ref):
        backend.calls.append(("takeover_begin", (snapshot_id, plan_ref)))
        raise HelperApplicationError(ComputerError(
            ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED,
            "PRIVATE-NATIVE-TAKEOVER-MESSAGE",
        ))

    monkeypatch.setattr(backend, "begin_takeover", reject_takeover)
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }
    required = run(registry.execute("computer_act", exact))
    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert required["code"] == "foreground_takeover_required"
    assert result["code"] == "foreground_takeover_required"
    # 站点2(计划成功但 requires_takeover=true): 恢复指引要求用同一快照同一批次重试,
    # 而 computer_begin_takeover 的必填参数是 plan_ref ⇒ details 必须带这个句柄,
    # 否则工具在要求模型走一条它自己调不动的流程。（站点1 helper 直接拒绝时根本没有
    # plan，不存在 plan_ref 可给，那条指引该另案改成不承诺同一快照。）
    plan_ref = required["details"]["plan_ref"]
    assert isinstance(plan_ref, str) and plan_ref.startswith("plan-")
    assert "PRIVATE-NATIVE-TAKEOVER-MESSAGE" not in json.dumps(result)
    assert ("plan_actions", ("snapshot-1", "background")) in backend.calls
    assert ("plan_actions", ("snapshot-1", "foreground_takeover")) in backend.calls
    assert ("takeover_begin", ("snapshot-1", "plan-2")) in backend.calls
    assert not any(name == "act" for name, _value in backend.calls)


def test_native_pointer_plan_rejection_escales_to_foreground_takeover(
    registry,
    manager,
    backend,
    monkeypatch,
):
    from agent.runtime.macos_computer import HelperApplicationError

    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    async def reject_pointer_plan(*_args, **_kwargs):
        raise HelperApplicationError(ComputerError(
            ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED,
            "pointer backend unavailable",
        ))

    registry.set_approval_handler(approve)
    monkeypatch.setattr(backend, "plan_actions", reject_pointer_plan)
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))[
            "fresh_output"
        ]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }))

    # 新契约(2026-09-02): background 不可行 ≠ 用户手点, 而是升级为显式授权的
    # foreground takeover(一次性批准); 不再映射 computer_handoff_required。
    assert result["code"] == "foreground_takeover_required"
    assert result["retryable"] is True
    assert result["recovery_hint"] == (
        "Retry this exact snapshot and action batch with "
        "interaction_mode='foreground_takeover'."
    )
    assert requests == []
    assert not any(name in {"takeover_begin", "act"} for name, _ in backend.calls)
    assert manager.handed_off is False


def test_native_pointer_plan_requires_active_maps_to_activation_hint(
    registry,
    manager,
    backend,
    monkeypatch,
):
    from agent.runtime.computer_backend import ComputerActionPlan, ComputerInteractionMode

    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    async def active_plan(*_args, **_kwargs):
        return ComputerActionPlan(
            plan_ref="plan-active",
            interaction_mode=ComputerInteractionMode.BACKGROUND,
            requires_takeover=True,
            reason="requires_active_foreground_takeover",
            action_classes=("click",),
            pid_action_classes=("click",),
        )

    registry.set_approval_handler(approve)
    monkeypatch.setattr(backend, "plan_actions", active_plan)
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))[
            "fresh_output"
        ]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }))

    # 新契约(2026-09-02): requires_active 的 app 在计划阶段路由到激活路径,
    # background 提交给出激活要求提示(与通用 foreground takeover 文案区分)。
    assert result["code"] == "foreground_takeover_required"
    assert result["retryable"] is True
    assert "activation" in result["error"]
    assert result["details"]["reason"] == "requires_active_foreground_takeover"
    assert requests == []
    assert manager.handed_off is False


def test_foreground_takeover_direct_submission_skips_background_prerequisite(
    registry,
    manager,
    backend,
    monkeypatch,
):
    from agent.runtime.computer_backend import ComputerActionPlan, ComputerInteractionMode

    requests = []

    async def approve(request):
        requests.append(request)
        return "once"

    async def active_plan(*_args, **_kwargs):
        return ComputerActionPlan(
            plan_ref="plan-takeover",
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            requires_takeover=True,
            reason="requires_active_foreground_takeover",
            action_classes=("click",),
            pid_action_classes=("click",),
        )

    registry.set_approval_handler(approve)
    monkeypatch.setattr(backend, "plan_actions", active_plan)
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    # 新契约(2026-09-02): foreground_takeover 直接提交不再要求先 background 建
    # pending——一次授权即可激活并投递(requires_active 最终验收链路)。
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "interaction_mode": "foreground_takeover",
        "actions": [{"type": "right_click", "element_ref": "snapshot-1:1", "x": 820, "y": 595}],
    }))

    assert "Run this exact action batch in background mode" not in result.get("error", "")
    assert result.get("code") != "foreground_takeover_required"
    assert requests == []
    assert manager.handed_off is False


def test_background_takeover_plan_returns_typed_retry_without_input_or_approval(
    registry,
    manager,
    backend,
):
    approvals = []

    async def approve(request):
        approvals.append(request)
        return "once"

    registry.set_approval_handler(approve)
    registry.yolo = True
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "scroll", "delta_y": 120, "element_ref": "snapshot-1:1"}],
    }))

    assert result["code"] == "foreground_takeover_required"
    assert result["retryable"] is True
    assert approvals == []
    assert ("plan_actions", ("snapshot-1", "background")) in backend.calls
    assert not any(name in {"takeover_begin", "act"} for name, _value in backend.calls)


@pytest.mark.parametrize("rejection", [None, ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED, ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED])
def test_modern_helper_auto_takeover_completes_in_one_public_action(registry, manager, backend, monkeypatch, rejection):
    monkeypatch.setattr("agent.runtime.tools.computer.enabled_pid_actions", lambda *_: frozenset({"click"}))
    backend.snapshot_ax_tree_override = {"role": "AXWindow", "observation_capabilities": ["auto_takeover_v1"],
        "children": [{"role": "AXButton", "element_ref": "snapshot-1:1", "label": "Ordinary"}]}
    register(registry, manager)
    registry.yolo = True
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    backend.force_takeover = True
    original_plan = backend.plan_actions

    async def plan_with_typed_rejection(target, snapshot_id, actions, interaction_mode):
        plan = await original_plan(target, snapshot_id, actions, interaction_mode)
        if rejection is not None and interaction_mode is ComputerInteractionMode.BACKGROUND:
            raise HelperApplicationError(ComputerError(rejection, "typed pre-input rejection"))
        return plan

    monkeypatch.setattr(backend, "plan_actions", plan_with_typed_rejection)
    result = run(registry.execute("computer_act", {"snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}]}))
    assert not result.get("error"), result
    assert [value[1] for name, value in backend.calls if name == "plan_actions"] == ["background", "foreground_takeover"]
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert result["computer_receipt"]["mode"] == "foreground_takeover"


def test_legacy_helper_checked_goal_fails_before_planning(registry, manager, backend):
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    result = run(registry.execute("computer_act", {"snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1", "checked": True}]}))
    assert result["code"] == "unsupported_operation"
    assert not any(name in {"act", "plan_actions"} for name, _ in backend.calls)


def test_legacy_helper_replacement_fails_before_planning(registry, manager, backend):
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    result = run(registry.execute("computer_act", {"snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "type", "text": "", "element_ref": "snapshot-1:1", "replace": True}]}))
    assert result["code"] == "unsupported_operation"
    assert not any(name in {"act", "plan_actions", "takeover_begin"} for name, _ in backend.calls)


def test_capable_helper_receives_exact_replacement_semantics(registry, manager, backend):
    backend.snapshot_ax_tree_override = {"role": "AXWindow", "observation_capabilities": ["replace_text_v1"],
        "children": [{"role": "AXTextField", "element_ref": "snapshot-1:field", "label": "Search",
                      "value": "old", "bounds": {"x": 10, "y": 10, "width": 50, "height": 20}}]}
    register(registry, manager)
    registry.set_approval_handler(lambda request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    action = {"type": "type", "text": "", "element_ref": "snapshot-1:field", "replace": True}
    run(registry.execute("computer_act", {"snapshot_id": snapshot["snapshot_id"], "actions": [action]}))
    sent = [payload[1] for name, payload in backend.calls if name == "act"]
    assert sent == [[action]]


def test_refused_replacement_recommends_select_all_and_typing(registry, manager, backend, monkeypatch):
    """Live Edge 2026-09-23: browser fields refuse native replacement before any input.
    The refusal must name the keyboard route that works instead of a user handoff."""
    from agent.runtime.macos_computer import HelperApplicationError

    backend.snapshot_ax_tree_override = {"role": "AXWindow",
        "observation_capabilities": ["replace_text_v1", "auto_takeover_v1"],
        "children": [{"role": "AXTextField", "element_ref": "snapshot-1:field", "label": "Address",
                      "value": "old", "bounds": {"x": 10, "y": 10, "width": 50, "height": 20}}]}

    async def refuse(*_args, **_kwargs):
        raise HelperApplicationError(ComputerError(
            ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED, "the action batch is unsafe"))

    monkeypatch.setattr(backend, "plan_actions", refuse)
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    result = run(registry.execute("computer_act", {"snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "type", "text": "example.com", "element_ref": "snapshot-1:field", "replace": True}]}))
    assert result["code"] == "background_action_unsupported"
    hint = result["recovery_hint"]
    assert "No input was dispatched" in hint
    assert "command+a" in hint and "without replace" in hint
    assert "hand this step to the user" not in hint
    assert not any(name in {"takeover_begin", "act"} for name, _ in backend.calls)


@pytest.mark.parametrize("approval", ["once", "deny"])
def test_dialog_intent_activates_before_an_ordinary_ax_button_with_normal_approval(
    registry, manager, backend, monkeypatch, approval,
):
    monkeypatch.setattr("agent.runtime.tools.computer.enabled_pid_actions", lambda *_: frozenset({"click"}))
    backend.snapshot_ax_tree_override = {"role": "AXWindow", "observation_capabilities": ["auto_takeover_v1"],
        "children": [{"role": "AXButton", "element_ref": "snapshot-1:1", "label": "Ordinary"}]}
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    requests = []

    async def approve(request):
        requests.append(request)
        return approval

    registry.set_approval_handler(approve)
    result = run(registry.execute("computer_act", {"snapshot_id": snapshot["snapshot_id"],
        "opens_dialog": True, "actions": [{"type": "click", "element_ref": "snapshot-1:1"}]}))
    assert [value[1] for name, value in backend.calls if name == "plan_actions"] == ["foreground_takeover"]
    assert len(requests) == 1
    if approval == "once":
        assert not result.get("error"), result
        assert result["computer_receipt"]["mode"] == "foreground_takeover"
        assert sum(name == "act" for name, _ in backend.calls) == 1
    else:
        assert result["code"] == "approval_denied"
        assert not any(name in {"takeover_begin", "act"} for name, _ in backend.calls)


def test_dialog_intent_rejects_explicit_background_before_planning(registry, manager, backend):
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    result = run(registry.execute("computer_act", {"snapshot_id": snapshot["snapshot_id"],
        "interaction_mode": "background", "opens_dialog": True,
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}]}))
    assert result["code"] == "invalid_arguments"
    assert not any(name in {"plan_actions", "takeover_begin", "act"} for name, _ in backend.calls)


def test_background_ax_scroll_uses_normal_approval_without_takeover_or_pid_lookup(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    approvals = []

    async def approve(request):
        approvals.append(request)
        return "once"

    def unexpected_pid_lookup(_bundle_id, _app_version):
        raise AssertionError("background AX scroll must not consult PID compatibility")

    registry.set_approval_handler(approve)
    monkeypatch.setattr(computer_tools, "enabled_pid_actions", unexpected_pid_lookup)
    backend.plan_action_classes = ("scroll",)
    backend.plan_pid_action_classes = ()
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "scroll", "delta_y": 120, "element_ref": "snapshot-1:1"}],
    }))

    assert result["error"] == ""
    assert len(approvals) == 1
    assert approvals[0]["kind"] == "computer_write"
    assert approvals[0]["arguments"]["action_classes"] == ["scroll"]
    assert not any(name == "takeover_begin" for name, _value in backend.calls)
    act_calls = [value for name, value in backend.calls if name == "act"]
    assert len(act_calls) == 1
    assert act_calls[0][2] == "background"


def test_foreground_retry_requires_exact_compatibility_and_one_redacted_approval(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    approvals = []
    events = []

    async def approve(request):
        approvals.append(request)
        return "once"

    registry.set_approval_handler(approve)
    registry.hooks.on_runtime_event(events.append)
    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda bundle_id, app_version: (
            frozenset({"scroll"})
            if (bundle_id, app_version) == ("dev.astra.fixture", "1.0.0")
            else frozenset()
        ),
    )
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True
    backend.plan_action_classes = ("press", "scroll")
    backend.plan_pid_action_classes = ("scroll",)
    backend.act_result = ComputerActionResult(
        result={
            "outcomes": [
                {"index": 0, "ok": True},
                {"index": 1, "ok": True},
            ],
            "last_acknowledged_action": 1,
        }
    )
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [
            {"type": "click", "element_ref": "snapshot-1:1"},
            {"type": "scroll", "delta_y": 120, "element_ref": "snapshot-1:1"},
        ],
    }

    required = run(registry.execute("computer_act", exact))
    assert required["code"] == "foreground_takeover_required"
    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert result["error"] == ""
    assert len(approvals) == 1
    request = approvals[0]
    assert request["kind"] == "computer_foreground_takeover"
    assert request["choices"] == ["once", "session", "deny"]
    assert request["arguments"] == {
        "application": "Fixture",
        "window": "Fixture",
        "reason": "This batch will interact with the selected macOS application.",
        "action_classes": ["press", "scroll"],
        "expected_effect": (
            "The selected window may be edited or navigated. "
            "The selected application may be temporarily brought to the foreground and control the real pointer and keyboard."
        ),
    }
    encoded = json.dumps(request, ensure_ascii=False)
    for forbidden in (
        "snapshot-1",
        "app-1",
        "window-1",
        "plan-",
        "takeover-",
        str(manager.session_dir),
    ):
        assert forbidden not in encoded
    assert [event["type"] for event in events] == [
        "foreground_takeover_begin",
        "foreground_takeover_focus_restored",
        "foreground_takeover_end",
    ]
    assert all(set(event) <= {"type", "application", "action_classes"} for event in events)
    assert all(event["action_classes"] == ["press", "scroll"] for event in events)
    assert ("takeover_end", "takeover-1") in backend.calls

    replay = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))
    # 新语义(2026-09-02): foreground 直接提交不依赖 background pending;
    # 重复提交同一批被识别为 stale, 拒绝二次授权。
    assert replay["code"] == "stale_snapshot"
    assert len(approvals) == 1


def test_foreground_approval_consumes_the_same_sealed_plan_without_replanning(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"scroll"}),
    )
    planned = []
    approved_plan_refs = []
    original_plan_actions = backend.plan_actions

    async def plan_with_potential_alignment_drift(*args, **kwargs):
        plan = await original_plan_actions(*args, **kwargs)
        planned.append(plan)
        return plan

    async def approve(_request):
        approved_plan_refs.append(planned[-1].plan_ref)
        return "once"

    monkeypatch.setattr(backend, "plan_actions", plan_with_potential_alignment_drift)
    registry.set_approval_handler(approve)
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True
    backend.plan_action_classes = ("press", "scroll")
    backend.plan_pid_action_classes = ("scroll",)
    backend.act_result = ComputerActionResult(
        result={
            "outcomes": [{"index": 0, "ok": True}, {"index": 1, "ok": True}],
            "last_acknowledged_action": 1,
        }
    )
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [
            {"type": "click", "element_ref": "snapshot-1:1"},
            {"type": "scroll", "delta_y": 120, "element_ref": "snapshot-1:1"},
        ],
    }

    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"
    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert result["error"] == ""
    assert approved_plan_refs == ["plan-2"]
    assert len(planned) == 2
    assert ("takeover_begin", ("snapshot-1", "plan-2")) in backend.calls
    act_calls = [value for name, value in backend.calls if name == "act"]
    assert len(act_calls) == 1
    assert act_calls[0][3] == "plan-2"


def test_high_impact_foreground_export_merges_risk_and_takeover_into_one_approval(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    approvals = []
    registry.set_approval_handler(
        lambda request: asyncio.sleep(0, result=approvals.append(request) or "once")
    )
    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"click"}),
    )
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:export"}],
    }

    required = run(registry.execute("computer_act", exact))
    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert required["code"] == "foreground_takeover_required"
    assert result["error"] == ""
    assert len(approvals) == 1
    request = approvals[0]
    assert request["choices"] == ["once", "deny"]
    assert "Export PDF" in request["operation"]
    assert "high-impact" in request["reason"]
    assert "send, save, export, delete, execute" in request["arguments"]["expected_effect"]
    assert "temporarily brought to the foreground" in request["arguments"]["expected_effect"]


def test_foreground_retry_default_denies_unreviewed_cell_before_approval_or_input(
    registry,
    manager,
    backend,
):
    approvals = []
    registry.set_approval_handler(
        lambda request: asyncio.sleep(0, result=approvals.append(request) or "once")
    )
    registry.yolo = True
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "scroll", "delta_y": 120, "element_ref": "snapshot-1:1"}],
    }

    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"
    denied = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert denied["code"] == "background_action_unsupported"
    assert approvals == []
    assert not any(name in {"takeover_begin", "act"} for name, _value in backend.calls)


def test_denied_foreground_fragment_is_consumed_and_cannot_be_reapproved_without_background(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    approvals = []

    async def deny(request):
        approvals.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"scroll"}),
    )
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "scroll", "delta_y": 120, "element_ref": "snapshot-1:1"}],
    }
    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"

    denied = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))
    replay = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert denied["code"] == "approval_denied"
    # 新语义(2026-09-02): foreground 直接提交不依赖 background pending;
    # deny 后重复提交同一批被识别为 stale, 不允许二次授权。
    assert replay["code"] == "stale_snapshot"
    assert len(approvals) == 1
    assert not any(name in {"takeover_begin", "act"} for name, _value in backend.calls)


@pytest.mark.parametrize("outcome", ["preserved", "pause", "unknown", "exception"])
def test_foreground_approval_is_consumed_across_terminal_outcomes(
    registry,
    manager,
    backend,
    monkeypatch,
    outcome,
):
    import agent.runtime.tools.computer as computer_tools

    approvals = []
    events = []
    registry.set_approval_handler(
        lambda request: asyncio.sleep(0, result=approvals.append(request) or "once")
    )
    registry.hooks.on_runtime_event(events.append)
    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"scroll"}),
    )
    if outcome == "preserved":
        backend.takeover_end_result = {
            "ended": True,
            "restoration": "preserved_user_focus",
        }
    elif outcome in {"pause", "unknown"}:
        code = (
            ComputerErrorCode.USER_ACTIVITY_PAUSED
            if outcome == "pause"
            else ComputerErrorCode.UNKNOWN_OUTCOME
        )
        backend.act_result = ComputerActionResult(
            error=ComputerError(code, "PRIVATE-NATIVE-ACTION-MESSAGE")
        )
    else:
        async def raise_after_dispatch(*_args, **_kwargs):
            raise RuntimeError("PRIVATE-DISPATCH-EXCEPTION")

        monkeypatch.setattr(backend, "act", raise_after_dispatch)

    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "scroll", "delta_y": 120, "element_ref": "snapshot-1:1"}],
    }
    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"

    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))
    replay = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert len(approvals) == 1
    assert replay["error_type"] != "approval_denied"
    encoded = json.dumps({"result": result, "events": events})
    assert "PRIVATE-" not in encoded
    event_types = [event["type"] for event in events]
    assert event_types[0] == "foreground_takeover_begin"
    assert event_types[-1] == "foreground_takeover_end"
    expected_event = {
        "preserved": "foreground_takeover_focus_preserved",
        "pause": "foreground_takeover_user_activity_paused",
        "unknown": "foreground_takeover_focus_restored",
        "exception": "foreground_takeover_focus_restored",
    }[outcome]
    assert expected_event in event_types
    assert sum(name == "takeover_end" for name, _value in backend.calls) == 1


def test_foreground_takeover_orders_approval_input_evidence_and_cleanup(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"scroll"}),
    )

    async def approve(_request):
        backend.calls.append(("approval", None))
        return "once"

    registry.set_approval_handler(approve)
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.calls.clear()
    backend.force_takeover = True
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "scroll", "delta_y": 120, "element_ref": "snapshot-1:1"}],
    }

    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"
    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert json.loads(result["fresh_output"])["success"] is True
    names = [name for name, _value in backend.calls]
    assert names == [
        "plan_actions",
        "plan_actions",
        "approval",
        "takeover_begin",
        "act",
        "snapshot",
        "takeover_end",
    ]


def test_foreground_application_error_keeps_target_not_frontmost_at_tool_boundary(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools
    from agent.runtime.macos_computer import HelperApplicationError

    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"click"}),
    )
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True

    async def reject_foreground(
        _target,
        _snapshot_id,
        _actions,
        *,
        interaction_mode,
        plan_ref,
        takeover_ref=None,
    ):
        assert interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
        assert plan_ref
        assert takeover_ref == "takeover-1"
        raise HelperApplicationError(ComputerError(
            ComputerErrorCode.TARGET_NOT_FRONTMOST,
            "PRIVATE-NATIVE-MESSAGE",
        ))

    monkeypatch.setattr(backend, "act", reject_foreground)
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }
    required = run(registry.execute("computer_act", exact))
    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert required["code"] == "foreground_takeover_required"
    assert result["code"] == "target_not_frontmost"
    assert "PRIVATE-NATIVE-MESSAGE" not in json.dumps(result)
    assert "target_gone" not in json.dumps(result)


def test_typed_user_activity_pause_preserves_takeover_cleanup_and_handoffs(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools
    from agent.runtime.macos_computer import HelperApplicationError

    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"click"}),
    )
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True

    async def pause_after_post(*_args, **_kwargs):
        raise HelperApplicationError(ComputerError(
            ComputerErrorCode.USER_ACTIVITY_PAUSED,
            "PRIVATE-NATIVE-MESSAGE",
        ))

    monkeypatch.setattr(backend, "act", pause_after_post)
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }
    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"
    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert result["code"] == "user_activity_paused"
    assert manager.handed_off is True
    assert [name for name, _value in backend.calls].count("takeover_end") == 1


def test_post_delivery_pause_receipt_is_non_replayable_and_cleanup_runs_once(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"click"}),
    )
    outcomes = [{"index": 0, "ok": False, "error_code": "unknown_outcome"}]
    transport = NativeActReceiptTransport(
        outcomes=outcomes,
        acknowledgement=-1,
        error=ComputerError(
            ComputerErrorCode.USER_ACTIVITY_PAUSED,
            "PRIVATE-NATIVE-PAUSE",
        ),
    )
    route_act_through_native_adapter(backend, monkeypatch, transport)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": f"{snapshot['snapshot_id']}:1"}],
    }
    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"
    backend.calls.clear()

    result = run(registry.execute("computer_act", {
        **exact,
        "interaction_mode": "foreground_takeover",
    }))

    assert result["code"] == "user_activity_paused"
    assert result["error"] == "Computer input paused because user activity was detected."
    assert result["retryable"] is False
    assert result["recoverable"] is False
    assert "Do not repeat this action batch" in result["recovery_hint"]
    assert result["details"] == {
        "outcomes": outcomes,
        "last_acknowledged_action": -1,
        "next_observation": {"tool": "computer_resume", "arguments": {
            "app_ref": "app-1", "window_ref": "window-1",
        }},
    }
    assert "PRIVATE-NATIVE-PAUSE" not in json.dumps(result)
    assert manager.handed_off is True
    assert [request.operation for request in transport.requests] == ["act"]
    assert [name for name, _value in backend.calls] == [
        "plan_actions",
        "takeover_begin",
        "act",
        "takeover_end",
    ]


def test_tool_finally_repeated_cancellation_cannot_skip_terminal_takeover_end(
    registry,
    manager,
    backend,
    monkeypatch,
):
    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(
        computer_tools,
        "enabled_pid_actions",
        lambda _bundle_id, _app_version: frozenset({"click"}),
    )
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    backend.force_takeover = True
    exact = {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "click", "element_ref": "snapshot-1:1"}],
    }
    assert run(registry.execute("computer_act", exact))["code"] == "foreground_takeover_required"
    end_entered = asyncio.Event()
    allow_end = asyncio.Event()

    async def delayed_end(takeover_ref):
        backend.calls.append(("takeover_end", takeover_ref))
        end_entered.set()
        await allow_end.wait()
        return {"started": True, "restoration": "restored"}

    monkeypatch.setattr(backend, "end_takeover", delayed_end)

    async def scenario():
        task = asyncio.create_task(registry.execute("computer_act", {
            **exact,
            "interaction_mode": "foreground_takeover",
        }))
        await end_entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        apps_task = asyncio.create_task(manager.apps())
        await asyncio.sleep(0)
        assert not apps_task.done()
        allow_end.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await apps_task
        assert manager._terminal_cleanup_task is None
        assert manager._takeover_end_task is None

    run(scenario())
    assert [name for name, _value in backend.calls].count("takeover_end") == 1


def test_plain_select_transport_failure_is_sanitized_helper_failed(
    registry,
    manager,
    backend,
    caplog,
):
    from agent.runtime.macos_computer import HelperTransportError

    register(registry, manager)
    run(registry.execute("computer_apps", {}))
    backend.select_error = HelperTransportError("PRIVATE-HELPER-STDERR")

    result = run(registry.execute("computer_focus", {
        "app_ref": "app-1",
        "window_ref": "window-1",
    }))

    assert result["code"] == "helper_failed"
    assert "PRIVATE-HELPER-STDERR" not in json.dumps(result)
    assert "PRIVATE-HELPER-STDERR" not in caplog.text


def test_unexpected_act_helper_failure_is_sanitized_unknown_outcome(
    registry, manager, backend, monkeypatch, caplog,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    async def crash_after_dispatch(
        _target,
        _snapshot_id,
        _actions,
        *,
        interaction_mode,
        plan_ref,
        takeover_ref=None,
    ):
        del interaction_mode, plan_ref, takeover_ref
        backend.calls.append(("act", "crashed"))
        raise RuntimeError("PRIVATE-ACT-HELPER-DETAIL")

    monkeypatch.setattr(backend, "act", crash_after_dispatch)

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["code"] == "unknown_outcome"
    assert result["retryable"] is False
    assert result["recoverable"] is False
    assert "PRIVATE-ACT-HELPER-DETAIL" not in json.dumps(result)
    assert "PRIVATE-ACT-HELPER-DETAIL" not in caplog.text
    assert sum(name == "act" for name, _value in backend.calls) == 1


def test_post_action_snapshot_failure_is_observation_pending_with_ack_and_no_replay(
    registry, manager, backend, monkeypatch, caplog,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )

    async def fail_observation_after_dispatch(_target, _scope, _artifact):
        backend.calls.append(("snapshot", "PRIVATE-POST-ACTION-SNAPSHOT-FAILURE"))
        raise ComputerSessionError("helper_failed", "PRIVATE-POST-ACTION-SNAPSHOT-FAILURE")

    monkeypatch.setattr(backend, "snapshot", fail_observation_after_dispatch)

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["code"] == "post_action_observation_pending"
    assert result["retryable"] is False
    assert result["recoverable"] is False
    assert result["details"]["observation_error_code"] == "helper_failed"
    assert result["details"]["action_result"] == {
        "outcomes": [{"index": 0, "ok": True}],
        "last_acknowledged_action": 0,
        "status": "action_acknowledged",
        "effect_verification": "unverified",
    }
    assert result["computer_receipt"]["dispatch_state"] == "acknowledged"
    assert result["computer_receipt"]["next_step"] == "follow_recovery"
    assert "Do not repeat" in result["recovery_hint"]
    assert "Capture a fresh snapshot" in result["recovery_hint"]
    assert "PRIVATE-POST-ACTION-SNAPSHOT-FAILURE" not in json.dumps(result)
    assert "PRIVATE-POST-ACTION-SNAPSHOT-FAILURE" not in caplog.text
    assert sum(name == "act" for name, _value in backend.calls) == 1


@pytest.mark.parametrize("observation_fails", [False, True])
def test_focus_checkpoint_observes_sent_prefix_without_replaying_suffix(
    registry, manager, backend, monkeypatch, observation_fails,
):
    import agent.runtime.tools.computer as computer_tools

    monkeypatch.setattr(computer_tools, "enabled_pid_actions", lambda *_args: frozenset({"press", "text"}))
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    receipt = {"outcomes": [{"index": 0, "ok": True, "observation_required": True}],
               "last_acknowledged_action": 0}
    backend.act_result = ComputerActionResult(result=receipt,
        error=ComputerError(ComputerErrorCode.OBSERVATION_REQUIRED, "PRIVATE-CHECKPOINT"))
    observed_actions = []
    original_observe = computer_tools.observe_action_effect

    def record_observed_actions(before, after, actions):
        observed_actions.extend(actions)
        return original_observe(before, after, actions)

    monkeypatch.setattr(computer_tools, "observe_action_effect", record_observed_actions)
    if observation_fails:
        async def fail_snapshot(*_args, **_kwargs):
            raise ComputerSessionError("snapshot_failed", "PRIVATE-CHECKPOINT-SNAPSHOT")
        monkeypatch.setattr(backend, "snapshot", fail_snapshot)
    actions = [{"type": "keypress", "key": "tab"}, {"type": "type", "text": "Must not be sent"}]
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"], "actions": actions,
        "interaction_mode": "foreground_takeover",
    }))
    if observation_fails:
        assert result["code"] == "post_action_observation_pending"
        assert result["details"]["observation_error_code"] == "snapshot_failed"
        assert result["computer_receipt"]["dispatch_state"] == "partial"
        assert result["computer_receipt"]["next_step"] == "follow_recovery"
        metadata = result["details"]["action_result"]
        assert "Do not repeat" in result["recovery_hint"]
        assert observed_actions == []
    else:
        assert result["error"] == ""
        payload = json.loads(result["fresh_output"])
        assert payload["snapshot_id"] != snapshot["snapshot_id"]
        metadata = payload["action_result"]
        assert len(observed_actions) == 1
        assert observed_actions[0].type == "keypress"
    assert metadata["outcomes"] == receipt["outcomes"]
    assert metadata["last_acknowledged_action"] == 0
    assert metadata["next_action_index"] == 1
    assert metadata["observation_required"] is True
    assert metadata["effect_verification"] == "unverified"
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert "PRIVATE-CHECKPOINT" not in json.dumps(result)


@pytest.mark.parametrize("native_error", [False, True])
def test_post_action_snapshot_retries_observation_without_replaying_input(
    registry, manager, backend, monkeypatch, native_error,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    original_snapshot = backend.snapshot
    attempts = 0

    async def fail_first_observation(target, scope, artifact):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            backend.calls.append(("snapshot", "transient-post-action-failure"))
            if native_error:
                raise HelperApplicationError(ComputerError(ComputerErrorCode.SNAPSHOT_FAILED, "transient"))
            raise ComputerSessionError("helper_failed", "transient post-action failure")
        return await original_snapshot(target, scope, artifact)

    monkeypatch.setattr(backend, "snapshot", fail_first_observation)

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["error"] == ""
    assert json.loads(result["fresh_output"])["action_result"] == {
        "outcomes": [{"index": 0, "ok": True}],
        "last_acknowledged_action": 0,
        "status": "action_acknowledged",
        "effect_verification": "unverified",
    }
    assert attempts == 2
    assert sum(name == "act" for name, _value in backend.calls) == 1


@pytest.mark.skipif(os.name != "posix", reason="ComputerSessionManager requires POSIX held-directory cache leases")
def test_close_failure_blocks_local_cleanup_and_manager_reuse(tmp_path):
    backend = FakeComputerBackend()
    backend.close_error = RuntimeError("helper crashed")
    manager = ComputerSessionManager(backend, cache_root=tmp_path / "cache")
    manager.grants.add("computer:interaction")
    session_dir = manager.session_dir
    registry = ToolRegistry(hooks=HookRegistry())
    register(registry, manager)

    result = run(registry.execute("computer_close", {}))

    assert "RuntimeError" in result["error"]
    assert "helper crashed" not in result["error"]
    assert session_dir.exists()
    assert manager.grants == set()
    assert manager.closed is False
    with pytest.raises(ComputerSessionError, match="poison|cleanup"):
        run(manager.select("app", "window"))


@pytest.mark.skipif(os.name != "posix", reason="ComputerSessionManager requires POSIX held-directory cache leases")
def test_session_end_hook_cleans_up_without_leaking_exceptions(tmp_path):
    backend = FakeComputerBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path / "cache")
    session_dir = manager.session_dir
    registry = ToolRegistry()
    register(registry, manager)

    registry.hooks.dispatch_session_end("agent-session", "cancelled")

    assert not session_dir.exists()
    assert manager.closed is True


@pytest.mark.skipif(os.name != "posix", reason="ComputerSessionManager requires POSIX held-directory cache leases")
def test_session_end_hook_stops_helper_before_local_cleanup_in_running_loop(tmp_path):
    backend = FakeComputerBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path / "cache")
    manager.grants.add("computer:interaction-session")
    session_dir = manager.session_dir
    registry = ToolRegistry()
    register(registry, manager)

    async def scenario():
        registry.hooks.dispatch_session_end("agent-session", "error")
        assert manager.closed is False
        assert session_dir.exists()
        for _ in range(5):
            if manager.closed:
                break
            await asyncio.sleep(0)
        assert manager.closed is True
        assert manager.grants == set()
        assert not session_dir.exists()

    run(scenario())


def test_react_attaches_verified_request_local_image_without_durable_image_or_ax_data(
    registry, manager,
):
    register(registry, manager)
    run(manager.select("app-1", "window-1"))

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.second_messages = None
            self.third_messages = None

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "computer-call-1",
                        "name": "computer_snapshot",
                        "arguments": json.dumps({"scope": "target_window"}),
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            if self.calls == 2:
                self.second_messages = messages
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "computer-call-2",
                        "name": "computer_status",
                        "arguments": "{}",
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            self.third_messages = messages
            yield {"type": "done", "content": "inspected", "usage": None}

    async def scenario():
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3)
        events = [
            event async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("inspect the target")])
            )
        ]
        return agent, llm, events

    agent, llm, events = run(scenario())

    image_parts = [
        part
        for message in llm.second_messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert image_parts[0]["image_url"]["detail"] == "original"
    third_image_parts = [
        part
        for message in llm.third_messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]
    assert third_image_parts == []
    durable = json.dumps({"events": events, "messages": agent.context.messages}, ensure_ascii=False)
    assert "iVBOR" not in durable
    assert "AXButton" not in durable
    assert str(manager.session_dir) not in durable
    assert "Request-local Computer Use screenshot omitted" in durable


def test_react_redacts_credentials_from_assistant_tool_history_and_events(
    registry, manager, backend,
):
    secret = "api_key=DO-NOT-PERSIST-THIS-TEXT"
    backend.act_result = ComputerActionResult(
        result={
            "outcomes": [{"index": 0, "ok": False, "error_code": "unknown_outcome"}],
            "last_acknowledged_action": -1,
        },
        error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown"),
    )
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "computer-act-1",
                        "name": "computer_act",
                        "arguments": json.dumps({
                            "snapshot_id": snapshot.snapshot_id,
                            "actions": [{"type": "type", "text": secret}],
                        }),
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "stopped", "usage": None}

    async def scenario():
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=3)
        events = [
            event async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("type the requested value")])
            )
        ]
        return agent, events

    agent, events = run(scenario())

    durable = json.dumps({"events": events, "messages": agent.context.messages}, ensure_ascii=False)
    assert secret not in durable
    act_call = next(call for name, call in backend.calls if name == "act")
    assert act_call[1][0]["text"] == secret


def test_react_repeat_circuit_does_not_persist_request_local_arguments(registry, manager):
    secret = "password=REPEATED-PRIVATE-TEXT"
    register(registry, manager)

    class RepeatingLLM:
        class Config:
            capabilities = frozenset({"tools", "vision"})

        config = Config()

        async def chat_stream(self, messages, tools):
            del messages, tools
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": "repeat-act",
                    "name": "computer_act",
                    "arguments": json.dumps({
                        "snapshot_id": "snapshot",
                        "actions": [{"type": "type", "text": secret, "unknown": True}],
                    }),
                }],
                "content": "",
                "usage": None,
            }

    async def scenario():
        agent = ReActAgent("agent", RepeatingLLM(), registry, max_iterations=3)
        agent.tool_allowlist = {"computer_act"}
        events = [
            event async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("perform the action")])
            )
        ]
        return agent, events

    agent, events = run(scenario())

    durable = json.dumps({"events": events, "messages": agent.context.messages}, ensure_ascii=False)
    assert "ToolCircuitOpen" in durable or "repeated tool call" in durable
    assert secret not in durable


def test_malformed_request_local_tool_call_omits_raw_arguments_from_diagnostic(
    registry, manager, tmp_path,
):
    secret = "MALFORMED-PRIVATE-TEXT"
    register(registry, manager)
    agent = ReActAgent("agent", object(), registry)
    agent.context.set_session(str(tmp_path / "session.json"))
    raw = (
        '{"snapshot_id":"snapshot","actions":['
        f'{{"type":"type","text":"{secret}\\q"}}]}}'
    )

    calls, failure = agent._validated_tool_calls(
        [{"id": "bad-act", "name": "computer_act", "arguments": raw}],
        "stop",
        None,
    )

    assert calls is None
    assert failure is not None and failure.artifact_ref
    diagnostic = Path(failure.artifact_ref).read_text(encoding="utf-8")
    assert secret not in diagnostic
    assert "request_local_placeholder" in diagnostic


@pytest.mark.parametrize("typed_text,masked", [
    ("验证🧪e\u0301!", False), ("api_key=synthetic-persistence-value", True),
])
def test_typed_text_persistence_across_requests_and_session_restore(
    registry, manager, backend, monkeypatch, tmp_path, typed_text, masked,
):
    from agent.runtime.task_store import TaskStore
    store = TaskStore(tmp_path / "tasks.db")
    task = store.start_run("verify-text", "enter requested value", session_id="test", model="test")
    backend.focused_value = "before"
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    run(manager.select("app-1", "window-1"))
    snapshot_id = json.loads(run(registry.execute(
        "computer_snapshot", {"scope": "target_window"},
    ))["fresh_output"])["snapshot_id"]
    original_act = backend.act

    async def deliver_text(*args, **kwargs):
        result = await original_act(*args, **kwargs)
        backend.focused_value = args[2][0].text
        return result

    monkeypatch.setattr(backend, "act", deliver_text)

    class VerifyingLLM:
        def __init__(self):
            self.prompts = []

        async def chat_stream(self, messages, tools):
            self.prompts.append(messages)
            if len(self.prompts) == 1:
                call = {
                    "id": "type-evidence",
                    "name": "computer_act",
                    "arguments": json.dumps({
                        "snapshot_id": snapshot_id,
                        "actions": [{
                            "type": "type",
                            "text": typed_text,
                            "element_ref": f"{snapshot_id}:1",
                        }],
                    }),
                }
            elif len(self.prompts) == 2:
                call = {"id": "later-status", "name": "computer_status", "arguments": "{}"}
            else:
                yield {"type": "done", "content": "verified", "usage": None}
                return
            yield {"type": "tool_calls", "calls": [call], "content": "", "usage": None}

    async def scenario():
        llm = VerifyingLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3, task_store=store)
        agent.context.set_session(str(tmp_path / "session.json"))
        events = [event async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("enter and verify the requested value")], metadata={"task_id": task["id"]})
        )]
        return agent, llm, events

    agent, llm, events = run(scenario())

    delivered = next(value for name, value in backend.calls if name == "act")
    assert delivered[1][0]["text"] == typed_text
    fresh_result = next(
        message["content"] for message in llm.prompts[1]
        if message.get("tool_call_id") == "type-evidence"
    )
    assert f'"value":"{typed_text}"' in fresh_result
    assert "matching_control_value_changed" in fresh_result
    image_parts = [
        part for message in llm.prompts[1]
        if isinstance(message.get("content"), list)
        for part in message["content"] if part.get("type") == "image_url"
    ]
    assert len(image_parts) == 1
    assert base64.b64decode(image_parts[0]["image_url"]["url"].split(",", 1)[1]) == PNG
    later = json.dumps(llm.prompts[2], ensure_ascii=False)
    durable = json.dumps({"events": events, "messages": agent.context.messages}, ensure_ascii=False)
    assert (typed_text in later) is not masked
    assert (typed_text in durable) is not masked
    assert "iVBOR" not in later and "iVBOR" not in durable
    assert ("[redacted" in durable) is masked
    from agent.runtime.context import AgentContext
    agent.context.save()
    restored = AgentContext(system_prompt="system")
    restored.set_session(str(tmp_path / "session.json"))
    assert restored.load()
    assert (typed_text in json.dumps(restored.messages, ensure_ascii=False)) is not masked
    persisted_task = json.dumps(store.get_task(task["id"]), ensure_ascii=False)
    assert (typed_text in persisted_task) is not masked
    archive = (tmp_path / ".artifacts/session.raw-tool-calls.jsonl").read_text(encoding="utf-8")
    assert (typed_text in archive) is not masked
    act_event = next(event for event in events if event.get("name") == "computer_act" and "args" in event)
    assert act_event["computer_receipt"]["dispatch_state"] == "acknowledged"
    assert act_event["computer_receipt"]["next_step"] == "observe_result"
    for saved in (later, durable, persisted_task, json.dumps(restored.messages, ensure_ascii=False)):
        assert 'Computer receipt:' in saved or 'computer_receipt' in saved
        assert 'dispatch_state' in saved and 'acknowledged' in saved
    assert act_event["args"]["actions"][0]["text"] == (
        f"[redacted {len(typed_text)} chars]" if masked else typed_text
    )
    assert agent._request_local_tool_context_ids == set()
    assert agent._request_local_image_overlay_messages == []


@pytest.mark.parametrize("text", [
    "汉字🧪e\u0301!", "abcdef", "", "  spaces\n and tabs\t", "password reset instructions",
    '{ "query": "Docker", "limit": 4 }', 'https://example.org/search?q=Docker&page=2',
    'token count: 4', 'the bearer of this letter',
    '{ "password": "", "query": "Docker" }', '{"password":null}', '{"authorization":false}',
    "password=''", 'api_key=""', "const example = {\"password\":\"\"};",
    '{"password":[]}', '{"password":{}}',
    pytest.param('{"number":' + '9' * 5000 + '}', id="large-json-integer"),
    pytest.param('[' * 1500 + '0' + ']' * 1500, id="deep-ordinary-json"),
])
def test_act_preserves_ordinary_text_across_persistence(registry, manager, text):
    register(registry, manager)
    tool = registry.get("computer_act")
    raw = {
        "snapshot_id": "snap",
        "actions": [{"type": "type", "text": text}],
    }

    first = registry.persistence_safe_args(tool, raw)
    second = registry.persistence_safe_args(tool, first)

    assert first["actions"][0]["text"] == text
    assert second == first
    assert raw["actions"][0]["text"] == text


@pytest.mark.parametrize("text", [
    "password=汉字🧪e\u0301!", 'api_key="hello world"', "OPENAI_API_KEY=synthetic-key",
    "Authorization: Bearer synthetic-token", "Bearer abcdef0123456789",
    "https://user:synthetic-password@example.org/a",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-key\n-----END OPENSSH PRIVATE KEY-----",
    '{"credentials":{"pass\\u0077ord":"synthetic-password"}}',
    '{"access_token":"synthetic-token"}',
    "api-key: synthetic-key", "clientSecret=synthetic-secret",
    "https://:synthetic-password@example.org/a",
    "AWS_SECRET_ACCESS_KEY=synthetic-secret", "secret_key=synthetic-secret",
    "api_key='synthetic-unfinished", 'password="synthetic-unfinished',
    "api_key=[1234]", 'password={"value":"synthetic"}',
    "https://user:abc[0]def@example.org", "PASSWORD=false", "PASSWORD=true",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\nsynthetic-key\n-----END PGP PRIVATE KEY BLOCK-----",
    pytest.param('{"pass\\u0077ord":' + '[' * 1500 + '"synthetic"' + ']' * 1500 + '}', id="deep-escaped-key"),
])
def test_act_masks_only_credential_bearing_inputs_idempotently(registry, manager, text):
    register(registry, manager)
    tool = registry.get("computer_act")
    raw = {"snapshot_id": "snap", "actions": [{"type": "type", "text": text}]}
    first = registry.persistence_safe_args(tool, raw)
    assert first["actions"][0]["text"] == f"[redacted {len(text)} chars]"
    assert registry.persistence_safe_args(tool, first) == first
    assert raw["actions"][0]["text"] == text


def test_cu_text_classifier_bounds_escaped_quote_scanning():
    from time import process_time

    from agent.runtime.computer_text import contains_credentials

    text = '\\"' * 10_000  # One legal 20,000-character CU input, no credentials.
    started = process_time()
    for _ in range(3):
        assert not contains_credentials(text)
    # Generous CPU budget for 60,000 characters; catches quadratic rescanning
    # without measuring scheduler delays or desktop activity.
    assert process_time() - started < 1.0


@pytest.mark.parametrize("marker", ["[redacted 4 chars]", "[redacted 6 chars]", "[redacted 18 chars]"])
def test_act_rejects_redacted_text_replay_before_planning_or_input(
    registry, manager, backend, marker,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    backend.calls.clear()

    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot.snapshot_id,
        "actions": [{"type": "type", "text": marker}],
    }))

    assert result.get("code") == "invalid_arguments"
    assert "redaction marker" in result["error"]
    assert "original" in result["error"]
    assert backend.calls == []


def test_assistant_history_replays_request_local_arguments_with_redacted_text(registry, manager):
    """Model-facing history must show the real call structure with only
    sensitive fields redacted — never a placeholder shape that models imitate,
    and never the credential value."""
    register(registry, manager)
    agent = ReActAgent("agent", object(), registry)
    secret = "api_key=DO-NOT-PERSIST-THIS-TEXT"
    msg = agent._assistant_message(
        "acting",
        [{
            "id": "act-1",
            "name": "computer_act",
            "arguments": json.dumps({
                "snapshot_id": "snap",
                "actions": [
                    {"type": "click", "element_index": 134},
                    {"type": "type", "text": secret},
                ],
            }),
        }],
        "",
    )
    rendered = msg["tool_calls"][0]["function"]["arguments"]
    assert secret not in rendered
    parsed = json.loads(rendered)
    assert parsed == {
        "snapshot_id": "snap",
        "actions": [
            {"type": "click", "element_index": 134},
            {"type": "type", "text": "[redacted 32 chars]"},
        ],
    }
    assert "request_local_placeholder" not in rendered


def test_model_placeholder_shaped_arguments_rejected_with_teaching_hint(
    registry, manager, tmp_path,
):
    """Model output that imitates the request-local placeholder shape must be
    rejected at validation with a hint to emit real schema arguments."""
    register(registry, manager)
    agent = ReActAgent("agent", object(), registry)
    agent.context.set_session(str(tmp_path / "session.json"))
    placeholder = json.dumps({
        "request_local_placeholder": True,
        "argument_keys": ["argument_keys", "collection_sizes", "request_local_placeholder"],
        "collection_sizes": {"argument_keys": 3, "collection_sizes": 2},
    })
    calls, failure = agent._validated_tool_calls(
        [{"id": "imitating-act", "name": "computer_act", "arguments": placeholder}],
        "stop",
        None,
    )
    assert calls is None
    assert failure is not None
    assert failure.code == "invalid_arguments"
    assert "request_local_placeholder" in failure.message
    assert "snapshot_id" in failure.recovery_hint
    assert failure.retryable


@pytest.mark.parametrize("text,masked", [
    ("普通搜索🧪", False), ("api_key=RAW-ARCHIVE-SECRET", True),
])
def test_tool_call_archive_preserves_ordinary_text_and_redacts_credentials(
    registry, manager, tmp_path, text, masked,
):
    """The diagnostic archive follows the same persistence policy as history."""
    secret = text
    register(registry, manager)
    agent = ReActAgent("agent", object(), registry)
    agent.context.set_session(str(tmp_path / "session.json"))
    agent._assistant_message(
        "acting",
        [{
            "id": "act-1",
            "name": "computer_act",
            "arguments": json.dumps({
                "snapshot_id": "snap",
                "actions": [{"type": "type", "text": secret}],
            }),
        }],
        "",
    )
    audit_path = tmp_path / ".artifacts" / "session.raw-tool-calls.jsonl"
    assert audit_path.exists()
    records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["name"] == "computer_act"
    assert records[0]["call_id"] == "act-1"
    stored_text = json.loads(records[0]["arguments"])["actions"][0]["text"]
    assert stored_text == (f"[redacted {len(text)} chars]" if masked else text)
    assert records[0]["argument_persistence"] == "request_local"
    # The audit trail is model-invisible: no message was appended to history.
    assert agent.context.messages == []


# --- screenshot request-body budget -------------------------------------------------
# Remote gateways can stop draining request bodies around ~1 MiB, turning an oversized
# screenshot into a provider timeout. These pin the recompression contract: fit the body
# budget without losing pixels, never touch a small capture, and never lose the image.


@lru_cache(maxsize=4)
def _screenshot_png_bytes(width: int = 2876, height: int = 1718) -> bytes:
    """A screenshot-plausible PNG: smooth gradient content under flat overlay blocks.

    Flat UI alone compresses too well to ever trip the budget, and pure per-pixel noise is
    the worst case for every lossy codec, so the fixture carries both the way a real Retina
    capture does: PNG pays for the gradient, WebP does not.
    """
    import io
    import random

    from PIL import Image, ImageDraw

    random.seed(7)
    thumb = Image.new("RGB", (40, 24))
    thumb.putdata(
        [
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in range(40 * 24)
        ]
    )
    image = thumb.resize((width, height), Image.Resampling.BICUBIC)
    draw = ImageDraw.Draw(image)
    for y in range(120, max(121, height - 60), 40):
        top = min(y, height - 1)
        draw.rectangle(
            (
                min(60, width - 1),
                top,
                min(60 + random.randint(400, max(401, width // 2)), width - 1),
                min(top + 9, height - 1),
            ),
            fill=(250, 250, 250),
        )
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _decoded_screenshot(url: str):
    import io

    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


def test_oversized_screenshot_is_recompressed_without_losing_pixels():
    from agent.runtime.tools import computer as computer_tools

    png = _screenshot_png_bytes()
    png_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    assert len(png_url) > computer_tools._screenshot_data_url_budget()

    url, meta = computer_tools._encode_screenshot_data_url(png)

    assert len(url) <= computer_tools._screenshot_data_url_budget()
    assert url.startswith(("data:image/webp;base64,", "data:image/jpeg;base64,"))
    assert meta["recompressed"] is True
    assert meta["fits_budget"] is True
    assert meta["scale"] == 1.0
    assert _decoded_screenshot(url).size == (2876, 1718)


def test_small_screenshot_keeps_original_png_bytes_untouched():
    from agent.runtime.tools import computer as computer_tools

    png = _screenshot_png_bytes(width=64, height=48)

    url, meta = computer_tools._encode_screenshot_data_url(png)

    assert url == "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    assert meta["recompressed"] is False
    assert meta["reason"] == "within_budget"


def test_unreachable_budget_still_ships_the_smallest_candidate_and_honors_the_floor(
    monkeypatch,
):
    from agent.runtime.tools import computer as computer_tools

    png = _screenshot_png_bytes()
    png_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    monkeypatch.setenv("CU_IMAGE_MAX_DATA_URL_BYTES", "20000")
    url, meta = computer_tools._encode_screenshot_data_url(png)

    # A budget nothing can reach must still improve on the PNG and never downscale past the
    # legibility floor, because dropping the screenshot costs more than a big body.
    assert len(url) < len(png_url)
    assert meta["fits_budget"] is False
    assert max(_decoded_screenshot(url).size) >= computer_tools._SCREENSHOT_MIN_LONG_EDGE


def test_recompressed_webp_stays_request_local_and_out_of_public_events():
    event = {
        "error": "",
        "request_local_placeholder": True,
        "_private_result": {
            "image_data_urls": ["data:image/webp;base64," + base64.b64encode(b"pixels").decode("ascii")],
            "detail": "original",
        },
        "_fresh_output": json.dumps(
            {"type": "image_attachment", "detail": "original", "image_labels": []}
        ),
    }

    separated = ReActAgent._separate_request_local_image_attachment(event)
    payload = separated.get("_request_local_image_attachment")

    assert isinstance(payload, dict)
    assert payload["image_data_urls"][0].startswith("data:image/webp;base64,")
    assert "_private_result" not in separated


def test_unrelated_data_url_is_still_not_separated_as_a_screenshot():
    event = {
        "error": "",
        "request_local_placeholder": True,
        "_private_result": {
            "image_data_urls": ["data:application/pdf;base64," + base64.b64encode(b"x").decode("ascii")],
        },
        "_fresh_output": json.dumps({"type": "image_attachment"}),
    }

    assert ReActAgent._separate_request_local_image_attachment(event) is event


def test_cancel_during_action_planning_propagates(registry, manager, monkeypatch):
    register(registry, manager)
    focus(registry)

    async def scenario():
        entered = asyncio.Event()
        async def plan(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(manager, "plan_actions", plan)
        task = asyncio.create_task(registry.execute("computer_act", {
            "snapshot_id": "cancel-test", "actions": [{"type": "wait", "duration_ms": 0}],
        }))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(scenario())


def test_permanent_post_action_observation_failure_is_not_retried(registry, manager, backend, monkeypatch):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    attempts = 0
    async def fail(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ComputerSessionError("unsafe_artifact")
    monkeypatch.setattr(backend, "snapshot", fail)
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}],
    }))
    assert result["code"] == "post_action_observation_pending"
    assert result["details"]["observation_error_code"] == "unsafe_artifact"
    assert attempts == 1
    assert sum(name == "act" for name, _value in backend.calls) == 1


def test_screenshot_encoding_runs_outside_event_loop(registry, manager, monkeypatch):
    import threading

    from agent.runtime.tools import computer
    register(registry, manager)
    focus(registry)
    original = computer._encode_screenshot_data_url
    observed = []
    async def scenario():
        loop_thread = threading.get_ident()
        def encode(png):
            observed.append(threading.get_ident() != loop_thread)
            return original(png)
        monkeypatch.setattr(computer, "_encode_screenshot_data_url", encode)
        result = await registry.execute("computer_snapshot", {"scope": "target_window"})
        assert not result["error"]
    run(scenario())
    assert observed == [True]


def test_image_worker_close_during_encoding_discards_publication(registry, manager, monkeypatch):
    import threading

    from agent.runtime.tools import computer
    register(registry, manager)
    focus(registry)
    original = computer._encode_screenshot_data_url
    async def scenario():
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def encode(png):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            return original(png)
        monkeypatch.setattr(computer, "_encode_screenshot_data_url", encode)
        task = asyncio.create_task(registry.execute("computer_snapshot", {"scope": "target_window"}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await manager.close()
        finally:
            release.set()
        result = await task
        assert result["code"] == "session_closed"
        assert "image_data_urls" not in json.dumps(result)
    run(scenario())


def test_cancelled_image_workers_retain_capacity_until_thread_finishes(monkeypatch):
    import threading

    from agent.runtime.tools import computer
    async def scenario():
        entered = asyncio.Queue()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def encode(png):
            loop.call_soon_threadsafe(entered.put_nowait, png)
            assert release.wait(3)
            return "data", {}
        monkeypatch.setattr(computer, "_encode_screenshot_data_url", encode)
        first = asyncio.create_task(computer._encode_screenshot_async(b"1"))
        second = asyncio.create_task(computer._encode_screenshot_async(b"2"))
        third = None
        try:
            await asyncio.wait_for(entered.get(), 1)
            await asyncio.wait_for(entered.get(), 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            third = asyncio.create_task(computer._encode_screenshot_async(b"3"))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(entered.get(), .05)
        finally:
            release.set()
            await second
            if third is not None:
                await third
        assert await asyncio.wait_for(entered.get(), 1) == b"3"
    run(scenario())


def test_successful_takeover_ends_before_screenshot_encoding(registry, manager, backend, monkeypatch):
    from agent.runtime.tools import computer
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    backend.force_takeover = True
    observed = []
    original = computer._encode_screenshot_data_url
    def encode(png):
        observed.append(any(name == "takeover_end" for name, _ in backend.calls))
        return original(png)
    monkeypatch.setattr(computer, "_encode_screenshot_data_url", encode)
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
        "interaction_mode": "foreground_takeover",
    }))
    assert result["error"] == ""
    assert observed == [True]
    assert sum(name == "takeover_end" for name, _ in backend.calls) == 1


def test_image_search_base64_encodes_only_final_candidate(monkeypatch):
    from agent.runtime.tools import computer
    png = _screenshot_png_bytes()
    monkeypatch.setenv("CU_IMAGE_MAX_DATA_URL_BYTES", "20000")
    original = base64.b64encode
    calls = []
    def encode(value):
        calls.append(len(value))
        return original(value)
    monkeypatch.setattr(computer.base64, "b64encode", encode)
    url, metadata = computer._encode_screenshot_data_url(png)
    assert len(calls) == 1
    assert metadata["data_url_bytes"] == len(url)


def test_post_action_observation_has_one_total_deadline(registry, manager, backend, monkeypatch):
    from agent.runtime.tools import computer
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    calls = []
    async def delayed(*args, **kwargs):
        calls.append("snapshot")
        await asyncio.sleep(.05)
        raise ComputerSessionError("helper_failed")
    monkeypatch.setattr(backend, "snapshot", delayed)
    monkeypatch.setattr(computer, "_POST_ACTION_OBSERVATION_TIMEOUT_SECONDS", .01, raising=False)
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}],
    }))
    assert result["code"] == "post_action_observation_pending"
    assert result["details"]["observation_error_code"] == "observation_timeout"
    assert calls == ["snapshot"]
    assert sum(name == "act" for name, _ in backend.calls) == 1


def test_catalog_includes_advisory_routes_without_rewriting_identity(registry, manager, backend):
    register(registry, manager)
    result = run(registry.execute("computer_apps", {}))
    app = json.loads(result["output"])["apps"][0]
    assert app["app_ref"] == backend.catalog_app_ref
    assert app["routing_advice"]["advisory_only"] is True
    assert app["routing_advice"]["real_app_acceptance"] == "unknown"
    assert app["routing_advice"]["routes"] == []


def test_post_action_deadline_includes_encoding_queue(registry, manager, backend, monkeypatch):
    from agent.runtime.tools import computer
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    original = computer._encode_screenshot_async
    async def delayed(png):
        await asyncio.sleep(.05)
        return await original(png)
    monkeypatch.setattr(computer, "_encode_screenshot_async", delayed)
    monkeypatch.setattr(computer, "_POST_ACTION_OBSERVATION_TIMEOUT_SECONDS", .01)
    backend.force_takeover = True
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}],
        "interaction_mode": "foreground_takeover",
    }))
    assert result["code"] == "post_action_observation_pending"
    assert result["details"]["observation_error_code"] == "observation_timeout"
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert sum(name == "takeover_end" for name, _ in backend.calls) == 1


def test_app_state_and_action_snapshots_chain_without_extra_focus_or_capture(registry, manager, backend):
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    register(registry, manager)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    state = run(registry.execute("computer_get_app_state", {
        "app_ref": "app-1", "window_ref": "window-1",
    }))
    assert state["error"] == ""
    snapshot = json.loads(state["fresh_output"])
    first_id = snapshot["snapshot_id"]
    for _ in range(2):
        acted = run(registry.execute("computer_act", {
            "snapshot_id": snapshot["snapshot_id"],
            "actions": [{"type": "click", "element_ref": f"{snapshot['snapshot_id']}:1"}],
        }))
        assert acted["error"] == ""
        fresh = json.loads(acted["fresh_output"])
        assert fresh["snapshot_id"] != snapshot["snapshot_id"]
        snapshot = fresh
    assert not any(name == "select" for name, _ in backend.calls)
    assert sum(name == "snapshot" for name, _ in backend.calls) == 3
    act_count = sum(name == "act" for name, _ in backend.calls)
    stale = run(registry.execute("computer_act", {
        "snapshot_id": first_id,
        "actions": [{"type": "click", "element_ref": f"{first_id}:1"}],
    }))
    assert stale["error"]
    assert sum(name == "act" for name, _ in backend.calls) == act_count


def test_action_observation_uses_returned_snapshot_without_extra_capture(registry, manager, backend, monkeypatch):
    backend.snapshot_ax_tree_override = {"role": "AXTextField", "label": "Search", "element_ref": "search", "value": "before", "bounds": {"x": 1, "y": 2, "width": 40, "height": 20}}
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    original_act = backend.act

    async def change_value(*args, **kwargs):
        result = await original_act(*args, **kwargs)
        backend.snapshot_ax_tree_override = {**backend.snapshot_ax_tree_override, "value": "after", "element_ref": "new-search"}
        return result

    monkeypatch.setattr(backend, "act", change_value)
    result = run(registry.execute("computer_act", {"snapshot_id": initial["snapshot_id"], "actions": [{"type": "click", "element_ref": "search"}]}))
    payload = json.loads(result["fresh_output"])
    assert payload["action_observation"] == {"status": "changed", "evidence": "matching_control_value_changed"}
    assert payload["action_result"]["effect_verification"] == "unverified"
    assert sum(name == "snapshot" for name, _ in backend.calls) == 2
    assert sum(name == "act" for name, _ in backend.calls) == 1


def test_post_action_target_gone_returns_fresh_catalog_without_replaying_or_granting(registry, manager, backend, monkeypatch):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    async def lost_window(*_args, **_kwargs):
        raise ComputerSessionError("target_gone")
    monkeypatch.setattr(backend, "snapshot", lost_window)
    backend.catalog_window_ref = "modal-ref"
    backend.catalog_window_identity_ref = "modal-identity"
    result = run(registry.execute("computer_act", {"snapshot_id": initial["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}]}))
    assert result["code"] == "post_action_observation_pending"
    assert result["details"]["observation_error_code"] == "target_gone"
    assert result["details"]["observation_status"] == "post_action_observation_pending"
    assert "not observed" in result["error"]
    recovery = result["details"]["window_transition"]
    assert recovery["status"] == "previous_target_not_observed"
    assert recovery["next_observation"]["arguments"]["window_ref"] == "modal-ref"
    assert "Other Fixture" not in json.dumps(recovery)
    assert manager.target is None
    assert not manager.grants
    assert result["computer_receipt"]["next_step"] == "follow_recovery"
    assert "computer_get_app_state" in result["recovery_hint"]
    assert "computer_snapshot" not in receipt_instruction(result["computer_receipt"])
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert sum(name == "apps" for name, _ in backend.calls) == 2


@pytest.mark.parametrize("interaction_mode", ["background", "foreground_takeover"])
def test_acknowledged_title_transition_returns_same_window_state_without_extra_tool_call(
    registry, manager, backend, monkeypatch, interaction_mode,
):
    register(registry, manager)
    approvals = []

    async def approve(request):
        approvals.append(request)
        return "once"

    registry.set_approval_handler(approve)
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    original_snapshot = backend.snapshot

    async def snapshot_current_window(target, *args, **kwargs):
        if target.window_ref == "window-1":
            raise ComputerSessionError("target_gone")
        return await original_snapshot(target, *args, **kwargs)

    monkeypatch.setattr(backend, "snapshot", snapshot_current_window)
    backend.catalog_window_ref = "fresh-window-ref"
    backend.catalog_window_title = "Containers - Docker Desktop"
    approval_count = len(approvals)
    result = run(registry.execute("computer_act", {
        "snapshot_id": initial["snapshot_id"],
        "interaction_mode": interaction_mode,
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["error"] == ""
    payload = json.loads(result["fresh_output"])
    transition = payload["window_transition"]
    assert transition["observation_status"] == "observed"
    assert transition["observation_reason"] == "exact_previous_target"
    assert transition["windows"][0]["title"] == "Containers - Docker Desktop"
    assert "next_observation" not in transition
    assert payload["snapshot_id"] != initial["snapshot_id"]
    assert payload["action_result"]["last_acknowledged_action"] == 0
    assert payload["action_result"]["effect_verification"] == "unverified"
    assert "does not prove the intended" in payload["message"]
    assert manager.target == ComputerTarget("app-1", "fresh-window-ref")
    assert not manager.grants
    assert len(approvals) == approval_count + (interaction_mode == "foreground_takeover")
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert sum(name == "get_app_state" for name, _ in backend.calls) == 1
    assert sum(name == "select" for name, _ in backend.calls) == 1
    if interaction_mode == "foreground_takeover":
        calls = [name for name, _ in backend.calls]
        assert calls.count("takeover_end") == 1
        assert calls.index("takeover_end") < calls.index("get_app_state")


@pytest.mark.parametrize("receipt", [
    {"last_acknowledged_action": -1, "outcomes": [{"index": 0, "ok": False, "error_code": "unknown_outcome"}]},
    {"last_acknowledged_action": 0, "outcomes": [{"index": 0, "ok": False, "error_code": "unknown_outcome"}]},
])
def test_unknown_input_with_changed_title_never_reobserves_or_upgrades_receipt(
    registry, manager, backend, receipt,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    backend.catalog_window_ref = "fresh-window-ref"
    backend.catalog_window_title = "Containers - Docker Desktop"
    backend.act_result = ComputerActionResult(
        result=receipt, error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown"),
    )

    result = run(registry.execute("computer_act", {
        "snapshot_id": initial["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["code"] == "unknown_outcome"
    assert result["details"]["last_acknowledged_action"] == receipt["last_acknowledged_action"]
    assert result["details"]["window_transition"]["observation_reason"] == "exact_previous_target"
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert not any(name == "get_app_state" for name, _ in backend.calls)
    assert manager.target is None and not manager.grants


@pytest.mark.parametrize("receipt", [
    {},
    {"last_acknowledged_action": -1, "outcomes": [{"index": 0, "ok": True}]},
    {"last_acknowledged_action": 0, "outcomes": []},
    {"last_acknowledged_action": 0, "outcomes": [{"index": 0, "ok": False}]},
    {"last_acknowledged_action": 0, "outcomes": [{"index": False, "ok": True}]},
    {"last_acknowledged_action": 0, "outcomes": [{"index": 0, "ok": True, "error_code": "unknown_outcome"}]},
    {"last_acknowledged_action": 1, "outcomes": [{"index": 0, "ok": True}]},
])
def test_incomplete_success_receipt_never_authorizes_same_window_reobservation(
    registry, manager, backend, monkeypatch, receipt,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])

    async def lost_window(*_args, **_kwargs):
        raise ComputerSessionError("target_gone")

    monkeypatch.setattr(backend, "snapshot", lost_window)
    backend.catalog_window_ref = "fresh-window-ref"
    backend.act_result = ComputerActionResult(result=receipt)
    result = run(registry.execute("computer_act", {
        "snapshot_id": initial["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["code"] == "unknown_outcome"
    assert not any(name == "get_app_state" for name, _ in backend.calls)
    assert sum(name == "act" for name, _ in backend.calls) == 1


@pytest.mark.parametrize("identity_change", [
    "missing_identity", "changed_identity", "duplicate_identity", "new_window", "duplicate_app",
])
def test_reobservation_requires_unique_exact_previous_window(
    registry, manager, backend, monkeypatch, identity_change,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    original_apps = backend.apps

    async def changed_catalog():
        catalog = await original_apps()
        app = catalog.apps[0]
        window = app["windows"][0]
        if identity_change == "missing_identity":
            window.pop("window_identity_ref")
        elif identity_change == "changed_identity":
            window["window_identity_ref"] = "replacement-window"
        elif identity_change in {"duplicate_identity", "new_window"}:
            app["windows"].append({
                **window,
                "window_ref": "another-ref",
                "window_identity_ref": window["window_identity_ref"] if identity_change == "duplicate_identity" else "new-window",
            })
        else:
            return ComputerAppCatalog(catalog.generation, (*catalog.apps, {**app, "app_ref": "duplicate-app"}))
        return catalog

    async def lost_window(*_args, **_kwargs):
        raise ComputerSessionError("target_gone")

    monkeypatch.setattr(backend, "snapshot", lost_window)
    monkeypatch.setattr(backend, "apps", changed_catalog)
    backend.catalog_window_ref = "fresh-window-ref"
    result = run(registry.execute("computer_act", {
        "snapshot_id": initial["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["code"] == "post_action_observation_pending"
    assert not any(name == "get_app_state" for name, _ in backend.calls)
    assert sum(name == "act" for name, _ in backend.calls) == 1


@pytest.mark.parametrize("interruption", ["handoff", "catalog", "target"])
def test_same_window_reobservation_rechecks_authority_inside_manager_lock(
    registry, manager, backend, monkeypatch, interruption,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    original_snapshot = backend.snapshot
    original_get_state = manager.get_app_state

    async def snapshot_current_window(target, *args, **kwargs):
        if target.window_ref == "window-1":
            raise ComputerSessionError("target_gone")
        return await original_snapshot(target, *args, **kwargs)

    async def interrupt_then_get_state(*args, **kwargs):
        if interruption == "handoff":
            await manager.select("app-1", "fresh-window-ref")
            await manager.handoff()
        elif interruption == "catalog":
            await manager.apps()
        else:
            await manager.select("app-2", "window-3")
        return await original_get_state(*args, **kwargs)

    monkeypatch.setattr(backend, "snapshot", snapshot_current_window)
    monkeypatch.setattr(manager, "get_app_state", interrupt_then_get_state)
    backend.catalog_window_ref = "fresh-window-ref"
    result = run(registry.execute("computer_act", {
        "snapshot_id": initial["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["code"] == "post_action_observation_pending"
    assert not any(name == "get_app_state" for name, _ in backend.calls)
    assert sum(name == "act" for name, _ in backend.calls) == 1
    if interruption == "handoff":
        assert manager.handed_off
        rendered = ReActAgent._tool_result_context({"name": "computer_act", **result})
        assert "Next step:" in rendered and "Recovery:" in rendered
        assert result["computer_receipt"]["next_step"] == "follow_recovery"
        assert result["computer_receipt"]["replay"] == "forbidden"
        assert "computer_status" in result["recovery_hint"]
        assert "computer_resume" in result["recovery_hint"]
        assert "After the user yields control" in result["recovery_hint"]
        assert "computer_apps" not in rendered
        assert "Capture a fresh snapshot" not in rendered
    if interruption == "target":
        assert manager.target == ComputerTarget("app-2", "window-3")


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_same_window_reobservation_failure_preserves_receipt_and_never_replays(
    registry, manager, backend, monkeypatch, failure,
):
    import agent.runtime.tools.computer as computer_tools

    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])

    async def lost_window(*_args, **_kwargs):
        raise ComputerSessionError("target_gone")

    async def unavailable_observation(*_args, **_kwargs):
        backend.calls.append(("get_app_state", "unavailable"))
        if failure == "timeout":
            await asyncio.sleep(1)
        raise RuntimeError("PRIVATE-OBSERVATION-FAILURE")

    monkeypatch.setattr(backend, "snapshot", lost_window)
    monkeypatch.setattr(backend, "get_app_state", unavailable_observation)
    monkeypatch.setattr(computer_tools, "_POST_ACTION_OBSERVATION_TIMEOUT_SECONDS", 0.04)
    backend.catalog_window_ref = "fresh-window-ref"
    result = run(registry.execute("computer_act", {
        "snapshot_id": initial["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["code"] == "post_action_observation_pending"
    assert result["details"]["action_result"]["last_acknowledged_action"] == 0
    assert result["details"]["action_result"]["effect_verification"] == "unverified"
    assert "PRIVATE-OBSERVATION" not in json.dumps(result)
    assert sum(name == "get_app_state" for name, _ in backend.calls) == 1
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert manager.target is None and not manager.grants


@pytest.mark.parametrize("failure", ["catalog_error", "catalog_timeout", "authority_changed"])
def test_transition_recovery_is_bounded_and_never_replays(registry, manager, backend, monkeypatch, failure):
    import agent.runtime.tools.computer as computer_tools
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    catalog_attempts = 0
    original_apps = backend.apps

    async def catalog_fail():
        nonlocal catalog_attempts
        catalog_attempts += 1
        if failure == "catalog_timeout":
            await asyncio.sleep(1)
        elif failure == "catalog_error":
            raise RuntimeError("PRIVATE-RECOVERY-FAILURE")
        return await original_apps()

    async def lost_window(*_args, **_kwargs):
        if failure == "authority_changed":
            # A concurrent catalog operation supersedes the action's observation context.
            await registry.execute("computer_apps", {})
        raise ComputerSessionError("target_gone")

    monkeypatch.setattr(manager, "snapshot", lost_window)
    monkeypatch.setattr(backend, "apps", catalog_fail)
    monkeypatch.setattr(computer_tools, "_POST_ACTION_OBSERVATION_TIMEOUT_SECONDS", 0.04)
    result = run(registry.execute("computer_act", {"snapshot_id": initial["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}]}))
    assert result["code"] == "post_action_observation_pending"
    assert "window_transition" not in result["details"]
    assert "PRIVATE-RECOVERY" not in json.dumps(result)
    assert catalog_attempts == 1
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert manager.target is None and not manager.grants
    rendered = ReActAgent._tool_result_context({"name": "computer_act", **result})
    assert "Next step:" in rendered and "Recovery:" in rendered
    assert result["computer_receipt"]["next_step"] == "follow_recovery"
    assert result["computer_receipt"]["replay"] == "forbidden"
    assert "computer_apps" in result["recovery_hint"]
    assert "computer_get_app_state" in result["recovery_hint"]
    assert "computer_snapshot" not in rendered
    assert "Capture a fresh snapshot" not in rendered


def test_post_action_observation_does_not_capture_a_concurrently_replaced_target(registry, manager, backend, monkeypatch):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    original_snapshot = manager.snapshot

    async def replace_then_observe(*args, **kwargs):
        await manager.select("app-2", "other-window")
        return await original_snapshot(*args, **kwargs)

    monkeypatch.setattr(manager, "snapshot", replace_then_observe)
    result = run(registry.execute("computer_act", {"snapshot_id": initial["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}]}))
    assert result["code"] == "post_action_observation_pending"
    assert sum(name == "snapshot" for name, _ in backend.calls) == 1
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert manager.target == ComputerTarget("app-2", "other-window")
    assert not manager.grants
    rendered = ReActAgent._tool_result_context({"name": "computer_act", **result})
    assert "Next step:" in rendered and "Recovery:" in rendered
    assert result["computer_receipt"]["next_step"] == "follow_recovery"
    assert result["computer_receipt"]["replay"] == "forbidden"
    assert "computer_apps" in result["recovery_hint"]
    assert "computer_get_app_state" in result["recovery_hint"]
    assert "computer_snapshot" not in rendered
    assert "Capture a fresh snapshot" not in rendered


@pytest.mark.parametrize("as_exception", [False, True])
def test_unknown_native_action_returns_observation_candidates_without_claiming_ack(registry, manager, backend, monkeypatch, as_exception):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    receipt = {"last_acknowledged_action": -1, "outcomes": [{"index": 0, "ok": False, "error_code": "unknown_outcome"}]}
    error = ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown")
    backend.act_result = ComputerActionResult(result=receipt, error=error)
    if as_exception:
        async def uncertain(*_args, **_kwargs):
            backend.calls.append(("act", "uncertain"))
            raise HelperApplicationError(error, result=receipt)
        monkeypatch.setattr(backend, "act", uncertain)
    backend.catalog_window_ref = "modal-ref"
    backend.catalog_window_identity_ref = "modal-identity"
    result = run(registry.execute("computer_act", {"snapshot_id": initial["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}]}))
    assert result["code"] == "unknown_outcome"
    assert result["details"]["last_acknowledged_action"] == -1
    assert result["details"]["window_transition"]["next_observation"]["arguments"]["window_ref"] == "modal-ref"
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert sum(name == "snapshot" for name, _ in backend.calls) == 1
    assert manager.target is None and not manager.grants


def test_unmatched_modal_recovery_does_not_suggest_parent_or_replay(registry, manager, backend, monkeypatch):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    original_apps = backend.apps

    async def blocked_catalog():
        catalog = await original_apps()
        catalog.apps[0]["windows"].append({
            "window_ref": "unmatched-panel", "bindable": False,
            "binding_reason": "ax_window_unmatched",
        })
        return catalog

    monkeypatch.setattr(backend, "apps", blocked_catalog)
    backend.act_result = ComputerActionResult(
        result={"last_acknowledged_action": -1, "outcomes": []},
        error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "uncertain"),
    )
    result = run(registry.execute("computer_act", {
        "snapshot_id": initial["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}],
    }))
    transition = result["details"]["window_transition"]
    assert result["code"] == "unknown_outcome"
    assert transition["status"] == "unmatched_window_observed"
    assert "next_observation" not in transition
    assert "Do not bind its parent" in result["recovery_hint"]
    assert sum(name == "act" for name, _ in backend.calls) == 1
    assert sum(name == "snapshot" for name, _ in backend.calls) == 1
    assert manager.target is None and not manager.grants


def test_transition_rechecks_transient_unmatched_windows_once(registry, manager, backend, monkeypatch):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    original_apps = backend.apps
    attempts = 0
    async def settling_apps():
        nonlocal attempts
        attempts += 1
        catalog = await original_apps()
        if attempts == 1:
            catalog.apps[0]["windows"][0]["bindable"] = False
            catalog.apps[0]["windows"][0].pop("window_identity_ref", None)
        return catalog
    async def gone(*_args, **_kwargs):
        raise ComputerSessionError("target_gone")
    monkeypatch.setattr(backend, "snapshot", gone)
    monkeypatch.setattr(backend, "apps", settling_apps)
    backend.catalog_window_ref = "modal-ref"
    backend.catalog_window_identity_ref = "modal-identity"
    result = run(registry.execute("computer_act", {"snapshot_id": initial["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}]}))
    assert attempts == 2
    assert result["details"]["window_transition"]["next_observation"]["arguments"]["window_ref"] == "modal-ref"
    assert sum(name == "act" for name, _ in backend.calls) == 1


def test_failed_second_catalog_refresh_does_not_return_revoked_first_refs(registry, manager, backend, monkeypatch):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    initial = json.loads(run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"])
    original_apps = backend.apps
    attempts = 0
    async def fail_second():
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("catalog unavailable")
        catalog = await original_apps()
        catalog.apps[0]["windows"][0]["bindable"] = False
        return catalog
    async def gone(*_args, **_kwargs):
        raise ComputerSessionError("target_gone")
    monkeypatch.setattr(backend, "snapshot", gone)
    monkeypatch.setattr(backend, "apps", fail_second)
    result = run(registry.execute("computer_act", {"snapshot_id": initial["snapshot_id"], "actions": [{"type": "wait", "duration_ms": 0}]}))
    assert attempts == 2
    assert result["code"] == "post_action_observation_pending"
    assert "window_transition" not in result["details"]
    assert "computer_apps" in result["recovery_hint"]
    assert manager.target is None and not manager.grants
    # The target is revoked, so a snapshot cannot be the next step.
    assert result["computer_receipt"]["next_step"] == "follow_recovery"
    assert "computer_snapshot" not in receipt_instruction(result["computer_receipt"])
    assert sum(name == "act" for name, _ in backend.calls) == 1


def test_subtree_filter_composes_and_updates_published_refs(registry, manager, backend):
    register(registry, manager)
    focus(registry)

    def tree(version):
        return {"role": "AXWindow", "children": [
            {"role": "AXRow", "title": "sidebar", "element_ref": f"sidebar-{version}"},
            {"role": "AXScrollArea", "element_ref": f"anchor-{version}",
             "bounds": {"x": 200, "y": 100, "width": 500, "height": 300},
             "children": [{"role": "AXRow", "element_ref": f"row-{version}"}]},
        ]}

    backend.snapshot_ax_tree_override = tree(1)
    run(registry.execute("computer_snapshot", {}))
    for version in (2, 3):
        backend.snapshot_ax_tree_override = tree(version)
        result = run(registry.execute("computer_snapshot", {
            "subtree_ref": f"anchor-{version - 1}", "role_filter": "AXRow",
        }))
        assert not result.get("error"), result
        projected = json.loads(result["fresh_output"])["ax_tree"]
        assert projected["element_ref"] == f"anchor-{version}"
        assert "sidebar" not in json.dumps(projected)
        assert f"row-{version}" in json.dumps(projected)


def test_subtree_filter_does_not_ignore_unknown_anchor(registry, manager, backend):
    register(registry, manager)
    focus(registry)
    backend.snapshot_ax_tree_override = {
        "role": "AXWindow", "children": [{"role": "AXRow", "element_ref": "row"}],
    }
    result = run(registry.execute("computer_snapshot", {
        "subtree_ref": "missing", "role_filter": "AXRow",
    }))
    assert result.get("code") == "stale_target", result


@pytest.mark.parametrize("native_error", [False, True])
def test_post_action_unavailable_pixels_preserve_capture_cause_and_stop_observing(
    registry, manager, backend, monkeypatch, native_error,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(
        run(registry.execute("computer_snapshot", {"scope": "target_window"}))["fresh_output"]
    )
    attempts = 0

    async def unavailable_after_dispatch(_target, _scope, _artifact):
        nonlocal attempts
        attempts += 1
        if native_error:
            raise HelperApplicationError(ComputerError(
                ComputerErrorCode.WINDOW_CONTENT_UNAVAILABLE, "PRIVATE-EMPTY-IMAGE-DETAIL",
            ))
        raise ComputerSessionError("window_content_unavailable", "PRIVATE-EMPTY-IMAGE-DETAIL")

    monkeypatch.setattr(backend, "snapshot", unavailable_after_dispatch)
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))

    assert result["code"] == "post_action_observation_pending"
    assert result["retryable"] is False
    assert result["details"]["observation_error_code"] == "window_content_unavailable"
    assert result["computer_receipt"]["dispatch_state"] == "acknowledged"
    assert result["computer_receipt"]["next_step"] == "follow_recovery"
    assert result["details"]["action_result"]["last_acknowledged_action"] == 0
    assert "capture/privacy state" in result["recovery_hint"]
    assert "Do not repeat" in result["recovery_hint"]
    assert "no readable pixels" in result["error"]
    assert "fresh_output" not in result
    assert "PRIVATE-EMPTY-IMAGE-DETAIL" not in json.dumps(result)
    assert attempts == 1
    assert sum(name == "act" for name, _value in backend.calls) == 1


@pytest.mark.parametrize("native_error", [False, True])
def test_post_action_overlay_keeps_cause_without_repeating_input_or_observation(
    registry, manager, backend, monkeypatch, native_error,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    attempts = []

    async def blocked(*_args, **_kwargs):
        attempts.append("snapshot")
        if native_error:
            raise HelperApplicationError(ComputerError(
                ComputerErrorCode.OVERLAY_BLOCKED, "PRIVATE-OVERLAY-DETAIL",
            ))
        raise ComputerSessionError("overlay_blocked", "PRIVATE-OVERLAY-DETAIL")

    monkeypatch.setattr(backend, "snapshot", blocked)
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))
    assert result["code"] == "post_action_observation_pending"
    assert result["details"]["observation_error_code"] == "overlay_blocked"
    assert result["computer_receipt"]["dispatch_state"] == "acknowledged"
    assert result["computer_receipt"]["verification_state"] == "unverified"
    assert result["computer_receipt"]["next_step"] == "follow_recovery"
    assert result["computer_receipt"]["replay"] == "forbidden"
    assert result["retryable"] is False
    assert "overlay" in result["recovery_hint"].lower()
    assert "Observe the visible popup as its own catalog target" in result["recovery_hint"]
    assert "stop automatic input" not in receipt_instruction(result["computer_receipt"]).lower()
    assert "Do not repeat" in result["recovery_hint"]
    assert "PRIVATE-OVERLAY-DETAIL" not in json.dumps(result)
    assert attempts == ["snapshot"]
    assert sum(name == "act" for name, _value in backend.calls) == 1


@pytest.mark.parametrize("native_result", [
    None, {}, {"outcomes": [], "last_acknowledged_action": -1},
    {"outcomes": [{"index": 7, "ok": True}], "last_acknowledged_action": 0},
])
def test_post_action_missing_delivery_proof_remains_unknown(
    registry, manager, backend, monkeypatch, native_result,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    backend.act_result = ComputerActionResult(result=native_result)

    async def unavailable(*_args, **_kwargs):
        raise ComputerSessionError("overlay_blocked")

    monkeypatch.setattr(backend, "snapshot", unavailable)
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))
    assert result["code"] == "unknown_outcome"
    assert result["computer_receipt"]["dispatch_state"] == "unknown"
    assert result["computer_receipt"]["replay"] == "forbidden"
    assert sum(name == "act" for name, _value in backend.calls) == 1


def test_post_action_unrecognized_observation_code_is_not_disclosed(
    registry, manager, backend, monkeypatch,
):
    register(registry, manager)
    registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
    focus(registry)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])

    async def unavailable(*_args, **_kwargs):
        raise ComputerSessionError("PRIVATE-ERROR-CODE", "PRIVATE-ERROR-MESSAGE")

    monkeypatch.setattr(backend, "snapshot", unavailable)
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"],
        "actions": [{"type": "wait", "duration_ms": 0}],
    }))
    assert result["code"] == "post_action_observation_pending"
    assert "observation_error_code" not in result["details"]
    assert "PRIVATE-ERROR" not in json.dumps(result)
    assert sum(name == "act" for name, _value in backend.calls) == 1


def test_generic_foreground_schema_accepts_screenshot_coordinates():
    from agent.runtime.tools.computer import ACTION_SCHEMA
    assert not ToolRegistry._schema_errors(ACTION_SCHEMA, {"type": "click", "x": 20, "y": 30}, strict=True)


@pytest.mark.parametrize("yolo", [False, True])
def test_continuous_foreground_permission_survives_observation_without_focus_restore(
    registry, manager, backend, yolo,
):
    requests = []

    async def approve(request):
        requests.append(request)
        return "session"

    register(registry, manager)
    focus(registry)
    backend.plan_pid_action_classes = ()
    registry.set_approval_handler(approve)
    registry.yolo = yolo
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])

    for _ in range(2):
        result = run(registry.execute("computer_act", {
            "snapshot_id": snapshot["snapshot_id"],
            "interaction_mode": "foreground_takeover",
            "actions": [{"type": "click", "element_ref": f'{snapshot["snapshot_id"]}:1'}],
        }))
        assert result["error"] == ""
        # Model may rediscover the app and observe between action batches.
        run(registry.execute("computer_apps", {}))
        snapshot = json.loads(run(registry.execute("computer_get_app_state", {
            "app_ref": "app-1", "window_ref": "window-1",
        }))["fresh_output"])

    assert len(requests) == (0 if yolo else 1)
    if requests:
        assert requests[0]["choices"] == ["once", "session", "deny"]
    assert backend.restoration_requests == [False, False]
    assert sum(name == "takeover_begin" for name, _ in backend.calls) == 2
    assert sum(name == "takeover_end" for name, _ in backend.calls) == 2
    if yolo:
        assert not registry.approved_permission_scopes
        assert "observe-app-state" not in manager.grants
        registry.yolo = False
        denied_requests = []

        async def deny(request):
            denied_requests.append(request)
            return "deny"

        registry.set_approval_handler(deny)
        denied = run(registry.execute("computer_act", {
            "snapshot_id": snapshot["snapshot_id"], "interaction_mode": "foreground_takeover",
            "actions": [{"type": "click", "element_ref": f'{snapshot["snapshot_id"]}:1'}],
        }))
        assert denied["code"] == "approval_denied"
        assert len(denied_requests) == 1


def test_foreground_session_does_not_cover_export_or_survive_interruption(registry, manager, backend):
    requests = []

    async def approve(request):
        requests.append(request)
        return "session" if len(requests) == 1 else "deny"

    register(registry, manager)
    focus(registry)
    backend.plan_pid_action_classes = ()
    registry.set_approval_handler(approve)
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])

    def act(snapshot, suffix):
        return run(registry.execute("computer_act", {
            "snapshot_id": snapshot["snapshot_id"], "interaction_mode": "foreground_takeover",
            "actions": [{"type": "click", "element_ref": f'{snapshot["snapshot_id"]}:{suffix}'}],
        }))

    ordinary = act(snapshot, "1")
    assert ordinary["error"] == ""
    snapshot = json.loads(ordinary["fresh_output"])
    assert act(snapshot, "export")["code"] == "approval_denied"
    assert requests[-1]["choices"] == ["once", "deny"]
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    backend.act_result = ComputerActionResult(error=ComputerError(
        ComputerErrorCode.USER_ACTIVITY_PAUSED, "paused",
    ))
    assert act(snapshot, "1")["code"] == "user_activity_paused"
    assert len(requests) == 2  # session covers the ordinary call up to interruption
    assert not any(s.startswith("computer-foreground:") for s in registry.approved_permission_scopes)


@pytest.mark.parametrize("interaction_mode", ["background", "foreground_takeover"])
def test_cu_yolo_honors_explicit_policy_deny(registry, manager, backend, interaction_mode):
    register(registry, manager)
    focus(registry)
    backend.plan_pid_action_classes = ()
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    registry.yolo = True
    registry.policy.deny("computer_act")
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"], "interaction_mode": interaction_mode,
        "actions": [{"type": "click", "element_ref": f'{snapshot["snapshot_id"]}:1'}],
    }))
    assert result["code"] == "sandbox_denied"
    assert not any(name in {"act", "takeover_begin"} for name, _ in backend.calls)
    assert not registry.approved_permission_scopes


def test_foreground_once_cannot_be_reused_during_screenshot_publication(registry, manager, backend, monkeypatch):
    from agent.runtime.tools import computer
    register(registry, manager)
    focus(registry)
    backend.plan_pid_action_classes = ()
    initial = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    requests = []

    async def approve(request):
        requests.append(request)
        return "once" if len(requests) == 1 else "deny"

    registry.set_approval_handler(approve)
    original = computer._encode_screenshot_async

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def encode(png):
            entered.set()
            await release.wait()
            return await original(png)

        monkeypatch.setattr(computer, "_encode_screenshot_async", encode)

        def arguments(snapshot_id):
            return {"snapshot_id": snapshot_id, "interaction_mode": "foreground_takeover",
                    "actions": [{"type": "click", "element_ref": f"{snapshot_id}:1"}]}

        first = asyncio.create_task(registry.execute("computer_act", arguments(initial["snapshot_id"])))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not any(s.startswith("computer-foreground:") for s in registry.approved_permission_scopes)
            second = await registry.execute("computer_act", arguments(manager._latest_snapshot.snapshot_id))
            assert second["code"] == "approval_denied"
            assert len(requests) == 2
        finally:
            release.set()
            await first

    run(scenario())
    assert sum(name == "act" for name, _ in backend.calls) == 1


@pytest.mark.parametrize("observe", ["computer_get_app_state", "computer_snapshot", "computer_act"])
@pytest.mark.parametrize("cancel", [False, True])
def test_failed_observation_revokes_foreground_session(registry, manager, backend, monkeypatch, observe, cancel):
    from agent.runtime.tools import computer
    register(registry, manager)
    focus(registry)
    backend.plan_pid_action_classes = ()
    registry.set_approval_handler(lambda request: asyncio.sleep(0, result="session"))
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    result = run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"], "interaction_mode": "foreground_takeover",
        "actions": [{"type": "click", "element_ref": f'{snapshot["snapshot_id"]}:1'}],
    }))
    assert result["error"] == ""
    assert any(s.startswith("computer-foreground:") for s in registry.approved_permission_scopes)

    async def fail_encoding(png):
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("publication failed")

    monkeypatch.setattr(computer, "_encode_screenshot_async", fail_encoding)
    args = {"app_ref": "app-1", "window_ref": "window-1"} if observe == "computer_get_app_state" else {}
    if observe == "computer_act":
        fresh_id = json.loads(result["fresh_output"])["snapshot_id"]
        args = {"snapshot_id": fresh_id, "interaction_mode": "foreground_takeover",
                "actions": [{"type": "click", "element_ref": f"{fresh_id}:1"}]}
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            run(registry.execute(observe, args))
    else:
        assert run(registry.execute(observe, args))["error"]
    assert not any(s.startswith("computer-foreground:") for s in registry.approved_permission_scopes)


@pytest.mark.parametrize("cancel", [False, True])
def test_observation_approval_failure_revokes_existing_foreground_session(registry, manager, backend, cancel):
    register(registry, manager)
    focus(registry)
    backend.plan_pid_action_classes = ()
    registry.set_approval_handler(lambda request: asyncio.sleep(0, result="session"))
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    assert run(registry.execute("computer_act", {
        "snapshot_id": snapshot["snapshot_id"], "interaction_mode": "foreground_takeover",
        "actions": [{"type": "click", "element_ref": f'{snapshot["snapshot_id"]}:1'}],
    }))["error"] == ""
    assert any(s.startswith("computer-foreground:") for s in registry.approved_permission_scopes)

    async def reject(request):
        if cancel:
            raise asyncio.CancelledError
        return "deny"

    registry.set_approval_handler(reject)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            run(registry.execute("computer_snapshot", {"text_detail": "on"}))
    else:
        assert run(registry.execute("computer_snapshot", {"text_detail": "on"}))["code"] == "approval_denied"
    assert not any(s.startswith("computer-foreground:") for s in registry.approved_permission_scopes)


def test_unknown_foreground_action_revokes_session_even_after_catalog_recovery(registry, manager, backend):
    register(registry, manager)
    focus(registry)
    backend.plan_pid_action_classes = ()
    registry.set_approval_handler(lambda request: asyncio.sleep(0, result="session"))
    snapshot = json.loads(run(registry.execute("computer_snapshot", {}))["fresh_output"])
    args = {"snapshot_id": snapshot["snapshot_id"], "interaction_mode": "foreground_takeover",
            "actions": [{"type": "click", "element_ref": f'{snapshot["snapshot_id"]}:1'}]}
    backend.act_result = ComputerActionResult(error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown"))
    result = run(registry.execute("computer_act", args))
    assert result["code"] == "unknown_outcome"
    assert "window_transition" in result["details"]
    assert not any(s.startswith("computer-foreground:") for s in registry.approved_permission_scopes)


@pytest.mark.parametrize("duration_ms, accepted", [(0, True), (300, True), (10000, True), (-1, False), (0.5, False), (10001, False)])
def test_drag_duration_schema_matches_native_wire_contract(duration_ms, accepted):
    from agent.runtime.computer_protocol import ComputerAction
    from agent.runtime.tools.computer import ACTION_SCHEMA

    action = {"type": "drag", "x": 15, "y": 15, "end_x": 30, "end_y": 30, "duration_ms": duration_ms}
    assert (not ToolRegistry._schema_errors(ACTION_SCHEMA, action, strict=True)) is accepted
    if accepted:
        assert ComputerAction.from_mapping(action).to_mapping()["duration_ms"] == duration_ms
    else:
        with pytest.raises(ValueError):
            ComputerAction.from_mapping(action)


def test_recovery_observations_are_repeatable_but_bounded(registry, manager):
    register(registry, manager)
    focus(registry)
    for _ in range(4):
        result = run(registry.execute("computer_snapshot", {}))
        assert not result["error"]
    for name in ("computer_snapshot", "computer_get_app_state"):
        definition = registry.get(name)
        assert definition.repeat_guard is False
        assert definition.max_calls_per_turn == 32
    assert registry.get("computer_act").replay == "never"


def test_handoff_returns_exact_resume_recipe_after_catalog_refresh(registry, manager):
    register(registry, manager)
    focus(registry)
    handed_off = run(registry.execute("computer_handoff", {}))
    recipe = json.loads(handed_off["output"])["next_observation"]
    assert recipe["tool"] == "computer_resume"
    original = recipe["arguments"]
    run(registry.execute("computer_apps", {}))
    blocked = run(registry.execute("computer_snapshot", {}))
    assert blocked["code"] == "handoff_active"
    assert blocked["details"]["next_observation"]["arguments"] == original
    status = json.loads(run(registry.execute("computer_status", {}))["output"])
    assert status["next_observation"] == recipe
    resumed = run(registry.execute(recipe["tool"], original))
    assert not resumed["error"]
    assert resumed["verified"] is True


def test_react_can_refresh_snapshot_after_multiple_input_steps(registry, manager):
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.react import ReActAgent

    class LLM:
        calls = 0
        async def chat_stream(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls <= 4:
                yield {"type": "tool_calls", "calls": [{"id": f"observe-{self.calls}",
                       "name": "computer_snapshot", "arguments": "{}"}], "content": "", "usage": None}
            else:
                yield {"type": "done", "content": "observed", "usage": None}

    register(registry, manager)
    focus(registry)
    llm = LLM()
    agent = ReActAgent("fixture", llm, registry, max_iterations=6, timing_log_enabled=False,
                      query_profile_enabled=False)
    events = run(_collect_recovery_events(agent, Msg(content=[ContentBlock.text("Observe current computer state")])) )
    assert llm.calls == 5
    assert not any("repeated tool call" in event.get("message", "") for event in events)


async def _collect_recovery_events(agent, msg):
    return [event async for event in agent.reply_stream(msg)]


def test_resume_refreshes_catalog_internally_without_replacing_suspended_refs(registry, manager, backend):
    register(registry, manager)
    focus(registry)
    handed_off = run(registry.execute("computer_handoff", {}))
    recipe = json.loads(handed_off["output"])["next_observation"]
    backend.calls.clear()
    result = run(registry.execute(recipe["tool"], recipe["arguments"]))
    assert not result["error"]
    assert result["verified"] is True
    assert any(name == "apps" for name, _ in backend.calls)
    assert manager.handed_off is False


def test_resume_without_suspended_target_does_not_invent_recovery_refs(registry, manager, backend):
    register(registry, manager)
    result = run(registry.execute('computer_resume', {'app_ref': 'unused', 'window_ref': 'unused'}))
    assert result['code'] == 'no_suspended_target'
    assert result['retryable'] is False
    assert 'next_observation' not in result.get('details', {})
    assert not backend.calls
    assert manager.handed_off is False


def _targetless_handoff(registry, manager, backend):
    register(registry, manager)
    focus(registry)
    # An automatic catalog refresh revokes the ordinary binding before any stop.
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    assert manager.target is None and not manager.handed_off
    backend.calls.clear()
    handed_off = run(registry.execute("computer_handoff", {}))
    assert handed_off["error"] == ""
    return json.loads(handed_off["output"])


def test_handoff_without_bound_target_stops_without_selecting(registry, manager, backend):
    payload = _targetless_handoff(registry, manager, backend)
    assert payload["handoff_active"] is True
    assert payload["next_observation"] == {"tool": "computer_resume", "arguments": {}}
    assert "No window is bound" in payload["message"]
    assert not backend.calls
    assert manager.handed_off and manager.suspended_target is None and not manager.grants
    status = json.loads(run(registry.execute("computer_status", {}))["output"])
    assert status["handoff_active"] is True and status["target"] is None
    assert status["next_observation"] == payload["next_observation"]
    blocked = run(registry.execute("computer_get_app_state", {"app_ref": "app-1", "window_ref": "window-1"}))
    assert blocked["code"] == "handoff_active"
    assert blocked["details"]["next_observation"] == payload["next_observation"]


def test_targetless_resume_publishes_unbound_receipt_without_native_calls(registry, manager, backend):
    recipe = _targetless_handoff(registry, manager, backend)["next_observation"]
    resumed = run(registry.execute(recipe["tool"], recipe["arguments"]))
    assert resumed["error"] == "" and resumed["verified"] is True
    payload = json.loads(resumed.get("fresh_output") or resumed["output"])
    assert payload["status"] == "resumed_unbound"
    assert "computer_apps" in payload["recovery_hint"] and "computer_get_app_state" in payload["recovery_hint"]
    assert not backend.calls
    assert not manager.handed_off and manager.target is None and not manager.grants
    assert run(registry.execute("computer_snapshot", {}))["code"] == "target_required"
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    assert run(registry.execute(
        "computer_get_app_state", {"app_ref": "app-1", "window_ref": "window-1"},
    ))["error"] == ""


def test_targetless_release_rejects_pre_handoff_catalog_refs(registry, manager, backend):
    recipe = _targetless_handoff(registry, manager, backend)["next_observation"]
    assert run(registry.execute(recipe["tool"], recipe["arguments"]))["verified"] is True
    backend.calls.clear()
    stale = run(registry.execute("computer_get_app_state", {"app_ref": "app-1", "window_ref": "window-1"}))
    assert stale["code"] == "catalog_required"
    assert "computer_apps" in stale["recovery_hint"]
    assert not backend.calls


@pytest.mark.parametrize("arguments", [
    {"app_ref": "app-1", "window_ref": "window-1"}, {"app_ref": "app-1"}, {"window_ref": "window-1"},
])
def test_targetless_resume_rejects_refs_before_releasing(registry, manager, backend, arguments):
    _targetless_handoff(registry, manager, backend)
    rejected = run(registry.execute("computer_resume", arguments))
    assert rejected["code"] == "invalid_arguments"
    assert rejected["details"]["next_observation"]["arguments"] == {}
    assert manager.handed_off and not backend.calls


@pytest.mark.parametrize("arguments", [{}, {"app_ref": "app-1"}])
def test_bound_resume_still_requires_both_supplied_refs(registry, manager, backend, arguments):
    register(registry, manager)
    focus(registry)
    recipe = json.loads(run(registry.execute("computer_handoff", {}))["output"])["next_observation"]
    backend.calls.clear()
    rejected = run(registry.execute("computer_resume", arguments))
    assert rejected["code"] == "invalid_arguments"
    assert rejected["details"]["next_observation"] == recipe
    assert manager.handed_off and manager.suspended_target is not None and not backend.calls


@pytest.mark.parametrize("failure", ["postcondition", "commit", "cancelled"])
def test_targetless_resume_publication_failure_keeps_the_stop(registry, manager, backend, monkeypatch, failure):
    _targetless_handoff(registry, manager, backend)
    if failure == "postcondition":
        registry.get("computer_resume").postcondition = lambda args, result: (False, "publication refused")
    else:
        async def fail_commit(publication_id):
            if failure == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("commit refused")
        monkeypatch.setattr(manager, "commit_resume_publication", fail_commit)
    result = run(registry.execute("computer_resume", {}))
    assert result["code"] == "postcondition_failed"
    assert manager.handed_off and manager.suspended_target is None
    assert manager.target is None and not manager.grants


def test_local_runtime_targetless_handoff_resumes_unbound(registry, manager, monkeypatch, tmp_path):
    runtime = register_local(registry, manager, monkeypatch, tmp_path)
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    assert run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "window-1"}))["error"] == ""
    assert run(registry.execute("computer_apps", {}))["error"] == ""
    handed_off = json.loads(run(registry.execute("computer_handoff", {}))["output"])
    assert handed_off["next_observation"] == {"tool": "computer_resume", "arguments": {}}
    resumed = run(registry.execute("computer_resume", {}))
    assert resumed["error"] == "" and resumed["verified"] is True
    assert not runtime.handed_off and runtime.target is None


def test_local_runtime_cold_targetless_stop_and_release_never_start_helper(
    registry, manager, monkeypatch, tmp_path,
):
    runtime = register_local(registry, manager, monkeypatch, tmp_path)
    handed_off = json.loads(run(registry.execute("computer_handoff", {}))["output"])
    assert handed_off["next_observation"] == {"tool": "computer_resume", "arguments": {}}
    resumed = run(registry.execute("computer_resume", {}))
    assert resumed["error"] == "" and resumed["verified"] is True
    assert runtime.helper_started is False and not runtime.handed_off


def test_overlay_failure_is_not_reported_as_expired_refs():
    from agent.runtime.tools.computer import _app_state_identity_failure, _application_failure
    error = ComputerError(ComputerErrorCode.OVERLAY_BLOCKED, 'private native details')
    for convert in (_app_state_identity_failure, _application_failure):
        failure = convert(error)
        assert failure.code == 'overlay_blocked'
        assert failure.retryable is False
        assert failure.details == {'retry_requires': 'overlay_state_changed'}
        assert 'private native details' not in failure.message
        assert 'Stop repeating' in failure.recovery_hint


def test_unmatched_window_failure_does_not_invent_expiration_or_system_prohibition():
    from agent.runtime.tools.computer import _app_state_identity_failure, _application_failure

    error = ComputerError(ComputerErrorCode.AX_WINDOW_UNMATCHED, 'private native title')
    for convert in (_app_state_identity_failure, _application_failure):
        failure = convert(error)
        assert failure.code == 'ax_window_unmatched'
        assert failure.retryable is False
        assert failure.details == {'retry_requires': 'window_identity_or_state_changed'}
        assert 'private native title' not in failure.message
        assert 'window is present' in failure.message
        assert 'Stop repeating' in failure.recovery_hint
        assert 'obscured parent' in failure.recovery_hint
