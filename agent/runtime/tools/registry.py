"""ToolRegistry — 工具注册、定义、执行"""

import asyncio
import contextvars
import copy
import inspect
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..hooks import HookRegistry, HookReject
from ..metrics import runtime_metrics
from ..tool_failure import ToolFailure
from ..tool_execution import ExecutionFailure, ExecutionResult
from ..tracing import trace_span
from .policy import VALID_RISKS, ToolPolicy

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]
ApprovalHandler = Callable[[dict[str, Any]], Awaitable[str]]
ApprovalAuditHandler = Callable[[dict[str, Any]], None | Awaitable[None]]
PermissionCleanup = Callable[[], None]
ApprovalFormatter = Callable[[dict], dict[str, str]]
CompletionFinalizer = Callable[
    [dict[str, Any], dict[str, Any], str],
    None | Awaitable[None],
]

_APPROVAL_DESCRIPTOR_FIELDS = (
    "approval_title",
    "approval_summary",
    "approval_effect",
    "approval_boundary",
    "approval_question",
)
_MAX_APPROVAL_JUSTIFICATION_CHARS = 2_000

_OMISSION_MARKER = "\n\n... [middle omitted] ...\n\n"
_REQUEST_LOCAL_RESULT_PLACEHOLDER = (
    "[Request-local tool result omitted after its one permitted model request.]"
)


def approval_justification_schema() -> dict[str, dict[str, str]]:
    """Return the public schema for an optional agent-authored explanation."""

    return {
        "reason": {
            "type": "string",
            "description": (
                "Optional one-sentence, user-facing explanation of why this exact operation is "
                "needed when this call may need approval. Prefer a concise, declarative "
                "statement (for example: 'I need to run the project memory tests to verify "
                "that the change did not break existing behavior.'). Do not include command "
                "output, metadata, approval "
                "instructions, or a second operation. This does not grant permission or "
                "change the requested target or scope."
            ),
        }
    }


def _normalize_approval_justification(value: Any) -> tuple[str, str | None]:
    if value is None:
        return "", None
    if not isinstance(value, str):
        return "", "approval reason must be a string"
    # Approval copy is untrusted model text. Keep it single-line and strip
    # control characters so a provider cannot forge terminal/UI structure.
    text = "".join(
        char if char == "\t" or ord(char) >= 32 else " "
        for char in value
    )
    text = " ".join(text.split())
    return text[:_MAX_APPROVAL_JUSTIFICATION_CHARS].strip(), None


def _bounded_head_tail(text: str, max_chars: int, head_ratio: float = 0.7) -> str:
    """Return a deterministic code-point-safe head/tail slice within ``max_chars``."""

    budget = max(0, int(max_chars))
    if len(text) <= budget:
        return text
    if budget <= len(_OMISSION_MARKER):
        return _OMISSION_MARKER[:budget]
    retained = budget - len(_OMISSION_MARKER)
    head_chars = max(1, min(retained, int(retained * head_ratio)))
    tail_chars = retained - head_chars
    tail = text[-tail_chars:] if tail_chars else ""
    return text[:head_chars] + _OMISSION_MARKER + tail


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    fn: Callable
    sandboxed: bool = False
    timeout: float | None = 30.0
    max_retries: int = 0
    retry_delay: float = 0.2
    risk: str = "read"
    approval: str = "never"
    output_schema: dict | None = None
    idempotent: bool = False
    # Explicit scheduling capability; network risk alone says nothing about
    # whether an operation is safe to overlap with another tool call.
    parallel_safe: bool = False
    # Cross-process recovery contract. ``safe`` permits re-executing an
    # interrupted (unknown) durable step; ``never`` requires manual recovery.
    # This is deliberately separate from same-process retry/idempotency.
    replay: str = "never"
    cache_ttl: float | None = None
    cancellable: bool = True
    max_calls_per_turn: int | None = None
    repeat_guard: bool = True
    group: str = "core"
    max_inline_chars: int | None = None
    expose_by_default: bool = True
    # Compatibility aliases may execute while absent from default schemas.
    # This never overrides explicit mode allowlists or authorization policy.
    allow_hidden_execution: bool = False
    # A successful direct tool result is also the assistant's final response.
    # This is used by atomic interaction modes where visible prose and local
    # state changes must commit together instead of requiring a second LLM pass.
    return_direct: bool = False
    # Opt-in extractor for a small, durable, verified fact from a successful
    # tool event. Raw tool output is never retained automatically.
    memory_evidence: Callable[[dict], str | None] | None = None
    # Optional correlation metadata extractor. Values are kept out of the
    # model-visible output and attached to the enclosing ReAct trace span.
    trace_context: Callable[[dict, dict], dict[str, str]] | None = None
    # Optional postcondition verifier: receives (args, result) and returns
    # (passed: bool, detail: str). A failed or crashed verifier turns the
    # execution into a tool error while preserving the raw output for audit.
    postcondition: Callable[[dict, dict], tuple[bool, str]] | None = None
    # Finalize a handler-owned transaction after postconditions/after hooks but
    # before the authoritative result is observed or returned. The call ID is
    # the same hidden ID supplied to opted-in tool handlers.
    completion_finalizer: CompletionFinalizer | None = None
    # Optional preflight for call-specific permissions such as an exact host
    # path. It returns a small, frontend-safe approval request or None.
    permission_check: Callable[
        [dict],
        dict[str, Any] | None | Awaitable[dict[str, Any] | None],
    ] | None = None
    # Apply a one-shot or process-scoped approval. A one-shot grant returns a
    # cleanup callback which is always run after the original call finishes.
    permission_grant: Callable[[dict, dict[str, Any], str], PermissionCleanup | None] | None = None
    # Release request-local authorization preparation on success, denial,
    # pause, unknown outcome, cancellation, and every exceptional exit.
    permission_finalizer: Callable[[dict], None] | None = None
    # When true, ``permission_check`` is the sole interactive authorization
    # prompt for this call. Policy modes and YOLO may not satisfy, broaden, or
    # suppress the exact resource scope; explicit deny rules still win.
    permission_authoritative: bool = False
    # Explicit opt-in: YOLO supplies an automatic once decision, while exact
    # preflight, grant validation, cleanup, and finalization still run.
    permission_yolo_auto_grant: bool = False
    # Backend-generated, user-facing description. This may explain the call,
    # but it cannot change the authoritative tool/risk/scope fields.
    approval_formatter: ApprovalFormatter | None = None
    # Whether ``reason`` is reserved as optional model-authored approval copy
    # instead of being forwarded as a normal tool argument.
    approval_justification: bool = False
    # False means results must never satisfy either the in-turn cache or a
    # later durable TaskStore claim.
    cache_results: bool = True
    # ``request_local`` makes the full successful result available to exactly
    # the next model request while durable context and stores receive only a
    # content-free placeholder. It also bypasses result-artifact persistence.
    result_persistence: str = "durable"
    # ``request_local`` routes durable task/events and observational hooks
    # through the argument redactor (or a content-free fallback). Runtime policy
    # and the tool function still receive the original validated arguments.
    argument_persistence: str = "durable"
    # For request-local arguments, transform a deep copy for durable stores
    # instead of replacing the whole shape with a placeholder. Keeps the
    # structure readable (and un-imitatable by models) while hiding sensitive
    # fields such as credentials. Ordinary fields may remain intact.
    # When None, the historical placeholder shape
    # is used as a safe fallback.
    argument_redactor: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Optional semantic validation after transforms/JSON-schema checks and
    # before policy/approval decisions. Raise TypeError/ValueError with a bounded
    # model-facing reason when structural schema alone cannot express a rule.
    argument_validator: Callable[[dict[str, Any]], None] | None = None
    # Opt in to enforcement of enum/const/range/collection-size keywords.
    # Existing tools retain their historical structural-schema behavior.
    strict_schema: bool = False


@dataclass(frozen=True)
class ToolPrivateResult:
    """A public tool result plus ephemeral data excluded from shaping and hooks."""

    output: str
    private: dict[str, Any]


@dataclass
class _PrivateResultSlot:
    """One execution-attempt side channel inherited by async/thread contexts."""

    payload: dict[str, Any] | None = None

    def capture(self, private: dict[str, Any]) -> None:
        if self.payload is not None:
            raise RuntimeError("A tool execution returned multiple private payloads")
        self.payload = copy.deepcopy(private)

    def retrieve(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.payload)


_PRIVATE_RESULT_SLOT: contextvars.ContextVar[_PrivateResultSlot | None] = (
    contextvars.ContextVar("tool_private_result_slot", default=None)
)


@dataclass
class _TimeoutFailureSlot:
    """Mutable attempt-local state shared with the task cancelled by wait_for."""

    failure: ToolFailure | None = None


_TIMEOUT_FAILURE_SLOT: contextvars.ContextVar[_TimeoutFailureSlot | None] = (
    contextvars.ContextVar("tool_timeout_failure_slot", default=None)
)


def _set_tool_timeout_failure(failure: ToolFailure) -> None:
    """Record handler-owned uncertainty without changing tool arguments/results."""
    slot = _TIMEOUT_FAILURE_SLOT.get()
    if slot is not None:
        slot.failure = copy.deepcopy(failure)


@dataclass
class _YoloState:
    enabled: bool = False


class ToolRegistry:
    ACTIVATE_GROUP_TOOL = "activate_tool_group"

    _project_hooks_loaded: bool = False

    def __init__(
        self,
        policy: ToolPolicy | None = None,
        artifact_dir: str | Path | None = None,
        max_inline_chars: int | None = None,
        hooks: HookRegistry | None = None,
    ):
        self._tools: dict[str, ToolDef] = {}
        self._tool_owners: dict[str, str] = {}
        self._schema_revision = 0
        self.policy = policy or ToolPolicy.from_env()
        self.hooks = hooks or HookRegistry()
        self.approval_handler: ApprovalHandler | None = None
        self.approval_audit_handler: ApprovalAuditHandler | None = None
        # Resolved filesystem policy of the registered file tools; runtime
        # bookkeeping uses it to resolve tool-reported paths to the stable
        # identity the capture chain records (M2 review P2).
        self.filesystem_policy: Any | None = None
        # YOLO is a transient bypass. It must not create durable grants,
        # otherwise turning it off would not restore the approval boundary.
        self._yolo_state = _YoloState()
        # Resource-scoped session grants shared by independently registered
        # tool families (for example browser_open and fetch_url on one origin).
        self.approved_permission_scopes: set[str] = set()
        # Policy approvals accepted through the interactive broker are also
        # session-scoped. Explicit /permissions overrides remain process-local.
        self._session_policy_allows: set[str] = set()
        self.hooks.on_session_end(self._clear_session_approvals)
        self.max_inline_chars = max_inline_chars or self._positive_int_env("TOOL_MAX_INLINE_CHARS", 12_000)
        self.max_fresh_chars = self._positive_int_env("TOOL_MAX_FRESH_RESULT_CHARS", 100_000)
        configured_dir = os.getenv("TOOL_RESULT_DIR", "").strip()
        self.artifact_dir = Path(artifact_dir or configured_dir or ".astra/tool-results").expanduser().resolve()

    def set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        """Attach an interactive approval broker supplied by a frontend."""
        self.approval_handler = handler

    def set_approval_audit_handler(self, handler: ApprovalAuditHandler | None) -> None:
        self.approval_audit_handler = handler

    async def _audit_approval(self, event: dict[str, Any]) -> None:
        if self.approval_audit_handler is None:
            return
        try:
            result = self.approval_audit_handler(dict(event))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("approval audit handler failed")

    def _clear_session_approvals(self, session_id: str, reason: str) -> None:
        del session_id, reason
        self.approved_permission_scopes.clear()
        self.policy.clear_allowed_calls(self._session_policy_allows)
        self._session_policy_allows.clear()

    @staticmethod
    def _safe_argument_summary(args: dict) -> dict[str, str]:
        summary: dict[str, str] = {}
        for key, value in args.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("secret", "token", "password", "api_key")):
                summary[str(key)] = "<redacted>"
                continue
            text = str(value)
            if lowered in {"content", "code", "patch"} and len(text) > 240:
                summary[str(key)] = f"<{len(text)} chars preserved>"
            else:
                summary[str(key)] = text if len(text) <= 240 else text[:237] + "..."
        return summary

    @staticmethod
    def _safe_approval_descriptor(formatter: ApprovalFormatter | None, args: dict) -> dict[str, str]:
        if formatter is None:
            return {}
        try:
            raw = formatter(args)
        except Exception:
            logger.exception("approval formatter failed")
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            key: str(raw[key])[:4_000]
            for key in _APPROVAL_DESCRIPTOR_FIELDS
            if key in raw and str(raw[key]).strip()
        }

    @property
    def yolo(self) -> bool:
        return self._yolo_state.enabled

    @yolo.setter
    def yolo(self, enabled: bool) -> None:
        self._yolo_state.enabled = enabled

    def share_yolo_with(self, parent: "ToolRegistry") -> None:
        """Delegates observe the parent's live switch, including revocation."""
        self._yolo_state = parent._yolo_state

    def share_session_with(self, parent: "ToolRegistry") -> None:
        """Rebound tools keep the parent's policy, hooks, grants and audit sink.

        Session cleanup stays owned by the parent; do not register a callback
        that would retain every short-lived child registry until session end.
        Tool definitions and filesystem roots remain local to this registry.
        """
        self.policy = parent.policy
        self.hooks = parent.hooks
        self.approval_handler = parent.approval_handler
        self.approval_audit_handler = parent.approval_audit_handler
        self.share_yolo_with(parent)
        self.approved_permission_scopes = parent.approved_permission_scopes
        self._session_policy_allows = parent._session_policy_allows
        self.artifact_dir = parent.artifact_dir
        self.max_inline_chars = parent.max_inline_chars
        self.max_fresh_chars = parent.max_fresh_chars

    async def _request_approval(
        self,
        *,
        call_id: str,
        tool: ToolDef,
        args: dict,
        reason: str,
        agent_reason: str = "",
        request: dict[str, Any] | None = None,
        bypass_yolo: bool = True,
    ) -> str | None:
        if self.yolo and bypass_yolo:
            return "once"
        request_id = uuid.uuid4().hex
        descriptor = self._safe_approval_descriptor(tool.approval_formatter, args)
        payload = {
            "request_id": request_id,
            "call_id": call_id,
            "tool_name": tool.name,
            "risk": tool.risk,
            "kind": "tool_policy",
            "reason": reason,
            "arguments": self._safe_argument_summary(
                self.persistence_safe_args(tool, args)
            ),
            "verifier": (
                getattr(tool.postcondition, "__name__", "configured")
                if tool.postcondition is not None
                else ""
            ),
            **descriptor,
            **(request or {}),
        }
        payload["reason_source"] = "agent" if agent_reason else "backend"
        # Set after request descriptors: only the registered permission boundary
        # can opt a pending request into live YOLO, never tool/model arguments.
        payload["yolo_bypass_allowed"] = bypass_yolo or tool.permission_yolo_auto_grant
        if agent_reason:
            payload["agent_reason"] = agent_reason
        asked_at = time.time()
        audit_event = {
            "type": "approval_asked",
            "request_id": request_id,
            "call_id": call_id,
            "tool_name": tool.name,
            "risk": tool.risk,
            "kind": str(payload.get("kind") or "tool_policy"),
            "reason": reason,
            "reason_source": payload["reason_source"],
        }
        if agent_reason:
            audit_event["agent_reason"] = agent_reason
        try:
            await self._audit_approval(audit_event)
            if self.approval_handler is None:
                await self._audit_approval({
                    "type": "approval_decided",
                    "request_id": request_id,
                    "call_id": call_id,
                    "tool_name": tool.name,
                    "decision": "unavailable",
                    "duration_ms": 0,
                })
                return None
            raw_decision = str(await self.approval_handler(payload)).strip().lower()
            decision = raw_decision if raw_decision in {"once", "session", "deny"} else "deny"
        except BaseException as exc:
            await self._audit_approval({
                "type": "approval_decided",
                "request_id": request_id,
                "call_id": call_id,
                "tool_name": tool.name,
                "decision": "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                "duration_ms": int((time.time() - asked_at) * 1000),
                "error_type": type(exc).__name__,
            })
            raise
        await self._audit_approval({
            "type": "approval_decided",
            "request_id": request_id,
            "call_id": call_id,
            "tool_name": tool.name,
            "decision": decision,
            "duration_ms": int((time.time() - asked_at) * 1000),
        })
        return decision

    @staticmethod
    def _positive_int_env(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    @classmethod
    def _schema_errors(
        cls,
        schema: dict[str, Any] | None,
        value: Any,
        path: str = "$",
        *,
        strict: bool = False,
    ) -> list[str]:
        """Validate the JSON-schema subset used by local and MCP tools."""
        if not isinstance(schema, dict):
            return []

        for keyword in ("allOf",):
            variants = schema.get(keyword)
            if isinstance(variants, list):
                errors = []
                for variant in variants:
                    errors.extend(cls._schema_errors(variant, value, path, strict=strict))
                if errors:
                    return errors
        for keyword in ("anyOf", "oneOf"):
            variants = schema.get(keyword)
            if isinstance(variants, list) and variants:
                matches = sum(
                    not cls._schema_errors(variant, value, path, strict=strict)
                    for variant in variants
                )
                if (keyword == "anyOf" and matches < 1) or (keyword == "oneOf" and matches != 1):
                    return [f"{path} does not match {keyword}"]

        expected = schema.get("type")
        expected_types = [expected] if isinstance(expected, str) else expected
        if isinstance(expected_types, list) and expected_types:
            type_checks = {
                "object": lambda item: isinstance(item, dict),
                "array": lambda item: isinstance(item, list),
                "string": lambda item: isinstance(item, str),
                "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
                "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
                "boolean": lambda item: isinstance(item, bool),
                "null": lambda item: item is None,
            }
            known = [item for item in expected_types if item in type_checks]
            if known and not any(type_checks[item](value) for item in known):
                return [f"{path} must be {' or '.join(known)}"]

        if strict and "const" in schema and value != schema["const"]:
            return [f"{path} must equal {schema['const']!r}"]
        choices = schema.get("enum")
        if strict and isinstance(choices, list) and value not in choices:
            return [f"{path} must be one of {choices!r}"]

        errors: list[str] = []
        if isinstance(value, dict):
            required = schema.get("required", [])
            if isinstance(required, list):
                errors.extend(
                    f"{path}.{key} is required"
                    for key in required
                    if key not in value
                )
            properties = schema.get("properties", {})
            if isinstance(properties, dict):
                for key, item in value.items():
                    if key in properties:
                        errors.extend(
                            cls._schema_errors(properties[key], item, f"{path}.{key}", strict=strict)
                        )
                    elif schema.get("additionalProperties") is False:
                        errors.append(f"{path}.{key} is not allowed")
        elif isinstance(value, list):
            minimum_items = schema.get("minItems") if strict else None
            maximum_items = schema.get("maxItems") if strict else None
            if (
                isinstance(minimum_items, int)
                and isinstance(maximum_items, int)
                and not minimum_items <= len(value) <= maximum_items
            ):
                errors.append(
                    f"{path} must contain between {minimum_items} and {maximum_items} items"
                )
            else:
                if isinstance(minimum_items, int) and len(value) < minimum_items:
                    errors.append(f"{path} must contain at least {minimum_items} items")
                if isinstance(maximum_items, int) and len(value) > maximum_items:
                    errors.append(f"{path} must contain at most {maximum_items} items")
            if strict and schema.get("uniqueItems") is True:
                try:
                    normalized = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
                except (TypeError, ValueError):
                    normalized = [repr(item) for item in value]
                if len(normalized) != len(set(normalized)):
                    errors.append(f"{path} contains duplicate items")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    errors.extend(
                        cls._schema_errors(item_schema, item, f"{path}[{index}]", strict=strict)
                    )
        elif isinstance(value, str):
            minimum_length = schema.get("minLength") if strict else None
            maximum_length = schema.get("maxLength") if strict else None
            if isinstance(minimum_length, int) and len(value) < minimum_length:
                errors.append(f"{path} must contain at least {minimum_length} characters")
            if isinstance(maximum_length, int) and len(value) > maximum_length:
                errors.append(f"{path} must contain at most {maximum_length} characters")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = schema.get("minimum") if strict else None
            maximum = schema.get("maximum") if strict else None
            if isinstance(minimum, (int, float)) and value < minimum:
                errors.append(f"{path} must be at least {minimum}")
            if isinstance(maximum, (int, float)) and value > maximum:
                errors.append(f"{path} must be at most {maximum}")
        return errors

    @staticmethod
    def _validate_definition(tool: ToolDef) -> None:
        if tool.parallel_safe and (not tool.idempotent or tool.risk not in {"read", "network"}):
            raise ValueError("parallel_safe requires an idempotent read/network tool")
        if tool.risk not in VALID_RISKS:
            raise ValueError(f"Invalid risk '{tool.risk}' for tool '{tool.name}'")
        if tool.replay not in {"never", "safe"}:
            raise ValueError(f"Invalid replay policy '{tool.replay}' for tool '{tool.name}'")
        if tool.max_calls_per_turn is not None and tool.max_calls_per_turn < 1:
            raise ValueError(f"max_calls_per_turn must be positive for tool '{tool.name}'")
        if tool.name == ToolRegistry.ACTIVATE_GROUP_TOOL:
            raise ValueError(f"Tool name is reserved: {tool.name}")
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", tool.group):
            raise ValueError(f"Invalid group '{tool.group}' for tool '{tool.name}'")
        if tool.max_inline_chars is not None and tool.max_inline_chars < 1:
            raise ValueError(f"max_inline_chars must be positive for tool '{tool.name}'")
        if tool.result_persistence not in {"durable", "request_local"}:
            raise ValueError(
                f"Invalid result persistence '{tool.result_persistence}' for tool '{tool.name}'"
            )
        if tool.argument_persistence not in {"durable", "request_local"}:
            raise ValueError(
                f"Invalid argument persistence '{tool.argument_persistence}' for tool '{tool.name}'"
            )
        if tool.permission_yolo_auto_grant and not tool.permission_authoritative:
            raise ValueError("YOLO exact auto-grants require authoritative permission handlers")
        if tool.permission_authoritative and (
            tool.permission_check is None or tool.permission_grant is None
        ):
            raise ValueError(
                f"Authoritative permission tool '{tool.name}' requires check and grant handlers"
            )

    @staticmethod
    def persistence_safe_args(tool: ToolDef | None, args: dict[str, Any]) -> dict[str, Any]:
        """Return arguments safe for durable events and observational hooks.

        Request-local tools may declare an ``argument_redactor`` that keeps
        the argument structure readable while hiding sensitive fields (credential
        text, opaque handles). Without a redactor the historical content-free
        placeholder shape is used so no raw values leak.
        """
        if tool is None or tool.argument_persistence == "durable":
            return copy.deepcopy(args)
        if tool.argument_redactor is not None:
            return tool.argument_redactor(copy.deepcopy(args))
        counts = {
            str(key): len(value)
            for key, value in args.items()
            if isinstance(value, (list, tuple, dict))
        }
        return {
            "request_local_placeholder": True,
            "argument_keys": sorted(str(key) for key in args),
            "collection_sizes": counts,
        }

    @staticmethod
    def persistence_safe_result(tool: ToolDef | None, result: dict[str, Any]) -> dict[str, Any]:
        """Return a result safe for durable stores and observational hooks."""
        if tool is None or tool.result_persistence == "durable":
            return copy.deepcopy(result)
        safe_keys = {
            "output",
            "output_truncated",
            "request_local_placeholder",
            "error",
            "code",
            "error_type",
            "recoverable",
            "retryable",
            "recovery_hint",
            "partial",
            "duration_ms",
            "risk",
            "verified",
            "verification_detail",
            "execution",
        }
        safe = {
            key: copy.deepcopy(value)
            for key, value in result.items()
            if key in safe_keys
        }
        if tool.name == "computer_act" and "computer_receipt" in result:
            from ..computer_forms import safe_action_receipt
            safe["computer_receipt"] = safe_action_receipt(result["computer_receipt"])
        return safe

    def register(self, tool: ToolDef, *, owner: str = ""):
        self._ensure_approval_question_required(tool)
        self._validate_definition(tool)
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        if owner:
            self._tool_owners[tool.name] = owner
        self._schema_revision += 1

    def replace_owned_tools(self, owner: str, tools: list[ToolDef]) -> None:
        """Atomically replace one provider's definitions without touching others."""
        clean_owner = str(owner).strip()
        if not clean_owner:
            raise ValueError("tool owner is required")
        names: set[str] = set()
        for tool in tools:
            self._ensure_approval_question_required(tool)
            self._validate_definition(tool)
            if tool.name in names:
                raise ValueError(f"Duplicate tool in replacement: {tool.name}")
            names.add(tool.name)
            existing_owner = self._tool_owners.get(tool.name, "")
            if tool.name in self._tools and existing_owner != clean_owner:
                raise ValueError(f"Tool already registered by another owner: {tool.name}")

        previous = {
            name for name, existing_owner in self._tool_owners.items()
            if existing_owner == clean_owner
        }
        schema_changed = previous != names or any(
            self._schema_signature(self._tools.get(tool.name))
            != self._schema_signature(tool)
            for tool in tools
        )
        for name in previous - names:
            self._tools.pop(name, None)
            self._tool_owners.pop(name, None)
        # Always replace the definitions so provider-specific invocation
        # closures point at the current connection. Only model-visible schema
        # changes invalidate the stable tool-schema cache.
        for tool in tools:
            self._tools[tool.name] = tool
            self._tool_owners[tool.name] = clean_owner
        if schema_changed:
            self._schema_revision += 1

    @staticmethod
    def _ensure_approval_question_required(tool: ToolDef) -> None:
        """Validate that model-authored approval copy is exposed as an optional field."""
        if not tool.approval_justification:
            return
        properties = tool.parameters.get("properties")
        if not isinstance(properties, dict) or not (
            "reason" in properties or "justification" in properties
        ):
            raise ValueError(
                f"Tool '{tool.name}' enables approval_justification but does not expose "
                "the reason property"
            )
        required = tool.parameters.get("required")
        if required is not None and not isinstance(required, list):
            raise ValueError(f"Tool '{tool.name}' has an invalid required schema")

    @staticmethod
    def _schema_signature(tool: ToolDef | None) -> tuple[Any, ...] | None:
        if tool is None:
            return None
        return (
            tool.name,
            tool.description,
            tool.parameters,
            tool.group,
            tool.expose_by_default,
        )

    @property
    def schema_revision(self) -> int:
        return self._schema_revision

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def groups(self) -> list[str]:
        return sorted({tool.group for tool in self._tools.values() if tool.expose_by_default})

    def tool_names_for_group(self, group: str) -> list[str]:
        return [
            tool.name for tool in self._tools.values()
            if tool.group == group and tool.expose_by_default
        ]

    def to_openai_tools(
        self,
        *,
        names: AbstractSet[str] | None = None,
        groups: AbstractSet[str] | None = None,
    ) -> list[dict]:
        tools = [
            tool for tool in self._tools.values()
            if (
                (names is not None and tool.name in names)
                or (
                    names is None
                    and tool.expose_by_default
                    and (groups is None or tool.group in groups)
                )
            )
        ]
        # Locale-independent name order keeps the model-visible tool prefix
        # byte-stable across plugin/MCP/skill registration order, matching
        # dsh's prompt-cache discipline.
        tools.sort(key=lambda tool: tool.name)
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def activation_tool_schema(self) -> dict | None:
        groups = [group for group in self.groups if group != "core"]
        if not groups:
            return None
        catalog = "; ".join(
            f"{group}: {', '.join(sorted(self.tool_names_for_group(group)))}"
            for group in groups
        )
        return {
            "type": "function",
            "function": {
                "name": self.ACTIVATE_GROUP_TOOL,
                "description": (
                    "Activate one additional tool group when the currently exposed tools are insufficient. "
                    f"Available groups and tools: {catalog}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"group": {"type": "string", "enum": groups}},
                    "required": ["group"],
                    "additionalProperties": False,
                },
            },
        }

    def select_groups(self, text: str) -> set[str]:
        """Cheap deterministic routing; the activation tool covers missed domains."""
        lower = (text or "").lower()
        # Python's Unicode ``\b`` treats CJK characters as word characters, so
        # mixed-language text such as ``comfyui开的吧`` has no word boundary
        # after ``comfyui``. Separate ASCII identifiers from adjacent non-ASCII
        # text before applying the domain patterns. This keeps substring guards
        # (for example, ``drawbridge`` must not match ``draw``) while routing
        # natural Chinese prompts that contain English product/tool names.
        lower = re.sub(
            r"(?<=[A-Za-z0-9_])(?=[^\x00-\x7f])|(?<=[^\x00-\x7f])(?=[A-Za-z0-9_])",
            " ",
            lower,
        )
        patterns = {
            "web": r"https?://|\b(search|web|news|latest|exa|searxng|url)\b|搜索|搜图|找图|参考图|联网|网页|新闻|最新|查资料",
            "files": r"[a-z]:[\\/]|[/\\]|\b(file|folder|directory|path|read|write|edit)\b|文件|目录|路径|读取|写入|保存|编辑",
            "code": r"\b(code|bug|test|python|shell|script|command|implement|debug)\b|代码|报错|测试|脚本|命令|实现|调试",
            "git": r"\bgit\b|\b(commit|branch|merge|diff|repository|repo)\b|提交|分支|合并|仓库",
            "memory": r"\b(memory|hindsight|recall|reflect)\b|记忆|记住|回忆|反思|工作计划|核心记忆",
            "skills": r"\bskills?\b|技能|学习复盘|learning review",
            "image": r"\.(png|jpe?g|webp|gif)\b|\b(image|picture|screenshot|comfyui|draw)\b|图片|图像|截图|生图|画图",
        }
        available = set(self.groups)
        return {group for group, pattern in patterns.items() if group in available and re.search(pattern, lower)}

    def describe(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "risk": tool.risk,
                "approval": tool.approval,
                "idempotent": tool.idempotent,
                "parallel_safe": tool.parallel_safe,
                "replay": tool.replay,
                "max_calls_per_turn": tool.max_calls_per_turn,
                "repeat_guard": tool.repeat_guard,
                "group": tool.group,
                "max_inline_chars": tool.max_inline_chars or self.max_inline_chars,
            }
            for tool in self._tools.values()
            if tool.expose_by_default
        ]

    async def execute(
        self,
        name: str,
        args: dict,
        sandbox=None,
        on_progress: ProgressCallback | None = None,
        call_id: str = "",
        task_id: str = "",
        execution_origin: str = "",
    ) -> dict:
        def report(
            stage: str,
            *,
            status: str = "running",
            message: str = "",
            current: float | None = None,
            total: float | None = None,
            percent: float | None = None,
            unit: str = "",
            turns_used: int | None = None,
            max_turns: int | None = None,
            turns_remaining: int | None = None,
        ) -> None:
            if on_progress is None:
                return
            payload: dict[str, Any] = {"stage": stage, "status": status}
            if message:
                payload["message"] = message
            if current is not None:
                payload["current"] = current
            if total is not None:
                payload["total"] = total
            if percent is not None:
                payload["percent"] = percent
            if unit:
                payload["unit"] = unit
            if turns_used is not None:
                payload["turns_used"] = turns_used
            if max_turns is not None:
                payload["max_turns"] = max_turns
            if turns_remaining is not None:
                payload["turns_remaining"] = turns_remaining
            try:
                on_progress(payload)
            except Exception:
                logger.exception("tool progress callback failed name=%s stage=%s", name, stage)

        tool = self._tools.get(name)
        if not tool:
            visible = sorted(item.name for item in self._tools.values() if item.expose_by_default)
            return {
                "output": "",
                "error": f"Tool '{name}' not found",
                "code": "invalid_arguments",
                "error_type": "invalid_input",
                "recoverable": True,
                "retryable": False,
                "recovery_hint": (
                    "Choose an exact name from the currently exposed tool schemas; do not invent an API. "
                    + ("Available tools: " + ", ".join(visible[:24]) if visible else "No tools are currently exposed.")
                ),
            }

        # ``reason`` is model-facing approval metadata, not a tool
        # argument. Strip it before hooks, policy checks, and execution so it
        # can never smuggle a second command/path or alter the authorization
        # decision. It is attached to the approval request separately.
        args = dict(args)
        agent_reason = ""
        justification_error = None
        if tool.approval_justification:
            agent_reason, justification_error = _normalize_approval_justification(
                args.pop("reason", args.pop("justification", None))
            )
        if justification_error:
            return {
                "output": "",
                "error": f"[ToolInputError] {name}: {justification_error}",
                "risk": tool.risk,
                "code": "invalid_arguments",
                "error_type": "invalid_input",
                "recoverable": True,
            }

        async def finalize(result: dict) -> dict:
            if tool.completion_finalizer is not None:
                try:
                    completion = tool.completion_finalizer(
                        dict(args), result, permission_call_id
                    )
                    if inspect.isawaitable(completion):
                        await completion
                except Exception:
                    logger.exception("tool completion finalizer failed name=%s", name)
            self.hooks.dispatch_tool_result(
                name,
                self.persistence_safe_args(tool, args),
                self.persistence_safe_result(tool, result),
                tool,
            )
            return result

        interactive_decision: str | None = None
        policy_reason = ""
        permission_cleanup: PermissionCleanup | None = None
        permission_args: dict[str, Any] | None = None
        permission_call_id = call_id or uuid.uuid4().hex
        # Preflight/permission checks can themselves raise TimeoutError before
        # any timed attempt has started; they have no handler-owned metadata.
        timeout_slot: _TimeoutFailureSlot | None = None
        # ── before_tool hooks ──
        try:
            args = self.hooks.dispatch_before_tool(name, args, tool)
        except HookReject as rej:
            report("blocked", status="failed", message=f"Hook rejected: {rej.reason}")
            return await finalize({
                "output": "",
                "error": f"[HookReject] {rej.reason}",
                "risk": tool.risk,
                "code": "sandbox_denied",
                "error_type": "hook_rejected",
                "recoverable": False,
            })
        try:
            sig = inspect.signature(tool.fn)
            params = sig.parameters
            public_params = {key: value for key, value in params.items() if not key.startswith("_")}
            required = [
                p.name for p in public_params.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            ]
            allowed = {
                p.name for p in public_params.values()
                if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
            accepts_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            missing = [key for key in required if key not in args]
            extra = [] if accepts_var_kwargs else [key for key in args if key not in allowed]
            if missing or extra:
                parts = []
                if missing:
                    parts.append(f"missing required: {', '.join(missing)}")
                if extra:
                    parts.append(f"unexpected: {', '.join(extra)}")
                message = f"[ToolInputError] {name}: {'; '.join(parts)}"
                report("invalid_input", status="failed", message=message)
                return await finalize({
                    "output": "",
                    "error": message,
                    "code": "invalid_arguments",
                    "error_type": "invalid_input",
                    "recoverable": True,
                })

            # ``reason`` is optional model-facing approval metadata. It is
            # deliberately removed before invoking the Python function; do not
            # re-apply it in runtime schema validation after extraction.
            runtime_schema = tool.parameters
            if tool.approval_justification:
                runtime_schema = {
                    **tool.parameters,
                    "required": [
                        key for key in tool.parameters.get("required", [])
                        if key != "reason"
                    ],
                }
            schema_errors = self._schema_errors(runtime_schema, args, strict=tool.strict_schema)
            if schema_errors:
                message = f"[ToolInputError] {name}: {'; '.join(schema_errors[:5])}"
                report("invalid_input", status="failed", message=message)
                return await finalize({
                    "output": "",
                    "error": message,
                    "code": "invalid_arguments",
                    "error_type": "invalid_input",
                    "recoverable": True,
                    "details": {"schema_errors": schema_errors[:20]},
                    "retryable": False,
                    "recovery_hint": "Correct the listed fields against this tool's current schema, then call it once with the corrected arguments. Do not repeat the unchanged call.",
                })

            if tool.argument_validator is not None:
                try:
                    tool.argument_validator(dict(args))
                except (TypeError, ValueError) as exc:
                    message = f"[ToolInputError] {name}: {str(exc)[:2_000]}"
                    report("invalid_input", status="failed", message=message)
                    return await finalize({
                        "output": "",
                        "error": message,
                        "risk": tool.risk,
                        "code": "invalid_arguments",
                        "error_type": "invalid_input",
                        "recoverable": True,
                    })

            # Typed hook decisions happen after structural validation. They
            # can tighten policy (ask/deny), but an allow can never bypass it.
            hook_decision = self.hooks.dispatch_tool_decision(name, args, tool)
            if hook_decision.kind == "deny":
                reason = hook_decision.reason or "denied by tool decision hook"
                report("blocked", status="failed", message=reason)
                return await finalize({
                    "output": "",
                    "error": f"[HookDecisionDenied] {reason}",
                    "risk": tool.risk,
                    "code": "sandbox_denied",
                    "error_type": "hook_decision_denied",
                    "recoverable": False,
                })

            permission_args = dict(args)
            permission_args["__permission_call_id"] = permission_call_id
            if execution_origin:
                permission_args["__execution_origin"] = execution_origin
            if agent_reason:
                permission_args["__approval_justification"] = agent_reason
            exact_permission_request = None
            if tool.permission_authoritative and tool.permission_check is not None:
                checked = tool.permission_check(permission_args)
                exact_permission_request = (
                    await checked if inspect.isawaitable(checked) else checked
                )

            # Authorization happens only after transform hooks and structural argument
            # validation, so malformed/incomplete actions can never trigger an
            # approval prompt.
            report("authorizing")
            allowed_by_policy, policy_reason = self.policy.authorize(
                name,
                tool.risk,
                tool.approval,
                args=args,
            )
            if allowed_by_policy and hook_decision.kind == "ask":
                allowed_by_policy = False
                policy_reason = hook_decision.reason or "approval requested by tool decision hook"
            if not allowed_by_policy:
                hard_denied = (
                    policy_reason.startswith("Denied by rule:")
                    or "is denied for this process" in policy_reason
                )
                if hard_denied:
                    report("blocked", status="failed", message=policy_reason)
                    return await finalize({
                        "output": "",
                        "error": f"[ToolDenied] {policy_reason}",
                        "risk": tool.risk,
                        "code": "sandbox_denied",
                        "error_type": "policy_denied",
                        "recoverable": False,
                    })
                if tool.permission_authoritative:
                    policy_reason = f"{policy_reason}; exact permission is authoritative"
                else:
                    report("awaiting_approval", message=policy_reason)
                    approval_started = time.perf_counter()
                    decision = await self._request_approval(
                        call_id=call_id,
                        tool=tool,
                        args=args,
                        reason=policy_reason,
                        agent_reason=agent_reason,
                    )
                    runtime_metrics.observe(
                        "approval_wait_ms",
                        (time.perf_counter() - approval_started) * 1000,
                    )
                    if decision is None:
                        report("blocked", status="failed", message=policy_reason)
                        return await finalize({
                            "output": "",
                            "error": (
                                f"[ToolApprovalRequired] {policy_reason}. "
                                f"Run /permissions allow {name} and retry."
                            ),
                            "risk": tool.risk,
                            "code": "approval_required",
                            "error_type": "approval_required",
                            "recoverable": True,
                        })
                    if decision == "deny":
                        message = f"User denied approval for {name}"
                        report("denied", status="failed", message=message)
                        return await finalize({
                            "output": "",
                            "error": f"[ToolApprovalDenied] {message}",
                            "risk": tool.risk,
                            "code": "approval_denied",
                            "error_type": "approval_denied",
                            "recoverable": False,
                        })
                    if decision == "session":
                        scope = self.policy.allow_call(name, args)
                        self._session_policy_allows.add(scope)
                    interactive_decision = decision
                    policy_reason = f"interactive {decision} approval"
                    report("approved", message=policy_reason)

            if tool.permission_check is not None and (
                tool.permission_authoritative or not self.yolo
            ):
                permission_request = (
                    exact_permission_request
                    if tool.permission_authoritative
                    else tool.permission_check(permission_args)
                )
                if inspect.isawaitable(permission_request):
                    permission_request = await permission_request
                if permission_request is not None:
                    reason = str(permission_request.get("reason") or f"{name} requires approval")
                    auto_grant = self.yolo and tool.permission_yolo_auto_grant
                    if not auto_grant:
                        report("awaiting_approval", message=reason)
                    if auto_grant:
                        decision = "once"
                    elif interactive_decision and not tool.permission_authoritative:
                        decision = interactive_decision
                    else:
                        approval_started = time.perf_counter()
                        decision = await self._request_approval(
                            call_id=call_id,
                            tool=tool,
                            args=args,
                            reason=reason,
                            agent_reason=agent_reason,
                            request=permission_request,
                            bypass_yolo=not tool.permission_authoritative,
                        )
                        runtime_metrics.observe(
                            "approval_wait_ms",
                            (time.perf_counter() - approval_started) * 1000,
                        )
                    if decision is None:
                        report("blocked", status="failed", message=reason)
                        detail = str(permission_request.get("detail") or "").strip()
                        return await finalize({
                            "output": "",
                            "error": f"[ToolApprovalRequired] {reason}" + (f". {detail}" if detail else ""),
                            "risk": tool.risk,
                            "code": "approval_required",
                            "error_type": "approval_required",
                            "recoverable": True,
                        })
                    if decision == "deny":
                        message = f"User denied approval for {name}"
                        report("denied", status="failed", message=message)
                        return await finalize({
                            "output": "",
                            "error": f"[ToolApprovalDenied] {message}",
                            "risk": tool.risk,
                            "code": "approval_denied",
                            "error_type": "approval_denied",
                            "recoverable": False,
                        })
                    if tool.permission_grant is None:
                        raise RuntimeError(f"{name} requested approval without a permission grant handler")
                    permission_cleanup = tool.permission_grant(args, permission_request, decision)
                    source = "YOLO automatic" if auto_grant else "interactive"
                    report("approved", message=f"{source} {decision} approval")

            with trace_span("agent.tool", {
                "agent.tool.name": name,
                "agent.tool.risk": tool.risk,
                "agent.tool.policy": self.policy.mode,
                "agent.tool.policy_reason": policy_reason,
            }):
                retryable = (TimeoutError, OSError, ConnectionError)
                start = time.perf_counter()
                total_attempts = tool.max_retries + 1
                for attempt in range(tool.max_retries + 1):
                    from ..turn_budget import check_work_budget
                    check_work_budget()
                    try:
                        report(
                            "running",
                            current=attempt + 1 if total_attempts > 1 else None,
                            total=total_attempts if total_attempts > 1 else None,
                        )
                        call_args = dict(args)
                        if "_permission_call_id" in params:
                            call_args["_permission_call_id"] = permission_call_id
                        if "_progress" in params:
                            call_args["_progress"] = report
                        if "_task_id" in params:
                            call_args["_task_id"] = task_id
                        async def invoke_tool(bound_call_args: dict[str, Any] = call_args) -> Any:
                            check_work_budget()
                            if asyncio.iscoroutinefunction(tool.fn):
                                raw_result = await tool.fn(**bound_call_args)
                            else:
                                raw_result = await asyncio.to_thread(
                                    tool.fn, **bound_call_args
                                )
                            if not isinstance(raw_result, ToolPrivateResult):
                                return raw_result
                            slot = _PRIVATE_RESULT_SLOT.get()
                            if slot is None:
                                raise RuntimeError("Private tool result side channel is unavailable")
                            slot.capture(raw_result.private)
                            return raw_result.output

                        private_slot = _PrivateResultSlot()
                        private_token = _PRIVATE_RESULT_SLOT.set(private_slot)
                        timeout_slot = _TimeoutFailureSlot()
                        timeout_token = _TIMEOUT_FAILURE_SLOT.set(timeout_slot)
                        try:
                            task = self.hooks.dispatch_around_tool(name, args, tool, invoke_tool)
                            if tool.timeout is None:
                                result = await task
                            else:
                                result = await asyncio.wait_for(task, timeout=tool.timeout)
                            private_result = private_slot.retrieve()
                        finally:
                            _TIMEOUT_FAILURE_SLOT.reset(timeout_token)
                            _PRIVATE_RESULT_SLOT.reset(private_token)
                        report("finalizing")
                        duration_ms = int((time.perf_counter() - start) * 1000)
                        if isinstance(result, ToolFailure):
                            failure_result = {
                                "output": "",
                                "error": result.message,
                                "code": result.code,
                                "error_type": result.code,
                                "recoverable": result.retryable,
                                "retryable": result.retryable,
                                "recovery_hint": result.recovery_hint,
                                "partial": result.partial,
                                "details": dict(result.details),
                                "duration_ms": duration_ms,
                                "risk": tool.risk,
                            }
                            if result.artifact_ref:
                                failure_result["artifact_ref"] = result.artifact_ref
                            report("failed", status="failed", message=result.message)
                            self.hooks.dispatch_tool_error(
                                name, self.persistence_safe_args(tool, args), result.message, tool
                            )
                            return await finalize(failure_result)
                        shaped = self._shape_output(
                            name,
                            str(result),
                            tool.max_inline_chars,
                            result_persistence=tool.result_persistence,
                        )
                        success_result = {**shaped, "error": "", "duration_ms": duration_ms, "risk": tool.risk}
                        if isinstance(result, ExecutionResult):
                            success_result["execution"] = dict(result.execution)
                        # ── postcondition verifier ──
                        if tool.postcondition is not None:
                            runtime_metrics.increment("postcondition_check_count")
                            try:
                                passed, detail = tool.postcondition(args, success_result)
                                success_result["verified"] = passed
                                if not passed:
                                    runtime_metrics.increment("postcondition_failure_count")
                                    if tool.result_persistence == "request_local":
                                        success_result.pop("fresh_output", None)
                                    success_result["verification_detail"] = detail
                                    success_result["error"] = (
                                        f"[ToolPostconditionFailed] {name}: "
                                        f"{detail or 'postcondition returned false'}"
                                    )
                                    success_result["code"] = "postcondition_failed"
                                    success_result["error_type"] = "postcondition_failed"
                                    success_result["recoverable"] = False
                                    logger.warning("postcondition failed name=%s detail=%s", name, detail)
                            except Exception as exc:
                                runtime_metrics.increment("postcondition_failure_count")
                                if tool.result_persistence == "request_local":
                                    success_result.pop("fresh_output", None)
                                logger.exception("postcondition verifier crashed name=%s", name)
                                success_result["verified"] = None
                                success_result["verification_detail"] = f"{type(exc).__name__}: {exc}"
                                success_result["error"] = (
                                    f"[ToolPostconditionError] {name}: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                                success_result["code"] = "postcondition_failed"
                                success_result["error_type"] = "postcondition_failed"
                                success_result["recoverable"] = False
                        # ── after_tool hooks ──
                        try:
                            success_result = self.hooks.dispatch_after_tool(name, args, success_result, tool)
                        except HookReject as rej:
                            return await finalize({
                                "output": "",
                                "error": f"[HookReject] {rej.reason}",
                                "risk": tool.risk,
                                "code": "sandbox_denied",
                                "error_type": "hook_rejected",
                                "recoverable": False,
                            })
                        if tool.trace_context is not None:
                            try:
                                dynamic_context = tool.trace_context(args, success_result)
                                if isinstance(dynamic_context, dict):
                                    success_result["trace_context"] = {
                                        str(key): str(value)
                                        for key, value in dynamic_context.items()
                                        if value is not None and str(value)
                                    }
                            except Exception:
                                logger.exception("trace context extractor crashed name=%s", name)
                        if success_result.get("error"):
                            self.hooks.dispatch_tool_error(
                                name,
                                self.persistence_safe_args(tool, args),
                                str(success_result["error"]),
                                tool,
                            )
                        finalized = await finalize(success_result)
                        if private_result is not None and not finalized.get("error"):
                            finalized["_private_result"] = private_result
                        return finalized
                    except TimeoutError:
                        if attempt >= tool.max_retries:
                            raise
                        report("retrying", current=attempt + 2, total=total_attempts, message="timeout")
                        logger.warning("tool timeout retry name=%s attempt=%s", name, attempt + 1)
                    except retryable:
                        if attempt >= tool.max_retries:
                            raise
                        report("retrying", current=attempt + 2, total=total_attempts, message="transient error")
                        logger.warning("tool retry name=%s attempt=%s", name, attempt + 1)
                    await asyncio.sleep(tool.retry_delay)
                return await finalize({"output": "", "error": f"[ToolError] {name}: retry loop exhausted"})
        except TimeoutError:
            logger.warning("tool timeout name=%s timeout=%s", name, tool.timeout)
            report("timed_out", status="failed", message=f"exceeded {tool.timeout}s")
            error_msg = f"[ToolTimeout] {name}: exceeded {tool.timeout}s"
            timeout_failure = timeout_slot.failure if timeout_slot is not None else None
            if timeout_failure is not None:
                # String-only consumers (including Code Mode RPC) must retain
                # the same observation/no-replay guidance as structured events.
                error_msg += f". {timeout_failure.message} {timeout_failure.recovery_hint}".rstrip()
            self.hooks.dispatch_tool_error(
                name, self.persistence_safe_args(tool, args), error_msg, tool
            )
            timeout_result: dict[str, Any] = {
                "output": "",
                "error": error_msg,
                "code": "overall_timeout",
                "error_type": "overall_timeout",
                "recoverable": bool(tool.idempotent),
                "retryable": False,
                "recovery_hint": (
                    "This exact invocation already exhausted its tool timeout. Do not repeat it "
                    "unchanged in the same turn; inspect the service or inputs, change approach, "
                    "or ask the user before retrying."
                ),
            }
            if timeout_failure is not None:
                timeout_result.update({
                    "partial": timeout_failure.partial,
                    "details": copy.deepcopy(timeout_failure.details),
                    "recovery_hint": timeout_failure.recovery_hint or timeout_result["recovery_hint"],
                })
            return await finalize(timeout_result)
        except Exception as e:
            from ..turn_budget import TurnBudgetExceeded
            if isinstance(e, TurnBudgetExceeded):
                raise
            logger.exception("tool error name=%s", name)
            report("failed", status="failed", message=f"{type(e).__name__}: {e}")
            error_msg = f"[ToolError] {type(e).__name__}: {e}"
            self.hooks.dispatch_tool_error(
                name, self.persistence_safe_args(tool, args), error_msg, tool
            )
            return await finalize({
                "output": "",
                "error": error_msg,
                "code": "execution_failed",
                "error_type": "execution_failed",
                "recoverable": isinstance(e, (ValueError, OSError, ConnectionError)),
                **({"execution": dict(e.execution)} if isinstance(e, ExecutionFailure) else {}),
            })
        finally:
            if permission_cleanup is not None:
                try:
                    permission_cleanup()
                except Exception:
                    logger.exception("permission cleanup failed name=%s", name)
            if tool.permission_finalizer is not None and permission_args is not None:
                try:
                    tool.permission_finalizer(permission_args)
                except Exception:
                    logger.exception("permission finalizer failed name=%s", name)

    def _shape_output(
        self,
        tool_name: str,
        output: str,
        override: int | None,
        *,
        result_persistence: str = "durable",
    ) -> dict:
        if result_persistence == "request_local":
            return {
                "output": _REQUEST_LOCAL_RESULT_PLACEHOLDER,
                "output_truncated": False,
                "request_local_placeholder": True,
                "fresh_output": output,
            }
        limit = override or self.max_inline_chars
        if len(output) <= limit:
            return {"output": output, "output_truncated": False}

        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", tool_name).strip("._") or "tool"
        try:
            artifact_path = self._write_private_artifact(safe_name, output)
        except OSError as exc:
            # Spill storage is a retention optimization, not part of the tool's
            # business operation.  A full disk or an unavailable artifact
            # directory must never turn an otherwise successful tool call into
            # an execution failure or hide its only copy from the model.
            logger.warning(
                "tool result artifact write failed name=%s dir=%s error=%s",
                tool_name,
                self.artifact_dir,
                type(exc).__name__,
            )
            return {
                "output": output,
                "output_truncated": False,
                "artifact_persist_failed": True,
                "artifact_error_type": type(exc).__name__,
            }
        size_bytes = len(output.encode("utf-8"))
        notice = (
            f"[Tool output truncated: {len(output)} chars / {size_bytes} bytes. "
            f"Full result saved to {artifact_path}]\n"
        )
        # Base64/data-URL payloads are opaque to a text model.  Inlining a
        # 12K slice only consumes attention and can make the next generation
        # ignore the fact that the tool already completed.
        opaque_payload = re.match(r"^\s*data:[^;,\s]+(?:;[^,\s]+)*;base64,", output, re.IGNORECASE)
        if opaque_payload:
            media_type = output.lstrip()[5:].split(";", 1)[0] or "unknown"
            preview = (
                notice
                + f"[Opaque base64 data URL omitted from text context; media type: {media_type}. "
                "Use the saved artifact or a media-aware tool to inspect it.]"
            )
        else:
            preview = notice + _bounded_head_tail(output, limit)
        shaped = {
            "output": preview,
            "output_truncated": True,
            "artifact_path": str(artifact_path),
            "artifact_chars": len(output),
            "artifact_bytes": size_bytes,
        }
        # The durable conversation/UI keeps the small preview, while the next
        # model iteration gets one chance to consume the complete fresh text.
        # Opaque payloads and exceptionally large text remain artifact-backed.
        if not opaque_payload and len(output) <= self.max_fresh_chars:
            shaped["fresh_output"] = output
        return shaped

    def _write_private_artifact(self, safe_name: str, output: str) -> Path:
        """Persist one tool result without overwriting an existing filesystem entry."""

        self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.artifact_dir, 0o700)
        except OSError:
            # Windows chmod cannot express a private DACL and some mounted
            # filesystems reject mode changes.  Exclusive creation below is
            # still mandatory; platform-specific ACL hardening can layer on it.
            logger.debug("could not tighten tool result directory mode", exc_info=True)

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        payload = output.encode("utf-8")
        for _attempt in range(3):
            artifact_path = self.artifact_dir / (
                f"{safe_name}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}.txt"
            )
            try:
                descriptor = os.open(artifact_path, flags, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
            except BaseException:
                artifact_path.unlink(missing_ok=True)
                raise
            return artifact_path
        raise FileExistsError("could not allocate a unique tool result artifact")
