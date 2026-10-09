"""Regression tests for the DSH-shaped Minimal Mode tool surface."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agent.sandbox.local import LocalSandbox
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_str_replace_editor_exposes_dsh_schema_and_file_commands(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))

    schema = registry.to_openai_tools(names={"str_replace_editor"})[0]
    assert schema["function"]["name"] == "str_replace_editor"
    assert schema["function"]["parameters"]["properties"]["command"]["enum"] == [
        "view", "create", "str_replace", "insert",
    ]
    assert registry.get("str_replace_editor") is not None
    assert registry.get("str_replace_editor").expose_by_default is False
    assert all(
        item["function"]["name"] != "str_replace_editor"
        for item in registry.to_openai_tools()
    )

    target = tmp_path / "sample.txt"
    created = run(registry.execute("str_replace_editor", {
        "command": "create",
        "path": str(target),
        "file_text": "one\ntwo\n",
    }))
    assert "New file created successfully" in created["output"]

    # DeepSeek may serialize omitted optional fields as JSON null. The DSH
    # surface should accept those fields and let the command-specific checks
    # decide which arguments are actually required.
    null_view = run(registry.execute("str_replace_editor", {
        "command": "view",
        "path": str(target),
        "file_text": None,
        "insert_line": None,
        "new_str": None,
        "old_str": None,
        "view_range": None,
    }))
    assert "     1  one" in null_view["output"]

    viewed = run(registry.execute("str_replace_editor", {
        "command": "view",
        "path": str(target),
        "view_range": [2, -1],
    }))
    assert "     2  two" in viewed["output"]

    replaced = run(registry.execute("str_replace_editor", {
        "command": "str_replace",
        "path": str(target),
        "old_str": "two",
        "new_str": "TWO",
    }))
    assert "edited successfully" in replaced["output"]

    inserted = run(registry.execute("str_replace_editor", {
        "command": "insert",
        "path": str(target),
        "insert_line": 1,
        "new_str": "between",
    }))
    assert "edited successfully" in inserted["output"]
    assert target.read_text(encoding="utf-8") == "one\nbetween\nTWO\n"


def test_str_replace_editor_create_refusal_names_only_editor_commands(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    target = tmp_path / "existing.txt"
    target.write_text("keep\n", encoding="utf-8")

    refused = run(registry.execute("str_replace_editor", {
        "command": "create",
        "path": str(target),
        "file_text": "replacement\n",
    }))

    assert refused["code"] == "existing_file_requires_edit"
    assert refused["output"] == ""
    assert target.read_text(encoding="utf-8") == "keep\n"
    guidance = refused["error"] + " " + refused["recovery_hint"]
    assert "str_replace" in guidance and "insert" in guidance
    # Minimal Mode has no other file tool to fall back on.
    other_file_tools = set(registry.tool_names) - {"str_replace_editor"}
    assert {"write_file", "edit_file", "apply_patch"} <= other_file_tools
    assert not [name for name in other_file_tools if name in guidance]


def test_str_replace_editor_insert_line_text_matches_where_the_text_lands(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    schema = registry.to_openai_tools(names={"str_replace_editor"})[0]["function"]
    described = schema["parameters"]["properties"]["insert_line"]["description"]
    assert "after this line" in described
    assert "0 inserts at the top" in described

    target = tmp_path / "lines.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    for insert_line, text in ((0, "top"), (2, "after-one")):
        result = run(registry.execute("str_replace_editor", {
            "command": "insert",
            "path": str(target),
            "insert_line": insert_line,
            "new_str": text,
        }))
        assert "edited successfully" in result["output"]
    assert target.read_text(encoding="utf-8") == "top\none\nafter-one\ntwo\n"


def test_str_replace_editor_str_replace_writes_a_trailing_whitespace_match(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    target = tmp_path / "drift.txt"
    target.write_bytes(b"alpha  \nbeta\n")

    replaced = run(registry.execute("str_replace_editor", {
        "command": "str_replace",
        "path": str(target),
        "old_str": "alpha\nbeta",
        "new_str": "ALPHA\nbeta",
    }))

    assert "edited successfully" in replaced["output"]
    assert target.read_bytes() == b"ALPHA\nbeta\n"


def test_str_replace_editor_does_not_report_success_when_nothing_was_written(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    binary = tmp_path / "latin1.txt"
    original = b"caf\xe9 one\n"
    binary.write_bytes(original)

    replaced = run(registry.execute("str_replace_editor", {
        "command": "str_replace", "path": str(binary), "old_str": "one", "new_str": "two",
    }))
    assert replaced["code"] == "file_not_utf8"
    assert replaced["output"] == ""
    assert binary.read_bytes() == original

    viewed = run(registry.execute("str_replace_editor", {"command": "view", "path": str(binary)}))
    assert viewed["code"] == "file_not_utf8"
    assert viewed["output"] == ""

    edited = run(registry.execute("edit_file", {"path": str(binary), "old": "one", "new": "two"}))
    assert edited["code"] == "file_not_utf8"
    assert edited["recovery_hint"]



@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX directory permissions")
def test_str_replace_editor_create_reports_a_failed_write(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        created = run(registry.execute("str_replace_editor", {
            "command": "create", "path": str(locked / "new.txt"), "file_text": "text\n",
        }))
    finally:
        locked.chmod(0o755)
    assert created["code"] == "file_write_failed"
    assert created["output"] == ""
    assert not (locked / "new.txt").exists()


def test_str_replace_editor_marks_only_clipped_views_partial(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINIMAL_EDITOR_MAX_OUTPUT_CHARS", "400")
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    target = tmp_path / "long.txt"
    target.write_text("".join(f"row {index}\n" for index in range(200)), encoding="utf-8")
    for index in range(40):
        (tmp_path / f"file-{index:02d}.txt").write_text("", encoding="utf-8")

    clipped_file = run(registry.execute("str_replace_editor", {
        "command": "view", "path": str(target),
    }))
    assert "<response clipped>" in clipped_file["output"]
    assert clipped_file["partial"] is True

    ranged = run(registry.execute("str_replace_editor", {
        "command": "view", "path": str(target), "view_range": [1, 3],
    }))
    assert "<response clipped>" not in ranged["output"]
    assert "     3  row 2" in ranged["output"]
    assert not ranged.get("partial")

    clipped_directory = run(registry.execute("str_replace_editor", {
        "command": "view", "path": str(tmp_path),
    }))
    assert "<response clipped>" in clipped_directory["output"]
    assert clipped_directory["partial"] is True


class _WslSidecar:
    workdir = "."
    current = object()

    async def execute_wsl_shell_stream(self, command: str, on_output=None) -> dict:
        return {
            "output": f"wsl:{command}",
            "error": "",
            "exit_code": 0,
            "environment": "wsl",
        }


def test_minimal_bash_uses_wsl_sidecar_and_keeps_dsh_schema() -> None:
    registry = ToolRegistry()
    register_code_tools(registry, _WslSidecar())

    tool = registry.get("bash")
    assert tool is not None
    assert tool.sandboxed is True
    assert tool.expose_by_default is False
    assert all(
        item["function"]["name"] != "bash"
        for item in registry.to_openai_tools()
    )
    schema = registry.to_openai_tools(names={"bash"})[0]
    assert schema["function"]["parameters"]["required"] == ["command"]
    assert set(schema["function"]["parameters"]["properties"]) == {"command"}
    assert schema["function"]["parameters"]["additionalProperties"] is False

    result = run(tool.fn(command="pwd"))
    assert result == "wsl:pwd"

    with pytest.raises(ValueError, match="non-empty"):
        run(tool.fn(command="  "))


def test_minimal_bash_reports_nonzero_exit_like_dsh() -> None:
    class FailingWslSidecar(_WslSidecar):
        async def execute_wsl_shell_stream(self, command: str, on_output=None) -> dict:
            return {
                "output": "",
                "error": "",
                "exit_code": 1,
                "environment": "wsl",
            }

    registry = ToolRegistry()
    register_code_tools(registry, FailingWslSidecar())

    result = run(registry.get("bash").fn(command="grep missing file"))

    assert result == "[exit code: 1]"


def test_execute_shell_schema_exposes_canonical_environments(monkeypatch) -> None:
    class PosixSandbox:
        workdir = "."

        async def execute_shell(self, command: str, environment: str = "auto") -> dict:
            return {"output": "", "error": "", "exit_code": 0, "environment": environment}

    monkeypatch.setattr("agent.runtime.tools.code.sys.platform", "darwin")
    registry = ToolRegistry()
    register_code_tools(registry, PosixSandbox())

    tool = registry.to_openai_tools(names={"execute_shell"})[0]["function"]
    environment = tool["parameters"]["properties"]["environment"]

    assert environment["enum"] == ["auto", "windows", "wsl", "posix"]
    assert "native POSIX shell" in tool["description"]
    assert "auto uses the native POSIX shell" in environment["description"]


def test_persistent_wsl_timeout_matches_dsh_and_is_configurable(monkeypatch) -> None:
    assert LocalSandbox(timeout=30).persistent_wsl_timeout == 300

    monkeypatch.setenv("ASTRA_WSL_PERSISTENT_TIMEOUT", "45")
    assert LocalSandbox(timeout=30).persistent_wsl_timeout == 45


def test_macos_persistent_bash_uses_bsd_script(monkeypatch) -> None:
    monkeypatch.setattr("agent.sandbox.local.sys.platform", "darwin")

    assert LocalSandbox._persistent_bash_args() == [
        "script", "-q", "/dev/null", "/bin/bash", "--noprofile", "--norc", "-i",
    ]


def test_persistent_bash_timeout_prefers_generic_setting(monkeypatch) -> None:
    monkeypatch.setenv("ASTRA_WSL_PERSISTENT_TIMEOUT", "45")
    monkeypatch.setenv("ASTRA_PERSISTENT_BASH_TIMEOUT", "60")

    sandbox = LocalSandbox(timeout=30)
    assert sandbox.persistent_bash_timeout == 60
    assert sandbox.persistent_wsl_timeout == 60


def test_minimal_bash_uses_native_posix_copy_on_macos(monkeypatch) -> None:
    class PosixSidecar:
        workdir = "."
        current = object()

        async def execute_persistent_bash_stream(self, command: str, on_output=None) -> dict:
            return {"output": command, "error": "", "exit_code": 0, "environment": "posix"}

    monkeypatch.setattr("agent.runtime.tools.code.sys.platform", "darwin")
    registry = ToolRegistry()
    register_code_tools(registry, PosixSidecar())

    tool = registry.get("bash")
    assert "persistent Bash environment" in tool.description
    assert "WSL" not in tool.description
    assert run(tool.fn(command="pwd")) == "pwd"
