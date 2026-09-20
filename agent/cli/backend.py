"""Backend — 被 Ink TUI 子进程调用，stdin/stdout JSON 协议"""

from agent.runtime.paths import sessions_dir
from agent.runtime.appshot_media import AppshotMediaError

import sys
import os
import json
import sqlite3
import asyncio
import contextvars
from dataclasses import replace
import inspect
import logging
import platform
import shlex
import time
import uuid
import urllib.error
import urllib.request
from contextlib import nullcontext, suppress
from pathlib import Path

if __name__ == "__main__":
    from agent.launcher.locking import protect_backend
    protect_backend()

from agent.runtime.process_env import hidden_process_creationflags
from agent.runtime.provider_errors import (
    format_provider_error,
    is_system_message_order_error,
    summarize_provider_error,
)
from agent.logging_config import setup_logging
from agent.core.msg import Msg
from agent.core.bus import MessageBus
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.deepseek import is_deepseek_model
from agent.runtime.react import ReActAgent
from agent.runtime.code_mode import register_run_code_tool
from agent.cli.mode_preferences import (
    MODE_USAGE,
    REASONING_EFFORTS,
    apply_reasoning_effort,
    reasoning_effort_status,
    set_reasoning_effort,
)
from agent.cli.turn_budget import execute_budget_command
from agent.cli.turn_changes_command import execute_changes_command
from agent.cli.stream_events import tool_progress_event, tool_result_event
from agent.runtime.bar_mode import BAR_SCENE_TOOL_NAMES, BarModeController
from agent.runtime import conversation_edits
from agent.runtime.minimal_mode import MINIMAL_STATUS_OPEN_TEMPLATE, MinimalModeController
from agent.runtime.local_mode import load_local_mode
from agent.runtime.harness import RequestLifecycle, build_health_report
from agent.runtime.mcp import initialize_mcp_tools
from agent.runtime.tracing import configure_tracing
from agent.runtime.startup_profiler import StartupProfiler
from agent.runtime.maintenance import RuntimeMaintenance, format_maintenance_report
from agent.runtime.task_store import (
    TaskStore,
    format_task_detail,
    format_task_list,
    format_today,
)
from agent.runtime.approval_inbox import ApprovalInbox
from agent.runtime.context import _valid_message_timestamp
from agent.cli.history import history_tool_results
from agent.runtime.event_stream import RuntimeEventStream
from agent.runtime.event_writer import OrderedEventWriter
from agent.runtime.async_io import durable_io
from agent.runtime.latency import RuntimeProfiler, current_profiler, profile_request
from agent.runtime.user_questions import UserQuestionBroker
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.cli.session_lifecycle import ControlledRestart, RESTART_EXIT_CODE
from agent.runtime.session_wakeup import SessionWakeups, visible_wakeup_history, wakeup_prompt
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.git import register_git_tools
from agent.runtime.tools.time import register_time_tools
from agent.runtime.tools.web import register_web_tools, create_browser_extract_fn, create_browser_status_fn
from agent.runtime.tools.image import register_image_tools, select_vision_tiles_from_holder
from agent.runtime.tools.computer import register_local_computer_runtime
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.cdp_backend import CdpBrowserBackend, find_chrome
from agent.runtime.tools.memory import register_memory_tools
from agent.runtime.tools.bar import register_bar_tools
from agent.runtime.memory import MemoryStore
from agent.runtime.skills import SkillStore
from agent.runtime.tools.skills import register_skill_tools
from agent.runtime.tools.session_recall import register_session_recall_tools
from agent.runtime.tools.activity import register_activity_tools
from agent.runtime.context_index import create_context_index_broker
from agent.runtime.tools.context_index import register_context_index_tools
from agent.runtime.tools.conclave import register_conclave_tools
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.context_index.workspace import resolve_workspace
from agent.runtime.session_recall import SessionRecall as _SessionRecall
from agent.runtime.learning import (
    LearningProviderError, LearningReviewer, LearningReviewResultError, LearningStore,
)
from agent.runtime.goal_verifier import (
    GoalVerifier,
    build_goal_start_message,
    build_goal_resume_message,
    decide_goal_continuation,
    goal_mode_enabled,
    should_verify_goal_turn,
    verification_is_current,
    verdict_is_usable,
)
from agent.runtime.tools.goals import register_goal_tools
from agent.runtime.tools.plans import register_plan_tools
from agent.runtime.tools.user_questions import register_user_question_tools
from agent.runtime.memory_consolidation import MemoryConsolidator
from agent.runtime.session_handoff import generate_handoff, save_handoff
from agent.runtime.session_store import SessionStore
from agent.sandbox.local import LocalSandbox
from agent.sandbox.docker import DockerSandbox
from agent.sandbox.router import SandboxRouter
from agent.cli.images import build_image_message_content, build_user_message_content, message_display_text
from agent.cli.models import ModelProfile, context_limit_for_model, prompt_token_budget
from agent.cli.model_catalog import (
    ModelCatalog,
    CatalogEntry,
    configured_model_catalog,
    discover_model_catalog,
    parse_model_command_argument,
)
from agent.cli.model_preferences import read_selected_model
from agent.cli.bar_preferences import load_bar_output_mode, save_bar_output_mode
from agent.cli.provider_connections import connection_routes_event
from agent.cli.model_catalog import provider_menu_items
from agent.cli.model_preferences import recent_models
from agent.cli.connections import (
    connect_provider,
    create_probe_and_switch,
    is_local_url,
    switch_to_profile,
    sync_context_budget,
)
from agent.cli.diagnostics import build_doctor_report, build_runtime_diagnostics
from agent.cli.environment import load_project_env
from agent.cli.persona_preferences import save_selected_persona, startup_persona
from agent.cli.search_preferences import (
    SEARCH_PROVIDERS,
    resolve_startup_search_provider,
    save_selected_search_provider,
)
from agent.cli.sandbox_preferences import execute_sandbox_command, resolve_startup_sandbox_mode
from agent.cli.vision_tile_preferences import (
    execute_vision_tiles_command,
    load_vision_tiles_enabled,
)
from agent.cli.context_index_commands import execute_context_index_command
from agent.cli.context_index_preferences import load_context_index_preferences
from agent.cli.browser_commands import execute_browser_command
from agent.cli.computer_commands import ComputerStateEmitter, approval_choices, execute_computer_command
from agent.cli.memory_commands import execute_memory_command
from agent.cli.skill_commands import execute_skill_command
from agent.cli.learning_commands import execute_learning_command
from agent.cli.conversation_commands import register_conversation_tools
from agent.runtime.command_workflows import workflow_message, raw_command as normalize_direct_command
from agent.channels import ChannelManager, load_channels_config
from agent.channels.manager import is_address_in_use
from agent.channels.router import AgentChannelRouter, active_channel
from agent.channels.tools import register_channel_tools
from agent.runtime.prompts import get_prompt_profile, prompt_profiles
from agent.cli.sessions import (
    SessionNameError,
    bar_session_exists,
    bar_session_msg_count,
    bar_session_path,
    create_bar_session_name,
    create_minimal_session_name,
    delete_session,
    export_session_markdown,
    list_bar_sessions,
    list_minimal_sessions,
    list_sessions,
    minimal_session_path,
    minimal_session_exists,
    minimal_session_msg_count,
    rename_session,
    startup_session_path,
    session_exists,
    session_msg_count,
    session_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ── Session recall auto-logging ──
_sr_auto: _SessionRecall | None = None
_sr_auto_sessions: dict[str, str] = {}  # context session stem → sr session id
_runtime_event_stream: RuntimeEventStream | None = None
_event_writer: OrderedEventWriter | None = None

def _get_context_limit(model: str) -> int:
    return context_limit_for_model(model)


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _context_limit_from_env(model: str) -> int | None:
    direct = _positive_int(os.getenv("LLM_CONTEXT_LIMIT"))
    if direct:
        return direct

    raw_map = os.getenv("LLM_CONTEXT_LIMITS", "").strip()
    if not raw_map:
        return None
    try:
        mapping = json.loads(raw_map)
    except json.JSONDecodeError:
        return None
    if not isinstance(mapping, dict):
        return None
    return _positive_int(mapping.get(model))


def _extract_context_limit(model_info: dict) -> int | None:
    keys = (
        "context_length",
        "max_context_length",
        "max_model_len",
        "max_sequence_length",
        "max_position_embeddings",
        "n_ctx",
    )
    stack = [model_info]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        for key in keys:
            value = _positive_int(current.get(key))
            if value:
                return value
        for nested_key in ("metadata", "meta", "config", "parameters", "model_config", "details"):
            nested = current.get(nested_key)
            if isinstance(nested, dict):
                stack.append(nested)
    return None


def _models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


async def _context_limit_from_models_endpoint(model: str, base_url: str, api_key: str, timeout: float = 6.0) -> int | None:
    url = _models_url(base_url)

    def fetch() -> int | None:
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None

        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            return None
        fallback = None
        for item in data:
            if not isinstance(item, dict):
                continue
            limit = _extract_context_limit(item)
            if limit and fallback is None:
                fallback = limit
            if item.get("id") == model or item.get("name") == model:
                return limit
        return fallback

    return await asyncio.to_thread(fetch)


async def _live_context_limit(config: LLMConfig) -> int | None:
    """Resolve context metadata from explicit configuration or the live model."""
    return (
        _context_limit_from_env(config.model)
        or await _context_limit_from_models_endpoint(config.model, config.base_url, config.api_key)
    )


async def _resolve_context_limit(config: LLMConfig) -> int:
    return await _live_context_limit(config) or _get_context_limit(config.model)


def _sync_current_context_budget(agent, catalog: ModelCatalog, current_model_key: str) -> bool:
    """Refresh the live prompt budget only from newly discovered metadata."""
    entry = catalog.resolve(current_model_key)
    if entry is None or entry.model_id != agent.llm.config.model:
        return False
    sync_context_budget(agent, entry.profile)
    return True


def _apply_reloaded_model_profile(agent, entry, api_key: str) -> None:
    """Atomically replace every model/profile field used by the live runtime."""
    profile = entry.profile
    agent.llm.switch_model(
        entry.model_id,
        entry.base_url,
        provider_name=profile.provider,
        api_key=api_key,
        capabilities=profile.capabilities,
        **profile.generation_settings(),
    )
    sync_context_budget(agent, profile)
    agent.invalidate_tool_schema_cache()


setup_logging("backend")
logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"


def _stream_error_message(exc: Exception) -> str:
    """Return a bounded provider error without reflecting provider text."""
    from agent.cli.appshots import AppshotValidationError

    if type(exc) is AppshotValidationError and len(exc.args) == 1 and type(exc.args[0]) is str:
        code = exc.args[0]
        reasons = {
            "context_budget_unavailable": "Image data or context-window metadata could not be validated. Check the attachment and model configuration.",
            "context_budget_exceeded": "The request exceeds the model input budget after accounting for history, tools, images and output reserve. Use /compress or select a larger model.",
            "appshot_vision_unavailable": "The selected model is text-only. Select a vision-capable model to use Appshots.",
        }
        if code in reasons:
            return f"Request blocked ({code}): {reasons[code]}"
    if is_system_message_order_error(exc):
        return (
            "Provider request rejected (400): "
            "System message must be at the beginning."
        )
    return format_provider_error(exc, component="backend-stream")


def _learning_review_error_message(exc: Exception, timeout: float) -> str:
    """Return a safe terminal reason for an optional learning review."""
    if isinstance(exc, (LearningReviewResultError, LearningProviderError)):
        return str(exc)
    return format_provider_error(
        exc,
        component="learning-review",
        timeout=timeout,
    )


def _log_stream_provider_error(exc: Exception) -> None:
    """Log a backend provider failure without rendering its traceback value."""
    summary = summarize_provider_error(exc)
    logger.error(
        "backend stream error error_type=%s category=%s status=%s "
        "request_id=%s component=backend-stream",
        summary.error_type,
        summary.category,
        summary.status_code,
        summary.request_id,
    )


def _write_learning_review_error(exc: Exception, timeout: float) -> None:
    """Write the learning-review failure using only safe provider diagnostics."""
    detail = _learning_review_error_message(exc, timeout)
    logger.warning("learning review failed: %s", detail)
    print(f"learning review failed: {detail}", file=sys.stderr, flush=True)


def _finish_pending_tool_calls(
    pending: dict[str, str],
    send,
    message: str,
    code: str,
) -> None:
    """Emit one terminal result for every frontend-visible unfinished call."""
    for call_id, name in list(pending.items()):
        send({
            "type": "tool_result",
            "name": name,
            "call_id": call_id,
            "output": "",
            "error": message,
            "code": code,
            "recoverable": False,
        })
        pending.pop(call_id, None)



def _session_recall_auto_logging_enabled(
    *,
    bar_active: bool,
    minimal_active: bool,
    local_active: bool,
) -> bool:
    """Return True when background recall logging is appropriate.

    Minimal is a zero-injection coding session and Bar/local modes are private
    isolated spaces, so none of them should write long-term recall records
    behind the scenes.
    """
    return not (bar_active or minimal_active or local_active)




def _isolated_mode_blocks_command(
    command: str,
    *,
    bar_active: bool,
    minimal_active: bool,
    local_active: bool,
) -> bool:
    """Return True when an isolated mode must keep a work-context command blocked."""
    if not bar_active and not minimal_active and not local_active:
        return False
    command = command.strip()
    if (
        command in {"/cancel", "/reset", "/think", "/vision-tiles", "/budget"}
        or command.startswith(("/think ", "/vision-tiles ", "/budget "))
    ):
        return False
    if command in {"/undo", "/retry"} or command.startswith("/undo "):
        # Universal conversation edits; the bar defers them for now.
        return bar_active
    # YOLO is only meaningful in Minimal, where host tools can still require
    # approval. Bar and local modes expose no approval prompts of their own, so
    # keep it blocked there.
    if minimal_active and command.split(maxsplit=1)[:1] == ["/yolo"]:
        return False
    return True

def _build_session_list_event(
    current_session_path: str,
    current_message_count: int,
    *,
    include_counts: bool,
) -> dict:
    """Build a cheap startup list or a fully counted session list."""
    current_name = Path(current_session_path).stem if current_session_path else ""
    items = []
    for name in list_sessions():
        items.append({
            "name": name,
            "messages": session_msg_count(name) if include_counts else 0,
            "current": bool(current_name and name == current_name),
        })
    if current_name and not any(item["name"] == current_name for item in items):
        items.insert(0, {
            "name": current_name,
            "messages": current_message_count,
            "current": True,
        })
    return {"type": "session_list", "sessions": items}


async def _handle_conclave(c: str, _send, agent) -> None:
    """Handle /conclave slash command with auto-saved config."""
    from agent.runtime.conclave.config import ConclaveConfig
    from agent.runtime.conclave.core import INTENT_ROUTES, Conclave, unique_source_count
    parts = c.split(maxsplit=2)
    cmd = parts[1] if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""
    config = ConclaveConfig.load()
    if cmd == "config":
        sub = rest.split(maxsplit=1)
        sc = sub[0] if sub else ""
        sa = sub[1] if len(sub) > 1 else ""
        if not sc:
            _send({"type":"tool_result","name":"conclave","output":config.format(),"error":"","code":""})
        elif sc == "chairperson" and sa:
            try:
                from agent.runtime.conclave.llm import resolve_chairperson_llm

                resolve_chairperson_llm(agent.llm, sa, fallback_to_active=False)
                config.update(chairperson_model=sa)
                _send({"type":"tool_result","name":"conclave","output":f"主席模型已设为: {sa}\n{config.format()}","error":"","code":""})
            except ValueError as exc:
                _send({"type":"tool_result","name":"conclave","output":"","error":f"主席模型不可用: {exc}","code":"invalid_arguments"})
        elif sc == "expert_list" and sa:
            cleaned = sa.strip().rstrip(",").strip()
            config.update(experts=cleaned)
            _send({"type":"tool_result","name":"conclave","output":f"✓ 专家列表: {cleaned}","error":"","code":""})
        elif sc == "sources" and sa:
            try:
                n = max(1, min(20, int(sa)))
                config.update(max_sources_per_expert=n)
                _send({"type":"tool_result","name":"conclave","output":f"每条来源数已设为: {n}\n{config.format()}","error":"","code":""})
            except ValueError:
                _send({"type":"tool_result","name":"conclave","output":"","error":"参数必须是数字","code":""})
        elif sc == "max_tokens" and sa:
            try:
                n = max(256, min(8192, int(sa)))
                config.update(max_tokens=n)
                _send({"type":"tool_result","name":"conclave","output":f"最大token已设为: {n}\n{config.format()}","error":"","code":""})
            except ValueError:
                _send({"type":"tool_result","name":"conclave","output":"","error":"参数必须是数字","code":""})
        elif sc == "discussion":
            en = sa.lower() in ("on","true","1","开")
            config.update(cross_discussion=en)
            _send({"type":"tool_result","name":"conclave","output":f"交叉讨论已{'开启' if en else '关闭'}\n{config.format()}","error":"","code":""})
        elif sc == "reset":
            config = ConclaveConfig()
            config.save()
            _send({"type":"tool_result","name":"conclave","output":f"配置已恢复默认\n{config.format()}","error":"","code":""})
        else:
            _send({"type":"tool_result","name":"conclave","output":"","error":f"未知子命令: {c}","code":""})
        return
    if cmd and cmd != "config":
        q = c[len("/conclave "):].strip()
        _send({"type":"tool_result","name":"conclave","output":f"🔍 Conclave 研究中: {q}\n主席: {config.chairperson_model}","error":"","code":""})
        try:
            from agent.runtime.conclave.llm import chairperson_chat

            cl = Conclave()
            r = await cl.run(
                q,
                llm_chat=chairperson_chat(
                    agent.llm,
                    config.chairperson_model,
                    config.max_tokens,
                ),
                expert_llm_chat=chairperson_chat(
                    agent.llm,
                    config.chairperson_model,
                    config.expert_max_tokens,
                    request_role="expert",
                ),
                experts=config.experts,
                max_sources_per_expert=config.max_sources_per_expert,
                cross_discussion=config.cross_discussion,
                max_search_rounds=config.max_search_rounds,
                max_parallel_experts=config.max_parallel_experts,
            )
            total_sources = unique_source_count(r.findings)
            lo = [r.synthesis.rstrip(), "", "---"]
            lo.append(
                f"*{INTENT_ROUTES.get(r.intent,('全面',[]))[0]} · "
                f"{len(r.experts_selected)} 位专家 · {total_sources} 条来源 · "
                f"{r.duration_ms/1000:.1f}s*"
            )
            _send({"type":"tool_result","name":"conclave","output":"\n".join(lo),"error":"","code":""})
        except Exception as e:
            _send({"type":"tool_result","name":"conclave","output":"","error":f"Conclave 失败: {e}","code":""})
        return
    usage = [
        "## Conclave — 多专家研究面板", "",
        "用法:",
        "  /conclave <问题>              快速研究一个问题",
        "  /conclave config              查看当前配置",
        "  /conclave config chairperson <模型>  设置主席模型",
        "  /conclave config expert_list <列表>  自定义专家名单（逗号分隔）",
        "  /conclave config sources <数字>     每条专家最多来源数",
        "  /conclave config max_tokens <数字>  主席最大token",
        "  /conclave config discussion on/off  交叉讨论开关",
        "  /conclave config reset              恢复默认配置", "",
        "模型格式: provider/model，例如 openai-compatible/gemma4",
        "专家格式: 逗号分隔名称，例如 学术专家,GitHub专家,知乎专家", "",
        config.format(),
    ]
    _send({"type":"tool_result","name":"conclave","output":"\n".join(usage),"error":"","code":""})


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


async def _init_sandbox(mode: str, timeout: int, workdir: str):
    # Windows 上除非强制 Docker，否则默认用本地沙箱
    if _IS_WINDOWS and mode != "true":
        return _local_sandbox(timeout, workdir)

    if mode == "false":
        return _local_sandbox(timeout, workdir)

    # 异步检测 Docker
    docker_ok = await _docker_available()
    if not docker_ok:
        if mode == "true":
            print("[backend] Docker requested but not available. Using local sandbox.", file=sys.stderr, flush=True)
        return _local_sandbox(timeout, workdir)

    # Docker 可用
    sb = DockerSandbox(
        timeout=timeout,
        workdir=workdir,
        network=_truthy_env("DOCKER_NETWORK"),
        reuse_container=_truthy_env("DOCKER_REUSE_CONTAINER"),
    )
    if mode == "true":
        return sb

    # auto 模式：测试 Docker 能否跑 Python
    try:
        test = await sb.execute_python("print('ok')")
        if test.get("exit_code") == 0:
            return sb
        else:
            print(f"[backend] Docker Python test failed: {test.get('error', 'unknown')[:60]}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[backend] Docker init error: {e}", file=sys.stderr, flush=True)

    return _local_sandbox(timeout, workdir)


def _run_memory_consolidation(memory_store: MemoryStore) -> dict[str, int]:
    """Run deterministic maintenance without letting bookkeeping break chat."""
    try:
        return MemoryConsolidator(memory_store.record_store).run_all()
    except Exception as exc:
        print(f"[backend] memory consolidation failed: {exc}", file=sys.stderr, flush=True)
        return {"expired": 0, "merged_observations": 0, "extracted_observations": 0}


def _save_handoff(agent: ReActAgent, task_store: TaskStore | None, *, notes: str = "") -> Path | None:
    """Persist a redacted handoff for the active work session."""
    if getattr(agent, "local_mode", False):
        return None
    try:
        session_id = Path(agent.context.session_path).stem if agent.context.session_path else "default"
        text = generate_handoff(
            session_id=session_id,
            task_store=task_store,
            messages=[
                item if isinstance(item, dict) else {"role": item.role, "content": item.get_text()}
                for item in agent.context.messages
            ],
            model=agent.llm.config.model,
            persona_id=agent.context.persona_id or "",
            extra_notes=notes,
        )
        return save_handoff(text, session_id=session_id)
    except Exception as exc:
        print(f"[backend] session handoff failed: {exc}", file=sys.stderr, flush=True)
        return None


async def main():
    global _event_writer
    # Establish IPC before environment/model initialization, including failures.
    _write_event({"type": "backend_hello", "protocol_version": 1})
    startup_started = time.perf_counter()
    load_project_env(PROJECT_ROOT)
    profile = RuntimeProfiler.from_env(PROJECT_ROOT)
    with profile.activate() if profile is not None else nullcontext():
        try:
            return await _main(startup_started)
        finally:
            try:
                if _event_writer is not None:
                    await _event_writer.close()
            finally:
                _event_writer = None
                if profile is not None:
                    await profile.close()


async def _main(startup_started: float):
    global _runtime_event_stream, _event_writer
    restart = ControlledRestart(supported=os.getenv("ASTRA_TUI_RESTART") == "1")
    wakeups = SessionWakeups()
    wakeup_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar("session_wakeup", default=None)
    latest_user_request = ""
    exit_code = 0
    terminal_output_failed = False
    from agent.runtime.native_startup import prepare_native_dependencies

    prepare_native_dependencies()
    startup_profiler = StartupProfiler.from_env(PROJECT_ROOT, started=startup_started)
    startup_profiler.mark("environment")

    configure_tracing()

    try:
        _runtime_event_stream = RuntimeEventStream()
    except Exception as exc:
        _runtime_event_stream = None
        print(f"[backend] event replay persistence disabled: {exc}", file=sys.stderr, flush=True)

    _event_writer = OrderedEventWriter(_runtime_event_stream, _write_event)

    task_store: TaskStore | None = None
    try:
        task_store = TaskStore()
        recovered = await durable_io(task_store.recover_interrupted)
        if recovered:
            logger.warning(
                "[backend] marked %s unfinished task(s) as interrupted",
                recovered,
            )
    except Exception as exc:
        print(f"[backend] task persistence disabled: {exc}", file=sys.stderr, flush=True)

    approval_inbox: ApprovalInbox | None = None
    orphaned_approvals = []
    try:
        approval_inbox = ApprovalInbox()
        orphaned_approvals = approval_inbox.recover_orphaned()
    except Exception as exc:
        print(f"[backend] approval inbox persistence disabled: {exc}", file=sys.stderr, flush=True)
    startup_profiler.mark("durable_state")

    # Startup uses configured metadata and the persisted selection without
    # touching provider /models endpoints. Opening the model menu performs the
    # live discovery, so a sleeping LAN provider cannot hold up the first frame.
    catalog = configured_model_catalog()
    selected = read_selected_model()
    startup_entry = (
        catalog.resolve_persisted(selected)
        or catalog.resolve(os.getenv("LLM_MODEL", ""))
        or (catalog.entries[0] if catalog.entries else None)
    )
    if startup_entry is None:
        startup_entry = CatalogEntry("unconfigured::none", "none", "unconfigured", "No provider connected",
                                     "https://unconfigured.invalid/v1",
                                     ModelProfile("https://unconfigured.invalid/v1", 32768,
                                                  api_key_env="ASTRA_UNCONFIGURED_KEY"))
    if not catalog.resolve(startup_entry.key):
        catalog = ModelCatalog((startup_entry, *catalog.entries), catalog.errors)
    current_model_key = startup_entry.key
    startup_model = startup_entry.model_id
    startup_base_url = startup_entry.base_url
    startup_profile = startup_entry.profile
    api_key = startup_profile.api_key()
    if not api_key and (is_local_url(startup_base_url) or not startup_profile.api_key_env):
        api_key = "local"
    if not api_key:
        _send({"type": "error", "message": "No provider connected. Use /connect to choose a provider and model."})

    llm_config = LLMConfig(
        connection_required=not bool(api_key),
        provider=startup_profile.provider,
        model=startup_model,
        api_key=api_key,
        base_url=startup_base_url,
        capabilities=startup_profile.capabilities,
        min_request_interval=_float_env("LLM_MIN_REQUEST_INTERVAL", 0.0),
        max_concurrent_requests=max(1, _int_env("LLM_MAX_CONCURRENT_REQUESTS", 1)),
        **startup_profile.generation_settings(),
    )
    llm_config = apply_reasoning_effort(llm_config)
    startup_profiler.mark("model_config")

    bus = MessageBus()
    sandbox_timeout = int(os.getenv("SANDBOX_TIMEOUT", "30"))
    sandbox_workdir = os.getenv("SANDBOX_WORKDIR", os.getcwd())
    initial_sandbox = await _init_sandbox(
        resolve_startup_sandbox_mode(os.getenv("SANDBOX_DOCKER", "auto").lower()),
        sandbox_timeout,
        sandbox_workdir,
    )
    sandbox = SandboxRouter(
        initial_sandbox,
        local_factory=lambda: _local_sandbox(sandbox_timeout, sandbox_workdir),
        docker_factory=lambda: DockerSandbox(
            timeout=sandbox_timeout,
            workdir=sandbox_workdir,
            network=_truthy_env("DOCKER_NETWORK"),
            reuse_container=_truthy_env("DOCKER_REUSE_CONTAINER"),
        ),
    )
    startup_profiler.mark("sandbox")
    tools = ToolRegistry()
    question_broker = UserQuestionBroker(
        _send,
        available=lambda: not active_channel.get(),
    )
    register_user_question_tools(tools, question_broker.ask)
    channel_manager_holder: dict[str, ChannelManager] = {}
    register_channel_tools(
        tools,
        lambda: channel_manager_holder.get("manager"),
    )
    memory_store = MemoryStore()
    skill_store = SkillStore()
    startup_profiler.mark("memory_and_skills")
    agent_holder = {}
    process_manager = register_code_tools(
        tools,
        sandbox,
        task_store=task_store,
        on_process_event=lambda event: _send({"type": "process_status", **event}),
    )
    register_file_tools(tools, workdir=os.getcwd(), sandbox=sandbox)
    register_git_tools(tools, workdir=os.getcwd())
    register_time_tools(tools)
    register_memory_tools(
        tools,
        memory_store,
        session_id=lambda: Path(agent_holder["agent"].context.session_path).stem if agent_holder else "default",
    )
    register_goal_tools(
        tools,
        task_store,
        session_id=lambda: Path(agent_holder["agent"].context.session_path).stem if agent_holder else "default",
        on_goal_event=lambda goal: _send({"type": "goal_status", "goal": goal}),
    )
    register_plan_tools(
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
    context_index_broker = create_context_index_broker(
        load_context_index_preferences(), os.getcwd()
    )
    context_index_broker.start_background(memory_store.path)
    register_context_index_tools(tools, context_index_broker)
    register_conclave_tools(
        tools,
        llm_getter=lambda: (
            agent_holder["agent"].llm
            if "agent" in agent_holder
            else None
        ),
    )
    delegate_mailbox = register_delegate_tools(
        tools,
        llm_getter=lambda: (
            agent_holder["agent"].llm
            if "agent" in agent_holder
            else None
        ),
        context_getter=lambda: (
            list(agent_holder["agent"].context.messages)
            if "agent" in agent_holder
            else []
        ),
        session_id_getter=lambda: (
            Path(agent_holder["agent"].context.session_path).stem
            if "agent" in agent_holder
            and agent_holder["agent"].context.session_path
            else "default"
        ),
        on_process_event=lambda event: _send({
            "type": "agent_team" if event.get("kind") == "agent_team" else "process_status",
            **event,
        }),
        on_session_event=lambda session_id, event: SessionStore(
            session_path(session_id)
        ).append_subagent_event(event),
        task_store=task_store,
        sandbox=sandbox,
    )
    search_provider_state = register_web_tools(
        tools, sandbox, default_provider=resolve_startup_search_provider()
    )
    register_image_tools(
        tools,
        workdir=os.getcwd(),
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
    browser_backend = None
    chrome_path = find_chrome()
    if chrome_path:
        browser_backend = CdpBrowserBackend(chrome_path)
        browser_backend = register_browser_tools(tools, backend=browser_backend, enable_extension=True).backend
    else:
        browser_backend = register_browser_tools(
            tools,
            extract_fn=create_browser_extract_fn(),
            status_fn=create_browser_status_fn(),
            enable_extension=True,
        ).backend
    browser_startup = getattr(browser_backend, 'startup', None)
    if callable(browser_startup):
        startup_result = browser_startup()
        if not inspect.isawaitable(startup_result):
            raise TypeError("Browser startup must return an awaitable")
        await startup_result
    startup_profiler.mark("local_tool_registration")
    # Optional MCP services initialize in the background so a slow, offline,
    # or logged-out server never delays the core agent becoming ready.
    mcp_manager = await initialize_mcp_tools(tools, background=True)
    startup_profiler.mark("mcp_background_schedule")

    pending_tool_approvals: dict[str, asyncio.Future[str]] = {}
    pending_yolo_approvals: set[str] = set()

    approval_resolutions: dict[str, asyncio.Task] = {}

    def resolve_tool_approval(request_id: str, decision: str) -> bool:
        future = pending_tool_approvals.get(request_id)
        if future is None or future.done() or request_id in approval_resolutions:
            return False
        normalized = decision if decision in {"once", "session", "deny"} else "deny"

        async def resolve() -> None:
            try:
                if future.done():
                    return
                resolved = None
                if approval_inbox is not None:
                    try:
                        resolved = await durable_io(approval_inbox.resolve, request_id, normalized)
                    except Exception:
                        logger.exception("approval inbox resolve failed request_id=%s", request_id)
                # The waiting turn may have been cancelled while the database
                # settled. A recorded decision alone cannot dispatch a tool.
                if future.done():
                    return
                future.set_result(normalized)
                _send({
                    "type": "approval_resolved", "request_id": request_id,
                    "state": resolved.state if resolved is not None else (
                        "denied" if normalized == "deny" else "approved"
                    ),
                    "decision": normalized,
                })
            finally:
                approval_resolutions.pop(request_id, None)

        def resolution_finished(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                logger.error("approval resolution failed error_type=%s", type(task.exception()).__name__)

        resolution = asyncio.create_task(resolve(), name="approval-resolution")
        resolution.add_done_callback(resolution_finished)
        approval_resolutions[request_id] = resolution
        return True

    async def request_tool_approval(request: dict) -> str:
        channel_name = active_channel.get()
        if channel_name and approval_inbox is None:
            # Without durable state, a channel-originated request could become
            # an invisible suspended coroutine. Preserve the old safe denial.
            logger.warning(
                "channel tool approval denied without inbox channel=%s tool=%s target=%s",
                channel_name,
                request.get("tool_name", ""),
                request.get("target", ""),
            )
            return "deny"
        request_id = str(request.get("request_id") or uuid.uuid4().hex)
        future = asyncio.get_running_loop().create_future()
        surface = "channel" if channel_name else "tui"
        session_id = (
            Path(agent_holder["agent"].context.session_path).stem
            if agent_holder and agent_holder["agent"].context.session_path
            else ""
        )
        try:
            persisted = None
            if approval_inbox is not None:
                try:
                    persisted = await durable_io(approval_inbox.create,
                        request,
                        request_id=request_id,
                        session_id=session_id,
                        task_id="" if channel_name else active_task_id,
                        surface=surface,
                        channel=channel_name,
                    )
                except Exception:
                    logger.exception("approval inbox write failed request_id=%s", request_id)
                    if channel_name:
                        return "deny"
            pending_tool_approvals[request_id] = future
            if request.get("yolo_bypass_allowed") is True:
                pending_yolo_approvals.add(request_id)
            _send({
                "type": "tool_approval_request",
                **request,
                "request_id": request_id,
                "surface": surface,
                "channel": channel_name,
                "state": persisted.state if persisted is not None else "pending",
                "choices": approval_choices(request),
            })
            if tools.yolo and request.get("yolo_bypass_allowed") is True:
                # YOLO may have been enabled while the inbox write was waiting.
                resolve_tool_approval(request_id, "once")
            return await future
        except asyncio.CancelledError:
            if approval_inbox is not None:
                try:
                    await durable_io(approval_inbox.cancel, request_id)
                except Exception:
                    logger.exception("approval inbox cancel failed request_id=%s", request_id)
            raise
        finally:
            pending_tool_approvals.pop(request_id, None)
            pending_yolo_approvals.discard(request_id)

    tools.set_approval_handler(request_tool_approval)
    if orphaned_approvals:
        _send({
            "type": "approval_inbox_snapshot",
            "orphaned": [record.public() for record in orphaned_approvals],
        })
        for record in orphaned_approvals:
            _send({
                "type": "approval_resolved",
                "request_id": record.request_id,
                "state": "orphaned",
                "decision": "",
            })

    llm = LLMClient(llm_config)
    learning_store = LearningStore()
    learning_reviewer = LearningReviewer(llm, learning_store, memory_store, skill_store)
    goal_verifier = GoalVerifier(llm)
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
    agent.context.compaction_observer = _send
    agent.set_persona(persona_state, system_prompt)
    agent.delegate_mailbox = delegate_mailbox
    agent_holder["agent"] = agent
    agent._computer_runtime = computer_runtime
    agent._sandbox = sandbox
    agent._mcp_manager = mcp_manager
    agent._learning_store = learning_store
    agent._learning_reviewer = learning_reviewer
    register_conversation_tools(agent, sandbox=sandbox, mcp_manager=mcp_manager,
                                startup_profile=lambda: startup_profile_report, process_manager=process_manager)
    computer_state_emitter = ComputerStateEmitter(computer_runtime, _send)
    tools.hooks.on_tool_result(computer_state_emitter.observe_tool_result)
    tools.hooks.on_tool_error(computer_state_emitter.observe_tool_error)
    tools.hooks.on_session_end(computer_state_emitter.observe_session_end)
    tools.hooks.on_runtime_event(computer_state_emitter.observe_runtime_event)
    register_run_code_tool(tools, agent_getter=lambda: agent_holder["agent"])
    # The configured/discovered profile already carries a safe context limit.
    # Avoid a second synchronous /models request during startup; an explicit
    # model-menu refresh will replace it with current live metadata.
    limit = startup_profile.context_limit or _get_context_limit(startup_model)
    agent.context.max_prompt_tokens = prompt_token_budget(
        limit, llm_config.max_tokens, model=llm_config.model, provider=llm_config.provider,
    )
    agent.llm.config.context_limit = limit
    agent.context.set_session(str(startup_session_path()))
    bus.register(agent)
    bar_mode = BarModeController(agent, load_bar_output_mode())
    register_bar_tools(tools, bar_mode)
    minimal_mode = MinimalModeController(agent)
    local_mode = load_local_mode(agent)

    def _task_tracking_enabled() -> bool:
        # Runtime turns still stream and cancel in a local mode, but their
        # text/checkpoints must never enter the shared work task database.
        return task_store is not None and not bar_mode.active and not local_mode.active

    startup_profiler.mark("agent_and_session_setup")

    def _current_memory_session() -> str:
        return Path(agent.context.session_path).stem if agent.context.session_path else "default"

    def _send_working_memory() -> None:
        if bar_mode.active or minimal_mode.active or local_mode.active:
            _send({"type": "working_memory", "session_id": "", "memory": {}})
            return
        _send({
            "type": "working_memory",
            "session_id": _current_memory_session(),
            "memory": memory_store.get_working(_current_memory_session()),
        })

    def _require_local_work_session() -> None:
        if active_channel.get() or bar_mode.active or minimal_mode.active or local_mode.active:
            raise ValueError("Session lifecycle controls are available in the local Work TUI only.")

    async def _request_restart() -> str:
        _require_local_work_session()
        event = restart.request()
        _send(event)
        return event["message"]

    async def _restart_permission(_args: dict) -> dict | None:
        _require_local_work_session()
        if not restart.supported:
            raise ValueError("Controlled restart requires the current Astra TUI.")
        if restart.draining:
            return None
        return {"reason": "Restart the current backend after its current work is delivered.",
                "approval_title": "Restart Astra backend",
                "approval_question": "Restart this backend after the current reply finishes?",
                "approval_effect": "The TUI stays open; the same session is restored. Other services are not restarted."}

    tools.register(ToolDef(
        name="request_restart",
        description="Request a controlled restart of this TUI's backend. Waits for current work and delivery; requires user confirmation. Does not update dependencies or restart other services. Return a final reply after requesting it; do not wait for the restart inside this turn.",
        parameters={"type": "object", "properties": {}},
        fn=_request_restart, risk="write", cache_results=False,
        permission_check=_restart_permission, permission_authoritative=True,
        permission_grant=lambda _args, _request, _decision: None,
    ))

    def _wakeup_event(status: dict, message: str = "") -> None:
        _send({"type": "wakeup_status", "plan": status, "message": message})

    async def _schedule_wakeup(prompt: str, delay_seconds: float = 300,
                               interval_seconds: float = 0, lifetime_seconds: float = 3600) -> str:
        _require_local_work_session()
        if wakeup_context.get() is not None or restart.draining:
            raise ValueError("A wakeup cannot create further schedules or run while restart is pending.")
        status = wakeups.schedule(session=Path(agent.context.session_path).stem, prompt=prompt,
                                  original_request=latest_user_request,
                                  delay_seconds=delay_seconds, interval_seconds=interval_seconds,
                                  lifetime_seconds=lifetime_seconds)
        await _stop_wakeup_turn()
        _wakeup_event(status, f"Wakeup scheduled for {status['next_at']}; expires {status['expires_at']}. "
                       + ("The previous plan was replaced. " if status.get("replaced_id") else "")
                       + "Use /wakeup to inspect or /wakeup cancel to stop. Closing Astra stops the plan.")
        return json.dumps(status, ensure_ascii=False)

    async def _wakeup_status() -> str:
        _require_local_work_session()
        return json.dumps(wakeups.status(), ensure_ascii=False)

    async def _cancel_wakeup() -> str:
        _require_local_work_session()
        status = wakeups.cancel()
        await _stop_wakeup_turn()
        _wakeup_event(status, "Session wakeup stopped.")
        return json.dumps(status, ensure_ascii=False)

    async def _report_wakeup(outcome: str, summary: str = "") -> str:
        tick = wakeup_context.get()
        if tick is None:
            raise ValueError("report_wakeup is only available during a scheduled wakeup turn.")
        if outcome not in {"unchanged", "changed", "completed", "failed"}:
            raise ValueError("Invalid wakeup outcome.")
        if tick.get("outcome"):
            raise ValueError("This wakeup already has an outcome. Finish the turn.")
        if outcome != "unchanged" and not summary.strip():
            raise ValueError("A visible wakeup outcome needs a summary.")
        tick.update(outcome=outcome, summary=summary[:4000])
        return "Wakeup outcome recorded. Finish this turn; do not wait or schedule another check."

    tools.register(ToolDef(
        name="schedule_wakeup",
        description="Only when the user explicitly asks to check back later or monitor something, schedule one bounded check in THIS session. Zero interval is one-shot; positive interval repeats until completion, failure, cancellation or expiry. Replaces the current plan. Closing/switching/restarting Astra stops it. Include what to inspect and the stop condition in prompt. Does not grant permission for new actions.",
        parameters={"type": "object", "properties": {
            "prompt": {"type": "string", "maxLength": 4000},
            "delay_seconds": {"type": "number", "minimum": 60, "default": 300},
            "interval_seconds": {"type": "number", "minimum": 0, "default": 0},
            "lifetime_seconds": {"type": "number", "minimum": 60, "maximum": 43200, "default": 3600},
        }, "required": ["prompt"]}, fn=_schedule_wakeup, risk="write", cache_results=False,
    ))
    for name, description, fn in (
        ("wakeup_status", "Inspect the current session's wakeup plan and latest outcome.", _wakeup_status),
        ("cancel_wakeup", "Stop the current session wakeup plan.", _cancel_wakeup),
    ):
        tools.register(ToolDef(name=name, description=description,
                               parameters={"type": "object", "properties": {}}, fn=fn, cache_results=False))
    tools.register(ToolDef(
        name="report_wakeup", description="During a scheduled wakeup only, record one result. unchanged stays quiet; changed notifies; completed/failed notifies and ends repetition. Include the useful result or blocker and relevant links in summary.",
        parameters={"type": "object", "properties": {
            "outcome": {"type": "string", "enum": ["unchanged", "changed", "completed", "failed"]},
            "summary": {"type": "string", "maxLength": 4000},
        }, "required": ["outcome"]}, fn=_report_wakeup, cache_results=False,
    ))

    manual_reviews: set[asyncio.Task] = set()
    startup_tasks: set[asyncio.Task] = set()
    agent_turn_lock = asyncio.Lock()
    goal_tasks: set[asyncio.Task] = set()

    async def _run_skill_command(parts: list[str], session: str, messages: list[dict]) -> None:
        _send({"type": "learning_review_status", "status": "running"})
        try:
            output, error = await execute_learning_command(
                learning_store, learning_reviewer, memory_store, skill_store, parts,
                session_id=session, messages=messages,
                on_progress=lambda message: _send({"type": "learning_review", "message": message, "proposal_ids": []}),
            )
            agent._refresh_skill_catalog(force=True)
            _send_startup_status()
            _send({"type": "tool_result", "name": "learn", "output": output, "error": error, "code": ""})
        except asyncio.CancelledError:
            _send({"type": "learning_review_status", "status": "cancelled"})
            _send({"type": "tool_result", "name": "learn", "output": "Skill review cancelled. Completed changes remain in /learn history; it will not restart automatically.", "error": "", "code": ""})
            raise
        finally:
            _send({"type": "learning_review_status", "status": "idle"})
            _send({"type": "done"})

    async def _cancel_manual_reviews() -> None:
        pending = [task for task in manual_reviews if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _cancel_goal_verifications(*, exclude: asyncio.Task | None = None) -> None:
        pending = [
            task for task in goal_tasks
            if task is not exclude and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_goal_verification(
        session_id: str,
        messages: list[dict],
        turn_generation: int,
    ) -> None:
        """Verify the live session goal after a completed turn.

        The verifier only accepts concrete evidence. When the goal is not met
        and no user turn started meanwhile, a synthetic continuation round is
        launched automatically (ZCode-style Goal Mode).
        """
        try:
            if task_store is None or not goal_mode_enabled() or bar_mode.active or minimal_mode.active or local_mode.active:
                return
            try:
                goal = await durable_io(task_store.active_goal_for_session, session_id)
            except Exception:
                return
            if goal is None or goal.get("status") != "active":
                return
            _send({"type": "goal_status", "goal": goal, "stage": "verifying"})
            verdict = await goal_verifier.verify(goal, messages)
            async with agent_turn_lock:
                # Re-check after the LLM call: the user may have paused or
                # cleared the goal, or started a new turn that outranks the
                # continuation. That turn's own hook will re-verify later.
                if not goal_mode_enabled() or bar_mode.active or minimal_mode.active or local_mode.active:
                    return
                if not verification_is_current(
                    captured_session=session_id,
                    captured_generation=turn_generation,
                    current_session=_current_memory_session(),
                    current_generation=goal_turn_generation,
                ):
                    return
                try:
                    current = await durable_io(task_store.active_goal_for_session, session_id)
                except Exception:
                    return
                if current is None or current["id"] != goal["id"] or current.get("status") != "active":
                    return
                if active_task is not None and not active_task.done():
                    return
                if not verdict_is_usable(verdict):
                    _send({
                        "type": "goal_status",
                        "goal": current,
                        "verdict": verdict,
                        "stage": "verification_failed",
                    })
                    print(
                        "[backend] goal verifier returned no usable verdict; "
                        "goal remains active without spending a round",
                        file=sys.stderr,
                        flush=True,
                    )
                    return
                try:
                    updated = await durable_io(task_store.record_goal_round, goal["id"], verdict)
                except (KeyError, ValueError):
                    return
                _send({"type": "goal_status", "goal": updated, "verdict": verdict})
                continuation = decide_goal_continuation(updated, verdict)
                if continuation is None:
                    if updated.get("status") == "completed":
                        print(
                            f"[backend] goal met after {updated.get('round')} round(s): {updated.get('objective', '')}",
                            flush=True,
                        )
                    elif updated.get("status") == "exhausted":
                        print(
                            f"[backend] goal exhausted its round budget: {updated.get('objective', '')}",
                            file=sys.stderr, flush=True,
                        )
                    return
                msg = Msg(sender="user", role="user", content=build_user_message_content(continuation))
                msg.metadata["goal_round"] = True
                await _launch_message(msg, continuation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[backend] goal verification failed: {exc}", file=sys.stderr, flush=True)

    async def _maybe_start_goal_verification(turn_generation: int) -> None:
        if restart.draining:
            return
        if task_store is None or not goal_mode_enabled() or bar_mode.active or minimal_mode.active or local_mode.active:
            return
        session_id = _current_memory_session()
        try:
            goal = await durable_io(task_store.active_goal_for_session, session_id)
        except Exception:
            return
        if goal is None or goal.get("status") != "active":
            return
        verification = asyncio.create_task(
            _run_goal_verification(
                session_id,
                list(agent.context.messages),
                turn_generation,
            ),
            name="goal-verification",
        )
        goal_tasks.add(verification)
        verification.add_done_callback(goal_tasks.discard)

    async def _stream_reply_inner(msg: Msg):
        nonlocal _reply_done
        tick = wakeup_context.get()
        lifecycle = RequestLifecycle(msg.metadata.get("request_id") or msg.id)
        task_id = str(msg.metadata.get("task_id") or "")
        runtime_task_id = str(msg.metadata.get("runtime_task_id") or "")
        failed_message = ""
        time_budget_exhausted = False
        was_cancelled = False
        turn_completed = False
        pending_tool_calls: dict[str, str] = {}

        _sr_chunks: list[str] = []
        _sr_sid = ""
        lifecycle.start()
        try:
            # ── Session recall auto-logging ──
            if tick is None and _session_recall_auto_logging_enabled(
                bar_active=bar_mode.active,
                minimal_active=minimal_mode.active,
                local_active=local_mode.active,
            ):
                try:
                    ctx = agent.context
                    stem = Path(ctx.session_path).stem if ctx and ctx.session_path else ""
                    if stem:
                        global _sr_auto, _sr_auto_sessions
                        if _sr_auto is None:
                            _sr_auto = _SessionRecall()
                            await durable_io(_sr_auto.init_db)
                        persona = ctx.persona_id if ctx else ""
                        workspace = resolve_workspace(Path(os.getcwd()))
                        sr_sid = await durable_io(
                            _sr_auto.get_or_create_session,
                            stem,
                            title=stem,
                            personality=persona,
                            workspace_key=workspace.key,
                            workspace_root=workspace.root,
                        )
                        _sr_auto_sessions[stem] = sr_sid
                        prev = getattr(_stream_reply_inner, "_sr_prev_sid", None)
                        if prev and prev != sr_sid:
                            try:
                                await durable_io(_sr_auto.close_session, prev)
                            except Exception:
                                logger.exception("session recall: close previous session failed")
                        _stream_reply_inner._sr_prev_sid = sr_sid  # type: ignore[attr-defined]
                        text = msg.get_text() or "(empty message)"
                        await durable_io(_sr_auto.log_message, sr_sid, "user", text)
                        _sr_sid = sr_sid
                except sqlite3.Error as exc:
                    logger.warning("session recall unavailable; continuing without recall: %s", exc)
                except Exception:
                    logger.exception("session recall: log user failed")

            async for event in agent.reply_stream(msg):
                if _event_writer is not None:
                    await _event_writer.wait_for_capacity()
                if event["type"] == "chunk":
                    _sr_chunks.append(event["content"])
                    if tick is None:
                        _send({"type": "chunk", "content": event["content"]})
                elif event["type"] == "reasoning":
                    if tick is None:
                        _send({"type": "reasoning", "content": event["content"]})
                elif event["type"] == "generation_progress":
                    _send({
                        "type": "generation_progress",
                        "phase": event["phase"],
                        "elapsed_seconds": event["elapsed_seconds"],
                        "idle_seconds": event["idle_seconds"],
                    })
                elif event["type"] == "generation_stats":
                    _send({
                        "type": "generation_stats",
                        "completion_tokens": event["completion_tokens"],
                        "elapsed_seconds": event["elapsed_seconds"],
                        "tokens_per_second": event["tokens_per_second"],
                    })
                elif event["type"] == "tool_result":
                    pending_tool_calls.pop(str(event.get("id") or ""), None)
                    if (
                        bar_mode.active
                        and event["name"] in BAR_SCENE_TOOL_NAMES
                        and not event.get("error")
                    ):
                        notices = {
                            "bar_turn": "BAR TURN // SYNCED",
                            "serve_drink": f"LYRA SERVED // {bar_mode.drink.name}",
                            "refill_drink": f"LYRA REFILLED // {bar_mode.drink.name}",
                            "rename_drink": f"LYRA RENAMED // {bar_mode.drink.name}",
                            "set_ambiance": (
                                f"AMBIENCE // {bar_mode.ambiance.weather} · {bar_mode.ambiance.power}"
                            ),
                            "pour_lyra_drink": f"LYRA CUP // {bar_mode.lyra_glass.name}",
                            "sip_lyra_drink": f"LYRA SIPS // {bar_mode.lyra_glass.name}",
                        }
                        _send_bar_state(notices[event["name"]])
                        continue
                    _send(tool_result_event(event))
                    if event["name"] in {"memory", "plan_update"} and not event.get("error"):
                        _send_working_memory()
                elif event["type"] == "error":
                    if not event.get("recoverable"):
                        failed_message = event.get("message", "Request failed")
                        time_budget_exhausted = event.get("code") == "turn_budget_exhausted"
                    was_cancelled = bool(event.get("cancelled"))
                    error_event = {
                        "type": "error",
                        "message": event.get("message", "Request failed"),
                        "recoverable": bool(event.get("recoverable")),
                    }
                    for key in (
                        "code",
                        "retryable",
                        "recovery_hint",
                        "tool_name",
                        "call_id",
                        "partial",
                        "duration_ms",
                        "details",
                        "artifact_ref",
                    ):
                        if key in event:
                            error_event[key] = event[key]
                    if tick is None:
                        _send(error_event)
                elif event["type"] == "tool_calls":
                    visible_calls = [
                        call for call in event.get("calls", [])
                        if isinstance(call, dict) and not (
                            bar_mode.active
                            and call.get("name") in BAR_SCENE_TOOL_NAMES
                        )
                    ]
                    if visible_calls:
                        for call in visible_calls:
                            call_id = str(call.get("id") or "")
                            if call_id:
                                pending_tool_calls[call_id] = str(call.get("name") or "tool")
                        _send({"type": "tool_calls", "calls": visible_calls})
                elif event["type"] == "tool_progress":
                    if bar_mode.active and event.get("name") in BAR_SCENE_TOOL_NAMES:
                        continue
                    _send(tool_progress_event(event))
                elif event["type"] == "memory_retention":
                    _send({
                        "type": "memory_retention",
                        "decision": event.get("decision", "retained"),
                        "mode": event.get("mode", "conservative"),
                        "retained_ids": event.get("retained_ids", []),
                        "confirmed_ids": event.get("confirmed_ids", []),
                        "superseded_ids": event.get("superseded_ids", []),
                    })
                elif event["type"] == "vision_preprocess":
                    vision_event = {
                        "type": "vision_preprocess",
                        "message": event.get("message", ""),
                        "protected": bool(event.get("protected")),
                        "protected_local_images": int(
                            event.get("protected_local_images") or 0
                        ),
                        "unprotected_external_images": int(
                            event.get("unprotected_external_images") or 0
                        ),
                    }
                    _send(vision_event)
                elif event["type"] == "turn_changes":
                    totals = event.get("totals")
                    _send({
                        "type": "turn_changes",
                        "session_id": event.get("session_id", ""),
                        "request_id": event.get("request_id", ""),
                        "turn_seq": event.get("turn_seq", 0),
                        "files": list(event.get("files") or []),
                        "unknown_count": int(event.get("unknown_count") or 0),
                        "totals": dict(totals) if isinstance(totals, dict) else {},
                    })
                elif event["type"] == "done":
                    turn_completed = True
                    _reply_done = True
                    _finish_pending_tool_calls(
                        pending_tool_calls,
                        _send,
                        "[ToolInterrupted] Tool ended without a result.",
                        "tool_result_missing",
                    )
                    ctx = agent.context
                    cache_hit = int(ctx.total_cache_hit_tokens or 0)
                    cache_miss = int(ctx.total_cache_miss_tokens or 0)
                    _send({
                        "type": "cache_status",
                        "cache_hit_tokens": cache_hit,
                        "cache_miss_tokens": cache_miss,
                    })
                    if tick is None:
                        _send({"type": "done"})
                    # ── Log assistant response ──
                    try:
                        if _sr_auto is not None and _sr_sid and _sr_chunks:
                            await durable_io(
                                _sr_auto.log_message,
                                _sr_sid, "assistant",
                                "".join(_sr_chunks),
                            )
                    except Exception:
                        logger.exception("session recall: log assistant failed")
        except asyncio.CancelledError:
            lifecycle.cancel()
            was_cancelled = True
            if task_id:
                await delegate_mailbox.cancel_task(task_id)
            logger.info("stream cancelled; saving context and ending turn")
            _finish_pending_tool_calls(
                pending_tool_calls,
                _send,
                "[Cancelled] Tool call was cancelled before completion.",
                "cancelled",
            )
            if tick is None:
                _send({"type": "error", "message": "Request cancelled before completion."})
            _reply_done = True
            if tick is None:
                _send({"type": "done"})
        except Exception as e:
            failed_message = _stream_error_message(e)
            lifecycle.fail(failed_message)
            _log_stream_provider_error(e)
            _finish_pending_tool_calls(
                pending_tool_calls,
                _send,
                f"[ToolInterrupted] Backend stream failed: {failed_message}",
                "backend_stream_failed",
            )
            if tick is None:
                _send({
                    "type": "error",
                    "code": "provider_request_failed",
                    "message": failed_message,
                })
            _reply_done = True
            if tick is None:
                _send({"type": "done"})
        finally:
            _finish_pending_tool_calls(
                pending_tool_calls,
                _send,
                "[ToolInterrupted] Backend stream closed before the tool returned.",
                "backend_stream_closed",
            )
            lifecycle.finish()
            if tick is not None:
                outcome = tick.get("outcome") or "failed"
                summary = tick.get("summary") or "Wakeup stopped: the check ended without a reported result."
                if was_cancelled or failed_message:
                    outcome, summary = "failed", failed_message or "Wakeup was interrupted."
                status = wakeups.finish(tick["plan"]["id"], outcome=outcome, summary=summary)
                if status is not None:
                    _wakeup_event(status)
                    if outcome != "unchanged":
                        agent.context.add_assistant_raw({"role": "assistant", "content": summary,
                                                         "provenance": "wakeup_notification"})
                        _send({"type": "chunk", "content": summary})
                _send({"type": "done"})
            await agent.context.save_async()
            if bar_mode.active:
                if turn_completed and not was_cancelled and not failed_message:
                    bar_mode.complete_turn()
                _send_bar_session_list()
                _send_bar_state()
            if task_id and task_store is not None:
                if was_cancelled:
                    await durable_io(task_store.request_cancel, task_id)
                    await durable_io(task_store.finish_run, task_id,
                                     "interrupted" if terminal_output_failed else "cancelled",
                                     "Terminal output unavailable; session saved without replaying tools"
                                     if terminal_output_failed else failed_message or "Cancelled by user")
                elif time_budget_exhausted:
                    await durable_io(task_store.finish_run, task_id, "interrupted", failed_message or "Turn budget exhausted; continue or /resume to resume")
                elif failed_message:
                    await durable_io(task_store.finish_run, task_id, "failed", failed_message)
                elif ((await durable_io(task_store.get_task, task_id)) or {}).get("checkpoint", {}).get("resume_kind") == "iteration_budget":
                    await durable_io(task_store.finish_run, task_id, "interrupted", "Iteration budget exhausted; continue or /resume to resume")
                else:
                    await durable_io(task_store.finish_run, task_id, "completed")
                task = await durable_io(task_store.get_task, task_id)
                _send({"type": "task_status", "task": task})
            elif runtime_task_id:
                status = (
                    "cancelled" if was_cancelled else
                    "interrupted" if time_budget_exhausted else
                    "failed" if failed_message else "completed"
                )
                _send({"type": "task_status", "task": {"id": runtime_task_id, "status": status}})
            await _send_model_info()
            _send_startup_status()
            if tick is None and not msg.metadata.get("command_workflow") and should_verify_goal_turn(
                turn_completed=turn_completed,
                was_cancelled=was_cancelled,
                failed_message=failed_message,
            ):
                await _maybe_start_goal_verification(
                    int(msg.metadata.get("goal_turn_generation") or 0)
                )

    async def _stream_reply(msg: Msg, *, appshot_turn_owned: bool = False, on_enter=None):
        if appshot_turn_owned:
            # The scheduling done callback owns release, including cancellation
            # before this coroutine is first resumed.
            if on_enter is not None:
                on_enter()
            await _stream_reply_inner(msg)
        else:
            async with agent_turn_lock:
                plan = msg.metadata.get("wakeup_plan")
                remaining = wakeups.remaining(plan["id"], Path(agent.context.session_path).stem) if plan else 0
                if plan is not None and remaining <= 0:
                    raise asyncio.CancelledError("Scheduled check no longer belongs to this session")
                if on_enter is not None:
                    on_enter()
                if plan is None:
                    await _stream_reply_inner(msg)
                else:
                    token = wakeup_context.set({"plan": plan})
                    previous_timeout, previous_iterations = agent.turn_timeout_seconds, agent.max_iterations
                    agent.turn_timeout_seconds = min(previous_timeout or 300, 300, remaining)
                    agent.max_iterations = min(previous_iterations or 10, 10)
                    try:
                        await _stream_reply_inner(msg)
                    finally:
                        agent.turn_timeout_seconds, agent.max_iterations = previous_timeout, previous_iterations
                        wakeup_context.reset(token)

    catalog_tasks: dict[str, asyncio.Task] = {}

    async def _refresh_model_catalog(provider_id: str | None = None, *, force: bool = False) -> ModelCatalog:
        nonlocal catalog
        refreshed = await discover_model_catalog(provider_id=provider_id, force=force)
        if provider_id:
            entries = tuple(e for e in catalog.entries if e.provider_id != provider_id) + refreshed.entries
            errors = {k: v for k, v in catalog.errors.items() if k != provider_id}
            errors.update(refreshed.errors)
            refreshed = ModelCatalog(entries, errors, {**catalog.statuses, **refreshed.statuses})
        current_entry = catalog.resolve(current_model_key) or refreshed.resolve_persisted(current_model_key)
        if current_entry is not None and not refreshed.resolve(current_entry.key):
            refreshed = ModelCatalog((*refreshed.entries, replace(current_entry, source="selected")), refreshed.errors, refreshed.statuses)
        catalog = refreshed
        return catalog

    async def _refresh_provider(provider_id: str, force: bool = False) -> None:
        try:
            await _refresh_model_catalog(provider_id, force=force)
            await _send_model_info()
        except (ValueError, OSError):
            _send({"type": "error", "message": "Could not read provider configuration. Check local connection files."})

    async def _connect_provider(request: dict) -> None:
        nonlocal catalog
        request_id = str(request.get("request_id", ""))
        try:
            async def auth_progress(challenge):
                _send({"type": "connection_auth", "request_id": request_id, **challenge})
            provider_id, discovered = await connect_provider(
                str(request.get("route_id", "")), base_url=str(request.get("base_url", "")),
                api_key=str(request.pop("api_key", "")), api_key_env=str(request.get("api_key_env", "")),
                on_progress=auth_progress)
            error = discovered.errors.get(provider_id, "")
            catalog = ModelCatalog(tuple(e for e in catalog.entries if e.provider_id != provider_id) + discovered.entries,
                                   {**{k: v for k, v in catalog.errors.items() if k != provider_id}, **discovered.errors}, {**catalog.statuses, **discovered.statuses})
            await _send_model_info()
            _send({"type": "connection_result", "request_id": request_id, "provider_id": provider_id,
                   "error": "", "notice": error or "Connection saved. Select a model; chat access is checked on use."})
        except asyncio.CancelledError:
            _send({"type": "connection_result", "request_id": request_id, "error": "Connection cancelled."})
            raise
        except Exception as exc:
            # Only validation errors are suitable for display; never include filesystem/HTTP payloads.
            message = (str(exc) if isinstance(exc, ValueError) else "Device login timed out; try again."
                       if isinstance(exc, TimeoutError) else "Could not connect or save connection; current model unchanged.")
            _send({"type": "connection_result", "request_id": request_id, "error": message})
        finally:
            request.pop("api_key", None)

    async def _send_model_info(*, refresh: bool = False):
        if refresh:
            await _refresh_model_catalog()
        ctx = agent.context
        used = ctx.estimate_prompt_tokens()
        limit = ctx.max_prompt_tokens
        pct = (used / limit * 100) if limit > 0 else 0.0
        personas = [
            {"name": profile.name, "description": profile.description}
            for profile in prompt_profiles().values()
        ]
        _send({"type": "model_info", "model": agent.llm.config.model,
               "model_key": current_model_key,
               "reasoning_effort": (agent.llm.config.reasoning_effort
                                    if is_deepseek_model(agent.llm.config.model) or agent.llm.config.provider == "openai-codex" else None),
               "code_mode": agent.code_mode,
               "personas": personas,
               "models": [entry.to_event(current=entry.key == current_model_key)
                          for entry in catalog.entries],
               "provider_errors": catalog.errors,
               "providers": provider_menu_items(catalog),
               "connection_routes": connection_routes_event(),
               "recent_models": recent_models(),
               "total_tokens": ctx.total_tokens,
               "prompt_tokens": ctx.total_prompt_tokens,
               "completion_tokens": ctx.total_completion_tokens,
               "cache_hit_tokens": ctx.total_cache_hit_tokens,
               "cache_miss_tokens": ctx.total_cache_miss_tokens,
               "context_used": used,
               "context_pct": round(pct, 1),
               "context_limit": limit,
               "show_reasoning": ctx.show_reasoning})

    async def _send_history():
        hist = []
        for message in visible_wakeup_history(agent.context.messages):
            if message.get("role") not in ("user", "assistant"):
                continue
            if message.get("_meta", {}).get("type") == "reasoning_context":
                continue
            item = {
                "role": message["role"],
                "content": message_display_text(message.get("display_command", message.get("content", ""))),
            }
            timestamp = _valid_message_timestamp(message.get("timestamp"))
            if timestamp is not None:
                item["timestamp"] = timestamp
            hist.append(item)
        saved_results = []
        if _task_tracking_enabled() and task_store is not None and agent.context.session_path:
            try:
                saved_results = await durable_io(task_store.recent_tool_results, Path(agent.context.session_path).stem)
            except Exception:
                logger.exception("could not restore saved tool details")
        results = history_tool_results(agent.context.messages, saved_results)
        if bar_mode.active:
            results = [result for result in results if result["name"] not in BAR_SCENE_TOOL_NAMES or result["error"]]
        _send({"type": "history", "session_id": Path(agent.context.session_path).stem if agent.context.session_path else "", "messages": hist, "tool_results": results})

    def _send_session_info():
        if bar_mode.active or minimal_mode.active or local_mode.active:
            return
        current = Path(agent.context.session_path).stem if agent.context.session_path else ""
        _send({"type": "session_info", "name": current, "messages": len(agent.context.messages)})

    def _send_mode_info() -> None:
        _send({
            "type": "mode_info",
            "mode": "bar" if bar_mode.active else ("minimal" if minimal_mode.active else ("local" if local_mode.active else "work")),
            "ephemeral": False,
            "bar_session": bar_mode.session_name if bar_mode.active else "",
            "minimal_session": minimal_mode.session_name if minimal_mode.active else "",
            "local_session": local_mode.session_name if local_mode.active else "",
            "memory_enabled": not bar_mode.active and not minimal_mode.active and not local_mode.active,
            "tools_enabled": not bar_mode.active and not minimal_mode.active and not local_mode.active,
            "learning_enabled": not bar_mode.active and not minimal_mode.active and not local_mode.active,
        })

    def _send_bar_session_list() -> None:
        current = bar_mode.session_name
        _send({
            "type": "bar_session_list",
            "sessions": [
                {
                    "name": name,
                    "messages": bar_session_msg_count(name),
                    "current": bool(current and name == current),
                }
                for name in list_bar_sessions()
            ],
        })

    def _send_minimal_session_list() -> None:
        current = minimal_mode.session_name
        _send({
            "type": "minimal_session_list",
            "sessions": [
                {
                    "name": name,
                    "messages": minimal_session_msg_count(name),
                    "current": bool(current and name == current),
                }
                for name in list_minimal_sessions()
            ],
        })

    def _send_local_mode_info() -> None:
        _send(local_mode.menu_event())

    def _send_bar_state(notice: str = "") -> None:
        _send({
            "type": "bar_state",
            "session": bar_mode.session_name,
            "drink": bar_mode.drink.to_event(),
            "ambiance": bar_mode.ambiance.to_event(),
            "lyra_glass": bar_mode.lyra_glass.to_event(),
            "shift": bar_mode.shift.to_event(),
            "output_mode": bar_mode.output_mode,
            "notice": notice,
        })

    session_list_revision = 0

    def _send_session_list(*, include_counts: bool = True) -> int:
        nonlocal session_list_revision
        if bar_mode.active or minimal_mode.active or local_mode.active:
            return session_list_revision
        session_list_revision += 1
        _send(_build_session_list_event(
            agent.context.session_path,
            len(agent.context.messages),
            include_counts=include_counts,
        ))
        return session_list_revision

    async def _hydrate_startup_session_counts(expected_revision: int) -> None:
        try:
            event = await asyncio.to_thread(
                _build_session_list_event,
                agent.context.session_path,
                len(agent.context.messages),
                include_counts=True,
            )
        except Exception:
            logger.exception("startup session count hydration failed")
            return
        # A user may switch/rename/delete a session while the background scan
        # is running. Never overwrite that newer list with the startup snapshot.
        if session_list_revision == expected_revision and not bar_mode.active and not minimal_mode.active and not local_mode.active:
            _send(event)

    # Send the minimum state needed for the first useful frame before scanning
    # hundreds of historical session files for sidebar message counts.
    restored = agent.context.load()
    startup_profiler.mark("session_restore")
    await _send_model_info()
    _send_session_info()
    startup_profile_report = startup_profiler.finish("ready")
    from agent.runtime.runtime_identity import runtime_identity
    startup_profile_report["runtime"] = runtime_identity()
    agent.context.append_diagnostic({"type": "runtime_started", **runtime_identity()})

    # ── Startup dashboard ──
    def _send_startup_status(_statuses=None) -> None:
        from agent.runtime.skill_learning import LearnedSkills
        from agent.runtime.skill_provenance import automatic_names

        learning_mode = learning_store.mode()
        learned_count: int | None = None
        learning_error = ""
        try:
            learned_count = len(automatic_names(LearnedSkills(skill_store)._state()))
        except (OSError, ValueError, KeyError) as exc:
            # A damaged maintenance record must not prevent normal chat.
            # /learn reports the error; leave the original record for recovery.
            learning_error = str(exc)
        _send({"type": "startup_banner",
               "skills": len(skill_store.list()),
               "mcp": [{"name": s.name, "state": s.state, "tools": s.tools, "error": s.error}
                       for s in mcp_manager.statuses],
               "learning": {
                   "mode": learning_mode,
                   "auto": False,
                   "pending": 0,
                   "learned": learned_count,
                   "error": learning_error,
                   "legacy_pending": learning_store.count("pending"),
               },
               "tools": len(tools.tool_names),
               "model": agent.llm.config.model,
               "startup_profile": startup_profile_report})

    # The welcome screen remains mounted after its animation finishes, so
    # repeating the same state event updates MCP/tool counts in place.
    mcp_manager.set_status_listener(_send_startup_status)
    _send_startup_status()

    startup_session_revision = _send_session_list(include_counts=False)
    session_count_task = asyncio.create_task(
        _hydrate_startup_session_counts(startup_session_revision),
        name="startup-session-counts",
    )
    startup_tasks.add(session_count_task)
    session_count_task.add_done_callback(startup_tasks.discard)
    _send_working_memory()
    _send_mode_info()
    _send_bar_session_list()
    _send_minimal_session_list()
    _send_local_mode_info()
    if bar_mode.active:
        _send_bar_state()

    if restored:
        await _send_history()

    channel_router = AgentChannelRouter(agent, agent_turn_lock, sessions_dir(PROJECT_ROOT),
                                        admission=lambda: not restart.draining)
    channel_manager = ChannelManager(load_channels_config(), channel_router.handle)
    channel_manager_holder["manager"] = channel_manager
    # The QQ reverse-WebSocket port is a process-wide resource, while the CLI
    # backend itself is intentionally multi-instance. If another backend owns
    # the channel, keep this CLI useful and leave only that optional adapter
    # disabled in this process.
    await _start_channel_manager(channel_manager)

    active_task: asyncio.Task | None = None
    active_task_id = ""
    active_task_persisted = False
    active_wakeup = False
    goal_turn_generation = 0
    _reply_done = False  # True once {"type": "done"} is sent; post-processing may still run

    async def _stop_wakeup_turn() -> None:
        # A tool inside this turn may stop repetition without awaiting its own parent.
        if (wakeup_context.get() is None and active_wakeup
                and active_task is not None and not active_task.done()):
            active_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await active_task

    async def _launch_message(msg: Msg, input_text: str, resume_task: dict | None = None) -> bool:
        nonlocal active_task, active_task_id, goal_turn_generation, _reply_done
        if restart.draining:
            _send({"type": "restart_status", "state": restart.state, "request_id": restart.request_id,
                   "message": "Restart is pending. Use /restart cancel before starting more work."})
            return False
        if active_wakeup and active_task is not None and not active_task.done():
            _wakeup_event(wakeups.cancel("user_interrupted"), "Wakeup stopped for your new request.")
            await _stop_wakeup_turn()
        if appshot_admission.reserved:
            return False
        await _cancel_goal_verifications(exclude=asyncio.current_task())
        if appshot_admission.reserved:
            return False
        if active_task is not None and not active_task.done() and not _reply_done:
            # Auto-steering: user typed while the agent is working.
            # Inject as a mid-run correction instead of rejecting.
            agent.queue_steering(input_text)
            _send({"type": "steering", "message": f"Steering injected into running task {active_task_id}.", "text": input_text})
            return True
        if active_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await active_task
            active_task = None
            active_task_id = ""

        # A user request cancels manual skill maintenance. This
        # also releases the shared LLM request slot immediately.
        await _cancel_manual_reviews()

        if appshot_admission.reserved:
            return False
        if (resume_task is None and _task_tracking_enabled() and task_store is not None
                and all(block.type == "text" for block in msg.content)):
            from agent.runtime.task_resume import budget_resume_candidate, resume_prompt
            session_id = Path(agent.context.session_path).stem if agent.context.session_path else ""
            candidate = await durable_io(budget_resume_candidate, task_store, session_id, input_text)
            if candidate is not None:
                resume_task = await durable_io(task_store.prepare_resume, candidate["id"])
                msg = Msg(sender="user", role="user",
                          content=build_user_message_content(resume_prompt(resume_task)),
                          metadata=dict(msg.metadata))
                input_text = resume_task["input_text"]
                _send({"type": "tool_result", "name": "resume",
                       "output": f"Resuming task {resume_task['id']} from its iteration checkpoint.",
                       "error": "", "code": ""})
        return _start_message(msg, input_text, resume_task=resume_task)

    def _start_message(msg: Msg, input_text: str, resume_task: dict | None = None, *, appshot_turn_owned: bool = False) -> bool:
        nonlocal active_task, active_task_id, active_task_persisted, active_wakeup, goal_turn_generation, _reply_done
        active_wakeup = msg.metadata.get("source") == "session_wakeup"
        if agent.llm.config.connection_required:
            _send({"type": "error", "message": "Use /connect and select a model before sending a message."})
            _send({"type": "done"})
            return False
        if appshot_turn_owned and active_task is not None and not active_task.done():
            return False
        request_id = msg.metadata.get("request_id") or msg.id
        active_task_persisted = _task_tracking_enabled()
        persisted = active_task_persisted and task_store is not None
        active_task_id = str(resume_task["id"] if resume_task is not None else request_id)
        goal_turn_generation += 1
        _reply_done = False
        msg.metadata["request_id"] = str(request_id)
        msg.metadata["goal_turn_generation"] = goal_turn_generation
        entered = False

        async def start_and_stream() -> None:
            nonlocal active_task_id, _reply_done, entered
            entered = True
            with profile_request(str(request_id)):
                run = resume_task
                streaming = False

                def create_run() -> None:
                    nonlocal run
                    assert task_store is not None
                    run = task_store.start_run(
                        str(request_id), input_text,
                        session_id=Path(agent.context.session_path).stem if agent.context.session_path else "",
                        model=agent.llm.config.model,
                    )

                try:
                    if persisted and run is None:
                        await durable_io(create_run)
                    if run is None:
                        run = {"id": str(request_id), "status": "running", "input_text": input_text}
                    active_task_id = str(run["id"])
                    msg.metadata.pop("task_id", None)
                    if not bar_mode.active and not local_mode.active:
                        msg.metadata["task_id"] = active_task_id
                    if not bar_mode.active:
                        msg.metadata["runtime_task_id"] = active_task_id
                        _send({"type": "task_started", "task": run})
                    def enter_stream() -> None:
                        nonlocal streaming
                        streaming = True

                    await _stream_reply(msg, appshot_turn_owned=appshot_turn_owned, on_enter=enter_stream)
                except (asyncio.CancelledError, Exception) as exc:
                    if streaming:
                        raise
                    cancelled = isinstance(exc, asyncio.CancelledError)
                    if persisted and run is not None and task_store is not None:
                        if cancelled:
                            await durable_io(task_store.request_cancel, str(run["id"]))
                        await durable_io(task_store.finish_run, str(run["id"]),
                                         "cancelled" if cancelled else "failed",
                                         "Cancelled before model execution" if cancelled else type(exc).__name__)
                        task = await durable_io(task_store.get_task, str(run["id"]))
                        _send({"type": "task_status", "task": task})
                    _reply_done = True
                    _send({"type": "error", "code": "cancelled" if cancelled else "task_start_failed",
                           "message": "Cancelled before model execution." if cancelled else "Task could not be started."})
                    _send({"type": "done"})
                    if not cancelled:
                        logger.exception("task start failed")

        def cancelled_before_start(task: asyncio.Task) -> None:
            nonlocal _reply_done
            if task.cancelled() and not entered:
                _reply_done = True
                _send({"type": "error", "code": "cancelled", "message": "Cancelled before model execution."})
                _send({"type": "done"})

        coroutine = start_and_stream()
        if appshot_turn_owned:
            active_task = schedule_reserved_turn(coroutine, agent_turn_lock, name=f"task-{active_task_id}")
        else:
            active_task = asyncio.create_task(coroutine, name=f"task-{active_task_id}")
        active_task.add_done_callback(cancelled_before_start)
        return True

    from agent.cli.appshot_admission import AppshotAdmission, prepare_appshot_message, schedule_reserved_turn

    async def _prepare_appshot(msg):
        await _cancel_goal_verifications(exclude=asyncio.current_task())
        await _cancel_manual_reviews()
        return await prepare_appshot_message(agent, msg)

    appshot_admission = AppshotAdmission(
        lock=agent_turn_lock,
        busy=lambda: restart.draining or (active_task is not None and not active_task.done()),
        context=lambda: agent.context,
        prepare=_prepare_appshot,
        launch=lambda msg, text: _start_message(msg, text, appshot_turn_owned=True),
        send=_send,
    )

    async def _lifecycle_tick() -> None:
        while True:
            busy = (agent_turn_lock.locked() or appshot_admission.reserved
                    or (active_task is not None and not active_task.done())
                    or any(not task.done() for task in (*goal_tasks, *manual_reviews)))
            event = restart.advance(busy=busy, session=Path(agent.context.session_path).stem)
            if event is not None:
                _send(event)
            previous = wakeups.status()
            # A channel turn temporarily borrows agent.context under the same lock.
            # Only an idle context is authoritative for a Work session switch.
            session = (previous.get("session", "") if agent_turn_lock.locked()
                       else Path(agent.context.session_path).stem)
            plan = wakeups.claim(session=session, busy=busy or restart.draining)
            current = wakeups.status()
            if previous["state"] != current["state"] and plan is None:
                await _stop_wakeup_turn()
                _wakeup_event(current, f"Session wakeup stopped: {current['state']}.")
            if plan is not None:
                text = wakeup_prompt(plan)
                msg = Msg(sender="scheduler", role="user", content=build_user_message_content(text),
                          metadata={"source": "session_wakeup", "wakeup_plan": plan})
                if not _start_message(msg, plan["prompt"]):
                    status = wakeups.finish(plan["id"], outcome="failed", summary="Could not start the wakeup turn.")
                    if status:
                        _wakeup_event(status, status["summary"])
            await asyncio.sleep(0.25)

    lifecycle_task = asyncio.create_task(_lifecycle_tick(), name="session-lifecycle")

    try:
        # 主循环
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(cmd, dict):
                _send({
                    "type": "error",
                    "message": "Invalid backend event: expected a JSON object.",
                })
                _send({"type": "done"})
                continue

            if cmd.get("type") == "exit":
                terminal_output_failed = cmd.get("reason") == "terminal_output_failure"
                break

            elif cmd.get("type") == "restart_ack":
                if restart.acknowledge(str(cmd.get("request_id") or "")):
                    try:
                        await agent.context.save_async(allow_empty=True)
                    except Exception:
                        _send(restart.cancel("Restart cancelled because the session could not be saved."))
                        continue
                    exit_code = RESTART_EXIT_CODE
                    _wakeup_event(wakeups.cancel("backend_restarted"), "Session wakeups stop on backend restart.")
                    break

            elif cmd.get("type") == "performance_ack":
                profile = current_profiler()
                if profile is not None:
                    profile.acknowledge(cmd.get("samples"))

            elif cmd.get("type") == "tool_approval_response":
                request_id = str(cmd.get("request_id") or "")
                decision = str(cmd.get("decision") or "deny").lower()
                if not resolve_tool_approval(request_id, decision):
                    _send({
                        "type": "approval_response_rejected",
                        "request_id": request_id,
                        "reason": "Approval is no longer attached to a live tool call; retry the task.",
                    })

            elif cmd.get("type") == "event_replay":
                after_cursor = _positive_int(cmd.get("after_cursor")) or 0
                limit = min(_positive_int(cmd.get("limit")) or 500, 2_000)
                if _event_writer is not None:
                    _event_writer.replay(after_cursor, limit=limit)
                    continue
                replayed = (
                    _runtime_event_stream.replay(after_cursor, limit=limit)
                    if _runtime_event_stream is not None
                    else []
                )
                for event in replayed:
                    _write_event(event)
                _send({
                    "type": "event_replay_complete",
                    "after_cursor": after_cursor,
                    "next_cursor": (
                        int(replayed[-1]["cursor"])
                        if replayed
                        else after_cursor
                    ),
                    "cursor": (
                        _runtime_event_stream.cursor
                        if _runtime_event_stream is not None
                        else after_cursor
                    ),
                    "count": len(replayed),
                    "has_more": len(replayed) >= limit,
                })

            elif cmd.get("type") == "user_question_response":
                request_id = str(cmd.get("request_id") or "")
                accepted, reason, retryable = question_broker.resolve(
                    request_id,
                    {"answers": cmd.get("answers")},
                )
                if not accepted:
                    _send({
                        "type": "user_question_response_rejected",
                        "request_id": request_id,
                        "reason": reason,
                        "retryable": retryable,
                    })

            elif cmd.get("type") == "user_question_cancel":
                request_id = str(cmd.get("request_id") or "")
                accepted, reason = question_broker.cancel(
                    request_id,
                    "The user dismissed the question request.",
                )
                if not accepted:
                    _send({
                        "type": "user_question_response_rejected",
                        "request_id": request_id,
                        "reason": reason,
                        "retryable": False,
                    })

            elif cmd.get("type") == "submission_status":
                submission_id = cmd.get("submission_id")
                if isinstance(submission_id, str) and len(submission_id) <= 128:
                    _send(appshot_admission.status(submission_id))

            elif cmd.get("type") == "message":
                if isinstance(cmd.get("text"), str):
                    latest_user_request = cmd["text"][:8000]
                if cmd.get("appshots") or "submission_id" in cmd:
                    if active_wakeup and active_task is not None and not active_task.done():
                        _wakeup_event(wakeups.cancel("user_interrupted"), "Wakeup stopped for your new request.")
                        await _stop_wakeup_turn()
                    await appshot_admission.submit(cmd)
                    continue
                text = cmd.get("text")
                if not isinstance(text, str):
                    _send({
                        "type": "error",
                        "message": "Invalid message event: expected a string field 'text'.",
                    })
                    _send({"type": "done"})
                    continue
                max_bytes = int(os.getenv("MAX_IMAGE_UPLOAD_BYTES", str(10 * 1024 * 1024)))
                msg = Msg(sender="user", role="user", content=build_user_message_content(text, max_bytes=max_bytes))
                await _launch_message(msg, text)

            elif cmd.get("type") == "image":
                try:
                    latest_user_request = str(cmd.get("prompt", ""))[:8000]
                    max_bytes = int(os.getenv("MAX_IMAGE_UPLOAD_BYTES", str(10 * 1024 * 1024)))
                    blocks = build_image_message_content(
                        cmd.get("path", ""),
                        cmd.get("prompt", ""),
                        max_bytes=max_bytes,
                    )
                    msg = Msg(sender="user", role="user", content=blocks)
                    await _launch_message(msg, f"/image {cmd.get('path', '')} {cmd.get('prompt', '')}".strip())
                except Exception as e:
                    _send({"type": "error", "message": str(e)})

            elif cmd.get("type") == "refresh_models":
                provider_id = str(cmd.get("provider_id", ""))
                if not provider_id:
                    await _send_model_info()  # Opening the provider list performs no network I/O.
                elif provider_id not in catalog_tasks or catalog_tasks[provider_id].done():
                    catalog_tasks[provider_id] = asyncio.create_task(_refresh_provider(provider_id, bool(cmd.get("force"))))

            elif cmd.get("type") == "connect_provider":
                if restart.draining:
                    _send({"type": "connection_result", "request_id": str(cmd.get("request_id") or ""),
                           "error": "Restart is pending. Cancel it before changing connections."})
                    continue
                if "connect" not in catalog_tasks or catalog_tasks["connect"].done():
                    catalog_tasks["connect"] = asyncio.create_task(_connect_provider(dict(cmd)),
                        name="connection:" + str(cmd.get("request_id", "")))
                else:
                    _send({"type": "connection_result", "request_id": str(cmd.get("request_id", "")),
                           "error": "A connection request is still running."})
                cmd.pop("api_key", None)

            elif cmd.get("type") == "cancel_connection":
                connection_task = catalog_tasks.get("connect")
                if connection_task and connection_task.get_name() == "connection:" + str(cmd.get("request_id", "")):
                    connection_task.cancel()

            elif cmd.get("type") == "command":
                raw_command = cmd.get("cmd")
                if not isinstance(raw_command, str):
                    _send({
                        "type": "error",
                        "message": "Invalid command event: expected a string field 'cmd'.",
                    })
                    _send({"type": "done"})
                    continue
                c = raw_command.strip()
                try:
                    workflow = workflow_message(c)
                    if workflow is not None:
                        _require_local_work_session()
                        if active_task is not None and not active_task.done() and not _reply_done:
                            _send({"type": "error", "message": "Finish or cancel the active task before starting a command workflow."})
                        else:
                            await _launch_message(workflow, c)
                        continue
                    c = normalize_direct_command(c)
                except (ValueError, RuntimeError) as exc:
                    _send({"type": "error", "message": str(exc)})
                    _send({"type": "done"})
                    continue
                if c == "/wakeup" or c.startswith("/wakeup "):
                    try:
                        _require_local_work_session()
                        if c == "/wakeup cancel":
                            await _cancel_wakeup()
                        elif c == "/wakeup":
                            _wakeup_event(wakeups.status(), await _wakeup_status())
                        else:
                            parts = c.split(maxsplit=3)
                            if len(parts) != 4 or parts[1] not in {"after", "every"}:
                                raise ValueError("Usage: /wakeup [cancel | after SECONDS PROMPT | every SECONDS PROMPT]")
                            latest_user_request = c[:8000]
                            seconds = float(parts[2])
                            await _schedule_wakeup(parts[3], delay_seconds=seconds,
                                                   interval_seconds=seconds if parts[1] == "every" else 0)
                    except ValueError as exc:
                        _wakeup_event(wakeups.status(), str(exc))
                    continue
                if c == "/restart" or c.startswith("/restart "):
                    try:
                        _require_local_work_session()
                        if c == "/restart cancel":
                            _send(restart.cancel())
                        elif c == "/restart":
                            _send(restart.request())
                        else:
                            raise ValueError("Usage: /restart [cancel]")
                    except ValueError as exc:
                        _send({"type": "restart_status", "state": restart.state,
                               "request_id": restart.request_id, "message": str(exc)})
                    continue
                if restart.draining and c.split(maxsplit=1)[:1] not in (["/cancel"], ["/yolo"]):
                    _send({"type": "restart_status", "state": restart.state,
                           "request_id": restart.request_id,
                           "message": "Restart is pending; use /restart cancel before changing the session."})
                    continue
                if c.lower().split(maxsplit=1)[:1] not in (["/learn"], ["/yolo"], ["/cancel"]):
                    await _cancel_manual_reviews()
                if c.lower().split(maxsplit=1)[:1] == ["/yolo"]:
                    # A control acknowledgement is not a model/tool completion.
                    # Keep this path synchronous so it can also unblock approvals.
                    error = ""
                    parts = c.lower().split()
                    if _isolated_mode_blocks_command(
                        c.lower(), bar_active=bar_mode.active,
                        minimal_active=minimal_mode.active, local_active=local_mode.active,
                    ):
                        error = "YOLO is available in Work and Minimal modes."
                    elif len(parts) > 2 or (len(parts) == 2 and parts[1] not in {"on", "off", "status"}):
                        error = "Usage: /yolo [on|off|status]"
                    else:
                        action = parts[1] if len(parts) == 2 else "toggle"
                        if action != "status":
                            agent.tools.yolo = not agent.tools.yolo if action == "toggle" else action == "on"
                            if agent.tools.yolo:
                                for request_id in tuple(pending_yolo_approvals):
                                    resolve_tool_approval(request_id, "once")
                    _send({"type": "yolo_status", "yolo": agent.tools.yolo, "error": error})
                elif c == "/bar" or c.startswith("/bar "):
                    if minimal_mode.active:
                        _send({"type": "tool_result", "name": "bar", "output": "", "error": "Minimal mode is open. Use /minimal leave first.", "code": ""})
                        _send({"type": "done"})
                        continue
                    if local_mode.active:
                        _send({"type": "tool_result", "name": "bar", "output": "", "error": f"{local_mode.label} mode is open. Use {local_mode.command} leave first.", "code": ""})
                        _send({"type": "done"})
                        continue
                    if active_task is not None and not active_task.done():
                        _send({"type": "tool_result", "name": "bar", "output": "", "error": "Finish or cancel the current reply before changing bar mode.", "code": ""})
                        _send({"type": "done"})
                        continue
                    argument = c.split(maxsplit=1)[1].strip() if " " in c else "enter"
                    action = argument.lower()
                    refresh_history = False
                    await _cancel_manual_reviews()
                    if action in {"enter", "open"}:
                        available = list_bar_sessions()
                        name = available[0] if available else create_bar_session_name()
                        if bar_mode.enter(bar_session_path(name)):
                            refresh_history = True
                            output, error = (
                                f"Night bar opened: {name}. Work context is parked; memory, work tools, tasks, and learning are off.\n"
                                "This bar session is saved separately. Use /bar leave to return or /bar new for another shift.",
                                "",
                            )
                        else:
                            output, error = "Night bar is already open. Use /bar leave to return to work.", ""
                    elif action in {"leave", "close", "exit"}:
                        if bar_mode.leave():
                            refresh_history = True
                            output, error = "Night bar closed. Restored the untouched work context.", ""
                        else:
                            output, error = "", "Night bar is not open. Use /bar to enter."
                    elif action in {"new", "reset"}:
                        name = create_bar_session_name()
                        if bar_mode.active:
                            bar_mode.switch(bar_session_path(name))
                        else:
                            bar_mode.enter(bar_session_path(name))
                        refresh_history = True
                        output, error = f"Started a new saved bar session: {name}.", ""
                    elif action in {"sip", "drink"}:
                        if bar_mode.active:
                            _, output = bar_mode.sip()
                            error = ""
                        else:
                            output, error = "", "Night bar is not open. Use /bar to enter."
                    elif action == "status":
                        output, error = (
                            f"Night bar: open · session {bar_mode.session_name} · output {bar_mode.output_mode} · saved separately · memory off · work tools off · scene actions on · learning off"
                            if bar_mode.active
                            else f"Night bar: closed · output {bar_mode.output_mode} · work context active",
                            "",
                        )
                    elif action == "output" or action.startswith("output "):
                        output_args = action.split()
                        if len(output_args) == 1 or output_args[1] == "status":
                            output, error = (
                                f"Bar output mode: {bar_mode.output_mode}. Use /bar output atomic or /bar output stream.",
                                "",
                            )
                        elif len(output_args) == 2 and output_args[1] in {"atomic", "stream"}:
                            selected_mode = output_args[1]
                            bar_mode.set_output_mode(selected_mode)
                            save_bar_output_mode(selected_mode)
                            explanation = (
                                "Replies wait for one validated bar_turn, keeping text and glass state atomic."
                                if selected_mode == "atomic"
                                else "Replies stream immediately; individual scene tools update the bar as they complete."
                            )
                            output, error = f"Bar output mode set to {selected_mode}. {explanation}", ""
                        else:
                            output, error = "", "Usage: /bar output [atomic|stream|status]"
                    else:
                        name = argument
                        try:
                            target = bar_session_path(name)
                            existed = bar_session_exists(name)
                            if bar_mode.active:
                                bar_mode.switch(target)
                            else:
                                bar_mode.enter(target)
                            refresh_history = True
                            output, error = (
                                f"Switched to saved bar session: {name}."
                                if existed else f"Opened new saved bar session: {name}.",
                                "",
                            )
                        except SessionNameError as exc:
                            output, error = "", str(exc)
                    await _send_model_info()
                    _send_session_info()
                    _send_session_list()
                    _send_working_memory()
                    _send_mode_info()
                    # Set the frontend mode before restoring history so its
                    # renderer cannot format a private-mode transcript with
                    # the previous mode's role prefixes.
                    if refresh_history:
                        await _send_history()
                    _send_bar_session_list()
                    _send_bar_state(output if action in {"sip", "drink"} and not error else "")
                    if error or action not in {"sip", "drink"}:
                        _send({"type": "tool_result", "name": "bar", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/minimal" or c.startswith("/minimal "):
                    if bar_mode.active:
                        _send({"type": "tool_result", "name": "minimal", "output": "", "error": "Night bar is open. Use /bar leave first.", "code": ""})
                        _send({"type": "done"})
                        continue
                    if local_mode.active:
                        _send({"type": "tool_result", "name": "minimal", "output": "", "error": f"{local_mode.label} mode is open. Use {local_mode.command} leave first.", "code": ""})
                        _send({"type": "done"})
                        continue
                    if active_task is not None and not active_task.done():
                        _send({"type": "tool_result", "name": "minimal", "output": "", "error": "Finish or cancel the current reply before changing minimal mode.", "code": ""})
                        _send({"type": "done"})
                        continue
                    argument = c.split(maxsplit=1)[1].strip() if " " in c else "enter"
                    action = argument.lower()
                    refresh_history = False
                    await _cancel_manual_reviews()
                    if action in {"enter", "open"}:
                        available = list_minimal_sessions()
                        name = available[0] if available else create_minimal_session_name()
                        try:
                            entered = minimal_mode.enter(minimal_session_path(name))
                        except Exception as exc:
                            entered = False
                            output, error = "", f"Minimal mode could not open: {exc}"
                        if entered:
                            refresh_history = True
                            output, error = (
                                f"Minimal mode opened: {name}. Work context is parked; memory, skills, tasks, and work tools are off.\n"
                                "PTC sandbox is forced ON for this isolated session; read-only web tools are available. Use /minimal leave to return.",
                                "",
                            )
                        elif minimal_mode.active:
                            output, error = "Minimal mode is already open. Use /minimal leave to return.", ""
                        else:
                            output, error = "", "Minimal mode could not open."
                    elif action in {"new", "reset"}:
                        name = create_minimal_session_name()
                        try:
                            if minimal_mode.active:
                                minimal_mode.switch(minimal_session_path(name))
                            else:
                                minimal_mode.enter(minimal_session_path(name))
                            refresh_history = True
                            output, error = f"Started a new saved Minimal session: {name}.", ""
                        except (SessionNameError, OSError, RuntimeError) as exc:
                            output, error = "", f"Minimal session could not start: {exc}"
                    elif action in {"leave", "close", "exit"}:
                        if minimal_mode.leave():
                            refresh_history = True
                            output, error = "Minimal mode closed. Restored the untouched work context.", ""
                        else:
                            output, error = "", "Minimal mode is not open. Use /minimal to enter."
                    elif action == "status":
                        output, error = (
                            MINIMAL_STATUS_OPEN_TEMPLATE.format(session=minimal_mode.session_name)
                            if minimal_mode.active
                            else "Minimal mode: closed · work context active",
                            "",
                        )
                    elif action == "sessions":
                        names = list_minimal_sessions()
                        output = "Minimal sessions:\n" + "\n".join(
                            f"  {'*' if name == minimal_mode.session_name else ' '} {name} ({minimal_session_msg_count(name)} msgs)"
                            for name in names
                        ) if names else "Minimal sessions: none"
                        error = ""
                    else:
                        try:
                            target = minimal_session_path(argument)
                            existed = minimal_session_exists(argument)
                            if minimal_mode.active:
                                minimal_mode.switch(target)
                            else:
                                minimal_mode.enter(target)
                            refresh_history = True
                            output, error = (
                                f"Switched to saved Minimal session: {argument}."
                                if existed else f"Opened new saved Minimal session: {argument}.",
                                "",
                            )
                        except (SessionNameError, OSError, RuntimeError) as exc:
                            output, error = "", str(exc)
                    await _send_model_info()
                    _send_session_info()
                    _send_session_list()
                    _send_working_memory()
                    _send_mode_info()
                    _send({"type": "yolo_status", "yolo": agent.tools.yolo})
                    if refresh_history:
                        await _send_history()
                    _send_minimal_session_list()
                    _send({"type": "tool_result", "name": "minimal", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif local_mode.matches(c):
                    if bar_mode.active:
                        _send({"type": "tool_result", "name": local_mode.tool_name, "output": "", "error": "Night bar is open. Use /bar leave first.", "code": ""})
                        _send({"type": "done"})
                        continue
                    if minimal_mode.active:
                        _send({"type": "tool_result", "name": local_mode.tool_name, "output": "", "error": "Minimal mode is open. Use /minimal leave first.", "code": ""})
                        _send({"type": "done"})
                        continue
                    if _reply_done and active_task is not None:
                        with suppress(asyncio.CancelledError, Exception):
                            await active_task
                    if active_task is not None and not active_task.done():
                        _send({"type": "tool_result", "name": local_mode.tool_name, "output": "", "error": f"Finish or cancel the current reply before changing {local_mode.label.lower()} mode.", "code": ""})
                        if _reply_done:
                            _send({"type": "done"})
                        continue
                    argument = c.split(maxsplit=1)[1].strip() if " " in c else "enter"
                    action = argument.lower()
                    refresh_history = False
                    resend_text = ""
                    await _cancel_manual_reviews()
                    if action in {"enter", "open"}:
                        available = local_mode.list_sessions()
                        name = available[0] if available else local_mode.new_session_name()
                        try:
                            entered = local_mode.enter(local_mode.session_path(name))
                        except Exception as exc:
                            entered = False
                            output, error = "", f"{local_mode.label} mode could not open: {exc}"
                        if entered:
                            refresh_history = True
                            output, error = (
                                f"{local_mode.label} mode opened: {name}. Work context is parked; memory, skills, tasks, and work tools are off.\n"
                                f"This {local_mode.label.lower()} session is saved separately. Use {local_mode.command} leave to return to work.",
                                "",
                            )
                        elif local_mode.active:
                            output, error = f"{local_mode.label} mode is already open. Use {local_mode.command} leave to return.", ""
                        else:
                            output, error = "", f"{local_mode.label} mode could not open."
                    elif action in {"new", "reset"}:
                        name = local_mode.new_session_name()
                        try:
                            if local_mode.active:
                                local_mode.switch(local_mode.session_path(name))
                            else:
                                local_mode.enter(local_mode.session_path(name))
                            refresh_history = True
                            output, error = f"Started a new saved {local_mode.label.lower()} session: {name}.", ""
                        except (SessionNameError, OSError, RuntimeError) as exc:
                            output, error = "", f"{local_mode.label} session could not start: {exc}"
                    elif action in {"leave", "close", "exit"}:
                        try:
                            if local_mode.leave():
                                refresh_history = True
                                output, error = f"{local_mode.label} mode closed. Restored the untouched work context.", ""
                            else:
                                output, error = "", f"{local_mode.label} mode is not open. Use {local_mode.command} to enter."
                        except (OSError, RuntimeError) as exc:
                            output, error = "", f"{local_mode.label} mode could not close: {exc}"
                    elif action == "status":
                        output, error = (
                            local_mode.status_template.format(session=local_mode.session_name)
                            if local_mode.active
                            else f"{local_mode.label} mode: closed · work context active",
                            "",
                        )
                    elif action == "undo" or action.startswith("undo "):
                        if action == "undo":
                            ok, message = local_mode.undo_last_reply()
                        else:
                            count_text = action[5:].strip()
                            try:
                                count = int(count_text)
                            except ValueError:
                                count = 0
                            if 1 <= count <= 50:
                                ok, message = local_mode.undo_last_exchanges(count)
                            else:
                                ok, message = False, f"Usage: {local_mode.command} undo [count] · count must be an integer between 1 and 50."
                        if ok:
                            refresh_history = True
                            output, error = message, ""
                        else:
                            output, error = "", message
                    elif action == "retry":
                        ok, message, retry_text = local_mode.take_last_request()
                        if ok:
                            refresh_history = True
                            snippet = retry_text if len(retry_text) <= 40 else f"{retry_text[:40]}…"
                            output, error = f'Retrying the last request: "{snippet}"', ""
                            resend_text = retry_text
                        else:
                            output, error = "", message
                    elif action == "sessions":
                        names = local_mode.list_sessions()
                        output = f"{local_mode.label} sessions:\n" + "\n".join(
                            f"  {'*' if name == local_mode.session_name else ' '} {name} ({local_mode.session_msg_count(name)} msgs)"
                            for name in names
                        ) if names else f"{local_mode.label} sessions: none"
                        error = ""
                    else:
                        try:
                            target = local_mode.session_path(argument)
                            existed = local_mode.session_exists(argument)
                            if local_mode.active:
                                local_mode.switch(target)
                            else:
                                local_mode.enter(target)
                            refresh_history = True
                            output, error = (
                                f"Switched to saved {local_mode.label.lower()} session: {argument}."
                                if existed else f"Opened new saved {local_mode.label.lower()} session: {argument}.",
                                "",
                            )
                        except (SessionNameError, OSError, RuntimeError) as exc:
                            output, error = "", str(exc)
                    await _send_model_info()
                    _send_session_info()
                    _send_session_list()
                    _send_working_memory()
                    _send_mode_info()
                    _send({"type": "yolo_status", "yolo": agent.tools.yolo})
                    if refresh_history:
                        await _send_history()
                    _send_local_mode_info()
                    _send({"type": "tool_result", "name": local_mode.tool_name, "output": output, "error": error, "code": ""})
                    if resend_text:
                        retry_msg = Msg(sender="user", role="user", content=build_user_message_content(resend_text))
                        launched = await _launch_message(retry_msg, resend_text)
                        if not launched:
                            _send({"type": "tool_result", "name": local_mode.tool_name, "output": "", "error": "Retry could not start; resend the request manually.", "code": ""})
                            _send({"type": "done"})
                    else:
                        _send({"type": "done"})
                elif c == "/sip":
                    if active_task is not None and not active_task.done():
                        _send({"type": "tool_result", "name": "sip", "output": "", "error": "Finish or cancel the current reply before drinking.", "code": ""})
                    elif not bar_mode.active:
                        _send({"type": "tool_result", "name": "sip", "output": "", "error": "Night bar is not open. Use /bar to enter.", "code": ""})
                    else:
                        _, output = bar_mode.sip()
                        _send_bar_state(output)
                    _send({"type": "done"})
                elif _isolated_mode_blocks_command(
                    c,
                    bar_active=bar_mode.active,
                    minimal_active=minimal_mode.active,
                    local_active=local_mode.active,
                ):
                    mode_name = "bar" if bar_mode.active else ("minimal" if minimal_mode.active else local_mode.tool_name)
                    _send({"type": "tool_result", "name": mode_name, "output": "", "error": f"This command belongs to the work context. Use /{mode_name} leave first.", "code": ""})
                    _send({"type": "done"})
                elif c == "/changes" or c.startswith("/changes "):
                    if _reply_done and active_task is not None:
                        with suppress(asyncio.CancelledError, Exception):
                            await active_task
                    if active_task is not None and not active_task.done():
                        _send({"type": "tool_result", "name": "changes", "output": "", "error": "Finish or cancel the current reply before viewing changes.", "code": ""})
                        if _reply_done:
                            _send({"type": "done"})
                        continue
                    output, error = execute_changes_command(agent, c[len("/changes"):].strip())
                    _send({"type": "tool_result", "name": "changes", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/undo" or c.startswith("/undo ") or c == "/retry":
                    command_name = "retry" if c == "/retry" else "undo"
                    if _reply_done and active_task is not None:
                        with suppress(asyncio.CancelledError, Exception):
                            await active_task
                    if active_task is not None and not active_task.done():
                        _send({"type": "tool_result", "name": command_name, "output": "", "error": "Finish or cancel the current reply before editing history.", "code": ""})
                        if _reply_done:
                            _send({"type": "done"})
                        continue
                    resend_text = ""
                    refresh_history = False
                    if command_name == "retry":
                        ok, message, retry_text = conversation_edits.take_last_request(agent.context)
                        if ok:
                            refresh_history = True
                            snippet = retry_text if len(retry_text) <= 40 else f"{retry_text[:40]}…"
                            output, error = f'Retrying the last request: "{snippet}"', ""
                            resend_text = retry_text
                        else:
                            output, error = "", message
                    elif c == "/undo":
                        ok, message = conversation_edits.undo_last_reply(agent.context)
                        if ok:
                            refresh_history = True
                            output, error = message, ""
                        else:
                            output, error = "", message
                    else:
                        count_text = c[len("/undo "):].strip()
                        try:
                            count = int(count_text)
                        except ValueError:
                            count = 0
                        if 1 <= count <= 50:
                            ok, message = conversation_edits.undo_last_exchanges(agent.context, count)
                            if ok:
                                refresh_history = True
                                output, error = message, ""
                            else:
                                output, error = "", message
                        else:
                            output, error = "", "Usage: /undo [count] · count must be an integer between 1 and 50."
                    if refresh_history:
                        await _send_history()
                        if minimal_mode.active:
                            _send_minimal_session_list()
                        elif local_mode.active:
                            _send_local_mode_info()
                        else:
                            _send_session_info()
                            _send_session_list()
                    _send({"type": "tool_result", "name": command_name, "output": output, "error": error, "code": ""})
                    if resend_text:
                        retry_msg = Msg(sender="user", role="user", content=build_user_message_content(resend_text))
                        launched = await _launch_message(retry_msg, resend_text)
                        if not launched:
                            _send({"type": "tool_result", "name": "retry", "output": "", "error": "Retry could not start; resend the request manually.", "code": ""})
                            _send({"type": "done"})
                    else:
                        _send({"type": "done"})
                elif c == "/reset" and minimal_mode.active:
                    minimal_mode.reset()
                    await _send_history()
                    await _send_model_info(refresh=True)
                    _send_mode_info()
                    _send({"type": "yolo_status", "yolo": agent.tools.yolo})
                    _send_minimal_session_list()
                    _send_working_memory()
                    _send({"type": "tool_result", "name": "minimal", "output": "Minimal session reset. Work context remains parked.", "error": "", "code": ""})
                    _send({"type": "done"})
                elif c == "/reset" and bar_mode.active:
                    name = create_bar_session_name()
                    bar_mode.switch(bar_session_path(name))
                    await _send_history()
                    await _send_model_info()
                    _send_mode_info()
                    _send_bar_session_list()
                    _send_bar_state()
                    _send({"type": "tool_result", "name": "bar", "output": f"Started a new saved bar session: {name}.", "error": "", "code": ""})
                    _send({"type": "done"})
                elif c == "/reset" and local_mode.active:
                    name = local_mode.new_session_name()
                    local_mode.switch(local_mode.session_path(name))
                    await _send_history()
                    await _send_model_info()
                    _send_mode_info()
                    _send_local_mode_info()
                    _send({"type": "tool_result", "name": local_mode.tool_name, "output": f"Started a new saved {local_mode.label.lower()} session: {name}.", "error": "", "code": ""})
                    _send({"type": "done"})
                elif c == "/tasks" or c.startswith("/tasks "):
                    if task_store is None:
                        output, error = "", "Task persistence is disabled; see backend logs."
                    else:
                        parts = c.split(maxsplit=1)
                        if len(parts) == 1:
                            output, error = format_task_list((await durable_io(task_store.list_tasks))), ""
                        else:
                            task = await durable_io(task_store.get_task, parts[1].strip())
                            output, error = (format_task_detail(task), "") if task else ("", f"Unknown task: {parts[1].strip()}")
                    _send({"type": "tool_result", "name": "tasks", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/goal" or c.startswith("/goal "):
                    goal_start_text = ""
                    if task_store is None:
                        output, error = "", "Task persistence is disabled; see backend logs."
                    else:
                        argument = c.split(maxsplit=1)[1].strip() if " " in c else ""
                        goal_session = _current_memory_session()
                        lowered = argument.lower()
                        goal = None
                        try:
                            if not argument:
                                output, error = (await durable_io(task_store.format_goal_status, goal_session)), ""
                            elif lowered == "pause":
                                goal = await durable_io(task_store.pause_goal, goal_session)
                                output, error = (
                                    (f"Goal paused at round {goal['round']}/{goal['max_rounds']}. Use /goal resume to continue.", "")
                                    if goal else ("", "No active goal to pause.")
                                )
                            elif lowered == "resume":
                                goal = await durable_io(task_store.resume_goal, goal_session)
                                if goal is None:
                                    current = await durable_io(task_store.active_goal_for_session, goal_session)
                                    goal = current if current and current.get("status") == "active" else None
                                if goal is not None:
                                    output, error = f"Goal resumed: {goal['objective']}", ""
                                    goal_start_text = build_goal_resume_message(goal)
                                else:
                                    output, error = "", "No paused or active goal to resume."
                            elif lowered == "clear":
                                goal = await durable_io(task_store.clear_goal, goal_session)
                                output, error = (f"Goal cleared: {goal['objective']}", "") if goal else ("", "No live goal to clear.")
                            else:
                                objective = argument.split(maxsplit=1)[1].strip() if lowered.startswith("replace ") else argument
                                goal = await durable_io(task_store.set_goal, goal_session, objective)
                                output, error = (
                                    f"Goal set (round 0/{goal['max_rounds']}): {goal['objective']}\n"
                                    "Every completed turn is now checked by an independent verifier against real "
                                    "evidence; unmet rounds continue automatically. /goal pause | clear to stop.",
                                    "",
                                )
                                goal_start_text = build_goal_start_message(goal)
                        except ValueError as exc:
                            output, error = "", str(exc)
                        if goal is not None:
                            _send({"type": "goal_status", "goal": goal})
                    _send({"type": "tool_result", "name": "goal", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                    if goal_start_text:
                        # ZCode semantics: setting a goal starts the work, not
                        # just records it. Launch the first goal round now.
                        goal_msg = Msg(sender="user", role="user", content=build_user_message_content(goal_start_text))
                        goal_msg.metadata["goal_round"] = True
                        await _launch_message(goal_msg, goal_start_text)
                elif c == "/today":
                    if task_store is None:
                        output, error = "", "Task persistence is disabled; see backend logs."
                    else:
                        output, error = format_today(task_store), ""
                    _send({"type": "tool_result", "name": "today", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/cancel" or c.startswith("/cancel "):
                    target_id = c.split(maxsplit=1)[1].strip() if " " in c else active_task_id
                    if c == "/cancel" and any(not task.done() for task in manual_reviews):
                        await _cancel_manual_reviews()
                        output, error = "Skill review cancelled.", ""
                    elif not target_id:
                        output, error = "", "No active task. Usage: /cancel [task-id]"
                    elif active_task is not None and not active_task.done() and target_id == active_task_id:
                        active_task.cancel()
                        # The owning turn records cancellation during cleanup.
                        # Keep this command free to accept the next live control
                        # even while a previous task-store write is settling.
                        output, error = f"Cancellation requested for task {target_id}.", ""
                    elif task_store is None:
                        output, error = "", "Task persistence is disabled; see backend logs."
                    else:
                        task = await durable_io(task_store.get_task, target_id)
                        if task is None:
                            output, error = "", f"Unknown task: {target_id}"
                        elif task["status"] in {"completed", "failed", "cancelled"}:
                            output, error = "", f"Task {target_id} is already {task['status']}."
                        else:
                            await durable_io(task_store.request_cancel, target_id)
                            await durable_io(task_store.finish_run, target_id, "cancelled", "Cancelled while not active")
                            output, error = f"Task {target_id} marked cancelled.", ""
                    _send({"type": "tool_result", "name": "cancel", "output": output, "error": error, "code": ""})
                    if not (active_task is not None and not active_task.done() and target_id == active_task_id):
                        _send({"type": "done"})
                elif c.startswith("/resume"):
                    parts = c.split(maxsplit=1)
                    if len(parts) < 2:
                        _send({"type": "tool_result", "name": "resume", "output": "", "error": "Usage: /resume <task-id>", "code": ""})
                        _send({"type": "done"})
                    elif task_store is None:
                        _send({"type": "tool_result", "name": "resume", "output": "", "error": "Task persistence is disabled; see backend logs.", "code": ""})
                        _send({"type": "done"})
                    elif active_task is not None and not active_task.done():
                        _send({"type": "tool_result", "name": "resume", "output": "", "error": f"Task {active_task_id} is still running.", "code": ""})
                        _send({"type": "done"})
                    else:
                        task_id = parts[1].strip()
                        task = await durable_io(task_store.get_task, task_id)
                        current_session = Path(agent.context.session_path).stem if agent.context.session_path else ""
                        if task is None:
                            _send({"type": "tool_result", "name": "resume", "output": "", "error": f"Unknown task: {task_id}", "code": ""})
                            _send({"type": "done"})
                        elif task.get("session_id") and task["session_id"] != current_session:
                            _send({"type": "tool_result", "name": "resume", "output": "", "error": f"Task belongs to session '{task['session_id']}'. Switch to it before resuming.", "code": ""})
                            _send({"type": "done"})
                        else:
                            try:
                                resumed = await durable_io(task_store.prepare_resume, task_id)
                                from agent.runtime.task_resume import resume_prompt
                                prompt = resume_prompt(resumed)
                                msg = Msg(sender="user", role="user", content=build_user_message_content(prompt))
                                msg.metadata["request_id"] = resumed["request_id"]
                                _send({"type": "tool_result", "name": "resume", "output": f"Resuming task {task_id} from checkpoint.", "error": "", "code": ""})
                                await _launch_message(msg, resumed["input_text"], resume_task=resumed)
                            except (KeyError, ValueError) as exc:
                                _send({"type": "tool_result", "name": "resume", "output": "", "error": str(exc), "code": ""})
                                _send({"type": "done"})
                elif c == "/reset":
                    if wakeups.status()["state"] in {"scheduled", "running"}:
                        _wakeup_event(wakeups.cancel("session_reset"), "Session wakeup stopped because the conversation was reset.")
                        await _stop_wakeup_turn()
                    latest_user_request = ""
                    current_memory_session = Path(agent.context.session_path).stem or "default"
                    memory_store.clear_working(current_memory_session)
                    agent.end_session("reset")
                    await agent.context.save_async()
                    agent.reset_conversation()
                    agent.context.set_session(str(startup_session_path()))
                    agent.begin_session()
                    learning_store.reset_review_counter(_current_memory_session())
                    await _send_history()
                    await _send_model_info(refresh=True)
                    _send_session_info()
                    _send_session_list()
                    _send_working_memory()
                    _send_startup_status()
                elif c == "/compress":
                    before = len(agent.context.messages)
                    await agent.context.compress_if_needed(force=True)
                    after = len(agent.context.messages)
                    maintenance = _run_memory_consolidation(memory_store)
                    await _send_history()
                    await _send_model_info()
                    _send({"type": "tool_result", "name": "compress",
                           "output": (
                               f"Compressed: {before} -> {after} messages ({before - after} removed)"
                               if before > after else "Nothing to compress"
                           ) + (
                               f"\nMemory maintenance: {maintenance['expired']} expired, "
                               f"{maintenance['merged_observations']} merged, "
                               f"{maintenance['extracted_observations']} extracted."
                           ),
                           "error": "", "code": ""})
                    _send({"type": "done"})
                elif c == "/handoff" or c.startswith("/handoff "):
                    notes = c[len("/handoff"):].strip()
                    path = _save_handoff(agent, task_store, notes=notes)
                    _send({
                        "type": "tool_result",
                        "name": "handoff",
                        "output": f"Saved redacted session handoff to {path}" if path else "",
                        "error": "" if path else "Failed to save session handoff; see backend log.",
                        "code": "",
                    })
                    _send({"type": "done"})
                elif c == "/think" or c.startswith("/think "):
                    parts = c.split(maxsplit=1)
                    if len(parts) == 1:
                        agent.context.show_reasoning = not agent.context.show_reasoning
                    else:
                        value = parts[1].strip().lower()
                        if value not in {"on", "off"}:
                            _send({"type": "tool_result", "name": "think", "output": "", "error": "Usage: /think [on|off]", "code": ""})
                            _send({"type": "done"})
                            continue
                        agent.context.show_reasoning = value == "on"
                    await agent.context.save_async()
                    await _send_model_info()
                    _send({"type": "tool_result", "name": "think", "output": f"Thinking display: {'ON' if agent.context.show_reasoning else 'OFF'}", "error": "", "code": ""})
                    _send({"type": "done"})
                elif c == "/tools":
                    details = sorted(agent.tools.describe(), key=lambda item: item["name"])
                    _send({"type": "tool_result", "name": "tools",
                           "output": "\n".join(f"{item['name']} [{item['risk']}]" for item in details), "error": "", "code": ""})
                    _send({"type": "done"})
                elif c.startswith("/memory"):
                    try:
                        memory_parts = shlex.split(c)[1:]
                    except ValueError as exc:
                        output, error = "", f"Invalid memory command: {exc}"
                    else:
                        output, error = execute_memory_command(
                            memory_store,
                            memory_parts,
                            session_id=Path(agent.context.session_path).stem or "default",
                            router=agent.memory_router,
                            retainer=agent.memory_retainer,
                        )
                    _send({"type": "tool_result", "name": "memory", "output": output, "error": error, "code": ""})
                    if not error:
                        _send_working_memory()
                    _send({"type": "done"})
                elif c.startswith("/skills"):
                    try:
                        skill_parts = shlex.split(c)[1:]
                    except ValueError as exc:
                        output, error = "", f"Invalid skills command: {exc}"
                    else:
                        output, error = execute_skill_command(skill_store, skill_parts)
                    _send_startup_status()
                    _send({"type": "tool_result", "name": "skills", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/learn" or c.startswith("/learn "):
                    try:
                        learn_parts = shlex.split(c)[1:]
                    except ValueError as exc:
                        _send({"type": "tool_result", "name": "learn", "output": "", "error": f"Invalid learn command: {exc}", "code": ""})
                        _send({"type": "done"})
                        continue
                    if learn_parts and learn_parts[0].lower() in {"review", "migrate"}:
                        if active_task is not None and not active_task.done():
                            _send({"type": "tool_result", "name": "learn", "output": "", "error": "Finish or cancel the active task before skill maintenance.", "code": ""})
                            continue
                        if any(not task.done() for task in manual_reviews):
                            _send({"type": "learning_review", "message": "A skill review is already running. Use Ctrl+C to cancel it.", "proposal_ids": []})
                            continue
                        task = asyncio.create_task(_run_skill_command(learn_parts, _current_memory_session(), list(agent.context.messages)), name="manual-skill-review")
                        manual_reviews.add(task)
                        task.add_done_callback(manual_reviews.discard)
                    else:
                        output, error = await execute_learning_command(
                            learning_store, learning_reviewer, memory_store, skill_store, learn_parts,
                            session_id=_current_memory_session(), messages=list(agent.context.messages),
                        )
                        agent._refresh_skill_catalog(force=True)
                        _send_startup_status()
                        _send({"type": "tool_result", "name": "learn", "output": output, "error": error, "code": ""})
                        if not any(not task.done() for task in manual_reviews):
                            _send({"type": "done"})
                elif c == "/browser" or c.startswith("/browser "):
                    output, error = await execute_browser_command(c.split()[1:], tools)
                    _send({"type": "tool_result", "name": "browser", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/computer" or c.startswith("/computer "):
                    try:
                        computer_args = shlex.split(c)[1:]
                    except ValueError as exc:
                        output, error = "", f"Invalid computer command: {exc}"
                    else:
                        output, error = await execute_computer_command(
                            computer_args,
                            computer_runtime,
                            emit_state=computer_state_emitter.emit,
                        )
                    _send({"type": "tool_result", "name": "computer", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/health":
                    _send({"type": "tool_result", "name": "health",
                           "output": build_health_report(agent.llm.config, agent.tools, agent.context.max_prompt_tokens, sandbox),
                           "error": "", "code": ""})
                    _send({"type": "done"})
                elif c == "/doctor" or c.startswith("/doctor "):
                    doctor_section = c.split(maxsplit=1)[1] if " " in c else ""
                    _send({"type": "tool_result", "name": "doctor",
                           "output": await build_doctor_report(agent, sandbox, mcp_manager, doctor_section),
                           "error": "", "code": ""})
                    _send({"type": "done"})
                elif c == "/diagnostics" or c.startswith("/diagnostics "):
                    diagnostics_section = c.split(maxsplit=1)[1] if " " in c else ""
                    _send({
                        "type": "tool_result",
                        "name": "diagnostics",
                        "output": build_runtime_diagnostics(
                            agent,
                            startup_profile=startup_profile_report,
                            mcp_manager=mcp_manager,
                            task_store=task_store,
                            process_manager=process_manager,
                            section=diagnostics_section,
                        ),
                        "error": "",
                        "code": "",
                    })
                    _send({"type": "done"})
                elif c == "/maintenance" or c.startswith("/maintenance "):
                    try:
                        maintenance_parts = shlex.split(c)[1:]
                        maintenance_action = maintenance_parts[0].lower() if maintenance_parts else "preview"
                        maintenance_days = int(maintenance_parts[1]) if len(maintenance_parts) > 1 else 30
                        if len(maintenance_parts) > 2:
                            raise ValueError("too many arguments")
                        database_paths = [
                            path for owner in (
                                task_store,
                                approval_inbox,
                                _runtime_event_stream,
                                memory_store,
                                learning_store,
                            )
                            if owner is not None and (path := getattr(owner, "path", None)) is not None
                        ]
                        maintenance = RuntimeMaintenance(
                            PROJECT_ROOT,
                            artifact_dirs=[agent.tools.artifact_dir],
                            database_paths=database_paths,
                        )
                        output = format_maintenance_report(
                            maintenance,
                            action=maintenance_action,
                            artifact_days=maintenance_days,
                        )
                        error = ""
                    except (TypeError, ValueError) as exc:
                        output = ""
                        error = f"Invalid maintenance command: {exc}. Usage: /maintenance [preview [days]|apply [days]|checkpoint]"
                    _send({
                        "type": "tool_result",
                        "name": "maintenance",
                        "output": output,
                        "error": error,
                        "code": "",
                    })
                    _send({"type": "done"})
                elif c == "/sandbox" or c.startswith("/sandbox "):
                    try:
                        sandbox_parts = shlex.split(c)[1:]
                    except ValueError as exc:
                        output, error = "", f"Invalid sandbox command: {exc}"
                    else:
                        output, error = execute_sandbox_command(sandbox, sandbox_parts)
                    _send({"type": "tool_result", "name": "sandbox",
                           "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/vision-tiles" or c.startswith("/vision-tiles "):
                    try:
                        vision_tile_parts = shlex.split(c)[1:]
                    except ValueError as exc:
                        output, error = "", f"Invalid vision-tiles command: {exc}"
                    else:
                        output, error = execute_vision_tiles_command(
                            agent,
                            vision_tile_parts,
                        )
                    _send({
                        "type": "tool_result",
                        "name": "vision-tiles",
                        "output": output,
                        "error": error,
                        "code": "",
                    })
                    _send({"type": "done"})
                elif c == "/context-index" or c.startswith("/context-index "):
                    try:
                        context_index_parts = shlex.split(c)[1:]
                    except ValueError as exc:
                        output, error = "", f"Invalid context-index command: {exc}"
                    else:
                        output, error = execute_context_index_command(
                            agent,
                            context_index_parts,
                        )
                    _send({
                        "type": "tool_result",
                        "name": "context-index",
                        "output": output,
                        "error": error,
                        "code": "",
                    })
                    _send({"type": "done"})
                elif c == "/budget" or c.startswith("/budget "):
                    output, error = execute_budget_command(agent, c[len("/budget"):].strip())
                    _send({"type": "tool_result", "name": "budget", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c == "/mcp":
                    _send({"type": "tool_result", "name": "mcp",
                           "output": mcp_manager.report(), "error": "", "code": ""})
                    _send({"type": "done"})
                elif c.startswith("/permissions"):
                    parts = c.split()
                    action = parts[1].lower() if len(parts) > 1 else ""
                    value = parts[2] if len(parts) > 2 else ""
                    try:
                        if action == "allow" and value:
                            agent.tools.policy.allow(value)
                            output = f"Approved for this process: {value}"
                        elif action == "deny" and value:
                            agent.tools.policy.deny(value)
                            output = f"Denied for this process: {value}"
                        elif action == "mode" and value:
                            agent.tools.policy.set_mode(value)
                            output = f"Tool policy mode: {agent.tools.policy.mode}"
                        else:
                            output = (f"Tool policy: {agent.tools.policy.mode}\n"
                                      "Usage: /permissions mode <permissive|safe|locked>\n"
                                      "       /permissions allow|deny <tool>")
                        error = ""
                    except ValueError as exc:
                        output, error = "", str(exc)
                    _send({"type": "tool_result", "name": "permissions",
                           "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c.startswith("/connect"):
                    parts = c.split()
                    if len(parts) < 3:
                        output, error = "", "Usage: /connect <model-name> <base-url> [api-key-env]"
                    else:
                        api_key_env = parts[3] if len(parts) > 3 else "LLM_API_KEY"
                        probe, settings_path = await create_probe_and_switch(
                            agent, parts[1], parts[2], api_key_env
                        )
                        if probe.ok:
                            catalog = configured_model_catalog()
                            connected_entry = next((e for e in catalog.entries if e.model_id == parts[1]
                                                    and e.base_url.rstrip("/") == parts[2].rstrip("/")), None)
                            if connected_entry:
                                current_model_key = connected_entry.key
                            output = f"{probe.message}\nSaved profile and startup selection in {settings_path}."
                            error = ""
                        else:
                            output, error = "", f"Connection failed; current model unchanged: {probe.message}"
                    _send({"type": "tool_result", "name": "connect",
                           "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                    await _send_model_info()
                elif c.startswith("/model"):
                    arg = parse_model_command_argument(c)
                    if active_task is not None and not active_task.done():
                        _send({"type": "tool_result", "name": "model", "output": "",
                               "error": "Wait for the current reply or cancel it before switching models.", "code": "busy"})
                        continue
                    if arg:
                        # An explicit provider::model also supports unlisted/custom IDs.
                        # Resolve from current metadata without refreshing other providers.
                        entry = catalog.resolve(arg) or catalog.resolve_persisted(arg)
                        if entry is not None:
                            try:
                                settings_path = switch_to_profile(
                                    agent,
                                    entry.key,
                                    entry.profile,
                                    valid_models=set(catalog.profiles) | {entry.key},
                                )
                            except (ValueError, OSError) as exc:
                                _send({
                                    "type": "tool_result",
                                    "name": "model",
                                    "output": "",
                                    "error": f"Model switch rejected; current model unchanged: {exc}",
                                    "code": "provider_not_configured",
                                })
                            else:
                                current_model_key = entry.key
                                if not catalog.resolve(entry.key):
                                    catalog = ModelCatalog((*catalog.entries, entry), catalog.errors, catalog.statuses)
                                _send({"type": "tool_result", "name": "model",
                                       "output": f"Model connection switched to: {entry.model_id}\n"
                                                 f"Provider: {entry.provider_label} @ {entry.base_url}\n"
                                                 f"Saved as startup default in {settings_path}.\n"
                                                 "Model access and tool support are checked when used.",
                                       "error": "", "code": ""})
                        else:
                            lines = [f"Unknown model: {arg}. Available:"]
                            for provider in dict.fromkeys(item.provider_label for item in catalog.entries):
                                lines.append(f"  [{provider}]")
                                for item in catalog.entries:
                                    if item.provider_label == provider:
                                        marker = "*" if item.key == current_model_key else " "
                                        lines.append(f"    {marker} {item.model_id} ({item.key})")
                            _send({"type": "tool_result", "name": "model",
                                   "output": "\n".join(lines), "error": "", "code": ""})
                    else:
                        lines = [f"Current model: {agent.llm.config.model} @ {agent.llm.config.base_url}", "Available:"]
                        for provider in dict.fromkeys(item.provider_label for item in catalog.entries):
                            lines.append(f"  [{provider}]")
                            for item in catalog.entries:
                                if item.provider_label == provider:
                                    marker = "*" if item.key == current_model_key else " "
                                    lines.append(f"    {marker} {item.model_id} ({item.key})")
                        for provider_id, error in catalog.errors.items():
                            lines.append(f"  [{provider_id}] unavailable: {error}")
                        _send({"type": "tool_result", "name": "model",
                               "output": "\n".join(lines), "error": "", "code": ""})
                    _send({"type": "done"})
                    # update status bar with new model name
                    await _send_model_info()
                elif c == "/mode" or c.startswith("/mode "):
                    argument = c[len("/mode"):].strip().lower()
                    if not argument:
                        output = f"{reasoning_effort_status(agent.llm.config)}\n{MODE_USAGE}"
                        error = ""
                    elif argument in REASONING_EFFORTS:
                        set_reasoning_effort(agent.llm, argument)
                        output = f"{reasoning_effort_status(agent.llm.config)}\nSaved as startup default."
                        error = ""
                        await _send_model_info()
                    else:
                        output = ""
                        error = f"Unknown reasoning effort: {argument}. {MODE_USAGE}"
                    _send({"type": "tool_result", "name": "mode", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c.startswith("/search"):
                    parts = c.split(maxsplit=2)
                    arg = parts[1].lower() if len(parts) > 1 else ""
                    if arg:
                        if arg in SEARCH_PROVIDERS:
                            search_provider_state.set(arg)
                            settings_path = save_selected_search_provider(arg)
                            output = (
                                f"Search provider switched to: {arg}\n"
                                f"Saved as startup default in {settings_path}."
                            )
                            error = ""
                        else:
                            output = ""
                            error = f"Unknown search provider: {arg}. Available: {', '.join(SEARCH_PROVIDERS)}"
                    else:
                        lines = [f"Current search provider: {search_provider_state.provider}", "Available:"]
                        for name in SEARCH_PROVIDERS:
                            marker = " *" if name == search_provider_state.provider else "  "
                            lines.append(f"  {marker} {name}")
                        output, error = "\n".join(lines), ""
                    _send({"type": "tool_result", "name": "search", "output": output, "error": error, "code": ""})
                    _send({"type": "done"})
                elif c.startswith("/persona"):
                    profiles = prompt_profiles()
                    parts = c.split(maxsplit=2)
                    arg = parts[1] if len(parts) > 1 else ""
                    if arg:
                        if arg in profiles:
                            profile = get_prompt_profile(arg)
                            state = profile.default_state()
                            agent.set_persona(state, profile.system_prompt(state))
                            settings_path = save_selected_persona(arg)
                            _send({"type": "tool_result", "name": "persona",
                                   "output": f"Persona switched to: {arg}\n"
                                             f"Saved as startup default in {settings_path}.",
                                   "error": "", "code": ""})
                        else:
                            lines = [f"Unknown persona: {arg}. Available:"]
                            for name, profile in profiles.items():
                                lines.append(f"  {name} — {profile.description}")
                            _send({"type": "tool_result", "name": "persona",
                                   "output": "\n".join(lines), "error": "", "code": ""})
                    else:
                        lines = ["Available personas:"]
                        for name, profile in profiles.items():
                            marker = " *" if name == agent.context.persona_id else "  "
                            lines.append(f"  {marker} {name} — {profile.description}")
                        _send({"type": "tool_result", "name": "persona",
                               "output": "\n".join(lines), "error": "", "code": ""})
                    _send({"type": "done"})
                    await _send_model_info()
                elif c == "/reload" or c.startswith("/reload "):
                    import importlib
                    t0 = time.perf_counter()
                    parts = c.split(maxsplit=1)
                    target = parts[1].strip().lower() if len(parts) > 1 else "all"
                    results: dict[str, str] = {}
                    # --- code: reload core Python modules + swap agent class ---
                    if target in ("all", "code"):
                        agent.end_session("reload")
                        await agent.context.save_async()
                        _reload_targets = [
                            "agent.runtime.tool_failure",
                            "agent.runtime.context_compressor",
                            "agent.runtime.prompts",
                            "agent.runtime.react",
                        ]
                        reloaded: list[str] = []
                        for _mod_name in _reload_targets:
                            _mod = sys.modules.get(_mod_name)
                            if _mod is not None:
                                try:
                                    importlib.reload(_mod)
                                    reloaded.append(_mod_name.rsplit(".", 1)[-1])
                                except Exception as exc:
                                    logger.warning("reload failed for %s: %s", _mod_name, exc)
                        from agent.runtime.react import ReActAgent as _FreshReAct
                        agent.__class__ = _FreshReAct
                        agent.invalidate_tool_schema_cache()
                        agent.begin_session()
                        results["code"] = f"reloaded {', '.join(reloaded) or 'nothing'}"
                    if target in ("all", "persona"):
                        _, new_prompt, new_state = startup_persona(os.getenv("AGENT_PERSONA"))
                        agent.set_persona(new_state, new_prompt)
                        results["persona"] = f"→ {new_state.persona_id}"
                    if target in ("all", "skills"):
                        agent._refresh_skill_catalog(force=True)
                        results["skills"] = f"{len(agent._available_skill_names)} skills"
                    if target in ("all", "model"):
                        new_catalog = configured_model_catalog()
                        new_selected = read_selected_model()
                        new_entry = (
                            new_catalog.resolve_persisted(new_selected)
                            or new_catalog.resolve(os.getenv("LLM_MODEL", ""))
                            or (new_catalog.entries[0] if new_catalog.entries else None)
                        )
                        if new_entry is not None:
                            new_profile = new_entry.profile
                            new_api_key = new_profile.api_key()
                            if not new_api_key and (is_local_url(new_entry.base_url) or not new_profile.api_key_env):
                                new_api_key = "local"
                            if new_api_key:
                                _apply_reloaded_model_profile(
                                    agent,
                                    new_entry,
                                    new_api_key,
                                )
                                catalog = new_catalog
                                current_model_key = new_entry.key
                                results["model"] = f"→ {new_entry.model_id}"
                            else:
                                results["model"] = f"skipped (no API key for {new_entry.model_id})"
                        else:
                            results["model"] = "skipped (no configured model)"
                    if target not in ("all", "code", "persona", "skills", "model"):
                        results["error"] = f"Unknown target: {target}. Available: all, code, persona, skills, model"
                    elapsed = time.perf_counter() - t0
                    lines = [f"/reload {target} ({elapsed:.2f}s):"]
                    for k, v in results.items():
                        lines.append(f"  {k}: {v}")
                    _send({"type": "tool_result", "name": "reload",
                           "output": "\n".join(lines), "error": "", "code": ""})
                    _send({"type": "done"})
                    await _send_model_info()
                elif c.startswith("/session"):
                    sessions = list_sessions()
                    current = agent.context.session_path
                    if c == "/session":
                        lines = [f"Sessions ({len(sessions)}):"]
                        for s in sessions:
                            sp = session_path(s)
                            marker = "*" if str(sp.resolve()) == str(Path(current).resolve()) else " "
                            count = session_msg_count(s)
                            lines.append(f"  {marker} {s} ({count} msgs)")
                        _send({"type": "tool_result", "name": "session",
                               "output": "\n".join(lines), "error": "", "code": ""})
                        _send({"type": "done"})
                    elif c.startswith("/session "):
                        parts = c.split()
                        try:
                            if len(parts) >= 3 and parts[1] == "export":
                                name = parts[2]
                                out = export_session_markdown(name)
                                _send({"type": "tool_result", "name": "session",
                                       "output": f"Exported session '{name}' to {out}", "error": "", "code": ""})
                            elif len(parts) >= 4 and parts[1] == "rename":
                                old_name, new_name = parts[2], parts[3]
                                old_path = session_path(old_name)
                                rename_session(old_name, new_name)
                                memory_store.rename_working(old_name, new_name)
                                new_path = session_path(new_name)
                                if Path(agent.context.session_path).resolve() == old_path.resolve():
                                    agent.context.set_session(str(new_path))
                                _send_session_info()
                                _send_session_list()
                                _send_working_memory()
                                _send({"type": "tool_result", "name": "session",
                                       "output": f"Renamed session '{old_name}' to '{new_name}'", "error": "", "code": ""})
                            elif len(parts) >= 3 and parts[1] == "delete":
                                name = parts[2]
                                if Path(agent.context.session_path).resolve() == session_path(name).resolve():
                                    raise SessionNameError("Switch to another session before deleting the active session.")
                                delete_session(name)
                                memory_store.clear_working(name)
                                _send_session_list()
                                _send_working_memory()
                                _send({"type": "tool_result", "name": "session",
                                       "output": f"Deleted session '{name}'", "error": "", "code": ""})
                            else:
                                name = parts[1]
                                target = session_path(name)
                                await agent.context.save_async()
                                candidate = agent.context.stage_session(str(target))
                                agent.end_session("session_switch")
                                agent.reset_conversation()
                                agent.context.adopt_session(candidate)
                                agent.begin_session()
                                if not session_exists(name):
                                    await agent.context.save_async()
                                await _send_history()
                                await _send_model_info(refresh=True)
                                _send_session_info()
                                _send_session_list()
                                _send_working_memory()
                                _send({"type": "tool_result", "name": "session",
                                       "output": f"Switched to session '{name}'", "error": "", "code": ""})
                        except (SessionNameError, OSError, AppshotMediaError) as e:
                            _send({"type": "tool_result", "name": "session",
                                   "output": "", "error": str(e), "code": ""})
                        _send({"type": "done"})

                elif c.startswith("/conclave"):
                    await _handle_conclave(c, _send, agent)
                    _send({"type": "done"})
                else:
                    _send({"type": "tool_result", "name": "command", "output": "",
                           "error": "Unknown command. Use /help to see available commands.", "code": ""})
                    if active_task is None or active_task.done():
                        _send({"type": "done"})
    finally:
        wakeups.cancel("backend_closed")
        lifecycle_task.cancel()
        with suppress(asyncio.CancelledError):
            await lifecycle_task
        await channel_manager.stop()
        pending_startup = [task for task in [*startup_tasks, *catalog_tasks.values()] if not task.done()]
        for task in pending_startup:
            task.cancel()
        if pending_startup:
            await asyncio.gather(*pending_startup, return_exceptions=True)
        await _cancel_manual_reviews()
        pending_goals = [task for task in goal_tasks if not task.done()]
        for task in pending_goals:
            task.cancel()
        if pending_goals:
            await asyncio.gather(*pending_goals, return_exceptions=True)
        question_broker.close("Backend is shutting down.")
        if active_task is not None and not active_task.done():
            active_task.cancel()
            if active_task_persisted and task_store is not None and active_task_id:
                await durable_io(task_store.request_cancel, active_task_id)
            with suppress(asyncio.CancelledError, Exception):
                await active_task
        pending_resolutions = list(approval_resolutions.values())
        for resolution in pending_resolutions:
            resolution.cancel()
        if pending_resolutions:
            await asyncio.gather(*pending_resolutions, return_exceptions=True)
        await delegate_mailbox.cancel_all()
        try:
            if computer_runtime is not None:
                await computer_runtime.shutdown()
        except Exception as exc:  # noqa: BLE001 - remaining shutdown cleanup must continue
            logger.warning("Computer Use shutdown failed error_type=%s", type(exc).__name__)
        finally:
            try:
                await agent.context.save_async()
            finally:
                agent.end_session("terminal_output_failure" if terminal_output_failed else "shutdown")
            if not bar_mode.active:
                _save_handoff(agent, task_store)
                _run_memory_consolidation(memory_store)
            if browser_backend is not None:
                stop_browser = tools.get("browser_stop")
                if stop_browser is not None:
                    await stop_browser.fn()
            await agent.close_external_memory()
            await mcp_manager.close()
            close = getattr(sandbox, "close", None)
            if close:
                await close()
            _runtime_event_stream = None

    return exit_code


def _write_event(event: dict) -> None:
    payload = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError):
        # Embedded/test streams may not have a descriptor.
        sys.stdout.write(payload)
        sys.stdout.flush()
        return
    # No TextIOWrapper lock is held by the daemon writer when a vanished TUI
    # leaves a blocked pipe behind. Shutdown can time out its join safely.
    data = payload.encode("utf-8")
    while data:
        written = os.write(fd, data)
        if written <= 0:
            raise BrokenPipeError("Backend event pipe made no progress")
        data = data[written:]


async def _start_channel_manager(channel_manager: ChannelManager) -> OSError | None:
    """Start optional channels without making their port a CLI singleton.

    OneBot/NapCat uses a reverse-WebSocket listener, so only one backend can
    own a configured port. A second CLI should still be able to use its own
    session and TUI; only the optional channel adapter is skipped there.
    """
    try:
        await channel_manager.start()
    except OSError as exc:
        if is_address_in_use(exc):
            logger.warning(
                "[backend] QQ channel port is already in use; "
                "this CLI remains available without the QQ channel"
            )
            return exc
        # A broken optional channel must not take down the TUI/backend core.
        print(f"[backend] channel startup failed: {exc}", file=sys.stderr, flush=True)
    except Exception as exc:
        # A broken optional channel must not take down the TUI/backend core.
        print(f"[backend] channel startup failed: {exc}", file=sys.stderr, flush=True)
    return None


def _send(event: dict):
    """Send one enveloped JSON event to the Ink TUI."""
    if _event_writer is not None:
        _event_writer.send(event)
        return
    if _runtime_event_stream is not None:
        try:
            event = _runtime_event_stream.publish(event)
        except Exception:
            logger.exception("runtime event persistence failed type=%s", event.get("type", ""))
    _write_event(event)


def run() -> int:
    """Run one independent backend process."""
    import faulthandler

    # Dump the Python stack on a native crash (segfault/access violation), so
    # an exit-without-traceback can still be located from the backend log.
    try:
        faulthandler.enable()
    except (OSError, ValueError):
        # Pytest and some embedders replace stderr with an object without a
        # Windows file descriptor; crash diagnostics must not block startup.
        pass
    return asyncio.run(main()) or 0


if __name__ == "__main__":
    raise SystemExit(run())
