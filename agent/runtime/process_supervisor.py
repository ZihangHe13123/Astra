"""Detached runner for background Local/Docker sandbox executions.

The frontend/backend may disappear while this process continues to own the
actual sandbox child. State and output are exchanged only through bounded
files under ``.astra/processes`` so a new backend can reattach.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from agent.runtime.tools.processes import unfinished_result
from agent.sandbox.docker import DockerSandbox
from agent.sandbox.local import LocalSandbox


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(
        path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt >= 4:
                    raise
                # Windows can briefly hold the destination while another
                # process is reading or replacing the same manifest.
                time.sleep(0.01 * (2**attempt))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _sandbox_from_spec(spec: dict[str, Any]):
    config = dict(spec.get("sandbox") or {})
    sandbox_type = str(config.pop("type", "local"))
    if sandbox_type == "docker":
        # A detached supervisor owns a uniquely named reusable container.
        # Backend shutdown cannot touch it, while cancellation can still
        # explicitly remove the daemon-side container after killing docker
        # exec (killing a one-shot docker CLI alone may leave its container).
        config["reuse_container"] = True
        config["container_name"] = f"agent-supervisor-{os.getpid()}"
        return DockerSandbox(**config)
    return LocalSandbox(**config)


async def _run(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    manifest_path = Path(spec["manifest_path"]).resolve()
    output_path = Path(spec["output_path"]).resolve()
    stdout_path = Path(spec["stdout_path"]).resolve()
    stderr_path = Path(spec["stderr_path"]).resolve()
    cancel_path = Path(spec["cancel_path"]).resolve()
    for path in (output_path, stdout_path, stderr_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    initial_manifest = _read_manifest(manifest_path)
    output_chars = int(initial_manifest.get("output_chars") or 0)
    stdout_chars = int(initial_manifest.get("stdout_chars") or 0)
    stderr_chars = int(initial_manifest.get("stderr_chars") or 0)

    # The command/code is sensitive and only needed to start execution.
    try:
        spec_path.unlink(missing_ok=True)
    except OSError:
        pass

    def update_manifest(
        status: str,
        *,
        result: dict[str, Any] | None = None,
        completed_at: float | None = None,
    ) -> None:
        payload = _read_manifest(manifest_path)
        payload.update({
            "version": 2,
            "execution_owner": "supervisor",
            "supervisor_pid": os.getpid(),
            "status": status,
            "completed_at": completed_at,
            "output_chars": output_chars,
            "stdout_chars": stdout_chars,
            "stderr_chars": stderr_chars,
            "result": result,
        })
        _atomic_json_write(manifest_path, payload)

    def on_output(stream: str, text: str) -> None:
        nonlocal output_chars, stdout_chars, stderr_chars
        if not text:
            return
        target = stderr_path if stream == "stderr" else stdout_path
        with target.open("a", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
        with output_path.open("a", encoding="utf-8", newline="") as handle:
            if stream == "stderr":
                handle.write("[stderr]\n")
            handle.write(text)
            handle.flush()
        chars = len(text)
        output_chars += chars + (9 if stream == "stderr" else 0)
        if stream == "stderr":
            stderr_chars += chars
        else:
            stdout_chars += chars
        update_manifest("running")

    sandbox = _sandbox_from_spec(spec)
    kind = str(spec.get("kind") or "")
    if kind == "python":
        execution = sandbox.execute_python_stream(
            str(spec.get("code") or ""),
            on_output=on_output,
        )
    else:
        execution = sandbox.execute_shell_stream(
            str(spec.get("command") or ""),
            environment=str(spec.get("environment") or "auto"),
            on_output=on_output,
        )

    update_manifest("running")
    task = asyncio.create_task(execution)
    cancelled = False
    while not task.done():
        if cancel_path.exists():
            cancelled = True
            task.cancel()
            break
        await asyncio.sleep(0.1)

    if cancelled:
        try:
            await task
        except asyncio.CancelledError:
            pass
        result = {
            "output": "",
            "error": "[Cancelled] Process cancelled by user",
            "exit_code": -1,
        }
        status = "cancelled"
    else:
        try:
            result = await task
        except asyncio.CancelledError:
            result = {
                "output": "",
                "error": "[Cancelled] Supervisor execution was cancelled",
                "exit_code": -1,
            }
            status = "cancelled"
        except Exception as exc:
            # Keep what the run had written before the sandbox failed.
            result = unfinished_result(
                stdout_path,
                stderr_path,
                output_path,
                f"[ExecutionFailed] {type(exc).__name__}: {exc}",
            )
            status = "failed"
        else:
            status = (
                "failed"
                if int(result.get("exit_code", -1 if result.get("error") else 0)) != 0
                else "completed"
            )

    completed_at = time.time()
    update_manifest(status, result=result, completed_at=completed_at)
    cancel_path.unlink(missing_ok=True)
    close = getattr(sandbox, "close", None)
    if close is not None:
        closed = close()
        if hasattr(closed, "__await__"):
            await closed
    return 0 if status == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    try:
        return asyncio.run(_run(Path(args.spec).resolve()))
    except Exception as exc:
        # The parent will convert a dead supervisor with a non-terminal
        # manifest to ``interrupted``. Avoid writing command/code to stderr.
        print(f"process supervisor failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
