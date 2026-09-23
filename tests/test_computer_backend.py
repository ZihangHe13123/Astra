"""Contract tests for the platform-neutral Computer Use session boundary."""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import fcntl
except ImportError:
    fcntl = None

requires_posix_authority = pytest.mark.skipif(
    os.name == "nt" or fcntl is None,
    reason="uses POSIX directory descriptors, file ownership or process-group authority",
)

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
from agent.runtime.computer_protocol import (
    ComputerError,
    ComputerErrorCode,
    ComputerInteractionMode,
    ComputerResponse,
    ComputerSnapshot,
    ComputerSnapshotTextDetailMode,
    ForegroundFragmentActionRequirement,
    ForegroundFragmentDeclaration,
    ForegroundFragmentPlanRequest,
    ForegroundFragmentStageDeclaration,
    FragmentStageAuthority,
    FragmentStageCommit,
    FragmentStageCommitResult,
    TakeoverRestoration,
)
from agent.runtime.macos_computer import (
    ComputerRequest,
    HelperApplicationError,
    HelperTransport,
    HelperTransportError,
    MacComputerBackend,
    resolve_macos_helper,
)


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _require_posix_session_authority(monkeypatch):
    if os.name != "nt" and fcntl is not None:
        return

    def unsupported(*args, **kwargs):
        pytest.skip("ComputerSessionManager requires POSIX directory descriptors and cache leases")

    # Portable dataclasses and mocked MacComputerBackend protocol tests remain
    # active. Only tests that request the POSIX artifact manager are skipped.
    monkeypatch.setattr(ComputerSessionManager, "__init__", unsupported)


async def _resume_and_commit_for_test(manager, publication_id="test-resume-publication"):
    target = await manager.resume(publication_id)
    await manager.commit_resume_publication(publication_id)
    return target


_SMART_SNAPSHOT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)


def _smart_snapshot_detail(snapshot_id: str) -> bytes:
    return json.dumps(
        {
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
                "node_count": 1,
                "max_depth_observed": 0,
                "truncated": False,
                "truncation_reasons": [],
            },
            "root": {
                "node_id": "node_0",
                "role": "AXWindow",
                "subrole": "AXStandardWindow",
                "title": "Document",
                "enabled": True,
                "focused": True,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _write_helper_artifact(artifact: ComputerArtifact, data: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(
        artifact.filename,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=artifact.directory_fd,
    )
    try:
        os.write(descriptor, data)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


class SmartSnapshotBackend:
    def __init__(self) -> None:
        self.smart_calls: list[tuple[ComputerArtifact, ComputerArtifact]] = []
        self.detail_mutator = lambda document: document
        self.fail_after_png = False
        self.detail_mode = 0o600
        self.closed = 0

    async def select(self, app_ref, window_ref):
        return ComputerTarget(app_ref, window_ref)

    async def snapshot(
        self,
        target,
        scope,
        artifact,
        *,
        text_detail=ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact=None,
    ):
        if text_detail is ComputerSnapshotTextDetailMode.OFF:
            return ComputerSnapshot("snapshot-off")
        assert text_detail_artifact is not None
        self.smart_calls.append((artifact, text_detail_artifact))
        _write_helper_artifact(artifact, _SMART_SNAPSHOT_PNG)
        if self.fail_after_png:
            raise OSError("helper crashed after PNG publication")
        snapshot_id = "snapshot-smart"
        document = json.loads(_smart_snapshot_detail(snapshot_id))
        detail = json.dumps(
            self.detail_mutator(document),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        _write_helper_artifact(text_detail_artifact, detail, mode=self.detail_mode)
        digest = hashlib.sha256(detail).hexdigest()
        stats = document["stats"]
        return ComputerSnapshot(
            snapshot_id,
            {
                "image_artifact": artifact.filename,
                "pixel_size": {"width": 1, "height": 1},
                "text_detail_artifact": text_detail_artifact.filename,
                "text_detail_metadata": {
                    "schema_version": 1,
                    "snapshot_id": snapshot_id,
                    "coverage": "reported_ax_subtree",
                    "node_count": stats["node_count"],
                    "max_depth_observed": stats["max_depth_observed"],
                    "byte_count": len(detail),
                    "sha256": digest,
                    "truncated": stats["truncated"],
                    "truncation_reasons": stats["truncation_reasons"],
                },
            },
        )

    async def close(self):
        self.closed += 1


def plan_and_act(manager, snapshot_id, actions):
    plan = run(manager.plan_actions(
        snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.BACKGROUND,
    ))
    return run(manager.act(
        snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.BACKGROUND,
        plan_ref=plan.plan_ref,
    ))


def mac_background_act(backend, target, snapshot_id, actions):
    return backend.act(
        target,
        snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.BACKGROUND,
        plan_ref="plan-test",
    )


class FakeBackend:
    def __init__(self) -> None:
        self.select_calls: list[tuple[str, str]] = []
        self.plan_calls: list[tuple[ComputerTarget, str, list[object], ComputerInteractionMode]] = []
        self.snapshot_calls: list[tuple[ComputerTarget, str]] = []
        self.act_calls: list[tuple[ComputerTarget, str, list[object], ComputerInteractionMode, str, str | None]] = []
        self.takeover_begin_calls: list[tuple[str, str]] = []
        self.takeover_end_calls: list[str] = []
        self.apps_result: list[dict[str, object]] = []
        self.confirmed_absent_window_identity_refs: tuple[str, ...] = ()
        self.closed = 0

    async def status(self) -> dict[str, object]:
        return {"supported": True}

    async def apps(self) -> ComputerAppCatalog:
        return ComputerAppCatalog(
            1, tuple(self.apps_result), self.confirmed_absent_window_identity_refs,
        )

    async def select(self, app_ref: str, window_ref: str) -> ComputerTarget:
        self.select_calls.append((app_ref, window_ref))
        return ComputerTarget(app_ref, window_ref)

    async def snapshot(self, target: ComputerTarget, scope: str, artifact: ComputerArtifact) -> ComputerSnapshot:
        self.snapshot_calls.append((target, scope))
        return ComputerSnapshot(f"snap-{len(self.snapshot_calls)}")

    async def plan_actions(self, target, snapshot_id, actions, interaction_mode):
        self.plan_calls.append((target, snapshot_id, actions, interaction_mode))
        return ComputerActionPlan(
            plan_ref=f"plan-{len(self.plan_calls)}",
            interaction_mode=interaction_mode,
            requires_takeover=interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER,
            reason=(
                "foreground_takeover_required"
                if interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
                else "background_ax_only"
            ),
            action_classes=tuple(action.type for action in actions if action.type != "wait"),
            pid_action_classes=tuple(action.type for action in actions if action.type != "wait"),
        )

    async def begin_takeover(self, snapshot_id, plan_ref):
        self.takeover_begin_calls.append((snapshot_id, plan_ref))
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
        fragment_stage=None,
    ) -> ComputerActionResult:
        self.act_calls.append(
            (target, snapshot_id, actions, interaction_mode, plan_ref, takeover_ref)
        )
        return ComputerActionResult(result={"actions": [{"ok": True}]})

    async def end_takeover(self, takeover_ref):
        self.takeover_end_calls.append(takeover_ref)
        return {"ended": True}

    async def close(self) -> None:
        self.closed += 1


def test_computer_action_plan_rejects_non_string_pid_class_with_stable_validation_error():
    with pytest.raises(ValueError, match="pid_action_classes"):
        ComputerActionPlan(
            plan_ref="plan-1",
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            requires_takeover=True,
            reason="foreground_takeover_required",
            action_classes=("scroll",),
            pid_action_classes=(["scroll"],),
        )


def test_platform_neutral_backend_module_imports_without_posix_fcntl() -> None:
    script = """
import builtins
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError("simulated non-POSIX host")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import agent.runtime.computer_backend as backend
assert backend._fcntl is None
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


def test_smart_snapshot_uses_one_token_and_held_directory_then_exposes_verified_detail(tmp_path):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path / "cache")
    run(manager.select("app", "window"))

    snapshot = run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))

    image, detail = backend.smart_calls[0]
    assert image.directory_fd == detail.directory_fd == manager._session_fd
    assert image.directory_path == detail.directory_path == manager.session_dir
    assert image.filename.removesuffix(".png") == detail.filename.removesuffix(".ax.json")
    assert snapshot.payload["text_detail_artifact"] == detail.filename
    detail_path = Path(snapshot.payload["text_detail_path"])
    assert detail_path.is_absolute()
    assert json.loads(detail_path.read_bytes())["snapshot_id"] == snapshot.snapshot_id


def test_smart_snapshot_resolves_verified_path_after_cache_root_rename(tmp_path):
    backend = SmartSnapshotBackend()
    cache_root = tmp_path / "cache"
    manager = ComputerSessionManager(backend, cache_root=cache_root)
    run(manager.select("app", "window"))
    moved = tmp_path / "moved-cache"
    cache_root.rename(moved)
    cache_root.symlink_to(tmp_path / "outside", target_is_directory=True)

    snapshot = run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))

    path = Path(snapshot.payload["text_detail_path"])
    assert path.read_bytes()
    assert moved in path.parents


@pytest.mark.parametrize("candidate_state", ["renamed", "wrong_inode", "outside_cache"])
def test_linux_fd_path_requires_same_session_and_held_cache_parent(
    tmp_path, monkeypatch, candidate_state,
):
    import agent.runtime.computer_backend as module

    cache_root = tmp_path / "cache"
    manager = ComputerSessionManager(FakeBackend(), cache_root=cache_root)
    moved_root = tmp_path / "moved-cache"
    session_name = manager.session_dir.name
    cache_root.rename(moved_root)
    candidate = moved_root / session_name
    if candidate_state == "wrong_inode":
        candidate = moved_root / "replacement"
        candidate.mkdir(mode=0o700)
    elif candidate_state == "outside_cache":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        candidate = candidate.rename(outside / session_name)

    def readlink(path):
        assert path == f"/proc/self/fd/{manager._session_fd}"
        return str(candidate)

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module.os, "readlink", readlink)

    if candidate_state == "renamed":
        assert manager._current_session_path() == candidate
    else:
        with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
            manager._current_session_path()


def test_linux_fd_path_unavailable_keeps_verified_original_path(tmp_path, monkeypatch):
    import agent.runtime.computer_backend as module

    manager = ComputerSessionManager(FakeBackend(), cache_root=tmp_path / "cache")

    def unavailable(path):
        assert path == f"/proc/self/fd/{manager._session_fd}"
        raise FileNotFoundError("proc is unavailable")

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module.os, "readlink", unavailable)
    assert manager._current_session_path() == manager.session_dir


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: {**value, "unknown": True}, "schema"),
        (lambda value: {**value, "snapshot_id": "other"}, "snapshot"),
        (
            lambda value: {
                **value,
                "root": {**value["root"], "value": "secret", "redacted": True},
            },
            "redact",
        ),
        (
            lambda value: {
                **value,
                "stats": {**value["stats"], "node_count": 1.0},
            },
            "integer",
        ),
    ],
)
def test_smart_snapshot_rejects_invalid_detail_schema_before_exposing_path(
    tmp_path, mutation, message,
):
    backend = SmartSnapshotBackend()
    backend.detail_mutator = mutation
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))

    with pytest.raises(ComputerSessionError, match=message):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))


def test_smart_snapshot_rejects_wrong_mode_and_hardlink(tmp_path):
    backend = SmartSnapshotBackend()
    backend.detail_mode = 0o644
    manager = ComputerSessionManager(backend, cache_root=tmp_path / "mode")
    run(manager.select("app", "window"))
    with pytest.raises(ComputerSessionError, match="0600"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))

    class HardlinkBackend(SmartSnapshotBackend):
        async def snapshot(self, target, scope, artifact, **kwargs):
            snapshot = await super().snapshot(target, scope, artifact, **kwargs)
            detail = kwargs["text_detail_artifact"]
            os.link(
                detail.filename,
                "outside-link",
                src_dir_fd=detail.directory_fd,
                dst_dir_fd=detail.directory_fd,
            )
            return snapshot

    hardlink = HardlinkBackend()
    manager = ComputerSessionManager(hardlink, cache_root=tmp_path / "link")
    run(manager.select("app", "window"))
    with pytest.raises(ComputerSessionError, match="link"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))

    class SymlinkBackend(SmartSnapshotBackend):
        async def snapshot(self, target, scope, artifact, **kwargs):
            snapshot = await super().snapshot(target, scope, artifact, **kwargs)
            detail = kwargs["text_detail_artifact"]
            os.rename(
                detail.filename,
                f"{detail.filename}.real",
                src_dir_fd=detail.directory_fd,
                dst_dir_fd=detail.directory_fd,
            )
            os.symlink(
                f"{detail.filename}.real",
                detail.filename,
                dir_fd=detail.directory_fd,
            )
            return snapshot

    symlink = SymlinkBackend()
    manager = ComputerSessionManager(symlink, cache_root=tmp_path / "symlink")
    run(manager.select("app", "window"))
    with pytest.raises(ComputerSessionError, match="regular file"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))


def test_partial_smart_snapshot_poison_blocks_another_helper_until_close(tmp_path):
    backend = SmartSnapshotBackend()
    backend.fail_after_png = True
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))

    with pytest.raises(ComputerSessionError, match="inventory"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))

    names = os.listdir(manager._session_fd)
    assert ComputerSessionManager._LEASE_NAME in names
    assert any(name.endswith(".png") for name in names)
    with pytest.raises(ComputerSessionError, match="cleanup|poison|inventory"):
        run(manager.select("app", "other-window"))
    run(manager.close())
    assert not manager.session_dir.exists()


@pytest.mark.parametrize(
    "failure",
    [
        asyncio.CancelledError(),
        HelperTransportError("timeout or EOF"),
        HelperApplicationError(ComputerError(ComputerErrorCode.TARGET_GONE, "gone")),
    ],
)
def test_partial_smart_snapshot_cancellation_transport_and_target_gone_are_zero_replay(
    tmp_path,
    failure,
):
    class FailingBackend(SmartSnapshotBackend):
        calls = 0

        async def snapshot(self, target, scope, artifact, **kwargs):
            self.calls += 1
            _write_helper_artifact(artifact, _SMART_SNAPSHOT_PNG)
            raise failure

    backend = FailingBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))

    expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else ComputerSessionError
    with pytest.raises(expected):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    with pytest.raises(ComputerSessionError, match="poison|inventory|cleanup"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    assert backend.calls == 1


def test_smart_snapshot_metadata_hash_and_bytes_must_match_opened_detail(tmp_path):
    class MismatchBackend(SmartSnapshotBackend):
        async def snapshot(self, target, scope, artifact, **kwargs):
            snapshot = await super().snapshot(target, scope, artifact, **kwargs)
            payload = dict(snapshot.payload)
            payload["text_detail_metadata"] = {
                **payload["text_detail_metadata"],
                "sha256": "0" * 64,
            }
            return ComputerSnapshot(snapshot.snapshot_id, payload)

    manager = ComputerSessionManager(MismatchBackend(), cache_root=tmp_path)
    run(manager.select("app", "window"))
    with pytest.raises(ComputerSessionError, match="hash"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))


def test_smart_snapshot_opens_both_descriptors_before_reading_either(tmp_path, monkeypatch):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    real_open = os.open
    real_read = os.read
    opened_artifacts: list[str] = []

    def observe_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith("snapshot-"):
            opened_artifacts.append(path)
        return descriptor

    def require_pair_before_read(descriptor, size):
        if len(opened_artifacts) >= 1:
            assert any(name.endswith(".png") for name in opened_artifacts)
            assert any(name.endswith(".ax.json") for name in opened_artifacts)
        return real_read(descriptor, size)

    monkeypatch.setattr("agent.runtime.computer_backend.os.open", observe_open)
    monkeypatch.setattr("agent.runtime.computer_backend.os.read", require_pair_before_read)

    run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))


def test_smart_snapshot_rejects_inode_swap_between_named_stat_and_open(tmp_path, monkeypatch):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    real_open = os.open
    swapped = False

    def swap_detail_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            isinstance(path, str)
            and path.endswith(".ax.json")
            and dir_fd == manager._session_fd
            and not flags & (os.O_WRONLY | os.O_RDWR)
            and not swapped
        ):
            swapped = True
            os.rename(path, f"{path}.old", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            descriptor = real_open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            os.write(descriptor, b"{}")
            os.close(descriptor)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agent.runtime.computer_backend.os.open", swap_detail_then_open)

    with pytest.raises(ComputerSessionError, match="identity|authority"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    assert swapped is True


@pytest.mark.parametrize("mutation", ["hardlink", "chmod"])
def test_smart_snapshot_rejects_authority_mutation_between_pre_stat_and_open(
    tmp_path,
    monkeypatch,
    mutation,
):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path / "cache")
    run(manager.select("app", "window"))
    real_open = os.open
    mutated = False

    def mutate_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal mutated
        if (
            isinstance(path, str)
            and path.endswith(".png")
            and dir_fd == manager._session_fd
            and not flags & (os.O_WRONLY | os.O_RDWR)
            and not mutated
        ):
            mutated = True
            if mutation == "hardlink":
                os.link(path, tmp_path / "external-link", src_dir_fd=dir_fd)
            else:
                os.chmod(path, 0o640, dir_fd=dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agent.runtime.computer_backend.os.open", mutate_then_open)

    with pytest.raises(ComputerSessionError, match="0600|link|authority"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    assert mutated is True


@pytest.mark.parametrize("mutation", ["hardlink", "chmod"])
def test_smart_snapshot_rejects_authority_mutation_between_read_and_post_stat(
    tmp_path,
    monkeypatch,
    mutation,
):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path / "cache")
    run(manager.select("app", "window"))
    real_read = os.read
    mutated = False

    def mutate_after_read(descriptor, size):
        nonlocal mutated
        data = real_read(descriptor, size)
        if data and not mutated:
            metadata = os.fstat(descriptor)
            image_name = next(
                name for name in os.listdir(manager._session_fd) if name.endswith(".png")
            )
            image = os.stat(image_name, dir_fd=manager._session_fd, follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) == (image.st_dev, image.st_ino):
                mutated = True
                if mutation == "hardlink":
                    os.link(
                        image_name,
                        tmp_path / "external-link",
                        src_dir_fd=manager._session_fd,
                    )
                else:
                    os.chmod(image_name, 0o640, dir_fd=manager._session_fd)
        return data

    monkeypatch.setattr("agent.runtime.computer_backend.os.read", mutate_after_read)

    with pytest.raises(ComputerSessionError, match="0600|link|authority"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    assert mutated is True


def test_smart_snapshot_rejects_png_ihdr_dimension_mismatch(tmp_path):
    class DimensionMismatchBackend(SmartSnapshotBackend):
        async def snapshot(self, target, scope, artifact, **kwargs):
            snapshot = await super().snapshot(target, scope, artifact, **kwargs)
            return ComputerSnapshot(snapshot.snapshot_id, {
                **snapshot.payload,
                "pixel_size": {"width": 2, "height": 1},
            })

    manager = ComputerSessionManager(DimensionMismatchBackend(), cache_root=tmp_path)
    run(manager.select("app", "window"))
    with pytest.raises(ComputerSessionError, match="dimensions"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))


def test_target_invalidation_removes_both_verified_smart_snapshot_artifacts(tmp_path):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    image, detail = backend.smart_calls[0]
    assert set(os.listdir(manager._session_fd)) == {
        ComputerSessionManager._LEASE_NAME,
        image.filename,
        detail.filename,
    }

    run(manager.select("app", "other-window"))

    assert os.listdir(manager._session_fd) == [ComputerSessionManager._LEASE_NAME]


@pytest.mark.parametrize("replaced_kind", ["detail", "image"])
def test_target_invalidation_preserves_entire_pair_when_verified_authority_changed(
    tmp_path,
    monkeypatch,
    replaced_kind,
):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    image, detail = backend.smart_calls[0]
    assert manager._current_snapshot_artifacts == {
        artifact.filename: manager._artifact_authority(
            os.stat(artifact.filename, dir_fd=manager._session_fd, follow_symlinks=False)
        )
        for artifact in (image, detail)
    }
    changed = detail if replaced_kind == "detail" else image
    unchanged = image if replaced_kind == "detail" else detail
    original = os.stat(changed.filename, dir_fd=manager._session_fd, follow_symlinks=False)
    original_bytes = (manager.session_dir / changed.filename).read_bytes()
    preserved_name = f"preserved-{replaced_kind}-original"
    os.rename(
        changed.filename,
        preserved_name,
        src_dir_fd=manager._session_fd,
        dst_dir_fd=manager._session_fd,
    )
    _write_helper_artifact(changed, b"replacement")
    replacement = os.stat(changed.filename, dir_fd=manager._session_fd, follow_symlinks=False)
    assert (replacement.st_dev, replacement.st_ino) != (original.st_dev, original.st_ino)

    unlink_calls: list[str] = []
    real_unlink = os.unlink

    def observe_unlink(path, *args, **kwargs):
        unlink_calls.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("agent.runtime.computer_backend.os.unlink", observe_unlink)

    with pytest.raises(ComputerSessionError, match="authority|identity"):
        run(manager.select("app", "other-window"))

    assert unlink_calls == []
    assert (manager.session_dir / changed.filename).read_bytes() == b"replacement"
    assert (manager.session_dir / preserved_name).read_bytes() == original_bytes
    assert (manager.session_dir / unchanged.filename).exists()
    with pytest.raises(ComputerSessionError, match="poison|cleanup"):
        run(manager.status())


@requires_posix_authority
def test_target_invalidation_peer_shared_lease_preserves_pair_without_side_effects(
    tmp_path,
    monkeypatch,
):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    before_names = os.listdir(manager._session_fd)
    before = {
        name: manager._artifact_authority(
            os.stat(name, dir_fd=manager._session_fd, follow_symlinks=False)
        )
        for name in before_names
    }
    peer_fd = os.open(manager.session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    fcntl.flock(peer_fd, fcntl.LOCK_SH)
    unlink_calls: list[str] = []
    fsync_calls: list[int] = []
    real_unlink = os.unlink
    real_fsync = os.fsync

    def observe_unlink(path, *args, **kwargs):
        unlink_calls.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    def observe_fsync(descriptor):
        fsync_calls.append(descriptor)
        return real_fsync(descriptor)

    monkeypatch.setattr("agent.runtime.computer_backend.os.unlink", observe_unlink)
    monkeypatch.setattr("agent.runtime.computer_backend.os.fsync", observe_fsync)
    try:
        with pytest.raises(ComputerSessionError, match="lease"):
            run(manager.select("app", "other-window"))

        assert unlink_calls == []
        assert fsync_calls == []
        assert os.listdir(manager._session_fd) == before_names
        assert {
            name: manager._artifact_authority(
                os.stat(name, dir_fd=manager._session_fd, follow_symlinks=False)
            )
            for name in before_names
        } == before
        assert manager.target == ComputerTarget("app", "window")
        with pytest.raises(ComputerSessionError, match="poison|cleanup"):
            run(manager.status())
    finally:
        os.close(peer_fd)

    probe_fd = os.open(manager.session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(OSError) as caught:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert caught.value.errno in {errno.EACCES, errno.EAGAIN}
    finally:
        os.close(probe_fd)


def test_terminal_cleanup_preserves_all_entries_when_residual_is_uncertain(tmp_path, monkeypatch):
    class ResidualBackend(SmartSnapshotBackend):
        async def snapshot(self, target, scope, artifact, **kwargs):
            _write_helper_artifact(artifact, _SMART_SNAPSHOT_PNG)
            identifier = "11111111-1111-1111-1111-111111111111"
            temp = ComputerArtifact(
                f".{artifact.filename}.{identifier}.tmp",
                artifact.directory_fd,
                artifact.directory_path,
            )
            quarantine = ComputerArtifact(
                f".rollback-{identifier}.quarantine",
                artifact.directory_fd,
                artifact.directory_path,
            )
            _write_helper_artifact(temp, b"temp")
            _write_helper_artifact(quarantine, b"quarantine")
            raise OSError("helper failed with residuals")

    manager = ComputerSessionManager(ResidualBackend(), cache_root=tmp_path)
    run(manager.select("app", "window"))

    with pytest.raises(ComputerSessionError, match="inventory"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))

    names = os.listdir(manager._session_fd)
    assert any(name.endswith(".tmp") for name in names)
    assert any(name.endswith(".quarantine") for name in names)
    with pytest.raises(ComputerSessionError, match="cleanup|poison|inventory"):
        run(manager.select("app", "other-window"))
    before = {
        name: os.stat(name, dir_fd=manager._session_fd, follow_symlinks=False)
        for name in names
    }
    unlink_calls: list[str] = []
    real_unlink = os.unlink

    def observe_unlink(path, *args, **kwargs):
        unlink_calls.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("agent.runtime.computer_backend.os.unlink", observe_unlink)

    with pytest.raises(ComputerSessionError, match="residual|unsafe_cache"):
        run(manager.close())

    assert unlink_calls == []
    assert os.listdir(manager._session_fd) == names
    for name, expected in before.items():
        current = os.stat(name, dir_fd=manager._session_fd, follow_symlinks=False)
        assert (current.st_dev, current.st_ino, current.st_size) == (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
        )
    with pytest.raises(ComputerSessionError, match="poison|cleanup"):
        run(manager.status())


def test_terminal_cleanup_rechecks_known_authority_before_any_named_unlink(tmp_path, monkeypatch):
    backend = FakeBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    artifact_name = "snapshot-0123456789abcdef0123456789abcdef.png"
    manager.write_artifact(artifact_name, b"original")
    real_open = os.open
    real_unlink = os.unlink
    unlink_calls: list[str] = []
    mutated = False

    def replace_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal mutated
        if (
            path == artifact_name
            and dir_fd == manager._session_fd
            and not flags & (os.O_WRONLY | os.O_RDWR)
            and not mutated
        ):
            mutated = True
            os.rename(
                artifact_name,
                "preserved-original",
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            replacement_fd = real_open(
                artifact_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            os.write(replacement_fd, b"replacement")
            os.close(replacement_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def observe_unlink(path, *args, **kwargs):
        unlink_calls.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("agent.runtime.computer_backend.os.open", replace_then_open)
    monkeypatch.setattr("agent.runtime.computer_backend.os.unlink", observe_unlink)

    with pytest.raises(ComputerSessionError, match="identity|authority"):
        run(manager.close())

    assert mutated is True
    assert unlink_calls == []
    assert (manager.session_dir / artifact_name).read_bytes() == b"replacement"
    assert (manager.session_dir / "preserved-original").read_bytes() == b"original"


@requires_posix_authority
def test_failed_exclusive_session_lease_preserves_pair_without_unlink_or_fsync(
    tmp_path,
    monkeypatch,
):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    before_names = os.listdir(manager._session_fd)
    before = {
        name: os.stat(name, dir_fd=manager._session_fd, follow_symlinks=False)
        for name in before_names
    }
    peer_fd = os.open(manager.session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    fcntl.flock(peer_fd, fcntl.LOCK_SH)
    unlink_calls: list[str] = []
    fsync_calls: list[int] = []
    real_unlink = os.unlink
    real_fsync = os.fsync

    def observe_unlink(path, *args, **kwargs):
        unlink_calls.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    def observe_fsync(descriptor):
        fsync_calls.append(descriptor)
        return real_fsync(descriptor)

    monkeypatch.setattr("agent.runtime.computer_backend.os.unlink", observe_unlink)
    monkeypatch.setattr("agent.runtime.computer_backend.os.fsync", observe_fsync)
    try:
        with pytest.raises(ComputerSessionError, match="lease"):
            run(manager.close())

        assert unlink_calls == []
        assert fsync_calls == []
        assert os.listdir(manager._session_fd) == before_names
        for name, expected in before.items():
            current = os.stat(name, dir_fd=manager._session_fd, follow_symlinks=False)
            assert (current.st_dev, current.st_ino, current.st_size) == (
                expected.st_dev,
                expected.st_ino,
                expected.st_size,
            )
        assert manager._current_snapshot_artifacts == {}
        assert manager._latest_snapshot is None
        with pytest.raises(ComputerSessionError, match="poison|cleanup"):
            run(manager.status())
    finally:
        os.close(peer_fd)

    probe_fd = os.open(manager.session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(OSError) as caught:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert caught.value.errno in {errno.EACCES, errno.EAGAIN}
    finally:
        os.close(probe_fd)


def test_success_response_with_extra_residual_is_not_exposed_and_poisoned(tmp_path):
    class ExtraResidualBackend(SmartSnapshotBackend):
        async def snapshot(self, target, scope, artifact, **kwargs):
            snapshot = await super().snapshot(target, scope, artifact, **kwargs)
            _write_helper_artifact(
                ComputerArtifact("unexpected.residual", artifact.directory_fd, artifact.directory_path),
                b"uncertain",
            )
            return snapshot

    manager = ComputerSessionManager(ExtraResidualBackend(), cache_root=tmp_path)
    run(manager.select("app", "window"))
    with pytest.raises(ComputerSessionError, match="inventory delta"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    with pytest.raises(ComputerSessionError, match="poison|cleanup"):
        run(manager.select("app", "other"))
    assert "unexpected.residual" in os.listdir(manager._session_fd)


def test_preexisting_unknown_residual_blocks_helper_dispatch(tmp_path):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    _write_helper_artifact(
        ComputerArtifact("unknown.residual", manager._session_fd, manager.session_dir),
        b"uncertain",
    )

    with pytest.raises(ComputerSessionError, match="residual"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))
    assert backend.smart_calls == []


def test_uncertain_invalidation_cleanup_blocks_session_until_close(tmp_path, monkeypatch):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))

    def fail_cleanup(_name):
        raise ComputerSessionError("unsafe_cache", "unsafe_cache: simulated cleanup failure")

    monkeypatch.setattr(manager, "_remove_owned_regular_artifact", fail_cleanup)
    with pytest.raises(ComputerSessionError, match="cleanup failure"):
        run(manager.select("app", "other-window"))
    with pytest.raises(ComputerSessionError, match="cleanup could not be proved"):
        run(manager.select("app", "third-window"))


def test_python_detail_quota_rejects_ninth_artifact_without_deleting_existing(tmp_path):
    backend = SmartSnapshotBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app", "window"))
    retained = []
    for index in range(8):
        token = f"{index:032x}"
        image = ComputerArtifact(f"snapshot-{token}.png", manager._session_fd, manager.session_dir)
        detail = ComputerArtifact(f"snapshot-{token}.ax.json", manager._session_fd, manager.session_dir)
        _write_helper_artifact(image, _SMART_SNAPSHOT_PNG)
        _write_helper_artifact(detail, _smart_snapshot_detail(f"old-{index}"))
        retained.extend((image.filename, detail.filename))

    with pytest.raises(ComputerSessionError, match="quota"):
        run(manager.snapshot(text_detail=ComputerSnapshotTextDetailMode.ON))

    assert set(retained).issubset(os.listdir(manager._session_fd))


def test_select_uses_nonactivating_backend_operation(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)

    selected = run(manager.select("app-1", "window-1"))

    assert selected == ComputerTarget("app-1", "window-1")
    assert fake_backend.select_calls == [("app-1", "window-1")]
    assert not hasattr(fake_backend, "focus")


def test_fragment_manager_keeps_one_takeover_across_fresh_single_use_stage_plans(tmp_path):
    class FragmentBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.fragment_plans = []
            self.fragment_acts = []
            self.fragment_commits = []

        async def plan_actions(self, target, snapshot_id, actions, interaction_mode, *, fragment=None):
            self.fragment_plans.append(fragment)
            return await super().plan_actions(target, snapshot_id, actions, interaction_mode)

        async def begin_takeover(self, snapshot_id, plan_ref, *, declaration=None):
            self.takeover_begin_calls.append((snapshot_id, plan_ref))
            assert declaration is not None
            return "takeover-1"

        async def act(self, *args, fragment_stage=None, **kwargs):
            self.fragment_acts.append(fragment_stage)
            return await super().act(*args, **kwargs)

        async def commit_fragment_stage(self, commit):
            self.fragment_commits.append(commit)
            terminal = commit.stage_index == 1
            return FragmentStageCommitResult(
                terminal,
                TakeoverRestoration.RESTORED if terminal else None,
            )

    backend = FragmentBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    wait_requirement = ForegroundFragmentActionRequirement("wait", None, None)
    declaration = ForegroundFragmentDeclaration(
        "a" * 64,
        (
            ForegroundFragmentStageDeclaration("0" * 64, 1, (wait_requirement,)),
            ForegroundFragmentStageDeclaration("1" * 64, 1, (wait_requirement,)),
        ),
        2,
        1_000,
        True,
    )

    async def scenario():
        await manager.select("app-1", "window-1")
        first_snapshot = await manager.snapshot()
        first_stage = FragmentStageAuthority(
            declaration.fragment_hash, 0, declaration.stages[0].stage_hash, first_snapshot.snapshot_id
        )
        first_plan = await manager.plan_actions(
            first_snapshot.snapshot_id,
            [{"type": "wait", "duration_ms": 0}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            fragment=ForegroundFragmentPlanRequest.stage(first_stage),
        )
        takeover_ref = await manager.begin_takeover(
            first_snapshot.snapshot_id,
            first_plan.plan_ref,
            declaration=declaration,
        )
        await manager.act(
            first_snapshot.snapshot_id,
            [{"type": "wait", "duration_ms": 0}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            plan_ref=first_plan.plan_ref,
            takeover_ref=takeover_ref,
            fragment_stage=first_stage,
        )
        second_snapshot = await manager.snapshot()
        assert not (await manager.commit_fragment_stage(FragmentStageCommit(
            takeover_ref, declaration.fragment_hash, 0, declaration.stages[0].stage_hash,
            first_plan.plan_ref, second_snapshot.snapshot_id, True,
        ))).terminal
        second_stage = FragmentStageAuthority(
            declaration.fragment_hash, 1, declaration.stages[1].stage_hash, second_snapshot.snapshot_id
        )
        second_plan = await manager.plan_actions(
            second_snapshot.snapshot_id,
            [{"type": "wait", "duration_ms": 0}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            fragment=ForegroundFragmentPlanRequest.stage(second_stage, takeover_ref),
        )
        await manager.act(
            second_snapshot.snapshot_id,
            [{"type": "wait", "duration_ms": 0}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            plan_ref=second_plan.plan_ref,
            takeover_ref=takeover_ref,
            fragment_stage=second_stage,
        )
        final_snapshot = await manager.snapshot()
        final = await manager.commit_fragment_stage(FragmentStageCommit(
            takeover_ref, declaration.fragment_hash, 1, declaration.stages[1].stage_hash,
            second_plan.plan_ref, final_snapshot.snapshot_id, True,
        ))
        assert final.terminal
        assert final.restoration is TakeoverRestoration.RESTORED

    run(scenario())
    assert backend.takeover_begin_calls == [("snap-1", "plan-1")]
    assert [stage.authority.stage_index for stage in backend.fragment_plans] == [0, 1]
    assert [stage.stage_index for stage in backend.fragment_acts] == [0, 1]
    assert len(backend.fragment_commits) == 2

    failing_backend = FragmentBackend()
    failing_manager = ComputerSessionManager(failing_backend, cache_root=tmp_path / "failure")

    async def failing_observation_scenario():
        await failing_manager.select("app-1", "window-1")
        input_snapshot = await failing_manager.snapshot()
        stage = FragmentStageAuthority(
            declaration.fragment_hash,
            0,
            declaration.stages[0].stage_hash,
            input_snapshot.snapshot_id,
        )
        plan = await failing_manager.plan_actions(
            input_snapshot.snapshot_id,
            [{"type": "wait", "duration_ms": 0}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            fragment=ForegroundFragmentPlanRequest.stage(stage),
        )
        takeover_ref = await failing_manager.begin_takeover(
            input_snapshot.snapshot_id,
            plan.plan_ref,
            declaration=declaration,
        )
        await failing_manager.act(
            input_snapshot.snapshot_id,
            [{"type": "wait", "duration_ms": 0}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            plan_ref=plan.plan_ref,
            takeover_ref=takeover_ref,
            fragment_stage=stage,
        )

        async def fail_snapshot(*_args, **_kwargs):
            raise OSError("observation failed")

        failing_backend.snapshot = fail_snapshot
        with pytest.raises(ComputerSessionError, match="unknown_outcome"):
            await failing_manager.snapshot()

    run(failing_observation_scenario())
    assert failing_backend.takeover_end_calls == ["takeover-1"]


def _single_wait_fragment():
    return _wait_fragment(1)


def _wait_fragment(stage_count):
    requirement = ForegroundFragmentActionRequirement("wait", None, None)
    declaration = ForegroundFragmentDeclaration(
        "b" * 64,
        tuple(
            ForegroundFragmentStageDeclaration(str(index + 2) * 64, 1, (requirement,))
            for index in range(stage_count)
        ),
        stage_count,
        1_000,
        True,
    )
    return declaration


async def _start_wait_fragment(manager, declaration):
    await manager.select("app-1", "window-1")
    snapshot = await manager.snapshot()
    stage = FragmentStageAuthority(
        declaration.fragment_hash,
        0,
        declaration.stages[0].stage_hash,
        snapshot.snapshot_id,
    )
    plan = await manager.plan_actions(
        snapshot.snapshot_id,
        [{"type": "wait", "duration_ms": 0}],
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        fragment=ForegroundFragmentPlanRequest.stage(stage),
    )
    takeover_ref = await manager.begin_takeover(
        snapshot.snapshot_id,
        plan.plan_ref,
        declaration=declaration,
    )
    return snapshot, stage, plan, takeover_ref


async def _ready_wait_fragment_commit(manager, declaration):
    snapshot, stage, plan, takeover_ref = await _start_wait_fragment(manager, declaration)
    await manager.act(
        snapshot.snapshot_id,
        [{"type": "wait", "duration_ms": 0}],
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        plan_ref=plan.plan_ref,
        takeover_ref=takeover_ref,
        fragment_stage=stage,
    )
    fresh = await manager.snapshot()
    return FragmentStageCommit(
        takeover_ref,
        declaration.fragment_hash,
        0,
        declaration.stages[0].stage_hash,
        plan.plan_ref,
        fresh.snapshot_id,
        True,
    )


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("cancel_before_receipt", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_fragment_commit_cancellation_harvests_receipt_and_owns_terminal_cleanup(
    tmp_path,
    terminal,
    cancel_before_receipt,
    cleanup_fails,
):
    class CommitCancellationBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.commit_started = asyncio.Event()
            self.release_commit = asyncio.Event()
            self.manager_task = None

        async def plan_actions(self, target, snapshot_id, actions, interaction_mode, *, fragment=None):
            return await super().plan_actions(target, snapshot_id, actions, interaction_mode)

        async def begin_takeover(self, snapshot_id, plan_ref, *, declaration=None):
            return "takeover-1"

        async def commit_fragment_stage(self, commit):
            self.commit_started.set()
            if cancel_before_receipt:
                await self.release_commit.wait()
            else:
                asyncio.get_running_loop().call_soon(self.manager_task.cancel)
            return FragmentStageCommitResult(
                terminal,
                TakeoverRestoration.RESTORED if terminal else None,
            )

        async def end_takeover(self, takeover_ref):
            self.takeover_end_calls.append(takeover_ref)
            if cleanup_fails:
                raise OSError("cleanup failed")
            return {"started": True, "restoration": "preserved_user_focus"}

    backend = CommitCancellationBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    declaration = _wait_fragment(1 if terminal else 2)

    async def scenario():
        commit = await _ready_wait_fragment_commit(manager, declaration)
        task = asyncio.create_task(manager.commit_fragment_stage(commit))
        backend.manager_task = task
        await backend.commit_started.wait()
        if cancel_before_receipt:
            task.cancel()
            await asyncio.sleep(0)
            backend.release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ComputerSessionError):
            await manager.commit_fragment_stage(commit)

    run(scenario())
    assert backend.takeover_end_calls == ([] if terminal else ["takeover-1"])
    assert backend.closed == int(cleanup_fails and not terminal)


@pytest.mark.parametrize("reply", ["transport", "malformed", "application"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_fragment_commit_failure_consumes_authority_and_force_closes_failed_cleanup(
    tmp_path,
    reply,
    cleanup_fails,
):
    class CommitFailureBackend(FakeBackend):
        async def plan_actions(self, target, snapshot_id, actions, interaction_mode, *, fragment=None):
            return await super().plan_actions(target, snapshot_id, actions, interaction_mode)

        async def begin_takeover(self, snapshot_id, plan_ref, *, declaration=None):
            return "takeover-1"

        async def commit_fragment_stage(self, commit):
            if reply == "transport":
                raise OSError("transport failed")
            if reply == "application":
                error = RuntimeError("application rejected commit")
                error.error = ComputerError(ComputerErrorCode.STALE_SNAPSHOT, "stale")
                raise error
            return {"terminal": False}

        async def end_takeover(self, takeover_ref):
            self.takeover_end_calls.append(takeover_ref)
            if cleanup_fails:
                raise OSError("cleanup failed")
            return {"started": True, "restoration": "restored"}

    backend = CommitFailureBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    declaration = _wait_fragment(2)

    async def scenario():
        commit = await _ready_wait_fragment_commit(manager, declaration)
        with pytest.raises((OSError, RuntimeError, ComputerSessionError)):
            await manager.commit_fragment_stage(commit)
        with pytest.raises(ComputerSessionError):
            await manager.commit_fragment_stage(commit)

    run(scenario())
    assert backend.takeover_end_calls == ["takeover-1"]
    assert backend.closed == int(cleanup_fails)


@pytest.mark.parametrize("receipt_after_cancel", [False, True])
def test_fragment_act_cancellation_consumes_authority_and_owns_one_terminal_cleanup(
    tmp_path,
    receipt_after_cancel,
):
    class CancelledFragmentBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def plan_actions(self, target, snapshot_id, actions, interaction_mode, *, fragment=None):
            return await super().plan_actions(target, snapshot_id, actions, interaction_mode)

        async def begin_takeover(self, snapshot_id, plan_ref, *, declaration=None):
            return "takeover-1"

        async def act(self, *args, fragment_stage=None, **kwargs):
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                if receipt_after_cancel:
                    return ComputerActionResult(
                        result={"outcomes": [{"index": 0, "ok": True}], "last_acknowledged_action": 0}
                    )
                raise

    backend = CancelledFragmentBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    declaration = _single_wait_fragment()

    async def scenario():
        snapshot, stage, plan, takeover_ref = await _start_wait_fragment(manager, declaration)
        task = asyncio.create_task(manager.act(
            snapshot.snapshot_id,
            [{"type": "wait", "duration_ms": 0}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            plan_ref=plan.plan_ref,
            takeover_ref=takeover_ref,
            fragment_stage=stage,
        ))
        await backend.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.cancelled.is_set()
        with pytest.raises(ComputerSessionError):
            await manager.plan_actions(
                snapshot.snapshot_id,
                [{"type": "wait", "duration_ms": 0}],
                interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
                fragment=ForegroundFragmentPlanRequest.stage(stage),
            )

    run(scenario())
    assert backend.takeover_end_calls == ["takeover-1"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("error_code", [ComputerErrorCode.UNKNOWN_OUTCOME, ComputerErrorCode.OBSERVATION_REQUIRED])
def test_fragment_act_uncertain_transport_terminates_once_or_force_closes(tmp_path, cleanup_fails, error_code):
    class UncertainFragmentBackend(FakeBackend):
        async def plan_actions(self, target, snapshot_id, actions, interaction_mode, *, fragment=None):
            return await super().plan_actions(target, snapshot_id, actions, interaction_mode)

        async def begin_takeover(self, snapshot_id, plan_ref, *, declaration=None):
            return "takeover-1"

        async def act(self, *args, fragment_stage=None, **kwargs):
            return ComputerActionResult(error=ComputerError(
                error_code,
                "transport outcome unknown",
            ))

        async def end_takeover(self, takeover_ref):
            self.takeover_end_calls.append(takeover_ref)
            if cleanup_fails:
                raise OSError("cleanup failed")
            return {"started": True, "restoration": "restored"}

    backend = UncertainFragmentBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    declaration = _single_wait_fragment()

    async def scenario():
        snapshot, stage, plan, takeover_ref = await _start_wait_fragment(manager, declaration)
        result = await manager.act(
            snapshot.snapshot_id,
            [{"type": "wait", "duration_ms": 0}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            plan_ref=plan.plan_ref,
            takeover_ref=takeover_ref,
            fragment_stage=stage,
        )
        assert result.error.code is error_code
        with pytest.raises(ComputerSessionError):
            await manager.act(
                snapshot.snapshot_id,
                [{"type": "wait", "duration_ms": 0}],
                interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
                plan_ref=plan.plan_ref,
                takeover_ref=takeover_ref,
                fragment_stage=stage,
            )

    run(scenario())
    assert backend.takeover_end_calls == ["takeover-1"]
    assert backend.closed == int(cleanup_fails)


def test_takeover_required_background_plan_preserves_snapshot_for_foreground_replan(
    fake_backend,
    tmp_path,
):
    async def plan_actions(target, snapshot_id, actions, interaction_mode):
        fake_backend.plan_calls.append((target, snapshot_id, actions, interaction_mode))
        return ComputerActionPlan(
            plan_ref=f"plan-{len(fake_backend.plan_calls)}",
            interaction_mode=interaction_mode,
            requires_takeover=True,
            reason="foreground_takeover_required",
            action_classes=("click",),
            pid_action_classes=("click",),
        )

    fake_backend.plan_actions = plan_actions
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    actions = [{"type": "click", "element_ref": "ax-1"}]

    background = run(manager.plan_actions(
        snapshot.snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.BACKGROUND,
    ))
    foreground = run(manager.plan_actions(
        snapshot.snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
    ))

    assert background.requires_takeover is True
    assert foreground.interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER
    assert [call[3] for call in fake_backend.plan_calls] == [
        ComputerInteractionMode.BACKGROUND,
        ComputerInteractionMode.FOREGROUND_TAKEOVER,
    ]


@pytest.mark.parametrize("failure", [RuntimeError("transport"), ValueError("malformed")])
def test_failed_planning_consumes_snapshot(fake_backend, tmp_path, failure):
    async def failed_plan(*_args):
        raise failure

    fake_backend.plan_actions = failed_plan
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())

    with pytest.raises(type(failure), match=str(failure)):
        run(manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.BACKGROUND,
        ))
    with pytest.raises(ComputerSessionError, match="stale_snapshot"):
        run(manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.BACKGROUND,
        ))


def test_foreground_act_consumes_plan_and_takeover_exactly_once(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    actions = [{"type": "click", "element_ref": "ax-1"}]
    plan = run(manager.plan_actions(
        snapshot.snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
    ))
    takeover_ref = run(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))

    run(manager.act(
        snapshot.snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        plan_ref=plan.plan_ref,
        takeover_ref=takeover_ref,
    ))
    with pytest.raises(ComputerSessionError, match="stale_plan"):
        run(manager.act(
            snapshot.snapshot_id,
            actions,
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            plan_ref=plan.plan_ref,
            takeover_ref=takeover_ref,
        ))
    assert run(manager.end_takeover(takeover_ref)) == {"ended": True}
    with pytest.raises(ComputerSessionError, match="stale_takeover"):
        run(manager.end_takeover(takeover_ref))

    assert len(fake_backend.act_calls) == 1
    assert fake_backend.takeover_end_calls == [takeover_ref]


def test_foreground_dispatch_preserves_cleanup_only_reference_and_end_consumes_it(
    fake_backend,
    tmp_path,
):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    actions = [{"type": "click", "element_ref": "ax-1"}]
    plan = run(manager.plan_actions(
        snapshot.snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
    ))
    takeover_ref = run(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))

    async def failed_act(*_args, **_kwargs):
        raise RuntimeError("dispatch failed")

    fake_backend.act = failed_act
    with pytest.raises(RuntimeError, match="dispatch failed"):
        run(manager.act(
            snapshot.snapshot_id,
            actions,
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
            plan_ref=plan.plan_ref,
            takeover_ref=takeover_ref,
        ))
    assert run(manager.end_takeover(takeover_ref)) == {"ended": True}
    with pytest.raises(ComputerSessionError, match="stale_takeover"):
        run(manager.end_takeover(takeover_ref))

    fake_backend.act = FakeBackend.act.__get__(fake_backend, FakeBackend)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    plan = run(manager.plan_actions(
        snapshot.snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
    ))
    takeover_ref = run(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))
    run(manager.act(
        snapshot.snapshot_id,
        actions,
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        plan_ref=plan.plan_ref,
        takeover_ref=takeover_ref,
    ))

    async def failed_end(_takeover_ref):
        raise RuntimeError("end failed")

    fake_backend.end_takeover = failed_end
    with pytest.raises(ComputerSessionError, match="takeover_cleanup_failed"):
        run(manager.end_takeover(takeover_ref))
    with pytest.raises(ComputerSessionError, match="session_closed"):
        run(manager.apps())
    assert fake_backend.closed == 1


def test_failed_takeover_begin_consumes_plan_without_retry(fake_backend, tmp_path):
    async def failed_begin(snapshot_id, plan_ref):
        fake_backend.takeover_begin_calls.append((snapshot_id, plan_ref))
        raise HelperTransportError("takeover outcome unknown")

    fake_backend.begin_takeover = failed_begin
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    plan = run(manager.plan_actions(
        snapshot.snapshot_id,
        [{"type": "click", "element_ref": "ax-1"}],
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
    ))

    with pytest.raises(HelperTransportError, match="outcome unknown"):
        run(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))
    with pytest.raises(ComputerSessionError, match="session_closed"):
        run(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))

    assert fake_backend.takeover_begin_calls == [(snapshot.snapshot_id, plan.plan_ref)]
    assert fake_backend.closed == 1


def test_cancelled_takeover_begin_cleans_native_authority_exactly_once(fake_backend, tmp_path):
    begin_succeeded = asyncio.Event()
    deliver_response = asyncio.Event()

    async def delayed_begin(snapshot_id, plan_ref):
        fake_backend.takeover_begin_calls.append((snapshot_id, plan_ref))
        begin_succeeded.set()
        await deliver_response.wait()
        return "takeover-cancelled"

    fake_backend.begin_takeover = delayed_begin

    async def scenario():
        manager = ComputerSessionManager(
            fake_backend,
            cache_root=tmp_path,
            takeover_begin_cleanup_timeout=0.1,
        )
        await manager.select("app-1", "window-1")
        snapshot = await manager.snapshot()
        plan = await manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        )
        task = asyncio.create_task(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))
        await begin_succeeded.wait()
        task.cancel()
        deliver_response.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ComputerSessionError, match="stale_takeover"):
            await manager.end_takeover("takeover-cancelled")
        return manager

    manager = run(scenario())
    assert fake_backend.takeover_end_calls == ["takeover-cancelled"]
    assert manager._closed is False


def test_uncertain_cancelled_takeover_begin_closes_session_and_cannot_reuse(fake_backend, tmp_path):
    begin_entered = asyncio.Event()
    never_deliver = asyncio.Event()

    async def hung_begin(_snapshot_id, _plan_ref):
        begin_entered.set()
        await never_deliver.wait()
        return "takeover-leaked"

    fake_backend.begin_takeover = hung_begin

    async def scenario():
        manager = ComputerSessionManager(
            fake_backend,
            cache_root=tmp_path,
            takeover_begin_cleanup_timeout=0.01,
        )
        await manager.select("app-1", "window-1")
        snapshot = await manager.snapshot()
        plan = await manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        )
        task = asyncio.create_task(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))
        await begin_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ComputerSessionError, match="session_closed"):
            await manager.apps()
        return manager

    manager = run(scenario())
    assert fake_backend.closed == 1
    assert manager._closed is True


def test_repeated_cancellation_cannot_interrupt_owned_begin_cleanup(fake_backend, tmp_path):
    begin_entered = asyncio.Event()
    deliver_begin = asyncio.Event()
    end_entered = asyncio.Event()
    allow_end = asyncio.Event()

    async def delayed_begin(_snapshot_id, _plan_ref):
        begin_entered.set()
        await deliver_begin.wait()
        return "takeover-repeated-cancel"

    async def delayed_end(takeover_ref):
        fake_backend.takeover_end_calls.append(takeover_ref)
        end_entered.set()
        await allow_end.wait()
        return {"started": True, "restoration": "preserved_user_focus"}

    fake_backend.begin_takeover = delayed_begin
    fake_backend.end_takeover = delayed_end

    async def scenario():
        manager = ComputerSessionManager(fake_backend, cache_root=tmp_path, takeover_begin_cleanup_timeout=1)
        await manager.select("app-1", "window-1")
        snapshot = await manager.snapshot()
        plan = await manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        )
        task = asyncio.create_task(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))
        await begin_entered.wait()
        task.cancel()
        deliver_begin.set()
        await end_entered.wait()
        task.cancel()
        apps_task = asyncio.create_task(manager.apps())
        await asyncio.sleep(0)
        assert not task.done()
        assert not apps_task.done()
        allow_end.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await apps_task
        assert manager._terminal_cleanup_task is None
        assert manager._takeover_end_task is None

    run(scenario())
    assert fake_backend.takeover_end_calls == ["takeover-repeated-cancel"]


@pytest.mark.parametrize("failure", ["empty", "malformed", "transport"])
def test_non_application_begin_uncertainty_force_closes_session(fake_backend, tmp_path, failure):
    async def uncertain_begin(_snapshot_id, _plan_ref):
        if failure == "empty":
            return ""
        if failure == "malformed":
            return object()
        raise HelperTransportError("post-send response failed")

    fake_backend.begin_takeover = uncertain_begin
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    plan = run(manager.plan_actions(
        snapshot.snapshot_id,
        [{"type": "click", "element_ref": "ax-1"}],
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
    ))

    with pytest.raises((ComputerSessionError, HelperTransportError)):
        run(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))
    with pytest.raises(ComputerSessionError, match="session_closed"):
        run(manager.apps())
    run(manager.close())
    assert fake_backend.closed == 1


@pytest.mark.parametrize("failure", ["empty", "transport"])
def test_cancellation_during_uncertain_begin_cleanup_wins_after_close(
    fake_backend,
    tmp_path,
    failure,
):
    close_entered = asyncio.Event()
    allow_close = asyncio.Event()

    async def uncertain_begin(_snapshot_id, _plan_ref):
        if failure == "empty":
            return ""
        raise HelperTransportError("post-send response failed")

    async def delayed_close():
        fake_backend.closed += 1
        close_entered.set()
        await allow_close.wait()

    fake_backend.begin_takeover = uncertain_begin
    fake_backend.close = delayed_close

    async def scenario():
        manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
        await manager.select("app-1", "window-1")
        snapshot = await manager.snapshot()
        plan = await manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        )
        task = asyncio.create_task(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))
        await close_entered.wait()
        task.cancel()
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.closed
        assert manager._terminal_cleanup_task is None
        assert manager._takeover_end_task is None
        assert manager._backend_close_task is not None
        assert manager._backend_close_task.done()

    run(scenario())
    assert fake_backend.closed == 1


def test_typed_application_begin_rejection_does_not_force_close(fake_backend, tmp_path):
    from agent.runtime.macos_computer import HelperApplicationError

    async def rejected_begin(_snapshot_id, _plan_ref):
        raise HelperApplicationError(ComputerError(ComputerErrorCode.STALE_SNAPSHOT, "rejected"))

    fake_backend.begin_takeover = rejected_begin
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    plan = run(manager.plan_actions(
        snapshot.snapshot_id,
        [{"type": "click", "element_ref": "ax-1"}],
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
    ))

    with pytest.raises(HelperApplicationError):
        run(manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref))
    assert run(manager.apps()) == []
    assert fake_backend.closed == 0


def test_hung_takeover_end_is_cancelled_retrieved_and_force_closes_once(fake_backend, tmp_path):
    end_entered = asyncio.Event()
    never_finish = asyncio.Event()

    async def hung_end(_takeover_ref):
        end_entered.set()
        await never_finish.wait()

    fake_backend.end_takeover = hung_end

    async def scenario():
        manager = ComputerSessionManager(fake_backend, cache_root=tmp_path, takeover_begin_cleanup_timeout=0.01)
        await manager.select("app-1", "window-1")
        snapshot = await manager.snapshot()
        plan = await manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        )
        takeover_ref = await manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref)
        with pytest.raises(ComputerSessionError, match="takeover_cleanup_failed"):
            await manager.end_takeover(takeover_ref)
        assert end_entered.is_set()
        assert manager._terminal_cleanup_task is None
        assert manager._takeover_end_task is None
        assert manager._backend_close_task is not None
        assert manager._backend_close_task.done()
        with pytest.raises(ComputerSessionError, match="session_closed"):
            await manager.apps()
        await manager.close()

    run(scenario())
    assert fake_backend.closed == 1


def test_timed_out_takeover_end_closes_transport_before_retrieving_late_exception(
    fake_backend,
    tmp_path,
):
    end_entered = asyncio.Event()
    transport_closed = asyncio.Event()

    async def close_transport():
        fake_backend.closed += 1
        transport_closed.set()

    async def end_blocked_on_transport(_takeover_ref):
        end_entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await transport_closed.wait()
            raise RuntimeError("late end failure")

    fake_backend.close = close_transport
    fake_backend.end_takeover = end_blocked_on_transport

    async def scenario():
        manager = ComputerSessionManager(
            fake_backend,
            cache_root=tmp_path,
            takeover_begin_cleanup_timeout=0.01,
        )
        await manager.select("app-1", "window-1")
        snapshot = await manager.snapshot()
        plan = await manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        )
        takeover_ref = await manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref)
        cleanup = asyncio.create_task(manager.end_takeover(takeover_ref))
        await end_entered.wait()
        try:
            await asyncio.sleep(0.03)
            assert transport_closed.is_set()
        finally:
            transport_closed.set()
            with pytest.raises(ComputerSessionError, match="takeover_cleanup_failed"):
                await cleanup
        assert manager._terminal_cleanup_task is None
        assert manager._takeover_end_task is None
        assert manager._backend_close_task is not None
        assert manager._backend_close_task.done()

    run(scenario())
    assert fake_backend.closed == 1


def test_cancellation_wins_after_owned_takeover_end_fails_and_closes(fake_backend, tmp_path):
    end_entered = asyncio.Event()
    allow_end_failure = asyncio.Event()

    async def delayed_failed_end(_takeover_ref):
        end_entered.set()
        await allow_end_failure.wait()
        raise RuntimeError("end failed")

    fake_backend.end_takeover = delayed_failed_end

    async def scenario():
        manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
        await manager.select("app-1", "window-1")
        snapshot = await manager.snapshot()
        plan = await manager.plan_actions(
            snapshot.snapshot_id,
            [{"type": "click", "element_ref": "ax-1"}],
            interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        )
        takeover_ref = await manager.begin_takeover(snapshot.snapshot_id, plan.plan_ref)
        task = asyncio.create_task(manager.end_takeover(takeover_ref))
        await end_entered.wait()
        task.cancel()
        allow_end_failure.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.closed
        assert manager._terminal_cleanup_task is None
        assert manager._takeover_end_task is None
        assert manager._backend_close_task is not None
        assert manager._backend_close_task.done()

    run(scenario())
    assert fake_backend.closed == 1


def test_handoff_requires_fresh_catalog_and_exact_suspended_refs_before_resume(
    fake_backend,
    tmp_path,
):
    fake_backend.apps_result = [{
        "app_ref": "app-1",
        "windows": [{
            "window_ref": "window-1",
            "bindable": True,
            "window_identity_ref": "identity-a",
        }],
    }]
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("app-1", "window-1"))
    run(manager.snapshot())
    manager.grants.add("computer-write:approved")

    run(manager.handoff())

    assert manager.grants == set()
    assert manager.suspended_target == ComputerTarget("app-1", "window-1")
    with pytest.raises(ComputerSessionError, match="fresh_apps_required"):
        run(manager.resume("resume-before-fresh-apps"))

    run(manager.apps())
    publication_id = "resume-with-mandatory-observation"
    resumed = run(manager.resume(publication_id))
    fresh = run(manager.snapshot(resume_publication_id=publication_id))
    run(manager.commit_resume_publication(publication_id))

    assert resumed == ComputerTarget("app-1", "window-1")
    assert fresh.snapshot_id == "snap-2"


def _robustness_vanished_menu(fake_backend, tmp_path, *, confirmed_absent=True, unrelated_before=False):
    parent = {"window_ref": "parent", "bindable": True, "window_identity_ref": "identity-parent"}
    menu = {"window_ref": "menu", "bindable": True, "window_identity_ref": "identity-menu"}
    fake_backend.apps_result = [{"app_ref": "app", "windows": [parent, menu]}]
    if unrelated_before:
        fake_backend.apps_result.append({"app_ref": "unrelated", "windows": [{
            "bindable": False, "reason": "ax_window_unmatched",
        }]})
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("app", "menu"))
    run(manager.snapshot())
    run(manager.handoff())
    fake_backend.apps_result = [{"app_ref": "app", "windows": [parent]}]
    fake_backend.confirmed_absent_window_identity_refs = ("identity-menu",) if confirmed_absent else ()
    return manager


@pytest.mark.parametrize("catalog_case", ["unrelated_before", "unrelated_after", "no_witness", "no_visible_apps"])
def test_robustness_exact_absence_does_not_require_unrelated_bindability_or_witness(
    fake_backend, tmp_path, catalog_case,
):
    manager = _robustness_vanished_menu(
        fake_backend, tmp_path, unrelated_before=catalog_case == "unrelated_before",
    )
    if catalog_case == "unrelated_after":
        fake_backend.apps_result.append({"app_ref": "unrelated", "windows": [{
            "bindable": False, "reason": "ax_window_unmatched",
        }]})
    elif catalog_case == "no_witness":
        fake_backend.apps_result[0]["windows"][0]["window_identity_ref"] = "new-window"
    elif catalog_case == "no_visible_apps":
        # The bounded UI catalog can be empty while the full WindowServer
        # inventory is nonempty and proves the old exact identity is gone.
        fake_backend.apps_result = []
    run(manager.apps())
    assert run(manager.resume("native-proof-only")) is None
    assert manager.handed_off and manager.target is None
    assert manager.grants == set() and fake_backend.select_calls == [("app", "menu")]
    run(manager.commit_resume_publication("native-proof-only"))
    assert not manager.handed_off and manager.suspended_target is None


@pytest.mark.parametrize("omission", ["filtered_offscreen", "catalog_truncated"])
def test_robustness_omitted_window_without_absence_proof_keeps_handoff(fake_backend, tmp_path, omission):
    manager = _robustness_vanished_menu(fake_backend, tmp_path, confirmed_absent=False)
    # Both eligible-window filtering and catalog limits can omit a still-live
    # menu while retaining a same-helper parent identity. Neither proves exit.
    run(manager.apps())
    with pytest.raises(ComputerSessionError, match="target_gone"):
        run(manager.resume(f"unproven-{omission}"))
    assert manager.handed_off and manager.suspended_target == ComputerTarget("app", "menu")
    assert manager._resume_publication_id is None
    assert fake_backend.select_calls == [("app", "menu")]


@pytest.mark.parametrize("proof", [None, [], ["identity"], ("",), ("x" * 257,), ("\ud800",), (1,), ([],), ("same", "same"), tuple(f"identity-{i}" for i in range(201))])
def test_robustness_absence_proof_rejects_malformed_inventory(proof):
    with pytest.raises(ValueError, match="confirmed_absent_window_identity_refs"):
        ComputerAppCatalog(1, (), confirmed_absent_window_identity_refs=proof)


def test_robustness_absence_proof_default_and_bounded_inventory():
    assert ComputerAppCatalog(1, ()).confirmed_absent_window_identity_refs == ()
    proof = tuple(f"identity-{i}" for i in range(200))
    assert ComputerAppCatalog(1, (), confirmed_absent_window_identity_refs=proof).confirmed_absent_window_identity_refs == proof


@pytest.mark.parametrize("proof", [(), ("identity-other",)])
def test_robustness_absence_proof_must_be_latest_and_exact(fake_backend, tmp_path, proof):
    manager = _robustness_vanished_menu(fake_backend, tmp_path)
    run(manager.apps())
    fake_backend.confirmed_absent_window_identity_refs = proof
    run(manager.apps())
    with pytest.raises(ComputerSessionError, match="target_gone"):
        run(manager.resume("expired-or-unrelated-proof"))
    assert manager.handed_off and manager._resume_publication_id is None


def test_robustness_absent_resume_is_unbound_only_after_publication(fake_backend, tmp_path):
    manager = _robustness_vanished_menu(fake_backend, tmp_path)
    run(manager.apps())
    assert manager.handed_off and manager.suspended_target == ComputerTarget("app", "menu")
    assert run(manager.resume("absent-publication")) is None
    assert manager.handed_off and manager.target is None
    assert manager._latest_snapshot is None and manager._pending_plan is None
    assert manager.grants == set()
    assert fake_backend.select_calls == [("app", "menu")]
    run(manager.commit_resume_publication("absent-publication"))
    assert not manager.handed_off and manager.suspended_target is None
    assert manager._suspended_window_identity_ref is None
    with pytest.raises(ComputerSessionError, match="stale_resume_publication"):
        run(manager.commit_resume_publication("absent-publication"))
    run(manager.select("app", "parent"))
    run(manager.abort_resume_publication("absent-publication"))
    assert manager.target == ComputerTarget("app", "parent")


def test_robustness_absent_resume_abort_retains_handoff(fake_backend, tmp_path):
    manager = _robustness_vanished_menu(fake_backend, tmp_path)
    run(manager.apps())
    assert run(manager.resume("abort-absent")) is None
    run(manager.abort_resume_publication("abort-absent"))
    assert manager.handed_off and manager.suspended_target == ComputerTarget("app", "menu")
    assert manager.target is None and manager.grants == set()
    with pytest.raises(ComputerSessionError, match="fresh_apps_required"):
        run(manager.resume("retry-absent"))


def test_robustness_absent_publication_refresh_blocks_late_commit_and_abort(fake_backend, tmp_path):
    manager = _robustness_vanished_menu(fake_backend, tmp_path)
    run(manager.apps())
    assert run(manager.resume("old-absence")) is None
    with pytest.raises(ComputerSessionError, match="stale_resume_publication"):
        run(manager.resume("overlapping-absence"))
    run(manager.apps())
    with pytest.raises(ComputerSessionError, match="stale_resume_publication"):
        run(manager.commit_resume_publication("old-absence"))
    assert run(manager.resume("new-absence")) is None
    run(manager.abort_resume_publication("old-absence"))
    manager.validate_unbound_resume_publication("new-absence")
    assert manager.handed_off
    run(manager.commit_resume_publication("new-absence"))
    assert not manager.handed_off and manager.target is None


def _unbound_after_catalog_refresh(fake_backend, tmp_path):
    fake_backend.apps_result = [{"app_ref": "app", "windows": [
        {"window_ref": "window", "bindable": True, "window_identity_ref": "identity-window"},
    ]}]
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("app", "window"))
    run(manager.snapshot())
    # A catalog refresh intentionally revokes the ordinary binding.
    run(manager.apps())
    manager.grants.add("stale-grant")
    assert manager.target is None and not manager.handed_off
    return manager


@pytest.mark.parametrize("pause", ["handoff", "pause_for_user_activity"])
def test_targetless_stop_needs_no_window_selection_or_capture(fake_backend, tmp_path, pause):
    manager = _unbound_after_catalog_refresh(fake_backend, tmp_path)
    native = (list(fake_backend.select_calls), list(fake_backend.snapshot_calls), list(fake_backend.act_calls))
    run(getattr(manager, pause)())
    assert manager.handed_off and manager.suspended_target is None and manager.target is None
    assert manager.grants == set()
    assert manager._latest_snapshot is None and manager._pending_plan is None
    run(manager.handoff())
    assert manager.handed_off and manager.suspended_target is None
    assert (fake_backend.select_calls, fake_backend.snapshot_calls, fake_backend.act_calls) == native


def test_targetless_resume_releases_only_into_an_unbound_session(fake_backend, tmp_path):
    manager = _unbound_after_catalog_refresh(fake_backend, tmp_path)
    run(manager.handoff())
    selects = list(fake_backend.select_calls)
    with pytest.raises(ComputerSessionError, match="handoff_inactive"):
        run(manager.resume("bound-release"))
    run(manager.resume_unbound("targetless-release"))
    assert manager.handed_off
    manager.validate_targetless_resume_publication("targetless-release")
    run(manager.commit_resume_publication("targetless-release"))
    assert not manager.handed_off and manager.target is None and manager.grants == set()
    assert fake_backend.select_calls == selects
    with pytest.raises(ComputerSessionError, match="target_required"):
        run(manager.snapshot())


def test_targetless_resume_abort_or_newer_stop_keeps_user_control(fake_backend, tmp_path):
    manager = _unbound_after_catalog_refresh(fake_backend, tmp_path)
    with pytest.raises(ComputerSessionError, match="handoff_inactive"):
        run(manager.resume_unbound("not-paused"))
    run(manager.handoff())
    run(manager.resume_unbound("aborted-release"))
    run(manager.abort_resume_publication("aborted-release"))
    assert manager.handed_off and manager.suspended_target is None and manager.grants == set()
    with pytest.raises(ComputerSessionError, match="stale_resume_publication"):
        run(manager.commit_resume_publication("aborted-release"))
    run(manager.resume_unbound("late-release"))
    with pytest.raises(ComputerSessionError, match="stale_resume_publication"):
        run(manager.resume_unbound("overlapping-release"))
    run(manager.handoff())
    with pytest.raises(ComputerSessionError, match="stale_resume_publication"):
        run(manager.commit_resume_publication("late-release"))
    with pytest.raises(ComputerSessionError, match="stale_resume_publication"):
        manager.validate_targetless_resume_publication("late-release")
    assert manager.handed_off


def test_repeated_handoff_during_absence_publication_keeps_bound_continuity(fake_backend, tmp_path):
    manager = _robustness_vanished_menu(fake_backend, tmp_path)
    run(manager.apps())
    assert run(manager.resume("absence-release")) is None
    run(manager.handoff())
    assert manager.handed_off and manager.suspended_target == ComputerTarget("app", "menu")
    assert manager._suspended_window_identity_ref == "identity-menu"
    with pytest.raises(ComputerSessionError, match="stale_resume_publication"):
        run(manager.commit_resume_publication("absence-release"))
    with pytest.raises(ComputerSessionError, match="handoff_inactive"):
        run(manager.resume_unbound("wrong-mode"))
    assert manager.handed_off


@pytest.mark.parametrize("uncertainty", ["empty", "missing_identity", "duplicate", "invalid_refs", "duplicate_refs", "unbindable", "restart", "catalog_error", "cancelled"])
def test_robustness_resume_uncertainty_never_releases_handoff(fake_backend, tmp_path, uncertainty):
    # Missing or changed UI entries alone remain insufficient. The one case
    # with a native proof below conflicts with a still-listed exact identity.
    manager = _robustness_vanished_menu(fake_backend, tmp_path, confirmed_absent=uncertainty == "unbindable")
    parent = fake_backend.apps_result[0]["windows"][0]
    if uncertainty == "empty":
        fake_backend.apps_result = []
    elif uncertainty == "missing_identity":
        parent.pop("window_identity_ref")
    elif uncertainty == "duplicate":
        fake_backend.apps_result[0]["windows"].append(dict(parent, window_ref="duplicate"))
    elif uncertainty == "invalid_refs":
        parent["window_ref"] = ""
    elif uncertainty == "duplicate_refs":
        fake_backend.apps_result[0]["windows"].append(dict(parent, window_identity_ref="different-identity"))
    elif uncertainty == "unbindable":
        fake_backend.apps_result[0]["windows"].append({
            "window_ref": "menu-new", "bindable": False, "window_identity_ref": "identity-menu",
        })
    elif uncertainty == "restart":
        parent["window_identity_ref"] = "new-helper-identity"
    else:
        # A previous successful refresh must not survive an unsuccessful one.
        run(manager.apps())

        async def failed_apps():
            if uncertainty == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("enumeration failed")

        fake_backend.apps = failed_apps
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            run(manager.apps())
    if uncertainty not in {"catalog_error", "cancelled"}:
        run(manager.apps())
    with pytest.raises(ComputerSessionError):
        run(manager.resume("uncertain-publication"))
    assert manager.handed_off and manager.suspended_target == ComputerTarget("app", "menu")
    assert fake_backend.select_calls == [("app", "menu")]


def test_resume_rejects_catalog_without_exact_suspended_refs(fake_backend, tmp_path):
    fake_backend.apps_result = [{
        "app_ref": "app-1",
        "windows": [{
            "window_ref": "window-1",
            "bindable": True,
            "window_identity_ref": "identity-a",
        }],
    }]
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("app-1", "window-1"))
    run(manager.handoff())
    fake_backend.apps_result = [{
        "app_ref": "app-1",
        "windows": [{"window_ref": "different-window"}],
    }]

    run(manager.apps())

    with pytest.raises(ComputerSessionError, match="target_gone"):
        run(manager.resume("resume-missing-target"))


def test_resume_rotating_refs_uses_exact_helper_lifetime_identity(fake_backend, tmp_path):
    fake_backend.apps_result = [{
        "app_ref": "old-app",
        "windows": [{
            "window_ref": "old-window",
            "bindable": True,
            "window_identity_ref": "identity-a",
        }],
    }]
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("old-app", "old-window"))
    run(manager.handoff())

    assert manager.suspended_target == ComputerTarget("old-app", "old-window")
    fake_backend.apps_result = [{
        "app_ref": "new-app",
        "windows": [{
            "window_ref": "new-window",
            "bindable": True,
            "window_identity_ref": "identity-a",
        }],
    }]

    run(manager.apps())

    assert run(_resume_and_commit_for_test(manager)) == ComputerTarget("new-app", "new-window")
    assert fake_backend.select_calls == [
        ("old-app", "old-window"),
        ("new-app", "new-window"),
    ]


@pytest.mark.parametrize(
    ("initial_window", "fresh_apps"),
    [
        (
            {
                "window_ref": "old-window",
                "bindable": True,
                "window_identity_ref": "identity-a",
            },
            [{
                "app_ref": "new-app",
                "windows": [{"window_ref": "new-window", "bindable": True}],
            }],
        ),
        (
            {
                "window_ref": "old-window",
                "bindable": True,
                "window_identity_ref": "identity-a",
            },
            [
                {
                    "app_ref": "new-app-a",
                    "windows": [{
                        "window_ref": "new-window-a",
                        "bindable": True,
                        "window_identity_ref": "identity-a",
                    }],
                },
                {
                    "app_ref": "new-app-b",
                    "windows": [{
                        "window_ref": "new-window-b",
                        "bindable": True,
                        "window_identity_ref": "identity-a",
                    }],
                },
            ],
        ),
        (
            {
                "window_ref": "old-window",
                "bindable": True,
                "window_identity_ref": "x" * 257,
            },
            [{
                "app_ref": "new-app",
                "windows": [{
                    "window_ref": "new-window",
                    "bindable": True,
                    "window_identity_ref": "x" * 257,
                }],
            }],
        ),
        (
            {
                "window_ref": "old-window",
                "bindable": True,
                "window_identity_ref": "identity-a",
            },
            [{
                "app_ref": "new-app",
                "windows": [{
                    "window_ref": "new-window",
                    "bindable": False,
                    "window_identity_ref": "identity-a",
                }],
            }],
        ),
        (
            {
                "window_ref": "old-window",
                "bindable": True,
                "window_identity_ref": "identity-a",
            },
            [{
                "app_ref": "new-app",
                "windows": [{
                    "window_ref": "new-window",
                    "bindable": True,
                    "window_identity_ref": "identity-after-helper-restart",
                }],
            }],
        ),
    ],
    ids=["missing", "duplicate", "malformed", "unbindable", "helper-restart"],
)
def test_resume_identity_failures_never_select_old_refs(
    fake_backend,
    tmp_path,
    initial_window,
    fresh_apps,
):
    fake_backend.apps_result = [{"app_ref": "old-app", "windows": [initial_window]}]
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("old-app", "old-window"))
    run(manager.handoff())
    fake_backend.apps_result = fresh_apps

    run(manager.apps())

    with pytest.raises(ComputerSessionError, match="target_gone"):
        run(manager.resume("resume-identity-failure"))
    assert fake_backend.select_calls == [("old-app", "old-window")]


def _prepare_rotating_resume(fake_backend, tmp_path):
    fake_backend.apps_result = [{
        "app_ref": "old-app",
        "windows": [{
            "window_ref": "old-window",
            "bindable": True,
            "window_identity_ref": "identity-a",
        }],
    }]
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("old-app", "old-window"))
    run(manager.handoff())
    fake_backend.apps_result = [{
        "app_ref": "new-app",
        "windows": [{
            "window_ref": "new-window",
            "bindable": True,
            "window_identity_ref": "identity-a",
        }],
    }]
    run(manager.apps())
    return manager


def test_resume_select_exception_consumes_candidate_until_fresh_apps(fake_backend, tmp_path):
    manager = _prepare_rotating_resume(fake_backend, tmp_path)
    original_select = fake_backend.select
    attempts = []

    async def fail_select(app_ref, window_ref):
        attempts.append((app_ref, window_ref))
        assert manager._resume_candidate is None
        assert manager._resume_catalog_refreshed is False
        raise RuntimeError("select failed")

    fake_backend.select = fail_select

    with pytest.raises(RuntimeError, match="select failed"):
        run(manager.resume("resume-select-failure"))
    with pytest.raises(ComputerSessionError, match="fresh_apps_required"):
        run(manager.resume("resume-select-retry"))

    assert attempts == [("new-app", "new-window")]
    assert manager.suspended_target == ComputerTarget("old-app", "old-window")
    assert manager._suspended_window_identity_ref == "identity-a"

    run(manager.apps())
    fake_backend.select = original_select
    assert run(_resume_and_commit_for_test(manager)) == ComputerTarget("new-app", "new-window")


def test_resume_wrong_target_consumes_candidate_until_fresh_apps(fake_backend, tmp_path):
    manager = _prepare_rotating_resume(fake_backend, tmp_path)
    attempts = []

    async def wrong_target(app_ref, window_ref):
        attempts.append((app_ref, window_ref))
        assert manager._resume_candidate is None
        assert manager._resume_catalog_refreshed is False
        return ComputerTarget("wrong-app", "wrong-window")

    fake_backend.select = wrong_target

    with pytest.raises(ComputerSessionError, match="target_gone"):
        run(manager.resume("resume-wrong-target"))
    with pytest.raises(ComputerSessionError, match="fresh_apps_required"):
        run(manager.resume("resume-wrong-target-retry"))

    assert attempts == [("new-app", "new-window")]
    assert manager.suspended_target == ComputerTarget("old-app", "old-window")
    assert manager._suspended_window_identity_ref == "identity-a"


def test_resume_cancellation_consumes_candidate_until_fresh_apps(fake_backend, tmp_path):
    async def scenario():
        fake_backend.apps_result = [{
            "app_ref": "old-app",
            "windows": [{
                "window_ref": "old-window",
                "bindable": True,
                "window_identity_ref": "identity-a",
            }],
        }]
        manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
        await manager.apps()
        await manager.select("old-app", "old-window")
        await manager.handoff()
        fake_backend.apps_result = [{
            "app_ref": "new-app",
            "windows": [{
                "window_ref": "new-window",
                "bindable": True,
                "window_identity_ref": "identity-a",
            }],
        }]
        await manager.apps()
        entered = asyncio.Event()
        attempts = []
        observed_resume_state = []

        async def blocked_select(app_ref, window_ref):
            attempts.append((app_ref, window_ref))
            observed_resume_state.append((
                manager._resume_candidate,
                manager._resume_catalog_refreshed,
            ))
            if len(attempts) > 1:
                raise AssertionError("stale resume candidate was replayed")
            entered.set()
            await asyncio.Future()

        fake_backend.select = blocked_select
        task = asyncio.create_task(manager.resume("resume-cancelled"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ComputerSessionError, match="fresh_apps_required"):
            await manager.resume("resume-after-cancellation")

        assert attempts == [("new-app", "new-window")]
        assert observed_resume_state == [(None, False)]
        assert manager.suspended_target == ComputerTarget("old-app", "old-window")
        assert manager._suspended_window_identity_ref == "identity-a"

    run(scenario())


def test_act_consumes_snapshot(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())
    plan_and_act(manager, snapshot.snapshot_id, [{"type": "click", "element_ref": "ax-1"}])
    with pytest.raises(ComputerSessionError, match="stale_snapshot"):
        plan_and_act(manager, snapshot.snapshot_id, [{"type": "click", "element_ref": "ax-1"}])


def test_python_rejects_coordinates_outside_recorded_capture_bounds_before_dispatch(tmp_path):
    class BoundedBackend(FakeBackend):
        async def snapshot(self, target, scope, artifact):
            self.snapshot_calls.append((target, scope))
            return ComputerSnapshot("bounded", {
                "capture_bounds": {"x": 0, "y": 0, "width": 100, "height": 50},
            })

    backend = BoundedBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())

    with pytest.raises(ComputerSessionError, match="out_of_bounds"):
        plan_and_act(manager, snapshot.snapshot_id, [{
            "type": "click",
            "x": 101,
            "y": 20,
            "target_element_ref": "bounded:canvas",
        }])

    assert backend.act_calls == []


class ArtifactBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.artifacts: list[ComputerArtifact] = []
        self.bound_directories: list[int] = []
        self.bound_directory_paths: list[Path] = []

    async def bind_artifact_directory(self, directory_fd):
        self.bound_directories.append(directory_fd)

    async def bind_artifact_directory_path(self, directory_fd, directory_path):
        self.bound_directories.append(directory_fd)
        self.bound_directory_paths.append(Path(directory_path))

    async def snapshot(self, target, scope, artifact):
        self.snapshot_calls.append((target, scope))
        self.artifacts.append(artifact)
        return ComputerSnapshot("snap-artifact", {"image_artifact": artifact.filename})


def test_snapshot_allocates_private_model_independent_artifact(tmp_path):
    backend = ArtifactBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))

    snapshot = run(manager.snapshot())

    assert snapshot.payload["image_artifact"] == backend.artifacts[0].filename
    assert backend.bound_directory_paths == [manager.session_dir]
    assert backend.artifacts[0].directory_path == manager.session_dir
    assert backend.artifacts[0].filename.startswith("snapshot-")
    assert "/" not in backend.artifacts[0].filename
    assert backend.artifacts[0].directory_fd == manager._session_fd
    assert backend.bound_directories == [manager._session_fd]


def test_focus_handoff_resume_and_act_invalidate_old_references(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    first = run(manager.snapshot())
    fake_backend.apps_result = [{
        "app_ref": "app-2",
        "windows": [{
            "window_ref": "window-2",
            "bindable": True,
            "window_identity_ref": "identity-2",
        }],
    }]
    run(manager.apps())
    run(manager.select("app-2", "window-2"))
    with pytest.raises(ComputerSessionError, match="stale_snapshot"):
        plan_and_act(manager, first.snapshot_id, [{"type": "click", "element_ref": "ax-1"}])

    second = run(manager.snapshot())
    run(manager.handoff())
    with pytest.raises(ComputerSessionError, match="handoff_active"):
        plan_and_act(manager, second.snapshot_id, [{"type": "click", "element_ref": "ax-1"}])

    fake_backend.apps_result = [{
        "app_ref": "app-2",
        "windows": [{
            "window_ref": "window-2",
            "bindable": True,
            "window_identity_ref": "identity-2",
        }],
    }]
    run(manager.apps())
    run(_resume_and_commit_for_test(manager))
    with pytest.raises(ComputerSessionError, match="stale_snapshot"):
        plan_and_act(manager, second.snapshot_id, [{"type": "click", "element_ref": "ax-1"}])
    assert fake_backend.select_calls == [("app-1", "window-1"), ("app-2", "window-2"), ("app-2", "window-2")]


def test_failed_select_still_invalidates_the_previous_snapshot(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())

    async def failed_select(_app_ref, _window_ref):
        raise RuntimeError("window changed")

    fake_backend.select = failed_select
    with pytest.raises(RuntimeError, match="window changed"):
        run(manager.select("app-2", "window-2"))
    with pytest.raises(ComputerSessionError, match="stale_snapshot"):
        plan_and_act(manager, snapshot.snapshot_id, [{"type": "click", "element_ref": "ax-1"}])


def test_failed_snapshot_consumes_the_previous_snapshot(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    old = run(manager.snapshot())

    async def failed_snapshot(_target, _scope, _artifact):
        raise RuntimeError("capture failed")

    fake_backend.snapshot = failed_snapshot
    with pytest.raises(RuntimeError, match="capture failed"):
        run(manager.snapshot())
    with pytest.raises(ComputerSessionError, match="stale_snapshot"):
        plan_and_act(manager, old.snapshot_id, [{"type": "click", "element_ref": "ax-1"}])


def _identity_catalog() -> list[dict[str, object]]:
    return [
        {
            "app_ref": "app-x",
            "bundle_id": "dev.astra.computer-fixture",
            "app_version": "1.0.0",
            "name": "AstraComputerFixture",
            "windows": [
                {
                    "window_ref": "win-y",
                    "title": "Astra Computer Fixture",
                    "document_path": "/tmp/astra-fullflow/AstraComputerFixture.app",
                    "bounds": {"x": 100, "y": 170, "width": 717, "height": 712},
                }
            ],
        }
    ]


class AppStateBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.catalog_generation = 1
        self.apps_result = _identity_catalog()
        self.apps_calls = 0
        self.get_state_calls: list[
            tuple[
                ComputerTarget,
                str,
                ComputerArtifact,
                ComputerSnapshotTextDetailMode,
                ComputerArtifact | None,
            ]
        ] = []
        self.state_error: BaseException | None = None
        self.publish_image = True
        self.fail_after_image = False
        self.returned_target: ComputerTarget | None = None
        self.returned_generation: int | None = None
        self.image_data = _SMART_SNAPSHOT_PNG
        self.image_mode = 0o600
        self.pixel_size: dict[str, int] = {"width": 1, "height": 1}
        self.payload_mutator = lambda payload: payload
        self.detail_mutator = lambda document: document
        self.manager: ComputerSessionManager | None = None
        self.replace_manager_catalog_generation: int | None = None

    async def apps(self) -> ComputerAppCatalog:
        self.apps_calls += 1
        return ComputerAppCatalog(self.catalog_generation, tuple(self.apps_result))

    async def get_app_state(
        self,
        target,
        scope,
        artifact,
        *,
        text_detail=ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact=None,
    ):
        self.get_state_calls.append(
            (target, scope, artifact, text_detail, text_detail_artifact)
        )
        if self.state_error is not None:
            raise self.state_error
        if self.publish_image:
            _write_helper_artifact(artifact, self.image_data, mode=self.image_mode)
        if self.fail_after_image:
            raise RuntimeError("helper failed after image publication")
        snapshot_id = f"state-{len(self.get_state_calls)}"
        payload: dict[str, object] = {
            "image_artifact": artifact.filename,
            "logical_size": {"width": 1, "height": 1},
            "pixel_size": dict(self.pixel_size),
            "backing_scale": 1,
            "capture_bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
            "ax_tree": {"element_ref": f"{snapshot_id}:root"},
        }
        if text_detail is ComputerSnapshotTextDetailMode.ON:
            assert text_detail_artifact is not None
            document = self.detail_mutator(json.loads(_smart_snapshot_detail(snapshot_id)))
            detail = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            _write_helper_artifact(text_detail_artifact, detail)
            stats = document["stats"]
            payload.update({
                "text_detail_artifact": text_detail_artifact.filename,
                "text_detail_metadata": {
                    "schema_version": 1,
                    "snapshot_id": snapshot_id,
                    "coverage": "reported_ax_subtree",
                    "node_count": stats["node_count"],
                    "max_depth_observed": stats["max_depth_observed"],
                    "byte_count": len(detail),
                    "sha256": hashlib.sha256(detail).hexdigest(),
                    "truncated": stats["truncated"],
                    "truncation_reasons": stats["truncation_reasons"],
                },
            })
        payload = self.payload_mutator(payload)
        if self.replace_manager_catalog_generation is not None:
            assert self.manager is not None
            self.manager._last_catalog = ComputerAppCatalog(
                self.replace_manager_catalog_generation,
                tuple(self.apps_result),
            )
        return ComputerBackendAppState(
            catalog_generation=(
                self.catalog_generation
                if self.returned_generation is None
                else self.returned_generation
            ),
            target=self.returned_target or target,
            snapshot=ComputerSnapshot(snapshot_id, payload),
        )


@pytest.mark.parametrize(
    ("catalog", "app_ref", "window_ref"),
    [
        (None, "app-x", "win-y"),
        (_identity_catalog(), "old-app", "old-window"),
        (_identity_catalog(), "app-x", "foreign-window"),
        (_identity_catalog(), "", "win-y"),
        (_identity_catalog(), "app-x", ""),
    ],
)
def test_manager_get_app_state_requires_exact_latest_catalog_pair_before_dispatch(
    tmp_path,
    catalog,
    app_ref,
    window_ref,
):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    if catalog is not None:
        backend.apps_result = catalog
        run(manager.apps())
    manager._latest_snapshot = ComputerSnapshot("old")
    manager._pending_plan = object()
    manager._takeover_ref = "old-takeover"
    manager._fragment_declaration = object()
    manager.grants.add("old-grant")

    with pytest.raises(ComputerSessionError, match="catalog_required"):
        run(manager.get_app_state(app_ref, window_ref))

    assert backend.get_state_calls == []
    assert manager._latest_snapshot is None
    assert manager._pending_plan is None
    assert manager._takeover_ref is None
    assert manager._fragment_declaration is None
    assert manager.grants == set()


def test_manager_get_app_state_rejects_refs_from_replaced_catalog_before_dispatch(tmp_path):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    backend.catalog_generation = 2
    backend.apps_result = [{
        **_identity_catalog()[0],
        "app_ref": "app-new",
        "windows": [{
            **_identity_catalog()[0]["windows"][0],
            "window_ref": "win-new",
        }],
    }]
    run(manager.apps())

    with pytest.raises(ComputerSessionError, match="catalog_required"):
        run(manager.get_app_state("app-x", "win-y"))

    assert backend.get_state_calls == []


@pytest.mark.parametrize("mismatch", ["target", "generation"])
def test_manager_get_app_state_rejects_crossed_helper_or_commit_state_without_commit(
    tmp_path,
    mismatch,
):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    backend.manager = manager
    run(manager.apps())
    run(manager.select("old-app", "old-window"))
    generation = manager._target_generation
    backend.publish_image = False
    if mismatch == "target":
        backend.returned_target = ComputerTarget("other-app", "other-window")
    elif mismatch == "generation":
        backend.returned_generation = 2

    with pytest.raises(ComputerSessionError, match="session_state_changed"):
        run(manager.get_app_state("app-x", "win-y"))

    assert manager.target == ComputerTarget("old-app", "old-window")
    assert manager._target_generation == generation
    assert manager._latest_snapshot is None


@pytest.mark.parametrize("mismatch", ["target", "generation", "manager_catalog"])
def test_manager_get_app_state_crossed_state_after_publication_is_unsafe_and_poisoned(
    tmp_path,
    mismatch,
):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    backend.manager = manager
    run(manager.apps())
    run(manager.select("old-app", "old-window"))
    generation = manager._target_generation
    if mismatch == "target":
        backend.returned_target = ComputerTarget("other-app", "other-window")
    elif mismatch == "generation":
        backend.returned_generation = 2
    else:
        backend.replace_manager_catalog_generation = 2

    with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
        run(manager.get_app_state("app-x", "win-y"))

    assert manager.target is None
    assert manager._target_identity is None
    assert manager._latest_snapshot is None
    assert manager._artifact_poisoned is True
    assert manager._target_generation == generation


def test_manager_get_app_state_uncertain_failure_inventory_scan_is_unsafe_and_poisoned(
    tmp_path,
    monkeypatch,
):
    backend = AppStateBackend()
    backend.publish_image = False
    backend.returned_generation = 2
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    real_inventory = manager._snapshot_artifact_inventory
    scans = 0

    def uncertain_inventory():
        nonlocal scans
        scans += 1
        if scans == 2:
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: simulated uncertain inventory rescan",
            )
        return real_inventory()

    monkeypatch.setattr(manager, "_snapshot_artifact_inventory", uncertain_inventory)

    with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
        run(manager.get_app_state("app-x", "win-y"))

    assert manager.target is None
    assert manager._artifact_poisoned is True


@pytest.mark.parametrize("error_code", ["unsafe_artifact", "unsafe_cache"])
def test_manager_get_app_state_first_inventory_scan_failure_is_terminal_before_dispatch(
    tmp_path,
    monkeypatch,
    error_code,
):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("app-x", "win-y"))
    manager._latest_snapshot = ComputerSnapshot("old")
    manager._latest_snapshot_scope = "target_window"
    manager._pending_plan = object()
    manager.grants.add("old-grant")
    generation = manager._target_generation
    apps_calls = backend.apps_calls

    def fail_first_inventory():
        raise ComputerSessionError(
            error_code,
            f"{error_code}: simulated initial inventory uncertainty",
        )

    monkeypatch.setattr(manager, "_snapshot_artifact_inventory", fail_first_inventory)

    with pytest.raises(ComputerSessionError, match="unsafe_artifact") as exc_info:
        run(manager.get_app_state("app-x", "win-y"))

    assert exc_info.value.code == "unsafe_artifact"
    assert backend.get_state_calls == []
    assert manager.target is None
    assert manager._target_identity is None
    assert manager._latest_snapshot is None
    assert manager._pending_plan is None
    assert manager.grants == set()
    assert manager._target_generation == generation
    assert manager._artifact_poisoned is True
    with pytest.raises(ComputerSessionError, match="unsafe_cache"):
        run(manager.apps())
    assert backend.apps_calls == apps_calls


def test_manager_get_app_state_rejects_nonregular_snapshot_residual_before_dispatch(
    tmp_path,
):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    residual_name = "snapshot-0123456789abcdef0123456789abcdef.png"
    os.mkfifo(residual_name, mode=0o600, dir_fd=manager._session_fd)

    try:
        inventory = manager._snapshot_artifact_inventory()
        assert stat.S_ISFIFO(inventory[residual_name][3])

        with pytest.raises(ComputerSessionError, match="unsafe_artifact") as exc_info:
            run(manager.get_app_state("app-x", "win-y"))

        assert exc_info.value.code == "unsafe_artifact"
        assert backend.get_state_calls == []
        assert manager.target is None
        assert manager._artifact_poisoned is True
        with pytest.raises(ComputerSessionError, match="unsafe_cache"):
            run(manager.apps())
    finally:
        os.unlink(residual_name, dir_fd=manager._session_fd)


def test_manager_get_app_state_invalidates_all_old_authority_before_first_await(tmp_path):
    class ProbeBackend(AppStateBackend):
        async def get_app_state(self, *args, **kwargs):
            assert self.manager is not None
            self.observed_at_dispatch = (
                self.manager._latest_snapshot,
                self.manager._pending_plan,
                self.manager._takeover_ref,
                self.manager._takeover_cleanup_ref,
                self.manager._fragment_declaration,
                self.manager._fragment_stage,
                self.manager._fragment_stage_input_completed,
                set(self.manager.grants),
            )
            await asyncio.sleep(0)
            return await super().get_app_state(*args, **kwargs)

    backend = ProbeBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    backend.manager = manager
    run(manager.apps())
    run(manager.select("app-x", "win-y"))
    manager._latest_snapshot = ComputerSnapshot("old", {"ax_tree": {"element_ref": "old:root"}})
    manager._latest_snapshot_scope = "target_window"
    manager._pending_plan = object()
    manager._takeover_ref = "old-takeover"
    manager._takeover_cleanup_ref = "old-cleanup"
    manager._fragment_declaration = object()
    manager._fragment_stage = object()
    manager._fragment_stage_input_completed = True
    manager.grants.add("old-grant")

    result = run(manager.get_app_state("app-x", "win-y"))

    assert backend.observed_at_dispatch == (
        None,
        None,
        None,
        None,
        None,
        None,
        False,
        set(),
    )
    assert result.target == ComputerTarget("app-x", "win-y")


@pytest.mark.parametrize(
    "mutate_payload",
    [
        lambda payload: {key: value for key, value in payload.items() if key != "logical_size"},
        lambda payload: {**payload, "backing_scale": True},
        lambda payload: {**payload, "capture_bounds": {"x": 0, "y": 0, "width": 0, "height": 1}},
        lambda payload: {**payload, "ax_tree": []},
        lambda payload: {**payload, "unexpected": True},
    ],
)
def test_manager_get_app_state_validates_complete_snapshot_payload_before_commit(
    tmp_path,
    mutate_payload,
):
    backend = AppStateBackend()
    backend.payload_mutator = mutate_payload
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    generation = manager._target_generation

    with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
        run(manager.get_app_state("app-x", "win-y"))

    assert manager.target is None
    assert manager._target_identity is None
    assert manager._latest_snapshot is None
    assert manager._artifact_poisoned is True
    assert manager._target_generation == generation


@pytest.mark.parametrize(
    ("scope", "payload_mutator"),
    [
        ("display", lambda payload: payload),
        (
            "target_window",
            lambda payload: {
                **payload,
                "display_id": 1,
                "target_window_bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
            },
        ),
    ],
)
def test_manager_get_app_state_binds_display_metadata_to_requested_scope_before_commit(
    tmp_path,
    scope,
    payload_mutator,
):
    backend = AppStateBackend()
    backend.payload_mutator = payload_mutator
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    generation = manager._target_generation

    with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
        run(manager.get_app_state("app-x", "win-y", scope=scope))

    assert manager.target is None
    assert manager._latest_snapshot is None
    assert manager._artifact_poisoned is True
    assert manager._target_generation == generation


def test_manager_get_app_state_success_commits_verified_state_exactly_once(tmp_path):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    generation = manager._target_generation

    result = run(manager.get_app_state("app-x", "win-y", scope="target_window"))

    assert result.target == ComputerTarget("app-x", "win-y")
    assert result.target_generation == generation + 1
    assert result.snapshot == manager._latest_snapshot
    assert result.image_path.read_bytes() == _SMART_SNAPSHOT_PNG
    assert result.image_data == _SMART_SNAPSHOT_PNG
    assert result.image_identity == (
        result.image_path.stat().st_dev,
        result.image_path.stat().st_ino,
    )
    assert result.image_sha256 == hashlib.sha256(_SMART_SNAPSHOT_PNG).hexdigest()
    assert manager.target == result.target
    assert manager._target_generation == generation + 1
    assert manager._target_identity == (
        "dev.astra.computer-fixture",
        "Astra Computer Fixture",
        "/tmp/astra-fullflow/AstraComputerFixture.app",
    )
    assert manager._latest_snapshot_scope == "target_window"
    assert set(manager._current_snapshot_artifacts) == {result.image_path.name}


@pytest.mark.parametrize("failure", [RuntimeError("capture failed"), asyncio.CancelledError()])
def test_manager_get_app_state_failure_or_cancellation_never_advances_generation(
    tmp_path,
    failure,
):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("old-app", "old-window"))
    manager._latest_snapshot = ComputerSnapshot("old")
    generation = manager._target_generation
    backend.state_error = failure

    with pytest.raises(type(failure)):
        run(manager.get_app_state("app-x", "win-y"))

    assert manager._target_generation == generation
    assert manager.target == ComputerTarget("old-app", "old-window")
    assert manager._latest_snapshot is None


def test_manager_get_app_state_does_not_retry_helper_restart_or_advance_generation(tmp_path):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    generation = manager._target_generation
    backend.state_error = HelperApplicationError(
        ComputerError(ComputerErrorCode.TARGET_GONE, "helper restarted")
    )

    with pytest.raises(HelperApplicationError, match="target_gone"):
        run(manager.get_app_state("app-x", "win-y"))

    assert len(backend.get_state_calls) == 1
    assert backend.apps_calls == 1
    assert manager._target_generation == generation
    assert manager._artifact_poisoned is False


def test_manager_get_app_state_partial_publication_failure_is_terminal_unsafe_artifact(tmp_path):
    backend = AppStateBackend()
    backend.fail_after_image = True
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())

    with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
        run(manager.get_app_state("app-x", "win-y"))

    assert manager.target is None
    assert manager._artifact_poisoned is True
    assert len(backend.get_state_calls) == 1


def test_manager_get_app_state_real_cancellation_after_publication_preserves_cancel_and_poison(
    tmp_path,
):
    class PublishedThenBlockedBackend(AppStateBackend):
        def __init__(self):
            super().__init__()
            self.published = asyncio.Event()
            self.never_release = asyncio.Event()

        async def get_app_state(self, target, scope, artifact, **kwargs):
            text_detail = kwargs.get("text_detail", ComputerSnapshotTextDetailMode.OFF)
            detail_artifact = kwargs.get("text_detail_artifact")
            self.get_state_calls.append(
                (target, scope, artifact, text_detail, detail_artifact)
            )
            _write_helper_artifact(artifact, self.image_data)
            self.published.set()
            await self.never_release.wait()
            raise AssertionError("unreachable")

    async def scenario():
        backend = PublishedThenBlockedBackend()
        manager = ComputerSessionManager(backend, cache_root=tmp_path)
        await manager.apps()
        await manager.select("old-app", "old-window")
        manager._latest_snapshot = ComputerSnapshot("old")
        manager._pending_plan = object()
        manager.grants.add("old-grant")
        generation = manager._target_generation
        apps_calls = backend.apps_calls
        task = asyncio.create_task(manager.get_app_state("app-x", "win-y"))
        await backend.published.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.target is None
        assert manager._target_identity is None
        assert manager._latest_snapshot is None
        assert manager._pending_plan is None
        assert manager.grants == set()
        assert manager._artifact_poisoned is True
        assert manager._target_generation == generation
        with pytest.raises(ComputerSessionError, match="unsafe_cache"):
            await manager.apps()
        assert backend.apps_calls == apps_calls

    run(scenario())


@pytest.mark.parametrize("fault", ["invalid_png", "mode", "dimensions"])
def test_manager_get_app_state_plain_artifact_failure_poison_clears_target_and_requires_fresh_manager(
    tmp_path,
    fault,
):
    backend = AppStateBackend()
    if fault == "invalid_png":
        backend.image_data = b"not-a-png"
    elif fault == "mode":
        backend.image_mode = 0o640
    else:
        backend.pixel_size = {"width": 2, "height": 1}
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    run(manager.select("old-app", "old-window"))
    generation = manager._target_generation

    with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
        run(manager.get_app_state("app-x", "win-y"))

    assert manager.target is None
    assert manager._target_identity is None
    assert manager._latest_snapshot is None
    assert manager._target_generation == generation
    apps_calls = backend.apps_calls
    with pytest.raises(ComputerSessionError, match="unsafe_cache"):
        run(manager.apps())
    assert backend.apps_calls == apps_calls

    if fault == "mode":
        with pytest.raises(ComputerSessionError, match="unsafe_cache"):
            run(manager.close())
        return
    run(manager.close())
    fresh_backend = AppStateBackend()
    fresh = ComputerSessionManager(fresh_backend, cache_root=tmp_path)
    run(fresh.apps())
    assert run(fresh.get_app_state("app-x", "win-y")).target == ComputerTarget(
        "app-x", "win-y"
    )
    run(fresh.close())


def test_manager_get_app_state_rejects_plain_png_authority_change_after_read(
    tmp_path,
    monkeypatch,
):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    real_read = os.read
    mutated = False

    def mutate_after_read(descriptor, size):
        nonlocal mutated
        data = real_read(descriptor, size)
        if data and not mutated:
            opened = os.fstat(descriptor)
            image_name = next(
                name for name in os.listdir(manager._session_fd) if name.endswith(".png")
            )
            named = os.stat(image_name, dir_fd=manager._session_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino):
                mutated = True
                os.chmod(image_name, 0o640, dir_fd=manager._session_fd)
        return data

    monkeypatch.setattr("agent.runtime.computer_backend.os.read", mutate_after_read)

    with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
        run(manager.get_app_state("app-x", "win-y"))

    assert mutated is True
    assert manager.target is None
    assert manager._artifact_poisoned is True


def test_manager_get_app_state_smart_success_commits_verified_pair_and_image_bytes(tmp_path):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())

    state = run(manager.get_app_state(
        "app-x",
        "win-y",
        text_detail=ComputerSnapshotTextDetailMode.ON,
    ))

    detail_name = state.snapshot.payload["text_detail_artifact"]
    assert state.image_data == _SMART_SNAPSHOT_PNG
    assert state.snapshot.payload["text_detail_path"] == str(
        manager.session_dir / detail_name
    )
    assert set(manager._current_snapshot_artifacts) == {
        state.image_path.name,
        detail_name,
    }


def test_manager_get_app_state_smart_schema_failure_poison_preserves_uncertain_pair(tmp_path):
    backend = AppStateBackend()

    def cross_snapshot(document):
        document["snapshot_id"] = "other-snapshot"
        return document

    backend.detail_mutator = cross_snapshot
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())

    with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
        run(manager.get_app_state(
            "app-x",
            "win-y",
            text_detail=ComputerSnapshotTextDetailMode.ON,
        ))

    assert manager.target is None
    assert manager._artifact_poisoned is True
    assert any(name.endswith(".png") for name in os.listdir(manager._session_fd))
    assert any(name.endswith(".ax.json") for name in os.listdir(manager._session_fd))


def test_manager_postcondition_poison_clears_committed_state_without_cleanup(tmp_path):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    state = run(manager.get_app_state("app-x", "win-y"))
    names = os.listdir(manager._session_fd)

    manager.poison_artifact_session()

    assert manager.target is None
    assert manager._target_identity is None
    assert manager._latest_snapshot is None
    assert manager._artifact_poisoned is True
    assert os.listdir(manager._session_fd) == names
    assert state.image_path.exists()


def test_manager_failed_catalog_refresh_preserves_latest_successful_catalog_for_new_state(tmp_path):
    backend = AppStateBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    run(manager.apps())
    catalog = manager._last_catalog

    async def fail_apps():
        backend.apps_calls += 1
        raise RuntimeError("refresh failed")

    backend.apps = fail_apps
    with pytest.raises(RuntimeError, match="refresh failed"):
        run(manager.apps())
    assert manager._last_catalog is catalog

    backend.apps = AppStateBackend.apps.__get__(backend, AppStateBackend)
    assert run(manager.get_app_state("app-x", "win-y")).target == ComputerTarget(
        "app-x", "win-y"
    )


def test_manager_get_app_state_serializes_with_catalog_focus_snapshot_handoff_and_close(tmp_path):
    class OrderingBackend(AppStateBackend):
        def __init__(self):
            super().__init__()
            self.state_started = asyncio.Event()
            self.release_state = asyncio.Event()
            self.events: list[str] = []
            self.artifact_names: list[str] = []

        async def apps(self):
            self.events.append("apps")
            return await super().apps()

        async def get_app_state(self, target, scope, artifact, **kwargs):
            self.events.append("state-start")
            self.artifact_names.append(artifact.filename)
            self.state_started.set()
            await self.release_state.wait()
            result = await super().get_app_state(target, scope, artifact, **kwargs)
            self.events.append("state-end")
            return result

        async def select(self, app_ref, window_ref):
            self.events.append("focus")
            return await super().select(app_ref, window_ref)

        async def snapshot(self, target, scope, artifact):
            self.events.append("snapshot")
            self.artifact_names.append(artifact.filename)
            return await super().snapshot(target, scope, artifact)

        async def close(self):
            self.events.append("close")
            await super().close()

    async def scenario():
        backend = OrderingBackend()
        manager = ComputerSessionManager(backend, cache_root=tmp_path)
        await manager.apps()
        state_task = asyncio.create_task(manager.get_app_state("app-x", "win-y"))
        await backend.state_started.wait()
        apps_task = asyncio.create_task(manager.apps())
        focus_task = asyncio.create_task(manager.select("app-x", "win-y"))
        snapshot_task = asyncio.create_task(manager.snapshot())
        handoff_task = asyncio.create_task(manager.handoff())
        await asyncio.sleep(0)
        assert not any(task.done() for task in (
            apps_task,
            focus_task,
            snapshot_task,
            handoff_task,
        ))
        assert backend.events == ["apps", "state-start"]
        backend.release_state.set()
        await state_task
        await apps_task
        await focus_task
        await snapshot_task
        await handoff_task
        await manager.apps()
        backend.state_started.clear()
        backend.release_state.clear()
        closing_state = asyncio.create_task(manager.get_app_state("app-x", "win-y"))
        await backend.state_started.wait()
        close_task = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        assert not close_task.done()
        backend.release_state.set()
        with pytest.raises(ComputerSessionError, match="unsafe_artifact"):
            await closing_state
        await close_task
        return backend

    backend = run(scenario())
    assert backend.events == [
        "apps",
        "state-start",
        "state-end",
        "apps",
        "focus",
        "snapshot",
        "apps",
        "state-start",
        "state-end",
        "close",
    ]
    assert len(backend.artifact_names) == len(set(backend.artifact_names)) == 3


def test_snapshot_recovers_target_by_document_path_after_helper_restart(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    fake_backend.apps_result = _identity_catalog()
    run(manager.apps())
    run(manager.select("app-x", "win-y"))

    snapshot_calls = 0

    async def flaky_snapshot(_target, _scope, _artifact):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            raise HelperApplicationError(ComputerError(ComputerErrorCode.TARGET_GONE, "restarted"))
        return ComputerSnapshot("snap-recovered")

    fake_backend.snapshot = flaky_snapshot
    result = run(manager.snapshot())

    assert result.snapshot_id == "snap-recovered"
    assert snapshot_calls == 2
    # recovery re-discovered and re-selected the same identity after the failure
    assert fake_backend.select_calls == [("app-x", "win-y"), ("app-x", "win-y")]


@pytest.mark.parametrize("target_gone", [False, True])
def test_native_subtree_uses_current_snapshot_and_never_rebinds(fake_backend, tmp_path, target_gone):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-x", "win-y"))
    anchored_calls = []

    async def snapshot(target, scope, artifact, *, subtree=None):
        if subtree is None:
            return ComputerSnapshot("original", {"ax_tree": {"observation_capabilities": ["subtree_v1"]}})
        anchored_calls.append((target, scope, subtree))
        if target_gone:
            raise HelperApplicationError(ComputerError(ComputerErrorCode.TARGET_GONE, "restarted"))
        return ComputerSnapshot("fresh-subtree")

    fake_backend.snapshot = snapshot
    run(manager.snapshot())
    if target_gone:
        with pytest.raises(HelperApplicationError):
            run(manager.snapshot(subtree_ref="current-web-ref"))
        assert manager._latest_snapshot is None
    else:
        assert run(manager.snapshot(subtree_ref="current-web-ref")).snapshot_id == "fresh-subtree"
    assert anchored_calls == [(ComputerTarget("app-x", "win-y"), "target_window", ("original", "current-web-ref"))]
    assert fake_backend.select_calls == [("app-x", "win-y")]


def test_native_subtree_rejects_legacy_helper_before_dispatch(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-x", "win-y"))
    run(manager.snapshot())
    with pytest.raises(ComputerSessionError) as failure:
        run(manager.snapshot(subtree_ref="current-web-ref"))
    assert failure.value.code == "unsupported_operation"
    assert len(fake_backend.snapshot_calls) == 1


def test_snapshot_expected_binding_is_checked_after_concurrent_select_releases_lock(
    fake_backend,
    tmp_path,
):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-old", "window-old"))
    approved_binding = manager.snapshot_target_binding
    assert approved_binding is not None
    select_entered = asyncio.Event()
    release_select = asyncio.Event()
    original_select = fake_backend.select

    async def blocking_select(app_ref, window_ref):
        select_entered.set()
        await release_select.wait()
        return await original_select(app_ref, window_ref)

    async def exercise():
        fake_backend.select = blocking_select
        focus_task = asyncio.create_task(manager.select("app-new", "window-new"))
        await select_entered.wait()
        snapshot_task = asyncio.create_task(manager.snapshot(
            expected_target_binding=approved_binding,
        ))
        await asyncio.sleep(0)
        release_select.set()
        await focus_task
        with pytest.raises(ComputerSessionError, match="snapshot_target_changed"):
            await snapshot_task

    run(exercise())

    assert fake_backend.snapshot_calls == []


def test_snapshot_recovery_refuses_ambiguous_title_without_document_path(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    fake_backend.apps_result = [
        {
            "app_ref": "app-x",
            "bundle_id": "com.example.dup",
            "windows": [
                {"window_ref": "win-1", "title": "Same Page"},
                {"window_ref": "win-2", "title": "Same Page"},
            ],
        }
    ]
    run(manager.apps())
    run(manager.select("app-x", "win-1"))

    async def flaky_snapshot(_target, _scope, _artifact):
        raise HelperApplicationError(ComputerError(ComputerErrorCode.TARGET_GONE, "restarted"))

    fake_backend.snapshot = flaky_snapshot
    with pytest.raises(HelperApplicationError):
        run(manager.snapshot())


def test_snapshot_recovery_not_triggered_for_non_target_gone(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    fake_backend.apps_result = _identity_catalog()
    run(manager.apps())
    run(manager.select("app-x", "win-y"))

    async def failing_snapshot(_target, _scope, _artifact):
        raise HelperApplicationError(ComputerError(ComputerErrorCode.HELPER_FAILED, "broken"))

    fake_backend.snapshot = failing_snapshot
    with pytest.raises(HelperApplicationError):
        run(manager.snapshot())
    # no recovery attempt for non-target_gone failures
    assert fake_backend.select_calls == [("app-x", "win-y")]


@pytest.mark.parametrize("fail_refresh", [False, True])
def test_apps_refresh_consumes_target_and_snapshot_before_backend_result(fake_backend, tmp_path, fail_refresh):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    run(manager.select("app-1", "window-1"))
    snapshot = run(manager.snapshot())

    if fail_refresh:
        async def apps():
            raise RuntimeError("refresh failed")

        fake_backend.apps = apps
        with pytest.raises(RuntimeError, match="refresh failed"):
            run(manager.apps())
    else:
        run(manager.apps())

    assert manager.target is None
    with pytest.raises(ComputerSessionError, match="target_required"):
        plan_and_act(manager, snapshot.snapshot_id, [{"type": "click", "element_ref": "ax-1"}])


def test_apps_serializes_against_snapshot_and_close(tmp_path):
    class OrderingBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.apps_started = asyncio.Event()
            self.release_apps = asyncio.Event()
            self.events = []

        async def apps(self):
            self.events.append("apps-start")
            self.apps_started.set()
            await self.release_apps.wait()
            self.events.append("apps-end")
            return ComputerAppCatalog(1, ())

        async def snapshot(self, target, scope, artifact):
            self.events.append("snapshot")
            return await super().snapshot(target, scope, artifact)

        async def close(self):
            self.events.append("close")
            await super().close()

    async def scenario():
        backend = OrderingBackend()
        manager = ComputerSessionManager(backend, cache_root=tmp_path)
        await manager.select("app-1", "window-1")
        await manager.snapshot()
        apps_task = asyncio.create_task(manager.apps())
        await backend.apps_started.wait()
        snapshot_task = asyncio.create_task(manager.snapshot())
        close_task = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        assert not snapshot_task.done()
        assert not close_task.done()
        backend.release_apps.set()
        await apps_task
        with pytest.raises(ComputerSessionError, match="target_required|session_closing"):
            await snapshot_task
        await close_task
        return backend.events

    assert run(scenario()) == ["snapshot", "apps-start", "apps-end", "close"]


def test_status_serializes_with_close_without_deadlock(tmp_path):
    class StatusBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.status_started = asyncio.Event()
            self.release_status = asyncio.Event()
            self.events = []

        async def status(self):
            self.events.append("status-start")
            self.status_started.set()
            await self.release_status.wait()
            self.events.append("status-end")
            return {"supported": True}

        async def close(self):
            self.events.append("close")
            await super().close()

    async def scenario():
        backend = StatusBackend()
        manager = ComputerSessionManager(backend, cache_root=tmp_path)
        status_task = asyncio.create_task(manager.status())
        await backend.status_started.wait()
        close_task = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        assert not close_task.done()
        backend.release_status.set()
        assert await status_task == {"supported": True}
        await close_task
        return backend.events

    assert run(scenario()) == ["status-start", "status-end", "close"]


def test_close_is_idempotent_clears_grants_and_only_removes_its_session_dir(fake_backend, tmp_path):
    survivor = tmp_path / "survivor"
    survivor.mkdir()
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    manager.grants.add("ordinary-interaction")
    session_dir = manager.session_dir
    manager.write_artifact("snapshot-0123456789abcdef0123456789abcdef.png", b"private")

    run(manager.close())
    run(manager.close())

    assert fake_backend.closed == 1
    assert manager.grants == set()
    assert not session_dir.exists()
    assert survivor.exists()


def test_session_cache_is_private_and_refuses_symlinked_root(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path / "cache")
    assert stat.S_IMODE(manager.session_dir.stat().st_mode) == 0o700

    link = tmp_path / "link"
    link.symlink_to(tmp_path / "cache")
    with pytest.raises(ComputerSessionError, match="symlink"):
        ComputerSessionManager(fake_backend, cache_root=link)


def test_startup_cleanup_preserves_a_live_peer_session(fake_backend, tmp_path):
    first = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    artifact_name = "snapshot-0123456789abcdef0123456789abcdef.png"
    first.write_artifact(artifact_name, b"live")

    second = ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert first.session_dir.exists()
    assert (first.session_dir / artifact_name).read_bytes() == b"live"
    run(second.close())
    run(first.close())


@requires_posix_authority
def test_session_directory_fd_is_the_helper_shared_lease(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    peer_fd = os.open(manager.session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(OSError) as caught:
            fcntl.flock(peer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert caught.value.errno in {errno.EACCES, errno.EAGAIN}
    finally:
        os.close(peer_fd)
    run(manager.close())


def test_close_awaits_helper_before_exclusive_cleanup_and_directory_removal(tmp_path, monkeypatch):
    class OrderBackend(FakeBackend):
        manager = None

        async def close(self):
            assert self.manager is not None
            assert self.manager.session_dir.exists()
            await super().close()

    backend = OrderBackend()
    manager = ComputerSessionManager(backend, cache_root=tmp_path)
    backend.manager = manager
    real_clear = manager._clear_owned_session_contents

    def require_stopped_helper():
        assert backend.closed == 1
        real_clear()

    monkeypatch.setattr(manager, "_clear_owned_session_contents", require_stopped_helper)

    run(manager.close())
    assert not manager.session_dir.exists()


def test_close_backend_retries_a_cancelled_backend_close_task(tmp_path):
    class CancelOnceBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.close_attempts = 0

        async def close(self):
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise asyncio.CancelledError
            await super().close()

    async def scenario():
        backend = CancelOnceBackend()
        manager = ComputerSessionManager(backend, cache_root=tmp_path)
        try:
            with pytest.raises(asyncio.CancelledError):
                await manager.close_backend()
            assert manager._backend_close_task is not None
            assert manager._backend_close_task.cancelled()

            await manager.close_backend()
            manager.close_local_state()
            return backend.close_attempts, manager.closed
        finally:
            if not manager.closed:
                manager._backend_close_task = None
                await manager.close()

    assert run(scenario()) == (2, True)


@requires_posix_authority
def test_startup_cleanup_uses_directory_lease_even_if_file_lease_is_unlocked(tmp_path):
    stale = tmp_path / ("9" * 32)
    stale.mkdir(mode=0o700)
    lease = stale / ComputerSessionManager._LEASE_NAME
    lease.write_bytes(b"")
    lease.chmod(0o600)
    held_fd = os.open(stale, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    fcntl.flock(held_fd, fcntl.LOCK_SH)
    try:
        manager = ComputerSessionManager(FakeBackend(), cache_root=tmp_path)
        assert stale.is_dir()
        run(manager.close())
    finally:
        os.close(held_fd)

    survivor = ComputerSessionManager(FakeBackend(), cache_root=tmp_path)
    assert not stale.exists()
    run(survivor.close())


@pytest.mark.parametrize("residual", [".rollback-deadbeef.quarantine", ".snapshot-deadbeef.tmp"])
def test_startup_cleanup_preserves_uncertain_smart_snapshot_residual(tmp_path, residual):
    stale = tmp_path / ("8" * 32)
    stale.mkdir(mode=0o700)
    lease = stale / ComputerSessionManager._LEASE_NAME
    lease.write_bytes(b"")
    lease.chmod(0o600)
    uncertain = stale / residual
    uncertain.write_bytes(b"uncertain")
    uncertain.chmod(0o600)

    with pytest.raises(ComputerSessionError, match="residual|unsafe_cache"):
        ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert uncertain.read_bytes() == b"uncertain"


@requires_posix_authority
def test_startup_cleanup_removes_a_sigkill_orphan(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; "
                "from pathlib import Path; "
                "from agent.runtime.computer_backend import ComputerSessionManager; "
                "manager=ComputerSessionManager(object(), cache_root=Path(sys.argv[1])); "
                    "manager.write_artifact('snapshot-0123456789abcdef0123456789abcdef.png', b'orphan'); "
                "print(manager.session_dir, flush=True); time.sleep(60)"
            ),
            str(tmp_path),
        ],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    orphan = Path(child.stdout.readline().strip())
    assert orphan.is_dir()
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=5)

    survivor = ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert not orphan.exists()
    run(survivor.close())


def test_startup_cleanup_fails_closed_on_unsafe_stale_lease(tmp_path):
    stale = tmp_path / ("a" * 32)
    stale.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (stale / ".lease").symlink_to(outside)

    with pytest.raises(ComputerSessionError, match="unsafe_cache"):
        ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert (stale / ".lease").is_symlink()
    assert outside.read_bytes() == b"outside"


def test_startup_cleanup_only_removes_old_legacy_session_with_exact_snapshot_png(tmp_path):
    fresh = tmp_path / ("b" * 32)
    fresh.mkdir(mode=0o700)
    stale = tmp_path / ("c" * 32)
    stale.mkdir(mode=0o700)
    artifact = stale / "snapshot-0123456789abcdef0123456789abcdef.png"
    artifact.write_bytes(b"stale")
    artifact.chmod(0o600)
    old = stale.stat().st_mtime - (25 * 60 * 60)
    os.utime(stale, (old, old))

    manager = ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert fresh.is_dir()
    assert not stale.exists()
    run(manager.close())


@pytest.mark.parametrize(
    "uncertain_name",
    [
        "capture.png",
        ".snapshot-deadbeef.tmp",
        ".rollback-deadbeef.quarantine",
        "snapshot-0123456789abcdef0123456789abcdef.ax.json",
    ],
)
def test_startup_cleanup_preserves_old_legacy_uncertain_residual_without_partial_unlink(
    tmp_path,
    monkeypatch,
    uncertain_name,
):
    stale = tmp_path / ("6" * 32)
    stale.mkdir(mode=0o700)
    legacy = stale / "snapshot-fedcba9876543210fedcba9876543210.png"
    legacy.write_bytes(b"legacy")
    legacy.chmod(0o600)
    uncertain = stale / uncertain_name
    uncertain.write_bytes(b"uncertain")
    uncertain.chmod(0o600)
    old = stale.stat().st_mtime - (25 * 60 * 60)
    os.utime(stale, (old, old))
    unlink_calls = []
    real_unlink = os.unlink

    def observe_unlink(path, *args, **kwargs):
        unlink_calls.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("agent.runtime.computer_backend.os.unlink", observe_unlink)

    with pytest.raises(ComputerSessionError, match="legacy|residual|unsafe_cache"):
        ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert unlink_calls == []
    assert legacy.read_bytes() == b"legacy"
    assert uncertain.read_bytes() == b"uncertain"


def test_startup_cleanup_rejects_nonregular_artifact_without_partial_deletion(tmp_path):
    stale = tmp_path / ("d" * 32)
    stale.mkdir(mode=0o700)
    lease = stale / ComputerSessionManager._LEASE_NAME
    lease.write_bytes(b"")
    lease.chmod(0o600)
    good = stale / "capture.png"
    good.write_bytes(b"keep")
    good.chmod(0o600)
    (stale / "unexpected-directory").mkdir()

    with pytest.raises(ComputerSessionError, match="unsafe_cache"):
        ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert good.read_bytes() == b"keep"
    assert (stale / "unexpected-directory").is_dir()


def test_startup_cleanup_rejects_unbounded_inventory_without_partial_deletion(
    tmp_path,
    monkeypatch,
):
    stale = tmp_path / ("7" * 32)
    stale.mkdir(mode=0o700)
    lease = stale / ComputerSessionManager._LEASE_NAME
    lease.write_bytes(b"")
    lease.chmod(0o600)
    for index in range(128):
        artifact = stale / f"snapshot-{index:032x}.png"
        artifact.write_bytes(b"owned")
        artifact.chmod(0o600)

    unlink_calls: list[str] = []
    real_unlink = os.unlink

    def observe_unlink(path, *args, **kwargs):
        unlink_calls.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("agent.runtime.computer_backend.os.unlink", observe_unlink)

    with pytest.raises(ComputerSessionError, match="unbounded"):
        ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert unlink_calls == []
    assert len(list(stale.iterdir())) == 129


@pytest.mark.parametrize(
    ("names", "expected_consumed"),
    [
        ([f"entry-{index}" for index in range(130)], 129),
        (["x" * (32 * 1024 + 1), "must-not-be-consumed"], 1),
    ],
)
def test_bounded_inventory_stops_incrementally_and_closes_iterator(
    tmp_path,
    monkeypatch,
    names,
    expected_consumed,
):
    manager = ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    class Entry:
        def __init__(self, name):
            self.name = name

    class RecordingScandir:
        def __init__(self):
            self._names = iter(names)
            self.consumed = 0
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.closed = True

        def __iter__(self):
            return self

        def __next__(self):
            if self.consumed >= expected_consumed:
                raise AssertionError("bounded inventory consumed past its failure boundary")
            self.consumed += 1
            return Entry(next(self._names))

    iterator = RecordingScandir()
    monkeypatch.setattr(
        "agent.runtime.computer_backend.os.scandir",
        lambda directory_fd: iterator,
    )

    with pytest.raises(ComputerSessionError, match="unbounded"):
        manager._bounded_artifact_names(manager._session_fd)

    assert iterator.consumed == expected_consumed
    assert iterator.closed is True


def test_startup_cleanup_fails_closed_on_wrong_owner_metadata(tmp_path, monkeypatch):
    name = "e" * 32
    stale = tmp_path / name
    stale.mkdir(mode=0o700)
    real_stat = os.stat

    def wrong_owner(path, *args, dir_fd=None, follow_symlinks=True, **kwargs):
        metadata = real_stat(
            path,
            *args,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
            **kwargs,
        )
        if path == name and dir_fd is not None:
            fields = list(metadata)
            fields[4] = metadata.st_uid + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr("agent.runtime.computer_backend.os.stat", wrong_owner)

    with pytest.raises(ComputerSessionError, match="unsafe_cache"):
        ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert stale.is_dir()


def test_startup_cleanup_preserves_replacement_on_identity_race(tmp_path, monkeypatch):
    name = "f" * 32
    stale = tmp_path / name
    stale.mkdir(mode=0o700)
    lease = stale / ComputerSessionManager._LEASE_NAME
    lease.write_bytes(b"")
    lease.chmod(0o600)
    real_open = os.open
    real_rename = os.rename
    swapped = False

    def swap_directory_then_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == name and dir_fd is not None and not swapped:
            swapped = True
            real_rename(name, "moved-stale", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.mkdir(name, mode=0o700, dir_fd=dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agent.runtime.computer_backend.os.open", swap_directory_then_open)

    with pytest.raises(ComputerSessionError, match="identity"):
        ComputerSessionManager(FakeBackend(), cache_root=tmp_path)

    assert swapped is True
    assert (tmp_path / name).is_dir()
    assert (tmp_path / "moved-stale" / ComputerSessionManager._LEASE_NAME).is_file()


def test_close_deletes_original_session_after_cache_root_is_renamed_and_replaced(fake_backend, tmp_path):
    cache_root = tmp_path / "cache"
    manager = ComputerSessionManager(fake_backend, cache_root=cache_root)
    original_session = manager.session_dir
    moved_root = tmp_path / "moved-cache"
    outside = tmp_path / "outside"
    outside.mkdir()
    cache_root.rename(moved_root)
    cache_root.symlink_to(outside, target_is_directory=True)

    run(manager.close())

    assert not (moved_root / original_session.name).exists()
    assert list(outside.iterdir()) == []


def test_close_deletes_original_session_after_ancestor_becomes_symlink(fake_backend, tmp_path):
    parent = tmp_path / "parent"
    cache_root = parent / "cache"
    manager = ComputerSessionManager(fake_backend, cache_root=cache_root)
    session_name = manager.session_dir.name
    moved_parent = tmp_path / "moved-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    parent.rename(moved_parent)
    parent.symlink_to(outside, target_is_directory=True)

    run(manager.close())

    assert not (moved_parent / "cache" / session_name).exists()
    assert list(outside.iterdir()) == []


def test_close_finds_and_removes_session_after_rename_within_held_cache_root(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    session_name = manager.session_dir.name
    moved_name = "renamed-session"
    manager.write_artifact("snapshot-0123456789abcdef0123456789abcdef.png", b"private")
    os.rename(session_name, moved_name, src_dir_fd=manager._cache_fd, dst_dir_fd=manager._cache_fd)

    run(manager.close())

    assert not (tmp_path / moved_name).exists()


def test_close_fails_closed_when_session_is_renamed_outside_held_cache_root(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    moved_root = tmp_path / "moved"
    moved_root.mkdir()
    manager.write_artifact("capture.png", b"private")
    manager.session_dir.rename(moved_root / "session")

    with pytest.raises(ComputerSessionError, match="identity"):
        run(manager.close())

    assert (moved_root / "session" / "capture.png").read_bytes() == b"private"
    assert manager._closed is False


def test_close_preserves_replacement_when_entry_swaps_after_identity_check(fake_backend, tmp_path, monkeypatch):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    root_fd = manager._cache_fd
    session_name = manager.session_dir.name
    moved_name = "original-session"
    real_stat = os.stat
    real_rename = os.rename
    swapped = False
    replacement_name: str | None = None

    def stat_then_swap(path, *args, dir_fd=None, follow_symlinks=True, **kwargs):
        nonlocal replacement_name, swapped
        metadata = real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks, **kwargs)
        if (
            isinstance(path, str)
            and (path == session_name or path.startswith(f".{session_name}.closing-"))
            and dir_fd == root_fd
            and not swapped
        ):
            swapped = True
            replacement_name = path
            real_rename(path, moved_name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.mkdir(path, mode=0o700, dir_fd=root_fd)
            replacement_fd = os.open(path, os.O_RDONLY, dir_fd=root_fd)
            try:
                keep_fd = os.open("keep.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=replacement_fd)
                try:
                    os.write(keep_fd, b"keep")
                finally:
                    os.close(keep_fd)
            finally:
                os.close(replacement_fd)
        return metadata

    monkeypatch.setattr("agent.runtime.computer_backend.os.stat", stat_then_swap)
    with pytest.raises(ComputerSessionError, match="identity"):
        run(manager.close())

    assert swapped is True
    assert replacement_name is not None
    assert (tmp_path / replacement_name / "keep.txt").read_bytes() == b"keep"
    assert (tmp_path / moved_name).exists()


def test_session_creation_rejects_stat_to_open_swap_without_writing_replacement(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    real_open = os.open
    real_rename = os.rename
    swapped_paths: list[str] = []

    def swap_then_open(path, flags, mode=0o777, *, dir_fd=None):
        if (
            dir_fd is not None
            and path != ComputerSessionManager._CLEANUP_LOCK_NAME
            and len(path) == 32
            and not swapped_paths
        ):
            swapped_paths.append(path)
            real_rename(path, "moved-session", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.mkdir(path, mode=0o700, dir_fd=dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("agent.runtime.computer_backend.os.open", swap_then_open)
    with pytest.raises(ComputerSessionError, match="identity"):
        ComputerSessionManager(FakeBackend(), cache_root=cache_root)

    assert swapped_paths
    replacement = cache_root / swapped_paths[0]
    assert replacement.exists()
    assert list(replacement.iterdir()) == []


@pytest.mark.parametrize("replace_ancestor", [False, True])
def test_safe_artifact_write_uses_original_session_fd_after_path_replacement(fake_backend, tmp_path, replace_ancestor):
    parent = tmp_path / "parent" if replace_ancestor else tmp_path
    cache_root = parent / "cache"
    manager = ComputerSessionManager(fake_backend, cache_root=cache_root)
    session_name = manager.session_dir.name
    outside = tmp_path / "outside"
    outside.mkdir()
    if replace_ancestor:
        moved_parent = tmp_path / "moved-parent"
        parent.rename(moved_parent)
        parent.symlink_to(outside, target_is_directory=True)
        original_session = moved_parent / "cache" / session_name
    else:
        moved_root = tmp_path / "moved-cache"
        cache_root.rename(moved_root)
        cache_root.symlink_to(outside, target_is_directory=True)
        original_session = moved_root / session_name

    manager.write_artifact("capture.png", b"private screenshot")

    artifact = original_session / "capture.png"
    assert artifact.read_bytes() == b"private screenshot"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert list(outside.iterdir()) == []


def test_safe_artifact_write_rejects_invalid_or_replaced_name(fake_backend, tmp_path):
    manager = ComputerSessionManager(fake_backend, cache_root=tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (manager.session_dir / "capture.png").symlink_to(outside)

    with pytest.raises(ComputerSessionError, match="artifact"):
        manager.write_artifact("capture.png", b"private")
    (manager.session_dir / "existing.png").write_bytes(b"replacement")
    with pytest.raises(ComputerSessionError, match="artifact"):
        manager.write_artifact("existing.png", b"private")
    with pytest.raises(ComputerSessionError, match="artifact"):
        manager.write_artifact("../escape.png", b"private")
    with pytest.raises(ComputerSessionError, match="artifact"):
        manager.write_artifact("nested\\escape.png", b"private")
    assert outside.read_bytes() == b"outside"


def test_session_initialization_failure_closes_root_fd_and_removes_created_session(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    opened_root_fd = []
    real_open = os.open

    def fail_session_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            descriptor = real_open(path, flags, mode)
            opened_root_fd.append(descriptor)
            return descriptor
        if path == ComputerSessionManager._CLEANUP_LOCK_NAME:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        raise OSError("simulated session descriptor failure")

    monkeypatch.setattr("agent.runtime.computer_backend.os.open", fail_session_open)
    with pytest.raises(ComputerSessionError, match="cannot create session"):
        ComputerSessionManager(FakeBackend(), cache_root=cache_root)

    assert [path.name for path in cache_root.iterdir()] == [ComputerSessionManager._CLEANUP_LOCK_NAME]
    with pytest.raises(OSError):
        os.fstat(opened_root_fd[0])


class BlockingCloseBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.allow_close.wait()
        await super().close()


def test_session_close_finishes_cleanup_after_caller_cancellation(tmp_path):
    backend = BlockingCloseBackend()

    async def scenario():
        manager = ComputerSessionManager(backend, cache_root=tmp_path)
        manager.grants.add("ordinary-interaction")
        session_dir = manager.session_dir
        first = asyncio.create_task(manager.close())
        await backend.close_started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert session_dir.exists()
        assert manager.grants == {"ordinary-interaction"}
        assert manager._closed is False
        backend.allow_close.set()
        await manager.close()
        return manager, session_dir

    manager, session_dir = run(scenario())
    assert backend.closed == 1
    assert manager.grants == set()
    assert not session_dir.exists()
    assert manager._closed is True


class CrashingTransport:
    def __init__(self) -> None:
        self.request_count = 0

    async def request(self, _request):
        self.request_count += 1
        raise HelperTransportError("helper exited")


def test_crashed_write_is_unknown_and_not_replayed():
    crashing_transport = CrashingTransport()
    target = ComputerTarget("app-1", "window-1")
    actions = [{"type": "click", "element_ref": "ax-1"}]

    result = run(mac_background_act(MacComputerBackend(crashing_transport), target, "snap-1", actions))

    assert result.error.code is ComputerErrorCode.UNKNOWN_OUTCOME
    assert crashing_transport.request_count == 1


class StaticTransport:
    def __init__(self, response: ComputerResponse) -> None:
        self.response = response
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        return self.response

    async def configure_artifact_directory(self, _directory_fd, _directory_path=None):
        return None


def _state_snapshot(artifact_name: str, *, snapshot_id: str = "state-snapshot") -> ComputerSnapshot:
    return ComputerSnapshot(snapshot_id, {
        "image_artifact": artifact_name,
        "logical_size": {"width": 1, "height": 1},
        "pixel_size": {"width": 1, "height": 1},
        "backing_scale": 1,
        "capture_bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
        "ax_tree": {},
    })


def _app_state_result(*, app_ref="app-1", window_ref="window-1", generation=7, mode="background"):
    return {
        "app_ref": app_ref,
        "window_ref": window_ref,
        "catalog_generation": generation,
        "interaction_mode": mode,
    }


def _native_catalog_result():
    return {
        "catalog_generation": 7,
        "apps": [
            {
                "app_ref": "app-1",
                "name": "Fixture App",
                "bundle_id": "com.example.fixture",
                "app_version": "1.0",
                "windows": [
                    {
                        "window_ref": "window-1",
                        "title": "Fixture Document",
                        "bounds": {"x": 1, "y": 2, "width": 640, "height": 480},
                        "document_path": "/tmp/fixture.txt",
                        "bindable": True,
                        "binding_status": "ready",
                        "window_identity_ref": "identity-1",
                    },
                    {
                        "window_ref": "window-2",
                        "title": "Permission Required",
                        "bounds": {"x": 3, "y": 4, "width": 320, "height": 240},
                        "bindable": False,
                        "binding_status": "accessibility_permission_required",
                    },
                    {
                        "window_ref": "window-3",
                        "title": "Unmatched Window",
                        "bounds": {"x": 5, "y": 6, "width": 160, "height": 120},
                        "bindable": False,
                        "binding_status": "ax_window_unmatched",
                    },
                ],
            },
        ],
    }


def _malformed_nested_catalog_results():
    results = []

    def mutate(callback):
        result = deepcopy(_native_catalog_result())
        callback(result)
        results.append(result)

    mutate(lambda result: result["apps"].append(deepcopy(result["apps"][0])))
    results[-1]["apps"][1]["app_ref"] = "app-1"
    mutate(lambda result: result["apps"][0].pop("name"))
    mutate(lambda result: result["apps"][0].__setitem__("extra", True))
    mutate(lambda result: result["apps"][0].__setitem__("app_ref", ""))
    mutate(lambda result: result["apps"][0].__setitem__("app_ref", "\ud800"))
    mutate(lambda result: result["apps"][0].__setitem__("name", "n" * 513))
    mutate(lambda result: result["apps"][0].__setitem__("bundle_id", 7))
    mutate(lambda result: result["apps"][0].__setitem__("windows", {}))
    mutate(lambda result: result["apps"][0]["windows"][0].pop("bounds"))
    mutate(lambda result: result["apps"][0]["windows"][0].__setitem__("extra", True))
    mutate(lambda result: result["apps"][0]["windows"][0].__setitem__("window_ref", ""))
    mutate(
        lambda result: result["apps"][0]["windows"][1].__setitem__(
            "window_ref", result["apps"][0]["windows"][0]["window_ref"]
        )
    )
    mutate(lambda result: result["apps"][0]["windows"][0].__setitem__("title", "t" * 513))
    mutate(
        lambda result: result["apps"][0]["windows"][0].__setitem__(
            "bounds", {"x": 0, "y": 0, "width": 0, "height": 1}
        )
    )
    mutate(lambda result: result["apps"][0]["windows"][0]["bounds"].__setitem__("x", float("nan")))
    mutate(lambda result: result["apps"][0]["windows"][0].__setitem__("binding_status", "unknown"))
    mutate(lambda result: result["apps"][0]["windows"][0].__setitem__("bindable", False))
    mutate(lambda result: result["apps"][0]["windows"][0].pop("window_identity_ref"))
    mutate(lambda result: result["apps"][0]["windows"][0].__setitem__("document_path", None))
    mutate(lambda result: result["apps"][0]["windows"][1].__setitem__("window_identity_ref", None))
    mutate(
        lambda result: result["apps"][0]["windows"][1].__setitem__(
            "window_identity_ref", "identity-not-bindable"
        )
    )
    mutate(lambda result: result["apps"][0]["windows"][1].__setitem__("bindable", True))
    mutate(
        lambda result: result["apps"][0]["windows"][1].__setitem__(
            "window_identity_ref", result["apps"][0]["windows"][0]["window_identity_ref"]
        )
    )

    too_many_apps = deepcopy(_native_catalog_result())
    too_many_apps["apps"] = [
        {**deepcopy(too_many_apps["apps"][0]), "app_ref": f"app-{index}", "windows": []}
        for index in range(101)
    ]
    results.append(too_many_apps)

    too_many_windows = deepcopy(_native_catalog_result())
    template = too_many_windows["apps"][0]["windows"][1]
    too_many_windows["apps"][0]["windows"] = [
        {**deepcopy(template), "window_ref": f"window-{index}"}
        for index in range(201)
    ]
    results.append(too_many_windows)
    return results


def test_mac_backend_decodes_catalog_and_preserves_previous_catalog_on_rejection():
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        transport.requests.append(request)
        if len(transport.requests) == 1:
            result = _native_catalog_result()
            return ComputerResponse(
                request.request_id,
                ok=True,
                result=result,
            )
        return ComputerResponse(
            request.request_id,
            ok=False,
            error=ComputerError(ComputerErrorCode.HELPER_FAILED, "bounded native failure"),
        )

    transport.request = request
    backend = MacComputerBackend(transport)

    catalog = run(backend.apps())
    assert catalog == ComputerAppCatalog(7, tuple(_native_catalog_result()["apps"]))
    with pytest.raises(HelperApplicationError, match="helper_failed"):
        run(backend.apps())
    assert backend._catalog == catalog


def test_mac_backend_preserves_previous_catalog_when_refresh_is_cancelled():
    transport = StaticTransport(ComputerResponse("unused", ok=True))
    calls = 0

    async def request(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ComputerResponse(
                request.request_id,
                ok=True,
                result={"catalog_generation": 7, "apps": []},
            )
        raise asyncio.CancelledError

    transport.request = request
    backend = MacComputerBackend(transport)
    catalog = run(backend.apps())
    with pytest.raises(asyncio.CancelledError):
        run(backend.apps())

    assert backend._catalog == catalog


@pytest.mark.parametrize("result", [
    {"apps": []},
    {"catalog_generation": 0, "apps": []},
    {"catalog_generation": -1, "apps": []},
    {"catalog_generation": 1.0, "apps": []},
    {"catalog_generation": True, "apps": []},
    {"catalog_generation": 1, "apps": [], "extra": True},
])
def test_mac_backend_rejects_malformed_catalog_result(result):
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        return ComputerResponse(request.request_id, ok=True, result=result)

    transport.request = request
    with pytest.raises(HelperTransportError, match="malformed app catalog"):
        run(MacComputerBackend(transport).apps())


@pytest.mark.parametrize("result", _malformed_nested_catalog_results())
def test_mac_backend_rejects_malformed_nested_catalog_result(result):
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        return ComputerResponse(request.request_id, ok=True, result=result)

    transport.request = request
    with pytest.raises(HelperTransportError, match="malformed app catalog"):
        run(MacComputerBackend(transport).apps())


@pytest.mark.parametrize("result", [
    {"window_ref": "window-1", "catalog_generation": 7, "interaction_mode": "background"},
    {"app_ref": "app-1", "catalog_generation": 7, "interaction_mode": "background"},
    {"app_ref": "app-1", "window_ref": "window-1", "interaction_mode": "background"},
    {**_app_state_result(), "extra": True},
    _app_state_result(app_ref="other-app"),
    _app_state_result(window_ref="other-window"),
    _app_state_result(generation=8),
    _app_state_result(mode="foreground_takeover"),
])
def test_mac_backend_get_app_state_rejects_crossed_or_malformed_result(result):
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        if request.operation == "apps":
            return ComputerResponse(request.request_id, ok=True, result={"catalog_generation": 7, "apps": []})
        return ComputerResponse(
            request.request_id,
            ok=True,
            result=result,
            snapshot=_state_snapshot("state.png"),
        )

    transport.request = request
    backend = MacComputerBackend(transport)
    run(backend.apps())
    with pytest.raises(HelperTransportError, match="malformed app state"):
        run(backend.get_app_state(
            ComputerTarget("app-1", "window-1"),
            "target_window",
            ComputerArtifact("state.png", 0),
        ))


@pytest.mark.parametrize("response", [
    lambda request: ComputerResponse(request.request_id, ok=True, result=_app_state_result()),
    lambda request: ComputerResponse(request.request_id, ok=True, snapshot=_state_snapshot("state.png")),
    lambda request: ComputerResponse(
        request.request_id,
        ok=True,
        result=_app_state_result(),
        snapshot=ComputerSnapshot("state-snapshot", {"image_artifact": "state.png"}),
    ),
])
def test_mac_backend_get_app_state_requires_complete_valid_response(response):
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        if request.operation == "apps":
            return ComputerResponse(request.request_id, ok=True, result={"catalog_generation": 7, "apps": []})
        return response(request)

    transport.request = request
    backend = MacComputerBackend(transport)
    run(backend.apps())
    with pytest.raises(HelperTransportError):
        run(backend.get_app_state(
            ComputerTarget("app-1", "window-1"),
            "target_window",
            ComputerArtifact("state.png", 0),
        ))


def test_mac_backend_get_app_state_binds_current_catalog_and_exact_response_metadata():
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        transport.requests.append(request)
        if request.operation == "apps":
            return ComputerResponse(request.request_id, ok=True, result={"catalog_generation": 7, "apps": []})
        return ComputerResponse(
            request.request_id,
            ok=True,
            result=_app_state_result(),
            snapshot=_state_snapshot("state.png"),
        )

    transport.request = request
    backend = MacComputerBackend(transport)
    catalog = run(backend.apps())
    state = run(backend.get_app_state(
        ComputerTarget("app-1", "window-1"),
        "target_window",
        ComputerArtifact("state.png", 0),
    ))

    assert state == ComputerBackendAppState(
        catalog_generation=catalog.generation,
        target=ComputerTarget("app-1", "window-1"),
        snapshot=_state_snapshot("state.png"),
    )
    assert transport.requests[-1].operation == "get_app_state"
    assert transport.requests[-1].payload == {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "catalog_generation": 7,
        "scope": "target_window",
        "artifact_name": "state.png",
    }


def test_mac_backend_get_app_state_carries_request_local_detail_artifact_and_native_error():
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        transport.requests.append(request)
        if request.operation == "apps":
            return ComputerResponse(request.request_id, ok=True, result={"catalog_generation": 7, "apps": []})
        return ComputerResponse(
            request.request_id,
            ok=False,
            error=ComputerError(ComputerErrorCode.TARGET_GONE, "bounded native failure"),
        )

    transport.request = request
    backend = MacComputerBackend(transport)
    run(backend.apps())
    with pytest.raises(HelperApplicationError, match="target_gone") as exc_info:
        run(backend.get_app_state(
            ComputerTarget("app-1", "window-1"),
            "target_window",
            ComputerArtifact("state.png", 0),
            text_detail=ComputerSnapshotTextDetailMode.ON,
            text_detail_artifact=ComputerArtifact("state.ax.json", 0),
        ))

    assert exc_info.value.error.code is ComputerErrorCode.TARGET_GONE
    assert transport.requests[-1].payload == {
        "app_ref": "app-1",
        "window_ref": "window-1",
        "catalog_generation": 7,
        "scope": "target_window",
        "artifact_name": "state.png",
        "text_detail": "on",
        "text_detail_artifact_name": "state.ax.json",
    }


@pytest.mark.parametrize(
    "result",
    [
        {"started": True, "restoration": "restored", "extra": True},
        {"started": "yes", "restoration": "restored"},
        {"started": False, "restoration": "restored"},
        {"started": False, "restoration": "not_started"},
        {"started": True, "restoration": "not_started"},
    ],
)
def test_mac_backend_strictly_validates_takeover_end_outcome(result):
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        return ComputerResponse(request.request_id, ok=True, result=result)

    transport.request = request
    with pytest.raises(HelperTransportError, match="malformed takeover outcome"):
        run(MacComputerBackend(transport).end_takeover("takeover-1"))


def test_mac_backend_normalizes_valid_takeover_end_outcome():
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        return ComputerResponse(
            request.request_id,
            ok=True,
            result={"started": True, "restoration": "preserved_user_focus"},
        )

    transport.request = request
    assert run(MacComputerBackend(transport).end_takeover("takeover-1")) == {
        "started": True,
        "restoration": "preserved_user_focus",
    }


def test_mac_backend_uses_only_v2_select_plan_and_mode_aware_act_payloads():
    target = ComputerTarget("app-1", "window-1")
    actions = [{"type": "click", "element_ref": "ax-1"}]
    transport = StaticTransport(ComputerResponse("unused", ok=True))

    async def request(request):
        transport.requests.append(request)
        if request.operation == "select":
            return ComputerResponse(
                request.request_id,
                ok=True,
                result={"app_ref": "app-1", "window_ref": "window-1", "interaction_mode": "background"},
            )
        if request.operation == "plan_actions":
            return ComputerResponse(
                request.request_id,
                ok=True,
                result={
                    "plan_ref": "plan-1",
                    "interaction_mode": "background",
                    "requires_takeover": False,
                    "reason": "background_ax_only",
                    "action_classes": ["click"],
                    "pid_action_classes": ["click"],
                    "last_acknowledged_action": -1,
                },
            )
        return ComputerResponse(
            request.request_id,
            ok=True,
            result={"outcomes": [{"index": 0, "ok": True}], "last_acknowledged_action": 0},
        )

    transport.request = request
    backend = MacComputerBackend(transport)

    selected = run(backend.select("app-1", "window-1"))
    plan = run(backend.plan_actions(
        target,
        "snap-1",
        actions,
        ComputerInteractionMode.BACKGROUND,
    ))
    result = run(backend.act(
        target,
        "snap-1",
        actions,
        interaction_mode=ComputerInteractionMode.BACKGROUND,
        plan_ref=plan.plan_ref,
    ))

    assert selected == target
    assert result.ok is True
    assert plan.pid_action_classes == ("click",)
    assert [request.operation for request in transport.requests] == ["select", "plan_actions", "act"]
    assert transport.requests[0].payload == {"app_ref": "app-1", "window_ref": "window-1"}
    assert transport.requests[1].payload == {
        "snapshot_id": "snap-1",
        "actions": actions,
        "interaction_mode": "background",
    }
    assert transport.requests[2].payload == {
        "snapshot_id": "snap-1",
        "actions": actions,
        "interaction_mode": "background",
        "plan_ref": "plan-1",
    }
    assert all(request.operation != "focus" for request in transport.requests)
    assert all("app_ref" not in request.payload for request in transport.requests[1:])


def test_well_formed_helper_application_error_preserves_bounded_typed_code():
    from agent.runtime.macos_computer import HelperApplicationError

    async def request(request):
        return ComputerResponse(
            request.request_id,
            ok=False,
            error=ComputerError(
                ComputerErrorCode.TARGET_NOT_FRONTMOST,
                "PRIVATE-NATIVE-MESSAGE",
            ),
        )

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request

    with pytest.raises(HelperApplicationError) as failure:
        run(MacComputerBackend(transport).select("app-1", "window-1"))

    assert failure.value.error == ComputerError(
        ComputerErrorCode.TARGET_NOT_FRONTMOST,
        "PRIVATE-NATIVE-MESSAGE",
    )
    assert str(failure.value) == ComputerErrorCode.TARGET_NOT_FRONTMOST.value


def test_takeover_required_plan_is_validated_and_returned_without_losing_snapshot_path():
    async def request(request):
        return ComputerResponse(
            request.request_id,
            ok=False,
            result={
                "plan_ref": "plan-takeover",
                "interaction_mode": "background",
                "requires_takeover": True,
                "reason": "foreground_takeover_required",
                "action_classes": ["click"],
                "pid_action_classes": ["click"],
                "last_acknowledged_action": -1,
            },
            error=ComputerError(
                ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED,
                "takeover needed",
            ),
        )

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request

    plan = run(MacComputerBackend(transport).plan_actions(
        ComputerTarget("app-1", "window-1"),
        "snap-1",
        [{"type": "click", "element_ref": "ax-1"}],
        ComputerInteractionMode.BACKGROUND,
    ))

    assert plan.plan_ref == "plan-takeover"
    assert plan.requires_takeover is True
    assert plan.interaction_mode is ComputerInteractionMode.BACKGROUND
    assert plan.pid_action_classes == ("click",)


@pytest.mark.parametrize('mode,anchored,message,expected', [
    ('background', False, 'action payload is invalid', 'background_action_unsupported'),
    ('background', True, 'action payload is invalid', 'protocol_mismatch'),
    ('foreground_takeover', False, 'action payload is invalid', 'protocol_mismatch'),
    ('background', False, 'plan_actions contains an invalid action', 'protocol_mismatch'),
])
def test_unanchored_background_pointer_rejection_has_actionable_recovery(mode, anchored, message, expected):
    from agent.runtime.macos_computer import HelperApplicationError
    from agent.runtime.tools.computer import _plan_rejection_failure

    requests = []
    async def request(req):
        requests.append(req.operation)
        return ComputerResponse(req.request_id, ok=False, error=ComputerError(
            ComputerErrorCode.PROTOCOL_MISMATCH, message,
        ))

    transport = StaticTransport(ComputerResponse('unused', ok=True))
    transport.request = request
    action = {'type': 'click', 'x': 700, 'y': 760}
    if anchored:
        action['target_element_ref'] = 'snap-1:editor'
    with pytest.raises(HelperApplicationError) as failure:
        run(MacComputerBackend(transport).plan_actions(
            ComputerTarget('edge', 'quiz'), 'snap-1', [action], ComputerInteractionMode(mode),
        ))
    assert failure.value.error.code.value == expected
    assert requests == ['plan_actions']  # No act or automatic mode switch.
    if expected == 'background_action_unsupported':
        recovery = _plan_rejection_failure(failure.value, requested_takeover=False)
        assert recovery.code == 'foreground_takeover_required'
        assert "interaction_mode='foreground_takeover'" in recovery.recovery_hint


def test_no_summary_pointer_plan_rejection_preserves_typed_application_error():
    from agent.runtime.macos_computer import HelperApplicationError

    native_result = {"outcomes": [], "last_acknowledged_action": -1}

    async def request(request):
        return ComputerResponse(
            request.request_id,
            ok=False,
            result=native_result,
            error=ComputerError(
                ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED,
                "PRIVATE-NATIVE-POINTER-MESSAGE",
            ),
        )

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request

    with pytest.raises(HelperApplicationError) as failure:
        run(MacComputerBackend(transport).plan_actions(
            ComputerTarget("wps-app", "wps-window"),
            "wps-snapshot",
            [{
                "type": "click",
                "x": 756.0,
                "y": 313.0,
                "target_element_ref": "wps-snapshot:canvas",
            }],
            ComputerInteractionMode.BACKGROUND,
        ))

    assert failure.value.error.code is ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED
    assert failure.value.result == native_result
    assert "PRIVATE-NATIVE-POINTER-MESSAGE" not in str(failure.value)


def test_malformed_plan_result_surfaces_typed_error_without_leaking_message():
    from agent.runtime.macos_computer import HelperApplicationError

    async def request(request):
        return ComputerResponse(
            request.request_id,
            ok=False,
            result={"last_acknowledged_action": -1},
            error=ComputerError(ComputerErrorCode.TARGET_GONE, "PRIVATE-NATIVE-MESSAGE"),
        )

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request

    with pytest.raises(HelperApplicationError) as failure:
        run(MacComputerBackend(transport).plan_actions(
            ComputerTarget("app-1", "window-1"),
            "snap-1",
            [{"type": "click", "element_ref": "ax-1"}],
            ComputerInteractionMode.BACKGROUND,
        ))

    assert failure.value.error.code is ComputerErrorCode.TARGET_GONE
    assert "PRIVATE-NATIVE-MESSAGE" not in str(failure.value)


def test_mac_backend_rejects_mismatched_read_id_and_treats_mismatched_act_as_unknown():
    target = ComputerTarget("app-1", "window-1")
    transport = StaticTransport(ComputerResponse("another-request", ok=True, result={}))

    with pytest.raises(HelperTransportError, match="request_id"):
        run(MacComputerBackend(transport).status())

    action_result = run(mac_background_act(MacComputerBackend(transport), target, "snap-1", [{"type": "click", "element_ref": "ax-1"}]))
    assert action_result.error.code is ComputerErrorCode.UNKNOWN_OUTCOME


def test_mac_backend_treats_post_dispatch_failed_response_without_error_as_unknown():
    target = ComputerTarget("app-1", "window-1")

    async def request(request):
        return ComputerResponse(request.request_id, ok=False)

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request
    result = run(mac_background_act(MacComputerBackend(transport), target, "snap-1", [{"type": "click", "element_ref": "ax-1"}]))

    assert result.error.code is ComputerErrorCode.UNKNOWN_OUTCOME
    assert result.result["last_acknowledged_action"] == -1


@pytest.mark.parametrize(
    ("name", "ok", "result", "error"),
    [
        ("outcomes_string", True, {"outcomes": "bad", "last_acknowledged_action": 1}, None),
        ("ack_out_of_range", True, {"outcomes": [], "last_acknowledged_action": 999}, None),
        ("extra_result_field", True, {"outcomes": [], "last_acknowledged_action": -1, "extra": True}, None),
        ("boolean_ack", True, {"outcomes": [], "last_acknowledged_action": False}, None),
        ("boolean_index", True, {"outcomes": [{"index": False, "ok": True}], "last_acknowledged_action": 0}, None),
        ("non_boolean_ok", True, {"outcomes": [{"index": 0, "ok": "yes"}], "last_acknowledged_action": 0}, None),
        (
            "successful_outcome_with_error_code",
            True,
            {"outcomes": [{"index": 0, "ok": True, "error_code": "helper_failed"}], "last_acknowledged_action": 0},
            None,
        ),
        (
            "failed_outcome_without_error_code",
            False,
            {"outcomes": [{"index": 0, "ok": False}], "last_acknowledged_action": -1},
            ComputerError(ComputerErrorCode.HELPER_FAILED, "failed"),
        ),
        (
            "extra_outcome_field",
            True,
            {"outcomes": [{"index": 0, "ok": True, "extra": True}], "last_acknowledged_action": 0},
            None,
        ),
        (
            "noncontiguous_indices",
            False,
            {"outcomes": [{"index": 1, "ok": False, "error_code": "helper_failed"}], "last_acknowledged_action": -1},
            ComputerError(ComputerErrorCode.HELPER_FAILED, "failed"),
        ),
        (
            "successful_response_with_unacknowledged_action",
            True,
            {"outcomes": [{"index": 0, "ok": True}, {"index": 1, "ok": True}], "last_acknowledged_action": 0},
            None,
        ),
        (
            "unknown_outcome_acknowledges_current_action",
            False,
            {"outcomes": [{"index": 0, "ok": False, "error_code": "unknown_outcome"}], "last_acknowledged_action": 0},
            ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown"),
        ),
        (
            "unknown_outcome_missing_current_outcome",
            False,
            {"outcomes": [], "last_acknowledged_action": -1},
            ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown"),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_mac_backend_treats_malformed_post_dispatch_action_result_as_unknown(name, ok, result, error):
    del name
    target = ComputerTarget("app-1", "window-1")

    async def request(request):
        return ComputerResponse(request.request_id, ok=ok, result=result, error=error)

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request
    actions = [{"type": "wait", "duration_ms": 0}, {"type": "wait", "duration_ms": 0}]
    action_result = run(mac_background_act(MacComputerBackend(transport), target, "snap-1", actions))

    assert action_result.error.code is ComputerErrorCode.UNKNOWN_OUTCOME


def test_malformed_action_response_preserves_only_independently_validated_success_prefix():
    target = ComputerTarget("app-1", "window-1")

    async def request(request):
        return ComputerResponse(
            request.request_id,
            ok=True,
            result={
                "outcomes": [
                    {"index": 0, "ok": True},
                    {"index": 1, "ok": True},
                    {"index": 9, "ok": True},
                ],
                "last_acknowledged_action": 1,
            },
        )

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request
    action_result = run(mac_background_act(
        MacComputerBackend(transport),
        target,
        "snap-1",
        [{"type": "wait", "duration_ms": 0}] * 3,
    ))

    assert action_result.error.code is ComputerErrorCode.UNKNOWN_OUTCOME
    assert action_result.result == {
        "outcomes": [{"index": 0, "ok": True}, {"index": 1, "ok": True}],
        "last_acknowledged_action": 1,
    }


@pytest.mark.parametrize("diagnostics", [None, {
    "stage": "before_key_up", "cause": "stale_snapshot",
    "input_may_have_started": True, "cleanup_failed": False,
}])
def test_mac_backend_accepts_exact_success_and_unknown_action_result_semantics(diagnostics):
    target = ComputerTarget("app-1", "window-1")
    failed_outcome = {"index": 0, "ok": False, "error_code": "unknown_outcome"}
    if diagnostics is not None:
        failed_outcome["input_diagnostics"] = diagnostics
    replies = [
        ComputerResponse(
            "unused",
            ok=True,
            result={"outcomes": [{"index": 0, "ok": True}], "last_acknowledged_action": 0},
        ),
        ComputerResponse(
            "unused",
            ok=False,
            result={
                "outcomes": [failed_outcome],
                "last_acknowledged_action": -1,
            },
            error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown"),
        ),
    ]

    async def request(request):
        reply = replies.pop(0)
        return ComputerResponse(request.request_id, ok=reply.ok, result=reply.result, error=reply.error)

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request
    success = run(mac_background_act(MacComputerBackend(transport), target, "snap-1", [{"type": "wait", "duration_ms": 0}]))
    unknown = run(mac_background_act(MacComputerBackend(transport), target, "snap-2", [{"type": "wait", "duration_ms": 0}]))

    assert success.ok is True
    assert success.result == {"outcomes": [{"index": 0, "ok": True}], "last_acknowledged_action": 0}
    assert unknown.error.code is ComputerErrorCode.UNKNOWN_OUTCOME
    assert unknown.result["last_acknowledged_action"] == -1
    assert unknown.result["outcomes"] == [failed_outcome]
    from agent.runtime.tools.computer import _action_failure
    public_failure = _action_failure(unknown.error, unknown.result)
    assert public_failure.details["outcomes"] == [failed_outcome]
    assert "may already have been sent" in public_failure.message


@pytest.mark.parametrize(
    ("name", "outcomes", "acknowledgement", "error"),
    [
        (
            "partial_success_then_stale",
            [
                {"index": 0, "ok": True},
                {"index": 1, "ok": False, "error_code": "stale_snapshot"},
            ],
            0,
            ComputerError(ComputerErrorCode.STALE_SNAPSHOT, "PRIVATE-NATIVE-STALE"),
        ),
        (
            "keyboard_focus_checkpoint",
            [{"index": 0, "ok": True, "observation_required": True}],
            0,
            ComputerError(ComputerErrorCode.OBSERVATION_REQUIRED, "PRIVATE-NATIVE-CHECKPOINT"),
        ),
        (
            "post_delivery_pause_with_unknown_outcome",
            [{"index": 0, "ok": False, "error_code": "unknown_outcome"}],
            -1,
            ComputerError(ComputerErrorCode.USER_ACTIVITY_PAUSED, "PRIVATE-NATIVE-PAUSE"),
        ),
        (
            "prefix_then_keyboard_focus_changed_before_down",
            [
                {"index": 0, "ok": True},
                {"index": 1, "ok": False, "error_code": "unknown_outcome", "input_diagnostics": {
                    "stage": "before_key_down", "cause": "stale_snapshot",
                    "input_may_have_started": False, "cleanup_failed": False,
                }},
            ],
            0,
            ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "PRIVATE-NATIVE-STALE"),
        ),
        (
            "prefix_then_keyboard_paused_before_down",
            [
                {"index": 0, "ok": True},
                {"index": 1, "ok": False, "error_code": "user_activity_paused", "input_diagnostics": {
                    "stage": "before_key_down", "cause": "user_activity_paused",
                    "input_may_have_started": False, "cleanup_failed": False,
                }},
            ],
            0,
            ComputerError(ComputerErrorCode.USER_ACTIVITY_PAUSED, "PRIVATE-NATIVE-PAUSE"),
        ),
        (
            "fully_pre_input_stale",
            [{"index": 0, "ok": False, "error_code": "stale_snapshot"}],
            -1,
            ComputerError(ComputerErrorCode.STALE_SNAPSHOT, "PRIVATE-NATIVE-STALE"),
        ),
    ],
)
def test_mac_backend_returns_every_protocol_valid_failed_act_receipt(
    name,
    outcomes,
    acknowledgement,
    error,
):
    target = ComputerTarget("app-1", "window-1")
    requests = []

    async def request(request):
        requests.append(request)
        return ComputerResponse(
            request.request_id,
            ok=False,
            result={
                "outcomes": outcomes,
                "last_acknowledged_action": acknowledgement,
            },
            error=error,
        )

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request
    action_result = run(mac_background_act(
        MacComputerBackend(transport),
        target,
        "snap-1",
        [{"type": "wait", "duration_ms": 0}] * (len(outcomes) + (name == "keyboard_focus_checkpoint")),
    ))

    assert action_result.error == error
    assert action_result.result == {
        "outcomes": outcomes,
        "last_acknowledged_action": acknowledgement,
    }
    assert [native_request.operation for native_request in requests] == ["act"]


def test_mac_backend_rejects_non_string_target_reference_from_helper():
    async def request(request):
        return ComputerResponse(request.request_id, ok=True, result={"app_ref": None, "window_ref": "window-1"})

    transport = StaticTransport(ComputerResponse("unused", ok=True))
    transport.request = request
    with pytest.raises(HelperTransportError, match="target"):
        run(MacComputerBackend(transport).select("app-1", "window-1"))


@requires_posix_authority
def test_mac_backend_binds_helper_to_session_artifact_and_checks_returned_name(tmp_path):
    artifact_directory = os.open(tmp_path, os.O_RDONLY)

    class ArtifactTransport:
        def __init__(self):
            self.directory_fds = []

        async def configure_artifact_directory(self, directory_fd, directory_path=None):
            self.directory_fds.append((directory_fd, directory_path))

        async def request(self, request):
                return ComputerResponse(
                    request.request_id,
                    ok=True,
                    snapshot=ComputerSnapshot("snapshot-1", {
                        "image_artifact": "snapshot-safe.png",
                        "logical_size": {"width": 1, "height": 1},
                        "pixel_size": {"width": 1, "height": 1},
                        "backing_scale": 1,
                        "capture_bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
                        "ax_tree": {},
                    }),
                )

        async def close(self):
            return None

    try:
        transport = ArtifactTransport()
        snapshot = run(
            MacComputerBackend(transport).snapshot(
                ComputerTarget("app-1", "window-1"),
                "target_window",
                ComputerArtifact("snapshot-safe.png", artifact_directory, tmp_path),
            )
        )
    finally:
        os.close(artifact_directory)

    assert snapshot.snapshot_id == "snapshot-1"
    assert transport.directory_fds == [(artifact_directory, tmp_path)]


@requires_posix_authority
def test_helper_transport_passes_only_verified_held_artifact_path(monkeypatch, tmp_path):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return _TransportProcess(lambda request: json.dumps({
            "protocol_version": 2,
            "request_id": request["request_id"],
            "ok": True,
            "result": {"ready": True},
        }))

    monkeypatch.setattr("agent.runtime.macos_computer.asyncio.create_subprocess_exec", spawn)
    directory_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        async def scenario():
            transport = HelperTransport(_owned_helper(tmp_path))
            await transport.configure_artifact_directory(directory_fd, tmp_path)
            await transport.request(ComputerRequest("req-path", "status"))
            await transport.close()

        run(scenario())
    finally:
        os.close(directory_fd)

    assert calls[0][1]["env"]["ASTRA_COMPUTER_ARTIFACT_DIR_FD"] == str(directory_fd)
    assert calls[0][1]["env"]["ASTRA_COMPUTER_ARTIFACT_DIR_PATH"] == str(tmp_path.resolve())


@requires_posix_authority
def test_macos_helper_override_must_be_owned_absolute_regular_executable(tmp_path, monkeypatch):
    helper = tmp_path / "AstraMacComputerHelper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setenv("ASTRA_COMPUTER_HELPER_PATH", str(helper))
    assert resolve_macos_helper() == helper.resolve()

    monkeypatch.setenv("ASTRA_COMPUTER_HELPER_PATH", "relative-helper")
    with pytest.raises(HelperTransportError, match="absolute"):
        resolve_macos_helper()

    linked = tmp_path / "linked-helper"
    linked.symlink_to(helper)
    monkeypatch.setenv("ASTRA_COMPUTER_HELPER_PATH", str(linked))
    with pytest.raises(HelperTransportError, match="symlink"):
        resolve_macos_helper()

    linked_parent = tmp_path / "helper-parent"
    linked_parent.symlink_to(tmp_path)
    monkeypatch.setenv("ASTRA_COMPUTER_HELPER_PATH", str(linked_parent / helper.name))
    with pytest.raises(HelperTransportError, match="symlink"):
        resolve_macos_helper()

    helper.chmod(0o600)
    monkeypatch.setenv("ASTRA_COMPUTER_HELPER_PATH", str(helper))
    with pytest.raises(HelperTransportError, match="executable"):
        resolve_macos_helper()


@requires_posix_authority
def test_macos_helper_override_rejects_directory_and_wrong_owner(tmp_path, monkeypatch):
    directory = tmp_path / "directory-helper"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("ASTRA_COMPUTER_HELPER_PATH", str(directory))
    with pytest.raises(HelperTransportError, match="regular"):
        resolve_macos_helper()

    helper = _owned_helper(tmp_path)
    monkeypatch.setenv("ASTRA_COMPUTER_HELPER_PATH", str(helper))
    monkeypatch.setattr("agent.runtime.macos_computer.os.getuid", lambda: helper.stat().st_uid + 1)
    with pytest.raises(HelperTransportError, match="ownership"):
        resolve_macos_helper()


def test_mac_backend_passes_snapshot_id_and_actions_to_transport():
    target = ComputerTarget("app-1", "window-1")
    transport = StaticTransport(ComputerResponse("placeholder", ok=True, result={}))

    async def scenario():
        async def request(request):
            transport.requests.append(request)
            return ComputerResponse(
                request.request_id,
                ok=True,
                result={"outcomes": [{"index": 0, "ok": True}], "last_acknowledged_action": 0},
            )

        transport.request = request
        result = await mac_background_act(
            MacComputerBackend(transport),
            target, "snap-1", [{"type": "click", "element_ref": "ax-1"}],
        )
        return result

    result = run(scenario())
    assert result.ok is True
    assert result.snapshot is None
    request = transport.requests[0]
    assert request.operation == "act"
    assert "app_ref" not in request.payload
    assert "window_ref" not in request.payload
    assert request.payload["snapshot_id"] == "snap-1"
    assert request.payload["interaction_mode"] == "background"
    assert request.payload["plan_ref"] == "plan-test"
    assert request.payload["actions"] == [{"type": "click", "element_ref": "ax-1"}]


class _TransportStdin:
    def __init__(self, process: _TransportProcess, response_factory) -> None:
        self._process = process
        self._response_factory = response_factory
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)
        request = json.loads(payload)
        reply = self._response_factory(request)
        if reply is None:
            return
        if reply == "exit":
            self._process.returncode = 1
            self._process.stdout.feed_eof()
            return
        self._process.stdout.feed_data(reply.encode("utf-8") + b"\n")

    async def drain(self) -> None:
        return None


class _TransportProcess:
    def __init__(self, response_factory) -> None:
        self.returncode = None
        self.pid = 0
        self.stdout = asyncio.StreamReader(limit=4 * 1024 * 1024 + 1)
        self.stderr = asyncio.StreamReader()
        self.stdin = _TransportStdin(self, response_factory)
        self.terminated = 0
        self.killed = 0
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = -15
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.wait_calls += 1
        return self.returncode if self.returncode is not None else 0


class _BlockingStopProcess(_TransportProcess):
    def __init__(self) -> None:
        super().__init__(lambda _request: None)
        self.stop_started = asyncio.Event()
        self.allow_stop = asyncio.Event()

    async def wait(self) -> int:
        self.stop_started.set()
        await self.allow_stop.wait()
        return self.returncode if self.returncode is not None else 0


def test_helper_close_finishes_teardown_after_caller_cancellation(monkeypatch, tmp_path):
    holder = []

    async def spawn(*_args, **_kwargs):
        process = _BlockingStopProcess()
        holder.append(process)
        return process

    monkeypatch.setattr("agent.runtime.macos_computer.asyncio.create_subprocess_exec", spawn)

    async def scenario():
        transport = HelperTransport(_owned_helper(tmp_path))
        await transport._ensure_process_locked()
        process = holder[0]
        close = asyncio.create_task(transport.close())
        await process.stop_started.wait()
        await asyncio.sleep(0)
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert transport._proc is process
        assert transport._closed is False
        process.allow_stop.set()
        await transport.close()
        return transport, process

    transport, process = run(scenario())
    assert transport._proc is None
    assert transport._closed is True
    assert process.terminated == 1


@requires_posix_authority
@pytest.mark.parametrize("later_state", ["missing", "denied", "alive"])
def test_helper_group_exit_retries_permission_denial_within_original_deadline(
    monkeypatch, later_state,
):
    import agent.runtime.macos_computer as module

    clock = SimpleNamespace(now=0.0)
    probes = []

    def killpg(pgid, signal_number):
        assert (pgid, signal_number) == (4242, 0)
        probes.append(clock.now)
        if len(probes) == 1 or later_state == "denied":
            raise PermissionError(errno.EPERM, "group exit is not yet observable")
        if later_state == "missing":
            raise ProcessLookupError(errno.ESRCH, "group has exited")

    async def sleep(delay):
        clock.now += delay

    monkeypatch.setattr(module.os, "killpg", killpg)
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now),
        sleep=sleep,
    ))

    exited = run(HelperTransport._wait_for_group_exit(4242))

    assert exited is (later_state == "missing")
    assert len(probes) > 1
    if later_state == "missing":
        assert clock.now == pytest.approx(0.05)
    else:
        assert 2 <= clock.now < 2.1


@requires_posix_authority
def test_helper_close_kills_owned_group_after_leader_already_exited(monkeypatch, tmp_path):
    signals = []
    process_holder = []

    async def spawn(*_args, **_kwargs):
        process = _TransportProcess(lambda _request: None)
        process.pid = 4242
        process.returncode = 1
        process_holder.append(process)
        return process

    def killpg(pgid, signal_number):
        signals.append((pgid, signal_number))

    waits = iter([False, True])

    async def wait_for_group_exit(_self, _pgid):
        return next(waits)

    monkeypatch.setattr("agent.runtime.macos_computer.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("agent.runtime.macos_computer.os.killpg", killpg)
    monkeypatch.setattr(HelperTransport, "_wait_for_group_exit", wait_for_group_exit)

    async def scenario():
        transport = HelperTransport(_owned_helper(tmp_path))
        # Make the lazily started process owned by the transport without
        # issuing a request whose cancelled reader would also stop it.
        await transport._ensure_process_locked()
        await transport.close()

    run(scenario())
    assert process_holder[0].returncode == 1
    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


@requires_posix_authority
def test_helper_close_reaps_leader_after_its_owned_group_exits(monkeypatch, tmp_path):
    holder = []

    async def spawn(*_args, **_kwargs):
        process = _TransportProcess(lambda _request: None)
        process.pid = 4343
        holder.append(process)
        return process

    async def wait_for_group_exit(_self, _pgid):
        return True

    monkeypatch.setattr("agent.runtime.macos_computer.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("agent.runtime.macos_computer.os.killpg", lambda *_args: None)
    monkeypatch.setattr(HelperTransport, "_wait_for_group_exit", wait_for_group_exit)

    async def scenario():
        transport = HelperTransport(_owned_helper(tmp_path))
        await transport._ensure_process_locked()
        await transport.close()

    run(scenario())
    assert holder[0].wait_calls == 1


def _owned_helper(tmp_path: Path) -> Path:
    if os.name == "nt":
        pytest.skip("owned native helper fixtures require POSIX executable permissions")
    helper = tmp_path / "helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o700)
    return helper


def test_helper_transport_uses_safe_environment_and_matching_ndjson(monkeypatch, tmp_path):
    calls = []

    def response_factory(request):
            return json.dumps({"protocol_version": 4, "request_id": request["request_id"], "ok": True, "result": {"ready": True}})

    process_holder = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        process = _TransportProcess(response_factory)
        process_holder.append(process)
        return process

    monkeypatch.setattr("agent.runtime.macos_computer.asyncio.create_subprocess_exec", spawn)

    async def scenario():
        transport = HelperTransport(_owned_helper(tmp_path))
        response = await transport.request(ComputerRequest("req-1", "status"))
        await transport.close()
        return response

    response = run(scenario())
    assert response.result == {"ready": True}
    assert calls[0][0] == (str(_owned_helper(tmp_path)),)
    assert calls[0][1]["env"] == {"PATH": os.defpath, "LANG": "en_US.UTF-8"}
    assert calls[0][1]["start_new_session"] is True
    process = process_holder[0]
    assert process.stdin.writes == [b'{"protocol_version":4,"request_id":"req-1","operation":"status","payload":{}}\n']


@pytest.mark.parametrize("reply", ["not-json", '{"protocol_version":1,"request_id":"wrong","ok":true}'])
def test_helper_transport_rejects_malformed_or_mismatched_frames(monkeypatch, tmp_path, reply):
    process_holder = []

    async def spawn(*_args, **_kwargs):
        process = _TransportProcess(lambda _request: reply)
        process_holder.append(process)
        return process

    monkeypatch.setattr("agent.runtime.macos_computer.asyncio.create_subprocess_exec", spawn)
    transport = HelperTransport(_owned_helper(tmp_path))
    with pytest.raises(HelperTransportError):
        run(transport.request(ComputerRequest("req-1", "status")))
    process = process_holder[0]
    assert process.terminated == 1


def test_helper_transport_timeout_and_exit_close_the_owned_process(monkeypatch, tmp_path):
    responses = iter([lambda _request: None, lambda _request: "exit"])
    process_holder = []

    async def spawn(*_args, **_kwargs):
        process = _TransportProcess(next(responses))
        process_holder.append(process)
        return process

    monkeypatch.setattr("agent.runtime.macos_computer.asyncio.create_subprocess_exec", spawn)

    async def scenario():
        first = HelperTransport(_owned_helper(tmp_path), timeout=0.001)
        with pytest.raises(HelperTransportError, match="request failed"):
            await first.request(ComputerRequest("req-timeout", "status"))
        second = HelperTransport(_owned_helper(tmp_path))
        with pytest.raises(HelperTransportError, match="complete response"):
            await second.request(ComputerRequest("req-exit", "status"))

    run(scenario())
    timed_out, exited = process_holder
    assert timed_out.terminated == 1
    assert exited.terminated == 0


def test_helper_transport_cancellation_stops_write_without_replay(monkeypatch, tmp_path):
    process_holder = []

    async def spawn(*_args, **_kwargs):
        process = _TransportProcess(lambda _request: None)
        process_holder.append(process)
        return process

    monkeypatch.setattr("agent.runtime.macos_computer.asyncio.create_subprocess_exec", spawn)

    async def scenario():
        transport = HelperTransport(_owned_helper(tmp_path))
        task = asyncio.create_task(transport.request(ComputerRequest("req-1", "act", {
            "interaction_mode": "background",
            "snapshot_id": "snapshot-1",
            "plan_ref": "plan-1",
            "actions": [],
        })))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    process = process_holder[0]
    assert len(process.stdin.writes) == 1
    assert process.terminated == 1


@pytest.mark.parametrize("restore", [True, False])
def test_mac_backend_takeover_end_carries_focus_policy(restore):
    transport = StaticTransport(ComputerResponse("unused", ok=True))
    payloads = []

    async def request(request):
        payloads.append(request.payload)
        return ComputerResponse(request.request_id, ok=True, result={
            "started": True, "restoration": "preserved_user_focus",
        })

    transport.request = request
    run(MacComputerBackend(transport).end_takeover("takeover-1", restore_previous_focus=restore))
    assert payloads == [{"takeover_ref": "takeover-1", **({} if restore else {"restore_previous_focus": False})}]
