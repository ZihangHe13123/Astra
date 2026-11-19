"""Stateless Gemini v1beta REST wire translation with opaque thoughtSignature state."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .codex_wire import signature
from .llm import LLMResponseError

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
PROVIDER_NAME = "google-gemini"
STATE_KEY = "_provider_state"
SKIP_THOUGHT_SIGNATURE = "skip_thought_signature_validator"
DEFAULT_CONTEXT_LIMIT = 1_048_576
DEFAULT_SYNC_MAX_OUTPUT_TOKENS = 65_536

_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.DOTALL)
_NON_CHAT_MODEL_RE = re.compile(
    r"(?:^|/)(?:imagen|veo|lyria|aqa|embedding|text-embedding|gemini-embedding)"
    r"|(?:^|[-_])(?:tts|image|image-generation|image-preview|transcribe|audio|live|robotics)(?:$|[-_])",
    re.IGNORECASE,
)


def normalize_model_id(name: str) -> str:
    value = str(name or "").strip()
    if value.startswith("models/"):
        return value[len("models/"):]
    return value


def is_gemini_4_model(model: str) -> bool:
    return normalize_model_id(model).lower().startswith("gemini-4")


def is_gemini_3_model(model: str) -> bool:
    return normalize_model_id(model).lower().startswith("gemini-3")


def is_gemini_25_model(model: str) -> bool:
    return normalize_model_id(model).lower().startswith("gemini-2.5")


def supports_thinking(model: str) -> bool:
    lowered = normalize_model_id(model).lower()
    return (
        lowered.startswith(("gemini-4", "gemini-3", "gemini-2.5"))
        or "thinking" in lowered
    )


def is_thinking_required_model(model: str) -> bool:
    lowered = normalize_model_id(model).lower()
    if lowered.startswith(("gemini-4", "gemini-3")):
        return True
    return lowered.startswith("gemini-2.5-pro")


def default_reasoning_levels(model: str) -> tuple[str, ...]:
    lowered = normalize_model_id(model).lower()
    if lowered.startswith("gemini-4"):
        return ("low", "medium", "high", "xhigh")
    if lowered.startswith("gemini-3"):
        if "flash" in lowered:
            return ("minimal", "low", "medium", "high")
        return ("low", "medium", "high")
    if lowered.startswith("gemini-2.5"):
        return ("low", "medium", "high")
    return ()


def _parse_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _image_url_to_part(part: dict[str, Any]) -> dict[str, Any] | None:
    image = part.get("image_url")
    url = str(image.get("url") or "") if isinstance(image, dict) else str(image or "")
    url = url.strip()
    if not url:
        return None
    match = _DATA_URL_RE.match(url)
    if match:
        mime, data = match.group(1).strip(), match.group(2).strip()
        if mime and data:
            return {"inlineData": {"mimeType": mime, "data": data}}
        return None
    if url.startswith(("http://", "https://")):
        return {"fileData": {"fileUri": url}}
    return None


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text") or "")
                if text:
                    pieces.append(text)
        return "\n".join(pieces)
    return str(content or "")


def _user_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text") or "")
            if text:
                parts.append({"text": text})
        elif kind == "image_url":
            converted = _image_url_to_part(part)
            if converted is not None:
                parts.append(converted)
    return parts


def _canonical_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = []
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        raw_fn = tc.get("function")
        function = raw_fn if isinstance(raw_fn, dict) else tc
        calls.append({
            "id": str(tc.get("id") or tc.get("call_id") or ""),
            "name": str(function.get("name") or ""),
            "arguments": function.get("arguments", "{}"),
        })
    return calls


def _replay_assistant(
    message: dict[str, Any],
    *,
    model: str,
    call_meta: dict[str, tuple[str, bool]],
) -> list[dict[str, Any]] | None:
    state = message.get(STATE_KEY)
    normalized_model = normalize_model_id(model)
    if (
        not isinstance(state, dict)
        or state.get("provider") != PROVIDER_NAME
        or normalize_model_id(str(state.get("model") or "")) != normalized_model
        or state.get("signature") != signature(message)
    ):
        return None
    items = state.get("items")
    if not isinstance(items, list):
        return None
    content = str(message.get("content") or "")
    calls_by_id = {call["id"]: call for call in _canonical_calls(message) if call["id"]}
    parts: list[dict[str, Any]] = []
    staged_meta: dict[str, tuple[str, bool]] = {}
    for item in items:
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        thought_sig = item.get("thoughtSignature")
        if kind == "thought_sig":
            if isinstance(thought_sig, str) and thought_sig:
                parts.append({"text": "", "thought": True, "thoughtSignature": thought_sig})
        elif kind == "text":
            start = item.get("start")
            end = item.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start or end > len(content):
                return None
            text = content[start:end]
            part: dict[str, Any] = {"text": text}
            if isinstance(thought_sig, str) and thought_sig:
                part["thoughtSignature"] = thought_sig
            if text or "thoughtSignature" in part:
                parts.append(part)
        elif kind == "function_call":
            call_id = str(item.get("call_id") or "")
            call = calls_by_id.get(call_id)
            if call is None or not call["name"]:
                return None
            had_id = bool(item.get("had_id"))
            fc: dict[str, Any] = {
                "name": call["name"],
                "args": _parse_json_object(call["arguments"]),
            }
            if had_id and call_id:
                fc["id"] = call_id
            part = {"functionCall": fc}
            if isinstance(thought_sig, str) and thought_sig:
                part["thoughtSignature"] = thought_sig
            parts.append(part)
            staged_meta[call_id] = (call["name"], had_id)
        else:
            return None
    call_meta.update(staged_meta)
    return parts


def _append_turn(contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    if not parts:
        return
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": list(parts)})


def build_contents(messages: list[dict[str, Any]], *, model: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Translate Astra canonical messages into Gemini systemInstruction + contents."""
    instructions: list[str] = []
    contents: list[dict[str, Any]] = []
    call_meta: dict[str, tuple[str, bool]] = {}
    strict_signatures = is_gemini_4_model(model) or is_gemini_3_model(model)

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _extract_text(content).strip()
            if text:
                instructions.append(text)
        elif role == "user":
            _append_turn(contents, "user", _user_parts(content))
        elif role == "assistant":
            replayed = _replay_assistant(message, model=model, call_meta=call_meta)
            if replayed is not None:
                _append_turn(contents, "model", replayed)
                continue
            parts: list[dict[str, Any]] = []
            text = _extract_text(content)
            if text:
                parts.append({"text": text})
            calls = _canonical_calls(message)
            for idx, call in enumerate(calls):
                if not call["name"]:
                    continue
                call_id = call["id"]
                include_id = bool(call_id) and strict_signatures
                fc: dict[str, Any] = {
                    "name": call["name"],
                    "args": _parse_json_object(call["arguments"]),
                }
                if include_id:
                    fc["id"] = call_id
                part: dict[str, Any] = {"functionCall": fc}
                if idx == 0 and strict_signatures:
                    # Cross-provider history or switched Gemini models lack a
                    # valid server signature; use Google's documented sentinel.
                    part["thoughtSignature"] = SKIP_THOUGHT_SIGNATURE
                parts.append(part)
                if call_id:
                    call_meta[call_id] = (call["name"], include_id)
            _append_turn(contents, "model", parts)
        elif role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "")
            fn_name, include_id = call_meta.get(tool_call_id, ("tool", False))
            extra_parts: list[dict[str, Any]] = []
            if isinstance(content, list):
                output_text = _extract_text(content)
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        converted = _image_url_to_part(block)
                        if converted is not None:
                            extra_parts.append(converted)
            else:
                output_text = str(content if content is not None else "")
            fn_resp: dict[str, Any] = {
                "name": fn_name,
                "response": {"output": output_text},
            }
            if include_id and tool_call_id:
                fn_resp["id"] = tool_call_id
            _append_turn(contents, "user", [{"functionResponse": fn_resp}, *extra_parts])

    system_instruction = (
        {"parts": [{"text": "\n\n".join(instructions)}]} if instructions else None
    )
    return system_instruction, contents


def build_tools(
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None = None,
    *,
    omit_tool_choice: bool = False,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    if not tools:
        return None, None
    declarations: list[dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        raw_fn = item.get("function")
        fn = raw_fn if isinstance(raw_fn, dict) else item
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        declaration: dict[str, Any] = {"name": name}
        description = fn.get("description")
        if isinstance(description, str) and description:
            declaration["description"] = description
        parameters = fn.get("parameters")
        if isinstance(parameters, dict) and parameters:
            # v1beta FunctionDeclaration.parametersJsonSchema accepts full JSON Schema
            # (additionalProperties, anyOf, freeform object arguments for call_tool).
            declaration["parametersJsonSchema"] = parameters
        declarations.append(declaration)
    if not declarations:
        return None, None
    gemini_tools = [{"functionDeclarations": declarations}]
    if omit_tool_choice:
        return gemini_tools, None
    config: dict[str, Any]
    if tool_choice == "none":
        config = {"mode": "NONE"}
    elif tool_choice == "required":
        config = {"mode": "ANY"}
    elif isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        target = str(fn.get("name") or "").strip() if isinstance(fn, dict) else ""
        config = (
            {"mode": "ANY", "allowedFunctionNames": [target]}
            if target
            else {"mode": "ANY"}
        )
    else:
        config = {"mode": "AUTO"}
    return gemini_tools, {"functionCallingConfig": config}


def resolve_thinking_level(
    model: str,
    effort: str | None,
    *,
    levels: tuple[str, ...] = (),
    disable_thinking: bool = False,
) -> str | None:
    supported = tuple(v.lower() for v in (levels or default_reasoning_levels(model)) if isinstance(v, str) and v)
    if not supported:
        return None
    if disable_thinking:
        return "minimal" if "minimal" in supported else ("low" if "low" in supported else supported[0])
    normalized = str(effort or "high").strip().lower()
    if normalized == "max":
        return next((v for v in ("xhigh", "high", "medium", "low", "minimal") if v in supported), supported[-1])
    if normalized == "xhigh":
        return next((v for v in ("xhigh", "high", "medium", "low", "minimal") if v in supported), supported[-1])
    if normalized == "high":
        return next((v for v in ("high", "medium", "low", "minimal") if v in supported), supported[-1])
    if normalized == "medium":
        return next((v for v in ("medium", "high", "low", "minimal") if v in supported), supported[0])
    if normalized == "low":
        return next((v for v in ("low", "minimal", "medium", "high") if v in supported), supported[0])
    if normalized == "minimal":
        return next((v for v in ("minimal", "low", "medium", "high") if v in supported), supported[0])
    return next((v for v in ("high", "medium", "low") if v in supported), supported[-1])


def build_thinking_config(
    model: str,
    *,
    effort: str | None = None,
    levels: tuple[str, ...] = (),
    disable_thinking: bool = False,
    include_thoughts: bool = True,
) -> dict[str, Any] | None:
    if not supports_thinking(model):
        return None
    if is_gemini_4_model(model) or is_gemini_3_model(model):
        level = resolve_thinking_level(
            model,
            effort,
            levels=levels,
            disable_thinking=disable_thinking,
        )
        config: dict[str, Any] = {"includeThoughts": bool(include_thoughts and not disable_thinking)}
        if level:
            config["thinkingLevel"] = level
        return config
    if is_gemini_25_model(model):
        if disable_thinking:
            budget = 1024 if is_thinking_required_model(model) else 0
            return {"thinkingBudget": budget, "includeThoughts": False}
        normalized = str(effort or "high").strip().lower()
        budgets = {"minimal": 1024, "low": 1024, "medium": 8192, "high": 24576, "xhigh": 24576, "max": 24576}
        return {
            "thinkingBudget": budgets.get(normalized, 24576),
            "includeThoughts": bool(include_thoughts),
        }
    return None


def effective_max_output_tokens(
    requested: int,
    *,
    configured_max_tokens: int,
    model: str,
    capabilities: frozenset[str],
    context_limit: int | None,
    disable_thinking: bool = False,
) -> int:
    """Keep enough output headroom when reasoning tokens share maxOutputTokens."""
    value = max(1, int(requested))
    thinking_required = is_thinking_required_model(model) or "thinking-required" in capabilities
    thinking_active = (
        thinking_required
        or (not disable_thinking and (supports_thinking(model) or "reasoning" in capabilities))
    )
    if not thinking_active:
        return value
    minimum = max(1, int(configured_max_tokens))
    if context_limit is not None and context_limit > 0:
        minimum = min(minimum, max(1, int(context_limit) // 2))
    return max(value, minimum)


def build_request_body(
    messages: list[dict[str, Any]],
    *,
    model: str,
    max_tokens: int,
    configured_max_tokens: int | None = None,
    context_limit: int | None = None,
    capabilities: frozenset[str] = frozenset(),
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    effort: str | None = None,
    reasoning_levels: tuple[str, ...] = (),
    disable_thinking: bool = False,
    include_thoughts: bool = True,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    omit_tool_choice: bool = False,
) -> dict[str, Any]:
    system_instruction, contents = build_contents(messages, model=model)
    max_output = effective_max_output_tokens(
        max_tokens,
        configured_max_tokens=configured_max_tokens if configured_max_tokens is not None else max_tokens,
        model=model,
        capabilities=capabilities,
        context_limit=context_limit,
        disable_thinking=disable_thinking,
    )
    generation_config: dict[str, Any] = {"maxOutputTokens": max_output}
    # Gemini 4 deprecates and ignores temperature/topP/topK; omit them to avoid
    # deprecation warnings and future 400 rejections.
    if not is_gemini_4_model(model):
        if temperature is not None:
            generation_config["temperature"] = float(temperature)
        if top_p is not None:
            generation_config["topP"] = float(top_p)
        if top_k is not None:
            generation_config["topK"] = int(top_k)

    thinking = build_thinking_config(
        model,
        effort=effort,
        levels=reasoning_levels,
        disable_thinking=disable_thinking,
        include_thoughts=include_thoughts,
    )
    if thinking is not None:
        generation_config["thinkingConfig"] = thinking

    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    if system_instruction is not None:
        body["systemInstruction"] = system_instruction

    gemini_tools, tool_config = build_tools(tools, tool_choice, omit_tool_choice=omit_tool_choice)
    if gemini_tools is not None:
        body["tools"] = gemini_tools
    if tool_config is not None:
        body["toolConfig"] = tool_config
    return body


def normalize_usage(meta: Any) -> dict[str, int] | None:
    if not isinstance(meta, dict) or not meta:
        return None
    prompt = int(meta.get("promptTokenCount") or 0)
    candidates = int(meta.get("candidatesTokenCount") or 0)
    thoughts = int(meta.get("thoughtsTokenCount") or 0)
    # Gemini bills thinking tokens as output tokens alongside candidatesTokenCount.
    completion = candidates + thoughts
    total = int(meta.get("totalTokenCount") or (prompt + completion))
    cached = int(meta.get("cachedContentTokenCount") or 0)
    result: dict[str, int] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    if "cachedContentTokenCount" in meta or cached > 0:
        result["prompt_cache_hit_tokens"] = cached
        result["prompt_cache_miss_tokens"] = max(0, prompt - cached)
    if "promptTokenCount" not in meta:
        result.pop("prompt_tokens", None)
    if "candidatesTokenCount" not in meta and "thoughtsTokenCount" not in meta:
        result.pop("completion_tokens", None)
    return result


def normalize_discovery_models(payload: Any) -> list[dict[str, Any]]:
    """Normalize native Gemini GET /v1beta/models responses for Astra's catalog."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("Response is not a Gemini model list")
    normalized: dict[str, dict[str, Any]] = {}
    for item in payload["models"]:
        if not isinstance(item, dict):
            continue
        methods = item.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            continue
        raw_name = str(item.get("name") or item.get("baseModelId") or "").strip()
        model_id = normalize_model_id(raw_name)
        if not model_id or len(model_id) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in model_id):
            continue
        if _NON_CHAT_MODEL_RE.search(model_id):
            continue
        entry: dict[str, Any] = {"id": model_id}
        input_limit = item.get("inputTokenLimit")
        if isinstance(input_limit, int) and input_limit > 0:
            entry["context_length"] = input_limit
        output_limit = item.get("outputTokenLimit")
        if isinstance(output_limit, int) and output_limit > 0:
            # Unified-window Gemini 4 models may advertise 1M outputTokenLimit;
            # synchronous generateContent defaults to 64K output.
            entry["max_completion_tokens"] = min(output_limit, DEFAULT_SYNC_MAX_OUTPUT_TOKENS)
        has_thinking = bool(item.get("thinking")) or supports_thinking(model_id)
        params = ["tools"]
        if has_thinking:
            params.append("reasoning")
        entry["supported_parameters"] = params
        entry["input_modalities"] = ["text", "image"]
        levels = default_reasoning_levels(model_id)
        if levels:
            entry["reasoning_levels"] = list(levels)
        normalized[model_id] = entry
    return list(normalized.values())


class StreamAccumulator:
    """Validate and assemble a Gemini SSE stream into Astra events + _provider_state."""

    def __init__(self, *, model: str):
        self.model = normalize_model_id(model)
        self.content = ""
        self.reasoning = ""
        self.calls: list[dict[str, str]] = []
        self.skeleton: list[dict[str, Any]] = []
        self.finish_reason = ""
        self.block_reason = ""
        self.usage: dict[str, int] | None = None
        self.served_model = ""
        self._seen_call_ids: set[str] = set()

    def observe(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Process one GenerateContentResponse frame and return incremental Astra events."""
        if not isinstance(chunk, dict):
            raise LLMResponseError("incomplete_response", "Invalid Gemini stream chunk.")
        model_version = chunk.get("modelVersion")
        if isinstance(model_version, str) and model_version.strip():
            self.served_model = normalize_model_id(model_version)
        if "usageMetadata" in chunk and chunk["usageMetadata"]:
            self.usage = normalize_usage(chunk["usageMetadata"])
        prompt_feedback = chunk.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            self.block_reason = str(prompt_feedback["blockReason"])

        candidates = chunk.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return []
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            return []

        events: list[dict[str, Any]] = []
        content_obj = candidate.get("content")
        parts = content_obj.get("parts") if isinstance(content_obj, dict) else None
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                thought_sig = part.get("thoughtSignature")
                sig_str = thought_sig if isinstance(thought_sig, str) and thought_sig else ""

                if part.get("thought") is True:
                    text = str(part.get("text") or "")
                    if text:
                        self.reasoning += text
                        events.append({"type": "reasoning", "content": text})
                    if sig_str:
                        self.skeleton.append({"type": "thought_sig", "thoughtSignature": sig_str})
                elif "functionCall" in part and isinstance(part["functionCall"], dict):
                    fc = part["functionCall"]
                    name = fc.get("name")
                    if not isinstance(name, str) or not name.strip():
                        raise LLMResponseError("conflicting_tool_identity", "Gemini functionCall is missing a valid name.")
                    raw_args = fc.get("args")
                    if raw_args is None:
                        args_obj: dict[str, Any] = {}
                    elif isinstance(raw_args, dict):
                        args_obj = raw_args
                    else:
                        raise LLMResponseError("invalid_tool_arguments", "Gemini functionCall args must be a JSON object.")
                    raw_id = fc.get("id")
                    if isinstance(raw_id, str) and raw_id.strip():
                        had_id = True
                        call_id = raw_id.strip()
                    else:
                        had_id = False
                        call_id = f"gemini_call_{len(self.calls)}_{uuid.uuid4().hex[:8]}"
                    if call_id in self._seen_call_ids:
                        raise LLMResponseError("conflicting_tool_identity", "Duplicate Gemini functionCall id.")
                    self._seen_call_ids.add(call_id)
                    arguments = json.dumps(args_obj, ensure_ascii=False)
                    call_index = len(self.calls)
                    self.calls.append({"id": call_id, "name": name, "arguments": arguments})
                    entry: dict[str, Any] = {
                        "type": "function_call",
                        "call_id": call_id,
                        "had_id": had_id,
                    }
                    if sig_str:
                        entry["thoughtSignature"] = sig_str
                    self.skeleton.append(entry)
                    events.append({
                        "type": "_tool_preparation",
                        "index": call_index,
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                    })
                elif "text" in part or sig_str:
                    text = str(part.get("text") or "")
                    start = len(self.content)
                    if text:
                        self.content += text
                        events.append({"type": "chunk", "content": text})
                    end = len(self.content)
                    if (
                        not sig_str
                        and text
                        and self.skeleton
                        and self.skeleton[-1]["type"] == "text"
                        and "thoughtSignature" not in self.skeleton[-1]
                        and self.skeleton[-1]["end"] == start
                    ):
                        self.skeleton[-1]["end"] = end
                    elif text or sig_str:
                        text_entry: dict[str, Any] = {"type": "text", "start": start, "end": end}
                        if sig_str:
                            text_entry["thoughtSignature"] = sig_str
                        self.skeleton.append(text_entry)

        finish_reason = candidate.get("finishReason")
        if isinstance(finish_reason, str) and finish_reason.strip():
            self.finish_reason = finish_reason.strip().upper()
        return events

    def finalize(self) -> dict[str, Any]:
        if self.block_reason and not self.content.strip() and not self.calls:
            raise LLMResponseError(
                "incomplete_response",
                f"Gemini blocked the prompt ({self.block_reason}).",
            )
        if not self.finish_reason:
            raise LLMResponseError(
                "stream_closed",
                "Gemini stream ended without a finishReason. Partial output was retained; no tool was executed.",
            )

        state = {
            "provider": PROVIDER_NAME,
            "model": self.model,
            "signature": signature({"content": self.content, "tool_calls": self.calls}),
            "items": self.skeleton,
        }

        if self.calls:
            if self.finish_reason == "MAX_TOKENS":
                return {
                    "type": "tool_calls",
                    "calls": self.calls,
                    "content": self.content,
                    "reasoning_content": self.reasoning,
                    "finish_reason": "length",
                    "tool_call_state": "incomplete",
                    STATE_KEY: state,
                    "usage": self.usage,
                }
            if self.finish_reason not in {"STOP", "FUNCTION_CALL", "TOOL_CALLS"}:
                raise LLMResponseError(
                    "incomplete_response",
                    f"Gemini tool batch did not complete cleanly (finishReason={self.finish_reason}); no tool was executed.",
                )
            return {
                "type": "tool_calls",
                "calls": self.calls,
                "content": self.content,
                "reasoning_content": self.reasoning,
                "finish_reason": "tool_calls",
                "tool_call_state": "complete",
                STATE_KEY: state,
                "usage": self.usage,
            }

        if self.finish_reason == "MAX_TOKENS":
            raise LLMResponseError(
                "response_truncated",
                "The output limit was reached. Partial text was retained; request a shorter answer or continue explicitly.",
            )
        if self.finish_reason != "STOP":
            raise LLMResponseError(
                "incomplete_response",
                f"Gemini did not report a successful completion (finishReason={self.finish_reason}).",
            )
        if not self.content.strip():
            code = "reasoning_only_response" if self.reasoning.strip() else "empty_response"
            raise LLMResponseError(
                code,
                "Gemini returned no answer or tool call.",
            )
        return {
            "type": "done",
            "content": self.content,
            "reasoning_content": self.reasoning,
            "finish_reason": "stop",
            STATE_KEY: state,
            "usage": self.usage,
        }
