"""The tool families every frontend registers for working in a workspace."""

from __future__ import annotations

from typing import Any

from .code import register_code_tools
from .documents import register_document_tools
from .files import register_file_tools
from .git import register_git_tools
from .registry import ToolRegistry
from .time import register_time_tools


def register_workspace_tools(registry: ToolRegistry, sandbox: Any, *, workdir: str, **code_options: Any) -> Any:
    """Register code execution, files, documents, git and time; return the process manager.

    The backend, the agent-lab CLI, the legacy TUI and the API server all call this, so a
    family added here reaches every frontend. Frontend-specific families (browser, computer
    use, delegates, activity) stay in each entry point. ``code_options`` go to
    ``register_code_tools``.
    """
    process_manager = register_code_tools(registry, sandbox, **code_options)
    register_file_tools(registry, workdir=workdir, sandbox=sandbox)
    register_document_tools(registry)
    register_git_tools(registry, workdir=workdir)
    register_time_tools(registry)
    return process_manager
