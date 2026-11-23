"""Stateless Anthropic Messages API wire translation for Astra's canonical messages.

Requests are built so that the same Astra history always produces the same bytes, and a newer
request only appends to the previous one: Claude's prompt cache and its preserved thinking both
depend on that. Thinking blocks (with their signatures) travel in ``_provider_state`` on the
assistant message that carried them and are sent back unchanged, in their original order.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from typing import Any

from .claude_code_provider import IMAGE_TYPES, _image_block, _is_error, _leading_system, _text, _tool_input, _tool_use_id
from .codex_wire import signature
from .llm import LLMResponseError

PROVIDER_NAME = "anthropic-api"
BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
API_KEY_ENV = "CLAUDE_API_KEY"
STATE_KEY = "_provider_state"
DEFAULT_CONTEXT_LIMIT = 1_000_000
DEFAULT_MAX_OUTPUT_TOKENS = 64_000
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# One hour, like the Claude Code route: Astra conversations pause for minutes between turns, and a
# 1h write costs 2x on the new tokens where a cold 5-minute cache rewrites the whole prefix at 1.25x.
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
# Lets the request ask for thinking blocks whose conversation changed to be dropped instead of
# failing the request (Astra compacts and clears old tool results, which edit earlier turns).
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
# Server-side refusal fallback: a declined request is re-run on another model in the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TOOL_NAME_LIMIT = 64
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,%d}" % TOOL_NAME_LIMIT)
_MODEL = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?:$|[-@\[])")


def normalize_model_id(model: str) -> str:
    return (model or "").strip()


def model_version(model: str) -> tuple[str, tuple[int, int]] | None:
    """Family and (major, minor) of a Claude model id; None for an id that names neither."""
    match = _MODEL.search(normalize_model_id(model).lower())
    if not match:
        return None
    return match[1], (int(match[2]), int(match[3] or 0))


def adaptive_thinking(model: str) -> bool:
    """Models that take ``thinking: {type: "adaptive"}`` and ``output_config.effort``: Opus and
    Sonnet 4.6 and later, Haiku 5 and later, every Fable and Mythos. An unknown id is assumed
    current; Haiku 4.5 and older models are sent no thinking settings at all."""
    parsed = model_version(model)
    if parsed is None:
        return True
    family, version = parsed
    if family in {"fable", "mythos"}:
        return True
    if family == "haiku":
        return version >= (5, 0)
    return version >= (4, 6)


def reasoning_levels(model: str) -> tuple[str, ...]:
    if not adaptive_thinking(model):
        return ()
    parsed = model_version(model)
    if parsed is not None and parsed[1] == (4, 6):
        return ("low", "medium", "high", "max")  # xhigh arrived with Opus 4.7
    return EFFORTS


def supports_server_fallback(model: str) -> bool:
    """Models whose refusals the API can re-run on another model (``fallbacks: "default"``)."""
    parsed = model_version(model)
    if parsed is None:
        return False
    family, version = parsed
    return (family == "fable" and version >= (5, 0)) or (family == "opus" and version >= (5, 0)) \
        or (family == "sonnet" and version >= (5, 5))


def resolve_effort(model: str, effort: str | None, levels: tuple[str, ...] = (), *, low: bool = False) -> str | None:
    supported = tuple(levels) or reasoning_levels(model)
    if not supported:
        return None
    if low:
        return supported[0]
    wanted = str(effort or "high").strip().lower()
    if wanted not in EFFORTS:
        wanted = "high"
    if wanted in supported:
        return wanted
    # The highest supported level not above the one asked for, else the lowest.
    below = [level for level in supported if EFFORTS.index(level) <= EFFORTS.index(wanted)]
    return below[-1] if below else supported[0]


def wire_tool_name(name: str) -> str:
    """The name Claude knows an Astra tool by. A name the API does not accept gets an alias made from
    the name alone, so the tool list and every replayed call show the same one."""
    if TOOL_NAME.fullmatch(name):
        return name
    digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    room = TOOL_NAME_LIMIT - len(digest) - 1
    if len(clean) <= room:
        return f"{clean}_{digest}"
    head = (room - 1) // 2
    return f"{clean[:head]}_{digest}_{clean[len(clean) - (room - 1 - head):]}"


def build_tools(tools: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Astra's function tools as Messages API tools, and the wire name -> Astra name map."""
    listed: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        raw_function = item.get("function")
        function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else item
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        wire = wire_tool_name(name)
        if wire in names:
            continue
        description = str(function.get("description") or "")
        schema = function.get("parameters")
        if schema is None or schema == {}:
            schema = {"type": "object", "properties": {}}
        elif isinstance(schema, dict) and "type" not in schema and isinstance(schema.get("properties"), dict):
            schema = {"type": "object", **schema}
        elif not isinstance(schema, dict) or schema.get("type") != "object":
            description += ("\n\n" if description else "") + (
                "[Astra: this tool's parameters are not listed here because their schema is not a JSON "
                "object schema. The declared schema: "
                + json.dumps(schema, ensure_ascii=False, sort_keys=True, default=str) + "]")
            schema = {"type": "object", "properties": {}}
        names[wire] = name
        # Streamed as generated: a large argument (a whole file) would otherwise arrive in one burst
        # after a silence long enough to look like a stalled stream. The provider checks the JSON.
        listed.append({"name": wire, "description": description, "input_schema": schema,
                       "eager_input_streaming": True})
    return listed, names


def _image(part: dict) -> dict | None:
    block = _image_block(part)
    if block is not None:
        return block
    image = part.get("image_url")
    url = str(image.get("url") or "") if isinstance(image, dict) else str(image or "")
    if url.startswith(("https://", "http://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def content_blocks(content: Any) -> list[dict]:
    if not isinstance(content, list):
        text = _text(content)
        return [{"type": "text", "text": text}] if text else []
    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            image = _image(part)
            blocks.append(image or {"type": "text", "text": "[image omitted: not a supported image "
                                    f"({', '.join(IMAGE_TYPES)} or an https URL)]"})
        elif str(part.get("text") or ""):
            blocks.append({"type": "text", "text": str(part["text"])})
    return blocks


def _reminder(text: str) -> dict:
    return {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}


def _call_text(call: dict) -> dict:
    function = call.get("function") or {}
    return {"type": "text", "text": "[tool call " + json.dumps(
        {"id": call.get("id", ""), "name": str(function.get("name", "")), "arguments": function.get("arguments", "{}")},
        ensure_ascii=False) + "]"}


def _canonical_calls(message: dict) -> list[dict]:
    calls = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        raw_function = call.get("function")
        function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else call
        calls.append({"id": str(call.get("id") or ""), "name": str(function.get("name") or ""),
                      "arguments": function.get("arguments", "{}"), "raw": call})
    return calls


def _replay_assistant(message: dict, offered: set[str], used: set[str],
                      ids: dict[str, str]) -> list[dict] | None:
    """The assistant turn exactly as Claude wrote it (thinking blocks included), or None when the
    message carries no usable state for this provider or was changed since."""
    state = message.get(STATE_KEY)
    if not isinstance(state, dict) or state.get("provider") != PROVIDER_NAME:
        return None
    if state.get("signature") != signature(message) or not isinstance(state.get("items"), list):
        return None
    content = _text(message.get("content"))
    calls = {call["id"]: call for call in _canonical_calls(message) if call["id"]}
    if any(call["name"] not in offered for call in calls.values()):
        return None
    blocks: list[dict] = []
    staged_used = set(used)
    staged_ids: dict[str, str] = {}
    for item in state["items"]:
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind == "thinking":
            if not isinstance(item.get("thinking"), str) or not isinstance(item.get("signature"), str):
                return None
            blocks.append({"type": "thinking", "thinking": item["thinking"], "signature": item["signature"]})
        elif kind == "redacted_thinking":
            if not isinstance(item.get("data"), str):
                return None
            blocks.append({"type": "redacted_thinking", "data": item["data"]})
        elif kind == "text":
            start, end = item.get("start"), item.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(content):
                return None
            blocks.append({"type": "text", "text": content[start:end]})
        elif kind == "tool_use":
            call = calls.pop(str(item.get("call_id") or ""), None)
            if call is None:
                return None
            native = _tool_use_id(call["id"], staged_used)
            staged_ids[call["id"]] = native
            blocks.append({"type": "tool_use", "id": native, "name": wire_tool_name(call["name"]),
                           "input": _tool_input(call["arguments"])})
        elif kind == "block" and isinstance(item.get("block"), dict):
            blocks.append(copy.deepcopy(item["block"]))
        else:
            return None
    if calls or not blocks:
        return None
    used.update(staged_used)
    ids.update(staged_ids)
    return blocks


def _plain_assistant(message: dict, offered: set[str], used: set[str], ids: dict[str, str]) -> list[dict]:
    blocks: list[dict] = []
    text = _text(message.get("content"))
    if text.strip():
        blocks.append({"type": "text", "text": text})
    for call in _canonical_calls(message):
        if call["name"] and call["name"] in offered:
            native = _tool_use_id(call["id"], used)
            ids[call["id"]] = native
            blocks.append({"type": "tool_use", "id": native, "name": wire_tool_name(call["name"]),
                           "input": _tool_input(call["arguments"])})
        else:
            blocks.append(_call_text(call["raw"]))
    return blocks


def build_messages(messages: list[dict], offered: set[str]) -> list[dict]:
    """Astra's conversation after its leading system messages as alternating native turns, starting
    and ending with a user turn: tool results first in each user turn, then text, images and later
    system messages as reminders. Calls to tools not offered now stay text, with their results."""
    turns: list[tuple[str, list[dict], list[dict]]] = []  # role, tool results, other blocks
    used: set[str] = set()
    pending: dict[str, str] = {}  # Astra's id -> native id, for calls of the latest assistant turn
    for message in messages[_leading_system(messages):]:
        role = message.get("role")
        if role == "assistant":
            ids: dict[str, str] = {}
            blocks = _replay_assistant(message, offered, used, ids)
            if blocks is None:
                blocks = _plain_assistant(message, offered, used, ids)
            if not blocks:
                continue
            if turns and turns[-1][0] == "assistant":
                turns[-1][2].extend(blocks)
            else:
                turns.append(("assistant", [], blocks))
                pending = {}
            pending.update(ids)
            continue
        if not turns or turns[-1][0] != "user":
            turns.append(("user", [], []))
        results, blocks = turns[-1][1], turns[-1][2]
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            content = content_blocks(message.get("content")) or [{"type": "text", "text": "(no output)"}]
            native = pending.pop(call_id, None)
            if native is not None:
                failed = _is_error(message.get("content")) and all(b["type"] == "text" for b in content)
                results.append({"type": "tool_result", "tool_use_id": native,
                                **({"is_error": True} if failed else {}), "content": content})
            else:
                blocks.extend([{"type": "text", "text": f"[tool result for call {call_id}]"}, *content])
        elif role in {"system", "developer"}:
            text = _text(message.get("content"))
            if text.strip():
                blocks.append(_reminder(text))
        else:
            blocks.extend(content_blocks(message.get("content")))
    if not turns:
        turns.append(("user", [], [{"type": "text", "text": "(empty message)"}]))
    if turns[0][0] != "user":
        turns.insert(0, ("user", [], [{"type": "text", "text": "[Earlier messages are not shown.]"}]))
    if turns[-1][0] != "user":
        # Current models take no assistant prefill: ask for the continuation instead.
        turns.append(("user", [], [{"type": "text", "text": "Continue."}]))
    for index, (role, _, blocks) in enumerate(turns):
        if role != "assistant":
            continue
        answered = {r["tool_use_id"] for r in turns[index + 1][1]}
        turns[index + 1][1].extend(
            {"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
             "content": [{"type": "text", "text": "No result was recorded for this call."}]}
            for b in blocks if b["type"] == "tool_use" and b["id"] not in answered)
    return [{"role": role, "content": (results + blocks) or [{"type": "text", "text": "(empty message)"}]}
            if role == "user" else {"role": role, "content": blocks} for role, results, blocks in turns]


def build_system(messages: list[dict]) -> list[dict]:
    texts = [_text(m.get("content")) for m in messages[:_leading_system(messages)]]
    return [{"type": "text", "text": text} for text in texts if text.strip()]


def _mark(block: dict) -> None:
    block["cache_control"] = dict(CACHE_CONTROL)


def place_cache_breakpoints(system: list[dict], tools: list[dict], turns: list[dict]) -> None:
    """Three of the four breakpoints the API allows: the end of the system prompt (or of the tools),
    the end of the user turn before the newest answer, and the end of the request. The middle one is
    where the previous request ended, so it is read even when this step added more blocks than the
    cache looks back over."""
    if system:
        _mark(system[-1])
    elif tools:
        _mark(tools[-1])
    users = [index for index, turn in enumerate(turns) if turn["role"] == "user"]
    for index in {users[-1], *users[-2:-1]}:
        content = turns[index]["content"]
        if content:
            _mark(content[-1])


def build_request_body(
    messages: list[dict[str, Any]],
    *,
    model: str,
    max_tokens: int,
    effort: str | None = None,
    reasoning_levels: tuple[str, ...] = (),
    low_effort: bool = False,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    thinking_binding: bool = True,
    fallbacks: bool = True,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """The request body, the beta flags it needs and the wire -> Astra tool name map."""
    wire_tools, names = build_tools(tools)
    system = build_system(messages)
    turns = build_messages(messages, set(names.values()))
    place_cache_breakpoints(system, wire_tools, turns)
    body: dict[str, Any] = {"model": normalize_model_id(model), "max_tokens": max(1, int(max_tokens)),
                            "messages": turns, "stream": True}
    betas: list[str] = []
    if system:
        body["system"] = system
    if wire_tools:
        body["tools"] = wire_tools
        # Forced tool use is rejected on current models and with thinking on; Astra's forced calls
        # offer only the one tool instead (supports_forced_tool_choice is False).
        if tool_choice == "none":
            body["tool_choice"] = {"type": "none"}
    if adaptive_thinking(model):
        thinking: dict[str, Any] = {"type": "adaptive", "display": "summarized"}
        if thinking_binding:
            thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
            betas.append(THINKING_BINDING_BETA)
        body["thinking"] = thinking
        level = resolve_effort(model, effort, reasoning_levels, low=low_effort)
        if level:
            body["output_config"] = {"effort": level}
    if fallbacks and supports_server_fallback(model):
        body["fallbacks"] = "default"
        betas.append(FALLBACK_BETA)
    return body, betas, names


def normalize_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    fresh = int(raw.get("input_tokens") or 0) + int(raw.get("cache_creation_input_tokens") or 0)
    cached = int(raw.get("cache_read_input_tokens") or 0)
    completion = int(raw.get("output_tokens") or 0)
    return {"prompt_tokens": fresh + cached, "completion_tokens": completion,
            "total_tokens": fresh + cached + completion,
            "prompt_cache_hit_tokens": cached, "prompt_cache_miss_tokens": fresh}


def normalize_discovery_models(payload: Any) -> list[dict[str, Any]]:
    """GET /v1/models items in the shape Astra's model catalog reads."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("Response is not a Claude model list")
    models: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or len(model_id) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in model_id):
            continue
        entry: dict[str, Any] = {"id": model_id}
        context = item.get("max_input_tokens")
        if isinstance(context, int) and context > 0:
            entry["context_length"] = context
        output = item.get("max_tokens")
        if isinstance(output, int) and output > 0:
            entry["max_completion_tokens"] = min(output, DEFAULT_MAX_OUTPUT_TOKENS)
        levels = reasoning_levels(model_id)
        entry["supported_parameters"] = ["tools", *(["reasoning"] if levels else [])]
        entry["input_modalities"] = ["text", "image"]
        if levels:
            entry["reasoning_levels"] = list(levels)
        models[model_id] = entry
    return list(models.values())


class StreamAccumulator:
    """Assemble Messages API stream events into Astra events and the replayable turn."""

    def __init__(self, *, model: str, names: dict[str, str]):
        self.model = normalize_model_id(model)
        self.names = names
        self.content = ""
        self.reasoning = ""
        self.calls: list[dict[str, str]] = []
        self.skeleton: list[dict[str, Any]] = []
        self.blocks: dict[int, dict[str, Any]] = {}
        self.stop_reason = ""
        self.stop_details: dict[str, Any] | None = None
        self.usage_raw: dict[str, Any] = {}
        self.served_model = ""
        self.stopped = False
        self.started = False
        self.dropped_thinking = 0
        self._tool_slots = 0

    def _boundary(self) -> list[dict[str, Any]]:
        """A mid-output refusal fallback: what was declined before it is not sent back (thinking and
        tool calls), and its calls are never run; text stays where it was."""
        self.skeleton = [item for item in self.skeleton if item["type"] not in {"thinking", "redacted_thinking", "tool_use"}]
        if not self.calls:
            return []
        self.calls = []
        return [{"type": "_tool_preparation_reset"}]

    def observe(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise LLMResponseError("incomplete_response", "Invalid Claude stream event.")
        kind = payload.get("type")
        events: list[dict[str, Any]] = []
        if kind == "message_start":
            raw_message = payload.get("message")
            message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
            self.started = True
            if isinstance(message.get("model"), str):
                self.served_model = message["model"]
            if isinstance(message.get("usage"), dict):
                self.usage_raw.update({k: v for k, v in message["usage"].items() if v is not None})
            transformations = message.get("input_transformations")
            if isinstance(transformations, list):
                self.dropped_thinking += sum(1 for t in transformations
                                             if isinstance(t, dict) and t.get("type") == "thinking_dropped")
        elif kind == "content_block_start":
            index, block = payload.get("index"), payload.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                raise LLMResponseError("incomplete_response", "Invalid Claude content block.")
            events.extend(self._start(index, block))
        elif kind == "content_block_delta":
            index, change = payload.get("index"), payload.get("delta")
            state = self.blocks.get(index) if isinstance(index, int) else None
            if state is None or not isinstance(change, dict):
                raise LLMResponseError("incomplete_response", "Claude sent a delta for an unknown block.")
            events.extend(self._delta(state, change))
        elif kind == "content_block_stop":
            index = payload.get("index")
            state = self.blocks.pop(index, None) if isinstance(index, int) else None
            if state is None:
                raise LLMResponseError("incomplete_response", "Claude closed an unknown block.")
            self._stop(state)
        elif kind == "message_delta":
            raw_delta = payload.get("delta")
            delta: dict[str, Any] = raw_delta if isinstance(raw_delta, dict) else {}
            if isinstance(delta.get("stop_reason"), str):
                self.stop_reason = delta["stop_reason"]
            if isinstance(delta.get("stop_details"), dict):
                self.stop_details = delta["stop_details"]
            if isinstance(payload.get("usage"), dict):
                self.usage_raw.update({k: v for k, v in payload["usage"].items() if v is not None})
        elif kind == "message_stop":
            self.stopped = True
        return events

    def _start(self, index: int, block: dict[str, Any]) -> list[dict[str, Any]]:
        kind = block.get("type")
        events: list[dict[str, Any]] = []
        if kind == "thinking":
            text = str(block.get("thinking") or "")
            self.blocks[index] = {"kind": "thinking", "thinking": text, "signature": str(block.get("signature") or "")}
            if text:
                self.reasoning += text
                events.append({"type": "reasoning", "content": text})
        elif kind == "redacted_thinking":
            self.blocks[index] = {"kind": "redacted_thinking", "data": str(block.get("data") or "")}
        elif kind == "text":
            text = str(block.get("text") or "")
            self.blocks[index] = {"kind": "text", "start": len(self.content)}
            if text:
                self.content += text
                events.append({"type": "chunk", "content": text})
        elif kind == "tool_use":
            call_id, wire = block.get("id"), block.get("name")
            if not isinstance(call_id, str) or not call_id or not isinstance(wire, str) or not wire:
                raise LLMResponseError("conflicting_tool_identity", "Claude tool_use is missing its id or name.")
            name = self.names.get(wire, wire)
            slot = self._tool_slots
            self._tool_slots += 1
            initial = block.get("input")
            self.blocks[index] = {"kind": "tool_use", "id": call_id, "name": name, "json": "", "slot": slot,
                                  "input": initial if isinstance(initial, dict) else {}}
            events.append({"type": "_tool_preparation", "index": slot, "call_id": call_id, "name": name})
        else:
            if kind == "fallback":
                events.extend(self._boundary())
            # Anything else (a fallback marker, a future block type) goes back exactly as it came.
            self.blocks[index] = {"kind": "block", "block": copy.deepcopy(block)}
        return events

    def _delta(self, state: dict[str, Any], delta: dict[str, Any]) -> list[dict[str, Any]]:
        kind = delta.get("type")
        if kind == "text_delta" and state["kind"] == "text":
            text = str(delta.get("text") or "")
            if text:
                self.content += text
                return [{"type": "chunk", "content": text}]
        elif kind == "thinking_delta" and state["kind"] == "thinking":
            text = str(delta.get("thinking") or "")
            if text:
                state["thinking"] += text
                self.reasoning += text
                return [{"type": "reasoning", "content": text}]
        elif kind == "signature_delta" and state["kind"] == "thinking":
            state["signature"] += str(delta.get("signature") or "")
        elif kind == "input_json_delta" and state["kind"] == "tool_use":
            part = str(delta.get("partial_json") or "")
            if part:
                state["json"] += part
                return [{"type": "_tool_preparation", "index": state["slot"], "call_id": state["id"],
                         "name": state["name"], "delta": part}]
        return []

    def _stop(self, state: dict[str, Any]) -> None:
        kind = state["kind"]
        if kind == "thinking":
            self.skeleton.append({"type": "thinking", "thinking": state["thinking"], "signature": state["signature"]})
        elif kind == "redacted_thinking":
            self.skeleton.append({"type": "redacted_thinking", "data": state["data"]})
        elif kind == "text":
            if len(self.content) > state["start"]:
                self.skeleton.append({"type": "text", "start": state["start"], "end": len(self.content)})
        elif kind == "tool_use":
            raw = state["json"].strip()
            if raw:
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = None
                if not isinstance(value, dict):
                    # Eager input streaming is not checked by the API; an unfinished or broken
                    # argument is only ever the last block of a cut-off answer.
                    state["broken"] = True
                    self.calls.append({"id": state["id"], "name": state["name"], "arguments": raw})
                    self.skeleton.append({"type": "tool_use", "call_id": state["id"]})
                    return
            else:
                value = state["input"]
            self.calls.append({"id": state["id"], "name": state["name"],
                               "arguments": json.dumps(value, ensure_ascii=False)})
            self.skeleton.append({"type": "tool_use", "call_id": state["id"]})
        elif kind == "block":
            self.skeleton.append({"type": "block", "block": state["block"]})

    def _state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "provider": PROVIDER_NAME,
            "model": self.served_model or self.model,
            "signature": signature({"content": self.content, "tool_calls": [
                {"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}} for c in self.calls]}),
            "items": self.skeleton,
        }
        details = self.usage_raw.get("output_tokens_details")
        thinking_tokens = details.get("thinking_tokens") if isinstance(details, dict) else None
        if isinstance(thinking_tokens, int) and thinking_tokens > 0:
            state["reasoning_tokens"] = thinking_tokens
        return state

    def finalize(self) -> dict[str, Any]:
        if not self.stopped or self.blocks:
            raise LLMResponseError(
                "stream_closed",
                "Claude's stream ended before the message was complete. Partial output was discarded; no tool was executed.")
        usage = normalize_usage(self.usage_raw)
        reason = self.stop_reason
        if reason == "refusal":
            category = str((self.stop_details or {}).get("category") or "").strip()
            raise LLMResponseError(
                "model_refusal",
                "Claude declined this request" + (f" ({category})" if category else "")
                + ". Rephrase it or switch to another model.")
        if reason == "model_context_window_exceeded":
            raise LLMResponseError(
                "context_length_exceeded",
                "Claude request rejected: prompt is too long for the model's context window.")
        truncated = reason == "max_tokens"
        if self.calls:
            if truncated:
                return {"type": "tool_calls", "calls": self.calls, "content": self.content,
                        "reasoning_content": self.reasoning, "finish_reason": "length",
                        "tool_call_state": "incomplete", STATE_KEY: self._state(), "usage": usage}
            if any(not isinstance(_load(call["arguments"]), dict) for call in self.calls):
                raise LLMResponseError(
                    "invalid_tool_arguments", "Claude returned tool arguments that are not a JSON object; no tool was executed.")
            if reason not in {"tool_use", "end_turn", "stop_sequence", "pause_turn"}:
                raise LLMResponseError(
                    "incomplete_response",
                    f"Claude's tool batch did not complete cleanly (stop_reason={reason or 'none'}); no tool was executed.")
            return {"type": "tool_calls", "calls": self.calls, "content": self.content,
                    "reasoning_content": self.reasoning, "finish_reason": "tool_calls",
                    "tool_call_state": "complete", STATE_KEY: self._state(), "usage": usage}
        if truncated:
            raise LLMResponseError(
                "response_truncated",
                "The output limit was reached. Partial text was retained; request a shorter answer or continue explicitly.")
        if reason not in {"end_turn", "stop_sequence", "pause_turn"}:
            raise LLMResponseError(
                "incomplete_response", f"Claude did not report a completed answer (stop_reason={reason or 'none'}).")
        if not self.content.strip():
            code = "reasoning_only_response" if self.reasoning.strip() else "empty_response"
            raise LLMResponseError(code, "Claude returned no answer or tool call.")
        return {"type": "done", "content": self.content, "reasoning_content": self.reasoning,
                "finish_reason": "stop", STATE_KEY: self._state(), "usage": usage}


def _load(arguments: str) -> Any:
    try:
        return json.loads(arguments)
    except ValueError:
        return None


def request_id() -> str:
    return uuid.uuid4().hex[:12]
