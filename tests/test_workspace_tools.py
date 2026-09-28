import ast
from pathlib import Path

from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.workspace import register_workspace_tools
from agent.sandbox.local import LocalSandbox

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = ("agent/cli/backend.py", "agent/cli/main.py", "agent/cli/tui_app.py", "agent/cli/api_server.py")
WORKSPACE_FAMILIES = {"register_code_tools", "register_file_tools", "register_document_tools",
                      "register_git_tools", "register_time_tools"}


def called_names(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return {node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


def test_every_entry_point_registers_workspace_tools_in_one_place():
    for relative in ENTRY_POINTS:
        calls = called_names(relative)
        assert "register_workspace_tools" in calls, relative
        assert not calls & WORKSPACE_FAMILIES, (relative, calls & WORKSPACE_FAMILIES)


def test_workspace_tools_cover_code_files_documents_git_and_time(tmp_path):
    registry = ToolRegistry()
    register_workspace_tools(registry, LocalSandbox(timeout=5, workdir=str(tmp_path)), workdir=str(tmp_path))
    assert {"execute_python", "read_file", "doc_edit", "git_status", "current_time"} <= set(registry.tool_names)
