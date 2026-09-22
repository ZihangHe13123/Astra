"""Platform-neutral state ownership for Computer Use backends.

This module deliberately contains no platform APIs.  It makes the lifetime of
window and accessibility references explicit so every backend has the same
fail-closed session semantics.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import math
import os
import stat
import struct
import sys
import time
import uuid
import zlib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol, TypeGuard, runtime_checkable

try:
    import fcntl as _fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised in an isolated import test
    _fcntl = None

from .computer_protocol import (
    DISPATCH_ACTION_CLASSES,
    ComputerAction,
    ComputerError,
    ComputerErrorCode,
    ComputerInteractionMode,
    ComputerSnapshot,
    ComputerSnapshotTextDetailMode,
    ForegroundFragmentDeclaration,
    ForegroundFragmentPlanRequest,
    FragmentStageAuthority,
    FragmentStageCommit,
    FragmentStageCommitResult,
    decode_text_detail_envelope,
    validate_snapshot_payload,
)


class ComputerSessionError(RuntimeError):
    """A stable session-boundary failure safe to show to callers."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


_LOCK_EX = _fcntl.LOCK_EX if os.name == "posix" and _fcntl is not None else 0
_LOCK_SH = _fcntl.LOCK_SH if os.name == "posix" and _fcntl is not None else 0
_LOCK_NB = _fcntl.LOCK_NB if os.name == "posix" and _fcntl is not None else 0
_LOCK_UN = _fcntl.LOCK_UN if os.name == "posix" and _fcntl is not None else 0
_MAX_WINDOW_IDENTITY_REF_SCALARS = 256


def _flock(descriptor: int, operation: int) -> None:
    if os.name != "posix" or _fcntl is None:
        raise ComputerSessionError(
            "unsupported_platform",
            "unsupported_platform: Computer Use cache leases require POSIX file locks",
        )
    _fcntl.flock(descriptor, operation)


def _owner_uid() -> int:
    if os.name != "posix":
        raise ComputerSessionError("unsupported_platform", "Computer Use cache ownership requires POSIX")
    return os.getuid()


def _owner_chmod(descriptor: int, mode: int) -> None:
    if os.name != "posix":
        raise ComputerSessionError("unsupported_platform", "Computer Use cache permissions require POSIX")
    os.fchmod(descriptor, mode)


@dataclass(frozen=True)
class ComputerTarget:
    """Opaque application/window references selected for one session."""

    app_ref: str
    window_ref: str

    def __post_init__(self) -> None:
        for name, value in (("app_ref", self.app_ref), ("window_ref", self.window_ref)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class ComputerAppCatalog:
    """One successfully decoded native application catalog."""

    generation: int
    apps: tuple[Mapping[str, object], ...]
    confirmed_absent_window_identity_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise TypeError("generation must be an integer")
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if not isinstance(self.apps, tuple) or not all(isinstance(app, Mapping) for app in self.apps):
            raise ValueError("apps must be a tuple of objects")
        refs = self.confirmed_absent_window_identity_refs
        if (
            not isinstance(refs, tuple) or len(refs) > 200
            or any(
                not isinstance(ref, str) or not ref
                or len(ref) > _MAX_WINDOW_IDENTITY_REF_SCALARS
                or any(0xD800 <= ord(character) <= 0xDFFF for character in ref)
                for ref in refs
            )
            or len(set(refs)) != len(refs)
        ):
            raise ValueError("confirmed_absent_window_identity_refs must be a tuple of at most 200 unique valid identity refs")


@dataclass(frozen=True)
class ComputerBackendAppState:
    """The exact target and snapshot returned by one catalog-bound read."""

    catalog_generation: int
    target: ComputerTarget
    snapshot: ComputerSnapshot

    def __post_init__(self) -> None:
        if isinstance(self.catalog_generation, bool) or not isinstance(self.catalog_generation, int):
            raise TypeError("catalog_generation must be an integer")
        if self.catalog_generation <= 0:
            raise ValueError("catalog_generation must be positive")
        if not isinstance(self.target, ComputerTarget):
            raise TypeError("target must be a ComputerTarget")
        if not isinstance(self.snapshot, ComputerSnapshot):
            raise TypeError("snapshot must be a ComputerSnapshot")


@dataclass(frozen=True)
class ComputerVerifiedAppState:
    """One catalog-bound target whose snapshot artifacts passed manager verification."""

    target: ComputerTarget
    target_generation: int
    snapshot: ComputerSnapshot
    image_path: Path
    image_data: bytes
    image_identity: tuple[int, int]
    image_sha256: str
    detail_path: Path | None = None
    detail_data: bytes | None = None
    detail_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, ComputerTarget):
            raise TypeError("target must be a ComputerTarget")
        if (
            isinstance(self.target_generation, bool)
            or not isinstance(self.target_generation, int)
            or self.target_generation <= 0
        ):
            raise ValueError("target_generation must be a positive integer")
        if not isinstance(self.snapshot, ComputerSnapshot):
            raise TypeError("snapshot must be a ComputerSnapshot")
        if not isinstance(self.image_path, Path) or not self.image_path.is_absolute():
            raise ValueError("image_path must be an absolute Path")
        if not isinstance(self.image_data, bytes):
            raise TypeError("image_data must be bytes")
        if (
            not isinstance(self.image_identity, tuple)
            or len(self.image_identity) != 2
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in self.image_identity)
        ):
            raise ValueError("image_identity must contain device and inode integers")
        if (
            not isinstance(self.image_sha256, str)
            or len(self.image_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.image_sha256)
        ):
            raise ValueError("image_sha256 must be a lowercase SHA-256 digest")
        detail_values = (self.detail_path, self.detail_data, self.detail_sha256)
        if any(value is not None for value in detail_values):
            if not isinstance(self.detail_path, Path) or not self.detail_path.is_absolute():
                raise ValueError("detail_path must be an absolute Path")
            if not isinstance(self.detail_data, bytes):
                raise TypeError("detail_data must be bytes")
            if (
                not isinstance(self.detail_sha256, str)
                or len(self.detail_sha256) != 64
                or any(character not in "0123456789abcdef" for character in self.detail_sha256)
            ):
                raise ValueError("detail_sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class ComputerSnapshotTargetBinding:
    """Exact session target authority for one approved snapshot request."""

    session_id: str
    app_ref: str
    window_ref: str
    generation: int

    def __post_init__(self) -> None:
        for name, value in (
            ("session_id", self.session_id),
            ("app_ref", self.app_ref),
            ("window_ref", self.window_ref),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise TypeError("generation must be an integer")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")


@dataclass(frozen=True)
class ComputerActionResult:
    """Backend result for one ordered action batch."""

    result: Mapping[str, object] | None = None
    snapshot: ComputerSnapshot | None = None
    error: ComputerError | None = None

    def __post_init__(self) -> None:
        if self.result is not None and not isinstance(self.result, Mapping):
            raise ValueError("result must be an object")
        if self.snapshot is not None and not isinstance(self.snapshot, ComputerSnapshot):
            raise ValueError("snapshot must be a ComputerSnapshot")
        if self.error is not None and not isinstance(self.error, ComputerError):
            raise ValueError("error must be a ComputerError")

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class ComputerActionPlan:
    """Opaque, bounded authorization for one exact cooperative action batch."""

    plan_ref: str
    interaction_mode: ComputerInteractionMode
    requires_takeover: bool
    reason: str
    action_classes: tuple[str, ...]
    pid_action_classes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan_ref, str) or not self.plan_ref:
            raise ValueError("plan_ref must be a non-empty string")
        if not isinstance(self.interaction_mode, ComputerInteractionMode):
            raise TypeError("interaction_mode must be a ComputerInteractionMode")
        if not isinstance(self.requires_takeover, bool):
            raise TypeError("requires_takeover must be a boolean")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason must be a non-empty string")
        for name, values in (
            ("action_classes", self.action_classes),
            ("pid_action_classes", self.pid_action_classes),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > len(DISPATCH_ACTION_CLASSES)
                or not all(
                    isinstance(item, str) and item in DISPATCH_ACTION_CLASSES
                    for item in values
                )
                or len(set(values)) != len(values)
            ):
                raise ValueError(f"{name} must contain unique supported action classes")
        if not set(self.pid_action_classes).issubset(self.action_classes):
            raise ValueError("pid_action_classes must be a subset of action_classes")


@dataclass(frozen=True)
class _PendingActionPlan:
    plan: ComputerActionPlan
    target: ComputerTarget
    snapshot_id: str
    actions: tuple[ComputerAction, ...]
    fragment: ForegroundFragmentPlanRequest | None = None


@dataclass(frozen=True)
class ComputerArtifact:
    """A helper-only name bound to the session's held directory descriptor."""

    filename: str
    directory_fd: int
    directory_path: Path | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.filename, str)
            or not self.filename
            or "/" in self.filename
            or "\\" in self.filename
            or "\x00" in self.filename
            or Path(self.filename).name != self.filename
        ):
            raise ValueError("artifact filename must be a plain filename")
        if isinstance(self.directory_fd, bool) or not isinstance(self.directory_fd, int) or self.directory_fd < 0:
            raise ValueError("artifact directory_fd must be open")
        if self.directory_path is not None and (
            not isinstance(self.directory_path, Path) or not self.directory_path.is_absolute()
        ):
            raise ValueError("artifact directory_path must be an absolute Path")


@runtime_checkable
class ComputerBackend(Protocol):
    async def status(self) -> dict[str, object]: ...
    async def apps(self) -> ComputerAppCatalog: ...
    async def get_app_state(
        self,
        target: ComputerTarget,
        scope: str,
        artifact: ComputerArtifact,
        *,
        text_detail: ComputerSnapshotTextDetailMode = ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact: ComputerArtifact | None = None,
    ) -> ComputerBackendAppState: ...
    async def select(self, app_ref: str, window_ref: str) -> ComputerTarget: ...
    async def snapshot(
        self,
        target: ComputerTarget,
        scope: str,
        artifact: ComputerArtifact,
        *,
        text_detail: ComputerSnapshotTextDetailMode = ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact: ComputerArtifact | None = None,
        subtree: tuple[str, str] | None = None,
    ) -> ComputerSnapshot: ...
    async def plan_actions(
        self,
        target: ComputerTarget,
        snapshot_id: str,
        actions: list[ComputerAction],
        interaction_mode: ComputerInteractionMode,
        *,
        fragment: ForegroundFragmentPlanRequest | None = None,
    ) -> ComputerActionPlan: ...
    async def act(
        self,
        target: ComputerTarget,
        snapshot_id: str,
        actions: list[ComputerAction],
        *,
        interaction_mode: ComputerInteractionMode,
        plan_ref: str,
        takeover_ref: str | None = None,
        fragment_stage: FragmentStageAuthority | None = None,
    ) -> ComputerActionResult: ...
    async def close(self) -> None: ...


class ComputerSessionManager:
    """Own a single target, its current snapshot, and private ephemeral cache."""

    _CLEANUP_LOCK_NAME = ".cleanup.lock"
    _LEASE_NAME = ".lease"
    _LEGACY_STALE_SECONDS = 24 * 60 * 60
    _MAX_DETAIL_BYTES = 8 * 1024 * 1024
    _MAX_DETAIL_ARTIFACTS = 8
    _MAX_DETAIL_SESSION_BYTES = 64 * 1024 * 1024

    def __init__(
        self,
        backend: ComputerBackend,
        *,
        cache_root: Path | str,
        takeover_begin_cleanup_timeout: float = 15.0,
    ) -> None:
        # Check ownership support before creating any cache directories.
        _owner_uid()
        if (
            isinstance(takeover_begin_cleanup_timeout, bool)
            or not isinstance(takeover_begin_cleanup_timeout, (int, float))
            or takeover_begin_cleanup_timeout <= 0
        ):
            raise ValueError("takeover begin cleanup timeout must be positive")
        self._backend = backend
        self._takeover_begin_cleanup_timeout = float(takeover_begin_cleanup_timeout)
        self._cache_root, self._cache_fd = self._prepare_cache_root(Path(cache_root))
        self._cleanup_lock_fd = -1
        self._lease_fd = -1
        self._session_id = uuid.uuid4().hex
        self._session_fd = -1
        try:
            self._cleanup_lock_fd = self._open_cleanup_lock()
            _flock(self._cleanup_lock_fd, _LOCK_EX)
            try:
                self._verify_cleanup_lock_identity()
                self._cleanup_stale_sessions()
                (
                    self._session_dir,
                    self._session_identity,
                    self._session_fd,
                    self._lease_fd,
                ) = self._create_session_dir()
            finally:
                _flock(self._cleanup_lock_fd, _LOCK_UN)
        except BaseException:
            self._close_session_fd()
            self._close_cache_fd()
            raise
        self._target: ComputerTarget | None = None
        self._target_generation = 0
        self._latest_snapshot: ComputerSnapshot | None = None
        self._latest_snapshot_scope = ""
        self._handed_off = False
        self._suspended_target: ComputerTarget | None = None
        self._target_window_identity_ref: str | None = None
        self._suspended_window_identity_ref: str | None = None

        self._resume_candidate: ComputerTarget | None = None
        self._resume_catalog_refreshed = False
        self._resume_publication_id: str | None = None
        self._last_catalog: ComputerAppCatalog | None = None
        self._target_identity: tuple[str, str, str | None] | None = None
        self._pending_plan: _PendingActionPlan | None = None
        self._takeover_ref: str | None = None
        self._takeover_cleanup_ref: str | None = None
        self._fragment_declaration: ForegroundFragmentDeclaration | None = None
        self._fragment_stage: FragmentStageAuthority | None = None
        self._fragment_stage_input_completed = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._terminal_cleanup_task: asyncio.Task[object] | None = None
        self._takeover_end_task: asyncio.Task[object] | None = None
        self._backend_close_task: asyncio.Task[None] | None = None
        self._backend_directory_bound = False
        self._artifact_poisoned = False
        self._current_snapshot_artifacts: dict[
            str,
            tuple[int, int, int, int, int, int],
        ] = {}
        self._lock = asyncio.Lock()
        # The policy/tool layer owns the contents.  The session owns the
        # lifetime and clears it on every close.
        self.grants: set[str] = set()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def target(self) -> ComputerTarget | None:
        return self._target

    @property
    def snapshot_target_binding(self) -> ComputerSnapshotTargetBinding | None:
        target = self._target
        if target is None:
            return None
        return ComputerSnapshotTargetBinding(
            session_id=self._session_id,
            app_ref=target.app_ref,
            window_ref=target.window_ref,
            generation=self._target_generation,
        )

    def validate_snapshot_publication(
        self,
        snapshot: ComputerSnapshot,
        binding: ComputerSnapshotTargetBinding | None,
        resume_publication_id: str | None = None,
    ) -> None:
        """Recheck observation authority after an asynchronous image worker returns."""
        self._require_open()
        resume_owner = resume_publication_id is not None and resume_publication_id == self._resume_publication_id
        if (
            (self._handed_off and not resume_owner)
            or binding != self.snapshot_target_binding
            or self._latest_snapshot is not snapshot
        ):
            raise ComputerSessionError("stale_snapshot")

    @property
    def handed_off(self) -> bool:
        return self._handed_off

    @property
    def suspended_target(self) -> ComputerTarget | None:
        return self._suspended_target

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def catalog_generation(self) -> int:
        """Expose the active native catalog generation without a surrogate."""
        catalog = self._last_catalog
        return catalog.generation if catalog is not None else 0

    async def status(self) -> dict[str, object]:
        async with self._lock:
            self._require_open()
            await self._bind_backend_directory()
            return await self._backend.status()

    async def apps(self) -> list[dict[str, object]]:
        async with self._lock:
            self._require_open()
            self._target_generation += 1
            # Refreshing the catalog replaces the helper's opaque target
            # registry. Consume Python-side state before dispatch so failure,
            # cancellation, and close races cannot retain stale references.
            try:
                self._invalidate_cooperative_state()
            except ComputerSessionError:
                if self._artifact_poisoned:
                    self._poison_artifact_session_locked(clear_target=True)
                raise
            self.grants.clear()
            self._resume_candidate = None
            self._resume_catalog_refreshed = False
            self._resume_publication_id = None
            if not self._handed_off:
                self._target = None
                self._target_window_identity_ref = None
                self._clear_suspended_continuity()
            await self._bind_backend_directory()
            catalog = await self._backend.apps()
            apps = [dict(app) for app in catalog.apps]
            self._last_catalog = catalog
            if self._handed_off:
                self._resume_candidate = self._resume_candidate_from_catalog(
                    catalog,
                    self._suspended_window_identity_ref,
                )
                self._resume_catalog_refreshed = True
            return apps

    async def select(self, app_ref: str, window_ref: str) -> ComputerTarget:
        async with self._lock:
            self._require_open()
            self._target_generation += 1
            await self._bind_backend_directory()
            self._invalidate_cooperative_state()
            target = await self._backend.select(app_ref, window_ref)
            self._target = target
            self._handed_off = False
            self._clear_suspended_continuity()
            self._target_window_identity_ref = self._window_identity_ref_from_catalog(
                self._last_catalog,
                app_ref,
                window_ref,
            )
            self._target_identity = self._identity_from_catalog(
                self._last_catalog, app_ref, window_ref
            )
            return target

    async def invalidate_app_state_attempt_authority(self) -> None:
        """Finish attempt-start invalidation even when its caller is cancelled."""
        task = asyncio.create_task(self._invalidate_app_state_attempt_authority_owned())
        _result, cancelled = await self._await_owned_task(task)
        if cancelled:
            raise asyncio.CancelledError

    async def _invalidate_app_state_attempt_authority_owned(self) -> None:
        """Serialize attempt-start invalidation with every snapshot publication."""
        async with self._lock:
            self._require_open()
            try:
                self._invalidate_cooperative_state()
            except ComputerSessionError:
                if self._artifact_poisoned:
                    self._poison_artifact_session_locked(clear_target=True)
                raise
            self.grants.clear()

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
        """Verify and commit one exact target from the latest successful catalog."""
        async with self._lock:
            self._require_open()
            if scope not in {"target_window", "display"}:
                raise ValueError("scope must be target_window or display")
            if not isinstance(text_detail, ComputerSnapshotTextDetailMode):
                raise TypeError("text_detail must be a ComputerSnapshotTextDetailMode")

            # Every valid get-state attempt consumes prior cooperative
            # authority, including attempts that later fail catalog lookup.
            # This must precede the first await and exact-ref validation.
            try:
                self._invalidate_cooperative_state()
            except ComputerSessionError:
                if self._artifact_poisoned:
                    self._poison_artifact_session_locked(clear_target=True)
                raise
            self.grants.clear()

            catalog = self._last_catalog
            identity = self._identity_from_catalog(catalog, app_ref, window_ref)
            if catalog is None or identity is None:
                raise ComputerSessionError(
                    "catalog_required",
                    "catalog_required: enumerate applications and use exact refs from the latest catalog",
                )
            try:
                requested_target = ComputerTarget(app_ref, window_ref)
            except (TypeError, ValueError) as exc:
                raise ComputerSessionError(
                    "catalog_required",
                    "catalog_required: enumerate applications and use exact refs from the latest catalog",
                ) from exc

            # A completed act may finish its same-window observation using
            # refreshed refs. Check continuity under the manager lock: this
            # must not override a handoff, a newer selection, or a catalog
            # refresh queued between tool-side matching and this dispatch.
            if (
                recovery_catalog_generation is not None or recovery_window_identity_ref is not None
            ) and (
                self._handed_off
                or self._target is not None
                or type(recovery_catalog_generation) is not int
                or recovery_catalog_generation != catalog.generation
                or self._resume_candidate_from_catalog(catalog, recovery_window_identity_ref)
                != requested_target
            ):
                raise ComputerSessionError(
                    "session_state_changed",
                    "session_state_changed: same-window observation continuity was lost",
                )

            await self._bind_backend_directory()

            token = uuid.uuid4().hex
            artifact = ComputerArtifact(
                filename=f"snapshot-{token}.png",
                directory_fd=self._session_fd,
                directory_path=self._session_dir,
            )
            detail_artifact = (
                ComputerArtifact(
                    filename=f"snapshot-{token}.ax.json",
                    directory_fd=self._session_fd,
                    directory_path=self._session_dir,
                )
                if text_detail is ComputerSnapshotTextDetailMode.ON
                else None
            )
            try:
                inventory_before = self._snapshot_artifact_inventory()
                self._validate_existing_snapshot_inventory(inventory_before)
            except ComputerSessionError as inventory_error:
                self._poison_artifact_session_locked(clear_target=True)
                raise ComputerSessionError(
                    "unsafe_artifact",
                    "unsafe_artifact: initial app state artifact inventory cannot be verified",
                ) from inventory_error

            try:
                state = await self._backend.get_app_state(
                    requested_target,
                    scope,
                    artifact,
                    text_detail=text_detail,
                    text_detail_artifact=detail_artifact,
                )
                if (
                    state.target != requested_target
                    or state.catalog_generation != catalog.generation
                ):
                    raise ComputerSessionError(
                        "session_state_changed",
                        "session_state_changed: helper returned crossed target state",
                    )
                if self._close_task is not None and not self._close_task.done():
                    raise ComputerSessionError(
                        "session_state_changed",
                        "session_state_changed: Computer Use session began closing",
                    )
                snapshot = state.snapshot
                try:
                    validate_snapshot_payload(
                        snapshot.payload,
                        snapshot_id=snapshot.snapshot_id,
                    )
                except (TypeError, ValueError) as validation_error:
                    raise ComputerSessionError(
                        "unsafe_artifact",
                        f"unsafe_artifact: snapshot payload {validation_error}",
                    ) from validation_error
                if snapshot.payload.get("image_artifact") != artifact.filename:
                    raise ComputerSessionError(
                        "unsafe_artifact",
                        "unsafe_artifact: helper changed PNG artifact name",
                    )
                if ("display_id" in snapshot.payload) != (scope == "display"):
                    raise ComputerSessionError(
                        "unsafe_artifact",
                        "unsafe_artifact: snapshot display metadata does not match capture scope",
                    )
                if text_detail is ComputerSnapshotTextDetailMode.ON:
                    (
                        snapshot,
                        image_path,
                        image_data,
                        image_identity,
                        image_digest,
                        detail_path,
                        detail_data,
                        detail_digest,
                        authorities,
                    ) = self._verify_smart_snapshot_artifacts(
                        snapshot,
                        artifact,
                        detail_artifact,
                    )
                else:
                    if any(
                        key in snapshot.payload
                        for key in (
                            "text_detail_artifact",
                            "text_detail_metadata",
                            "text_detail_path",
                        )
                    ):
                        raise ComputerSessionError(
                            "unsafe_artifact",
                            "unsafe_artifact: plain snapshot returned Smart detail metadata",
                        )
                    (
                        image_path,
                        image_data,
                        image_identity,
                        image_digest,
                        image_authority,
                    ) = self._read_verified_png_artifact_details(artifact.filename)
                    self._validate_png_dimensions(image_data, snapshot.payload.get("pixel_size"))
                    authorities = {artifact.filename: image_authority}
                    detail_path = None
                    detail_data = None
                    detail_digest = None
                self._verify_snapshot_inventory_delta(
                    inventory_before,
                    expected=authorities,
                )
                current_catalog = self._last_catalog
                if (
                    current_catalog is None
                    or current_catalog is not catalog
                    or current_catalog.generation != catalog.generation
                    or self._identity_from_catalog(current_catalog, app_ref, window_ref) != identity
                ):
                    raise ComputerSessionError(
                        "session_state_changed",
                        "session_state_changed: application catalog changed before commit",
                    )
                next_generation = self._target_generation + 1
                verified = ComputerVerifiedAppState(
                    target=requested_target,
                    target_generation=next_generation,
                    snapshot=snapshot,
                    image_path=image_path,
                    image_data=image_data,
                    image_identity=image_identity,
                    image_sha256=image_digest,
                    detail_path=detail_path,
                    detail_data=detail_data,
                    detail_sha256=detail_digest,
                )
            except BaseException as exc:
                inventory_changed = False
                inventory_uncertain = False
                try:
                    inventory_changed = self._snapshot_artifact_inventory() != inventory_before
                except ComputerSessionError:
                    inventory_uncertain = True
                unsafe_artifact = (
                    isinstance(exc, ComputerSessionError)
                    and exc.code == "unsafe_artifact"
                )
                publication_untrusted = inventory_changed or inventory_uncertain
                if isinstance(exc, asyncio.CancelledError):
                    if publication_untrusted:
                        self._poison_artifact_session_locked(clear_target=True)
                    raise
                if unsafe_artifact:
                    self._poison_artifact_session_locked(clear_target=True)
                    raise
                if publication_untrusted:
                    self._poison_artifact_session_locked(clear_target=True)
                    raise ComputerSessionError(
                        "unsafe_artifact",
                        "unsafe_artifact: app state publication is changed or cannot be verified",
                    ) from exc
                raise

            self._current_snapshot_artifacts = dict(authorities)
            self._target = requested_target
            self._target_identity = identity
            self._target_window_identity_ref = self._window_identity_ref_from_catalog(
                self._last_catalog,
                app_ref,
                window_ref,
            )
            self._latest_snapshot = snapshot
            self._latest_snapshot_scope = scope
            self._target_generation = next_generation
            self._handed_off = False
            self._clear_suspended_continuity()
            return verified

    def poison_artifact_session(self, *, clear_target: bool = True) -> None:
        """Fail closed from the registry's synchronous artifact postcondition."""
        self._poison_artifact_session_locked(clear_target=clear_target)

    def _poison_artifact_session_locked(self, *, clear_target: bool) -> None:
        # Do not clean current artifacts here: their authority is uncertain.
        self._clear_cooperative_references()
        self.grants.clear()
        if clear_target:
            self._target = None
            self._target_identity = None
            self._target_window_identity_ref = None
            self._clear_suspended_continuity()
        self._artifact_poisoned = True

    async def snapshot(
        self,
        scope: str = "target_window",
        *,
        text_detail: ComputerSnapshotTextDetailMode = ComputerSnapshotTextDetailMode.OFF,
        expected_target_binding: ComputerSnapshotTargetBinding | None = None,
        resume_publication_id: str | None = None,
        subtree_ref: str = "",
    ) -> ComputerSnapshot:
        async with self._lock:
            self._require_open()
            if not isinstance(text_detail, ComputerSnapshotTextDetailMode):
                raise TypeError("text_detail must be a ComputerSnapshotTextDetailMode")
            if expected_target_binding is not None and not isinstance(
                expected_target_binding,
                ComputerSnapshotTargetBinding,
            ):
                raise TypeError("expected_target_binding must be a ComputerSnapshotTargetBinding")
            await self._bind_backend_directory()
            target = self._require_target()
            self._validate_snapshot_target_binding(expected_target_binding, target)
            pending_publication = self._resume_publication_id
            if pending_publication is None:
                if resume_publication_id is not None:
                    raise ComputerSessionError("stale_resume_publication")
            elif resume_publication_id != pending_publication:
                raise ComputerSessionError("stale_resume_publication")
            if self._handed_off and pending_publication is None:
                raise ComputerSessionError("handoff_active", "handoff_active: user controls the target")
            subtree = None
            if subtree_ref:
                previous = self._latest_snapshot
                tree = previous.payload.get("ax_tree", {}) if previous else {}
                if not isinstance(tree, Mapping) or "subtree_v1" not in tree.get("observation_capabilities", []):
                    raise ComputerSessionError("unsupported_operation", "Native subtree capture requires an updated helper; do not repeat this request")
                assert previous is not None
                subtree = (previous.snapshot_id, subtree_ref)
            token = uuid.uuid4().hex
            artifact = ComputerArtifact(
                filename=f"snapshot-{token}.png",
                directory_fd=self._session_fd,
                directory_path=self._session_dir,
            )
            detail_artifact = (
                ComputerArtifact(
                    filename=f"snapshot-{token}.ax.json",
                    directory_fd=self._session_fd,
                    directory_path=self._session_dir,
                )
                if text_detail is ComputerSnapshotTextDetailMode.ON
                else None
            )
            return await self._snapshot_with_target_recovery(
                scope,
                artifact,
                text_detail=text_detail,
                text_detail_artifact=detail_artifact,
                expected_target_binding=expected_target_binding,
                subtree=subtree,
            )

    async def _snapshot_once(
        self,
        scope: str,
        artifact: ComputerArtifact,
        target: ComputerTarget,
        *,
        text_detail: ComputerSnapshotTextDetailMode,
        text_detail_artifact: ComputerArtifact | None,
        subtree: tuple[str, str] | None = None,
    ) -> ComputerSnapshot:
        # A new capture attempt may observe a changed target even when it
        # fails; never leave an earlier element reference replayable.
        self._invalidate_cooperative_state(
            preserve_takeover_cleanup=True,
            preserve_active_fragment=self._fragment_declaration is not None,
        )
        post_fragment_input = self._fragment_stage_input_completed
        inventory_before = self._snapshot_artifact_inventory() if text_detail_artifact is not None else None
        if inventory_before is not None:
            try:
                self._validate_existing_snapshot_inventory(inventory_before)
            except ComputerSessionError:
                self._artifact_poisoned = True
                raise
        try:
            subtree_args = {"subtree": subtree} if subtree is not None else {}
            if text_detail is ComputerSnapshotTextDetailMode.ON:
                snapshot = await self._backend.snapshot(
                    target,
                    scope,
                    artifact,
                    text_detail=text_detail,
                    text_detail_artifact=text_detail_artifact,
                    **subtree_args,
                )
                snapshot, _, _, _, _, _, _, _, authorities = self._verify_smart_snapshot_artifacts(
                    snapshot,
                    artifact,
                    text_detail_artifact,
                )
                assert inventory_before is not None
                self._verify_snapshot_inventory_delta(
                    inventory_before,
                    expected=authorities,
                )
                self._current_snapshot_artifacts = dict(authorities)
            else:
                if subtree is not None:
                    snapshot = await self._backend.snapshot(target, scope, artifact, subtree=subtree)
                else:
                    snapshot = await self._backend.snapshot(target, scope, artifact)
        except BaseException as exc:
            inventory_changed = False
            if inventory_before is not None:
                try:
                    inventory_changed = self._snapshot_artifact_inventory() != inventory_before
                except ComputerSessionError:
                    inventory_changed = True
                if inventory_changed:
                    self._artifact_poisoned = True
            if post_fragment_input:
                cleanup_task = asyncio.create_task(
                    self._terminate_fragment_after_observation_failure()
                )
                self._terminal_cleanup_task = cleanup_task
                try:
                    _, cancelled = await self._await_owned_task(cleanup_task)
                finally:
                    self._terminal_cleanup_task = None
                if cancelled or isinstance(exc, asyncio.CancelledError):
                    raise asyncio.CancelledError from exc
                raise ComputerSessionError(
                    "unknown_outcome",
                    "unknown_outcome: fragment input succeeded but fresh observation failed",
                ) from exc
            if (
                inventory_changed
                and not isinstance(exc, (asyncio.CancelledError, ComputerSessionError))
            ):
                raise ComputerSessionError(
                    "unsafe_artifact",
                    "unsafe_artifact: smart snapshot inventory changed without a verified response",
                ) from exc
            raise
        returned_artifact = snapshot.payload.get("image_artifact")
        if returned_artifact is not None and returned_artifact != artifact.filename:
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: helper returned an artifact outside the current capture",
            )
        self._latest_snapshot = snapshot
        self._latest_snapshot_scope = scope
        return snapshot

    async def _snapshot_with_target_recovery(
        self,
        scope: str,
        artifact: ComputerArtifact,
        *,
        text_detail: ComputerSnapshotTextDetailMode,
        text_detail_artifact: ComputerArtifact | None,
        expected_target_binding: ComputerSnapshotTargetBinding | None,
        subtree: tuple[str, str] | None = None,
    ) -> ComputerSnapshot:
        """Snapshot once; if the helper process restarted (target_gone), recover
        the selected window identity from a fresh catalog and retry exactly once.

        The helper keeps its opaque target registry in-process, and binding the
        session artifact directory can restart it before a read operation. This
        recovery is read-only and never replays approval or input.
        """
        target = self._require_target()
        self._validate_snapshot_target_binding(expected_target_binding, target)
        try:
            snapshot = await self._snapshot_once(
                scope,
                artifact,
                target,
                text_detail=text_detail,
                text_detail_artifact=text_detail_artifact,
                subtree=subtree,
            )
            self._validate_snapshot_target_binding(expected_target_binding, self._require_target())
            return snapshot
        except Exception as exc:
            # Delayed import avoids the macOS module's reverse dependency.
            from .macos_computer import HelperApplicationError
            if not isinstance(exc, HelperApplicationError) or exc.error.code is not ComputerErrorCode.TARGET_GONE:
                raise
            if expected_target_binding is not None or subtree is not None:
                # The native helper's opaque refs may be recreated by recovery.
                # An approved smart request is bound to the original exact refs,
                # so it must fail and require a new request-level approval.
                raise
            recovered = await self._recover_target_locked()
            if recovered is None:
                raise
            return await self._snapshot_once(
                scope,
                artifact,
                recovered,
                text_detail=text_detail,
                text_detail_artifact=text_detail_artifact,
            )

    async def _recover_target_locked(self) -> ComputerTarget | None:
        identity = self._target_identity
        if identity is None:
            return None
        bundle_id, title, document_path = identity
        self._invalidate_cooperative_state()
        self._resume_candidate = None
        self._resume_catalog_refreshed = False
        self._target_generation += 1
        self._target = None
        catalog = await self._backend.apps()
        apps = list(catalog.apps)
        self._last_catalog = catalog
        candidate = self._match_catalog_identity(apps, bundle_id, title, document_path)
        if candidate is None:
            return None
        target = await self._backend.select(candidate[0], candidate[1])
        self._target = target
        self._target_window_identity_ref = self._window_identity_ref_from_catalog(
            catalog,
            candidate[0],
            candidate[1],
        )
        self._handed_off = False
        self._clear_suspended_continuity()
        return target

    def _validate_snapshot_target_binding(
        self,
        expected: ComputerSnapshotTargetBinding | None,
        target: ComputerTarget,
    ) -> None:
        if expected is None:
            return
        current = self.snapshot_target_binding
        if current != expected or target != ComputerTarget(expected.app_ref, expected.window_ref):
            raise ComputerSessionError(
                "snapshot_target_changed",
                "snapshot_target_changed: approved Computer Use target changed",
            )

    @staticmethod
    def _identity_from_catalog(
        catalog: ComputerAppCatalog | None,
        app_ref: str,
        window_ref: str,
    ) -> tuple[str, str, str | None] | None:
        if not isinstance(catalog, ComputerAppCatalog):
            return None
        if (
            not isinstance(app_ref, str)
            or not app_ref
            or not isinstance(window_ref, str)
            or not window_ref
        ):
            return None
        match: tuple[str, str, str | None] | None = None
        for application in catalog.apps:
            if not isinstance(application, Mapping) or application.get("app_ref") != app_ref:
                continue
            bundle_id = application.get("bundle_id")
            windows = application.get("windows")
            if not isinstance(bundle_id, str) or not isinstance(windows, list):
                return None
            for window in windows:
                if not isinstance(window, Mapping) or window.get("window_ref") != window_ref:
                    continue
                title = window.get("title")
                document_path = window.get("document_path")
                candidate = (
                    bundle_id,
                    title if isinstance(title, str) else "",
                    document_path if isinstance(document_path, str) else None,
                )
                if match is not None:
                    return None
                match = candidate
        return match

    @staticmethod
    def _match_catalog_identity(
        apps: list[Mapping[str, object]],
        bundle_id: str,
        title: str,
        document_path: str | None,
    ) -> tuple[str, str] | None:
        path_matches: list[tuple[str, str]] = []
        title_matches: list[tuple[str, str]] = []
        for application in apps:
            if not isinstance(application, Mapping) or application.get("bundle_id") != bundle_id:
                continue
            app_ref = application.get("app_ref")
            windows = application.get("windows")
            if not isinstance(app_ref, str) or not isinstance(windows, list):
                continue
            for window in windows:
                if not isinstance(window, Mapping):
                    continue
                window_ref = window.get("window_ref")
                if not isinstance(window_ref, str):
                    continue
                if document_path is not None and window.get("document_path") == document_path:
                    path_matches.append((app_ref, window_ref))
                elif document_path is None and title and window.get("title") == title:
                    title_matches.append((app_ref, window_ref))
        if len(path_matches) == 1:
            return path_matches[0]
        if not path_matches and len(title_matches) == 1:
            return title_matches[0]
        return None

    async def plan_actions(
        self,
        snapshot_id: str,
        actions: list[ComputerAction | Mapping[str, object]],
        *,
        interaction_mode: ComputerInteractionMode,
        fragment: ForegroundFragmentPlanRequest | None = None,
    ) -> ComputerActionPlan:
        async with self._lock:
            self._require_open()
            target = self._require_target()
            if self._handed_off:
                raise ComputerSessionError("handoff_active", "handoff_active: user controls the target")
            if not isinstance(interaction_mode, ComputerInteractionMode):
                raise ComputerSessionError("invalid_interaction_mode")
            snapshot = self._latest_snapshot
            if snapshot is None or snapshot.snapshot_id != snapshot_id:
                current = snapshot.snapshot_id if snapshot is not None else "none"
                raise ComputerSessionError(
                    "stale_snapshot",
                    f"stale_snapshot: capture a fresh snapshot; current_snapshot_id={current}",
                )
            if self._latest_snapshot_scope != "target_window":
                raise ComputerSessionError(
                    "stale_snapshot",
                    "stale_snapshot: display observations cannot authorize input",
                )
            normalized = [self._coerce_action(action) for action in actions]
            if fragment is not None:
                if interaction_mode is not ComputerInteractionMode.FOREGROUND_TAKEOVER:
                    raise ComputerSessionError("invalid_fragment")
                if fragment.authority.input_snapshot_id != snapshot_id:
                    raise ComputerSessionError(
                        "stale_snapshot",
                        "stale_snapshot: fragment snapshot mismatch; "
                        f"fragment_snapshot_id={fragment.authority.input_snapshot_id} "
                        f"current_snapshot_id={snapshot.snapshot_id}",
                    )
                if fragment.kind == "continuing":
                    if (
                        self._fragment_declaration is None
                        or fragment.takeover_ref != self._takeover_ref
                    ):
                        raise ComputerSessionError("stale_takeover")
                elif self._takeover_ref is not None:
                    raise ComputerSessionError("takeover_active")
            self._validate_action_coordinates(snapshot, normalized)
            self._pending_plan = None
            if fragment is None:
                self._takeover_ref = None
            # Planning may invalidate native references even when its response
            # is lost or malformed. Preserve only the explicitly validated
            # background-to-foreground transition below.
            preserved_scope = self._latest_snapshot_scope
            self._latest_snapshot = None
            self._latest_snapshot_scope = ""
            try:
                if fragment is None:
                    raw_plan = await self._backend.plan_actions(
                        target,
                        snapshot_id,
                        normalized,
                        interaction_mode,
                    )
                else:
                    raw_plan = await self._backend.plan_actions(
                        target,
                        snapshot_id,
                        normalized,
                        interaction_mode,
                        fragment=fragment,
                    )
            except Exception as exc:
                # helper 的**结构化拒绝**没有存储 plan、也没有发出任何输入。若连这种
                # 情况也作废观察授权，工具自己的恢复指引（"retry this exact snapshot
                # with foreground_takeover"）就变成一条必然失败的指令：重试会在
                # 快照身份校验处撞上 stale_snapshot。响应丢失/畸形仍按原注释作废。
                from .macos_computer import HelperApplicationError

                if isinstance(exc, HelperApplicationError):
                    self._latest_snapshot = snapshot
                    self._latest_snapshot_scope = preserved_scope
                raise
            plan = self._coerce_plan(raw_plan)
            if plan.interaction_mode is not interaction_mode:
                raise ComputerSessionError("stale_plan", "stale_plan: helper returned the wrong mode")
            pending = _PendingActionPlan(plan, target, snapshot_id, tuple(normalized), fragment)
            self._pending_plan = pending
            if (
                interaction_mode is ComputerInteractionMode.BACKGROUND
                and plan.requires_takeover
            ):
                self._latest_snapshot = snapshot
                self._latest_snapshot_scope = "target_window"
            return plan

    async def begin_takeover(
        self,
        snapshot_id: str,
        plan_ref: str,
        *,
        declaration: ForegroundFragmentDeclaration | None = None,
    ) -> str:
        async with self._lock:
            self._require_open()
            if self._handed_off:
                raise ComputerSessionError("handoff_active", "handoff_active: user controls the target")
            pending = self._require_pending_plan(snapshot_id, plan_ref)
            if pending.plan.interaction_mode is not ComputerInteractionMode.FOREGROUND_TAKEOVER:
                raise ComputerSessionError("takeover_not_allowed")
            if self._takeover_ref is not None:
                raise ComputerSessionError("takeover_active")
            if self._takeover_cleanup_ref is not None:
                raise ComputerSessionError("takeover_cleanup_required")
            if declaration is not None and (
                pending.fragment is None
                or pending.fragment.kind != "initial"
                or pending.fragment.authority.fragment_hash != declaration.fragment_hash
                or pending.fragment.authority.stage_hash != declaration.stages[0].stage_hash
            ):
                raise ComputerSessionError("stale_plan")
            begin = getattr(self._backend, "begin_takeover", None)
            if begin is None:
                raise ComputerSessionError("takeover_unsupported")
            self._pending_plan = None
            self._takeover_ref = None
            self._takeover_cleanup_ref = None
            if declaration is None:
                begin_task = asyncio.create_task(begin(snapshot_id, plan_ref))
            else:
                begin_task = asyncio.create_task(
                    begin(snapshot_id, plan_ref, declaration=declaration)
                )
            try:
                takeover_ref = await asyncio.shield(begin_task)
            except asyncio.CancelledError:
                self._uncancel_current_task()
                cleanup_task = asyncio.create_task(self._cleanup_cancelled_takeover_begin(begin_task))
                self._terminal_cleanup_task = cleanup_task
                try:
                    await self._await_owned_task(cleanup_task)
                finally:
                    self._terminal_cleanup_task = None
                raise
            except BaseException as exc:
                self._takeover_ref = None
                self._takeover_cleanup_ref = None
                if self._is_definite_application_rejection(exc):
                    raise
                cleanup_task = asyncio.create_task(self._force_close_after_uncertain_takeover_begin())
                self._terminal_cleanup_task = cleanup_task
                try:
                    _, cancelled = await self._await_owned_task(cleanup_task)
                finally:
                    self._terminal_cleanup_task = None
                if cancelled:
                    raise asyncio.CancelledError
                raise
            if not isinstance(takeover_ref, str) or not takeover_ref:
                cleanup_task = asyncio.create_task(self._force_close_after_uncertain_takeover_begin())
                self._terminal_cleanup_task = cleanup_task
                try:
                    _, cancelled = await self._await_owned_task(cleanup_task)
                finally:
                    self._terminal_cleanup_task = None
                if cancelled:
                    raise asyncio.CancelledError
                raise ComputerSessionError("stale_plan", "stale_plan: invalid takeover reference")
            self._pending_plan = pending
            self._takeover_ref = takeover_ref
            self._takeover_cleanup_ref = takeover_ref
            self._fragment_declaration = declaration
            self._fragment_stage = pending.fragment.authority if declaration is not None and pending.fragment else None
            self._fragment_stage_input_completed = False
            return takeover_ref

    async def _cleanup_cancelled_takeover_begin(self, begin_task: asyncio.Task[object]) -> None:
        try:
            takeover_ref = await asyncio.wait_for(
                asyncio.shield(begin_task),
                timeout=self._takeover_begin_cleanup_timeout,
            )
        except BaseException:  # noqa: BLE001 - any uncertain begin outcome forces session teardown
            begin_task.cancel()
            with suppress(BaseException):
                await begin_task
            await self._force_close_after_uncertain_takeover_begin()
            return
        if not isinstance(takeover_ref, str) or not takeover_ref:
            await self._force_close_after_uncertain_takeover_begin()
            return
        end = getattr(self._backend, "end_takeover", None)
        if end is None:
            await self._force_close_after_uncertain_takeover_begin()
            return
        try:
            result = await self._run_takeover_end_task(end, takeover_ref)
        except BaseException:  # noqa: BLE001 - failed terminal cleanup forces session teardown
            await self._force_close_after_uncertain_takeover_begin()
            return
        if not isinstance(result, Mapping):
            await self._force_close_after_uncertain_takeover_begin()

    async def _force_close_after_uncertain_takeover_begin(self) -> None:
        try:
            await self._close_backend_once()
            self.close_local_state()
        except BaseException:  # noqa: BLE001 - uncertain helper liveness must preserve artifacts
            self._artifact_poisoned = True
        finally:
            self._target = None
            self._target_window_identity_ref = None
            self._latest_snapshot = None
            self._latest_snapshot_scope = ""
            self._pending_plan = None
            self._takeover_ref = None
            self._takeover_cleanup_ref = None
            self._fragment_declaration = None
            self._fragment_stage = None
            self._fragment_stage_input_completed = False
            self._handed_off = False
            self._clear_suspended_continuity()
            self.grants.clear()

    async def _run_takeover_end_task(self, end: object, takeover_ref: str) -> object:
        assert callable(end)
        coroutine = end(takeover_ref)
        if not inspect.iscoroutine(coroutine):
            raise TypeError("takeover cleanup must return a coroutine")
        task = asyncio.create_task(coroutine)
        self._takeover_end_task = task
        try:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self._takeover_begin_cleanup_timeout,
                )
            except BaseException:
                task.cancel()
                # Closing the transport is what gives a blocked native request
                # a bounded way to leave after cancellation.  Retrieve the
                # request only after shutdown has made further I/O impossible.
                with suppress(BaseException):
                    await self._close_backend_once()
                with suppress(BaseException):
                    await task
                raise
        finally:
            self._takeover_end_task = None

    async def _await_owned_task(self, task: asyncio.Task[object]) -> tuple[object, bool]:
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
                self._uncancel_current_task()
            except BaseException as exc:
                if cancelled:
                    raise asyncio.CancelledError from exc
                raise

    @staticmethod
    def _uncancel_current_task() -> None:
        current = asyncio.current_task()
        if current is not None and hasattr(current, "uncancel"):
            current.uncancel()

    @staticmethod
    def _is_definite_application_rejection(exc: BaseException) -> bool:
        return isinstance(getattr(exc, "error", None), ComputerError)

    async def _close_backend_once(self) -> None:
        task = self._backend_close_task
        if task is not None and task.done() and (
            task.cancelled() or task.exception() is not None
        ):
            self._backend_close_task = None
            task = None
        if task is None:
            task = asyncio.create_task(self._backend.close())
            self._backend_close_task = task
        await asyncio.shield(task)

    async def act(
        self,
        snapshot_id: str,
        actions: list[ComputerAction | Mapping[str, object]],
        *,
        interaction_mode: ComputerInteractionMode,
        plan_ref: str,
        takeover_ref: str | None = None,
        fragment_stage: FragmentStageAuthority | None = None,
    ) -> ComputerActionResult:
        async with self._lock:
            self._require_open()
            self._require_target()
            if self._handed_off:
                raise ComputerSessionError("handoff_active", "handoff_active: user controls the target")
            normalized = [self._coerce_action(action) for action in actions]
            pending = self._require_pending_plan(snapshot_id, plan_ref)
            if pending.plan.interaction_mode is not interaction_mode:
                raise ComputerSessionError("stale_plan", "stale_plan: interaction mode changed")
            if pending.actions != tuple(normalized):
                raise ComputerSessionError("stale_plan", "stale_plan: action batch changed")
            if fragment_stage is not None and (
                pending.fragment is None
                or pending.fragment.authority != fragment_stage
                or self._fragment_declaration is None
            ):
                raise ComputerSessionError("stale_fragment")
            if interaction_mode is ComputerInteractionMode.BACKGROUND:
                if takeover_ref is not None or pending.plan.requires_takeover:
                    raise ComputerSessionError("takeover_required")
            elif (
                interaction_mode is not ComputerInteractionMode.FOREGROUND_TAKEOVER
                or takeover_ref is None
                or takeover_ref != self._takeover_ref
            ):
                raise ComputerSessionError("stale_takeover")
            # Consume before dispatch: cancellation, crash, and malformed
            # replies can never leave either authority replayable.
            self._pending_plan = None
            if fragment_stage is None:
                self._takeover_ref = None
            self._latest_snapshot = None
            self._latest_snapshot_scope = ""
            if fragment_stage is None:
                result = await self._backend.act(
                    pending.target,
                    snapshot_id,
                    normalized,
                    interaction_mode=interaction_mode,
                    plan_ref=plan_ref,
                    takeover_ref=takeover_ref,
                )
            else:
                act_task = asyncio.create_task(self._backend.act(
                    pending.target,
                    snapshot_id,
                    normalized,
                    interaction_mode=interaction_mode,
                    plan_ref=plan_ref,
                    takeover_ref=takeover_ref,
                    fragment_stage=fragment_stage,
                ))
                try:
                    result = await asyncio.shield(act_task)
                except asyncio.CancelledError:
                    self._uncancel_current_task()
                    act_task.cancel()
                    with suppress(BaseException):
                        await act_task
                    await self._run_uncertain_fragment_terminal_cleanup()
                    raise asyncio.CancelledError
                except BaseException:
                    await self._run_uncertain_fragment_terminal_cleanup()
                    raise
            if (
                result.error is not None
                and result.error.code is ComputerErrorCode.USER_ACTIVITY_PAUSED
            ):
                self._pause_for_user(pending.target, preserve_takeover_cleanup=True)
            elif fragment_stage is not None:
                self._fragment_stage = fragment_stage if result.error is None else None
                self._fragment_stage_input_completed = result.error is None
                if result.error is not None:
                    self._takeover_ref = None
                    self._fragment_declaration = None
                    if result.error.code in {ComputerErrorCode.UNKNOWN_OUTCOME, ComputerErrorCode.OBSERVATION_REQUIRED}:
                        await self._run_uncertain_fragment_terminal_cleanup()
            return result

    async def _run_uncertain_fragment_terminal_cleanup(self) -> None:
        cleanup_task = asyncio.create_task(self._terminate_fragment_after_uncertain_act())
        self._terminal_cleanup_task = cleanup_task
        try:
            _, cancelled = await self._await_owned_task(cleanup_task)
        finally:
            self._terminal_cleanup_task = None
        if cancelled:
            raise asyncio.CancelledError

    async def _terminate_fragment_after_uncertain_act(self) -> None:
        takeover_ref = self._takeover_cleanup_ref
        self._takeover_ref = None
        self._pending_plan = None
        self._fragment_declaration = None
        self._fragment_stage = None
        self._fragment_stage_input_completed = False
        if takeover_ref is None:
            return
        end = getattr(self._backend, "end_takeover", None)
        if end is None:
            await self._force_close_after_uncertain_takeover_begin()
            return
        try:
            outcome = await self._run_takeover_end_task(end, takeover_ref)
        except BaseException:  # noqa: BLE001 - uncertain input plus failed cleanup requires force-close
            await self._force_close_after_uncertain_takeover_begin()
            return
        if not isinstance(outcome, Mapping):
            await self._force_close_after_uncertain_takeover_begin()
            return
        self._takeover_cleanup_ref = None

    async def commit_fragment_stage(self, commit: FragmentStageCommit) -> FragmentStageCommitResult:
        async with self._lock:
            self._require_open()
            if (
                self._fragment_declaration is None
                or self._fragment_stage is None
                or commit.takeover_ref != self._takeover_ref
                or commit.takeover_ref != self._takeover_cleanup_ref
                or commit.fragment_hash != self._fragment_stage.fragment_hash
                or commit.stage_index != self._fragment_stage.stage_index
                or commit.stage_hash != self._fragment_stage.stage_hash
                or self._latest_snapshot is None
                or commit.fresh_snapshot_id != self._latest_snapshot.snapshot_id
            ):
                self._invalidate_cooperative_state(preserve_takeover_cleanup=True)
                raise ComputerSessionError("stale_fragment")
            commit_stage = getattr(self._backend, "commit_fragment_stage", None)
            if commit_stage is None:
                raise ComputerSessionError("takeover_unsupported")
            # Consume the stage and observation authority before dispatch.  The
            # helper may commit even if this task is cancelled or its reply is
            # lost, so neither authority can remain locally replayable.
            fresh_snapshot = self._latest_snapshot
            fresh_snapshot_scope = self._latest_snapshot_scope
            self._fragment_stage = None
            self._fragment_stage_input_completed = False
            self._latest_snapshot = None
            self._latest_snapshot_scope = ""
            commit_task = asyncio.create_task(commit_stage(commit))
            try:
                result, cancelled = await self._await_owned_task(commit_task)
            except BaseException:
                await self._run_uncertain_fragment_terminal_cleanup()
                raise
            if not isinstance(result, FragmentStageCommitResult):
                await self._run_uncertain_fragment_terminal_cleanup()
                raise ComputerSessionError("stale_fragment")
            if result.terminal:
                self._takeover_ref = None
                self._takeover_cleanup_ref = None
                self._fragment_declaration = None
            elif cancelled:
                await self._run_uncertain_fragment_terminal_cleanup()
            else:
                # A definite nonterminal receipt advances the fragment and
                # grants a new, bounded authority to plan the next stage from
                # the committed fresh observation.
                self._latest_snapshot = fresh_snapshot
                self._latest_snapshot_scope = fresh_snapshot_scope
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def _terminate_fragment_after_observation_failure(self) -> None:
        takeover_ref = self._takeover_cleanup_ref
        self._takeover_ref = None
        self._pending_plan = None
        self._fragment_declaration = None
        self._fragment_stage = None
        self._fragment_stage_input_completed = False
        if takeover_ref is None:
            return
        end = getattr(self._backend, "end_takeover", None)
        if end is None:
            return
        try:
            outcome = await self._run_takeover_end_task(end, takeover_ref)
        except BaseException:  # noqa: BLE001 - retain cleanup authority for any failed terminal attempt
            return
        if isinstance(outcome, Mapping):
            self._takeover_cleanup_ref = None

    async def end_takeover(
        self, takeover_ref: str, *, restore_previous_focus: bool = True,
    ) -> Mapping[str, object]:
        if not isinstance(restore_previous_focus, bool):
            raise TypeError("restore_previous_focus must be boolean")
        cancelled = await self._acquire_lock_for_terminal_cleanup()
        try:
            self._require_open()
            if takeover_ref != self._takeover_cleanup_ref:
                raise ComputerSessionError("stale_takeover")
            self._takeover_ref = None
            self._takeover_cleanup_ref = None
            self._pending_plan = None
            end = getattr(self._backend, "end_takeover", None)
            if end is None:
                raise ComputerSessionError("takeover_unsupported")
            if not restore_previous_focus:
                end = partial(end, restore_previous_focus=False)
            cleanup_task = asyncio.create_task(self._end_takeover_owned(end, takeover_ref))
            self._terminal_cleanup_task = cleanup_task
            try:
                result, cancelled_during_cleanup = await self._await_owned_task(cleanup_task)
                cancelled = cancelled or cancelled_during_cleanup
            finally:
                self._terminal_cleanup_task = None
            if not isinstance(result, Mapping):
                raise ComputerSessionError("stale_takeover")
        finally:
            self._lock.release()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _end_takeover_owned(self, end: object, takeover_ref: str) -> object:
        try:
            result = await self._run_takeover_end_task(end, takeover_ref)
        except BaseException as exc:
            await self._force_close_after_uncertain_takeover_begin()
            raise ComputerSessionError("takeover_cleanup_failed") from exc
        if not isinstance(result, Mapping):
            await self._force_close_after_uncertain_takeover_begin()
            raise ComputerSessionError("takeover_cleanup_failed")
        return result

    async def _acquire_lock_for_terminal_cleanup(self) -> bool:
        acquire_task = asyncio.create_task(self._lock.acquire())
        cancelled = False
        while True:
            try:
                await asyncio.shield(acquire_task)
                return cancelled
            except asyncio.CancelledError:
                cancelled = True
                self._uncancel_current_task()

    async def handoff(self) -> None:
        async with self._lock:
            self._require_open()
            self._pause_for_user(self._require_target())

    async def pause_for_user_activity(self) -> None:
        """Enter user-control state while retaining only active takeover cleanup."""
        async with self._lock:
            self._require_open()
            self._pause_for_user(
                self._require_target(),
                preserve_takeover_cleanup=True,
            )

    async def resume(self, publication_id: str) -> ComputerTarget | None:
        async with self._lock:
            self._require_open()
            if not self._valid_window_identity_ref(publication_id):
                raise ComputerSessionError("stale_resume_publication")
            if self._resume_publication_id is not None:
                raise ComputerSessionError("stale_resume_publication")
            if not self._handed_off or self._suspended_target is None:
                raise ComputerSessionError("handoff_inactive")
            if not self._resume_catalog_refreshed:
                raise ComputerSessionError(
                    "fresh_apps_required",
                    "fresh_apps_required: enumerate applications after user handoff",
                )
            candidate = self._resume_candidate
            if candidate is None:
                identities = self._catalog_window_identities(self._last_catalog)
                if (
                    identities is None
                    or self._last_catalog is None
                    or not self._valid_window_identity_ref(self._suspended_window_identity_ref)
                    or self._suspended_window_identity_ref not in self._last_catalog.confirmed_absent_window_identity_refs
                    or self._suspended_window_identity_ref in identities
                ):
                    raise ComputerSessionError("target_gone", "target_gone: suspended target absence is unproven")
                # This fresh proof is produced only for opaque identities
                # exposed in this helper lifetime, against full WindowServer
                # inventory. Unrelated UI bindability is not continuity proof.
                self._resume_catalog_refreshed = False
                self._invalidate_cooperative_state()
                self.grants.clear()
                self._target = None
                self._target_identity = None
                self._target_window_identity_ref = None
                self._target_generation += 1
                self._resume_publication_id = publication_id
                # Keep handoff and continuity until the unbound receipt passes
                # publication. Abort/cancellation must retain user control.
                return None
            self._resume_candidate = None
            self._resume_catalog_refreshed = False
            self._invalidate_cooperative_state()
            self._target_generation += 1
            refreshed = await self._backend.select(candidate.app_ref, candidate.window_ref)
            if refreshed != candidate:
                raise ComputerSessionError("target_gone", "target_gone: suspended target identity changed")
            self._target = refreshed
            self._target_identity = self._identity_from_catalog(
                self._last_catalog,
                candidate.app_ref,
                candidate.window_ref,
            )
            self._target_window_identity_ref = self._suspended_window_identity_ref
            self._resume_publication_id = publication_id
            return refreshed

    def validate_unbound_resume_publication(self, publication_id: str) -> None:
        self._require_open()
        if (
            self._resume_publication_id != publication_id
            or not self._handed_off or self._target is not None
            or self._suspended_target is None
        ):
            raise ComputerSessionError("stale_resume_publication")

    async def commit_resume_publication(self, publication_id: str) -> None:
        async with self._lock:
            self._require_open()
            if self._resume_publication_id != publication_id or not self._handed_off:
                raise ComputerSessionError("stale_resume_publication")
            self._resume_publication_id = None
            self._handed_off = False
            self._clear_suspended_continuity()

    async def abort_resume_publication(self, publication_id: str) -> None:
        async with self._lock:
            if self._resume_publication_id != publication_id:
                return
            try:
                self._invalidate_cooperative_state()
            except BaseException:
                self._resume_publication_id = None
                self._poison_artifact_session_locked(clear_target=True)
                raise
            self._resume_publication_id = None
            self._target = None
            self._target_identity = None
            self._target_window_identity_ref = None
            self._target_generation += 1
            self._handed_off = True
            self.grants.clear()

    async def close(self) -> None:
        if self._closed:
            return
        task = self._close_task
        if task is None or task.done():
            if task is not None and not task.cancelled() and task.exception() is not None:
                self._close_task = None
            task = self._close_task
            if task is None:
                task = asyncio.create_task(self._close_owned())
                self._close_task = task
        try:
            await asyncio.shield(task)
        except BaseException:
            if task.done() and not task.cancelled() and task.exception() is not None:
                self._close_task = None
            raise

    async def _close_owned(self) -> None:
        async with self._lock:
            try:
                await self._close_backend_once()
            except BaseException:
                self._artifact_poisoned = True
                raise
            self.close_local_state()

    def close_local_state(self) -> None:
        """Erase local state only after helper termination has been proven."""
        if self._closed:
            return
        if (
            self._backend_close_task is None
            or not self._backend_close_task.done()
            or self._backend_close_task.cancelled()
            or self._backend_close_task.exception() is not None
        ):
            raise ComputerSessionError(
                "unsafe_cache",
                "unsafe_cache: helper termination is not proven; local cleanup is blocked",
            )
        cleanup_error: BaseException | None = None
        try:
            _flock(self._cleanup_lock_fd, _LOCK_EX)
            try:
                self._verify_cleanup_lock_identity()
                self._remove_session_dir()
            finally:
                _flock(self._cleanup_lock_fd, _LOCK_UN)
        except BaseException as exc:  # noqa: BLE001 - fail closed and let lifecycle report it
            cleanup_error = exc
        self._target = None
        self._target_window_identity_ref = None
        self._target_generation += 1
        self._clear_cooperative_references()
        self._handed_off = False
        self._clear_suspended_continuity()
        self.grants.clear()
        if cleanup_error is None:
            self._close_cache_fd()
            self._closed = True
            return
        self._artifact_poisoned = True
        raise cleanup_error

    async def close_backend(self) -> None:
        """Stop and await the helper before any local artifact cleanup."""
        await self._close_backend_once()

    def _invalidate_cooperative_state(
        self,
        *,
        preserve_takeover_cleanup: bool = False,
        preserve_active_fragment: bool = False,
    ) -> None:
        self._cleanup_current_snapshot_artifacts()
        self._clear_cooperative_references(
            preserve_takeover_cleanup=preserve_takeover_cleanup,
            preserve_active_fragment=preserve_active_fragment,
        )

    def _clear_cooperative_references(
        self,
        *,
        preserve_takeover_cleanup: bool = False,
        preserve_active_fragment: bool = False,
    ) -> None:
        cleanup_ref = self._takeover_cleanup_ref if preserve_takeover_cleanup else None
        active_ref = self._takeover_ref if preserve_active_fragment else None
        declaration = self._fragment_declaration if preserve_active_fragment else None
        fragment_stage = self._fragment_stage if preserve_active_fragment else None
        fragment_input_completed = self._fragment_stage_input_completed if preserve_active_fragment else False
        self._latest_snapshot = None
        self._latest_snapshot_scope = ""
        self._pending_plan = None
        self._takeover_ref = active_ref
        self._takeover_cleanup_ref = cleanup_ref
        self._fragment_declaration = declaration
        self._fragment_stage = fragment_stage
        self._fragment_stage_input_completed = fragment_input_completed
        self._current_snapshot_artifacts = {}

    def _pause_for_user(
        self,
        target: ComputerTarget,
        *,
        preserve_takeover_cleanup: bool = False,
    ) -> None:
        self._target_generation += 1
        self._invalidate_cooperative_state(
            preserve_takeover_cleanup=preserve_takeover_cleanup,
        )
        self._suspended_target = target
        self._suspended_window_identity_ref = self._target_window_identity_ref
        self._resume_candidate = None
        self._resume_catalog_refreshed = False
        self._resume_publication_id = None
        self._handed_off = True
        self.grants.clear()

    def _clear_suspended_continuity(self) -> None:
        self._suspended_target = None
        self._suspended_window_identity_ref = None
        self._resume_candidate = None
        self._resume_catalog_refreshed = False
        self._resume_publication_id = None

    def _require_pending_plan(self, snapshot_id: str, plan_ref: str) -> _PendingActionPlan:
        pending = self._pending_plan
        if (
            pending is None
            or pending.snapshot_id != snapshot_id
            or pending.plan.plan_ref != plan_ref
        ):
            raise ComputerSessionError("stale_plan", "stale_plan: plan is missing or consumed")
        return pending

    @staticmethod
    def _coerce_plan(value: object) -> ComputerActionPlan:
        if isinstance(value, ComputerActionPlan):
            return value
        raise ComputerSessionError("stale_plan", "stale_plan: malformed action plan")

    @classmethod
    def _window_identity_ref_from_catalog(
        cls,
        catalog: ComputerAppCatalog | None,
        app_ref: str,
        window_ref: str,
    ) -> str | None:
        if not isinstance(catalog, ComputerAppCatalog):
            return None
        matches: list[str] = []
        for application in catalog.apps:
            if not isinstance(application, Mapping) or application.get("app_ref") != app_ref:
                continue
            windows = application.get("windows")
            if not isinstance(windows, list):
                return None
            for window in windows:
                if not isinstance(window, Mapping) or window.get("window_ref") != window_ref:
                    continue
                identity_ref = window.get("window_identity_ref")
                if window.get("bindable") is not True or not cls._valid_window_identity_ref(identity_ref):
                    return None
                matches.append(identity_ref)
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _resume_candidate_from_catalog(
        cls,
        catalog: ComputerAppCatalog,
        identity_ref: str | None,
    ) -> ComputerTarget | None:
        if not cls._valid_window_identity_ref(identity_ref):
            return None
        matches: list[ComputerTarget] = []
        for application in catalog.apps:
            if not isinstance(application, Mapping):
                continue
            app_ref = application.get("app_ref")
            windows = application.get("windows")
            if not isinstance(app_ref, str) or not isinstance(windows, list):
                continue
            for window in windows:
                if (
                    not isinstance(window, Mapping)
                    or window.get("bindable") is not True
                    or window.get("window_identity_ref") != identity_ref
                ):
                    continue
                window_ref = window.get("window_ref")
                if not isinstance(window_ref, str):
                    return None
                try:
                    matches.append(ComputerTarget(app_ref, window_ref))
                except (TypeError, ValueError):
                    return None
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _catalog_window_identities(cls, catalog: ComputerAppCatalog | None) -> frozenset[str] | None:
        """Find conflicts with a native absence proof, never infer absence."""
        if not isinstance(catalog, ComputerAppCatalog):
            return None
        identities: set[str] = set()
        for application in catalog.apps:
            if not isinstance(application, Mapping) or not isinstance(application.get("windows"), list):
                return None
            for window in application["windows"]:
                if not isinstance(window, Mapping):
                    return None
                identity = window.get("window_identity_ref")
                if identity is None:
                    continue
                if not cls._valid_window_identity_ref(identity):
                    return None
                identities.add(identity)
        return frozenset(identities)

    @staticmethod
    def _valid_window_identity_ref(value: object) -> TypeGuard[str]:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= _MAX_WINDOW_IDENTITY_REF_SCALARS
            and all(not 0xD800 <= ord(character) <= 0xDFFF for character in value)
        )

    def write_artifact(self, filename: str, content: bytes) -> str:
        """Create one private request-local artifact through the held session fd.

        Callers receive only the validated filename.  They must not use the
        public ``session_dir`` pathname for writes because its ancestors can be
        renamed after the session starts.
        """
        self._require_open()
        if (
            not isinstance(filename, str)
            or not filename
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
            or filename in {".", ".."}
        ):
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: invalid artifact filename")
        if not isinstance(content, bytes):
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: artifact content must be bytes")
        if self._session_fd < 0:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: session artifact directory is closed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(filename, flags, 0o600, dir_fd=self._session_fd)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != _owner_uid():
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: artifact is not a private regular file")
            _owner_chmod(descriptor, 0o600)
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("short artifact write")
                offset += written
            os.fsync(descriptor)
            return filename
        except ComputerSessionError:
            raise
        except OSError as exc:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: cannot create artifact") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _verify_smart_snapshot_artifacts(
        self,
        snapshot: ComputerSnapshot,
        image_artifact: ComputerArtifact,
        detail_artifact: ComputerArtifact | None,
    ) -> tuple[
        ComputerSnapshot,
        Path,
        bytes,
        tuple[int, int],
        str,
        Path,
        bytes,
        str,
        dict[str, tuple[int, int, int, int, int, int]],
    ]:
        if detail_artifact is None:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: detail artifact is missing")
        payload = dict(snapshot.payload)
        if payload.get("image_artifact") != image_artifact.filename:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: helper changed PNG artifact name")
        if payload.get("text_detail_artifact") != detail_artifact.filename:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: helper changed detail artifact name")
        metadata = payload.get("text_detail_metadata")
        if not isinstance(metadata, Mapping):
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: detail metadata is missing")

        self._verify_detail_quota()
        path, image_data, detail_data, detail_digest, authorities = self._read_verified_snapshot_pair(
            image_artifact.filename,
            detail_artifact.filename,
        )
        self._validate_png(image_data)
        self._validate_png_dimensions(image_data, payload.get("pixel_size"))
        try:
            decode_text_detail_envelope(
                detail_data,
                snapshot_id=snapshot.snapshot_id,
                metadata=metadata,
                sha256=detail_digest,
            )
        except (TypeError, ValueError) as exc:
            raise ComputerSessionError(
                "unsafe_artifact",
                f"unsafe_artifact: AX detail {exc}",
            ) from exc
        payload["text_detail_path"] = str(path)
        image_authority = authorities[image_artifact.filename]
        return (
            ComputerSnapshot(snapshot.snapshot_id, payload),
            self._current_session_path() / image_artifact.filename,
            image_data,
            image_authority[:2],
            hashlib.sha256(image_data).hexdigest(),
            path,
            detail_data,
            detail_digest,
            authorities,
        )

    def _read_verified_snapshot_pair(
        self,
        image_filename: str,
        detail_filename: str,
    ) -> tuple[
        Path,
        bytes,
        bytes,
        str,
        dict[str, tuple[int, int, int, int, int, int]],
    ]:
        """Open both published files before reading either and verify stable identities."""
        self._require_open()
        for filename in (image_filename, detail_filename):
            if not self._plain_artifact_name(filename):
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: invalid artifact filename")
        descriptors: list[int] = []
        try:
            directory = os.fstat(self._session_fd)
            before = [
                os.stat(filename, dir_fd=self._session_fd, follow_symlinks=False)
                for filename in (image_filename, detail_filename)
            ]
            self._validate_snapshot_artifact_stat(
                before[0], directory=directory, maximum=64 * 1024 * 1024, kind="PNG"
            )
            self._validate_snapshot_artifact_stat(
                before[1], directory=directory, maximum=self._MAX_DETAIL_BYTES, kind="AX detail"
            )
            if (before[0].st_dev, before[0].st_ino) == (before[1].st_dev, before[1].st_ino):
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: snapshot artifacts share one inode")
            for filename in (image_filename, detail_filename):
                descriptors.append(os.open(
                    filename,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=self._session_fd,
                ))
            opened = [os.fstat(descriptor) for descriptor in descriptors]
            for index, (expected, actual) in enumerate(zip(before, opened, strict=True)):
                kind = "PNG" if index == 0 else "AX detail"
                maximum = 64 * 1024 * 1024 if index == 0 else self._MAX_DETAIL_BYTES
                self._validate_snapshot_artifact_stat(
                    actual,
                    directory=directory,
                    maximum=maximum,
                    kind=kind,
                )
                if self._artifact_authority(expected) != self._artifact_authority(actual):
                    raise ComputerSessionError(
                        "unsafe_artifact",
                        f"unsafe_artifact: {kind} authority changed while opening",
                    )
            if (opened[0].st_dev, opened[0].st_ino) == (opened[1].st_dev, opened[1].st_ino):
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: snapshot artifacts share one inode")
            contents = [
                self._read_exact_artifact(descriptor, expected.st_size)
                for descriptor, expected in zip(descriptors, opened, strict=True)
            ]
            after_descriptors = [os.fstat(descriptor) for descriptor in descriptors]
            after_names = [
                os.stat(filename, dir_fd=self._session_fd, follow_symlinks=False)
                for filename in (image_filename, detail_filename)
            ]
            for index in range(2):
                kind = "PNG" if index == 0 else "AX detail"
                maximum = 64 * 1024 * 1024 if index == 0 else self._MAX_DETAIL_BYTES
                self._validate_snapshot_artifact_stat(
                    after_descriptors[index],
                    directory=directory,
                    maximum=maximum,
                    kind=kind,
                )
                self._validate_snapshot_artifact_stat(
                    after_names[index],
                    directory=directory,
                    maximum=maximum,
                    kind=kind,
                )
                if (
                    len(contents[index]) != opened[index].st_size
                    or self._artifact_authority(opened[index])
                    != self._artifact_authority(after_descriptors[index])
                    or self._artifact_authority(opened[index])
                    != self._artifact_authority(after_names[index])
                ):
                    raise ComputerSessionError(
                        "unsafe_artifact",
                        f"unsafe_artifact: {kind} authority changed while reading",
                    )
            current_path = self._current_session_path()
            authorities = {
                image_filename: self._artifact_authority(after_names[0]),
                detail_filename: self._artifact_authority(after_names[1]),
            }
            return (
                current_path / detail_filename,
                contents[0],
                contents[1],
                hashlib.sha256(contents[1]).hexdigest(),
                authorities,
            )
        except ComputerSessionError:
            raise
        except OSError as exc:
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: cannot verify snapshot artifact pair",
            ) from exc
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    @staticmethod
    def _read_exact_artifact(descriptor: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _validate_snapshot_artifact_stat(
        value: os.stat_result,
        *,
        directory: os.stat_result,
        maximum: int,
        kind: str,
    ) -> None:
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != _owner_uid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
            or value.st_dev != directory.st_dev
            or not 1 <= value.st_size <= maximum
        ):
            raise ComputerSessionError(
                "unsafe_artifact",
                f"unsafe_artifact: {kind} must be a singly linked owned regular file with mode 0600 and bounded size",
            )

    @staticmethod
    def _artifact_authority(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_uid,
            value.st_mode,
            value.st_nlink,
            value.st_size,
        )

    @staticmethod
    def _validate_png_dimensions(data: bytes, pixel_size: object) -> None:
        if (
            len(data) < 24
            or data[:8] != b"\x89PNG\r\n\x1a\n"
            or data[12:16] != b"IHDR"
        ):
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG header is invalid")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if (
            not isinstance(pixel_size, Mapping)
            or set(pixel_size) != {"width", "height"}
            or pixel_size["width"] != width
            or pixel_size["height"] != height
            or isinstance(pixel_size["width"], bool)
            or isinstance(pixel_size["height"], bool)
        ):
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: PNG dimensions do not match snapshot metadata",
            )

    def _verify_detail_quota(self) -> None:
        try:
            names = self._bounded_artifact_names(self._session_fd)
        except OSError as exc:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: cannot inspect detail quota") from exc
        detail_names = [name for name in names if self._valid_detail_artifact_name(name)]
        if len(detail_names) > self._MAX_DETAIL_ARTIFACTS:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: detail artifact quota exceeded")
        total = 0
        directory = os.fstat(self._session_fd)
        for name in detail_names:
            try:
                value = os.stat(name, dir_fd=self._session_fd, follow_symlinks=False)
            except OSError as exc:
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: cannot inspect detail quota") from exc
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_uid != _owner_uid()
                or stat.S_IMODE(value.st_mode) != 0o600
                or value.st_nlink != 1
                or value.st_dev != directory.st_dev
                or value.st_size < 1
                or value.st_size > self._MAX_DETAIL_BYTES
            ):
                raise ComputerSessionError(
                    "unsafe_artifact",
                    "unsafe_artifact: detail artifact must be a singly linked owned regular file with mode 0600 and bounded size",
                )
            total += value.st_size
            if total > self._MAX_DETAIL_SESSION_BYTES:
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: detail artifact quota exceeded")

    @staticmethod
    def _valid_detail_artifact_name(name: str) -> bool:
        prefix = "snapshot-"
        suffix = ".ax.json"
        if not name.startswith(prefix) or not name.endswith(suffix):
            return False
        token = name[len(prefix):-len(suffix)]
        return len(token) == 32 and all(character in "0123456789abcdef" for character in token)

    def _cleanup_snapshot_request_artifacts(self, *names: str) -> None:
        for name in names:
            try:
                os.stat(name, dir_fd=self._session_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot inspect partial snapshot") from exc
            self._remove_owned_regular_artifact(name)
        try:
            os.fsync(self._session_fd)
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: partial snapshot cleanup is not durable") from exc

    def _cleanup_current_snapshot_artifacts(self) -> None:
        if not self._current_snapshot_artifacts:
            return
        try:
            self._acquire_exclusive_snapshot_cleanup_lease()
            try:
                self._preflight_current_snapshot_artifacts()
                self._cleanup_snapshot_request_artifacts(*self._current_snapshot_artifacts)
            finally:
                self._restore_shared_session_lease()
        except ComputerSessionError:
            self._artifact_poisoned = True
            raise
        self._current_snapshot_artifacts = {}

    def _acquire_exclusive_snapshot_cleanup_lease(self) -> None:
        try:
            _flock(self._session_fd, _LOCK_EX | _LOCK_NB)
        except OSError as exc:
            try:
                _flock(self._session_fd, _LOCK_SH)
            except OSError as restore_exc:
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: snapshot cleanup lease failed and shared lease could not be restored",
                ) from restore_exc
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: peer still holds the session cleanup lease",
                ) from exc
            raise ComputerSessionError(
                "unsafe_cache",
                "unsafe_cache: cannot acquire the session cleanup lease",
            ) from exc

    def _restore_shared_session_lease(self) -> None:
        try:
            _flock(self._session_fd, _LOCK_SH)
        except OSError as exc:
            raise ComputerSessionError(
                "unsafe_cache",
                "unsafe_cache: shared session lease could not be restored after cleanup",
            ) from exc

    def _preflight_current_snapshot_artifacts(self) -> None:
        expected = self._current_snapshot_artifacts
        detail_count = sum(self._valid_detail_artifact_name(name) for name in expected)
        image_count = sum(self._valid_legacy_snapshot_artifact_name(name) for name in expected)
        if (
            (len(expected), image_count, detail_count) not in {(1, 1, 0), (2, 1, 1)}
            or not all(self._valid_snapshot_residual_name(name) for name in expected)
        ):
            raise ComputerSessionError(
                "unsafe_cache",
                "unsafe_cache: verified snapshot artifact authority is incomplete",
            )
        try:
            directory = os.fstat(self._session_fd)
            observed = {
                name: os.stat(name, dir_fd=self._session_fd, follow_symlinks=False)
                for name in expected
            }
        except OSError as exc:
            raise ComputerSessionError(
                "unsafe_cache",
                "unsafe_cache: verified snapshot artifact authority cannot be inspected",
            ) from exc
        for name, metadata in observed.items():
            detail = self._valid_detail_artifact_name(name)
            self._validate_snapshot_artifact_stat(
                metadata,
                directory=directory,
                maximum=self._MAX_DETAIL_BYTES if detail else 64 * 1024 * 1024,
                kind="AX detail" if detail else "PNG",
            )
            if self._artifact_authority(metadata) != expected[name]:
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: verified snapshot artifact authority changed",
                )

    def _snapshot_artifact_inventory(self) -> dict[str, tuple[int, int, int, int, int, int]]:
        try:
            names = self._bounded_artifact_names(self._session_fd)
        except OSError as exc:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: cannot inspect artifact inventory") from exc
        inventory: dict[str, tuple[int, int, int, int, int, int]] = {}
        for name in names:
            try:
                value = os.stat(name, dir_fd=self._session_fd, follow_symlinks=False)
            except OSError as exc:
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: artifact inventory changed") from exc
            inventory[name] = (
                value.st_dev,
                value.st_ino,
                value.st_uid,
                value.st_mode,
                value.st_nlink,
                value.st_size,
            )
        return inventory

    @staticmethod
    def _bounded_artifact_names(directory_fd: int) -> list[str]:
        names: list[str] = []
        encoded_size = 0
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if len(names) == 128:
                    raise ComputerSessionError(
                        "unsafe_cache",
                        "unsafe_cache: artifact inventory is unbounded",
                    )
                try:
                    encoded_size += len(entry.name.encode("utf-8"))
                except UnicodeEncodeError as exc:
                    raise ComputerSessionError(
                        "unsafe_cache",
                        "unsafe_cache: artifact inventory contains invalid Unicode",
                    ) from exc
                if encoded_size > 32 * 1024:
                    raise ComputerSessionError(
                        "unsafe_cache",
                        "unsafe_cache: artifact inventory is unbounded",
                    )
                names.append(entry.name)
        return names

    def _verify_snapshot_inventory_delta(
        self,
        before: Mapping[str, tuple[int, int, int, int, int, int]],
        *,
        expected: Mapping[str, tuple[int, int, int, int, int, int]],
    ) -> None:
        after = self._snapshot_artifact_inventory()
        if (
            any(after.get(name) != identity for name, identity in before.items())
            or set(after) - set(before) != set(expected)
            or any(after.get(name) != identity for name, identity in expected.items())
        ):
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: smart snapshot published an unexpected inventory delta",
            )

    def _validate_existing_snapshot_inventory(
        self,
        inventory: Mapping[str, tuple[int, int, int, int, int, int]],
    ) -> None:
        directory = os.fstat(self._session_fd)
        if self._LEASE_NAME not in inventory:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: session lease is missing")
        for name, identity in inventory.items():
            if name == self._LEASE_NAME:
                lease = os.fstat(self._lease_fd)
                if (
                    identity[:2] != (lease.st_dev, lease.st_ino)
                    or identity[2] != _owner_uid()
                    or not stat.S_ISREG(identity[3])
                    or stat.S_IMODE(identity[3]) != 0o600
                    or identity[4] != 1
                ):
                    raise ComputerSessionError(
                        "unsafe_cache",
                        "unsafe_cache: session lease identity changed",
                    )
                continue
            if (
                not self._valid_snapshot_residual_name(name)
                or identity[0] != directory.st_dev
                or identity[2] != _owner_uid()
                or not stat.S_ISREG(identity[3])
                or stat.S_IMODE(identity[3]) != 0o600
                or identity[4] != 1
                or identity[5] < 1
            ):
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: uncertain snapshot residual requires explicit recovery",
                )

    @staticmethod
    def _plain_artifact_name(filename: object) -> bool:
        return (
            isinstance(filename, str)
            and bool(filename)
            and "\x00" not in filename
            and "/" not in filename
            and "\\" not in filename
            and Path(filename).name == filename
            and filename not in {".", ".."}
        )

    def _current_session_path(self) -> Path:
        try:
            value: str | None = None
            if sys.platform == "darwin" and _fcntl is not None:
                raw = _fcntl.fcntl(self._session_fd, _fcntl.F_GETPATH, b"\0" * 1024)
                value = os.fsdecode(raw.split(b"\0", 1)[0])
            elif sys.platform == "linux":
                # Procfs follows the held descriptor through renames. Treat
                # its pathname only as a candidate for the identity checks.
                value = os.readlink(f"/proc/self/fd/{self._session_fd}")
            if value is not None:
                path = Path(value)
                current = path.stat(follow_symlinks=False)
                parent = path.parent.stat(follow_symlinks=False)
                cache = os.fstat(self._cache_fd)
                if (
                    path.is_absolute()
                    and (current.st_dev, current.st_ino) == self._session_identity
                    and self._same_owned_directory(cache, parent)
                ):
                    return path
        except (OSError, ValueError):
            pass
        try:
            current = self._session_dir.stat(follow_symlinks=False)
        except OSError as exc:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: session path is unavailable") from exc
        if (current.st_dev, current.st_ino) != self._session_identity:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: session path identity changed")
        try:
            parent = self._session_dir.parent.stat(follow_symlinks=False)
            cache = os.fstat(self._cache_fd)
        except OSError as exc:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: cache root path is unavailable") from exc
        if not self._same_owned_directory(cache, parent):
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: cache root path identity changed")
        return self._session_dir

    def read_verified_png_artifact(
        self,
        filename: str,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        expected_identity: tuple[int, int] | None = None,
        expected_sha256: str | None = None,
    ) -> tuple[Path, bytes, tuple[int, int], str]:
        """Read a helper capture through the held session fd after strict verification.

        The pathname returned is a request-local compatibility hint for image
        tooling.  Security decisions are based only on the held directory
        descriptor plus the opened file identity and bytes, never on pathname
        containment.
        """
        path, data, identity, digest, _ = self._read_verified_png_artifact_details(
            filename,
            max_bytes=max_bytes,
            expected_identity=expected_identity,
            expected_sha256=expected_sha256,
        )
        return path, data, identity, digest

    def _read_verified_png_artifact_details(
        self,
        filename: str,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        expected_identity: tuple[int, int] | None = None,
        expected_sha256: str | None = None,
    ) -> tuple[
        Path,
        bytes,
        tuple[int, int],
        str,
        tuple[int, int, int, int, int, int],
    ]:
        self._require_open()
        if (
            not isinstance(filename, str)
            or not filename
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
            or filename in {".", ".."}
        ):
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: invalid artifact filename")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        descriptor: int | None = None
        try:
            directory = os.fstat(self._session_fd)
            before_open = os.stat(filename, dir_fd=self._session_fd, follow_symlinks=False)
            identity = (before_open.st_dev, before_open.st_ino)
            if expected_identity is not None and identity != expected_identity:
                raise ComputerSessionError(
                    "unsafe_artifact",
                    "unsafe_artifact: screenshot identity changed before postcondition verification",
                )
            if (
                not stat.S_ISREG(before_open.st_mode)
                or before_open.st_uid != _owner_uid()
                or stat.S_IMODE(before_open.st_mode) != 0o600
                or before_open.st_nlink != 1
                or before_open.st_dev != directory.st_dev
                or not 1 <= before_open.st_size <= max_bytes
            ):
                raise ComputerSessionError(
                    "unsafe_artifact",
                    "unsafe_artifact: screenshot must be a singly linked owned regular file with mode 0600 and bounded size",
                )
            descriptor = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self._session_fd,
            )
            opened = os.fstat(descriptor)
            if (
                not self._same_owned_regular_file(before_open, opened)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_nlink != 1
                or opened.st_dev != directory.st_dev
                or opened.st_size != before_open.st_size
            ):
                raise ComputerSessionError(
                    "unsafe_artifact",
                    "unsafe_artifact: screenshot identity changed while opening",
                )
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) != opened.st_size:
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG read was incomplete")
            after_descriptor = os.fstat(descriptor)
            after_name = os.stat(filename, dir_fd=self._session_fd, follow_symlinks=False)
            self._validate_snapshot_artifact_stat(
                after_descriptor,
                directory=directory,
                maximum=max_bytes,
                kind="PNG",
            )
            self._validate_snapshot_artifact_stat(
                after_name,
                directory=directory,
                maximum=max_bytes,
                kind="PNG",
            )
            authority = self._artifact_authority(opened)
            if (
                authority != self._artifact_authority(after_descriptor)
                or authority != self._artifact_authority(after_name)
            ):
                raise ComputerSessionError(
                    "unsafe_artifact",
                    "unsafe_artifact: screenshot authority changed while reading",
                )
            self._validate_png(data)
            digest = hashlib.sha256(data).hexdigest()
            if expected_sha256 is not None and digest != expected_sha256:
                raise ComputerSessionError(
                    "unsafe_artifact",
                    "unsafe_artifact: screenshot content changed before postcondition verification",
                )
            return self._current_session_path() / filename, data, identity, digest, authority
        except ComputerSessionError:
            raise
        except OSError as exc:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: cannot verify screenshot") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _validate_png(data: bytes) -> None:
        if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: screenshot is not a valid PNG")
        offset = 8
        saw_header = False
        saw_data = False
        saw_end = False
        width = height = bit_depth = color_type = interlace = 0
        compressed_pixels: list[bytes] = []
        while offset < len(data):
            if offset + 12 > len(data):
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG is truncated")
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            chunk_type = data[offset + 4:offset + 8]
            chunk_end = offset + 12 + length
            if chunk_end > len(data):
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG chunk is truncated")
            chunk_data = data[offset + 8:offset + 8 + length]
            expected_crc = struct.unpack(">I", data[offset + 8 + length:chunk_end])[0]
            actual_crc = zlib.crc32(chunk_type)
            actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
            if expected_crc != actual_crc:
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG checksum is invalid")
            if not saw_header:
                if chunk_type != b"IHDR" or length != 13:
                    raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG header is invalid")
                width, height = struct.unpack(">II", chunk_data[:8])
                bit_depth, color_type, compression, filter_method, interlace = chunk_data[8:13]
                valid_depths = {
                    0: {1, 2, 4, 8, 16},
                    2: {8, 16},
                    3: {1, 2, 4, 8},
                    4: {8, 16},
                    6: {8, 16},
                }
                if (
                    width < 1
                    or height < 1
                    or width > 16_384
                    or height > 16_384
                    or width * height > 67_108_864
                    or bit_depth not in valid_depths.get(color_type, set())
                    or compression != 0
                    or filter_method != 0
                    or interlace not in {0, 1}
                ):
                    raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG dimensions are invalid")
                saw_header = True
            elif chunk_type == b"IHDR":
                raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG has duplicate headers")
            if chunk_type == b"IDAT":
                saw_data = True
                compressed_pixels.append(chunk_data)
            if chunk_type == b"IEND":
                if length != 0 or chunk_end != len(data):
                    raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG terminator is invalid")
                saw_end = True
                break
            offset = chunk_end
        if not saw_header or not saw_data or not saw_end:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG is incomplete")
        ComputerSessionManager._validate_png_pixels(
            compressed_pixels,
            width=width,
            height=height,
            bit_depth=bit_depth,
            color_type=color_type,
            interlace=interlace,
        )

    @staticmethod
    def _validate_png_pixels(
        compressed: list[bytes],
        *,
        width: int,
        height: int,
        bit_depth: int,
        color_type: int,
        interlace: int,
    ) -> None:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
        passes = (
            ((0, 0, 1, 1),)
            if interlace == 0
            else (
                (0, 0, 8, 8),
                (4, 0, 8, 8),
                (0, 4, 4, 8),
                (2, 0, 4, 4),
                (0, 2, 2, 4),
                (1, 0, 2, 2),
                (0, 1, 1, 2),
            )
        )
        rows: list[tuple[int, int]] = []
        expected = 0
        for start_x, start_y, step_x, step_y in passes:
            pass_width = 0 if width <= start_x else (width - start_x + step_x - 1) // step_x
            pass_height = 0 if height <= start_y else (height - start_y + step_y - 1) // step_y
            if pass_width == 0 or pass_height == 0:
                continue
            row_bytes = (pass_width * channels * bit_depth + 7) // 8
            rows.append((pass_height, row_bytes))
            expected += pass_height * (row_bytes + 1)
        if expected > 256 * 1024 * 1024:
            raise ComputerSessionError("unsafe_artifact", "unsafe_artifact: PNG pixels exceed size limit")
        decoder = zlib.decompressobj()
        try:
            pixels = decoder.decompress(b"".join(compressed), expected + 1)
            pixels += decoder.flush(max(1, expected + 1 - len(pixels)))
        except zlib.error as exc:
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: PNG compressed pixels are invalid",
            ) from exc
        if (
            len(pixels) != expected
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise ComputerSessionError(
                "unsafe_artifact",
                "unsafe_artifact: PNG pixel stream has an invalid length",
            )
        offset = 0
        for pass_height, row_bytes in rows:
            for _ in range(pass_height):
                if pixels[offset] > 4:
                    raise ComputerSessionError(
                        "unsafe_artifact",
                        "unsafe_artifact: PNG scanline filter is invalid",
                    )
                offset += row_bytes + 1

    def _require_open(self) -> None:
        if self._closed:
            raise ComputerSessionError("session_closed")
        if self._artifact_poisoned:
            raise ComputerSessionError(
                "unsafe_cache",
                "unsafe_cache: artifact inventory is poisoned; cleanup could not be proved",
            )
        if self._close_task is not None and not self._close_task.done():
            raise ComputerSessionError("session_closing")

    def _require_target(self) -> ComputerTarget:
        if self._target is None:
            raise ComputerSessionError("target_required", "target_required: call focus first")
        return self._target

    async def _bind_backend_directory(self) -> None:
        if self._backend_directory_bound:
            return
        bind_path = getattr(self._backend, "bind_artifact_directory_path", None)
        if bind_path is not None:
            result = bind_path(self._session_fd, self._session_dir)
            if inspect.isawaitable(result):
                await result
            self._backend_directory_bound = True
            return
        bind = getattr(self._backend, "bind_artifact_directory", None)
        if bind is not None:
            result = bind(self._session_fd)
            if inspect.isawaitable(result):
                await result
        self._backend_directory_bound = True

    @staticmethod
    def _coerce_action(action: ComputerAction | Mapping[str, object]) -> ComputerAction:
        if isinstance(action, ComputerAction):
            return action
        if not isinstance(action, Mapping):
            raise TypeError("actions must contain ComputerAction objects or mappings")
        return ComputerAction.from_mapping(action)

    @staticmethod
    def _validate_action_coordinates(
        snapshot: ComputerSnapshot,
        actions: list[ComputerAction],
    ) -> None:
        coordinate_actions = [
            action for action in actions
            if action.x is not None or action.y is not None
            or action.end_x is not None or action.end_y is not None
        ]
        if not coordinate_actions:
            return
        raw = snapshot.payload.get("capture_bounds")
        if not isinstance(raw, Mapping):
            raise ComputerSessionError(
                "stale_snapshot",
                "stale_snapshot: capture omitted coordinate bounds",
            )
        try:
            x = float(raw["x"])
            y = float(raw["y"])
            width = float(raw["width"])
            height = float(raw["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ComputerSessionError(
                "stale_snapshot",
                "stale_snapshot: capture has malformed coordinate bounds",
            ) from exc
        if (
            not all(math.isfinite(value) for value in (x, y, width, height))
            or width <= 0
            or height <= 0
        ):
            raise ComputerSessionError(
                "stale_snapshot",
                "stale_snapshot: capture has malformed coordinate bounds",
            )
        for action in coordinate_actions:
            points = ((action.x, action.y), (action.end_x, action.end_y))
            for point_x, point_y in points:
                if point_x is None and point_y is None:
                    continue
                if (
                    point_x is None
                    or point_y is None
                    or not x <= point_x <= x + width
                    or not y <= point_y <= y + height
                ):
                    raise ComputerSessionError(
                        "out_of_bounds",
                        "out_of_bounds: action coordinate is outside capture bounds",
                    )

    @staticmethod
    def _prepare_cache_root(cache_root: Path) -> tuple[Path, int]:
        if cache_root.is_symlink():
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cache root is a symlink")
        descriptor: int | None = None
        try:
            cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(cache_root, flags)
            root_stat = os.fstat(descriptor)
            if not stat.S_ISDIR(root_stat.st_mode) or cache_root.is_symlink():
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: cache root is not a directory")
            _owner_chmod(descriptor, 0o700)
            return cache_root.resolve(strict=True), descriptor
        except ComputerSessionError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot create private cache root") from exc

    def _open_cleanup_lock(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(self._CLEANUP_LOCK_NAME, flags, 0o600, dir_fd=self._cache_fd)
            metadata = os.fstat(descriptor)
            current = os.stat(self._CLEANUP_LOCK_NAME, dir_fd=self._cache_fd, follow_symlinks=False)
            if (
                not self._same_owned_regular_file(metadata, current)
                or metadata.st_nlink != 1
            ):
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: cleanup lock is unsafe")
            _owner_chmod(descriptor, 0o600)
            return descriptor
        except ComputerSessionError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot open cleanup lock") from exc

    def _verify_cleanup_lock_identity(self) -> None:
        try:
            current = os.stat(self._CLEANUP_LOCK_NAME, dir_fd=self._cache_fd, follow_symlinks=False)
            opened = os.fstat(self._cleanup_lock_fd)
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cleanup lock identity changed") from exc
        if (
            not self._same_owned_regular_file(current, opened)
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cleanup lock identity changed")

    @staticmethod
    def _looks_like_session_name(name: str) -> bool:
        return len(name) == 32 and all(character in "0123456789abcdef" for character in name)

    def _cleanup_stale_sessions(self) -> None:
        """Remove only dead, descriptor-verified sessions while holding the root lock."""
        try:
            names = os.listdir(self._cache_fd)
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot inspect cache root") from exc
        for name in names:
            if name == self._CLEANUP_LOCK_NAME or not self._looks_like_session_name(name):
                continue
            self._cleanup_stale_session(name)

    def _cleanup_stale_session(self, name: str) -> None:
        directory_fd: int | None = None
        lease_fd: int | None = None
        directory_locked = False
        lease_locked = False
        has_lease = False
        try:
            before_open = os.stat(name, dir_fd=self._cache_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(before_open.st_mode)
                or before_open.st_uid != _owner_uid()
                or stat.S_IMODE(before_open.st_mode) != 0o700
            ):
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: stale session directory is unsafe")
            directory_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self._cache_fd,
            )
            opened = os.fstat(directory_fd)
            if not self._same_owned_directory(before_open, opened):
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: stale session identity changed")
            try:
                _flock(directory_fd, _LOCK_EX | _LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return
                raise
            directory_locked = True
            try:
                lease_before = os.stat(self._LEASE_NAME, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                # Pre-lease versions cannot prove liveness.  Preserve recent
                # directories and retire only an old, private legacy orphan.
                if time.time() - before_open.st_mtime < self._LEGACY_STALE_SECONDS:
                    return
            else:
                has_lease = True
                if (
                    not stat.S_ISREG(lease_before.st_mode)
                    or lease_before.st_uid != _owner_uid()
                    or stat.S_IMODE(lease_before.st_mode) != 0o600
                    or lease_before.st_nlink != 1
                ):
                    raise ComputerSessionError("unsafe_cache", "unsafe_cache: stale session lease is unsafe")
                lease_fd = os.open(
                    self._LEASE_NAME,
                    os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                if not self._same_owned_regular_file(lease_before, os.fstat(lease_fd)):
                    raise ComputerSessionError("unsafe_cache", "unsafe_cache: stale session lease identity changed")
                try:
                    _flock(lease_fd, _LOCK_EX | _LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        return
                    raise
                lease_locked = True

            artifact_names = self._bounded_artifact_names(directory_fd)
            if has_lease:
                uncertain_residual = any(
                    artifact_name != self._LEASE_NAME
                    and not self._valid_snapshot_residual_name(artifact_name)
                    for artifact_name in artifact_names
                )
            else:
                uncertain_residual = any(
                    not self._valid_legacy_snapshot_artifact_name(artifact_name)
                    for artifact_name in artifact_names
                )
            if uncertain_residual:
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: uncertain smart snapshot residual requires explicit recovery",
                )
            for artifact_name in artifact_names:
                artifact = os.stat(artifact_name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(artifact.st_mode)
                    or artifact.st_uid != _owner_uid()
                    or stat.S_IMODE(artifact.st_mode) != 0o600
                    or artifact.st_nlink != 1
                ):
                    raise ComputerSessionError("unsafe_cache", "unsafe_cache: stale session artifact is unsafe")
            for artifact_name in artifact_names:
                self._remove_owned_regular_at(directory_fd, artifact_name, require_private=True)
            os.fsync(directory_fd)
            self._verify_named_directory_identity(name, directory_fd)
            os.rmdir(name, dir_fd=self._cache_fd)
            os.fsync(self._cache_fd)
        except ComputerSessionError:
            raise
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot clean stale session") from exc
        finally:
            if lease_fd is not None:
                if lease_locked:
                    with suppress(OSError):
                        _flock(lease_fd, _LOCK_UN)
                os.close(lease_fd)
            if directory_fd is not None:
                if directory_locked:
                    with suppress(OSError):
                        _flock(directory_fd, _LOCK_UN)
                os.close(directory_fd)

    @classmethod
    def _valid_snapshot_residual_name(cls, name: str) -> bool:
        return cls._valid_legacy_snapshot_artifact_name(name) or cls._valid_detail_artifact_name(name)

    @staticmethod
    def _valid_legacy_snapshot_artifact_name(name: str) -> bool:
        prefix = "snapshot-"
        suffix = ".png"
        if not name.startswith(prefix) or not name.endswith(suffix):
            return False
        token = name[len(prefix):-len(suffix)]
        return len(token) == 32 and all(character in "0123456789abcdef" for character in token)

    def _create_session_dir(self) -> tuple[Path, tuple[int, int], int, int]:
        path = self._cache_root / self._session_id
        created = False
        descriptor: int | None = None
        lease_descriptor: int | None = None
        metadata: os.stat_result | None = None
        try:
            os.mkdir(self._session_id, mode=0o700, dir_fd=self._cache_fd)
            created = True
            metadata = os.stat(self._session_id, dir_fd=self._cache_fd, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != _owner_uid():
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: session directory is unsafe")
            descriptor = os.open(
                self._session_id,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._cache_fd,
            )
            opened_metadata = os.fstat(descriptor)
            if not self._same_owned_directory(metadata, opened_metadata):
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: session directory identity changed during creation",
                )
            _owner_chmod(descriptor, 0o700)
            _flock(descriptor, _LOCK_SH)
            lease_descriptor = os.open(
                self._LEASE_NAME,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=descriptor,
            )
            lease_metadata = os.fstat(lease_descriptor)
            if (
                not stat.S_ISREG(lease_metadata.st_mode)
                or lease_metadata.st_uid != _owner_uid()
                or lease_metadata.st_nlink != 1
            ):
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: session lease is unsafe")
            _owner_chmod(lease_descriptor, 0o600)
            _flock(lease_descriptor, _LOCK_EX)
            return path, (metadata.st_dev, metadata.st_ino), descriptor, lease_descriptor
        except ComputerSessionError:
            if lease_descriptor is not None:
                os.close(lease_descriptor)
                with suppress(OSError):
                    os.unlink(self._LEASE_NAME, dir_fd=descriptor)
            if descriptor is not None:
                os.close(descriptor)
            if created and metadata is not None:
                self._remove_created_session_dir(metadata)
            raise
        except OSError as exc:
            if lease_descriptor is not None:
                os.close(lease_descriptor)
                with suppress(OSError):
                    os.unlink(self._LEASE_NAME, dir_fd=descriptor)
            if descriptor is not None:
                os.close(descriptor)
            if created and metadata is not None:
                self._remove_created_session_dir(metadata)
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot create session directory") from exc

    def _remove_session_dir(self) -> None:
        if self._cache_fd < 0:
            return
        if self._session_fd < 0:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: session directory descriptor is closed")
        name = self._find_owned_session_name()
        root_entry_fd: int | None = None
        shared_lease_released = False
        root_exclusive_acquired = False
        try:
            root_entry_fd = self._open_owned_session_dir(name)
            _flock(self._session_fd, _LOCK_UN)
            shared_lease_released = True
            try:
                _flock(root_entry_fd, _LOCK_EX | _LOCK_NB)
                root_exclusive_acquired = True
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ComputerSessionError(
                        "unsafe_cache",
                        "unsafe_cache: helper still holds the session directory lease",
                    ) from exc
                raise
            self._clear_owned_session_contents()
            os.fsync(self._session_fd)
            self._current_snapshot_artifacts = {}
            self._verify_root_entry_identity(name, root_entry_fd)
            os.rmdir(name, dir_fd=self._cache_fd)
            os.fsync(self._cache_fd)
        except BaseException as exc:
            if root_exclusive_acquired and root_entry_fd is not None:
                _flock(root_entry_fd, _LOCK_UN)
                root_exclusive_acquired = False
            if shared_lease_released and self._session_fd >= 0:
                try:
                    _flock(self._session_fd, _LOCK_SH)
                except OSError as lease_exc:
                    raise ComputerSessionError(
                        "unsafe_cache",
                        "unsafe_cache: session cleanup failed and shared lease could not be restored",
                    ) from lease_exc
            if isinstance(exc, ComputerSessionError):
                raise
            if isinstance(exc, OSError):
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: cannot remove session directory",
                ) from exc
            raise
        finally:
            if root_entry_fd is not None:
                os.close(root_entry_fd)
        self._close_session_fd()

    @staticmethod
    def _same_owned_directory(expected: os.stat_result, actual: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(expected.st_mode)
            and stat.S_ISDIR(actual.st_mode)
            and expected.st_uid == _owner_uid()
            and actual.st_uid == _owner_uid()
            and (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)
        )

    @staticmethod
    def _same_owned_regular_file(expected: os.stat_result, actual: os.stat_result) -> bool:
        return (
            stat.S_ISREG(expected.st_mode)
            and stat.S_ISREG(actual.st_mode)
            and expected.st_uid == _owner_uid()
            and actual.st_uid == _owner_uid()
            and (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)
        )

    def _remove_created_session_dir(self, expected: os.stat_result) -> None:
        """Best-effort rollback that never removes a detected replacement."""
        try:
            current = os.stat(self._session_id, dir_fd=self._cache_fd, follow_symlinks=False)
        except OSError:
            return
        if not self._same_owned_directory(expected, current):
            return
        # POSIX rmdir is non-recursive.  If another same-UID actor swaps this
        # final name after the check, it can at most remove an empty directory;
        # never recursively delete a replacement's contents.
        with suppress(OSError):
            os.rmdir(self._session_id, dir_fd=self._cache_fd)

    def _find_owned_session_name(self) -> str:
        """Locate the held session inode under the original cache-root fd."""
        try:
            names = os.listdir(self._cache_fd)
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot inspect cache root") from exc
        matches: list[str] = []
        for name in names:
            try:
                metadata = os.stat(name, dir_fd=self._cache_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot inspect session directory") from exc
            if (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == _owner_uid()
                and (metadata.st_dev, metadata.st_ino) == self._session_identity
            ):
                matches.append(name)
        if len(matches) != 1:
            raise ComputerSessionError(
                "unsafe_cache",
                "unsafe_cache: session directory identity is missing or ambiguous",
            )
        return matches[0]

    def _open_owned_session_dir(self, name: str) -> int:
        try:
            before_open = os.stat(name, dir_fd=self._cache_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(before_open.st_mode)
                or before_open.st_uid != _owner_uid()
                or (before_open.st_dev, before_open.st_ino) != self._session_identity
            ):
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: session directory identity changed")
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._cache_fd,
            )
        except ComputerSessionError:
            raise
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot open session directory") from exc
        try:
            if not self._same_owned_directory(before_open, os.fstat(descriptor)):
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: session directory identity changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _clear_owned_session_contents(self) -> None:
        """Classify the complete inventory before removing any certain entry."""
        try:
            names = self._bounded_artifact_names(self._session_fd)
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot inspect session artifacts") from exc
        classified = self._classify_terminal_cleanup_inventory(names)
        for name, authority in classified:
            self._remove_owned_regular_at(
                self._session_fd,
                name,
                require_private=True,
                expected_authority=authority,
            )

    def _classify_terminal_cleanup_inventory(
        self,
        names: list[str],
    ) -> list[tuple[str, tuple[int, int, int, int, int, int]]]:
        directory = os.fstat(self._session_fd)
        classified: list[tuple[str, tuple[int, int, int, int, int, int]]] = []
        for name in names:
            if name != self._LEASE_NAME and not self._valid_snapshot_residual_name(name):
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: uncertain residual blocks terminal cleanup",
                )
            try:
                metadata = os.stat(name, dir_fd=self._session_fd, follow_symlinks=False)
            except OSError as exc:
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: terminal cleanup inventory changed during classification",
                ) from exc
            authority = self._artifact_authority(metadata)
            maximum = (
                0
                if name == self._LEASE_NAME
                else self._MAX_DETAIL_BYTES
                if self._valid_detail_artifact_name(name)
                else 64 * 1024 * 1024
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != _owner_uid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_dev != directory.st_dev
                or (name == self._LEASE_NAME and authority[:2] != self._artifact_authority(os.fstat(self._lease_fd))[:2])
                or (name != self._LEASE_NAME and not 1 <= metadata.st_size <= maximum)
                or (name == self._LEASE_NAME and metadata.st_size != maximum)
            ):
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: terminal cleanup artifact authority is invalid",
                )
            classified.append((name, authority))
        return sorted(classified, key=lambda item: item[0] == self._LEASE_NAME)

    def _remove_owned_regular_artifact(self, name: str) -> None:
        self._remove_owned_regular_at(self._session_fd, name, require_private=True)

    @staticmethod
    def _remove_owned_regular_at(
        directory_fd: int,
        name: str,
        *,
        require_private: bool,
        expected_authority: tuple[int, int, int, int, int, int] | None = None,
    ) -> None:
        descriptor: int | None = None
        try:
            before_open = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            before_authority = ComputerSessionManager._artifact_authority(before_open)
            if (
                not stat.S_ISREG(before_open.st_mode)
                or before_open.st_uid != _owner_uid()
                or (require_private and stat.S_IMODE(before_open.st_mode) != 0o600)
                or (require_private and before_open.st_nlink != 1)
                or (expected_authority is not None and before_authority != expected_authority)
            ):
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: session artifact is unsafe")
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            opened = os.fstat(descriptor)
            if ComputerSessionManager._artifact_authority(opened) != before_authority:
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: session artifact identity changed")
            final_named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if ComputerSessionManager._artifact_authority(final_named) != before_authority:
                raise ComputerSessionError("unsafe_cache", "unsafe_cache: session artifact identity changed")
            os.unlink(name, dir_fd=directory_fd)
            unlinked = os.fstat(descriptor)
            if (
                not stat.S_ISREG(unlinked.st_mode)
                or unlinked.st_uid != _owner_uid()
                or (unlinked.st_dev, unlinked.st_ino) != (opened.st_dev, opened.st_ino)
                or stat.S_IMODE(unlinked.st_mode) != stat.S_IMODE(opened.st_mode)
                or unlinked.st_size != opened.st_size
                or unlinked.st_nlink != opened.st_nlink - 1
            ):
                raise ComputerSessionError(
                    "unsafe_cache",
                    "unsafe_cache: session artifact unlink authority changed",
                )
        except ComputerSessionError:
            raise
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: cannot remove session artifact") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _verify_named_directory_identity(self, name: str, directory_fd: int) -> None:
        try:
            current = os.stat(name, dir_fd=self._cache_fd, follow_symlinks=False)
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: stale session identity changed") from exc
        if not self._same_owned_directory(current, os.fstat(directory_fd)):
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: stale session identity changed")

    def _verify_root_entry_identity(self, name: str, root_entry_fd: int) -> None:
        try:
            current = os.stat(name, dir_fd=self._cache_fd, follow_symlinks=False)
        except OSError as exc:
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: session directory identity changed") from exc
        if not self._same_owned_directory(current, os.fstat(root_entry_fd)):
            raise ComputerSessionError("unsafe_cache", "unsafe_cache: session directory identity changed")

    def _close_cache_fd(self) -> None:
        if self._cleanup_lock_fd >= 0:
            os.close(self._cleanup_lock_fd)
            self._cleanup_lock_fd = -1
        if self._cache_fd >= 0:
            os.close(self._cache_fd)
            self._cache_fd = -1

    def _close_session_fd(self) -> None:
        if self._lease_fd >= 0:
            os.close(self._lease_fd)
            self._lease_fd = -1
        if self._session_fd >= 0:
            os.close(self._session_fd)
            self._session_fd = -1
