"""Claude subscription through the user's own signed-in claude CLI, against a fake executable."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agent.cli import connections, model_catalog, provider_connections
from agent.runtime import claude_code_provider as ccp
from agent.runtime.claude_code_provider import ClaudeCodeError, ClaudeCodeProvider
from agent.runtime.llm import LLMConfig, LLMIdleTimeout, LLMResponseError
from agent.runtime.providers import DEFAULT_PROVIDER_REGISTRY

FAKE_CLI = r'''#!__PYTHON__
import json, os, sys, time
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
          "env": {k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_", "ENABLE_TOOL_SEARCH"))}}
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
record["system"] = open(sys.argv[sys.argv.index("--system-prompt-file") + 1]).read()
if "--mcp-config" in sys.argv:
    servers = json.loads(sys.argv[sys.argv.index("--mcp-config") + 1])["mcpServers"]
    record["servers"] = servers
    record["tools"] = json.load(open(servers["astra"]["args"][1]))
record["stdin"] = json.loads(sys.stdin.readline())
json.dump(record, open(os.environ["FAKE_CLAUDE_RECORD"], "w"))
scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "tools")
emit = lambda value: (print(json.dumps(value)), sys.stdout.flush())
emit({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
if scenario == "hang":
    time.sleep(60)
emit({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "Need the file."}]}})
usage = {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7}
denied = {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "denied"}]}}
if scenario in {"tools", "foreign"}:
    name = "mcp__astra__read_file" if scenario == "tools" else "execute_shell"
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading it."}]}})
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": name,
                                                        "input": {"path": "a.txt"}}]}})
    emit(denied)
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_2", "name": name,
                                                        "input": {"path": "b.txt"}}]}})
    emit(denied)
    emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 2, "usage": usage})
elif scenario == "text":
    for piece in ("Hello ", "from "):
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
                                                 "delta": {"type": "text_delta", "text": piece}}})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello from Claude."}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "usage": usage, "result": "Hello from Claude."})
elif scenario == "not_logged_in":
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"})
'''

READ_FILE = {"type": "function", "function": {"name": "read_file", "description": "Read a file.",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLI.replace("__PYTHON__", sys.executable))
    path.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    return path, record


def provider(command, **overrides):
    config = LLMConfig(model="sonnet", provider="claude-code", base_url=ccp.BASE_URL,
                       capabilities=frozenset({"streaming", "tools", "reasoning", "vision"}),
                       reasoning_effort="high", **overrides)
    return ClaudeCodeProvider(config, command=str(command))


def collect(stream):
    async def run():
        return [event async for event in stream]
    return asyncio.run(run())


MESSAGES = [{"role": "system", "content": "You are Astra."}, {"role": "user", "content": "Read a.txt"}]


def test_native_tool_calls_come_back_as_an_astra_tool_batch(fake_cli):
    command, _ = fake_cli
    events = collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))
    assert events[0] == {"type": "reasoning", "content": "Need the file."}
    final = events[-1]
    assert final["type"] == "tool_calls" and final["finish_reason"] == "tool_calls"
    assert final["content"] == "Reading it."
    assert [(c["id"], c["name"], json.loads(c["arguments"])) for c in final["calls"]] == [
        ("toolu_1", "read_file", {"path": "a.txt"}), ("toolu_2", "read_file", {"path": "b.txt"})]
    assert final["usage"] == {"prompt_tokens": 115, "completion_tokens": 7, "total_tokens": 122,
                              "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 15}


def test_a_plain_or_unoffered_tool_name_reaches_astras_registry(fake_cli, monkeypatch):
    """Live Astra 2026-09-24: Claude called execute_shell by the plain name it read in the
    transcript, and the turn failed. Astra's registry, not the provider, decides whether a name
    exists; an unknown one gets a recoverable error the model can act on."""
    command, _ = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "foreign")
    final = collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))[-1]
    assert final["type"] == "tool_calls"
    assert [c["name"] for c in final["calls"]] == ["execute_shell", "execute_shell"]


def test_cli_runs_isolated_and_bills_only_the_signed_in_subscription(fake_cli, monkeypatch):
    """No built-in tools, settings, skills or other MCP servers; overrides never reach the CLI."""
    command, record = fake_cli
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://redirect.invalid")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    # A provider switcher redirects the CLI's model aliases through these.
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "deepseek-v4-flash")
    collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                 "--include-partial-messages"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "high"
    # One response, then stop: every call is denied, so the CLI can never run a tool itself.
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--json-schema" not in argv
    [(name, server)] = seen["servers"].items()
    assert name == "astra" and server["command"] == sys.executable and server["alwaysLoad"] is True
    assert server["args"][0].endswith("claude_code_tool_bridge.py")
    assert seen["tools"] == [{"name": "read_file", "description": "Read a file.",
                              "inputSchema": READ_FILE["function"]["parameters"]}]
    # Only the switch that keeps every Astra tool loaded up front, never a routing variable.
    assert seen["env"] == {"ENABLE_TOOL_SEARCH": "false"}
    assert Path(seen["cwd"]).name.startswith("astra-claude-code-")
    assert "You are Astra." in seen["system"] and "Astra runs your tool calls" in seen["system"]
    assert seen["stdin"]["type"] == "user"


def test_bridge_lists_astras_tools_and_never_runs_them(tmp_path):
    tools = ccp.bridge_tools([READ_FILE, {"type": "function", "function": {"name": "bad name!", "parameters": {}}}])
    assert [t["name"] for t in tools] == ["read_file"]
    path = tmp_path / "tools.json"
    path.write_text(json.dumps(tools))
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {}}}]

    async def run():
        process = await asyncio.create_subprocess_exec(sys.executable, ccp.BRIDGE, str(path),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        out, _ = await process.communicate("".join(json.dumps(r) + "\n" for r in requests).encode())
        return [json.loads(line) for line in out.decode().splitlines()]

    replies = asyncio.run(run())
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert replies[1]["result"]["tools"] == tools
    assert replies[2]["result"]["isError"] is True


def test_plain_answer_without_tools_starts_no_bridge(fake_cli, monkeypatch):
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    final = collect(provider(command).chat_stream(MESSAGES))[-1]
    assert final["type"] == "done" and final["content"] == "Hello from Claude."
    assert "--mcp-config" not in json.loads(record.read_text())["argv"]


def test_answer_text_always_reaches_the_screen_as_chunks(fake_cli, monkeypatch):
    """Live Astra 2026-09-24: a reply arrived only in the final event, and Astra, which shows
    prose from chunks alone, displayed nothing. Streamed pieces go out as they come and the rest
    at the end, so the chunks always add up to the answer."""
    command, _ = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    events = collect(provider(command).chat_stream(MESSAGES))
    chunks = [e["content"] for e in events if e["type"] == "chunk"]
    assert chunks == ["Hello ", "from ", "Claude."]
    assert "".join(chunks) == events[-1]["content"]
    # Text before tool calls is shown too, even with no partial stream.
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "tools")
    events = collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))
    assert [e["content"] for e in events if e["type"] == "chunk"] == ["Reading it."]


def test_transcript_keeps_tool_results_and_screenshots_in_order():
    png = "data:image/png;base64,iVBORw0KGgo="
    blocks = ccp.transcript([
        {"role": "system", "content": "hidden from the transcript"},
        {"role": "user", "content": "Look at the window"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "computer_snapshot", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": [{"type": "text", "text": "snapshot"}, {"type": "image_url", "image_url": {"url": png}}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}]},
    ])
    texts = [b.get("text", "") for b in blocks]
    assert not any("hidden" in t for t in texts)
    assert any('"name": "mcp__astra__computer_snapshot"' in t for t in texts)
    assert "\n[tool result for call call_1]" in texts
    image = next(b for b in blocks if b["type"] == "image")
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}
    # Remote images are never fetched on the model's behalf.
    assert "[image omitted: not a supported inline image]" in texts


def test_signed_out_cli_explains_how_to_sign_in(fake_cli, monkeypatch):
    command, _ = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "not_logged_in")
    with pytest.raises(ClaudeCodeError) as error:
        collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))
    assert "claude auth login" in error.value.public_message


def test_missing_cli_explains_how_to_install(monkeypatch):
    monkeypatch.setattr(ccp, "claude_command", lambda: None)
    config = LLMConfig(model="sonnet", provider="claude-code", base_url=ccp.BASE_URL)
    with pytest.raises(ClaudeCodeError) as error:
        collect(ClaudeCodeProvider(config).chat_stream(MESSAGES))
    assert "install.sh" in error.value.public_message


def test_idle_cli_is_stopped_with_its_process(fake_cli, monkeypatch):
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "hang")
    with pytest.raises(LLMIdleTimeout):
        collect(provider(command, idle_timeout=1.0).chat_stream(MESSAGES))
    pid = json.loads(record.read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_login_status_reads_only_the_cli_status(fake_cli):
    command, _ = fake_cli
    assert asyncio.run(ccp.login_status(str(command))) == (True, "Claude Code signed in (claude.ai · max)")


def test_route_profile_and_catalog_need_no_api_key(monkeypatch):
    assert "claude-code" in DEFAULT_PROVIDER_REGISTRY.names
    provider_id, record = provider_connections.connection_record("claude-code")
    assert record["auth_mode"] == "oauth" and record["base_url"] == ccp.BASE_URL
    with pytest.raises(ValueError, match="own sign-in"):
        provider_connections.connection_record("claude-code", api_key="sk-x")
    profile = provider_connections.record_profile(provider_id, record)
    assert profile.provider == "claude-code" and "vision" in profile.capabilities
    catalog = asyncio.run(model_catalog.discover_endpoint(
        model_catalog.ProviderEndpoint(provider_id, profile.provider_label, profile, inferred=True), force=True))
    windows = {entry.model_id: entry.profile.context_limit for entry in catalog.entries}
    assert windows == {"fable": 1_000_000, "opus": 1_000_000, "sonnet": 1_000_000, "haiku": 200_000}


def test_connecting_requires_a_signed_in_cli(monkeypatch):
    async def signed_out(*_, **__):
        return False, "Claude Code is not signed in. Run `claude auth login` in a terminal."
    monkeypatch.setattr(ccp, "login_status", signed_out)
    with pytest.raises(ValueError, match="claude auth login"):
        asyncio.run(connections.connect_provider("claude-code"))
