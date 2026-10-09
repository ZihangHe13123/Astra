"""Durable lifecycle manager for long-running sandbox executions."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from ..metrics import runtime_metrics
from ..process_env import hidden_process_creationflags, pid_alive


OutputCallback = Callable[[str, str], None]
ProcessFactory = Callable[[OutputCallback], Coroutine[Any, Any, dict]]
ProcessEventCallback = Callable[[dict[str, Any]], None]


@dataclass
class ManagedProcess:
    process_id: str
    kind: str
    label: str
    task: asyncio.Task | None
    started_at: float
    manifest_path: Path
    output_path: Path
    stdout_path: Path
    stderr_path: Path
    task_id: str = ""
    result: dict | None = None
    completed_at: float | None = None
    cancelled: bool = False
    read_offset: int = 0
    read_offsets: dict[str, int] = field(default_factory=dict)
    read_byte_offsets: dict[str, int] = field(default_factory=dict)
    visible: bool = False
    recovered_status: str = ""
    output_chars: int = 0
    stdout_chars: int = 0
    stderr_chars: int = 0
    last_manifest_at: float = 0.0
    last_output_event_at: float = 0.0
    output_event_recorded: bool = False
    terminal_emitted: bool = False
    external: bool = False
    supervisor_pid: int | None = None
    spec_path: Path | None = None
    cancel_path: Path | None = None
    # Small, JSON-serializable runtime metadata. Callers must not place
    # commands, prompts, credentials, or other sensitive payloads here.
    metadata: dict[str, Any] = field(default_factory=dict)


class ProcessManager:
    def __init__(
        self,
        *,
        artifact_dir: str | Path = ".astra/processes",
        max_processes: int = 32,
        task_store: Any = None,
        on_event: ProcessEventCallback | None = None,
    ):
        self.max_processes = max(1, max_processes)
        self.artifact_dir = Path(artifact_dir).expanduser().resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.task_store = task_store
        self.on_event = on_event
        self._processes: dict[str, ManagedProcess] = {}
        self._load_manifests()

    def _paths(self, process_id: str) -> tuple[Path, Path, Path, Path]:
        return (
            self.artifact_dir / f"{process_id}.json",
            self.artifact_dir / f"{process_id}.log",
            self.artifact_dir / f"{process_id}.stdout.log",
            self.artifact_dir / f"{process_id}.stderr.log",
        )

    def _control_paths(self, process_id: str) -> tuple[Path, Path]:
        return (
            self.artifact_dir / f"{process_id}.spec.json",
            self.artifact_dir / f"{process_id}.cancel",
        )

    def _marker_paths(self, process_id: str) -> tuple[Path, Path]:
        return (
            self.artifact_dir / f"{process_id}.recoverable",
            self.artifact_dir / f"{process_id}.visible",
        )

    def _supervisor_log_path(self, process_id: str) -> Path:
        return self.artifact_dir / f"{process_id}.supervisor.log"

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if not pid or pid <= 0:
            return False
        return pid_alive(pid)

    def _load_manifests(self) -> None:
        for manifest_path in sorted(self.artifact_dir.glob("*.json")):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                process_id = str(payload["process_id"])
                _, output_path, stdout_path, stderr_path = self._paths(process_id)
                spec_path, cancel_path = self._control_paths(process_id)
                recoverable_path, visible_path = self._marker_paths(process_id)
                external = payload.get("execution_owner") == "supervisor"
                recoverable = recoverable_path.exists()
                visible = bool(payload.get("visible")) or visible_path.exists()
                if not visible and not (external and recoverable):
                    continue
                if recoverable and not visible:
                    visible_path.touch()
                    recoverable_path.unlink(missing_ok=True)
                status = str(payload.get("status") or "interrupted")
                supervisor_pid = int(payload.get("supervisor_pid") or 0) or None
                if status in {"starting", "running"} and not (
                    external and self._pid_alive(supervisor_pid)
                ):
                    status = "interrupted"
                    payload["status"] = status
                    payload["completed_at"] = time.time()
                    self._atomic_json_write(manifest_path, payload)
                process = ManagedProcess(
                    process_id=process_id,
                    kind=str(payload.get("kind") or "process"),
                    label=str(payload.get("label") or "recovered process"),
                    task=None,
                    started_at=float(payload.get("started_at") or time.time()),
                    manifest_path=manifest_path,
                    output_path=output_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    task_id=str(payload.get("task_id") or ""),
                    result=payload.get("result") if isinstance(payload.get("result"), dict) else None,
                    completed_at=float(payload["completed_at"]) if payload.get("completed_at") else None,
                    cancelled=status == "cancelled",
                    # A recoverable marker without a visible marker means the
                    # previous backend disappeared before it could return the
                    # foreground result. Surface it after restart.
                    visible=True,
                    recovered_status=status,
                    output_chars=int(payload.get("output_chars") or self._char_count(output_path)),
                    stdout_chars=int(payload.get("stdout_chars") or self._char_count(stdout_path)),
                    stderr_chars=int(payload.get("stderr_chars") or self._char_count(stderr_path)),
                    external=external,
                    supervisor_pid=supervisor_pid,
                    spec_path=spec_path,
                    cancel_path=cancel_path,
                    metadata=(
                        dict(payload.get("metadata"))
                        if isinstance(payload.get("metadata"), dict)
                        else {}
                    ),
                )
                self._processes[process_id] = process
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        self._prune()

    @staticmethod
    def _char_count(path: Path) -> int:
        try:
            return len(path.read_text(encoding="utf-8"))
        except OSError:
            return 0

    @staticmethod
    def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(5):
                try:
                    os.replace(temp, path)
                    return
                except PermissionError:
                    if attempt >= 4:
                        raise
                    time.sleep(0.01 * (2**attempt))
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _manifest_payload(self, process: ManagedProcess) -> dict[str, Any]:
        status = (
            process.recovered_status or "starting"
            if process.external
            else self.status(process)
        )
        return {
            "version": 2 if process.external else 1,
            "process_id": process.process_id,
            "kind": process.kind,
            "label": process.label,
            "task_id": process.task_id,
            "status": status,
            "visible": process.visible,
            "started_at": process.started_at,
            "completed_at": process.completed_at,
            "output_path": str(process.output_path),
            "stdout_path": str(process.stdout_path),
            "stderr_path": str(process.stderr_path),
            "output_chars": process.output_chars,
            "stdout_chars": process.stdout_chars,
            "stderr_chars": process.stderr_chars,
            "result": process.result,
            "execution_owner": "supervisor" if process.external else "backend",
            "supervisor_pid": process.supervisor_pid,
            "cancel_path": str(process.cancel_path) if process.cancel_path else "",
            "metadata": process.metadata,
        }

    def _persist(self, process: ManagedProcess) -> None:
        self._atomic_json_write(process.manifest_path, self._manifest_payload(process))

    def _delete_transient_files(self, process: ManagedProcess) -> None:
        recoverable_path, visible_path = self._marker_paths(process.process_id)
        for path in (
            process.manifest_path,
            process.manifest_path.with_suffix(process.manifest_path.suffix + ".tmp"),
            process.output_path,
            process.stdout_path,
            process.stderr_path,
            process.spec_path,
            process.cancel_path,
            recoverable_path,
            visible_path,
            self._supervisor_log_path(process.process_id),
        ):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune(self, *, reserve: int = 0) -> None:
        target = max(0, self.max_processes - max(0, reserve))
        completed = sorted(
            (
                process
                for process in self._processes.values()
                if self.status(process) != "running"
            ),
            key=lambda process: process.completed_at or process.started_at,
        )
        while len(self._processes) > target and completed:
            process = completed.pop(0)
            self._processes.pop(process.process_id, None)
            self._delete_transient_files(process)

    def _append_text(self, path: Path, text: str) -> None:
        if not text:
            return
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def _record_task_event(self, process: ManagedProcess, event_type: str, payload: dict) -> None:
        if not process.task_id or self.task_store is None:
            return
        try:
            self.task_store.append_event(
                process.task_id,
                f"process:{process.process_id}:{event_type}",
                event_type,
                payload,
            )
        except Exception:
            # Process execution must not fail because the optional task journal
            # is unavailable or the enclosing task already reached a terminal state.
            return

    def _emit(self, process: ManagedProcess, event_type: str, *, task_event: bool = True) -> None:
        if not process.visible:
            return
        payload = {"event": event_type, **self.describe(process)}
        if task_event:
            self._record_task_event(process, event_type, payload)
        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception:
                pass

    def _emit_terminal(self, process: ManagedProcess) -> None:
        if process.terminal_emitted:
            return
        status = self.status(process)
        if status == "running":
            return
        process.terminal_emitted = True
        runtime_metrics.increment("background_terminal_count")
        if status == "completed":
            runtime_metrics.increment("background_completed_count")
        self._emit(process, f"process_{status}")

    def _on_output(self, process: ManagedProcess, stream: str, text: str) -> None:
        if not text:
            return
        target = process.stderr_path if stream == "stderr" else process.stdout_path
        self._append_text(target, text)
        self._append_text(
            process.output_path,
            (f"[{stream}]\n" if stream == "stderr" else "") + text,
        )
        chars = len(text)
        process.output_chars += chars + (9 if stream == "stderr" else 0)
        if stream == "stderr":
            process.stderr_chars += chars
        else:
            process.stdout_chars += chars
        now = time.monotonic()
        if now - process.last_manifest_at >= 0.25:
            process.last_manifest_at = now
            self._persist(process)
        if process.visible and now - process.last_output_event_at >= 0.25:
            process.last_output_event_at = now
            self._emit(process, "process_output", task_event=not process.output_event_recorded)
            process.output_event_recorded = True

    def start(
        self,
        factory: ProcessFactory,
        *,
        kind: str,
        label: str,
        task_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ManagedProcess:
        metadata_payload = dict(metadata or {})
        try:
            json.dumps(metadata_payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("process metadata must be JSON serializable") from exc
        self._prune(reserve=1)
        live_count = sum(1 for process in self._processes.values() if self.status(process) == "running")
        if live_count >= self.max_processes:
            raise RuntimeError(f"background process limit reached ({self.max_processes})")

        process_id = uuid.uuid4().hex
        manifest_path, output_path, stdout_path, stderr_path = self._paths(process_id)
        process = ManagedProcess(
            process_id=process_id,
            kind=kind,
            label=label,
            task=None,
            started_at=time.time(),
            manifest_path=manifest_path,
            output_path=output_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            task_id=task_id,
            metadata=metadata_payload,
        )
        self._processes[process_id] = process
        self._persist(process)
        process.task = asyncio.create_task(
            factory(lambda stream, text: self._on_output(process, stream, text)),
            name=f"agent-process-{process_id}",
        )

        def completed(done: asyncio.Task) -> None:
            process.completed_at = time.time()
            if done.cancelled():
                process.cancelled = True
                process.result = {
                    "output": "",
                    "error": "[Cancelled] Process cancelled by user",
                    "exit_code": -1,
                }
            else:
                try:
                    process.result = done.result()
                except Exception as exc:
                    process.result = {
                        "output": "",
                        "error": f"[ExecutionFailed] {type(exc).__name__}: {exc}",
                        "exit_code": -1,
                    }
            if not process.visible and (process.result or {}).get("artifact_path"):
                # Truncated foreground results need their original streamed bytes,
                # not a reconstruction from the already truncated result text.
                self.expose(process)
            if process.visible:
                self._persist(process)
                self._emit_terminal(process)
            else:
                self._delete_transient_files(process)

        process.task.add_done_callback(completed)
        return process

    async def start_supervised(
        self,
        spec: dict[str, Any],
        *,
        kind: str,
        label: str,
        task_id: str = "",
    ) -> ManagedProcess:
        """Launch a detached execution owner that survives backend shutdown."""
        self._prune(reserve=1)
        live_count = sum(
            1 for process in self._processes.values()
            if self.status(process) == "running"
        )
        if live_count >= self.max_processes:
            raise RuntimeError(f"background process limit reached ({self.max_processes})")

        process_id = uuid.uuid4().hex
        manifest_path, output_path, stdout_path, stderr_path = self._paths(process_id)
        spec_path, cancel_path = self._control_paths(process_id)
        process = ManagedProcess(
            process_id=process_id,
            kind=kind,
            label=label,
            task=None,
            started_at=time.time(),
            manifest_path=manifest_path,
            output_path=output_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            task_id=task_id,
            recovered_status="starting",
            external=True,
            spec_path=spec_path,
            cancel_path=cancel_path,
        )
        self._processes[process_id] = process
        recoverable_path, _ = self._marker_paths(process_id)
        recoverable_path.touch()
        self._persist(process)

        runner_spec = {
            **spec,
            "kind": kind,
            "manifest_path": str(manifest_path),
            "output_path": str(output_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "cancel_path": str(cancel_path),
        }
        temporary = spec_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(runner_spec, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, spec_path)

        environment = os.environ.copy()
        project_root = str(Path(__file__).resolve().parents[3])
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            project_root
            if not existing_pythonpath
            else project_root + os.pathsep + existing_pythonpath
        )
        command = [
            sys.executable,
            "-m",
            "agent.runtime.process_supervisor",
            "--spec",
            str(spec_path),
        ]
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
            "env": environment,
        }
        if os.name == "nt":
            # CREATE_NO_WINDOW is ignored when combined with DETACHED_PROCESS.
            # A separate process group preserves control-event isolation while
            # keeping the supervisor console-free.
            kwargs["creationflags"] = hidden_process_creationflags(
                new_process_group=True,
            )
        else:
            kwargs["start_new_session"] = True
        supervisor_log_path = self._supervisor_log_path(process_id)
        try:
            with supervisor_log_path.open("ab") as supervisor_log:
                child = subprocess.Popen(
                    command,
                    stdout=supervisor_log,
                    stderr=supervisor_log,
                    **kwargs,
                )
        except Exception:
            self._processes.pop(process_id, None)
            self._delete_transient_files(process)
            raise
        process.supervisor_pid = child.pid
        # Wait for the detached owner to publish its PID/state. This closes
        # the small restart window where a new backend could see a starting
        # manifest without a live owner and incorrectly mark it interrupted.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            status = str(payload.get("status") or "")
            if payload.get("supervisor_pid") or status in {
                "completed", "failed", "cancelled",
            }:
                break
            if child.poll() is not None:
                break
            await asyncio.sleep(0.01)
        self._refresh_external(process)
        if process.recovered_status == "interrupted":
            diagnostic = self._read_text(supervisor_log_path).strip()
            self._delete_transient_files(process)
            self._processes.pop(process_id, None)
            detail = f": {diagnostic[-2000:]}" if diagnostic else ""
            raise RuntimeError(f"detached process supervisor failed to start{detail}")
        return process

    def expose(self, process: ManagedProcess) -> None:
        """Mark a yielded process as user-visible and durable."""
        if process.visible:
            return
        process.visible = True
        if process.external:
            recoverable_path, visible_path = self._marker_paths(process.process_id)
            visible_path.touch()
            recoverable_path.unlink(missing_ok=True)
        runtime_metrics.increment("background_started_count")
        if process.task is not None and process.task.done():
            self._sync_result(process)
            if not process.output_path.exists() and process.result:
                output = str(process.result.get("output") or "")
                error = str(process.result.get("error") or "")
                if output:
                    self._on_output(process, "stdout", output)
                if error:
                    self._on_output(process, "stderr", error)
        # The detached supervisor is the sole manifest writer after launch.
        # Visibility lives in a separate marker to avoid clobbering a
        # concurrent terminal-state update.
        if not process.external:
            self._persist(process)
        self._emit(process, "process_started")
        self.observe(process)

    def discard_unexposed(self, process: ManagedProcess) -> None:
        """Remove a supervised foreground process that returned before yield."""
        if process.visible:
            return
        self._processes.pop(process.process_id, None)
        self._delete_transient_files(process)

    def observe(self, process: ManagedProcess) -> None:
        """Publish detached output/terminal transitions seen by this backend."""
        if not process.external or not process.visible:
            self._emit_terminal(process)
            return
        self.status(process)
        if process.output_chars > 0 and not process.output_event_recorded:
            process.output_event_recorded = True
            self._emit(process, "process_output")
        self._emit_terminal(process)

    def get(self, process_id: str) -> ManagedProcess:
        process = self._processes.get(process_id)
        if process is None or not process.visible:
            raise ValueError(f"unknown process_id: {process_id}")
        return process

    async def wait(self, process: ManagedProcess, foreground_yield_ms: int) -> bool:
        if process.external:
            deadline = (
                None
                if foreground_yield_ms <= 0
                else time.monotonic() + foreground_yield_ms / 1000
            )
            while self.status(process) == "running":
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.05)
            return True
        if process.task is None:
            return True
        if foreground_yield_ms <= 0:
            await asyncio.shield(process.task)
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(process.task),
                timeout=foreground_yield_ms / 1000,
            )
            return True
        except asyncio.TimeoutError:
            return False

    def status(self, process: ManagedProcess) -> str:
        if process.external:
            self._refresh_external(process)
            return process.recovered_status or "interrupted"
        if process.task is None:
            return process.recovered_status or "interrupted"
        if not process.task.done():
            return "running"
        self._sync_result(process)
        if process.cancelled or process.task.cancelled():
            return "cancelled"
        result = process.result or {}
        return "failed" if result.get("error") or result.get("exit_code", 0) != 0 else "completed"

    @staticmethod
    def _sync_result(process: ManagedProcess) -> None:
        if process.task is None or not process.task.done() or process.result is not None:
            return
        process.completed_at = process.completed_at or time.time()
        if process.task.cancelled():
            process.cancelled = True
            process.result = {
                "output": "",
                "error": "[Cancelled] Process cancelled by user",
                "exit_code": -1,
            }
            return
        try:
            process.result = process.task.result()
        except Exception as exc:
            process.result = {
                "output": "",
                "error": f"[ExecutionFailed] {type(exc).__name__}: {exc}",
                "exit_code": -1,
            }

    def _refresh_external(self, process: ManagedProcess) -> None:
        try:
            payload = json.loads(process.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        status = str(payload.get("status") or process.recovered_status or "running")
        process.supervisor_pid = (
            int(payload.get("supervisor_pid") or process.supervisor_pid or 0)
            or None
        )
        # The detached supervisor publishes these counters while appending.
        # Trust them instead of re-reading every artifact on every status poll.
        process.output_chars = int(payload.get("output_chars") or process.output_chars)
        process.stdout_chars = int(payload.get("stdout_chars") or process.stdout_chars)
        process.stderr_chars = int(payload.get("stderr_chars") or process.stderr_chars)
        if isinstance(payload.get("result"), dict):
            process.result = payload["result"]
        if payload.get("completed_at"):
            process.completed_at = float(payload["completed_at"])
        if status in {"starting", "running"} and not self._pid_alive(process.supervisor_pid):
            status = "interrupted"
            process.completed_at = process.completed_at or time.time()
            process.result = process.result or {
                "output": "",
                "error": "[Interrupted] Detached process owner exited without a terminal result",
                "exit_code": -1,
            }
            process.recovered_status = status
            self._persist(process)
            return
        if status == "starting":
            status = "running"
        process.recovered_status = status

    def describe(self, process: ManagedProcess) -> dict:
        self._sync_result(process)
        status = self.status(process)
        result = process.result or {}
        description = {
            "process_id": process.process_id,
            "kind": process.kind,
            "label": process.label,
            "task_id": process.task_id,
            "status": status,
            "started_at": process.started_at,
            "completed_at": process.completed_at,
            "duration_ms": int(
                ((process.completed_at or time.time()) - process.started_at) * 1000
            ),
            "exit_code": result.get("exit_code") if status != "running" else None,
            "has_output": process.output_chars > 0,
            "output_chars": process.output_chars,
            "stdout_chars": process.stdout_chars,
            "stderr_chars": process.stderr_chars,
            "artifact_path": str(process.output_path) if process.output_path.exists() else "",
            "output_reader": {
                "tool": "delegate_read" if process.kind == "subagent" else "process_read",
                # No offset: the reader's own cursor continues after the last
                # read. A fixed offset 0 would return the first page every time.
                "arguments": {"process_id": process.process_id, "max_chars": 12000},
            },
            "execution_owner": "supervisor" if process.external else "backend",
            "supervisor_pid": process.supervisor_pid if process.external else None,
        }
        if process.metadata:
            description["metadata"] = dict(process.metadata)
        return description

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def _byte_offset_for_char(path: Path, char_offset: int) -> int:
        """Map a legacy character cursor with bounded memory."""
        if char_offset <= 0:
            return 0
        remaining = char_offset
        try:
            # newline=None preserves the historical read_text() behavior:
            # Windows CRLF is exposed as one ``\n`` character.
            with path.open("r", encoding="utf-8", errors="replace", newline=None) as handle:
                while remaining > 0:
                    text = handle.read(min(64 * 1024, remaining))
                    if not text:
                        break
                    remaining -= len(text)
                return handle.tell()
        except OSError:
            return 0

    @staticmethod
    def _read_file_chunk(
        path: Path,
        byte_offset: int,
        max_chars: int,
    ) -> tuple[str, int, int]:
        """Read a bounded UTF-8 page from a byte cursor."""
        try:
            total_bytes = path.stat().st_size
            start = min(max(0, byte_offset), total_bytes)
            with path.open("rb") as handle:
                handle.seek(start)
                raw = handle.read(max(8, max_chars * 4 + 4))
        except OSError:
            return "", max(0, byte_offset), 0

        skipped = 0
        while skipped < len(raw) and raw[skipped] & 0xC0 == 0x80:
            skipped += 1
        start += skipped
        raw = raw[skipped:]
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":
                decoded = raw[:exc.start].decode("utf-8", errors="replace")
            else:
                decoded = raw.decode("utf-8", errors="replace")
        # Match Path.read_text()/TextIOWrapper universal-newline behavior while
        # advancing the byte cursor by the exact raw prefix consumed.
        normalized: list[str] = []
        raw_chars = 0
        while raw_chars < len(decoded) and len(normalized) < max_chars:
            char = decoded[raw_chars]
            if char == "\r":
                raw_chars += 1
                if raw_chars < len(decoded) and decoded[raw_chars] == "\n":
                    raw_chars += 1
                normalized.append("\n")
                continue
            normalized.append(char)
            raw_chars += 1
        content = "".join(normalized)
        raw_prefix = decoded[:raw_chars]
        next_byte = min(total_bytes, start + len(raw_prefix.encode("utf-8")))
        return content, next_byte, total_bytes

    def read(
        self,
        process: ManagedProcess,
        *,
        offset: int | None,
        max_chars: int,
        byte_offset: int | None = None,
        stream: str = "combined",
    ) -> dict:
        self._sync_result(process)
        paths = {
            "combined": process.output_path,
            "stdout": process.stdout_path,
            "stderr": process.stderr_path,
        }
        if stream not in paths:
            raise ValueError("stream must be one of: combined, stdout, stderr")
        path = paths[stream]
        legacy_start = (
            process.read_offsets.get(
                stream,
                process.read_offset if stream == "combined" else 0,
            )
            if offset is None
            else max(0, offset)
        )
        if byte_offset is not None:
            start_byte = max(0, byte_offset)
        elif offset is not None:
            start_byte = self._byte_offset_for_char(path, legacy_start)
        else:
            start_byte = process.read_byte_offsets.get(stream, 0)

        content, next_byte, total_bytes = self._read_file_chunk(
            path,
            start_byte,
            max(1, max_chars),
        )
        if not path.is_file() and process.result:
            if stream == "stdout":
                fallback_content = str(process.result.get("output") or "")
            elif stream == "stderr":
                fallback_content = str(process.result.get("error") or "")
            else:
                fallback_content = str(process.result.get("output") or "")
                error = str(process.result.get("error") or "")
                if error:
                    fallback_content += ("\n" if fallback_content else "") + f"[stderr]\n{error}"
            # The text is in memory, so byte and character positions map directly.
            if byte_offset is not None:
                legacy_start = len(
                    fallback_content.encode("utf-8")[:start_byte].decode("utf-8", errors="ignore")
                )
            else:
                start_byte = len(fallback_content[:legacy_start].encode("utf-8"))
            content = fallback_content[legacy_start:legacy_start + max(1, max_chars)]
            next_byte = len(fallback_content[:legacy_start + len(content)].encode("utf-8"))
            total_bytes = len(fallback_content.encode("utf-8"))

        next_legacy = legacy_start + len(content)
        # Only a read without an explicit position uses and advances the cursor,
        # so looking at another part of the output does not lose the place.
        if offset is None and byte_offset is None:
            process.read_offsets[stream] = next_legacy
            process.read_byte_offsets[stream] = next_byte
            if stream == "combined":
                process.read_offset = next_legacy
        status = self.status(process)
        description = self.describe(process)
        # Where the next read continues from this one.
        reader_arguments: dict[str, Any] = {
            "process_id": process.process_id,
            "stream": stream,
            "max_chars": 12000,
        }
        if byte_offset is not None:
            reader_arguments["byte_offset"] = next_byte
        elif offset is not None:
            reader_arguments["offset"] = next_legacy
        description["output_reader"] = {
            **description["output_reader"],
            "arguments": reader_arguments,
        }
        result = {
            **description,
            "stream": stream,
            "offset": legacy_start,
            "next_offset": next_legacy,
            "byte_offset": start_byte,
            "next_byte_offset": next_byte,
            "offset_unit": "characters; prefer byte_offset for explicit paging",
            "content": content,
            "eof": status != "running" and next_byte >= total_bytes,
            "total_chars": (
                next_legacy if status != "running" and next_byte >= total_bytes else None
            ),
            "total_bytes": total_bytes,
        }
        if byte_offset is not None:
            # A byte position does not say how many characters precede it.
            for key in ("offset", "next_offset", "offset_unit", "total_chars"):
                del result[key]
        return result

    def list(self, *, include_completed: bool = True) -> list[dict]:
        processes = [
            process
            for process in self._processes.values()
            if process.visible and (include_completed or self.status(process) == "running")
        ]
        processes.sort(key=lambda process: process.started_at, reverse=True)
        return [self.describe(process) for process in processes]

    async def cancel(self, process: ManagedProcess) -> dict:
        if process.external and self.status(process) == "running":
            if process.cancel_path is None:
                raise RuntimeError("supervised process is missing its cancel path")
            process.cancel_path.touch()
            deadline = time.monotonic() + 10
            while self.status(process) == "running" and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if self.status(process) == "running" and process.supervisor_pid:
                try:
                    os.kill(process.supervisor_pid, signal.SIGTERM)
                except OSError:
                    pass
                process.recovered_status = "interrupted"
                process.completed_at = time.time()
                self._persist(process)
            self._emit_terminal(process)
            return self.describe(process)
        if process.task is not None and not process.task.done():
            process.task.cancel()
            try:
                await process.task
            except asyncio.CancelledError:
                pass
        return self.describe(process)

    @staticmethod
    def dumps(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False)
