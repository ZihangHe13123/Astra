"""Safe NDJSON transport and macOS implementation of :mod:`computer_backend`."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import signal
import stat
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol, TypeGuard

from .computer_backend import (
    ComputerActionPlan,
    ComputerActionResult,
    ComputerAppCatalog,
    ComputerArtifact,
    ComputerBackendAppState,
    ComputerTarget,
)
from .computer_protocol import (
    MAX_RESPONSE_BYTES,
    ComputerAction,
    ComputerActResult,
    ComputerError,
    ComputerErrorCode,
    ComputerInteractionMode,
    ComputerRequest,
    ComputerResponse,
    ComputerSnapshot,
    ComputerSnapshotTextDetailMode,
    DispatchPlanSummary,
    ForegroundFragmentDeclaration,
    ForegroundFragmentPlanRequest,
    FragmentStageAuthority,
    FragmentStageCommit,
    FragmentStageCommitResult,
    TakeoverOutcome,
    decode_response,
    encode_request,
    validate_snapshot_payload,
)

_MAX_STDERR_BYTES = 64 * 1024
_DEFAULT_REQUEST_TIMEOUT = 15.0
_REQUEST_CLEANUP_TIMEOUT = 7.0
_MAX_PLANNED_DELAY_MS = 10_000
logger = logging.getLogger(__name__)
_BUNDLED_HELPER = Path(".astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper")
_MAX_CATALOG_APPLICATIONS = 100
_MAX_CATALOG_WINDOWS = 200
_MAX_CATALOG_REFERENCE_SCALARS = 256
_MAX_CATALOG_STRING_SCALARS = 512
_CATALOG_BINDING_STATUSES = {
    "ready",
    "accessibility_permission_required",
    "ax_window_unmatched",
}


class HelperTransportError(RuntimeError):
    """A helper launch, framing, or process-lifetime failure."""


class HelperApplicationError(HelperTransportError):
    """A well-formed, bounded native application failure."""

    def __init__(
        self,
        error: ComputerError,
        *,
        result: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(error, ComputerError):
            raise TypeError("error must be a ComputerError")
        self.error = error
        self.result = dict(result) if isinstance(result, Mapping) else None
        super().__init__(error.code.value)


class _RequestTransport(Protocol):
    async def request(self, request: ComputerRequest) -> ComputerResponse: ...

    async def close(self) -> None: ...

    async def configure_artifact_directory(
        self,
        directory_fd: int,
        directory_path: Path | None = None,
    ) -> None: ...


def _valid_catalog_string(value: object, maximum: int, *, nonempty: bool = False) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and (bool(value) or not nonempty)
        and len(value) <= maximum
        and not any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    )


def _decode_catalog_bounds(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping) or set(value) != {"x", "y", "width", "height"}:
        raise ValueError("malformed catalog bounds")
    decoded: dict[str, int | float] = {}
    for key in ("x", "y", "width", "height"):
        coordinate = value[key]
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or not math.isfinite(coordinate)
            or (key in {"width", "height"} and coordinate <= 0)
        ):
            raise ValueError("malformed catalog bounds")
        decoded[key] = coordinate
    return decoded


def _decode_catalog_apps(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or len(value) > _MAX_CATALOG_APPLICATIONS:
        raise ValueError("malformed catalog applications")

    app_refs: set[str] = set()
    window_refs: set[str] = set()
    identity_refs: set[str] = set()
    decoded_apps: list[dict[str, object]] = []
    window_count = 0
    required_app_keys = {"app_ref", "name", "bundle_id", "app_version", "windows"}
    required_window_keys = {"window_ref", "title", "bounds", "bindable", "binding_status"}
    optional_window_keys = {"document_path", "window_identity_ref"}

    for application in value:
        if not isinstance(application, Mapping) or set(application) != required_app_keys:
            raise ValueError("malformed catalog application")
        app_ref = application["app_ref"]
        if (
            not _valid_catalog_string(app_ref, _MAX_CATALOG_REFERENCE_SCALARS, nonempty=True)
            or app_ref in app_refs
        ):
            raise ValueError("malformed catalog application reference")
        app_refs.add(app_ref)
        if not _valid_catalog_string(application["name"], _MAX_CATALOG_STRING_SCALARS):
            raise ValueError("malformed catalog application name")
        for key in ("bundle_id", "app_version"):
            text = application[key]
            if text is not None and not _valid_catalog_string(text, _MAX_CATALOG_STRING_SCALARS):
                raise ValueError("malformed catalog application metadata")

        windows = application["windows"]
        if not isinstance(windows, list):
            raise TypeError("malformed catalog windows")
        window_count += len(windows)
        if window_count > _MAX_CATALOG_WINDOWS:
            raise ValueError("too many catalog windows")

        decoded_windows: list[dict[str, object]] = []
        for window in windows:
            if (
                not isinstance(window, Mapping)
                or not required_window_keys.issubset(window)
                or not set(window).issubset(required_window_keys | optional_window_keys)
            ):
                raise ValueError("malformed catalog window")
            window_ref = window["window_ref"]
            if (
                not _valid_catalog_string(window_ref, _MAX_CATALOG_REFERENCE_SCALARS, nonempty=True)
                or window_ref in window_refs
            ):
                raise ValueError("malformed catalog window reference")
            window_refs.add(window_ref)
            if not _valid_catalog_string(window["title"], _MAX_CATALOG_STRING_SCALARS):
                raise ValueError("malformed catalog window title")
            document_path_present = "document_path" in window
            document_path = window.get("document_path")
            if document_path_present and not _valid_catalog_string(
                document_path,
                _MAX_CATALOG_STRING_SCALARS,
                nonempty=True,
            ):
                raise ValueError("malformed catalog document path")

            bindable = window["bindable"]
            binding_status = window["binding_status"]
            identity_ref_present = "window_identity_ref" in window
            identity_ref = window.get("window_identity_ref")
            if not isinstance(bindable, bool) or binding_status not in _CATALOG_BINDING_STATUSES:
                raise ValueError("malformed catalog binding status")
            if binding_status == "ready":
                if (
                    bindable is not True
                    or not identity_ref_present
                    or not _valid_catalog_string(
                        identity_ref,
                        _MAX_CATALOG_REFERENCE_SCALARS,
                        nonempty=True,
                    )
                    or identity_ref in identity_refs
                ):
                    raise ValueError("malformed ready catalog window")
                identity_refs.add(identity_ref)
            elif bindable is not False or identity_ref_present:
                raise ValueError("malformed unbindable catalog window")

            decoded_window: dict[str, object] = {
                "window_ref": window_ref,
                "title": window["title"],
                "bounds": _decode_catalog_bounds(window["bounds"]),
                "bindable": bindable,
                "binding_status": binding_status,
            }
            if document_path is not None:
                decoded_window["document_path"] = document_path
            if identity_ref is not None:
                decoded_window["window_identity_ref"] = identity_ref
            decoded_windows.append(decoded_window)

        decoded_apps.append({
            "app_ref": app_ref,
            "name": application["name"],
            "bundle_id": application["bundle_id"],
            "app_version": application["app_version"],
            "windows": decoded_windows,
        })
    return tuple(decoded_apps)


def resolve_macos_helper(
    *,
    project_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the validated helper executable without executing it.

    Development overrides must name an absolute, directly owned executable.
    The bundled location is fixed beneath the resolved project root, avoiding
    PATH lookups and shell expansion.
    """
    environment = os.environ if environ is None else environ
    override = str(environment.get("ASTRA_COMPUTER_HELPER_PATH", "")).strip()
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            raise HelperTransportError("helper path must be absolute")
        return _validate_helper(candidate)

    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[2]
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise HelperTransportError("project root is unavailable") from exc
    candidate = root / _BUNDLED_HELPER
    if not candidate.resolve(strict=False).is_relative_to(root):
        raise HelperTransportError("bundled helper escaped project root")
    return _validate_helper(candidate)


def _validate_helper(candidate: Path) -> Path:
    if os.name != "posix":
        raise HelperTransportError("native helper ownership requires POSIX")
    if candidate.is_symlink():
        raise HelperTransportError("helper path must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        if resolved != candidate:
            raise HelperTransportError("helper path must not contain a symlink")
        metadata = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise HelperTransportError("helper executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise HelperTransportError("helper path is not a regular executable")
    if metadata.st_uid != os.getuid():
        raise HelperTransportError("helper executable has unexpected ownership")
    if not metadata.st_mode & stat.S_IXUSR:
        raise HelperTransportError("helper executable is not executable by its owner")
    try:
        return resolved
    except OSError as exc:
        raise HelperTransportError("helper executable is unavailable") from exc


class HelperTransport:
    """A single owned helper process with serialized request/response framing."""

    def __init__(
        self,
        helper: Path | str | None = None,
        *,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._helper = resolve_macos_helper() if helper is None else _validate_helper(Path(helper))
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = float(timeout)
        self._proc: asyncio.subprocess.Process | None = None
        self._pgid: int | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr = bytearray()
        self._lock = asyncio.Lock()
        self._closed = False
        self._request_cleanup_failed = False
        self._close_task: asyncio.Task[None] | None = None
        self._artifact_directory_fd: int | None = None
        self._artifact_directory_path: str | None = None

    @property
    def stderr(self) -> str:
        return bytes(self._stderr).decode("utf-8", errors="replace")

    async def request(self, request: ComputerRequest) -> ComputerResponse:
        if not isinstance(request, ComputerRequest):
            raise TypeError("request must be a ComputerRequest")
        if self._closed or self._request_cleanup_failed or (self._close_task is not None and not self._close_task.done()):
            raise HelperTransportError("helper transport is closing or closed")
        loop = asyncio.get_running_loop()
        started = loop.time()
        owned = False
        stage = "queue"
        stage_started = started
        timings = {name: 0.0 for name in ("queue", "start", "write", "read")}
        outcome = "error"
        request_finished: float | None = None
        try:
            # One absolute budget includes queueing and backpressure, not only
            # the response read. Ownership starts only after acquire succeeds.
            async with asyncio.timeout_at(started + self._timeout):
                await self._lock.acquire()
                owned = True
                timings[stage] = (loop.time() - stage_started) * 1000
                if self._closed or self._request_cleanup_failed or (self._close_task is not None and not self._close_task.done()):
                    raise HelperTransportError("helper transport is closing or closed")
                stage, stage_started = "start", loop.time()
                process = await self._ensure_process_locked()
                timings[stage] = (loop.time() - stage_started) * 1000
                stage, stage_started = "write", loop.time()
                await self._write_locked(process, request)
                timings[stage] = (loop.time() - stage_started) * 1000
                stage, stage_started = "read", loop.time()
                response = await self._read_response_locked(process)
                if response.request_id != request.request_id:
                    raise HelperTransportError("helper response request_id did not match request")
                outcome = "ok"
                return response
        except asyncio.CancelledError:
            outcome = "cancelled"
            request_finished = loop.time()
            if owned:
                try:
                    await self._cleanup_failed_request_locked()
                except Exception as exc:  # noqa: BLE001 - preserve cancellation after poisoned cleanup
                    # Cleanup already poisoned reuse. Preserve the caller's
                    # cancellation even if teardown itself failed or timed out.
                    logger.warning(
                        "computer_helper_cancel_cleanup_failed error_type=%s",
                        type(exc).__name__,
                    )
            raise
        except (TimeoutError, OSError, ValueError, HelperTransportError) as exc:
            outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
            request_finished = loop.time()
            if owned:
                await self._cleanup_failed_request_locked()
            if isinstance(exc, HelperTransportError):
                raise
            raise HelperTransportError("helper request failed") from exc
        finally:
            timings[stage] = ((request_finished or loop.time()) - stage_started) * 1000
            if owned:
                self._lock.release()
            logger.debug(
                "computer_helper_request operation=%s outcome=%s queue_ms=%.1f "
                "start_ms=%.1f write_ms=%.1f read_ms=%.1f elapsed_ms=%.1f action_count=%d",
                request.operation, outcome, timings["queue"], timings["start"],
                timings["write"], timings["read"], (loop.time() - started) * 1000,
                len(request.payload.get("actions", [])) if isinstance(request.payload.get("actions"), list) else 0,
            )

    async def _cleanup_failed_request_locked(self) -> None:
        # Keep framing ownership until the old process is gone. Repeated caller
        # cancellation must not expose its unread reply to a subsequent request.
        cleanup = asyncio.create_task(asyncio.wait_for(
            self._stop_process_locked(), timeout=_REQUEST_CLEANUP_TIMEOUT
        ))
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.done():
                        raise
                    cancelled = True
        except TimeoutError as exc:
            self._request_cleanup_failed = True
            raise HelperTransportError("helper cleanup exceeded its budget") from exc
        except BaseException:
            self._request_cleanup_failed = True  # Uncertain cleanup forbids transport reuse.
            raise
        if cancelled:
            raise asyncio.CancelledError

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

    async def configure_artifact_directory(
        self,
        directory_fd: int,
        directory_path: Path | None = None,
    ) -> None:
        """Restart only before a read operation so the helper inherits a held fd."""
        if os.name != "posix":
            raise HelperTransportError("native helper artifact directories require POSIX")
        try:
            metadata = os.fstat(directory_fd)
        except OSError as exc:
            raise HelperTransportError("artifact directory is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise HelperTransportError("artifact directory is unsafe")
        resolved_path: str | None = None
        if directory_path is not None:
            candidate = Path(directory_path)
            if not candidate.is_absolute() or candidate.is_symlink():
                raise HelperTransportError("artifact directory path is unsafe")
            try:
                resolved = candidate.resolve(strict=True)
                path_metadata = resolved.stat(follow_symlinks=False)
            except OSError as exc:
                raise HelperTransportError("artifact directory path is unavailable") from exc
            if (
                not stat.S_ISDIR(path_metadata.st_mode)
                or path_metadata.st_uid != os.getuid()
                or (path_metadata.st_dev, path_metadata.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise HelperTransportError("artifact directory path identity changed")
            resolved_path = str(resolved)
        async with self._lock:
            if (
                self._artifact_directory_fd == directory_fd
                and self._artifact_directory_path == resolved_path
            ):
                return
            if self._proc is not None:
                await self._stop_process_locked()
            self._artifact_directory_fd = directory_fd
            self._artifact_directory_path = resolved_path

    async def _close_owned(self) -> None:
        async with self._lock:
            await self._stop_process_locked()
            self._closed = True

    async def _ensure_process_locked(self) -> asyncio.subprocess.Process:
        process = self._proc
        if process is not None and process.returncode is None:
            return process
        if process is not None:
            await self._stop_process_locked()
        environment = {"PATH": os.defpath, "LANG": "en_US.UTF-8"}
        pass_fds: tuple[int, ...] = ()
        if self._artifact_directory_fd is not None:
            environment["ASTRA_COMPUTER_ARTIFACT_DIR_FD"] = str(self._artifact_directory_fd)
            if self._artifact_directory_path is not None:
                environment["ASTRA_COMPUTER_ARTIFACT_DIR_PATH"] = self._artifact_directory_path
            pass_fds = (self._artifact_directory_fd,)
        self._proc = await asyncio.create_subprocess_exec(
            str(self._helper),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            pass_fds=pass_fds,
            start_new_session=True,
            limit=MAX_RESPONSE_BYTES + 1,
        )
        self._stderr.clear()
        process = self._proc
        pid = getattr(process, "pid", None)
        self._pgid = pid if isinstance(pid, int) and pid > 0 else None
        if process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        return process

    async def _write_locked(self, process: asyncio.subprocess.Process, request: ComputerRequest) -> None:
        if process.returncode is not None or process.stdin is None:
            raise HelperTransportError("helper exited before receiving request")
        process.stdin.write((encode_request(request) + "\n").encode("utf-8"))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise HelperTransportError("helper exited while receiving request") from exc

    async def _read_response_locked(self, process: asyncio.subprocess.Process) -> ComputerResponse:
        if process.stdout is None:
            raise HelperTransportError("helper stdout is unavailable")
        try:
            raw = await process.stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            raise HelperTransportError("helper exited before returning a complete response") from exc
        except asyncio.LimitOverrunError as exc:
            raise HelperTransportError("helper response exceeded the maximum size") from exc
        if len(raw) > MAX_RESPONSE_BYTES + 1:
            raise HelperTransportError("helper response exceeded the maximum size")
        try:
            return decode_response(raw[:-1])
        except ValueError as exc:
            raise HelperTransportError("helper returned a malformed response") from exc

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        try:
            while chunk := await stream.read(4096):
                remaining = _MAX_STDERR_BYTES - len(self._stderr)
                if remaining > 0:
                    self._stderr.extend(chunk[:remaining])
        except (OSError, RuntimeError):
            return

    async def _stop_process_locked(self) -> None:
        process = self._proc
        pgid = self._pgid
        if pgid is not None:
            if os.name != "posix":
                raise HelperTransportError("native helper process groups require POSIX")
            self._signal_group(pgid, signal.SIGTERM)
            if not await self._wait_for_group_exit(pgid):
                self._signal_group(pgid, signal.SIGKILL)
                if not await self._wait_for_group_exit(pgid):
                    raise HelperTransportError("owned helper process group did not exit")
            if process is not None and process.returncode is None:
                with suppress(TimeoutError, ProcessLookupError, OSError):
                    await asyncio.wait_for(process.wait(), timeout=2)
        elif process is not None and process.returncode is None:
            with suppress(ProcessLookupError, OSError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except (TimeoutError, ProcessLookupError, OSError):
                with suppress(ProcessLookupError, OSError):
                    process.kill()
                with suppress(TimeoutError, ProcessLookupError, OSError):
                    await asyncio.wait_for(process.wait(), timeout=2)
        task = self._stderr_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, OSError, RuntimeError):
                await task
        self._proc = None
        self._pgid = None
        self._stderr_task = None

    @staticmethod
    def _signal_group(pgid: int, signal_number: int) -> None:
        if os.name != "posix":
            raise HelperTransportError("native helper process groups require POSIX")
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal_number)

    @staticmethod
    async def _wait_for_group_exit(pgid: int) -> bool:
        if os.name != "posix":
            raise HelperTransportError("native helper process groups require POSIX")
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                # Darwin can briefly report EPERM while an exiting group is
                # being reaped. It is still unconfirmed, so use the remaining
                # deadline and succeed only once the kernel reports ESRCH.
                pass
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)


class MacComputerBackend:
    """Map the platform-neutral backend contract to helper protocol messages."""

    def __init__(self, transport: _RequestTransport) -> None:
        self._transport = transport
        self._catalog: ComputerAppCatalog | None = None

    async def status(self) -> dict[str, object]:
        response = await self._request("status", {})
        return self._required_result(response)

    async def bind_artifact_directory(self, directory_fd: int) -> None:
        configure = getattr(self._transport, "configure_artifact_directory", None)
        if configure is None:
            raise HelperTransportError("helper transport cannot bind a session artifact directory")
        configured = configure(directory_fd)
        if inspect.isawaitable(configured):
            await configured

    async def bind_artifact_directory_path(self, directory_fd: int, directory_path: Path) -> None:
        configure = getattr(self._transport, "configure_artifact_directory", None)
        if configure is None:
            raise HelperTransportError("helper transport cannot bind a session artifact directory")
        configured = configure(directory_fd, directory_path)
        if inspect.isawaitable(configured):
            await configured

    async def apps(self) -> ComputerAppCatalog:
        response = await self._request("apps", {})
        result = self._required_result(response)
        generation = result.get("catalog_generation")
        apps = result.get("apps")
        if (
            set(result) not in (
                {"catalog_generation", "apps"},
                {"catalog_generation", "apps", "confirmed_absent_window_identity_refs"},
            )
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            raise HelperTransportError("helper returned malformed app catalog")
        try:
            absence = result.get("confirmed_absent_window_identity_refs", [])
            if not isinstance(absence, list):
                raise ValueError("malformed exact-window absence evidence")
            catalog = ComputerAppCatalog(
                generation, _decode_catalog_apps(apps), tuple(absence),
            )
        except (TypeError, ValueError) as exc:
            raise HelperTransportError("helper returned malformed app catalog") from exc
        self._catalog = catalog
        return catalog

    async def get_app_state(
        self,
        target: ComputerTarget,
        scope: str,
        artifact: ComputerArtifact,
        *,
        text_detail: ComputerSnapshotTextDetailMode = ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact: ComputerArtifact | None = None,
    ) -> ComputerBackendAppState:
        catalog = self._catalog
        if catalog is None:
            raise HelperTransportError("helper has no current app catalog")
        if artifact.directory_path is None:
            await self.bind_artifact_directory(artifact.directory_fd)
        else:
            await self.bind_artifact_directory_path(artifact.directory_fd, artifact.directory_path)
        if not isinstance(text_detail, ComputerSnapshotTextDetailMode):
            raise TypeError("text_detail must be a ComputerSnapshotTextDetailMode")
        payload: dict[str, object] = {
            "app_ref": target.app_ref,
            "window_ref": target.window_ref,
            "catalog_generation": catalog.generation,
            "scope": scope,
            "artifact_name": artifact.filename,
        }
        if text_detail is ComputerSnapshotTextDetailMode.ON:
            if text_detail_artifact is None:
                raise ValueError("text detail on requires an artifact filename")
            if (
                text_detail_artifact.directory_fd != artifact.directory_fd
                or text_detail_artifact.directory_path != artifact.directory_path
            ):
                raise ValueError("snapshot artifacts must share one held directory")
            payload.update({
                "text_detail": text_detail.value,
                "text_detail_artifact_name": text_detail_artifact.filename,
            })
        elif text_detail_artifact is not None:
            raise ValueError("text detail artifact filename requires text detail on")
        response = await self._request("get_app_state", payload)
        self._raise_response_error(response)
        if response.result is None or response.snapshot is None:
            raise HelperTransportError("helper returned incomplete app state")
        result = dict(response.result)
        if (
            set(result) != {"app_ref", "window_ref", "catalog_generation", "interaction_mode"}
            or result.get("app_ref") != target.app_ref
            or result.get("window_ref") != target.window_ref
            or result.get("catalog_generation") != catalog.generation
            or isinstance(result.get("catalog_generation"), bool)
            or not isinstance(result.get("catalog_generation"), int)
            or result.get("interaction_mode") != ComputerInteractionMode.BACKGROUND.value
        ):
            raise HelperTransportError("helper returned malformed app state")
        try:
            returned_target = ComputerTarget(result["app_ref"], result["window_ref"])
            validate_snapshot_payload(
                response.snapshot.payload,
                snapshot_id=response.snapshot.snapshot_id,
            )
        except (TypeError, ValueError) as exc:
            raise HelperTransportError("helper returned malformed app state") from exc
        if response.snapshot.payload.get("image_artifact") != artifact.filename:
            raise HelperTransportError("helper returned malformed app state")
        returned_detail = response.snapshot.payload.get("text_detail_artifact")
        returned_metadata = response.snapshot.payload.get("text_detail_metadata")
        if text_detail is ComputerSnapshotTextDetailMode.ON:
            if (
                text_detail_artifact is None
                or returned_detail != text_detail_artifact.filename
                or not isinstance(returned_metadata, Mapping)
            ):
                raise HelperTransportError("helper returned malformed app state")
        elif returned_detail is not None or returned_metadata is not None:
            raise HelperTransportError("helper returned malformed app state")
        return ComputerBackendAppState(catalog.generation, returned_target, response.snapshot)

    async def select(self, app_ref: str, window_ref: str) -> ComputerTarget:
        response = await self._request("select", {"app_ref": app_ref, "window_ref": window_ref})
        result = self._required_result(response)
        if set(result) != {"app_ref", "window_ref", "interaction_mode"}:
            raise HelperTransportError("helper returned malformed target")
        selected_app = result.get("app_ref", app_ref)
        selected_window = result.get("window_ref", window_ref)
        if (
            not isinstance(selected_app, str)
            or not isinstance(selected_window, str)
            or result.get("interaction_mode") != ComputerInteractionMode.BACKGROUND.value
        ):
            raise HelperTransportError("helper returned malformed target")
        try:
            return ComputerTarget(selected_app, selected_window)
        except ValueError as exc:
            raise HelperTransportError("helper returned malformed target") from exc

    async def plan_actions(
        self,
        target: ComputerTarget,
        snapshot_id: str,
        actions: list[ComputerAction | Mapping[str, object]],
        interaction_mode: ComputerInteractionMode,
        *,
        fragment: ForegroundFragmentPlanRequest | None = None,
    ) -> ComputerActionPlan:
        serialized = [self._serialize_action(action) for action in actions]
        self._validate_planned_delay(serialized)
        payload: dict[str, object] = {
            "interaction_mode": interaction_mode.value,
            "snapshot_id": snapshot_id,
            "actions": serialized,
        }
        if fragment is not None:
            payload["fragment"] = fragment
        response = await self._request(
            "plan_actions",
            payload,
        )
        if (
            interaction_mode is ComputerInteractionMode.BACKGROUND
            and not response.ok
            and response.error is not None
            and response.error.code is ComputerErrorCode.PROTOCOL_MISMATCH
            and response.error.message == "action payload is invalid"
            and any(
                action.get("type") in {"click", "right_click", "double_click", "scroll", "drag"}
                and action.get("x") is not None
                and action.get("y") is not None
                and not action.get("target_element_ref")
                for action in serialized
            )
        ):
            # The native planner requires a verified element region for
            # background coordinates. This is a mode refusal, not wire drift.
            # Keep planning/approval explicit: never replay or switch modes here.
            raise HelperApplicationError(ComputerError(
                ComputerErrorCode.BACKGROUND_ACTION_UNSUPPORTED,
                "Background coordinate input requires target_element_ref from the current snapshot; "
                "unanchored visual coordinates require explicit foreground_takeover planning.",
            ))
        if (
            not response.ok
            and response.error is not None
            and (
                response.error.code is not ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED
                or interaction_mode is not ComputerInteractionMode.BACKGROUND
            )
        ):
            # Surface the native error instead of attempting to parse the
            # error-shaped result as a plan summary.
            raise HelperApplicationError(response.error, result=response.result)
        try:
            raw_summary = dict(response.result or {})
            if fragment is not None:
                expected = fragment.authority
                if (
                    raw_summary.pop("fragment_hash", None) != expected.fragment_hash
                    or raw_summary.pop("stage_index", None) != expected.stage_index
                    or raw_summary.pop("stage_hash", None) != expected.stage_hash
                ):
                    raise ValueError("fragment plan response binding mismatch")
            summary = DispatchPlanSummary.from_mapping(raw_summary)
        except (TypeError, ValueError) as exc:
            raise HelperTransportError("helper returned malformed action plan") from exc
        if summary.interaction_mode is not interaction_mode:
            raise HelperTransportError("helper returned action plan for the wrong mode")
        if not response.ok:
            if (
                response.error is None
                or response.error.code is not ComputerErrorCode.FOREGROUND_TAKEOVER_REQUIRED
                or interaction_mode is not ComputerInteractionMode.BACKGROUND
                or not summary.requires_takeover
                or summary.last_acknowledged_action != -1
            ):
                if response.error is None:
                    raise HelperTransportError("helper rejected action plan without an error")
                raise HelperApplicationError(response.error, result=response.result)
        elif response.error is not None:
            raise HelperApplicationError(response.error, result=response.result)
        return ComputerActionPlan(
            plan_ref=summary.plan_ref,
            interaction_mode=summary.interaction_mode,
            requires_takeover=summary.requires_takeover,
            reason=summary.reason,
            action_classes=summary.action_classes,
            pid_action_classes=summary.pid_action_classes,
        )

    async def begin_takeover(
        self,
        snapshot_id: str,
        plan_ref: str,
        *,
        declaration: ForegroundFragmentDeclaration | None = None,
    ) -> str:
        payload: dict[str, object] = {"snapshot_id": snapshot_id, "plan_ref": plan_ref}
        if declaration is not None:
            payload["fragment"] = declaration
        response = await self._request(
            "takeover_begin",
            payload,
        )
        result = self._required_result(response)
        expected = {"takeover_ref"}
        if declaration is not None:
            expected.update({"fragment_hash", "stage_index", "stage_hash"})
        if set(result) != expected or not isinstance(result.get("takeover_ref"), str):
            raise HelperTransportError("helper returned malformed takeover reference")
        if declaration is not None:
            first = declaration.stages[0]
            if (
                result.get("fragment_hash") != declaration.fragment_hash
                or result.get("stage_index") != 0
                or result.get("stage_hash") != first.stage_hash
            ):
                raise HelperTransportError("helper returned malformed takeover reference")
        takeover_ref = result["takeover_ref"]
        if not isinstance(takeover_ref, str) or not takeover_ref:
            raise HelperTransportError("helper returned malformed takeover reference")
        return takeover_ref

    async def commit_fragment_stage(self, commit: FragmentStageCommit) -> FragmentStageCommitResult:
        response = await self._request("fragment_stage_commit", commit.to_mapping())
        result = self._required_result(response)
        terminal = result.get("terminal")
        expected_keys = {"fragment_hash", "stage_index", "stage_hash", "terminal"}
        if terminal is True:
            expected_keys.add("restoration")
        if (
            set(result) != expected_keys
            or result.get("fragment_hash") != commit.fragment_hash
            or result.get("stage_index") != commit.stage_index
            or result.get("stage_hash") != commit.stage_hash
            or not isinstance(terminal, bool)
        ):
            raise HelperTransportError("helper returned malformed fragment commit")
        try:
            return FragmentStageCommitResult.from_mapping({
                key: value for key, value in result.items()
                if key in {"terminal", "restoration"}
            })
        except (TypeError, ValueError) as exc:
            raise HelperTransportError("helper returned malformed fragment commit") from exc

    async def end_takeover(
        self, takeover_ref: str, *, restore_previous_focus: bool = True,
    ) -> Mapping[str, object]:
        if not isinstance(restore_previous_focus, bool):
            raise TypeError("restore_previous_focus must be boolean")
        payload: dict[str, object] = {"takeover_ref": takeover_ref}
        if not restore_previous_focus:
            payload["restore_previous_focus"] = False
        response = await self._request("takeover_end", payload)
        try:
            outcome = TakeoverOutcome.from_mapping(self._required_result(response))
        except (TypeError, ValueError) as exc:
            raise HelperTransportError("helper returned malformed takeover outcome") from exc
        if not outcome.started:
            raise HelperTransportError("helper returned malformed takeover outcome")
        return outcome.to_mapping()

    async def snapshot(
        self,
        target: ComputerTarget,
        scope: str,
        artifact: ComputerArtifact,
        *,
        text_detail: ComputerSnapshotTextDetailMode = ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact: ComputerArtifact | None = None,
        subtree: tuple[str, str] | None = None,
    ) -> ComputerSnapshot:
        if artifact.directory_path is None:
            await self.bind_artifact_directory(artifact.directory_fd)
        else:
            await self.bind_artifact_directory_path(artifact.directory_fd, artifact.directory_path)
        if not isinstance(text_detail, ComputerSnapshotTextDetailMode):
            raise TypeError("text_detail must be a ComputerSnapshotTextDetailMode")
        payload: dict[str, object] = {
            "app_ref": target.app_ref,
            "window_ref": target.window_ref,
            "scope": scope,
            "artifact_name": artifact.filename,
        }
        if text_detail is ComputerSnapshotTextDetailMode.ON:
            if text_detail_artifact is None:
                raise ValueError("text detail on requires an artifact filename")
            if (
                text_detail_artifact.directory_fd != artifact.directory_fd
                or text_detail_artifact.directory_path != artifact.directory_path
            ):
                raise ValueError("snapshot artifacts must share one held directory")
            payload.update({
                "text_detail": text_detail.value,
                "text_detail_artifact_name": text_detail_artifact.filename,
            })
        elif text_detail_artifact is not None:
            raise ValueError("text detail artifact filename requires text detail on")
        if subtree is not None:
            payload.update({"snapshot_id": subtree[0], "subtree_ref": subtree[1]})
        response = await self._request("snapshot_subtree" if subtree is not None else "snapshot", payload)
        self._raise_response_error(response)
        if response.snapshot is None:
            raise HelperTransportError("helper returned no snapshot")
        try:
            validate_snapshot_payload(
                response.snapshot.payload,
                snapshot_id=response.snapshot.snapshot_id,
            )
        except (TypeError, ValueError) as exc:
            raise HelperTransportError("helper returned malformed snapshot text detail") from exc
        image_artifact = response.snapshot.payload.get("image_artifact")
        if image_artifact != artifact.filename:
            raise HelperTransportError("helper returned an unexpected snapshot artifact")
        returned_detail = response.snapshot.payload.get("text_detail_artifact")
        returned_metadata = response.snapshot.payload.get("text_detail_metadata")
        if text_detail is ComputerSnapshotTextDetailMode.ON:
            if (
                text_detail_artifact is None
                or returned_detail != text_detail_artifact.filename
                or not isinstance(returned_metadata, Mapping)
            ):
                raise HelperTransportError("helper omitted or changed snapshot text detail")
        elif returned_detail is not None or returned_metadata is not None:
            raise HelperTransportError("helper returned unexpected snapshot text detail")
        return response.snapshot

    async def act(
        self,
        target: ComputerTarget,
        snapshot_id: str,
        actions: list[ComputerAction | Mapping[str, object]],
        *,
        interaction_mode: ComputerInteractionMode,
        plan_ref: str,
        takeover_ref: str | None = None,
        fragment_stage: FragmentStageAuthority | None = None,
    ) -> ComputerActionResult:
        try:
            serialized = [self._serialize_action(action) for action in actions]
            try:
                self._validate_planned_delay(serialized)
            except HelperApplicationError as exc:
                return ComputerActionResult(error=exc.error)
            payload: dict[str, object] = {
                "interaction_mode": interaction_mode.value,
                "snapshot_id": snapshot_id,
                "plan_ref": plan_ref,
                "actions": serialized,
            }
            if interaction_mode is ComputerInteractionMode.FOREGROUND_TAKEOVER:
                if not isinstance(takeover_ref, str) or not takeover_ref:
                    raise ValueError("foreground takeover requires takeover_ref")
                payload["takeover_ref"] = takeover_ref
                if fragment_stage is not None:
                    payload.update({
                        "fragment_hash": fragment_stage.fragment_hash,
                        "stage_index": fragment_stage.stage_index,
                        "stage_hash": fragment_stage.stage_hash,
                    })
            elif takeover_ref is not None or fragment_stage is not None:
                raise ValueError("background act cannot carry takeover authority")
            response = await self._request(
                "act",
                payload,
            )
        except asyncio.CancelledError:
            # Do not fabricate success or issue a second write.  The manager
            # has already consumed the snapshot before this cancellation.
            raise
        except (HelperTransportError, OSError, ValueError):
            return ComputerActionResult(
                error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "action outcome is unknown"),
            )
        if not response.ok and response.error is None:
            return self._unknown_action_result(response.result, len(serialized))
        try:
            if response.snapshot is not None:
                raise ValueError("act response must not contain a snapshot")
            raw_result = dict(response.result or {})
            if fragment_stage is not None and (
                raw_result.pop("fragment_hash", None) != fragment_stage.fragment_hash
                or raw_result.pop("stage_index", None) != fragment_stage.stage_index
                or raw_result.pop("stage_hash", None) != fragment_stage.stage_hash
            ):
                raise ValueError("fragment act response binding mismatch")
            action_result = ComputerActResult.from_mapping(
                raw_result,
                action_count=len(serialized),
                response_ok=response.ok,
                response_error=response.error,
                interaction_mode=interaction_mode,
            )
        except ValueError:
            return self._unknown_action_result(response.result, len(serialized))
        return ComputerActionResult(result=action_result.to_mapping(), error=response.error)

    @staticmethod
    def _unknown_action_result(
        raw_result: Mapping[str, object] | None,
        action_count: int,
    ) -> ComputerActionResult:
        """Return a non-replayable outcome with only a proven success prefix.

        Once an ``act`` request was dispatched, framing or semantic corruption
        cannot safely be described as a protocol-only rejection: input may
        already have happened. An acknowledgement is retained only when both
        the bounded integer and every corresponding outcome independently form
        an exact, contiguous, successful prefix.
        """
        acknowledged = -1
        outcomes: list[dict[str, object]] = []
        if isinstance(raw_result, Mapping):
            raw_ack = raw_result.get("last_acknowledged_action")
            raw_outcomes = raw_result.get("outcomes")
            if (
                isinstance(raw_ack, int)
                and not isinstance(raw_ack, bool)
                and -1 <= raw_ack < action_count
                and isinstance(raw_outcomes, list)
            ):
                for expected_index in range(raw_ack + 1):
                    if expected_index >= len(raw_outcomes):
                        break
                    raw = raw_outcomes[expected_index]
                    if (
                        not isinstance(raw, Mapping)
                        or set(raw) != {"index", "ok"}
                        or raw.get("index") != expected_index
                        or isinstance(raw.get("index"), bool)
                        or raw.get("ok") is not True
                    ):
                        break
                    outcomes.append({"index": expected_index, "ok": True})
                    acknowledged = expected_index
        return ComputerActionResult(
            result={
                "outcomes": outcomes,
                "last_acknowledged_action": acknowledged,
            },
            error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "action outcome is unknown"),
        )

    async def close(self) -> None:
        await self._transport.close()

    async def _request(self, operation: str, payload: Mapping[str, object]) -> ComputerResponse:
        request = ComputerRequest(uuid.uuid4().hex, operation, payload)
        response = await self._transport.request(request)
        if not isinstance(response, ComputerResponse):
            raise HelperTransportError("helper returned a malformed response")
        if response.request_id != request.request_id:
            raise HelperTransportError("helper response request_id did not match request")
        return response

    @staticmethod
    def _validate_planned_delay(actions: list[dict[str, object]]) -> None:
        # Same bound as native planning: retain 2s native work margin under its
        # 12s batch cap, then 3s transport headroom. Reject before any dispatch.
        delay = 0
        for action in actions:
            if action.get("type") not in {"wait", "drag"}:
                continue
            duration = action.get("duration_ms", 300 if action.get("type") == "drag" else 0)
            if isinstance(duration, bool) or not isinstance(duration, int):
                raise ValueError("planned duration_ms must be an integer")
            delay += duration
        if delay > _MAX_PLANNED_DELAY_MS:
            raise HelperApplicationError(ComputerError(
                ComputerErrorCode.ACTION_TIMEOUT,
                "planned waits and drags exceed the 10-second batch budget",
            ))

    @staticmethod
    def _serialize_action(action: ComputerAction | Mapping[str, object]) -> dict[str, object]:
        if isinstance(action, ComputerAction):
            return action.to_mapping()
        if not isinstance(action, Mapping):
            raise TypeError("actions must contain ComputerAction objects or mappings")
        return ComputerAction.from_mapping(action).to_mapping()

    @staticmethod
    def _raise_response_error(response: ComputerResponse) -> None:
        if response.error is not None:
            raise HelperApplicationError(response.error, result=response.result)
        if not response.ok:
            raise HelperTransportError("helper rejected request")

    def _required_result(self, response: ComputerResponse) -> dict[str, object]:
        self._raise_response_error(response)
        if response.result is None:
            raise HelperTransportError("helper returned no result")
        return dict(response.result)
