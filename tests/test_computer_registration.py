import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent.runtime.computer_backend import ComputerSessionManager
from agent.runtime.tools.registry import ToolRegistry

COMPUTER_TOOLS = {
    "computer_status",
    "computer_apps",
    "computer_get_app_state",
    "computer_focus",
    "computer_snapshot",
    "computer_act",
    "computer_handoff",
    "computer_resume",
    "computer_close",
}


class _FakeManager:
    def __init__(self, status_result=None, status_error=None):
        self.session_id = "session-test"
        self.session_dir = Path("/private/session-test")
        self.target = None
        self.handed_off = False
        self.closed = False
        self.grants = set()
        self.status_result = status_result or {
            "supported": True,
            "permissions": {"accessibility": True, "screen_recording": True},
        }
        self.status_error = status_error
        self.status_calls = 0
        self.close_calls = 0

    async def status(self):
        self.status_calls += 1
        if self.status_error is not None:
            raise self.status_error
        return self.status_result

    async def close(self):
        self.close_calls += 1
        self.closed = True

    def close_local_state(self):
        self.closed = True
        self.grants.clear()

    async def close_backend(self):
        await self.close()


class _ControlledManager(_FakeManager):
    def __init__(self, *, block_close=False, close_failures=0, local_close_failures=0):
        super().__init__()
        self.block_close = block_close
        self.close_failures = close_failures
        self.local_close_failures = local_close_failures
        self.local_close_calls = 0
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        if not block_close:
            self.allow_close.set()

    async def close(self):
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("controlled close failure")
        self.closed = True

    def close_local_state(self):
        self.local_close_calls += 1
        self.grants.clear()
        if self.local_close_failures:
            self.local_close_failures -= 1
            raise OSError("controlled local cleanup failure")


def _register(monkeypatch, manager, *, capabilities=lambda: {"tools", "vision"}, cache_root=None):
    return _register_factory(
        monkeypatch,
        lambda **_kwargs: manager,
        capabilities=capabilities,
        cache_root=cache_root,
    )


def _register_factory(monkeypatch, factory, *, capabilities=lambda: {"tools", "vision"}, cache_root=None):
    from agent.runtime.tools import computer

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(computer, "_new_macos_computer_manager", factory)
    registry = ToolRegistry()
    runtime = computer.register_local_computer_runtime(
        registry,
        helper_path="/missing/is-not-validated-during-registration",
        cache_root=cache_root,
        model_capabilities=capabilities,
    )
    assert runtime is not None
    return registry, runtime


def test_darwin_registers_lazily_without_creating_cache_or_manager(monkeypatch, tmp_path):
    manager = _FakeManager()
    cache_root = tmp_path / "computer-cache"
    registry, runtime = _register(monkeypatch, manager, cache_root=cache_root)

    assert runtime is not None
    assert COMPUTER_TOOLS.issubset(registry.tool_names)
    assert runtime.manager_started is False
    assert runtime.helper_started is False
    assert not cache_root.exists()


def test_status_command_reads_local_runtime_cache_without_activation(monkeypatch, tmp_path):
    from agent.cli.computer_commands import execute_computer_command

    manager = _FakeManager()
    cache_root = tmp_path / "computer-cache"
    _registry, runtime = _register(monkeypatch, manager, cache_root=cache_root)

    output, error = asyncio.run(execute_computer_command(["status"], runtime, platform_name="darwin"))

    assert error == ""
    assert "probed: no" in output
    assert runtime.manager_started is False
    assert runtime.helper_started is False
    assert manager.status_calls == 0
    assert not cache_root.exists()


def test_linux_does_not_import_macos_runtime_or_register_tools(monkeypatch):
    from agent.runtime.tools import computer

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delitem(sys.modules, "agent.runtime.macos_computer", raising=False)
    registry = ToolRegistry()

    assert computer.register_local_computer_runtime(registry) is None
    assert COMPUTER_TOOLS.isdisjoint(registry.tool_names)
    assert "agent.runtime.macos_computer" not in sys.modules


def test_registration_is_idempotent_for_one_surface(monkeypatch):
    manager = _FakeManager()
    registry, first = _register(monkeypatch, manager)
    from agent.runtime.tools.computer import register_local_computer_runtime

    revision = registry.schema_revision
    second = register_local_computer_runtime(registry, model_capabilities=lambda: {"tools", "vision"})

    assert second is first
    assert registry.schema_revision == revision
    assert len(COMPUTER_TOOLS & set(registry.tool_names)) == 9


def test_model_capability_gate_reads_current_model_after_switch(monkeypatch):
    manager = _FakeManager()
    active = {"capabilities": frozenset({"tools"})}
    registry, runtime = _register(monkeypatch, manager, capabilities=lambda: active["capabilities"])

    first = asyncio.run(registry.execute("computer_status", {}))
    assert runtime.manager_started is False
    active["capabilities"] = frozenset({"tools", "vision"})
    second = asyncio.run(registry.execute("computer_status", {}))

    assert first["code"] == "computer_capability_unavailable"
    assert second["error"] == ""
    assert manager.status_calls == 1
    assert runtime.manager_started is True


def test_unused_session_end_does_not_disable_future_activation(monkeypatch):
    manager = _FakeManager()
    registry, runtime = _register(monkeypatch, manager)

    registry.hooks.dispatch_session_end("unused", "reset")
    result = asyncio.run(registry.execute("computer_status", {}))

    assert result["error"] == ""
    assert runtime.manager_started is True
    assert manager.status_calls == 1


def test_active_session_end_rotates_to_a_fresh_manager(monkeypatch):
    managers = [_FakeManager(), _FakeManager()]
    created = []

    def factory(**_kwargs):
        manager = managers[len(created)]
        created.append(manager)
        return manager

    registry, runtime = _register_factory(monkeypatch, factory)

    async def scenario():
        assert (await registry.execute("computer_status", {}))["error"] == ""
        registry.hooks.dispatch_session_end("first", "session_switch")
        assert (await registry.execute("computer_status", {}))["error"] == ""
        registry.hooks.dispatch_session_end("second", "reset")
        await runtime.wait_for_teardown()

    asyncio.run(scenario())

    assert created == managers
    assert managers[0].close_calls == 1
    assert managers[1].close_calls == 1
    assert runtime.manager_started is False
    assert runtime.capability_status()[0] == "configured"


def test_running_loop_cleanup_blocks_new_manager_until_old_helper_closes(monkeypatch):
    first = _ControlledManager(block_close=True)
    second = _FakeManager()
    managers = iter([first, second])
    created = []

    def factory(**_kwargs):
        manager = next(managers)
        created.append(manager)
        return manager

    registry, runtime = _register_factory(monkeypatch, factory)

    async def scenario():
        await registry.execute("computer_status", {})
        registry.hooks.dispatch_session_end("first", "session_switch")
        await first.close_started.wait()
        next_status = asyncio.create_task(registry.execute("computer_status", {}))
        await asyncio.sleep(0)
        assert created == [first]
        first.allow_close.set()
        assert (await next_status)["error"] == ""
        await runtime.shutdown()

    asyncio.run(scenario())

    assert created == [first, second]
    assert first.closed is True


def test_cancelled_waiter_does_not_cancel_teardown_or_overlap_helpers(monkeypatch):
    first = _ControlledManager(block_close=True)
    second = _FakeManager()
    managers = iter([first, second])
    registry, runtime = _register_factory(monkeypatch, lambda **_kwargs: next(managers))

    async def scenario():
        await registry.execute("computer_status", {})
        registry.hooks.dispatch_session_end("first", "session_switch")
        await first.close_started.wait()
        waiter = asyncio.create_task(registry.execute("computer_status", {}))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert first.closed is False
        first.allow_close.set()
        assert (await registry.execute("computer_status", {}))["error"] == ""
        await runtime.shutdown()

    asyncio.run(scenario())

    assert first.close_calls == 1
    assert second.status_calls == 1


def test_shutdown_waits_for_helper_teardown_before_returning(monkeypatch):
    manager = _ControlledManager(block_close=True)
    registry, runtime = _register(monkeypatch, manager)

    async def scenario():
        await registry.execute("computer_status", {})
        shutdown = asyncio.create_task(runtime.shutdown())
        await manager.close_started.wait()
        assert shutdown.done() is False
        manager.allow_close.set()
        await shutdown

    asyncio.run(scenario())

    assert manager.closed is True
    assert manager.close_calls == 1


def test_final_shutdown_barrier_prevents_waiting_call_from_creating_ghost_manager(monkeypatch):
    first = _ControlledManager(block_close=True)
    second = _FakeManager()
    managers = iter([first, second])
    created = []

    def factory(**_kwargs):
        manager = next(managers)
        created.append(manager)
        return manager

    registry, runtime = _register_factory(monkeypatch, factory)

    async def scenario():
        await registry.execute("computer_status", {})
        shutdown = asyncio.create_task(runtime.shutdown())
        await first.close_started.wait()
        waiting_status = asyncio.create_task(registry.execute("computer_status", {}))
        await asyncio.sleep(0)
        assert created == [first]
        first.allow_close.set()
        assert await shutdown is True
        blocked = await waiting_status
        assert blocked["code"] == "computer_runtime_shutdown"

    asyncio.run(scenario())

    assert created == [first]


def test_waiter_from_old_generation_cannot_activate_after_second_session_boundary(monkeypatch):
    first = _ControlledManager(block_close=True)
    second = _FakeManager()
    managers = iter([first, second])
    registry, runtime = _register_factory(monkeypatch, lambda **_kwargs: next(managers))

    async def scenario():
        await registry.execute("computer_status", {})
        registry.hooks.dispatch_session_end("first", "session_switch")
        await first.close_started.wait()
        old_waiter = asyncio.create_task(registry.execute("computer_status", {}))
        for _ in range(10):
            if runtime._rotation_lock.locked():
                break
            await asyncio.sleep(0)
        assert runtime._rotation_lock.locked()
        registry.hooks.dispatch_session_end("second", "reset")
        first.allow_close.set()
        stale = await old_waiter
        assert stale["code"] == "computer_session_changed"
        assert (await registry.execute("computer_status", {}))["error"] == ""
        await runtime.shutdown()

    asyncio.run(scenario())

    assert second.status_calls == 1


def test_teardown_failure_is_degraded_fail_closed_then_retryable(monkeypatch):
    first = _ControlledManager(close_failures=1)
    second = _FakeManager()
    managers = iter([first, second])
    registry, runtime = _register_factory(monkeypatch, lambda **_kwargs: next(managers))

    async def scenario():
        await registry.execute("computer_status", {})
        registry.hooks.dispatch_session_end("first", "session_switch")
        await asyncio.sleep(0)
        blocked = await registry.execute("computer_status", {})
        assert blocked["code"] == "computer_teardown_failed"
        assert runtime.capability_status()[0] == "degraded"
        recovered = await registry.execute("computer_status", {})
        assert recovered["error"] == ""
        await runtime.shutdown()

    asyncio.run(scenario())

    assert first.close_calls == 2
    assert second.status_calls == 1


def test_local_cleanup_failure_still_terminates_helper_and_remains_retryable(monkeypatch):
    first = _ControlledManager(local_close_failures=2)
    second = _FakeManager()
    managers = iter([first, second])
    registry, runtime = _register_factory(monkeypatch, lambda **_kwargs: next(managers))

    async def scenario():
        await registry.execute("computer_status", {})
        registry.hooks.dispatch_session_end("first", "session_switch")
        await asyncio.sleep(0)
        blocked = await registry.execute("computer_status", {})
        assert blocked["code"] == "computer_teardown_failed"
        assert runtime.capability_status()[0] == "degraded"
        assert first.close_calls >= 1
        retried = await registry.execute("computer_status", {})
        assert retried["code"] == "computer_teardown_failed"
        recovered = await registry.execute("computer_status", {})
        assert recovered["error"] == ""
        await runtime.shutdown()

    asyncio.run(scenario())

    assert first.local_close_calls >= 3
    assert first.close_calls >= 1


def test_persistent_teardown_failure_is_recorded_without_escaping_final_shutdown(monkeypatch):
    manager = _ControlledManager(close_failures=10, local_close_failures=10)
    registry, runtime = _register(monkeypatch, manager)

    async def scenario():
        await registry.execute("computer_status", {})
        assert await runtime.shutdown() is False

    asyncio.run(scenario())

    assert manager.close_calls == 2
    assert manager.local_close_calls == 0
    assert runtime.capability_status()[0] == "degraded"


def test_three_consecutive_sessions_never_reuse_managers(monkeypatch):
    managers = iter([_FakeManager(), _FakeManager(), _FakeManager()])
    created = []

    def factory(**_kwargs):
        manager = next(managers)
        created.append(manager)
        return manager

    registry, runtime = _register_factory(monkeypatch, factory)

    async def scenario():
        for index in range(3):
            assert (await registry.execute("computer_status", {}))["error"] == ""
            registry.hooks.dispatch_session_end(str(index), "session_switch")
        await runtime.wait_for_teardown()

    asyncio.run(scenario())

    assert len(created) == 3
    assert all(manager.close_calls == 1 for manager in created)


@pytest.mark.parametrize(
    ("status_result", "expected_detail"),
    [
        (
            {"supported": True, "permissions": {"accessibility": False, "screen_recording": True}},
            "Accessibility",
        ),
        (
            {"supported": True, "permissions": {"accessibility": True, "screen_recording": False}},
            "Screen Recording",
        ),
    ],
)
def test_explicit_probe_records_permission_degradation(monkeypatch, status_result, expected_detail):
    manager = _FakeManager(status_result=status_result)
    _registry, runtime = _register(monkeypatch, manager)

    before = runtime.capability_status()
    after = asyncio.run(runtime.probe())

    assert before == ("configured", "Native helper is configured but has not been probed.")
    assert after[0] == "degraded"
    assert expected_detail in after[1]
    assert manager.status_calls == 1


@pytest.mark.parametrize(
    ("manager", "expected_state", "expected_detail"),
    [
        (_FakeManager(), "available", "required macOS permissions are available"),
        (
            _FakeManager(status_error=RuntimeError("/Users/private/helper executable is unavailable")),
            "degraded",
            "helper is unavailable",
        ),
        (
            _FakeManager(status_error=RuntimeError("malformed protocol response at /Users/private")),
            "degraded",
            "protocol check failed",
        ),
    ],
)
def test_probe_reports_bounded_helper_and_protocol_states(
    monkeypatch,
    manager,
    expected_state,
    expected_detail,
):
    _registry, runtime = _register(monkeypatch, manager)

    state, detail = asyncio.run(runtime.probe())

    assert state == expected_state
    assert expected_detail in detail
    assert "/Users/" not in detail


def test_local_surfaces_register_after_image_tools_and_api_surface_does_not():
    root = Path(__file__).resolve().parents[1] / "agent" / "cli"
    for filename, marker in (
        ("main.py", "async def _async_init"),
        ("backend.py", "async def main"),
    ):
        source = (root / filename).read_text(encoding="utf-8")
        factory = source[source.index(marker):]
        assert factory.index("register_image_tools(") < factory.index("register_local_computer_runtime(")

    api_source = (root / "api_server.py").read_text(encoding="utf-8")
    assert "register_local_computer_runtime" not in api_source


def test_local_shutdown_paths_await_computer_teardown():
    root = Path(__file__).resolve().parents[1] / "agent" / "cli"
    expected_followups = {
        "main.py": ("agent.context.save()", "await stop_browser.fn()", "agent.close_external_memory()", "mcp_manager.close()", "await close()"),
        "backend.py": ("await agent.context.save_async()", "await stop_browser.fn()", "agent.close_external_memory()", "mcp_manager.close()", "await close()"),
    }
    for filename, followups in expected_followups.items():
        source = (root / filename).read_text(encoding="utf-8")
        assert "await computer_runtime.shutdown()" in source
        shutdown = source.index("await computer_runtime.shutdown()")
        assert source.rfind("try:", 0, shutdown) > source.rfind("finally:", 0, shutdown)
        assert source.find("except Exception as exc:", shutdown) > shutdown
        assert all(source.find(followup, shutdown) > shutdown for followup in followups)


def test_classic_cli_continues_all_cleanup_after_computer_shutdown_failure(
    monkeypatch,
    tmp_path,
):
    from agent.cli import main as main_module
    from agent.runtime.context import AgentContext
    from agent.runtime.llm import LLMConfig

    events = []

    class FailingComputerRuntime:
        async def shutdown(self):
            events.append("computer")
            raise RuntimeError("persistent controlled teardown failure")

    class FakeBrowserBackend:
        def __init__(self, _chrome_path):
            pass

        async def close_connection(self):
            events.append("browser")

    class FakeMcpManager:
        async def close(self):
            events.append("mcp")

    async def fake_initialize_mcp(*_args, **_kwargs):
        return FakeMcpManager()

    async def fake_external_memory_close(_agent):
        events.append("external_memory")

    async def fake_sandbox_close(_sandbox):
        events.append("sandbox")

    def fake_context_save(_context):
        events.append("context")

    for name, value in {
        "AGENT_SESSION_DIR": tmp_path / "sessions",
        "AGENT_TASK_DB": tmp_path / "tasks.db",
        "AGENT_MEMORY_PATH": tmp_path / "memory.db",
        "AGENT_LEARNING_PATH": tmp_path / "learning.db",
        "AGENT_SKILLS_PATH": tmp_path / "skills",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("SANDBOX_DOCKER", "false")
    monkeypatch.setattr(main_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(main_module, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(main_module, "CdpBrowserBackend", FakeBrowserBackend)
    monkeypatch.setattr(
        main_module,
        "register_local_computer_runtime",
        lambda *_args, **_kwargs: FailingComputerRuntime(),
    )
    monkeypatch.setattr(main_module, "initialize_mcp_tools", fake_initialize_mcp)
    monkeypatch.setattr(main_module, "cli_loop", lambda *_args: asyncio.sleep(0))
    monkeypatch.setattr(main_module.ReActAgent, "close_external_memory", fake_external_memory_close)
    monkeypatch.setattr(main_module.SandboxRouter, "close", fake_sandbox_close)
    monkeypatch.setattr(AgentContext, "save", fake_context_save)

    asyncio.run(
        main_module._async_init(
            LLMConfig(
                model="computer-test-model",
                api_key="local",
                base_url="http://127.0.0.1:9/v1",
                capabilities=frozenset({"vision", "tools"}),
            ),
            1,
            str(tmp_path),
        )
    )

    assert events == ["computer", "context", "browser", "external_memory", "mcp", "sandbox"]


def test_computer_close_returns_the_closed_native_session_id(monkeypatch):
    manager = _FakeManager()
    registry, runtime = _register(monkeypatch, manager)

    async def scenario():
        await registry.execute("computer_status", {})
        result = await registry.execute("computer_close", {})
        await runtime.shutdown()
        return result

    result = asyncio.run(scenario())

    assert result["error"] == ""
    assert '"session_id":"session-test"' in result["output"]


@pytest.mark.skipif(os.name != 'posix', reason='real ComputerSessionManager requires POSIX held-directory cache leases')
def test_computer_close_retries_transient_backend_failure_with_real_session_manager(
    monkeypatch,
    tmp_path,
):
    class TransientCloseBackend:
        def __init__(self):
            self.close_calls = 0

        async def status(self):
            return {
                "supported": True,
                "permissions": {"accessibility": True, "screen_recording": True},
            }

        async def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("transient helper close failure")

    backend = TransientCloseBackend()
    managers = []

    def factory(**kwargs):
        manager = ComputerSessionManager(backend, cache_root=kwargs["cache_root"])
        managers.append(manager)
        return manager

    registry, runtime = _register_factory(
        monkeypatch,
        factory,
        cache_root=tmp_path / "computer-cache",
    )

    async def scenario():
        assert (await registry.execute("computer_status", {}))["error"] == ""
        manager = managers[0]
        session_dir = manager.session_dir
        try:
            result = await registry.execute("computer_close", {})
        finally:
            if not manager.closed:
                manager._backend_close_task = None
            await runtime.shutdown()
        return result, manager, session_dir

    result, manager, session_dir = asyncio.run(scenario())

    assert result["error"] == ""
    assert backend.close_calls == 2
    assert manager.closed is True
    assert not session_dir.exists()
    assert runtime._retired_manager is None
