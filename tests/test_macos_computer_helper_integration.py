"""macOS native-helper wire compatibility checks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime.computer_backend import ComputerArtifact, ComputerSessionManager, ComputerTarget
from agent.runtime.computer_protocol import (
    MAX_RESPONSE_BYTES,
    ComputerErrorCode,
    ComputerInteractionMode,
    ComputerRequest,
    ComputerResponse,
    ComputerSnapshot,
    ComputerSnapshotTextDetailMode,
    ForegroundFragmentActionRequirement,
    ForegroundFragmentDeclaration,
    ForegroundFragmentPlanRequest,
    ForegroundFragmentStageDeclaration,
    FragmentStageAuthority,
    FragmentStageCommit,
    TakeoverRestoration,
    decode_response,
    decode_text_detail_envelope,
)


class RecordingFragmentTransport:
    def __init__(self):
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        from agent.runtime.computer_protocol import ComputerResponse

        if request.operation == "plan_actions":
            return ComputerResponse(
                request.request_id,
                True,
                result={
                    "plan_ref": "plan-0",
                    "interaction_mode": "foreground_takeover",
                    "requires_takeover": True,
                    "reason": "foreground_takeover_required",
                    "action_classes": ["click"],
                    "pid_action_classes": ["click"],
                    "last_acknowledged_action": -1,
                    "fragment_hash": "a" * 64,
                    "stage_index": 0,
                    "stage_hash": "0" * 64,
                },
            )
        if request.operation == "takeover_begin":
            return ComputerResponse(
                request.request_id,
                True,
                result={
                    "takeover_ref": "takeover-0",
                    "fragment_hash": "a" * 64,
                    "stage_index": 0,
                    "stage_hash": "0" * 64,
                },
            )
        if request.operation == "fragment_stage_commit":
            return ComputerResponse(request.request_id, True, result={
                "fragment_hash": "a" * 64,
                "stage_index": 0,
                "stage_hash": "0" * 64,
                "terminal": True,
                "restoration": "restored",
            })
        if request.operation == "act":
            return ComputerResponse(request.request_id, True, result={
                "outcomes": [{"index": 0, "ok": True}],
                "last_acknowledged_action": 0,
                "fragment_hash": "a" * 64,
                "stage_index": 0,
                "stage_hash": "0" * 64,
            })
        raise AssertionError(request.operation)


def test_macos_backend_carries_low_level_fragment_plan_and_commit_authority():
    transport = RecordingFragmentTransport()
    backend = MacComputerBackend(transport)
    requirement = ForegroundFragmentActionRequirement("pid_pointer", "click", "pointer:click")
    declaration = ForegroundFragmentDeclaration(
        "a" * 64,
        (ForegroundFragmentStageDeclaration("0" * 64, 1, (requirement,)),),
        1,
        1_000,
        True,
    )

    async def scenario():
        plan = await backend.plan_actions(
            ComputerTarget("app", "window"),
            "snapshot-0",
            [{"type": "click", "element_ref": "button"}],
            ComputerInteractionMode.FOREGROUND_TAKEOVER,
            fragment=ForegroundFragmentPlanRequest.stage(
                FragmentStageAuthority("a" * 64, 0, "0" * 64, "snapshot-0")
            ),
        )
        takeover_ref = await backend.begin_takeover(
            "snapshot-0", plan.plan_ref, declaration=declaration
        )
        await backend.act(
            ComputerTarget("app", "window"),
            "snapshot-0",
            [{"type": "click", "element_ref": "button"}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            plan_ref=plan.plan_ref,
            takeover_ref=takeover_ref,
            fragment_stage=FragmentStageAuthority("a" * 64, 0, "0" * 64, "snapshot-0"),
        )
        terminal = await backend.commit_fragment_stage(FragmentStageCommit(
            takeover_ref=takeover_ref,
            fragment_hash="a" * 64,
            stage_index=0,
            stage_hash="0" * 64,
            plan_ref=plan.plan_ref,
            fresh_snapshot_id="snapshot-1",
            postcondition_verified=True,
        ))
        return terminal

    result = run(scenario())
    assert result.terminal is True
    assert result.restoration is TakeoverRestoration.RESTORED
    assert [request.operation for request in transport.requests] == [
        "plan_actions", "takeover_begin", "act", "fragment_stage_commit",
    ]
    assert transport.requests[0].payload["fragment"].kind == "initial"
from agent.runtime.macos_computer import HelperTransport, HelperTransportError, MacComputerBackend
from agent.runtime.tools.computer import register_computer_tools
from agent.runtime.tools.registry import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER = PROJECT_ROOT / ".astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper"
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_macos_computer_helper.sh"
SYMBOL_GATE = PROJECT_ROOT / "scripts" / "check_macos_computer_helper_symbols.py"
SNAPSHOT_FILENAME_VECTORS = json.loads((
    PROJECT_ROOT
    / "native/macos-computer-helper/Tests/Fixtures/snapshot_artifact_filename_vectors.json"
).read_text(encoding="utf-8"))


def build_fixture() -> Path:
    subprocess.run(
        ["swift", "build", "--package-path", "native/macos-computer-helper", "--product", "AstraComputerProtocolFixture"],
        cwd=PROJECT_ROOT,
        check=True,
    )
    binary_directory = subprocess.run(
        ["swift", "build", "--package-path", "native/macos-computer-helper", "--show-bin-path"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return (Path(binary_directory) / "AstraComputerProtocolFixture").resolve(strict=True)


@pytest.fixture(scope="module")
def protocol_fixture() -> Path:
    """Build the Swift protocol fixture once; each test still starts its own fixture process."""
    return build_fixture()


def build_native_harness() -> Path:
    environment = {**os.environ, "ASTRA_MACOS_COMPUTER_HELPER_HARNESS": "1"}
    subprocess.run(
        [
            "swift", "build", "--package-path", "native/macos-computer-helper",
            "--product", "AstraMacComputerHelperHarness",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
    )
    binary_directory = subprocess.run(
        ["swift", "build", "--package-path", "native/macos-computer-helper", "--show-bin-path"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return (Path(binary_directory) / "AstraMacComputerHelperHarness").resolve(strict=True)


def run(awaitable):
    return asyncio.run(awaitable)


def run_symbol_gate(symbols):
    return subprocess.run(
        [sys.executable, SYMBOL_GATE],
        input="\n".join(symbols) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )


def encoded_filename_vector(vector):
    if "json_string" in vector:
        return vector["json_string"]
    if "repeat" in vector:
        value = vector["repeat"] * vector["count"] + vector.get("suffix", "")
    else:
        value = vector["name"]
    return json.dumps(value, ensure_ascii=True)


class OldSnapshotTransport:
    def __init__(self):
        self.request_seen = None

    async def configure_artifact_directory(self, _directory_fd, _directory_path=None):
        return None

    async def request(self, request):
        self.request_seen = request
        return ComputerResponse(
            request.request_id,
            True,
            snapshot=ComputerSnapshot(
                "snapshot_old",
                {
                    "image_artifact": request.payload["artifact_name"],
                    "logical_size": {"width": 1, "height": 1},
                    "pixel_size": {"width": 1, "height": 1},
                    "backing_scale": 1,
                    "capture_bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "ax_tree": {},
                },
            ),
        )


def test_macos_backend_text_detail_on_fails_closed_for_an_old_off_response(tmp_path):
    transport = OldSnapshotTransport()
    backend = MacComputerBackend(transport)
    # The transport is mocked; this test compares descriptor identities only.
    directory_fd = 101

    async def scenario():
        with pytest.raises(HelperTransportError, match="text detail"):
            await backend.snapshot(
                ComputerTarget("app", "window"),
                "target_window",
                ComputerArtifact(
                    "snapshot-0123456789abcdef0123456789abcdef.png",
                    directory_fd,
                    tmp_path,
                ),
                text_detail=ComputerSnapshotTextDetailMode.ON,
                text_detail_artifact=ComputerArtifact(
                    "snapshot-0123456789abcdef0123456789abcdef.ax.json",
                    directory_fd,
                    tmp_path,
                ),
            )

    run(scenario())
    assert transport.request_seen.payload["text_detail"] == "on"


def test_macos_backend_rejects_text_detail_from_a_different_held_directory(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_fd, second_fd = 101, 102
    backend = MacComputerBackend(OldSnapshotTransport())
    with pytest.raises(ValueError, match="share one held directory"):
        run(backend.snapshot(
            ComputerTarget("app-1", "window-1"),
            "target_window",
            ComputerArtifact(
                "snapshot-0123456789abcdef0123456789abcdef.png",
                first_fd,
                first,
            ),
            text_detail=ComputerSnapshotTextDetailMode.ON,
            text_detail_artifact=ComputerArtifact(
                "snapshot-0123456789abcdef0123456789abcdef.ax.json",
                second_fd,
                second,
            ),
        ))

def test_symbol_gate_accepts_both_required_symbols():
    result = run_symbol_gate([
        "                 U _CGEventPostToPid",
        "_CGEventTapCreate",
    ])

    assert result.returncode == 0


@pytest.mark.parametrize("missing", ["_CGEventPostToPid", "_CGEventTapCreate"])
def test_symbol_gate_rejects_each_missing_required_symbol(missing):
    required = {"_CGEventPostToPid", "_CGEventTapCreate"}
    result = run_symbol_gate(sorted(required - {missing}))

    assert result.returncode != 0
    assert missing in result.stderr


@pytest.mark.parametrize("forbidden", ["_CGWarpMouseCursorPosition"])
def test_symbol_gate_rejects_each_forbidden_symbol(forbidden):
    result = run_symbol_gate([
        "_CGEventPostToPid",
        "_CGEventTapCreate",
        forbidden,
    ])

    assert result.returncode != 0
    assert forbidden in result.stderr


@pytest.mark.parametrize(
    ("similar", "missing"),
    [
        ("_CGEventPostToPidSuffix", "_CGEventPostToPid"),
        ("_CGEventTapCreateSuffix", "_CGEventTapCreate"),
    ],
)
def test_similarly_prefixed_symbol_does_not_satisfy_required_exact_name(similar, missing):
    other = "_CGEventTapCreate" if missing == "_CGEventPostToPid" else "_CGEventPostToPid"
    result = run_symbol_gate([other, similar])

    assert result.returncode != 0
    assert missing in result.stderr


@pytest.mark.parametrize(
    "similar",
    ["_CGWarpMouseCursorPositionSafe", "_CGEventPostSuffix"],
)
def test_similarly_prefixed_symbol_does_not_trigger_forbidden_exact_name(similar):
    result = run_symbol_gate([
        "_CGEventPostToPid",
        "_CGEventTapCreate",
        similar,
    ])

    assert result.returncode == 0


def test_build_routes_captured_nm_output_through_behavioral_symbol_gate():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'helper_symbols="$(nm -u ' in script
    assert 'check_macos_computer_helper_symbols.py' in script
    assert '<<<"$helper_symbols"' in script


@pytest.fixture(scope="module")
def isolated_signed_helper(tmp_path_factory):
    """Build once into private test storage; leave the deployed bundle untouched."""
    def deployment_fingerprint():
        bundle = PROJECT_ROOT / ".astra/bin/AstraMacComputerHelper.app"
        return {str(path.relative_to(bundle)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in bundle.rglob("*") if path.is_file()}

    before = deployment_fingerprint()
    output = tmp_path_factory.mktemp("signed-helper") / "isolated bundle"
    subprocess.run(["bash", str(BUILD_SCRIPT), "--output-root", str(output)],
                   cwd=PROJECT_ROOT, check=True, capture_output=True, text=True)
    assert deployment_fingerprint() == before
    helper = output / "AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper"
    yield helper
    assert deployment_fingerprint() == before


async def request_status_with_owned_transport(helper=HELPER):
    transport = HelperTransport(helper)
    try:
        return await transport.request(ComputerRequest("transport-status", "status", {}))
    finally:
        await transport.close()


async def request_recognized_errors_with_owned_transport(helper=HELPER):
    transport = HelperTransport(helper)
    try:
        wrong_version = ComputerRequest("transport-version", "status", {})
        object.__setattr__(wrong_version, "protocol_version", 1)
        version_response = await transport.request(wrong_version)
        unsupported = ComputerRequest("transport-operation", "status", {})
        object.__setattr__(unsupported, "operation", "not-supported")
        operation_response = await transport.request(unsupported)
        return version_response, operation_response
    finally:
        await transport.close()


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_signed_native_helper_has_strict_frames_and_python_transport_compatibility(isolated_signed_helper):
    helper = isolated_signed_helper

    response = run(request_status_with_owned_transport(helper))
    assert response.ok is True
    assert isinstance(response.result["permissions"]["accessibility"], bool)
    assert isinstance(response.result["permissions"]["screen_recording"], bool)

    wrong_version, unsupported_operation = run(request_recognized_errors_with_owned_transport(helper))
    assert wrong_version.request_id == "transport-version"
    assert wrong_version.ok is False
    assert wrong_version.error.code.value == "protocol_mismatch"
    assert unsupported_operation.request_id == "transport-operation"
    assert unsupported_operation.ok is False
    assert unsupported_operation.error.code.value == "protocol_mismatch"

    invalid = [
        '{"protocol_version":1.0,"request_id":"version","operation":"ping","payload":{}}',
        '{"protocol_version":1,"request_id":"missing","operation":"ping"}',
        '{"protocol_version":1,"request_id":"array","operation":"ping","payload":[]}',
        '{"protocol_version":1,"request_id":"unknown","operation":"ping","payload":{},"extra":true}',
        '{"protocol_version":1,"request_id":"one","request_id":"two","operation":"ping","payload":{}}',
        '{"protocol_version":1,"request_id":"' + ("x" * 257) + '","operation":"ping","payload":{}}',
    ]
    process = subprocess.run(
        [helper],
        input="\n".join(invalid) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert process.stderr == ""
    lines = process.stdout.splitlines()
    assert all(len(line.encode("utf-8")) <= MAX_RESPONSE_BYTES for line in lines)
    responses = [decode_response(line) for line in lines]
    assert len(responses) == len(invalid)
    assert all(response.ok is False for response in responses)


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_signed_production_helper_contains_no_test_backdoor(isolated_signed_helper):
    strings = subprocess.run(["/usr/bin/strings", isolated_signed_helper], text=True, capture_output=True, check=True).stdout
    forbidden = [
        "ASTRA_COMPUTER_HELPER_TEST_MODE",
        "__test_",
        "app_test",
        "Astra test application",
        "fixture_app",
        "Astra computer fixture",
    ]
    assert all(marker not in strings for marker in forbidden)

    process = subprocess.run(
        [isolated_signed_helper],
        input='{"protocol_version":1,"request_id":"hidden","operation":"__test_ax_caps","payload":{}}\n',
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "ASTRA_COMPUTER_HELPER_TEST_MODE": "1"},
    )
    response = decode_response(process.stdout.strip())
    assert response.ok is False
    assert response.error.code.value == "protocol_mismatch"


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_real_transport_returns_snapshot_schema_through_held_directory_fd(tmp_path, protocol_fixture):
    fixture = protocol_fixture
    cache_root = tmp_path / "cache"
    cache_root.mkdir(mode=0o700)
    directory_fd = os.open(cache_root, os.O_RDONLY)
    moved_root = tmp_path / "moved-cache"
    outside = tmp_path / "outside"
    outside.mkdir()
    cache_root.rename(moved_root)
    cache_root.symlink_to(outside, target_is_directory=True)

    async def scenario():
        transport = HelperTransport(fixture)
        backend = MacComputerBackend(transport)
        try:
            await backend.bind_artifact_directory(directory_fd)
            catalog = await backend.apps()
            target = await backend.select(catalog.apps[0]["app_ref"], catalog.apps[0]["windows"][0]["window_ref"])
            return await backend.snapshot(
                target,
                "target_window",
                ComputerArtifact("snapshot-swapped.png", directory_fd),
            )
        finally:
            await transport.close()

    try:
        snapshot = run(scenario())
    finally:
        os.close(directory_fd)

    capture = moved_root / "snapshot-swapped.png"
    assert snapshot.payload["image_artifact"] == "snapshot-swapped.png"
    assert capture.is_file()
    assert capture.stat().st_mode & 0o777 == 0o600
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_real_transport_keeps_registry_across_apps_select_snapshot(tmp_path, protocol_fixture):
    fixture = protocol_fixture

    async def scenario():
        transport = HelperTransport(fixture)
        manager = ComputerSessionManager(MacComputerBackend(transport), cache_root=tmp_path / "cache")
        try:
            apps = await manager.apps()
            target = await manager.select(apps[0]["app_ref"], apps[0]["windows"][0]["window_ref"])
            snapshot = await manager.snapshot()
            return target, snapshot
        finally:
            await manager.close()

    target, snapshot = run(scenario())
    assert target == ComputerTarget("fixture_app", "fixture_window")
    assert snapshot.payload["image_artifact"].startswith("snapshot-")


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_snapshot_off_emits_v4_default_button_shape(tmp_path, protocol_fixture):
    fixture = protocol_fixture
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    directory_fd = os.open(artifact_root, os.O_RDONLY)

    async def scenario():
        transport = HelperTransport(fixture)
        try:
            await transport.configure_artifact_directory(directory_fd)
            await transport.request(ComputerRequest("apps", "apps", {}))
            await transport.request(ComputerRequest(
                "select", "select", {"app_ref": "fixture_app", "window_ref": "fixture_window"},
            ))
            return await transport.request(ComputerRequest("snapshot-off", "snapshot", {
                "app_ref": "fixture_app",
                "window_ref": "fixture_window",
                "scope": "target_window",
                "artifact_name": "snapshot-0123456789abcdef0123456789abcdef.png",
            }))
        finally:
            await transport.close()

    try:
        response = run(scenario())
    finally:
        os.close(directory_fd)

    assert response.ok is True
    assert response.snapshot is not None
    assert set(response.snapshot.payload) == {
        "image_artifact", "logical_size", "pixel_size", "backing_scale",
        "capture_bounds", "ax_tree", "has_default_button",
    }


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_snapshot_on_round_trips_paired_schema_one_metadata(tmp_path, protocol_fixture):
    fixture = protocol_fixture
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    directory_fd = os.open(artifact_root, os.O_RDONLY)
    detail_name = "snapshot-0123456789abcdef0123456789abcdef.ax.json"

    async def scenario():
        transport = HelperTransport(fixture)
        try:
            await transport.configure_artifact_directory(directory_fd)
            await transport.request(ComputerRequest("apps", "apps", {}))
            await transport.request(ComputerRequest(
                "select", "select", {"app_ref": "fixture_app", "window_ref": "fixture_window"},
            ))
            return await transport.request(ComputerRequest("snapshot-on", "snapshot", {
                "app_ref": "fixture_app",
                "window_ref": "fixture_window",
                "scope": "target_window",
                "artifact_name": "snapshot-0123456789abcdef0123456789abcdef.png",
                "text_detail": "on",
                "text_detail_artifact_name": detail_name,
            }))
        finally:
            await transport.close()

    try:
        response = run(scenario())
    finally:
        os.close(directory_fd)

    assert response.ok is True
    assert response.snapshot is not None
    assert response.snapshot.payload["text_detail_artifact"] == detail_name
    metadata = response.snapshot.payload["text_detail_metadata"]
    assert metadata["schema_version"] == 1
    assert metadata["snapshot_id"] == response.snapshot.snapshot_id
    assert metadata["coverage"] == "reported_ax_subtree"


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_swift_text_detail_identity_vector_is_accepted_by_python_decoder():
    harness = build_native_harness()
    completed = subprocess.run(
        [harness, "--emit-ax-text-detail-conformance"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    vector = json.loads(completed.stdout)
    data = base64.b64decode(vector["artifact_base64"], validate=True)
    digest = hashlib.sha256(data).hexdigest()
    swift_envelope = json.loads(data)
    stats = swift_envelope["stats"]
    metadata = {
        "schema_version": 1,
        "snapshot_id": swift_envelope["snapshot_id"],
        "coverage": "reported_ax_subtree",
        "node_count": stats["node_count"],
        "max_depth_observed": stats["max_depth_observed"],
        "byte_count": len(data),
        "sha256": digest,
        "truncated": stats["truncated"],
        "truncation_reasons": stats["truncation_reasons"],
    }

    decoded = decode_text_detail_envelope(
        data,
        snapshot_id=swift_envelope["snapshot_id"],
        metadata=metadata,
        sha256=digest,
    )

    assert vector["sensitive_accessor_count"] == 0
    root = decoded["root"]
    assert (root["role"], root["subrole"], root["redacted"]) == (
        "AXUnknown", "AXStandardWindow", True,
    )
    first, second, third = root["children"]
    assert first["role"] == "AXTextField" and "subrole" not in first
    assert (second["role"], second["subrole"]) == ("AXUnknown", "AXStandardContent")
    assert (third["role"], third["subrole"]) == ("AXTextField", "AXUnknown")
    assert all(node["redacted"] is True and "value" not in node for node in [root, first, second, third])


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_rejects_duplicate_keys_and_noninteger_v3(protocol_fixture):
    fixture = protocol_fixture
    requests = """{"protocol_version":4,"request_id":"first","request_id":"duplicate","operation":"ping","payload":{}}
{"protocol_version":4.0,"request_id":"float","operation":"ping","payload":{}}
{"protocol_version":4,"request_id":"close","operation":"close","payload":{}}
"""

    process = subprocess.run(
        [fixture],
        input=requests,
        text=True,
        capture_output=True,
        check=True,
    )
    responses = [decode_response(line) for line in process.stdout.splitlines()]

    assert responses[0].ok is False
    assert responses[0].error.code is ComputerErrorCode.PROTOCOL_MISMATCH
    assert responses[1].ok is False
    assert responses[1].error.code is ComputerErrorCode.PROTOCOL_MISMATCH


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_apps_returns_generation_bound_catalog(protocol_fixture):
    fixture = protocol_fixture
    request = json.dumps({
        "protocol_version": 4,
        "request_id": "apps",
        "operation": "apps",
        "payload": {},
    })
    repeat_request = json.dumps({
        "protocol_version": 4,
        "request_id": "apps-repeat",
        "operation": "apps",
        "payload": {},
    })

    process = subprocess.run(
        [fixture],
        input=request + "\n" + repeat_request + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]

    assert len(responses) == 2
    windows = []
    for response in responses:
        assert set(response["result"]) == {"catalog_generation", "apps"}
        assert response["result"]["catalog_generation"] == 7
        windows.append(response["result"]["apps"][0]["windows"][0])

    for window in windows:
        assert window["bindable"] is True
        assert window["binding_status"] == "ready"
        assert window["window_identity_ref"] == "fixture-window-identity-v1"
        assert 0 < len(window["window_identity_ref"].encode("utf-8")) <= 256
    assert windows[0]["window_identity_ref"] == windows[1]["window_identity_ref"]


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_get_app_state_matches_the_catalog_bound_wire_contract(protocol_fixture):
    fixture = protocol_fixture
    off = {
        "app_ref": "app_a",
        "window_ref": "win_a",
        "catalog_generation": 7,
        "scope": "target_window",
        "artifact_name": "snapshot-0123456789abcdef0123456789abcdef.png",
    }
    on = {
        **off,
        "text_detail": "on",
        "text_detail_artifact_name": "snapshot-0123456789abcdef0123456789abcdef.ax.json",
    }
    display = {**off, "scope": "display"}
    invalid = [
        {key: value for key, value in off.items() if key != "app_ref"},
        {key: value for key, value in off.items() if key != "window_ref"},
        {key: value for key, value in off.items() if key != "catalog_generation"},
        {key: value for key, value in off.items() if key != "scope"},
        {key: value for key, value in off.items() if key != "artifact_name"},
        {**off, "catalog_generation": 0},
        {**off, "catalog_generation": -1},
        {**off, "catalog_generation": 7.0},
        {**off, "catalog_generation": True},
        {**off, "unexpected": True},
        {**on, "text_detail": "off"},
        {**on, "text_detail_artifact_name": "snapshot-other.ax.json"},
        {**off, "artifact_name": "../snapshot-token.png"},
        {**on, "text_detail_artifact_name": "../snapshot-token.ax.json"},
    ]
    requests = [json.dumps({"protocol_version": 4, "request_id": "apps", "operation": "apps", "payload": {}})]
    requests.extend(
        json.dumps({"protocol_version": 4, "request_id": request_id, "operation": "get_app_state", "payload": payload})
        for request_id, payload in [("off", off), ("on", on), ("display", display)]
    )
    requests.extend(
        json.dumps({"protocol_version": 4, "request_id": f"invalid-{index}", "operation": "get_app_state", "payload": payload})
        for index, payload in enumerate(invalid)
    )
    requests.append(
        '{"protocol_version":4,"request_id":"duplicate","operation":"get_app_state",'
        '"payload":{"app_ref":"app_a","app_ref":"other","window_ref":"win_a",'
        '"catalog_generation":7,"scope":"target_window","artifact_name":"snapshot-token.png"}}'
    )

    process = subprocess.run(
        [fixture], input="\n".join(requests) + "\n", text=True, capture_output=True, check=True,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]

    for response in responses[1:4]:
        assert response["ok"] is True
        assert response["result"] == {
            "app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7,
            "interaction_mode": "background",
        }
        decoded = decode_response(json.dumps(response))
        assert decoded.snapshot is not None
        assert decoded.snapshot.snapshot_id.startswith("snapshot_fixture_")
        assert decoded.snapshot.payload["has_default_button"] is True
        assert decoded.snapshot.payload["default_button_element_ref"].endswith(":default")
    assert all(response["ok"] is False for response in responses[4:])


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_matches_shared_artifact_filename_vectors(tmp_path, protocol_fixture):
    fixture = protocol_fixture
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    directory_fd = os.open(artifact_root, os.O_RDONLY)
    requests = [
        '{"protocol_version":4,"request_id":"apps","operation":"apps","payload":{}}',
        (
            '{"protocol_version":4,"request_id":"select","operation":"select",'
            '"payload":{"app_ref":"fixture_app","window_ref":"fixture_window"}}'
        ),
    ]
    expected = []
    for index, vector in enumerate(SNAPSHOT_FILENAME_VECTORS["plain_names"]):
        requests.append(json.dumps({
            "protocol_version": 4,
            "request_id": f"plain-{index}",
            "operation": "snapshot",
            "payload": {
                "app_ref": "fixture_app",
                "window_ref": "fixture_window",
                "scope": "target_window",
                "artifact_name": json.loads(encoded_filename_vector(vector)),
            },
        }, separators=(",", ":")))
        expected.append(vector["valid"])
    for index, vector in enumerate(SNAPSHOT_FILENAME_VECTORS["detail_pairs"]):
        requests.append(json.dumps({
            "protocol_version": 4,
            "request_id": f"detail-{index}",
            "operation": "snapshot",
            "payload": {
                "app_ref": "fixture_app",
                "window_ref": "fixture_window",
                "scope": "target_window",
                "artifact_name": vector["image"],
                "text_detail": "on",
                "text_detail_artifact_name": vector["detail"],
            },
        }, separators=(",", ":")))
        expected.append(vector["valid"])
    requests.append(
        '{"protocol_version":4,"request_id":"close","operation":"close","payload":{}}'
    )

    try:
        process = subprocess.run(
            [fixture],
            input="\n".join(requests) + "\n",
            text=True,
            capture_output=True,
            check=True,
            env={**os.environ, "ASTRA_COMPUTER_ARTIFACT_DIR_FD": str(directory_fd)},
            pass_fds=(directory_fd,),
        )
    finally:
        os.close(directory_fd)

    responses = [json.loads(line) for line in process.stdout.splitlines()][2:-1]
    assert [response["ok"] for response in responses] == expected


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_rejects_oversized_frames_and_unbounded_headers_before_dispatch(protocol_fixture):
    fixture = protocol_fixture
    oversized_padding = "x" * (4 * 1024 * 1024)
    requests = [
        json.dumps({
            "protocol_version": 4,
            "request_id": "x" * 300,
            "operation": "ping",
            "payload": {},
        }, separators=(",", ":")),
        '{"protocol_version":4,"request_id":"","operation":"ping","payload":{}}',
        '{"protocol_version":4,"request_id":"op-empty","operation":"","payload":{}}',
        json.dumps({
            "protocol_version": 4,
            "request_id": "op-long",
            "operation": "x" * 65,
            "payload": {},
        }, separators=(",", ":")),
        json.dumps({
            "protocol_version": 4,
            "request_id": "oversized",
            "operation": "ping",
            "payload": {"padding": oversized_padding},
        }, separators=(",", ":")),
        '{"protocol_version":4,"request_id":"close","operation":"close","payload":{}}',
    ]

    process = subprocess.run(
        [fixture],
        input="\n".join(requests) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]

    assert [response["ok"] for response in responses[:5]] == [False] * 5
    assert [response["request_id"] for response in responses[:5]] == ["invalid"] * 5


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_swift_fixture_reaches_public_snapshot_attachment_with_strict_bounds(tmp_path, protocol_fixture):
    fixture = protocol_fixture

    async def scenario():
        transport = HelperTransport(fixture)
        manager = ComputerSessionManager(
            MacComputerBackend(transport),
            cache_root=tmp_path / "cache",
        )
        registry = ToolRegistry()
        register_computer_tools(registry, manager)
        try:
            apps = json.loads((await registry.execute("computer_apps", {}))["output"])
            application = apps["apps"][0]
            window = application["windows"][0]
            focused = await registry.execute("computer_focus", {
                "app_ref": application["app_ref"],
                "window_ref": window["window_ref"],
            })
            assert focused["error"] == ""
            return await registry.execute("computer_snapshot", {"scope": "target_window"})
        finally:
            await manager.close()

    result = run(scenario())
    payload = json.loads(result["fresh_output"])
    bounds = payload["capture_bounds"]
    assert result["verified"] is True
    assert payload["type"] == "image_attachment"
    assert bounds["width"] > 0 and bounds["height"] > 0
    assert result["_private_result"]["image_data_urls"][0].startswith("data:image/png;base64,")


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_swift_fixture_display_metadata_reaches_public_snapshot_attachment(tmp_path, protocol_fixture):
    fixture = protocol_fixture

    async def scenario():
        transport = HelperTransport(fixture)
        manager = ComputerSessionManager(
            MacComputerBackend(transport),
            cache_root=tmp_path / "cache",
        )
        registry = ToolRegistry()
        registry.set_approval_handler(lambda _request: asyncio.sleep(0, result="once"))
        register_computer_tools(registry, manager)
        try:
            apps = json.loads((await registry.execute("computer_apps", {}))["output"])
            application = apps["apps"][0]
            window = application["windows"][0]
            focused = await registry.execute("computer_focus", {
                "app_ref": application["app_ref"],
                "window_ref": window["window_ref"],
            })
            assert focused["error"] == ""
            return await registry.execute("computer_snapshot", {"scope": "display"})
        finally:
            await manager.close()

    result = run(scenario())
    payload = json.loads(result["fresh_output"])
    assert result["verified"] is True
    assert payload["scope"] == "display"
    assert payload["display_id"] == 7
    assert payload["target_window_bounds"] == {
        "x": 0,
        "y": 0,
        "width": 1,
        "height": 1,
    }


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_plan_response_includes_exact_pid_action_classes(protocol_fixture):
    fixture = protocol_fixture

    async def scenario():
        transport = HelperTransport(fixture)
        try:
            await transport.request(ComputerRequest("apps", "apps", {}))
            await transport.request(ComputerRequest(
                "select",
                "select",
                {"app_ref": "fixture_app", "window_ref": "fixture_window"},
            ))
            return await transport.request(ComputerRequest(
                "plan",
                "plan_actions",
                {
                    "interaction_mode": "background",
                    "snapshot_id": "fixture_snapshot",
                    "actions": [{"type": "click", "element_ref": "ordinary"}],
                },
            ))
        finally:
            await transport.close()

    response = run(scenario())
    assert response.ok is True
    assert response.result["pid_action_classes"] == []


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
@pytest.mark.parametrize(
    ("fixture_case", "expected_result"),
    [
        ("malformed_outcomes_string", {"outcomes": "bad", "last_acknowledged_action": 0}),
        ("malformed_ack_out_of_range", {"outcomes": [], "last_acknowledged_action": 999}),
        ("malformed_extra_field", {"outcomes": [], "last_acknowledged_action": -1, "extra": True}),
        (
            "malformed_noncontiguous",
            {"outcomes": [{"index": 1, "ok": False, "error_code": "helper_failed"}], "last_acknowledged_action": -1},
        ),
        (
            "malformed_success_ack",
            {"outcomes": [{"index": 0, "ok": True}], "last_acknowledged_action": -1},
        ),
        (
            "malformed_unknown_ack",
            {"outcomes": [{"index": 0, "ok": False, "error_code": "unknown_outcome"}], "last_acknowledged_action": 0},
        ),
        ("malicious_unknown_missing_outcome", {"outcomes": [], "last_acknowledged_action": -1}),
    ],
)
def test_independent_protocol_fixture_malicious_action_results_fail_closed(fixture_case, expected_result, protocol_fixture):
    fixture = protocol_fixture
    action = {"type": "click", "element_ref": fixture_case}

    async def scenario():
        transport = HelperTransport(fixture)
        backend = MacComputerBackend(transport)
        try:
            await backend.apps()
            target = await backend.select("fixture_app", "fixture_window")
            plan = await backend.plan_actions(
                target,
                "fixture_snapshot",
                [action],
                ComputerInteractionMode.BACKGROUND,
            )
            payload = {
                "interaction_mode": "background",
                "snapshot_id": "fixture_snapshot",
                "plan_ref": plan.plan_ref,
                "actions": [action],
            }
            raw = await transport.request(ComputerRequest(f"raw-{fixture_case}", "act", payload))
            target = await backend.select("fixture_app", "fixture_window")
            plan = await backend.plan_actions(
                target,
                "fixture_snapshot",
                [action],
                ComputerInteractionMode.BACKGROUND,
            )
            result = await backend.act(
                target,
                "fixture_snapshot",
                [action],
                interaction_mode=ComputerInteractionMode.BACKGROUND,
                plan_ref=plan.plan_ref,
            )
            return raw, result
        finally:
            await transport.close()

    raw, result = run(scenario())
    assert raw.result == expected_result
    assert result.error.code is ComputerErrorCode.UNKNOWN_OUTCOME


@pytest.mark.skipif(platform.system() != "Darwin", reason="native helper is macOS-only")
def test_protocol_fixture_consumes_cooperative_plan_exactly_once(protocol_fixture):
    fixture = protocol_fixture
    action = {"type": "click", "element_ref": "ordinary"}

    async def scenario():
        transport = HelperTransport(fixture)
        backend = MacComputerBackend(transport)
        try:
            await backend.apps()
            target = await backend.select("fixture_app", "fixture_window")
            plan = await backend.plan_actions(
                target,
                "fixture_snapshot",
                [action],
                ComputerInteractionMode.BACKGROUND,
            )
            first = await backend.act(
                target,
                "fixture_snapshot",
                [action],
                interaction_mode=ComputerInteractionMode.BACKGROUND,
                plan_ref=plan.plan_ref,
            )
            replay = await backend.act(
                target,
                "fixture_snapshot",
                [action],
                interaction_mode=ComputerInteractionMode.BACKGROUND,
                plan_ref=plan.plan_ref,
            )
            return first, replay
        finally:
            await transport.close()

    first, replay = run(scenario())
    assert first.ok is True
    assert replay.error.code is ComputerErrorCode.STALE_SNAPSHOT
    assert replay.result == {
        "outcomes": [],
        "last_acknowledged_action": -1,
    }


def test_symbol_gate_allows_explicit_foreground_hid_backend():
    result = run_symbol_gate(["_CGEventPostToPid", "_CGEventTapCreate", "_CGEventPost"])
    assert result.returncode == 0
