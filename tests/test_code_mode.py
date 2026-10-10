"""Tests for the Code Mode run_code transport."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime.code_mode import (
    CODE_MODE_DIRECT_TOOLS,
    CodeRunFailed,
    _stop_program,
    _spawn_program,
    execute_run_code,
    register_run_code_tool,
)
from agent.sandbox.docker import DockerSandbox
from agent.runtime.tools.registry import ToolDef, ToolRegistry


class FakeAgent:
    """Minimal stand-in for the ReActAgent interface run_code uses."""

    def __init__(self, registry: ToolRegistry):
        self.tools = registry
        self.events: list[dict] = []

    async def _execute_tool_call(self, tc: dict, task_id: str | None = None):
        self.events.append(dict(tc, task_id=task_id))
        name = tc["name"]
        tool = self.tools.get(name)
        args = json.loads(tc.get("arguments") or "{}")
        if tool is None or tool.fn is None:
            return {"output": "", "error": f"unknown tool {name}", "code": "unknown_tool", "tool_output": ""}
        try:
            if asyncio.iscoroutinefunction(tool.fn):
                output = await tool.fn(**args)
            else:
                output = tool.fn(**args)
            text = str(output)
            return {"output": text, "error": "", "code": "", "tool_output": text}
        except Exception as exc:  # noqa: BLE001
            return {"output": "", "error": str(exc), "code": "failed", "tool_output": ""}


class _CancellableAgent(FakeAgent):
    def __init__(self, registry: ToolRegistry):
        super().__init__(registry)
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def _execute_tool_call(self, tc: dict, task_id: str | None = None):
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return await super()._execute_tool_call(tc, task_id=task_id)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def echo(message: str) -> str:
        return f"echo:{message}"

    async def broken() -> str:
        raise RuntimeError("broken tool")

    registry.register(ToolDef("echo", "echo a message", {
        "type": "object", "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }, echo, group="core"))
    registry.register(ToolDef("broken", "always fails", {
        "type": "object", "properties": {},
    }, broken, group="core"))
    return registry


class ExecutingAgent(FakeAgent):
    """FakeAgent variant that routes sub-calls through the real
    ``registry.execute`` path so policy, approval, and permission hooks apply."""

    async def _execute_tool_call(self, tc: dict, task_id: str | None = None):
        self.events.append(dict(tc, task_id=task_id))
        name = tc["name"]
        args = json.loads(tc.get("arguments") or "{}")
        result = await self.tools.execute(
            name, args, call_id=str(tc.get("id") or ""), task_id=task_id or ""
        )
        return {
            "output": str(result.get("output") or ""),
            "error": str(result.get("error") or ""),
            "code": result.get("code", ""),
            "tool_output": str(result.get("output") or ""),
        }


def _approval_registry(handler) -> ToolRegistry:
    """Registry whose guarded_write tool always asks for approval."""
    registry = ToolRegistry()
    registry.policy.add_rule_shortcut("guarded_write", "ask", description="needs approval")
    registry.register(ToolDef(
        "guarded_write",
        "writes a file",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        lambda path="": f"wrote:{path}",
        group="code",
    ))
    registry.set_approval_handler(handler)
    return registry


def test_run_code_calls_tool_and_returns_result():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(
        agent,
        "return await tools.echo(message='hi')",
        "echo hi",
        task_id="t-1",
    ))
    assert "echo:hi" in text
    assert agent.events[0]["name"] == "echo"
    assert agent.events[0]["task_id"] == "t-1"


def test_run_code_prints_and_returns_both():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(
        agent,
        "print('log line')\nreturn await tools.echo(message='x')",
        "log and echo",
    ))
    assert "log line" in text
    assert "echo:x" in text


def test_run_code_returns_no_output_marker():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(agent, "return None", "do nothing"))
    assert text == "(run_code completed with no output)"


def test_tool_failure_raises_toolcallerror():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(
        agent,
        "try:\n    await tools.broken()\nexcept ToolCallError as e:\n    return f'caught:{e}'",
        "catch broken tool",
    ))
    assert "caught:broken tool" in text


def test_uncaught_tool_failure_is_classified():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(agent, "await tools.broken()", "break"))
    assert text.startswith("[run_code] tool call failed:")


def test_exception_is_classified():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(agent, "raise ValueError('nope')", "raise"))
    assert "[run_code] exception:" in text
    assert "ValueError" in text


def test_syntax_error_is_classified_and_protocol_is_closed():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(agent, "return (", "syntax error"))
    assert text.startswith("[run_code] exception:")
    assert "SyntaxError" in text
    assert "without a done message" not in text


def test_timeout_cancels_host_side_tool_request():
    agent = _CancellableAgent(_registry())

    async def run():
        # Generous startup budget: the timeout must fire while the host-side
        # call is in flight, not before the subprocess sends its first request.
        task = asyncio.create_task(
            execute_run_code(agent, "return await tools.echo(message='slow')", "cancel host call", timeout=5)
        )
        await asyncio.wait_for(agent.started.wait(), timeout=5)
        result = await task
        assert result.startswith("[run_code] timeout:")
        await asyncio.wait_for(agent.cancelled.wait(), timeout=5)

    asyncio.run(run())


def test_timeout_cancels_host_side_effect_task():
    registry = _registry()
    registry.register(ToolDef(
        "slow_execute",
        "slow execute",
        {"type": "object", "properties": {}},
        lambda: "finished",
        risk="execute",
        approval="never",
    ))
    agent = _CancellableAgent(registry)

    async def run():
        task = asyncio.create_task(
            execute_run_code(
                agent,
                "return await tools.slow_execute()",
                "cancel host side effect",
                timeout=5,
            )
        )
        await asyncio.wait_for(agent.started.wait(), timeout=5)
        result = await task
        assert result.startswith("[run_code] timeout:")
        await asyncio.wait_for(agent.cancelled.wait(), timeout=5)

    asyncio.run(run())


def test_timeout_cleanup_kills_owned_process_group(monkeypatch):
    import agent.runtime.code_mode as code_mode

    class FakeProcess:
        pid = 321
        returncode = None

        def __init__(self):
            self.killed = False
            self.waited = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True

    process = FakeProcess()
    groups: list[tuple[int, int]] = []
    monkeypatch.setattr(code_mode.os, "name", "posix")
    monkeypatch.setattr(code_mode.os, "killpg", lambda pid, sig: groups.append((pid, sig)), raising=False)

    asyncio.run(_stop_program(process))

    assert groups and groups[0][0] == process.pid
    assert process.killed and process.waited


def test_run_code_requires_docker_for_configured_host_sandbox(monkeypatch):
    class Agent:
        _sandbox = object()

    monkeypatch.delenv("RUN_CODE_ALLOW_HOST", raising=False)
    with pytest.raises(CodeRunFailed, match="requires DockerSandbox"):
        asyncio.run(_spawn_program(Agent()))


def test_run_code_uses_configured_docker_image(monkeypatch):
    sandbox = DockerSandbox(workdir="D:\\work", docker_cmd="docker", image="astra-sandbox:test")
    calls = []

    async def fake_ensure_image(self):
        return None

    class FakeProcess:
        pass

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr(DockerSandbox, "_ensure_image", fake_ensure_image)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(_spawn_program(SimpleNamespace(_sandbox=sandbox)))

    assert calls and "astra-sandbox:test" in calls[0]


def test_docker_startup_cancellation_reaps_image_process():
    class FakeProcess:
        returncode = None

        def __init__(self):
            self.killed = False
            self.waited = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def communicate(self):
            await asyncio.sleep(30)

        async def wait(self):
            self.waited = True

    async def scenario():
        process = FakeProcess()
        task = asyncio.create_task(DockerSandbox._communicate_startup_process(process))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed and process.waited

    asyncio.run(scenario())


def test_invalid_output_is_classified():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(agent, "return asyncio", "bad return"))
    assert "[run_code] invalid_output:" in text


def test_run_code_not_exposed_to_program():
    agent = FakeAgent(_registry())
    asyncio.run(execute_run_code(
        agent,
        "return hasattr(tools, 'run_code')",
        "check recursion",
    ))
    # No run_code binding exists in the program namespace.
    assert "[run_code]" not in json.dumps(agent.events)


def test_direct_interaction_tools_are_not_callable_inside_run_code():
    registry = _registry()
    registry.register(ToolDef(
        "ask_user_question",
        "ask",
        {"type": "object", "properties": {}},
        lambda: "must stay direct",
        group="core",
    ))
    agent = FakeAgent(registry)

    text = asyncio.run(execute_run_code(
        agent,
        "return await tools.ask_user_question()",
        "reject nested question",
    ))

    assert "[ToolDisabled]" in text
    assert not any(event["name"] == "ask_user_question" for event in agent.events)


def test_computer_apps_stays_direct_and_cannot_run_inside_code_mode():
    registry = _registry()
    registry.register(ToolDef(
        "computer_apps",
        "list applications",
        {"type": "object", "properties": {}},
        lambda: "catalog",
        group="computer",
    ))
    agent = FakeAgent(registry)

    direct = asyncio.run(registry.execute("computer_apps", {}))
    nested = asyncio.run(execute_run_code(
        agent,
        "return await tools.computer_apps()",
        "reject nested catalog refresh",
    ))

    assert "computer_apps" in CODE_MODE_DIRECT_TOOLS
    assert direct["output"] == "catalog"
    assert "[ToolDisabled]" in nested
    assert not any(event["name"] == "computer_apps" for event in agent.events)


def test_register_adds_run_code_tool():
    registry = _registry()
    holder: dict = {}

    def agent_getter():
        return holder.get("agent")

    register_run_code_tool(registry, agent_getter)
    assert registry.get("run_code") is not None
    assert registry.get("run_code").sandboxed is True
    holder["agent"] = FakeAgent(_registry())

    async def run_it():
        tool = registry.get("run_code")
        assert tool is not None
        return await tool.fn(code="return await tools.echo(message='z')", description="echo z")

    text = asyncio.run(run_it())
    assert "echo:z" in text


def test_gather_parallelizes_read_tools():
    import time

    registry = ToolRegistry()

    def make(label):
        async def slow() -> str:
            await asyncio.sleep(0.3)
            return label
        return slow

    registry.register(ToolDef("slow_a", "slow a", {"type": "object", "properties": {}}, make("a"), group="core"))
    registry.register(ToolDef("slow_b", "slow b", {"type": "object", "properties": {}}, make("b"), group="core"))
    registry.register(ToolDef("slow_c", "slow c", {"type": "object", "properties": {}}, make("c"), group="core"))
    agent = FakeAgent(registry)

    started = time.monotonic()
    text = asyncio.run(execute_run_code(
        agent,
        "return await tools.gather(tools.slow_a(), tools.slow_b(), tools.slow_c())",
        "gather three slow reads",
    ))
    elapsed = time.monotonic() - started

    # Three 0.3s reads: parallel ≈ 0.3s (+ subprocess startup), serial ≈ 0.9s.
    assert elapsed < 0.7, f"expected parallel execution, took {elapsed:.2f}s"
    assert "a" in text and "b" in text and "c" in text


def test_asyncio_gather_is_available():
    registry = _registry()
    agent = FakeAgent(registry)
    text = asyncio.run(execute_run_code(
        agent,
        "return await asyncio.gather(tools.echo(message='x'), tools.echo(message='y'))",
        "use asyncio.gather",
    ))
    assert "echo:x" in text and "echo:y" in text


def test_failure_output_keeps_logs_before_error():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(
        agent,
        "print('before crash')\nraise ValueError('boom')",
        "print then raise",
    ))
    assert text.index("before crash") < text.index("[run_code] exception")


def test_stdlib_modules_are_available():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(
        agent,
        "import math, re, functools\nreturn {'sqrt': math.sqrt(16), 'digits': re.sub(r'\\D', '', 'a1b2c3'), 'total': functools.reduce(lambda a, b: a + b, [1, 2, 3])}",
        "import math re functools",
    ))
    assert '"sqrt": 4.0' in text
    assert '"digits": "123"' in text
    assert '"total": 6' in text


def test_import_and_type_available_in_program():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(
        agent,
        "import os\nimport json as j\nreturn {'cwd': os.path.basename(os.getcwd()), 't': type(1).__name__, 'j': j.dumps([1, 2])}",
        "use import type json",
    ))
    assert '"t": "int"' in text
    assert '"j": "[1, 2]"' in text


def test_specific_exception_names_are_available():
    agent = FakeAgent(_registry())
    text = asyncio.run(execute_run_code(
        agent,
        "try:\n    raise ValueError('bad value')\nexcept ValueError as e:\n    return f'caught:{e}'",
        "catch ValueError by name",
    ))
    assert "caught:bad value" in text


def test_approval_gated_tools_wait_for_approval_in_run_code():
    asked = []

    async def handler(payload):
        asked.append(payload)
        return "once"

    registry = ToolRegistry()
    registry.register(ToolDef(
        "dangerous_shell", "dangerous", {"type": "object", "properties": {}},
        lambda: "shell-ran", group="code", approval="always",
    ))
    registry.set_approval_handler(handler)
    agent = ExecutingAgent(registry)

    text = asyncio.run(execute_run_code(
        agent,
        "return await tools.dangerous_shell()",
        "gated shell",
    ))
    assert "shell-ran" in text
    assert len(asked) == 1
    assert asked[0]["tool_name"] == "dangerous_shell"


def test_run_code_approval_fails_closed_without_an_answerer():
    registry = ToolRegistry()
    registry.register(ToolDef(
        "dangerous_shell", "dangerous", {"type": "object", "properties": {}},
        lambda: "shell-ran", group="code", approval="always",
    ))
    agent = ExecutingAgent(registry)

    text = asyncio.run(execute_run_code(
        agent,
        "try:\n    await tools.dangerous_shell()\nexcept ToolCallError as e:\n    return str(e)",
        "gated shell with no answerer",
    ))
    assert "[ToolApprovalRequired]" in text


def test_run_code_enforces_nested_tool_budget():
    registry = _registry()
    calls = []

    async def bounded(message: str) -> str:
        calls.append(message)
        return message

    registry.register(ToolDef(
        "bounded",
        "bounded",
        {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        bounded,
        max_calls_per_turn=1,
    ))
    agent = FakeAgent(registry)
    text = asyncio.run(execute_run_code(
        agent,
        """
try:
    await tools.bounded(message='first')
    await tools.bounded(message='second')
except ToolCallError as exc:
    return str(exc)
""",
        "enforce nested per-tool budget",
    ))

    assert "ToolBudgetExhausted" in text
    assert calls == ["first"]
    assert len(agent.events) == 1


def test_run_code_enforces_nested_repeat_guard():
    registry = _registry()
    calls = []

    async def repeatable(message: str) -> str:
        calls.append(message)
        return message

    registry.register(ToolDef(
        "repeatable",
        "repeatable",
        {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        repeatable,
    ))
    agent = FakeAgent(registry)
    text = asyncio.run(execute_run_code(
        agent,
        """
errors = []
for _ in range(3):
    try:
        await tools.repeatable(message='same')
    except ToolCallError as exc:
        errors.append(str(exc))
return errors
""",
        "enforce nested repeat guard",
    ))

    assert "ToolCircuitOpen" in text
    assert calls == ["same", "same"]
    assert len(agent.events) == 2


def test_dynamic_permission_check_waits_only_outside_scope():
    asked = []

    async def handler(payload):
        asked.append(payload)
        return "once"

    registry = ToolRegistry()
    registry.register(ToolDef(
        "scoped_read", "scoped", {"type": "object", "properties": {"path": {"type": "string"}}},
        lambda path="": f"read:{path}", group="core", approval="never",
        permission_check=lambda args: (
            None if args.get("path") == "inside"
            else {"reason": "outside workspace", "kind": "filesystem"}
        ),
        permission_grant=lambda args, request, decision: None,
    ))
    registry.set_approval_handler(handler)
    agent = ExecutingAgent(registry)

    ok = asyncio.run(execute_run_code(agent, "return await tools.scoped_read(path='inside')", "inside"))
    assert "read:inside" in ok
    assert asked == []

    gated = asyncio.run(execute_run_code(agent, "return await tools.scoped_read(path='outside')", "outside"))
    assert "read:outside" in gated
    assert len(asked) == 1
    assert asked[0]["kind"] == "filesystem"


def test_run_code_waits_for_approval_then_executes():
    asked = []

    async def handler(payload):
        asked.append(payload)
        return "once"

    agent = ExecutingAgent(_approval_registry(handler))
    text = asyncio.run(execute_run_code(
        agent,
        "return await tools.guarded_write(path='a.txt')",
        "guarded write",
    ))
    assert "wrote:a.txt" in text
    assert len(asked) == 1
    assert asked[0]["tool_name"] == "guarded_write"


def test_run_code_surfaces_approval_denial_to_program():
    async def handler(payload):
        return "deny"

    agent = ExecutingAgent(_approval_registry(handler))
    text = asyncio.run(execute_run_code(
        agent,
        "try:\n    await tools.guarded_write(path='a.txt')\nexcept ToolCallError as e:\n    return f'caught:{e}'",
        "denied guarded write",
    ))
    assert "caught:" in text
    assert "[ToolApprovalDenied]" in text


def test_run_code_session_approval_covers_later_calls():
    calls = []

    async def handler(payload):
        calls.append(payload)
        return "session"

    agent = ExecutingAgent(_approval_registry(handler))
    text = asyncio.run(execute_run_code(
        agent,
        "await tools.guarded_write(path='a.txt')\nreturn await tools.guarded_write(path='a.txt')",
        "two guarded writes",
    ))
    assert "wrote:a.txt" in text
    assert len(calls) == 1


def test_run_code_cancel_during_approval_propagates():
    asked = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(payload):
        asked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    agent = ExecutingAgent(_approval_registry(handler))

    async def run():
        task = asyncio.create_task(execute_run_code(
            agent,
            "return await tools.guarded_write(path='a.txt')",
            "guarded write",
            timeout=60,
        ))
        await asyncio.wait_for(asked.wait(), timeout=15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert cancelled.is_set()


def test_run_code_tool_budget_is_raised_and_env_overrideable(monkeypatch):
    monkeypatch.delenv("RUN_CODE_MAX_CALLS_PER_TURN", raising=False)
    registry = ToolRegistry()
    register_run_code_tool(registry, lambda: None)
    assert registry.get("run_code").max_calls_per_turn == 32

    monkeypatch.setenv("RUN_CODE_MAX_CALLS_PER_TURN", "24")
    overridden = ToolRegistry()
    register_run_code_tool(overridden, lambda: None)
    assert overridden.get("run_code").max_calls_per_turn == 24


def test_registered_run_code_reports_a_failed_program_as_a_failure():
    registry = _registry()
    agent = ExecutingAgent(registry)
    register_run_code_tool(registry, lambda: agent)

    def run(code: str) -> dict:
        return asyncio.run(registry.execute("run_code", {"code": code, "description": "probe"}))

    ok = run("return await tools.echo(message='hi')")
    assert ok["error"] == "" and "echo:hi" in ok["output"]

    # A failed run used to come back as ordinary output, which the model was shown as a success.
    failed = run("print('step one done')\nawait tools.broken()")
    assert failed["output"] == "" and failed["code"] == "run_code_tool_call_failed"
    assert failed["error"].index("step one done") < failed["error"].index("[run_code] tool call failed:")
    assert "not undone" in failed["recovery_hint"]

    assert run("raise ValueError('nope')")["code"] == "run_code_exception"
    assert run("return asyncio")["code"] == "run_code_invalid_output"

    # The failure keeps the start of the program output and its end, where the reason is.
    big = run("print('head-marker' + 'x' * 40000)\nreturn 1")
    assert big["code"] == "run_code_output_limit"
    assert "head-marker" in big["error"] and len(big["error"]) < 13_000
    assert big["output_truncated"] is True
    assert big["error"].rstrip().endswith("run_code output exceeded 32000 characters")
    # What the failed run reported is kept whole on disk; the error shows a shortened copy.
    saved = Path(big["artifact_path"]).read_text(encoding="utf-8")
    assert saved.startswith("head-marker") and len(saved) > 2 * len(big["error"])


def test_a_tool_recovery_hint_reaches_the_program_and_the_failed_run():
    from agent.runtime.react import ReActAgent
    from agent.runtime.tool_failure import ToolFailure

    registry = ToolRegistry()
    registry.register(ToolDef(
        "guided", "fails with a hint", {"type": "object", "properties": {}},
        lambda: ToolFailure(code="stale", message="hash is stale", retryable=True,
                            recovery_hint="Read the outline again."),
        group="core",
    ))
    agent = ReActAgent("test", SimpleNamespace(), registry, code_mode="code")
    register_run_code_tool(registry, agent_getter=lambda: agent)

    def run(code: str) -> dict:
        async def scenario():
            event, = await agent._execute_tool_calls([{
                "id": "call-1", "name": "run_code",
                "arguments": json.dumps({"code": code, "description": "probe"}),
            }])
            return event
        return asyncio.run(scenario())

    caught = run("try:\n    await tools.guided()\nexcept ToolCallError as e:\n    return str(e)")
    assert not caught["error"]
    assert "hash is stale" in caught["output"] and "Read the outline again." in caught["output"]

    uncaught = run("await tools.guided()")
    assert uncaught["code"] == "run_code_tool_call_failed"
    assert "hash is stale" in uncaught["error"] and "Read the outline again." in uncaught["error"]
