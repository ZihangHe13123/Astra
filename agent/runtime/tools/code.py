"""code tools — execute_python, execute_shell"""

import asyncio
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

from agent.sandbox.docker import DockerSandbox
from agent.sandbox.local import LocalSandbox
from ..hook_config import install_project_hooks
from ..tool_execution import ExecutionFailure, ExecutionResult, PartialResult
from ..execution_limits import COMMAND_FOREGROUND_MAX_MS, POLL_MAX_MS, validate_wait_ms

from .approval import ScopedApprovalStore
from .processes import ProcessManager
from .registry import ToolRegistry, ToolDef, approval_justification_schema


def _bounded_text_preview(text: str, *, label: str, limit: int) -> str:
    """Return a bounded head/tail preview with an explicit omission marker."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    available = 2
    marker = ""
    for _ in range(3):
        marker = f"[{label}已截断：省略 {omitted} 字符] "
        available = max(2, limit - len(marker) - len(" … "))
        updated = len(text) - available
        if updated == omitted:
            break
        omitted = updated
    head = max(1, available // 2)
    tail = max(1, available - head)
    return f"{marker}{text[:head]} … {text[-tail:]}"


def _bounded_command_preview(command: str, limit: int = 1_000) -> str:
    """Return a bounded head/tail command preview with an explicit omission marker."""
    return _bounded_text_preview(command, label="命令", limit=limit)


def _bounded_python_preview(code: str, limit: int = 240) -> str:
    """Return a bounded head/tail Python preview with an explicit omission marker."""
    return _bounded_text_preview(code, label="代码", limit=limit)


_FOREGROUND_YIELD_DESCRIPTION = (
    "How long to wait for the run, in milliseconds, before returning a process_id and "
    "letting it continue. 0 does not yield: the run stays in the foreground and is "
    "stopped at the sandbox time limit. On the host sandbox a positive value or "
    "background=true runs without that limit; the Docker sandbox applies it to every run."
)
_BACKGROUND_DESCRIPTION = "Return immediately with a process_id."


def _hit_sandbox_time_limit(result: dict) -> bool:
    """Sandboxes report their own time limit as exit code -1 and a [Timeout] error.

    One that keeps the stderr written before the stop puts it ahead of that
    notice and sets ``timed_out`` instead.
    """
    return result.get("exit_code") == -1 and (
        result.get("timed_out") is True
        or str(result.get("error") or "").startswith("[Timeout]")
    )


class _StoppedExecutionResult(ExecutionResult, PartialResult):
    """Result of a run stopped at the time limit: only what it wrote until then."""


def _compound_commands(command: str) -> list[str]:
    """Split common POSIX/PowerShell/cmd separators without misreading quotes.

    This is intentionally a conservative display/approval parser, not an
    executor. If quoting is malformed we return the original command as one
    opaque operation and retain the stricter exact-command approval.
    """
    pieces: list[str] = []
    start = 0
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char in "'\"":
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif not quote:
            pair = command[index:index + 2]
            width = 2 if pair in {"&&", "||", "\r\n"} else 1
            separator = command[index:index + width]
            if separator in {"&&", "||", "\r\n", "\r", "\n", ";", "|", "&"}:
                part = command[start:index].strip()
                if part:
                    pieces.append(part)
                start = index + width
                index += width - 1
        index += 1
    if quote:
        return [command.strip()] if command.strip() else []
    tail = command[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces or ([command.strip()] if command.strip() else [])


_INSPECTION_OPERATION_LABELS = {
    "检查 Git 工作区状态",
    "搜索源码或文本",
    "读取文件内容",
}


def _shell_operation_language(operation: str) -> tuple[list[str], bool]:
    """Classify one parsed operation for presentation only.

    Unknown operations remain explicit and make the complete command
    non-inspection. This copy never participates in authorization.
    """
    source = operation.lower()
    labels: list[str] = []

    def add(pattern: str, label: str, *, anchored: bool = False) -> None:
        matcher = re.match if anchored else re.search
        if matcher(pattern, source) and label not in labels:
            labels.append(label)

    add(
        r"\s*git(?:\.exe)?\s+(?:status|diff|log|show)\b",
        "检查 Git 工作区状态",
        anchored=True,
    )
    add(
        r"\s*(?:select-string|rg|ripgrep|grep|findstr)(?:\.exe)?\b",
        "搜索源码或文本",
        anchored=True,
    )
    add(r"\s*(?:get-content|cat|type)\b", "读取文件内容", anchored=True)
    add(r"(?:pytest|python\s+(?:\S+\s+)?-m\s+pytest)", "运行项目测试")
    add(r"\bruff\b", "运行代码检查")
    add(r"\b(?:npm|pnpm|yarn)\b", "运行项目命令")
    if not labels:
        return ["运行所请求的 shell 命令"], False
    inspection_only = (
        all(label in _INSPECTION_OPERATION_LABELS for label in labels)
        and not re.search(r"[&|;<>\r\n`]|\$\(", source)
    )
    return labels, inspection_only


def register_code_tools(
    registry: ToolRegistry,
    sandbox,
    *,
    task_store=None,
    on_process_event=None,
):
    # Declarative project hooks are installed once with the core coding tools.
    # They cannot run arbitrary shell; see runtime.hook_config.
    if not getattr(registry, "_project_hooks_loaded", False):
        install_project_hooks(registry, getattr(sandbox, "workdir", "."))
        registry._project_hooks_loaded = True
    process_dir = Path(getattr(sandbox, "workdir", ".")) / ".astra" / "processes"
    processes = ProcessManager(
        artifact_dir=process_dir,
        task_store=task_store,
        on_event=on_process_event,
    )
    host_approvals = ScopedApprovalStore(
        enabled=lambda: registry.approval_handler is not None,
        approved_scopes=registry.approved_permission_scopes,
    )

    def _uses_host_sandbox() -> bool:
        active = getattr(sandbox, "current", sandbox)
        return not isinstance(active, DockerSandbox)

    def _supervisor_sandbox_spec() -> dict | None:
        active = getattr(sandbox, "current", sandbox)
        if type(active) is LocalSandbox:
            return {
                "type": "local",
                # Background/supervised tasks must not be killed by the
                # foreground sandbox timeout (default 30-60s); the user
                # manages them via process_poll/process_cancel instead.
                "timeout": None,
                "workdir": active.workdir,
                "max_output_bytes": active.max_output_bytes,
                "max_memory_mb": active.max_memory_mb,
                "max_cpu_seconds": active.max_cpu_seconds,
            }
        if type(active) is DockerSandbox:
            return {
                "type": "docker",
                "timeout": active.timeout,
                "workdir": active.workdir,
                "memory_limit": active.memory_limit,
                "network": active.network,
                "docker_cmd": active.docker_cmd,
                "reuse_container": False,
                "image": active.image,
            }
        return None

    def _python_permission_check(args: dict):
        if not _uses_host_sandbox():
            return None
        code = str(args.get("code") or "")
        return host_approvals.request(
            scope=f"host-execution:python:{hashlib.sha256(code.encode('utf-8')).hexdigest()}",
            kind="host_execution",
            operation="在受保护主机上执行 Python",
            target=str(Path(getattr(sandbox, "workdir", ".")).resolve()),
            reason="Python 将在宿主机（而非隔离的 Docker 沙箱）中运行",
            detail=f"代码长度：{len(code)} 字符。",
            approval_title="在受保护主机上运行 Python",
            approval_summary=f"运行 {len(code)} 字符的 Python 程序",
            approval_effect="该代码可能读取或修改宿主机文件与进程。",
            approval_boundary="仅限本次精确的 Python 调用",
            arguments={
                "code": _bounded_python_preview(code),
                "background": str(bool(args.get("background", False))).lower(),
            },
        )

    def _shell_session_scope(command: str, environment: str) -> tuple[str, str] | None:
        """Return a conservative Codex-style session prefix for dev commands."""
        # Any shell control operator disables prefix reuse. This intentionally
        # errs on the side of another exact-command prompt, including when a
        # separator appears inside quoting that this small scanner does not
        # attempt to interpret for execution.
        if re.search(r"[&|;<>\r\n`]", command) or "$(" in command:
            return None
        try:
            tokens = [token.strip().strip('"\'') for token in shlex.split(command, posix=False)]
        except ValueError:
            return None
        if not tokens:
            return None
        executable = Path(tokens[0]).name.lower()
        prefix: list[str] | None = None
        label = ""
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable):
            if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in {
                "pytest", "compileall", "unittest",
            }:
                prefix = tokens[:3]
            elif len(tokens) >= 2:
                script = Path(tokens[1])
                if not script.is_absolute():
                    script = Path(getattr(sandbox, "workdir", ".")) / script
                resolved_script: Path | None = None
                try:
                    resolved_script = script.resolve()
                    workspace = Path(getattr(sandbox, "workdir", ".")).resolve()
                    inside_workspace = os.path.commonpath(
                        [str(workspace), str(resolved_script)]
                    ) == str(workspace)
                except (OSError, ValueError):
                    inside_workspace = False
                script_name = resolved_script.name.lower() if inside_workspace and resolved_script is not None else ""
                if inside_workspace and (
                    script_name.startswith("test_") or script_name.endswith("_test.py")
                ):
                    prefix = [tokens[0], "<workspace-test-script>"]
                    label = f"{tokens[0]} <any workspace test script>"
        elif executable in {"pytest", "pytest.exe", "ruff", "ruff.exe"}:
            prefix = tokens[:1]
        elif executable in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"}:
            if len(tokens) >= 2 and tokens[1] == "test":
                prefix = tokens[:2]
            elif len(tokens) >= 3 and tokens[1] == "run" and re.match(
                r"^(?:test|lint|check|build)(?::|$)", tokens[2], re.IGNORECASE
            ):
                prefix = tokens[:3]
        elif executable in {"cargo", "go", "dotnet"} and len(tokens) >= 2:
            if tokens[1] in {"test", "check", "build"}:
                prefix = tokens[:2]
        if prefix is None:
            return None
        if not label:
            label = " ".join(prefix)
        workspace_key = str(Path(getattr(sandbox, "workdir", ".")).resolve())
        digest = hashlib.sha256(
            f"{workspace_key}\0{environment}\0{label}".encode("utf-8")
        ).hexdigest()
        return f"host-execution:shell-prefix:{environment}:{digest}", label

    def _shell_approval_language(
        command: str,
        environment: str,
        parts: list[str] | None = None,
        session_scope: tuple[str, str] | None = None,
    ) -> dict[str, str]:
        """Build readable copy without allowing it to change authorization."""
        operations: list[str] = []
        inspection_flags: list[bool] = []
        parsed_operations = parts if parts is not None else _compound_commands(command)
        for operation in parsed_operations or [command]:
            labels, operation_is_inspection = _shell_operation_language(operation)
            inspection_flags.append(operation_is_inspection)
            for label in labels:
                if label not in operations:
                    operations.append(label)

        operation_summary = "; ".join(operations)
        if parts is not None and len(parts) > 1:
            operation_summary += f"（{len(parts)} 项操作）"
        inspection = bool(inspection_flags) and all(inspection_flags)
        title = (
            "检查工作区状态与源码"
            if len(operations) > 1 and inspection
            else "运行主机检查命令"
            if inspection
            else "在受保护主机上运行 shell 命令"
        )
        effect = (
            "该命令看起来是检查类操作；批准前请核实展开后的完整命令。"
            if inspection
            else "该命令可能读取或修改主机状态；批准前请核实展开后的完整命令。"
        )
        boundary = f"精确命令 · {environment} · 仅本次"
        if session_scope is not None:
            boundary += f"；可选会话前缀：{session_scope[1]}"
        return {
            "approval_title": title,
            "approval_summary": operation_summary,
            "approval_effect": effect,
            "approval_boundary": boundary,
            "approval_question": f"是否允许我在宿主机上{operation_summary}？",
            "session_scope_label": session_scope[1] if session_scope is not None else "",
        }

    def _shell_permission_check(args: dict, *, force_host: bool = False):
        if not force_host and not _uses_host_sandbox():
            return None
        command = str(args.get("command") or "")
        environment = str(args.get("environment") or "auto")
        session_scope = _shell_session_scope(command, environment)
        # Delegated Workers are already constrained to the shared workspace.
        # Auto-run only conservative test/build prefixes; arbitrary host
        # commands and execute_python still cross the normal approval boundary.
        if (
            args.get("__execution_origin") == "delegate:worker"
            and session_scope is not None
        ):
            return None
        command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
        exact_scope = f"host-execution:shell:{environment}:{command_digest}"
        if exact_scope in host_approvals.approved_scopes or (
            session_scope is not None
            and session_scope[0] in host_approvals.approved_scopes
        ):
            return None
        parts = _compound_commands(command)
        language = _shell_approval_language(command, environment, parts, session_scope)
        command_preview = _bounded_command_preview(command)
        request = host_approvals.request(
            scope=exact_scope,
            kind="host_execution",
            operation="在受保护主机上执行 shell 命令",
            target=command_preview,
            reason="该命令将在宿主机（而非隔离的 Docker 沙箱）中运行",
            detail=(
                f"精确命令范围；环境：{environment}；工作目录："
                f"{Path(getattr(sandbox, 'workdir', '.')).resolve()}"
            ),
            **language,
            arguments={
                "command": command_preview,
                "environment": environment,
                "background": str(bool(args.get("background", False))).lower(),
            },
        )
        if request is not None and len(parts) > 1:
            # The frontend can show exactly which independent operations are
            # covered by this prompt. Session grants remain disabled for a
            # compound command by _shell_session_scope above.
            request["compound_commands"] = [
                _bounded_command_preview(part, limit=240)
                for part in parts[:16]
            ]
            request["compound_command_count"] = len(parts)
            request["detail"] += (
                f" 复合命令共 {len(parts)} 项；操作预览已单独列出。"
            )
        if request is not None and session_scope is not None:
            request["session_scope"] = session_scope[0]
            request["session_scope_label"] = session_scope[1]
            request["detail"] += (
                f" 选择会话授权同时允许此工作区命令前缀："
                f"{session_scope[1]}"
            )
        return request

    def _host_permission_grant(args: dict, request: dict, decision: str):
        grant_request = dict(request)
        if decision == "session" and request.get("session_scope"):
            grant_request["scopes"] = [
                str(request["scope"]),
                str(request["session_scope"]),
            ]
        return host_approvals.grant(args, grant_request, decision)

    def _python_approval_formatter(args: dict) -> dict[str, str]:
        code = str(args.get("code") or "")
        return {
            "approval_title": "在受保护主机上运行 Python",
            "approval_summary": f"运行 {len(code)} 字符的 Python 程序",
            "approval_effect": "该代码可能读取或修改宿主机文件与进程。",
            "approval_boundary": "仅限本次精确的 Python 调用",
            "approval_question": f"是否允许我在宿主机上运行这段 {len(code)} 字符的 Python 程序？",
        }

    def _shell_approval_formatter(args: dict) -> dict[str, str]:
        command = str(args.get("command") or "")
        environment = str(args.get("environment") or "auto")
        return _shell_approval_language(
            command,
            environment,
            _compound_commands(command),
            _shell_session_scope(command, environment),
        )

    def _persistent_bash_environment() -> str:
        return "wsl" if sys.platform == "win32" else "posix"

    def _persistent_bash_permission_check(args: dict):
        return _shell_permission_check(
            {**args, "environment": _persistent_bash_environment()},
            force_host=True,
        )

    def _persistent_bash_approval_formatter(args: dict) -> dict[str, str]:
        return _shell_approval_formatter({**args, "environment": _persistent_bash_environment()})

    async def _execute_python_stream(code: str, on_output):
        execute = getattr(sandbox, "execute_python_stream", None)
        if execute is not None:
            return await execute(code, on_output=on_output)
        result = await sandbox.execute_python(code)
        if result.get("output"):
            on_output("stdout", str(result["output"]))
        if result.get("error"):
            on_output("stderr", str(result["error"]))
        return result

    async def _execute_shell_stream(command: str, environment: str, on_output):
        execute = getattr(sandbox, "execute_shell_stream", None)
        if execute is not None:
            return await execute(command, environment=environment, on_output=on_output)
        result = await sandbox.execute_shell(command, environment=environment)
        if result.get("output"):
            on_output("stdout", str(result["output"]))
        if result.get("error"):
            on_output("stderr", str(result["error"]))
        return result

    async def _execute_persistent_bash_stream(command: str, on_output):
        execute = getattr(sandbox, "execute_persistent_bash_stream", None)
        if execute is None:
            # Keep lightweight sidecars from before the generic lifecycle.
            execute = getattr(sandbox, "execute_wsl_shell_stream", None)
        if execute is None:
            raise RuntimeError(
                "persistent Bash is unavailable while the active sandbox does not expose a sidecar"
            )
        return await execute(command, on_output=on_output)

    def _render_result(
        result: dict,
        *,
        environment: str = "",
        raise_on_failure: bool = True,
    ) -> str:
        parts = []
        if result["output"]:
            parts.append(result["output"])
        if result["error"]:
            parts.append(f"[Stderr]\n{result['error']}")
        if (
            not parts
            and not (
                result["exit_code"] != 0
                and environment
                and not raise_on_failure
            )
        ):
            parts.append(f"(Completed, exit code {result['exit_code']})")
        if result.get("artifact_path"):
            parts.append(f"[Full output artifact: {result['artifact_path']}]")
        if result.get("output_reader"):
            parts.append("[Read full output: " + json.dumps(result["output_reader"]) + "]")
        if result["exit_code"] != 0 and environment and raise_on_failure:
            resolved = result.get("environment", environment)
            raise ExecutionFailure(
                f"Shell command failed in {resolved} environment (exit code {result['exit_code']}).\n"
                + "\n".join(parts), result,
            )
        if (
            result["exit_code"] != 0
            and (not environment or not raise_on_failure)
            and not result.get("shell_reset")
        ):
            parts.append(f"[exit code: {result['exit_code']}]")
        if result.get("status") == "timed_out":
            return _StoppedExecutionResult("\n".join(parts), result)
        return ExecutionResult("\n".join(parts), result)

    async def _run_or_background(
        factory,
        *,
        kind: str,
        label: str,
        foreground_yield_ms: int,
        background: bool,
        task_id: str,
        supervisor_spec: dict | None = None,
    ):
        validate_wait_ms(foreground_yield_ms, name="foreground_yield_ms", maximum=COMMAND_FOREGROUND_MAX_MS)
        use_supervisor = (
            supervisor_spec is not None
            and (background or foreground_yield_ms > 0)
        )
        if use_supervisor:
            assert supervisor_spec is not None
            process = await processes.start_supervised(
                supervisor_spec,
                kind=kind,
                label=label[:240],
                task_id=task_id,
            )
        else:
            process = processes.start(
                factory,
                kind=kind,
                label=label[:240],
                task_id=task_id,
            )
        if background:
            processes.expose(process)
            return None, process
        try:
            completed = await processes.wait(process, foreground_yield_ms)
        except asyncio.CancelledError:
            # The call was cancelled before it returned a result or a
            # process_id, so nothing else could stop this run afterwards.
            await processes.stop_abandoned(process)
            raise
        if completed:
            processes.describe(process)
            result = process.result
            if result and _hit_sandbox_time_limit(result):
                # The receipt must not call a stopped run completed.
                result = {**result, "status": "timed_out"}
                unlimited_when_supervised = (
                    supervisor_spec is not None
                    and supervisor_spec["sandbox"].get("timeout") is None
                )
                if not use_supervisor and unlimited_when_supervised:
                    result["error"] = (
                        f"{result['error']}. The run was in the foreground "
                        "(foreground_yield_ms=0), where the sandbox time limit applies, so it "
                        "was stopped. A positive foreground_yield_ms or background=true runs "
                        "without that limit; follow it with process_poll/process_read."
                    )
                    if kind == "shell":
                        # The agent loop forces tracked source-mutating commands to 0.
                        result["error"] += (
                            " Formatter and code-generator commands always run in the foreground."
                        )
            if result and result.get("artifact_path"):
                processes.expose(process)
                result["output_reader"] = processes.describe(process)["output_reader"]
            else:
                processes.discard_unexposed(process)
            return result, process
        else:
            processes.expose(process)
        return None, process

    def _process_result(description: dict) -> str:
        return ExecutionResult(processes.dumps(description), description)

    async def _execute_python(
        code: str,
        foreground_yield_ms: int = 10_000,
        background: bool = False,
        _task_id: str = "",
    ) -> str:
        active = getattr(sandbox, "current", sandbox)
        if isinstance(active, LocalSandbox):
            active.process_guard.check_python(code)
        sandbox_spec = _supervisor_sandbox_spec()
        result, process = await _run_or_background(
            lambda on_output: _execute_python_stream(code, on_output),
            kind="python",
            label="python code",
            foreground_yield_ms=foreground_yield_ms,
            background=background,
            task_id=_task_id,
            supervisor_spec={
                "sandbox": sandbox_spec,
                "code": code,
            } if sandbox_spec is not None else None,
        )
        if result is None:
            return _process_result({
                **processes.describe(process),
                "message": "Execution continues in the background; use process_poll/process_read.",
            })
        return _render_result(result)

    async def _execute_shell(
        command: str,
        environment: str = "auto",
        foreground_yield_ms: int = 10_000,
        background: bool = False,
        _task_id: str = "",
    ) -> str:
        active = getattr(sandbox, "current", sandbox)
        if isinstance(active, LocalSandbox):
            active.check_host_processes(command, environment)
        sandbox_spec = _supervisor_sandbox_spec()
        result, process = await _run_or_background(
            lambda on_output: _execute_shell_stream(command, environment, on_output),
            kind="shell",
            label=command,
            foreground_yield_ms=foreground_yield_ms,
            background=background,
            task_id=_task_id,
            supervisor_spec={
                "sandbox": sandbox_spec,
                "command": command,
                "environment": environment,
            } if sandbox_spec is not None else None,
        )
        if result is None:
            return _process_result({
                **processes.describe(process),
                "environment": environment,
                "message": "Command continues in the background; use process_poll/process_read.",
            })
        return _render_result(result, environment=environment)

    async def _execute_persistent_bash(command: str, _task_id: str = "") -> str:
        """Expose a DSH-shaped command tool without disabling Docker PTC."""
        if not command.strip():
            raise ValueError("command must be a non-empty string")
        result, process = await _run_or_background(
            lambda on_output: _execute_persistent_bash_stream(command, on_output),
            kind="bash",
            label=command,
            # DSH's minimal bash is a foreground tool.  Waiting here also
            # avoids leaking a process_id field into its compact schema.
            foreground_yield_ms=0,
            background=False,
            task_id=_task_id,
        )
        if result is None:
            return _process_result({
                **processes.describe(process),
                "environment": _persistent_bash_environment(),
                "message": "Persistent Bash command continues in the background; use process_poll/process_read.",
            })
        # dsh's persistent bash reports non-zero exits as ordinary command
        # results. The model needs the exit code to decide whether a grep,
        # probe, or test failed; turning it into a generic tool exception loses
        # that DSH-shaped contract.
        return _render_result(
            result,
            environment=_persistent_bash_environment(),
            raise_on_failure=False,
        )

    async def _process_poll(process_id: str, wait_ms: int = 0) -> str:
        validate_wait_ms(wait_ms, name="wait_ms", maximum=POLL_MAX_MS)
        process = processes.get(process_id)
        if wait_ms and processes.status(process) == "running":
            await processes.wait(process, wait_ms)
        processes.observe(process)
        return _process_result(processes.describe(process))

    async def _process_read(
        process_id: str,
        offset: int | None = None,
        byte_offset: int | None = None,
        max_chars: int = 12_000,
        stream: str = "combined",
    ) -> str:
        if offset is not None and offset < 0:
            raise ValueError("offset must be zero or greater")
        if byte_offset is not None and byte_offset < 0:
            raise ValueError("byte_offset must be zero or greater")
        if offset is not None and byte_offset is not None:
            raise ValueError("offset and byte_offset cannot be combined")
        if max_chars < 1 or max_chars > 100_000:
            raise ValueError("max_chars must be between 1 and 100000")
        process = processes.get(process_id)
        result = processes.read(
            process,
            offset=offset,
            max_chars=max_chars,
            byte_offset=byte_offset,
            stream=stream,
        )
        processes.observe(process)
        # describe() refreshes the status; the reader arguments of this read say
        # where the next one continues.
        return _process_result({
            **result,
            **processes.describe(process),
            "output_reader": result["output_reader"],
        })

    async def _process_cancel(process_id: str) -> str:
        process = processes.get(process_id)
        return _process_result(await processes.cancel(process))

    async def _process_list(include_completed: bool = True) -> str:
        return processes.dumps(processes.list(include_completed=include_completed))

    registry.register(ToolDef(
        name="execute_python",
        description=(
            "Run Python code in a sandboxed environment. Long runs return a process_id "
            "after foreground_yield_ms and continue in the background. When this call may "
            "cross the host sandbox boundary, include an optional concise user-facing explanation "
            "in `reason` of why this exact operation is needed; it is shown to the user but does "
            "not change the granted permissions."
        ),
        parameters={"type": "object", "properties": {
            "code": {"type": "string", "description": "Python code"},
            "foreground_yield_ms": {
                "type": "integer", "minimum": 0, "maximum": COMMAND_FOREGROUND_MAX_MS, "default": 10000,
                "description": _FOREGROUND_YIELD_DESCRIPTION,
            },
            "background": {
                "type": "boolean", "default": False, "description": _BACKGROUND_DESCRIPTION,
            },
            **approval_justification_schema(),
        }, "required": ["code"]},
        fn=_execute_python, sandboxed=True, risk="execute", approval="on_risk", group="code",
        # The selected sandbox already owns timeout/cancellation. A second
        # registry-level 30s deadline used to cut off a configured 60s run.
        timeout=None,
        permission_check=_python_permission_check,
        permission_grant=_host_permission_grant,
        approval_formatter=_python_approval_formatter,
        approval_justification=True,
    ))
    from .notebook import register_notebook_tools
    register_notebook_tools(registry, _execute_python, _python_permission_check, _host_permission_grant)

    shell_description = (
        "Run a shell command and return stdout/stderr. Long runs return a process_id "
        "after foreground_yield_ms and continue in the background. "
        "Direct source rewrites through redirection, in-place shell edits, or embedded file-write "
        "APIs are blocked; use edit_file/apply_patch instead. Approved formatter and code-generator "
        "commands are allowed with automatic Git worktree change tracking and run in the foreground "
        "until the post-command snapshot is complete. "
        "Set environment to auto, windows, wsl, or posix. For project tests, discover and use the "
        "project-declared runtime (for example .venv/Scripts/python.exe) before using a system "
        "interpreter. Do not install or upgrade dependencies unless the user explicitly requests it. "
        "When this call may cross the host sandbox boundary, include an optional concise user-facing "
        "explanation in `reason` of why this exact operation is needed; it is shown to the user but "
        "does not change the granted permissions"
    )
    if sys.platform == "win32":
        shell_description += (
            ". On this Windows host, auto routes common Linux commands and Linux paths to WSL; "
            "use environment='windows' or environment='wsl' when the target must be explicit. "
            "environment='posix' is unavailable on Windows"
        )
        environment_description = (
            "Execution environment. auto detects Linux commands/paths on Windows; "
            "posix is unavailable on Windows."
        )
    else:
        shell_description += (
            ". On this POSIX host, auto uses the native POSIX shell; use environment='posix' "
            "when the target must be explicit. windows and wsl are unavailable on this host"
        )
        environment_description = (
            "Execution environment. auto uses the native POSIX shell; windows and wsl are "
            "unavailable on this host."
        )

    registry.register(ToolDef(
        name="execute_shell",
        description=shell_description + ". Host Bash and WSL commands enable pipefail so failed commands piped to tail keep a failing exit status. Use set +o pipefail explicitly when last-command pipeline semantics are intended.",
        parameters={"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell command"},
            "environment": {
                "type": "string",
                "enum": ["auto", "windows", "wsl", "posix"],
                "default": "auto",
                "description": environment_description,
            },
            "foreground_yield_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": COMMAND_FOREGROUND_MAX_MS,
                "default": 10000,
                "description": _FOREGROUND_YIELD_DESCRIPTION,
            },
            "background": {
                "type": "boolean",
                "default": False,
                "description": _BACKGROUND_DESCRIPTION,
            },
            **approval_justification_schema(),
        }, "required": ["command"]},
        fn=_execute_shell, sandboxed=True, risk="execute", approval="on_risk", group="code",
        timeout=None,
        permission_check=_shell_permission_check,
        permission_grant=_host_permission_grant,
        approval_formatter=_shell_approval_formatter,
        approval_justification=True,
    ))
    persistent_bash_transport = "WSL" if sys.platform == "win32" else "the native POSIX host"
    registry.register(ToolDef(
        name="bash",
        description=(
            "Run a command in the owner-scoped persistent Bash environment from the workspace. "
            "cwd and exported variables persist across calls until Minimal Mode is left or reset. "
            "This is the DSH-compatible Minimal Mode shell surface; PTC `run_code` "
            f"continues to run in the Docker sandbox. Bash runs through {persistent_bash_transport}, "
            "crosses the host boundary, and may require approval."
        ),
        parameters={"type": "object", "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to run. Relative paths are preferred.",
            },
        }, "required": ["command"], "additionalProperties": False},
        fn=_execute_persistent_bash,
        sandboxed=True,
        risk="execute",
        approval="on_risk",
        group="minimal",
        timeout=None,
        permission_check=_persistent_bash_permission_check,
        permission_grant=_host_permission_grant,
        approval_formatter=_persistent_bash_approval_formatter,
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="process_poll",
        description="Check status and exit code for a background sandbox process.",
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "wait_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": POLL_MAX_MS,
                    "default": 0,
                },
            },
            "required": ["process_id"],
        },
        fn=_process_poll,
        risk="read",
        # Status changes over time, so identical calls in one ReAct turn must
        # not be satisfied from the registry's idempotent-result cache.
        idempotent=False,
        repeat_guard=False,
        group="code",
        timeout=None,
    ))
    registry.register(ToolDef(
        name="process_read",
        description=(
            "Read live or completed output incrementally from a background sandbox process. "
            "Without offset or byte_offset, each call continues after the previous such call "
            "(one cursor per stream). A call with offset or byte_offset reads from that "
            "position and does not move the cursor."
        ),
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Legacy character offset; prefer byte_offset for efficient explicit paging.",
                },
                "byte_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Byte cursor returned as next_byte_offset by the previous read.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100000,
                    "default": 12000,
                },
                "stream": {
                    "type": "string",
                    "enum": ["combined", "stdout", "stderr"],
                    "default": "combined",
                },
            },
            "required": ["process_id"],
        },
        fn=_process_read,
        risk="read",
        # Omitting offset and byte_offset advances the per-stream cursor.
        idempotent=False,
        repeat_guard=False,
        group="code",
        timeout=None,
    ))
    registry.register(ToolDef(
        name="process_list",
        description=(
            "List current and recovered background sandbox processes. Detached executions "
            "remain attached after backend restart while their supervisor is alive."
        ),
        parameters={
            "type": "object",
            "properties": {
                "include_completed": {"type": "boolean", "default": True},
            },
        },
        fn=_process_list,
        risk="read",
        idempotent=False,
        repeat_guard=False,
        group="code",
        timeout=None,
    ))
    registry.register(ToolDef(
        name="process_cancel",
        description="Cancel one background sandbox process and wait for child cleanup.",
        parameters={
            "type": "object",
            "properties": {"process_id": {"type": "string"}},
            "required": ["process_id"],
        },
        fn=_process_cancel,
        risk="write",
        group="code",
        timeout=None,
    ))
    return processes
