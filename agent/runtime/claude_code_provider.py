"""Claude subscription through the user's own signed-in, unmodified Claude Code CLI.

Each model call starts the official ``claude`` executable in print mode. The CLI signs its own
requests with the account the user logged into through Anthropic's flow (``claude auth login``);
Astra never reads, stores or forwards Claude credentials. Anthropic permits an end user signing in
to the unmodified binary with their own subscription; collecting or proxying Claude.ai tokens is
not permitted and is not done here.

The CLI runs isolated: no built-in tools, no user, project or local settings (so a settings-file
redirect such as a provider switcher cannot move the request), no skills and no saved session.
Astra's tools reach it only through a local MCP bridge that lists them and never runs them, so
Claude calls them natively; the CLI denies each call and stops after that single response, and
Astra runs the calls itself with its own approvals.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import tempfile
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path

from .llm import LLMIdleTimeout, LLMResponseError, _messages_for_capabilities
from .token_estimator import estimate_messages_tokens

BASE_URL = "claude-code://local"
COMMAND_ENV = "ASTRA_CLAUDE_CODE_COMMAND"
# Aliases the CLI resolves to the newest model the signed-in account can use, with their context
# windows: current Fable, Opus and Sonnet run with 1M tokens on every plan, Haiku with 200K.
MODELS = ("fable", "opus", "sonnet", "haiku")
CONTEXT_WINDOWS = {"fable": 1_000_000, "opus": 1_000_000, "sonnet": 1_000_000, "haiku": 200_000}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BRIDGE = str(Path(__file__).with_name("claude_code_tool_bridge.py"))
BRIDGE_NAME = "astra"
TOOL_PREFIX = f"mcp__{BRIDGE_NAME}__"
# Claude accepts tool names up to 64 characters, and the bridge prefix counts.
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,%d}" % (64 - len(TOOL_PREFIX)))
# Anything that would move billing, routing or model aliases away from the signed-in subscription:
# keys, endpoints, alias overrides (a provider switcher sets ANTHROPIC_DEFAULT_*_MODEL[_NAME]) and
# cloud-provider selectors. The subscription login needs none of them.
ROUTING_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
LOGIN_HINT = "Install the Claude Code CLI (https://claude.ai/install.sh) and run `claude auth login` in a terminal."


class ClaudeCodeError(LLMResponseError):
    """Astra-authored diagnostics only; CLI output is never shown verbatim."""

    def __init__(self, message: str):
        super().__init__("claude_code_failed", message)
        self.message = message

    @property
    def public_message(self) -> str:
        return self.message

    @property
    def public_code(self) -> str:
        return "claude_code_failed"


def claude_command() -> str | None:
    """The official CLI: an explicit path, then PATH, then the native installer's location."""
    configured = os.getenv(COMMAND_ENV, "").strip()
    candidates = [configured] if configured else [shutil.which("claude") or "", str(Path.home() / ".local/bin/claude")]
    return next((c for c in candidates if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    environment = {k: v for k, v in (os.environ if base is None else base).items()
                   if not k.startswith(ROUTING_ENV_PREFIXES)}
    # Tool search would hide Astra's tools behind Claude Code's own ToolSearch call, which the
    # one-response limit leaves no room for (live Astra: the first real task failed on it).
    environment["ENABLE_TOOL_SEARCH"] = "false"
    return environment


async def login_status(command: str | None = None, timeout: float = 15) -> tuple[bool, str]:
    """Whether the CLI is signed in, and how, from ``claude auth status``; no credential is read."""
    command = command or claude_command()
    if command is None:
        return False, "Claude Code CLI not found. " + LOGIN_HINT
    process = await asyncio.create_subprocess_exec(
        command, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=child_environment(), start_new_session=True)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        _terminate(process)
        return False, "Claude Code did not report its login status in time."
    try:
        status = json.loads(output or b"{}")
    except ValueError:
        return False, "Claude Code returned an unreadable login status."
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        return False, "Claude Code is not signed in. Run `claude auth login` in a terminal."
    method = str(status.get("authMethod") or "unknown")
    plan = str(status.get("subscriptionType") or "").strip()
    return True, f"Claude Code signed in ({method}{' · ' + plan if plan else ''})"


def _terminate(process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict) and p.get("type") in {"text", "input_text", "output_text"})
    return "" if content is None else str(content)


def _image_block(part: dict) -> dict | None:
    """A base64 data URL as a native image block; remote URLs are not fetched."""
    url = str((part.get("image_url") or {}).get("url") or "") if isinstance(part.get("image_url"), dict) \
        else str(part.get("image_url") or "")
    header, _, data = url.partition(",")
    if not header.startswith("data:") or not header.endswith(";base64") or not data:
        return None
    media_type = header[5:-7]
    if media_type not in IMAGE_TYPES:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def _content_blocks(content) -> list[dict]:
    if not isinstance(content, list):
        text = _text(content)
        return [{"type": "text", "text": text}] if text else []
    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            image = _image_block(part)
            blocks.append(image or {"type": "text", "text": "[image omitted: not a supported inline image]"})
        elif str(part.get("text") or ""):
            blocks.append({"type": "text", "text": str(part["text"])})
    return blocks


def system_prompt(messages: list[dict], tools: list[dict] | None, tool_choice=None) -> str:
    sections = [_text(m.get("content")) for m in messages if m.get("role") in {"system", "developer"}]
    if tools:
        forced = tool_choice.get("function", {}).get("name") if isinstance(tool_choice, dict) else None
        sections.append(
            "# Tools\nAstra runs your tool calls and shows each result in the next message of the "
            "transcript. Call tools directly, several at once when they are independent."
            + (f" You must call `{forced}` now." if forced else
               " Do not call any tool now." if tool_choice == "none" else
               " You must call at least one tool now." if tool_choice == "required" else ""))
    return "\n\n".join(s for s in sections if s.strip())


def bridge_tools(tools: list[dict]) -> list[dict]:
    """Astra's function tools as MCP tool definitions; names Claude cannot accept are left out."""
    listed = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
            continue
        schema = function.get("parameters")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        listed.append({"name": name, "description": str(function.get("description") or ""), "inputSchema": schema})
    return listed


def transcript(messages: list[dict]) -> list[dict]:
    """The conversation as one user turn of native blocks, images kept in place."""
    blocks: list[dict] = [{"type": "text", "text": "Conversation so far, oldest first. Reply as the assistant to its last message."}]
    for message in messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            continue
        if role == "tool":
            blocks.append({"type": "text", "text": f"\n[tool result for call {message.get('tool_call_id', '')}]"})
        else:
            blocks.append({"type": "text", "text": f"\n[{role}]"})
        blocks.extend(_content_blocks(message.get("content")))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = str(function.get("name", ""))
            # The same name Claude sees in its tool list, so it does not copy a different form.
            native = TOOL_PREFIX + name if TOOL_NAME.fullmatch(name) else name
            blocks.append({"type": "text", "text": "[tool call "
                           + json.dumps({"id": call.get("id", ""), "name": native,
                                         "arguments": function.get("arguments", "{}")}, ensure_ascii=False) + "]"})
    return blocks


def _usage(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    fresh = int(raw.get("input_tokens") or 0) + int(raw.get("cache_creation_input_tokens") or 0)
    cached = int(raw.get("cache_read_input_tokens") or 0)
    completion = int(raw.get("output_tokens") or 0)
    return {"prompt_tokens": fresh + cached, "completion_tokens": completion, "total_tokens": fresh + cached + completion,
            "prompt_cache_hit_tokens": cached, "prompt_cache_miss_tokens": fresh}


def _failure(result: dict) -> ClaudeCodeError:
    text = str(result.get("result") or "").lower()
    if "login" in text or "logged in" in text or "authenticat" in text:
        return ClaudeCodeError("Claude Code is not signed in. Run `claude auth login` in a terminal.")
    if "limit" in text or "rate" in text or "overloaded" in text:
        return ClaudeCodeError("The Claude subscription is at its usage limit or busy; try again later.")
    if result.get("subtype") == "error_max_turns":
        return ClaudeCodeError("Claude Code stopped without an answer or tool call; retry the request.")
    return ClaudeCodeError("Claude Code reported an error and returned no answer; retry the request.")


class ClaudeCodeProvider:
    def __init__(self, config, *, command: str | None = None):
        self.config = config
        self.command = command
        self._estimate_calibration = 1.0

    def supports_forced_tool_choice(self) -> bool:
        return False

    def _arguments(self, command: str, workdir: str, bridged: bool) -> list[str]:
        model = (self.config.model or "sonnet").strip()
        arguments = [command, "-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
                     "--include-partial-messages", "--model", model, "--system-prompt-file", os.path.join(workdir, "system.md"),
                     "--restricted", "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence", "--max-turns", "1", "--permission-mode", "dontAsk"]
        if bridged:
            servers = {"mcpServers": {BRIDGE_NAME: {"command": sys.executable,
                                                    "args": [BRIDGE, os.path.join(workdir, "tools.json")],
                                                    "alwaysLoad": True}}}
            arguments += ["--mcp-config", json.dumps(servers)]
        effort = str(getattr(self.config, "reasoning_effort", "") or "").strip().lower()
        if effort in EFFORTS:
            arguments += ["--effort", effort]
        return arguments

    async def chat_stream(self, messages, tools=None, tool_choice=None, *, omit_tool_choice=False,
                          generation_overrides=None) -> AsyncGenerator[dict, None]:
        command = self.command or claude_command()
        if command is None:
            raise ClaudeCodeError("Claude Code CLI not found. " + LOGIN_HINT)
        listed = bridge_tools([t for t in tools or [] if isinstance(t, dict)])
        names = frozenset(t["name"] for t in listed)
        prepared = _messages_for_capabilities(messages, self.config.capabilities, names, self.config.vision_detail)
        user = {"type": "user", "message": {"role": "user", "content": transcript(prepared)}}
        with tempfile.TemporaryDirectory(prefix="astra-claude-code-") as workdir:
            Path(workdir, "system.md").write_text(
                system_prompt(prepared, listed, None if omit_tool_choice else tool_choice) or "You are a helpful assistant.",
                encoding="utf-8")
            if listed:
                Path(workdir, "tools.json").write_text(json.dumps(listed, ensure_ascii=False), encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                *self._arguments(command, workdir, bool(listed)), cwd=workdir, env=child_environment(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True, limit=64 * 1024 * 1024)
            try:
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write((json.dumps(user, ensure_ascii=False) + "\n").encode())
                await process.stdin.drain()
                process.stdin.close()
                async for event in self._events(process):
                    yield event
            finally:
                _terminate(process)
                await process.wait()

    async def _events(self, process) -> AsyncGenerator[dict, None]:
        loop = asyncio.get_running_loop()
        idle = float(getattr(self.config, "idle_timeout", 0) or 0) or 300.0
        overall = getattr(self.config, "overall_timeout", None)
        deadline = loop.time() + float(overall) if overall else None
        reasoning, content, streamed, calls = "", "", "", []
        while True:
            wait = idle if deadline is None else max(0.0, min(idle, deadline - loop.time()))
            try:
                line = await asyncio.wait_for(process.stdout.readline(), wait)
            except TimeoutError:
                raise LLMIdleTimeout("Claude Code made no progress before the idle or overall timeout.") from None
            if not line:
                raise ClaudeCodeError("Claude Code ended without an answer. " + LOGIN_HINT)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "stream_event":
                delta = (event.get("event") or {}).get("delta") or {}
                if (event.get("event") or {}).get("type") != "content_block_delta" or not isinstance(delta, dict):
                    continue
                if delta.get("type") == "text_delta" and delta.get("text"):
                    streamed += str(delta["text"])
                    yield {"type": "chunk", "content": str(delta["text"])}
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    piece = str(delta["thinking"])
                    reasoning += piece
                    yield {"type": "reasoning", "content": piece}
            elif event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "thinking" and block.get("thinking") and not reasoning:
                        # Only when the thinking was not streamed piece by piece.
                        reasoning = str(block["thinking"])
                        yield {"type": "reasoning", "content": reasoning}
                    elif block.get("type") == "text" and block.get("text"):
                        content += str(block["text"])
                    elif block.get("type") == "tool_use":
                        calls.append(self._call(block))
            elif event.get("type") == "result":
                final = self._final(event, content, calls, reasoning)
                # Astra shows prose only from chunks: send whatever the stream did not.
                if final["content"].startswith(streamed) and final["content"][len(streamed):]:
                    yield {"type": "chunk", "content": final["content"][len(streamed):]}
                yield final
                return

    @staticmethod
    def _call(block: dict) -> dict:
        """A native call as Astra's call. Claude sometimes repeats the plain name it read in the
        transcript (live: execute_shell for mcp__astra__execute_shell); Astra's registry decides
        whether a name exists and answers an unknown one with a recoverable error, as for any model."""
        raw = str(block.get("name") or "")
        name = raw[len(TOOL_PREFIX):] if raw.startswith(TOOL_PREFIX) else raw
        arguments = block.get("input")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) or not isinstance(arguments, dict) or not block.get("id"):
            raise LLMResponseError("invalid_tool_arguments", "Claude Code returned a malformed tool call.")
        return {"id": str(block["id"]), "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}

    def _final(self, result: dict, content: str, calls: list[dict], reasoning: str) -> dict:
        # With tools the CLI stops at its one-turn limit right after the calls; that is the answer.
        stopped_for_calls = bool(calls) and result.get("subtype") == "error_max_turns"
        if not stopped_for_calls and (result.get("is_error") or result.get("subtype") != "success"):
            raise _failure(result)
        if not calls:
            content = content or str(result.get("result") or "")
        if not content.strip() and not calls:
            raise LLMResponseError("empty_response", "Claude Code returned no answer or tool call.")
        return {"type": "tool_calls" if calls else "done", "calls": calls, "content": content,
                "reasoning_content": reasoning, "finish_reason": "tool_calls" if calls else "stop",
                "tool_call_state": "complete", "usage": _usage(result.get("usage"))}

    async def chat(self, messages, tools=None, tool_choice=None, max_tokens=None) -> dict:
        stream = self.chat_stream(messages, tools, tool_choice)
        try:
            async for event in stream:
                if event["type"] in {"done", "tool_calls"}:
                    return {**event, "tool_calls": event.get("calls", [])}
        finally:
            await stream.aclose()
        raise LLMResponseError("stream_closed", "Claude Code returned no completed response.")

    async def chat_limited(self, messages, tools=None, *, max_tokens, temperature=0.1,
                           disable_thinking=False, reasoning_effort=None, request_timeout=None, max_retries=None):
        config = replace(self.config, reasoning_effort=reasoning_effort or self.config.reasoning_effort)
        provider = type(self)(config, command=self.command)
        if request_timeout:
            return await asyncio.wait_for(provider.chat(messages, tools), request_timeout)
        return await provider.chat(messages, tools)

    def estimate_tokens(self, messages):
        return max(1, round(estimate_messages_tokens(messages) * self._estimate_calibration))

    def record_prompt_usage(self, estimated, actual):
        if estimated and actual and estimated > 0 and actual > 0:
            ratio = max(0.25, min(4.0, self._estimate_calibration * actual / estimated))
            self._estimate_calibration = self._estimate_calibration * 0.8 + ratio * 0.2
