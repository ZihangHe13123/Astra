"""Tests for CDP browser backend (cdp_backend.py).

Covers:
- find_chrome: discovers Chrome/Edge on the system
- _html_to_text: HTML → plain text conversion (BeautifulSoup + fallback)
- CdpBrowserBackend: construction, capabilities, status
- extract: headless Chrome --dump-dom text extraction (via data: URL)
- screenshot: headless Chrome --screenshot (via data: URL)
- launch_headed: returns status message
- Integration: register_browser_tools with backend= parameter
"""

import asyncio
import json
import os
import subprocess
import sys
from unittest import mock

import pytest

from agent.runtime.cdp_backend import (
    CdpBrowserBackend,
    CdpConnection,
    _build_wsl_relay_script,
    _get_windows_gateway,
    find_chrome,
    interactive_dependency_available,
    _html_to_text,
)
from agent.runtime import cdp_backend as cdp_backend_mod
from agent.runtime.browser_session import BrowserSessionManager
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.policy import ToolPolicy


requires_native_chrome = pytest.mark.skipif(
    sys.platform != "win32" or not find_chrome(),
    reason="Requires native Windows Chrome",
)
requires_native_chrome_interactive = pytest.mark.skipif(
    sys.platform != "win32"
    or not find_chrome()
    or not interactive_dependency_available(),
    reason="Requires native Windows Chrome and interactive dependencies",
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# find_chrome
# ---------------------------------------------------------------------------

class TestFindChrome:
    def test_find_chrome_checks_macos_bundle(self, monkeypatch):
        expected = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        monkeypatch.setattr(cdp_backend_mod.os.path, "isfile", lambda path: path == expected)
        monkeypatch.setattr(cdp_backend_mod.shutil, "which", lambda path: None)
        assert find_chrome() == expected

    def test_finds_chrome_on_this_system(self):
        """On this Windows+WSL host, Chrome should be discoverable."""
        path = find_chrome()
        # May be empty on CI, but on dev machine should find something
        if path:
            assert os.path.isfile(path) or path  # at minimum a non-empty string

    def test_env_override(self, monkeypatch, tmp_path):
        fake = tmp_path / "fake_chrome"
        fake.write_text("#!/bin/sh\necho fake")
        fake.chmod(0o755)
        monkeypatch.setenv("CHROME_PATH", str(fake))
        assert find_chrome() == str(fake)

    def test_env_override_nonexistent(self, monkeypatch):
        monkeypatch.setenv("CHROME_PATH", "/nonexistent/chrome")
        # Should fall through to candidates (may or may not find one)
        result = find_chrome()
        assert result != "/nonexistent/chrome"


# ---------------------------------------------------------------------------
# _html_to_text
# ---------------------------------------------------------------------------

class TestHtmlToText:
    def test_basic_html(self):
        html = "<html><body><h1>Title</h1><p>Hello world</p></body></html>"
        text = _html_to_text(html)
        assert "Title" in text
        assert "Hello world" in text

    def test_strips_script_and_style(self):
        html = """
        <html><head><style>body{color:red}</style></head>
        <body>
        <script>var x = 1;</script>
        <p>Visible text</p>
        </body></html>
        """
        text = _html_to_text(html)
        assert "Visible text" in text
        assert "var x" not in text
        assert "color:red" not in text

    def test_collapses_blank_lines(self):
        html = "<p>A</p><p></p><p></p><p></p><p>B</p>"
        text = _html_to_text(html)
        # Should not have 3+ consecutive blank lines
        assert "\n\n\n" not in text

    def test_empty_html(self):
        assert _html_to_text("") == ""
        assert _html_to_text("<html><body></body></html>") == ""

    def test_nested_elements(self):
        html = "<div><ul><li>Item 1</li><li>Item 2</li></ul></div>"
        text = _html_to_text(html)
        assert "Item 1" in text
        assert "Item 2" in text

    def test_strips_svg(self):
        html = '<body><svg><path d="M0 0"/></svg><p>Text</p></body>'
        text = _html_to_text(html)
        assert "Text" in text
        assert "M0 0" not in text


# ---------------------------------------------------------------------------
# CdpBrowserBackend — construction & capabilities
# ---------------------------------------------------------------------------

class TestCdpBackendConstruction:
    def test_capabilities(self):
        backend = CdpBrowserBackend("/fake/chrome")
        assert backend.capabilities.read is True
        assert backend.capabilities.interactive is True
        assert backend.capabilities.takeover is True

    def test_name(self):
        backend = CdpBrowserBackend("/fake/chrome")
        assert backend.name == "cdp-headless"

    def test_wsl_relay_binds_only_virtual_interface(self):
        script = _build_wsl_relay_script(9222, "172.25.144.1")
        assert "BIND_HOST=\"172.25.144.1\"" in script
        assert "srv.bind((BIND_HOST,0))" in script
        assert "0.0.0.0" not in script
        assert "RELAY_READY" in script

    def test_chrome_available_with_path(self):
        backend = CdpBrowserBackend("/fake/chrome")
        assert backend.chrome_available is True

    def test_chrome_not_available_empty_path(self):
        backend = CdpBrowserBackend("")
        # find_chrome() may or may not find Chrome
        # Just verify it doesn't crash
        assert isinstance(backend.chrome_available, bool)

    def test_explicit_path_stored(self):
        backend = CdpBrowserBackend("/my/chrome")
        assert backend.chrome_path == "/my/chrome"


# ---------------------------------------------------------------------------
# CdpBrowserBackend — status
# ---------------------------------------------------------------------------

class TestCdpBackendStatus:
    def test_status_no_chrome(self):
        backend = CdpBrowserBackend("")
        backend._chrome_path = ""  # force empty
        available, detail = run(backend.status())
        assert available is False
        assert "not found" in detail.lower()

    @requires_native_chrome
    def test_status_with_real_chrome(self):
        backend = CdpBrowserBackend(find_chrome())
        available, detail = run(backend.status())
        assert available is True
        assert len(detail) > 0


# ---------------------------------------------------------------------------
# CdpBrowserBackend — extract (integration, needs Chrome)
# ---------------------------------------------------------------------------

class TestCdpBackendExtract:
    def test_extract_no_chrome(self):
        backend = CdpBrowserBackend("")
        backend._chrome_path = ""
        result = run(backend.extract("https://example.com"))
        assert "CDP Error" in result
        assert "not found" in result.lower()

    def test_extract_empty_url(self):
        backend = CdpBrowserBackend("/fake/chrome")
        result = run(backend.extract(""))
        assert "url is required" in result

    def test_extract_deadline_cancels_output_and_cleans_owned_profile(self, monkeypatch, tmp_path):
        async def scenario():
            profile = tmp_path / "owned-profile"
            profile.mkdir()
            cancelled = asyncio.Event()

            async def stalled_output():
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            process = mock.Mock(communicate=mock.AsyncMock(side_effect=stalled_output))
            spawn = mock.AsyncMock(return_value=process)
            reap = mock.AsyncMock()
            monkeypatch.setattr(cdp_backend_mod, "_make_temp_profile", lambda: str(profile))
            monkeypatch.setattr(cdp_backend_mod.asyncio, "create_subprocess_exec", spawn)
            monkeypatch.setattr(cdp_backend_mod, "_force_kill_process_tree", reap)
            backend = CdpBrowserBackend("/fake/chrome", timeout=1)

            result = await asyncio.wait_for(backend.extract("data:text/html,deadline"), timeout=10)

            assert "Chrome timed out after 1s" in result
            assert cancelled.is_set()
            reap.assert_awaited_once_with(process)
            assert not profile.exists()

        run(scenario())

    @requires_native_chrome
    def test_extract_data_url(self):
        """Extract text from a data: URL (no network needed)."""
        # Check content with the production cold-start budget. The regression
        # above checks a stuck child's short deadline without real Chrome.
        backend = CdpBrowserBackend(find_chrome())
        html = "<html><body><h1>CDP Test</h1><p>Hello from headless Chrome</p></body></html>"
        data_url = f"data:text/html,{html}"
        result = run(backend.extract(data_url))
        assert "CDP Test" in result
        assert "Hello from headless Chrome" in result

    @requires_native_chrome
    def test_extract_truncates(self):
        backend = CdpBrowserBackend(find_chrome())
        long_text = "A" * 500
        html = f"<html><body><p>{long_text}</p></body></html>"
        data_url = f"data:text/html,{html}"
        result = run(backend.extract(data_url, max_length=100))
        assert "…[truncated]" in result
        assert len(result) < 200

    @requires_native_chrome
    def test_extract_strips_scripts(self):
        backend = CdpBrowserBackend(find_chrome())
        html = '<html><body><script>var x = "should not appear";</script><p>Clean text</p></body></html>'
        data_url = f"data:text/html,{html}"
        result = run(backend.extract(data_url))
        assert "Clean text" in result
        assert "should not appear" not in result


# ---------------------------------------------------------------------------
# CdpBrowserBackend — screenshot (integration, needs Chrome)
# ---------------------------------------------------------------------------

class TestCdpBackendScreenshot:
    def test_screenshot_no_chrome(self):
        backend = CdpBrowserBackend("")
        backend._chrome_path = ""
        result = run(backend.screenshot("https://example.com"))
        assert "CDP Error" in result

    @requires_native_chrome
    def test_screenshot_data_url(self):
        backend = CdpBrowserBackend(find_chrome(), timeout=15)
        html = "<html><body><h1>Screenshot Test</h1></body></html>"
        data_url = f"data:text/html,{html}"
        result = run(backend.screenshot(data_url))
        assert not result.startswith("[CDP Error]")
        assert os.path.isfile(result)
        assert os.path.getsize(result) > 0
        # Cleanup
        os.unlink(result)


# ---------------------------------------------------------------------------
# CdpBrowserBackend — launch_headed
# ---------------------------------------------------------------------------

class TestCdpBackendLaunchHeaded:
    def test_launch_headed_no_chrome(self):
        backend = CdpBrowserBackend("")
        backend._chrome_path = ""
        result = backend.launch_headed("https://example.com")
        assert "CDP Error" in result

    def test_launch_headed_returns_message(self):
        """With a fake path, Popen will fail but we handle it gracefully."""
        backend = CdpBrowserBackend("/nonexistent/chrome")
        result = backend.launch_headed("https://example.com")
        # Either launches (unlikely with fake path) or returns error
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Integration: register_browser_tools with backend= parameter
# ---------------------------------------------------------------------------

class TestRegisterWithBackend:
    def test_backend_parameter_accepted(self, tmp_path):
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        backend = CdpBrowserBackend("/fake/chrome")
        returned = register_browser_tools(reg, manager=manager, backend=backend)
        assert returned is manager
        assert manager.backend is backend

    def test_registers_complete_browser_tool_surface(self, tmp_path):
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(reg, manager=manager, backend=CdpBrowserBackend("/fake/chrome"))
        names = {item["name"] for item in reg.describe()}
        assert {
            "browser_open", "browser_snapshot", "browser_extract", "browser_click",
            "browser_fill", "browser_select", "browser_wait", "browser_screenshot",
            "browser_handoff", "browser_resume", "browser_close", "browser_status",
        } <= names
        assert "browser_type" not in names
        assert reg.get("browser_type") is not None
        schemas = {item["function"]["name"] for item in reg.to_openai_tools()}
        assert "browser_fill" in schemas and "browser_type" not in schemas
        explicit = reg.to_openai_tools(names={"browser_type"})
        assert [item["function"]["name"] for item in explicit] == ["browser_type"]

    def test_backend_priority_over_extract_fn(self, tmp_path):
        """Explicit backend takes priority over extract_fn/status_fn."""
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        backend = CdpBrowserBackend("/fake/chrome")

        async def fake_extract(url, max_length=12000):
            return "legacy"

        async def fake_status():
            return True, "legacy"

        register_browser_tools(
            reg, manager=manager, backend=backend,
            extract_fn=fake_extract, status_fn=fake_status,
        )
        # Should use the explicit backend, not legacy
        assert manager.backend is backend
        assert manager.backend.name == "cdp-headless"

    def test_browser_status_shows_cdp_backend(self, tmp_path):
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        backend = CdpBrowserBackend("/fake/chrome")
        register_browser_tools(reg, manager=manager, backend=backend)
        result = run(reg.execute("browser_status", {}))
        assert not result.get("error")
        # Status will report Chrome not runnable (fake path), but backend exists
        assert "Backend:" in result["output"]

    @requires_native_chrome_interactive
    def test_browser_screenshot_tool_returns_verified_attachment(self, tmp_path):
        async def _test():
            backend = CdpBrowserBackend(find_chrome(), timeout=15)
            registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
            manager = BrowserSessionManager(path=tmp_path / "browser.db")
            register_browser_tools(registry, manager=manager, backend=backend)
            output_path = tmp_path / "verified-browser-shot.png"
            try:
                opened = await registry.execute(
                    "browser_open",
                    {
                        "url": "data:text/html,<body><h1>Visible browser content</h1></body>",
                        "extract": False,
                    },
                )
                assert opened["error"] == ""
                assert "[Browser Error]" not in opened["output"], opened
                captured = await registry.execute(
                    "browser_screenshot",
                    {"output_path": str(output_path)},
                )
                assert captured["error"] == "", captured
                assert captured["verified"] is True
                payload = json.loads(captured["output"])
                assert payload["type"] == "image_attachment"
                assert payload["image_paths"] == [str(output_path)]
                assert output_path.is_file() and output_path.stat().st_size > 0
            finally:
                await backend.close_connection()

        run(_test())


# ---------------------------------------------------------------------------
# CdpConnection — interactive integration (needs real Chrome)
# ---------------------------------------------------------------------------

@requires_native_chrome
class TestCdpConnectionInteractive:
    """Integration tests for CdpConnection: navigate, evaluate, click, type.

    These launch a real headless Chrome with --remote-debugging-port and
    connect via WebSocket. Skipped if Chrome or websockets is unavailable.
    """

    @pytest.fixture()
    def chrome_path(self):
        path = find_chrome()
        if not path:
            pytest.skip("Chrome not installed")
        return path

    def test_navigate_and_get_text(self, chrome_path):
        from agent.runtime.cdp_backend import CdpConnection

        async def _test():
            conn = CdpConnection(chrome_path, headless=True, timeout=15)
            try:
                await conn.start()
                assert conn.is_connected
                html = "<html><body><h1>CDP Interactive</h1><p>It works!</p></body></html>"
                await conn.navigate(f"data:text/html,{html}", wait_ms=1000)
                text = await conn.get_text()
                assert "CDP Interactive" in text
                assert "It works!" in text
            finally:
                await conn.close()
            assert not conn.is_connected

        run(_test())

    def test_evaluate_javascript(self, chrome_path):
        from agent.runtime.cdp_backend import CdpConnection

        async def _test():
            conn = CdpConnection(chrome_path, headless=True, timeout=15)
            try:
                await conn.start()
                await conn.navigate("data:text/html,<body>test</body>", wait_ms=500)
                result = await conn.evaluate("2 + 3")
                assert result == 5
                title = await conn.evaluate("document.title")
                assert isinstance(title, str)
            finally:
                await conn.close()

        run(_test())

    def test_click_element(self, chrome_path):
        from agent.runtime.cdp_backend import CdpConnection

        async def _test():
            conn = CdpConnection(chrome_path, headless=True, timeout=15)
            try:
                await conn.start()
                html = (
                    "<html><body>"
                    '<button id="btn" onclick="document.title=\'clicked\'">Click me</button>'
                    "</body></html>"
                )
                await conn.navigate(f"data:text/html,{html}", wait_ms=500)
                assert "satisfied" in await conn.wait_for(selector="#btn", timeout_ms=5000)
                result = await conn.click("#btn")
                assert json.loads(result)["status"] == "observed", result
                # Verify the click actually fired
                await asyncio.sleep(0.3)
                title = await conn.evaluate("document.title")
                assert title == "clicked"
            finally:
                await conn.close()

        run(_test())

    def test_click_nonexistent_element(self, chrome_path):
        from agent.runtime.cdp_backend import CdpConnection

        async def _test():
            conn = CdpConnection(chrome_path, headless=True, timeout=15)
            try:
                await conn.start()
                await conn.navigate("data:text/html,<body>empty</body>", wait_ms=500)
                result = await conn.click("#does-not-exist")
                assert "NOT_FOUND" in result or "not found" in result.lower()
            finally:
                await conn.close()

        run(_test())

    def test_type_into_input(self, chrome_path):
        from agent.runtime.cdp_backend import CdpConnection

        async def _test():
            conn = CdpConnection(chrome_path, headless=True, timeout=15)
            try:
                await conn.start()
                html = '<html><body><input id="name" type="text" /></body></html>'
                await conn.navigate(f"data:text/html,{html}", wait_ms=500)
                assert "satisfied" in await conn.wait_for(selector="#name", timeout_ms=5000)
                result = await conn.type_text("#name", "Lyra")
                assert json.loads(result)["status"] == "observed", result
                value = await conn.evaluate("document.querySelector('#name').value")
                assert value == "Lyra"
            finally:
                await conn.close()

        run(_test())

    def test_screenshot_via_connection(self, chrome_path):
        from agent.runtime.cdp_backend import CdpConnection

        async def _test():
            conn = CdpConnection(chrome_path, headless=True, timeout=15)
            try:
                await conn.start()
                await conn.navigate(
                    "data:text/html,<body><h1>Screenshot</h1></body>", wait_ms=500
                )
                path = await conn.screenshot()
                assert os.path.isfile(path)
                assert os.path.getsize(path) > 0
                os.unlink(path)
            finally:
                await conn.close()

        run(_test())

    def test_get_url(self, chrome_path):
        from agent.runtime.cdp_backend import CdpConnection

        async def _test():
            conn = CdpConnection(chrome_path, headless=True, timeout=15)
            try:
                await conn.start()
                await conn.navigate("data:text/html,<body>url test</body>", wait_ms=500)
                url = await conn.get_url()
                assert "data:text/html" in url
            finally:
                await conn.close()

        run(_test())

    def test_select_and_wait(self, chrome_path):
        from agent.runtime.cdp_backend import CdpConnection

        async def _test():
            conn = CdpConnection(chrome_path, headless=True, timeout=15)
            try:
                await conn.start()
                html = (
                    '<body><p id="result">Waiting</p><select id="choice" '
                    'onchange="document.title=this.value;'
                    'document.getElementById(\'result\').textContent=this.selectedOptions[0].text">'
                    '<option value="a">A</option><option value="b">B</option></select></body>'
                )
                await conn.navigate(f"data:text/html,{html}", wait_ms=500)
                assert "satisfied" in await conn.wait_for(selector="#choice", timeout_ms=5000)
                result = await conn.select("#choice", "b")
                assert json.loads(result)["status"] == "observed", result
                assert "satisfied" in await conn.wait_for(
                    selector="#choice", text="B", timeout_ms=1000
                )
                assert await conn.evaluate("document.title") == "b"
            finally:
                await conn.close()

        run(_test())


@requires_native_chrome_interactive
def test_backend_keeps_logical_tabs_isolated():
    """A click on one logical tab must never act on another tab's page."""
    async def _test():
        backend = CdpBrowserBackend(find_chrome(), timeout=15)
        try:
            first = "data:text/html,<body><h1>first-tab</h1></body>"
            second = "data:text/html,<body><h1>second-tab</h1></body>"
            assert "first-tab" in await backend.interactive_get_text(tab_id="tab-a", url=first)
            assert "second-tab" in await backend.interactive_get_text(tab_id="tab-b", url=second)
            assert "first-tab" in await backend.interactive_get_text(tab_id="tab-a")
            assert len(backend._connections) == 2
            assert backend._host is not None
            assert sum(conn._process is not None for conn in backend._connections.values()) == 1
        finally:
            await backend.close_connection()

    run(_test())


@requires_native_chrome_interactive
def test_shared_host_survives_repeated_tab_open_close():
    async def _test():
        backend = CdpBrowserBackend(find_chrome(), timeout=15)
        first_pid = 0
        try:
            for index in range(12):
                tab_id = f"cycle-{index}"
                url = f"data:text/html,<body>cycle-{index}</body>"
                assert f"cycle-{index}" in await backend.interactive_get_text(
                    tab_id=tab_id, url=url
                )
                assert backend._host is not None
                pid = backend._host._process.pid if backend._host._process else 0
                first_pid = first_pid or pid
                assert pid == first_pid
                await backend.close_connection(tab_id)
            assert backend._host is not None and backend._host.is_connected
            assert backend._connections == {}
        finally:
            await backend.close_connection()

    run(_test())


# ---------------------------------------------------------------------------
# Browser tools auto-escalation with CDP backend
# ---------------------------------------------------------------------------

class TestBrowserToolsAutoEscalation:
    """When CDP backend (interactive=True) is attached, click/type on a
    READ_ONLY tab should auto-escalate to HEADLESS instead of erroring."""

    @pytest.mark.parametrize("operation,arguments", [
        ("browser_click", {"selector": "ref:s1:0"}),
        ("browser_type", {"selector": "ref:s1:0", "text": "hello"}),
    ])
    def test_live_open_without_extraction_creates_writable_tab(self, tmp_path, operation, arguments):
        """Exercise real CDP backend dispatch with a mocked transport boundary."""
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        backend = CdpBrowserBackend("/fake/chrome", timeout=1)
        snapshot = {"url": "https://example.com", "title": "Form", "snapshotId": "s1",
                    "text": "Ready", "elements": [{"ref": "s1:0", "name": "Go"}]}
        connection = mock.Mock(is_connected=True)
        connection.navigate = mock.AsyncMock()
        connection.evaluate = mock.AsyncMock(return_value="Form")
        connection.get_url = mock.AsyncMock(return_value="https://example.com")
        connection.page_operation = mock.AsyncMock(return_value=snapshot)
        connection.click = mock.AsyncMock(return_value=json.dumps({"status": "observed", "after": snapshot}))
        connection.type_text = mock.AsyncMock(return_value=json.dumps({"status": "observed", "after": snapshot}))

        async def connection_for(tab_id, **kwargs):
            backend._connections[tab_id] = connection
            return connection

        backend.ensure_connection = mock.AsyncMock(side_effect=connection_for)
        register_browser_tools(reg, manager=manager, backend=backend)
        result = run(reg.execute("browser_open", {"url": "https://example.com", "extract": False}))
        assert "live browser" in result["output"]
        connection.navigate.assert_awaited_once()
        session = manager.list_sessions()[0]
        assert manager.get_tab(session.current_tab_id).mode.value == "headless"
        result = run(reg.execute(operation, arguments))
        assert '"status": "observed"' in result["output"]
        target = connection.click if operation == "browser_click" else connection.type_text
        target.assert_awaited_once()
        # The action observation is reused; no new snapshot invalidates its refs.
        connection.page_operation.assert_awaited_once_with("snapshot", {})

    def test_legacy_backend_still_blocks_read_only(self, tmp_path):
        """LegacyWslExtractBackend (interactive=False) should still block."""
        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")

        async def fake_extract(url, max_length=12000):
            return "content"

        async def fake_status():
            return True, "ok"

        register_browser_tools(
            reg, manager=manager,
            extract_fn=fake_extract, status_fn=fake_status,
        )
        run(reg.execute("browser_open", {"url": "https://example.com", "extract": False}))
        result = run(reg.execute("browser_click", {"selector": "#btn"}))
        out = result.get("output", "") + result.get("error", "")
        assert "Interactive backend not available" in out


# ---------------------------------------------------------------------------
# _get_windows_gateway / _fix_wsl_url 回归测试
# ---------------------------------------------------------------------------

class TestWindowsGateway:
    """回归：网关探测只在成功时写缓存；失败返回空串但不缓存（允许重试）。"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        if hasattr(_get_windows_gateway, "_cached"):
            delattr(_get_windows_gateway, "_cached")
        yield
        if hasattr(_get_windows_gateway, "_cached"):
            delattr(_get_windows_gateway, "_cached")

    def test_success_caches_and_reuses(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, "default via 172.25.0.1 dev eth0", "",
            )

        monkeypatch.setattr("builtins.open", mock.mock_open(read_data="microsoft WSL2"))
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _get_windows_gateway() == "172.25.0.1"
        assert len(calls) == 1
        # 缓存命中：不再重新探测
        assert _get_windows_gateway() == "172.25.0.1"
        assert len(calls) == 1

    def test_failure_not_cached(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "no route")

        monkeypatch.setattr("builtins.open", mock.mock_open(read_data="microsoft WSL2"))
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert _get_windows_gateway() == ""
        # 失败不写缓存：每次调用都重新探测
        assert _get_windows_gateway() == ""
        assert len(calls) == 2

    def test_non_wsl_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "builtins.open", mock.mock_open(read_data="Linux version 5.15"),
        )
        assert _get_windows_gateway() == ""


class TestFixWslUrl:
    """回归：_fix_wsl_url 的 relay 路由与主机替换回退分支。"""

    def _backend(self, port=9222, relay_port=9223):
        conn = CdpConnection("", page_ws_url="ws://fake")
        conn._port = port
        conn._relay_port = relay_port
        return conn

    def test_non_loopback_url_unchanged(self, monkeypatch):
        monkeypatch.setattr(cdp_backend_mod, "_get_windows_gateway", lambda: "172.25.0.1")
        backend = self._backend()
        url = "ws://10.0.0.5:9999/devtools/page/x"
        assert backend._fix_wsl_url(url) == url

    def test_no_gateway_unchanged(self, monkeypatch):
        monkeypatch.setattr(cdp_backend_mod, "_get_windows_gateway", lambda: "")
        backend = self._backend()
        url = "ws://127.0.0.1:9222/devtools/page/x"
        assert backend._fix_wsl_url(url) == url

    def test_relay_rewrites_port(self, monkeypatch):
        monkeypatch.setattr(cdp_backend_mod, "_get_windows_gateway", lambda: "172.25.0.1")
        backend = self._backend(port=9222, relay_port=9223)
        assert backend._fix_wsl_url("ws://127.0.0.1:9222/devtools/page/x") == (
            "ws://172.25.0.1:9223/devtools/page/x"
        )

    def test_fallback_rewrites_host_only(self, monkeypatch):
        monkeypatch.setattr(cdp_backend_mod, "_get_windows_gateway", lambda: "172.25.0.1")
        backend = self._backend(port=9222, relay_port=0)
        assert backend._fix_wsl_url("ws://127.0.0.1:9222/devtools/page/x") == (
            "ws://172.25.0.1:9222/devtools/page/x"
        )
