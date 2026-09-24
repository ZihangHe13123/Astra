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
from agent.runtime.llm import LLMConfig, LLMIdleTimeout
from agent.runtime.providers import DEFAULT_PROVIDER_REGISTRY

FAKE_CLI = r'''#!__PYTHON__
import json, os, sys, time
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
          "env": {k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))}}
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
if "--system-prompt-file" in sys.argv:
    record["system"] = open(sys.argv[sys.argv.index("--system-prompt-file") + 1]).read()
record["stdin"] = json.loads(sys.stdin.readline())
json.dump(record, open(os.environ["FAKE_CLAUDE_RECORD"], "w"))
scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "tools")
emit = lambda value: (print(json.dumps(value)), sys.stdout.flush())
emit({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
if scenario == "hang":
    time.sleep(60)
emit({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "Need the file."}]}})
usage = {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7}
if scenario == "tools":
    emit({"type": "result", "subtype": "success", "is_error": False, "usage": usage,
          "structured_output": {"content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]}})
elif scenario == "text":
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


def test_tool_call_comes_back_as_an_astra_tool_batch(fake_cli):
    command, _ = fake_cli
    events = collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))
    assert events[0] == {"type": "reasoning", "content": "Need the file."}
    final = events[-1]
    assert final["type"] == "tool_calls" and final["finish_reason"] == "tool_calls"
    [call] = final["calls"]
    assert call["name"] == "read_file" and json.loads(call["arguments"]) == {"path": "a.txt"}
    assert call["id"].startswith("call_")
    assert final["usage"] == {"prompt_tokens": 115, "completion_tokens": 7, "total_tokens": 122,
                              "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 15}


def test_cli_runs_isolated_and_bills_only_the_signed_in_subscription(fake_cli, monkeypatch):
    """No built-in tools, settings, MCP or skills; API-key or endpoint overrides never reach it."""
    command, record = fake_cli
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://redirect.invalid")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    # A provider switcher redirects the CLI's model aliases through these.
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "deepseek-v4-flash")
    collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "high"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["read_file"]
    assert seen["env"] == {}
    assert Path(seen["cwd"]).name.startswith("astra-claude-code-")
    assert "You are Astra." in seen["system"] and '"read_file"' in seen["system"]
    assert seen["stdin"]["type"] == "user"


def test_plain_answer_without_tools_uses_no_schema(fake_cli, monkeypatch):
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    final = collect(provider(command).chat_stream(MESSAGES))[-1]
    assert final["type"] == "done" and final["content"] == "Hello from Claude."
    assert "--json-schema" not in json.loads(record.read_text())["argv"]


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
    assert any('"name": "computer_snapshot"' in t for t in texts)
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
    assert {entry.model_id for entry in catalog.entries} >= {"opus", "sonnet", "haiku"}


def test_connecting_requires_a_signed_in_cli(monkeypatch):
    async def signed_out(*_, **__):
        return False, "Claude Code is not signed in. Run `claude auth login` in a terminal."
    monkeypatch.setattr(ccp, "login_status", signed_out)
    with pytest.raises(ValueError, match="claude auth login"):
        asyncio.run(connections.connect_provider("claude-code"))
