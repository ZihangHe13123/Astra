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

Earlier turns are replayed as native turns before the last one asks for the answer, so each
request repeats the previous one's prefix and reads it from the prompt cache instead of writing
the whole conversation again (live 2026-09-24: one tool round written per step instead of all).
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
# Dropped from the CLI's environment: anything that would move billing, routing or model aliases
# away from the signed-in subscription (a provider switcher sets ANTHROPIC_DEFAULT_*_MODEL[_NAME]),
# and whatever a parent Claude Code session or the user's shell set for their own sessions
# (effort, entrypoint, host sockets, MCP tuning), so every Astra request behaves the same wherever
# Astra was started. Only the CLI's own login location is kept.
STRIPPED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE", "MCP_")
KEPT_ENV = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"})
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
LOGIN_HINT = "Install the Claude Code CLI (https://claude.ai/install.sh) and run `claude auth login` in a terminal."
# Astra's one cache breakpoint; the CLI spends the other three the API allows. One hour, as the
# CLI is told to use for its own, because a longer breakpoint may not follow a shorter one.
CACHE_MARKER = {"type": "ephemeral", "ttl": "1h"}
# A replayed turn is acknowledged without any request; the first also waits for the CLI to start.
ACK_TIMEOUT = 60.0


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
                   if not k.startswith(STRIPPED_ENV_PREFIXES) or k in KEPT_ENV}
    # Tool search would hide Astra's tools behind Claude Code's own ToolSearch call, which the
    # one-response limit leaves no room for (live Astra: the first real task failed on it).
    environment["ENABLE_TOOL_SEARCH"] = "false"
    # A token countdown the CLI appends to each request would differ every time and move the
    # cached prefix; Astra keeps its own budget.
    environment["CLAUDE_CODE_TOTAL_TOKENS_REMINDER"] = "off"
    environment["CLAUDE_CODE_PROMPT_CACHE_TTL"] = CACHE_MARKER["ttl"]
    return environment


def workspace() -> str:
    """The CLI's working directory: fixed, private and outside any project. The CLI tells Claude
    its working directory and git status on every request, so a fresh temporary directory each
    time changed the prompt right after the first message and nothing past it was ever cached."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "Astra"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Astra"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "astra"
    path = base / "claude-code"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(path)


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


def _leading_system(messages: list[dict]) -> int:
    count = 0
    while count < len(messages) and messages[count].get("role") in {"system", "developer"}:
        count += 1
    return count


def system_prompt(messages: list[dict], tools: list[dict] | None) -> str:
    """Astra's leading system messages. Later ones stay where they are in the conversation, and
    nothing that changes from step to step goes here, since the whole cached prefix follows it."""
    sections = [_text(m.get("content")) for m in messages[:_leading_system(messages)]]
    if tools:
        sections.append("# Tools\nAstra runs your tool calls itself and returns each result in the next "
                        "message. Call tools directly, several at once when they are independent.")
    return "\n\n".join(s for s in sections if s.strip())


def tool_choice_note(tool_choice) -> str:
    forced = tool_choice.get("function", {}).get("name") if isinstance(tool_choice, dict) else None
    return (f"You must call `{forced}` now." if forced else
            "Do not call any tool now." if tool_choice == "none" else
            "You must call at least one tool now." if tool_choice == "required" else "")


def _reminder(text: str) -> dict:
    return {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}


def _call_text(call: dict) -> dict:
    function = call.get("function") or {}
    name = str(function.get("name", ""))
    # The same name Claude sees in its tool list, so it does not copy a different form.
    native = TOOL_PREFIX + name if TOOL_NAME.fullmatch(name) else name
    return {"type": "text", "text": "[tool call " + json.dumps(
        {"id": call.get("id", ""), "name": native, "arguments": function.get("arguments", "{}")},
        ensure_ascii=False) + "]"}


def _tool_input(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {"arguments": str(arguments)}
    return value if isinstance(value, dict) else {"arguments": value}


def _tool_use_id(raw, used: set[str]) -> str:
    """Claude's pattern for a call id, unique in the conversation; other providers' ids can hold
    other characters or repeat in every turn. Derived from history alone, so a replay is stable."""
    base = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw or ""))[:60] or "call"
    candidate, suffix = base, 1
    while candidate in used:
        suffix += 1
        candidate = f"{base}_{suffix}"
    used.add(candidate)
    return candidate


def conversation(messages: list[dict], names: frozenset[str]) -> list[tuple[str, list[dict]]] | None:
    """The conversation after the system prompt as alternating native turns, starting and ending
    with a user turn: tool results first in each user turn, then text, images and later system
    messages as reminders. Calls to tools not offered now stay text, with their results, so the
    request stays valid. None when the conversation ends on Claude's own words (a prefill), which a
    replay cannot continue."""
    turns: list[tuple[str, list[dict], list[dict]]] = []  # role, tool results, other blocks
    used: set[str] = set()
    pending: dict[str, str] = {}  # Astra's id -> native id, for calls of the latest assistant turn
    for message in messages[_leading_system(messages):]:
        role = message.get("role")
        if role == "assistant":
            blocks: list[dict] = []
            text = _text(message.get("content"))
            if text.strip():
                blocks.append({"type": "text", "text": text})
            calls: dict[str, str] = {}
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                if name in names and TOOL_NAME.fullmatch(name):
                    native = _tool_use_id(call.get("id"), used)
                    calls[str(call.get("id") or "")] = native
                    blocks.append({"type": "tool_use", "id": native, "name": TOOL_PREFIX + name,
                                   "input": _tool_input(function.get("arguments"))})
                else:
                    blocks.append(_call_text(call))
            if not blocks:
                continue
            if turns and turns[-1][0] == "assistant":
                turns[-1][2].extend(blocks)
            else:
                turns.append(("assistant", [], blocks))
                pending = {}
            pending.update(calls)
            continue
        if not turns or turns[-1][0] != "user":
            turns.append(("user", [], []))
        results, blocks = turns[-1][1], turns[-1][2]
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            content = _content_blocks(message.get("content")) or [{"type": "text", "text": "(no output)"}]
            native = pending.pop(call_id, None)
            if native is not None:
                results.append({"type": "tool_result", "tool_use_id": native, "content": content})
            else:
                blocks.extend([{"type": "text", "text": f"[tool result for call {call_id}]"}, *content])
        elif role in {"system", "developer"}:
            text = _text(message.get("content"))
            if text.strip():
                blocks.append(_reminder(text))
        else:
            blocks.extend(_content_blocks(message.get("content")))
    if not turns or turns[-1][0] != "user":
        return None
    if turns[0][0] != "user":
        turns.insert(0, ("user", [], [{"type": "text", "text": "[Earlier messages are not shown.]"}]))
    for index, (role, _, blocks) in enumerate(turns):
        if role != "assistant":
            continue
        # Every native call needs its result in the next turn; a lost one is reported as such.
        answered = {r["tool_use_id"] for r in turns[index + 1][1]}
        turns[index + 1][1].extend(
            {"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
             "content": [{"type": "text", "text": "No result was recorded for this call."}]}
            for b in blocks if b["type"] == "tool_use" and b["id"] not in answered)
    return [(role, (results + blocks) or [{"type": "text", "text": "(empty message)"}])
            if role == "user" else (role, blocks) for role, results, blocks in turns]


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
    """The conversation as one user turn of native blocks, images kept in place: the fallback when
    it cannot be replayed turn by turn."""
    blocks: list[dict] = [{"type": "text", "text": "Conversation so far, oldest first. Reply as the assistant to its last message."}]
    for message in messages[_leading_system(messages):]:
        role = message.get("role")
        if role == "tool":
            blocks.append({"type": "text", "text": f"\n[tool result for call {message.get('tool_call_id', '')}]"})
        else:
            blocks.append({"type": "text", "text": f"\n[{'system' if role == 'developer' else role}]"})
        blocks.extend(_content_blocks(message.get("content")))
        blocks.extend(_call_text(call) for call in message.get("tool_calls") or [])
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


class _Fallback(Exception):
    """A CLI behaviour the request relied on is unavailable; retry without it for this process."""

    def __init__(self, switch: str, message: str):
        super().__init__(message)
        self.switch = switch
        self.message = message


class _Lines:
    """The CLI's output, remembering whether it printed anything at all."""

    def __init__(self, stream):
        self.stream = stream
        self.seen = False

    async def read(self, timeout: float) -> bytes:
        line = await asyncio.wait_for(self.stream.readline(), timeout)
        self.seen = self.seen or bool(line)
        return line


class ClaudeCodeProvider:
    # Turned off for the rest of the process when this CLI rejects them: turn-by-turn replay
    # (falls back to one transcript turn), Astra's cache breakpoint and thinking summaries.
    replay = True
    cache_marker = True
    thinking_display = True

    def __init__(self, config, *, command: str | None = None):
        self.config = config
        self.command = command
        self._estimate_calibration = 1.0

    def supports_forced_tool_choice(self) -> bool:
        return False

    def _arguments(self, command: str, files: str, bridged: bool) -> list[str]:
        model = (self.config.model or "sonnet").strip()
        arguments = [command, "-p", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
                     "--include-partial-messages", "--model", model, "--system-prompt-file", os.path.join(files, "system.md"),
                     "--restricted", "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence", "--max-turns", "1", "--permission-mode", "dontAsk"]
        if type(self).thinking_display:
            # Current models return no thinking text unless asked (live Astra 2026-09-24: one step in
            # sixty showed any), so a long think looked like a stalled backend. Summaries stream as
            # reasoning; billing counts the thinking either way. The Agent SDK's thinking.display.
            arguments += ["--thinking-display", "summarized"]
        if bridged:
            servers = {"mcpServers": {BRIDGE_NAME: {"command": sys.executable,
                                                    "args": [BRIDGE, os.path.join(files, "tools.json")],
                                                    "alwaysLoad": True}}}
            arguments += ["--mcp-config", json.dumps(servers)]
        effort = str(getattr(self.config, "reasoning_effort", "") or "").strip().lower()
        if effort in EFFORTS:
            arguments += ["--effort", effort]
        return arguments

    async def chat_stream(self, messages, tools=None, tool_choice=None, *, omit_tool_choice=False,
                          generation_overrides=None) -> AsyncGenerator[dict, None]:
        for _ in range(4):
            started = False
            try:
                async for event in self._stream(messages, tools, None if omit_tool_choice else tool_choice):
                    started = True
                    yield event
                return
            except _Fallback as fallback:
                if started or not getattr(type(self), fallback.switch):
                    raise ClaudeCodeError(fallback.message) from None
                setattr(type(self), fallback.switch, False)
        raise ClaudeCodeError("Claude Code could not complete the request; retry it.")

    def _turns(self, prepared: list[dict], names: frozenset[str], note: str) -> tuple[list[tuple[str, list[dict]]], bool]:
        """Native turns to send, and whether Astra's cache breakpoint is among them."""
        turns = conversation(prepared, names) if type(self).replay else None
        if turns is None:
            blocks = transcript(prepared)
            return [("user", blocks + ([{"type": "text", "text": note}] if note else []))], False
        if note:
            turns[-1][1].append(_reminder(note))
        if not type(self).cache_marker or len(turns) < 3:
            return turns, False
        # The newest turn is sent differently the next time, once it is no longer the one asking
        # for the answer, so its cached copy is never reused: the breakpoint goes on the user turn
        # before it (never on a tool call, whose breakpoint the CLI drops). The next request finds
        # this one's entry a few blocks back from its own breakpoint.
        blocks = turns[-3][1]
        blocks[-1] = {**blocks[-1], "cache_control": dict(CACHE_MARKER)}
        return turns, True

    async def _stream(self, messages, tools, tool_choice) -> AsyncGenerator[dict, None]:
        command = self.command or claude_command()
        if command is None:
            raise ClaudeCodeError("Claude Code CLI not found. " + LOGIN_HINT)
        listed = bridge_tools([t for t in tools or [] if isinstance(t, dict)])
        names = frozenset(t["name"] for t in listed)
        prepared = _messages_for_capabilities(messages, self.config.capabilities, names, self.config.vision_detail)
        turns, marked = self._turns(prepared, names, tool_choice_note(tool_choice) if listed else "")
        with tempfile.TemporaryDirectory(prefix="astra-claude-code-") as files:
            Path(files, "system.md").write_text(system_prompt(prepared, listed) or "You are a helpful assistant.",
                                                encoding="utf-8")
            if listed:
                Path(files, "tools.json").write_text(json.dumps(listed, ensure_ascii=False), encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                *self._arguments(command, files, bool(listed)), cwd=workspace(), env=child_environment(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True, limit=64 * 1024 * 1024)
            try:
                assert process.stdin is not None and process.stdout is not None
                lines = _Lines(process.stdout)
                await self._replay(process, lines, turns[:-1])
                await self._send(process, lines, {"type": "user", "message": {"role": "user", "content": turns[-1][1]}})
                process.stdin.close()
                async for event in self._events(lines, marked):
                    yield event
            finally:
                _terminate(process)
                await process.wait()

    async def _send(self, process, lines: _Lines, frame: dict) -> None:
        try:
            process.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise self._ended(lines, "replay", "Claude Code stopped reading the conversation. " + LOGIN_HINT) from None

    async def _replay(self, process, lines: _Lines, turns: list[tuple[str, list[dict]]]) -> None:
        """Append earlier turns without a request: each user turn waits for the CLI's zero-turn
        result, so the following assistant turn cannot land ahead of it."""
        for role, blocks in turns:
            if role == "assistant":
                await self._send(process, lines, {"type": "assistant", "message": {"role": "assistant", "content": blocks}})
                continue
            await self._send(process, lines, {"type": "user", "message": {"role": "user", "content": blocks},
                                              "shouldQuery": False})
            await self._acknowledged(lines)

    def _ended(self, lines: _Lines, fallback: str, message: str) -> Exception:
        """The CLI closed its output. Nothing at all means it refused to start, which is how a
        version without an option Astra passes behaves; otherwise the given fallback applies."""
        if not lines.seen and type(self).thinking_display:
            return _Fallback("thinking_display", "Claude Code did not start; retry the request.")
        return _Fallback(fallback, message) if fallback else ClaudeCodeError(message)

    async def _acknowledged(self, lines: _Lines) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ACK_TIMEOUT
        while True:
            try:
                line = await lines.read(max(0.0, deadline - loop.time()))
            except TimeoutError:
                raise ClaudeCodeError("Claude Code did not take the conversation history in time; retry the request.") from None
            if not line:
                raise self._ended(lines, "replay", "Claude Code ended while reading the conversation. " + LOGIN_HINT)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "result":
                continue
            if event.get("num_turns") == 0 and not event.get("is_error"):
                return
            # A CLI that answers a history turn instead of appending it would spend a request per turn.
            raise _Fallback("replay", "Claude Code answered an earlier turn instead of the latest one; retry the request.")

    async def _events(self, lines: _Lines, marked: bool = False) -> AsyncGenerator[dict, None]:
        loop = asyncio.get_running_loop()
        idle = float(getattr(self.config, "idle_timeout", 0) or 0) or 300.0
        overall = getattr(self.config, "overall_timeout", None)
        deadline = loop.time() + float(overall) if overall else None
        reasoning, content, streamed, calls = "", "", "", []
        while True:
            wait = idle if deadline is None else max(0.0, min(idle, deadline - loop.time()))
            try:
                line = await lines.read(wait)
            except TimeoutError:
                raise LLMIdleTimeout("Claude Code made no progress before the idle or overall timeout.") from None
            if not line:
                raise self._ended(lines, "", "Claude Code ended without an answer. " + LOGIN_HINT)
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
                final = self._final(event, content, calls, reasoning, marked)
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

    def _final(self, result: dict, content: str, calls: list[dict], reasoning: str, marked: bool = False) -> dict:
        # With tools the CLI stops at its one-turn limit right after the calls; that is the answer.
        stopped_for_calls = bool(calls) and result.get("subtype") == "error_max_turns"
        if not stopped_for_calls and (result.get("is_error") or result.get("subtype") != "success"):
            if marked and "cache_control" in str(result.get("result") or ""):
                # The API refused the breakpoint (a CLI that spends all four itself): answer without it.
                raise _Fallback("cache_marker", "Claude Code rejected the request's cache breakpoint; retry the request.")
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
