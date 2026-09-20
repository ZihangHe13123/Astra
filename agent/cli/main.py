"""CLI 主入口 — 流式输出 + ANSI 渲染 + 会话持久化 + Git/Docker + 状态栏

支持两种模式:
  python -m agent.cli.main           # 普通 CLI（默认）
  python -m agent.cli.main --tui     # TUI 模式（需 textual）
  python -m agent.cli.main --ink     # Ink TUI 模式（需 Node.js）
"""

import os
import sys
import asyncio
import inspect
import signal
import shlex
import platform
from pathlib import Path

from ..runtime.process_env import hidden_process_creationflags
from ..runtime.appshot_media import AppshotMediaError
from ..logging_config import setup_logging
from ..core.msg import Msg
from ..core.bus import MessageBus
from ..runtime.llm import LLMClient, LLMConfig
from ..runtime.react import ReActAgent
from ..runtime.harness import build_health_report
from ..runtime.mcp import initialize_mcp_tools
from ..runtime.tracing import configure_tracing
from ..runtime.task_store import (
    TaskStore,
    format_task_detail,
    format_task_list,
    format_today,
)
from ..runtime.tools.registry import ToolRegistry
from ..runtime.tools.code import register_code_tools
from ..runtime.tools.files import register_file_tools
from ..runtime.tools.git import register_git_tools
from ..runtime.tools.time import register_time_tools
from ..runtime.tools.web import register_web_tools, create_browser_extract_fn, create_browser_status_fn
from ..runtime.tools.image import register_image_tools, select_vision_tiles_from_holder
from ..runtime.tools.computer import register_local_computer_runtime
from ..runtime.tools.browser import register_browser_tools
from ..runtime.cdp_backend import CdpBrowserBackend, find_chrome
from ..runtime.tools.memory import register_memory_tools
from ..runtime.memory import MemoryStore
from ..runtime.skills import SkillStore
from ..runtime.tools.skills import register_skill_tools
from ..runtime.tools.session_recall import register_session_recall_tools
from ..runtime.tools.activity import register_activity_tools
from ..runtime.context_index import create_context_index_broker
from ..runtime.tools.context_index import register_context_index_tools
from ..runtime.learning import LearningReviewer, LearningStore
from ..sandbox.local import LocalSandbox
from ..sandbox.docker import DockerSandbox
from ..sandbox.router import SandboxRouter
from .images import build_image_message_content, build_user_message_content
from .models import context_limit_for_model, model_profiles, prompt_token_budget
from .model_catalog import parse_model_command_argument
from .model_preferences import resolve_startup_model, read_selected_model
from .model_catalog import configured_model_catalog, discover_model_catalog
from .mode_preferences import (
    MODE_USAGE,
    REASONING_EFFORTS,
    apply_reasoning_effort,
    reasoning_effort_status,
    set_reasoning_effort,
)
from .connections import create_probe_and_switch, is_local_url, switch_to_profile
from .diagnostics import build_doctor_report
from .environment import load_project_env
from .persona_preferences import save_selected_persona, startup_persona
from .sandbox_preferences import execute_sandbox_command, resolve_startup_sandbox_mode
from .vision_tile_preferences import (
    execute_vision_tiles_command,
    load_vision_tiles_enabled,
)
from .context_index_commands import execute_context_index_command
from .context_index_preferences import load_context_index_preferences
from agent.cli.browser_commands import execute_browser_command
from .computer_commands import execute_computer_command
from .memory_commands import execute_memory_command
from .skill_commands import execute_skill_command
from .learning_commands import execute_learning_command
from .conversation_commands import register_conversation_tools
from ..runtime.command_workflows import workflow_message, raw_command as normalize_direct_command
from ..runtime.tools.conclave import register_conclave_tools
from ..runtime.prompts import get_prompt_profile, prompt_profiles
from .render import render_text, render_tool_block, StreamRenderer, ReasoningRenderer, render_status_bar
from .search_preferences import SEARCH_PROVIDERS, resolve_startup_search_provider, save_selected_search_provider

# run_tui 懒加载，见 _run_tui_mode()
from .sessions import (
    SessionNameError,
    delete_session,
    export_session_markdown,
    list_sessions,
    rename_session,
    session_exists,
    session_msg_count,
    startup_session_path,
    session_path,
)

def _resolve_context_limit_simple(model: str, configured_limit: int | None = None) -> int:
    env_limit = os.getenv("LLM_CONTEXT_LIMIT", "").strip()
    if env_limit and env_limit.isdigit():
        return int(env_limit)
    return configured_limit or context_limit_for_model(model)


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


_IS_WINDOWS = platform.system() == "Windows"


def _local_sandbox(timeout: int, workdir: str) -> LocalSandbox:
    return LocalSandbox(
        timeout=timeout,
        workdir=workdir,
        max_output_bytes=_int_env("LOCAL_SANDBOX_MAX_OUTPUT_BYTES", 200_000),
        max_memory_mb=_optional_int_env("LOCAL_SANDBOX_MAX_MEMORY_MB"),
        max_cpu_seconds=_optional_int_env("LOCAL_SANDBOX_MAX_CPU_SECONDS"),
    )


async def _docker_available() -> bool:
    """异步检查 Docker 是否可用"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=hidden_process_creationflags(),
        )
        code = await asyncio.wait_for(proc.wait(), timeout=10)
        return code == 0
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return False


# ── CLI ────────────────────────────────────────────────────────────

def print_banner():
    print(r"""
  ╔══════════════════════════════════════════╗
  ║         Agent System  v0.2               ║
  ║     message-driven · tool-using          ║
  ╚══════════════════════════════════════════╝
    """)
    print("  \033[90mType 'exit' to quit, '/help' for commands\033[0m\n")


def _print_status_bar(agent: ReActAgent, status: str = "ready"):
    """打印底部状态栏"""
    ctx = agent.context
    used = ctx.estimate_prompt_tokens()
    limit = ctx.max_prompt_tokens
    pct = (used / limit * 100) if limit > 0 else 0.0
    bar = render_status_bar(
        status=status,
        model=agent.llm.config.model,
        context_used=used,
        context_limit=limit,
        context_used_pct=pct,
        session_tokens=ctx.total_tokens,
    )
    print(f"\n{bar}\n")


async def cli_loop(bus: MessageBus, agent: ReActAgent):
    print_banner()
    _print_status_bar(agent)

    # 启动时提示思维链状态
    if agent.context.show_reasoning:
        print("  \033[90m[Thinking display: ON — use /think to toggle]\033[0m\n")

    while True:
        try:
            raw = (await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("\033[1mYou: \033[0m"))).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw.lower() in ("exit", "quit"):
            print("Bye!")
            break
        resume_msg: Msg | None = None
        content = build_user_message_content("")
        if raw.startswith("/image"):
            try:
                parts = shlex.split(raw, posix=False)
                if len(parts) < 2:
                    print("  \033[33mUsage: /image <path> [prompt]\033[0m\n")
                    continue
                path = parts[1].strip("\"'")
                prompt = raw[raw.find(parts[1]) + len(parts[1]):].strip()
                content = build_image_message_content(
                    path,
                    prompt,
                    max_bytes=_int_env("MAX_IMAGE_UPLOAD_BYTES", 10 * 1024 * 1024),
                )
            except Exception as e:
                print(f"  \033[33m{e}\033[0m\n")
                continue
        elif raw.startswith("/"):
            resume_msg = await handle_slash(raw, agent)
            _print_status_bar(agent)
            if resume_msg is None:
                continue
        else:
            content = build_user_message_content(
                raw,
                max_bytes=_int_env("MAX_IMAGE_UPLOAD_BYTES", 10 * 1024 * 1024),
            )

        msg = resume_msg or Msg(sender="user", role="user", content=content)
        task_id = str(msg.metadata.get("task_id") or "")
        if not task_id and agent.task_store is not None:
            run = agent.task_store.start_run(
                msg.metadata.get("request_id") or msg.id,
                raw,
                session_id=Path(agent.context.session_path).stem if agent.context.session_path else "",
                model=agent.llm.config.model,
            )
            task_id = run["id"]
            msg.metadata["task_id"] = task_id
        failed_message = ""
        was_cancelled = False
        text_renderer = StreamRenderer()
        reasoning_renderer = ReasoningRenderer()
        try:
            print()
            async for event in bus.send_stream("agent", msg):
                if event.get("type") == "error":
                    failed_message = event.get("message", "Request failed")
                    was_cancelled = bool(event.get("cancelled"))
                await handle_event(event, text_renderer, reasoning_renderer)
            # 刷新两个渲染器的缓冲区
            tail_text = text_renderer.flush()
            if tail_text:
                print(tail_text, end="", flush=True)
            tail_reasoning = reasoning_renderer.flush()
            if tail_reasoning:
                print(tail_reasoning, end="", flush=True)
            print()
        except Exception as e:
            failed_message = str(e)
            print(f"\n\033[31m[Error] {e}\033[0m\n")
        finally:
            agent.context.save()  # 无论是否报错都保存
            if task_id and agent.task_store is not None:
                if was_cancelled:
                    agent.task_store.finish_run(task_id, "cancelled", failed_message or "Cancelled by user")
                elif failed_message:
                    agent.task_store.finish_run(task_id, "failed", failed_message)
                else:
                    agent.task_store.finish_run(task_id, "completed")

        # 打印状态栏
        _print_status_bar(agent)

    # Auto-generate handoff on session exit
    _auto_handoff(agent)
    # Run memory consolidation on exit (silent unless something changed)
    _run_consolidation(agent, verbose=True)


def _run_consolidation(agent: ReActAgent, *, verbose: bool = False) -> dict[str, int] | None:
    """Run memory consolidation passes. Returns stats or None if unavailable."""
    try:
        if agent.memory_store is None:
            return None
        from ..runtime.memory_consolidation import MemoryConsolidator
        consolidator = MemoryConsolidator(agent.memory_store.record_store)
        stats = consolidator.run_all()
        if verbose and any(v > 0 for v in stats.values()):
            parts = []
            if stats["expired"]:
                parts.append(f"{stats['expired']} expired")
            if stats["merged_observations"]:
                parts.append(f"{stats['merged_observations']} merged")
            if stats["extracted_observations"]:
                parts.append(f"{stats['extracted_observations']} extracted")
            print(f"  \033[90m🧹 Memory consolidation: {', '.join(parts)}\033[0m")
        return stats
    except Exception:
        return None  # Never block on consolidation failure


def _auto_handoff(agent: ReActAgent) -> None:
    """Generate and persist a session handoff document on exit."""
    try:
        from ..runtime.session_handoff import generate_handoff, save_handoff
        session_id = Path(agent.context.session_path).stem if agent.context.session_path else "default"
        handoff_text = generate_handoff(
            session_id=session_id,
            task_store=agent.task_store,
            messages=[m if isinstance(m, dict) else {"role": m.role, "content": m.get_text()} for m in agent.context.messages],
            model=agent.llm.config.model,
            persona_id=agent.context.persona_id or "",
        )
        path = save_handoff(handoff_text, session_id=session_id)
        print(f"  \033[90m📋 Handoff saved: {path}\033[0m")
    except Exception:
        pass  # Never block exit on handoff failure


async def handle_event(
    event: dict,
    text_renderer: StreamRenderer | None = None,
    reasoning_renderer: ReasoningRenderer | None = None,
):
    t = event.get("type")
    if t == "reasoning":
        if reasoning_renderer:
            rendered = reasoning_renderer.feed(event["content"])
            if rendered:
                print(rendered, end="", flush=True)
    elif t == "chunk":
        if text_renderer:
            rendered = text_renderer.feed(event["content"])
            if rendered:
                print(rendered, end="", flush=True)
        else:
            print(render_text(event["content"]), end="", flush=True)
    elif t == "tool_result":
        card = render_tool_block(
            event.get("name", "tool"),
            code=event.get("code", ""),
            output=event.get("output", ""),
            error=event.get("error", ""),
            duration_ms=event.get("duration_ms", 0),
        )
        print(f"\n{card}\n", flush=True)


async def handle_slash(cmd: str, agent: ReActAgent) -> Msg | None:
    try:
        workflow = workflow_message(cmd)
        if workflow is not None:
            if getattr(agent, "tool_allowlist", None) is not None:
                print("  Return to the work session before starting a command workflow.")
                return None
            return workflow
        cmd = normalize_direct_command(cmd)
        parts = shlex.split(cmd.strip())
    except ValueError as e:
        print(f"  \033[33mInvalid command: {e}\033[0m\n")
        return
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/reset":
        if agent.memory_store is not None:
            agent.memory_store.clear_working(Path(agent.context.session_path).stem or "default")
        agent.end_session("reset")
        agent.reset_conversation()
        agent.context.save()
        agent.begin_session()
        learning_store = getattr(agent, "_learning_store", None)
        if learning_store is not None:
            learning_store.reset_review_counter(Path(agent.context.session_path).stem or "default")
        print("  \033[90mContext cleared.\033[0m\n")

    elif command == "/compress":
        before = len(agent.context.messages)
        await agent.context.compress_if_needed(force=True)
        after = len(agent.context.messages)
        savings = before - after
        if savings > 0:
            print(f"  \033[90mCompressed: {before} -> {after} messages ({savings} removed)\033[0m\n")
        else:
            print(f"  \033[90mNothing to compress ({before} messages, {after} kept)\033[0m\n")
        # Also run memory consolidation alongside compression
        _run_consolidation(agent, verbose=True)

    elif command == "/budget":
        from agent.cli.turn_budget import execute_budget_command
        output, error = execute_budget_command(agent, cmd[len("/budget"):].strip())
        print(f"  {error or output}\n")

    elif command == "/changes":
        from agent.cli.turn_changes_command import execute_changes_command
        output, error = execute_changes_command(agent, cmd[len("/changes"):].strip())
        print(f"  {error or output}\n")

    elif command == "/think":
        agent.context.show_reasoning = not agent.context.show_reasoning
        state = "ON" if agent.context.show_reasoning else "OFF"
        print(f"  \033[90mThinking display: {state}\033[0m\n")

    elif command == "/browser":
        output, error = await execute_browser_command(parts[1:], agent.tools)
        print(f"  {error or output}\n")

    elif command == "/computer":
        computer_runtime = getattr(agent, "_computer_runtime", None)
        output, error = await execute_computer_command(parts[1:], computer_runtime)
        print(f"  {error or output}\n")

    elif command == "/mode":
        argument = cmd[len("/mode"):].strip().lower()
        if not argument:
            print(f"  {reasoning_effort_status(agent.llm.config)}\n  {MODE_USAGE}\n")
        elif argument in REASONING_EFFORTS:
            set_reasoning_effort(agent.llm, argument)
            print(f"  {reasoning_effort_status(agent.llm.config)} (saved as startup default)\n")
        else:
            print(f"  Unknown reasoning effort: {argument}. {MODE_USAGE}\n")

    elif command == "/session":
        try:
            if arg == "delete":
                target = parts[2] if len(parts) > 2 else ""
                if not target:
                    print("  \033[33mUsage: /session delete <name>\033[0m\n")
                    return
                if Path(agent.context.session_path).resolve() == session_path(target).resolve():
                    raise SessionNameError("Switch to another session before deleting the active session.")
                delete_session(target)
                if agent.memory_store is not None:
                    agent.memory_store.clear_working(target)
                print(f"  \033[90mSession '{target}' deleted.\033[0m\n")

            elif arg == "rename":
                old_name = parts[2] if len(parts) > 2 else ""
                new_name = parts[3] if len(parts) > 3 else ""
                if not old_name or not new_name:
                    print("  \033[33mUsage: /session rename <old> <new>\033[0m\n")
                    return
                old_path = session_path(old_name)
                rename_session(old_name, new_name)
                if agent.memory_store is not None:
                    agent.memory_store.rename_working(old_name, new_name)
                new_path = session_path(new_name)
                if Path(agent.context.session_path).resolve() == old_path.resolve():
                    agent.context.set_session(str(new_path))
                print(f"  \033[90mSession renamed: '{old_name}' -> '{new_name}'.\033[0m\n")

            elif arg == "export":
                target = parts[2] if len(parts) > 2 else Path(agent.context.session_path).stem
                out = export_session_markdown(target)
                print(f"  \033[90mSession '{target}' exported to {out}\033[0m\n")

            elif arg:
                target = arg
                target_path = session_path(target)
                exists = session_exists(target)

                agent.context.save()
                candidate = agent.context.stage_session(str(target_path))
                agent.end_session("session_switch")
                agent.reset_conversation()
                agent.context.adopt_session(candidate)
                agent.begin_session()

                if exists:
                    count = len(agent.context.messages) // 2
                    msg = f"Switched to session '{target}' ({count} previous exchanges)."
                else:
                    agent.context.save()
                    msg = f"Switched to new session '{target}'."
                print(f"  \033[90m{msg}\033[0m\n")

            else:
                current_path = Path(agent.context.session_path).resolve()
                sessions = list_sessions()
                print(f"  \033[90mSessions ({len(sessions)}):\033[0m")
                for s in sessions:
                    sp = session_path(s).resolve()
                    marker = " \033[1m*\033[0m" if sp == current_path else "  "
                    count = session_msg_count(s)
                    print(f"    {marker} \033[36m{s}\033[0m ({count} msgs)")
                print()
        except (SessionNameError, OSError, AppshotMediaError) as e:
            print(f"  \033[33m{e}\033[0m\n")

    elif command == "/prompt":
        if arg:
            prompt = cmd.strip()[len(parts[0]):].strip()
            agent.set_system_prompt(prompt)
            print("  \033[90mSystem prompt updated for this session. Use /persona to switch saved profiles.\033[0m\n")
        else:
            chars = len(agent.context.system_prompt)
            print(f"  \033[90mCurrent system prompt ({chars} chars):\033[0m\n")
            for line in agent.context.system_prompt.strip().split("\n")[:14]:
                print(f"  {line}")
            print()

    elif command == "/persona":
        profiles = prompt_profiles()
        if arg:
            if arg in profiles:
                profile = get_prompt_profile(arg)
                state = profile.default_state()
                agent.set_persona(state, profile.system_prompt(state))
                settings_path = save_selected_persona(arg)
                print(f"  \033[90mPersona switched to: \033[36m{arg}\033[0m")
                print(f"  \033[90mSaved as startup default in {settings_path}.\033[0m\n")
            else:
                print(f"  \033[33mUnknown persona: {arg}. Available:\033[0m")
                for name, profile in profiles.items():
                    print(f"    \033[36m{name}\033[0m — {profile.description}")
                print()
        else:
            print("  \033[90mAvailable personas:\033[0m")
            for name, profile in profiles.items():
                marker = " \033[1m*\033[0m" if name == agent.context.persona_id else "  "
                print(f"    {marker} \033[36m{name}\033[0m — {profile.description}")
            print()

    elif command == "/tools":
        details = agent.tools.describe()
        print(f"  \033[90mRegistered tools ({len(details)}):\033[0m")
        for item in sorted(details, key=lambda value: value["name"]):
            print(f"    \033[36m{item['name']}\033[0m  [{item['risk']}]")
        print()

    elif command == "/memory":
        store = agent.memory_store
        if store is None:
            print("  Memory is unavailable.\n")
        else:
            output, error = execute_memory_command(
                store,
                parts[1:],
                session_id=Path(agent.context.session_path).stem or "default",
                router=agent.memory_router,
                retainer=agent.memory_retainer,
            )
            print(f"  {error or output}\n")

    elif command == "/skills":
        store = agent.skill_store
        if store is None:
            print("  Skills are unavailable.\n")
        else:
            output, error = execute_skill_command(store, parts[1:])
            print(f"  {error or output}\n")

    elif command == "/learn":
        store = getattr(agent, "_learning_store", None)
        reviewer = getattr(agent, "_learning_reviewer", None)
        if store is None or reviewer is None or agent.memory_store is None or agent.skill_store is None:
            print("  Learning Review is unavailable.\n")
        else:
            output, error = await execute_learning_command(
                store,
                reviewer,
                agent.memory_store,
                agent.skill_store,
                parts[1:],
                session_id=Path(agent.context.session_path).stem or "default",
                messages=list(agent.context.messages),
            )
            print(f"  {error or output}\n")

    elif command == "/search":
        state = getattr(agent, "_search_provider_state", None)
        if state is None:
            print("  Search provider control is unavailable.\n")
        elif arg:
            if arg.lower() in SEARCH_PROVIDERS:
                selected = state.set(arg)
                settings_path = save_selected_search_provider(selected)
                print(f"  Search provider switched to: {selected}")
                print(f"  Saved as startup default in {settings_path}.\n")
            else:
                print(f"  Unknown search provider: {arg}. Available: {', '.join(SEARCH_PROVIDERS)}\n")
        else:
            print(f"  Current search provider: {state.provider}")
            print("  Available: " + ", ".join(SEARCH_PROVIDERS) + "\n")

    elif command == "/health":
        print(build_health_report(agent.llm.config, agent.tools, agent.context.max_prompt_tokens))
        print()

    elif command == "/doctor":
        print(await build_doctor_report(
            agent,
            getattr(agent, "_sandbox", None),
            getattr(agent, "_mcp_manager", None),
            arg,
        ))
        print()

    elif command == "/diagnostics":
        from .diagnostics import build_runtime_diagnostics
        print(build_runtime_diagnostics(agent, mcp_manager=getattr(agent, "_mcp_manager", None),
                                        task_store=agent.task_store, section=arg))
        print()

    elif command == "/sandbox":
        router = getattr(agent, "_sandbox", None)
        if not isinstance(router, SandboxRouter):
            print("  Sandbox switching is unavailable.\n")
        else:
            output, error = execute_sandbox_command(router, parts[1:])
            print(f"  {error or output}\n")

    elif command == "/vision-tiles":
        output, error = execute_vision_tiles_command(agent, parts[1:])
        print(f"  {error or output}\n")

    elif command == "/context-index":
        output, error = execute_context_index_command(agent, parts[1:])
        print(f"  {error or output}\n")

    elif command == "/mcp":
        manager = getattr(agent, "_mcp_manager", None)
        print(manager.report() if manager is not None else "MCP is not initialized.")
        print()

    elif command == "/permissions":
        action = arg.lower()
        value = parts[2] if len(parts) > 2 else ""
        try:
            if action == "allow" and value:
                agent.tools.policy.allow(value)
                print(f"  Approved for this process: {value}\n")
            elif action == "deny" and value:
                agent.tools.policy.deny(value)
                print(f"  Denied for this process: {value}\n")
            elif action == "mode" and value:
                agent.tools.policy.set_mode(value)
                print(f"  Tool policy mode: {agent.tools.policy.mode}\n")
            else:
                print(f"  Tool policy: {agent.tools.policy.mode}")
                print("  Usage: /permissions mode <permissive|safe|locked>")
                print("         /permissions allow|deny <tool>\n")
        except ValueError as exc:
            print(f"  \033[33m{exc}\033[0m\n")

    elif command == "/yolo":
        agent.tools.yolo = not agent.tools.yolo
        status = "ON" if agent.tools.yolo else "OFF"
        print(f"  YOLO mode {status} — all approvals bypassed\n")

    elif command == "/tasks":
        if agent.task_store is None:
            print("  Task persistence is disabled.\n")
        elif arg:
            task = agent.task_store.get_task(arg)
            print((format_task_detail(task) if task else f"Unknown task: {arg}") + "\n")
        else:
            print(format_task_list(agent.task_store.list_tasks()) + "\n")

    elif command == "/today":
        if agent.task_store is None:
            print("  Task persistence is disabled.\n")
        else:
            print(format_today(agent.task_store) + "\n")

    elif command == "/cancel":
        if agent.task_store is None:
            print("  Task persistence is disabled.\n")
        elif not arg:
            print("  Usage: /cancel <task-id> (Ink TUI also supports /cancel during execution)\n")
        else:
            task = agent.task_store.get_task(arg)
            if task is None:
                print(f"  Unknown task: {arg}\n")
            elif task["status"] in {"completed", "failed", "cancelled"}:
                print(f"  Task {arg} is already {task['status']}.\n")
            else:
                agent.task_store.request_cancel(arg)
                agent.task_store.finish_run(arg, "cancelled", "Cancelled from CLI")
                print(f"  Task {arg} marked cancelled.\n")

    elif command == "/resume":
        if agent.task_store is None:
            print("  Task persistence is disabled.\n")
        elif not arg:
            print("  Usage: /resume <task-id>\n")
        else:
            task = agent.task_store.get_task(arg)
            current_session = Path(agent.context.session_path).stem if agent.context.session_path else ""
            if task is None:
                print(f"  Unknown task: {arg}\n")
            elif task.get("session_id") and task["session_id"] != current_session:
                print(f"  Task belongs to session '{task['session_id']}'. Switch to it before resuming.\n")
            else:
                try:
                    resumed = agent.task_store.prepare_resume(arg)
                except (KeyError, ValueError) as exc:
                    print(f"  {exc}\n")
                else:
                    prompt = (
                        "Continue the interrupted task from the existing conversation and checkpoint. "
                        "Do not repeat completed tool calls; inspect prior results before acting.\n\n"
                        f"Original task: {resumed['input_text']}"
                    )
                    msg = Msg(sender="user", role="user", content=build_user_message_content(prompt))
                    msg.metadata.update({"request_id": resumed["request_id"], "task_id": resumed["id"]})
                    print(f"  Resuming task {arg} from checkpoint.\n")
                    return msg

    elif command == "/connect":
        if len(parts) == 1:
            from .connections import prompt_provider_connection
            await prompt_provider_connection(agent)
        elif len(parts) < 3:
            print("  Usage: /connect or /connect <model-name> <base-url> [api-key-env]\n")
        else:
            name, base_url = parts[1], parts[2]
            api_key_env = parts[3] if len(parts) > 3 else "LLM_API_KEY"
            probe, settings_path = await create_probe_and_switch(agent, name, base_url, api_key_env)
            if probe.ok:
                print(f"  {probe.message}")
                print(f"  Saved profile and startup selection in {settings_path}.\n")
            else:
                print(f"  \033[33mConnection failed; current model unchanged: {probe.message}\033[0m\n")

    elif command == "/model":
        catalog = configured_model_catalog()
        model_arg = parse_model_command_argument(cmd)
        if not model_arg or model_arg.endswith("::"):
            provider_id = model_arg[:-2] if model_arg else None
            catalog = await discover_model_catalog(provider_id=provider_id, force=False)
            for entry in catalog.entries:
                print(f"  {entry.key} · {entry.source}")
            for provider_id, error in catalog.errors.items():
                print(f"  {provider_id}: {error}")
        else:
            entry = catalog.resolve(model_arg) or catalog.resolve_persisted(model_arg)
            if entry is None:
                print("  Unknown model. Use /model or /model <provider>::<model ID>.")
            else:
                try:
                    switch_to_profile(agent, entry.key, entry.profile, valid_models={entry.key})
                    print(f"  Selected {entry.key}; saved as startup default.")
                except (ValueError, OSError) as exc:
                    print(f"  Model switch rejected; current model unchanged: {exc}")

    elif command == "/handoff":
        from ..runtime.session_handoff import generate_handoff, save_handoff
        session_id = Path(agent.context.session_path).stem if agent.context.session_path else "default"
        notes = cmd.strip()[len(parts[0]):].strip()
        handoff_text = generate_handoff(
            session_id=session_id,
            task_store=agent.task_store,
            messages=[m if isinstance(m, dict) else {"role": m.role, "content": m.get_text()} for m in agent.context.messages],
            model=agent.llm.config.model,
            persona_id=agent.context.persona_id or "",
            extra_notes=notes,
        )
        path = save_handoff(handoff_text, session_id=session_id)
        print(f"  \033[90mHandoff saved to {path}\033[0m\n")
        print(handoff_text)
        print()

    elif command == "/help":
        print("""  \033[90mCommands:
    /reset              — Clear current conversation
    /session            — List all sessions
    /session <name>     — Switch to (or create) a session
    /session delete <n> — Delete a session
    /session rename <o> <n>  Rename a session
    /session export [n] — Export a session to Markdown
    /yolo               — Toggle YOLO mode (bypass all approvals)
    /compress           — Force context compression (LLM summary)
    /think              — Toggle thinking (reasoning) display
    /budget [secs|off]   — Set a shared wall-clock budget for subsequent turns
    /changes [@k] [n|path] — Review the change ledger of recent turns
    /prompt             — View system prompt
    /prompt ...         — Set system prompt
    /persona [name]     — View / switch saved prompt persona
    /model [name]       — Show / switch model (deepseek-flash, deepseek-v4-pro)
    /search [provider]  — Show / switch search provider (auto, exa, searxng)
    /memory             — Inspect or update working/core memory
    /skills             — List, inspect, or create local procedural skills
    /learn              — Save skills; discuss /learn review before applying changes
    /connect <n> <url>  — Probe, save and switch a model connection
    /tools              — List available tools
    /browser [status|stop] — Inspect or release this session’s browser control
    /computer [status|setup|stop] — Inspect, set up, or stop local macOS Computer Use
    /health             — Show harness diagnostics
    /doctor             — Discuss diagnosis and repairs; --raw for a direct report
    /diagnostics        — Explain a runtime snapshot; --raw or json for direct output
    /memory review      — Discuss memory evidence and proposed corrections
    /conclave <question> — Research and continue discussing the findings
    /sandbox [on|off]   — Show or switch Docker isolation at runtime
    /vision-tiles [on|off]  Show or switch DeepSeek original-pixel tiling
    /context-index [on|off|session|all|status|why]  Configure proactive local context suggestions
    /mcp                — Show MCP server status
    /permissions        — Inspect or change process-local tool policy
    /tasks [id]         — List per-request TaskRuns or inspect steps/checkpoint
    /today              — Dashboard: blocked, scheduled, and recent tasks
    /handoff [notes]    — Compose and save a handoff; --raw for a direct snapshot
    /resume <id>        — Resume an interrupted/failed/cancelled task
    /cancel <id>        — Mark a saved task cancelled
    /help               — Show this help
    exit                — Quit
\033[0m""")

    else:
        print(f"  \033[90mUnknown: {command}. Type /help\033[0m\n")


# ── Init ───────────────────────────────────────────────────────────

async def _init_sandbox(mode: str, timeout: int, workdir: str):
    """初始化沙箱

    mode 取值:
      "auto"  — 优先 Docker，不可用则 fallback 到本地
      "true"  — 强制 Docker，不可用则报错
      "false" — 强制本地
    """
    # Windows 上除非强制 Docker，否则默认用本地沙箱
    if _IS_WINDOWS and mode != "true":
        return _local_sandbox(timeout, workdir)

    if mode == "false":
        return _local_sandbox(timeout, workdir)

    docker_ok = await _docker_available()
    if not docker_ok:
        if mode == "true":
            print("  \033[31m[Docker not available (SANDBOX_DOCKER=true)]\033[0m")
        else:
            # mode == "auto"
            print("  \033[33m[Docker not available, using local sandbox]\033[0m")
        return _local_sandbox(timeout, workdir)

    docker_sb = DockerSandbox(timeout=timeout, workdir=workdir,
                               network=_truthy_env("DOCKER_NETWORK"),
                               reuse_container=_truthy_env("DOCKER_REUSE_CONTAINER"))
    if mode == "true":
        return docker_sb

    try:
        test = await docker_sb.execute_python("print('docker ok')")
        if test["exit_code"] == 0:
            return docker_sb
        else:
            print(f"  \033[33m[Docker available but Python test failed: {test['error'][:50]}]\033[0m")
    except Exception as e:
        print(f"  \033[33m[Docker Python test failed: {e}]\033[0m")

    return _local_sandbox(timeout, workdir)


async def _async_init(llm_config: LLMConfig, sandbox_timeout: int, workdir: str):
    bus = MessageBus()
    use_docker = resolve_startup_sandbox_mode(os.getenv("SANDBOX_DOCKER", "auto").lower())
    initial_sandbox = await _init_sandbox(use_docker, sandbox_timeout, workdir)
    sandbox = SandboxRouter(
        initial_sandbox,
        local_factory=lambda: _local_sandbox(sandbox_timeout, workdir),
        docker_factory=lambda: DockerSandbox(
            timeout=sandbox_timeout,
            workdir=workdir,
            network=_truthy_env("DOCKER_NETWORK"),
            reuse_container=_truthy_env("DOCKER_REUSE_CONTAINER"),
        ),
    )

    tools = ToolRegistry()
    memory_store = MemoryStore()
    skill_store = SkillStore()
    context_index_broker = create_context_index_broker(
        load_context_index_preferences(), workdir
    )
    context_index_broker.start_background(memory_store.path)
    agent_holder = {}
    register_code_tools(tools, sandbox)
    register_file_tools(tools, workdir=workdir, sandbox=sandbox)
    register_git_tools(tools, workdir=workdir)
    register_time_tools(tools)
    register_memory_tools(
        tools,
        memory_store,
        session_id=lambda: Path(agent_holder["agent"].context.session_path).stem if agent_holder else "default",
    )
    register_skill_tools(
        tools, skill_store,
        learning_getter=lambda: agent_holder["agent"]._learning_reviewer.lifecycle,
        messages=lambda: list(agent_holder["agent"].context.messages) if agent_holder else [],
        session_id=lambda: Path(agent_holder["agent"].context.session_path).stem if agent_holder else "default",
    )
    register_session_recall_tools(tools)
    register_activity_tools(tools)
    register_context_index_tools(tools, context_index_broker)
    search_provider_state = register_web_tools(
        tools, sandbox, default_provider=resolve_startup_search_provider()
    )
    register_image_tools(
        tools,
        workdir=workdir,
        select_tiles=lambda tile_set_id, tile_ids: select_vision_tiles_from_holder(
            agent_holder,
            tile_set_id,
            tile_ids,
        ),
    )
    computer_runtime = register_local_computer_runtime(
        tools,
        model_capabilities=lambda: (
            agent_holder["agent"].llm.config.capabilities
            if "agent" in agent_holder
            else llm_config.capabilities
        ),
    )
    # Browser backend: prefer CDP (headless Chrome), fall back to browser extraction.
    browser_backend = None
    chrome_path = find_chrome()
    if chrome_path:
        browser_backend = CdpBrowserBackend(chrome_path)
        browser_backend = register_browser_tools(tools, backend=browser_backend, enable_extension=True, workdir=workdir).backend
    else:
        browser_backend = register_browser_tools(
            tools,
            extract_fn=create_browser_extract_fn(),
            status_fn=create_browser_status_fn(),
            enable_extension=True,
            workdir=workdir,
        ).backend
    browser_startup = getattr(browser_backend, 'startup', None)
    if callable(browser_startup):
        startup_result = browser_startup()
        if not inspect.isawaitable(startup_result):
            raise TypeError("Browser startup must return an awaitable")
        await startup_result
    mcp_manager = await initialize_mcp_tools(tools, background=True)

    task_store: TaskStore | None = None
    try:
        task_store = TaskStore()
        recovered = task_store.recover_interrupted()
        if recovered:
            print(f"  \033[90m[Marked {recovered} unfinished task(s) as interrupted]\033[0m")
    except Exception as exc:
        print(f"  \033[33m[Task persistence disabled: {exc}]\033[0m")

    llm = LLMClient(llm_config)
    learning_store = LearningStore()
    learning_reviewer = LearningReviewer(llm, learning_store, memory_store, skill_store)
    _, system_prompt, persona_state = startup_persona(os.getenv("AGENT_PERSONA"))
    agent = ReActAgent(
        name="agent",
        llm_client=llm,
        tool_registry=tools,
        system_prompt=system_prompt,
        max_iterations=max(0, _int_env("AGENT_MAX_REACT_ITERATIONS", 50)),
        tool_concurrency=max(1, int(os.getenv("TOOL_CONCURRENCY", "4"))),
        task_store=task_store,
        memory_store=memory_store,
        skill_store=skill_store,
        context_index_broker=context_index_broker,
    )
    agent.set_vision_tiles_enabled(load_vision_tiles_enabled())
    agent.set_persona(persona_state, system_prompt)
    agent_holder["agent"] = agent
    agent._computer_runtime = computer_runtime
    agent._sandbox = sandbox
    agent._mcp_manager = mcp_manager
    agent._learning_store = learning_store
    agent._learning_reviewer = learning_reviewer
    register_conversation_tools(agent, sandbox=sandbox, mcp_manager=mcp_manager)
    register_conclave_tools(tools, llm_getter=lambda: agent.llm)
    setattr(agent, "_search_provider_state", search_provider_state)
    limit = _resolve_context_limit_simple(llm_config.model, llm_config.context_limit)
    agent.context.max_prompt_tokens = prompt_token_budget(
        limit, llm_config.max_tokens, model=llm_config.model, provider=llm_config.provider,
    )
    agent.llm.config.context_limit = limit
    agent.context.set_session(str(startup_session_path()))
    bus.register(agent)

    restored = agent.context.load()
    if restored:
        count = len(agent.context.messages) // 2
        print(f"  \033[90m[Restored session with {count} previous exchanges]\033[0m\n")

    def _save(): agent.context.save()
    signal.signal(signal.SIGINT, lambda s, f: (_save(), sys.exit(0)))

    try:
        await cli_loop(bus, agent)
    finally:
        agent.end_session("shutdown")
        try:
            if computer_runtime is not None:
                await computer_runtime.shutdown()
        except Exception as exc:  # noqa: BLE001 - remaining shutdown cleanup must continue
            print(
                "  \033[33m[Computer Use shutdown failed: "
                f"{type(exc).__name__}]\033[0m"
            )
        finally:
            agent.context.save()
            if browser_backend is not None:
                stop_browser = tools.get("browser_stop")
                if stop_browser is not None:
                    await stop_browser.fn()
            await agent.close_external_memory()
            await mcp_manager.close()
            close = getattr(sandbox, "close", None)
            if close:
                await close()


def _run_tui_mode(config, sandbox_timeout, workdir):
    try:
        from .tui_app import run_tui as _tui_run
    except ImportError as e:
        print(f"\033[31m[Error] TUI 模式不可用: {e}\033[0m")
        print("请安装: pip install textual")
        sys.exit(1)
    _tui_run(config, sandbox_timeout, workdir)





def main():
    setup_logging("cli")
    load_project_env(Path(__file__).resolve().parents[2])

    args = sys.argv[1:]
    if args[:1] == ["activity"]:
        from .activity_commands import execute_activity_command

        raise SystemExit(execute_activity_command(args[1:]))

    os.system("")
    configure_tracing()

    # ── 参数解析 ──
    tui_mode = "--tui" in args
    ink_mode = "--ink" in args

    startup_model, startup_base_url = resolve_startup_model(
        os.getenv("LLM_MODEL", "deepseek-flash"),
        os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    )
    # ASTRA_BACKEND accepts a profile key or the configured provider model ID.
    astra_backend = os.getenv("ASTRA_BACKEND")
    profiles = model_profiles()
    if astra_backend:
        from .models import resolve_profile_key

        astra_backend = resolve_profile_key(astra_backend, profiles)
        selected_key = astra_backend if astra_backend in profiles else next(
            (key for key, profile in profiles.items() if profile.model_id == astra_backend), None,
        )
        if selected_key is not None:
            startup_model = selected_key
            startup_base_url = profiles[selected_key].base_url or startup_base_url

    catalog = configured_model_catalog()
    saved_entry = catalog.resolve_persisted(read_selected_model())
    if saved_entry is not None and not astra_backend:
        startup_model, startup_base_url = saved_entry.key, saved_entry.base_url
        profiles[startup_model] = saved_entry.profile
    startup_profile = profiles.get(startup_model) or next(iter(profiles.values()))
    api_key = startup_profile.api_key()
    if not api_key and is_local_url(startup_base_url):
        api_key = "local"
    if not api_key:
        print("No provider connected. Use /connect to choose a provider and model.")

    config = LLMConfig(
        connection_required=not bool(api_key),
        provider=startup_profile.provider,
        model=(startup_profile.model_id or startup_model) if startup_model in profiles else startup_model,
        api_key=api_key,
        base_url=startup_base_url,
        context_limit=_resolve_context_limit_simple(
            startup_model, startup_profile.context_limit if startup_model in profiles else None,
        ),
        capabilities=startup_profile.capabilities,
        min_request_interval=_float_env("LLM_MIN_REQUEST_INTERVAL", 0.0),
        max_concurrent_requests=max(1, _int_env("LLM_MAX_CONCURRENT_REQUESTS", 1)),
        **startup_profile.generation_settings(),
    )
    config = apply_reasoning_effort(config)
    sandbox_timeout = _int_env("SANDBOX_TIMEOUT", 30)
    workdir = os.getenv("SANDBOX_WORKDIR", os.getenv("HOME", os.getcwd()))

    if tui_mode:
        _run_tui_mode(config, sandbox_timeout, workdir)
    elif ink_mode:
        import subprocess
        # 找 Node.js TUI 脚本
        ui_dir = Path(__file__).parent.parent.parent / "ui-tui"
        entry = ui_dir / "src" / "index.tsx"
        tsx_cli = ui_dir / "node_modules" / "tsx" / "dist" / "cli.mjs"
        if tsx_cli.exists():
            subprocess.run(["node", str(tsx_cli), str(entry)], cwd=str(Path.cwd()))
        else:
            print("\033[31m[Error] tsx not found. Run: cd ui-tui && npm install\033[0m")
            sys.exit(1)
    else:
        asyncio.run(_async_init(config, sandbox_timeout, workdir))


if __name__ == "__main__":
    main()
