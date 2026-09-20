"""Stateless Responses translation, with opaque reasoning kept out of display text."""
from __future__ import annotations

import hashlib
import json

from .llm import LLMResponseError

STATE_KEY = "_provider_state"


def account_scope(account: str) -> str:
    return hashlib.sha256(account.encode()).hexdigest()


def _call(tc: dict) -> dict:
    function = tc.get("function", tc)
    args = function.get("arguments", "{}")
    return {"type": "function_call", "call_id": tc.get("id") or tc.get("call_id"),
            "name": function.get("name"),
            "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}


def signature(message: dict) -> str:
    calls = []
    for tc in message.get("tool_calls") or []:
        call = _call(tc)
        try:
            call["arguments"] = json.loads(call["arguments"])
        except (ValueError, TypeError):
            pass
        calls.append(call)
    value = {"content": message.get("content") or "", "calls": calls}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _content(value, *, assistant: bool = False) -> list[dict]:
    if isinstance(value, str):
        return [{"type": "output_text" if assistant else "input_text", "text": value}] if value else []
    parts = []
    for part in value or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.extend(_content(str(part.get("text") or ""), assistant=assistant))
        elif part.get("type") == "image_url" and not assistant:
            image = part.get("image_url") or {}
            parts.append({"type": "input_image", "image_url": image.get("url", ""),
                          "detail": image.get("detail", "auto")})
    return parts


def _replay(message: dict, *, account: str, model: str) -> list[dict] | None:
    state = message.get(STATE_KEY)
    if not isinstance(state, dict) or (state.get("provider"), state.get("account"), state.get("model")) != (
            "openai-codex", account_scope(account), model) or state.get("signature") != signature(message):
        return None
    content = message.get("content") or ""
    calls = {_call(tc)["call_id"]: _call(tc) for tc in message.get("tool_calls") or []}
    items = []
    for part in state.get("items", []):
        if part["type"] == "reasoning":
            # IDs reference server storage; store=false requires the encrypted
            # payload itself. Never turn ciphertext into visible reasoning.
            items.append({k: v for k, v in part.items() if k in {"type", "summary", "encrypted_content"}})
        elif part["type"] == "message":
            text = content[part["start"]:part["end"]]
            items.append({"type": "message", "role": "assistant", "phase": part["phase"],
                          "content": _content(text, assistant=True)})
        elif part["type"] == "function_call":
            items.append(calls[part["call_id"]])
    return items


def input_items(messages: list[dict], *, account: str, model: str) -> tuple[str, list[dict]]:
    instructions, items = [], []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role in {"system", "developer"}:
            instructions.extend(p["text"] for p in _content(content) if p["type"] == "input_text")
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
        elif role == "assistant":
            replay = _replay(message, account=account, model=model)
            if replay is not None:
                items.extend(replay)
                continue
            parts = _content(content, assistant=True)
            if parts:
                items.append({"type": "message", "role": role, "content": parts,
                              "phase": "commentary" if message.get("tool_calls") else "final_answer"})
            items.extend(_call(tc) for tc in message.get("tool_calls") or [])
        elif role == "user":
            items.append({"type": "message", "role": role, "content": _content(content)})
    return "\n\n".join(instructions), items


def completed_response(response: dict, *, account: str, model: str) -> dict:
    """Validate the entire completed batch before returning anything executable."""
    if response.get("status") != "completed":
        raise LLMResponseError("incomplete_response", "Codex response did not complete.")
    content, summaries, calls, skeleton = "", [], [], []
    ids = set()
    for item in response.get("output") or []:
        kind = item.get("type")
        if kind == "reasoning":
            summary = [p for p in item.get("summary", []) if p.get("type") == "summary_text"]
            summaries.extend(str(p.get("text") or "") for p in summary)
            if item.get("encrypted_content"):
                skeleton.append({"type": kind, "encrypted_content": item["encrypted_content"], "summary": summary})
        elif kind == "message":
            if item.get("status", "completed") != "completed":
                raise LLMResponseError("incomplete_response", "Codex assistant message is incomplete.")
            text = "".join(str(p.get("text") or p.get("refusal") or "") for p in item.get("content", []))
            phase = item.get("phase") or "final_answer"
            if phase not in {"commentary", "final_answer"}:
                raise LLMResponseError("incomplete_response", "Unrecognized Codex assistant phase.")
            skeleton.append({"type": kind, "start": len(content), "end": len(content) + len(text), "phase": phase})
            content += text
        elif kind == "function_call":
            call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
            if not call_id or call_id in ids or not name or item.get("status", "completed") != "completed":
                raise LLMResponseError("conflicting_tool_identity", "Missing, duplicate or incomplete Codex tool identity.")
            try:
                if not isinstance(arguments, str) or not isinstance(json.loads(arguments), dict):
                    raise ValueError
            except ValueError as exc:
                raise LLMResponseError("invalid_tool_arguments", "Codex tool arguments are not a complete JSON object.") from exc
            ids.add(call_id)
            calls.append({"id": call_id, "name": name, "arguments": arguments})
            skeleton.append({"type": kind, "call_id": call_id})
        elif kind:
            raise LLMResponseError("incomplete_response", "Codex returned an unsupported output item.")
    if not content.strip() and not calls:
        raise LLMResponseError("reasoning_only_response" if summaries else "empty_response", "Codex returned no answer or tool call.")
    raw = response.get("usage") or {}
    prompt, completion = int(raw.get("input_tokens") or 0), int(raw.get("output_tokens") or 0)
    cached = int((raw.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    return {"content": content, "tool_calls": calls, "reasoning_content": "\n\n".join(summaries),
            "finish_reason": "tool_calls" if calls else "stop", "tool_call_state": "complete",
            STATE_KEY: {"provider": "openai-codex", "account": account_scope(account), "model": model,
                        "signature": signature({"content": content, "tool_calls": calls}), "items": skeleton,
                        "reasoning_tokens": int((raw.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)},
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
                      "prompt_cache_hit_tokens": cached, "prompt_cache_miss_tokens": max(0, prompt - cached)}}


class ToolStream:
    """Reject contradictory streamed identities/arguments before tool dispatch."""

    def __init__(self):
        self.calls: dict[int, dict] = {}

    def observe(self, event: dict) -> None:
        kind = event.get("type", "")
        item = event.get("item") or {}
        if kind.startswith("response.function_call_arguments.") or item.get("type") == "function_call":
            index = event.get("output_index")
            if not isinstance(index, int) or index < 0:
                raise LLMResponseError("invalid_tool_index", "Invalid Codex output index.")
            call = self.calls.setdefault(index, {"arguments": ""})
            for field in ("call_id", "name"):
                if item.get(field):
                    if call.get(field) and call[field] != item[field]:
                        raise LLMResponseError("conflicting_tool_identity", "Codex tool identity changed while streaming.")
                    call[field] = item[field]
            if kind == "response.function_call_arguments.delta":
                call["arguments"] += str(event.get("delta") or "")
            full = item.get("arguments") if kind == "response.output_item.done" else (
                event.get("arguments") if kind == "response.function_call_arguments.done" else None)
            if full is not None:
                if not isinstance(full, str) or not full.startswith(call["arguments"]):
                    raise LLMResponseError("invalid_tool_arguments", "Codex completed arguments conflict with streamed arguments.")
                call["arguments"] = full

    def validate(self, output: list[dict]) -> None:
        for index, call in self.calls.items():
            if index >= len(output) or output[index].get("type") != "function_call":
                raise LLMResponseError("incomplete_response", "Codex dropped a streamed tool call.")
            item = output[index]
            for field in ("call_id", "name"):
                if call.get(field) and item.get(field) != call[field]:
                    raise LLMResponseError("conflicting_tool_identity", "Codex final tool identity conflicts with streamed identity.")
            if not str(item.get("arguments") or "").startswith(call["arguments"]):
                raise LLMResponseError("invalid_tool_arguments", "Codex final arguments conflict with streamed arguments.")
