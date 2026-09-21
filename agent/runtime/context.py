"""AgentContext — 对话上下文管理"""

import asyncio
import copy
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, TYPE_CHECKING, cast

from .session_store import SessionStore
from .async_io import durable_io
from .token_estimator import estimate_value_tokens
from .persona import PersonaState, parse_persona_metadata
from .prompts import get_prompt_profile, is_legacy_persona_prompt, normalize_system_prompt
from .system_prompt_projection import SystemPromptProjection
from .runtime_context_projection import RuntimeContextProjection
from .system_suffix import split_legacy_suffix
from .time_utils import needs_relative_date_anchor, relative_date_anchor, relative_date_metadata, weekday_label

if TYPE_CHECKING:
    from .context_compressor import ContextCompressor

logger = logging.getLogger(__name__)

_NO_OVERRIDE = object()


def _valid_message_timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else None


def _model_timestamp_marker(value: Any) -> str | None:
    timestamp = _valid_message_timestamp(value)
    if timestamp is None:
        return None
    try:
        authored_at = datetime.fromtimestamp(timestamp).astimezone()
    except (OverflowError, OSError, ValueError):
        return None
    return f"<message_time>{authored_at.isoformat(timespec='seconds')} {weekday_label(authored_at)}</message_time>"


def _timestamped_content(content: Any, marker: str | None) -> Any:
    if not marker or content in (None, ""):
        return content
    if isinstance(content, str):
        return f"{marker}\n{content}"
    if isinstance(content, list):
        return [{"type": "text", "text": marker}, *copy.deepcopy(content)]
    return content


def _usage_int(value: Any) -> int:
    """Coerce provider usage fields to non-negative integers defensively."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _strip_image_content(content) -> str | None:
    """Remove image_url blocks from message content, keeping text only.

    Returns the stripped content, or None if nothing meaningful remains.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text", "")
                if t.strip():
                    texts.append(t)
        return "\n".join(texts) if texts else None
    return str(content)


def _strip_images_from_messages(messages: list[dict]) -> list[dict]:
    """Remove base64 image data from session messages to prevent context bloat."""
    cleaned = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "appshot_image" for p in content):
            # Durable opaque references remain canonical. Hydration is validated
            # separately and never written back to message history.
            stripped = [p for p in content if isinstance(p, dict) and p.get("type") in {"text", "appshot_image"}]
        else:
            stripped = _strip_image_content(content)
        if stripped is None:
            continue
        msg_copy = dict(msg)
        msg_copy["content"] = stripped
        cleaned.append(msg_copy)
    return cleaned


def _restore_legacy_reasoning_content(messages: list[dict]) -> list[dict]:
    """Move legacy reasoning-context messages back onto their tool-call assistant.

    Older sessions stored DeepSeek reasoning as a synthetic user message after
    the tool result. Thinking mode instead requires the original assistant
    tool-call message to carry ``reasoning_content`` on every replay.
    """
    restored: list[dict] = []
    for msg in messages:
        meta = msg.get("_meta")
        is_legacy_reasoning = (
            msg.get("role") == "user"
            and isinstance(meta, dict)
            and meta.get("type") == "reasoning_context"
        )
        if not is_legacy_reasoning:
            restored.append(msg)
            continue

        target_index = len(restored) - 1
        while target_index >= 0 and restored[target_index].get("role") == "tool":
            target_index -= 1
        target = restored[target_index] if target_index >= 0 else None
        if not (
            target
            and target.get("role") == "assistant"
            and target.get("tool_calls")
            and not target.get("reasoning_content")
        ):
            target = None
        if target is None:
            restored.append(msg)
            continue

        content = str(msg.get("content", ""))
        marker = "\n\n"
        reasoning = content.split(marker, 1)[1] if marker in content else content
        if reasoning.strip():
            target["reasoning_content"] = reasoning.strip()
        # The synthetic user message is no longer part of the conversation.

    return restored


def _strip_orphan_tool_calls(messages: list[dict]) -> list[dict]:
    """Clean malformed and orphaned tool-call chains before provider replay.

    Provider APIs validate historical function arguments again. A single
    truncated JSON string can therefore make every future turn fail before
    generation starts. Preserve useful assistant prose, but remove malformed
    calls and their tool results from canonical history.
    """
    sanitized: list[dict] = []
    invalid_ids: set[str] = set()
    repaired_calls = 0
    for original in messages:
        if original.get("role") != "assistant" or not original.get("tool_calls"):
            if (
                original.get("role") == "tool"
                and str(original.get("tool_call_id") or "") in invalid_ids
            ):
                continue
            sanitized.append(original)
            continue

        valid_calls = []
        invalid_names = []
        for tc in original.get("tool_calls") or []:
            call_id = str(tc.get("id") or "") if isinstance(tc, dict) else ""
            function = tc.get("function") if isinstance(tc, dict) else None
            name = str(function.get("name") or "") if isinstance(function, dict) else ""
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if call_id and name and isinstance(arguments, str):
                try:
                    valid = isinstance(json.loads(arguments), dict)
                except json.JSONDecodeError:
                    valid = False
            else:
                valid = False
            if valid:
                valid_calls.append(tc)
                continue
            if call_id:
                invalid_ids.add(call_id)
            invalid_names.append(name or "tool")

        if not invalid_names:
            sanitized.append(original)
            continue

        repaired_calls += len(invalid_names)
        msg = copy.deepcopy(original)
        if valid_calls:
            msg["tool_calls"] = valid_calls
        else:
            msg.pop("tool_calls", None)
        notice = (
            "[Session recovery: removed an incomplete "
            f"{', '.join(invalid_names)} call; it was not executed.]"
        )
        content = str(msg.get("content") or "").strip()
        msg["content"] = f"{content}\n\n{notice}" if content else notice
        sanitized.append(msg)

    if repaired_calls:
        logger.warning("session recovery removed malformed tool calls count=%s", repaired_calls)

    # Enforce the provider protocol structurally, not merely by finding the
    # same call id somewhere in history. Tool messages must be the contiguous
    # messages immediately following their assistant tool_calls message.
    result: list[dict] = []
    i = 0
    while i < len(sanitized):
        msg = sanitized[i]
        if msg.get("role") == "tool":
            # A standalone tool result is always invalid, even if a matching
            # id appeared much earlier in the session.
            i += 1
            continue

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tc_ids = {tc["id"] for tc in msg["tool_calls"] if "id" in tc}
            j = i + 1
            tool_messages: list[dict] = []
            while j < len(sanitized) and sanitized[j].get("role") == "tool":
                tool_messages.append(sanitized[j])
                j += 1
            found = {
                str(tool_message.get("tool_call_id") or "")
                for tool_message in tool_messages
            }
            complete = bool(tc_ids) and found == tc_ids and len(tool_messages) == len(tc_ids)
            if complete:
                result.append(msg)
                result.extend(tool_messages)
                i = j
                continue
            repaired = copy.deepcopy(msg)
            repaired.pop("tool_calls", None)
            notice = "[Session recovery: removed an incomplete or cancelled tool call.]"
            content = str(repaired.get("content") or "").strip()
            repaired["content"] = f"{content}\n\n{notice}" if content else notice
            result.append(repaired)
            i = j
            continue

        result.append(msg)
        i += 1

    return result


def _estimate_value_tokens(value) -> int:
    """Cheap provider-agnostic estimate without serializing full history."""
    return estimate_value_tokens(value)


@dataclass
class AgentContext:
    system_prompt: str = ""
    persona_id: str = ""
    persona_definition_version: int = 0
    persona_state_revision: int = 0
    persona_active_mode: str = ""
    persona_relationship_context: str = ""
    persona_affect: str = ""
    messages: list[dict] = field(default_factory=list)
    # Retained for constructor/session compatibility only. Compression is
    # token-budget driven; long tool-heavy conversations must not compact just
    # because they contain many small protocol messages.
    max_messages: int = 500
    max_prompt_tokens: int = 100_000
    show_reasoning: bool = True
    # Some isolated profiles deliberately have no compaction service at all.
    # Keep that boundary explicit: ``compressor is None`` alone is not enough,
    # because the deterministic and legacy fallback layers can still rewrite
    # the message history before they consult the service.
    compaction_enabled: bool = True
    compressor: "ContextCompressor | None" = field(default=None, init=False, repr=False)
    compaction_observer: Callable[[dict], None] | None = field(default=None, init=False, repr=False)
    _last_compaction_report: dict | None = field(default=None, init=False, repr=False)
    # Resolves a tool name to its risk tier ("read"/"network"/"write"/"execute"/
    # "secret") for the deterministic compaction layers. None treats every tool
    # as read. Installed by the owning agent, which knows the tool registry.
    tool_risk_provider: Callable[[str], str] | None = field(default=None, init=False, repr=False)
    tool_request_local_provider: Callable[[str], bool] | None = field(default=None, init=False, repr=False)
    # Trailing completed tool steps to preserve during deterministic
    # compaction (active tool chains must never be collapsed mid-flight).
    compact_keep_recent: int = 4
    _session_path: str = ""
    enforce_session_ownership: bool = False
    _session_lease: Any = field(default=None, init=False, repr=False)

    # Token 统计（跨会话持久化）
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cache_hit_tokens: int = 0
    total_cache_miss_tokens: int = 0
    last_prompt_tokens: int = 0  # 最近一次请求的 prompt 大小（用于上下文百分比显示）
    _session_store: SessionStore | None = field(default=None, init=False, repr=False)
    _saved_message_count: int = field(default=0, init=False, repr=False)
    _save_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _message_token_costs: list[int] = field(default_factory=list, init=False, repr=False)
    _system_token_cost: int = field(default=0, init=False, repr=False)
    _tools_token_cost: int = 0
    _stable_system_suffix: str = field(default="", init=False, repr=False)
    _system_prompt_migration: dict = field(default_factory=dict, init=False, repr=False)
    system_projection: SystemPromptProjection = field(default_factory=SystemPromptProjection, init=False, repr=False)
    runtime_projection: RuntimeContextProjection = field(default_factory=RuntimeContextProjection, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_prompt_tokens == 100_000:
            # This is a token budget, not a limit on the serialized request size.
            # 显式传值优先于环境，免得把评估回放/隔离 profile 的预算悄悄改掉。
            # See specs/llm-request-byte-budget.md for the unresolved byte-budget proposal.
            override = os.getenv("CONTEXT_MAX_PROMPT_TOKENS", "").strip()
            if override.isdigit() and int(override) > 0:
                self.max_prompt_tokens = int(override)
        self._system_token_cost = _estimate_value_tokens(self.effective_system_prompt)

    @property
    def session_path(self) -> str:
        return self._session_path

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    def set_session(self, path: str):
        if self.enforce_session_ownership:
            from agent.ui.session_ownership import claim_session
            lease = claim_session(path)
        else:
            lease = None
        self._system_prompt_migration = {}
        self.system_projection.reset()
        self.runtime_projection.reset()
        self._session_path = path
        self._session_lease = lease
        self._session_store = SessionStore(path)
        self._saved_message_count = 0
        if self.compressor is not None:
            self.compressor.reset()

    def set_system_prompt(self, prompt: str):
        self.system_prompt = prompt
        self.persona_id = ""
        self.persona_definition_version = 0
        self.persona_state_revision = 0
        self.persona_active_mode = ""
        self.persona_relationship_context = ""
        self.persona_affect = ""
        self._system_token_cost = _estimate_value_tokens(self.effective_system_prompt)

    @property
    def effective_system_prompt(self) -> str:
        if not self._stable_system_suffix:
            return self.system_prompt
        return f"{self.system_prompt}\n\n{self._stable_system_suffix}".strip()

    def set_stable_system_suffix(self, suffix: str):
        """Set deterministic capability guidance kept outside session state.

        The suffix is reconstructed from installed capabilities on startup, so
        legacy generated tails can be retired without losing custom prompt text.
        """
        self._stable_system_suffix = str(suffix or "").strip()
        self._migrate_system_suffix()
        self._system_token_cost = _estimate_value_tokens(self.effective_system_prompt)

    def _migrate_system_suffix(self) -> None:
        base, catalogs, projects = split_legacy_suffix(self.system_prompt, self._stable_system_suffix)
        if not (catalogs or projects):
            return
        # Audit-only reversible backup: persisted atomically with the new base,
        # never included in provider messages or token accounting.
        migration = dict(self._system_prompt_migration)
        migration.setdefault("version", 1)
        migration.setdefault("original_system_prompt", self.system_prompt)
        for key, count in (("skill_catalogs", catalogs), ("project_blocks", projects)):
            migration[key] = int(migration.get(key, 0)) + count
        self._system_prompt_migration = migration
        self.system_prompt = base
        self.system_projection.reset()
        logger.info("Retired legacy system suffix: %s catalogs, %s project blocks", catalogs, projects)
        if "<available-skills>" in base or "## Project guidance:" in base:
            logger.warning("Unrecognized legacy prompt blocks retained; original prompt backed up")

    def set_persona(self, state: PersonaState, prompt: str):
        """Set an assembled persona prompt and persist its identity separately."""
        self.system_prompt = prompt
        self.persona_id = state.persona_id
        self.persona_definition_version = state.definition_version
        self.persona_state_revision = state.state_revision
        self.persona_active_mode = state.active_mode
        self.persona_relationship_context = state.relationship_context
        self.persona_affect = state.affect
        self._system_token_cost = _estimate_value_tokens(self.effective_system_prompt)

    def set_tools_token_cost(self, cost: int):
        self._tools_token_cost = cost

    def add_usage(self, usage: dict | None):
        """累加一次 LLM 调用的 token 用量"""
        if not usage:
            return
        pt = _usage_int(usage.get("prompt_tokens", 0))
        ct = _usage_int(usage.get("completion_tokens", 0))
        cache_hit = _usage_int(usage.get("prompt_cache_hit_tokens", 0))
        cache_miss = _usage_int(usage.get("prompt_cache_miss_tokens", 0))
        self.total_prompt_tokens += pt
        self.total_completion_tokens += ct
        self.total_cache_hit_tokens += cache_hit
        self.total_cache_miss_tokens += cache_miss
        self.last_prompt_tokens = _usage_int(usage.get("last_prompt_tokens", pt))

    def append_diagnostic(self, event: dict) -> str:
        """Write audit-only data without adding it to model-visible history."""
        if not self._session_path or not self._session_store:
            return ""
        return str(self._session_store.append_diagnostic(event))

    def append_raw_tool_call(self, event: dict) -> str:
        """Archive a model-invisible call after its argument persistence policy.

        The caller supplies safe arguments; this archive is a durable sink too.
        """
        if not self._session_path or not self._session_store:
            return ""
        return str(self._session_store.append_raw_tool_call(event))

    def append_context_index_feedback(self, event: dict) -> str:
        """Persist Context Index feedback outside model-visible history."""
        if not self._session_store:
            return ""
        return str(self._session_store.append_context_index_feedback(event))

    def record_session_start(self) -> None:
        if self._session_store is not None:
            self._session_store.begin()

    def record_session_end(self, reason: str) -> None:
        if self._session_store is not None:
            self._session_store.record_end(reason)

    def append_approval_audit(self, event: dict) -> str:
        if not self._session_store:
            return ""
        return str(self._session_store.append_approval_audit(event))

    def save(self, *, allow_empty: bool = False):
        """Persist the session.

        ``allow_empty`` lets a deliberate wipe (isolated-session undo/retry)
        clear the stored history instead of being skipped.
        """
        data = self._prepare_save(allow_empty=allow_empty)
        if data is None or self._session_store is None:
            return
        self._session_store.save(data, append_from=self._saved_message_count)
        self._saved_message_count = len(data["messages"])

    async def save_async(self, *, allow_empty: bool = False) -> None:
        """Snapshot on the owning loop; only immutable snapshot I/O is threaded."""
        async with self._save_lock:
            data = self._prepare_save(allow_empty=allow_empty)
            store = self._session_store
            if data is None or store is None:
                return
            snapshot = copy.deepcopy(data)
            append_from = self._saved_message_count
            saved = False

            def write() -> None:
                nonlocal saved
                store.save(snapshot, append_from=append_from)
                saved = True

            try:
                await durable_io(write)
            finally:
                # durable_io drains cancellation, so this flag is settled.
                if saved and self._session_store is store:
                    self._saved_message_count = len(snapshot["messages"])

    def _prepare_save(self, *, allow_empty: bool) -> dict | None:
        if not self._session_path or not self._session_store:
            return None
        if not self.messages and not allow_empty:
            return None
        # 保存前清理孤立的 tool_calls 链
        clean = _strip_orphan_tool_calls(self.messages)
        for message in clean:
            message.pop("api_content", None)
        self.messages = clean
        self._rebuild_token_cache()
        data = {
            "system_prompt": self.system_prompt,
            "system_prompt_migration": self._system_prompt_migration,
            "system_prompt_projection": self.system_projection.state,
            "runtime_context_projection": self.runtime_projection.state,
            "persona_id": self.persona_id,
            "persona_definition_version": self.persona_definition_version,
            "persona_state_revision": self.persona_state_revision,
            "persona_state": {
                "active_mode": self.persona_active_mode,
                "relationship_context": self.persona_relationship_context,
                "affect": self.persona_affect,
            },
            "messages": clean,
            "show_reasoning": self.show_reasoning,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cache_hit_tokens": self.total_cache_hit_tokens,
            "total_cache_miss_tokens": self.total_cache_miss_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
        }
        return data

    def stage_session(self, path: str):
        """Load a candidate without resetting the active session on bad media."""
        candidate = copy.copy(self)
        candidate.system_projection = copy.deepcopy(self.system_projection)
        candidate.runtime_projection = copy.deepcopy(self.runtime_projection)
        candidate.messages = []
        candidate._message_token_costs = []
        candidate.compressor = None  # never reset the live compressor in preview
        candidate.set_session(path)
        candidate.reset()
        assert candidate._session_store is not None  # initialized by set_session
        if candidate._session_store.exists and not candidate.load():
            raise OSError("session_load_failed")
        candidate.compressor = self.compressor
        return candidate

    def adopt_session(self, candidate):
        """Called after the agent resets its per-session runtime state."""
        self.__dict__.update(candidate.__dict__)

    def load(self) -> bool:
        if not self._session_path:
            return False
        if not self._session_store:
            self._session_store = SessionStore(self._session_path)
        if not self._session_store.exists:
            return False
        try:
            data = self._session_store.load()
            # Verify media before changing any live prompt/persona/history state.
            raw = data.get("messages", [])
            migrated = _restore_legacy_reasoning_content(_strip_images_from_messages(raw))
            from .appshot_media import hydrate_content
            for message in migrated:
                hydrate_content(message.get("content"), self.session_path)
            self.system_projection = SystemPromptProjection(data.get("system_prompt_projection"))
            self.runtime_projection = RuntimeContextProjection(data.get("runtime_context_projection"))
            migration = data.get("system_prompt_migration")
            self._system_prompt_migration = dict(migration) if isinstance(migration, dict) else {}
            loaded_prompt = data.get("system_prompt")
            if isinstance(loaded_prompt, str) and loaded_prompt.strip():
                persona_id = data.get("persona_id") if isinstance(data.get("persona_id"), str) else ""
                persona_version = data.get("persona_definition_version", 0)
                state_revision = data.get("persona_state_revision", 0)
                if not isinstance(persona_version, int):
                    persona_version = 0
                if not isinstance(state_revision, int) or state_revision < 0:
                    state_revision = 0
                raw_state = data.get("persona_state")
                raw_state = raw_state if isinstance(raw_state, dict) else {}

                def state_text(key: str) -> str:
                    value = raw_state.get(key)
                    return value if isinstance(value, str) else ""

                active_mode = state_text("active_mode")
                relationship_context = state_text("relationship_context")
                affect = state_text("affect")
                # Migrate before persona normalization: that may rebuild the
                # base itself, but cannot be allowed to hide a contaminated
                # persisted projection or discard the original backup.
                self.system_prompt = loaded_prompt
                self._migrate_system_suffix()
                retiring_persona = is_legacy_persona_prompt(self.system_prompt, persona_id or None)
                if retiring_persona:
                    active_mode = relationship_context = affect = ""
                self.system_prompt = normalize_system_prompt(
                    self.system_prompt,
                    persona_id=persona_id or None,
                    persona_version=persona_version,
                    state_revision=state_revision,
                    active_mode=active_mode,
                    relationship_context=relationship_context,
                    affect=affect,
                )
                if self.system_prompt != loaded_prompt:
                    # Do not retain a retired persona or core body through a
                    # previously saved projection head.
                    self.system_projection.reset()
                metadata = parse_persona_metadata(self.system_prompt)
                self.persona_id = metadata.persona_id if metadata else ""
                self.persona_definition_version = metadata.definition_version if metadata else 0
                self.persona_state_revision = metadata.state_revision if metadata else 0
                if metadata:
                    profile = get_prompt_profile(metadata.persona_id)
                    self.persona_active_mode = active_mode or profile.active_mode
                    self.persona_relationship_context = relationship_context
                    self.persona_affect = affect
                else:
                    self.persona_active_mode = ""
                    self.persona_relationship_context = ""
                    self.persona_affect = ""
                self._system_token_cost = _estimate_value_tokens(self.effective_system_prompt)
            self.messages = _strip_orphan_tool_calls(migrated)
            retired_sidecar = False
            for message in self.messages:
                retired_sidecar = message.pop("api_content", None) is not None or retired_sidecar
            self._rebuild_token_cache()
            self.show_reasoning = data.get("show_reasoning", True)
            self.total_prompt_tokens = data.get("total_prompt_tokens", 0)
            self.total_completion_tokens = data.get("total_completion_tokens", 0)
            self.total_cache_hit_tokens = data.get("total_cache_hit_tokens", 0)
            self.total_cache_miss_tokens = data.get("total_cache_miss_tokens", 0)
            self.last_prompt_tokens = 0  # 加载后重置，让 estimate_prompt_tokens 走实际计算
            # Force one replace event on the next save so legacy dynamic
            # memory sidecars are removed from durable session history too.
            self._saved_message_count = 0 if retired_sidecar else len(self.messages)
            return True
        except KeyError:
            return False

    def add_user(self, content: Any, *, provenance: str = ""):
        message = {"role": "user", "content": content}
        text = content if isinstance(content, str) else "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ) if isinstance(content, list) else ""
        if needs_relative_date_anchor(text):
            # Persist only date/offset, never injected prose in the user's
            # text. Reconstruct the same provider-only anchor on later turns
            # and restore; a transient anchor would invalidate cached history.
            message["relative_date"] = relative_date_metadata()
        if provenance:
            message["provenance"] = provenance
        self._append_message(message)

    def replace_message_content(self, index: int, content: Any):
        if index < 0 or index >= len(self.messages):
            return
        self.messages[index]["content"] = content
        # The sidecar is byte-exact for the old content. Replaying it after a
        # rewrite would silently resurrect content the caller intentionally
        # replaced (for example stripped image data).
        self.messages[index].pop("api_content", None)
        self._ensure_token_cache()
        self._message_token_costs[index] = self._message_cost(self.messages[index])

    def remove_last_message(self, *, role: str | None = None) -> dict | None:
        """Drop the trailing message, optionally only when it has this role.

        Returns the removed message, or None when nothing matched. Callers
        persist separately (see save).
        """
        if not self.messages:
            return None
        if role is not None and self.messages[-1].get("role") != role:
            return None
        removed = self.messages.pop()
        if len(self._message_token_costs) == len(self.messages) + 1:
            self._message_token_costs.pop()
        return removed

    def add_assistant(self, content: str, tool_calls: list | None = None):
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._append_message(msg)

    def add_assistant_raw(self, msg: dict):
        self._append_message(msg)

    def add_tool(self, tool_call_id: str, content: str):
        self._append_message({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def _append_message(self, msg: dict):
        if msg.get("role") in {"user", "assistant"}:
            msg["timestamp"] = _valid_message_timestamp(msg.get("timestamp")) or time.time()
        self.messages.append(msg)
        self._message_token_costs.append(self._message_cost(msg))

    def _message_cost(self, msg: dict) -> int:
        return _estimate_value_tokens(self._provider_message(msg))

    def _provider_message(self, stored: dict, content_override: Any = _NO_OVERRIDE) -> dict:
        model_message = copy.deepcopy(stored)
        timestamp = model_message.pop("timestamp", None)
        date_metadata = model_message.pop("relative_date", None)
        # Old sessions may contain the retired memory-injection sidecar.
        # Never replay it: current user and assistant messages are canonical.
        model_message.pop("api_content", None)
        model_message.pop("provenance", None)
        model_message.pop("display_command", None)
        if content_override is not _NO_OVERRIDE:
            model_message["content"] = copy.deepcopy(content_override)
        from .appshot_media import hydrate_content
        model_message["content"] = hydrate_content(model_message.get("content"), self.session_path)
        if model_message.get("role") == "user":
            marker = _model_timestamp_marker(timestamp)
            model_message["content"] = _timestamped_content(
                model_message.get("content"), marker
            )
            if date_metadata is not None:
                anchor = relative_date_anchor(date_metadata)
                if anchor:
                    model_message["content"] = _timestamped_content(model_message.get("content"), anchor + "\n")
        return model_message

    def _rebuild_token_cache(self):
        self._message_token_costs = [self._message_cost(msg) for msg in self.messages]

    def _ensure_token_cache(self):
        if len(self._message_token_costs) != len(self.messages):
            self._rebuild_token_cache()

    def get_prompt(
        self,
        content_overrides: dict[int, Any] | None = None,
        *,
        runtime_context: str | None = None,
        include_runtime_context: bool = True,
    ) -> list[dict]:
        """Build provider context with optional non-canonical message content.

        Overrides are applied only to the returned deep copy. The canonical
        message list remains safe for checkpoints and session persistence.
        Work-mode runtime context is recorded separately: None replays existing
        snapshots, an empty string clears current state, and unchanged text is
        deduplicated. Restricted interaction modes can exclude this projection.
        """
        result = []
        effective_system = self.effective_system_prompt
        if effective_system:
            result.append({"role": "system", "content": effective_system})
        inserts = self.runtime_projection.project(self.messages, runtime_context) if include_runtime_context else {}
        result.extend(inserts.get(0, []))
        for index, stored in enumerate(self.messages):
            override = _NO_OVERRIDE
            if content_overrides is not None and index in content_overrides:
                override = content_overrides[index]
            message = self._provider_message(stored, override)
            result.append(message)
            result.extend(inserts.get(index + 1, []))
        return result

    def _runtime_context_tokens(self) -> int:
        return sum(
            _estimate_value_tokens(message)
            for items in self.runtime_projection.project(self.messages).values()
            for message in items
        )

    def _compaction_messages(self) -> list[dict]:
        """Build canonical compaction input without provider-only time markers."""
        result = []
        effective_system = self.effective_system_prompt
        if effective_system:
            result.append({"role": "system", "content": effective_system})
        for stored in self.messages:
            message = copy.deepcopy(stored)
            message.pop("api_content", None)
            result.append(message)
        return result

    def sanitize_tool_history(self) -> bool:
        """Repair tool protocol chains before persistence or provider replay."""
        cleaned = _strip_orphan_tool_calls(self.messages)
        if cleaned == self.messages:
            return False
        self.messages = cleaned
        self._rebuild_token_cache()
        return True

    def estimate_prompt_tokens(self) -> int:
        """估算 prompt token 数，用于提前压缩。

        优先用 API 返回的精确 last_prompt_tokens（来自 llama.cpp 等），
        没有则用缓存的 system+tools 固定开销 + 消息 token 估算。
        """
        if self.last_prompt_tokens > 0:
            return self.last_prompt_tokens
        self._ensure_token_cache()
        return max(1, (
            self._system_token_cost + self._tools_token_cost
            + sum(self._message_token_costs) + self._runtime_context_tokens()
        ))

    def prompt_token_breakdown(self) -> dict[str, Any]:
        """Return a cheap, model-agnostic breakdown for local diagnostics."""
        self._ensure_token_cache()
        role_tokens: dict[str, int] = {}
        role_messages: dict[str, int] = {}
        for message, cost in zip(self.messages, self._message_token_costs):
            role = str(message.get("role") or "unknown")
            role_tokens[role] = role_tokens.get(role, 0) + int(cost)
            role_messages[role] = role_messages.get(role, 0) + 1
        runtime_tokens = self._runtime_context_tokens()
        estimated_current = max(
            1,
            self._system_token_cost + self._tools_token_cost + sum(self._message_token_costs) + runtime_tokens,
        )
        return {
            "system": int(self._system_token_cost),
            "tools": int(self._tools_token_cost),
            "messages": int(sum(self._message_token_costs)),
            "runtime_context": runtime_tokens,
            "estimated_current": estimated_current,
            "last_request": int(self.last_prompt_tokens),
            "limit": int(self.max_prompt_tokens),
            "message_count": len(self.messages),
            "role_tokens": role_tokens,
            "role_messages": role_messages,
        }

    def estimate_compaction_tokens(self) -> int:
        """Fresh estimate; never compare old API usage with a cheaper fallback."""
        estimate = getattr(getattr(self.compressor, "llm", None), "estimate_tokens", None)
        if callable(estimate):
            return max(1, cast(Callable[[list[dict]], int], estimate)(self.get_prompt()))
        self._ensure_token_cache()
        return max(1, (
            self._system_token_cost + self._tools_token_cost
            + sum(self._message_token_costs) + self._runtime_context_tokens()
        ))

    def reset(self):
        self.system_projection.reset()
        self.runtime_projection.reset()
        self.messages.clear()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cache_hit_tokens = 0
        self.total_cache_miss_tokens = 0
        self.last_prompt_tokens = 0
        self._saved_message_count = 0
        self._message_token_costs.clear()
        self._last_compaction_report = None
        if self.compressor is not None:
            self.compressor.reset()
        # system + tools token costs are preserved (tied to agent config, not conversation)

    def report_compaction(self, status: str, messages_before: int, *, details: dict | None = None) -> None:
        """Report lifecycle and estimates without exposing conversation content."""
        if self.compaction_observer is not None:
            try:
                self.compaction_observer({
                    **(details or {}),
                    "type": "context_compaction", "status": status,
                    "messages_before": messages_before, "messages_after": len(self.messages),
                })
            except Exception:
                logger.warning("compaction observer failed")

    async def compress_if_needed(
        self, force: bool = False, *, preserve_on_failure: bool = False,
        measure_tokens: Callable[[], int] | None = None,
    ):
        """Compact history when enabled and the token budget requires it."""
        if not self.compaction_enabled:
            return
        measure = measure_tokens or self.estimate_compaction_tokens
        tokens_before = measure()
        if not force and max(tokens_before, self.last_prompt_tokens) < self.max_prompt_tokens:
            return
        before = len(self.messages)
        # Leave room for the next tool results instead of stopping just below
        # the trigger and immediately compacting again on the next iteration.
        target = max(1, int(self.max_prompt_tokens * (1.0 if force else 0.9)))
        report: dict[str, Any] = {
            "tokens_before": tokens_before, "target_tokens": target,
            "method": "none", "dropped_steps": 0, "cleared_results": 0,
        }
        self.report_compaction("started", before, details=report)
        outcome = "failed"
        try:
            changed = await self._compress_history(
                force, preserve_on_failure=preserve_on_failure,
                measure_tokens=measure, target_tokens=target, report=report,
            )
            outcome = "completed" if changed else "failed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            try:
                report["tokens_after"] = measure()
                if report["method"] != "none":
                    self.last_prompt_tokens = report["tokens_after"]
            except Exception:
                # Optional diagnostics must never mask cancellation or failure.
                logger.warning("compaction token readback failed")
            self._last_compaction_report = dict(report)
            self.report_compaction(outcome, before, details=report)

    async def _compress_history(
        self, force: bool, *, preserve_on_failure: bool,
        measure_tokens: Callable[[], int], target_tokens: int, report: dict,
    ):

        # Superseded runtime snapshots are useful only for prefix reuse. Retire
        # them under budget pressure before sacrificing canonical conversation.
        changed = self.runtime_projection.compact(self.messages)
        if changed:
            report["method"] = "cleanup"
            if not force and measure_tokens() <= target_tokens:
                return True

        # Cheapest-first: deterministic layers before the expensive LLM summary.
        # Drop completed read/network tool steps (side-effecting and current-turn
        # steps stay), then clear old read tool results in place. When those
        # sufficed, skip the summary entirely.
        from .deterministic_compact import drop_completed_steps
        from .micro_compact import micro_compact_tool_results

        risk = self.tool_risk_provider or (lambda name: "read")
        is_request_local = self.tool_request_local_provider or (lambda name: False)
        self.messages, drop_stats = drop_completed_steps(
            self.messages,
            tool_risk=risk,
            tool_request_local=is_request_local,
            keep_recent=self.compact_keep_recent,
        )
        self.messages, micro_stats = micro_compact_tool_results(self.messages, tool_risk=risk)
        report.update(dropped_steps=drop_stats.dropped_steps, cleared_results=micro_stats.cleared_results)
        history_changed = bool(drop_stats.dropped_steps or micro_stats.cleared_results)
        changed = changed or history_changed
        if history_changed:
            self._message_token_costs.clear()
            self.last_prompt_tokens = 0
            self._saved_message_count = 0
            report["method"] = "cleanup"
        remaining = measure_tokens()
        if not force and remaining <= target_tokens:
            return changed

        if self.compressor is not None:
            prompt = self._compaction_messages()
            options = {"force": force}
            if preserve_on_failure:
                options["preserve_on_failure"] = True
            compressed = await self.compressor.compress(prompt, target_tokens, **options)
            if compressed is prompt:
                # Compressor declined (nothing safely compressible, or the
                # summary failed on a non-forced pass). Keep history and save
                # bookkeeping untouched; a forced retry may still act.
                return changed
            # The compressor's head is the effective prompt (base + runtime
            # suffix). It is input context, never authority to replace the base.
            if compressed and compressed[0].get("role") == "system":
                self.messages = compressed[1:]
            else:
                self.messages = compressed
            self._message_token_costs.clear()  # invalidate cache
            self.last_prompt_tokens = 0
            self._saved_message_count = 0
            report["method"] = "summary"
            return True

        if preserve_on_failure:
            return changed

        # ── Legacy fallback: hard truncation ──
        token_excess = max(0, remaining - target_tokens)
        self._ensure_token_cache()
        spans: list[tuple[int, int, str, int]] = []
        idx = 0
        first_user_seen = False
        while idx < len(self.messages):
            msg = self.messages[idx]

            if msg["role"] == "tool":
                idx += 1
                continue

            if msg["role"] == "assistant" and msg.get("tool_calls"):
                tc_ids = {tc["id"] for tc in msg["tool_calls"] if "id" in tc}
                j = idx + 1
                found = set()
                while j < len(self.messages) and self.messages[j]["role"] == "tool":
                    tid = self.messages[j].get("tool_call_id", "")
                    if tid in tc_ids:
                        found.add(tid)
                    j += 1
                if tc_ids and found:
                    cost = sum(self._message_token_costs[idx:j])
                    spans.append((idx, j, "tool_chain", cost))
                    idx = j
                    continue

            role = msg.get("role", "")
            if role == "user":
                priority = "first_user" if not first_user_seen else "user"
                first_user_seen = True
            elif role == "assistant":
                priority = "assistant"
            else:
                priority = "other"
            cost = self._message_token_costs[idx]
            spans.append((idx, idx + 1, priority, cost))
            idx += 1

        priority_order = {"tool_chain": 0, "assistant": 1, "other": 2, "user": 3, "first_user": 4}
        candidates = sorted(spans, key=lambda item: (priority_order[item[2]], item[0]))
        selected: list[tuple[int, int]] = []
        removed_tokens = 0
        for start, end, _priority, cost in candidates:
            selected.append((start, end))
            removed_tokens += cost
            if removed_tokens >= token_excess:
                break

        summaries = []
        for start, end in selected:
            for i in range(start, end):
                if self.messages[i].get("role") == "user":
                    content = self.messages[i].get("content", "")
                    if isinstance(content, str) and len(content.strip()) > 10:
                        summaries.append(content.strip()[:80])

        for start, end in sorted(selected, reverse=True):
            del self.messages[start:end]
            del self._message_token_costs[start:end]

        if summaries:
            text = "[上下文摘要] " + " | ".join(summaries[:6])
            summary_msg = {"role": "assistant", "content": text}
            self.messages.insert(0, summary_msg)
            self._message_token_costs.insert(0, self._message_cost(summary_msg))
        if selected:
            self._saved_message_count = 0
            report["method"] = "truncate"
        return changed or bool(selected)
