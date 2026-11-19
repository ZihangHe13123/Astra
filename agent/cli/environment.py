"""Deterministic project .env loading shared by CLI entrypoints."""

from __future__ import annotations

from agent.runtime.paths import env_file, sessions_dir, state_dir

import os
from pathlib import Path

from agent.runtime.process_env import mark_agent_environment


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def load_project_env(project_root: str | Path) -> None:
    """Load project defaults while refreshing the project-owned API key.

    Ordinary process variables keep their normal higher precedence, but the
    key in the project .env is refreshed on every launch so a stale parent
    shell cannot silently authenticate the new backend with an old secret.
    """
    # Mark this runtime before optional imports or subprocess initialization.
    # AI_AGENT preserves an outer harness such as Codex; ASTRA_AGENT records
    # that this process and its descendants passed through Astra.
    mark_agent_environment()
    try:
        from dotenv import dotenv_values, load_dotenv
    except ImportError:
        return
    path = env_file(Path(project_root))
    load_dotenv(path, override=False)
    values = dotenv_values(path)
    if not os.environ.get("SANDBOX_WORKDIR", "").strip() and os.environ.get("ASTRA_WORKSPACE", "").strip():
        os.environ["SANDBOX_WORKDIR"] = os.environ["ASTRA_WORKSPACE"]
    # Legacy source configurations use .astra/... and .sessions/... values.
    # Resolve only these known application-state settings against the selected
    # profile; an installed wheel must never write into site-packages.
    state_settings = (
        "AGENT_SETTINGS_PATH", "AGENT_SKILLS_PATH", "AGENT_USER_MODELS_FILE",
        "AGENT_FILESYSTEM_CONFIG", "AGENT_MCP_CONFIG", "TOOL_RESULT_DIR",
        "AGENT_MEMORY_PATH", "AGENT_LEARNING_PATH", "AGENT_TASK_DB", "AGENT_BROWSER_DB",
        "ASTRA_EVENT_DB", "ASTRA_APPROVAL_DB", "ASTRA_ACTIVITY_DB", "ASTRA_SESSION_RECALL_DB",
    )
    for name in state_settings:
        configured = os.environ.get(name, "").replace("\\", "/")
        if configured.startswith(".astra/"):
            os.environ[name] = str(state_dir(Path(project_root)) / configured.removeprefix(".astra/"))
        elif configured.startswith(".sessions/"):
            os.environ[name] = str(sessions_dir(Path(project_root)) / configured.removeprefix(".sessions/"))
    # Astra is direct by default. Parent applications (for example Codex) may
    # carry proxy variables for their own route; do not silently pass those to
    # the model clients or MCP subprocesses. A proxy remains available as an
    # explicit Astra project setting by declaring it in this .env file.
    for key_name in _PROXY_ENV_KEYS:
        os.environ.pop(key_name, None)
    for key_name in _PROXY_ENV_KEYS:
        value = values.get(key_name)
        if value is not None and str(value).strip():
            os.environ[key_name] = str(value).strip()
    # One-time compatibility for installations where adding Qwen 3.8
    # previously replaced the shared LLM_API_KEY. A dedicated Qwen key in
    # .env always wins; otherwise preserve the existing value during migration.
    if not values.get("QWEN38_API_KEY") and values.get("LLM_API_KEY"):
        values["QWEN38_API_KEY"] = values["LLM_API_KEY"]
    for key_name in (
        "DEEPSEEK_API_KEY",
        "QWEN38_API_KEY",
        "ZHIPU_API_KEY",
        "LLM_API_KEY",
        "EXA_API_KEY",
    ):
        key = values.get(key_name)
        if key is not None:
            os.environ[key_name] = str(key).strip()


def enter_workspace() -> None:
    """Make the workspace the current directory, as the desktop does when it starts a backend.

    Call after load_project_env: the workspace is SANDBOX_WORKDIR, set explicitly in the .env or
    filled in from the launcher's ASTRA_WORKSPACE. The launcher starts the terminal interfaces in the
    installation, and everything that takes the current directory for the workspace (file, git and
    image tools, project memories, the Context Index, project trust) would otherwise work there.
    """
    configured = os.environ.get("SANDBOX_WORKDIR", "")
    if not configured.strip():
        return
    workspace = os.path.abspath(os.path.expanduser(configured))
    try:
        os.chdir(workspace)
    except OSError as exc:
        raise SystemExit(f"Astra cannot work in {workspace}: {exc.strerror or exc}. "
                         "Set SANDBOX_WORKDIR to an existing folder.") from exc
    # A relative setting was relative to the starting directory, which is no longer the current one.
    os.environ["SANDBOX_WORKDIR"] = workspace
