"""Direct Codex subscription transport for Astra's own agent/tool loop."""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import replace

import httpx

from . import codex_auth
from .codex_wire import OutputStream, ToolStream, completed_response, input_items
from .llm import (LLMIdleTimeout, LLMResponseError, _RequestBudget, _RequestCapTimeout,
                  _StreamReadTiming, _messages_for_capabilities)
from .network import active_proxy_for_url
from .token_estimator import estimate_messages_tokens


class CodexRequestError(LLMResponseError):
    """Display only Astra-authored diagnostics, never an upstream payload."""

    def __init__(self, message):
        super().__init__("codex_request_failed", message)
        self.message = message

    @property
    def public_message(self):
        return self.message

    @property
    def public_code(self):
        return "codex_request_failed"


class CodexProvider:
    def __init__(self, config, *, transport=None):
        if config.base_url.rstrip("/") != codex_auth.BASE_URL:
            raise ValueError("Codex OAuth credentials can only be used with the official Codex endpoint.")
        self.config = config
        self.transport = transport
        self._estimate_calibration = 1.0

    def supports_forced_tool_choice(self) -> bool:
        return True

    def _body(self, messages, tools, tool_choice, account, omit_tool_choice=False) -> dict:
        prepared = _messages_for_capabilities(messages, self.config.capabilities,
            frozenset(t.get("function", {}).get("name", "") for t in tools or []), self.config.vision_detail)
        instructions, items = input_items(prepared, account=account, model=self.config.model)
        effort = self.config.reasoning_effort or "high"
        levels = getattr(self.config, "reasoning_levels", ())
        if effort == "max":
            # Intersect the model catalog with this endpoint's wire enum: the
            # catalog can advertise "ultra" while /responses rejects that value.
            effort = next((v for v in ("max", "xhigh", "high", "medium", "low", "minimal", "none") if v in levels), "xhigh")
        elif levels and effort not in levels:
            raise codex_auth.CodexAuthError(f"This Codex model supports reasoning efforts: {', '.join(levels)}; use /mode high or /mode max.")
        body = {"model": self.config.model, "instructions": instructions, "input": items,
                "store": False, "stream": True, "reasoning": {"effort": effort, "summary": "auto"},
                "include": ["reasoning.encrypted_content"]}
        # The subscription backend does not accept max_output_tokens or normal
        # sampling parameters. Do not forward Chat Completions-only options.
        if tools:
            # Responses may otherwise normalize optional arguments into required
            # strict-mode fields. Preserve an explicitly supplied strict flag.
            body["tools"] = [{"type": "function", "strict": False, **t["function"]} for t in tools]
            body["parallel_tool_calls"] = True
            if not omit_tool_choice:
                body["tool_choice"] = ({"type": "function", "name": tool_choice["function"]["name"]}
                                       if isinstance(tool_choice, dict) else tool_choice or "auto")
        return body

    async def chat_stream(self, messages, tools=None, tool_choice=None, *, omit_tool_choice=False,
                          generation_overrides=None) -> AsyncGenerator[dict, None]:
        stream = self._chat_stream(messages, tools, tool_choice, omit_tool_choice=omit_tool_choice)
        try:
            async for event in stream:
                yield event
        except codex_auth.CodexAuthError as exc:
            raise CodexRequestError(str(exc)) from exc
        finally:
            await stream.aclose()

    async def _chat_stream(self, messages, tools=None, tool_choice=None, *, omit_tool_choice=False) -> AsyncGenerator[dict, None]:
        budget = _RequestBudget.from_policy(self.config.overall_timeout, self.config.max_retries)
        visible = False
        timeout = httpx.Timeout(connect=self.config.connect_timeout, read=None, write=self.config.timeout,
                                pool=self.config.connect_timeout)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, transport=self.transport,
                proxy=active_proxy_for_url(codex_auth.BASE_URL) if self.transport is None else None) as client:
            while budget.can_attempt():
                budget.begin_attempt()
                data = await budget.run(lambda: codex_auth.credentials(client=client), cap=30)
                body = self._body(messages, tools, tool_choice, data["account_id"], omit_tool_choice)
                response = None
                try:
                    for auth_attempt in range(2):
                        request = client.build_request("POST", codex_auth.BASE_URL + "/responses",
                            headers={**codex_auth.headers(data), "Accept": "text/event-stream"}, json=body)
                        response = await budget.run(lambda request=request: client.send(request, stream=True), cap=self.config.connect_timeout)
                        if response.status_code != 401 or auth_attempt:
                            break
                        await response.aclose()
                        data = await budget.run(lambda rejected=data["access_token"]: codex_auth.credentials(client=client,
                            rejected_token=rejected), cap=30)
                        body = self._body(messages, tools, tool_choice, data["account_id"], omit_tool_choice)
                    assert response is not None
                    if response.status_code != 200:
                        status = response.status_code
                        if status in {429, 500, 502, 503, 504} and budget.can_attempt():
                            await response.aclose()
                            await budget.backoff(min(8, 2 ** (budget.attempts - 1)))
                            continue
                        detail = ("Run astra auth login again." if status == 401 else
                                  "Check this account's Codex access." if status == 403 else
                                  "Subscription usage limit reached; try again later." if status == 429 else
                                  "Check the selected model and account, then retry.")
                        raise codex_auth.CodexAuthError(f"Codex request failed (HTTP {status}). {detail}")
                    lines = response.aiter_lines().__aiter__()
                    timing = _StreamReadTiming()
                    frame, content, reasoning = [], "", ""
                    summary_part = None
                    summaries = {}
                    tool_stream = ToolStream()
                    output_stream = OutputStream()
                    while True:
                        try:
                            line = await timing.read(lines, budget, self.config.idle_timeout)
                        except StopAsyncIteration:
                            raise LLMResponseError("stream_closed", "Codex stream ended before response.completed.") from None
                        if line.startswith("data:"):
                            frame.append(line[5:].lstrip())
                            continue
                        if line or not frame:
                            continue
                        encoded, frame = "\n".join(frame), []
                        if encoded == "[DONE]":
                            raise LLMResponseError("stream_closed", "Codex did not provide a completed response.")
                        try:
                            event = json.loads(encoded)
                        except ValueError as exc:
                            raise LLMResponseError("incomplete_response", "Invalid Codex stream event.") from exc
                        kind = event.get("type", "")
                        tool_stream.observe(event)
                        output_stream.observe(event)
                        if kind in {"response.output_text.delta", "response.refusal.delta"}:
                            delta = str(event.get("delta") or "")
                            if delta:
                                timing.observe("content")
                                content += delta
                                visible = True
                                yield {"type": "chunk", "content": delta}
                        elif kind == "response.reasoning_summary_text.delta":
                            delta = str(event.get("delta") or "")
                            if delta:
                                key = (event.get("output_index"), event.get("summary_index"))
                                summaries[key] = summaries.get(key, "") + delta
                                prefix = "推理摘要\n" if not reasoning else "\n\n" if summary_part != key else ""
                                summary_part = key
                                delta = prefix + delta
                                reasoning += delta
                                timing.observe("reasoning")
                                visible = True
                                yield {"type": "reasoning", "content": delta}
                        elif kind in {"response.function_call_arguments.delta", "response.output_item.done"}:
                            timing.observe("tool")
                            visible = True  # Never retry partially generated tool arguments.
                        elif kind in {"error", "response.failed", "response.incomplete"}:
                            raise LLMResponseError("incomplete_response", "Codex reported a failed or incomplete response.")
                        elif kind == "response.completed":
                            completed = output_stream.complete(event.get("response") or {})
                            result = completed_response(completed, account=data["account_id"], model=self.config.model)
                            tool_stream.validate(completed.get("output") or [])
                            if not result["content"].startswith(content):
                                raise LLMResponseError("incomplete_response", "Codex final text conflicts with streamed text.")
                            if result["content"][len(content):]:
                                yield {"type": "chunk", "content": result["content"][len(content):]}
                            for output_index, item in enumerate(completed.get("output") or []):
                                for summary_index, part in enumerate(item.get("summary") or []):
                                    if part.get("type") != "summary_text":
                                        continue
                                    key = (output_index, summary_index)
                                    text = str(part.get("text") or "")
                                    seen = summaries.get(key, "")
                                    # Final-only summaries are valid. Append missing suffixes
                                    # without printing already streamed text a second time.
                                    delta = text[len(seen):] if text.startswith(seen) else ""
                                    if delta:
                                        prefix = "推理摘要\n" if not reasoning else "\n\n" if summary_part != key else ""
                                        summary_part = key
                                        reasoning += prefix + delta
                                        yield {"type": "reasoning", "content": prefix + delta}
                            result["reasoning_content"] = reasoning
                            calls = result.pop("tool_calls")
                            yield {"type": "tool_calls" if calls else "done", "calls": calls, **result}
                            return
                except _RequestCapTimeout as exc:
                    raise LLMIdleTimeout("Codex made no progress before the connection/idle timeout.") from exc
                except httpx.RequestError as exc:
                    if visible or not budget.can_attempt():
                        raise LLMResponseError("stream_closed", "Codex connection failed; partial calls were discarded.") from exc
                    await budget.backoff(min(8, 2 ** (budget.attempts - 1)))
                finally:
                    if response is not None:
                        await response.aclose()

    async def chat(self, messages, tools=None, tool_choice=None, max_tokens=None) -> dict:
        stream = self.chat_stream(messages, tools, tool_choice)
        try:
            async for event in stream:
                if event["type"] in {"done", "tool_calls"}:
                    return {**event, "tool_calls": event.get("calls", [])}
        finally:
            await stream.aclose()
        raise LLMResponseError("stream_closed", "Codex returned no completed response.")

    async def chat_limited(self, messages, tools=None, *, max_tokens, temperature=0.1,
                           disable_thinking=False, reasoning_effort=None, request_timeout=None, max_retries=None):
        config = replace(self.config,
            reasoning_effort=reasoning_effort or self.config.reasoning_effort,
            overall_timeout=request_timeout if request_timeout is not None else self.config.overall_timeout,
            max_retries=max_retries if max_retries is not None else self.config.max_retries)
        return await type(self)(config, transport=self.transport).chat(messages, tools)

    def estimate_tokens(self, messages):
        return max(1, round(estimate_messages_tokens(messages) * self._estimate_calibration))

    def record_prompt_usage(self, estimated, actual):
        if estimated and actual and estimated > 0 and actual > 0:
            ratio = max(0.25, min(4.0, self._estimate_calibration * actual / estimated))
            self._estimate_calibration = self._estimate_calibration * 0.8 + ratio * 0.2
