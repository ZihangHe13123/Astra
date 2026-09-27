import asyncio

import pytest

from agent.cli.browser_commands import execute_browser_command
from agent.runtime.browser_backend_router import BrowserBackendRouter
from agent.runtime.browser_control_transport import BrowserControlTransport
from agent.runtime.browser_lifecycle import BrowserLifecycle
from agent.runtime.browser_session import BackendCapabilities, BrowserSessionManager
from agent.runtime.extension_browser_backend import ExtensionBrowserBackend
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.registry import ToolRegistry


class Backend:
    name = "fixture"
    capabilities = BackendCapabilities(True, True, True)

    def __init__(self):
        self.closes = 0
        self.fail_close = False

    async def status(self):
        return True, "fixture"

    async def close_connection(self, tab_id=""):
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("cleanup incomplete")


def setup(tmp_path, backend=None):
    manager = BrowserSessionManager(tmp_path / "browser.db", backend or Backend())
    registry = ToolRegistry()
    register_browser_tools(registry, manager=manager)
    return registry, manager


def test_session_switch_and_same_name_reopen_expire_handles(tmp_path):
    async def scenario():
        registry, manager = setup(tmp_path)
        for _ in range(2):
            opened = await registry.execute("browser_open", {"url": "https://example.com", "extract": False})
            assert not opened["error"]
            session = manager.list_sessions()[0]
            tab = session.current_tab_id
            registry.hooks.dispatch_session_end("same-durable-name", "session_switch")
            stale = await registry.execute("browser_snapshot", {"tab_id": tab})
            assert "expired handle" in (stale["output"] + stale["error"])
            # Durable evidence remains, but confers no live ownership.
            assert manager.get_tab(tab) is not None
            assert manager.backend.closes == _ + 1
        result = await registry.execute("browser_open", {"url": "https://example.org", "extract": False})
        assert not result["error"]
        assert manager.list_sessions()[0].session_id != session.session_id
    asyncio.run(scenario())


def test_release_drains_dispatched_write_and_rejects_old_queued_work(tmp_path):
    async def scenario():
        manager = BrowserSessionManager(tmp_path / "browser.db", Backend())
        runtime = BrowserLifecycle(manager)
        started, finish = asyncio.Event(), asyncio.Event()
        effects = []

        async def write(value):
            effects.append(value)
            started.set()
            await finish.wait()
            return "verified"

        wrapped = runtime.wrap(write)
        active = asyncio.create_task(wrapped("first"))
        await started.wait()
        queued = asyncio.create_task(wrapped("queued"))
        await asyncio.sleep(0)
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        assert runtime.release_state == "releasing" and manager.backend.closes == 0
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert runtime.release_state == "releasing"
        finish.set()
        assert await active == "verified"
        rejected = await queued
        assert rejected.code == "browser_session_ended"
        assert rejected.details["dispatch_state"] == "not_dispatched"
        # A new activation waits for the owned cleanup even after caller cancel.
        assert await wrapped("new") == "verified"
        assert effects == ["first", "new"] and manager.backend.closes == 1
    asyncio.run(scenario())


def test_failed_cleanup_blocks_admission_until_explicit_stop_succeeds(tmp_path):
    async def scenario():
        registry, manager = setup(tmp_path)
        manager.backend.fail_close = True
        registry.hooks.dispatch_session_end("chat", "reset")
        result = await registry.execute("browser_open", {"url": "https://example.com", "extract": False})
        assert result["code"] == "browser_release_failed"
        assert not manager.list_sessions()
        state, error = await execute_browser_command([], registry)
        assert not error and "release_failed" in state
        manager.backend.fail_close = False
        output, error = await execute_browser_command(["stop"], registry)
        assert not error and "released" in output
        result = await registry.execute("browser_open", {"url": "https://example.com", "extract": False})
        assert not result["error"]
    asyncio.run(scenario())


def test_release_rearms_router_and_unlocks_real_endpoint_for_other_owner(tmp_path):
    async def scenario():
        endpoint = tmp_path / "endpoint"
        router = BrowserBackendRouter(Backend(), lambda: ExtensionBrowserBackend(endpoint_dir=endpoint))
        first = router._extension()
        await first.transport.start()
        second = BrowserControlTransport(endpoint)
        with pytest.raises(RuntimeError, match="owned"):
            await second.start()
        await router.release_session()
        await second.start()
        assert second._lock_fd is not None and first.transport._lock_fd is None
        await second.close()
        fresh = router._extension()
        assert fresh is not first and not router.bindings
        await fresh.transport.start()
        await router.close_connection()
    asyncio.run(scenario())


def _file_request_from_other_window(endpoint):
    import json
    import os
    import time
    from agent.runtime.browser_control_storage import open_private
    from agent.runtime.browser_control_transport import RELEASE_REQUEST
    path = endpoint / RELEASE_REQUEST
    with os.fdopen(open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'w') as stream:
        json.dump({"pid": os.getpid() + 1, "label": "跑测试", "at": time.time()}, stream)
    return path


def test_idle_owner_hands_the_endpoint_to_the_window_that_asked(tmp_path):
    async def scenario():
        endpoint = tmp_path / "endpoint"
        router = BrowserBackendRouter(Backend(), lambda: ExtensionBrowserBackend(endpoint_dir=endpoint))
        manager = BrowserSessionManager(tmp_path / "browser.db", router)
        register_browser_tools(ToolRegistry(), manager=manager)
        lifecycle = manager.lifecycle
        await router._extension().transport.start()
        # Nothing asked: the idle owner keeps the channel and its tabs; there is no idle timeout.
        assert await lifecycle.hand_over_if_requested(busy=False) is None
        request = _file_request_from_other_window(endpoint)
        # A busy owner, or one with a browser operation in flight, keeps it too.
        assert await lifecycle.hand_over_if_requested(busy=True) is None
        async with lifecycle._operation_lock:
            assert await lifecycle.hand_over_if_requested(busy=False) is None
        assert router.extension.transport._lock_fd is not None and request.exists()
        handed = await lifecycle.hand_over_if_requested(busy=False)
        assert handed["label"] == "跑测试"
        assert not request.exists() and router.extension is None
        other = BrowserControlTransport(endpoint)
        await other.start()  # the lock is free for the window that asked
        await other.close()
    asyncio.run(scenario())


def test_local_browser_command_rejects_unknown_actions(tmp_path):
    registry, _ = setup(tmp_path)
    output, error = asyncio.run(execute_browser_command(["kill"], registry))
    assert not output and "Usage:" in error


def test_synchronous_session_end_is_pending_until_async_cleanup(tmp_path):
    manager = BrowserSessionManager(tmp_path / "browser.db", Backend())
    runtime = BrowserLifecycle(manager)
    asyncio.run(runtime.stop())
    runtime.end_session("same-name", "reset")
    assert runtime.release_state == "releasing"

    async def open_session():
        runtime.active["session_id"] = "new-live-activation"

    asyncio.run(runtime.wrap(open_session)())
    assert runtime.release_state == "active"
    assert manager.backend.closes == 2


def test_delayed_approval_cannot_retarget_new_sessions_implicit_tab(tmp_path):
    async def scenario():
        registry, manager = setup(tmp_path)
        await registry.execute("browser_open", {"url": "https://old.example.com", "extract": False})
        asked, approve = asyncio.Event(), asyncio.Event()

        async def approval(_request):
            asked.set()
            await approve.wait()
            return "once"

        registry.set_approval_handler(approval)
        old_call = asyncio.create_task(registry.execute("browser_click", {"selector": "#save"}))
        await asked.wait()
        registry.hooks.dispatch_session_end("old-chat", "session_switch")
        # Open creates a new current tab. The old call omitted tab_id, but must
        # still fail before it can resolve to this new page or request any input.
        await registry.execute("browser_open", {"url": "https://new.example.com", "extract": False})
        approve.set()
        result = await old_call
        assert result["code"] == "browser_session_ended"
        assert result["details"]["dispatch_state"] == "not_dispatched"
        assert manager.backend.closes == 1
    asyncio.run(scenario())
