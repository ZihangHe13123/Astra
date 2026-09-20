"""Tests for browser tools registration and state-machine interaction.

Covers:
- All 8 browser tools register with correct names, group, and risk
- browser_open creates a session + tab
- browser_snapshot reads stored (sanitized) snapshot
- browser_extract via legacy backend (sanitized)
- browser_click/type interactive guard (no interactive backend → clear error)
- browser_handoff/browser_resume takeover lifecycle through the tool layer
- browser_status diagnostic output
- capabilities.py contract closure: interactive tool names now exist
"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime.browser_session import BackendCapabilities, BrowserSessionManager
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def registry(tmp_path):
    reg = ToolRegistry()
    manager = BrowserSessionManager(path=tmp_path / "browser.db")
    register_browser_tools(reg, manager=manager)
    return reg


@pytest.fixture()
def registry_with_backend(tmp_path):
    reg = ToolRegistry()
    manager = BrowserSessionManager(path=tmp_path / "browser.db")

    async def fake_extract(url, max_length=12000):
        body = f"Content of {url}\nCookie: session=secret123\nBody text here."
        # Pad to exceed _MIN_STATIC_CHARS (200) so the fallback ladder
        # accepts this as a successful static extraction.
        return body + "\n" + "Lorem ipsum dolor sit amet. " * 10

    async def fake_status():
        return True, "/usr/bin/extract"

    register_browser_tools(reg, manager=manager, extract_fn=fake_extract, status_fn=fake_status)
    return reg


@pytest.fixture()
def registry_interactive(tmp_path):
    class FakeInteractiveBackend:
        name = "fake-interactive"
        capabilities = BackendCapabilities(read=True, interactive=True, takeover=True)

        async def extract(self, url, *, max_length=12000):
            return "fake page"

        async def status(self):
            return True, "fake interactive"

        async def interactive_handoff(self, *, tab_id, url, profile_dir=""):
            return f"headed:{url}"

        async def interactive_resume(self, *, tab_id, url, profile_dir=""):
            return f"headless:{url}"

        async def interactive_state(self, *, tab_id="", url=""):
            return url or "https://example.com", "Example", "fake page"

        async def close_connection(self, tab_id=""):
            return None

    reg = ToolRegistry()
    manager = BrowserSessionManager(path=tmp_path / "browser.db")
    register_browser_tools(reg, manager=manager, backend=FakeInteractiveBackend())
    return reg


EXPECTED_TOOLS = {
    "browser_open",
    "browser_snapshot",
    "browser_extract",
    "browser_click",
    "browser_type",
    "browser_fill",
    "browser_check",
    "browser_read",
    "browser_select",
    "browser_wait",
    "browser_screenshot",
    "browser_handoff",
    "browser_resume",
    "browser_status",
    "browser_close",
}


@pytest.fixture()
def checked_registry(tmp_path):
    class ChoiceBackend:
        name = "choice-test"
        capabilities = BackendCapabilities(read=True, interactive=True, takeover=True)
        calls = []
        reject_origin = False
        outcome = {"status": "verified", "verified": True, "dispatch_state": "dispatched"}

        async def assert_origin(self, **kwargs):
            if self.reject_origin:
                raise PermissionError("Origin changed")

        async def interactive_check(self, selector, **kwargs):
            self.calls.append((selector, kwargs))
            return json.dumps({**self.outcome, "after": {
                "url": "https://example.test/form", "title": "Form", "elements": [{"ref": "fresh"}]
            }})

        async def interactive_state(self, **kwargs):
            raise AssertionError("The exact after snapshot must be returned without another observation")

    backend = ChoiceBackend()
    backend.calls = []
    manager = BrowserSessionManager(path=tmp_path / "browser.db")
    reg = ToolRegistry()
    reg.yolo = True
    register_browser_tools(reg, manager=manager, backend=backend)
    run(reg.execute("browser_open", {"url": "https://example.test/form", "extract": False}))
    session = manager.list_sessions()[0]
    return reg, backend, manager, session.current_tab_id


def test_browser_check_batch_preserves_exact_after_snapshot(checked_registry):
    reg, backend, manager, tab_id = checked_registry
    checks = [{"selector": "ref:a", "checked": True}, {"selector": "ref:b", "checked": False}]
    result = run(reg.execute("browser_check", {"tab_id": tab_id, "checks": checks}))
    assert not result.get("error"), result
    assert len(backend.calls) == 1 and backend.calls[0][1]["checks"] == checks
    assert "fresh" in manager.get_tab(tab_id).last_snapshot


@pytest.mark.parametrize("arguments", [
    {"checks": []}, {"selector": "ref:a", "checks": [{"selector": "ref:b", "checked": True}]},
    {"checks": [{"selector": "ref:a", "checked": "true"}]},
    {"checks": [{"selector": "ref:a", "checked": True}] * 21},
])
def test_browser_check_invalid_batch_does_not_dispatch(checked_registry, arguments):
    reg, backend, _manager, tab_id = checked_registry
    result = run(reg.execute("browser_check", {"tab_id": tab_id, **arguments}))
    assert result.get("error")
    assert backend.calls == []


@pytest.mark.parametrize("status", ["timeout", "stale_snapshot", "verification_failed"])
def test_browser_check_partial_failure_does_not_replay(checked_registry, status):
    reg, backend, _manager, tab_id = checked_registry
    backend.outcome = {"status": status, "dispatch_state": "partial", "completed": 1}
    result = run(reg.execute("browser_check", {"tab_id": tab_id, "selector": "ref:a"}))
    assert result["code"] == "browser_" + status
    assert result["partial"] is True and result["retryable"] is False
    assert len(backend.calls) == 1


def test_browser_check_origin_rejection_is_preinput(checked_registry):
    reg, backend, _manager, tab_id = checked_registry
    backend.reject_origin = True
    result = run(reg.execute("browser_check", {"tab_id": tab_id, "selector": "ref:a"}))
    assert result.get("error") and result["partial"] is False
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_all_tools_registered(self, registry):
        names = set(registry.tool_names)
        assert EXPECTED_TOOLS <= names, f"Missing: {EXPECTED_TOOLS - names}"

    def test_all_tools_in_browser_group(self, registry):
        for name in EXPECTED_TOOLS:
            tool = registry.get(name)
            assert tool is not None
            assert tool.group == "browser", f"{name} group={tool.group}"

    def test_risk_levels(self, registry):
        assert registry.get("browser_open").risk == "network"
        assert registry.get("browser_snapshot").risk == "read"
        assert registry.get("browser_extract").risk == "network"
        assert registry.get("browser_click").risk == "write"
        assert registry.get("browser_type").risk == "write"
        assert registry.get("browser_fill").risk == "write"
        assert registry.get("browser_check").risk == "write"
        assert registry.get("browser_read").risk == "read"
        assert registry.get("browser_handoff").risk == "read"
        assert registry.get("browser_resume").risk == "read"
        assert registry.get("browser_status").risk == "read"

    def test_screenshot_has_real_postcondition(self, registry):
        assert registry.get("browser_screenshot").postcondition is not None

    def test_browser_tools_expose_runtime_trace_context(self, registry_with_backend):
        opened = run(registry_with_backend.execute(
            "browser_open",
            {"url": "https://example.test", "extract": False},
        ))
        assert opened["trace_context"]["browser_session_id"]

    def test_capabilities_contract_closed(self, registry):
        """capabilities.py declares these interactive tools; they must exist."""
        interactive = {
            "browser_open", "browser_snapshot", "browser_click",
            "browser_type", "browser_handoff", "browser_resume",
        }
        names = set(registry.tool_names)
        missing = interactive - names
        assert not missing, f"capabilities.py contract not closed, missing: {missing}"

    def test_openai_tools_include_browser(self, registry):
        schemas = registry.to_openai_tools(groups={"browser"})
        schema_names = {s["function"]["name"] for s in schemas}
        assert EXPECTED_TOOLS - {"browser_type"} <= schema_names

    def test_browser_type_is_explicit_compatibility_only(self, registry):
        schemas = registry.to_openai_tools(groups={"browser"})
        names = [schema["function"]["name"] for schema in schemas]
        assert "browser_type" not in names
        assert "browser_fill" in names
        assert names == sorted(names)
        assert "browser_type" not in {
            schema["function"]["name"] for schema in registry.to_openai_tools()
        }
        explicit = registry.to_openai_tools(names={"browser_type"})
        assert [schema["function"]["name"] for schema in explicit] == ["browser_type"]
        legacy = registry.get("browser_type")
        assert legacy is not None
        assert legacy.risk == "write" and legacy.approval == "on_risk"
        assert legacy.idempotent is False
        assert callable(legacy.permission_check) and callable(legacy.permission_grant)

    def test_browser_open_guides_new_input_to_fill(self, registry):
        description = registry.get("browser_open").description
        assert "browser_fill" in description
        assert "browser_type" not in description


# ---------------------------------------------------------------------------
# browser_open
# ---------------------------------------------------------------------------

class TestBrowserOpen:
    def test_open_creates_tab(self, registry):
        result = run(registry.execute("browser_open", {"url": "https://example.com", "extract": False}))
        assert not result.get("error")
        assert "Opened tab" in result["output"]
        assert "read_only" in result["output"]

    def test_open_empty_url_errors(self, registry):
        result = run(registry.execute("browser_open", {"url": "", "extract": False}))
        assert "url is required" in result.get("error", "") or "url is required" in result.get("output", "")

    def test_open_with_extract_backend(self, registry_with_backend):
        result = run(registry_with_backend.execute(
            "browser_open", {"url": "https://example.com", "extract": True}
        ))
        assert not result.get("error")
        assert "Extracted" in result["output"]
        # Secret must be sanitized
        assert "secret123" not in result["output"]
        assert "[REDACTED]" in result["output"]

    def test_open_without_backend(self, registry):
        result = run(registry.execute("browser_open", {"url": "https://example.com", "extract": True}))
        assert not result.get("error")
        # With the fallback ladder, no backend → all rungs fail
        assert "All extraction rungs failed" in result["output"] or "No read backend" in result["output"]


# ---------------------------------------------------------------------------
# browser_snapshot
# ---------------------------------------------------------------------------

class TestBrowserSnapshot:
    def test_snapshot_no_tab_errors(self, registry):
        result = run(registry.execute("browser_snapshot", {}))
        out = result.get("output", "") + result.get("error", "")
        assert "No active tab" in out or "browser_open" in out

    def test_snapshot_after_open(self, registry_with_backend):
        run(registry_with_backend.execute("browser_open", {"url": "https://example.com"}))
        result = run(registry_with_backend.execute("browser_snapshot", {}))
        assert not result.get("error")
        assert "Snapshot of" in result["output"]
        assert "secret123" not in result["output"]

    def test_snapshot_unknown_tab(self, registry):
        result = run(registry.execute("browser_snapshot", {"tab_id": "nonexistent"}))
        out = result.get("output", "") + result.get("error", "")
        assert "Unknown tab" in out


# ---------------------------------------------------------------------------
# browser_extract
# ---------------------------------------------------------------------------

class TestBrowserExtract:
    def test_extract_with_backend(self, registry_with_backend):
        result = run(registry_with_backend.execute("browser_extract", {"url": "https://example.com"}))
        assert not result.get("error")
        assert "Content of https://example.com" in result["output"]
        assert "secret123" not in result["output"]

    def test_extract_no_backend(self, registry):
        result = run(registry.execute("browser_extract", {"url": "https://example.com"}))
        out = result.get("output", "") + result.get("error", "")
        assert "All extraction rungs failed" in out or "No read backend" in out

    def test_extract_empty_url(self, registry_with_backend):
        result = run(registry_with_backend.execute("browser_extract", {"url": ""}))
        out = result.get("output", "") + result.get("error", "")
        assert "url is required" in out


# ---------------------------------------------------------------------------
# Interactive guard (click / type)
# ---------------------------------------------------------------------------

class TestInteractiveGuard:
    def test_click_no_interactive_backend(self, registry_with_backend):
        # Open a tab first (read_only backend only)
        run(registry_with_backend.execute("browser_open", {"url": "https://example.com", "extract": False}))
        result = run(registry_with_backend.execute("browser_click", {"selector": "#btn"}))
        out = result.get("output", "") + result.get("error", "")
        assert "Interactive backend not available" in out

    def test_type_no_interactive_backend(self, registry_with_backend):
        run(registry_with_backend.execute("browser_open", {"url": "https://example.com", "extract": False}))
        result = run(registry_with_backend.execute("browser_type", {"selector": "#input", "text": "hello"}))
        out = result.get("output", "") + result.get("error", "")
        assert "Interactive backend not available" in out

    def test_click_no_tab(self, registry):
        result = run(registry.execute("browser_click", {"selector": "#btn"}))
        out = result.get("output", "") + result.get("error", "")
        assert "No active tab" in out or "browser_open" in out


# ---------------------------------------------------------------------------
# Takeover lifecycle through tools
# ---------------------------------------------------------------------------

class TestTakeoverTools:
    def test_handoff_and_resume(self, registry_interactive):
        run(registry_interactive.execute("browser_open", {"url": "https://example.com", "extract": False}))
        # Handoff
        result = run(registry_interactive.execute("browser_handoff", {"reason": "captcha"}))
        assert not result.get("error")
        assert "human takeover" in result["output"]
        assert "captcha" in result["output"]
        # Resume
        result = run(registry_interactive.execute("browser_resume", {}))
        assert not result.get("error")
        assert "resumed" in result["output"]
        assert "read_only" in result["output"]  # restored to pre-takeover mode

    def test_handoff_invalid_reason_defaults(self, registry_interactive):
        run(registry_interactive.execute("browser_open", {"url": "https://example.com", "extract": False}))
        result = run(registry_interactive.execute("browser_handoff", {"reason": "bogus"}))
        assert "unknown" in result["output"]

    def test_resume_not_in_takeover(self, registry):
        run(registry.execute("browser_open", {"url": "https://example.com", "extract": False}))
        result = run(registry.execute("browser_resume", {}))
        out = result.get("output", "") + result.get("error", "")
        assert "not in takeover" in out

    def test_click_during_takeover_blocked(self, registry):
        run(registry.execute("browser_open", {"url": "https://example.com", "extract": False}))
        run(registry.execute("browser_handoff", {"reason": "login"}))
        result = run(registry.execute("browser_click", {"selector": "#btn"}))
        out = result.get("output", "") + result.get("error", "")
        # Either the interactive guard or the takeover guard fires
        assert "takeover" in out.lower() or "Interactive backend" in out


# ---------------------------------------------------------------------------
# browser_status
# ---------------------------------------------------------------------------

class TestBrowserStatus:
    def test_status_no_backend(self, registry):
        result = run(registry.execute("browser_status", {}))
        assert not result.get("error")
        assert "unavailable" in result["output"]

    def test_status_with_backend(self, registry_with_backend):
        result = run(registry_with_backend.execute("browser_status", {}))
        assert not result.get("error")
        assert "available" in result["output"]

    def test_status_shows_sessions(self, registry_with_backend):
        run(registry_with_backend.execute("browser_open", {"url": "https://example.com", "extract": False}))
        result = run(registry_with_backend.execute("browser_status", {}))
        assert "example.com" in result["output"]


# ---------------------------------------------------------------------------
# Browser extract factory functions (web.py → browser tools wiring)
# ---------------------------------------------------------------------------

class TestWslExtractFactories:
    """Test create_wsl_extract_fn / create_wsl_status_fn and their integration
    with register_browser_tools → LegacyWslExtractBackend."""

    def test_create_extract_fn_returns_callable(self):
        from agent.runtime.tools.web import create_wsl_extract_fn
        fn = create_wsl_extract_fn("echo")
        assert callable(fn)

    def test_create_status_fn_returns_callable(self):
        from agent.runtime.tools.web import create_wsl_status_fn
        fn = create_wsl_status_fn()
        assert callable(fn)

    def test_extract_fn_with_echo_command(self):
        """Use 'echo' as a mock WSL command to test the subprocess path."""
        from agent.runtime.tools.web import create_wsl_extract_fn
        # 'echo' will print the URL as output
        fn = create_wsl_extract_fn("echo")
        result = run(fn("https://example.com"))
        assert "example.com" in result

    def test_extract_fn_empty_url(self):
        from agent.runtime.tools.web import create_wsl_extract_fn
        fn = create_wsl_extract_fn("echo")
        result = run(fn(""))
        assert "url is required" in result

    def test_extract_fn_truncates(self):
        from agent.runtime.tools.web import create_wsl_extract_fn
        fn = create_wsl_extract_fn("echo")
        result = run(fn("x" * 100, max_length=50))
        # echo outputs the long string, should be truncated with marker
        assert "…[truncated]" in result
        assert len(result) < 100  # significantly shorter than input

    def test_extract_fn_failing_command(self):
        from agent.runtime.tools.web import create_wsl_extract_fn
        fn = create_wsl_extract_fn("false")  # 'false' always exits 1
        result = run(fn("https://example.com"))
        assert "Browser Error" in result

    def test_status_fn_returns_tuple(self):
        from agent.runtime.tools.web import create_wsl_status_fn
        fn = create_wsl_status_fn()
        result = run(fn())
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)

    def test_register_browser_tools_with_factories(self, tmp_path):
        """register_browser_tools with factory fns creates LegacyWslExtractBackend."""
        from agent.runtime.tools.web import create_wsl_extract_fn, create_wsl_status_fn
        from agent.runtime.browser_session import LegacyWslExtractBackend

        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        # Use 'echo' as mock command
        returned_manager = register_browser_tools(
            reg,
            manager=manager,
            extract_fn=create_wsl_extract_fn("echo"),
            status_fn=create_wsl_status_fn(),
        )
        assert returned_manager is manager
        assert isinstance(manager.backend, LegacyWslExtractBackend)
        assert manager.backend.name == "legacy-wsl-extract"
        assert manager.backend.capabilities.read is True
        assert manager.backend.capabilities.interactive is False

    def test_browser_extract_through_factory_backend(self, tmp_path):
        """End-to-end: browser_extract works through factory-created backend."""
        from agent.runtime.tools.web import create_wsl_extract_fn, create_wsl_status_fn

        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(
            reg,
            manager=manager,
            extract_fn=create_wsl_extract_fn("echo"),
            status_fn=create_wsl_status_fn(),
        )
        result = run(reg.execute("browser_extract", {"url": "https://example.com"}))
        assert not result.get("error")
        assert "example.com" in result["output"]

    def test_browser_open_with_factory_extract(self, tmp_path):
        """browser_open with extract=True works through factory backend."""
        from agent.runtime.tools.web import create_wsl_extract_fn, create_wsl_status_fn

        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(
            reg,
            manager=manager,
            extract_fn=create_wsl_extract_fn("echo"),
            status_fn=create_wsl_status_fn(),
        )
        result = run(reg.execute("browser_open", {"url": "https://example.com", "extract": True}))
        assert not result.get("error")
        # echo produces <200 chars so the fallback ladder escalates past static;
        # either "Extracted" (static rung) or the fallback error is acceptable —
        # the point is the factory backend integration does not crash.
        out = result["output"]
        assert "Extracted" in out or "All extraction rungs failed" in out

    def test_browser_status_through_factory(self, tmp_path):
        """browser_status runs without error through factory-created backend."""
        from agent.runtime.tools.web import create_wsl_extract_fn, create_wsl_status_fn

        reg = ToolRegistry()
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(
            reg,
            manager=manager,
            extract_fn=create_wsl_extract_fn("echo"),
            status_fn=create_wsl_status_fn(),
        )
        result = run(reg.execute("browser_status", {}))
        assert not result.get("error")
        # In WSL test env, 'wsl' command doesn't exist → status reports unavailable
        # In Windows env, it may report available. Either way, the tool runs.
        assert "Backend:" in result["output"]


class TestBrowserExtractPortability:
    def test_neutral_factories_remain_wsl_aliases(self):
        from agent.runtime.tools.web import (
            create_browser_extract_fn,
            create_browser_status_fn,
            create_wsl_extract_fn,
            create_wsl_status_fn,
        )

        assert create_wsl_extract_fn is create_browser_extract_fn
        assert create_wsl_status_fn is create_browser_status_fn

    def test_darwin_default_command_uses_native_bash(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)
        monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
        monkeypatch.setattr(web_mod.sys, "platform", "darwin")
        assert web_mod.resolve_browser_extract_command().startswith("bash ")

    def test_windows_default_command_uses_wsl(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)
        monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        assert web_mod.resolve_browser_extract_command().startswith("wsl ")

    def test_windows_compatibility_command_keeps_cmd_safe_double_quotes(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)
        monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
        monkeypatch.setattr(web_mod.sys, "platform", "win32")

        assert web_mod.resolve_browser_extract_command().startswith('wsl sh -lc "')

    def test_linux_wsl_default_command_uses_native_bash(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)
        monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
        monkeypatch.setattr(web_mod.sys, "platform", "linux")
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
        assert web_mod.resolve_browser_extract_command().startswith("bash ")

    def test_windows_default_url_metacharacters_are_passed_as_one_argv_element(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"safe output", b""

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        async def fail_shell(*args, **kwargs):
            raise AssertionError("host shell must not execute browser extractor URLs")

        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)
        monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
        monkeypatch.setattr(web_mod.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(web_mod.asyncio, "create_subprocess_shell", fail_shell)
        url = "https://example.com/path?a=1&b=2|whoami>pwned"

        result = run(web_mod.create_browser_extract_fn()(url))

        assert result == "safe output"
        assert calls[0][0][0:3] == ("wsl", "sh", "-lc")
        assert calls[0][0][-1] == url

    def test_json_argv_preserves_windows_path_spaces_and_backslashes(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"safe output", b""

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess()

        executable = r"C:\Program Files\Browser Extract\extract.exe"
        monkeypatch.setenv("BROWSER_EXTRACT_ARGV", json.dumps([executable, "--mode", "safe"]))
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", "ignored legacy command")
        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        monkeypatch.setattr(web_mod.asyncio, "create_subprocess_exec", fake_exec)
        url = "https://example.com/path?a=1&b=2|whoami>pwned"

        result = run(web_mod.create_browser_extract_fn()(url))

        assert result == "safe output"
        assert calls[0] == (executable, "--mode", "safe", url)

    def test_windows_legacy_default_shape_uses_cmd_with_url_only_in_child_env(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            def _close(self):
                pass

            async def communicate(self):
                return b"safe output", b""

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        command = web_mod.DEFAULT_WSL_EXTRACT_CMD
        url = r"https://example.com/a path/C:\tmp\x?q=%PATH%!PATH!^&whoami|more>pwned<input"
        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)
        monkeypatch.setenv("WSL_EXTRACT_CMD", command)
        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        monkeypatch.setattr(web_mod, "_create_windows_legacy_subprocess", fake_exec)

        result = run(web_mod.create_browser_extract_fn()(url))

        assert result == "safe output"
        args, kwargs = calls[0]
        assert args[:5] == ("cmd.exe", "/D", "/V:OFF", "/S", "/C")
        assert args[-1].startswith(command)
        assert f"%{web_mod._WINDOWS_LEGACY_URL_ENV}%" in args[-1]
        assert all(url not in arg for arg in args)
        assert kwargs["env"][web_mod._WINDOWS_LEGACY_URL_ENV] == url
        assert web_mod._WINDOWS_LEGACY_URL_ENV not in web_mod.os.environ

    def test_windows_legacy_preserves_literal_matched_and_unmatched_bangs(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            def _close(self):
                pass

            async def communicate(self):
                return b"safe output", b""

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        command = "echo literal! matched!PATH! unmatched!"
        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", command)
        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        monkeypatch.setattr(web_mod, "_create_windows_legacy_subprocess", fake_exec)

        assert run(web_mod.create_browser_extract_fn()("https://example.com/!url!")) == "safe output"

        args, _ = calls[0]
        assert args[:5] == ("cmd.exe", "/D", "/V:OFF", "/S", "/C")
        assert args[-1].startswith(command)
        assert "literal! matched!PATH! unmatched!" in args[-1]

    @pytest.mark.parametrize("invalid", ['"', "\r", "\n", "\0"])
    def test_windows_legacy_rejects_quote_and_line_controls_before_subprocess(self, monkeypatch, invalid):
        from agent.runtime.tools import web as web_mod

        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("invalid Windows legacy URL must fail before subprocess creation")

        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", "wsl extract")
        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        monkeypatch.setattr(web_mod, "_create_windows_legacy_subprocess", fake_exec)

        result = run(web_mod.create_browser_extract_fn()(f"https://example.com/a{invalid}b"))

        assert "Browser Error" in result
        assert "unsupported" in result.lower()
        assert calls == []

    def test_windows_legacy_preserves_nested_bash_quotes_backslashes_and_spaces(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            def _close(self):
                pass

            async def communicate(self):
                return b"safe output", b""

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        command = (
            r'set "EXTRACT_ROOT=C:\Program Files\Browser Extract" && '
            r'wsl bash -lc "printf \\\"%s\\n\\\" \\\"$HOME\\\"; exec extract \\\"$1\\\"" extract'
        )
        url = "https://example.com/a path/?x=1&y=2"
        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", command)
        monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        monkeypatch.setattr(web_mod, "_create_windows_legacy_subprocess", fake_exec)

        assert run(web_mod.create_browser_extract_fn()(url)) == "safe output"

        args, kwargs = calls[0]
        assert args[-1].startswith(command)
        assert command in args[-1]
        assert url not in args[-1]
        assert kwargs["env"][web_mod._WINDOWS_LEGACY_URL_ENV] == url

    def test_windows_legacy_concurrent_urls_get_independent_child_environments(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            def _close(self):
                pass

            async def communicate(self):
                await asyncio.sleep(0)
                return b"safe output", b""

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            await asyncio.sleep(0)
            return FakeProcess()

        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", "wsl extract")
        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        monkeypatch.setattr(web_mod, "_create_windows_legacy_subprocess", fake_exec)
        urls = ["https://one.example/?a=1&b=2", "https://two.example/a path/!x!"]
        extract = web_mod.create_browser_extract_fn()

        async def scenario():
            return await asyncio.gather(*(extract(url) for url in urls))

        results = run(scenario())

        assert results == ["safe output", "safe output"]
        assert all(args[:5] == ("cmd.exe", "/D", "/V:OFF", "/S", "/C") for args, _ in calls)
        child_envs = [kwargs["env"] for _, kwargs in calls]
        assert len({id(env) for env in child_envs}) == 2
        assert {env[web_mod._WINDOWS_LEGACY_URL_ENV] for env in child_envs} == set(urls)
        assert web_mod._WINDOWS_LEGACY_URL_ENV not in web_mod.os.environ

    def test_windows_custom_status_uses_exact_cmd_semantics_without_fake_input(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            def _close(self):
                pass

            async def communicate(self):
                return b"ready", b""

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        status_command = (
            r'set "EXTRACT_ROOT=C:\Program Files\Browser Extract" && '
            r'wsl bash -lc "test -x \\\"$HOME/bin/extract\\\""'
        )
        monkeypatch.setenv("BROWSER_EXTRACT_ARGV", '["custom-extract"]')
        monkeypatch.setenv("BROWSER_EXTRACT_STATUS_CMD", status_command)
        monkeypatch.setattr(web_mod.sys, "platform", "win32")
        monkeypatch.setattr(web_mod, "_create_windows_legacy_subprocess", fake_exec)

        available, detail = run(web_mod.create_browser_status_fn()())

        assert available is True
        assert detail == "Browser extractor available: ready"
        args, kwargs = calls[0]
        assert args == ("cmd.exe", "/D", "/V:OFF", "/S", "/C", status_command)
        assert "env" not in kwargs
        assert web_mod._BROWSER_EXTRACT_STATUS_ARGUMENT not in args

    @pytest.mark.parametrize("cmd_source", ["comspec", "system-root"])
    def test_windows_legacy_adapter_passes_raw_command_and_attaches_before_start(self, monkeypatch, cmd_source):
        from agent.runtime.tools import web as web_mod

        events = []
        captured = {}
        if cmd_source == "comspec":
            monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
            monkeypatch.setenv("SystemRoot", r"C:\Ignored")
        else:
            monkeypatch.delenv("COMSPEC", raising=False)
            monkeypatch.setenv("SystemRoot", r"C:\Windows")

        class Input:
            def write(self, value):
                events.append(("start", value))

            def close(self):
                events.append("stdin-close")

        class Process:
            _handle = 123
            returncode = 0
            stdin = Input()

            def poll(self):
                return self.returncode

            def wait(self):
                events.append("wait")
                return self.returncode

        class Job:
            def close(self):
                events.append("job-close")

        def create_job(**kwargs):
            events.append("attach")
            assert kwargs["process_handle"] == 123
            return Job()

        def popen(command_line, **kwargs):
            captured.update(command_line=command_line, **kwargs)
            kwargs["stdout"].write(b"safe output")
            return Process()

        monkeypatch.setattr(web_mod.subprocess, "Popen", popen)
        monkeypatch.setattr(web_mod.WindowsJob, "create", create_job)
        command = 'set "EXTRACT_ROOT=C:\\Program Files" && extract "%ASTRA_BROWSER_EXTRACT_URL%"'
        env = {web_mod._WINDOWS_LEGACY_URL_ENV: "https://example.com/?a=1&b=2"}

        async def scenario():
            proc = await web_mod._create_windows_legacy_subprocess(
                *web_mod._WINDOWS_LEGACY_CMD_PREFIX, command, env=env,
            )
            return await proc.communicate()

        assert run(scenario()) == (b"safe output", b"")
        assert captured["command_line"] == (
            'cmd.exe /D /V:OFF /S /C "set /p "__ASTRA_BROWSER_START_GATE=" >nul && '
            + command + '"'
        )
        assert captured["shell"] is False
        assert captured["executable"] == r"C:\Windows\System32\cmd.exe"
        assert captured["env"] == env
        assert env[web_mod._WINDOWS_LEGACY_URL_ENV] not in captured["command_line"]
        assert events.index("attach") < events.index(("start", b"1\n"))
        assert events.index("job-close") < events.index("wait")
        assert captured["stdout"].closed and captured["stderr"].closed

    @pytest.mark.parametrize("comspec,system_root", [
        ("cmd.exe", r"C:\Windows"), (r"\Windows\System32\cmd.exe", r"C:\Windows"), ("", ""),
    ])
    def test_windows_legacy_adapter_rejects_relative_cmd_without_path_search(self, monkeypatch, comspec, system_root):
        from agent.runtime.tools import web as web_mod

        monkeypatch.setenv("COMSPEC", comspec)
        monkeypatch.setenv("SystemRoot", system_root)

        def forbidden(*args, **kwargs):
            raise AssertionError("relative CMD must fail before spawning or searching PATH")

        monkeypatch.setattr(web_mod.subprocess, "Popen", forbidden)
        monkeypatch.setattr(web_mod.shutil, "which", forbidden)
        with pytest.raises(FileNotFoundError, match="absolute COMSPEC or SystemRoot"):
            run(web_mod._create_windows_legacy_subprocess(
                *web_mod._WINDOWS_LEGACY_CMD_PREFIX, "echo ready",
            ))

    @pytest.mark.parametrize("finish", ["timeout", "cancel", "attach-error", "before-communicate"])
    def test_windows_legacy_adapter_reaps_on_interruption(self, monkeypatch, finish):
        from agent.runtime.tools import web as web_mod

        events = []
        outputs = []
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

        class Input:
            def write(self, value):
                events.append("start")

            def close(self):
                events.append("stdin-close")

        class Process:
            _handle = 123
            returncode = None
            stdin = Input()

            def poll(self):
                return self.returncode

            def kill(self):
                events.append("kill")
                self.returncode = -9

            def wait(self):
                events.append("wait")
                return self.returncode

        class Job:
            def close(self):
                events.append("job-close")

        def create_job(**kwargs):
            if finish == "attach-error":
                raise OSError("job unavailable")
            return Job()

        def popen(command_line, **kwargs):
            outputs.extend([kwargs["stdout"], kwargs["stderr"]])
            return Process()

        monkeypatch.setattr(web_mod.subprocess, "Popen", popen)
        monkeypatch.setattr(web_mod.WindowsJob, "create", create_job)

        async def scenario():
            if finish == "before-communicate":
                async def cancel_before_start(coro, **kwargs):
                    coro.close()
                    raise asyncio.CancelledError

                monkeypatch.setattr(web_mod.asyncio, "wait_for", cancel_before_start)
                monkeypatch.setattr(web_mod.sys, "platform", "win32")
                with pytest.raises(asyncio.CancelledError):
                    await web_mod.create_browser_extract_fn("echo ready")("https://example.com")
                return
            if finish == "attach-error":
                with pytest.raises(OSError, match="job unavailable"):
                    await web_mod._create_windows_legacy_subprocess(
                        *web_mod._WINDOWS_LEGACY_CMD_PREFIX, "echo ready",
                    )
                return
            proc = await web_mod._create_windows_legacy_subprocess(
                *web_mod._WINDOWS_LEGACY_CMD_PREFIX, "echo ready",
            )
            task = asyncio.create_task(proc.communicate())
            if finish == "timeout":
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, timeout=0.02)
            else:
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        run(scenario())
        assert events.index("kill") < events.index("wait")
        if finish == "attach-error":
            assert "start" not in events
        else:
            assert events.index("job-close") < events.index("kill")
        assert all(output.closed for output in outputs)

    @pytest.mark.skipif(sys.platform != "win32", reason="real CMD argv parsing requires Windows")
    @pytest.mark.parametrize("url", [
        "https://example.com/a path/?x=1&y=2|whoami>unused<input^!PATH!%PATH%",
        "https://example.com/backslash/" + "\\" * 2,
    ])
    def test_windows_legacy_native_roundtrip(self, monkeypatch, tmp_path, url):
        from agent.runtime.tools import web as web_mod

        script = tmp_path / "extractor script.py"
        script.write_text("import json, sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", subprocess.list2cmdline([sys.executable, str(script)]))
        output = run(web_mod.create_browser_extract_fn()(url))
        assert json.loads(output) == [url]

    @pytest.mark.skipif(sys.platform != "win32", reason="real Windows Job cleanup requires Windows")
    @pytest.mark.parametrize("finish", ["timeout", "cancel"])
    def test_windows_legacy_native_interrupt_closes_descendants(self, tmp_path, finish):
        import ctypes
        from ctypes import wintypes
        from agent.runtime.tools import web as web_mod

        marker = tmp_path / "pids.json"
        script = tmp_path / "extractor child.py"
        script.write_text(
            "import json, os, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "with open(sys.argv[1] + '.tmp', 'w') as f: json.dump([os.getpid(), child.pid], f)\n"
            "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "time.sleep(60)\n", encoding="utf-8",
        )
        command = subprocess.list2cmdline([sys.executable, str(script), str(marker)])
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        def exited(pid):
            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                assert ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID gone
                return True
            try:
                return kernel32.WaitForSingleObject(handle, 0) == 0
            finally:
                kernel32.CloseHandle(handle)

        async def scenario():
            proc = await web_mod._create_windows_legacy_subprocess(
                *web_mod._WINDOWS_LEGACY_CMD_PREFIX, command,
            )
            task = asyncio.create_task(proc.communicate())
            try:
                async with asyncio.timeout(10):
                    while not marker.exists():
                        await asyncio.sleep(0.01)
                pids = json.loads(marker.read_text())
                if finish == "timeout":
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(task, timeout=0.02)
                else:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                assert proc.returncode is not None
                async with asyncio.timeout(5):
                    while not all(exited(pid) for pid in pids):
                        await asyncio.sleep(0.01)
            finally:
                if not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task

        run(scenario())

    @pytest.mark.skipif(os.name != "posix", reason="real bash execution requires POSIX")
    def test_legacy_posix_command_keeps_shell_expansion_and_isolates_url(self, monkeypatch, tmp_path):
        from agent.runtime.tools import web as web_mod

        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", 'printf "%s|" "$HOME"')
        monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
        monkeypatch.setattr(web_mod.sys, "platform", "linux")
        marker = tmp_path / "pwned"
        url = f'https://example.com/?q="$(touch {marker})"&x=1|whoami'

        result = run(web_mod.create_browser_extract_fn()(url))

        assert result == f"{Path.home()}|{url}|"
        assert not marker.exists()

    def test_legacy_command_and_malicious_url_are_separate_wrapper_arguments(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"safe", b""

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess()

        command = 'printf "%s" "$HOME"'
        url = "https://example.com/?a=1&b=2|whoami>pwned"
        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", command)
        monkeypatch.setattr(web_mod.sys, "platform", "linux")
        monkeypatch.setattr(web_mod.asyncio, "create_subprocess_exec", fake_exec)

        assert run(web_mod.create_browser_extract_fn()(url)) == "safe"

        assert calls[0][0:2] == ("bash", "-lc")
        assert calls[0][-2:] == (command, url)
        assert all(url not in arg for arg in calls[0][:-1])

    def test_custom_status_without_override_only_checks_readiness(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        subprocess_calls = []
        monkeypatch.setenv("BROWSER_EXTRACT_ARGV", json.dumps(["custom-extract", "--render"]))
        monkeypatch.delenv("BROWSER_EXTRACT_STATUS_CMD", raising=False)
        monkeypatch.setattr(web_mod.shutil, "which", lambda executable: "/usr/bin/custom-extract")

        async def fake_exec(*args, **kwargs):
            subprocess_calls.append(args)
            raise AssertionError("custom extractor must not receive a status sentinel")

        monkeypatch.setattr(web_mod.asyncio, "create_subprocess_exec", fake_exec)

        available, detail = run(web_mod.create_browser_status_fn()())

        assert available is True
        assert "ready" in detail.lower()
        assert subprocess_calls == []

    def test_custom_status_override_runs_without_fake_url_or_sentinel(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        calls = []

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"ready", b""

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess()

        status_command = 'printf "%s" "$HOME"'
        monkeypatch.setenv("BROWSER_EXTRACT_ARGV", json.dumps(["custom-extract", "--render"]))
        monkeypatch.setenv("BROWSER_EXTRACT_STATUS_CMD", status_command)
        monkeypatch.setattr(web_mod.sys, "platform", "linux")
        monkeypatch.setattr(web_mod.asyncio, "create_subprocess_exec", fake_exec)

        available, detail = run(web_mod.create_browser_status_fn()())

        assert available is True
        assert detail == "Browser extractor available: ready"
        assert calls[0][0:2] == ("bash", "-lc")
        assert calls[0][-1] == status_command
        assert web_mod._BROWSER_EXTRACT_STATUS_ARGUMENT not in calls[0]

    def test_default_status_diagnostic_is_platform_neutral(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"/usr/local/bin/extract\n", b""

        async def fake_exec(*args, **kwargs):
            return FakeProcess()

        monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)
        monkeypatch.delenv("BROWSER_EXTRACT_ARGV", raising=False)
        monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
        monkeypatch.setattr(web_mod.sys, "platform", "darwin")
        monkeypatch.setattr(web_mod.asyncio, "create_subprocess_exec", fake_exec)

        available, detail = run(web_mod.create_browser_status_fn()())

        assert available is True
        assert detail == "Browser extractor available: /usr/local/bin/extract"

    def test_new_environment_variable_wins_over_wsl_compatibility_variable(self, monkeypatch):
        from agent.runtime.tools import web as web_mod

        monkeypatch.setenv("BROWSER_EXTRACT_CMD", "echo preferred")
        monkeypatch.setenv("WSL_EXTRACT_CMD", "echo legacy")
        assert web_mod.resolve_browser_extract_command() == "echo preferred"

    def test_web_tool_fallback_uses_the_same_resolved_command_as_direct_factory(self, monkeypatch, tmp_path):
        from agent.runtime.tools.web import create_browser_extract_fn, register_web_tools

        script = tmp_path / "extractor script.py"
        script.write_text("import sys; print('preferred-extractor ' + sys.argv[1])", encoding="utf-8")
        argv = [sys.executable, str(script)]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        monkeypatch.setenv("BROWSER_EXTRACT_CMD", command)
        monkeypatch.setenv("WSL_EXTRACT_CMD", "echo legacy-extractor")
        url = "https://example.com"
        direct = run(create_browser_extract_fn()(url))

        registry = ToolRegistry()
        register_web_tools(registry, None)
        fallback = run(registry.execute(
            "extract_url", {"url": url, "browser_first": True},
        ))

        assert direct == "preferred-extractor https://example.com"
        assert fallback["output"] == direct

    def test_user_facing_web_descriptions_use_neutral_extractor_configuration(self):
        from agent.runtime.tools.web import register_web_tools

        registry = ToolRegistry()
        register_web_tools(registry, None)

        status_description = registry.get("search_status").description
        extract_description = registry.get("extract_url").description
        assert "browser" in status_description.lower()
        assert "wsl" not in status_description.lower()
        assert "BROWSER_EXTRACT_CMD" in extract_description
        assert "BROWSER_EXTRACT_ARGV" in extract_description
        assert "BROWSER_EXTRACT_STATUS_CMD" in extract_description
        assert "WSL_EXTRACT_CMD" in extract_description
        assert "legacy" in extract_description.lower()


@pytest.mark.parametrize('reported_verified', [False, True])
@pytest.mark.parametrize('actual,truncated,equivalent', [('1 2', False, True), ('1 3', False, False), ('1', True, None)])
def test_fill_reports_whitespace_comparison_without_upgrading_failed_verification(checked_registry, actual, truncated, equivalent, reported_verified):
    reg, backend, _manager, tab_id = checked_registry
    async def fill(selector, **kwargs):
        backend.calls.append((selector, kwargs))
        return json.dumps({'status': 'verified' if reported_verified and not truncated else 'verification_failed', 'verified': reported_verified,
                           'value': actual, 'valueTruncated': truncated,
                           'after': {'url': 'https://example.test/form', 'elements': []}})
    backend.interactive_fill = fill
    result = run(reg.execute('browser_fill', {'tab_id': tab_id, 'selector': 'ref:a', 'text': '1  2'}))
    assert result['code'] == 'browser_verification_failed'
    assert len(backend.calls) == 1
    assert '"whitespace_equivalent": ' + json.dumps(equivalent) in result['error']
    if not truncated:
        assert 'do not claim all fields matched' in result['recovery_hint']
