"""Claude subscription through the user's own signed-in claude CLI, against a fake executable."""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import pytest

from agent.cli import connections, model_catalog, provider_connections
from agent.runtime import claude_code_provider as ccp
from agent.runtime.claude_code_provider import ClaudeCodeError, ClaudeCodeProvider, workspace as cli_workspace
from agent.runtime.llm import LLMClient, LLMConfig, LLMIdleTimeout
from agent.runtime.process_env import pid_alive
from agent.runtime.providers import DEFAULT_PROVIDER_REGISTRY
from agent.runtime.react import ReActAgent
from agent.runtime.subagent_resume import UNKNOWN_RESULT

FAKE_CLI = r'''
import json, os, sys, time
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
          "env": {k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "CLAUDE", "MCP_", "ENABLE_TOOL_SEARCH"))}}
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
if os.environ.get("FAKE_CLAUDE_SCENARIO") == "old_flags" and "--thinking-display" in sys.argv:
    # How a CLI without an option exits: a usage error on stderr, nothing on stdout.
    sys.stderr.write("error: unknown option '--thinking-display'\n")
    sys.exit(1)
record["system"] = open(sys.argv[sys.argv.index("--system-prompt-file") + 1]).read()
if "--mcp-config" in sys.argv:
    servers = json.loads(sys.argv[sys.argv.index("--mcp-config") + 1])["mcpServers"]
    record["servers"] = servers
    record["tools"] = json.load(open(servers["astra"]["args"][1]))
scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "tools")
emit = lambda value: (print(json.dumps(value)), sys.stdout.flush())
usage = {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7}
emit({"type": "system", "subtype": "init", "model": os.environ.get("FAKE_CLAUDE_MODEL", "claude-sonnet-5")})
record["frames"] = []
for line in sys.stdin:
    frame = json.loads(line)
    record["frames"].append(frame)
    if frame["type"] == "assistant":
        continue
    if frame.get("shouldQuery") is False and scenario != "answers_history":
        # Appended without a request: a zero-turn result acknowledges it.
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0,
              "usage": {"input_tokens": 0, "output_tokens": 0}})
        continue
    break
record["stdin"] = record["frames"][-1]
json.dump(record, open(os.environ["FAKE_CLAUDE_RECORD"], "w"))
marked = "cache_control" in json.dumps(record["frames"])
if scenario == "answers_history" and record["stdin"].get("shouldQuery") is False:
    # A CLI that ignores shouldQuery answers the first history turn with a request of its own.
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "usage": usage, "result": "Hi."})
    sys.exit(0)
if scenario == "marker_rejected" and marked:
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "API Error: 400 A maximum of 4 blocks with cache_control may be provided. Found 5."}]}})
    emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "usage": usage,
          "result": "API Error: 400 A maximum of 4 blocks with cache_control may be provided. Found 5."})
    sys.exit(0)
if scenario in {"answers_history", "marker_rejected", "old_flags"}:
    scenario = "text"
if scenario == "hang":
    time.sleep(60)
emit({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "Need the file."}]}})
denied = {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "denied"}]}}
if scenario in {"tools", "foreign", "listed"}:
    # "listed": the first tool of the list Claude was given, under the name that list shows.
    name = ({"tools": "mcp__astra__read_file", "foreign": "execute_shell"}.get(scenario)
            or "mcp__astra__" + record["tools"][0]["name"])
    if scenario == "listed":
        emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
                                                 "content_block": {"type": "tool_use", "id": "toolu_1", "name": name}}})
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


@pytest.fixture(autouse=True)
def fresh_provider_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ClaudeCodeProvider, "replay", True)
    monkeypatch.setattr(ClaudeCodeProvider, "cache_marker", True)
    monkeypatch.setattr(ClaudeCodeProvider, "thinking_display", True)
    # What the bridge has already logged in this process; each test starts with nothing logged.
    monkeypatch.setattr(ccp, "_reported", set(), raising=False)
    workdir = tmp_path / "claude-code"
    workdir.mkdir()
    monkeypatch.setattr(ccp, "workspace", lambda: str(workdir))


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude.py"
    path.write_text(FAKE_CLI, encoding="utf-8")
    spawn = asyncio.create_subprocess_exec

    async def run_fake(command, *args, **kwargs):
        # Windows cannot execute a shebang script; keep real pipes/processes on every OS.
        if str(command) == str(path):
            return await spawn(sys.executable, str(path), *args, **kwargs)
        return await spawn(command, *args, **kwargs)

    monkeypatch.setattr(ccp.asyncio, "create_subprocess_exec", run_fake)
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
    # A parent Claude Code session's markers and its own effort must not reach Astra's requests.
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("MCP_CONNECTION_NONBLOCKING", "true")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/profiles/claude")
    collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                 "--include-partial-messages"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "high"
    # Thinking arrives as summaries instead of nothing, so a long think does not look stalled.
    assert argv[argv.index("--thinking-display") + 1] == "summarized"
    # One response, then stop: every call is denied, so the CLI can never run a tool itself.
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--json-schema" not in argv
    [(name, server)] = seen["servers"].items()
    assert name == "astra" and server["command"] == sys.executable and server["alwaysLoad"] is True
    assert server["args"][0].endswith("claude_code_tool_bridge.py")
    assert seen["tools"] == [{"name": "read_file", "description": "Read a file.",
                              "inputSchema": READ_FILE["function"]["parameters"]}]
    # Never a routing variable: only where the CLI keeps its login, all tools up front, and the
    # two switches that keep request prefixes stable for the cache.
    assert seen["env"] == {"ENABLE_TOOL_SEARCH": "false", "CLAUDE_CONFIG_DIR": "/profiles/claude",
                           "CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off", "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}
    assert Path(seen["cwd"]).name == "claude-code"
    assert "You are Astra." in seen["system"] and "Astra runs your tool calls" in seen["system"]
    assert seen["stdin"]["type"] == "user"


def test_bridge_lists_astras_tools_and_never_runs_them(tmp_path):
    tools = ccp.bridge_tools([READ_FILE, {"type": "function", "function": {"name": "bad name!", "parameters": {}}}])
    plain, aliased = tools
    assert plain["name"] == "read_file"
    # A name with characters Claude does not accept is listed under one it does, not left out.
    assert aliased["name"].startswith("bad_name_") and ccp.TOOL_NAME.fullmatch(aliased["name"])
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


@pytest.mark.parametrize("stdio_encoding", ["cp1252", "gbk"])
def test_bridge_passes_descriptions_unchanged_whatever_its_stdio_encoding(tmp_path, stdio_encoding):
    """Windows gives a piped Python process the ANSI code page: cp1252 cannot write Chinese at
    all, and GBK writes bytes the CLI does not read as UTF-8."""
    described = {"type": "function", "function": {
        "name": "apply_patch",
        "description": "应用补丁。\nSecond line — with a dash → and an arrow.\n  *** Begin Patch",
        "parameters": {"type": "object", "properties": {
            "patch": {"type": "string", "description": "多行\n补丁文本"}}, "required": ["patch"]},
    }}
    tools = ccp.bridge_tools([described])
    path = tmp_path / "tools.json"
    path.write_text(json.dumps(tools, ensure_ascii=False), encoding="utf-8")
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "apply_patch", "arguments": {"patch": "补丁"}}}]

    async def run():
        process = await asyncio.create_subprocess_exec(
            sys.executable, ccp.BRIDGE, str(path), env={**os.environ, "PYTHONIOENCODING": stdio_encoding},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await process.communicate(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests).encode("utf-8"))
        return [json.loads(line) for line in out.decode("utf-8").splitlines()]

    replies = asyncio.run(run())
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[0]["result"]["tools"] == tools
    assert replies[0]["result"]["tools"][0]["description"] == described["function"]["description"]
    assert replies[1]["result"]["isError"] is True


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
    assert not pid_alive(pid)


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


HISTORY = [
    {"role": "system", "content": "You are Astra."},
    {"role": "user", "content": "Read a.txt"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "toolu_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]},
    {"role": "tool", "tool_call_id": "toolu_1", "content": "alpha"},
    {"role": "assistant", "content": "It says alpha."},
    {"role": "user", "content": "And b.txt?"},
]


def cache_markers(frames):
    return [(i, block) for i, frame in enumerate(frames)
            for block in frame["message"]["content"] if "cache_control" in block]


def test_history_is_replayed_as_native_turns_with_one_cache_breakpoint(fake_cli, monkeypatch):
    """Live 2026-09-24: sent as one growing message, every step wrote the whole conversation to
    the cache again and read none of it. Replayed turn by turn, a step reads everything but the
    last round; the breakpoint sits on the user turn before the newest one, whose rendering is the
    one that changes once it stops being the turn that asks."""
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    final = collect(provider(command).chat_stream(HISTORY, [READ_FILE], tool_choice="required"))[-1]
    assert final["content"] == "Hello from Claude."
    frames = json.loads(record.read_text())["frames"]
    assert [(f["type"], f.get("shouldQuery")) for f in frames] == [
        ("user", False), ("assistant", None), ("user", False), ("assistant", None), ("user", None)]
    assert frames[1]["message"]["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "mcp__astra__read_file", "input": {"path": "a.txt"}}]
    assert frames[2]["message"]["content"][0]["type"] == "tool_result"
    [(index, block)] = cache_markers(frames)
    assert index == 2 and block["tool_use_id"] == "toolu_1" and block["cache_control"] == ccp.CACHE_MARKER
    # The step's tool choice rides on the newest turn, so the system prompt stays the same.
    assert frames[-1]["message"]["content"][-1]["text"].endswith("You must call at least one tool now.\n</system-reminder>")
    assert "must call" not in json.loads(record.read_text())["system"]


def test_first_turn_has_no_history_and_no_breakpoint(fake_cli, monkeypatch):
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    collect(provider(command).chat_stream(MESSAGES, [READ_FILE]))
    frames = json.loads(record.read_text())["frames"]
    assert len(frames) == 1 and "shouldQuery" not in frames[0] and not cache_markers(frames)


@pytest.mark.parametrize("messages", [MESSAGES, HISTORY], ids=["single turn", "replayed history"])
def test_model_the_alias_resolved_to_is_reported(fake_cli, monkeypatch, messages):
    """Live Astra 2026-09-29: an older CLI still resolved the alias `sonnet` to claude-sonnet-5
    after 5.5 was out, and nothing on screen said so. The CLI's init line names the model; a
    replayed history consumes that line while waiting for its first acknowledgement."""
    command, _ = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    claude = provider(command)
    assert claude.served_model == ""
    collect(claude.chat_stream(messages, [READ_FILE]))
    assert claude.served_model == "claude-sonnet-5"
    monkeypatch.setenv("FAKE_CLAUDE_MODEL", "claude-sonnet-5-5")
    collect(claude.chat_stream(messages, [READ_FILE]))
    assert claude.served_model == "claude-sonnet-5-5"


def test_the_reported_model_never_changes_what_the_next_request_sends(fake_cli, monkeypatch):
    """The prompt cache keys on the request prefix. Knowing the resolved model is display only:
    the system prompt, tools, replayed turns, cache breakpoint, environment and command line of
    the next request are exactly those of the first."""
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    claude = provider(command)

    def sent() -> str:
        collect(claude.chat_stream(HISTORY, [READ_FILE]))
        seen = json.loads(record.read_text())
        seen["pid"] = 0
        # Only the random name of the scratch directory differs between two runs.
        return re.sub(r"astra-claude-code-\w+", "SCRATCH", json.dumps(seen, sort_keys=True))

    first = sent()
    assert claude.served_model == "claude-sonnet-5"
    assert sent() == first
    assert '"cache_control"' in first


@pytest.mark.parametrize("model", ["claude-opus-5[1m]", "claude-sonnet-5-5", "sonnet"])
def test_model_names_the_cli_may_report_are_kept(fake_cli, monkeypatch, model):
    command, _ = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    monkeypatch.setenv("FAKE_CLAUDE_MODEL", model)
    claude = provider(command)
    collect(claude.chat_stream(MESSAGES))
    assert claude.served_model == model


@pytest.mark.parametrize("model", ["claude sonnet", "<b>claude</b>", "-claude", "x" * 200])
def test_an_unusable_model_name_is_not_kept(fake_cli, monkeypatch, model):
    """CLI output is never shown verbatim; only a plain model id reaches the screen."""
    command, _ = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    monkeypatch.setenv("FAKE_CLAUDE_MODEL", model)
    claude = provider(command)
    collect(claude.chat_stream(MESSAGES))
    assert claude.served_model == ""


def test_llm_client_passes_on_the_model_only_where_the_adapter_knows_it(fake_cli, monkeypatch):
    command, _ = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    claude = provider(command)
    client = LLMClient(claude.config, provider=claude)
    assert client.served_model == ""
    collect(client.chat_stream(MESSAGES))
    assert client.served_model == "claude-sonnet-5"
    # Adapters without the notion (OpenAI-compatible endpoints, test doubles) report nothing.
    assert LLMClient(LLMConfig(model="m"), provider=object()).served_model == ""
    assert LLMClient(LLMConfig(model="m"), provider=type("P", (), {"served_model": 5})()).served_model == ""


def test_cli_that_answers_history_falls_back_to_one_transcript_turn(fake_cli, monkeypatch):
    """An older CLI without shouldQuery would spend a request on every history turn: the first
    one it answers switches this process to the single-turn transcript."""
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "answers_history")
    final = collect(provider(command).chat_stream(HISTORY, [READ_FILE]))[-1]
    assert final["content"] == "Hello from Claude."
    frames = json.loads(record.read_text())["frames"]
    assert len(frames) == 1 and frames[0]["message"]["content"][0]["text"].startswith("Conversation so far")
    assert ClaudeCodeProvider.replay is False


def test_rejected_cache_breakpoint_is_dropped_and_the_step_retried(fake_cli, monkeypatch):
    """A CLI that spends all four breakpoints itself makes the API refuse a fifth: the step is
    retried without it and later steps leave it out."""
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "marker_rejected")
    final = collect(provider(command).chat_stream(HISTORY, [READ_FILE]))[-1]
    assert final["content"] == "Hello from Claude."
    frames = json.loads(record.read_text())["frames"]
    assert len(frames) == 5 and not cache_markers(frames)
    assert ClaudeCodeProvider.cache_marker is False


def test_conversation_keeps_calls_valid_across_providers():
    """History from other models can hold ids Claude rejects, ids reused every turn, calls to
    tools not offered now and calls whose result was lost; each still makes a valid request."""
    turns = ccp.conversation([
        {"role": "system", "content": "You are Astra."},
        {"role": "user", "content": "Go"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [
            {"id": "functions.read_file:0", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "call_9", "type": "function", "function": {"name": "gone_tool", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_9", "content": "gone output"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "functions.read_file:0", "type": "function", "function": {"name": "read_file", "arguments": "oops"}}]},
        {"role": "tool", "tool_call_id": "functions.read_file:0", "content": ""},
        {"role": "system", "content": "[USER STEERING] focus"},
    ], frozenset({"read_file"}))
    assert [role for role, _ in turns] == ["user", "assistant", "user", "assistant", "user"]
    first, second = turns[1][1], turns[3][1]
    assert [b.get("id") for b in first if b["type"] == "tool_use"] == ["functions_read_file_0"]
    assert '"name": "mcp__astra__gone_tool"' in first[-1]["text"]
    assert second == [{"type": "tool_use", "id": "functions_read_file_0_2", "name": "mcp__astra__read_file",
                       "input": {"arguments": "oops"}}]
    lost, *rest = turns[2][1]
    assert lost["tool_use_id"] == "functions_read_file_0" and lost["is_error"] is True
    assert [b["text"] for b in rest] == ["[tool result for call call_9]", "gone output"]
    assert turns[4][1][0] == {"type": "tool_result", "tool_use_id": "functions_read_file_0_2",
                              "content": [{"type": "text", "text": "(no output)"}]}
    assert turns[4][1][1]["text"] == "<system-reminder>\n[USER STEERING] focus\n</system-reminder>"
    # Ending on Claude's own words is a prefill, which only the transcript can carry.
    assert ccp.conversation(HISTORY[:5], frozenset({"read_file"})) is None


def test_workspace_is_a_fixed_private_directory_outside_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    first, second = cli_workspace(), cli_workspace()
    assert first == second and Path(first).name == "claude-code" and Path(first).is_dir()
    assert str(tmp_path) in first
    if os.name != "nt":
        assert (Path(first).stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("messages", [MESSAGES, HISTORY], ids=["single turn", "replayed history"])
def test_cli_that_rejects_thinking_display_is_retried_without_it(fake_cli, monkeypatch, messages):
    """A CLI without an option exits before printing anything: the step is retried without
    thinking summaries rather than failing, and later steps leave the option out."""
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "old_flags")
    final = collect(provider(command).chat_stream(messages, [READ_FILE]))[-1]
    assert final["content"] == "Hello from Claude."
    assert "--thinking-display" not in json.loads(record.read_text())["argv"]
    assert ClaudeCodeProvider.thinking_display is False and ClaudeCodeProvider.replay is True


def stored_result(content, call_id="toolu_1"):
    """HISTORY with its one tool result replaced."""
    return [*HISTORY[:3], {"role": "tool", "tool_call_id": call_id, "content": content}, *HISTORY[4:]]


def replayed_result(content) -> dict:
    turns = ccp.conversation(stored_result(content), frozenset({"read_file"}))
    [block] = [b for b in turns[2][1] if b["type"] == "tool_result"]
    return block


FAILED_READ = {"name": "read_file", "tool_output": "", "error": "[ToolError] FileNotFoundError: a.txt",
               "code": "execution_failed", "retryable": False, "recovery_hint": "List the directory first."}


def test_a_failed_tool_result_is_replayed_as_an_error_with_the_same_bytes_every_time(fake_cli, monkeypatch):
    """Astra reports a failed tool as an error with a code, a Retryable line and a Recovery line.
    Claude got that as an ordinary result whose text happened to begin with a status; the block is
    now marked the way Claude's own tools mark a failure. The mark is read from the stored text,
    so a result is replayed with the same bytes on every later request and stays cached."""
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")
    failure = ReActAgent._tool_result_context(FAILED_READ)
    assert "Retryable: no" in failure and "Recovery: List the directory first." in failure

    def sent() -> list[dict]:
        collect(provider(command).chat_stream(stored_result(failure), [READ_FILE]))
        return json.loads(record.read_text())["frames"]

    frames = sent()
    [block] = frames[2]["message"]["content"]
    assert block["type"] == "tool_result" and block["tool_use_id"] == "toolu_1"
    assert block["is_error"] is True
    # Nothing of what the result says is changed or dropped.
    assert block["content"] == [{"type": "text", "text": failure}]
    assert sent() == frames
    # What Claude first got, when this result was the newest turn, is what is replayed.
    collect(provider(command).chat_stream(stored_result(failure)[:4], [READ_FILE]))
    [first] = json.loads(record.read_text())["frames"][-1]["message"]["content"]
    assert {**first, "cache_control": ccp.CACHE_MARKER} == block


@pytest.mark.parametrize("event", [
    {"name": "read_file", "tool_output": "alpha"},
    # A command that ran and exited non-zero is a result of a call that worked.
    {"name": "execute_shell", "tool_output": "[exit code: 1]", "execution": {"status": "completed", "exit_code": 1}},
    {"name": "execute_shell", "tool_output": "partial", "partial": True,
     "execution": {"status": "timed_out", "exit_code": None}},
    {"name": "execute_shell", "tool_output": "", "execution": {"status": "cancelled", "exit_code": None}},
    {"name": "execute_shell", "tool_output": "started", "execution": {"status": "running", "exit_code": None}},
], ids=["success", "failed", "timed_out", "cancelled", "running"])
def test_a_result_the_tool_returned_normally_is_not_marked_as_an_error(event):
    content = ReActAgent._tool_result_context(event)
    assert replayed_result(content) == {"type": "tool_result", "tool_use_id": "toolu_1",
                                        "content": [{"type": "text", "text": content}]}


@pytest.mark.parametrize("content", [
    # Calls the runtime refused to run before it ended the turn.
    "[ToolCircuitOpen] 检测到重复工具调用：read_file",
    "[ToolBudgetExhausted] 工具 read_file 已达到本轮调用上限 3",
    # A worker stores the registry's result as JSON; guidance may follow it.
    json.dumps({"output": "", "error": "[ToolError] FileNotFoundError: a.txt", "code": "execution_failed"}),
    json.dumps({"output": "", "error": "Tool 'read_file' not found"}) + "\n\nProject guidance for this path.",
    UNKNOWN_RESULT,
], ids=["circuit open", "budget exhausted", "worker failure", "worker failure with guidance", "unknown outcome"])
def test_failures_stored_without_astras_result_header_are_marked_too(content):
    block = replayed_result(content)
    assert block["is_error"] is True and block["content"] == [{"type": "text", "text": content}]


@pytest.mark.parametrize("content", [
    json.dumps({"output": "alpha", "error": "", "duration_ms": 3, "risk": "read"}),
    '{"error": "cut off before the closing brace',
    "plain text that mentions [Tool result: read_file | status: error]\nlater on",
    "",
], ids=["worker success", "unreadable json", "header not first", "empty"])
def test_other_stored_results_are_not_marked(content):
    assert "is_error" not in replayed_result(content)


def test_a_failed_result_with_an_image_keeps_both_and_stays_unmarked():
    png = "data:image/png;base64,iVBORw0KGgo="
    block = replayed_result([{"type": "text", "text": ReActAgent._tool_result_context(FAILED_READ)},
                             {"type": "image_url", "image_url": {"url": png}}])
    assert [b["type"] for b in block["content"]] == ["text", "image"] and "is_error" not in block


LONG_NAME = "mcp__design-review-workspace-server__export_annotated_screenshots"
LONG_TOOL = {"type": "function", "function": {
    "name": LONG_NAME, "description": "Export screenshots.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}


def test_a_tool_name_claude_cannot_accept_is_offered_and_called_under_a_stable_alias(fake_cli, monkeypatch):
    """A third-party MCP tool is `mcp__<server>__<tool>` in Astra and gets the bridge's own prefix
    on top. Past 64 characters it was left out of Claude's tool list without a word. It is now
    listed under an alias made from its name alone: Claude's call comes back as the real tool, and
    a replayed call shows the alias the list shows, the same on every request."""
    command, record = fake_cli
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "listed")
    events = collect(provider(command).chat_stream(MESSAGES, [LONG_TOOL, READ_FILE]))
    aliased, plain = json.loads(record.read_text())["tools"]
    alias = aliased["name"]
    assert plain["name"] == "read_file"
    assert alias != LONG_NAME and ccp.TOOL_NAME.fullmatch(alias) and len(ccp.TOOL_PREFIX + alias) <= 64
    assert (aliased["description"], aliased["inputSchema"]) == ("Export screenshots.", LONG_TOOL["function"]["parameters"])
    # Claude calls the alias; Astra gets its own tool's name, while the call streams and at the end.
    assert [c["name"] for c in events[-1]["calls"]] == [LONG_NAME, LONG_NAME]
    previewed = {c["name"] for e in events if e["type"] == "tool_preparing" for c in e["calls"]}
    assert previewed == {LONG_NAME}

    history = [
        *MESSAGES,
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "toolu_1", "type": "function", "function": {"name": LONG_NAME, "arguments": '{"path": "a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "exported"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "Again?"},
    ]
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "text")

    def sent() -> tuple[list[dict], list[dict]]:
        collect(provider(command).chat_stream(history, [LONG_TOOL, READ_FILE]))
        seen = json.loads(record.read_text())
        return seen["tools"], seen["frames"]

    tools, frames = sent()
    assert tools[0]["name"] == alias
    assert frames[1]["message"]["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": ccp.TOOL_PREFIX + alias, "input": {"path": "a.txt"}}]
    assert frames[2]["message"]["content"][0]["tool_use_id"] == "toolu_1"
    assert sent() == (tools, frames)
    # The one-turn fallback and a forced choice name the tool the same way.
    assert any(f'"name": "{ccp.TOOL_PREFIX + alias}"' in b.get("text", "") for b in ccp.transcript(history))
    assert ccp.tool_choice_note({"type": "function", "function": {"name": LONG_NAME}}) == f"You must call `{alias}` now."
    assert ccp.tool_choice_note({"type": "function", "function": {"name": "read_file"}}) == "You must call `read_file` now."


def function_tool(name, description="", **extra):
    return {"type": "function", "function": {"name": name, "description": description, **extra}}


def warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == ccp.logger.name and r.levelno >= logging.WARNING]


def test_names_that_differ_only_where_an_alias_is_cut_stay_apart():
    first, second = ("a" * 40 + middle + "b" * 40 for middle in ("1", "2"))
    one, two = (t["name"] for t in ccp.bridge_tools([function_tool(first), function_tool(second)]))
    assert one != two and ccp.TOOL_NAME.fullmatch(one) and ccp.TOOL_NAME.fullmatch(two)
    assert ccp.ClaudeCodeProvider._call({"id": "t", "name": ccp.TOOL_PREFIX + two, "input": {}},
                                        {one: first, two: second})["name"] == second


def test_a_tool_that_cannot_be_listed_is_reported_by_name(caplog):
    """An alias that is already another tool's name would make one name mean two tools, and a
    tool without a name cannot be called at all: neither is listed, and the log says which."""
    alias = ccp.bridge_name(LONG_NAME)
    tools = [LONG_TOOL, function_tool(alias, "Holds the alias as its own name."),
             {"type": "function", "function": {"description": "No name."}}]
    with caplog.at_level(logging.WARNING, logger=ccp.logger.name):
        listed = ccp.bridge_tools(tools)
        ccp.bridge_tools(tools)
    assert [(t["name"], t["description"]) for t in listed] == [(alias, "Holds the alias as its own name.")]
    # Once per process, however often the same tools are bridged.
    taken, unnamed = sorted(warnings(caplog), key=lambda message: LONG_NAME not in message)
    assert LONG_NAME in taken and alias in taken and "not shown" in taken
    assert "without a name" in unnamed


def test_parameters_that_are_not_an_object_schema_are_not_shown_as_no_parameters(caplog):
    """Claude takes only an object schema. One that is an object schema in all but the missing
    `type` gets it. Any other was replaced by an empty one, which told Claude the tool takes no
    arguments; the description now says the parameters are not listed and carries the schema."""
    properties = {"properties": {"key": {"type": "string"}}, "required": ["key"]}
    union = {"anyOf": [{"type": "object", "properties": {"celsius": {"type": "number"}}},
                       {"type": "object", "properties": {"kelvin": {"type": "number"}}}]}
    tools = [function_tool("lookup", "Look up.", parameters=properties),
             function_tool("convert", "Convert a temperature.", parameters=union),
             function_tool("ping", "Ping.")]
    with caplog.at_level(logging.WARNING, logger=ccp.logger.name):
        lookup, convert, ping = ccp.bridge_tools(tools)
    assert lookup == {"name": "lookup", "description": "Look up.", "inputSchema": {"type": "object", **properties}}
    assert convert["inputSchema"] == {"type": "object", "properties": {}}
    assert convert["description"].startswith("Convert a temperature.\n\n")
    assert "not listed" in convert["description"]
    assert json.dumps(union, sort_keys=True) in convert["description"]
    # A tool that declares no parameters has none: nothing to say.
    assert ping == {"name": "ping", "description": "Ping.", "inputSchema": {"type": "object", "properties": {}}}
    [warning] = warnings(caplog)
    assert "'convert'" in warning and "object schema" in warning
    assert ccp.bridge_tools(tools) == [lookup, convert, ping]


BUILTIN_DESCRIPTION_LIMIT = 2000


def over_long_descriptions(tools: list[dict], limit: int = BUILTIN_DESCRIPTION_LIMIT) -> dict[str, int]:
    return {t["name"]: len(t["description"]) for t in ccp.bridge_tools(tools) if len(t["description"]) > limit}


def test_an_over_long_description_reaches_claude_code_whole_and_is_reported(caplog):
    """Claude Code cuts an MCP tool description past its own cap. Astra cannot see that happen,
    so it sends the text whole and logs which tool is over, with its length."""
    long = "Describes a third-party tool. " + "x" * ccp.DESCRIPTION_LIMIT
    tools = [function_tool("verbose_tool", long), READ_FILE,
             function_tool("exact_tool", "y" * ccp.DESCRIPTION_LIMIT)]
    with caplog.at_level(logging.WARNING, logger=ccp.logger.name):
        listed = ccp.bridge_tools(tools)
    assert [t["description"] for t in listed] == [long, "Read a file.", "y" * ccp.DESCRIPTION_LIMIT]
    [warning] = warnings(caplog)
    assert "'verbose_tool'" in warning and str(len(long)) in warning
    assert over_long_descriptions(tools) == {"verbose_tool": len(long), "exact_tool": ccp.DESCRIPTION_LIMIT}


def builtin_tools(root: Path) -> list[dict]:
    """Astra's own tools as the model is offered them, from the real registration functions."""
    from types import SimpleNamespace

    from agent.channels.tools import register_channel_tools
    from agent.cli.conversation_commands import register_conversation_tools
    from agent.runtime.code_mode import register_run_code_tool
    from agent.runtime.hindsight_provider import HindsightMemoryProvider
    from agent.runtime.memory import MemoryStore
    from agent.runtime.skills import SkillStore
    from agent.runtime.task_store import TaskStore
    from agent.runtime.tools.activity import register_activity_tools
    from agent.runtime.tools.bar import register_bar_tools
    from agent.runtime.tools.browser import register_browser_tools
    from agent.runtime.tools.computer import register_local_computer_runtime
    from agent.runtime.tools.conclave import register_conclave_tools
    from agent.runtime.tools.context_index import register_context_index_tools
    from agent.runtime.tools.delegate import register_delegate_tools
    from agent.runtime.tools.goals import register_goal_tools
    from agent.runtime.tools.hindsight import register_hindsight_tools
    from agent.runtime.tools.image import register_image_tools
    from agent.runtime.tools.memory import register_memory_tools
    from agent.runtime.tools.plans import register_plan_tools
    from agent.runtime.tools.registry import ToolRegistry
    from agent.runtime.tools.session_recall import register_session_recall_tools
    from agent.runtime.tools.skills import register_skill_tools
    from agent.runtime.tools.user_questions import register_user_question_tools
    from agent.runtime.tools.web import register_web_tools
    from agent.runtime.tools.workspace import register_workspace_tools
    from agent.runtime.tools.workspace_dependencies import register_workspace_dependency_tools
    from agent.sandbox.local import LocalSandbox

    async def ask(questions, mode="blocking", **_):
        return {}

    registry = ToolRegistry()
    sandbox = LocalSandbox(timeout=5, workdir=str(root))
    memory, skills = MemoryStore(root / "memory.db"), SkillStore(root / "skills")
    register_workspace_tools(registry, sandbox, workdir=str(root))
    register_user_question_tools(registry, ask)
    register_channel_tools(registry, lambda: None)
    register_memory_tools(registry, memory, session_id=lambda: "default")
    register_goal_tools(registry, TaskStore(root / "tasks.db"), session_id=lambda: "default")
    register_plan_tools(registry, memory, session_id=lambda: "default")
    register_skill_tools(registry, skills)
    register_workspace_dependency_tools(registry)
    register_session_recall_tools(registry)
    register_activity_tools(registry)
    register_context_index_tools(registry, SimpleNamespace(inspect=lambda: ""))
    register_conclave_tools(registry, llm_getter=lambda: None)
    register_delegate_tools(registry, llm_getter=lambda: None, sandbox=sandbox)
    register_web_tools(registry, sandbox)
    register_image_tools(registry, workdir=str(root))
    register_local_computer_runtime(registry, cache_root=root / "computer")  # macOS only
    register_browser_tools(registry, workdir=str(root))
    register_hindsight_tools(registry, HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888", bank_id="test", client=SimpleNamespace()))
    register_run_code_tool(registry, agent_getter=lambda: None)
    register_bar_tools(registry, None)
    register_conversation_tools(SimpleNamespace(
        tools=registry, skill_store=skills, memory_store=memory, task_store=None,
        llm=SimpleNamespace(config=SimpleNamespace(model="test")),
        context=SimpleNamespace(session_path=str(root / "session.json"), persona_id="", messages=[]),
        _refresh_skill_catalog=lambda **_: None))
    # The group activation tool is offered too; its description lists every group's tool names.
    activation = registry.activation_tool_schema()
    return registry.to_openai_tools(names=set(registry.tool_names)) + ([activation] if activation else [])


def test_builtin_tools_reach_claude_unchanged_and_under_the_description_cap(tmp_path, monkeypatch):
    """Claude Code cuts an MCP tool description past its cap (2,048 characters as reported), and
    every Astra tool reaches Claude as an MCP tool. A built-in description that grows past 2,000
    fails here, where its author sees it, and not in a prompt that was cut without notice. Shorten
    the description, or move detail into parameter descriptions or a skill."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "home"))
    tools = builtin_tools(tmp_path)
    functions = [tool["function"] for tool in tools]
    assert {"run_code", "session_search", "memory", "execute_shell", "apply_patch", "read_file", "delegate_task",
            "search_web", "browser_open", "ask_user_question", "skill_manage",
            "activate_tool_group"} <= {f["name"] for f in functions}
    assert over_long_descriptions(tools) == {}
    # The same check does see a description one character over.
    assert over_long_descriptions([function_tool("grown", "x" * (BUILTIN_DESCRIPTION_LIMIT + 1))]) == {
        "grown": BUILTIN_DESCRIPTION_LIMIT + 1}
    # No built-in needs an alias or has parameters Claude cannot take: each is listed as it is.
    assert ccp.bridge_tools(tools) == [
        {"name": f["name"], "description": f["description"], "inputSchema": f["parameters"]} for f in functions]
