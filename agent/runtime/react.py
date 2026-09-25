"""ReActAgent — 基于 ReAct 循环的单 Agent"""

from agent.runtime.paths import state_path

from .async_io import durable_io
from .latency import ToolTiming, current_profiler, tool_phase

import asyncio
import base64
import copy
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import sys
import time as _time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..core.agent import AgentBase
from ..core.msg import ContentBlock, Msg
from .code_mode import CODE_MODE_DIRECT_TOOLS
from .coding_contracts import append_check, new_contract
from .context import AgentContext
from .context_compressor import (
    SUMMARY_PREFIX,
    ContextCompressor,
    _is_synthetic_user_turn,
)
from .context_open_history import (
    CONTEXT_OPEN_RECOVERY_HINT,
    has_context_open_placeholder,
    without_context_open_placeholders,
)
from .core_rules import CORE_SKILL_NAME
from .deepseek import DEEPSEEK_VISION_MODELS
from .llm import LLMClient, LLMIdleTimeout, LLMOverallTimeout, LLMResponseError
from .turn_budget import TurnBudgetExceeded, budgeted_events, check_work_budget, parse_turn_budget
from .turn_change_store import (
    TurnChangeStore,
    current_turn_change_store,
    session_history_evidence,
    turn_store_scope,
)
from .message_time import filter_message_time_events
from .metrics import runtime_metrics
from .micro_compact import micro_compact_tool_results
from .process_env import hidden_process_creationflags
from .stream_progress import stream_with_progress
from .project_instructions import ProjectInstructions
from .prompts import DEFAULT_SYSTEM_PROMPT, persona_generation_overrides
from .query_profiler import QueryProfiler
from .tool_failure import ToolFailure
from .tools.registry import ToolRegistry
from .tracing import TraceContext, trace_span
from .vision_policy import VisionPreprocessPolicy
from .vision_preprocessor import VisionPreprocessError, VisionPreprocessor, VisionTileSelectionError

if TYPE_CHECKING:
    from .context_index.broker import ContextIndexBroker
    from .hindsight_provider import HindsightMemoryProvider
    from .learning import LearningReviewer, LearningStore
    from .memory import MemoryStore
    from .memory_retainer import MemoryRetainer
    from .memory_router import MemoryRouter
    from .skills import SkillStore
    from .task_store import TaskStore
    from .tools.delegate import DelegateMailbox

logger = logging.getLogger(__name__)



# Request-local screenshots may be recompressed by the capture tool to stay inside the
# provider request-body budget, so the verified-bytes gate accepts those encodings.
# Tile payloads stay PNG-only and keep their own stricter check.
_REQUEST_LOCAL_IMAGE_DATA_URL_PREFIXES = (
    "data:image/png;base64,",
    "data:image/webp;base64,",
    "data:image/jpeg;base64,",
)


def _is_request_local_image_data_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith(_REQUEST_LOCAL_IMAGE_DATA_URL_PREFIXES)


class ToolArgumentError(ValueError):
    pass


async def _stream_with_generation_stats(events):
    """Report provider-counted output over one successful request's wall time.

    The interval includes connection and first-token waiting, but ends before
    tool execution. Provider completion_tokens already includes reasoning.
    """
    started = _time.perf_counter()
    usage = None
    finished = None
    try:
        async for event in events:
            if event.get("type") in {"done", "tool_calls"}:
                usage = event.get("usage")
                finished = _time.perf_counter()
            yield event
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()
    completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    if (
        isinstance(completion_tokens, bool)
        or not isinstance(completion_tokens, int)
        or completion_tokens <= 0
        or finished is None
    ):
        return
    elapsed = finished - started
    if not math.isfinite(elapsed) or elapsed <= 0:
        return
    tokens_per_second = completion_tokens / elapsed
    if not math.isfinite(tokens_per_second):
        return
    yield {
        "type": "generation_stats",
        "completion_tokens": completion_tokens,
        "elapsed_seconds": elapsed,
        "tokens_per_second": tokens_per_second,
    }


async def _cancel_and_drain_task(task: asyncio.Task[Any]) -> bool:
    """Cancel an owned task and consume its outcome despite repeated cancellation."""
    if not task.done():
        task.cancel()
    drain = asyncio.gather(task, return_exceptions=True)
    interrupted = False
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            interrupted = True
            if not task.done():
                task.cancel()
    return interrupted


class ReActAgent(AgentBase):
    def __init__(self, name: str, llm_client: LLMClient, tool_registry: ToolRegistry,
                 system_prompt: str = DEFAULT_SYSTEM_PROMPT, max_iterations: int = 50,
                 tool_concurrency: int = 4, task_store: "TaskStore | None" = None,
                 memory_store: "MemoryStore | None" = None,
                 memory_router: "MemoryRouter | None" = None,
                 memory_retainer: "MemoryRetainer | None" = None,
                 skill_store: "SkillStore | None" = None,
                 failure_threshold: int | None = None,
                 progressive_tools: bool | None = None,
                 code_mode: str = "native",
                 vision_cache_root: str | Path | None = None,
                 timing_log_enabled: bool = True,
                 minimal_mode: bool = False,
                 query_profile_enabled: bool | None = None,
                 context_index_broker: "ContextIndexBroker | None" = None,
                 turn_timeout_seconds: float | None = None):
        super().__init__(name)
        self.llm = llm_client
        self.tools = tool_registry
        # Code Mode controls tool exposure: "native" hides run_code, "code"
        # exposes run_code plus native approval fallbacks, and "both" exposes
        # everything. Native is the safe default; code/both must be selected
        # explicitly.
        self.code_mode = code_mode if code_mode in {"native", "code", "both"} else "native"
        self.external_memory_provider: "HindsightMemoryProvider | None" = None
        builtin_memory_provider = None
        if memory_store is not None:
            from .memory_provider import BuiltinMemoryProvider

            builtin_memory_provider = BuiltinMemoryProvider(memory_store)
        if memory_router is not None:
            router_provider = getattr(memory_router, "provider", None)
            self.external_memory_provider = (
                router_provider
                if getattr(router_provider, "name", "") == "hindsight"
                else getattr(router_provider, "external", None)
            )
        elif memory_store is not None:
            from .hindsight_provider import HindsightMemoryProvider

            self.external_memory_provider = HindsightMemoryProvider.from_env()
        if self.external_memory_provider is not None and self.tools.get("hindsight_recall") is None:
            from .tools.hindsight import register_hindsight_tools

            register_hindsight_tools(self.tools, self.external_memory_provider)
        self.context = AgentContext(system_prompt=system_prompt)
        self.context.set_system_prompt(system_prompt)
        self.progressive_tools = (
            progressive_tools
            if progressive_tools is not None
            else os.getenv("TOOL_PROGRESSIVE_EXPOSURE", "true").lower() in {"1", "true", "yes", "on"}
        )
        # Prefix-cache economics strongly favor a byte-stable tool manifest.
        # The legacy progressive schema mutation remains an explicit opt-out.
        self.prompt_cache_stable_tools = os.getenv(
            "PROMPT_CACHE_STABLE_TOOLS", "true"
        ).lower() in {"1", "true", "yes", "on"}
        self._stable_tool_schema_key: tuple | None = None
        self._stable_tool_schemas: list[dict] = []
        self.vision_tiles_enabled = True
        self.vision_preprocessor = VisionPreprocessor(
            vision_cache_root or Path.cwd() / ".astra" / "image-cache" / "tiles"
        )
        self._timing_log_enabled = bool(timing_log_enabled)
        self._query_profile_enabled = query_profile_enabled
        self.context_index_broker = context_index_broker
        set_feedback_sink = getattr(context_index_broker, "set_feedback_sink", None)
        if callable(set_feedback_sink):
            set_feedback_sink(
                lambda event: self.context.append_context_index_feedback(event)
            )
        self._context_index_request_id = ""
        self._active_vision_request_id: str | None = None
        self._active_vision_storage_replacements: list[tuple[int, object]] = []
        self._active_vision_prompt_overlays: list[tuple[dict, object, object]] = []
        self._active_vision_attachment_occurrences: set[str] = set()
        self._request_local_image_overlay_messages: list[dict] = []
        self.context.set_tools_token_cost(self._estimate_tools_cost())
        self._tool_cost_revision = self.tools.schema_revision
        self.context.compressor = ContextCompressor(llm_client, hooks=self.tools.hooks)

        def risk_of(name: str) -> str:
            tool = self.tools.get(name)
            return str(getattr(tool, "risk", "read")) if tool is not None else "read"

        def request_local_of(name: str) -> bool:
            tool = self.tools.get(name)
            return bool(tool is not None and getattr(tool, "result_persistence", "durable") == "request_local")

        self.context.tool_risk_provider = risk_of
        self.context.tool_request_local_provider = request_local_of
        # Zero means no fixed iteration ceiling. Other guards still stop exact
        # repeated calls, recurring failures, prompt exhaustion and cancellation.
        self.max_iterations = max(0, max_iterations)
        self.tool_concurrency = max(1, tool_concurrency)
        self.turn_timeout_seconds = parse_turn_budget(
            os.getenv("ASTRA_TURN_TIMEOUT_SECONDS", "0")
            if turn_timeout_seconds is None else turn_timeout_seconds
        )

        # Turn-change ledger (M1): session-owned snapshot area, never part of
        # model requests. Best-effort bookkeeping only; all failures degrade.
        self._turn_change_store: "TurnChangeStore | None" = None
        self._turn_change_store_session: str | None = None
        self._turn_changes_ready: dict | None = None
        self.failure_threshold = max(1, failure_threshold or self._int_env("TOOL_FAILURE_THRESHOLD", 3))
        # Runtime-owned resources are attached by the CLI entrypoints and
        # retained here so cleanup/diagnostics do not rely on dynamic attrs.
        self._sandbox: Any | None = None
        self._mcp_manager: Any | None = None
        self._learning_store: "LearningStore | None" = None
        self._learning_reviewer: "LearningReviewer | None" = None
        self.task_store = task_store
        self.memory_store = memory_store
        if memory_router is not None:
            self.memory_router = memory_router
        elif memory_store is not None and builtin_memory_provider is not None:
            from .memory_provider import FederatedMemoryProvider
            from .memory_router import MemoryRouter

            recall_provider = builtin_memory_provider
            if self.external_memory_provider is not None:
                recall_provider = FederatedMemoryProvider(
                    builtin_memory_provider,
                    self.external_memory_provider,
                )
            self.memory_router = MemoryRouter(
                recall_provider,
                memory_store,
                task_store,
                recall_timeout=self._float_env(
                    "HINDSIGHT_INTERACTIVE_RECALL_TIMEOUT",
                    2.5,
                ),
            )
        else:
            self.memory_router = None
        if memory_retainer is not None:
            self.memory_retainer = memory_retainer
            self.memory_retainer.hooks = self.tools.hooks
        elif memory_store is not None and builtin_memory_provider is not None:
            from .memory_retainer import MemoryRetainer

            self.memory_retainer = MemoryRetainer(
                builtin_memory_provider,
                memory_store,
                hooks=self.tools.hooks,
            )
        else:
            self.memory_retainer = None
        self.skill_store = skill_store
        self._skill_catalog_snapshot = ""
        self._available_skill_names: set[str] = set()
        self._active_skill_names: set[str] = set()
        self._project_instructions: ProjectInstructions | None = None
        self._project_instructions_workdir = ""
        self._task_mutated_paths: set[str] = set()
        self._task_context_paths: set[str] = set()
        self._verification_required = False
        # Minimal Mode suppresses skill/project suffix injection; initialized
        # before _refresh_skill_catalog() runs in __init__.
        self.minimal_mode = bool(minimal_mode)
        # Optional local controllers suppress work-context injection while active.
        self.local_mode = False
        self._refresh_skill_catalog()
        self.tools_enabled = True
        # None exposes the normal routed tool catalog. A non-empty set creates
        # a strict capability sandbox used by modes such as the private bar.
        self.tool_allowlist: set[str] | None = None
        self.runtime_context_provider: Callable[[], str] | None = None
        # Background delegate completions are delivered at safe ReAct
        # boundaries. The concrete mailbox lives with the delegate tools.
        self.delegate_mailbox: "DelegateMailbox | None" = None
        # Local-only frontends attach the process-scoped Computer Use runtime.
        # Remote/API surfaces intentionally leave it as None.
        self._computer_runtime: Any | None = None
        # Some volatile state needs to sit next to the latest user turn to
        # outweigh stale narrative after context compression.  The provider is
        # applied only to the deep-copied LLM prompt, never to saved history.
        self.runtime_turn_context_provider: Callable[[], str] | None = None
        # Interaction modes may override sampling for their own requests
        # without mutating the selected model's normal work configuration.
        self.generation_overrides_provider: Callable[[], dict] | None = None
        # A mode may declare a completed content+tool response terminal after
        # successful execution. Normal work mode leaves this unset and keeps
        # the full ReAct continuation cycle.
        self.finalize_after_tools_provider: Callable[[str, list[dict], list[dict]], bool] | None = None
        # Interaction modes may require one specific atomic tool on every
        # model turn. Normal work mode leaves this unset and keeps tool_choice
        # on auto.
        self.forced_tool_name: str | None = None
        self._fresh_tool_context: dict[str, str] = {}
        self._request_local_tool_context_ids: set[str] = set()
        self._steering: list[str] = []
        self._steering_seen: set[str] = set()  # texts already queued this task
        self._memory_turn_key = ""
        self._turn_context_key = ""
        self._turn_context_block = ""
        self._last_ended_session: str | None = None
        append_approval_audit = self.context.append_approval_audit

        async def record_approval_audit(event: dict) -> None:
            await durable_io(append_approval_audit, copy.deepcopy(event))

        self.tools.set_approval_audit_handler(record_approval_audit)
        # External memory is best-effort bookkeeping. A failed retain must not
        # be retried on every subsequent user turn: Hindsight already retries
        # LLM calls internally, so doing that here creates a multiplicative
        # retry/cost loop while the durable cursor remains unchanged.
        self._external_sync_failures = 0
        self._external_sync_retry_at = 0.0

    def end_session(self, reason: str = "shutdown") -> None:
        """Dispatch session lifecycle hooks once for the current session."""
        session_id = Path(self.context.session_path).stem if self.context.session_path else "default"
        if session_id == self._last_ended_session:
            return
        self._last_ended_session = session_id
        context_index_broker = getattr(self, "context_index_broker", None)
        if context_index_broker is not None:
            context_index_broker.end_session()
        record_end = getattr(self.context, "record_session_end", None)
        if callable(record_end):
            record_end(reason)
        self.tools.hooks.dispatch_session_end(session_id, reason)
        self._close_turn_change_store()

    async def sync_external_session(
        self,
        *,
        force: bool = False,
        reason: str = "turn",
    ) -> dict[str, Any]:
        """Deliver new canonical session messages without failing the user turn."""
        provider = self.external_memory_provider
        session_path = self.context.session_path
        if provider is None or not session_path:
            return {"decision": "unavailable", "synced_messages": 0}
        now = _time.monotonic()
        if now < self._external_sync_retry_at:
            retry_after = max(1, int(self._external_sync_retry_at - now + 0.999))
            provider.last_sync_detail = (
                f"cooldown after {self._external_sync_failures} failure(s); "
                f"retry in {retry_after}s"
            )
            return {
                "decision": "cooldown",
                "synced_messages": 0,
                "failures": self._external_sync_failures,
                "retry_after_seconds": retry_after,
            }
        try:
            outcome = await provider.sync_session(
                session_path,
                list(self.context.messages),
                force=force,
                reason=reason,
            )
            self._external_sync_failures = 0
            self._external_sync_retry_at = 0.0
            return outcome
        except Exception as exc:
            self._external_sync_failures += 1
            try:
                base_cooldown = max(
                    1.0,
                    float(os.getenv("HINDSIGHT_SESSION_SYNC_FAILURE_COOLDOWN", "300")),
                )
            except ValueError:
                base_cooldown = 300.0
            try:
                max_cooldown = max(
                    base_cooldown,
                    float(os.getenv("HINDSIGHT_SESSION_SYNC_MAX_COOLDOWN", "21600")),
                )
            except ValueError:
                max_cooldown = max(base_cooldown, 21600.0)
            error_text = f"{type(exc).__name__}: {exc}"
            cooldown = min(
                max_cooldown,
                base_cooldown * (3 ** (self._external_sync_failures - 1)),
            )
            if re.search(r"(?:AuthenticationError|invalid api key|\b401\b)", error_text, re.IGNORECASE):
                cooldown = max_cooldown
            self._external_sync_retry_at = _time.monotonic() + cooldown
            provider.last_sync_detail = (
                f"{error_text}; retry cooldown={int(cooldown)}s; "
                f"failures={self._external_sync_failures}"
            )
            self.context.append_diagnostic({
                "type": "hindsight_session_sync_failed",
                "at": datetime.now(timezone.utc).isoformat(),
                "session_id": Path(session_path).stem,
                "reason": reason,
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
                "retry_after_seconds": int(cooldown),
            })
            logger.warning(
                "Hindsight session sync failed session=%s cooldown=%ss failures=%s: %s",
                Path(session_path).stem,
                int(cooldown),
                self._external_sync_failures,
                exc,
            )
            return {
                "decision": "failed",
                "synced_messages": 0,
                "error": error_text,
                "retry_after_seconds": int(cooldown),
            }

    async def close_external_memory(self) -> None:
        provider = self.external_memory_provider
        if provider is not None:
            await self.sync_external_session(force=True, reason="shutdown")
            await provider.aclose()

    def close_external_memory_nowait(self) -> None:
        provider = self.external_memory_provider
        if provider is not None:
            provider.close_nowait()

    def begin_session(self) -> None:
        """Start a fresh lifecycle even when reset reuses the same session path."""
        self._last_ended_session = None
        record_start = getattr(self.context, "record_session_start", None)
        if callable(record_start):
            record_start()
        if hasattr(self, "_task_mutated_paths"):
            self._task_mutated_paths.clear()
        else:
            self._task_mutated_paths = set()
        self._verification_required = False
        if hasattr(self, "skill_store") and hasattr(self.context, "set_stable_system_suffix"):
            self._refresh_skill_catalog()

    def queue_steering(self, text: str) -> None:
        """Inject a steering message into the running ReAct loop.

        Picked up at the start of the next iteration as a transient system
        instruction (never persisted to conversation history).  Thread-safe:
        ``list.append`` is atomic under CPython's GIL, and the loop drains
        with ``list()`` + ``clear()`` on the single event-loop thread.

        Duplicate suppression: identical text is dropped for the entire
        task lifetime (guards against TUI re-render / terminal echo
        re-sending the same steering text across multiple iterations).
        The seen-set is cleared when the task ends (reset_conversation).
        """
        if text in self._steering_seen:
            return
        self._steering_seen.add(text)
        self._steering.append(text)

    def _refresh_skill_catalog(self, *, force: bool = False) -> bool:
        """Refresh skill metadata and rebuild the stable system suffix."""
        if self.minimal_mode or self.local_mode:
            # Minimal and local modes are zero-injection presets: skills and
            # project instructions must never leak into their sessions.
            return False
        if self.skill_store is None:
            block = ""
            names: set[str] = set()
        else:
            items = self.skill_store.list()
            if not isinstance(items, (list, tuple)):
                items = []
            names = {
                str(item.get("name") or "").strip().lower()
                for item in items
                if isinstance(item, dict)
            }
            raw_block = self.skill_store.catalog_prompt()
            block = raw_block if isinstance(raw_block, str) else ""
        changed = block != self._skill_catalog_snapshot
        self._available_skill_names = {name for name in names if name}
        if changed or force:
            self._skill_catalog_snapshot = block
            project_block = (
                self._project_instructions.base_prompt
                if self._project_instructions is not None
                else ""
            )
            self.context.set_stable_system_suffix(
                "\n\n".join(part for part in (project_block, block) if part)
            )
        return changed

    def _refresh_project_instructions(self, *, force: bool = False) -> bool:
        """Reload AGENTS.md/rules and rebuild the stable system suffix."""
        if self.minimal_mode or self.local_mode:
            # Minimal and local modes are zero-injection presets; project
            # instructions must never leak into their sessions even on forced
            # refreshes.
            return False
        workdir = str(self._source_tracking_workdir())
        changed_root = workdir != self._project_instructions_workdir
        if self._project_instructions is None or changed_root:
            self._project_instructions = ProjectInstructions(workdir)
            self._project_instructions_workdir = workdir
        elif force:
            self._project_instructions.reload()
        project_block = self._project_instructions.base_prompt
        suffix = "\n\n".join(
            part for part in (project_block, self._skill_catalog_snapshot) if part
        )
        changed = suffix != self.context._stable_system_suffix
        if changed:
            self.context.set_stable_system_suffix(suffix)
        return changed or changed_root

    def _active_skill_contract(self) -> str:
        if self.skill_store is None or not self._active_skill_names:
            return ""
        blocks: list[str] = []
        remaining = self._int_env("ACTIVE_SKILL_CONTRACT_CHARS", 24_000)
        for name in sorted(self._active_skill_names):
            if name == CORE_SKILL_NAME:
                # Core rules belong only in model-requested tool results.
                # Duplicating them in snapshots adds avoidable context and
                # hides whether the model actually remembered to read them.
                continue
            try:
                content = self.skill_store.view(name)
            except (OSError, ValueError):
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            body = content[:remaining]
            blocks.append(
                f"## Active skill contract: {name} (sha256={digest})\n{body}"
            )
            remaining -= len(body)
            if remaining <= 0:
                break
        if not blocks:
            return ""
        return (
            "[ACTIVE SKILLS — current files override compacted historical descriptions]\n"
            + "\n\n".join(blocks)
            + "\n[END ACTIVE SKILLS]"
        )


    @staticmethod
    def _normalize_workflow_path(value: str) -> str:
        normalized = str(value or "").strip().strip("\"'").replace("\\", "/").lower()
        marker = "/agent-system/"
        if marker in normalized:
            normalized = normalized.split(marker, 1)[1]
        return normalized.lstrip("./")

    def _tool_mutation_paths(self, name: str, args: dict) -> set[str]:
        paths: set[str] = set()
        for key in ("path", "file_path"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                paths.add(self._normalize_workflow_path(value))
        if name == "apply_patch":
            patch = str(args.get("patch") or args.get("input") or "")
            for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+)$", patch, re.MULTILINE):
                paths.add(self._normalize_workflow_path(match.group(1)))
        return {path for path in paths if path}

    def _tool_raw_paths(self, name: str, args: dict) -> set[str]:
        """Path arguments exactly as the tool received them (no normalization).

        The ledger resolves these through the file policy to the capture-chain
        identity, so case, absolute roots and hidden-file prefixes must survive
        (M2 P2 follow-up); workflow classification keeps the normalized variant
        from ``_tool_mutation_paths``.
        """
        paths: set[str] = set()
        for key in ("path", "file_path"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                paths.add(value.strip())
        if name == "apply_patch":
            patch = str(args.get("patch") or args.get("input") or "")
            for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+)$", patch, re.MULTILINE):
                paths.add(match.group(1).strip())
        return {path for path in paths if path}

    def _ledger_identity_paths(self, paths: set[str]) -> set[str]:
        """Resolve raw tool-reported paths to the capture-chain identity.

        The ledger resolves relative candidate paths against its sandbox
        workspace while the file tools resolve against their filesystem-policy
        root; when a supported configuration separates the two roots, the raw
        relative path registered a second (phantom) entry for the file the
        capture chain had already confirmed (M2 review P2). Resolve through the
        registered policy so both chains share one stable identity; paths the
        policy cannot resolve keep their previous treatment.
        """
        policy = getattr(self.tools, "filesystem_policy", None)
        if policy is None:
            return paths
        resolved: set[str] = set()
        for path in paths:
            try:
                resolved.add(str(policy.resolve(path)))
            except (OSError, ValueError):
                resolved.add(path)
        return resolved

    def _is_source_path(
        self,
        path: str,
        _pattern=re.compile(r"(?:agent/|ui-tui/src/|tests/|scripts/|\.py\b|\.tsx?\b|\.jsx?\b)"),
    ) -> bool:
        """True when a mutated path is source/code that warrants post-write verification."""
        return bool(_pattern.search(path.replace("\\", "/").lower()))

    @staticmethod
    def _strip_heredocs(text: str) -> str:
        # Remove <<'DELIM' ... DELIM blocks so their bodies (e.g. Python
        # comparison operators like `x > 3000`) are not mistaken for shell
        # redirections by the source-mutation guard.
        pattern = re.compile(
            r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\r?\n.*?^\s*\1\s*$",
            re.MULTILINE | re.DOTALL,
        )
        return pattern.sub("", text)

    @staticmethod
    def _quoted_or_bare_value(match: re.Match) -> str:
        return next((value for value in match.groups() if value is not None), "").strip()

    def _source_mutation_intent(self, name: str, args: dict) -> dict[str, Any]:
        """Classify source mutation as blocked, tracked, or ordinary execution."""
        if name not in {"execute_shell", "execute_python"}:
            return {"mode": "allow", "reason": "", "targets": ()}
        payload = str(args.get("command") or args.get("code") or "")
        lower = payload.lower().replace("\\", "/")
        targets: set[str] = set()
        reasons: set[str] = set()

        # Python write APIs are dangerous only when their destination is
        # source. Reading a .py file while writing an unrelated report stays allowed.
        for match in re.finditer(
            r"\bopen\s*\(\s*(?:\"([^\"]+)\"|'([^']+)')\s*,\s*"
            r"(?:\"([^\"]*[wax+][^\"]*)\"|'([^']*[wax+][^']*)')",
            lower,
        ):
            target = match.group(1) or match.group(2) or ""
            if self._is_source_path(target):
                targets.add(target)
                reasons.add("Python open(..., write-mode)")
        for match in re.finditer(
            r"(?:pathlib\.)?path\s*\(\s*(?:\"([^\"]+)\"|'([^']+)')\s*\)"
            r"\s*\.write_(?:text|bytes)\s*\(",
            lower,
        ):
            target = match.group(1) or match.group(2) or ""
            if self._is_source_path(target):
                targets.add(target)
                reasons.add("Path.write_text/write_bytes")

        if name == "execute_python":
            has_dynamic_write = bool(re.search(
                r"\bopen\s*\([^)]*,\s*[\"'][^\"']*[wax+]|\.write_(?:text|bytes)\s*\(",
                lower,
            ))
            if not targets and has_dynamic_write and self._is_source_path(lower):
                targets.add("<dynamic path referencing source code>")
                reasons.add("dynamic Python file-write API")
        else:
            stripped = self._strip_heredocs(lower)
            value_pattern = r'(?:"([^\"]+)"|\'([^\']+)\'|([^\s|&;]+))'

            # Inspect the redirection destination itself. A source file in the
            # read side of `pytest x.py > report.txt` is not a mutation target.
            for match in re.finditer(r">{1,2}(?!=)\s*" + value_pattern, stripped):
                target = self._quoted_or_bare_value(match)
                if target not in {"/dev/null", "nul"} and self._is_source_path(target):
                    targets.add(target)
                    reasons.add("shell output redirection")

            for command_match in re.finditer(
                r"\b(set-content|add-content|out-file)\b([^;&|\r\n]*)",
                stripped,
            ):
                command, tail = command_match.groups()
                path_match = re.search(
                    r"-(?:literalpath|filepath|path)\s+" + value_pattern,
                    tail,
                )
                if path_match is None:
                    path_match = re.search(r"^\s*" + value_pattern, tail)
                if path_match is not None:
                    target = self._quoted_or_bare_value(path_match)
                    if self._is_source_path(target):
                        targets.add(target)
                        reasons.add(f"PowerShell {command}")

            if re.search(r"\bsed\s+[^;&|\r\n]*\s-i(?:\s|$)", stripped):
                sed_targets = {
                    token.strip("\"'(),")
                    for token in re.findall(r"[^\s;&|]+", stripped)
                    if self._is_source_path(token.strip("\"'(),"))
                }
                if sed_targets:
                    targets.update(sed_targets)
                    reasons.add("sed in-place edit")
            if re.search(r"\bgit\s+apply\b", stripped):
                targets.add("<paths selected by git apply>")
                reasons.add("git apply bypasses the file edit journal")

        if targets:
            return {
                "mode": "block",
                "reason": ", ".join(sorted(reasons)),
                "targets": tuple(sorted(targets)),
            }

        if name == "execute_shell" and self._tracked_source_command(lower):
            return {
                "mode": "track",
                "reason": "approved formatter or code generator",
                "targets": (),
            }
        return {"mode": "allow", "reason": "", "targets": ()}

    @staticmethod
    def _tracked_source_command(command: str) -> bool:
        patterns = (
            r"\b(?:ruff\s+format|black|isort|cargo\s+fmt|rustfmt)\b",
            r"\b(?:prettier|eslint|biome)\b[^;&|\r\n]*(?:--write|--fix)\b",
            r"\b(?:clang-format|gofmt)\b[^;&|\r\n]*(?:\s-i\b|\s-w\b)",
            r"\b(?:npm|pnpm|yarn)(?:\s+run)?\s+(?:format|fmt|lint:fix|generate|codegen)\b",
            r"\bpython(?:\d+(?:\.\d+)?)?\s+[^;&|\r\n]*(?:generate|codegen)[\w.-]*\.py\b",
        )
        return any(re.search(pattern, command) for pattern in patterns)

    def _source_tracking_workdir(self) -> Path:
        sandbox = self._sandbox
        current = getattr(sandbox, "current", None)
        if callable(current):
            sandbox = current()
        return Path(getattr(sandbox, "workdir", Path.cwd())).resolve()

    async def _git_worktree_snapshot(self) -> dict[str, str] | None:
        """Fingerprint every currently changed Git path without touching the index."""
        root = self._source_tracking_workdir()
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=hidden_process_creationflags(),
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            logger.warning("source mutation tracking could not inspect Git status", exc_info=True)
            return None
        if process.returncode != 0:
            return None

        records = stdout.split(b"\0")
        paths: set[str] = set()
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4:
                continue
            status = record[:2].decode("ascii", errors="replace")
            paths.add(record[3:].decode("utf-8", errors="surrogateescape"))
            if "R" in status or "C" in status:
                if index < len(records) and records[index]:
                    paths.add(records[index].decode("utf-8", errors="surrogateescape"))
                index += 1

        snapshot: dict[str, str] = {}
        for relative in paths:
            target = root / relative
            try:
                if target.is_symlink():
                    fingerprint = f"symlink:{os.readlink(target)}"
                elif target.is_file():
                    hasher = hashlib.sha256()
                    with target.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    fingerprint = f"file:{hasher.hexdigest()}"
                else:
                    fingerprint = "missing"
            except OSError as exc:
                fingerprint = f"error:{type(exc).__name__}"
            snapshot[self._normalize_workflow_path(relative)] = fingerprint
        return snapshot

    @staticmethod
    def _changed_snapshot_paths(
        before: dict[str, str],
        after: dict[str, str],
    ) -> set[str]:
        return {
            path
            for path in before.keys() | after.keys()
            if before.get(path, "<clean>") != after.get(path, "<clean>")
        }










    def _source_mutation_failure(self, name: str, args: dict, tool) -> dict | None:
        """Keep source changes on version-aware tools without enforcing a workflow."""
        intent = self._source_mutation_intent(name, args)
        if (
            tool is None
            or tool.group not in {"code", "files", "git"}
            or tool.risk not in {"write", "execute"}
            or intent["mode"] != "block"
        ):
            return None
        targets = ", ".join(intent["targets"])
        message = (
            f"[SourceMutationBlocked] `{name}` was blocked: {intent['reason']}. "
            f"Mutation target(s): {targets}. Use read_file plus edit_file/apply_patch "
            "so existing content and concurrent changes are checked."
        )
        return {
            "type": "tool_result",
            "id": "",
            "name": name,
            "args": args,
            "output": "",
            "error": message,
            "code": "source_mutation_requires_file_tool",
            "tool_output": message,
            "duration_ms": 0,
            "error_type": "source_mutation_requires_file_tool",
            "recoverable": True,
            "retryable": True,
            "recovery_hint": (
                f"Read the current version of {targets}, then use edit_file for an exact "
                "replacement or apply_patch for a multi-hunk change."
            ),
            "details": {
                "reason": intent["reason"],
                "targets": list(intent["targets"]),
            },
        }

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    @staticmethod
    def _float_env(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except ValueError:
            return default

    def _estimate_tools_cost(self) -> int:
        """Cache the schemas actually exposed before per-turn routing."""
        try:
            if getattr(self, "local_mode", False):
                # An isolated local mode exposes no tools at all, so no tool schema is
                # charged to its prompt.
                return 0
            from .token_estimator import estimate_value_tokens
            allowlist = getattr(self, "tool_allowlist", None)
            if allowlist is not None and getattr(self, "minimal_mode", False):
                # Minimal exposes a fixed catalog. Count that catalog,
                # including Code Mode's nested PTC description, instead of
                # charging the context for every registered tool.
                schemas = self.tools.to_openai_tools(names=allowlist)
                return estimate_value_tokens(self._apply_code_mode(schemas))
            if self.prompt_cache_stable_tools or not self.progressive_tools:
                return estimate_value_tokens(self.tools.to_openai_tools())
            schemas = self.tools.to_openai_tools(groups={"core"})
            activation = self.tools.activation_tool_schema()
            if activation is not None:
                schemas.append(activation)
            return estimate_value_tokens(schemas)
        except Exception:
            return 0

    def refresh_tool_token_cost(self) -> None:
        """Synchronize prompt accounting after dynamic registry changes."""
        revision = self.tools.schema_revision
        if revision == self._tool_cost_revision:
            return
        self.context.set_tools_token_cost(self._estimate_tools_cost())
        self._tool_cost_revision = revision

    def invalidate_tool_schema_cache(self) -> None:
        """Force the next turn to rebuild the model-visible tool schemas."""
        self._stable_tool_schema_key = None
        self._stable_tool_schemas = []
        # Reloading code can change schema accounting without changing the
        # registry revision, so refresh the prompt cost explicitly as well.
        self._tool_cost_revision = -1
        self.refresh_tool_token_cost()

    def set_vision_tiles_enabled(self, enabled: bool) -> None:
        """Apply the tiling preference to the next vision request."""
        self.vision_tiles_enabled = bool(enabled)
        self.invalidate_tool_schema_cache()

    def select_vision_tiles(self, tile_set_id: str, tile_ids: list[str]) -> dict:
        """Resolve bounded detail tiles for the currently active request."""
        request_id = self._active_vision_request_id
        if request_id is None:
            raise VisionTileSelectionError(
                "Vision tile selection is unavailable outside an active request."
            )
        selection = self.vision_preprocessor.select_tiles(
            tile_set_id,
            tile_ids,
            session_id=self._vision_session_id(),
            request_id=request_id,
        )
        return {
            "data_urls": list(selection.data_urls),
            "labels": list(selection.labels),
            "unserved_ids": list(selection.unserved_ids),
            "remaining_images": selection.remaining_images,
            "remaining_inline_bytes": selection.remaining_inline_bytes,
        }

    def _vision_tile_tool_enabled(self) -> bool:
        """Return whether the current model and preference authorize tiling."""
        config = getattr(self.llm, "config", None)
        policy = getattr(config, "vision_preprocess", None)
        capabilities = getattr(config, "capabilities", frozenset())
        if (
            not self.vision_tiles_enabled
            or getattr(config, "model", "") not in DEEPSEEK_VISION_MODELS
            or "vision" not in capabilities
            or not isinstance(policy, VisionPreprocessPolicy)
        ):
            return False
        integer_fields = (
            policy.tile_width,
            policy.tile_height,
            policy.overlap,
            policy.max_images_per_request,
            policy.max_inline_body_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_fields):
            return False
        return bool(
            policy.strategy == "tiled"
            and policy.tile_width > 0
            and policy.tile_height > 0
            and 0 <= policy.overlap < min(policy.tile_width, policy.tile_height)
            and policy.max_images_per_request > 0
            and policy.max_inline_body_bytes > 0
            and policy.overflow_strategy == "model_select"
        )

    def _filter_vision_tile_schema(self, schemas: list[dict]) -> list[dict]:
        if self._vision_tile_tool_enabled():
            return schemas
        return [
            schema
            for schema in schemas
            if schema.get("function", {}).get("name") != "read_image_tiles"
        ]

    def _vision_session_id(self) -> str:
        if not self.context.session_path:
            return "default"
        return Path(self.context.session_path).stem or "default"

    def _vision_prompt_content_overrides(self) -> dict[int, object]:
        """Resolve active provider-only image content onto canonical messages."""
        overrides: dict[int, object] = {}
        used_indexes: set[int] = set()
        for original_message, storage_content, chat_content in self._active_vision_prompt_overlays:
            match = next(
                (
                    index
                    for index, message in enumerate(self.context.messages)
                    if index not in used_indexes and message is original_message
                ),
                None,
            )
            if match is None:
                provenance = original_message.get("provenance", "")
                match = next(
                    (
                        index
                        for index in range(len(self.context.messages) - 1, -1, -1)
                        if index not in used_indexes
                        and self.context.messages[index].get("role") == "user"
                        and self.context.messages[index].get("provenance", "") == provenance
                        and self.context.messages[index].get("content") == storage_content
                    ),
                    None,
                )
            if match is not None:
                overrides[match] = chat_content
                used_indexes.add(match)
        return overrides

    @staticmethod
    def _vision_request_id(msg: Msg) -> str:
        request_id = str(msg.metadata.get("request_id") or msg.id or "").strip()
        if request_id:
            return request_id
        request_id = uuid.uuid4().hex
        msg.metadata["request_id"] = request_id
        return request_id

    def _available_tool_schemas(
        self,
        user_text: str,
        active_groups: set[str],
        tool_name_counts: dict[str, int],
        blocked_tools: set[str],
    ) -> list[dict]:
        if not self.tools_enabled:
            return []
        if self.prompt_cache_stable_tools and (
            self.tool_allowlist is None or self.minimal_mode
        ):
            allowlist = (
                tuple(sorted(self.tool_allowlist))
                if self.tool_allowlist is not None
                else None
            )
            key = (
                allowlist,
                tuple(self.tools.tool_names),
                self.tools.schema_revision,
                self.code_mode,
                self._vision_tile_tool_enabled(),
            )
            if key != self._stable_tool_schema_key:
                if self.tool_allowlist is not None:
                    schemas = self.tools.to_openai_tools(names=self.tool_allowlist)
                else:
                    schemas = self.tools.to_openai_tools()
                schemas = self._filter_vision_tile_schema(schemas)
                self._stable_tool_schema_key = key
                self._stable_tool_schemas = copy.deepcopy(self._apply_code_mode(schemas))
            # Availability, call budgets and circuit breakers are enforced by
            # the execution layer. Removing schemas mid-loop destroys the
            # provider's cached prefix without adding safety.
            return copy.deepcopy(self._stable_tool_schemas)
        if self.tool_allowlist is not None:
            schemas = self.tools.to_openai_tools(names=self.tool_allowlist)
        elif self.progressive_tools:
            active_groups.update(self.tools.select_groups(user_text))
            schemas = self.tools.to_openai_tools(groups=active_groups)
            # Code Mode keeps a native escape hatch for approval-gated calls:
            # the PTC subprocess pauses on approval, but a denied, cancelled,
            # or expired decision still needs a native retry route.
            # Progressive routing may omit the relevant group (for example, a
            # short follow-up such as "retry"), so source these fallback
            # schemas from the complete registry rather than only from the
            # routed groups.
            schemas = self._append_code_mode_fallbacks(schemas)
        else:
            schemas = self.tools.to_openai_tools()
        schemas = self._apply_code_mode(schemas)
        schemas = self._filter_vision_tile_schema(schemas)

        available = []
        for schema in schemas:
            schema_name = schema.get("function", {}).get("name", "")
            if schema_name in blocked_tools:
                continue
            tool_def = self.tools.get(schema_name)
            limit = tool_def.max_calls_per_turn if tool_def is not None else None
            if limit is not None and tool_name_counts.get(schema_name, 0) >= limit:
                continue
            available.append(schema)

        if self.tool_allowlist is None and self.progressive_tools:
            activation = self.tools.activation_tool_schema()
            if activation is not None:
                available.append(activation)
        return available

    def _append_code_mode_fallbacks(self, schemas: list[dict]) -> list[dict]:
        """Add direct interaction tools and fallbacks omitted by routing.

        The normal progressive catalog is intentionally narrow. ``run_code``
        pauses on interactive approval now, but a denied, cancelled, or
        expired decision still needs a native retry route, and progressive
        routing may omit the relevant group (for example, a short follow-up
        such as "retry"), so source these fallback schemas from the complete
        registry rather than only from the routed groups. A strict allowlist
        remains authoritative and is never widened here.
        """
        if self.code_mode != "code" or self.tool_allowlist is not None:
            return schemas

        existing = {
            str(schema.get("function", {}).get("name", ""))
            for schema in schemas
        }
        candidates = self.tools.to_openai_tools()
        fallbacks = []
        for schema in candidates:
            name = str(schema.get("function", {}).get("name", ""))
            if name in existing:
                continue
            if (
                name == "run_code"
                or self._is_code_mode_direct_tool(name)
                or self._is_approval_fallback(name)
            ):
                fallbacks.append(schema)
                existing.add(name)
        return [*schemas, *fallbacks]

    def _apply_code_mode(self, schemas: list[dict]) -> list[dict]:
        """Filter tool schemas by Code Mode.

        ``native`` hides ``run_code``; ``code`` exposes ``run_code`` plus
        native tools that may need an interactive approval, and injects the
        tool-name list into its ``code`` parameter description so the program
        knows which bindings exist; ``both`` passes everything through
        unchanged.
        """

        def schema_name(schema: dict) -> str:
            return str(schema.get("function", {}).get("name", ""))

        if self.code_mode == "both":
            # Minimal Mode deliberately exposes direct native tools alongside
            # PTC.  Keep the strict allowlist visible inside run_code too, so
            # the model does not have to infer the nested namespace from the
            # surrounding tool catalog.
            if self.tool_allowlist is None:
                return schemas
            tool_names = [
                name for name in self.tools.tool_names
                if name in self.tool_allowlist
                and name != "run_code"
                and not self._is_code_mode_direct_tool(name)
            ]
            listing = ", ".join(tool_names)
            prepared = []
            for schema in schemas:
                if schema_name(schema) != "run_code":
                    prepared.append(schema)
                    continue
                item = copy.deepcopy(schema)
                properties = item.get("function", {}).get("parameters", {}).get("properties", {})
                code_param = properties.get("code") if isinstance(properties, dict) else None
                if isinstance(code_param, dict) and listing:
                    existing = code_param.get("description", "")
                    if "Available tools (call as tools.<name>(...))" not in existing:
                        code_param["description"] = (
                            f"{existing}\n\nAvailable tools (call as tools.<name>(...)): {listing}"
                        )
                prepared.append(item)
            return prepared
        if self.code_mode == "native":
            return [schema for schema in schemas if schema_name(schema) != "run_code"]

        run_code = [schema for schema in schemas if schema_name(schema) == "run_code"]
        if not run_code:
            return []
        direct_tools = [
            schema
            for schema in schemas
            if self._is_code_mode_direct_tool(schema_name(schema))
        ]
        approval_fallbacks = [
            schema
            for schema in schemas
            if schema_name(schema) != "run_code"
            and not self._is_code_mode_direct_tool(schema_name(schema))
            and self._is_approval_fallback(schema_name(schema))
        ]
        # The registry also contains mode-private tools (for example the
        # Night Bar tools).  They are intentionally registered globally so a
        # mode can activate them, but their names must not leak into the
        # normal PTC contract: the subprocess applies the same availability
        # gate before dispatching a call.  Keep this list aligned with that
        # gate instead of exposing every registered name to the model.
        if self.tool_allowlist is not None:
            tool_names = [
                name for name in self.tools.tool_names
                if name in self.tool_allowlist
                and name != "run_code"
                and not self._is_code_mode_direct_tool(name)
            ]
        else:
            tool_names = [
                name for name in self.tools.tool_names
                if name != "run_code"
                and not self._is_code_mode_direct_tool(name)
                and (tool := self.tools.get(name)) is not None
                and tool.expose_by_default
            ]
        prepared = copy.deepcopy(run_code[0])
        properties = prepared.get("function", {}).get("parameters", {}).get("properties", {})
        code_param = properties.get("code") if isinstance(properties, dict) else None
        if isinstance(code_param, dict):
            listing = ", ".join(tool_names)
            existing = code_param.get("description", "")
            code_param["description"] = (
                f"{existing}\n\nAvailable tools (call as tools.<name>(...)): {listing}"
            )
        return [prepared, *direct_tools, *approval_fallbacks]

    def _is_code_mode_direct_tool(self, name: str) -> bool:
        """Return whether a tool must remain a first-class model call."""
        if name in CODE_MODE_DIRECT_TOOLS:
            return True
        tool = self.tools.get(name)
        return tool is not None and tool.result_persistence == "request_local"

    def _is_approval_fallback(self, name: str) -> bool:
        """Keep native escape hatches visible for approval-gated tools.

        A ``run_code`` subprocess pauses on interactive approval and fails
        closed on a denied, cancelled, or expired decision. In ``code`` mode
        the model still needs a native route for that exact call so it can
        retry after a rejection. Permission hooks are included because
        filesystem tools use dynamic path approval while keeping their base
        ``approval`` as ``never``.
        """
        tool = self.tools.get(name)
        return tool is not None and (
            tool.approval != "never" or tool.permission_check is not None
        )

    def _generation_overrides(self) -> dict | None:
        if self.generation_overrides_provider is None:
            # No interaction mode is engaged: apply persona-declared sampling
            # (e.g. a profile's declared temperature) when the active
            # persona profile declares one. Modes still win by swapping the
            # provider, which they save and restore around their session.
            return persona_generation_overrides(
                getattr(self.context, "persona_id", "") or None
            )
        overrides = self.generation_overrides_provider()
        return dict(overrides) if overrides else None

    def _llm_stream(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str | dict | None = None,
        omit_tool_choice: bool = False,
        profiler: QueryProfiler | None = None,
    ):
        from .system_prompt_projection import system_prompt_series
        check_work_budget()
        original = messages
        series = system_prompt_series(getattr(self.llm, "config", None), tools)
        projection = getattr(self.context, "system_projection", None)
        if projection is not None:
            messages = projection.project(messages, series)
        # Prefix reuse is optional; never crowd out the answer or defeat the
        # compaction/admission budget just to retain superseded system text.
        if projection is not None and messages != original:
            estimator = getattr(self.llm, "estimate_tokens", None)
            if estimator is not None:
                projected_tokens = estimator(messages)
                if projected_tokens >= self.context.max_prompt_tokens:
                    projection.reset()
                    messages = original
                else:
                    self.context.last_prompt_tokens = projected_tokens
        if self._vision_tile_tool_enabled():
            policy = getattr(self.llm.config, "vision_preprocess", None)
            assert isinstance(policy, VisionPreprocessPolicy)
            self.vision_preprocessor.validate_provider_prompt(messages, policy)
        kwargs: dict[str, Any] = {"messages": messages, "tools": tools}
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if omit_tool_choice:
            kwargs["omit_tool_choice"] = True
        overrides = self._generation_overrides()
        if overrides:
            kwargs["generation_overrides"] = overrides
        if any(
            isinstance(message.get("content"), list) and any(
                isinstance(part, dict) and part.get("type") == "appshot_image"
                for part in message["content"]
            ) for message in self.context.messages
        ):
            from agent.cli.appshot_admission import appshot_prompt_limit, request_token_bound
            from agent.cli.appshots import AppshotValidationError
            from agent.runtime.llm import _messages_for_capabilities
            if "vision" not in self.llm.config.capabilities:
                # Historical Appshots must not poison ordinary follow-ups after
                # a model switch. Preserve AX text and the adapter's explicit
                # image-unavailable notice; keep owned pixels in session storage.
                # Fresh Appshot admission still requires vision capability.
                messages = _messages_for_capabilities(
                    messages, self.llm.config.capabilities,
                    frozenset(tool.get("function", {}).get("name", "") for tool in tools),
                )
                kwargs["messages"] = messages
            limit = appshot_prompt_limit(self.context, self.llm.config, overrides)
            if request_token_bound(messages, tools, self.llm.config) >= limit:
                if projection is not None and messages != original:
                    projection.reset()
                    messages = original
                    if "vision" not in self.llm.config.capabilities:
                        messages = _messages_for_capabilities(
                            messages, self.llm.config.capabilities,
                            frozenset(tool.get("function", {}).get("name", "") for tool in tools),
                        )
                    kwargs["messages"] = messages
                if request_token_bound(messages, tools, self.llm.config) >= limit:
                    raise AppshotValidationError("context_budget_exceeded")
        broker = getattr(self, "context_index_broker", None)
        if broker is not None and getattr(broker, "native_memory", False):
            broker.mark_submitted(messages)
        if profiler is not None:
            profiler.set_request(messages, tools, self._active_skill_names)
        provider_events = self.llm.chat_stream(**kwargs)
        return filter_message_time_events(stream_with_progress(_stream_with_generation_stats(provider_events)))

    async def _fit_appshot_request_budget(
        self, prompt: list[dict], tools: list[dict], *, user_text: str,
        transient_messages: list[dict] | None = None, profiler=None,
    ) -> tuple[list[dict], int | None]:
        """Use the final image/schema bound before dispatch, including plain follow-ups.

        One forced compaction and rebuild is permitted. No provider request or
        input action is replayed, and request-local observations remain attached.
        The synchronous stream guard remains the final fail-closed check.
        """
        if not any(
            isinstance(message.get("content"), list) and any(
                isinstance(part, dict) and part.get("type") == "appshot_image"
                for part in message["content"]
            ) for message in self.context.messages
        ):
            return prompt, None
        from agent.cli.appshot_admission import appshot_prompt_limit, request_token_bound
        from agent.cli.appshots import AppshotValidationError
        from agent.runtime.llm import _messages_for_capabilities

        def bound(messages):
            if "vision" not in self.llm.config.capabilities:
                messages = _messages_for_capabilities(
                    messages, self.llm.config.capabilities,
                    frozenset(tool.get("function", {}).get("name", "") for tool in tools),
                )
            return request_token_bound(messages, tools, self.llm.config)

        limit = appshot_prompt_limit(self.context, self.llm.config, self._generation_overrides())
        if bound(prompt) < limit:
            return prompt, None
        await self.context.compress_if_needed(force=True)
        # Compaction changes message indices. Resolve the current user again.
        prompt, estimate = await self._prepare_prompt_for_llm(
            user_text, getattr(self.llm, "estimate_tokens", None),
            transient_messages=transient_messages, profiler=profiler,
        )
        if bound(prompt) >= limit:
            raise AppshotValidationError("context_budget_exceeded")
        return prompt, estimate

    def _tool_routing_text(self, user_text: str) -> str:
        """Route tools from the active task, not only the final short follow-up."""
        parts = [user_text]
        previous_users = 0
        for message in reversed(self.context.messages[:-1]):
            content = message.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            if (
                message.get("role") == "user"
                and not _is_synthetic_user_turn(message)
                and previous_users < 3
            ):
                parts.append(content[-2000:])
                previous_users += 1
                continue
            if SUMMARY_PREFIX in content:
                active = re.search(
                    r"## Active Task\s*(.*?)(?=\n## |\Z)",
                    content,
                    flags=re.DOTALL,
                )
                if active:
                    parts.append(active.group(1).strip()[:4000])
        return "\n".join(reversed(parts))

    @staticmethod
    def _error_fingerprint(error: str) -> str:
        marker = re.search(r"\[([^\]]+)\]", error)
        status = re.search(r"\b([45]\d\d)\b", error)
        exc_type = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Timeout))\b", error)
        stable = ":".join(
            part for part in (
                marker.group(1) if marker else "tool-error",
                status.group(1) if status else "",
                exc_type.group(1) if exc_type else "",
            )
            if part
        )
        if stable != "tool-error":
            return stable
        normalized = re.sub(r"\b\d+\b", "#", error.lower())
        return re.sub(r"\s+", " ", normalized).strip()[:160]

    @staticmethod
    def _is_deterministic_tool_error(error: str, event: dict | None = None) -> bool:
        lower = error.lower()
        normalized_path = lower.replace("\\", "/")
        if event is not None and event.get("error_type") == "overall_timeout":
            # The underlying outage may be transient, but immediately replaying
            # an identical invocation can double a long wait or duplicate paid
            # work. A changed invocation remains available as a recovery path.
            return True
        if (
            event is not None
            and event.get("error_type") == "execution_failed"
            and event.get("recoverable")
            and "/.astra/processes/" in normalized_path
            and ("permissionerror" in lower or "access is denied" in lower or "拒绝访问" in lower)
        ):
            # This is an internal process-manifest persistence race, not proof
            # that the requested command and arguments will fail again.
            return False
        return any(marker in lower for marker in (
            "filenotfounderror",
            "no such file",
            "image not found",
            "toolinputerror",
            "permissionerror",
            "access is denied",
            "拒绝访问",
        ))

    @staticmethod
    def _fallback_stop_answer(reason: str) -> str:
        return (
            "工具执行已安全停止，避免继续重复调用或扩大错误。"
            f"停止原因：{reason}。"
            "当前已取得的工具结果仍保留在会话中；你可以根据上面的结果继续，或调整条件后让我重试。"
        )

    async def _prepare_turn(
        self,
        msg: Msg,
    ) -> tuple[bool, int | None, object | None, dict | None]:
        if not msg.has_user_content():
            return False, None, None, None
        self._refresh_skill_catalog()
        user_text = msg.get_text()
        continuation = bool(
            re.fullmatch(
                r"\s*(继续|继续吧|接着|接着做|go on|continue)\s*[。.!！]?\s*",
                user_text.lower(),
            )
        )
        if not continuation:
            self._task_mutated_paths.clear()
            self._task_context_paths.clear()
            self._active_skill_names.clear()
            self._verification_required = False
        # Project instructions are small and budgeted; reload at each genuine
        # turn boundary so edits to AGENTS.md/rules take effect without restart.
        self._refresh_project_instructions(force=True)
        observed = await self.drain_observed()
        for obs in observed:
            text = obs.get_text()
            if text:
                self.context.add_user(f"[通知 {obs.sender}]: {text}", provenance="notification")
        user_index = len(self.context.messages)
        chat_content = msg.to_chat_content()
        storage_content = msg.to_storage_content()
        vision_event = None
        def native_appshot(block):
            return block.type == "image_url" and (block.data.get("appshot_verified") or block.data.get("appshot_media"))

        if self._vision_tile_tool_enabled() and any(
            block.type == "image_url" and not native_appshot(block) for block in msg.content
        ):
            # Keep verified original Appshot bytes opaque to ordinary tiling.
            # Ordinary images in the same message retain existing preprocessing.
            preprocess_blocks = [
                ContentBlock("appshot_passthrough", block.data) if native_appshot(block) else block
                for block in msg.content
            ]

            def restore_appshots(blocks):
                return [
                    ContentBlock("image_url", block.data) if block.type == "appshot_passthrough" else block
                    for block in blocks
                ]
            policy = getattr(self.llm.config, "vision_preprocess", None)
            assert policy is not None
            try:
                bundle = self.vision_preprocessor.prepare_blocks(
                    preprocess_blocks,
                    policy=policy,
                    enabled=True,
                    session_id=self._vision_session_id(),
                    request_id=self._active_vision_request_id or "",
                    occurrence_id=f"user:{self._active_vision_request_id}:initial",
                )
            except VisionPreprocessError:
                # The provider-facing bundle is fail-closed, but the user's
                # original attachment placeholder remains durable and retryable.
                self.context.add_user(storage_content)
                await self.context.save_async()
                raise
            chat_msg = Msg(
                sender=msg.sender,
                role=msg.role,
                content=restore_appshots(bundle.chat_blocks),
                metadata=msg.metadata,
            )
            storage_msg = Msg(
                sender=msg.sender,
                role=msg.role,
                content=restore_appshots(bundle.storage_blocks),
                metadata=msg.metadata,
            )
            chat_content = chat_msg.to_chat_content()
            storage_content = storage_msg.to_storage_content()
            vision_event = {
                "type": "vision_preprocess",
                "message": bundle.status,
                "protected": bundle.protected,
                "protected_local_images": bundle.protected_local_images,
                "unprotected_external_images": bundle.unprotected_external_images,
            }
        canonical_content = storage_content if storage_content != chat_content else chat_content
        self.context.add_user(canonical_content, provenance=(
            str(msg.metadata["source"]) if msg.metadata.get("source") in {"session_wakeup", "command_workflow", "peer_message"}
            else "goal_continuation" if msg.metadata.get("goal_round") else ""
        ))
        if msg.metadata.get("source") in {"command_workflow", "peer_message"} and msg.metadata.get("display_command"):
            self.context.messages[user_index]["display_command"] = msg.metadata["display_command"]
        if storage_content == chat_content:
            storage_content = None
        if storage_content is not None:
            self._active_vision_prompt_overlays.append(
                (self.context.messages[user_index], storage_content, chat_content)
            )
            self._active_vision_storage_replacements.append(
                (user_index, storage_content)
            )
        return True, user_index, storage_content, vision_event

    def _assistant_message(
        self,
        content: str,
        tool_calls: list | None,
        reasoning_content: str = "",
        provider_state: dict | None = None,
    ) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        capabilities = getattr(getattr(self.llm, "config", None), "capabilities", frozenset())
        if reasoning_content and "reasoning" in capabilities:
            # DeepSeek V4 thinking mode requires this field to be replayed,
            # especially for assistant messages that contain tool calls.
            msg["reasoning_content"] = reasoning_content
        if tool_calls:
            persistent_calls = []
            for tc in tool_calls:
                arguments = self._persistent_tool_call_arguments(tc)
                tool = self.tools.get(str(tc.get("name") or ""))
                self.context.append_raw_tool_call({
                    "type": "raw_tool_call",
                    "call_id": str(tc.get("id") or ""),
                    "name": str(tc.get("name") or "tool"),
                    "arguments": arguments,
                    "argument_persistence": tool.argument_persistence if tool is not None else "durable",
                })
                persistent_calls.append({
                    "id": tc["id"], "type": "function",
                    "function": {"name": tc["name"], "arguments": arguments},
                })
            msg["tool_calls"] = persistent_calls
        if provider_state:
            from .codex_wire import signature
            if provider_state.get("signature") == signature(msg):
                msg["_provider_state"] = provider_state
        return msg

    def _persistent_tool_call_arguments(self, tool_call: dict[str, Any]) -> str:
        """Return model tool arguments safe for events and durable history.

        Request-local tools declare an ``argument_redactor`` (keeps structure,
        hides sensitive fields); tools without one fall back to a content-free
        placeholder shape. Tool-specific redactors may retain ordinary values
        while excluding only credential-bearing or otherwise private fields.
        """
        raw = str(tool_call.get("arguments") or "")
        tool = self.tools.get(str(tool_call.get("name") or ""))
        if tool is None or tool.argument_persistence == "durable":
            return raw
        try:
            parsed = self._parse_tool_args(raw)
        except ToolArgumentError:
            safe = {
                "request_local_placeholder": True,
                "argument_keys": [],
                "collection_sizes": {},
            }
        else:
            safe = self.tools.persistence_safe_args(tool, parsed)
        return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))

    def _parse_tool_args(self, raw_args: str) -> dict:
        try:
            return json.loads(raw_args)
        except json.JSONDecodeError:
            repaired = self._repair_json(raw_args)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                raise ToolArgumentError("invalid JSON arguments")

    def _validated_tool_calls(
        self,
        tool_calls: list[dict] | None,
        finish_reason: str,
        usage: dict | None,
    ) -> tuple[list[dict] | None, ToolFailure | None]:
        """Validate and canonicalize tool arguments before history or execution."""
        if not tool_calls:
            return tool_calls, None

        output_limited = finish_reason.lower() in {"length", "max_tokens", "max_output_tokens"}
        validated: list[dict] = []
        seen_ids: set[str] = set()
        for tc in tool_calls:
            name = str(tc.get("name") or "")
            call_id = str(tc.get("id") or "")
            raw_args = tc.get("arguments", "")
            tool_def = self.tools.get(name)
            request_local_args = (
                tool_def is not None
                and tool_def.argument_persistence == "request_local"
            )
            try:
                if output_limited:
                    raise ToolArgumentError("provider truncated the tool batch")
                if not name.strip() or not call_id.strip() or not isinstance(raw_args, str):
                    raise ToolArgumentError("missing tool name, call id, or JSON arguments")
                if call_id in seen_ids:
                    raise ToolArgumentError("duplicate tool call id in one response")
                seen_ids.add(call_id)
                if self._looks_like_incomplete_json(raw_args):
                    raise ToolArgumentError("tool arguments contain incomplete JSON")
                parsed = self._parse_tool_args(raw_args)
                if not isinstance(parsed, dict):
                    raise ToolArgumentError("tool arguments must be a JSON object")
            except ToolArgumentError as exc:
                provider_stop_mismatch = (
                    isinstance(raw_args, str)
                    and len(raw_args) >= 512
                    and self._looks_like_incomplete_json(raw_args)
                )
                truncated = output_limited or provider_stop_mismatch
                code = "tool_call_truncated" if truncated else "invalid_arguments"
                completion_tokens = (usage or {}).get("completion_tokens")
                if code == "tool_call_truncated":
                    message = (
                        f"OUTPUT LIMIT · {name or 'tool'} · incomplete arguments were discarded; "
                        "the tool was not executed."
                    )
                    hint = "Retry with a smaller edit, apply_patch, or transactional file chunks."
                else:
                    message = (
                        f"INVALID ARGUMENTS · {name or 'tool'} · malformed tool JSON was discarded; "
                        "the tool was not executed."
                    )
                    hint = "Submit one complete JSON object matching the tool schema."
                raw_prefix = (
                    raw_args[:512]
                    if isinstance(raw_args, str) and not request_local_args
                    else ""
                )
                path_match = re.search(r'"path"\s*:\s*"([^"]*)', raw_prefix)
                path_hint = path_match.group(1) if path_match else ""
                prefix_digest = (
                    hashlib.sha256(raw_prefix.encode("utf-8")).hexdigest()
                    if raw_prefix else ""
                )
                fingerprint = "|".join((
                    name or "tool",
                    "request-local" if request_local_args else path_hint,
                    prefix_digest[:16],
                    finish_reason or "unknown",
                ))
                diagnostic_arguments: object = (
                    {
                        "request_local_placeholder": True,
                        "argument_chars": len(raw_args) if isinstance(raw_args, str) else 0,
                    }
                    if request_local_args
                    else raw_args if isinstance(raw_args, str) else repr(raw_args)
                )
                artifact_ref = self.context.append_diagnostic({
                    "type": "tool_call_failure",
                    "code": code,
                    "tool_name": name,
                    "call_id": call_id,
                    "finish_reason": finish_reason or "unknown",
                    "usage": dict(usage or {}),
                    "parse_error": str(exc),
                    "raw_arguments": diagnostic_arguments,
                })
                return None, ToolFailure(
                    code=code,
                    message=message,
                    retryable=truncated,
                    recovery_hint=hint,
                    tool_name=name,
                    call_id=call_id,
                    partial=bool(raw_args),
                    details={
                        "finish_reason": finish_reason or "unknown",
                        "finish_reason_mismatch": provider_stop_mismatch and not output_limited,
                        "argument_chars": len(raw_args) if isinstance(raw_args, str) else 0,
                        "completion_tokens": completion_tokens,
                        "parse_error": str(exc),
                        "path": path_hint,
                        "argument_prefix_sha256": prefix_digest,
                        "fingerprint": fingerprint,
                    },
                    artifact_ref=artifact_ref,
                )
            canonical = dict(tc)
            context_open_placeholder = name == "context_open" and has_context_open_placeholder(parsed)
            if context_open_placeholder or (request_local_args and "request_local_placeholder" in parsed):
                # The model imitated the internal redaction placeholder
                # instead of emitting real schema arguments (seen with
                # computer_act after the placeholder leaked into history).
                # Reject with a teaching hint rather than a cryptic schema
                # failure downstream.
                marker = "redacted handle placeholder" if context_open_placeholder else "request_local_placeholder"
                artifact_ref = self.context.append_diagnostic({
                    "type": "tool_call_failure",
                    "code": "invalid_arguments",
                    "tool_name": name,
                    "call_id": call_id,
                    "finish_reason": finish_reason or "unknown",
                    "usage": dict(usage or {}),
                    "parse_error": (
                        f"arguments contained the {marker} "
                        "redaction marker instead of real tool arguments"
                    ),
                    "raw_arguments": {
                        "request_local_placeholder": True,
                        "argument_chars": len(raw_args) if isinstance(raw_args, str) else 0,
                    },
                })
                return None, ToolFailure(
                    code="invalid_arguments",
                    message=(
                        f"INVALID ARGUMENTS · {name} · arguments contained the internal "
                        f"{marker} redaction marker; the tool was not executed."
                    ),
                    retryable=True,
                    recovery_hint=(
                        CONTEXT_OPEN_RECOVERY_HINT if context_open_placeholder else
                        "Emit real arguments matching the tool schema (for example "
                        "snapshot_id and actions for computer_act). The "
                        "request_local_placeholder / argument_keys / collection_sizes "
                        "fields are internal redaction markers and are never accepted as input."
                    ),
                    tool_name=name,
                    call_id=call_id,
                    partial=False,
                    details={
                        "finish_reason": finish_reason or "unknown",
                        "argument_chars": len(raw_args),
                        "request_local_placeholder_rejected": True,
                    },
                    artifact_ref=artifact_ref,
                )
            canonical["id"] = call_id
            canonical["name"] = name
            canonical["arguments"] = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            validated.append(canonical)
        return validated, None

    def _assistant_prefill_enabled(self) -> bool:
        """Use provider-specific prefill only when the model opts in."""
        capabilities = getattr(
            getattr(self.llm, "config", None),
            "capabilities",
            frozenset(),
        )
        return "assistant_prefill" in capabilities

    @staticmethod
    def _merge_assistant_prefill(prefix: str, continuation: str) -> str:
        """Reconstruct JSON from providers returning a suffix or full value."""
        if continuation.startswith(prefix):
            return continuation
        try:
            replacement = json.loads(continuation)
        except (json.JSONDecodeError, TypeError):
            return prefix + continuation
        return continuation if isinstance(replacement, dict) else prefix + continuation

    @staticmethod
    def _looks_like_incomplete_json(raw: str) -> bool:
        """Detect a long call cut inside a string/container despite a bad stop reason."""
        stack: list[str] = []
        in_string = False
        escaped = False
        pairs = {"}": "{", "]": "["}
        for char in raw:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "[{":
                stack.append(char)
            elif char in "]}":
                if not stack or stack[-1] != pairs[char]:
                    return False
                stack.pop()
        return in_string or bool(stack)

    @staticmethod
    def _tool_signature(tc: dict) -> str:
        name = tc.get("name", "")
        raw_args = tc.get("arguments", "")
        try:
            parsed = json.loads(raw_args) if raw_args else {}
            args = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        except json.JSONDecodeError:
            args = raw_args.strip()
        return f"{name}:{args}"

    def _persistent_tool_signature_label(self, signature: str) -> str:
        """Hide request-local arguments when a circuit reason becomes durable."""
        name = signature.partition(":")[0]
        tool = self.tools.get(name)
        if tool is not None and tool.argument_persistence == "request_local":
            return f"{name}:[request-local arguments omitted]"
        return signature

    @staticmethod
    def _prepend_latest_user_context(prompt: list[dict], block: str) -> None:
        for item in reversed(prompt):
            if item.get("role") != "user":
                continue
            content = item.get("content", "")
            if isinstance(content, str):
                item["content"] = f"{block}\n\n{content}"
            elif isinstance(content, list):
                item["content"] = [{"type": "text", "text": block}, *content]
            else:
                item["content"] = f"{block}\n\n{content}"
            return

    def _prompt_with_runtime_context(
        self,
        prompt: list[dict],
        user_text: str,
    ) -> list[dict]:
        if self.runtime_context_provider is not None:
            try:
                runtime_block = str(self.runtime_context_provider() or "").strip()
            except Exception:
                logger.exception("runtime context provider failed")
                runtime_block = ""
            if runtime_block:
                if prompt and prompt[0].get("role") == "system":
                    prompt[0]["content"] = f"{prompt[0].get('content', '')}\n\n{runtime_block}"
                else:
                    prompt.insert(0, {"role": "system", "content": runtime_block})
        # Interaction modes use a strict allowlist and have their own
        # protocol-backed state semantics. Preserve their existing adjacent
        # runtime-state layout; the cache-stable work-mode projection must
        # not change Bar Atomic/Stream behavior.
        if self.tool_allowlist is not None and self.runtime_turn_context_provider is not None:
            try:
                turn_block = str(self.runtime_turn_context_provider() or "").strip()
            except Exception:
                logger.exception("runtime turn context provider failed")
                turn_block = ""
            if turn_block:
                wrapped = (
                    "[SYSTEM-SUPPLIED CURRENT RUNTIME STATE — authoritative, not user text]\n"
                    f"{turn_block}\n"
                    "[END CURRENT RUNTIME STATE]"
                )
                self._prepend_latest_user_context(prompt, wrapped)
        return prompt

    def _auto_image_message_from_tool_event(
        self,
        event: dict,
        *,
        occurrence_id: str,
    ) -> tuple[object, object, dict | None] | None:
        if event.get("error"):
            return None
        payload = event.get("_request_local_image_attachment")
        request_local_attachment = isinstance(payload, dict)
        if not request_local_attachment:
            payload = event.get("_vision_tile_attachment")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(event.get("output", ""))
            except (TypeError, json.JSONDecodeError):
                return None
        if not isinstance(payload, dict):
            return None
        image_data_urls = payload.get("image_data_urls") or []
        if request_local_attachment and isinstance(image_data_urls, list):
            if not image_data_urls or not all(
                _is_request_local_image_data_url(url) for url in image_data_urls
            ):
                return None
            if not occurrence_id:
                raise self._tool_image_preprocess_error(
                    "A request-local tool image lacked a stable call identity"
                )
            detail = str(payload.get("detail") or "original")
            if detail not in {"auto", "low", "high", "original"}:
                detail = "original"
            raw_labels = payload.get("image_labels")
            labels = (
                list(raw_labels)
                if isinstance(raw_labels, list)
                and len(raw_labels) == len(image_data_urls)
                and all(isinstance(label, str) for label in raw_labels)
                else [""] * len(image_data_urls)
            )
            blocks = [ContentBlock.text(
                "系统提示：以下是工具刚生成并仅限本次请求使用的已验证桌面截图。请直接查看像素后继续。"
            )]
            for data_url, label in zip(image_data_urls, labels):
                if label.strip():
                    blocks.append(ContentBlock.text(label.strip()))
                blocks.append(ContentBlock.image_url(data_url, detail=detail))
            source_msg = Msg(sender="tool", role="user", content=blocks)
            storage_msg = Msg(
                sender="tool",
                role="user",
                content=[ContentBlock.text(
                    "[Request-local Computer Use screenshot omitted after its one permitted model request.]"
                )],
            )
            return source_msg.to_chat_content(), storage_msg.to_storage_content(), None
        if event.get("name") == "read_image_tiles" and isinstance(image_data_urls, list):
            if not image_data_urls or not all(
                isinstance(url, str) and url.startswith("data:image/png;base64,")
                for url in image_data_urls
            ):
                return None
            if not occurrence_id:
                raise self._tool_image_preprocess_error(
                    "A tool-produced image lacked a stable call identity"
                )
            raw_labels = payload.get("image_labels")
            labels = (
                list(raw_labels)
                if isinstance(raw_labels, list)
                and len(raw_labels) == len(image_data_urls)
                and all(isinstance(label, str) for label in raw_labels)
                else [""] * len(image_data_urls)
            )
            blocks = [ContentBlock.text(
                "系统提示：以下是本次请求中原子验证并选定的原像素细节图块。请直接查看像素后回答。"
            )]
            for data_url, label in zip(image_data_urls, labels):
                if label.strip():
                    blocks.append(ContentBlock.text(label.strip()))
                blocks.append(ContentBlock.image_url(data_url, detail="original"))
            source_msg = Msg(sender="tool", role="user", content=blocks)
            return (
                source_msg.to_chat_content(),
                source_msg.to_storage_content(),
                None,
            )
        paths = payload.get("image_paths") or payload.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return None
        if payload.get("type") != "image_attachment" and event.get("name") != "read_image":
            return None
        if not occurrence_id:
            raise self._tool_image_preprocess_error(
                "A tool-produced image lacked a stable call identity"
            )

        question = str(payload.get("question") or "").strip()
        intro = "系统提示：工具刚附加了以下图片。请直接查看图片内容并基于实际画面回答，不要只根据 prompt、文件名或路径猜测。"
        if question:
            intro += f"\n用户想检查的问题：{question}"
        detail = str(payload.get("detail") or "auto")
        if detail not in {"auto", "low", "high", "original"}:
            detail = "auto"
        raw_labels = payload.get("image_labels")
        labels = (
            list(raw_labels)
            if isinstance(raw_labels, list)
            and len(raw_labels) == len(paths)
            and all(isinstance(label, str) for label in raw_labels)
            else [""] * len(paths)
        )

        tiling_required = self._vision_tile_tool_enabled()
        max_bytes = int(os.getenv("MAX_AUTO_TOOL_IMAGE_BYTES", str(10 * 1024 * 1024)))
        blocks = [ContentBlock.text(intro)]
        for raw_path, label in zip(paths, labels):
            try:
                path = Path(str(raw_path)).expanduser()
                if not path.is_absolute():
                    path = Path.cwd() / path
                path = path.resolve()
                is_file = path.is_file()
                file_size = path.stat().st_size if is_file else 0
            except (OSError, RuntimeError) as exc:
                if tiling_required:
                    raise self._tool_image_preprocess_error(
                        "Unable to access a tool-produced local image"
                    ) from exc
                continue
            if not is_file:
                if tiling_required:
                    raise self._tool_image_preprocess_error(
                        "A tool-produced local image is not a regular file"
                    )
                continue
            if file_size > max_bytes:
                if tiling_required:
                    raise self._tool_image_preprocess_error(
                        "A tool-produced local image exceeded the safe materialization limit"
                    )
                continue
            mime, _ = mimetypes.guess_type(str(path))
            if mime == "image/jpg":
                mime = "image/jpeg"
            if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                if tiling_required:
                    raise self._tool_image_preprocess_error(
                        "A tool-produced local image has an unsupported format"
                    )
                continue
            try:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as exc:
                if tiling_required:
                    raise self._tool_image_preprocess_error(
                        "Unable to read a tool-produced local image"
                    ) from exc
                continue
            if label.strip():
                blocks.append(ContentBlock.text(label.strip()))
            blocks.append(ContentBlock.image_url(f"data:{mime};base64,{encoded}", detail=detail, source_path=str(path)))

        if len(blocks) == 1:
            return None
        source_msg = Msg(sender="tool", role="user", content=blocks)
        chat_content = source_msg.to_chat_content()
        storage_blocks = list(blocks)
        vision_event = None
        if self._vision_tile_tool_enabled():
            policy = getattr(self.llm.config, "vision_preprocess", None)
            assert policy is not None
            bundle = self.vision_preprocessor.prepare_blocks(
                blocks,
                policy=policy,
                enabled=True,
                session_id=self._vision_session_id(),
                request_id=self._active_vision_request_id or "",
                occurrence_id=occurrence_id,
            )
            chat_content = Msg(
                sender="tool",
                role="user",
                content=list(bundle.chat_blocks),
            ).to_chat_content()
            storage_blocks = list(bundle.storage_blocks)
            vision_event = {
                "type": "vision_preprocess",
                "message": bundle.status,
                "protected": bundle.protected,
                "protected_local_images": bundle.protected_local_images,
                "unprotected_external_images": bundle.unprotected_external_images,
            }
        if event.get("name") == "read_image_tiles":
            storage_blocks = [
                ContentBlock.image_url(
                    str(block.data.get("url", "")),
                    detail=str(block.data.get("detail") or "auto"),
                )
                if block.type == "image_url"
                else block
                for block in storage_blocks
            ]
        storage_content = Msg(
            sender="tool",
            role="user",
            content=storage_blocks,
        ).to_storage_content()
        return chat_content, storage_content, vision_event

    @staticmethod
    def _tool_image_preprocess_error(reason: str) -> VisionPreprocessError:
        return VisionPreprocessError(
            f"{reason}. Astra did not silently send a provider-downscaled substitute; "
            "disable vision tiles explicitly to restore direct sending."
        )

    async def _prepare_prompt_for_llm(
        self,
        user_text: str,
        estimate_tokens,
        trace_ctx: TraceContext | None = None,
        transient_messages: list[dict] | None = None,
        current_user_index: int | None = None,
        profiler: QueryProfiler | None = None,
    ) -> tuple[list[dict], int | None]:
        # MCP servers may change their tool list between turns. Refresh the
        # schema overhead before deciding whether the context needs compaction.
        self.refresh_tool_token_cost()
        # Cancellation can interrupt a turn after assistant tool_calls were
        # recorded but before every tool result was appended. Never send that
        # transient protocol state to the next provider request.
        profile = profiler or QueryProfiler(
            enabled=False,
            session_id="",
            request_id="",
            step=0,
            model="",
            root=Path.cwd(),
        )
        with profile.phase("sanitize_tool_history"):
            self.context.sanitize_tool_history()
        session_id = Path(self.context.session_path).stem if self.context.session_path else "default"
        if current_user_index is None:
            current_user_index = next(
                (
                    index
                    for index in range(len(self.context.messages) - 1, -1, -1)
                    if self.context.messages[index].get("role") == "user"
                ),
                None,
            )
        cache_key = self._memory_turn_key or f"turn:{current_user_index}"
        if cache_key != self._turn_context_key:
            blocks = []
            context_index_broker = getattr(self, "context_index_broker", None)
            native_index = bool(
                context_index_broker is not None
                and getattr(context_index_broker, "native_memory", False)
                and context_index_broker.mode in {"session", "all"}
                and self.tool_allowlist is None
                and self.memory_store is not None
                and not str(user_text).lstrip().startswith("/")
            )
            task_text = ""
            active_memory_text = ""
            # Interaction sandboxes such as Night Bar disable memory by
            # temporarily detaching memory_store. Freeze the resulting pack
            # once per user turn so TaskRun checkpoints cannot mutate the
            # already-cached prefix between tool iterations.
            with profile.phase("memory_and_turn_context"):
                if self.memory_router is not None and self.memory_store is not None:
                    with trace_span("agent.memory.recall", ctx=trace_ctx) as memory_span:
                        pack = await self.memory_router.build_context(
                            user_text,
                            session_id=session_id,
                            turn_key=self._memory_turn_key,
                            **({"recall_enabled": False} if native_index else {}),
                        )
                        memory_ids = ",".join(pack.trace.record_ids)
                        if trace_ctx is not None:
                            trace_ctx.memory_id = memory_ids
                        if memory_span is not None and memory_ids:
                            memory_span.set_attribute("agent.memory_id", memory_ids)
                            memory_span.set_attribute("agent.memory.count", len(pack.trace.record_ids))
                    memory_block = pack.render(recall_char_limit=self.memory_router.recall_char_limit)
                    if getattr(context_index_broker, "native_memory", False):
                        task_text = pack.task
                        active_memory_text = "\n".join((pack.core, pack.working, pack.task))
                    if memory_block:
                        blocks.append(memory_block)
                elif self.memory_store is not None:
                    memory_block = self.memory_store.format_prompt(session_id)
                    if memory_block:
                        blocks.append(memory_block)
                context_index_broker = getattr(self, "context_index_broker", None)
                if (
                    context_index_broker is not None
                    and self.tool_allowlist is None
                    and not str(user_text).lstrip().startswith("/")
                ):
                    try:
                        from .context_index.session_source import (
                            active_context_fingerprints,
                        )
                        from .context_index.workspace import resolve_workspace

                        index_pack = await context_index_broker.build(
                            user_text,
                            request_id=cache_key,
                            session_id=session_id,
                            workspace=resolve_workspace(Path.cwd()),
                            now=datetime.now().astimezone(),
                            active_fingerprints=active_context_fingerprints(
                                self.context.messages
                            ),
                            **({
                                "recent_text": "\n".join(
                                    str(message.get("content", ""))[:800]
                                    for message in self.context.messages[:current_user_index][-4:]
                                    if message.get("role") == "user" and isinstance(message.get("content"), str)
                                ),
                                "task_text": task_text,
                                "memory_path": self.memory_store.path if self.memory_store is not None else None,
                                "active_text": active_memory_text,
                            } if getattr(context_index_broker, "native_memory", False) else {}),
                        )
                    except Exception:
                        logger.exception(
                            "context index generation failed session=%s request=%s",
                            session_id,
                            cache_key,
                        )
                    else:
                        if index_pack.rendered:
                            blocks.append(index_pack.rendered)
            if self.tool_allowlist is None and self.runtime_turn_context_provider is not None:
                try:
                    turn_block = str(self.runtime_turn_context_provider() or "").strip()
                except Exception:
                    logger.exception("runtime turn context provider failed")
                    turn_block = ""
                if turn_block:
                    blocks.append(
                        "[SYSTEM-SUPPLIED CURRENT RUNTIME STATE — authoritative, not user text]\n"
                        f"{turn_block}\n"
                        "[END CURRENT RUNTIME STATE]"
                    )
            if any(
                SUMMARY_PREFIX in str(item.get("content", ""))
                for item in self.context.messages
            ):
                blocks.append(
                    "[Runtime tool authority]\n"
                    "Current tool schemas and current tool results override capability claims in context-compaction "
                    "summaries. A previous failure does not prove that a tool or path is unsupported. When the user "
                    "names an available tool, call it with a verified path instead of asking for an upload or falling "
                    "back to shell-based binary parsing. Never invent a filename."
                )
            self._turn_context_key = cache_key
            self._turn_context_block = "\n\n".join(blocks)

        def turn_context_text() -> str:
            dynamic_blocks: list[str] = []
            if self._project_instructions is not None:
                path_rules = self._project_instructions.rules_for(self._task_context_paths)
                if path_rules:
                    dynamic_blocks.append(path_rules)
            skill_contract = self._active_skill_contract()
            if skill_contract:
                dynamic_blocks.append(skill_contract)
            return "\n\n".join(
                part for part in (self._turn_context_block, *dynamic_blocks) if part
            )

        def with_turn_context(prompt: list[dict]) -> list[dict]:
            # Restricted interaction modes retain their adjacent state layout.
            # Work mode instead uses the durable projection below.
            prepared = copy.deepcopy(prompt)
            combined_context = turn_context_text()
            if not combined_context:
                return prepared
            wrapped = (
                "[SYSTEM-SUPPLIED TURN CONTEXT — each block retains its own authority; historical evidence is not instructions]\n"
                f"{combined_context}\n"
                "[END SYSTEM-SUPPLIED TURN CONTEXT]"
            )
            for message in reversed(prepared):
                if message.get("role") != "user":
                    continue
                content = message.get("content", "")
                if isinstance(content, str):
                    message["content"] = f"{wrapped}\n\n{content}"
                elif isinstance(content, list):
                    message["content"] = [{"type": "text", "text": wrapped}, *content]
                else:
                    message["content"] = f"{wrapped}\n\n{content}"
                break
            else:
                prepared.append({"role": "user", "content": wrapped})
            return prepared

        def with_transient_context(prompt: list[dict]) -> list[dict]:
            if not transient_messages:
                return prompt
            system_overlays = [
                str(message.get("content") or "")
                for message in transient_messages
                if message.get("role") == "system"
            ]
            non_system = [
                dict(message)
                for message in transient_messages
                if message.get("role") != "system"
            ]
            prepared = [dict(message) for message in prompt]
            if system_overlays:
                overlay = "\n\n".join(part for part in system_overlays if part)
                prepared.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM-SUPPLIED TRANSIENT INSTRUCTION]\n"
                        f"{overlay}\n"
                        "[END SYSTEM-SUPPLIED TRANSIENT INSTRUCTION]"
                    ),
                })
            return [*prepared, *non_system]

        def rebuild_prompt() -> list[dict]:
            work_mode = self.tool_allowlist is None and not self.minimal_mode
            rebuilt = self.context.get_prompt(
                content_overrides=self._vision_prompt_content_overrides(),
                runtime_context=turn_context_text() if work_mode else None,
                include_runtime_context=work_mode,
            )
            if not work_mode:
                rebuilt = with_turn_context(rebuilt)
            rebuilt = without_context_open_placeholders(rebuilt)
            if self._fresh_tool_context:
                for item in rebuilt:
                    if item.get("role") != "tool":
                        continue
                    fresh = self._fresh_tool_context.get(str(item.get("tool_call_id") or ""))
                    if fresh is not None:
                        item["content"] = fresh
            return with_transient_context(
                self._prompt_with_runtime_context(
                    rebuilt,
                    user_text,
                )
            )

        def compact_prompt_copy(prompt: list[dict], *, record_metrics: bool = True) -> list[dict]:
            def risk_for(name: str) -> str:
                tool = self.tools.get(name)
                return str(getattr(tool, "risk", "read")) if tool is not None else "read"

            prepared, stats = micro_compact_tool_results(prompt, tool_risk=risk_for)
            if stats.cleared_results and record_metrics:
                runtime_metrics.increment("tool_result_microcompact_count", stats.cleared_results)
                runtime_metrics.increment("tool_result_microcompact_saved_chars", stats.saved_chars)
                runtime_metrics.increment("tool_result_microcompact_saved_tokens", stats.saved_tokens)
            return prepared

        with profile.phase("prompt_copy_and_overlays"):
            prompt = compact_prompt_copy(rebuild_prompt())
        prompt_estimate = None
        if estimate_tokens:
            with profile.phase("token_estimate"):
                prompt_estimate = estimate_tokens(prompt)
            self.context.last_prompt_tokens = prompt_estimate
            if prompt_estimate >= self.context.max_prompt_tokens:
                def measure_compaction_tokens() -> int:
                    return estimate_tokens(compact_prompt_copy(rebuild_prompt(), record_metrics=False))

                with profile.phase("context_compaction"):
                    await self.context.compress_if_needed(measure_tokens=measure_compaction_tokens)
                with profile.phase("prompt_rebuild_after_compaction"):
                    prompt = compact_prompt_copy(rebuild_prompt())
                    prompt_estimate = estimate_tokens(prompt)
                self.context.last_prompt_tokens = prompt_estimate
                if prompt_estimate >= self.context.max_prompt_tokens:
                    # Mirror the post-tool path: the opportunistic pass can be
                    # a no-op (e.g. a failed summary preserves history), so
                    # give compaction one forced retry before the request is
                    # sent to the provider.
                    with profile.phase("forced_context_compaction"):
                        await self.context.compress_if_needed(force=True, measure_tokens=measure_compaction_tokens)
                    with profile.phase("prompt_rebuild_after_forced_compaction"):
                        prompt = compact_prompt_copy(rebuild_prompt())
                        prompt_estimate = estimate_tokens(prompt)
                    self.context.last_prompt_tokens = prompt_estimate
        return prompt, prompt_estimate

    @staticmethod
    def _repair_json(text: str) -> str:
        """Repair common JSON errors from LLM tool-call output."""
        text = text.strip()
        # Extract JSON object if wrapped in markdown or extra text
        m = re.search(r'\{[^{}]*\}', text)
        if m:
            text = m.group(0)
        # Fix trailing comma before closing brace
        text = re.sub(r',\s*}', '}', text)
        text = re.sub(r',\s*]', ']', text)
        # Fix single-quoted keys/values
        text = re.sub(r"'([^']*)':", r'"\1":', text)
        text = re.sub(r":\s*'([^']*)'", r': "\1"', text)
        # Fix unquoted keys (simple case: word followed by colon)
        text = re.sub(r'([{,])\s*(\w+)\s*:', r'\1"\2":', text)
        return text

    _CONTEXT_OVERFLOW_RE = re.compile(
        r"context_length_exceeded"
        r"|maximum context length"
        r"|context (?:length|window|limit|size) (?:is )?(?:too (?:long|large)|exceeded)"
        r"|prompt is too long"
        r"|input (?:is )?too long"
        r"|too many (?:input|prompt) tokens"
        r"|too many tokens in (?:the )?(?:request|prompt|input)"
        r"|exceeds? (?:the |your )?(?:model'?s? )?(?:maximum |max )?context",
        re.IGNORECASE,
    )

    @classmethod
    def _is_context_overflow_error(cls, exc: BaseException) -> bool:
        """Best-effort provider-agnostic context-window overflow detection.

        Providers phrase the rejection differently (OpenAI-style "maximum
        context length" / context_length_exceeded, Anthropic-style "prompt is
        too long", local servers "context length exceeded"), so classify by
        error text instead of provider-specific exception types. False
        negatives only lose the one-shot overflow recovery; false positives
        cost one forced compaction and one retry, bounded by the caller.
        """
        if not isinstance(exc, Exception):
            return False
        return bool(cls._CONTEXT_OVERFLOW_RE.search(f"{type(exc).__name__}: {exc}"))

    async def _execute_tool_call(
        self,
        tc: dict,
        task_id: str | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        trace_ctx: TraceContext | None = None,
    ) -> dict:
        name = tc["name"]

        def report(payload: dict) -> None:
            tool_phase(str(payload.get("stage") or ""))
            if progress_callback is None:
                return
            progress_callback({
                "type": "tool_progress",
                "id": str(tc.get("id") or ""),
                "name": name,
                **payload,
            })

        report({"stage": "preparing", "status": "running"})
        try:
            args = self._parse_tool_args(tc["arguments"])
        except ToolArgumentError as e:
            return {
                "type": "tool_result",
                "id": tc["id"],
                "name": name,
                "args": {},
                "output": "",
                "error": f"[ToolInputError] {name}: {e}",
                "code": "invalid_arguments",
                "tool_output": f"[ToolInputError] {name}: {e}",
                "duration_ms": 0,
                "error_type": "invalid_input",
                "recoverable": True,
            }
        tool = self.tools.get(name)
        safety_failure = self._source_mutation_failure(name, args, tool)
        if safety_failure is not None:
            safety_failure["id"] = tc["id"]
            return safety_failure
        mutation_intent = self._source_mutation_intent(name, args)
        tracking_before: dict[str, str] | None = None
        if mutation_intent["mode"] == "track":
            # A process_id returned before completion cannot be diffed safely.
            # Keep approved source-mutating commands in the foreground so the
            # second snapshot always observes their final worktree state.
            parameters = tool.parameters if tool is not None else {}
            properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
            if "background" in properties or "foreground_yield_ms" in properties:
                args = dict(args)
                if "background" in properties:
                    args["background"] = False
                if "foreground_yield_ms" in properties:
                    args["foreground_yield_ms"] = 0
            tracking_before = await self._git_worktree_snapshot()
            if tracking_before is None:
                message = (
                    f"[SourceMutationTrackingUnavailable] `{name}` is an approved formatter or "
                    "code generator, but Astra could not snapshot the Git worktree before running it. "
                    "The command was not executed. Run it inside a Git worktree, or use "
                    "read_file plus edit_file/apply_patch."
                )
                return {
                    "type": "tool_result",
                    "id": tc["id"],
                    "name": name,
                    "args": args,
                    "output": "",
                    "error": message,
                    "code": "source_mutation_tracking_unavailable",
                    "tool_output": message,
                    "duration_ms": 0,
                    "error_type": "source_mutation_tracking_unavailable",
                    "recoverable": True,
                    "retryable": False,
                    "recovery_hint": (
                        "Confirm the command is running in the project Git worktree, or apply the "
                        "required source edits through edit_file/apply_patch."
                    ),
                    "details": {"reason": mutation_intent["reason"]},
                }
        risk = tool.risk if tool is not None else ""
        durable_args = self.tools.persistence_safe_args(tool, args)
        step_id: str | None = None
        if task_id and self.task_store is not None:
            tool_phase("claiming")
            try:
                if tool is not None and not tool.cache_results:
                    invocation_id = uuid.uuid4().hex
                elif tool is None or tool.idempotent:
                    invocation_id = None
                else:
                    invocation_id = str(tc.get("id") or "")
                claim = await durable_io(
                    self.task_store.claim_tool,
                    task_id,
                    name,
                    durable_args,
                    risk,
                    # Non-idempotent tools may intentionally be called more
                    # than once with identical arguments (for example an image
                    # generator that chooses a fresh seed internally).  Keep
                    # each model-issued call distinct while preserving resume
                    # protection for the same call id.
                    invocation_id=invocation_id,
                    replay=tool.replay if tool is not None else "never",
                )
                step_id = claim.step_id
                if claim.action == "cached" and claim.output is not None:
                    cached = dict(claim.output)
                    cached["id"] = tc["id"]
                    cached["cached"] = True
                    cached["persistent_cache"] = True
                    return cached
                if claim.action == "uncertain":
                    error = (
                        f"[ToolRecoveryBlocked] {name} started before the previous process ended, "
                        "so its side effects are unknown and it will not be replayed automatically."
                    )
                    return {
                        "type": "tool_result", "id": tc["id"], "name": name, "args": durable_args,
                        "output": "", "error": error, "code": args.get("code", args.get("command", "")),
                        "tool_output": error, "duration_ms": 0, "recovery_blocked": True,
                    }
            except Exception as exc:
                logger.exception("task store tool claim failed task_id=%s name=%s", task_id, name)
                safe_without_durable_claim = bool(
                    tool is not None
                    and tool.risk == "read"
                    and tool.idempotent
                )
                if not safe_without_durable_claim:
                    error = (
                        f"[ToolClaimUnavailable] `{name}` was not executed because Astra could "
                        "not durably confirm exclusive execution rights. This prevents duplicate "
                        "or unknown side effects while the task store is unavailable."
                    )
                    return {
                        "type": "tool_result",
                        "id": tc["id"],
                        "name": name,
                        "args": durable_args,
                        "output": "",
                        "error": error,
                        "code": "tool_claim_unavailable",
                        "tool_output": error,
                        "duration_ms": 0,
                        "error_type": "tool_claim_unavailable",
                        "recoverable": True,
                        "retryable": False,
                        "recovery_blocked": True,
                        "recovery_hint": (
                            "Check task-store health and the persisted task step before retrying. "
                            "If a claim was committed before the failure, confirm that the prior "
                            "tool attempt did not run or compensate its effects first."
                        ),
                        "details": {
                            "exception_type": type(exc).__name__,
                            "risk": risk or "unknown",
                            "idempotent": bool(tool is not None and tool.idempotent),
                        },
                    }
                logger.warning(
                    "continuing explicitly idempotent read without durable claim "
                    "task_id=%s name=%s",
                    task_id,
                    name,
                )

        logger.info("tool call start name=%s id=%s", name, tc.get("id", ""))
        tool_trace_ctx = trace_ctx.with_tool_call(str(tc.get("id") or "")) if trace_ctx else None
        with trace_span("agent.react.tool", {"agent.tool.name": name}, ctx=tool_trace_ctx) as tool_span:
            try:
                result = await self.tools.execute(
                    name,
                    args,
                    sandbox=None,
                    on_progress=report,
                    call_id=str(tc.get("id") or ""),
                    task_id=task_id or "",
                    execution_origin="model",
                )
            except (asyncio.CancelledError, TurnBudgetExceeded):
                # A cancelled run_code request may have already claimed a
                # durable step. Mark it unknown so a later resume cannot
                # mistake an interrupted side effect for a completed call.
                if step_id and self.task_store is not None:
                    try:
                        await durable_io(
                            self.task_store.finish_step,
                            step_id,
                            status="unknown",
                            output={"cancelled": True, "name": name},
                            error="Tool execution cancelled before completion could be confirmed",
                        )
                    except Exception:
                        logger.exception("task store cancellation finish failed task_id=%s name=%s", task_id, name)
                raise
            tool_phase("postprocessing")
            if not result.get("error") and name == "skill_manage":
                self._refresh_skill_catalog(force=True)
            if not result.get("error") and name == "skill_view":
                skill_name = str(args.get("name") or "").strip().lower()
                if skill_name in self._available_skill_names:
                    self._active_skill_names.add(skill_name)
            if not result.get("error"):
                self._task_context_paths.update(self._tool_mutation_paths(name, args))
                for key in ("directory", "root"):
                    value = args.get(key)
                    if isinstance(value, str) and value.strip():
                        self._task_context_paths.add(self._normalize_workflow_path(value))
            dynamic_context = result.pop("trace_context", {})
            if isinstance(dynamic_context, dict):
                browser_session_id = str(dynamic_context.get("browser_session_id") or "")
                memory_id = str(dynamic_context.get("memory_id") or "")
                if trace_ctx is not None:
                    if browser_session_id:
                        trace_ctx.browser_session_id = browser_session_id
                    if memory_id:
                        trace_ctx.memory_id = memory_id
                if tool_span is not None:
                    if browser_session_id:
                        tool_span.set_attribute("agent.browser_session_id", browser_session_id)
                    if memory_id:
                        tool_span.set_attribute("agent.memory_id", memory_id)
        logger.info(
            "tool call finish name=%s id=%s error=%s duration_ms=%s",
            name, tc.get("id", ""), bool(result.get("error")), result.get("duration_ms"),
        )
        mutation_paths: set[str] = set()
        if tracking_before is not None:
            tracking_after = await self._git_worktree_snapshot()
            if tracking_after is None:
                result["mutation_tracking_warning"] = (
                    "Git worktree tracking succeeded before execution but failed afterward; "
                    "inspect git status before making further source changes."
                )
            else:
                changed_paths = self._changed_snapshot_paths(tracking_before, tracking_after)
                mutation_paths.update(changed_paths)
                turn_store = current_turn_change_store()
                if turn_store is not None and changed_paths:
                    try:
                        for path in changed_paths:
                            turn_store.note_tracked(
                                path, tracking_before.get(path), tracking_after.get(path)
                            )
                    except Exception:
                        logger.exception("turn-change tracked note failed")
        dry_run_write = (
            name == "apply_patch"
            and (
                bool(args.get("dry_run"))
                or '"status": "dry_run"' in str(result.get("output") or "")
            )
        )
        if (
            not result.get("error")
            and tool is not None
            and tool.risk == "write"
            and not dry_run_write
        ):
            write_paths = self._tool_mutation_paths(name, args)
            mutation_paths.update(write_paths)
            turn_store = current_turn_change_store()
            if turn_store is not None and write_paths:
                try:
                    turn_store.note_paths(self._ledger_identity_paths(self._tool_raw_paths(name, args)))
                except Exception:
                    logger.exception("turn-change candidate note failed")
        if mutation_paths:
            self._task_mutated_paths.update(mutation_paths)
            # Tracked execute commands may fail after partially changing the
            # worktree. Journal and verify those changes regardless of exit code.
            if any(self._is_source_path(path) for path in mutation_paths):
                self._verification_required = True
        # Preserve concrete coding evidence independently of the final
        # completion verdict, so resumes and handoffs retain what changed and
        # what was actually checked.
        if task_id and self.task_store is not None and mutation_paths:
            try:
                prior = (await durable_io(self.task_store.get_task, task_id)) or {}
                contract = prior.get("verification") or {}
                if not contract.get("required"):
                    contract = new_contract(
                        paths=self._task_mutated_paths,
                        workdir=self._source_tracking_workdir(),
                    )
                else:
                    contract = dict(contract)
                    contract["status"] = "pending"
                    contract["mutated_paths"] = sorted(
                        set(contract.get("mutated_paths") or ()) | self._task_mutated_paths
                    )
                await durable_io(self.task_store.record_verification_contract, task_id, contract)
            except Exception:
                logger.exception("could not persist coding verification contract task_id=%s", task_id)
        execution = result.get("execution") or {}
        if (task_id and self.task_store is not None and self._verification_required
                and tool is not None and (tool.risk == "execute" or execution)):
            try:
                prior = (await durable_io(self.task_store.get_task, task_id)) or {}
                contract = prior.get("verification") or new_contract(
                    paths=self._task_mutated_paths,
                    workdir=self._source_tracking_workdir(),
                )
                command = str(args.get("command") or args.get("code") or name)
                await durable_io(
                    self.task_store.record_verification_contract,
                    task_id,
                    append_check(
                        contract,
                        tool=name,
                        command=command,
                        execution=execution,
                        error=str(result.get("error") or ""),
                        output=str(result.get("output") or ""),
                    ),
                )
            except Exception:
                logger.exception("could not record verification evidence task_id=%s", task_id)
        output = result.get("output", "")
        error = result.get("error", "")
        code = result.get("code") or args.get("code", args.get("command", ""))
        tool_output = output or error or "(tool returned empty)"
        event = {
            "type": "tool_result",
            "id": tc["id"],
            "name": name,
            "args": durable_args,
            "output": output,
            "error": error,
            "code": code,
            "tool_output": tool_output,
            "duration_ms": result.get("duration_ms"),
        }
        if execution:
            event["tool_output"] += "\n[Execution receipt: " + json.dumps(execution, ensure_ascii=False) + "]"
        if mutation_paths:
            journal = sorted(self._task_mutated_paths)
            event["coding_change_journal"] = journal
            event["tool_output"] = (
                f"{event['tool_output']}\n\n"
                "[Coding change journal: this task observed mutations to "
                f"{', '.join(journal)}. Verify these paths and do not claim pre-existing diff.]"
            )
        for key in (
            "output_truncated",
            "artifact_path",
            "artifact_chars",
            "artifact_bytes",
            "error_type",
            "recoverable",
            "retryable",
            "recovery_hint",
            "partial",
            "details",
            "artifact_ref",
            "mutation_tracking_warning",
            "execution",
            "request_local_placeholder",
            "_private_result",
        ):
            if key in result:
                event[key] = result[key]
        if name == "computer_act" and "computer_receipt" in result:
            from .computer_forms import safe_action_receipt
            event["computer_receipt"] = safe_action_receipt(result["computer_receipt"])
        if result.get("fresh_output") is not None:
            event["_fresh_output"] = result["fresh_output"]
        event = self._separate_vision_tile_attachment(event)
        event = self._separate_request_local_image_attachment(event)
        if step_id and self.task_store is not None:
            try:
                tool_phase("persisting_result")
                await durable_io(
                    self.task_store.finish_step,
                    step_id,
                    status="failed" if error else "completed",
                    output=self._public_tool_event(event),
                    error=error,
                )
            except Exception:
                logger.exception("task store tool finish failed task_id=%s name=%s", task_id, name)
        return event

    async def _execute_tool_calls(
        self,
        tool_calls: list[dict],
        turn_tool_cache: dict[str, dict] | None = None,
        task_id: str | None = None,
        active_groups: set[str] | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        trace_ctx: TraceContext | None = None,
        result_callback: Callable[[dict], None] | None = None,
    ) -> list[dict]:
        semaphore = asyncio.Semaphore(self.tool_concurrency)
        turn_tool_cache = turn_tool_cache if turn_tool_cache is not None else {}

        async def run_one(tc: dict) -> dict:
            check_work_budget()
            if progress_callback is not None:
                progress_callback({
                    "type": "tool_progress",
                    "id": str(tc.get("id") or ""),
                    "name": str(tc.get("name") or "tool"),
                    "stage": "queued",
                    "status": "queued",
                })
            name = str(tc.get("name") or "")
            if name == "read_image_tiles" and not self._vision_tile_tool_enabled():
                error = (
                    "[VisionTileSelectionUnavailable] read_image_tiles is unavailable "
                    "because the active model policy or vision-tiles preference does not authorize it."
                )
                return {
                    "type": "tool_result",
                    "id": tc.get("id", ""),
                    "name": name,
                    "args": {},
                    "output": "",
                    "error": error,
                    "code": "vision_tile_selection_unavailable",
                    "tool_output": error,
                    "duration_ms": 0,
                    "error_type": "vision_tile_selection_unavailable",
                    "recoverable": False,
                    "retryable": False,
                }
            tool_def = self.tools.get(name)
            hidden_from_work = (
                self.tool_allowlist is None
                and tool_def is not None
                and not tool_def.expose_by_default
                and not tool_def.allow_hidden_execution
            )
            if hidden_from_work or (self.tool_allowlist is not None and name not in self.tool_allowlist):
                error = f"[ToolDisabled] Tool '{name}' is not available in the current mode."
                return {
                    "type": "tool_result", "id": tc.get("id", ""), "name": name or "tool",
                    "args": {}, "output": "", "error": error, "code": "",
                    "tool_output": error, "duration_ms": 0,
                }
            if tc.get("name") == self.tools.ACTIVATE_GROUP_TOOL:
                try:
                    args = self._parse_tool_args(tc.get("arguments", ""))
                    group = str(args.get("group", ""))
                    if group not in self.tools.groups or group == "core":
                        raise ToolArgumentError(f"unknown tool group: {group}")
                    if active_groups is not None:
                        active_groups.add(group)
                    output = f"Activated tool group '{group}': {', '.join(self.tools.tool_names_for_group(group))}"
                    return {
                        "type": "tool_result", "id": tc["id"], "name": self.tools.ACTIVATE_GROUP_TOOL,
                        "args": args, "output": output, "error": "", "code": "",
                        "tool_output": output, "duration_ms": 0,
                    }
                except ToolArgumentError as exc:
                    error = f"[ToolInputError] {self.tools.ACTIVATE_GROUP_TOOL}: {exc}"
                    return {
                        "type": "tool_result", "id": tc["id"], "name": self.tools.ACTIVATE_GROUP_TOOL,
                        "args": {}, "output": "", "error": error, "code": "",
                        "tool_output": error, "duration_ms": 0,
                    }
            signature = self._tool_signature(tc)
            tool = self.tools.get(str(tc.get("name") or ""))
            cacheable = bool(
                tool is not None
                and tool.idempotent
                and tool.cache_results
            )
            cached = turn_tool_cache.get(signature) if cacheable else None
            if cached is not None:
                if progress_callback is not None:
                    progress_callback({
                        "type": "tool_progress",
                        "id": str(tc.get("id") or ""),
                        "name": str(tc.get("name") or "tool"),
                        "stage": "cached",
                        "status": "completed",
                    })
                event = dict(cached)
                event["id"] = tc["id"]
                event["cached"] = True
                return event
            async with semaphore:
                check_work_budget()
                event = await self._execute_tool_call(
                    tc,
                    task_id=task_id,
                    progress_callback=progress_callback,
                    trace_ctx=trace_ctx,
                )
                if cacheable and not event.get("error"):
                    turn_tool_cache[signature] = dict(event)
                return event

        profiler = current_profiler()
        queued_at = _time.perf_counter() if profiler is not None else 0.0

        async def run_and_publish(tc: dict) -> dict:
            async def execute() -> dict:
                event = await run_one(tc)
                if result_callback is not None:
                    result_callback(self._public_tool_event(event))
                return event

            if profiler is None:
                return await execute()
            timing = ToolTiming(profiler, queued_at=queued_at, identity=f"{task_id or ''}:{tc.get('id', '')}")
            with timing.active():
                event = await execute()
                timing.outcome = "failed" if event.get("error") else "cached" if event.get("cached") else "completed"
                return event

        from .tool_scheduler import execute_ordered

        def can_overlap(tc: dict) -> bool:
            # Preserve existing read eligibility and explicit network opt-in.
            # A registry refresh can change an unstarted call into a barrier.
            tool = self.tools.get(str(tc.get("name") or ""))
            return tool is not None and (tool.risk == "read" or tool.parallel_safe)

        events = await execute_ordered(
            tool_calls, run_and_publish, can_overlap, self.tool_concurrency,
        )
        for tc, event in zip(tool_calls, events):
            tool_def = self.tools.get(str(tc.get("name") or ""))
            if tool_def is not None and tool_def.return_direct and not event.get("error"):
                context_result = "[Atomic interaction committed successfully.]"
            else:
                context_result = self._tool_result_context(event)
            self.context.add_tool(tc["id"], context_result)
            fresh_output = event.pop("_fresh_output", None)
            if fresh_output is not None:
                fresh_event = dict(event)
                fresh_event["tool_output"] = fresh_output
                fresh_event["output_truncated"] = False
                fresh_event["request_local_placeholder"] = False
                self._fresh_tool_context[tc["id"]] = self._tool_result_context(fresh_event)
                if tool_def is not None and tool_def.result_persistence == "request_local":
                    self._request_local_tool_context_ids.add(tc["id"])
        return events

    @staticmethod
    def _separate_vision_tile_attachment(event: dict) -> dict:
        """Keep attachment paths private while exposing bounded selection facts."""
        if event.get("name") != "read_image_tiles" or event.get("error"):
            return event
        try:
            payload = json.loads(str(event.get("output") or ""))
        except (TypeError, json.JSONDecodeError):
            return event
        if not isinstance(payload, dict) or payload.get("type") != "image_attachment":
            return event
        private = event.get("_private_result")
        data_urls = private.get("image_data_urls") if isinstance(private, dict) else None
        if not isinstance(data_urls, list) or not all(isinstance(url, str) for url in data_urls):
            return event
        public_payload = {
            "success": bool(payload.get("success")),
            "type": "vision_tile_selection",
            "image_labels": list(payload.get("image_labels") or []),
            "unserved_ids": list(payload.get("unserved_ids") or []),
            "remaining_images": payload.get("remaining_images"),
            "remaining_inline_bytes": payload.get("remaining_inline_bytes"),
            "message": (
                "Selected image tiles were attached in the following user message; "
                "internal cache locations are intentionally hidden."
            ),
        }
        public_output = json.dumps(public_payload, ensure_ascii=False)
        separated = dict(event)
        separated["output"] = public_output
        separated["tool_output"] = public_output
        separated.pop("_private_result", None)
        separated["_vision_tile_attachment"] = {
            **payload,
            "image_data_urls": list(data_urls),
        }
        return separated

    @staticmethod
    def _separate_request_local_image_attachment(event: dict) -> dict:
        """Keep verified request-local image bytes out of public tool events."""
        if event.get("error") or not event.get("request_local_placeholder"):
            return event
        private = event.get("_private_result")
        data_urls = private.get("image_data_urls") if isinstance(private, dict) else None
        if not isinstance(data_urls, list) or not data_urls or not all(
            _is_request_local_image_data_url(url) for url in data_urls
        ):
            return event
        try:
            payload = json.loads(str(event.get("_fresh_output") or ""))
        except (TypeError, json.JSONDecodeError):
            return event
        if not isinstance(payload, dict) or payload.get("type") != "image_attachment":
            return event
        separated = dict(event)
        separated.pop("_private_result", None)
        separated["_request_local_image_attachment"] = {
            "type": "image_attachment",
            "detail": payload.get("detail", "original"),
            "image_labels": list(payload.get("image_labels") or []),
            "image_data_urls": list(data_urls),
        }
        return separated

    @staticmethod
    def _public_tool_event(event: dict) -> dict:
        """Return an event safe for durable stores and external consumers."""
        return {
            key: value
            for key, value in event.items()
            if not str(key).startswith("_")
        }

    @staticmethod
    def _tool_result_context(event: dict) -> str:
        """Make a completed tool result salient to the next model iteration.

        Some OpenAI-compatible models otherwise continue the assistant text
        emitted before the tool call, especially when the result is a large
        truncated preview.  Keep the result bounded, but make its status and
        relationship to the active request explicit inside the tool message.
        """
        name = str(event.get("name") or "tool")
        error = str(event.get("error") or "")
        status = "error" if error else "success"
        execution = event.get("execution") or {}
        if execution and not error:
            status = str(execution.get("status") or "unknown")
            if status == "completed":
                status = "success" if execution.get("exit_code") == 0 else "failed"
        output = str(event.get("tool_output") or "(tool returned empty)")
        if name == "read_image_tiles" and not error:
            try:
                payload = json.loads(str(event.get("output") or ""))
            except (TypeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("type") == "image_attachment":
                output = json.dumps(
                    {
                        "success": bool(payload.get("success")),
                        "type": "vision_tile_selection",
                        "image_labels": list(payload.get("image_labels") or []),
                        "unserved_ids": list(payload.get("unserved_ids") or []),
                        "remaining_images": payload.get("remaining_images"),
                        "remaining_inline_bytes": payload.get("remaining_inline_bytes"),
                        "message": (
                            "Selected image tiles were attached in the following user message; "
                            "internal cache locations are intentionally hidden."
                        ),
                    },
                    ensure_ascii=False,
                )
        lines = [f"[Tool result: {name} | status: {status}]"]
        if name == "computer_act" and isinstance(event.get("computer_receipt"), dict):
            from .computer_feedback import receipt_instruction
            from .computer_forms import safe_action_receipt
            receipt = safe_action_receipt(event["computer_receipt"])
            lines.append("Computer receipt: " + json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
            instruction = receipt_instruction(receipt)
            if instruction:
                lines.append("Next step: " + instruction)
        lines.append(output)
        if error:
            code = str(event.get("code") or event.get("error_type") or "").strip()
            if code:
                lines.append(f"Error code: {code}")
            if "retryable" in event:
                lines.append(
                    f"Retryable: {'yes' if bool(event.get('retryable')) else 'no'}"
                )
            recovery_hint = str(event.get("recovery_hint") or "").strip()
            if recovery_hint:
                lines.append(f"Recovery: {recovery_hint}")
            details = event.get("details")
            if isinstance(details, dict) and details:
                details_text = json.dumps(details, ensure_ascii=False, sort_keys=True)
                if len(details_text) > 1600:
                    details_text = details_text[:1597] + "..."
                lines.append(f"Details: {details_text}")
        if event.get("request_local_placeholder"):
            lines.append("Result lifecycle: request-local content is no longer retained.")
        elif event.get("output_truncated"):
            artifact_path = str(event.get("artifact_path") or "").strip()
            if artifact_path:
                lines.append(
                    "This is a bounded preview. If omitted details are needed, "
                    f"inspect the complete result at: {artifact_path}"
                )
        else:
            # Give weaker local models an unambiguous positive signal. Avoid
            # mentioning UI folding here: negated wording about truncation can
            # itself prime a model to assume that content is missing.
            lines.append("Result completeness: complete.")
        lines.append(
            "[End tool result. Continue the current user request from this result; "
            "do not restart from assumptions made before the tool call.]"
        )
        return "\n".join(lines)

    def _clear_request_local_tool_context(self) -> None:
        """Discard full results after their one permitted provider request."""
        request_local_ids = getattr(self, "_request_local_tool_context_ids", None)
        if not request_local_ids:
            return
        fresh_context = getattr(self, "_fresh_tool_context", None)
        if fresh_context is not None:
            for call_id in request_local_ids:
                fresh_context.pop(call_id, None)
        request_local_ids.clear()

    def _clear_request_local_image_overlays(self) -> None:
        """Remove computer screenshots after their one provider request."""
        messages = self._request_local_image_overlay_messages
        if not messages:
            return
        identities = {id(message) for message in messages}
        self._active_vision_prompt_overlays = [
            overlay
            for overlay in self._active_vision_prompt_overlays
            if id(overlay[0]) not in identities
        ]
        messages.clear()

    async def _run_react_loop(self, msg: Msg, emit_events: bool = True):
        request_id = self._vision_request_id(msg)
        context_index_request_id = f"context-index:{uuid.uuid4().hex}"
        vision_request_id = uuid.uuid4().hex
        session_id = self._vision_session_id()
        if self._active_vision_request_id is not None:
            raise RuntimeError("A vision request is already active for this agent.")
        self._active_vision_request_id = vision_request_id
        self._active_vision_storage_replacements = []
        self._active_vision_prompt_overlays = []
        self._active_vision_attachment_occurrences = set()
        self._request_local_image_overlay_messages = []
        self._context_index_request_id = context_index_request_id
        from .command_workflows import workflow_allowlist
        previous_allowlist = getattr(self, "tool_allowlist", None)
        self.tool_allowlist = workflow_allowlist(str(msg.metadata.get("command_workflow", "")), previous_allowlist)
        try:
            try:
                active_events = self._run_react_loop_active(msg, emit_events)
                try:
                    async for event in active_events:
                        yield event
                finally:
                    await active_events.aclose()
            except VisionPreprocessError as exc:
                logger.warning(
                    "vision preprocessing failed session=%s request=%s",
                    session_id,
                    request_id,
                )
                yield {
                    "type": "error",
                    "message": (
                        "Vision preprocessing failed; the local image was not sent "
                        f"to the provider. {exc}"
                    ),
                    "code": "vision_preprocess_failed",
                    "retryable": True,
                    "recoverable": True,
                    "request_id": request_id,
                }
                yield {"type": "done", "content": "", "request_id": request_id}
        finally:
            self.tool_allowlist = previous_allowlist
            context_index_broker = getattr(self, "context_index_broker", None)
            if context_index_broker is not None:
                try:
                    context_index_broker.complete_request(context_index_request_id)
                except Exception:
                    logger.exception(
                        "context index request cleanup failed request=%s",
                        context_index_request_id,
                    )
            if self._context_index_request_id == context_index_request_id:
                self._context_index_request_id = ""
            self._clear_request_local_tool_context()
            self._clear_request_local_image_overlays()
            replacements = list(self._active_vision_storage_replacements)
            try:
                for message_index, storage_content in replacements:
                    self.context.replace_message_content(message_index, storage_content)
                if replacements:
                    await self.context.save_async()
            finally:
                try:
                    self.vision_preprocessor.release_request(session_id, vision_request_id)
                except Exception:
                    logger.exception(
                        "vision request release failed session=%s request=%s",
                        session_id,
                        request_id,
                    )
                finally:
                    self._active_vision_request_id = None
                    self._active_vision_storage_replacements = []
                    self._active_vision_prompt_overlays = []
                    self._active_vision_attachment_occurrences = set()
                    self._request_local_image_overlay_messages = []

    async def _run_react_loop_active(self, msg: Msg, emit_events: bool = True):
        request_id = self._vision_request_id(msg)
        task_id = msg.metadata.get("task_id")
        # Provider-facing metadata can intentionally reuse a Msg id.  The
        # frozen prompt sidecar and Context Index handle registry instead need
        # a fresh key for every physical generator invocation.
        self._memory_turn_key = (
            self._context_index_request_id
            or f"context-index:{uuid.uuid4().hex}"
        )

        # Build trace correlation context for this turn
        session_id = self._vision_session_id()
        trace_ctx = TraceContext(
            session_id=session_id,
            task_id=str(task_id or ""),
            request_id=str(request_id),
        )

        def with_request(event: dict) -> dict:
            event["request_id"] = request_id
            return event

        _turn_prepare_started = _time.perf_counter()
        has_content, user_index, storage_content, vision_event = await self._prepare_turn(msg)
        _turn_prepare_ms = (_time.perf_counter() - _turn_prepare_started) * 1000
        if not has_content:
            yield with_request({"type": "done", "content": ""})
            return
        if emit_events and vision_event is not None:
            yield with_request(vision_event)

        step = 0
        last_text = ""
        user_text = msg.get_text()
        tool_signature_counts: dict[str, int] = {}
        tool_name_counts: dict[str, int] = {}
        turn_tool_cache: dict[str, dict] = {}
        completed_tool_events: list[dict] = []
        auto_image_messages: list[tuple[int, object]] = []
        active_groups = {"core"}
        blocked_tools: set[str] = set()
        # Per tool, track only consecutive failures of the exact same
        # normalized invocation. Different arguments are diagnostic progress,
        # not a retry loop, even when their wrapper errors look identical.
        failure_state: dict[str, tuple[str, int]] = {}
        failed_tool_signatures: set[str] = set()
        tool_recovery_counts: dict[str, int] = {}
        tool_recovery_total = 0
        recovery_overlay = ""
        pending_tool_recovery = False
        recovery_tool_name = ""
        recovery_call_id = ""
        prefill_recovery: dict[str, str] | None = None
        stop_reason = ""
        hard_prompt_stop = False
        completed_normally = False
        repeated_tool_limit = 2
        forced_tool_failures = 0
        mailbox_iteration_extension = 0
        overflow_recoveries = 0
        while (
            self.max_iterations == 0
            or step < self.max_iterations + mailbox_iteration_extension
        ):
            check_work_budget()
            step += 1
            background_delegates_running = False
            if self.delegate_mailbox is not None and task_id:
                drain_for = getattr(self.delegate_mailbox, "drain_for", None)
                envelopes = (
                    self.delegate_mailbox.drain_for(str(task_id), session_id)
                    if callable(drain_for)
                    else self.delegate_mailbox.drain(str(task_id))
                )
                for envelope in envelopes:
                    self.context.add_user(envelope, provenance="delegate")
                background_delegates_running = self.delegate_mailbox.has_running(
                    str(task_id)
                )
            current_prefill = prefill_recovery
            prefill_recovery = None
            full_content = ""
            full_reasoning = ""
            provider_state = None
            tool_calls = None
            usage = None
            finish_reason = ""
            estimate_tokens = getattr(self.llm, "estimate_tokens", None)
            transient_messages: list[dict] = []
            if self._steering:
                steering_texts = list(self._steering)
                self._steering.clear()
                for steering_text in steering_texts:
                    transient_messages.append({
                        "role": "system",
                        "content": (
                            "[USER STEERING — mid-run correction, authoritative]\n"
                            f"{steering_text}\n"
                            "[END USER STEERING]"
                        ),
                    })
            if recovery_overlay:
                transient_messages.append({
                    "role": "system",
                    "content": recovery_overlay,
                })
                recovery_overlay = ""
            if background_delegates_running:
                transient_messages.append({
                    "role": "system",
                    "content": (
                        "Background subagents are still running. Continue all independent "
                        "foreground analysis and tool work now; do not stop merely because "
                        "they are running. Their mailbox results will be joined automatically "
                        "when you are otherwise ready to give the final answer."
                    ),
                })
            if current_prefill is not None:
                transient_messages.extend([
                    {
                        "role": "system",
                        "content": (
                            "Continue the final assistant prefill from the exact next character. "
                            "Complete only the unfinished JSON object. Do not repeat the prefix, "
                            "add Markdown fences, commentary, or start another tool call."
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": current_prefill["arguments"],
                    },
                ])
            _t_prep_start = _time.perf_counter()
            profiler_kwargs = {
                "session_id": session_id,
                "request_id": str(request_id),
                "step": step,
                "model": str(
                    getattr(getattr(self.llm, "config", None), "model", "unknown")
                ),
            }
            if self._query_profile_enabled is None:
                profiler = QueryProfiler.from_env(
                    **profiler_kwargs,
                    root=self._source_tracking_workdir(),
                )
            elif self._query_profile_enabled:
                profiler = QueryProfiler(
                    enabled=True,
                    **profiler_kwargs,
                    root=self._source_tracking_workdir(),
                )
            else:
                # Audit/minimal callers can disable profiling without resolving
                # an untrusted or replaceable source-tracking pathname.
                profiler = QueryProfiler(
                    enabled=False,
                    **profiler_kwargs,
                    root=Path.cwd(),
                )
            if step == 1:
                profiler.observe_ms("turn_prepare", _turn_prepare_ms)
            prompt, prompt_estimate = await self._prepare_prompt_for_llm(
                user_text,
                estimate_tokens,
                trace_ctx=trace_ctx,
                transient_messages=transient_messages,
                current_user_index=user_index,
                profiler=profiler,
            )
            _t_prep_done = _time.perf_counter() - _t_prep_start
            if current_prefill is not None:
                # Prefill continuation returns plain assistant text which is
                # reconstructed into the original tool call below.
                available_tools = []
            else:
                with profiler.phase("tool_schema_selection"):
                    available_tools = self._available_tool_schemas(
                        self._tool_routing_text(user_text),
                        active_groups,
                        tool_name_counts,
                        blocked_tools,
                    )
            prompt, budget_estimate = await self._fit_appshot_request_budget(
                prompt, available_tools, user_text=user_text,
                transient_messages=transient_messages, profiler=profiler,
            )
            if budget_estimate is not None:
                prompt_estimate = budget_estimate
            # Forced compaction can happen even when no token estimator is
            # available. Never retain a pre-compaction message index.
            user_index = next((i for i in range(len(self.context.messages) - 1, -1, -1)
                               if self.context.messages[i].get("role") == "user"), None)
            if msg.metadata.get("appshot_budget_required"):
                from agent.cli.appshot_admission import appshot_prompt_limit, request_token_bound
                from agent.cli.appshots import AppshotValidationError
                try:
                    limit = appshot_prompt_limit(self.context, self.llm.config, self._generation_overrides())
                    if request_token_bound(prompt, available_tools, self.llm.config) >= limit:
                        raise AppshotValidationError("context_budget_exceeded")
                except AppshotValidationError as exc:
                    yield with_request({"type": "error", "message": str(exc), "recoverable": True})
                    hard_prompt_stop = True
                    break
            profiler.set_request(
                prompt,
                available_tools,
                getattr(self, "_active_skill_names", set()),
            )
            forced_tool = self.forced_tool_name
            forced_choice = None
            omit_forced_choice = False
            if forced_tool:
                available_names = {
                    str(schema.get("function", {}).get("name") or "")
                    for schema in available_tools
                }
                if forced_tool not in available_names:
                    stop_reason = f"强制交互工具 {forced_tool} 当前不可用"
                    yield with_request({
                        "type": "error",
                        "message": f"Stopping: required interaction tool is unavailable ({forced_tool}).",
                        "recoverable": True,
                    })
                    break
                supports_choice = getattr(self.llm, "supports_forced_tool_choice", None)
                can_force_choice = True if supports_choice is None else bool(supports_choice())
                if can_force_choice:
                    forced_choice = {"type": "function", "function": {"name": forced_tool}}
                else:
                    # DeepSeek V4 thinking mode accepts tools but rejects the
                    # tool_choice parameter. Keep only the atomic tool exposed,
                    # omit that parameter, then validate the returned call.
                    omit_forced_choice = True

            llm_step_id: str | None = None
            if task_id and self.task_store is not None:
                try:
                    task = (await durable_io(self.task_store.get_task, task_id)) or {}
                    resume_count = int(task.get("resume_count", 0))
                    llm_step = await durable_io(
                        self.task_store.start_step,
                        task_id,
                        f"llm:{resume_count}:{step}",
                        "llm",
                        name=self.llm.config.model,
                        input_value={"iteration": step, "prompt_messages": len(prompt)},
                    )
                    llm_step_id = llm_step["id"]
                except Exception:
                    logger.exception("task store LLM start failed task_id=%s", task_id)
            llm_span_scope = None
            try:
                llm_trace_attributes = {
                    "agent.llm.model": str(
                        getattr(getattr(self.llm, "config", None), "model", "unknown")
                    ),
                    "agent.react.iteration": step,
                    "agent.llm.prompt_messages": len(prompt),
                }
                llm_span_scope = trace_span(
                    "agent.react.llm",
                    llm_trace_attributes,
                    ctx=trace_ctx,
                )
                llm_span_scope.__enter__()
                if not forced_tool:
                    stream = self._llm_stream(messages=prompt, tools=available_tools, profiler=profiler)
                elif omit_forced_choice:
                    stream = self._llm_stream(
                        messages=prompt,
                        tools=available_tools,
                        omit_tool_choice=True,
                        profiler=profiler,
                    )
                else:
                    stream = self._llm_stream(
                        messages=prompt,
                        tools=available_tools,
                        tool_choice=forced_choice,
                        profiler=profiler,
                    )
                _t_llm_start = _time.perf_counter()
                profiler.request_sent()
                _ttft_logged = False
                try:
                    async for event in stream:
                        if event["type"] not in {"generation_progress", "generation_stats"}:
                            profiler.first_event(event["type"])
                        if not _ttft_logged and event["type"] not in {"generation_progress", "generation_stats"}:
                            _ttft_logged = True
                            _t_now = _time.perf_counter()
                            _t_prep_ms = _t_prep_done * 1000
                            _t_ttft_ms = (_t_now - _t_llm_start) * 1000
                            _t_total_ms = (_t_now - _t_prep_start) * 1000
                            _timing_line = (
                                f"[TIMING] ts={_time.strftime('%H:%M:%S')} step={step} "
                                f"prompt_prep={_t_prep_ms:.0f}ms llm_ttft={_t_ttft_ms:.0f}ms "
                                f"total={_t_total_ms:.0f}ms prompt_msgs={len(prompt)}"
                            )
                            logger.info(_timing_line)
                            # Persist only slow calls to keep timing.log small;
                            # set TIMING_LOG_ALL=1 to record every call.
                            if self._timing_log_enabled and (
                                _t_ttft_ms > 3000
                                or _t_total_ms > 5000
                                or _t_prep_ms > 500
                                or os.getenv("TIMING_LOG_ALL", "").strip()
                            ):
                                try:
                                    _tf = state_path("timing.log")
                                    _tf.parent.mkdir(parents=True, exist_ok=True)
                                    with open(_tf, "a", encoding="utf-8") as _f:
                                        _f.write(_timing_line + "\n")
                                except Exception:
                                    pass
                        if event["type"] == "reasoning":
                            full_reasoning += event["content"]
                            if (
                                emit_events
                                and self.context.show_reasoning
                            ):
                                yield with_request({"type": "reasoning", "content": event["content"]})
                        elif event["type"] == "chunk":
                            full_content += event["content"]
                            # Atomic modes do not expose prose until the matching
                            # state transaction has committed successfully.
                            if (
                                emit_events
                                and not forced_tool
                                and current_prefill is None
                            ):
                                yield with_request({"type": "chunk", "content": event["content"]})
                        elif event["type"] == "tool_calls":
                            tool_calls = event["calls"]
                            provider_state = event.get("_provider_state")
                            full_content = event.get("content", full_content)
                            full_reasoning = event.get("reasoning_content", full_reasoning)
                            finish_reason = str(
                                event.get("finish_reason") or event.get("stop_reason") or finish_reason
                            )
                            usage = event.get("usage")
                        elif event["type"] == "done":
                            provider_state = event.get("_provider_state")
                            full_content = event.get("content", full_content)
                            full_reasoning = event.get("reasoning_content", full_reasoning)
                            finish_reason = str(
                                event.get("finish_reason") or event.get("stop_reason") or finish_reason
                            )
                            usage = event.get("usage")
                        elif event["type"] == "generation_recovery":
                            full_content = full_reasoning = ""
                            if emit_events:
                                yield with_request({**event, "type": "error", "recoverable": True})
                        elif event["type"] in {"generation_stats", "generation_progress"} and emit_events:
                            yield with_request(event)
                finally:
                    await stream.aclose()
            except BaseException as exc:
                # Profiling is intentionally content-safe: provider exception
                # messages may echo prompt fragments, so retain only the type.
                await profiler.finish_async(usage, error=type(exc).__name__)
                if llm_span_scope is not None:
                    llm_span_scope.__exit__(type(exc), exc, exc.__traceback__)
                    llm_span_scope = None
                if llm_step_id and self.task_store is not None:
                    await durable_io(
                        self.task_store.finish_step,
                        llm_step_id,
                        status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                        error=str(exc) or type(exc).__name__,
                    )
                if (
                    isinstance(exc, Exception)
                    and not full_content
                    and not tool_calls
                    and overflow_recoveries < 2
                    and self._is_context_overflow_error(exc)
                ):
                    # The provider rejected the request as too long for its
                    # window. Local estimates can under-count tool schemas or
                    # multimodal overhead, so recover the way Codex/OpenCode
                    # do: force-compact once and retry the same step before
                    # surfacing the error. Retries require an empty stream so
                    # far and a compaction that actually shrank the history,
                    # which keeps this from looping.
                    overflow_recoveries += 1
                    before_messages = len(self.context.messages)
                    before_tokens = self.context.estimate_prompt_tokens()
                    try:
                        await self.context.compress_if_needed(force=True)
                    except Exception:
                        logger.exception("forced compaction after provider overflow failed")
                    after_tokens = self.context.estimate_prompt_tokens()
                    after_messages = len(self.context.messages)
                    if after_tokens < before_tokens or after_messages < before_messages:
                        logger.warning(
                            "Provider context overflow at step %d; forced compaction "
                            "%d -> %d tokens (%d -> %d messages), retrying the step",
                            step,
                            before_tokens,
                            after_tokens,
                            before_messages,
                            after_messages,
                        )
                        step -= 1
                        continue
                raise
            finally:
                self._clear_request_local_tool_context()
                self._clear_request_local_image_overlays()
                if llm_span_scope is not None:
                    llm_span_scope.__exit__(None, None, None)
            # Fresh full results are intentionally single-use. The durable
            # context already contains their bounded previews.
            self._fresh_tool_context.clear()
            if llm_step_id and self.task_store is not None:
                await durable_io(
                    self.task_store.finish_step,
                    llm_step_id,
                    output={"tool_calls": len(tool_calls or []), "has_content": bool(full_content)},
                )

            # 累加 token 用量
            if usage and prompt_estimate is not None:
                record_prompt_usage = getattr(self.llm, "record_prompt_usage", None)
                if record_prompt_usage:
                    record_prompt_usage(prompt_estimate, usage.get("last_prompt_tokens", usage.get("prompt_tokens")))
            self.context.add_usage(usage)
            await profiler.finish_async(usage)
            if usage:
                cache_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
                cache_miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
                cache_input = cache_hit + cache_miss
                if cache_input:
                    logger.info(
                        "prompt cache input=%s hit=%s miss=%s rate=%.1f%% session=%s request=%s",
                        cache_input,
                        cache_hit,
                        cache_miss,
                        cache_hit / cache_input * 100,
                        session_id,
                        request_id,
                    )
            raw_tool_calls = tool_calls
            if current_prefill is not None:
                continuation = full_content
                tool_calls = [{
                    "id": current_prefill["call_id"],
                    "name": current_prefill["tool_name"],
                    "arguments": self._merge_assistant_prefill(
                        current_prefill["arguments"],
                        continuation,
                    ),
                }]
                # The continuation is tool JSON, not assistant prose.
                full_content = ""
            tool_calls, tool_call_failure = self._validated_tool_calls(
                tool_calls,
                finish_reason,
                usage,
            )
            if tool_call_failure is not None:
                if tool_call_failure.code == "tool_call_truncated":
                    runtime_metrics.increment("tool_truncation_count")
                logger.warning(
                    "discarding incomplete tool call code=%s tool=%s call_id=%s finish_reason=%s chars=%s",
                    tool_call_failure.code,
                    tool_call_failure.tool_name,
                    tool_call_failure.call_id,
                    finish_reason or "unknown",
                    tool_call_failure.details.get("argument_chars", 0),
                )
                if full_content and not forced_tool:
                    self.context.add_assistant_raw(
                        self._assistant_message(full_content, None, full_reasoning)
                    )
                    last_text = full_content
                fingerprint = str(
                    tool_call_failure.details.get("fingerprint")
                    or f"{tool_call_failure.tool_name}|{finish_reason}"
                )
                recovery_count = tool_recovery_counts.get(fingerprint, 0) + 1
                tool_recovery_counts[fingerprint] = recovery_count
                tool_recovery_total += 1
                failure_event = tool_call_failure.to_event()
                failure_event["details"]["recovery_attempt"] = recovery_count
                if emit_events:
                    yield with_request(failure_event)
                if (
                    tool_call_failure.retryable
                    and recovery_count <= 2
                    and tool_recovery_total <= 2
                ):
                    runtime_metrics.increment("recovery_attempt_count")
                    pending_tool_recovery = True
                    recovery_tool_name = tool_call_failure.tool_name or "tool"
                    recovery_call_id = tool_call_failure.call_id
                    raw_call = next(
                        (
                            call
                            for call in (raw_tool_calls or [])
                            if str(call.get("id") or "") == recovery_call_id
                        ),
                        None,
                    )
                    raw_arguments = (
                        raw_call.get("arguments")
                        if isinstance(raw_call, dict)
                        else None
                    )
                    truncated_call = tool_call_failure.code == "tool_call_truncated"
                    use_prefill = (
                        truncated_call
                        and current_prefill is None
                        and not forced_tool
                        and self._assistant_prefill_enabled()
                        and isinstance(raw_arguments, str)
                        and 0 < len(raw_arguments) <= 32_000
                    )
                    if emit_events:
                        yield with_request({
                            "type": "tool_progress",
                            "call_id": recovery_call_id,
                            "name": recovery_tool_name,
                            "stage": "recovering",
                            "status": "running",
                            "message": (
                                (
                                    f"OUTPUT LIMIT · retry {recovery_count}/2 with "
                                    + ("assistant prefill continuation" if use_prefill else "a bounded tool call")
                                ) if truncated_call else
                                f"INVALID ARGUMENTS · retry {recovery_count}/2 using the tool recovery hint"
                            ),
                        })
                    if not truncated_call:
                        recovery_overlay = f"{tool_call_failure.message} {tool_call_failure.recovery_hint}"
                    elif use_prefill:
                        prefill_recovery = {
                            "call_id": recovery_call_id,
                            "tool_name": recovery_tool_name,
                            "arguments": str(raw_arguments),
                        }
                    elif forced_tool:
                        recovery_overlay = (
                            "The previous forced tool call was truncated by the provider output limit and was "
                            "discarded without execution. Resubmit exactly one complete, compact call to "
                            f"{forced_tool}. Do not include prose or unrelated tools."
                        )
                    elif (
                        tool_call_failure.tool_name == "write_file_chunk"
                        and tool_call_failure.details.get("finish_reason_mismatch")
                    ):
                        recovery_overlay = (
                            "The provider reported stop but cut the previous write_file_chunk JSON inside its "
                            "content string. The call was discarded and its sequence was not consumed. Resubmit "
                            "the SAME write_id and SAME sequence with a complete JSON call whose content is at "
                            "most 1000 characters. Continue future chunks at no more than 1000 characters each. "
                            "Do not skip or duplicate the sequence."
                        )
                    else:
                        if tool_call_failure.tool_name == "write_file" and not forced_tool:
                            # A whole-file retry with the same 4K model is very
                            # likely to truncate at the same point. Remove the
                            # tool for the rest of this turn so the next model
                            # pass must choose a semantic edit instead.
                            blocked_tools.add("write_file")
                        recovery_overlay = (
                            "The previous tool call was truncated by the provider output limit and was discarded "
                            "without execution. Do not repeat a whole-file write. For an existing file, read only the "
                            "relevant region and submit one smaller edit_file or apply_patch call. For a new file, "
                            "use apply_patch Add File to create only a compact skeleton, then extend one logical "
                            "class, function, or section at a time with later Update File patches. Submit one complete, "
                            "compact JSON tool call now. Never estimate byte counts or manage character chunks."
                        )
                    continue
                if emit_events:
                    yield with_request({
                        "type": "tool_progress",
                        "call_id": tool_call_failure.call_id,
                        "name": tool_call_failure.tool_name or "tool",
                        "stage": "stopped",
                        "status": "failed",
                        "message": "Recovery limit reached; incomplete call remained unexecuted.",
                    })
                stop_reason = tool_call_failure.message
                break
            if pending_tool_recovery and tool_calls:
                runtime_metrics.increment("recovery_success_count")
                pending_tool_recovery = False
                if emit_events:
                    yield with_request({
                        "type": "tool_progress",
                        "call_id": recovery_call_id,
                        "name": recovery_tool_name or "tool",
                        "stage": "recovered",
                        "status": "completed",
                        "message": "Complete replacement tool call received.",
                    })
            if forced_tool:
                valid_forced_call = (
                    len(tool_calls or []) == 1
                    and str((tool_calls or [{}])[0].get("name") or "") == forced_tool
                )
                if not valid_forced_call:
                    forced_tool_failures += 1
                    if forced_tool_failures < 2:
                        continue
                    stop_reason = f"模型连续未提交强制交互工具 {forced_tool}"
                    yield with_request({
                        "type": "error",
                        "message": f"Stopping: model did not submit required interaction tool ({forced_tool}).",
                        "recoverable": True,
                    })
                    break
            self.context.add_assistant_raw(
                self._assistant_message("" if forced_tool else full_content, tool_calls, full_reasoning, provider_state)
            )
            if task_id and self.task_store is not None:
                # An assistant tool_call is intentionally incomplete until its
                # tool result is appended. Saving it here would invoke orphan
                # cleanup and delete the call before the model can see the
                # result on the next iteration.
                if not tool_calls:
                    await self.context.save_async()
                await durable_io(self.task_store.checkpoint, task_id, {"phase": "after_llm", "iteration": step})

            if full_content and not forced_tool:
                last_text = full_content

            if tool_calls:
                repeated = None
                exhausted = None
                for tc in tool_calls:
                    name = tc.get("name", "")
                    tool_def = self.tools.get(name)
                    signature = self._tool_signature(tc)
                    if signature in failed_tool_signatures:
                        repeated = signature
                        break
                    if tool_def is None or tool_def.repeat_guard:
                        tool_signature_counts[signature] = tool_signature_counts.get(signature, 0) + 1
                        if tool_signature_counts[signature] > repeated_tool_limit:
                            repeated = signature
                            break
                    tool_name_counts[name] = tool_name_counts.get(name, 0) + 1
                    limit = tool_def.max_calls_per_turn if tool_def is not None else None
                    if limit is not None and tool_name_counts[name] > limit:
                        exhausted = (name, limit)
                        break
                if repeated:
                    runtime_metrics.increment("repeated_payload_count")
                    repeated_label = self._persistent_tool_signature_label(repeated)
                    stop_reason = f"检测到重复工具调用：{repeated_label}"
                    for tc in tool_calls:
                        self.context.add_tool(tc["id"], f"[ToolCircuitOpen] {stop_reason}")
                    yield with_request({
                        "type": "error",
                        "message": f"Stopping: repeated tool call detected ({repeated_label}).",
                        "recoverable": True,
                    })
                    break
                if exhausted:
                    name, limit = exhausted
                    stop_reason = f"工具 {name} 已达到本轮调用上限 {limit}"
                    for tc in tool_calls:
                        self.context.add_tool(tc["id"], f"[ToolBudgetExhausted] {stop_reason}")
                    yield with_request({
                        "type": "error",
                        "message": f"Stopping: per-turn tool budget exhausted ({name}, limit={limit}).",
                        "recoverable": True,
                    })
                    break

                # 通知前端：当前轮结束，finalize 本轮推理/内容
                if emit_events:
                    yield with_request({
                        "type": "tool_calls",
                        "calls": [
                            {
                                "id": str(tc.get("id") or ""),
                                "name": str(tc.get("name") or "tool"),
                                "arguments": self._persistent_tool_call_arguments(tc),
                            }
                            for tc in tool_calls
                        ],
                    })
                if emit_events:
                    progress_queue: asyncio.Queue[dict] = asyncio.Queue()
                    loop = asyncio.get_running_loop()

                    def enqueue_progress(
                        event: dict,
                        *,
                        event_loop: asyncio.AbstractEventLoop = loop,
                        event_queue: asyncio.Queue[dict] = progress_queue,
                    ) -> None:
                        event_loop.call_soon_threadsafe(event_queue.put_nowait, event)

                    execution_task = asyncio.create_task(self._execute_tool_calls(
                        tool_calls,
                        turn_tool_cache,
                        task_id=task_id,
                        active_groups=active_groups,
                        progress_callback=enqueue_progress,
                        trace_ctx=trace_ctx,
                        result_callback=enqueue_progress,
                    ))
                    next_progress: asyncio.Task | None = None
                    try:
                        while not execution_task.done():
                            next_progress = asyncio.create_task(progress_queue.get())
                            done, _ = await asyncio.wait(
                                {execution_task, next_progress},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if next_progress in done:
                                yield with_request(next_progress.result())
                                next_progress = None
                                continue
                            next_progress.cancel()
                            await asyncio.gather(next_progress, return_exceptions=True)
                            next_progress = None
                        tool_events = await execution_task
                    finally:
                        cleanup_interrupted = False
                        if next_progress is not None:
                            cleanup_interrupted |= await _cancel_and_drain_task(next_progress)
                        cleanup_interrupted |= await _cancel_and_drain_task(execution_task)
                        if cleanup_interrupted:
                            raise asyncio.CancelledError
                    await asyncio.sleep(0)
                    while not progress_queue.empty():
                        yield with_request(progress_queue.get_nowait())
                else:
                    tool_events = await self._execute_tool_calls(
                        tool_calls,
                        turn_tool_cache,
                        task_id=task_id,
                        active_groups=active_groups,
                        trace_ctx=trace_ctx,
                    )
                call_signatures = {
                    str(tc.get("id") or ""): self._tool_signature(tc)
                    for tc in tool_calls
                }
                completed_tool_events.extend(
                    self._public_tool_event(event)
                    for event in tool_events
                    if not event.get("error")
                )
                if any(event.get("coding_change_journal") for event in tool_events):
                    # A successful source mutation changes the world in which
                    # earlier checks ran. Re-running the same check is a new
                    # verification attempt, not a stuck duplicate loop.
                    tool_signature_counts.clear()
                    failed_tool_signatures.clear()
                    failure_state.clear()
                # Streamed results were published at individual completion;
                # model context above still preserves original call ordering.

                direct_tool = None
                if len(tool_calls) == 1:
                    candidate = self.tools.get(str(tool_calls[0].get("name") or ""))
                    if candidate is not None and candidate.return_direct:
                        direct_tool = candidate
                if direct_tool is not None and not tool_events[0].get("error"):
                    direct_text = str(tool_events[0].get("output") or "").strip()
                    if direct_text:
                        self.context.add_assistant_raw({"role": "assistant", "content": direct_text})
                        await self.context.save_async()
                        last_text = direct_text
                        completed_normally = True
                        if emit_events:
                            yield with_request({"type": "chunk", "content": direct_text})
                        break

                finalize_after_tools = self.finalize_after_tools_provider
                if (
                    finalize_after_tools is not None
                    and finalize_after_tools(full_content, tool_calls, tool_events)
                ):
                    completed_normally = True
                    break

                circuit = None
                for event in tool_events:
                    name = event.get("name", "")
                    if name == self.tools.ACTIVATE_GROUP_TOOL:
                        continue
                    error = str(event.get("error") or "")
                    if not error:
                        failure_state.pop(name, None)
                        continue
                    signature = call_signatures.get(str(event.get("id") or ""))
                    if signature and self._is_deterministic_tool_error(error, event):
                        failed_tool_signatures.add(signature)
                    fingerprint = self._error_fingerprint(error)
                    if not signature:
                        failure_state.pop(name, None)
                        continue
                    previous_signature, previous_count = failure_state.get(name, ("", 0))
                    count = previous_count + 1 if previous_signature == signature else 1
                    failure_state[name] = (signature, count)
                    if count >= self.failure_threshold:
                        blocked_tools.add(name)
                        circuit = (name, fingerprint, count)
                        break
                if task_id and self.task_store is not None:
                    # Save only after assistant tool_calls, every tool result,
                    # and reasoning_content form one coherent checkpoint.
                    await self.context.save_async()
                    await durable_io(self.task_store.checkpoint, task_id, {"phase": "after_tools", "iteration": step})
                def measure_post_tool_tokens(estimate=estimate_tokens) -> int:
                    if estimate:
                        return estimate(self.context.get_prompt(
                            content_overrides=self._vision_prompt_content_overrides(),
                        ))
                    return self.context.estimate_compaction_tokens()

                await self.context.compress_if_needed(measure_tokens=measure_post_tool_tokens)
                if estimate_tokens:
                    self.context.last_prompt_tokens = estimate_tokens(self.context.get_prompt(
                        content_overrides=self._vision_prompt_content_overrides(),
                    ))
                if self.context.last_prompt_tokens >= self.context.max_prompt_tokens:
                    # The provider-aware estimate can still exceed the budget
                    # after the normal context check (for example because tool
                    # schemas or multimodal overhead differ). Give compaction
                    # one forced retry before opening the safety circuit.
                    await self.context.compress_if_needed(force=True, measure_tokens=measure_post_tool_tokens)
                    if estimate_tokens:
                        self.context.last_prompt_tokens = estimate_tokens(self.context.get_prompt(
                            content_overrides=self._vision_prompt_content_overrides(),
                        ))
                if self.context.last_prompt_tokens >= self.context.max_prompt_tokens:
                    stop_reason = (
                        "工具调用后，本地提示词估算在强制压缩后仍超出预算"
                        f"（estimated={self.context.last_prompt_tokens:,}, "
                        f"budget={self.context.max_prompt_tokens:,}）"
                    )
                    hard_prompt_stop = True
                    yield with_request({
                        "type": "error",
                        "message": (
                            "Stopping: local prompt token budget still exceeded after compaction "
                            f"(estimated={self.context.last_prompt_tokens:,}, "
                            f"budget={self.context.max_prompt_tokens:,})."
                        ),
                        "recoverable": True,
                    })
                    break
                for event in tool_events:
                    call_id = str(event.get("id") or "").strip()
                    occurrence_id = f"tool:{call_id}" if call_id else ""
                    if (
                        occurrence_id
                        and occurrence_id in self._active_vision_attachment_occurrences
                    ):
                        continue
                    image_message = self._auto_image_message_from_tool_event(
                        event,
                        occurrence_id=occurrence_id,
                    )
                    if image_message is None:
                        continue
                    chat_content, image_storage_content, image_status = image_message
                    image_index = len(self.context.messages)
                    self.context.add_user(image_storage_content, provenance="tool_image")
                    self._active_vision_prompt_overlays.append(
                        (
                            self.context.messages[image_index],
                            image_storage_content,
                            chat_content,
                        )
                    )
                    if isinstance(event.get("_request_local_image_attachment"), dict):
                        self._request_local_image_overlay_messages.append(
                            self.context.messages[image_index]
                        )
                    auto_image_messages.append((image_index, image_storage_content))
                    self._active_vision_storage_replacements.append(
                        (image_index, image_storage_content)
                    )
                    if occurrence_id:
                        self._active_vision_attachment_occurrences.add(occurrence_id)
                    if emit_events and image_status is not None:
                        yield with_request(image_status)
                if circuit:
                    name, fingerprint, count = circuit
                    stop_reason = f"工具 {name} 连续 {count} 次出现同类错误（{fingerprint}），熔断器已打开"
                    yield with_request({
                        "type": "error",
                        "message": f"Stopping: tool circuit opened ({name}, failures={count}, fingerprint={fingerprint}).",
                        "recoverable": True,
                    })
                    break
                continue
            if self.delegate_mailbox is not None and task_id:
                drain_for = getattr(self.delegate_mailbox, "drain_for", None)
                envelopes = (
                    self.delegate_mailbox.drain_for(str(task_id), session_id)
                    if callable(drain_for)
                    else self.delegate_mailbox.drain(str(task_id))
                )
                if not envelopes and self.delegate_mailbox.has_running(str(task_id)):
                    # Parallel work has overlapped with the root's independent
                    # ReAct steps. Only when the root is ready to finalize does
                    # the dependency become serial: keep the task alive so
                    # approvals remain interactive, then synthesize the result.
                    if emit_events:
                        yield with_request({
                            "type": "tool_progress",
                            "call_id": "delegate-mailbox-join",
                            "name": "delegate_task",
                            "stage": "joining",
                            "status": "running",
                            "message": (
                                "Foreground work complete; waiting for background "
                                "subagents before final synthesis."
                            ),
                        })
                    envelopes = await self.delegate_mailbox.wait_and_drain(
                        str(task_id),
                        timeout=None,
                    )
                    if emit_events:
                        yield with_request({
                            "type": "tool_progress",
                            "call_id": "delegate-mailbox-join",
                            "name": "delegate_task",
                            "stage": "joined",
                            "status": "completed",
                            "message": "Background subagent results joined.",
                        })
                if envelopes:
                    if (
                        self.context.messages
                        and self.context.messages[-1].get("role") == "assistant"
                    ):
                        self.context.messages.pop()
                    for envelope in envelopes:
                        self.context.add_user(envelope, provenance="delegate")
                    recovery_overlay = (
                        "Background subagent results are now available. Continue from your "
                        "completed foreground work and reconcile the evidence. You may run "
                        "the minimal verification reads or checks needed to confirm "
                        "conclusions you cite, then give one clean final answer. Do not "
                        "repeat the dispatch acknowledgement."
                    )
                    # Final synthesis is bookkeeping for completed parallel
                    # work, not another unit of the caller's ReAct budget.
                    mailbox_iteration_extension += 1
                    continue
            completed_normally = True
            break

        if (
            self.max_iterations > 0
            and not completed_normally
            and not stop_reason
            and step >= self.max_iterations + mailbox_iteration_extension
        ):
            stop_reason = f"达到最大 ReAct 迭代次数 {self.max_iterations}"
            if task_id and self.task_store is not None:
                await durable_io(self.task_store.checkpoint, task_id, {
                    "phase": "budget_exhausted", "iteration": step,
                    "resume_kind": "iteration_budget", "stop_reason": stop_reason,
                })
            yield with_request({
                "type": "error",
                "message": f"Stopping: maximum ReAct iterations reached (limit={self.max_iterations}).",
                "recoverable": True,
            })

        if stop_reason:
            final_text = ""
            final_reasoning = ""
            final_provider_state = None
            final_tool_calls = None
            if not hard_prompt_stop:
                try:
                    final_prompt, _ = await self._prepare_prompt_for_llm(
                        user_text,
                        getattr(self.llm, "estimate_tokens", None),
                        trace_ctx=trace_ctx,
                        current_user_index=user_index,
                    )
                    final_prompt.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM-SUPPLIED TRANSIENT INSTRUCTION]\n"
                            "Tool execution has been stopped by a safety guard. Do not request or call any tools. "
                            "Give the user a concise final response using only results already present in the conversation. "
                            "State what was completed, the blocker, and a practical next step. "
                            f"Stop reason: {stop_reason}\n"
                            "[END SYSTEM-SUPPLIED TRANSIENT INSTRUCTION]"
                        ),
                    })
                    final_prompt, _ = await self._fit_appshot_request_budget(
                        final_prompt, [], user_text=user_text,
                    )
                    final_stream = self._llm_stream(messages=final_prompt, tools=[])
                    try:
                        async for event in final_stream:
                            if event["type"] == "reasoning":
                                final_reasoning += event.get("content", "")
                                if emit_events and self.context.show_reasoning:
                                    yield with_request({"type": "reasoning", "content": event.get("content", "")})
                            elif event["type"] == "chunk":
                                chunk = event.get("content", "")
                                final_text += chunk
                                if emit_events and chunk:
                                    yield with_request({"type": "chunk", "content": chunk})
                            elif event["type"] == "tool_calls":
                                final_tool_calls = event.get("calls")
                                final_text = event.get("content", final_text)
                                final_reasoning = event.get("reasoning_content", final_reasoning)
                            elif event["type"] == "done":
                                final_provider_state = event.get("_provider_state")
                                final_text = event.get("content", final_text)
                                final_reasoning = event.get("reasoning_content", final_reasoning)
                            elif event["type"] in {"generation_stats", "generation_progress"} and emit_events:
                                yield with_request(event)
                    finally:
                        await final_stream.aclose()
                except Exception:
                    logger.exception("graceful stop synthesis failed reason=%s", stop_reason)

            if not final_text.strip() or final_tool_calls:
                final_provider_state = None
                final_text = self._fallback_stop_answer(stop_reason)
                if emit_events:
                    yield with_request({"type": "chunk", "content": final_text})
            self.context.add_assistant_raw(self._assistant_message(final_text, None, final_reasoning, final_provider_state))
            last_text = final_text

        if user_index is not None and storage_content is not None:
            self.context.replace_message_content(user_index, storage_content)
        for image_index, image_storage_content in auto_image_messages:
            self.context.replace_message_content(image_index, image_storage_content)

        if task_id and self.task_store is not None:
            try:
                await durable_io(self.task_store.checkpoint, task_id, {
                    "phase": "turn_complete" if completed_normally and not stop_reason else "turn_stopped",
                    "iteration": step,
                    "final_summary": last_text[:2400],
                    "stop_reason": stop_reason,
                    "resume_kind": "iteration_budget" if stop_reason == f"达到最大 ReAct 迭代次数 {self.max_iterations}" else "",
                })
            except Exception:
                logger.exception("task store final checkpoint failed task_id=%s", task_id)

        # Once completion is visible it must already be durable. Automatic
        # memory retention and external sync below are best-effort bookkeeping.
        await self.context.save_async()
        yield with_request({"type": "done", "content": last_text})
        if (
            completed_normally
            and not stop_reason
            and self.memory_store is not None
            and self.memory_router is not None
            and self.memory_retainer is not None
        ):
            session_id = Path(self.context.session_path).stem if self.context.session_path else "default"
            core_was_written = any(
                event.get("name") == "memory" and (event.get("args") or {}).get("action") == "core_add"
                for event in completed_tool_events
            )
            if not core_was_written:
                try:
                    from .memory_retainer import ToolEvidence

                    evidence = []
                    for event in completed_tool_events if self.memory_retainer.enabled else ():
                        if event.get("error"):
                            continue
                        tool = self.tools.get(str(event.get("name") or ""))
                        if event.get("execution") or (tool is not None and tool.risk == "execute"):
                            execution = event.get("execution") or {}
                            if execution.get("status") != "completed" or execution.get("exit_code") != 0:
                                continue
                        extractor = tool.memory_evidence if tool is not None else None
                        if extractor is None:
                            continue
                        try:
                            content = extractor(dict(event))
                        except Exception:
                            logger.exception("tool memory evidence extractor failed name=%s", event.get("name"))
                            continue
                        if content:
                            evidence.append(ToolEvidence(
                                tool_name=str(event.get("name") or "tool"),
                                content=str(content),
                                call_id=str(event.get("id") or ""),
                            ))
                    with trace_span("agent.memory.retain", ctx=trace_ctx) as retain_span:
                        outcome = await self.memory_retainer.retain_turn(
                            user_text,
                            session_id=session_id,
                            message_id=msg.id,
                            recalled=self.memory_router.last_recalled(session_id),
                            tool_evidence=evidence,
                        )
                        changed_ids = (*outcome.retained_ids, *outcome.confirmed_ids)
                        if changed_ids:
                            trace_ctx.memory_id = ",".join(changed_ids)
                            if retain_span is not None:
                                retain_span.set_attribute("agent.memory_id", trace_ctx.memory_id)
                                retain_span.set_attribute("agent.memory.count", len(changed_ids))
                        if changed_ids or outcome.superseded_ids:
                            yield with_request({
                                "type": "memory_retention",
                                "decision": outcome.decision,
                                "mode": self.memory_retainer.mode,
                                "retained_ids": list(outcome.retained_ids),
                                "confirmed_ids": list(outcome.confirmed_ids),
                                "superseded_ids": list(outcome.superseded_ids),
                            })
                except Exception:
                    # Retention is post-turn bookkeeping. It must never turn a
                    # successfully completed user request into a failed turn.
                    logger.exception("automatic memory retention failed session=%s", session_id)
        if completed_normally and not stop_reason:
            await self.sync_external_session(reason="turn")

    async def _record_turn_budget_exhaustion(self, msg: Msg, exc: TurnBudgetExceeded) -> None:
        self.context.sanitize_tool_history()
        await self.context.save_async()
        task_id = msg.metadata.get("task_id")
        if task_id and self.task_store is not None:
            task = (await durable_io(self.task_store.get_task, task_id)) or {}
            # Cancellation can land after dispatch while post-action readback
            # is still running, outside the registry's invocation handler.
            for step in task.get("steps", []):
                if step.get("kind") == "tool" and step.get("status") == "running":
                    await durable_io(
                        self.task_store.finish_step,
                        step["id"], status="unknown",
                        error="Turn budget expired before the tool outcome was durably recorded; inspect before retrying.",
                    )
            previous = task.get("checkpoint", {})
            await durable_io(self.task_store.checkpoint, task_id, {
                **previous,
                "phase": "budget_exhausted", "resume_kind": "time_budget",
                "stop_reason": str(exc), "budget_seconds": exc.seconds,
                "elapsed_seconds": round(exc.elapsed, 3),
            })

    # -- Turn-change ledger (M1) ------------------------------------------

    def _turn_change_workspace(self) -> Path:
        """Sandbox workdir for the ledger *without* resolving symlinks.

        The store anchors the workspace identity and refuses a swapped final
        symlink; resolving here would follow the swap before the store can
        check it (review R1).
        """
        sandbox = self._sandbox
        current = getattr(sandbox, "current", None)
        if callable(current):
            sandbox = current()
        return Path(getattr(sandbox, "workdir", Path.cwd()))

    def _make_turn_change_store(self, session_key: str) -> "TurnChangeStore":
        """Factory seam: tests isolate the snapshot root by overriding this."""
        return TurnChangeStore(self._turn_change_workspace(), session_key)

    def _close_turn_change_store(self) -> None:
        store = getattr(self, "_turn_change_store", None)
        if store is None:
            return
        self._turn_change_store = None
        self._turn_change_store_session = None
        try:
            store.close()
        except Exception:
            logger.exception("turn-change store close failed")

    def turn_change_store(self) -> "TurnChangeStore | None":
        """Read-only accessor for the live session store (M3 · T4).

        Returns the existing instance only when it belongs to the current
        session; never creates, rebuilds or closes one (review R3: ``/changes``
        must not turn a read into a store lifecycle event).
        """
        store = getattr(self, "_turn_change_store", None)
        if store is None:
            return None
        session_path = self.context.session_path
        session_key = Path(session_path).stem if session_path else "default"
        if self._turn_change_store_session != session_key:
            return None
        return store

    def turn_changes_session_state(self) -> str:
        """Read-only ``/changes`` state: ``available`` / ``none`` / ``unavailable``.

        Never creates or rebuilds the store (review R5).  With a live session
        store the answer is ``available``; otherwise the session directory is
        probed read-only and ``none`` is reserved for a session without any
        evidence of completed turns.  A session that carries history but has
        no live store — on disk or in the loaded conversation (for example one
        restored after a normal shutdown cleaned its snapshots) — stays
        ``unavailable``.
        """
        if self.turn_change_store() is not None:
            return "available"
        session_path = self.context.session_path
        session_key = Path(session_path).stem if session_path else "default"
        try:
            evidence = session_history_evidence(self._turn_change_workspace(), session_key)
        except Exception:
            logger.exception("turn-change session probe failed")
            return "unavailable"
        if evidence is False:
            if self._session_has_messages():
                # 恢复的旧会话：聊天历史已加载，但快照/索引不可恢复 → 保守
                # （review R5 adjacent）
                return "unavailable"
            return "none"
        return "unavailable"

    def _session_has_messages(self) -> bool:
        """True when the loaded context already carries conversation history."""
        try:
            return bool(self.context.messages)
        except Exception:
            return True  # 无法判断时保守

    def _current_turn_change_store(self) -> "TurnChangeStore | None":
        """Session-owned turn-change store; rebuilt when the session changes."""
        session_key = Path(self.context.session_path).stem if self.context.session_path else "default"
        store = getattr(self, "_turn_change_store", None)
        if store is not None and self._turn_change_store_session == session_key:
            return store
        if store is not None:
            self._close_turn_change_store()
        try:
            store = self._make_turn_change_store(session_key)
        except Exception:
            logger.exception("turn-change store initialization failed")
            store = None
        self._turn_change_store = store
        self._turn_change_store_session = session_key
        return store

    def _begin_turn_changes(self, msg: Msg):
        """Begin the turn ledger; returns (store, scope_cm) or (None, None)."""
        store = self._current_turn_change_store()
        self._turn_changes_ready = None
        if store is None:
            return None, None
        try:
            store.begin_turn(self._vision_request_id(msg))
        except Exception:
            logger.exception("turn-change begin_turn failed")
            return None, None
        scope_cm = turn_store_scope(store)
        scope_cm.__enter__()
        return store, scope_cm

    def _turn_cancel_probe(self) -> "Callable[[], bool] | None":
        """Synchronous cancellation channel for the done-path seal (review R6).

        The seal runs synchronously, so a pending asyncio cancellation is only
        delivered at the next await; this probe lets the store observe it and
        stop new discardable reads during that window.
        """
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return None
        cancel_count = getattr(task, "cancelling", None)
        if not callable(cancel_count):
            return None
        return lambda: bool(cancel_count())

    def _seal_turn_changes(self, store, *, cancelled: "Callable[[], bool] | None" = None) -> None:
        """Seal the active turn (idempotent) and stash a publish-ready payload."""
        if store is None:
            return
        if cancelled is None:
            # 正常收尾同样给出同步取消通道（review R6）
            cancelled = self._turn_cancel_probe()
        try:
            manifest = store.seal(cancelled=cancelled)
        except Exception:
            logger.exception("turn-change seal failed")
            return
        if manifest is not None:
            self._turn_changes_ready = self._turn_changes_event(manifest)

    async def _seal_turn_changes_async(
        self, store, *, cancelled: "Callable[[], bool] | None" = None
    ) -> None:
        """Cooperative seal for the streaming done path (review F4/G2).

        收尾在条目之间把控制权交还事件循环：排期的外部取消能在其中得到投递，
        剩余可放弃工作（读取/写入/淘汰）随即停止；收尾完成后的取消语义继续
        传播（清单先暂存），由调用方走取消终态路径。
        """
        if store is None:
            return
        if cancelled is None:
            cancelled = self._turn_cancel_probe()
        try:
            manifest = await store.seal_async(cancelled=cancelled)
        except asyncio.CancelledError:
            # 收尾期间的取消：清单已产出 → 暂存 payload 后继续传播（review G2）
            stopped = store.take_stopped_manifest()
            if stopped is not None:
                self._turn_changes_ready = self._turn_changes_event(stopped)
            raise
        except Exception:
            logger.exception("turn-change seal failed")
            return
        if manifest is not None:
            self._turn_changes_ready = self._turn_changes_event(manifest)

    def _take_turn_changes_payload(self) -> dict | None:
        payload = self._turn_changes_ready
        self._turn_changes_ready = None
        return payload

    def _degraded_turn_changes_payload(self, *, cancelled: bool) -> dict | None:
        """Seal (idempotent) after an aborted turn, then take its payload."""
        store = getattr(self, "_turn_change_store", None)
        if store is not None:
            self._seal_turn_changes(store, cancelled=(lambda: True) if cancelled else None)
        return self._take_turn_changes_payload()

    async def _degraded_turn_changes_payload_async(self, *, cancelled: bool) -> dict | None:
        """Aborted-turn seal (idempotent) that yields to the loop while sealing (F4)."""
        store = getattr(self, "_turn_change_store", None)
        if store is not None:
            await self._seal_turn_changes_async(
                store, cancelled=(lambda: True) if cancelled else None
            )
        return self._take_turn_changes_payload()

    def _end_turn_changes(self, store, scope_cm) -> None:
        """Finally-path fallback: silent seal (idempotent) + scope reset."""
        if store is not None:
            cancelled = (lambda: True) if sys.exc_info()[0] is not None else None
            self._seal_turn_changes(store, cancelled=cancelled)
        if scope_cm is not None:
            try:
                scope_cm.__exit__(None, None, None)
            except Exception:
                logger.exception("turn-change scope exit failed")

    @staticmethod
    def _turn_changes_event(manifest) -> dict:
        return {
            "type": "turn_changes",
            "session_id": manifest.session_id,
            "request_id": manifest.request_id,
            "turn_seq": manifest.turn_seq,
            "files": [
                {
                    "path": change.path,
                    "state": change.state,
                    "added": change.added,
                    "removed": change.removed,
                    "compare": change.compare,
                    "reason": change.reason,
                }
                for change in manifest.files
            ],
            "unknown_count": len(manifest.unknown),
            "totals": dict(manifest.totals),
        }

    async def reply_stream(self, msg: Msg):
        """流式回复，yield 事件供 CLI 渲染"""
        turn_store, turn_scope_cm = self._begin_turn_changes(msg)
        try:
            source = self._run_react_loop(msg, emit_events=True)
            react_events = budgeted_events(source, getattr(self, "turn_timeout_seconds", 0.0))
            try:
                async for event in react_events:
                    if event["type"] == "done":
                        await self._seal_turn_changes_async(turn_store)
                        payload = self._take_turn_changes_payload()
                        if payload is not None:
                            yield payload
                        yield {"type": "done", "request_id": event.get("request_id")}
                    else:
                        yield event
            finally:
                try:
                    await react_events.aclose()
                finally:
                    await source.aclose()
        except TurnBudgetExceeded as exc:
            await self._record_turn_budget_exhaustion(msg, exc)
            payload = await self._degraded_turn_changes_payload_async(cancelled=False)
            if payload is not None:
                yield payload
            yield {
                "type": "error", "code": "turn_budget_exhausted", "message": str(exc),
                "retryable": False, "recoverable": False,
                "request_id": msg.metadata.get("request_id") or msg.id,
            }
            yield {"type": "done", "request_id": msg.metadata.get("request_id") or msg.id}
        except asyncio.CancelledError:
            logger.warning("react stream cancelled request_id=%s", msg.metadata.get("request_id") or msg.id)
            self.context.sanitize_tool_history()
            await self.context.save_async()
            payload = await self._degraded_turn_changes_payload_async(cancelled=True)
            if payload is not None:
                yield payload
            yield {
                "type": "error",
                "message": "LLM stream cancelled; the current turn was stopped before completion.",
                "code": "cancelled",
                "retryable": False,
                "recoverable": False,
                "cancelled": True,
                "request_id": msg.metadata.get("request_id") or msg.id,
            }
            yield {"type": "done", "request_id": msg.metadata.get("request_id") or msg.id}
        except LLMResponseError as exc:
            self.context.sanitize_tool_history()
            await self.context.save_async()
            payload = await self._degraded_turn_changes_payload_async(cancelled=False)
            if payload is not None:
                yield payload
            yield {
                "type": "error", "message": exc.public_message,
                "code": exc.public_code, "retryable": False, "recoverable": False,
                "request_id": msg.metadata.get("request_id") or msg.id,
            }
            yield {"type": "done", "request_id": msg.metadata.get("request_id") or msg.id}
        except LLMIdleTimeout as exc:
            logger.warning("react stream idle timeout request_id=%s", msg.metadata.get("request_id") or msg.id)
            payload = await self._degraded_turn_changes_payload_async(cancelled=False)
            if payload is not None:
                yield payload
            yield {
                "type": "error",
                "message": str(exc),
                "code": "idle_timeout",
                "retryable": True,
                "recoverable": False,
                "request_id": msg.metadata.get("request_id") or msg.id,
            }
            yield {"type": "done", "request_id": msg.metadata.get("request_id") or msg.id}
        except LLMOverallTimeout as exc:
            logger.warning("react stream overall timeout request_id=%s", msg.metadata.get("request_id") or msg.id)
            payload = await self._degraded_turn_changes_payload_async(cancelled=False)
            if payload is not None:
                yield payload
            yield {
                "type": "error",
                "message": str(exc),
                "code": "overall_timeout",
                "retryable": True,
                "recoverable": False,
                "request_id": msg.metadata.get("request_id") or msg.id,
            }
            yield {"type": "done", "request_id": msg.metadata.get("request_id") or msg.id}
        finally:
            self._end_turn_changes(turn_store, turn_scope_cm)

    async def reply(self, msg: Msg) -> Optional[Msg]:
        """非流式回复"""
        if not msg.has_user_content():
            return None

        last_text = ""
        turn_store, turn_scope_cm = self._begin_turn_changes(msg)
        source = self._run_react_loop(msg, emit_events=False)
        react_events = budgeted_events(source, getattr(self, "turn_timeout_seconds", 0.0))
        try:
            async for event in react_events:
                if event["type"] == "done":
                    last_text = event.get("content", "")
            await self._seal_turn_changes_async(turn_store)
        except TurnBudgetExceeded as exc:
            await self._record_turn_budget_exhaustion(msg, exc)
            raise
        finally:
            try:
                await react_events.aclose()
            finally:
                await source.aclose()
            self._end_turn_changes(turn_store, turn_scope_cm)

        return Msg(sender=self.name, role="assistant",
                   content=[ContentBlock.text(last_text or "(no response)")],
                   parent_id=msg.id)

    def reset_conversation(self):
        context_index_broker = getattr(self, "context_index_broker", None)
        if context_index_broker is not None:
            context_index_broker.end_session()
        self.context.reset()
        self._fresh_tool_context.clear()
        self._request_local_tool_context_ids.clear()
        self._steering.clear()
        self._steering_seen.clear()
        self._task_mutated_paths.clear()
        self._task_context_paths.clear()
        self._active_skill_names.clear()
        self._verification_required = False
        self._refresh_skill_catalog(force=True)

    def set_system_prompt(self, prompt: str):
        self.context.set_system_prompt(prompt)

    def set_persona(self, state, prompt: str):
        self.context.set_persona(state, prompt)
