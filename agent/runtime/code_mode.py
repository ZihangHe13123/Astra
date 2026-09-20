"""Code Mode ``run_code``: model-written programs that orchestrate tool calls.

A ``run_code`` program runs in a subprocess (``sys.executable -c <wrapper>``),
NOT in the agent process. The wrapper binds a ``tools`` namespace whose calls
are marshalled as JSON over the subprocess's stderr/stdin to the host, which
dispatches them through the agent's real single-tool execution path (approval,
claim, policy, hooks, postconditions all apply). This keeps the model's code
out of the agent's process, so a Python reflection escape cannot touch host
memory, the process-wide stdout, or spawn orphaned asyncio tasks that survive
cancellation.

Only what the program prints and returns comes back: prints become ``logs``
and the returned value becomes the JSON ``result``.

A tool call that needs interactive approval suspends its per-request RPC
future until the user decides; a denied, cancelled, or unanswerable decision
surfaces to the program as a ``ToolCallError`` starting with a
``[ToolApproval...]`` prefix.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
import signal
import sys
import traceback
from typing import Any, Callable
import uuid

from agent.runtime.process_env import hidden_process_creationflags
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.sandbox.docker import DockerSandbox


class ToolCallError(Exception):
    """Raised inside a run_code program when a bound tool call fails."""


class CodeRunFailed(Exception):
    """A program-run failure with a stable classification and captured logs."""

    def __init__(self, kind: str, message: str, logs: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.logs = logs


# These tools must remain first-class model calls so their provenance and
# backend event names stay observable. They are never callable from a PTC
# program, even when the surrounding mode also exposes run_code.
CODE_MODE_DIRECT_TOOLS = frozenset({"ask_user_question", "computer_apps", "plan_update"})


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _max_tool_calls() -> int:
    return _positive_env("RUN_CODE_MAX_TOOL_CALLS", 100)


def _program_tool_signature(name: str, args: dict[str, Any]) -> str:
    """Return the canonical signature used by the host-side repeat guard."""
    return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


def _max_output_chars() -> int:
    return _positive_env("RUN_CODE_MAX_OUTPUT_CHARS", 32_000)


def _render_success(logs: str, result: Any, returned: bool) -> str:
    parts: list[str] = []
    if logs.strip():
        parts.append(logs.rstrip())
    if returned:
        try:
            rendered = json.dumps(result, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise CodeRunFailed(
                "invalid_output",
                f"run_code returned a value that is not JSON-serializable: {exc}",
                logs,
            )
        if rendered != "null":
            parts.append(rendered)
    if not parts:
        return "(run_code completed with no output)"
    text = "\n".join(parts)
    if len(text) > _max_output_chars():
        raise CodeRunFailed(
            "output_limit",
            f"run_code output exceeded {_max_output_chars()} characters",
            logs[:_max_output_chars()],
        )
    return text


def _logs_then(message: str, logs_text: str) -> str:
    """Place captured program output before the failure message (chronological)."""
    stripped = logs_text.rstrip()
    return f"{stripped}\n{message}" if stripped else message


# The subprocess wrapper. stderr carries the RPC protocol (tool_request / done);
# the program's stdout is redirected into a buffer and returned with the final
# `done` message, so RPC framing never mixes with program prints. Tool requests
# are synchronous: the program awaits a stdin reply before continuing.
_PTC_WRAPPER = r'''
import asyncio, json, sys, io, traceback, contextlib

class ToolCallError(Exception):
    pass

_rpc_id = 0

def _next_id():
    global _rpc_id
    _rpc_id += 1
    return _rpc_id

def _send(msg):
    sys.stderr.write(json.dumps(msg) + "\n")
    sys.stderr.flush()

_pending = {}

class _Tools:
    def __getattr__(self, name):
        async def bound(**kwargs):
            rid = _next_id()
            fut = asyncio.get_running_loop().create_future()
            _pending[rid] = fut
            _send({"type": "tool_request", "id": rid, "name": name, "args": kwargs})
            resp = await fut
            if resp.get("error"):
                raise ToolCallError(resp["error"])
            return resp.get("output", "")
        return bound

    async def gather(self, *coros):
        return list(await asyncio.gather(*coros))

def _main():
    logs = io.StringIO()
    loop = None
    try:
        spec = json.loads(sys.stdin.readline())
        code = spec["code"]
        import threading
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def _stdin_reader_loop():
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                try:
                    resp = json.loads(line)
                except Exception:
                    continue
                fut = _pending.pop(resp.get("id"), None)
                if fut is not None and not fut.done():
                    loop.call_soon_threadsafe(fut.set_result, resp)

        threading.Thread(target=_stdin_reader_loop, daemon=True).start()
        tools = _Tools()
        wrapped = "async def __ptc_main__():\n" + "\n".join(("    " + line if line.strip() else "") for line in code.splitlines())
        # No __builtins__ key: exec injects the full builtins, so `import`, `type`,
        # and `except Exception` all work. Process isolation (and, with Docker, the
        # filesystem mount) is the security boundary, not a builtins allowlist.
        ns = {"tools": tools, "ToolCallError": ToolCallError, "asyncio": asyncio}
        exec(compile(wrapped, "<run_code>", "exec"), ns, ns)
        main = ns["__ptc_main__"]

        async def run():
            with contextlib.redirect_stdout(logs):
                return await main()

        result = loop.run_until_complete(run())
        _send({"type": "done", "result": result, "logs": logs.getvalue()})
    except BaseException as exc:
        _send({"type": "done", "error": "".join(traceback.format_exception_only(type(exc), exc)).strip(), "logs": logs.getvalue()})
    finally:
        if loop is not None:
            loop.close()

_main()
'''


def _tool_allowed(agent: Any, name: str) -> bool:
    """Mirror mode allowlists and the default exposure/compatibility gate."""
    tool_def = agent.tools.get(name)
    if tool_def is None:
        return False
    if name in CODE_MODE_DIRECT_TOOLS or tool_def.result_persistence == "request_local":
        return False
    allowlist = getattr(agent, "tool_allowlist", None)
    if allowlist is not None:
        return name in allowlist
    return bool(tool_def.expose_by_default or tool_def.allow_hidden_execution)


def _tool_risk(agent: Any, name: str) -> str:
    tool_def = agent.tools.get(name)
    return str(getattr(tool_def, "risk", "read")) if tool_def is not None else "read"


async def _stop_program(proc: asyncio.subprocess.Process) -> None:
    """Stop the owned wrapper and descendants, including a Docker container."""
    container_name = str(getattr(proc, "_astra_container_name", "") or "")
    if container_name:
        if proc.returncode is None:
            with suppress(ProcessLookupError, OSError):
                proc.kill()
            with suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
                await asyncio.wait_for(proc.wait(), timeout=5)
        cleanup = await asyncio.create_subprocess_exec(
            str(getattr(proc, "_astra_docker_cmd", "docker")),
            "rm", "-f", container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=hidden_process_creationflags(),
        )
        with suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
            await asyncio.wait_for(cleanup.communicate(), timeout=5)
        return

    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=hidden_process_creationflags(),
        )
        with suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
            await asyncio.wait_for(killer.communicate(), timeout=5)
    else:
        with suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        with suppress(ProcessLookupError, OSError):
            proc.kill()
    with suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
        await asyncio.wait_for(proc.wait(), timeout=5)


async def _spawn_program(agent: Any) -> asyncio.subprocess.Process:
    """Start the run_code wrapper in Docker or an explicitly trusted host mode."""
    sandbox = getattr(agent, "_sandbox", None)
    active = getattr(sandbox, "current", sandbox)
    container_name = ""
    docker_command = ""
    if isinstance(active, DockerSandbox):
        await active._ensure_image()
        docker_command = active.docker_cmd
        container_name = f"astra-run-code-{uuid.uuid4().hex[:16]}"
        args = [
            active.docker_cmd, "run", "--rm", "-i", "--name", container_name,
            "--workdir", "/workspace",
            "-v", f"{active.workdir}:/workspace",
            "--memory", active.memory_limit,
            "--network", "none" if not active.network else "bridge",
            "--security-opt", "no-new-privileges:true",
            "--cap-drop", "ALL",
            "--pids-limit", "100",
            active.image,
            "python", "-c", _PTC_WRAPPER,
        ]
    else:
        host_opt_in = os.getenv("RUN_CODE_ALLOW_HOST", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if active is not None and not host_opt_in:
            raise CodeRunFailed(
                "sandbox",
                "PTC requires DockerSandbox; set RUN_CODE_ALLOW_HOST=1 only for "
                "explicitly trusted host execution",
            )
        args = [sys.executable, "-c", _PTC_WRAPPER]
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        # stdout is unused: program prints are captured in the wrapper and
        # returned via the `done` RPC message. DEVNULL prevents a full pipe
        # buffer from deadlocking a program that writes to stdout directly.
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = hidden_process_creationflags(new_process_group=True)
    else:
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    if container_name:
        setattr(proc, "_astra_container_name", container_name)
        setattr(proc, "_astra_docker_cmd", docker_command)
    return proc


async def _write_stdin(proc: asyncio.subprocess.Process, payload: dict) -> None:
    if proc.stdin is None:
        raise RuntimeError("subprocess stdin is unavailable")
    proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def _handle_tool_request(
    proc: asyncio.subprocess.Process,
    agent: Any,
    msg: dict,
    state: dict[str, Any],
    task_id: str,
    stdin_lock: asyncio.Lock,
) -> None:
    name = str(msg.get("name") or "")
    request_id = msg.get("id")

    async def reply(output: str, error: str) -> None:
        try:
            async with stdin_lock:
                await _write_stdin(proc, {"type": "tool_result", "id": request_id, "output": output, "error": error})
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, EOFError, OSError):
            # The program may have timed out or exited while the host-side
            # tool call was finishing. Cleanup owns the process at that point.
            return

    if not _tool_allowed(agent, name):
        await reply("", f"[ToolDisabled] Tool '{name}' is not available in the current mode.")
        return
    # Authorization (policy, static approval gate, dynamic permission_check,
    # the interactive decision) lives inside the registry execute path that
    # _execute_tool_call enters, so this task suspends here until the user
    # decides; a denied/cancelled decision returns its structured error.
    tool_def = agent.tools.get(name)
    args = dict(msg.get("args") or {})
    signature = _program_tool_signature(name, args)
    async with state["lock"]:
        state["calls"] += 1
        tool_calls = state["tool_counts"].get(name, 0) + 1
        state["tool_counts"][name] = tool_calls
        signature_calls = state["signature_counts"].get(signature, 0) + 1
        state["signature_counts"][signature] = signature_calls
        tool_limit = tool_def.max_calls_per_turn if tool_def is not None else None
        over_budget = state["calls"] > state["limit"]
        over_tool_budget = tool_limit is not None and tool_calls > tool_limit
        repeated = (
            (tool_def is None or tool_def.repeat_guard)
            and signature_calls > state["repeat_limit"]
        )
    if over_budget:
        await reply("", f"run_code program exceeded its {state['limit']}-call tool budget")
        return
    if over_tool_budget:
        await reply(
            "",
            f"[ToolBudgetExhausted] tool '{name}' reached its per-program limit "
            f"of {tool_limit} calls",
        )
        return
    if repeated:
        await reply(
            "",
            f"[ToolCircuitOpen] repeated tool call detected for '{name}' "
            f"after {state['repeat_limit']} identical calls",
        )
        return
    try:
        event = await agent._execute_tool_call(
            {"id": f"code:{request_id}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            task_id=task_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        event = {"output": "", "error": f"{type(exc).__name__}: {exc}"}
    await reply(
        str(event.get("output") or event.get("tool_output") or ""),
        str(event.get("error") or ""),
    )


async def execute_run_code(
    agent: Any,
    code: str,
    description: str,
    *,
    task_id: str = "",
    timeout: float | None = None,
) -> str:
    """Execute a model-written program in a subprocess against the tool registry."""
    if not description.strip():
        return "[run_code] invalid description: expected a non-empty string"

    effective_timeout = timeout if timeout is not None else _positive_env(
        "RUN_CODE_TIMEOUT_SECONDS", 120
    )
    proc: asyncio.subprocess.Process | None = None
    proc_reaped = False
    tool_tasks: set[asyncio.Task[Any]] = set()
    read_tasks: set[asyncio.Task[Any]] = set()

    async def cancel_tool_tasks() -> None:
        tasks = tuple(tool_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tool_tasks.clear()
        read_tasks.clear()

    try:
        proc = await _spawn_program(agent)
        await _write_stdin(proc, {"code": code})
        state: dict[str, Any] = {
            "calls": 0,
            "limit": _max_tool_calls(),
            "repeat_limit": 2,
            "tool_counts": {},
            "signature_counts": {},
            "lock": asyncio.Lock(),
        }
        stdin_lock = asyncio.Lock()
        write_lock = asyncio.Lock()
        final: dict[str, Any] = {"result": None, "error": "", "logs": ""}
        done_received = False
        stderr = proc.stderr
        if stderr is None:
            return "[run_code] exception: subprocess stderr unavailable"

        async def drain_tool_tasks() -> None:
            tasks = tuple(read_tasks)
            if not tasks:
                return
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if task.done():
                        read_tasks.discard(task)

        def schedule_tool_task(msg: dict) -> asyncio.Task[Any]:
            task = asyncio.create_task(
                _handle_tool_request(proc, agent, msg, state, task_id, stdin_lock)
            )
            tool_tasks.add(task)
            task.add_done_callback(tool_tasks.discard)
            return task

        async def rpc_loop() -> None:
            nonlocal done_received
            while True:
                line = await stderr.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    final["logs"] += line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                    continue
                if msg.get("type") == "tool_request":
                    name = str(msg.get("name") or "")
                    risk = _tool_risk(agent, name)
                    if risk in {"read", "network"}:
                        task = schedule_tool_task(msg)
                        read_tasks.add(task)
                    else:
                        # Side-effecting tools: drain reads, then run serially.
                        await drain_tool_tasks()
                        async with write_lock:
                            await schedule_tool_task(msg)
                elif msg.get("type") == "done":
                    await drain_tool_tasks()
                    done_received = True
                    final["result"] = msg.get("result")
                    final["error"] = str(msg.get("error") or "")
                    final["logs"] = str(msg.get("logs") or "")
                    break

        await asyncio.wait_for(rpc_loop(), timeout=float(effective_timeout))
        if proc.stdin is not None:
            proc.stdin.close()
        await proc.wait()
        proc_reaped = True

        if not done_received:
            detail = final["logs"].strip() or f"subprocess exited with code {proc.returncode}"
            return _logs_then(
                "[run_code] exception: subprocess exited without a done message",
                detail,
            )

        if final["error"]:
            error_text = final["error"]
            prefix = "ToolCallError:"
            if error_text.startswith(prefix):
                return _logs_then(
                    f"[run_code] tool call failed: {error_text[len(prefix):].strip()}",
                    final["logs"],
                )
            if "not JSON serializable" in error_text:
                return _logs_then(f"[run_code] invalid_output: {error_text}", final["logs"])
            return _logs_then(f"[run_code] exception: {error_text}", final["logs"])
        return _render_success(final["logs"], final["result"], True)
    except CodeRunFailed as exc:
        return _logs_then(f"[run_code] {exc.kind}: {exc.message}", exc.logs)
    except asyncio.TimeoutError:
        return f"[run_code] timeout: program exceeded {effective_timeout}s"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return f"[run_code] exception: {detail}"
    finally:
        await cancel_tool_tasks()
        if proc is not None and not proc_reaped:
            with suppress(Exception):
                await _stop_program(proc)


def register_run_code_tool(registry: ToolRegistry, agent_getter: Callable[[], Any]) -> None:
    """Register the run_code transport bound to a lazily-resolved agent."""

    async def _run_code(code: str, description: str, _task_id: str = "") -> str:
        agent = agent_getter()
        if agent is None:
            return "[run_code] agent is not available"
        return await execute_run_code(agent, code, description, task_id=_task_id)

    registry.register(ToolDef(
        name="run_code",
        description=(
            "Execute a Docker-sandboxed Python program that orchestrates multiple tool calls in one "
            "step. Takes two required arguments: `code` — the body of an async Python "
            "function (top-level `await` and `return` both work) — and `description`, a "
            "short summary of what the program does. Inside the program call tools as "
            "`await tools.<name>(<keyword args>)`; the names `tools`, `ToolCallError`, "
            "and `asyncio` are bound. Use `await tools.gather(tools.a(), tools.b())` or "
            "`await asyncio.gather(*coros)` to fan out independent reads; side-effecting "
            "calls should stay sequential. A tool that needs interactive approval "
            "pauses the program until you decide (the prompt looks like a native "
            "call); a denied, cancelled, or unanswerable decision surfaces to the "
            "program as a `ToolCallError` starting with `[ToolApproval...]`. "
            "Answer with `print(...)` and/or `return <value>`: only prints and the "
            "returned JSON value come back, so curate the result. A failing tool call "
            "raises `ToolCallError`, which you may catch. Use this for multi-step "
            "read/edit/test work instead of one round-trip per tool. The Docker runtime "
            "contains Python's standard library only and does not inherit the host "
            "project virtualenv; use an available shell tool (Minimal's `bash` for WSL) "
            "for project-declared test runners such as pytest."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The program: the body of an async Python function.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Clear, concise description of what this program does, 5-10 words."
                    ),
                },
            },
            "required": ["code", "description"],
        },
        fn=_run_code,
        sandboxed=True,
        risk="execute",
        approval="on_risk",
        group="code",
        timeout=120.0,
        max_calls_per_turn=_positive_env("RUN_CODE_MAX_CALLS_PER_TURN", 32),
        repeat_guard=True,
        cancellable=True,
    ))
